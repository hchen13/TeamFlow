from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .daemon import _daemon_request


mcp = FastMCP("TeamFlow")


def _invoke(tool_name: str, arguments: dict[str, Any], grant: str) -> dict[str, Any]:
    return _daemon_request(
        {
            "action": "invoke_tool",
            "grant": grant,
            "tool_name": tool_name,
            "arguments": arguments,
        },
        timeout=30,
    )


@mcp.tool()
def get_assignment(teamflow_authorization: str = "") -> dict[str, Any]:
    """Return the caller's trusted TeamFlow workspace, workflow, role, and agent assignment."""
    return _invoke("get_assignment", {}, teamflow_authorization)


@mcp.tool()
def list_available_tasks(teamflow_authorization: str = "") -> dict[str, Any]:
    """List Ready tasks that the caller's registered TeamFlow role is allowed to claim."""
    return _invoke("list_available_tasks", {}, teamflow_authorization)


@mcp.tool()
def get_task(record_id: str, teamflow_authorization: str = "") -> dict[str, Any]:
    """Read one complete TeamFlow task by its Lark record ID."""
    return _invoke("get_task", {"record_id": record_id}, teamflow_authorization)


@mcp.tool()
def claim_task(record_id: str, teamflow_authorization: str = "") -> dict[str, Any]:
    """Atomically claim one Ready TeamFlow task for the caller and move it to In Progress."""
    return _invoke("claim_task", {"record_id": record_id}, teamflow_authorization)


def run_mcp_server() -> None:
    mcp.run(transport="stdio")
