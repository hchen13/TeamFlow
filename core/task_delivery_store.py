from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import resolve_workspace_paths
from .db import connect, now
from .lark_events import LarkEventContext
from .task_routing import (
    WorkflowLoader,
    current_dispatch,
    dispatch_event_state,
    dispatch_states,
    render_task_prompt,
    target_role,
)


def recover_task_deliveries(context: LarkEventContext) -> None:
    with connect(context.db_path) as conn:
        conn.execute(
            """
            UPDATE task_event_deliveries
            SET status = 'pending', started_at = NULL, next_attempt_at = NULL
            WHERE status = 'processing'
              AND turn_id IS NULL
              AND client_message_id IS NULL
            """
        )
        conn.execute(
            """
            UPDATE task_event_deliveries
            SET next_attempt_at = COALESCE(next_attempt_at, ?)
            WHERE status = 'processing'
              AND (turn_id IS NOT NULL OR client_message_id IS NOT NULL)
            """,
            (now(),),
        )


def claim_task_deliveries(
    context: LarkEventContext,
    *,
    load_workflow: WorkflowLoader,
    limit: int = 100,
    exclude_session_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    claimed = []
    reserved_sessions = set(exclude_session_ids or ())
    timestamp = now()
    states = dispatch_states(
        context.workflow_key,
        load_workflow=load_workflow,
    )
    with connect(context.db_path) as conn:
        reserved_sessions.update(
            str(row["session_id"])
            for row in conn.execute(
                """
                SELECT DISTINCT session_id
                FROM task_event_deliveries
                WHERE status = 'processing'
                """
            )
        )
        rows = conn.execute(
            """
            SELECT delivery.*, event.board_id, event.table_id,
                   event.workflow_id, event.event_type, event.record_id,
                   event.source_event_id, event.after_json,
                   event.before_json, agent.display_name, agent.role_key,
                   COALESCE(
                     roles.display_name_zh,
                     roles.display_name
                   ) AS role_name,
                   state.status AS current_task_status,
                   state.snapshot_json AS current_snapshot_json
            FROM task_event_deliveries AS delivery
            JOIN task_events AS event
              ON event.event_key = delivery.event_key
            JOIN agents AS agent ON agent.id = delivery.agent_id
            JOIN roles ON roles.id = agent.role_id
            LEFT JOIN lark_task_state AS state
              ON state.board_id = event.board_id
             AND state.table_id = event.table_id
             AND state.record_id = event.record_id
            WHERE delivery.status IN ('pending', 'retry')
              AND (
                delivery.next_attempt_at IS NULL
                OR delivery.next_attempt_at <= ?
              )
            ORDER BY delivery.created_at,
                     delivery.event_key,
                     delivery.agent_id
            LIMIT ?
            """,
            (timestamp, max(limit * 4, limit)),
        ).fetchall()
        for row in rows:
            current = current_dispatch(conn, row, states)
            if not current or current["event"]["event_key"] != row["event_key"]:
                expected_status = (
                    (dispatch_event_state(states, row) or {}).get("key")
                    or "an actionable state"
                )
                reason = (
                    f"task is no longer {expected_status}"
                    if not current
                    or current["state"]["key"] != expected_status
                    else (
                        f"task has a newer {expected_status} "
                        "dispatch event"
                    )
                )
                conn.execute(
                    """
                    UPDATE task_event_deliveries
                    SET status = 'canceled',
                        last_error = ?,
                        completed_at = ?
                    WHERE event_key = ? AND agent_id = ?
                      AND status IN ('pending', 'retry')
                    """,
                    (
                        reason,
                        timestamp,
                        row["event_key"],
                        row["agent_id"],
                    ),
                )
                continue
            task = current["task"]
            target = target_role(
                context.workflow_key,
                current["state"]["key"],
                task,
                load_workflow=load_workflow,
            )
            if target != row["role_key"]:
                conn.execute(
                    """
                    UPDATE task_event_deliveries
                    SET status = 'canceled',
                        last_error = ?,
                        completed_at = ?
                    WHERE event_key = ? AND agent_id = ?
                      AND status IN ('pending', 'retry')
                    """,
                    (
                        f"task now targets role {target or 'none'}",
                        timestamp,
                        row["event_key"],
                        row["agent_id"],
                    ),
                )
                continue
            session_id = str(row["session_id"])
            if session_id in reserved_sessions:
                continue
            prompt = render_task_prompt(
                context,
                event_type=str(row["event_type"]),
                event_key=str(row["event_key"]),
                workflow_key=context.workflow_key,
                role_name=str(row["role_name"] or row["role_key"]),
                task=task,
                load_workflow=load_workflow,
            )
            client_message_id = str(row["client_message_id"] or "")
            if not client_message_id or row["turn_id"] is not None:
                client_message_id = str(uuid.uuid4())
            cursor = conn.execute(
                """
                UPDATE task_event_deliveries
                SET status = 'processing',
                    attempts = attempts + 1,
                    turn_id = NULL,
                    turn_status = NULL,
                    last_error = NULL,
                    next_attempt_at = NULL,
                    started_at = ?,
                    completed_at = NULL,
                    prompt = ?,
                    client_message_id = ?
                WHERE event_key = ? AND agent_id = ?
                  AND status IN ('pending', 'retry')
                  AND (
                    next_attempt_at IS NULL
                    OR next_attempt_at <= ?
                  )
                """,
                (
                    timestamp,
                    prompt,
                    client_message_id,
                    row["event_key"],
                    row["agent_id"],
                    timestamp,
                ),
            )
            if cursor.rowcount == 1:
                item = dict(row)
                item["attempts"] = int(item["attempts"]) + 1
                item["prompt"] = prompt
                item["client_message_id"] = client_message_id
                item["after_json"] = json.dumps(
                    task,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                claimed.append(item)
                reserved_sessions.add(session_id)
                if len(claimed) >= limit:
                    break
    return claimed


def finish_task_delivery(
    context: LarkEventContext,
    *,
    delivery_id: int,
    result: dict[str, Any] | None = None,
    error: Exception | None = None,
    retry: bool = False,
    reset_client_message_id: bool = False,
) -> None:
    timestamp = now()
    if retry:
        status = "retry"
        next_attempt_at = _retry_at(context, delivery_id)
        completed_at = None
        delivered_at = None
    else:
        status = (
            "completed"
            if error is None and (result or {}).get("ok", True)
            else "failed"
        )
        next_attempt_at = None
        completed_at = timestamp
        delivered_at = timestamp if status == "completed" else None
    with connect(context.db_path) as conn:
        conn.execute(
            """
            UPDATE task_event_deliveries
            SET status = ?,
                turn_status = COALESCE(?, turn_status),
                last_error = ?,
                next_attempt_at = ?,
                completed_at = ?,
                delivered_at = ?,
                client_message_id = CASE WHEN ? THEN NULL ELSE client_message_id END
            WHERE id = ?
            """,
            (
                status,
                (result or {}).get("status"),
                str(error) if error else (result or {}).get("error"),
                next_attempt_at,
                completed_at,
                delivered_at,
                reset_client_message_id,
                delivery_id,
            ),
        )


def mark_task_delivery_waiting_for_permission(
    context: LarkEventContext,
    *,
    delivery_id: int,
    error: Exception,
) -> None:
    with connect(context.db_path) as conn:
        conn.execute(
            """
            UPDATE task_event_deliveries
            SET status = 'waiting_permission',
                last_error = ?,
                next_attempt_at = NULL,
                started_at = NULL,
                completed_at = NULL,
                delivered_at = NULL
            WHERE id = ?
              AND status = 'processing'
              AND turn_id IS NULL
            """,
            (str(error), delivery_id),
        )


def mark_task_delivery_waiting_for_session(
    context: LarkEventContext,
    *,
    delivery_id: int,
    error: Exception,
) -> None:
    with connect(context.db_path) as conn:
        conn.execute(
            """
            UPDATE task_event_deliveries
            SET status = 'waiting_session',
                last_error = ?,
                next_attempt_at = NULL,
                started_at = NULL,
                completed_at = NULL,
                delivered_at = NULL
            WHERE id = ?
              AND status = 'processing'
              AND turn_id IS NULL
            """,
            (
                str(error),
                delivery_id,
            ),
        )


def task_delivery_sessions_waiting_for_owner(
    context: LarkEventContext,
) -> list[str]:
    with connect(context.db_path) as conn:
        return [
            str(row["session_id"])
            for row in conn.execute(
                """
                SELECT DISTINCT session_id
                FROM task_event_deliveries
                WHERE status = 'waiting_session'
                ORDER BY session_id
                """
            )
        ]


def resume_task_deliveries_waiting_for_session(
    context: LarkEventContext,
    *,
    session_id: str,
) -> None:
    with connect(context.db_path) as conn:
        conn.execute(
            """
            UPDATE task_event_deliveries
            SET status = 'retry', next_attempt_at = NULL, last_error = NULL
            WHERE status = 'waiting_session' AND session_id = ?
            """,
            (session_id,),
        )


def has_task_deliveries_waiting_for_permission(
    context: LarkEventContext,
) -> bool:
    with connect(context.db_path) as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM task_event_deliveries
            WHERE status = 'waiting_permission'
            LIMIT 1
            """
        ).fetchone()
    return row is not None


def resume_task_deliveries_waiting_for_permission(
    context: LarkEventContext,
) -> int:
    with connect(context.db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE task_event_deliveries
            SET status = 'retry',
                last_error = NULL,
                next_attempt_at = NULL
            WHERE status = 'waiting_permission'
            """
        )
    return int(cursor.rowcount)


def mark_task_delivery_turn_started(
    context: LarkEventContext,
    *,
    delivery_id: int,
    turn_id: str,
) -> None:
    with connect(context.db_path) as conn:
        conn.execute(
            """
            UPDATE task_event_deliveries
            SET turn_id = ?,
                turn_status = 'inProgress',
                next_attempt_at = ?
            WHERE id = ? AND status = 'processing'
            """,
            (
                turn_id,
                (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(),
                delivery_id,
            ),
        )


def due_processing_task_deliveries(
    context: LarkEventContext,
) -> list[dict[str, Any]]:
    with connect(context.db_path) as conn:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT delivery.*, event.record_id,
                       event.source_event_id,
                       COALESCE(
                         state.snapshot_json,
                         event.after_json
                       ) AS after_json,
                       event.before_json,
                       agent.display_name,
                       agent.role_key
                FROM task_event_deliveries AS delivery
                JOIN task_events AS event
                  ON event.event_key = delivery.event_key
                LEFT JOIN agents AS agent
                  ON agent.id = delivery.agent_id
                LEFT JOIN lark_task_state AS state
                  ON state.board_id = event.board_id
                 AND state.table_id = event.table_id
                 AND state.record_id = event.record_id
                WHERE delivery.status = 'processing'
                  AND (
                    delivery.turn_id IS NOT NULL
                    OR delivery.client_message_id IS NOT NULL
                  )
                  AND (
                    delivery.next_attempt_at IS NULL
                    OR delivery.next_attempt_at <= ?
                  )
                ORDER BY delivery.started_at,
                         delivery.event_key,
                         delivery.agent_id
                """,
                (now(),),
            )
        ]


def processing_task_delivery_sessions(
    context: LarkEventContext,
) -> set[str]:
    return processing_task_delivery_sessions_for_workspace(
        context.workspace_root,
    )


def processing_task_delivery_sessions_for_workspace(
    workspace_root: str,
) -> set[str]:
    db_path = resolve_workspace_paths(workspace_root).db_path
    if not db_path.exists():
        return set()
    with connect(db_path) as conn:
        return {
            str(row["session_id"])
            for row in conn.execute(
                """
                SELECT DISTINCT session_id
                FROM task_event_deliveries
                WHERE status = 'processing'
                """
            )
            if row["session_id"]
        }


def task_delivery_record_id(
    workspace_root: str,
    *,
    turn_id: str,
    agent_id: str,
) -> str | None:
    db_path = resolve_workspace_paths(workspace_root).db_path
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT event.record_id
            FROM task_event_deliveries AS delivery
            JOIN task_events AS event
              ON event.event_key = delivery.event_key
            WHERE delivery.turn_id = ?
              AND delivery.agent_id = ?
            ORDER BY delivery.started_at DESC, delivery.id DESC
            LIMIT 1
            """,
            (turn_id, agent_id),
        ).fetchone()
    return str(row["record_id"]) if row else None


def task_delivery_turn_is_current(
    context: LarkEventContext,
    *,
    turn_id: str,
    agent_id: str,
    load_workflow: WorkflowLoader,
) -> bool | None:
    with connect(context.db_path) as conn:
        row = conn.execute(
            """
            SELECT delivery.id, delivery.status, event.record_id
            FROM task_event_deliveries AS delivery
            JOIN task_events AS event
              ON event.event_key = delivery.event_key
            WHERE delivery.turn_id = ?
              AND delivery.agent_id = ?
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """,
            (turn_id, agent_id),
        ).fetchone()
        if row is None:
            return None
        if row["status"] != "processing":
            return False
        execution = conn.execute(
            """
            SELECT 1
            FROM task_executions
            WHERE record_id = ?
              AND agent_id = ?
              AND turn_id = ?
              AND state = 'active'
            """,
            (row["record_id"], agent_id, turn_id),
        ).fetchone()
    if execution is not None:
        return True
    return task_delivery_is_current(
        context,
        delivery_id=int(row["id"]),
        load_workflow=load_workflow,
    )


def defer_task_delivery_reconciliation(
    context: LarkEventContext,
    *,
    delivery_id: int,
    error: Exception | None = None,
) -> None:
    with connect(context.db_path) as conn:
        conn.execute(
            """
            UPDATE task_event_deliveries
            SET last_error = ?, next_attempt_at = ?
            WHERE id = ? AND status = 'processing'
            """,
            (
                str(error) if error else None,
                (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(),
                delivery_id,
            ),
        )


def cancel_task_delivery(
    context: LarkEventContext,
    *,
    delivery_id: int,
    reason: str,
) -> None:
    with connect(context.db_path) as conn:
        conn.execute(
            """
            UPDATE task_event_deliveries
            SET status = 'canceled',
                last_error = ?,
                completed_at = ?
            WHERE id = ?
              AND status = 'processing'
              AND turn_id IS NULL
            """,
            (reason, now(), delivery_id),
        )


def cancel_reconciled_task_delivery(
    context: LarkEventContext,
    *,
    delivery_id: int,
    reason: str,
    turn_status: str = "missing",
) -> None:
    with connect(context.db_path) as conn:
        conn.execute(
            """
            UPDATE task_event_deliveries
            SET status = 'canceled',
                turn_status = ?,
                last_error = ?,
                next_attempt_at = NULL,
                completed_at = ?
            WHERE id = ? AND status = 'processing'
            """,
            (turn_status, reason, now(), delivery_id),
        )


def refresh_task_delivery_prompt(
    context: LarkEventContext,
    *,
    delivery_id: int,
    prompt: str,
) -> None:
    with connect(context.db_path) as conn:
        conn.execute(
            """
            UPDATE task_event_deliveries
            SET prompt = ?
            WHERE id = ?
              AND status = 'processing'
              AND turn_id IS NULL
            """,
            (prompt, delivery_id),
        )


def task_delivery_is_current(
    context: LarkEventContext,
    *,
    delivery_id: int,
    load_workflow: WorkflowLoader,
) -> bool:
    states = dispatch_states(
        context.workflow_key,
        load_workflow=load_workflow,
    )
    with connect(context.db_path) as conn:
        row = conn.execute(
            """
            SELECT event.*
            FROM task_event_deliveries AS delivery
            JOIN task_events AS event
              ON event.event_key = delivery.event_key
            WHERE delivery.id = ?
            """,
            (delivery_id,),
        ).fetchone()
        if row is None:
            return False
        current = current_dispatch(conn, row, states)
        return bool(
            current
            and current["event"]["event_key"] == row["event_key"]
        )


def _retry_at(context: LarkEventContext, delivery_id: int) -> str:
    with connect(context.db_path) as conn:
        row = conn.execute(
            "SELECT attempts FROM task_event_deliveries WHERE id = ?",
            (delivery_id,),
        ).fetchone()
    attempts = int(row["attempts"]) if row else 1
    delay = min(60, 2 ** min(attempts, 6))
    return (
        datetime.now(timezone.utc) + timedelta(seconds=delay)
    ).isoformat()
