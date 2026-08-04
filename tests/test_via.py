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



class SettleBrightnessV3Tests(unittest.TestCase):
    """On VIA v3 the brightness read-back cannot confirm anything.

    quantum/via.c stores scale8(value, RGB_MATRIX_MAXIMUM_BRIGHTNESS) -- an
    (i * sc) / 256 divide -- and reads back val * 255 / MAXIMUM_BRIGHTNESS. The
    divisors differ, so the round trip is not the identity by construction.
    Measured on a Q1 HE 8K (protocol 13): 44 -> 42, 120 -> 119, 0 and 255 exact.
    """

    @staticmethod
    def _kb():
        kb = via.Keyboard.__new__(via.Keyboard)
        kb._reset_on_effect = True
        kb._v3 = True
        return kb

    @staticmethod
    def _lossy_reader(store):
        """Read-back as measured: one or two lower, exact at 0 and 255."""
        def get_value(_vid, length=1, tries=6):
            v = store["brightness"]
            return [v if v in (0, 255) else max(0, v - 1)]
        return get_value

    def test_succeeds_although_the_read_never_equals_the_write(self):
        kb = self._kb()
        store = {"brightness": 0}
        kb.set_value = lambda _vid, *data: store.update(brightness=data[0])
        kb.get_value = self._lossy_reader(store)
        self.assertTrue(via.Keyboard.settle_brightness(kb, 120, settle=0, hold=0))
        self.assertEqual(store["brightness"], 120)

    def test_never_reads_back_so_it_cannot_burn_the_budget(self):
        """The bug being fixed: because the read can never equal the write, the
        loop spun the full 1.50 s per restore with the state lock held and ended
        in a False that was not a real failure. Pinned without a clock: on v3 no
        confirmation read happens at all, and the early True proves the loop did
        not fall through to the budget-exhausted return."""
        kb = self._kb()
        store = {"brightness": 0}
        reads = []

        def get_value(_vid, length=1, tries=6):
            reads.append(store["brightness"])
            return [max(0, store["brightness"] - 1)]

        kb.set_value = lambda _vid, *data: store.update(brightness=data[0])
        kb.get_value = get_value
        self.assertTrue(via.Keyboard.settle_brightness(kb, 120, settle=0, hold=0))
        self.assertEqual(reads, [], "v3 must not attempt a confirmation read")

    def test_v2_still_confirms_by_read_back(self):
        """Backward compatibility: v2 scales by RGBLIGHT_LIMIT_VAL / 255 in both
        directions, lossless at QMK's default 255, and v2 is where #34 was
        measured -- so the confirmation stays."""
        kb = self._kb()
        kb._v3 = False
        store = {"brightness": 0}
        reads = []

        def get_value(_vid, length=1, tries=6):
            reads.append(store["brightness"])
            return [store["brightness"]]

        kb.set_value = lambda _vid, *data: store.update(brightness=data[0])
        kb.get_value = get_value
        self.assertTrue(via.Keyboard.settle_brightness(kb, 120, settle=0, hold=0))
        self.assertEqual(reads, [120], "v2 must still verify the write")

    def test_still_rewrites_across_the_revert_window(self):
        """The rewrites are the whole #34 protection now, so they must outlive
        the window rather than being skipped along with the read-back."""
        kb = self._kb()
        writes = []
        kb.set_value = lambda _vid, *data: writes.append(data[0])
        kb.get_value = lambda *_a, **_kw: [0]
        via.Keyboard.settle_brightness(kb, 77, settle=0.01, hold=0.05)
        self.assertGreater(len(writes), 1)
        self.assertEqual(set(writes), {77})

    def test_write_errors_are_swallowed_so_a_hook_never_fails(self):
        kb = self._kb()

        def set_value(*_a):
            raise OSError("device gone")

        kb.set_value = set_value
        kb.get_value = lambda *_a, **_kw: [0]
        self.assertTrue(via.Keyboard.settle_brightness(kb, 5, settle=0, hold=0))

if __name__ == "__main__":
    unittest.main()
