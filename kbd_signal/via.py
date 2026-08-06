"""VIA raw HID protocol layer for VIA-compatible RGB keyboards.

Defaults target the Keychron K8 Pro (custom channel 3 from
keyboards/keychron/k8_pro/via_json/k8_pro_ansi_rgb.json:
brightness=1, effect=2, speed=3, color(hue,sat)=4); vendor id, product
filter, v3 channel and effect indices are all overridable via
config.json — see kbd_signal.config.

All writes are RAM-only (id_custom_set_value). id_custom_save (0x09) is
deliberately never sent, so a power cycle always restores the user's
persisted settings and the EEPROM is never worn.
"""

import time

from . import config

# `import hid` is deferred into the functions below: importing this module
# must stay cheap for the hot no-op hook path (no hidapi DLL load).

USAGE_PAGE = 0xFF60  # QMK raw HID
USAGE = 0x61

CMD_GET_PROTOCOL_VERSION = 0x01
CMD_CUSTOM_SET = 0x07  # VIA v2: id_lighting_set_value (same byte)
CMD_CUSTOM_GET = 0x08

# Logical value ids -> (VIA v2 lighting id, VIA v3 rgb_matrix channel id).
# Shipped K8 Pro stock firmware speaks VIA protocol 9 (v2): no channel byte,
# lighting ids 0x80-0x83. Newer firmware (protocol >= 11) uses custom
# channel 3 with ids 1-4. Detected at open time via 0x01.
VALUE_BRIGHTNESS = "brightness"
VALUE_EFFECT = "effect"
VALUE_SPEED = "speed"
VALUE_COLOR = "color"  # hue, sat

_VALUE_IDS = {
    VALUE_BRIGHTNESS: (0x80, 1),
    VALUE_EFFECT: (0x81, 2),
    VALUE_SPEED: (0x82, 3),
    VALUE_COLOR: (0x83, 4),
}

REPORT_SIZE = 32  # QMK RAW_EPSIZE; hidapi pads to the actual report length


class DeviceNotFound(Exception):
    pass


def enumerate_raw_hid(vendor_id=None):
    """All raw-HID (0xFF60) interfaces, optionally filtered by VID.
    `kbd-signal detect --all` uses vendor_id=None to help users of other
    keyboards find their VID/PID."""
    import hid
    return [
        d for d in hid.enumerate(vendor_id or 0)
        if d.get("usage_page") == USAGE_PAGE and d.get("usage") == USAGE
    ]


def find_device_path(dev_cfg=None):
    dev_cfg = dev_cfg or config.device()
    candidates = enumerate_raw_hid(dev_cfg["vendor_id"])
    if dev_cfg.get("product_id") is not None:
        candidates = [d for d in candidates
                      if d["product_id"] == dev_cfg["product_id"]]
    if not candidates:
        raise DeviceNotFound(
            f"raw HID interface not found for VID "
            f"{dev_cfg['vendor_id']:#06x} (wired USB required)")
    # Prefer the configured product substring if several boards are attached
    match = dev_cfg.get("product_match")
    if match:
        for d in candidates:
            if match in (d.get("product_string") or ""):
                return d["path"]
    return candidates[0]["path"]


def verify_channel(kb):
    """Round-trip the configured v3 channel: do writes actually land?

    A GET being answered is not proof. Measured on a Keychron Q1 HE 8K
    (protocol 13, rgb_matrix on channel 3): channel 0 answers a brightness GET
    with `[0]` while channels 5 and 7 stay silent. So a read-only probe can
    accept a channel that drives nothing, and because set_value ignores a
    missing echo every later write would then vanish without a word.

    Probes SPEED, not brightness. A VIA v3 brightness read-back cannot equal what
    was written: quantum/via.c stores scale8(value, RGB_MATRIX_MAXIMUM_BRIGHTNESS)
    -- an (i * sc) / 256 divide -- and reads back val * 255 / MAXIMUM_BRIGHTNESS,
    so the round trip is not the identity by construction (#56). Measured on that
    board: 44 -> 42, 120 -> 119, 52 -> 52, exact only at 0 and 255. Speed and
    color have no such scaling and round-trip exactly. Speed is also invisible on
    a static effect, so a board mid-probe does not flicker.

    This verifies rather than searches. An earlier version walked candidate
    channels writing to each, which the simulator was happy with and real
    hardware was not: probing channel 0 left that board at effect 5 /
    brightness 255 (a write to an unhandled channel is not inert -- the value id
    means something else there) and wedged the HID handle into `read error`.
    Guessing by writing to arbitrary channels is not worth it when the value is
    published in the board's VIA definition as id_qmk_rgb_matrix_channel.

    Restores the speed it probed with on every path that read the original --
    the caller snapshots *after* this, so a leftover probe value would be
    captured as the user's baseline. The probe
    value is never 0, because a channel that echoes the request back reads as
    zeros and would otherwise confirm itself.
    """
    before = None
    try:
        before = kb.get_value(VALUE_SPEED, tries=2)[0]
        probe = 1 if before != 1 else 2
        kb.set_value(VALUE_SPEED, probe)
        return kb.get_value(VALUE_SPEED, tries=2)[0] == probe
    except OSError:
        return False
    finally:
        # Restore on every path that got as far as reading the original,
        # including the one where the confirming read raised.
        if before is not None:
            try:
                kb.set_value(VALUE_SPEED, before)
            except OSError:
                pass


