"""Dispatch Claude Code, Codex, Grok, and Cursor events to lighting states.

Design rule: hook entry points must NEVER block or fail the agent.
Every path exits 0, errors only go to the log file.
"""

import hashlib
import json
import sys

from . import states


def _read_stdin(stdin, source):
    stream = stdin or sys.stdin
    try:
        # Bare Windows hook environments decode text stdin using the locale
        # encoding, while hook payloads are UTF-8 JSON (#38). A detached
        # wrapper keeps the attribute but returns None, so probe the value.
        buffer = getattr(stream, "buffer", None)
        data = buffer.read() if buffer is not None else stream.read()
        payload = json.loads(data)
    except (TypeError, ValueError) as e:
        states.log(f"hook {source}: stdin parse failed ({e})")
        return None
    if not isinstance(payload, dict):
        states.log(f"hook {source}: stdin ignored (expected JSON object)")
        return None
    return payload


def _identity(source, payload):
    """Return canonical owner, session scope, aliases, and subagent status.

    New owner ids are namespaced by agent product so Claude and Codex cannot
    collide.  The raw session id is retained as an alias so an approval that
    was already active during an upgrade from v0.2.0 can still be released.
    """
    session = (payload.get("session_id") or payload.get("thread-id")
               or payload.get("thread_id"))
    if not isinstance(session, str) or not session:
        return None
    agent = payload.get("agent_id")
    is_subagent = isinstance(agent, str) and bool(agent)
    agent = agent if is_subagent else "main"
    session_scope = f"{source}:{session}:"
    owner = f"{session_scope}{agent}"
    return owner, session_scope, (session,), is_subagent


def _owner_tag(owner):
    """Short non-reversible id for diagnostics without logging session ids."""
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()[:12]


def _handle_lifecycle(source, payload):
    event = payload.get("hook_event_name", "")
    identity = _identity(source, payload)
    if identity is None:
        states.log(f"hook {source}: event={event or '?'} ignored (missing session id)")
        return

    owner, session_scope, aliases, is_subagent = identity
    mode_suffix = ""
    if event == "PermissionRequest" and "permission_mode" in payload:
        mode = payload["permission_mode"]
        # Sanitize external input before writing to the line-oriented log.
        if (isinstance(mode, str) and 0 < len(mode) <= 32
                and mode.isalnum()):
            mode_suffix = f" mode={mode}"
        else:
            mode_suffix = " mode=invalid"
    states.log(
        f"hook {source}: event={event or '?'} owner={_owner_tag(owner)}"
        f"{mode_suffix}"
    )

    if event == "PermissionRequest":
        states.set_state("waiting", session=owner, owner_aliases=aliases)
    elif event == "PostToolUse":
        # Only release this agent's approval. Other sessions keep waiting.
        states.release_waiting(session=owner, owner_aliases=aliases)
    elif event == "Stop":
        if is_subagent:
            # A child finishing must not flash green for the whole task.
            states.release_waiting(session=owner, owner_aliases=aliases)
        else:
            states.set_state(
                "done",
                session=owner,
                owner_prefix=session_scope,
                owner_aliases=aliases,
            )
    elif event == "SubagentStop":
        states.release_waiting(session=owner, owner_aliases=aliases)
    elif event in ("SessionStart", "UserPromptSubmit", "SessionEnd"):
        # A new main-session lifecycle edge proves an older approval from that
        # same session is stale. Subagent lifecycle edges only clear the child.
        states.release_waiting(
            session=owner,
            owner_prefix=None if is_subagent else session_scope,
            owner_aliases=aliases,
        )


def handle_claude(stdin=None):
    """Handle Claude Code hook JSON received on stdin."""
    payload = _read_stdin(stdin, "claude")
    if payload is not None:
        _handle_lifecycle("claude", payload)


def handle_codex(argv, stdin=None):
    """Handle current Codex hooks or the legacy ``notify`` JSON argument.

    Current lifecycle hooks write a Claude-compatible JSON object to stdin.
    Older Codex versions append an ``agent-turn-complete`` object to argv.
    """
    if not argv:
        payload = _read_stdin(stdin, "codex")
        if payload is not None:
            _handle_lifecycle("codex", payload)
        return

    try:
        payload = json.loads(argv[-1])
    except (TypeError, ValueError) as e:
        states.log(f"hook codex: notify parse failed ({e})")
        return
    if not isinstance(payload, dict):
        states.log("hook codex: notify ignored (expected JSON object)")
        return
    if payload.get("type") != "agent-turn-complete":
        return

    identity = _identity("codex", payload)
    if identity is None:
        states.log("hook codex: agent-turn-complete ignored (missing thread id)")
        return
    owner, session_scope, aliases, _ = identity
    states.log(f"hook codex: legacy complete owner={_owner_tag(owner)}")
    states.set_state(
        "done",
        session=owner,
        owner_prefix=session_scope,
        owner_aliases=aliases,
    )


# Canonical event       State operation
# PermissionRequest     Set waiting for one owner.
# PostToolUse           Release one owner's waiting state.
# SessionEnd            Release the session scope.
# SessionStart          Release the session scope.
# UserPromptSubmit      Release the session scope.
# Stop                  Signal done and clean the session scope.
# SubagentStop          Release one owner's waiting state.
_GROK_EVENTS = {
    "post_tool_use": "PostToolUse",
    "post_tool_use_failure": "PostToolUse",
    "permission_denied": "PostToolUse",
    "stop_failure": "SessionEnd",
    "subagent_stop": "SubagentStop",
    "subagent_end": "SubagentStop",
    "session_start": "SessionStart",
    "user_prompt_submit": "UserPromptSubmit",
    "session_end": "SessionEnd",
}

