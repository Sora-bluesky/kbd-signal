"""Baseline brightness must not walk down over signal cycles (#58).

VIA's brightness round trip is not the identity on v3 (#56), so re-reading a
value kbd-signal itself wrote returns a slightly lower number. Taking that as
the next baseline and writing it back compounds: measured on a Q1 HE 8K, 120
settles at 117 but 10 walks to 0 -- a dim backlight goes dark.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from kbd_signal import config, states, via

DEVICE = {"vendor_id": 0x3434, "product_id": 0x1012}
OTHER_DEVICE = {"vendor_id": 0x3434, "product_id": 0x0192}


def _snap(brightness, **over):
    snap = {"brightness": brightness, "effect": 16, "speed": 127,
            "color": [0, 255]}
    snap.update(over)
    return snap


class _IsolatedState(unittest.TestCase):
    """State dir, config and the detached restore all pointed away from the
    machine running the tests.

    config.CONFIG_FILE matters: _restore_locked reads `restore` from it, so
    without this the suite takes the dark path on any developer running
    `{"restore": "off"}` -- a documented setting -- and three tests here fail.
    CI passes only because its runners have no config.json at all.

    _spawn_delayed_restore matters more: set_state("done") launches
    `python -m kbd_signal restore --after 5 --gen N` detached, and that child is
    a fresh interpreter, so no patch here reaches it. It reads the real state
    dir and the real config and writes to a real keyboard five seconds later.
    """

    def setUp(self):
        directory = tempfile.mkdtemp()
        config_path = os.path.join(directory, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"restore": "baseline"}, f)
        patches = [
            mock.patch.object(states, "STATE_DIR", directory),
            mock.patch.object(states, "STATE_FILE",
                              os.path.join(directory, "state.json")),
            mock.patch.object(states, "ACTIVE_FLAG",
                              os.path.join(directory, "active.flag")),
            mock.patch.object(states, "LOG_FILE",
                              os.path.join(directory, "log")),
            mock.patch.object(config, "CONFIG_FILE", config_path),
            mock.patch.object(states, "_spawn_delayed_restore",
                              return_value=True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)


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


class EchoPersistenceTests(_IsolatedState):
    """A pair describes the last thing written to the keyboard, so it survives
    exactly the case where nothing was written."""


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

    def test_a_restore_records_what_it_wrote_and_what_came_back(self):
        """The load-bearing link: everything else here builds the pair by hand,
        so making _brightness_echo return None left the whole fix inert with the
        suite green."""
        kb = mock.MagicMock()
        kb.__enter__ = mock.Mock(return_value=kb)
        kb.__exit__ = mock.Mock(return_value=False)
        kb.apply_snapshot.return_value = True
        kb.settle_brightness.return_value = True
        kb.get_value.return_value = [119]
        written = self._restore_with(mock.Mock(return_value=kb))
        self.assertEqual(written.get("brightness_echo"),
                         {"written": 119, "readback": 119,
                          "device": states._device_identity()})

    def test_the_pair_survives_a_full_restore_then_capture(self):
        """End to end: restore writes the baseline, the next capture reads the
        lossy value back and recovers the one that was written."""
        kb = mock.MagicMock()
        kb.__enter__ = mock.Mock(return_value=kb)
        kb.__exit__ = mock.Mock(return_value=False)
        kb.apply_snapshot.return_value = True
        kb.settle_brightness.return_value = True
        kb.apply.return_value = True
        # The board answers 119 for the 120 restore wrote, and keeps answering
        # 119 when the next cycle snapshots it.
        kb.get_value.return_value = [119]
        kb.snapshot.return_value = _snap(119)

        state = {"active": "waiting", "generation": 4, "owners": [],
                 "baseline": _snap(120), "last_baseline": _snap(120),
                 "last_baseline_device": states._device_identity()}
        states.save_state(state)
        keyboard = mock.Mock(return_value=kb)
        with mock.patch.object(states.via, "Keyboard", keyboard):
            states._restore_locked(None, None)
            states.set_state("done")
        self.assertEqual(states.load_state()["baseline"]["brightness"], 120,
                         "the capture should recover the value restore wrote")

    def test_an_unsettled_brightness_still_records_the_pair(self):
        """Only the "off" path had a test for a brightness that would not
        settle; the baseline path's branch was unreached, and it now also
        decides whether the pair is recorded.

        Recording it is right: the read happens after the write attempt, so the
        pair describes what the device actually shows either way, and the value
        it maps back to is the baseline we meant."""
        kb = mock.MagicMock()
        kb.__enter__ = mock.Mock(return_value=kb)
        kb.__exit__ = mock.Mock(return_value=False)
        kb.apply_snapshot.return_value = True
        kb.settle_brightness.return_value = False      # the #34 revert
        kb.get_value.return_value = [255]              # reverted to the signal
        written = self._restore_with(mock.Mock(return_value=kb))
        self.assertEqual(written.get("brightness_echo"),
                         {"written": 119, "readback": 255,
                          "device": states._device_identity()})
        with open(states.LOG_FILE, encoding="utf-8") as f:
            self.assertIn("brightness not confirmed", f.read())

    def test_the_dark_path_records_no_pair(self):
        """The 0 written there is a forced blackout, not the user's setting.

        Recording it would mean the pair says "whatever the firmware reports
        back means 0", and if the write did not land that reading is the user's
        real brightness -- which the next capture then stores as 0, in
        last_baseline too, so #45's fallback cannot recover it.
        """
        kb = mock.MagicMock()
        kb.__enter__ = mock.Mock(return_value=kb)
        kb.__exit__ = mock.Mock(return_value=False)
        kb.settle_brightness.return_value = False   # the 0 never landed
        kb.get_value.return_value = [119]           # the board is still at 119
        with mock.patch.object(states, "load_config",
                               return_value={"restore": "off"}):
            written = self._restore_with(mock.Mock(return_value=kb))
        self.assertIsNone(written.get("brightness_echo"))

    def test_a_dark_restore_cannot_turn_the_baseline_off(self):
        """End to end for the same hazard, through the default mode: with no
        baseline to repaint from, restore takes the dark path too (#35)."""
        kb = mock.MagicMock()
        kb.__enter__ = mock.Mock(return_value=kb)
        kb.__exit__ = mock.Mock(return_value=False)
        kb.settle_brightness.return_value = False
        kb.get_value.return_value = [119]
        kb.apply.return_value = True
        kb.snapshot.return_value = _snap(119)
        states.save_state({"active": "waiting", "generation": 3, "owners": [],
                           "baseline": None,
                           "last_baseline_device": states._device_identity()})
        with mock.patch.object(states.via, "Keyboard",
                               mock.Mock(return_value=kb)):
            states._restore_locked(None, None)
            states.set_state("done")
        stored = states.load_state()
        self.assertEqual(stored["baseline"]["brightness"], 119)
        self.assertEqual(stored["last_baseline"]["brightness"], 119)

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


class SignalGuardOrderingTests(_IsolatedState):
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

if __name__ == "__main__":
    unittest.main()
