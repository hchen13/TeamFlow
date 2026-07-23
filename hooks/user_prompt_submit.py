from __future__ import annotations

from teamflow_hook import daemon_request, read_input, write_output


def main() -> None:
    hook = read_input()
    session_id = str(hook.get("session_id") or "")
    if not session_id:
        return
    try:
        result = daemon_request({
            "action": "assignment_context",
            "session_id": session_id,
            "cwd": hook.get("cwd"),
            "consume": True,
        })
    except (OSError, TimeoutError, ValueError):
        return
    context = result.get("additional_context")
    if context:
        write_output({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        })


if __name__ == "__main__":
    main()
