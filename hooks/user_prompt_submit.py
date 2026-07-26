from __future__ import annotations

from teamflow_hook import daemon_request, read_input, write_output


def main() -> None:
    hook = read_input()
    session_id = str(hook.get("session_id") or "")
    if not session_id:
        return
    hook_event_name = str(hook.get("hook_event_name") or "UserPromptSubmit")
    refresh = hook_event_name == "SessionStart" and hook.get("source") == "compact"
    if hook_event_name != "UserPromptSubmit" and not refresh:
        return
    try:
        result = daemon_request({
            "action": "assignment_context",
            "session_id": session_id,
            "cwd": hook.get("cwd"),
            "consume": True,
            "refresh": refresh,
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
