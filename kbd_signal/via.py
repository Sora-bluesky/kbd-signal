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

    def get_value(self, value_id, length=1):
        v2_id, v3_id = _VALUE_IDS[value_id]
        if self._v3:
            resp = self._request(CMD_CUSTOM_GET, self._channel, v3_id)
        else:
            resp = self._request(CMD_CUSTOM_GET, v2_id)
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

    def set_color(self, hue, sat, tries=5, settle=0.12):
        """Write hue/sat and confirm it stuck via read-back, retrying if not.

        Some firmware resets the color to hue 0 roughly 50-150 ms *after* an
        EFFECT change, wiping any color written in between, so `done` (green)
        would come up red (observed on a Keychron Q1 HE 8K). A single write —
        even reordered after the effect — loses to that delayed reset. Writing,
        waiting for the reset to land, then re-reading and rewriting converges:
        once the reset has passed, the next write sticks."""
        for _ in range(tries):
            self.set_value(VALUE_COLOR, hue, sat)
            time.sleep(settle)  # let a pending effect-triggered reset land
            try:
                if self.get_value(VALUE_COLOR, 2) == [hue, sat]:
                    return
            except IOError:
                pass

    def apply(self, effect=None, hue=None, sat=255, speed=None, brightness=None):
        # Color is written LAST (after the effect change that can reset it) and
        # via set_color, which verifies the write landed. See set_color.
        if effect is not None:
            self.set_value(VALUE_EFFECT, effect)
        if speed is not None:
            self.set_value(VALUE_SPEED, speed)
        if brightness is not None:
            self.set_value(VALUE_BRIGHTNESS, brightness)
        if hue is not None:
            self.set_color(hue, sat)

    def apply_snapshot(self, snap):
        # Color last and verified — same firmware quirk as apply().
        self.set_value(VALUE_EFFECT, snap["effect"])
        self.set_value(VALUE_SPEED, snap["speed"])
        self.set_value(VALUE_BRIGHTNESS, snap["brightness"])
        self.set_color(*snap["color"])
