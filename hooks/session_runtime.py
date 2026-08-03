from __future__ import annotations

import json
import sys
from typing import Any

from codex_runtime import record_runtime_event
from teamflow_hook import (
    daemon_request,
    inject_assignment_context,
    local_request,
    read_input,
)


def main() -> None:
    hook = read_input()
    record_runtime_event(hook)
    event = str(hook.get("hook_event_name") or "")
    if event == "SessionStart" and str(hook.get("source") or "") == "compact":
        recover_compacted_context(hook)
        return
    if event == "Stop":
        print(json.dumps({}))


def recover_compacted_context(hook: dict[str, Any]) -> None:
    session_id = str(hook.get("session_id") or "")
    if not session_id:
        return
    try:
        daemon_request({
            "action": "compact_assignment_context",
            "session_id": session_id,
            "cwd": hook.get("cwd"),
        })
    except (OSError, TimeoutError, ValueError):
        recover_without_daemon(hook, session_id)
        return
    inject_assignment_context(hook, event_name="SessionStart")


def recover_without_daemon(hook: dict[str, Any], session_id: str) -> None:
    request = local_request()
    if request is None:
        return
    try:
        marked = request({
            "action": "compact_assignment_context",
            "session_id": session_id,
            "cwd": hook.get("cwd"),
        })
    except Exception:
        # Nothing here proves this session belongs to TeamFlow, so it stays untouched.
        return
    if not marked.get("marked"):
        return
    try:
        inject_assignment_context(hook, event_name="SessionStart", request=request)
    except Exception as error:
        # The assignment is already pending, so the next UserPromptSubmit retries.
        print(f"TeamFlow could not restore the compacted assignment: {error}", file=sys.stderr)


if __name__ == "__main__":
    main()
