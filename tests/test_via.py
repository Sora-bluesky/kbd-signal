import unittest
from unittest import mock

from kbd_signal import via


class SetColorTests(unittest.TestCase):
    """set_color settles the color while holding the LEDs dark (defeating the
    delayed post-effect reset without showing its red), bounded by a time
    budget so it can never hang a hook, and reports whether it succeeded."""

    @staticmethod
    def _kb():
        # Bypass __init__ (which opens a device); we only exercise set_color.
        return via.Keyboard.__new__(via.Keyboard)

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
        # brightness is raised to the target only after the color is settled.
        kb.set_value.assert_any_call(via.VALUE_BRIGHTNESS, 255)


if __name__ == "__main__":
    unittest.main()
