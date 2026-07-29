from typing import ClassVar
from unittest import mock

from kbd_signal import states
from test_states import FakeKeyboard, StateFileTestCase


class LastBaselineTests(StateFileTestCase):
    """A last-known-good capture breaks signal-shaped baseline loops (#39)."""

    FULL_PATTERNS: ClassVar[dict] = {
        "waiting": {"effect": 2, "hue": 21, "sat": 255, "speed": 170,
                    "brightness": 255},
        "done": {"effect": 1, "hue": 85, "sat": 255, "brightness": 255},
        "error": {"effect": 2, "hue": 0, "sat": 255, "speed": 255,
                  "brightness": 255},
    }
    OWNER = "claude:session-a:main"
    WAITING_SNAPSHOT = {
        "effect": 2,
        "speed": 170,
        "brightness": 255,
        "color": [21, 255],
    }
    DARK_WAITING_SNAPSHOT = {
        "effect": 2,
        "speed": 170,
        "brightness": 0,
        "color": [21, 255],
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
            states.set_state("waiting", session=self.OWNER)

    def _save_idle_with_last(self, last):
        states.save_state({
            "active": None,
            "generation": 4,
            "baseline": None,
            "last_baseline": last,
        })

    def test_non_signal_snapshot_stores_baseline_and_last_baseline(self):
        snap = FakeKeyboard().snapshot()

        self._set_waiting_with_snapshot(snap)

        state = states.load_state()
        self.assertEqual(state["baseline"], snap)
        self.assertEqual(state["last_baseline"], snap)

    def test_signal_snapshot_uses_stored_last_baseline_and_restores_it(self):
        last = FakeKeyboard().snapshot()
        self._save_idle_with_last(last)

        self._set_waiting_with_snapshot(self.WAITING_SNAPSHOT)

        state = states.load_state()
        self.assertEqual(state["baseline"], last)
        self.assertEqual(state["active"], "waiting")
        with open(states.LOG_FILE, encoding="utf-8") as log_file:
            self.assertIn(
                "set waiting: snapshot matches a signal pattern, "
                "using last known baseline",
                log_file.read(),
            )

        states.restore(session=self.OWNER)

        self.assertEqual(FakeKeyboard.restored, [last])

    def test_signal_snapshot_without_last_baseline_keeps_none(self):
        self._set_waiting_with_snapshot(self.WAITING_SNAPSHOT)

        state = states.load_state()
        self.assertIsNone(state["baseline"])
        self.assertNotIn("last_baseline", state)
        self.assertEqual(state["active"], "waiting")

    def test_restore_carries_present_last_baseline_only(self):
        last = FakeKeyboard().snapshot()
        states.save_state({
            "active": "done",
            "generation": 3,
            "baseline": last,
            "last_baseline": last,
        })

        states.restore()

        self.assertEqual(states.load_state()["last_baseline"], last)

        states.save_state({
            "active": "done",
            "generation": 4,
            "baseline": last,
        })
        states.restore()

        self.assertNotIn("last_baseline", states.load_state())

    def test_signal_shaped_last_baseline_is_not_adopted(self):
        self._save_idle_with_last(self.WAITING_SNAPSHOT)

        self._set_waiting_with_snapshot(self.DARK_WAITING_SNAPSHOT)

        state = states.load_state()
        self.assertIsNone(state["baseline"])
        self.assertEqual(state["active"], "waiting")

    def test_malformed_last_baseline_is_ignored_safely(self):
        malformed = [
            "not a snapshot",
            {
                "effect": 5,
                "speed": 90,
                "brightness": 120,
                "color": [20],
            },
            {
                "effect": True,
                "speed": 90,
                "brightness": 120,
                "color": [20, 200],
            },
        ]
        for last in malformed:
            with self.subTest(last=last):
                self._save_idle_with_last(last)

                self._set_waiting_with_snapshot(self.WAITING_SNAPSHOT)

                state = states.load_state()
                self.assertIsNone(state["baseline"])
                self.assertEqual(state["active"], "waiting")

    def test_valid_snapshot_accepts_tuple_color(self):
        snap = FakeKeyboard().snapshot()
        snap["color"] = tuple(snap["color"])

        self.assertTrue(states._valid_snapshot(snap))

    def test_absorbing_state_exit_repaints_last_good_capture(self):
        good = FakeKeyboard().snapshot()
        self._set_waiting_with_snapshot(good)

        # Recreate the incident boundary: the signal is active, its current
        # baseline was lost, and only the durable non-signal capture remains.
        active = states.load_state()
        active["baseline"] = None
        states.save_state(active)

        with mock.patch.object(
            states, "load_config", return_value={"restore": "off"}
        ):
            states.restore(session=self.OWNER)

        idle = states.load_state()
        self.assertIsNone(idle["baseline"])
        self.assertEqual(idle["last_baseline"], good)
        self.assertEqual(
            FakeKeyboard.values,
            [(states.via.VALUE_BRIGHTNESS, 0)],
        )

        self._set_waiting_with_snapshot(self.DARK_WAITING_SNAPSHOT)

        state = states.load_state()
        self.assertEqual(state["baseline"], good)
        self.assertEqual(state["active"], "waiting")

        states.restore(session=self.OWNER)

        self.assertEqual(FakeKeyboard.restored, [good])
        self.assertEqual(states.load_state()["last_baseline"], good)
