from __future__ import annotations

import json
from typing import Any

from .db import connect, now
from .lark_events import LarkEventContext
from .task_routing import WorkflowLoader


def active_delivery_execution(
    context: LarkEventContext,
    *,
    delivery_id: int,
    load_workflow: WorkflowLoader,
    turn_id: str | None = None,
) -> dict[str, Any] | None:
    with connect(context.db_path) as conn:
        return active_delivery_execution_in(
            conn,
            delivery_id=delivery_id,
            workflow_key=context.workflow_key,
            load_workflow=load_workflow,
            turn_id=turn_id,
        )


def active_delivery_execution_in(
    conn: Any,
    *,
    delivery_id: int,
    workflow_key: str,
    load_workflow: WorkflowLoader,
    turn_id: str | None = None,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT delivery.agent_id AS delivery_agent_id,
               delivery.session_id AS delivery_session_id,
               delivery.turn_id AS delivery_turn_id,
               event.record_id,
               state.status AS task_status,
               state.snapshot_json,
               state.updated_at AS task_updated_at,
               execution.agent_id AS execution_agent_id,
               execution.session_id AS execution_session_id,
               execution.turn_id AS execution_turn_id,
               execution.state AS execution_state,
               execution.updated_at AS execution_updated_at
        FROM task_event_deliveries AS delivery
        JOIN task_events AS event ON event.event_key = delivery.event_key
        JOIN task_executions AS execution
          ON execution.record_id = event.record_id
        LEFT JOIN lark_task_state AS state
          ON state.board_id = event.board_id
         AND state.table_id = event.table_id
         AND state.record_id = event.record_id
        WHERE delivery.id = ?
        """,
        (delivery_id,),
    ).fetchone()
    if row is None or row["execution_state"] != "active":
        return None
    expected_turn = turn_id if turn_id is not None else row["delivery_turn_id"]
    if (
        row["execution_agent_id"] != row["delivery_agent_id"]
        or row["execution_session_id"] != row["delivery_session_id"]
        or not expected_turn
        or row["execution_turn_id"] != expected_turn
    ):
        return None

    task = _json_object(row["snapshot_json"])
    definition = load_workflow(workflow_key)
    execution_states = set(
        definition.get("runtime_actions", {})
        .get("stop_execution", {})
        .get("states", [])
    )
    snapshot_confirms_execution = (
        row["task_status"] in execution_states
        and task.get("agent_id") == row["delivery_agent_id"]
    )
    snapshot_predates_execution = (
        not row["task_updated_at"]
        or str(row["task_updated_at"]) <= str(row["execution_updated_at"])
    )
    if not snapshot_confirms_execution and not snapshot_predates_execution:
        return None
    return {
        "record_id": str(row["record_id"]),
        "agent_id": str(row["execution_agent_id"]),
        "session_id": str(row["execution_session_id"]),
        "turn_id": str(row["execution_turn_id"]),
        "task": task,
    }


def rebind_active_delivery_execution(
    conn: Any,
    *,
    delivery_id: int,
    previous_turn_id: str,
    turn_id: str,
) -> None:
    row = conn.execute(
        """
        SELECT event.record_id, delivery.agent_id, delivery.session_id
        FROM task_event_deliveries AS delivery
        JOIN task_events AS event ON event.event_key = delivery.event_key
        WHERE delivery.id = ?
        """,
        (delivery_id,),
    ).fetchone()
    if row is None:
        raise ValueError("TeamFlow delivery no longer exists")
    cursor = conn.execute(
        """
        UPDATE task_executions
        SET turn_id = ?, updated_at = ?
        WHERE record_id = ? AND agent_id = ? AND session_id = ?
          AND turn_id = ? AND state = 'active'
        """,
        (
            turn_id,
            now(),
            row["record_id"],
            row["agent_id"],
            row["session_id"],
            previous_turn_id,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("TeamFlow active execution changed before continuation started")


def stop_active_delivery_execution(
    conn: Any,
    *,
    delivery_id: int,
    turn_id: str,
    reason: str,
) -> bool:
    row = conn.execute(
        """
        SELECT event.record_id, delivery.agent_id, delivery.session_id
        FROM task_event_deliveries AS delivery
        JOIN task_events AS event ON event.event_key = delivery.event_key
        WHERE delivery.id = ?
        """,
        (delivery_id,),
    ).fetchone()
    if row is None:
        return False
    timestamp = now()
    cursor = conn.execute(
        """
        UPDATE task_executions
        SET state = 'stopped', stop_status = 'continuation_exhausted',
            stop_reason = ?, updated_at = ?, stopped_at = ?
        WHERE record_id = ? AND agent_id = ? AND session_id = ?
          AND turn_id = ? AND state = 'active'
        """,
        (
            reason,
            timestamp,
            timestamp,
            row["record_id"],
            row["agent_id"],
            row["session_id"],
            turn_id,
        ),
    )
    return cursor.rowcount == 1


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
