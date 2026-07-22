import unittest
from unittest import mock

from kbd_signal import via


class SetColorTests(unittest.TestCase):
    """set_color settles the color while holding the LEDs dark (defeating the
    delayed post-effect reset without showing its red), bounded by a time
    budget so it can never hang a hook, and reports whether it succeeded."""

    @staticmethod
    def _kb(reset_on_effect=True):
        # Bypass __init__ (which opens a device); we only exercise the high-level
        # methods. Default to the reset-prone gate so the workaround path runs.
        kb = via.Keyboard.__new__(via.Keyboard)
        kb._reset_on_effect = reset_on_effect
        return kb

    def test_holds_dark_and_writes_color(self):
        kb = self._kb()
        kb.set_value = mock.Mock()
        kb.get_value = mock.Mock(return_value=[85, 255])
        self.assertTrue(via.Keyboard.set_color(kb, 85, 255, hold=0, settle=0))
        kb.set_value.assert_any_call(via.VALUE_BRIGHTNESS, 0)
        kb.set_value.assert_any_call(via.VALUE_COLOR, 85, 255)

    def test_retries_until_readback_matches(self):
        kb = self._kb()
        kb.set_value = mock.Mock()
        # Two dropped reads (reset landed), then it sticks.
        kb.get_value = mock.Mock(side_effect=[[0, 255], [0, 255], [85, 255]])
        self.assertTrue(via.Keyboard.set_color(kb, 85, 255, hold=0, settle=0))
        self.assertEqual(kb.get_value.call_count, 3)

    def test_gives_up_within_budget_without_hanging(self):
        kb = self._kb()
        kb.set_value = mock.Mock()
        kb.get_value = mock.Mock(return_value=[0, 255])  # never sticks
        self.assertFalse(
            via.Keyboard.set_color(kb, 85, 255, hold=0, settle=0, budget=0.02)
        )

    def test_errors_are_treated_as_miss_not_raised(self):
        # A hook must exit cleanly: read/write errors count as a miss, never
        # propagate.
        kb = self._kb()
        kb.set_value = mock.Mock()
        kb.get_value = mock.Mock(side_effect=[IOError("no response"), [85, 255]])
        self.assertTrue(via.Keyboard.set_color(kb, 85, 255, hold=0, settle=0))
        self.assertEqual(kb.get_value.call_count, 2)

    def test_apply_settles_dark_then_raises_brightness(self):
        kb = self._kb()
        kb.set_value = mock.Mock()
        kb.set_color = mock.Mock(return_value=True)
        self.assertTrue(
            via.Keyboard.apply(kb, effect=1, hue=85, sat=255, brightness=255)
        )
        kb.set_color.assert_called_once_with(85, 255)
        # Dark is dropped BEFORE the effect change (closing the bright-reset
        # window), and the target brightness is raised only after the settle.
        calls = kb.set_value.call_args_list
        self.assertEqual(calls[0], mock.call(via.VALUE_BRIGHTNESS, 0))
        self.assertLess(
            calls.index(mock.call(via.VALUE_BRIGHTNESS, 0)),
            calls.index(mock.call(via.VALUE_EFFECT, 1)),
        )
        kb.set_value.assert_any_call(via.VALUE_BRIGHTNESS, 255)

    def test_apply_keeps_dark_when_color_unconfirmed(self):
        # The failure path we were fixing: if the color never settles, the LEDs
        # must stay dark (never the reset's red) — brightness is NOT raised, and
        # apply reports False so states.py logs it.
        kb = self._kb()
        kb.set_value = mock.Mock()
        kb.set_color = mock.Mock(return_value=False)
        self.assertFalse(
            via.Keyboard.apply(kb, effect=1, hue=85, sat=255, brightness=255)
        )
        # brightness=0 was written (the pre-effect drop) but 255 never was.
        kb.set_value.assert_any_call(via.VALUE_BRIGHTNESS, 0)
        self.assertNotIn(
            mock.call(via.VALUE_BRIGHTNESS, 255), kb.set_value.call_args_list
        )

    def test_apply_snapshot_keeps_dark_when_color_unconfirmed(self):
        # Same guard on the restore path (routed through apply).
        kb = self._kb()
        kb.set_value = mock.Mock()
        kb.set_color = mock.Mock(return_value=False)
        snap = {"effect": 5, "speed": 90, "brightness": 120, "color": [20, 200]}
        self.assertFalse(via.Keyboard.apply_snapshot(kb, snap))
        self.assertNotIn(
            mock.call(via.VALUE_BRIGHTNESS, 120), kb.set_value.call_args_list
        )

    def test_non_reset_device_writes_directly_without_dark_hold(self):
        # Device gate: keyboards not known to reset (the default) skip the
        # workaround entirely — no set_color, no dark dip, brightness written.
        kb = self._kb(reset_on_effect=False)
        kb.set_value = mock.Mock()
        kb.set_color = mock.Mock()
        self.assertTrue(
            via.Keyboard.apply(kb, effect=1, hue=85, sat=255, brightness=255)
        )
        kb.set_color.assert_not_called()
        kb.set_value.assert_any_call(via.VALUE_COLOR, 85, 255)
        kb.set_value.assert_any_call(via.VALUE_BRIGHTNESS, 255)
        # No brightness=0 pre-drop on a device that doesn't reset.
        self.assertNotIn(
            mock.call(via.VALUE_BRIGHTNESS, 0), kb.set_value.call_args_list
        )

    def test_set_color_default_hold_follows_device_gate(self):
        # A reset-prone device holds dark past the reset window; a normal one
        # doesn't, so its first read-back is trusted immediately.
        reset_kb = self._kb(reset_on_effect=True)
        reset_kb.set_value = mock.Mock()
        # Never matches: forces the loop to run until budget, so a nonzero hold
        # means at least a couple of write cycles before giving up.
        reset_kb.get_value = mock.Mock(return_value=[0, 0])
        via.Keyboard.set_color(reset_kb, 85, 255, budget=0.05)
        held_writes = reset_kb.set_value.call_count

        plain_kb = self._kb(reset_on_effect=False)
        plain_kb.set_value = mock.Mock()
        plain_kb.get_value = mock.Mock(return_value=[85, 255])  # confirms at once
        self.assertTrue(via.Keyboard.set_color(plain_kb, 85, 255))
        # hold=0 means it trusts the first read: one write cycle (2 set_values).
        self.assertEqual(plain_kb.set_value.call_count, 2)
        self.assertGreater(held_writes, plain_kb.set_value.call_count)

    def test_read_back_tries_capped_by_remaining_budget(self):
        # With an unresponsive get, the read tries must shrink to fit the
        # remaining budget so set_color can't overshoot its hard ceiling.
        kb = self._kb(reset_on_effect=True)
        kb.set_value = mock.Mock()
        clock = iter([
            0.0,    # deadline base
            0.0,    # hold_until base
            0.0,    # while: < deadline
            0.0,    # hold check (hold=0 -> not before hold_until)
            1.4,    # remaining = deadline(1.5) - 1.4 = 0.1 -> tries = 1
            1.6,    # while: >= deadline, exit
        ])
        with mock.patch.object(via.time, "monotonic", lambda: next(clock)), \
                mock.patch.object(via.time, "sleep", lambda _s: None):
            kb.get_value = mock.Mock(return_value=[0, 0])
            self.assertFalse(
                via.Keyboard.set_color(kb, 85, 255, hold=0, budget=1.5)
            )
        kb.get_value.assert_called_once_with(via.VALUE_COLOR, 2, tries=1)


if __name__ == "__main__":
    unittest.main()