# Seed values for probe_reset_on_effect: a hue that is not the reset's hue 0
# and a brightness that is not the reset's full 255, so the snap-back is
# distinguishable from what was on the board already.
PROBE_HUE = 85     # green
PROBE_BRIGHTNESS = 120


def probe_reset_on_effect(kb, effect, window=0.3, settle=0.02):
    """Whether this firmware forces color/brightness shortly after an EFFECT
    change -- the quirk `reset_on_effect` compensates for (see set_color).

    Seeds a known state, changes the effect, then watches for the reset's
    signature (hue snapping to 0 or brightness to full) for `window` seconds.
    `effect` must differ from the current one or nothing triggers.
    """
    kb.set_value(VALUE_BRIGHTNESS, PROBE_BRIGHTNESS)
    kb.set_value(VALUE_COLOR, PROBE_HUE, 255)
    kb.set_value(VALUE_EFFECT, effect)
    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        time.sleep(settle)
        try:
            hue = kb.get_value(VALUE_COLOR, 2)[0]
            brightness = kb.get_value(VALUE_BRIGHTNESS)[0]
        except OSError:
            continue  # a dropped read is not evidence either way
        if hue == 0 or brightness == 255:
            return True
    return False


class Keyboard:
    def __init__(self, dev_cfg=None):
        import hid
        self._cfg = dev_cfg or config.device()
        self._channel = self._cfg["v3_channel"]
        self._dev = hid.device()
        self._dev.open_path(find_device_path(self._cfg))
        # Per-device quirk flag (config, default off): keyboards whose firmware
        # resets color/brightness shortly after an EFFECT change opt into the
        # dark-hold workaround below. Off by default — most boards don't need it.
        self._reset_on_effect = bool(self._cfg.get("reset_on_effect", False))
        self.protocol = self._probe_protocol()
        self._v3 = self.protocol >= 11

    def _probe_protocol(self):
        resp = self._request(CMD_GET_PROTOCOL_VERSION, match=1)
        if resp is None:
            raise IOError("protocol probe failed: no matching response")
        return (resp[1] << 8) | resp[2]

    def _drain(self):
        """Discard pending input reports. Windows delivers HID input reports
        to every open handle of the collection, so echoes of commands sent by
        a concurrently running kbd-signal process (hooks) land here too.
        Note: read(size, 0) means *blocking* in cython-hidapi, so use a 1 ms
        timeout for the non-blocking sweep."""
        while self._dev.read(64, 1):
            pass

    def _request(self, *payload, tries=6, match=None):
        """Write a command and read until a response echoing the first
        `match` payload bytes arrives, discarding unrelated echoes from
        concurrent processes. Returns None on timeout."""
        if match is None:
            match = len(payload)
        self._drain()
        self._write(*payload)
        want = list(payload[:match])
        for _ in range(tries):
            resp = self._dev.read(64, 250)
            if resp and list(resp[:match]) == want:
                return resp
        return None

    def close(self):
        self._dev.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _write(self, *payload):
        # Leading 0x00 = report id (Windows requires it explicitly)
        packet = [0x00] + list(payload)
        packet += [0x00] * (1 + REPORT_SIZE - len(packet))
        if self._dev.write(bytes(packet)) < 0:
            raise IOError("HID write failed")

    def set_value(self, value_id, *data):
        v2_id, v3_id = _VALUE_IDS[value_id]
        # _request also consumes the firmware's echo of this SET, keeping the
        # input queue clean for later reads. A missed echo is not fatal.
        if self._v3:
            self._request(CMD_CUSTOM_SET, self._channel, v3_id, *data,
                          tries=2, match=3)
        else:
            self._request(CMD_CUSTOM_SET, v2_id, *data, tries=2, match=2)

    def get_value(self, value_id, length=1, tries=6):
        v2_id, v3_id = _VALUE_IDS[value_id]
        if self._v3:
            resp = self._request(CMD_CUSTOM_GET, self._channel, v3_id,
                                 tries=tries)
        else:
            resp = self._request(CMD_CUSTOM_GET, v2_id, tries=tries)
        if resp is None:
            raise IOError(f"no response for value {value_id}")
        offset = 3 if self._v3 else 2
        return list(resp[offset:offset + length])

    # -- high level -------------------------------------------------

    def snapshot(self):
        return {
            "brightness": self.get_value(VALUE_BRIGHTNESS)[0],
            "effect": self.get_value(VALUE_EFFECT)[0],
            "speed": self.get_value(VALUE_SPEED)[0],
            "color": self.get_value(VALUE_COLOR, 2),  # [hue, sat]
        }

    # Some firmware, ~50-150 ms *after* an EFFECT change, performs a reset that
    # forces BOTH the color (to hue 0) and the brightness (to full). Writing the
    # color once loses to it (`done` stays red); settling the color at full
    # brightness flashes that red on screen. set_color therefore holds the LEDs
    # dark while it settles the color across the reset window: it keeps
    # rewriting brightness=0 and the color, so the reset's full-brightness red is
    # overwritten within one write cycle (never visibly shown), then confirms
    # the color once the window has passed. The caller raises brightness
    # afterwards, on the already-settled color.
    #
    # The dark hold is a per-device workaround gated on `reset_on_effect`
    # (config). Only boards whose firmware shows this quirk enable it; every
    # other board uses hold=0 and simply confirms the color with no dark dip.
    COLOR_HOLD = 0.2     # blast dark past the observed ~150 ms reset window
    COLOR_SETTLE = 0.03  # read-back cadence once the window has passed
    COLOR_BUDGET = 1.5   # hard ceiling; stays well under the 5 s hook timeout
    _READ_TIMEOUT = 0.25  # per-attempt HID read timeout (see _request)

    def set_color(self, hue, sat, hold=None, settle=COLOR_SETTLE,
                  budget=COLOR_BUDGET):
        """Settle the color to (hue, sat) while keeping the LEDs dark, defeating
        the delayed post-effect reset without ever showing its red. Leaves
        brightness at 0 (the caller raises it on the settled color). Never
        raises — write/read errors count as a miss — so a hook always exits
        cleanly. Returns True once the color reads back correct, False if it
        gave up within `budget` (the caller logs that).

        `hold` defaults to the device gate: COLOR_HOLD only when the firmware is
        known to reset (reset_on_effect), else 0. The read-back is capped to the
        remaining budget so an unresponsive get can't blow past `budget`."""
        if hold is None:
            hold = self.COLOR_HOLD if self._reset_on_effect else 0.0
        deadline = time.monotonic() + budget
        hold_until = time.monotonic() + hold
        while time.monotonic() < deadline:
            try:
                self.set_value(VALUE_BRIGHTNESS, 0)
                self.set_value(VALUE_COLOR, hue, sat)
            except OSError:
                pass
            if time.monotonic() < hold_until:
                continue  # blast through the reset window before trusting a read
            time.sleep(settle)
            # Cap read attempts to what the budget still allows — each blocks up
            # to _READ_TIMEOUT — so a no-response get can't overshoot `budget`.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            tries = max(1, min(6, int(remaining / self._READ_TIMEOUT)))
            try:
                if self.get_value(VALUE_COLOR, 2, tries=tries) == [hue, sat]:
                    return True
            except OSError:
                pass
        return False

    # Some firmware silently reverts a brightness write that follows an
    # effect change: the SET is ACKed, but a later read shows the pre-change
    # value (#34, measured on a K8 Pro — restore('off') ended at 255 three
    # times, while a bare 255->0 write stuck 3/3). The cause is not fully
    # explained; write-and-verify wins regardless.
    BRIGHTNESS_SETTLE = 0.1  # rewrite/read-back cadence
    BRIGHTNESS_HOLD = 0.4    # reverts measured as late as ~300 ms (#34)

    def settle_brightness(self, value, settle=BRIGHTNESS_SETTLE,
                          hold=BRIGHTNESS_HOLD, budget=COLOR_BUDGET):
        """Write brightness until it sticks, within `budget` seconds.

        On VIA v3 there is nothing to confirm against, so the rewrites *are* the
        protection and the loop stops once the window has passed. VIA scales
        brightness through the firmware's internal limit in both directions, and
        on the rgb_matrix channel the two directions do not use the same divisor
        (upstream QMK, quantum/via.c):

            set: scale8(value, RGB_MATRIX_MAXIMUM_BRIGHTNESS)   # (i * sc) / 256
            get: rgb_matrix_get_val() * 255 / RGB_MATRIX_MAXIMUM_BRIGHTNESS

        so the round trip is not the identity by construction. Measured on a
        Q1 HE 8K (protocol 13): 44 -> 42, 120 -> 119, 52 -> 52, with 0 and 255
        exact -- deterministic, stable across repeated writes and over a second
        of reads, all 8 bits of resolution preserved, so neither a settling lag
        nor quantisation. An equality check there can only ever burn its budget:
        1.50 s versus 0.43 s, measured, while _restore_locked holds the state
        lock, and it logs a failure that is not one.

        On VIA v2 the read-back is kept: both directions scale by
        RGBLIGHT_LIMIT_VAL / 255, which is lossless at QMK's default limit of
        255 -- and v2 is where the #34 revert was measured (a K8 Pro on stock
        protocol-9 firmware, this project's default target), so the confirmation
        demonstrably works there.

        ponytail: a v2 board that lowers RGBLIGHT_LIMIT_VAL is lossy too and
        would burn the budget like v3 does. Gate on the measured limit instead
        of the protocol if such a board is ever reported.

        A read inside the revert window is not proof:
        the firmware can confirm the new value at ~100 ms and still snap
        it back at ~150 ms, with reverts measured as late as ~300 ms
        (#34) — so for the first `hold` seconds the loop only rewrites,
        and confirmations count solely after the window has passed. The
        hold is unconditional (not gated on reset_on_effect) because the
        board it was measured on ships with the quirk flag off. Same
        contract as set_color: never raises (errors count as a miss),
        read tries are capped by the remaining budget, and it returns
        False when it gave up (the caller logs that)."""
        deadline = time.monotonic() + budget
        hold_until = time.monotonic() + hold
        while True:
            # Check the deadline right before the write — a blocking SET can
            # itself take two read timeouts, so an iteration entered "just in
            # time" could otherwise overshoot the advertised budget by most
            # of a write. One in-flight write may still exceed it; nothing
            # further is started past the deadline (#36 review).
            if time.monotonic() >= deadline:
                break
            try:
                self.set_value(VALUE_BRIGHTNESS, value)
            except OSError:
                pass
            time.sleep(min(settle, max(0.0, deadline - time.monotonic())))
            if time.monotonic() < hold_until:
                continue  # rewrite across the revert window; don't trust reads
            if self._v3:
                # Nothing to compare a read against (see above). The rewrites
                # have outlived the revert window, which is the protection #34
                # actually added; the read-back only decided when to stop.
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            tries = max(1, min(6, int(remaining / self._READ_TIMEOUT)))
            try:
                if self.get_value(VALUE_BRIGHTNESS, tries=tries) == [value]:
                    return True
            except OSError:
                pass
        return False

    def apply(self, effect=None, hue=None, sat=255, speed=None, brightness=None):
        """Apply a lighting pattern. Returns whether the color was confirmed
        (True when there is no color to set, or the device needs no workaround).

        On a reset-prone device the writes are ordered brightness=0 -> effect ->
        color-settled-dark -> brightness, so the reset can never land while the
        LEDs are bright, and brightness is raised ONLY once the color is
        confirmed — a failed settle leaves the LEDs dark (never the reset's red)
        and returns False for the caller to log."""
        if not self._reset_on_effect:
            # No delayed post-effect reset: write directly, no dark hold.
            if effect is not None:
                self.set_value(VALUE_EFFECT, effect)
            if speed is not None:
                self.set_value(VALUE_SPEED, speed)
            if hue is not None:
                self.set_value(VALUE_COLOR, hue, sat)
            if brightness is not None:
                self.set_value(VALUE_BRIGHTNESS, brightness)
            return True
        settling = hue is not None
        if settling:
            # Drop dark BEFORE the effect change so the reset can't flash bright.
            self.set_value(VALUE_BRIGHTNESS, 0)
        if effect is not None:
            self.set_value(VALUE_EFFECT, effect)
        if speed is not None:
            self.set_value(VALUE_SPEED, speed)
        ok = True
        if settling:
            ok = self.set_color(hue, sat)
        if brightness is not None and ok:
            self.set_value(VALUE_BRIGHTNESS, brightness)
        return ok

    def apply_snapshot(self, snap):
        # Same ordering/gating as apply(); route through it so the device gate
        # and the brightness-only-if-confirmed rule apply to restore too.
        hue, sat = snap["color"]
        return self.apply(effect=snap["effect"], hue=hue, sat=sat,
                          speed=snap["speed"], brightness=snap["brightness"])
