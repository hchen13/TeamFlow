from __future__ import annotations

import json
from typing import Any

from codex_runtime import record_runtime_event
from teamflow_hook import daemon_request, inject_assignment_context, read_input


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
        return
    inject_assignment_context(hook, event_name="SessionStart")


if __name__ == "__main__":
    main()
