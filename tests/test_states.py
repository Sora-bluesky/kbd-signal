import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from unittest import mock

from kbd_signal import states


class FakeKeyboard:
    applied = []
    restored = []
    values = []

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


class StateOwnershipTests(unittest.TestCase):
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
        self.spawn_restore.assert_not_called()

        states.set_state(
            "done", session=claude, owner_prefix="claude:session-a:"
        )
        state = states.load_state()
        self.assertEqual(state["active"], "done")
        self.assertEqual(state["owners"], [])
        self.spawn_restore.assert_called_once()

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
