"""State management: pattern mapping, baseline snapshot, restore.

State file (%LOCALAPPDATA%/kbd-signal/state.json):
  {"active": "waiting"|"done"|"error"|null,
   "generation": int,
   "baseline": {...snapshot...}}

The baseline is captured once when entering a signal state and kept until
restore, so chained notifications (waiting -> done) still restore the
user's original lighting.
"""

import json
import os
import subprocess
import sys
import time

from . import via

STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                         "kbd-signal")
CONFIG_FILE_NAME = "config.json"  # {"restore": "baseline" | "off"}
STATE_FILE = os.path.join(STATE_DIR, "state.json")
# Marker for cheap hook-side guards (`if exist active.flag ...`) so hot hooks
# like PostToolUse can skip launching Python entirely when idle.
ACTIVE_FLAG = os.path.join(STATE_DIR, "active.flag")
LOG_FILE = os.path.join(STATE_DIR, "kbd-signal.log")

DONE_RESTORE_AFTER = 5  # seconds

# QMK hue wheel: red=0, orange=21, green=85
PATTERNS = {
    "waiting": dict(effect=via.EFFECT_BREATHING, hue=21, sat=255,
                    speed=170, brightness=255),
    "done": dict(effect=via.EFFECT_SOLID_COLOR, hue=85, sat=255,
                 brightness=255),
    "error": dict(effect=via.EFFECT_BREATHING, hue=0, sat=255,
                  speed=255, brightness=255),
}


def log(msg):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def load_config():
    try:
        with open(os.path.join(STATE_DIR, CONFIG_FILE_NAME), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"active": None, "generation": 0, "baseline": None}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)
    if state["active"] is None:
        try:
            os.remove(ACTIVE_FLAG)
        except OSError:
            pass
    else:
        with open(ACTIVE_FLAG, "w", encoding="utf-8") as f:
            f.write(state["active"])


def is_active():
    return load_state()["active"] is not None


def _owners(state):
    """Sessions with a pending waiting signal (refcount). Migrates the
    v0.1.1 single-"owner" field transparently."""
    if "owners" in state:
        return list(state["owners"] or [])
    owner = state.get("owner")
    return [owner] if owner else []


def _release_from_waiting(state, name, session):
    """Multi-session guard for entering `name` while waiting is active.
    Every session with a pending approval keeps the orange signal alive:
    a session's done/turn-end only removes itself from the owner list, and
    the signal survives until the list is empty. Returns:
      "blocked"  -> keep waiting, do nothing else
      "held"     -> caller removed from owners but others remain (save state)
      "clear"    -> waiting fully released, proceed with `name`
    """
    if state["active"] != "waiting" or name != "done":
        return "clear"
    owners = _owners(state)
    if not owners or session is None:
        return "clear"  # anonymous/manual override keeps old behavior
    if session not in owners:
        return "blocked"
    owners.remove(session)
    state["owners"] = owners
    state.pop("owner", None)
    return "held" if owners else "clear"


def set_state(name, session=None):
    """Enter a signal state. Silently no-ops if the keyboard is absent."""
    pattern = PATTERNS[name]
    try:
        kb = via.Keyboard()
    except (via.DeviceNotFound, OSError) as e:
        log(f"set {name}: device unavailable ({e})")
        return False
    with kb:
        state = load_state()
        if state["active"] == "error" and name != "error":
            log(f"set {name}: blocked by sticky error")
            return True  # error is manual and sticky until restore
        verdict = _release_from_waiting(state, name, session)
        if verdict == "blocked":
            log(f"set {name}: blocked, waiting owned by {_owners(state)}")
            return True
        if verdict == "held":
            state["generation"] += 1
            save_state(state)
            log(f"set {name}: held, still waiting for {state['owners']}")
            return True
        if state["active"] is None:
            try:
                state["baseline"] = kb.snapshot()
            except IOError as e:
                log(f"set {name}: snapshot failed ({e})")
                return False
        if name == "waiting":
            owners = _owners(state)
            if session is not None and session not in owners:
                owners.append(session)
            state["owners"] = owners
        else:
            state["owners"] = []
        state.pop("owner", None)
        state["active"] = name
        state["generation"] += 1
        save_state(state)
        kb.apply(**pattern)
    log(f"set {name} (gen {state['generation']}, owners {state['owners']})")
    if name == "done":
        _spawn_delayed_restore(DONE_RESTORE_AFTER, state["generation"])
    return True


def restore(after=None, generation=None, session=None):
    """Restore the user's baseline lighting and clear the active state.

    When `session` is given, a waiting signal owned by a different session
    is left untouched (same guard as set_state)."""
    if after:
        time.sleep(after)
    state = load_state()
    if state["active"] is None:
        return True
    if generation is not None and generation != state["generation"]:
        return True  # superseded by a newer signal
    if state["active"] == "waiting" and session is not None:
        owners = _owners(state)
        if owners:
            if session not in owners:
                log(f"restore: skipped, waiting owned by {owners}")
                return True
            owners.remove(session)
            if owners:  # other sessions still awaiting approval
                state["owners"] = owners
                state.pop("owner", None)
                save_state(state)
                log(f"restore: released {session}, still waiting for {owners}")
                return True
    baseline = state.get("baseline")
    mode = load_config().get("restore", "baseline")
    try:
        with via.Keyboard() as kb:
            if mode == "off":
                # Go dark first (avoids a flash of the baseline effect), then
                # put the stored effect/color back so a manual Fn wake-up
                # shows the user's own settings, not our last signal.
                kb.set_value(via.VALUE_BRIGHTNESS, 0)
                if baseline:
                    kb.set_value(via.VALUE_EFFECT, baseline["effect"])
                    kb.set_value(via.VALUE_COLOR, *baseline["color"])
                    kb.set_value(via.VALUE_SPEED, baseline["speed"])
            elif baseline:
                kb.apply_snapshot(baseline)
    except (via.DeviceNotFound, OSError) as e:
        # Keyboard gone: RAM-only changes vanish on power cycle anyway.
        log(f"restore: device unavailable ({e})")
    save_state({"active": None, "generation": state["generation"],
                "baseline": None})
    log("restore")
    return True


def _spawn_delayed_restore(after, generation):
    """Fire-and-forget `kbd-signal restore --after N --gen G` so the CLI
    itself never has to stay resident."""
    cmd = [sys.executable, "-m", "kbd_signal", "restore",
           "--after", str(after), "--gen", str(generation)]
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    try:
        subprocess.Popen(cmd, creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError as e:
        log(f"delayed restore spawn failed: {e}")
