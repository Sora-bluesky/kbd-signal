"""State management: pattern mapping, baseline snapshot, restore.

State file (%LOCALAPPDATA%/kbd-signal/state.json):
  {"active": "waiting"|"done"|"error"|null,
   "generation": int,
   "baseline": {...snapshot...},
   "owners": ["product:session:agent", ...]}

The baseline is captured once when entering a signal state and kept until
restore, so chained notifications (waiting -> done) still restore the
user's original lighting.
"""

import contextlib
import json
import os
import subprocess
import sys
import time

from . import _platform, config, via

STATE_DIR = config.STATE_DIR
STATE_FILE = os.path.join(STATE_DIR, "state.json")
# Marker for cheap hook-side guards (`if exist active.flag ...`) so hot hooks
# like PostToolUse can skip launching Python entirely when idle.
ACTIVE_FLAG = os.path.join(STATE_DIR, "active.flag")
LOG_FILE = os.path.join(STATE_DIR, "kbd-signal.log")

DONE_RESTORE_AFTER = 5  # seconds

STATE_NAMES = ("waiting", "done", "error")


def patterns():
    """State -> lighting pattern, with effect indices from config so other
    VIA keyboards (different enabled-animation lists) can remap them.
    QMK hue wheel: red=0, orange=21, green=85."""
    fx = config.device()["effects"]
    return {
        "waiting": dict(effect=fx["breathing"], hue=21, sat=255,
                        speed=170, brightness=255),
        "done": dict(effect=fx["solid"], hue=85, sat=255,
                     brightness=255),
        "error": dict(effect=fx["breathing"], hue=0, sat=255,
                      speed=255, brightness=255),
    }


def log(msg):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


class LockTimeout(Exception):
    pass


@contextlib.contextmanager
def _state_lock(timeout=3.0):
    """Interprocess lock serializing load/mutate/save of the state file.
    Concurrent hook processes (e.g. two sessions' PermissionRequest at once)
    would otherwise race read-modify-write and drop a waiting owner.

    Bounded: gives up with LockTimeout after `timeout` seconds so a stuck
    holder can never pin a hook past Claude's 5-second hook timeout —
    dropping one signal beats hanging the agent."""
    os.makedirs(STATE_DIR, exist_ok=True)
    f = open(os.path.join(STATE_DIR, "state.lock"), "a+b")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                _platform.try_lock(f)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"state lock not acquired in {timeout}s")
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                _platform.unlock(f)
            except OSError:
                pass
    finally:
        f.close()


def load_config():
    return config.load()


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
    """Agents with a pending waiting signal (refcount).

    Migrates the v0.1.1 single-"owner" field transparently. Raw v0.2.0
    session ids are replaced lazily through ``owner_aliases`` when their next
    lifecycle event arrives.
    """
    if "owners" in state:
        return list(state["owners"] or [])
    owner = state.get("owner")
    return [owner] if owner else []


def _owner_matches(owner, session=None, owner_prefix=None, owner_aliases=()):
    if session is not None and owner == session:
        return True
    if owner in owner_aliases:
        return True
    return (owner_prefix is not None and isinstance(owner, str)
            and owner.startswith(owner_prefix))


def _has_owner_target(session=None, owner_prefix=None, owner_aliases=()):
    return (session is not None or owner_prefix is not None
            or bool(owner_aliases))


def _release_from_waiting(state, name, session, owner_prefix=None,
                          owner_aliases=()):
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
    if not owners or not _has_owner_target(
            session, owner_prefix, owner_aliases):
        return "clear"  # anonymous/manual override keeps old behavior
    remaining = [
        owner for owner in owners
        if not _owner_matches(owner, session, owner_prefix, owner_aliases)
    ]
    if len(remaining) == len(owners):
        return "blocked"
    state["owners"] = remaining
    state.pop("owner", None)
    return "held" if remaining else "clear"


