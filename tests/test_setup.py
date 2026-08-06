"""`kbd-signal setup`: the detectable half of a device config.

The interview itself is I/O, so what is pinned here is everything that decides
what lands in config.json: the reset-quirk probe, the channel probe, the
effect-index interview's bookkeeping, and the merge that must not lose keys.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from kbd_signal import config, setup, via


def _kb():
    """A Keyboard with the device open bypassed (see tests/test_via.py)."""
    kb = via.Keyboard.__new__(via.Keyboard)
    kb._channel = 3
    kb._reset_on_effect = False
    return kb


class ProbeResetOnEffectTests(unittest.TestCase):
    def test_detects_hue_snapping_to_zero(self):
        kb = _kb()
        kb.set_value = mock.Mock()
        # The seeded green is gone on the first read: the firmware reset it.
        kb.get_value = mock.Mock(side_effect=[[0, 255], [via.PROBE_BRIGHTNESS]])
        self.assertTrue(via.probe_reset_on_effect(kb, 1, settle=0))

    def test_detects_brightness_snapping_to_full(self):
        kb = _kb()
        kb.set_value = mock.Mock()
        kb.get_value = mock.Mock(side_effect=[[via.PROBE_HUE, 255], [255]])
        self.assertTrue(via.probe_reset_on_effect(kb, 1, settle=0))

    def test_quiet_firmware_reports_no_quirk(self):
        kb = _kb()
        kb.set_value = mock.Mock()
        kb.get_value = mock.Mock(
            return_value=None,
            side_effect=lambda vid, length=1, **kw: (
                [via.PROBE_HUE, 255] if length == 2 else [via.PROBE_BRIGHTNESS]))
        self.assertFalse(via.probe_reset_on_effect(kb, 1, window=0.02, settle=0))

    def test_seeds_a_state_the_reset_would_change(self):
        """Without a non-zero hue and a non-full brightness first, the reset's
        signature is indistinguishable from what was already on the board."""
        kb = _kb()
        kb.set_value = mock.Mock()
        kb.get_value = mock.Mock(side_effect=[[0, 255], [255]])
        via.probe_reset_on_effect(kb, 7, settle=0)
        kb.set_value.assert_any_call(via.VALUE_BRIGHTNESS, via.PROBE_BRIGHTNESS)
        kb.set_value.assert_any_call(via.VALUE_COLOR, via.PROBE_HUE, 255)
        kb.set_value.assert_any_call(via.VALUE_EFFECT, 7)
        self.assertNotEqual(via.PROBE_HUE, 0)
        self.assertNotEqual(via.PROBE_BRIGHTNESS, 255)

    def test_dropped_reads_are_not_evidence(self):
        kb = _kb()
        kb.set_value = mock.Mock()
        kb.get_value = mock.Mock(side_effect=OSError("no response"))
        self.assertFalse(via.probe_reset_on_effect(kb, 1, window=0.02, settle=0))


class VerifyChannelTests(unittest.TestCase):
    """verify_channel confirms the configured channel by round-trip. It does not
    search: a write to an unhandled channel is not inert on real firmware (see
    via.verify_channel), so guessing is worse than reporting."""

    @staticmethod
    def _kb(live=True, echo_unknown=False, speed=127):
        kb = _kb()
        store = {"speed": speed}

        def get_value(_vid, length=1, **_kw):
            if live:
                return [store["speed"]]
            if echo_unknown:
                return [0] * length   # request echoed back: zeros, not silence
            raise OSError("no response")

        def set_value(_vid, *data):
            if live:
                store["speed"] = data[0]

        kb.get_value, kb.set_value = get_value, set_value
        return kb, store

    def test_confirms_a_channel_whose_writes_land(self):
        kb, _ = self._kb(live=True)
        self.assertTrue(via.verify_channel(kb))

    def test_rejects_a_channel_that_answers_but_writes_nowhere(self):
        """Measured on real hardware: channel 0 answers a brightness GET with
        [0] on a board whose rgb_matrix lives on channel 3. A read-only probe
        would accept it and every later write would vanish silently."""
        kb, _ = self._kb(live=False, echo_unknown=True)
        self.assertFalse(via.verify_channel(kb))

    def test_restores_even_when_the_confirming_read_raises(self):
        """The probe value must not be left behind on the error path either:
        setup snapshots after this, so a leftover becomes the user's baseline."""
        kb = _kb()
        store = {"speed": 127}
        reads = [0]

        def get_value(_vid, length=1, **_kw):
            reads[0] += 1
            if reads[0] == 1:
                return [store["speed"]]
            raise OSError("device went away mid-probe")

        kb.get_value = get_value
        kb.set_value = lambda _vid, *data: store.update(speed=data[0])
        self.assertFalse(via.verify_channel(kb))
        self.assertEqual(store["speed"], 127)

    def test_a_restore_that_itself_fails_is_not_fatal(self):
        kb = _kb()
        kb.get_value = lambda *_a, **_kw: [127]
        calls = [0]

        def set_value(*_a):
            calls[0] += 1
            raise OSError("gone")

        kb.set_value = set_value
        self.assertFalse(via.verify_channel(kb))

    def test_rejects_a_silent_channel(self):
        kb, _ = self._kb(live=False)
        self.assertFalse(via.verify_channel(kb))

    def test_restores_the_speed_it_probed_with(self):
        """setup snapshots *after* this, so a leftover probe value would become
        the user's baseline."""
        kb, store = self._kb(live=True, speed=127)
        via.verify_channel(kb)
        self.assertEqual(store["speed"], 127)

    def test_a_board_reading_zero_still_confirms(self):
        """0 is what an echoed request reads back as, so the probe value must
        differ from both the stored value and 0."""
        kb, store = self._kb(live=True, speed=0)
        self.assertTrue(via.verify_channel(kb))
        self.assertEqual(store["speed"], 0)

    def test_a_board_already_at_the_probe_value_still_confirms(self):
        kb, store = self._kb(live=True, speed=1)
        self.assertTrue(via.verify_channel(kb))
        self.assertEqual(store["speed"], 1)


