from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .daemon import _daemon_request


mcp = FastMCP("TeamFlow")
CODEX_TURN_METADATA_KEY = "x-codex-turn-metadata"


def _request_metadata(context: Context) -> dict[str, Any]:
    metadata = context.request_context.meta
    if metadata is None:
        raise ValueError("TeamFlow requires Codex MCP request metadata")
    if hasattr(metadata, "model_dump"):
        payload = metadata.model_dump(exclude_none=True)
    elif isinstance(metadata, dict):
        payload = dict(metadata)
    else:
        raise ValueError("TeamFlow received invalid Codex MCP request metadata")
    return payload


def _codex_caller(context: Context) -> dict[str, Any]:
    metadata = _request_metadata(context)
    turn_metadata = metadata.get(CODEX_TURN_METADATA_KEY)
    if isinstance(turn_metadata, str):
        try:
            turn_metadata = json.loads(turn_metadata)
        except json.JSONDecodeError as error:
            raise ValueError("TeamFlow received invalid Codex turn metadata") from error
    if not isinstance(turn_metadata, dict):
        turn_metadata = {}

    thread_id = str(metadata.get("threadId") or "").strip()
    metadata_thread_id = str(turn_metadata.get("thread_id") or "").strip()
    if thread_id and metadata_thread_id and thread_id != metadata_thread_id:
        raise ValueError("TeamFlow received inconsistent Codex thread metadata")
    session_id = thread_id or metadata_thread_id
    if not session_id:
        raise ValueError("TeamFlow requires a Codex thread identity")
    return {
        "session_id": session_id,
        "turn_id": str(turn_metadata.get("turn_id") or "").strip() or None,
    }


def _invoke(
    tool_name: str,
    arguments: dict[str, Any],
    context: Context,
) -> dict[str, Any]:
    caller = _codex_caller(context)
    authorization = _daemon_request(
        {
            "action": "authorize_tool",
            "session_id": caller["session_id"],
            "turn_id": caller["turn_id"],
            "tool_name": f"mcp__teamflow__{tool_name}",
            "tool_input": arguments,
        },
        timeout=30,
    )
    return _daemon_request(
        {
            "action": "invoke_tool",
            "grant": authorization["grant"],
            "tool_name": tool_name,
            "arguments": arguments,
        },
        timeout=30,
    )


@mcp.tool()
def get_assignment(context: Context) -> dict[str, Any]:
    """Return the caller's trusted TeamFlow workspace, workflow, role, and agent assignment."""
    return _invoke("get_assignment", {}, context)


@mcp.tool()
def list_available_tasks(context: Context) -> dict[str, Any]:
    """List Ready tasks that the caller's registered TeamFlow role is allowed to claim."""
    return _invoke("list_available_tasks", {}, context)


@mcp.tool()
def get_task(record_id: str, context: Context) -> dict[str, Any]:
    """Read one complete TeamFlow task by its Lark record ID."""
    return _invoke("get_task", {"record_id": record_id}, context)


@mcp.tool()
def claim_task(record_id: str, context: Context) -> dict[str, Any]:
    """Atomically claim one Ready TeamFlow task for the caller and move it to In Progress."""
    return _invoke("claim_task", {"record_id": record_id}, context)


def run_mcp_server() -> None:
    mcp.run(transport="stdio")
