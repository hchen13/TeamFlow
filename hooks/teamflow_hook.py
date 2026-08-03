from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any


def read_input() -> dict[str, Any]:
    try:
        value = json.load(__import__("sys").stdin)
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def daemon_request(payload: dict[str, Any], *, timeout: float = 2) -> dict[str, Any]:
    home = Path(os.environ.get("TEAMFLOW_HOME", "~/.teamflow")).expanduser()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(home / "daemon.sock"))
        stream = client.makefile("rwb")
        stream.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        stream.flush()
        line = stream.readline()
    if not line:
        raise ValueError("TeamFlow daemon closed the connection")
    response = json.loads(line)
    if not response.get("ok"):
        raise ValueError(response.get("error") or "TeamFlow daemon request failed")
    result = response.get("result")
    if not isinstance(result, dict):
        raise ValueError("TeamFlow daemon returned an invalid response")
    return result


def write_output(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def inject_assignment_context(hook: dict[str, Any], *, event_name: str) -> bool:
    session_id = str(hook.get("session_id") or "")
    if not session_id:
        return False
    try:
        result = daemon_request({
            "action": "assignment_context",
            "session_id": session_id,
            "cwd": hook.get("cwd"),
            "consume": True,
            "refresh": False,
        })
    except (OSError, TimeoutError, ValueError):
        return False
    context = result.get("additional_context")
    if not context:
        return False
    write_output({
        "hookSpecificOutput": {
            "hookEventName": event_name,
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
        # The context is already delivered; an unconfirmed injection stays pending
        # so the next UserPromptSubmit retries instead of silently losing it.
        pass
    return True