class InterviewTests(unittest.TestCase):
    @staticmethod
    def _run(answers):
        kb = _kb()
        kb.apply = mock.Mock()
        with mock.patch("builtins.input", side_effect=answers):
            return setup._interview(kb), kb

    def test_qmk_default_order_ends_in_two_questions(self):
        found, kb = self._run(["s", "p"])
        self.assertEqual(found, {"solid": 1, "breathing": 2})
        self.assertEqual([c.kwargs["effect"] for c in kb.apply.call_args_list],
                         [1, 2])

    def test_walks_past_rejected_indices(self):
        found, kb = self._run(["n", "s", "n", "p"])
        self.assertEqual(found, {"solid": 2, "breathing": 3})
        self.assertEqual([c.kwargs["effect"] for c in kb.apply.call_args_list],
                         [1, 2, 0, 3])

    def test_interview_lights_the_leds_enough_to_judge(self):
        _, kb = self._run(["s", "p"])
        for call in kb.apply.call_args_list:
            self.assertGreater(call.kwargs["brightness"], 0)
            self.assertIsNotNone(call.kwargs["hue"])

    def test_eof_aborts(self):
        kb = _kb()
        kb.apply = mock.Mock()
        with mock.patch("builtins.input", side_effect=EOFError):
            self.assertIsNone(setup._interview(kb))

    def test_reprompts_on_junk(self):
        found, _ = self._run(["x", "", "s", "p"])
        self.assertEqual(found, {"solid": 1, "breathing": 2})

    def test_gives_up_when_no_pair_exists(self):
        kb = _kb()
        kb.apply = mock.Mock()
        with mock.patch("builtins.input",
                        side_effect=["n"] * len(setup.EFFECT_CANDIDATES)):
            self.assertIsNone(setup._interview(kb))


