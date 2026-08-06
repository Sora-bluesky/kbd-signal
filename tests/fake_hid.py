"""A VIA raw-HID device simulator standing in for `hid.device()`.

The rest of the suite mocks `Keyboard.set_value` / `get_value`, i.e. it replaces
the layer *above* the protocol. That leaves the protocol itself unverified: the
report framing, the v2-vs-v3 command shape, the value-id mapping, the protocol
probe's parsing, the read offsets, and the echo draining. Every hardware bug
this project has shipped (#25, #31, #32, #34, #42, #45) reached a user's desk
with CI green.

This speaks the protocol at the byte level instead of replaying a recorded
transcript: a recording only protects the path that was recorded, and capturing
one needs real hardware, which CI does not have.

Strict on purpose -- a malformed report raises rather than being tolerated, so
any framing regression fails loudly in whatever test happens to be running.
"""

import contextlib
import sys
import types
from unittest import mock

from kbd_signal import via

# How many data bytes each value carries; everything but the hue/sat pair is one.
_LENGTHS = {via.VALUE_COLOR: 2}

# Reverse of via._VALUE_IDS, per protocol generation.
_V2_NAMES = {v2: name for name, (v2, _) in via._VALUE_IDS.items()}
_V3_NAMES = {v3: name for name, (_, v3) in via._VALUE_IDS.items()}

DEFAULT_ENTRY = {
    "vendor_id": 0x3434,
    "product_id": 0x1012,
    "path": b"/fake/kbd",
    "product_string": "Fake VIA Board",
    "manufacturer_string": "Fake",
    "usage_page": via.USAGE_PAGE,
    "usage": via.USAGE,
}


