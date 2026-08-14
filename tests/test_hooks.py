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

    def test_codex_permission_request_logs_permission_mode(self):
        hooks.handle_codex([], self._stdin({
            "hook_event_name": "PermissionRequest",
            "session_id": "session-a",
            "permission_mode": "bypassPermissions",
        }))

        self.mocks["log"].assert_called_once()
        self.assertTrue(
            self.mocks["log"].call_args.args[0].endswith(
                " mode=bypassPermissions"
            )
        )
        self.mocks["set_state"].assert_called_once_with(
            "waiting",
            session="codex:session-a:main",
            owner_aliases=("session-a",),
        )

    def test_codex_permission_request_without_mode_keeps_log_format(self):
        hooks.handle_codex([], self._stdin({
            "hook_event_name": "PermissionRequest",
            "session_id": "session-a",
        }))

        self.mocks["log"].assert_called_once()
        # Byte-identical to the pre-#47 format: no suffix of any kind.
        tag = hooks._owner_tag("codex:session-a:main")
        self.assertEqual(
            self.mocks["log"].call_args.args[0],
            f"hook codex: event=PermissionRequest owner={tag}",
        )

    def test_codex_permission_request_logs_invalid_mode(self):
        for mode in (5, "bad\nvalue"):
            with self.subTest(mode=mode):
                self.mocks["log"].reset_mock()
                self.mocks["set_state"].reset_mock()

                hooks.handle_codex([], self._stdin({
                    "hook_event_name": "PermissionRequest",
                    "session_id": "session-a",
                    "permission_mode": mode,
                }))

                self.mocks["log"].assert_called_once()
                self.assertTrue(
                    self.mocks["log"].call_args.args[0].endswith(
                        " mode=invalid"
                    )
                )

    def test_non_permission_request_does_not_log_mode(self):
        hooks.handle_codex([], self._stdin({
            "hook_event_name": "PostToolUse",
            "session_id": "session-a",
            "permission_mode": "bypassPermissions",
        }))

        self.mocks["log"].assert_called_once()
        self.assertNotIn(" mode=", self.mocks["log"].call_args.args[0])

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