_GROK_WAITING_TYPES = frozenset({"permission_prompt"})

_GROK_SESSION_CLOSE_REASONS = frozenset({
    "channel_closed",
    "shutdown",
})

_GROK_LOGGABLE_EVENTS = frozenset({
    "session_start",
    "user_prompt_submit",
    "pre_tool_use",
    "post_tool_use",
    "post_tool_use_failure",
    "permission_denied",
    "stop",
    "stop_failure",
    "notification",
    "subagent_start",
    "subagent_stop",
    "subagent_end",
    "pre_compact",
    "post_compact",
    "session_end",
})

_GROK_LOGGABLE_NOTIFICATION_TYPES = frozenset({
    "idle_prompt",
    "permission_prompt",
    "task_complete",
})


def _grok_notification_type(payload):
    if "notificationType" in payload:
        return payload["notificationType"]
    return payload.get("notification_type")


def _grok_safe_name(value, allowed):
    if isinstance(value, str) and value in allowed:
        return value
    return "?"


def _grok_event(payload):
    event = payload.get("hookEventName")
    if not isinstance(event, str):
        return None

    if event == "notification":
        notification_type = _grok_notification_type(payload)
        # isinstance before the set lookup: an unhashable wire value (list,
        # dict) must degrade to "ignored", not to a swallowed TypeError.
        if (isinstance(notification_type, str)
                and notification_type in _GROK_WAITING_TYPES):
            return "PermissionRequest"
        return None

    if event == "stop":
        subagent_id = payload.get("subagentId")
        if isinstance(subagent_id, str) and subagent_id:
            return "SubagentStop"

        reason = payload.get("reason")
        if reason == "end_turn":
            return "Stop"
        if (isinstance(reason, str)
                and reason in _GROK_SESSION_CLOSE_REASONS):
            return "SessionEnd"
        # Future stop reasons must not signal success or erase sibling owners.
        return "PostToolUse"

    return _GROK_EVENTS.get(event)


def handle_grok(stdin=None):
    """Handle Grok Build hook JSON received on stdin."""
    try:
        payload = _read_stdin(stdin, "grok")
        if payload is None:
            return

        event = _grok_event(payload)
        if event is None:
            raw_event = payload.get("hookEventName")
            safe_event = _grok_safe_name(
                raw_event,
                _GROK_LOGGABLE_EVENTS,
            )
            detail = ""
            if raw_event == "notification":
                safe_type = _grok_safe_name(
                    _grok_notification_type(payload),
                    _GROK_LOGGABLE_NOTIFICATION_TYPES,
                )
                detail = f" type={safe_type}"
            states.log(
                f"hook grok: event={safe_event}{detail} ignored"
            )
            return

        normalized = {
            "hook_event_name": event,
            "session_id": payload.get("sessionId"),
        }
        if "permissionMode" in payload:
            normalized["permission_mode"] = payload["permissionMode"]

        # Grok subagents are independent sessions. Keeping every session on
        # "main" ensures child waiting and child cleanup address the same owner.
        _handle_lifecycle("grok", normalized)
    except Exception as e:
        # The Stop hook is a blocking gate, so even unexpected local failures
        # must be contained without writing a response to stdout.
        try:
            states.log(f"hook grok: error ({type(e).__name__})")
        except Exception:
            pass


# Cursor is completion-notify only: its hooks API has no approval-wait event.
# A completed top-level turn is therefore the only DONE_SESSION operation.
# Every other recognized event is a logged no-op because nothing exists to
# release; revisit this if Cursor ships an approval event.
_CURSOR_LOGGABLE_EVENTS = frozenset({
    "sessionStart",
    "sessionEnd",
    "beforeSubmitPrompt",
    "preToolUse",
    "postToolUse",
    "postToolUseFailure",
    "beforeShellExecution",
    "afterShellExecution",
    "beforeMCPExecution",
    "afterMCPExecution",
    "beforeReadFile",
    "afterFileEdit",
    "stop",
    "subagentStart",
    "subagentStop",
    "preCompact",
    "afterAgentResponse",
    "afterAgentThought",
    "workspaceOpen",
})

_CURSOR_LOGGABLE_STATUSES = frozenset({
    "completed",
    "aborted",
    "error",
})


def _cursor_event(payload):
    event = payload.get("hook_event_name")
    if not isinstance(event, str):
        return None

    if event == "stop":
        subagent_id = payload.get("subagent_id")
        if isinstance(subagent_id, str) and subagent_id:
            return None

        status = payload.get("status")
        if status == "completed":
            return "Stop"

    return None


def handle_cursor(stdin=None):
    """Handle Cursor hook JSON received on stdin."""
    try:
        payload = _read_stdin(stdin, "cursor")
        if payload is None:
            return

        event = _cursor_event(payload)
        if event is None:
            raw_event = payload.get("hook_event_name")
            safe_event = _grok_safe_name(
                raw_event,
                _CURSOR_LOGGABLE_EVENTS,
            )
            detail = ""
            if raw_event == "stop":
                safe_status = _grok_safe_name(
                    payload.get("status"),
                    _CURSOR_LOGGABLE_STATUSES,
                )
                detail = f" status={safe_status}"
            states.log(
                f"hook cursor: event={safe_event}{detail} ignored"
            )
            return

        session = payload.get("session_id")
        if not isinstance(session, str) or not session:
            session = payload.get("conversation_id")
        normalized = {
            "hook_event_name": event,
            "session_id": session,
        }
        _handle_lifecycle("cursor", normalized)
    except Exception as e:
        # Cursor consumes hook stdout as control JSON. Unexpected failures must
        # be contained without emitting a followup_message or any other output.
        try:
            states.log(f"hook cursor: error ({type(e).__name__})")
        except Exception:
            pass
