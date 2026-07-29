import io
import json
import unittest
from unittest import mock

from kbd_signal import hooks


class HookDispatchTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.multiple(
            hooks.states,
            set_state=mock.DEFAULT,
            release_waiting=mock.DEFAULT,
            log=mock.DEFAULT,
        )
        self.mocks = patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _stdin(payload):
        return io.StringIO(json.dumps(payload))

    def test_utf8_payload_ignores_text_layer_locale_encoding(self):
        payload = {
            "hook_event_name": "PermissionRequest",
            "session_id": "session-utf8",
            "tool_response": "パス C:\\Users\\日本語\\ファイル.txt",
        }
        stdin = io.TextIOWrapper(
            io.BytesIO(
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
            ),
            encoding="cp932",
        )

        hooks.handle_claude(stdin)

        self.mocks["set_state"].assert_called_once_with(
            "waiting",
            session="claude:session-utf8:main",
            owner_aliases=("session-utf8",),
        )

    def test_bytesio_payload_without_text_layer_parses(self):
        payload = {
            "hook_event_name": "PermissionRequest",
            "session_id": "session-bytes",
            "tool_response": "日本語",
        }
        stdin = io.BytesIO(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )

        hooks.handle_codex([], stdin)

        self.mocks["set_state"].assert_called_once_with(
            "waiting",
            session="codex:session-bytes:main",
            owner_aliases=("session-bytes",),
        )

    def test_detached_text_stream_is_safe_noop(self):
        # After detach() the wrapper still has a `buffer` attribute but it
        # is None; reading must fall back and stay a logged no-op.
        stdin = io.TextIOWrapper(io.BytesIO(b"{}"))
        stdin.detach()

        hooks.handle_claude(stdin)

        self.mocks["set_state"].assert_not_called()
        self.mocks["release_waiting"].assert_not_called()
        self.mocks["log"].assert_called_once()

    def test_codex_permission_request_uses_namespaced_owner(self):
        hooks.handle_codex([], self._stdin({
            "hook_event_name": "PermissionRequest",
            "session_id": "session-a",
            "turn_id": "turn-1",
        }))

        self.mocks["set_state"].assert_called_once_with(
            "waiting",
            session="codex:session-a:main",
            owner_aliases=("session-a",),
        )

    def test_claude_and_codex_sessions_have_distinct_owners(self):
        payload = {
            "hook_event_name": "PermissionRequest",
            "session_id": "same-session-id",
        }
        hooks.handle_claude(self._stdin(payload))
        hooks.handle_codex([], self._stdin(payload))

        calls = self.mocks["set_state"].call_args_list
        self.assertEqual(calls[0].kwargs["session"],
                         "claude:same-session-id:main")
        self.assertEqual(calls[1].kwargs["session"],
                         "codex:same-session-id:main")

    def test_post_tool_use_releases_only_its_agent(self):
        hooks.handle_codex([], self._stdin({
            "hook_event_name": "PostToolUse",
            "session_id": "session-a",
            "agent_id": "agent-7",
        }))

        self.mocks["release_waiting"].assert_called_once_with(
            session="codex:session-a:agent-7",
            owner_aliases=("session-a",),
        )

    def test_main_stop_releases_whole_session_then_signals_done(self):
        hooks.handle_codex([], self._stdin({
            "hook_event_name": "Stop",
            "session_id": "session-a",
        }))

        self.mocks["set_state"].assert_called_once_with(
            "done",
            session="codex:session-a:main",
            owner_prefix="codex:session-a:",
            owner_aliases=("session-a",),
        )

    def test_subagent_stop_does_not_signal_done(self):
        hooks.handle_codex([], self._stdin({
            "hook_event_name": "SubagentStop",
            "session_id": "session-a",
            "agent_id": "agent-7",
        }))

        self.mocks["set_state"].assert_not_called()
        self.mocks["release_waiting"].assert_called_once_with(
            session="codex:session-a:agent-7",
            owner_aliases=("session-a",),
        )

    def test_stop_with_agent_id_is_treated_as_child_cleanup(self):
        hooks.handle_claude(self._stdin({
            "hook_event_name": "Stop",
            "session_id": "session-a",
            "agent_id": "agent-7",
        }))

        self.mocks["set_state"].assert_not_called()
        self.mocks["release_waiting"].assert_called_once()

    def test_main_session_start_cleans_whole_session_scope(self):
        hooks.handle_codex([], self._stdin({
            "hook_event_name": "SessionStart",
            "session_id": "session-a",
        }))

        self.mocks["release_waiting"].assert_called_once_with(
            session="codex:session-a:main",
            owner_prefix="codex:session-a:",
            owner_aliases=("session-a",),
        )

    def test_legacy_notify_uses_thread_id_as_owner(self):
        payload = json.dumps({
            "type": "agent-turn-complete",
            "thread-id": "thread-a",
        })
        hooks.handle_codex([payload])

        self.mocks["set_state"].assert_called_once_with(
            "done",
            session="codex:thread-a:main",
            owner_prefix="codex:thread-a:",
            owner_aliases=("thread-a",),
        )

    def test_missing_session_id_is_safe_noop(self):
        hooks.handle_codex([], self._stdin({
            "hook_event_name": "PermissionRequest",
        }))

        self.mocks["set_state"].assert_not_called()
        self.mocks["release_waiting"].assert_not_called()

    def test_non_object_payload_is_safe_noop(self):
        hooks.handle_codex([], self._stdin(["not", "an", "object"]))
        hooks.handle_codex([json.dumps(["legacy", "array"])])

        self.mocks["set_state"].assert_not_called()
        self.mocks["release_waiting"].assert_not_called()

    def test_logs_hash_instead_of_raw_session_id(self):
        hooks.handle_codex([], self._stdin({
            "hook_event_name": "PermissionRequest",
            "session_id": "private-session-value",
        }))

        messages = "\n".join(
            call.args[0] for call in self.mocks["log"].call_args_list
        )
        self.assertNotIn("private-session-value", messages)
        self.assertIn("owner=", messages)


if __name__ == "__main__":
    unittest.main()