class FakeViaDevice:
    """Implements the slice of hid.device() that via.Keyboard uses.

    protocol      9 for VIA v2 framing, >= 11 for v3 (channel byte present).
    quirk_after   arm the post-effect reset: this many writes after an EFFECT
                  set, the stored color snaps to hue 0 and brightness to full,
                  once. Counted in writes rather than seconds so the retry loop
                  in set_color is exercised deterministically, with no clock.
    """

    def __init__(self, protocol=13, channel=3, quirk_after=None, values=None,
                 max_brightness=None, echo_unknown_channel=False):
        # echo_unknown_channel is the behaviour that makes verify_channel
        # necessary and that silence cannot express: a request for a channel the
        # board does not implement comes back echoed, data bytes zeroed, so the
        # channel looks alive to anything that only reads. Measured on a
        # Q1 HE 8K whose rgb_matrix is channel 3: channels 5 and 7 stay silent,
        # but channel 0 answers a brightness GET with [0].
        #
        # Scoped to exactly that: an unknown *value id* on the right channel
        # stays silent either way, because no measurement says otherwise and a
        # model should not invent behaviour it has not seen. VIA v2 has no
        # channel byte, so the flag does nothing there.
        self.echo_unknown_channel = echo_unknown_channel
        # max_brightness models VIA's lossy brightness round-trip: quantum/via.c
        # stores scale8(value, RGB_MATRIX_MAXIMUM_BRIGHTNESS) -- an (i * sc) / 256
        # divide -- and reads back val * 255 / MAXIMUM_BRIGHTNESS. The divisors
        # differ, so a written brightness does not come back. None keeps the
        # round trip exact, which is what VIA v2 does (both directions scale by
        # RGBLIGHT_LIMIT_VAL / 255, lossless at QMK's default limit).
        self.max_brightness = max_brightness
        self.protocol = protocol
        self.channel = channel
        self.values = {via.VALUE_BRIGHTNESS: 200, via.VALUE_EFFECT: 6,
                       via.VALUE_SPEED: 128, via.VALUE_COLOR: [142, 255]}
        self.values.update(values or {})
        self.packets = []      # every raw packet written, for framing assertions
        self.reads = 0
        self.opened = None
        self.closed = False
        self._queue = []
        self._quirk_after = quirk_after
        self._countdown = None

    # -- hid.device() surface ---------------------------------------

    def open_path(self, path):
        self.opened = path

    def close(self):
        self.closed = True

    def write(self, packet):
        packet = list(packet)
        if len(packet) != 1 + via.REPORT_SIZE:
            raise ValueError(
                f"report is {len(packet)} bytes, expected {1 + via.REPORT_SIZE}")
        if packet[0] != 0x00:
            raise ValueError(f"missing report id: first byte {packet[0]:#04x}")
        self.packets.append(packet)
        self._dispatch(packet[1:])
        self._tick()
        return len(packet)

    def read(self, size, timeout=0):
        self.reads += 1
        return self._queue.pop(0) if self._queue else []

    # -- protocol ---------------------------------------------------

    @property
    def _v3(self):
        return self.protocol >= 11

    def _decode(self, payload):
        """(value name, data bytes, response header) for a SET/GET payload, or
        None when the request is not for this device's channel."""
        if self._v3:
            # The channel was checked by the caller.
            name = _V3_NAMES.get(payload[2])
            header, rest = payload[:3], payload[3:]
        else:
            name = _V2_NAMES.get(payload[1])
            header, rest = payload[:2], payload[2:]
        if name is None:
            return None
        return name, rest[:_LENGTHS.get(name, 1)], list(header)

    def _dispatch(self, payload):
        cmd = payload[0]
        if cmd == via.CMD_GET_PROTOCOL_VERSION:
            self._queue.append([cmd, self.protocol >> 8, self.protocol & 0xFF])
            return
        if (cmd in (via.CMD_CUSTOM_SET, via.CMD_CUSTOM_GET)
                and self._v3 and payload[1] != self.channel):
            if self.echo_unknown_channel:
                # The header matches what _request waits for, so the client
                # reads this as a valid response carrying zeros.
                self._queue.append(list(payload[:3]) + [0, 0])
            return
        decoded = self._decode(payload)
        if decoded is None:
            return  # unknown value id: silence, on either generation
        name, data, header = decoded
        if cmd == via.CMD_CUSTOM_SET:
            if name == via.VALUE_COLOR:
                self.values[name] = list(data)
            elif name == via.VALUE_BRIGHTNESS and self.max_brightness:
                self.values[name] = (data[0] * self.max_brightness) // 256
            else:
                self.values[name] = data[0]
            self._queue.append(header + list(data))  # firmware echoes the SET
            if name == via.VALUE_EFFECT and self._quirk_after is not None:
                self._countdown = self._quirk_after
        elif cmd == via.CMD_CUSTOM_GET:
            stored = self.values[name]
            if name == via.VALUE_BRIGHTNESS and self.max_brightness:
                stored = (stored * 255) // self.max_brightness
            stored = list(stored) if isinstance(stored, list) else [stored]
            self._queue.append(header + stored)

    def _tick(self):
        """Land the armed post-effect reset. Applied after the write that
        exhausts the countdown, so a color written by that same write is
        clobbered -- the race set_color exists to defeat."""
        if self._countdown is None:
            return
        self._countdown -= 1
        if self._countdown > 0:
            return
        self._countdown = None
        self.values[via.VALUE_COLOR] = [0, self.values[via.VALUE_COLOR][1]]
        self.values[via.VALUE_BRIGHTNESS] = 255


@contextlib.contextmanager
def attached(device, entry=None):
    """Install a stub `hid` module so via.Keyboard() opens `device`.

    A stub module rather than patching the real `hid` attributes: these tests
    then need no hidapi runtime at all, which keeps them honest on CI runners
    with no HID stack.
    """
    stub = types.ModuleType("hid")
    stub.device = lambda: device
    stub.enumerate = lambda vendor_id=0: [entry or DEFAULT_ENTRY]
    with mock.patch.dict(sys.modules, {"hid": stub}):
        yield device
