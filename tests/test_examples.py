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

    def test_claude_hooks_matcher_contract_matches_spec(self):
        # Pin the per-event matcher contract for the events this example uses:
        # Stop silently ignores `matcher` (a leftover would be a copy-paste
        # trap), the other three honor it. Either direction (adding one to
        # Stop, dropping it from a supported event) breaks the test. Also
        # asserts each event's groups and every group's hooks are non-empty,
        # so a stray `[]` can't slip past.
        path = self._repo_path("examples", "claude-hooks.json")
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
        matcher_supported = {"PermissionRequest", "PostToolUse", "SessionEnd"}
        for event, groups in config["hooks"].items():
            self.assertTrue(groups, f"{event} has no hook groups")
            for group in groups:
                self.assertTrue(
                    group.get("hooks"), f"{event}: hook group has no handlers",
                )
                if event in matcher_supported:
                    self.assertEqual(
                        group.get("matcher"), "*",
                        f"{event} supports matcher; example should set it to '*'",
                    )
                else:
                    self.assertNotIn(
                        "matcher", group,
                        f"{event} silently ignores matcher; drop the key",
                    )

    def test_codex_hooks_matcher_contract_matches_spec(self):
        # Same shape as the Claude example (see above), scoped to the events
        # this Codex example uses: Codex silently ignores matcher on Stop /
        # UserPromptSubmit.
        path = self._repo_path("examples", "codex-hooks.json")
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
        matcher_supported = {
            "PermissionRequest", "PostToolUse", "SessionStart", "SubagentStop",
        }
        for event, groups in config["hooks"].items():
            self.assertTrue(groups, f"{event} has no hook groups")
            for group in groups:
                self.assertTrue(
                    group.get("hooks"), f"{event}: hook group has no handlers",
                )
                if event in matcher_supported:
                    self.assertEqual(
                        group.get("matcher"), "*",
                        f"{event} supports matcher; example should set it to '*'",
                    )
                else:
                    self.assertNotIn(
                        "matcher", group,
                        f"{event} silently ignores matcher; drop the key",
                    )

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
        # This board resets color/brightness after an effect change; the
        # preset must opt into the workaround so `done` doesn't stick red.
        self.assertTrue(dev["reset_on_effect"])
        self.assertEqual(dev["effects"]["solid"], 1)
        self.assertEqual(dev["effects"]["breathing"], 2)

    def test_package_versions_match(self):
        with open(self._repo_path("pyproject.toml"), "rb") as f:
            project = tomllib.load(f)
        self.assertEqual(project["project"]["version"], __version__)


class GrokExampleConfigTests(unittest.TestCase):
    @staticmethod
    def _repo_path(*parts):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            *parts,
        )

    def _load_example(self):
        path = self._repo_path("examples", "grok-hooks.json")
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_grok_hooks_example_is_valid_and_complete(self):
        config = self._load_example()
        required = {
            "Notification",
            "PostToolUse",
            "PostToolUseFailure",
            "PermissionDenied",
            "StopFailure",
            "SubagentStop",
            "SessionStart",
            "UserPromptSubmit",
            "Stop",
            "SessionEnd",
        }
        self.assertEqual(set(config["hooks"]), required)

        for event, groups in config["hooks"].items():
            self.assertTrue(groups, f"{event} has no hook groups")
            for group in groups:
                handlers = group.get("hooks")
                self.assertTrue(
                    handlers,
                    f"{event}: hook group has no handlers",
                )
                for handler in handlers:
                    self.assertEqual(handler["type"], "command")
                    self.assertEqual(
                        handler["command"],
                        "kbd-signal hook grok",
                    )
                    self.assertEqual(handler["timeout"], 5)

    def test_grok_matchers_are_regexes_and_only_notification_has_one(self):
        config = self._load_example()

        for event, groups in config["hooks"].items():
            for group in groups:
                self.assertNotEqual(
                    group.get("matcher"),
                    "*",
                    "Grok matchers are regexes; '*' is an invalid regex",
                )
                if event == "Notification":
                    self.assertEqual(
                        group.get("matcher"),
                        "^permission_prompt$",
                    )
                else:
                    self.assertNotIn(
                        "matcher", group,
                        f"{event} must omit matcher",
                    )

    def test_every_registered_grok_event_is_dispatched(self):
        from kbd_signal import hooks

        config = self._load_example()
        # Registered event -> (representative payload, canonical dispatch).
        # Pinning the canonical name (not just "is dispatched") catches a
        # mapping regression such as Stop losing its reason branch.
        payloads = {
            "Notification": ({
                "hookEventName": "notification",
                "notificationType": "permission_prompt",
            }, "PermissionRequest"),
            "PostToolUse": ({
                "hookEventName": "post_tool_use",
            }, "PostToolUse"),
            "PostToolUseFailure": ({
                "hookEventName": "post_tool_use_failure",
            }, "PostToolUse"),
            "PermissionDenied": ({
                "hookEventName": "permission_denied",
            }, "PostToolUse"),
            "StopFailure": ({
                "hookEventName": "stop_failure",
            }, "SessionEnd"),
            "SubagentStop": ({
                "hookEventName": "subagent_stop",
            }, "SubagentStop"),
            "SessionStart": ({
                "hookEventName": "session_start",
            }, "SessionStart"),
            "UserPromptSubmit": ({
                "hookEventName": "user_prompt_submit",
            }, "UserPromptSubmit"),
            "Stop": ({
                "hookEventName": "stop",
                "reason": "end_turn",
            }, "Stop"),
            "SessionEnd": ({
                "hookEventName": "session_end",
            }, "SessionEnd"),
        }

        self.assertEqual(set(payloads), set(config["hooks"]))
        for event, (payload, canonical) in payloads.items():
            with self.subTest(event=event):
                self.assertEqual(
                    hooks._grok_event(payload),
                    canonical,
                    f"{event} is registered but dispatches unexpectedly",
                )


class CursorExampleConfigTests(unittest.TestCase):
    @staticmethod
    def _repo_path(*parts):
        return os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            *parts,
        )

    def _load_example(self):
        path = self._repo_path("examples", "cursor-hooks.json")
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_cursor_hooks_example_is_minimal_and_valid(self):
        config = self._load_example()

        self.assertEqual(config["version"], 1)
        self.assertEqual(set(config["hooks"]), {"stop"})
        self.assertEqual(len(config["hooks"]["stop"]), 1)

        handler = config["hooks"]["stop"][0]
        self.assertEqual(handler["command"], "kbd-signal hook cursor")
        self.assertEqual(handler["timeout"], 5)
        self.assertNotIn("type", handler)
        self.assertNotIn("matcher", handler)

    def test_cursor_completed_stop_dispatches_to_canonical_stop(self):
        from kbd_signal import hooks

        payload = {
            "hook_event_name": "stop",
            "status": "completed",
            "session_id": "representative-session",
        }
        self.assertEqual(hooks._cursor_event(payload), "Stop")


if __name__ == "__main__":
    unittest.main()
