"""Dispatch Claude Code hook events / Codex notify events to states.

Design rule: hook entry points must NEVER block or fail the agent.
Every path exits 0, errors only go to the log file.
"""

import json
import sys

from . import states


def handle_claude(stdin=None):
    """Claude Code hooks pipe a JSON payload on stdin. One command serves
    all registered events, dispatched by hook_event_name."""
    try:
        payload = json.load(stdin or sys.stdin)
    except ValueError as e:
        states.log(f"hook claude: stdin parse failed ({e})")
        return
    event = payload.get("hook_event_name", "")
    session = payload.get("session_id")
    states.log(f"hook claude: event={event or '?'} session={session}")

    if event == "PermissionRequest":
        states.set_state("waiting", session=session)
    elif event == "PostToolUse":
        # Fast path: only touch HID when a "waiting" signal needs clearing
        # (fires on every tool call, so stay cheap by default).
        if states.load_state()["active"] == "waiting":
            states.restore(session=session)
    elif event == "Stop":
        states.set_state("done", session=session)
    elif event == "SessionEnd":
        if states.is_active():
            states.restore(session=session)


def handle_codex(argv):
    """Codex CLI notify passes one JSON argument:
    {"type": "agent-turn-complete", ...}"""
    if not argv:
        return
    try:
        payload = json.loads(argv[-1])
    except ValueError:
        return
    if payload.get("type") == "agent-turn-complete":
        states.set_state("done")