class ChooseDeviceTests(unittest.TestCase):
    DEVS = [{"vendor_id": 0x3434, "product_id": 0x0192,
             "product_string": "Keychron K8 Pro"},
            {"vendor_id": 0x3434, "product_id": 0x0300,
             "product_string": "Link-KM"}]

    def test_single_device_asks_nothing(self):
        with mock.patch("builtins.input", side_effect=AssertionError("asked")):
            self.assertIs(setup._choose_device(self.DEVS[:1]), self.DEVS[0])

    def test_picks_by_number(self):
        with mock.patch("builtins.input", return_value="2"):
            self.assertIs(setup._choose_device(self.DEVS), self.DEVS[1])

    def test_rejects_out_of_range(self):
        with mock.patch("builtins.input", side_effect=["0", "3", "1"]):
            self.assertIs(setup._choose_device(self.DEVS), self.DEVS[0])


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        patches = [
            mock.patch.object(config, "STATE_DIR", self.dir),
            mock.patch.object(config, "CONFIG_FILE",
                              os.path.join(self.dir, "config.json")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _read(self, name="config.json"):
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return json.load(f)

    def test_round_trips_through_load(self):
        config.save({"device": {"vendor_id": "0x1111", "product_id": "0x2222"}})
        self.assertEqual(config.device()["vendor_id"], 0x1111)
        self.assertEqual(config.device()["product_id"], 0x2222)

    def test_keeps_unrelated_keys(self):
        """setup rewrites "device"; everything else in a hand-edited config
        must survive."""
        config.save({"restore": "off", "mystery": [1, 2],
                     "device": {"product_match": "old"}})
        cfg = config.load()
        cfg["device"] = {"product_match": "new"}
        config.save(cfg)
        written = self._read()
        self.assertEqual(written["restore"], "off")
        self.assertEqual(written["mystery"], [1, 2])
        self.assertEqual(written["device"]["product_match"], "new")

    def test_previous_config_survives_as_bak(self):
        config.save({"device": {"product_match": "first"}})
        config.save({"device": {"product_match": "second"}})
        self.assertEqual(self._read()["device"]["product_match"], "second")
        self.assertEqual(self._read("config.json.bak")["device"]["product_match"],
                         "first")

    def test_a_failed_write_leaves_the_previous_config_intact(self):
        """The .bak rotation must not run before the new content is on disk.
        Rotating first leaves a window with no config.json at all, and load()
        would silently fall back to the K8 Pro defaults -- on another board that
        means product_match stops matching and find_device_path picks whatever
        enumerated first (a Link-KM dock, say)."""
        config.save({"device": {"product_match": "first"}})
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                config.save({"device": {"product_match": "second"}})
        self.assertEqual(self._read()["device"]["product_match"], "first")

    def test_config_json_is_never_absent_even_if_the_swap_fails(self):
        """The .bak is a copy, not a move, so the old file stays in place until
        the final replace succeeds. Moving it aside first would leave a window
        with no config.json -- and load() answers that with the K8 Pro defaults,
        silently."""
        config.save({"device": {"product_match": "first"}})
        real_replace = os.replace

        def fail_only_the_swap(src, dst):
            # Let the .bak rotation through; break just the final swap-in, which
            # is the window a move-then-write order would leave open.
            if str(dst) == config.CONFIG_FILE:
                raise OSError("swap failed")
            return real_replace(src, dst)

        with mock.patch("os.replace", side_effect=fail_only_the_swap):
            with self.assertRaises(OSError):
                config.save({"device": {"product_match": "second"}})
        self.assertEqual(self._read()["device"]["product_match"], "first")

    def test_first_write_needs_no_previous_file(self):
        config.save({"device": {}})
        self.assertFalse(os.path.exists(
            os.path.join(self.dir, "config.json.bak")))

    def test_null_product_match_is_written_not_omitted(self):
        """Omitting the key lets DEFAULT_DEVICE's "K8" merge back in and
        mis-target a non-Keychron board."""
        config.save({"device": {"vendor_id": "0x05ac", "product_match": None}})
        self.assertIn("product_match", self._read()["device"])
        self.assertIsNone(config.device()["product_match"])


class ConfiguredChannelTests(unittest.TestCase):
    """setup must read v3_channel from config.

    verify_channel's failure tells the user to set it in config.json and re-run.
    Building dev_cfg from DEFAULT_DEVICE alone pinned it to 3, so a board on any
    other channel followed that advice straight back into the same error.
    """

    DEV = {"vendor_id": 0x3434, "product_id": 0x1012, "path": b"/dev/kbd",
           "product_string": "Some Board"}

    def _run_with_channel(self, channel):
        captured = {}
        kb = mock.MagicMock()
        kb.protocol = 13
        kb.__enter__ = mock.Mock(return_value=kb)
        kb.__exit__ = mock.Mock(return_value=False)

        def keyboard(dev_cfg):
            captured.update(dev_cfg)
            return kb

        cfg = {**config.DEFAULT_DEVICE, "v3_channel": channel}
        with mock.patch.object(setup.states, "is_active", return_value=False), \
             mock.patch.object(config, "device", return_value=cfg), \
             mock.patch.object(via, "enumerate_raw_hid", return_value=[self.DEV]), \
             mock.patch.object(via, "Keyboard", side_effect=keyboard), \
             mock.patch.object(via, "verify_channel", return_value=False):
            code = setup.run()
        return code, captured

    def test_the_configured_channel_reaches_the_keyboard(self):
        code, dev_cfg = self._run_with_channel(6)
        self.assertEqual(code, 1)  # verify_channel said no, so setup stops
        self.assertEqual(dev_cfg["v3_channel"], 6)

    def test_the_default_still_applies_when_config_says_nothing(self):
        _, dev_cfg = self._run_with_channel(config.DEFAULT_DEVICE["v3_channel"])
        self.assertEqual(dev_cfg["v3_channel"], 3)


class SetupGuardTests(unittest.TestCase):
    def test_refuses_while_a_signal_is_showing(self):
        with mock.patch.object(setup.states, "is_active", return_value=True), \
             mock.patch.object(via, "enumerate_raw_hid",
                               side_effect=AssertionError("touched device")):
            self.assertEqual(setup.run(), 1)

    def test_reports_no_device(self):
        with mock.patch.object(setup.states, "is_active", return_value=False), \
             mock.patch.object(via, "enumerate_raw_hid", return_value=[]):
            self.assertEqual(setup.run(), 1)


class DelegationTests(unittest.TestCase):
    """cli.cmd_setup is a thin delegate, like cmd_hook. Extracting this module
    out of cli.py put that wiring at risk and nothing else covers it."""

    def test_cmd_setup_returns_the_exit_code_from_run(self):
        from kbd_signal import cli
        with mock.patch.object(setup, "run", return_value=7) as run:
            self.assertEqual(cli.cmd_setup(None), 7)
        run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