def set_state(name, session=None, owner_prefix=None, owner_aliases=()):
    """Enter a signal state. Silently no-ops if the keyboard is absent."""
    pattern = patterns()[name]
    try:
        kb = via.Keyboard()
    except (via.DeviceNotFound, OSError) as e:
        log(f"set {name}: device unavailable ({e})")
        return False
    try:
        return _set_state_locked(
            kb, name, session, pattern, owner_prefix, owner_aliases
        )
    except LockTimeout as e:
        log(f"set {name}: {e}, skipped")
        return False


def _set_state_locked(kb, name, session, pattern, owner_prefix=None,
                      owner_aliases=()):
    with kb, _state_lock():
        state = load_state()
        if state["active"] == "error" and name != "error":
            log(f"set {name}: blocked by sticky error")
            return True  # error is manual and sticky until restore
        verdict = _release_from_waiting(
            state, name, session, owner_prefix, owner_aliases
        )
        if verdict == "blocked":
            log(f"set {name}: blocked by {len(_owners(state))} pending owner(s)")
            return True
        if verdict == "held":
            state["generation"] += 1
            save_state(state)
            log(f"set {name}: held for {len(state['owners'])} pending owner(s)")
            return True
        if state["active"] is None:
            try:
                state["baseline"] = kb.snapshot()
            except IOError as e:
                log(f"set {name}: snapshot failed ({e})")
                return False
        if name == "waiting":
            aliases = set(owner_aliases)
            owners = [owner for owner in _owners(state)
                      if owner not in aliases]
            if session is not None and session not in owners:
                owners.append(session)
            state["owners"] = owners
        else:
            state["owners"] = []
        state.pop("owner", None)
        state["active"] = name
        state["generation"] += 1
        save_state(state)
        if not kb.apply(**pattern):
            log(f"set {name}: color not confirmed after retries")
    log(f"set {name} (gen {state['generation']}, "
        f"owner_count {len(state['owners'])})")
    if name == "done":
        _spawn_delayed_restore(DONE_RESTORE_AFTER, state["generation"])
    return True


def restore(after=None, generation=None, session=None):
    """Restore the user's baseline lighting and clear the active state.

    When `session` is given, a waiting signal owned by a different session
    is left untouched (same guard as set_state)."""
    if after:
        time.sleep(after)
    try:
        with _state_lock():
            return _restore_locked(generation, session)
    except LockTimeout as e:
        log(f"restore: {e}, skipped")
        return False


def release_waiting(session=None, owner_prefix=None, owner_aliases=()):
    """Release only matching waiting owners without touching other states."""
    try:
        with _state_lock():
            return _restore_locked(
                None,
                session,
                owner_prefix=owner_prefix,
                owner_aliases=owner_aliases,
                waiting_only=True,
            )
    except LockTimeout as e:
        log(f"release waiting: {e}, skipped")
        return False


def _restore_locked(generation, session, owner_prefix=None, owner_aliases=(),
                    waiting_only=False):
    state = load_state()
    if state["active"] is None:
        return True
    if generation is not None and generation != state["generation"]:
        return True  # superseded by a newer signal
    if waiting_only and state["active"] != "waiting":
        return True
    if state["active"] == "waiting" and _has_owner_target(
            session, owner_prefix, owner_aliases):
        owners = _owners(state)
        if owners:
            remaining = [
                owner for owner in owners
                if not _owner_matches(
                    owner, session, owner_prefix, owner_aliases
                )
            ]
            if len(remaining) == len(owners):
                log(f"restore: skipped, {len(owners)} pending owner(s)")
                return True
            if remaining:  # other sessions still awaiting approval
                state["owners"] = remaining
                state.pop("owner", None)
                save_state(state)
                log(f"restore: released owner, {len(remaining)} still pending")
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
                    kb.set_value(via.VALUE_SPEED, baseline["speed"])
                    # verified write; the effect change resets color first
                    if not kb.set_color(*baseline["color"]):
                        log("restore: color not confirmed after retries")
            elif baseline:
                if not kb.apply_snapshot(baseline):
                    log("restore: color not confirmed after retries")
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
    try:
        subprocess.Popen(cmd, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **_platform.detach_kwargs())
    except OSError as e:
        log(f"delayed restore spawn failed: {e}")
