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
    },
    "states": {                        // what each signal looks like (#49)
      "waiting": {"effect": "breathing", "hue": 21, "sat": 255,
                  "speed": 170, "brightness": 255},
      "done": {"effect": "solid", "hue": 85, "sat": 255, "brightness": 255},
      "error": {"effect": "breathing", "hue": 0, "sat": 255,
                "speed": 255, "brightness": 255}
    }
  }

Both blocks merge over the defaults field by field, so a config naming only
`states.done.hue` keeps everything else. `states[*].effect` is a key of
`device.effects`, not a raw index: the device block says what an index means
on this board, the states block says which meaning each signal uses.

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
import sys

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


# Per-state lighting. `effect` names an entry in the device's `effects` map
# rather than a raw index, so the same states block works on any board: the
# device block says what an index means there, this says which meaning each
# signal uses. Adding an animation is adding a name to `effects` and using it
# here. Hues are the QMK wheel: red=0, orange=21, green=85.
#
# `done` deliberately carries no speed. It is a solid effect, so speed has no
# visible meaning, and _looks_like_signal only compares the keys a pattern
# actually specifies.
DEFAULT_STATES = {
    "waiting": {"effect": "breathing", "hue": 21, "sat": 255,
                "speed": 170, "brightness": 255},
    "done": {"effect": "solid", "hue": 85, "sat": 255, "brightness": 255},
    "error": {"effect": "breathing", "hue": 0, "sat": 255,
              "speed": 255, "brightness": 255},
}

_STATE_BYTES = ("hue", "sat", "speed", "brightness")


def _to_int(value):
    if isinstance(value, str):
        return int(value, 0)  # accepts "0x3434" and "13364"
    return value


def states(cfg, effects):
    """Merge the user's states over the defaults, one state at a time.

    Raises on anything malformed rather than falling back quietly. This block
    is only ever hand-edited -- no command writes it -- so a typo that
    silently kept the old colour would be reported as "my config does
    nothing". The hook path catches the exception and logs it (cli.cmd_hook),
    so surfacing it costs a log line, not a signal.
    """
    given = cfg.get("states") or {}
    if not isinstance(given, dict):
        raise ValueError('config "states" must be an object')
    unknown = set(given) - set(DEFAULT_STATES)
    if unknown:
        raise ValueError(
            f'config "states" has unknown state(s): {sorted(unknown)}; '
            f'expected any of {sorted(DEFAULT_STATES)}')
    merged = {}
    for name, default in DEFAULT_STATES.items():
        override = given.get(name) or {}
        if not isinstance(override, dict):
            raise ValueError(f'config states.{name} must be an object')
        state = {**default, **override}
        if state["effect"] not in effects:
            raise ValueError(
                f'config states.{name}.effect "{state["effect"]}" is not in '
                f'device.effects {sorted(effects)}')
        for field in _STATE_BYTES:
            if field not in state:
                continue
            value = state[field]
            if (not isinstance(value, int) or isinstance(value, bool)
                    or not 0 <= value < 256):
                raise ValueError(
                    f"config states.{name}.{field} must be 0-255, got "
                    f"{value!r}")
        merged[name] = state
    return merged


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
    # `states` is deliberately NOT merged into the returned config. `setup`
    # writes back whatever load() hands it, so filling the defaults in here
    # would freeze them into every config.json it touches -- and a later
    # release changing a default colour would silently never reach those
    # users. Callers that need the resolved patterns go through
    # states.patterns(), which merges on the way out.
    return cfg


def device():
    return load()["device"]


def _keep_previous(path):
    """Copy the current config aside as .bak, atomically.

    Copying straight onto .bak is not atomic: a failure partway (a full disk)
    leaves it truncated, destroying the generation it was meant to preserve. So
    the copy lands on .bak.tmp and is swapped in with os.replace.

    Only "there is nothing to keep yet" is silent. Anything else -- a read-only
    .bak, another process holding it open on Windows, a full disk -- is reported,
    because `kbd-signal setup` promises "previous kept as .bak" at its prompt and
    a promise that quietly does not hold is worse than a noisy one. The config
    write itself carries on either way: losing the backup is not a reason to
    refuse to save.
    """
    staging = path + ".bak.tmp"
    try:
        shutil.copyfile(path, staging)
        os.replace(staging, path + ".bak")
    except FileNotFoundError:
        return  # first save on this machine; no previous generation exists
    except OSError as e:
        print(f"kbd-signal: could not keep a backup of {path} ({e})",
              file=sys.stderr)
        try:
            os.remove(staging)
        except OSError:
            pass


def save(cfg):
    """Write config.json atomically, keeping one .bak generation.

    config.json is a hand-edited file, so `kbd-signal setup` never clobbers it
    in place: the previous contents are copied to config.json.bak and the new
    file lands via os.replace, same as states.save_state.

    config.json is never absent, even for an instant. The replacement is written
    in full first, then the old contents are copied aside, and only the final
    os.replace swaps it in. *Moving* the old file aside instead would leave a
    window with no config.json -- and load() answers a missing file with the
    K8 Pro defaults, silently, so a failure in that window would look like a
    working config for the wrong keyboard.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        _keep_previous(CONFIG_FILE)
        os.replace(tmp, CONFIG_FILE)
    finally:
        # A successful swap already moved it; this only bites on the paths that
        # left it behind, where a stale .tmp would outlive the failure.
        try:
            os.remove(tmp)
        except OSError:
            pass
