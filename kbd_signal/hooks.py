"""Dispatch Claude Code and Codex lifecycle events to lighting states.

Design rule: hook entry points must NEVER block or fail the agent.
Every path exits 0, errors only go to the log file.
"""

import hashlib
import json
import sys

from . import states


def _read_stdin(stdin, source):
    try:
        payload = json.load(stdin or sys.stdin)
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
    states.log(
        f"hook {source}: event={event or '?'} owner={_owner_tag(owner)}"
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
