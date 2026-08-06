"""Baseline brightness must not walk down over signal cycles (#58).

VIA's brightness round trip is not the identity on v3 (#56), so re-reading a
value kbd-signal itself wrote returns a slightly lower number. Taking that as
the next baseline and writing it back compounds: measured on a Q1 HE 8K, 120
settles at 117 but 10 walks to 0 -- a dim backlight goes dark.
"""

import os
import tempfile
import unittest
from unittest import mock

from kbd_signal import states, via

DEVICE = {"vendor_id": 0x3434, "product_id": 0x1012}
OTHER_DEVICE = {"vendor_id": 0x3434, "product_id": 0x0192}


def _snap(brightness, **over):
    snap = {"brightness": brightness, "effect": 16, "speed": 127,
            "color": [0, 255]}
    snap.update(over)
    return snap


class UndoLossyBrightnessTests(unittest.TestCase):
    def test_a_reading_that_matches_our_echo_is_our_own_write(self):
        state = {"brightness_echo": {"written": 120, "readback": 119,
                                     "device": DEVICE}}
        out = states._undo_lossy_brightness(_snap(119), state, DEVICE)
        self.assertEqual(out["brightness"], 120)

    def test_everything_else_in_the_snapshot_is_untouched(self):
        state = {"brightness_echo": {"written": 120, "readback": 119,
                                     "device": DEVICE}}
        snap = _snap(119, effect=7, speed=200, color=[85, 254])
        out = states._undo_lossy_brightness(snap, state, DEVICE)
        self.assertEqual({k: v for k, v in out.items() if k != "brightness"},
                         {k: v for k, v in snap.items() if k != "brightness"})

    def test_a_different_reading_is_the_user_and_wins(self):
        """The correction must never mask a real change, or turning the
        backlight down would silently spring back."""
        state = {"brightness_echo": {"written": 120, "readback": 119,
                                     "device": DEVICE}}
        out = states._undo_lossy_brightness(_snap(40), state, DEVICE)
        self.assertEqual(out["brightness"], 40)

    def test_an_echo_from_another_keyboard_is_ignored(self):
        state = {"brightness_echo": {"written": 120, "readback": 119,
                                     "device": OTHER_DEVICE}}
        out = states._undo_lossy_brightness(_snap(119), state, DEVICE)
        self.assertEqual(out["brightness"], 119)

    def test_no_echo_yet_leaves_the_reading_alone(self):
        self.assertEqual(
            states._undo_lossy_brightness(_snap(119), {}, DEVICE)["brightness"],
            119)

    def test_a_hand_edited_echo_is_rejected(self):
        """state.json is a plain file; a nonsense pair must not reach the
        keyboard as a brightness write."""
        for bad in ({"written": 300, "readback": 119, "device": DEVICE},
                    {"written": 120, "readback": None, "device": DEVICE},
                    {"written": True, "readback": 119, "device": DEVICE},
                    "not a dict"):
            out = states._undo_lossy_brightness(_snap(119),
                                                {"brightness_echo": bad}, DEVICE)
            self.assertEqual(out["brightness"], 119, bad)

    def test_a_lossless_board_is_unaffected(self):
        """VIA v2 at QMK's default limit reads back exactly what was written, so
        written == readback and the correction is the identity. No protocol test
        is needed for that -- the pair says it."""
        state = {"brightness_echo": {"written": 120, "readback": 120,
                                     "device": DEVICE}}
        out = states._undo_lossy_brightness(_snap(120), state, DEVICE)
        self.assertEqual(out["brightness"], 120)


class DecayTests(unittest.TestCase):
    """The cycle itself: capture a baseline, write it back, capture again."""

    # Round trip measured on a Q1 HE 8K (2026-08-06 sweep, 65 points).
    MEASURED = {200: 199, 199: 199, 120: 119, 119: 118, 118: 117, 117: 117,
                10: 9, 9: 7, 7: 6, 6: 5, 5: 3, 3: 2, 2: 1, 1: 0, 0: 0}

    def _cycles(self, start, rounds, correcting):
        """Run `rounds` of snapshot -> restore and return the baselines seen."""
        state, device_brightness, seen = {}, start, []
        for _ in range(rounds):
            snap = _snap(device_brightness)
            if correcting:
                snap = states._undo_lossy_brightness(snap, state, DEVICE)
            baseline = snap["brightness"]
            seen.append(baseline)
            device_brightness = self.MEASURED[baseline]   # restore writes it
            state["brightness_echo"] = {"written": baseline,
                                        "readback": device_brightness,
                                        "device": DEVICE}
        return seen

    def test_a_dim_backlight_goes_dark_without_the_correction(self):
        self.assertEqual(self._cycles(10, 9, correcting=False)[-1], 0)

    def test_the_correction_holds_a_dim_backlight(self):
        self.assertEqual(set(self._cycles(10, 9, correcting=True)), {10})

    def test_the_correction_holds_a_mid_brightness(self):
        self.assertEqual(set(self._cycles(120, 9, correcting=True)), {120})

    def test_without_it_a_mid_brightness_still_drifts(self):
        self.assertEqual(self._cycles(120, 9, correcting=False)[-1], 117)


