import json
import os
import tomllib
import unittest

from kbd_signal import __version__


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

    def test_package_versions_match(self):
        with open(self._repo_path("pyproject.toml"), "rb") as f:
            project = tomllib.load(f)
        self.assertEqual(project["project"]["version"], __version__)


if __name__ == "__main__":
    unittest.main()
