from __future__ import annotations

from typing import Any

from .lark_events import LarkEventContext
from .task_delivery_planner import (
    prepare_agent_catchup_deliveries as _prepare_agent_catchup_deliveries,
    prepare_task_deliveries as _prepare_task_deliveries,
)
from .task_delivery_store import (
    cancel_reconciled_task_delivery,
    cancel_task_delivery,
    claim_task_deliveries as _claim_task_deliveries,
    defer_task_delivery_reconciliation,
    due_processing_task_deliveries,
    finish_task_delivery,
    has_task_deliveries_waiting_for_permission,
    mark_task_delivery_waiting_for_permission,
    mark_task_delivery_waiting_for_session,
    mark_task_delivery_turn_started,
    processing_task_delivery_sessions,
    processing_task_delivery_sessions_for_workspace,
    recover_task_deliveries,
    resume_task_deliveries_waiting_for_session,
    resume_task_deliveries_waiting_for_permission,
    refresh_task_delivery_prompt,
    task_delivery_record_id,
    task_delivery_sessions_waiting_for_owner,
    task_delivery_is_current as _task_delivery_is_current,
    task_delivery_turn_is_current as _task_delivery_turn_is_current,
)
from .task_routing import (
    render_task_prompt as _render_task_prompt,
    task_dispatch_target as _task_dispatch_target,
)
from .workflow import load_workflow_definition


def prepare_task_deliveries(context: LarkEventContext) -> dict[str, Any]:
    return _prepare_task_deliveries(
        context,
        load_workflow=load_workflow_definition,
    )


def prepare_agent_catchup_deliveries(context: LarkEventContext) -> int:
    return _prepare_agent_catchup_deliveries(
        context,
        load_workflow=load_workflow_definition,
    )


def claim_task_deliveries(
    context: LarkEventContext,
    *,
    limit: int = 100,
    exclude_session_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    return _claim_task_deliveries(
        context,
        load_workflow=load_workflow_definition,
        limit=limit,
        exclude_session_ids=exclude_session_ids,
    )


def task_delivery_is_current(
    context: LarkEventContext,
    *,
    delivery_id: int,
) -> bool:
    return _task_delivery_is_current(
        context,
        delivery_id=delivery_id,
        load_workflow=load_workflow_definition,
    )


def task_delivery_turn_is_current(
    context: LarkEventContext,
    *,
    turn_id: str,
    agent_id: str,
) -> bool | None:
    return _task_delivery_turn_is_current(
        context,
        turn_id=turn_id,
        agent_id=agent_id,
        load_workflow=load_workflow_definition,
    )


def render_task_prompt(
    context: LarkEventContext,
    *,
    event_type: str,
    event_key: str,
    workflow_key: str,
    role_name: str,
    task: dict[str, Any],
) -> str:
    return _render_task_prompt(
        context,
        event_type=event_type,
        event_key=event_key,
        workflow_key=workflow_key,
        role_name=role_name,
        task=task,
        load_workflow=load_workflow_definition,
    )


def task_dispatch_target(
    workflow_key: str,
    task: dict[str, Any],
) -> str | None:
    return _task_dispatch_target(
        workflow_key,
        task,
        load_workflow=load_workflow_definition,
    )


__all__ = [
    "cancel_reconciled_task_delivery",
    "cancel_task_delivery",
    "claim_task_deliveries",
    "defer_task_delivery_reconciliation",
    "due_processing_task_deliveries",
    "finish_task_delivery",
    "has_task_deliveries_waiting_for_permission",
    "mark_task_delivery_waiting_for_permission",
    "mark_task_delivery_waiting_for_session",
    "mark_task_delivery_turn_started",
    "prepare_agent_catchup_deliveries",
    "prepare_task_deliveries",
    "processing_task_delivery_sessions",
    "processing_task_delivery_sessions_for_workspace",
    "recover_task_deliveries",
    "resume_task_deliveries_waiting_for_permission",
    "refresh_task_delivery_prompt",
    "render_task_prompt",
    "resume_task_deliveries_waiting_for_session",
    "task_delivery_is_current",
    "task_delivery_record_id",
    "task_delivery_sessions_waiting_for_owner",
    "task_delivery_turn_is_current",
    "task_dispatch_target",
]
