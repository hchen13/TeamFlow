from __future__ import annotations

import json
import time
import uuid
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .daemon import _daemon_request


mcp = FastMCP("TeamFlow")
CODEX_TURN_METADATA_KEY = "x-codex-turn-metadata"
DAEMON_RECOVERY_TIMEOUT = 60
DAEMON_RETRY_INTERVAL = 0.25


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
    invocation_id = str(uuid.uuid4())
    deadline = time.monotonic() + DAEMON_RECOVERY_TIMEOUT
    while True:
        try:
            authorization = _daemon_request(
                {
                    "action": "authorize_tool",
                    "invocation_id": invocation_id,
                    "session_id": caller["session_id"],
                    "turn_id": caller["turn_id"],
                    "tool_name": f"mcp__teamflow__{tool_name}",
                    "tool_input": arguments,
                },
                timeout=30,
            )
        except Exception as error:
            if _daemon_unavailable(error) and time.monotonic() < deadline:
                time.sleep(DAEMON_RETRY_INTERVAL)
                continue
            raise
        try:
            return _daemon_request(
                {
                    "action": "invoke_tool",
                    "invocation_id": invocation_id,
                    "grant": authorization["grant"],
                    "tool_name": tool_name,
                    "arguments": arguments,
                },
                timeout=30,
            )
        except Exception as error:
            if _daemon_unavailable(error) and time.monotonic() < deadline:
                time.sleep(DAEMON_RETRY_INTERVAL)
                continue
            raise


def _daemon_unavailable(error: Exception) -> bool:
    if isinstance(error, (OSError, TimeoutError)):
        return True
    message = str(error).lower()
    return any(fragment in message for fragment in (
        "closed the connection without a response",
        "authorization is missing or expired",
        "connection refused",
        "no such file or directory",
    ))


@mcp.tool()
def get_assignment(context: Context) -> dict[str, Any]:
    """Return the caller's trusted TeamFlow workspace, workflow, role, and agent assignment."""
    return _invoke("get_assignment", {}, context)


@mcp.tool()
def list_available_tasks(context: Context) -> dict[str, Any]:
    """List tasks in workflow-defined states that the caller is currently allowed to claim."""
    return _invoke("list_available_tasks", {}, context)


@mcp.tool()
def get_task(record_id: str, context: Context) -> dict[str, Any]:
    """Read one complete TeamFlow task by its Lark record ID."""
    return _invoke("get_task", {"record_id": record_id}, context)


@mcp.tool()
def claim_task(record_id: str, context: Context) -> dict[str, Any]:
    """Claim one task through the caller's workflow rules. The daemon verifies role, state, and executor identity."""
    return _invoke("claim_task", {"record_id": record_id}, context)


@mcp.tool()
def create_task(
    title: str,
    context: Context,
    task_type: str = "",
    priority: str = "",
    role: str = "",
    description: str = "",
    additional_context: str = "",
    acceptance_criteria: str = "",
    dependencies: str = "",
) -> dict[str, Any]:
    """Create a workflow task. The workflow decides who may create it and its required initial state."""
    return _invoke("create_task", {
        "title": title,
        "task_type": task_type,
        "priority": priority,
        "role": role,
        "description": description,
        "context": additional_context,
        "acceptance_criteria": acceptance_criteria,
        "dependencies": dependencies,
    }, context)


@mcp.tool()
def update_task(
    record_id: str,
    fields: dict[str, Any],
    context: Context,
) -> dict[str, Any]:
    """Update ordinary task fields allowed for the caller, current state, and workflow. Lifecycle fields are rejected."""
    return _invoke("update_task", {"record_id": record_id, "fields": fields}, context)


@mcp.tool()
def route_task(record_id: str, role: str, context: Context) -> dict[str, Any]:
    """Route a prepared, unblocked, or recoverable task to a role's claimable queue according to the workflow."""
    return _invoke("route_task", {"record_id": record_id, "role": role}, context)


@mcp.tool()
def submit_task(
    record_id: str,
    outcome: str,
    result_evidence: str,
    context: Context,
    progress: str = "",
    next_action: str = "",
) -> dict[str, Any]:
    """Submit claimed work with a workflow-defined outcome and evidence for its next review stage."""
    return _invoke("submit_task", {
        "record_id": record_id,
        "outcome": outcome,
        "result_evidence": result_evidence,
        "progress": progress,
        "next_action": next_action,
    }, context)


@mcp.tool()
def block_task(
    record_id: str,
    waiting_on: str,
    blocked_reason: str,
    next_action: str,
    context: Context,
    progress: str = "",
) -> dict[str, Any]:
    """Block a task with an explicit reason, waiting target, and concrete next action permitted by the workflow."""
    return _invoke("block_task", {
        "record_id": record_id,
        "waiting_on": waiting_on,
        "blocked_reason": blocked_reason,
        "next_action": next_action,
        "progress": progress,
    }, context)


@mcp.tool()
def review_task(
    record_id: str,
    decision: str,
    result_evidence: str,
    context: Context,
    role: str = "",
    next_action: str = "",
) -> dict[str, Any]:
    """Apply a workflow-defined review decision such as approve, rework, or send_to_qa."""
    return _invoke("review_task", {
        "record_id": record_id,
        "decision": decision,
        "result_evidence": result_evidence,
        "role": role,
        "next_action": next_action,
    }, context)


@mcp.tool()
def stop_task_execution(
    record_id: str,
    reason: str,
    context: Context,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Stop the task's current executor turn after workflow authorization and explicit confirmation. This does not cancel the task."""
    return _invoke("stop_task_execution", {
        "record_id": record_id,
        "reason": reason,
        "confirmed": confirmed,
    }, context)


@mcp.tool()
def cancel_task(
    record_id: str,
    result_evidence: str,
    context: Context,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Cancel a task only after an explicit confirmation and the workflow's cancellation preconditions are satisfied."""
    return _invoke("cancel_task", {
        "record_id": record_id,
        "result_evidence": result_evidence,
        "confirmed": confirmed,
    }, context)


def run_mcp_server() -> None:
    mcp.run(transport="stdio")