class GrokHookDispatchTests(unittest.TestCase):
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

    def _reset_mocks(self):
        for mocked in self.mocks.values():
            mocked.reset_mock()

    def test_permission_prompt_notification_sets_waiting(self):
        hooks.handle_grok(self._stdin({
            "hookEventName": "notification",
            "notificationType": "permission_prompt",
            "sessionId": "s1",
        }))

        self.mocks["set_state"].assert_called_once_with(
            "waiting",
            session="grok:s1:main",
            owner_aliases=("s1",),
        )

    def test_other_notification_types_are_ignored(self):
        payloads = (
            {
                "hookEventName": "notification",
                "notificationType": "idle_prompt",
                "sessionId": "s1",
            },
            {
                "hookEventName": "notification",
                "notificationType": "task_complete",
                "sessionId": "s1",
            },
            {
                "hookEventName": "notification",
                "sessionId": "s1",
            },
            {
                "hookEventName": "notification",
                "notificationType": 7,
                "sessionId": "s1",
            },
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self._reset_mocks()

                hooks.handle_grok(self._stdin(payload))

                self.mocks["set_state"].assert_not_called()
                self.mocks["release_waiting"].assert_not_called()

    def test_legacy_notification_type_sets_waiting(self):
        hooks.handle_grok(self._stdin({
            "hookEventName": "notification",
            "notification_type": "permission_prompt",
            "sessionId": "s1",
        }))

        self.mocks["set_state"].assert_called_once_with(
            "waiting",
            session="grok:s1:main",
            owner_aliases=("s1",),
        )

    def test_post_tool_use_releases_owner(self):
        hooks.handle_grok(self._stdin({
            "hookEventName": "post_tool_use",
            "sessionId": "s1",
        }))

        self.mocks["release_waiting"].assert_called_once_with(
            session="grok:s1:main",
            owner_aliases=("s1",),
        )

    def test_post_tool_use_failure_releases_owner(self):
        hooks.handle_grok(self._stdin({
            "hookEventName": "post_tool_use_failure",
            "sessionId": "s1",
        }))

        self.mocks["release_waiting"].assert_called_once_with(
            session="grok:s1:main",
            owner_aliases=("s1",),
        )

    def test_permission_denied_releases_owner(self):
        hooks.handle_grok(self._stdin({
            "hookEventName": "permission_denied",
            "sessionId": "s1",
        }))

        self.mocks["release_waiting"].assert_called_once_with(
            session="grok:s1:main",
            owner_aliases=("s1",),
        )

    def test_end_turn_stop_signals_done(self):
        hooks.handle_grok(self._stdin({
            "hookEventName": "stop",
            "reason": "end_turn",
            "sessionId": "s1",
        }))

        self.mocks["set_state"].assert_called_once_with(
            "done",
            session="grok:s1:main",
            owner_prefix="grok:s1:",
            owner_aliases=("s1",),
        )

    def test_session_close_stops_only_release_session_scope(self):
        for reason in ("shutdown", "channel_closed"):
            with self.subTest(reason=reason):
                self._reset_mocks()

                hooks.handle_grok(self._stdin({
                    "hookEventName": "stop",
                    "reason": reason,
                    "sessionId": "s1",
                }))

                self.mocks["release_waiting"].assert_called_once_with(
                    session="grok:s1:main",
                    owner_prefix="grok:s1:",
                    owner_aliases=("s1",),
                )
                self.mocks["set_state"].assert_not_called()

    def test_unknown_stop_reasons_release_only_owner(self):
        for reason in (None, "future_reason"):
            with self.subTest(reason=reason):
                self._reset_mocks()
                payload = {
                    "hookEventName": "stop",
                    "sessionId": "s1",
                }
                if reason is not None:
                    payload["reason"] = reason

                hooks.handle_grok(self._stdin(payload))

                self.mocks["release_waiting"].assert_called_once_with(
                    session="grok:s1:main",
                    owner_aliases=("s1",),
                )
                self.mocks["set_state"].assert_not_called()

    def test_stop_with_subagent_id_releases_without_done(self):
        hooks.handle_grok(self._stdin({
            "hookEventName": "stop",
            "reason": "end_turn",
            "sessionId": "s1",
            "subagentId": "child-1",
        }))

        self.mocks["release_waiting"].assert_called_once_with(
            session="grok:s1:main",
            owner_aliases=("s1",),
        )
        self.mocks["set_state"].assert_not_called()

    def test_subagent_stop_aliases_release_child_session_main_owner(self):
        for event in ("subagent_stop", "subagent_end"):
            with self.subTest(event=event):
                self._reset_mocks()

                hooks.handle_grok(self._stdin({
                    "hookEventName": event,
                    "sessionId": "child-1",
                    "subagentId": "child-1",
                }))

                self.mocks["release_waiting"].assert_called_once_with(
                    session="grok:child-1:main",
                    owner_aliases=("child-1",),
                )
                self.mocks["set_state"].assert_not_called()

    def test_stop_failure_releases_session_scope_without_done(self):
        hooks.handle_grok(self._stdin({
            "hookEventName": "stop_failure",
            "sessionId": "s1",
        }))

        self.mocks["release_waiting"].assert_called_once_with(
            session="grok:s1:main",
            owner_prefix="grok:s1:",
            owner_aliases=("s1",),
        )
        self.mocks["set_state"].assert_not_called()

    def test_session_lifecycle_events_release_session_scope(self):
        for event in (
            "session_start",
            "user_prompt_submit",
            "session_end",
        ):
            with self.subTest(event=event):
                self._reset_mocks()

                hooks.handle_grok(self._stdin({
                    "hookEventName": event,
                    "sessionId": "s1",
                }))

                self.mocks["release_waiting"].assert_called_once_with(
                    session="grok:s1:main",
                    owner_prefix="grok:s1:",
                    owner_aliases=("s1",),
                )
                self.mocks["set_state"].assert_not_called()

    def test_all_sources_use_distinct_owner_namespaces(self):
        canonical = {
            "hook_event_name": "PermissionRequest",
            "session_id": "same-session-id",
        }
        grok = {
            "hookEventName": "notification",
            "notificationType": "permission_prompt",
            "sessionId": "same-session-id",
        }

        hooks.handle_claude(self._stdin(canonical))
        hooks.handle_codex([], self._stdin(canonical))
        hooks.handle_grok(self._stdin(grok))

        calls = self.mocks["set_state"].call_args_list
        self.assertEqual(
            [call.kwargs["session"] for call in calls],
            [
                "claude:same-session-id:main",
                "codex:same-session-id:main",
                "grok:same-session-id:main",
            ],
        )

    def test_grok_payload_fed_to_claude_is_noop(self):
        hooks.handle_claude(self._stdin({
            "hookEventName": "stop",
            "reason": "end_turn",
            "sessionId": "s1",
        }))

        self.mocks["set_state"].assert_not_called()
        self.mocks["release_waiting"].assert_not_called()

    def test_grok_payload_fed_to_codex_is_noop(self):
        hooks.handle_codex([], self._stdin({
            "hookEventName": "stop",
            "reason": "end_turn",
            "sessionId": "s1",
        }))

        self.mocks["set_state"].assert_not_called()
        self.mocks["release_waiting"].assert_not_called()

    def test_snake_case_claude_payload_fed_to_grok_is_noop(self):
        hooks.handle_grok(self._stdin({
            "hook_event_name": "PermissionRequest",
            "session_id": "s1",
        }))

        self.mocks["set_state"].assert_not_called()
        self.mocks["release_waiting"].assert_not_called()

    def test_unhandled_grok_events_are_noops(self):
        events = (
            "pre_tool_use",
            "subagent_start",
            "pre_compact",
            "post_compact",
            "unknown_junk",
            17,
        )
        for event in events:
            with self.subTest(event=event):
                self._reset_mocks()

                hooks.handle_grok(self._stdin({
                    "hookEventName": event,
                    "sessionId": "s1",
                }))

                self.mocks["set_state"].assert_not_called()
                self.mocks["release_waiting"].assert_not_called()

    def test_missing_session_id_is_noop(self):
        hooks.handle_grok(self._stdin({
            "hookEventName": "notification",
            "notificationType": "permission_prompt",
        }))

        self.mocks["set_state"].assert_not_called()
        self.mocks["release_waiting"].assert_not_called()

    def test_permission_mode_is_copied_only_when_present(self):
        hooks.handle_grok(self._stdin({
            "hookEventName": "notification",
            "notificationType": "permission_prompt",
            "permissionMode": "askEveryTime",
            "sessionId": "s1",
        }))

        self.mocks["log"].assert_called_once()
        self.assertTrue(
            self.mocks["log"].call_args.args[0].endswith(
                " mode=askEveryTime"
            )
        )

        self._reset_mocks()
        hooks.handle_grok(self._stdin({
            "hookEventName": "notification",
            "notificationType": "permission_prompt",
            "sessionId": "s1",
        }))

        self.mocks["log"].assert_called_once()
        self.assertNotIn(" mode=", self.mocks["log"].call_args.args[0])

    def test_hostile_names_are_not_echoed_to_log(self):
        hostile_event = "x\ninjected"
        hostile_type = "type\ninjected"

        hooks.handle_grok(self._stdin({
            "hookEventName": hostile_event,
            "sessionId": "s1",
        }))
        hooks.handle_grok(self._stdin({
            "hookEventName": "notification",
            "notificationType": hostile_type,
            "sessionId": "s1",
        }))

        messages = "\n".join(
            call.args[0] for call in self.mocks["log"].call_args_list
        )
        self.assertIn("?", messages)
        self.assertNotIn(hostile_event, messages)
        self.assertNotIn(hostile_type, messages)

    def test_logs_owner_hash_without_raw_session_id(self):
        raw_session = "private-grok-session"
        hooks.handle_grok(self._stdin({
            "hookEventName": "notification",
            "notificationType": "permission_prompt",
            "sessionId": raw_session,
        }))

        messages = "\n".join(
            call.args[0] for call in self.mocks["log"].call_args_list
        )
        tag = hooks._owner_tag(f"grok:{raw_session}:main")
        self.assertIn(f"owner={tag}", messages)
        self.assertNotIn(raw_session, messages)

    def test_hook_handlers_never_write_to_stdout(self):
        import contextlib

        fixtures = (
            {
                "hookEventName": "notification",
                "notificationType": "permission_prompt",
                "sessionId": "s1",
            },
            {
                "hookEventName": "notification",
                "notificationType": "idle_prompt",
                "sessionId": "s1",
            },
            {
                "hookEventName": "post_tool_use",
                "sessionId": "s1",
            },
            {
                "hookEventName": "post_tool_use_failure",
                "sessionId": "s1",
            },
            {
                "hookEventName": "permission_denied",
                "sessionId": "s1",
            },
            {
                "hookEventName": "stop",
                "reason": "end_turn",
                "sessionId": "s1",
            },
            {
                "hookEventName": "stop",
                "reason": "shutdown",
                "sessionId": "s1",
            },
            {
                "hookEventName": "stop",
                "reason": "future_reason",
                "sessionId": "s1",
            },
            {
                "hookEventName": "stop_failure",
                "sessionId": "s1",
            },
            {
                "hookEventName": "subagent_stop",
                "sessionId": "child-1",
            },
            {
                "hookEventName": "session_start",
                "sessionId": "s1",
            },
            {
                "hookEventName": "user_prompt_submit",
                "sessionId": "s1",
            },
            {
                "hookEventName": "session_end",
                "sessionId": "s1",
            },
            {
                "hookEventName": "pre_tool_use",
                "sessionId": "s1",
            },
        )
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            for payload in fixtures:
                hooks.handle_grok(self._stdin(payload))
            hooks.handle_claude(self._stdin({
                "hookEventName": "stop",
                "reason": "end_turn",
                "sessionId": "s1",
            }))

        self.assertEqual(captured.getvalue(), "")

    def test_duplicate_end_turn_delivery_has_identical_dispatch(self):
        payload = {
            "hookEventName": "stop",
            "reason": "end_turn",
            "sessionId": "s1",
        }

        hooks.handle_grok(self._stdin(payload))
        hooks.handle_grok(self._stdin(payload))

        calls = self.mocks["set_state"].call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.mocks["release_waiting"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
