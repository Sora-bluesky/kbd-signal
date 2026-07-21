import unittest
from unittest import mock

from kbd_signal import via


class SetColorTests(unittest.TestCase):
    """set_color must survive the firmware's delayed post-effect color reset
    by re-reading and rewriting until the value sticks (bounded)."""

    @staticmethod
    def _kb():
        # Bypass __init__ (which opens a device); we only exercise set_color.
        return via.Keyboard.__new__(via.Keyboard)

    def test_retries_until_readback_matches(self):
        kb = self._kb()
        kb.set_value = mock.Mock()
        # Two dropped reads (reset landed), then it sticks.
        kb.get_value = mock.Mock(side_effect=[[0, 255], [0, 255], [85, 255]])
        via.Keyboard.set_color(kb, 85, 255, tries=5, settle=0)
        self.assertEqual(kb.set_value.call_count, 3)
        kb.set_value.assert_called_with(via.VALUE_COLOR, 85, 255)

    def test_returns_immediately_when_first_write_sticks(self):
        kb = self._kb()
        kb.set_value = mock.Mock()
        kb.get_value = mock.Mock(return_value=[85, 255])
        via.Keyboard.set_color(kb, 85, 255, tries=5, settle=0)
        self.assertEqual(kb.set_value.call_count, 1)

    def test_bounded_when_never_sticks(self):
        kb = self._kb()
        kb.set_value = mock.Mock()
        kb.get_value = mock.Mock(return_value=[0, 255])
        via.Keyboard.set_color(kb, 85, 255, tries=3, settle=0)
        self.assertEqual(kb.set_value.call_count, 3)  # gives up, never hangs

    def test_readback_ioerror_is_treated_as_miss(self):
        kb = self._kb()
        kb.set_value = mock.Mock()
        kb.get_value = mock.Mock(side_effect=IOError("no response"))
        via.Keyboard.set_color(kb, 85, 255, tries=2, settle=0)
        self.assertEqual(kb.set_value.call_count, 2)


if __name__ == "__main__":
    unittest.main()
