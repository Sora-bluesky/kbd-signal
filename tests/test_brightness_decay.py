"""Baseline brightness must not walk down over signal cycles (#58).

VIA's brightness round trip is not the identity on v3 (#56), so re-reading a
value kbd-signal itself wrote returns a slightly lower number. Taking that as
the next baseline and writing it back compounds: measured on a Q1 HE 8K, 120
settles at 117 but 10 walks to 0 -- a dim backlight goes dark.
"""

import unittest

from kbd_signal import states

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
