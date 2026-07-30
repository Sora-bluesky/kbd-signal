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
    DEVICE: ClassVar[dict] = {
        "vendor_id": 0x3434,
        "product_id": None,
        "product_match": "K8",
        "v3_channel": 3,
        "reset_on_effect": False,
        "effects": {"solid": 1, "breathing": 2},
    }
    DEVICE_IDENTITY: ClassVar[dict] = {
        "vendor_id": 0x3434,
        "product_id": None,
        "product_match": "K8",
        "v3_channel": 3,
        "effects": {"solid": 1, "breathing": 2},
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
        # Device identity is state input, not host configuration: every test
        # must remain stable when the machine's config.json selects another
        # keyboard.
        self.stack.enter_context(mock.patch.object(
            states.config, "device", return_value=self.DEVICE
        ))

    def _set_waiting_with_snapshot(self, snap):
        with mock.patch.object(FakeKeyboard, "snapshot", return_value=snap):
            states.set_state("waiting", session=self.OWNER)

    def _save_idle_with_last(self, last, device_identity=None):
        state = {
            "active": None,
            "generation": 4,
            "baseline": None,
            "last_baseline": last,
        }
        if device_identity is not None:
            state["last_baseline_device"] = device_identity
        states.save_state(state)

    def _save_active_with_baseline(self, baseline, device_identity=None):
        state = {
            "active": "done",
            "generation": 4,
            "baseline": baseline,
        }
        if device_identity is not None:
            state["last_baseline_device"] = device_identity
        states.save_state(state)

    def test_non_signal_snapshot_stores_baseline_last_and_device(self):
        snap = FakeKeyboard().snapshot()

        self._set_waiting_with_snapshot(snap)

        state = states.load_state()
        self.assertEqual(state["baseline"], snap)
        self.assertEqual(state["last_baseline"], snap)
        self.assertEqual(
            state["last_baseline_device"], self.DEVICE_IDENTITY
        )

    def test_matching_device_fingerprint_adopts_and_restores_baseline(self):
        last = FakeKeyboard().snapshot()
        self._save_idle_with_last(last, self.DEVICE_IDENTITY)

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
        self.assertNotIn("last_baseline_device", state)
        self.assertEqual(state["active"], "waiting")

    def test_restore_carries_last_baseline_and_device_together(self):
        last = FakeKeyboard().snapshot()
        states.save_state({
            "active": "done",
            "generation": 3,
            "baseline": last,
            "last_baseline": last,
            "last_baseline_device": self.DEVICE_IDENTITY,
        })

        states.restore()

        state = states.load_state()
        self.assertEqual(state["last_baseline"], last)
        self.assertEqual(
            state["last_baseline_device"], self.DEVICE_IDENTITY
        )

        states.save_state({
            "active": "done",
            "generation": 4,
            "baseline": last,
        })
        states.restore()

        state = states.load_state()
        self.assertNotIn("last_baseline", state)
        self.assertNotIn("last_baseline_device", state)

    def test_corrupt_baseline_is_rejected_and_restore_goes_dark(self):
        corrupt = {
            **FakeKeyboard().snapshot(),
            "effect": 999,
        }
        self._save_active_with_baseline(corrupt, self.DEVICE_IDENTITY)

        self.assertTrue(states.restore())

        state = states.load_state()
        self.assertIsNone(state["active"])
        self.assertIsNone(state["baseline"])
        self.assertEqual(FakeKeyboard.restored, [])
        self.assertEqual(
            FakeKeyboard.values,
            [(states.via.VALUE_BRIGHTNESS, 0)],
        )
        self.assertEqual(FakeKeyboard.settled, [0])
        with open(states.LOG_FILE, encoding="utf-8") as log_file:
            self.assertIn(
                "restore: baseline rejected "
                "(invalid or from another device)",
                log_file.read(),
            )

    def test_restore_rejects_missing_or_mismatched_device_fingerprint(self):
        baseline = FakeKeyboard().snapshot()
        fingerprints = [
            None,
            {**self.DEVICE_IDENTITY, "product_match": "Q1"},
        ]
        for device_identity in fingerprints:
            with self.subTest(device_identity=device_identity):
                FakeKeyboard.restored = []
                FakeKeyboard.values = []
                FakeKeyboard.settled = []
                self._save_active_with_baseline(
                    baseline, device_identity
                )

                self.assertTrue(states.restore())

                state = states.load_state()
                self.assertIsNone(state["active"])
                self.assertIsNone(state["baseline"])
                self.assertEqual(FakeKeyboard.restored, [])
                self.assertEqual(
                    FakeKeyboard.values,
                    [(states.via.VALUE_BRIGHTNESS, 0)],
                )
                self.assertEqual(FakeKeyboard.settled, [0])

    def test_restore_with_matching_device_fingerprint_repaints(self):
        baseline = FakeKeyboard().snapshot()
        self._save_active_with_baseline(
            baseline, self.DEVICE_IDENTITY
        )

        self.assertTrue(states.restore())

        state = states.load_state()
        self.assertIsNone(state["active"])
        self.assertIsNone(state["baseline"])
        self.assertEqual(FakeKeyboard.restored, [baseline])
        self.assertEqual(FakeKeyboard.values, [])
        self.assertEqual(
            FakeKeyboard.settled,
            [baseline["brightness"]],
        )

    def test_signal_shaped_last_baseline_is_not_adopted(self):
        self._save_idle_with_last(
            self.WAITING_SNAPSHOT, self.DEVICE_IDENTITY
        )

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
            {
                "effect": 5,
                "speed": 90,
                "brightness": 120,
                "color": [True, 200],
            },
        ]
        for last in malformed:
            with self.subTest(last=last):
                self._save_idle_with_last(last, self.DEVICE_IDENTITY)

                self._set_waiting_with_snapshot(self.WAITING_SNAPSHOT)

                state = states.load_state()
                self.assertIsNone(state["baseline"])
                self.assertEqual(state["active"], "waiting")

    def test_out_of_range_snapshot_values_are_not_adopted(self):
        out_of_range = [
            {
                "effect": 999,
                "speed": 90,
                "brightness": 120,
                "color": [20, 200],
            },
            {
                "effect": 5,
                "speed": 90,
                "brightness": 120,
                "color": [300, 200],
            },
            {
                "effect": 5,
                "speed": 90,
                "brightness": 120,
                "color": [20, -1],
            },
        ]
        for last in out_of_range:
            with self.subTest(last=last):
                self._save_idle_with_last(last, self.DEVICE_IDENTITY)

                self._set_waiting_with_snapshot(self.WAITING_SNAPSHOT)

                state = states.load_state()
                self.assertIsNone(state["baseline"])
                self.assertEqual(state["active"], "waiting")

    def test_missing_device_fingerprint_is_not_adopted(self):
        last = FakeKeyboard().snapshot()
        self._save_idle_with_last(last)

        self._set_waiting_with_snapshot(self.WAITING_SNAPSHOT)

        state = states.load_state()
        self.assertIsNone(state["baseline"])
        self.assertEqual(state["active"], "waiting")

    def test_mismatched_device_fingerprint_is_not_adopted(self):
        last = FakeKeyboard().snapshot()
        mismatches = [
            {**self.DEVICE_IDENTITY, "product_match": "Q1"},
            {**self.DEVICE_IDENTITY, "vendor_id": 0x1234},
        ]
        for device_identity in mismatches:
            with self.subTest(device_identity=device_identity):
                self._save_idle_with_last(last, device_identity)

                self._set_waiting_with_snapshot(self.WAITING_SNAPSHOT)

                state = states.load_state()
                self.assertIsNone(state["baseline"])
                self.assertEqual(state["active"], "waiting")

    def test_effects_or_v3_channel_mismatch_is_not_adopted(self):
        last = FakeKeyboard().snapshot()
        mismatches = [
            {**self.DEVICE_IDENTITY, "v3_channel": 4},
            {
                **self.DEVICE_IDENTITY,
                "effects": {"solid": 1, "breathing": 3},
            },
        ]
        for device_identity in mismatches:
            with self.subTest(device_identity=device_identity):
                self._save_idle_with_last(last, device_identity)

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
            idle["last_baseline_device"], self.DEVICE_IDENTITY
        )
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
        state = states.load_state()
        self.assertEqual(state["last_baseline"], good)
        self.assertEqual(
            state["last_baseline_device"], self.DEVICE_IDENTITY
        )
