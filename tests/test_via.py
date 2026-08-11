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
        """A reset-prone device must keep rewriting past the reset window while
        a normal device can trust its first read-back.

        The clock is virtual because this loop never sleeps while holding --
        it `continue`s before the sleep -- so the write count is set by how
        much work fits in `budget` of real time. A single 60 ms stall anywhere
        in the first few iterations was enough to drop it to one cycle and
        break the comparison (#76). Time is charged to the writes instead.
        """
        now = [0.0]

        def set_value(*_args):
            now[0] += 0.01  # a write costs 10 ms of virtual time

        def sleep(seconds):
            now[0] += seconds

        reset_kb = self._kb(reset_on_effect=True)
        reset_kb.set_value = mock.Mock(side_effect=set_value)
        # Never matches: forces the loop to run until budget, so the default
        # hold keeps the reset-prone device rewriting without a read.
        reset_kb.get_value = mock.Mock(return_value=[0, 0])

        plain_kb = self._kb(reset_on_effect=False)
        plain_kb.set_value = mock.Mock(side_effect=set_value)
        plain_kb.get_value = mock.Mock(return_value=[85, 255])  # confirms at once

        with mock.patch.object(via.time, "monotonic", lambda: now[0]), \
                mock.patch.object(via.time, "sleep", sleep):
            via.Keyboard.set_color(reset_kb, 85, 255, budget=0.05)
            held_writes = reset_kb.set_value.call_count
            self.assertTrue(via.Keyboard.set_color(plain_kb, 85, 255))

        # The deadline is checked at the loop head, so the budget bounds where
        # a cycle may start, not where it ends: entries at 0.00, 0.02 and 0.04
        # are all under 0.05, and the third carries the clock to 0.06 and out.
        # Three cycles of two writes each.
        self.assertEqual(held_writes, 6)
        # hold=0 means it trusts the first read: one write cycle (2 set_values).
        self.assertEqual(plain_kb.set_value.call_count, 2)
        # The exact counts above are arithmetic; this is the behaviour they
        # exist to pin, and it stays readable if the constants ever move.
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


class SettleBrightnessTests(unittest.TestCase):
    """settle_brightness writes-and-verifies until the value sticks (#34).

    Measured on a K8 Pro: after a sequence that includes an effect change,
    a lone brightness write can be ACKed yet silently reverted by the
    firmware (a later read shows the pre-change value). The loop does not
    depend on explaining that behavior — write-and-verify wins regardless.
    """

    @staticmethod
    def _kb(v3=False):
        # #34 was measured on a K8 Pro on stock protocol-9 firmware, so these
        # are v2 boards: the read-back is lossless there and is kept.
        kb = via.Keyboard.__new__(via.Keyboard)
        kb._reset_on_effect = True
        kb._v3 = v3
        return kb

    def test_converges_when_first_writes_are_swallowed(self):
        # Failure mode from #34: the firmware swallows the first N
        # brightness writes, read-back keeps showing the old value.
        kb = self._kb()
        state = {"swallow": 2, "brightness": 255}

        def set_value(value_id, *data):
            if value_id != via.VALUE_BRIGHTNESS:
                return
            if state["swallow"]:
                state["swallow"] -= 1  # ACKed but silently reverted
            else:
                state["brightness"] = data[0]

        kb.set_value = set_value
        kb.get_value = lambda value_id, length=1, tries=6: [state["brightness"]]
        self.assertTrue(via.Keyboard.settle_brightness(kb, 0, settle=0, hold=0))
        self.assertEqual(state["brightness"], 0)

    def test_confirmation_inside_revert_window_is_not_trusted(self):
        # #34 on the K8 Pro: a write is confirmed by a read at ~100 ms,
        # then the delayed firmware restore snaps it back at ~150 ms
        # (reverts measured as late as ~300 ms). A confirmation must
        # only count once the revert window has passed; until then the
        # loop keeps rewriting so it outlives the reversion.
        kb = self._kb()
        now = [0.0]
        state = {"brightness": 255, "reverted": False}

        def set_value(value_id, *data):
            state["brightness"] = data[0]

        def sleep(seconds):
            now[0] += seconds
            if now[0] >= 0.15 and not state["reverted"]:
                state["reverted"] = True
                state["brightness"] = 255  # delayed firmware restore

        kb.set_value = set_value
        kb.get_value = lambda value_id, length=1, tries=6: \
            [state["brightness"]]
        with mock.patch.object(via.time, "monotonic", lambda: now[0]), \
                mock.patch.object(via.time, "sleep", sleep):
            self.assertTrue(via.Keyboard.settle_brightness(kb, 0))
        # The loop must have stayed alive past the reversion and won.
        self.assertTrue(state["reverted"])
        self.assertEqual(state["brightness"], 0)

    def test_gives_up_within_budget_without_raising(self):
        kb = self._kb()
        kb.set_value = mock.Mock()
        kb.get_value = mock.Mock(return_value=[255])  # never sticks
        self.assertFalse(
            via.Keyboard.settle_brightness(kb, 0, settle=0, hold=0,
                                           budget=0.02)
        )

    def test_confirmed_read_past_the_window_stops_the_loop(self):
        # Once the revert window is over (hold=0 here), the first
        # confirmed read must terminate the loop immediately.
        kb = self._kb()
        kb.set_value = mock.Mock()
        kb.get_value = mock.Mock(return_value=[0])
        self.assertTrue(via.Keyboard.settle_brightness(kb, 0, settle=0, hold=0))
        kb.get_value.assert_called_once()

    def test_errors_count_as_miss_not_raised(self):
        # Same contract as set_color: a hook must exit cleanly, so
        # write/read errors are a miss, never an exception.
        kb = self._kb()
        kb.set_value = mock.Mock(side_effect=OSError("write failed"))
        kb.get_value = mock.Mock(side_effect=[OSError("no response"), [0]])
        self.assertTrue(via.Keyboard.settle_brightness(kb, 0, settle=0, hold=0))

    def test_writes_stop_at_the_deadline_and_sleeps_are_clamped(self):
        # A blocking SET burns two read timeouts, so the deadline must be
        # checked right before each write and the inter-write sleep clamped
        # to the remaining budget — otherwise an iteration entered just in
        # time overshoots the advertised ceiling by most of a write plus a
        # full settle (#36 review).
        kb = self._kb()
        now = [0.0]
        start_times, sleeps = [], []

        def fake_set(*_args):
            start_times.append(now[0])
            now[0] += 0.6  # blocking write

        def fake_sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        kb.set_value = fake_set
        kb.get_value = mock.Mock(return_value=[123])  # never confirms
        with mock.patch.object(via.time, "monotonic", lambda: now[0]), \
                mock.patch.object(via.time, "sleep", fake_sleep):
            self.assertFalse(via.Keyboard.settle_brightness(
                kb, 0, settle=0.1, hold=0, budget=1.5))
        self.assertTrue(all(t < 1.5 for t in start_times))
        # Total elapsed: at most the budget plus one in-flight write; an
        # unclamped final sleep would push past this.
        self.assertLessEqual(now[0], 2.05)


if __name__ == "__main__":
    unittest.main()
