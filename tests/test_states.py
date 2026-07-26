import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from typing import ClassVar
from unittest import mock

from kbd_signal import states


class FakeKeyboard:
    applied = []
    restored = []
    values = []
    settled: ClassVar[list] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def snapshot(self):
        return {
            "effect": 5,
            "speed": 90,
            "brightness": 120,
            "color": [20, 200],
        }

    def apply(self, **pattern):
        self.applied.append(pattern)
        return True

    def apply_snapshot(self, baseline):
        self.restored.append(baseline)
        return True

    def set_value(self, *values):
        self.values.append(values)

    def set_color(self, hue, sat):
        return True

    def settle_brightness(self, value):
        self.settled.append(value)
        return True


class StateFileTestCase(unittest.TestCase):
    """Shared fixture: state files in a temp dir, VIA replaced by FakeKeyboard."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

        state_dir = self.tmp.name
        self.stack.enter_context(mock.patch.object(states, "STATE_DIR", state_dir))
        self.stack.enter_context(mock.patch.object(
            states, "STATE_FILE", os.path.join(state_dir, "state.json")
        ))
        self.stack.enter_context(mock.patch.object(
            states, "ACTIVE_FLAG", os.path.join(state_dir, "active.flag")
        ))
        self.stack.enter_context(mock.patch.object(
            states, "LOG_FILE", os.path.join(state_dir, "kbd-signal.log")
        ))
        self.stack.enter_context(mock.patch.object(states.via, "Keyboard", FakeKeyboard))
        self.spawn_restore = self.stack.enter_context(mock.patch.object(
            states, "_spawn_delayed_restore"
        ))
        self.stack.enter_context(mock.patch.object(
            states, "load_config", return_value={"restore": "baseline"}
        ))
        self.stack.enter_context(mock.patch.object(
            states,
            "patterns",
            return_value={
                "waiting": {"effect": 2},
                "done": {"effect": 1},
                "error": {"effect": 2},
            },
        ))
        FakeKeyboard.applied = []
        FakeKeyboard.restored = []
        FakeKeyboard.values = []
        FakeKeyboard.settled = []

    def _age_owner(self, owner):
        state = states.load_state()
        state["owner_seen"][owner] = {
            "wall": time.time() - states.WAITING_TTL - 1,
            "mono": time.monotonic() - states.WAITING_TTL - 1,
        }
        states.save_state(state)


class StateOwnershipTests(StateFileTestCase):
    def test_other_product_completion_cannot_clear_waiting(self):
        claude = "claude:session-a:main"
        codex = "codex:session-b:main"
        states.set_state("waiting", session=claude)
        states.set_state("waiting", session=codex)

        states.set_state(
            "done", session=codex, owner_prefix="codex:session-b:"
        )
        state = states.load_state()
        self.assertEqual(state["active"], "waiting")
        self.assertEqual(state["owners"], [claude])
        # No done-restore was scheduled (the only spawn so far is the
        # waiting transition's TTL wake-up).
        self.assertNotIn(
            states.DONE_RESTORE_AFTER,
            [c.args[0] for c in self.spawn_restore.call_args_list],
        )

        states.set_state(
            "done", session=claude, owner_prefix="claude:session-a:"
        )
        state = states.load_state()
        self.assertEqual(state["active"], "done")
        self.assertEqual(state["owners"], [])
        self.assertEqual(self.spawn_restore.call_args.args[0],
                         states.DONE_RESTORE_AFTER)

    def test_non_owner_completion_is_blocked(self):
        states.set_state("waiting", session="claude:session-a:main")
        generation = states.load_state()["generation"]

        states.set_state(
            "done",
            session="codex:session-b:main",
            owner_prefix="codex:session-b:",
        )

        state = states.load_state()
        self.assertEqual(state["active"], "waiting")
        self.assertEqual(state["generation"], generation)

    def test_main_stop_releases_its_subagents_but_not_other_sessions(self):
        owners = [
            "codex:session-a:agent-1",
            "codex:session-a:agent-2",
            "claude:session-b:main",
        ]
        for owner in owners:
            states.set_state("waiting", session=owner)

        states.set_state(
            "done",
            session="codex:session-a:main",
            owner_prefix="codex:session-a:",
        )

        state = states.load_state()
        self.assertEqual(state["active"], "waiting")
        self.assertEqual(state["owners"], ["claude:session-b:main"])

    def test_subagent_release_keeps_sibling_waiting(self):
        first = "codex:session-a:agent-1"
        second = "codex:session-a:agent-2"
        states.set_state("waiting", session=first)
        states.set_state("waiting", session=second)

        states.release_waiting(session=first)

        state = states.load_state()
        self.assertEqual(state["active"], "waiting")
        self.assertEqual(state["owners"], [second])

    def test_waiting_only_release_does_not_clear_done(self):
        states.set_state("done", session="codex:session-a:main")
        before = states.load_state()

        states.release_waiting(session="claude:session-b:main")

        self.assertEqual(states.load_state(), before)
        self.assertEqual(FakeKeyboard.restored, [])

    def test_waiting_only_release_does_not_clear_sticky_error(self):
        states.set_state("error")
        before = states.load_state()

        states.release_waiting(session="claude:session-a:main")

        self.assertEqual(states.load_state(), before)
        self.assertEqual(FakeKeyboard.restored, [])

    def test_v020_raw_session_owner_is_migrated_to_namespaced_owner(self):
        baseline = FakeKeyboard().snapshot()
        states.save_state({
            "active": "waiting",
            "generation": 4,
            "baseline": baseline,
            "owners": ["legacy-session"],
        })

        states.set_state(
            "waiting",
            session="codex:legacy-session:main",
            owner_aliases=("legacy-session",),
        )
        state = states.load_state()
        self.assertEqual(state["owners"], ["codex:legacy-session:main"])

        states.set_state(
            "done",
            session="codex:legacy-session:main",
            owner_prefix="codex:legacy-session:",
            owner_aliases=("legacy-session",),
        )
        self.assertEqual(states.load_state()["active"], "done")

    def test_concurrent_permission_requests_keep_both_owners(self):
        barrier = threading.Barrier(2)

        def enter_waiting(owner):
            barrier.wait()
            return states.set_state("waiting", session=owner)

        owners = ["claude:session-a:main", "codex:session-b:main"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(enter_waiting, owners))

        self.assertEqual(results, [True, True])
        self.assertEqual(set(states.load_state()["owners"]), set(owners))

    def test_state_file_remains_valid_json(self):
        states.set_state("waiting", session="codex:session-a:main")
        with open(states.STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(state["active"], "waiting")


class WaitingTTLTests(StateFileTestCase):
    """Stale waiting owners expire after WAITING_TTL (#31).

    Timestamps are made stale by rewriting owner_seen in the saved state
    (both the wall and the monotonic half), so no test ever sleeps or
    mocks the clock.
    """

    def test_stale_last_owner_expires_and_restore_runs(self):
        stale = "claude:session-a:main"
        states.set_state("waiting", session=stale)
        self._age_owner(stale)

        # Another session's turn-end event triggers the expiry sweep.
        states.release_waiting(session="codex:session-b:main")

        state = states.load_state()
        self.assertIsNone(state["active"])
        self.assertEqual(FakeKeyboard.restored, [FakeKeyboard().snapshot()])

    def test_fresh_owner_is_not_expired(self):
        fresh = "claude:session-a:main"
        states.set_state("waiting", session=fresh)

        states.release_waiting(session="codex:session-b:main")

        state = states.load_state()
        self.assertEqual(state["active"], "waiting")
        self.assertEqual(state["owners"], [fresh])
        self.assertEqual(FakeKeyboard.restored, [])

    def test_legacy_state_without_owner_seen_is_touched_not_expired(self):
        owner = "claude:session-a:main"
        states.save_state({
            "active": "waiting",
            "generation": 3,
            "baseline": FakeKeyboard().snapshot(),
            "owners": [owner],
        })

        states.release_waiting(session="codex:session-b:main")

        state = states.load_state()
        self.assertEqual(state["active"], "waiting")
        self.assertEqual(state["owners"], [owner])
        self.assertIn(owner, state["owner_seen"])

    def test_only_the_stale_owner_drops_when_mixed_with_fresh(self):
        stale = "claude:session-a:main"
        fresh = "codex:session-b:main"
        states.set_state("waiting", session=stale)
        states.set_state("waiting", session=fresh)
        self._age_owner(stale)

        states.release_waiting(session="gemini:session-c:main")

        state = states.load_state()
        self.assertEqual(state["active"], "waiting")
        self.assertEqual(state["owners"], [fresh])
        self.assertEqual(FakeKeyboard.restored, [])

    def test_reentering_waiting_refreshes_owner_seen(self):
        owner = "claude:session-a:main"
        states.set_state("waiting", session=owner)
        # Backdate but keep within the TTL so the refresh is observable.
        backdated = time.time() - states.WAITING_TTL + 600
        state = states.load_state()
        state["owner_seen"][owner]["wall"] = backdated
        states.save_state(state)

        states.set_state("waiting", session=owner)

        state = states.load_state()
        self.assertEqual(state["owners"], [owner])
        self.assertGreater(state["owner_seen"][owner]["wall"], backdated)

    def test_stale_generation_restore_still_clears_orphaned_waiting(self):
        stale = "claude:session-a:main"
        states.set_state("waiting", session=stale)
        self._age_owner(stale)
        generation = states.load_state()["generation"]

        # A superseded delayed restore still runs the expiry sweep. Once the
        # sweep dropped the last owner there is no later hook, so the restore
        # must run anyway or the orange signal is stranded.
        states.restore(generation=generation - 1)

        state = states.load_state()
        self.assertIsNone(state["active"])
        self.assertEqual(FakeKeyboard.restored, [FakeKeyboard().snapshot()])

    def test_stale_generation_restore_keeps_fresh_waiting(self):
        fresh = "claude:session-a:main"
        states.set_state("waiting", session=fresh)
        generation = states.load_state()["generation"]

        states.restore(generation=generation - 1)

        state = states.load_state()
        self.assertEqual(state["active"], "waiting")
        self.assertEqual(state["owners"], [fresh])
        self.assertEqual(FakeKeyboard.restored, [])

    def test_partial_expiry_under_stale_generation_keeps_waiting(self):
        stale = "claude:session-a:main"
        fresh = "codex:session-b:main"
        states.set_state("waiting", session=stale)
        states.set_state("waiting", session=fresh)
        self._age_owner(stale)
        generation = states.load_state()["generation"]

        states.restore(generation=generation - 1)

        # The stale owner expired, but another owner still holds the signal:
        # the superseded restore stays superseded.
        state = states.load_state()
        self.assertEqual(state["active"], "waiting")
        self.assertEqual(state["owners"], [fresh])
        self.assertEqual(FakeKeyboard.restored, [])

    def test_owner_readded_by_ttl_unaware_writer_is_not_expired(self):
        owner = "claude:session-a:main"
        states.set_state("waiting", session=owner)
        self._age_owner(owner)
        # Simulate v1.0.1 releasing and re-adding the owner: it rewrites
        # owners and bumps the generation, but round-trips owner_seen
        # untouched — the stale timestamp now describes a fresh approval.
        state = states.load_state()
        state["generation"] += 1
        states.save_state(state)

        states.release_waiting(session="codex:session-b:main")

        state = states.load_state()
        self.assertEqual(state["active"], "waiting")
        self.assertEqual(state["owners"], [owner])
        self.assertEqual(FakeKeyboard.restored, [])
        # The sweep restarted the owner's clock instead of trusting it.
        self.assertLess(
            time.time() - state["owner_seen"][owner]["wall"],
            states.WAITING_TTL,
        )

    def test_forward_clock_step_does_not_expire_fresh_owner(self):
        owner = "claude:session-a:main"
        states.set_state("waiting", session=owner)
        # Simulate a clock correction: the wall timestamp claims the
        # approval is over an hour old, but the monotonic clock shows it is
        # seconds old.
        state = states.load_state()
        state["owner_seen"][owner]["wall"] = (
            time.time() - states.WAITING_TTL - 1
        )
        states.save_state(state)

        states.release_waiting(session="codex:session-b:main")

        state = states.load_state()
        self.assertEqual(state["active"], "waiting")
        self.assertEqual(state["owners"], [owner])
        self.assertEqual(FakeKeyboard.restored, [])

    def test_reboot_restarts_monotonic_clock_instead_of_expiring(self):
        owner = "claude:session-a:main"
        states.set_state("waiting", session=owner)
        # After a reboot the monotonic clock restarts, so a stored value can
        # exceed the current one. The sweep must reset that half, not expire.
        state = states.load_state()
        state["owner_seen"][owner] = {
            "wall": time.time() - states.WAITING_TTL - 1,
            "mono": time.monotonic() + 10_000_000,
        }
        states.save_state(state)

        states.release_waiting(session="codex:session-b:main")

        state = states.load_state()
        self.assertEqual(state["active"], "waiting")
        self.assertEqual(state["owners"], [owner])
        self.assertLessEqual(
            state["owner_seen"][owner]["mono"], time.monotonic()
        )

    def test_device_failure_does_not_strand_stale_owner(self):
        stale = "claude:session-a:main"
        states.set_state("waiting", session=stale)
        self._age_owner(stale)

        # A main-session Stop can be the final lifecycle event; if the HID
        # open fails transiently, the sweep must already be persisted or the
        # stale owner survives indefinitely — the exact bug of #31.
        with mock.patch.object(
            states.via, "Keyboard",
            side_effect=states.via.DeviceNotFound("transient"),
        ):
            result = states.set_state(
                "done",
                session="codex:session-b:main",
                owner_prefix="codex:session-b:",
            )

        self.assertFalse(result)
        state = states.load_state()
        self.assertEqual(state["owners"], [])
        self.assertEqual(FakeKeyboard.restored, [])
        # The next event with a working device fully clears the signal.
        states.release_waiting(session="codex:session-b:main")
        self.assertIsNone(states.load_state()["active"])
        self.assertEqual(FakeKeyboard.restored, [FakeKeyboard().snapshot()])

    def test_corrupt_config_still_sweeps_stale_owner(self):
        stale = "claude:session-a:main"
        states.set_state("waiting", session=stale)
        self._age_owner(stale)

        # patterns() reads the config and can raise on a corrupt file. The
        # sweep runs before it under its own short lock, so even then a
        # stale owner cannot survive the event.
        with mock.patch.object(
            states, "patterns", side_effect=KeyError("effects"),
        ), self.assertRaises(KeyError):
            states.set_state(
                "done",
                session="codex:session-b:main",
                owner_prefix="codex:session-b:",
            )

        state = states.load_state()
        self.assertEqual(state["owners"], [])
        self.assertEqual(state["active"], "waiting")  # cleared on next event

    def test_orphaned_by_failed_event_recovers_via_stale_restore(self):
        # The sweep drops the last owner, then the same event dies before
        # it can restore (corrupt config). The orphaning is recorded, so
        # even a later restore with a mismatched generation — possibly the
        # only event left — bypasses its guard and turns the lights off.
        stale = "claude:session-a:main"
        states.set_state("waiting", session=stale)
        self._age_owner(stale)
        generation = states.load_state()["generation"]

        with mock.patch.object(
            states, "patterns", side_effect=KeyError("effects"),
        ), self.assertRaises(KeyError):
            states.set_state("done", session="codex:session-b:main",
                             owner_prefix="codex:session-b:")

        states.restore(generation=generation - 1)

        state = states.load_state()
        self.assertIsNone(state["active"])
        self.assertEqual(FakeKeyboard.restored, [FakeKeyboard().snapshot()])

    def test_orphaned_after_device_failure_recovers_via_stale_restore(self):
        # Same recovery, with the event dying at the device-open step.
        stale = "claude:session-a:main"
        states.set_state("waiting", session=stale)
        self._age_owner(stale)
        generation = states.load_state()["generation"]

        with mock.patch.object(
            states.via, "Keyboard",
            side_effect=states.via.DeviceNotFound("transient"),
        ):
            states.set_state("done", session="codex:session-b:main",
                             owner_prefix="codex:session-b:")

        states.restore(generation=generation - 1)

        self.assertIsNone(states.load_state()["active"])
        self.assertEqual(FakeKeyboard.restored, [FakeKeyboard().snapshot()])

    def test_fresh_waiting_after_orphaning_is_not_killed_by_stale_restore(self):
        # A real transition clears the recorded orphaning: once a fresh
        # approval holds the signal again, a superseded delayed restore
        # must stay superseded.
        stale = "claude:session-a:main"
        states.set_state("waiting", session=stale)
        self._age_owner(stale)
        with mock.patch.object(
            states.via, "Keyboard",
            side_effect=states.via.DeviceNotFound("transient"),
        ):
            states.set_state("done", session="codex:session-b:main",
                             owner_prefix="codex:session-b:")

        fresh = "codex:session-c:main"
        states.set_state("waiting", session=fresh)
        generation = states.load_state()["generation"]

        states.restore(generation=generation - 1)

        state = states.load_state()
        self.assertEqual(state["active"], "waiting")
        self.assertEqual(state["owners"], [fresh])
        self.assertEqual(FakeKeyboard.restored, [])

    def test_done_is_unblocked_after_the_blocker_expires(self):
        blocker = "claude:session-a:main"
        states.set_state("waiting", session=blocker)
        self._age_owner(blocker)

        states.set_state(
            "done",
            session="codex:session-b:main",
            owner_prefix="codex:session-b:",
        )

        state = states.load_state()
        self.assertEqual(state["active"], "done")
        self.assertEqual(state["owners"], [])
        # The done-restore was scheduled (waiting's TTL wake-up aside).
        self.assertEqual(self.spawn_restore.call_args.args[0],
                         states.DONE_RESTORE_AFTER)


class TTLWakeUpTests(StateFileTestCase):
    """Entering waiting schedules its own TTL wake-up (#31, #33).

    The expiry sweep only runs when some later hook fires; a lone session
    that dies at its PermissionRequest never sends one, so the orange
    stayed until an unrelated event (the 1h20m incident). The waiting
    transition therefore spawns a delayed restore of its own — while
    keeping at most ~one such hour-long sleeper pending, because
    PermissionRequest fires constantly.
    """

    def test_entering_waiting_schedules_the_ttl_wake_up(self):
        states.set_state("waiting", session="claude:session-a:main")

        generation = states.load_state()["generation"]
        self.spawn_restore.assert_called_once_with(
            states.WAITING_WAKE_AFTER, generation
        )

    def test_reserved_wake_up_is_not_stacked(self):
        states.set_state("waiting", session="claude:session-a:main")
        states.set_state("waiting", session="codex:session-b:main")

        self.assertEqual(self.spawn_restore.call_count, 1)

    def test_lapsed_reservation_is_rearmed_on_the_next_waiting_entry(self):
        states.set_state("waiting", session="claude:session-a:main")
        state = states.load_state()
        state["wake_at"] = time.time() - 1  # the reserved wake has fired
        states.save_state(state)

        states.set_state("waiting", session="codex:session-b:main")

        self.assertEqual(self.spawn_restore.call_count, 2)

    def test_release_event_rearms_a_lapsed_wake_for_surviving_owners(self):
        # An owner that joined mid-chain outlives the reserved wake. Once
        # the reservation has lapsed, the wake's own restore event (or any
        # later one) must arm a follow-up, or that owner could strand.
        survivor = "claude:session-a:main"
        states.set_state("waiting", session=survivor)
        state = states.load_state()
        state["wake_at"] = time.time() - 1
        states.save_state(state)

        states.release_waiting(session="codex:session-b:main")

        self.assertEqual(
            [c.args[0] for c in self.spawn_restore.call_args_list],
            [states.WAITING_WAKE_AFTER, states.WAITING_WAKE_AFTER],
        )
        self.assertEqual(states.load_state()["owners"], [survivor])

    def test_full_restore_keeps_the_pending_wake_reservation(self):
        owner = "claude:session-a:main"
        states.set_state("waiting", session=owner)
        states.restore(session=owner)  # approval answered, back to idle

        states.set_state("waiting", session=owner)  # next PermissionRequest

        # The hour-long sleeper from the first entry is still out there; a
        # second one must not stack behind it.
        self.assertEqual(self.spawn_restore.call_count, 1)

    def test_ttl_wake_up_clears_lone_dead_sessions(self):
        first = "claude:session-a:main"
        second = "codex:session-b:main"
        states.set_state("waiting", session=first)
        delay, generation = self.spawn_restore.call_args.args
        self.assertEqual(delay, states.WAITING_WAKE_AFTER)
        states.set_state("waiting", session=second)  # supersedes generation
        self._age_owner(first)
        self._age_owner(second)

        # Both sessions are dead: the reserved wake-up is the only event
        # left. Its restore carries the spawn-time (now stale) generation;
        # the sweep expires both owners and the durable-orphan bypass lets
        # it turn the lights off anyway.
        states.restore(generation=generation)

        self.assertIsNone(states.load_state()["active"])
        self.assertEqual(FakeKeyboard.restored, [FakeKeyboard().snapshot()])

    def test_reboot_invalidates_the_wake_reservation(self):
        # The sleeper died with the OS. Its reservation carries a monotonic
        # mark from the previous boot, which reads as larger than the
        # current clock — the next waiting entry must spawn a replacement.
        states.set_state("waiting", session="claude:session-a:main")
        state = states.load_state()
        state["wake_mono"] = time.monotonic() + 10_000_000
        states.save_state(state)

        states.set_state("waiting", session="codex:session-b:main")

        self.assertEqual(self.spawn_restore.call_count, 2)

    def test_failed_rearm_spawn_drops_the_reservation_too(self):
        # The restore-side rearm follows the same liveness rule as the
        # waiting-entry site (found in review: only one of the two spawn
        # sites cancelled on failure).
        survivor = "claude:session-a:main"
        states.set_state("waiting", session=survivor)
        state = states.load_state()
        state["wake_at"] = time.time() - 1  # the reserved wake has lapsed
        states.save_state(state)

        self.spawn_restore.return_value = False
        states.release_waiting(session="codex:session-b:main")

        self.assertNotIn("wake_at", states.load_state())

    def test_failed_wake_spawn_drops_the_reservation(self):
        # A reservation without a sleeper behind it would suppress every
        # replacement; when the spawn fails the reservation must go too.
        self.spawn_restore.return_value = False
        states.set_state("waiting", session="claude:session-a:main")

        self.assertNotIn("wake_at", states.load_state())

        self.spawn_restore.return_value = True
        states.set_state("waiting", session="codex:session-b:main")
        self.assertEqual(self.spawn_restore.call_count, 2)


class SweepLockBudgetTests(StateFileTestCase):
    """set_state acquires the state lock twice (sweep, then transition).

    Two full 3 s acquisitions could burn ~6 s under contention, past the
    documented 5 s hook deadline. The best-effort sweep phase must ask for
    less so the worst case stays below it (#33).
    """

    def test_sweep_phase_requests_a_shorter_lock_timeout(self):
        timeouts = []
        real_lock = states._state_lock

        def recording_lock(timeout=3.0):
            timeouts.append(timeout)
            return real_lock(timeout=timeout)

        with mock.patch.object(states, "_state_lock", recording_lock):
            states.set_state("waiting", session="claude:session-a:main")

        self.assertEqual(timeouts, [1.0, 3.0])


class SignalBaselineGuardTests(StateFileTestCase):
    """Snapshots that look like a leftover signal are never adopted (#32).

    A baseline captured while the keyboard still shows an earlier signal
    would save the signal itself as the user's setting, and every later
    restore would write it back — self-perpetuating pollution.
    """

    FULL_PATTERNS: ClassVar[dict] = {
        "waiting": {"effect": 2, "hue": 21, "sat": 255, "speed": 170,
                    "brightness": 255},
        "done": {"effect": 1, "hue": 85, "sat": 255, "brightness": 255},
        "error": {"effect": 2, "hue": 0, "sat": 255, "speed": 255,
                  "brightness": 255},
    }

    def setUp(self):
        super().setUp()
        # The shared fixture stubs patterns() down to bare effects; the
        # guard compares every field, so give it the full shape.
        self.stack.enter_context(mock.patch.object(
            states, "patterns", return_value=self.FULL_PATTERNS
        ))

    def _set_waiting_with_snapshot(self, snap):
        with mock.patch.object(FakeKeyboard, "snapshot", return_value=snap):
            states.set_state("waiting", session="claude:session-a:main")

    def test_waiting_shaped_snapshot_is_not_adopted_as_baseline(self):
        self._set_waiting_with_snapshot({
            "effect": 2, "speed": 170, "brightness": 255, "color": [21, 255],
        })

        state = states.load_state()
        self.assertIsNone(state["baseline"])
        # The signal itself still applies.
        self.assertEqual(state["active"], "waiting")
        self.assertEqual(FakeKeyboard.applied,
                         [self.FULL_PATTERNS["waiting"]])

    def test_done_shaped_snapshot_matches_without_speed_key(self):
        # The done pattern has no speed key, so any snapshot speed matches.
        self._set_waiting_with_snapshot({
            "effect": 1, "speed": 42, "brightness": 255, "color": [85, 255],
        })

        self.assertIsNone(states.load_state()["baseline"])

    def test_normal_snapshot_is_adopted_unchanged(self):
        states.set_state("waiting", session="claude:session-a:main")

        self.assertEqual(states.load_state()["baseline"],
                         FakeKeyboard().snapshot())

    def test_baseline_mode_without_baseline_still_goes_dark(self):
        # Discarding a polluted snapshot leaves baseline None. Under the
        # default "baseline" restore mode there is then nothing to repaint,
        # and skipping the keyboard entirely would leave the last signal
        # lit forever — the restore must still darken it (#35 review).
        self._set_waiting_with_snapshot({
            "effect": 2, "speed": 170, "brightness": 255, "color": [21, 255],
        })
        self.assertIsNone(states.load_state()["baseline"])

        states.restore(session="claude:session-a:main")

        state = states.load_state()
        self.assertIsNone(state["active"])
        self.assertIn((states.via.VALUE_BRIGHTNESS, 0), FakeKeyboard.values)
        self.assertIn(0, FakeKeyboard.settled)
        self.assertEqual(FakeKeyboard.restored, [])  # nothing to repaint

    def test_one_field_off_a_signal_pattern_is_still_adopted(self):
        snap = {"effect": 2, "speed": 170, "brightness": 254,
                "color": [21, 255]}  # brightness one off waiting's 255
        self._set_waiting_with_snapshot(snap)

        self.assertEqual(states.load_state()["baseline"], snap)

    def test_dark_leftover_from_off_restore_is_not_adopted(self):
        # An off-mode restore with no baseline can only dim the leftover
        # signal, so the next snapshot is the same pattern at brightness 0.
        # Adopting it would hand the guard's own residue back to it and
        # restart the pollution loop one cycle later.
        self._set_waiting_with_snapshot({
            "effect": 2, "speed": 170, "brightness": 255, "color": [21, 255],
        })
        with mock.patch.object(
            states, "load_config", return_value={"restore": "off"}
        ):
            states.restore(session="claude:session-a:main")
        self.assertEqual(FakeKeyboard.values,
                         [(states.via.VALUE_BRIGHTNESS, 0)])

        self._set_waiting_with_snapshot({
            "effect": 2, "speed": 170, "brightness": 0, "color": [21, 255],
        })

        state = states.load_state()
        self.assertIsNone(state["baseline"])
        self.assertEqual(state["active"], "waiting")

    def test_dark_non_signal_snapshot_is_still_adopted(self):
        # Brightness 0 alone is not a signal marker: a user's genuinely
        # dark keyboard with its own effect/color keeps baseline capture.
        snap = {"effect": 5, "speed": 90, "brightness": 0, "color": [20, 200]}
        self._set_waiting_with_snapshot(snap)

        self.assertEqual(states.load_state()["baseline"], snap)

    def test_restore_off_without_baseline_only_writes_brightness_zero(self):
        self._set_waiting_with_snapshot({
            "effect": 2, "speed": 170, "brightness": 255, "color": [21, 255],
        })

        with mock.patch.object(
            states, "load_config", return_value={"restore": "off"}
        ):
            states.restore(session="claude:session-a:main")

        self.assertIsNone(states.load_state()["active"])
        self.assertEqual(FakeKeyboard.values,
                         [(states.via.VALUE_BRIGHTNESS, 0)])
        self.assertEqual(FakeKeyboard.restored, [])


class RestoreBrightnessSettleTests(StateFileTestCase):
    """Restore settles the final brightness with read-back (#34).

    Measured on a K8 Pro: after a sequence that includes an effect change,
    the firmware can ACK a lone brightness write and silently revert it —
    three production repros ended restore('off') at brightness 255. Both
    restore branches therefore route the final brightness through the
    settle_brightness helper.
    """

    def _enter_waiting(self):
        states.set_state("waiting", session="claude:session-a:main")

    def test_off_restore_settles_brightness_to_zero(self):
        self._enter_waiting()

        with mock.patch.object(
            states, "load_config", return_value={"restore": "off"}
        ):
            states.restore(session="claude:session-a:main")

        self.assertEqual(FakeKeyboard.settled, [0])

    def test_baseline_restore_settles_snapshot_brightness(self):
        self._enter_waiting()

        states.restore(session="claude:session-a:main")

        self.assertEqual(FakeKeyboard.settled,
                         [FakeKeyboard().snapshot()["brightness"]])

    def test_unsettled_brightness_logs_and_does_not_raise(self):
        self._enter_waiting()

        with mock.patch.object(
            FakeKeyboard, "settle_brightness", return_value=False,
        ), mock.patch.object(
            states, "load_config", return_value={"restore": "off"}
        ):
            states.restore(session="claude:session-a:main")  # must not raise

        self.assertIsNone(states.load_state()["active"])
        with open(states.LOG_FILE, encoding="utf-8") as f:
            self.assertIn("brightness 0 not confirmed", f.read())

    def test_unconfirmed_color_keeps_baseline_restore_dark(self):
        # apply_snapshot returning False means the color never settled and
        # the LEDs were left dark; raising brightness onto the wrong color
        # would defeat that guard, so no settle may run.
        self._enter_waiting()

        with mock.patch.object(
            FakeKeyboard, "apply_snapshot", return_value=False,
        ):
            states.restore(session="claude:session-a:main")

        self.assertIsNone(states.load_state()["active"])
        self.assertEqual(FakeKeyboard.settled, [])


class LogRotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

        state_dir = self.tmp.name
        self.log_file = os.path.join(state_dir, "kbd-signal.log")
        self.stack.enter_context(mock.patch.object(states, "STATE_DIR", state_dir))
        self.stack.enter_context(
            mock.patch.object(states, "LOG_FILE", self.log_file)
        )
        # Small cap so a couple of lines trip rotation.
        self.stack.enter_context(
            mock.patch.object(states, "LOG_MAX_BYTES", 80)
        )

    def test_no_rotation_below_limit(self):
        states.log("small")
        self.assertTrue(os.path.exists(self.log_file))
        self.assertFalse(os.path.exists(self.log_file + ".1"))

    def test_rotates_once_over_limit(self):
        for i in range(5):
            states.log(f"line {i}")
        # Once over the cap the live log is rotated to `.1` and a fresh one
        # starts; total on disk stays bounded at ~2x the cap.
        self.assertTrue(os.path.exists(self.log_file + ".1"))
        self.assertLessEqual(os.path.getsize(self.log_file), states.LOG_MAX_BYTES)

    def test_rotation_failure_is_swallowed(self):
        # A sharing violation (Windows) or any OSError from os.replace must
        # not propagate — the line is written, the rotation is simply skipped.
        #
        # Grow the live log to just under the cap so the *next* write is the
        # one that trips rotation, and mock os.replace only for that write —
        # otherwise an earlier line rotates for real and resets the file,
        # leaving the failure path unexercised.
        states.log("pad")
        line_bytes = os.path.getsize(self.log_file)
        while os.path.getsize(self.log_file) + line_bytes < states.LOG_MAX_BYTES:
            states.log("pad")
        self.assertLess(os.path.getsize(self.log_file), states.LOG_MAX_BYTES)

        with mock.patch.object(
            states.os, "replace", side_effect=OSError
        ) as replace:
            states.log("this line trips rotation")  # must not raise
        replace.assert_called_once()  # the failing rotation was attempted
        self.assertTrue(os.path.exists(self.log_file))  # line still written
        # os.replace failed, so no generation was produced.
        self.assertFalse(os.path.exists(self.log_file + ".1"))


if __name__ == "__main__":
    unittest.main()
