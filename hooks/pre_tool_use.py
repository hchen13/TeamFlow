from __future__ import annotations

from teamflow_hook import daemon_request, read_input, write_output


def main() -> None:
    hook = read_input()
    tool_name = str(hook.get("tool_name") or "")
    if not tool_name.startswith("mcp__teamflow__"):
        return
    tool_input = hook.get("tool_input")
    arguments = dict(tool_input) if isinstance(tool_input, dict) else {}
    arguments.pop("teamflow_authorization", None)
    try:
        result = daemon_request({
            "action": "authorize_tool",
            "session_id": str(hook.get("session_id") or ""),
            "cwd": hook.get("cwd"),
            "turn_id": hook.get("turn_id"),
            "tool_name": tool_name,
            "tool_input": arguments,
        })
    except (OSError, TimeoutError, ValueError) as error:
        write_output({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": str(error),
            }
        })
        return
    arguments["teamflow_authorization"] = result["grant"]
    write_output({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": arguments,
        }
    })


if __name__ == "__main__":
    main()