if __name__ == "__main__":
    unittest.main()


class EchoCaptureTests(unittest.TestCase):
    """How the pair is read, and when a stale one is dropped."""

    def test_the_read_does_not_linger_under_the_state_lock(self):
        """get_value's default of 6 tries is 1.5 s at 250 ms each, and this runs
        with the lock held -- the stall #57 removed. The pair is optional."""
        kb = mock.Mock()
        kb.get_value.return_value = [119]
        states._brightness_echo(kb, 120, {"vendor_id": 1})
        self.assertEqual(kb.get_value.call_args.kwargs.get("tries"), 2)

    def test_a_read_failure_yields_no_pair(self):
        kb = mock.Mock()
        kb.get_value.side_effect = OSError("gone")
        self.assertIsNone(states._brightness_echo(kb, 120, {"vendor_id": 1}))


class EchoPersistenceTests(unittest.TestCase):
    """A pair describes the last thing written to the keyboard, so it survives
    exactly the case where nothing was written."""

    def setUp(self):
        directory = tempfile.mkdtemp()
        for name, value in (("STATE_DIR", directory),
                            ("STATE_FILE", os.path.join(directory, "state.json")),
                            ("ACTIVE_FLAG", os.path.join(directory, "active.flag")),
                            ("LOG_FILE", os.path.join(directory, "log"))):
            patch = mock.patch.object(states, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    ECHO = {"written": 120, "readback": 119, "device": DEVICE}

    def _restore_with(self, keyboard):
        state = {"active": "waiting", "generation": 4, "owners": [],
                 "baseline": _snap(119), "last_baseline": _snap(119),
                 "last_baseline_device": states._device_identity(),
                 "brightness_echo": self.ECHO}
        states.save_state(state)
        with mock.patch.object(states.via, "Keyboard", keyboard):
            states._restore_locked(None, None)
        return states.load_state()

    def test_an_absent_keyboard_keeps_the_pair(self):
        def missing():
            raise via.DeviceNotFound("no raw HID interface")
        self.assertEqual(self._restore_with(missing).get("brightness_echo"),
                         self.ECHO)

    def test_a_write_that_could_not_be_read_back_drops_it(self):
        """Writing started, so the old pair no longer describes the device."""
        kb = mock.MagicMock()
        kb.__enter__ = mock.Mock(return_value=kb)
        kb.__exit__ = mock.Mock(return_value=False)
        kb.apply_snapshot.return_value = True
        kb.settle_brightness.return_value = True
        kb.get_value.side_effect = OSError("gone mid-restore")
        self.assertIsNone(
            self._restore_with(mock.Mock(return_value=kb)).get("brightness_echo"))


class SignalGuardOrderingTests(unittest.TestCase):
    """The correction runs *after* the leftover-signal guard, and that ordering
    is load-bearing.

    _looks_like_signal matches only at the pattern's brightness (255 for every
    signal) or 0. On a board where 255 does not round-trip -- the plain QMK
    formula gives 254 -- the pair becomes (255, 254), and a reading of 254 with
    a signal's effect and color is not a signal until the correction turns it
    into one. Correcting first would hand the guard a value it never saw, throw
    away a good baseline, and restore into the dark: exactly #32's failure mode,
    induced by the fix for #58.
    """

    def setUp(self):
        directory = tempfile.mkdtemp()
        for name, value in (("STATE_DIR", directory),
                            ("STATE_FILE", os.path.join(directory, "state.json")),
                            ("ACTIVE_FLAG", os.path.join(directory, "active.flag")),
                            ("LOG_FILE", os.path.join(directory, "log"))):
            patch = mock.patch.object(states, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_reading_the_correction_would_turn_into_a_signal_is_still_kept(self):
        done = states.patterns()["done"]
        raw = {"effect": done["effect"], "brightness": done["brightness"] - 1,
               "speed": 127, "color": [done["hue"], done["sat"]]}
        self.assertFalse(states._looks_like_signal(raw), "raw must not match")
        corrected = {**raw, "brightness": done["brightness"]}
        self.assertTrue(states._looks_like_signal(corrected),
                        "corrected must match, or this test proves nothing")

        identity = states._device_identity()
        states.save_state({
            "active": None, "generation": 1, "baseline": None,
            "brightness_echo": {"written": done["brightness"],
                                "readback": raw["brightness"],
                                "device": identity}})

        kb = mock.MagicMock()
        kb.__enter__ = mock.Mock(return_value=kb)
        kb.__exit__ = mock.Mock(return_value=False)
        kb.snapshot.return_value = raw
        kb.apply.return_value = True
        with mock.patch.object(states.via, "Keyboard", mock.Mock(return_value=kb)):
            states.set_state("done")

        baseline = states.load_state()["baseline"]
        self.assertIsNotNone(baseline, "the guard saw the corrected value")
        self.assertEqual(baseline["brightness"], done["brightness"])
