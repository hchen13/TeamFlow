from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import Any, Callable


PLUGIN_ROOT = Path(__file__).resolve().parent.parent


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


def local_request() -> Callable[..., dict[str, Any]] | None:
    """Serve the daemon's context actions straight from the local databases.

    Only reached when the daemon socket is unreachable: a compact boundary missed
    there would otherwise leave the agent finishing its turn without a role.
    """
    if str(PLUGIN_ROOT) not in sys.path:
        sys.path.append(str(PLUGIN_ROOT))
    try:
        from core.agent_context_runtime import AgentContextRuntime
        from core.agent_runtime import (
            confirm_agent_context,
            find_agent_assignment,
            mark_agent_context_recovery_pending,
        )
        from core.global_db import registered_workspaces
    except ImportError:
        return None

    sources = {
        "find_agent_assignment": find_agent_assignment,
        "confirm_agent_context": confirm_agent_context,
        "mark_agent_context_recovery_pending": mark_agent_context_recovery_pending,
    }
    runtime = AgentContextRuntime(
        workspaces=lambda: registered_workspaces(enabled_only=True),
        resolve=sources.__getitem__,
        emit_log=lambda *_args, **_kwargs: None,
        style=lambda text, _code: text,
    )

    def request(payload: dict[str, Any], *, timeout: float = 2) -> dict[str, Any]:
        action = payload["action"]
        if action == "compact_assignment_context":
            return runtime.mark_compacted(
                session_id=payload["session_id"],
                cwd=payload.get("cwd"),
            )
        if action == "assignment_context":
            return runtime.assignment(
                session_id=payload["session_id"],
                cwd=payload.get("cwd"),
                consume=bool(payload.get("consume")),
                refresh=bool(payload.get("refresh")),
            )
        return runtime.confirm(
            workspace=payload["workspace"],
            agent_id=payload["agent_id"],
            session_id=payload["session_id"],
            assignment_revision=int(payload["assignment_revision"]),
            context_fingerprint=payload["context_fingerprint"],
            context_kind=payload.get("context_kind"),
        )

    return request


def inject_assignment_context(
    hook: dict[str, Any],
    *,
    event_name: str,
    request: Callable[..., dict[str, Any]] | None = None,
) -> bool:
    request = request or daemon_request
    session_id = str(hook.get("session_id") or "")
    if not session_id:
        return False
    try:
        result = request({
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
        request({
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
