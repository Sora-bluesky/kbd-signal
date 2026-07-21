import json
import os
import tomllib
import unittest
from unittest import mock

from kbd_signal import __version__, config


class ExampleConfigTests(unittest.TestCase):
    @staticmethod
    def _repo_path(*parts):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            *parts,
        )

    def test_codex_hooks_example_is_valid_and_complete(self):
        path = self._repo_path("examples", "codex-hooks.json")
        with open(path, encoding="utf-8") as f:
            config = json.load(f)

        required = {
            "PermissionRequest",
            "PostToolUse",
            "SessionStart",
            "UserPromptSubmit",
            "SubagentStop",
            "Stop",
        }
        self.assertEqual(set(config["hooks"]), required)

        handlers = [
            handler
            for groups in config["hooks"].values()
            for group in groups
            for handler in group["hooks"]
        ]
        self.assertTrue(handlers)
        for handler in handlers:
            self.assertEqual(handler["type"], "command")
            self.assertEqual(
                handler["command"],
                "kbd-signal hook codex",
            )
            self.assertEqual(handler["timeout"], 5)

    def test_claude_hooks_example_is_valid_and_complete(self):
        path = self._repo_path("examples", "claude-hooks.json")
        with open(path, encoding="utf-8") as f:
            config = json.load(f)

        required = {
            "PermissionRequest",
            "PostToolUse",
            "Stop",
            "SessionEnd",
        }
        self.assertEqual(set(config["hooks"]), required)

        handlers = [
            handler
            for groups in config["hooks"].values()
            for group in groups
            for handler in group["hooks"]
        ]
        self.assertTrue(handlers)
        for handler in handlers:
            self.assertEqual(handler["type"], "command")
            self.assertEqual(
                handler["command"],
                "kbd-signal hook claude",
            )
            self.assertEqual(handler["timeout"], 5)

    def test_q1_he_8k_example_targets_the_wired_keyboard(self):
        path = self._repo_path("examples", "config.q1-he-8k.json")
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        self.assertIn("device", raw)

        # Resolve through the real config merge/parse path.
        with mock.patch.object(config, "CONFIG_FILE", path):
            dev = config.device()
        self.assertEqual(dev["vendor_id"], 0x3434)
        # product_id must pin the wired keyboard, not the Link-KM docking station.
        self.assertEqual(dev["product_id"], 0x1012)
        self.assertEqual(dev["v3_channel"], 3)
        self.assertEqual(dev["effects"]["solid"], 1)
        self.assertEqual(dev["effects"]["breathing"], 2)

    def test_package_versions_match(self):
        with open(self._repo_path("pyproject.toml"), "rb") as f:
            project = tomllib.load(f)
        self.assertEqual(project["project"]["version"], __version__)


if __name__ == "__main__":
    unittest.main()
