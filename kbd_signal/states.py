"""State management: pattern mapping, baseline snapshot, restore.

State file (%LOCALAPPDATA%/kbd-signal/state.json):
  {"active": "waiting"|"done"|"error"|null,
   "generation": int,
   "baseline": {...snapshot...},
   "last_baseline": {...snapshot...},  (optional)
   "last_baseline_device": {"vendor_id": int|null,
                            "product_id": int|null,
                            "product_match": str|null,
                            "v3_channel": int,
                            "effects": {"solid": int,
                                        "breathing": int}},  (optional)
   "owners": ["product:session:agent", ...],
   "owner_seen": {"product:session:agent": {"wall": epoch_seconds,
                                            "mono": monotonic_seconds}, ...},
   "owner_seen_gen": int}

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

# Waiting owners whose approval has been pending longer than this expire
# automatically: an approval left unanswered for over an hour has lost its
# value as a signal, and an abandoned session would otherwise keep the
# keyboard orange until a manual restore (measured 1h20m stuck on
# 2026-07-26). A constant, not a config knob — same call as the log cap
# (#26/#28).
WAITING_TTL = 3600  # seconds

# Delay of the TTL wake-up a waiting transition schedules for itself: a lone
# session that dies at its PermissionRequest sends no later hook, so the
# expiry sweep would never run and the orange would stay until an unrelated
# event — the exact 1h20m incident (#31). Slightly past the TTL so the sweep
# the wake-up triggers sees the owner unambiguously expired.
WAITING_WAKE_AFTER = WAITING_TTL + 5  # seconds

# Cap the log by rotating to a single `.1` generation once it grows past this,
# so total on-disk usage stays bounded at ~2x instead of growing forever
# (every hook writes a line; PostToolUse fires constantly).
LOG_MAX_BYTES = 1024 * 1024  # 1 MB

STATE_NAMES = ("waiting", "done", "error")


def patterns(dev_cfg=None):
    """State -> lighting pattern, with effect indices from config so other
    VIA keyboards (different enabled-animation lists) can remap them.
    QMK hue wheel: red=0, orange=21, green=85.

    Pass the caller's config snapshot to keep the indices from the same read
    as the board being opened (#44); omit it and the live config is read."""
    fx = (config.device() if dev_cfg is None else dev_cfg)["effects"]
    return {
        "waiting": dict(effect=fx["breathing"], hue=21, sat=255,
                        speed=170, brightness=255),
        "done": dict(effect=fx["solid"], hue=85, sat=255,
                     brightness=255),
        "error": dict(effect=fx["breathing"], hue=0, sat=255,
                      speed=255, brightness=255),
    }


def _device_identity(dev_cfg=None):
    """Configuration fields selecting and interpreting the VIA device.

    Takes the caller's config snapshot so the fingerprint stored with a
    baseline describes the same read as the board it was captured from (#44);
    omit it and the live config is read.

    ``reset_on_effect`` is intentionally excluded: it changes write timing,
    not what stored effect, speed, or color values mean.

    Accepted limitation: ``product_match`` is only a preferred filter because
    VIA falls back to the first candidate. The fingerprint therefore proves
    the configured selection, not the physical unit. The same pre-existing
    ambiguity applies to baseline capture versus restore within one signal
    and is outside this guard's scope.
    """
    device = config.device() if dev_cfg is None else dev_cfg
    return {
        field: device.get(field)
        for field in (
            "vendor_id",
            "product_id",
            "product_match",
            "v3_channel",
            "effects",
        )
    }


def log(msg):
    """Append one line, rotating to `.1` past LOG_MAX_BYTES.

    Rotation is lock-free: between concurrent hook processes a rare 1-2
    lines or one generation of logs may be lost (a check-then-replace
    race, or a swallowed OSError below), but making a hook wait is worse.
    Same "drop a line rather than hang" philosophy as the bounded state
    lock — logs are diagnostic, never worth blocking a signal for.
    """
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
            size = f.tell()  # cheaper than a separate getsize()/stat per line
    except OSError:
        return
    if size >= LOG_MAX_BYTES:
        # Single-generation rotation, lock-free best-effort. On Windows a
        # concurrent hook process holding the file open can make os.replace
        # raise PermissionError (sharing violation); swallow it and let the
        # next line retry — dropping a rotation beats hanging a hook, same
        # philosophy as the bounded state lock.
        try:
            os.replace(LOG_FILE, LOG_FILE + ".1")
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


def _sync_owner_seen(state):
    """Prune owner_seen down to the current owners list.

    Called wherever the owners list is rewritten, so a released owner never
    leaves a dangling timestamp behind."""
    seen = state.get("owner_seen") or {}
    state["owner_seen"] = {
        owner: seen[owner]
        for owner in (state.get("owners") or [])
        if owner in seen
    }


def _stamp_owner_seen(state):
    """Mark owner_seen as in step with the generation this save writes.

    The expiry sweep only trusts the TTL clocks while the stamp matches:
    a save that moved the generation without restamping came from a
    TTL-unaware writer (v1.0.1 or older), which can re-add an owner while
    its old timestamp survives the round-trip."""
    state["owner_seen_gen"] = state["generation"]


def _valid_seen_entry(entry):
    """A usable owner_seen entry: wall and monotonic halves, both numbers."""
    return (isinstance(entry, dict)
            and isinstance(entry.get("wall"), (int, float))
            and isinstance(entry.get("mono"), (int, float)))


def _expire_stale_owners(state, now=None, now_mono=None):
    """Drop waiting owners whose approval has been pending past WAITING_TTL.

    Expiry must never kill a live approval, so the clocks are guarded twice:

    - If owner_seen_gen does not match the generation, a TTL-unaware writer
      rewrote the state since our last save (see _stamp_owner_seen); its
      timestamps cannot be trusted, so every clock restarts instead — the
      same lazy migration idiom as the v0.2.0 raw-session-id aliases, which
      also covers owners with no entry at all (older state files).
    - An owner expires only when both its wall-clock age and its monotonic
      age reach the TTL: a wall clock stepped forward cannot expire a
      seconds-old approval (its monotonic age stays small), and a clock that
      ran backward or a reboot resets the affected half instead of
      producing a bogus age.

    Returns True when the state dict was modified and needs saving."""
    if state["active"] != "waiting":
        return False
    owners = _owners(state)
    if not owners:
        return False
    if now is None:
        now = time.time()
    if now_mono is None:
        now_mono = time.monotonic()
    if state.get("owner_seen_gen") != state["generation"]:
        log(f"restarted TTL clocks for {len(owners)} owner(s) "
            f"(state rewritten by a TTL-unaware writer)")
        state["owners"] = owners
        state.pop("owner", None)
        state["owner_seen"] = {
            owner: {"wall": now, "mono": now_mono} for owner in owners
        }
        _stamp_owner_seen(state)
        return True
    seen = dict(state.get("owner_seen") or {})
    changed = False
    remaining = []
    for owner in owners:
        entry = seen.get(owner)
        if not _valid_seen_entry(entry):
            # Legacy or corrupt entry: start its clock, don't expire.
            seen[owner] = {"wall": now, "mono": now_mono}
            changed = True
            remaining.append(owner)
            continue
        if entry["wall"] > now or entry["mono"] > now_mono:
            # The wall clock stepped backward, or the machine rebooted (the
            # monotonic clock restarted): reset the affected half rather
            # than trust a clock that visibly moved under us.
            entry = {"wall": min(entry["wall"], now),
                     "mono": min(entry["mono"], now_mono)}
            seen[owner] = entry
            changed = True
        if (now - entry["wall"] >= WAITING_TTL
                and now_mono - entry["mono"] >= WAITING_TTL):
            log(f"expired waiting owner (pending {int(now - entry['wall'])}s)")
            changed = True
        else:
            remaining.append(owner)
    if changed:
        state["owners"] = remaining
        state.pop("owner", None)
        state["owner_seen"] = {owner: seen[owner] for owner in remaining}
        _stamp_owner_seen(state)
        if not remaining:
            # The sweep just orphaned the waiting signal: no owner is left
            # to release it. Record that durably — this very event can still
            # die before restoring (corrupt config, absent device), and a
            # later superseded restore must know it may bypass its
            # generation guard. Cleared on the next state transition.
            state["orphaned"] = True
    return changed


def _wake_reserved(state, now):
    """True while an already-spawned TTL wake-up is still due to fire.

    The reservation is a wall-clock epoch in the state file: hooks are
    separate processes, so nothing in memory can dedupe them. An epoch
    beyond now + WAITING_WAKE_AFTER is impossible for a live reservation
    (the wall clock stepped back under it) and is treated as absent
    rather than trusted — it could otherwise suppress wake-ups for an
    arbitrary while.

    A reservation also proves nothing across a reboot: the sleeper died
    with the OS. Reboots are detected with a boot-time anchor (wall clock
    minus monotonic clock, stored at reservation time) — the anchor shifts
    on every reboot, whereas a plain monotonic comparison stops detecting
    one as soon as the new boot's uptime passes the stored mark (#36
    review). A suspend can shift the anchor too; that only over-
    invalidates, which is safe — the next waiting entry just spawns a
    fresh sleeper and the epoch gate keeps them deduplicated.
    """
    wake_at = state.get("wake_at")
    wake_boot = state.get("wake_boot")
    same_boot = (isinstance(wake_boot, (int, float))
                 and abs((now - time.monotonic()) - wake_boot) < 60)
    return (isinstance(wake_at, (int, float))
            and now < wake_at <= now + WAITING_WAKE_AFTER
            and same_boot)


def _reserve_wake(state):
    """Claim the single pending TTL wake-up slot. Returns True when the
    caller must spawn the wake-up (after saving the state just mutated).

    PermissionRequest fires constantly and every wake-up is an hour-long
    sleeper process, so spawning one per waiting entry would stack them;
    one pending wake is enough. Guarantee for owners that join mid-chain
    and outlive the reserved wake: the wake's own restore runs the expiry
    sweep, and by then the reservation has lapsed, so that very event (or
    any later waiting entry / restore) reserves and spawns a follow-up —
    every abandoned owner is swept within one further wake period, with
    no dependence on external events."""
    now = time.time()
    if _wake_reserved(state, now):
        return False
    state["wake_at"] = now + WAITING_WAKE_AFTER
    state["wake_boot"] = now - time.monotonic()
    return True


def _cancel_wake(state):
    """Drop a wake reservation whose sleeper never started, so the next
    waiting entry retries instead of trusting a wake that will never
    come (#36 review)."""
    state.pop("wake_at", None)
    state.pop("wake_boot", None)


def _valid_byte(value):
    return (isinstance(value, int) and not isinstance(value, bool)
            and 0 <= value < 256)


def _valid_snapshot(snap):
    """True when snap has the complete byte-safe shape for restoration."""
    if not isinstance(snap, dict):
        return False
    if not all(_valid_byte(snap.get(field))
               for field in ("effect", "speed", "brightness")):
        return False
    color = snap.get("color")
    return (isinstance(color, (list, tuple))
            and len(color) == 2
            and all(_valid_byte(value) for value in color))


def _brightness_echo(kb, written, device_identity):
    """What restore wrote, paired with what the firmware reads back for it.

    Returns None when the read fails; the caller then simply forgets the pair
    and the next baseline is taken as read, which is the pre-#58 behaviour.
    """
    try:
        # tries=2, not get_value's default 6: this runs inside the state lock,
        # and at 250 ms a try the default would add up to 1.5 s on a device that
        # has stopped answering -- the same lock-held stall #57 removed. The
        # pair is an optimisation; not getting it costs one cycle of drift.
        readback = kb.get_value(via.VALUE_BRIGHTNESS, tries=2)[0]
    except OSError:
        return None
    return {"written": written, "readback": readback,
            "device": device_identity}


def _undo_lossy_brightness(snap, state, device_identity):
    """Put the brightness we wrote back into a snapshot that only reads it.

    VIA scales brightness through the firmware's internal limit in both
    directions, and on the rgb_matrix channel the two directions do not use the
    same divisor (#56), so re-reading our own write returns a slightly lower
    number. Storing that as the new baseline and writing it back walks the value
    down a little every signal cycle.

    Measured on a Q1 HE 8K over 60 cycles: 200 settles at 199 and 120 at 117 --
    invisible -- but 10 walks 9, 7, 6, 5, 3, 2, 1, 0. The absolute loss is a
    near-constant 0-2, so the *relative* loss explodes at the bottom and a dim
    backlight goes fully dark -- eight cycles, which at the 107 restores a day
    in the log this was found in is under two hours (#58).

    The echo pair is what the last restore wrote and what the firmware reported
    for it. A reading that still matches the echo means nobody has touched the
    keyboard since, so the value we meant is the honest baseline. Anything else
    is the user's own change and is taken as read.

    "Nobody touched it" is an equality test, so it cannot separate an untouched
    board from a user who happened to land on the recorded readback. 255 is
    where that bites: people pick it deliberately, and a firmware revert (#34)
    parks a board there. The two cases need keeping apart. A board parked at 255
    by the revert and left alone is what the pair is for -- putting the written
    value back is the repair, not a mistake. Only a user who set 255 themselves
    loses their choice to it.

    _looks_like_signal does not narrow that down much, because it only intercepts
    a reading that still matches a pattern on every field. That holds for a
    leftover signal nothing has repainted; it does not once a restore has put
    the user's own effect and color back, and the brightness alone reverted. So
    255 sitting on the user's effect reaches this code and is traded away.

    Deliberately not gated on the protocol generation: where the round trip is
    lossless -- VIA v2 at QMK's default RGBLIGHT_LIMIT_VAL, and any v3 value
    that lands on a fixed point -- written equals readback and this is the
    identity. A v2 board with a lowered limit is lossy and gets the correction,
    which a `self._v3` test would have missed.
    """
    echo = state.get("brightness_echo")
    if not isinstance(echo, dict) or echo.get("device") != device_identity:
        return snap
    if not (_valid_byte(echo.get("written")) and _valid_byte(echo.get("readback"))):
        return snap
    if snap.get("brightness") != echo["readback"]:
        return snap  # the user moved it; that reading is the real baseline
    return {**snap, "brightness": echo["written"]}


def _log_brightness_drift(name, previous, current):
    """One line when the stored baseline brightness moves, and only then.

    #58's decay is invisible from the logs: the correction runs on every
    capture, so logging when it fires would print on every cycle and say
    nothing. What is worth a line is the value changing, because in a healthy
    steady state it does not -- the correction hands back what was written, so
    consecutive baselines agree and this stays quiet. A line therefore means
    either the user turned the backlight up or down, or the correction did not
    hold and the value is walking again.

    Nothing is logged for the first capture: with no previous baseline there is
    no movement to report, only a starting point. The caller is responsible for
    only passing a previous baseline from the same keyboard -- swapping boards
    changes the value without anything having drifted.
    """
    if not isinstance(previous, dict):
        return
    before = previous.get("brightness")
    after = current.get("brightness")
    if before == after:
        return
    log(f"set {name}: baseline brightness {before} -> {after}")


def _looks_like_signal(snap, dev_cfg=None):
    """True when the snapshot matches a known signal pattern on every field.

    A baseline captured while the keyboard still shows an earlier signal (a
    leftover from a crashed restore) would save the signal itself as the
    user's setting, and every later restore would write it back —
    self-perpetuating pollution (#32, measured 2026-07-26: the baseline
    became the waiting pattern, so every Fn wake-up showed orange
    breathing). Speed is compared only when the pattern specifies it (done
    has none). The patterns derive from config, so boards with remapped
    effect indices are covered automatically; pass the caller's snapshot to
    compare against the same read the board was opened from (#44).

    Brightness 0 counts as a match too: an "off"-mode restore with no
    baseline can only dim the leftover signal (there is nothing to repaint
    effect/color/speed from), so the next snapshot shows the same pattern
    at brightness 0 — adopting that would hand the guard's own residue
    back to it and restart the pollution loop one cycle later.

    Trade-off: a user whose real everyday setting exactly equals a signal
    pattern (full match on every compared field, or its brightness-0
    variant) falls back to the last non-signal capture; with none stored
    yet, restore can only go dark. That cosmetic cost is strictly better
    than restoring the signal forever."""
    for pattern in patterns(dev_cfg).values():
        if snap["effect"] != pattern["effect"]:
            continue
        if snap["brightness"] not in (pattern["brightness"], 0):
            continue
        if list(snap["color"]) != [pattern["hue"], pattern["sat"]]:
            continue
        if "speed" in pattern and snap["speed"] != pattern["speed"]:
            continue
        return True
    return False


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
    _sync_owner_seen(state)
    return "held" if remaining else "clear"


def set_state(name, session=None, owner_prefix=None, owner_aliases=()):
    """Enter a signal state. Silently no-ops if the keyboard is absent."""
    # Sweep expired owners first, under a short lock of their own, before
    # anything that can fail or stall (a corrupt config, a missing or slow
    # device): a main-session Stop can be the final lifecycle event, and it
    # must clean up stale waiting state no matter what happens after —
    # otherwise an abandoned approval strands the orange signal (#31).
    # The sweep gets a shorter lock budget than the transition: set_state
    # acquires the lock twice, and two full 3 s waits under contention
    # would burn ~6 s — past the 5 s hook deadline. 1 s + 3 s keeps the
    # worst case around 4 s, and a sweep that loses the lock just runs on
    # the next event anyway.
    try:
        with _state_lock(timeout=1.0):
            state = load_state()
            if _expire_stale_owners(state):
                # No generation bump: expiry is bookkeeping, not a signal.
                save_state(state)
    except LockTimeout as e:
        log(f"set {name}: {e} (expiry sweep), continuing")
    # One read for the whole transition (#44). The pattern's effect indices,
    # the fingerprint stored with the baseline, and the device actually opened
    # all have to describe the same keyboard; reading the config separately for
    # each let an edit land between them and mix two generations. Deliberately
    # below the sweep: this is the first thing here that can raise, and #31
    # requires the sweep to have run before anything that can.
    cfg = load_config()
    dev_cfg = cfg["device"]
    pattern = patterns(dev_cfg)[name]
    # Open the device OUTSIDE the lock: enumerate/open/probe have no time
    # bound, and holding the 3s lock across them would make every
    # concurrent hook time out and drop its signal on a slow device.
    try:
        kb = via.Keyboard(dev_cfg)
    except (via.DeviceNotFound, OSError) as e:
        log(f"set {name}: device unavailable ({e})")
        return False
    try:
        return _set_state_locked(
            kb, name, session, pattern, cfg,
            owner_prefix, owner_aliases
        )
    except LockTimeout as e:
        log(f"set {name}: {e}, skipped")
        return False


def _set_state_locked(kb, name, session, pattern, cfg,
                      owner_prefix=None, owner_aliases=()):
    with kb, _state_lock():
        # Re-read and re-sweep under the transition lock: the state may
        # have moved while the device was opening, and the sweep is cheap
        # and idempotent.
        state = load_state()
        if _expire_stale_owners(state):
            # Persist without a generation bump so the expiry sticks even
            # when a branch below returns early (blocked / sticky error).
            save_state(state)
        return _apply_state(
            kb, state, name, session, pattern, cfg,
            owner_prefix, owner_aliases
        )


def _apply_state(kb, state, name, session, pattern, cfg,
                 owner_prefix, owner_aliases):
    """Run the guarded transition. Caller holds the state lock and the
    open keyboard, and has already swept (and persisted) expired owners.

    Takes the whole config snapshot rather than a ready-made fingerprint so
    that everything derived here — the fingerprint, the signal-shape guard's
    patterns, and whether restore will preserve a baseline at all — comes
    from the same read as the board that is already open (#44)."""
    dev_cfg = cfg["device"]
    device_identity = _device_identity(dev_cfg)
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
        _stamp_owner_seen(state)
        save_state(state)
        log(f"set {name}: held for {len(state['owners'])} pending owner(s)")
        return True
    if state["active"] is None:
        try:
            snap = kb.snapshot()
        except OSError as e:
            log(f"set {name}: snapshot failed ({e})")
            return False
        if _looks_like_signal(snap, dev_cfg):
            # A leftover signal must not become the user's setting. Reuse
            # requires byte-range safety through _valid_snapshot and an
            # exact current-device identity match; otherwise baseline stays
            # None and restore goes dark (#32: 1,263 discards measured
            # 2026-07-29).
            last = state.get("last_baseline")
            if (_valid_snapshot(last)
                    and state.get("last_baseline_device") == device_identity
                    and not _looks_like_signal(last, dev_cfg)):
                state["baseline"] = last
                log(f"set {name}: snapshot matches a signal pattern, "
                    f"using last known baseline")
            else:
                log(f"set {name}: snapshot matches a signal pattern, "
                    f"baseline discarded")
        else:
            # After the signal check, so the guard still sees what the device
            # actually reports; only the value we keep is corrected.
            snap = _undo_lossy_brightness(snap, state, device_identity)
            # Two gates. The fingerprint one is what the baseline itself gets
            # (#45): a value from another keyboard did not drift, it was
            # replaced. The mode one is because "off" restores nothing — it
            # writes 0 on purpose and records no pair, so the next capture
            # reads back this program's own blackout. Reporting that as drift
            # would put a false line in the one place a real one has to show.
            if (cfg.get("restore", "baseline") != "off"
                    and state.get("last_baseline_device") == device_identity):
                _log_brightness_drift(name, state.get("last_baseline"), snap)
            state["baseline"] = snap
            state["last_baseline"] = snap
            state["last_baseline_device"] = device_identity
    if name == "waiting":
        aliases = set(owner_aliases)
        owners = [owner for owner in _owners(state)
                  if owner not in aliases]
        if session is not None and session not in owners:
            owners.append(session)
        state["owners"] = owners
        if session is not None:
            # A fresh PermissionRequest restarts this owner's TTL clock.
            state.setdefault("owner_seen", {})[session] = {
                "wall": time.time(), "mono": time.monotonic()}
        # Schedule the wake-up that expires this owner if its session dies
        # here (#31). No new logic at the receiving end: the sweep expires
        # the stale owner, and the durable-orphan bypass lets even a
        # superseded generation turn the lights off.
        spawn_wake = _reserve_wake(state)
    else:
        state["owners"] = []
        spawn_wake = False
    state.pop("owner", None)
    _sync_owner_seen(state)
    # A real transition supersedes any recorded orphaning: waiting gets a
    # live owner again, and done/error no longer need the bypass — leaving
    # the flag would let a stale delayed restore kill a fresh signal.
    state.pop("orphaned", None)
    state["active"] = name
    state["generation"] += 1
    _stamp_owner_seen(state)
    save_state(state)
    if not kb.apply(**pattern):
        log(f"set {name}: color not confirmed after retries")
    log(f"set {name} (gen {state['generation']}, "
        f"owner_count {len(state['owners'])})")
    if name == "done":
        _spawn_delayed_restore(DONE_RESTORE_AFTER, state["generation"])
    elif spawn_wake:
        if not _spawn_delayed_restore(WAITING_WAKE_AFTER,
                                      state["generation"]):
            _cancel_wake(state)
            save_state(state)
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
    expired = _expire_stale_owners(state)
    if expired:
        # Persist without a generation bump so the expiry sticks even when
        # a guard below skips the restore.
        save_state(state)
    if state["active"] is None:
        return True
    if (state["active"] == "waiting" and _owners(state)
            and _reserve_wake(state)):
        # Keep the wake chain alive: owners that joined while an earlier
        # wake was reserved can outlive it with their sessions already
        # dead. Once the reservation lapses (this event may BE that wake),
        # arm the next one so every abandoned owner is swept within one
        # further wake period. The epoch gate above bounds this to ~one
        # spawn per wake period no matter how often restores fire.
        save_state(state)
        if not _spawn_delayed_restore(WAITING_WAKE_AFTER,
                                      state["generation"]):
            # Same liveness rule as the waiting-entry site: a reservation
            # without a sleeper behind it would suppress every replacement.
            _cancel_wake(state)
            save_state(state)
    # When the sweep above dropped the last owner, waiting is over and this
    # event is the only hook left that can turn the lights off — an
    # abandoned session sends no further events, so even a superseded
    # delayed restore must fall through to the actual restore.
    orphaned = not _owners(state) and (expired or state.get("orphaned"))
    if (generation is not None and generation != state["generation"]
            and not orphaned):
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
                _sync_owner_seen(state)
                _stamp_owner_seen(state)
                save_state(state)
                log(f"restore: released owner, {len(remaining)} still pending")
                return True
    # Read once and reuse: restore's I/O runs for up to ~2 s between the checks
    # below and the echo stamp at the end (set_color's settle plus
    # settle_brightness's hold), and re-reading config across that window is how
    # #44's split-read inconsistency would land here -- the pair could be
    # stamped with a different identity than the baseline was validated against.
    # All three consumers come from this one read: the fingerprint the baseline
    # is checked against, the restore mode, and the board that gets opened.
    # Kept here rather than at the top of the function so the owner-guard early
    # returns above, which need no config, gain no new way to fail.
    cfg = load_config()
    dev_cfg = cfg["device"]
    device_identity = _device_identity(dev_cfg)
    baseline = state.get("baseline")
    if (baseline is not None
            and (not _valid_snapshot(baseline)
                 or state.get("last_baseline_device") != device_identity)):
        log("restore: baseline rejected (invalid or from another device)")
        baseline = None
    mode = cfg.get("restore", "baseline")
    echo = None
    reached_device = False
    try:
        with via.Keyboard(dev_cfg) as kb:
            reached_device = True
            if mode == "off" or not baseline:
                # Off mode always goes dark first (avoids a flash of the
                # baseline effect), then puts the stored effect/color back so
                # a manual Fn wake-up shows the user's own settings. With no
                # baseline at all (discarded as signal-polluted, or never
                # captured) there is nothing to repaint in either mode, and
                # under the default "baseline" mode this used to skip the
                # keyboard entirely — leaving the last signal lit forever.
                # Going dark is the only safe restore then (#35 review).
                kb.set_value(via.VALUE_BRIGHTNESS, 0)
                if baseline:
                    kb.set_value(via.VALUE_EFFECT, baseline["effect"])
                    kb.set_value(via.VALUE_SPEED, baseline["speed"])
                    # verified write; the effect change resets color first
                    if not kb.set_color(*baseline["color"]):
                        log("restore: color not confirmed after retries")
                # The firmware can ACK a brightness write after an effect
                # change and silently revert it (#34: restore('off')
                # measured ending at 255) — settle with read-back so "off"
                # reliably goes dark.
                if not kb.settle_brightness(0):
                    log("restore: brightness 0 not confirmed after retries")
                # No pair here. The 0 is a forced blackout, not the user's
                # setting, so recording it as the meaning of whatever the
                # firmware reports back is a lie: if the write did not land the
                # pair becomes {0, <the user's real brightness>}, and the next
                # capture reads that brightness, calls it untouched, and stores
                # 0 as the baseline -- in last_baseline too, so #45's fallback
                # cannot undo it. The baseline path below records instead,
                # where the written value really is the baseline.
            else:
                if not kb.apply_snapshot(baseline):
                    # Color never settled: the LEDs were left dark, and
                    # raising brightness onto the wrong color would defeat
                    # that guard — no brightness settle either.
                    log("restore: color not confirmed after retries")
                else:
                    if not kb.settle_brightness(baseline["brightness"]):
                        # Same #34 revert can undo the baseline brightness.
                        log("restore: brightness not confirmed after retries")
                    # Pair what we just wrote with what the firmware reports
                    # for it, so the next baseline capture can tell "nobody
                    # touched it" from "the user turned it down" (#58).
                    echo = _brightness_echo(kb, baseline["brightness"],
                                            device_identity)
    except (via.DeviceNotFound, OSError) as e:
        # Keyboard gone: RAM-only changes vanish on power cycle anyway.
        log(f"restore: device unavailable ({e})")
    cleared = {"active": None, "generation": state["generation"],
               "baseline": None}
    if echo is not None:
        cleared["brightness_echo"] = echo
    elif not reached_device and "brightness_echo" in state:
        # The keyboard was never opened, so nothing was written and the pair
        # from the last restore still describes what is on it. Once writing has
        # started the old pair is stale and is dropped instead: the next
        # baseline is then taken as read, which is the pre-#58 behaviour.
        cleared["brightness_echo"] = state["brightness_echo"]
    if "last_baseline" in state:
        cleared["last_baseline"] = state["last_baseline"]
        if "last_baseline_device" in state:
            cleared["last_baseline_device"] = state["last_baseline_device"]
    if _wake_reserved(state, time.time()):
        # An hour-long wake-up sleeper is still pending out there; keep its
        # reservation across the idle gap so the next waiting entry does
        # not stack another one behind it.
        cleared["wake_at"] = state["wake_at"]
        cleared["wake_boot"] = state["wake_boot"]
    save_state(cleared)
    log("restore")
    return True


def _spawn_delayed_restore(after, generation):
    """Fire-and-forget `kbd-signal restore --after N --gen G` so the CLI
    itself never has to stay resident. Returns False when the process
    could not be started (the caller may drop a wake reservation that
    now has no sleeper behind it)."""
    cmd = [sys.executable, "-m", "kbd_signal", "restore",
           "--after", str(after), "--gen", str(generation)]
    try:
        subprocess.Popen(cmd, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **_platform.detach_kwargs())
    except OSError as e:
        log(f"delayed restore spawn failed: {e}")
        return False
    return True
