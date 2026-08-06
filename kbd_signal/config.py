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
      "reset_on_effect": false,        // device-specific reset workaround (see below)
      "effects": {"solid": 1, "breathing": 2}
    }
  }

`reset_on_effect` is a per-device workaround flag (default false) for the
minority of firmware that, ~50-150 ms after an EFFECT change, forces the color
to hue 0 and brightness to full (see kbd_signal.via.Keyboard.set_color). Enable
it per keyboard only when `done` flashes or sticks red; otherwise leave it off
and color/brightness are written directly with no dark hold. The Keychron
Q1 HE 8K is one board known to need it.

Workflow for a new keyboard: `kbd-signal setup` fills in the detectable half
(VID/PID, product string, reset_on_effect), confirms the configured v3 channel
really drives the board, and asks which effect index looks steady and which
pulses -- the firmware's enabled-animation list
is not readable over this protocol, so those two indices can only come from
looking at the keyboard. `kbd-signal detect --all` and `raw-effect <n>` remain
for doing it by hand.
"""

import json
import os
import shutil

from . import _platform

STATE_DIR = _platform.state_dir()
CONFIG_FILE = os.path.join(STATE_DIR, "config.json")

DEFAULT_DEVICE = {
    "vendor_id": 0x3434,   # Keychron
    "product_id": None,
    "product_match": "K8",
    "v3_channel": 3,       # id_qmk_rgb_matrix_channel in Keychron via_json
    "reset_on_effect": False,  # per-device quirk; opt in (see module docstring)
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


def save(cfg):
    """Write config.json atomically, keeping one .bak generation.

    config.json is a hand-edited file, so `kbd-signal setup` never clobbers it
    in place: the previous contents move to config.json.bak and the new file
    lands via os.replace, same as states.save_state.

    config.json is never absent, even for an instant. The replacement is written
    in full first, then the previous contents are *copied* to .bak, and only the
    final os.replace swaps it in. Moving the old file aside instead would leave a
    window with no config.json -- and load() answers a missing file with the
    K8 Pro defaults, silently, so a failure in that window would look like a
    working config for the wrong keyboard.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    try:
        shutil.copyfile(CONFIG_FILE, CONFIG_FILE + ".bak")
    except OSError:
        pass  # no previous config to keep
    os.replace(tmp, CONFIG_FILE)
