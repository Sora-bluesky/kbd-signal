"""`kbd-signal export`: turning a working config into a preset skeleton.

What matters is that the emitted JSON is a config kbd-signal can actually load,
that the page states the detected facts rather than inventing them, and that
every field only a human can fill stays visibly unfilled.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from kbd_signal import config, export, via

Q1 = {"vendor_id": 0x3434, "product_id": 0x1012, "path": b"/dev/q1",
      "product_string": "Keychron Q1 HE 8K",
      "manufacturer_string": "Keychron"}
DOCK = {"vendor_id": 0x3434, "product_id": 0xD026, "path": b"/dev/dock",
        "product_string": "Keychron Link-KM",
        "manufacturer_string": "Keychron"}

CFG = {**config.DEFAULT_DEVICE, "vendor_id": 0x3434, "product_id": 0x1012,
       "product_match": "Keychron Q1 HE 8K", "reset_on_effect": True}


class SlugTests(unittest.TestCase):
    def test_product_string_becomes_a_file_name(self):
        self.assertEqual(export.slug("Keychron Q1 HE 8K"), "keychron-q1-he-8k")

    def test_runs_of_punctuation_collapse(self):
        self.assertEqual(export.slug("NuPhy  Air75 V2 (wired)"),
                         "nuphy-air75-v2-wired")

    def test_missing_or_unusable_product_string_still_yields_a_name(self):
        for value in (None, "", "???"):
            self.assertEqual(export.slug(value), "unknown-device")


class ConfigJsonTests(unittest.TestCase):
    def test_emitted_json_loads_back_as_the_same_device(self):
        """The whole point is a preset someone can paste in, so run the emitted
        block through the real config.load() -- hex-string parsing, default
        merging and all -- and check it lands on the values it was made from."""
        emitted = export._config_json(CFG, "Keychron Q1 HE 8K")
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(emitted, f)
        with mock.patch.object(config, "CONFIG_FILE", path):
            loaded = config.device()
        for key in ("vendor_id", "product_id", "product_match",
                    "v3_channel", "reset_on_effect", "effects"):
            self.assertEqual(loaded[key], CFG[key], key)

    def test_ids_are_written_as_hex_strings(self):
        device = export._config_json(CFG, "x")["device"]
        self.assertEqual(device["vendor_id"], "0x3434")
        self.assertEqual(device["product_id"], "0x1012")

    def test_unpinned_product_id_stays_null(self):
        device = export._config_json({**CFG, "product_id": None}, "x")["device"]
        self.assertIsNone(device["product_id"])

    def test_description_is_left_to_be_written(self):
        self.assertIn("TODO", export._config_json(CFG, "x")["description"])

    def test_no_keys_beyond_the_device_block_are_invented(self):
        device = export._config_json(CFG, "x")["device"]
        self.assertEqual(set(device), {"vendor_id", "product_id",
                                       "product_match", "v3_channel",
                                       "reset_on_effect", "effects"})


class DevicePageTests(unittest.TestCase):
    @staticmethod
    def _page(cfg=None, protocol=13, found=(DOCK, Q1)):
        return export._device_page(cfg or CFG, "Keychron Q1 HE 8K", protocol,
                                   list(found), "keychron-q1-he-8k")

    def test_states_the_detected_values(self):
        page = self._page()
        for expected in ("`0x3434`", "`0x1012`", "protocol **13**",
                         "Keychron Q1 HE 8K", "PID=0xd026"):
            self.assertIn(expected, page)

    def test_v3_board_documents_channel_value_ids(self):
        self.assertIn("v3 custom channel", self._page(protocol=13))
        self.assertIn("`brightness`=1", self._page(protocol=13))

    def test_v2_board_documents_lighting_ids_instead(self):
        page = self._page(protocol=9)
        self.assertIn("v2 lighting", page)
        self.assertIn("0x80", page)
        self.assertIn("unused on a v2 board", page)

    def test_quirk_note_follows_the_measured_flag(self):
        self.assertIn("holds the LEDs dark", self._page())
        self.assertIn("does not show the post-effect reset",
                      self._page(cfg={**CFG, "reset_on_effect": False}))

    def test_human_only_sections_stay_marked(self):
        """Generating this prose would produce a page that looks researched and
        is not -- the PID cross-check and the dock trap are the parts worth
        reading in the existing Q1 HE 8K page."""
        page = self._page()
        self.assertGreaterEqual(page.count("TODO"), 6)
        for section in ("## Detection", "## Connection", "speed", "hue wheel"):
            self.assertIn(section, page)

    def test_unpinned_product_id_is_shown_as_null(self):
        self.assertIn("`null`", self._page(cfg={**CFG, "product_id": None}))


class DeviceSelectionTests(unittest.TestCase):
    """find_device now backs both find_device_path and export, so the selection
    rule has one implementation."""

    def _find(self, cfg, devices):
        with mock.patch.object(via, "enumerate_raw_hid", return_value=devices):
            return via.find_device(cfg)

    def test_pinned_product_id_wins_over_enumeration_order(self):
        self.assertIs(self._find(CFG, [DOCK, Q1]), Q1)

    def test_product_match_picks_the_keyboard_when_pid_is_unpinned(self):
        cfg = {**CFG, "product_id": None}
        self.assertIs(self._find(cfg, [DOCK, Q1]), Q1)

    def test_falls_back_to_the_first_enumerated_device(self):
        cfg = {**CFG, "product_id": None, "product_match": "nothing matches"}
        self.assertIs(self._find(cfg, [DOCK, Q1]), DOCK)

    def test_no_candidate_raises(self):
        with self.assertRaises(via.DeviceNotFound):
            self._find(CFG, [])

    def test_find_device_path_still_returns_just_the_path(self):
        with mock.patch.object(via, "enumerate_raw_hid", return_value=[DOCK, Q1]):
            self.assertEqual(via.find_device_path(CFG), Q1["path"])


class RunTests(unittest.TestCase):
    def test_prints_both_files_and_never_writes_to_the_device(self):
        kb = mock.MagicMock()
        kb.protocol = 13
        kb.__enter__ = mock.Mock(return_value=kb)
        kb.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(config, "device", return_value=CFG), \
             mock.patch.object(via, "enumerate_raw_hid", return_value=[DOCK, Q1]), \
             mock.patch.object(via, "find_device", return_value=Q1), \
             mock.patch.object(via, "Keyboard", return_value=kb), \
             mock.patch("builtins.print") as out:
            self.assertEqual(export.run(), 0)
        printed = "\n".join(str(c.args[0]) for c in out.call_args_list if c.args)
        self.assertIn("examples/config.keychron-q1-he-8k.json", printed)
        self.assertIn("docs/devices/keychron-q1-he-8k.md", printed)
        # Read-only: a protocol probe happens via the property, nothing is set.
        kb.set_value.assert_not_called()
        kb.apply.assert_not_called()
        kb.apply_snapshot.assert_not_called()

    def test_emitted_json_block_is_valid_json(self):
        emitted = export._config_json(CFG, "Keychron Q1 HE 8K")
        self.assertEqual(json.loads(json.dumps(emitted)), emitted)


if __name__ == "__main__":
    unittest.main()
