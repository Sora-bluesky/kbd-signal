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
