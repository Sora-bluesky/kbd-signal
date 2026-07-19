"""VIA v3 raw HID protocol layer for Keychron K8 Pro (stock firmware).

Speaks the QMK RGB Matrix custom channel (id 3) defined in
keyboards/keychron/k8_pro/via_json/k8_pro_ansi_rgb.json:
  brightness=1, effect=2, speed=3, color(hue,sat)=4

All writes are RAM-only (id_custom_set_value). id_custom_save (0x09) is
deliberately never sent, so a power cycle always restores the user's
persisted settings and the EEPROM is never worn.
"""

import hid

VENDOR_ID = 0x3434  # Keychron
USAGE_PAGE = 0xFF60  # QMK raw HID
USAGE = 0x61

CMD_GET_PROTOCOL_VERSION = 0x01
CMD_CUSTOM_SET = 0x07  # VIA v2: id_lighting_set_value (same byte)
CMD_CUSTOM_GET = 0x08

CHANNEL_RGB_MATRIX = 3  # v3 only

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

# Effect indices from the official via_json (stock firmware)
EFFECT_NONE = 0
EFFECT_SOLID_COLOR = 1
EFFECT_BREATHING = 2

REPORT_SIZE = 32  # QMK RAW_EPSIZE; hidapi pads to the actual report length


class DeviceNotFound(Exception):
    pass


def find_device_path():
    candidates = [
        d for d in hid.enumerate(VENDOR_ID)
        if d.get("usage_page") == USAGE_PAGE and d.get("usage") == USAGE
    ]
    if not candidates:
        raise DeviceNotFound(
            "Keychron raw HID interface not found (wired USB required)")
    # Prefer a K8 in case multiple Keychron boards are attached
    for d in candidates:
        if "K8" in (d.get("product_string") or ""):
            return d["path"]
    return candidates[0]["path"]


class Keyboard:
    def __init__(self):
        self._dev = hid.device()
        self._dev.open_path(find_device_path())
        self.protocol = self._probe_protocol()
        self._v3 = self.protocol >= 11

    def _probe_protocol(self):
        self._write(CMD_GET_PROTOCOL_VERSION)
        resp = self._dev.read(64, 500)
        if not resp or resp[0] != CMD_GET_PROTOCOL_VERSION:
            raise IOError(f"protocol probe failed: {resp!r}")
        return (resp[1] << 8) | resp[2]

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
        if self._v3:
            self._write(CMD_CUSTOM_SET, CHANNEL_RGB_MATRIX, v3_id, *data)
        else:
            self._write(CMD_CUSTOM_SET, v2_id, *data)

    def get_value(self, value_id, length=1):
        v2_id, v3_id = _VALUE_IDS[value_id]
        if self._v3:
            self._write(CMD_CUSTOM_GET, CHANNEL_RGB_MATRIX, v3_id)
        else:
            self._write(CMD_CUSTOM_GET, v2_id)
        resp = self._dev.read(64, 500)
        if not resp or resp[0] != CMD_CUSTOM_GET:
            raise IOError(f"unexpected response for value {value_id}: {resp!r}")
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

    def apply(self, effect=None, hue=None, sat=255, speed=None, brightness=None):
        if effect is not None:
            self.set_value(VALUE_EFFECT, effect)
        if hue is not None:
            self.set_value(VALUE_COLOR, hue, sat)
        if speed is not None:
            self.set_value(VALUE_SPEED, speed)
        if brightness is not None:
            self.set_value(VALUE_BRIGHTNESS, brightness)

    def apply_snapshot(self, snap):
        self.set_value(VALUE_EFFECT, snap["effect"])
        self.set_value(VALUE_COLOR, *snap["color"])
        self.set_value(VALUE_SPEED, snap["speed"])
        self.set_value(VALUE_BRIGHTNESS, snap["brightness"])
