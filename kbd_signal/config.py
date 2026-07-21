"""User configuration (config.json in the platform state dir; the dir
itself is resolved per-OS by kbd_signal._platform.state_dir).


Defaults target the Keychron K8 Pro; every device-specific constant can be
overridden so other VIA-compatible RGB keyboards work without code changes:

  {
    "restore": "off",                  // or "baseline" (default)
    "device": {
      "vendor_id": "0x3434",           // USB VID (int or hex string)
      "product_id": null,              // optional exact PID filter
      "product_match": "K8",           // preferred product-string substring
      "v3_channel": 3,                 // VIA v3 custom channel for rgb_matrix
      "effects": {"solid": 1, "breathing": 2}
    }
  }

Workflow for a new keyboard: `kbd-signal detect --all` to find VID/PID,
then `kbd-signal raw-effect <n>` to probe its effect indices.
"""

import json
import os

from . import _platform

STATE_DIR = _platform.state_dir()
CONFIG_FILE = os.path.join(STATE_DIR, "config.json")

DEFAULT_DEVICE = {
    "vendor_id": 0x3434,   # Keychron
    "product_id": None,
    "product_match": "K8",
    "v3_channel": 3,       # id_qmk_rgb_matrix_channel in Keychron via_json
    "effects": {"solid": 1, "breathing": 2},
}


def _to_int(value):
    if isinstance(value, str):
        return int(value, 0)  # accepts "0x3434" and "13364"
    return value


def load():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    device = {**DEFAULT_DEVICE, **cfg.get("device", {})}
    device["vendor_id"] = _to_int(device["vendor_id"])
    if device.get("product_id") is not None:
        device["product_id"] = _to_int(device["product_id"])
    device["effects"] = {**DEFAULT_DEVICE["effects"],
                         **(device.get("effects") or {})}
    cfg["device"] = device
    return cfg


def device():
    return load()["device"]
