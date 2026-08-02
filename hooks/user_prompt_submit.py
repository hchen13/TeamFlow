from __future__ import annotations

from codex_runtime import record_runtime_event
from teamflow_hook import daemon_request, read_input, write_output


def main() -> None:
    hook = read_input()
    record_runtime_event(hook)
    session_id = str(hook.get("session_id") or "")
    if not session_id:
        return
    hook_event_name = str(hook.get("hook_event_name") or "UserPromptSubmit")
    if hook_event_name == "PostCompact":
        try:
            daemon_request({
                "action": "compact_assignment_context",
                "session_id": session_id,
                "cwd": hook.get("cwd"),
            })
        except (OSError, TimeoutError, ValueError):
            pass
        return
    if hook_event_name != "UserPromptSubmit":
        return
    try:
        result = daemon_request({
            "action": "assignment_context",
            "session_id": session_id,
            "cwd": hook.get("cwd"),
            "consume": True,
            "refresh": False,
        })
    except (OSError, TimeoutError, ValueError):
        return
    context = result.get("additional_context")
    if context:
        write_output({
            "hookSpecificOutput": {
                "hookEventName": hook_event_name,
                "additionalContext": context,
            }
        })
        assignment = result["assignment"]
        try:
            daemon_request({
                "action": "confirm_assignment_context",
                "workspace": assignment["workspace_root"],
                "agent_id": assignment["agent_id"],
                "session_id": session_id,
                "assignment_revision": assignment["assignment_revision"],
                "context_fingerprint": result["context_fingerprint"],
                "context_kind": result["context_kind"],
            })
        except (OSError, TimeoutError, ValueError):
            pass


if __name__ == "__main__":
    main()
