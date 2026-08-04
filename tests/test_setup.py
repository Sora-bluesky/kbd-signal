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


class ProbeChannelTests(unittest.TestCase):
    def test_prefers_the_default_channel(self):
        kb = _kb()
        kb.get_value = mock.Mock(return_value=[200])
        self.assertEqual(via.probe_channel(kb), 3)
        self.assertEqual(kb._channel, 3)

    def test_walks_to_a_channel_that_answers(self):
        kb = _kb()
        answers = {1: [200]}

        def get_value(*_a, **_kw):
            if kb._channel not in answers:
                raise OSError("no response")
            return answers[kb._channel]

        kb.get_value = get_value
        self.assertEqual(via.probe_channel(kb), 1)
        self.assertEqual(kb._channel, 1)

    def test_silence_returns_none_and_keeps_the_original(self):
        kb = _kb()
        kb.get_value = mock.Mock(side_effect=OSError("no response"))
        self.assertIsNone(via.probe_channel(kb))
        self.assertEqual(kb._channel, 3)


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
