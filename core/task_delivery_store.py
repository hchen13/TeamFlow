from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import resolve_workspace_paths
from .db import connect, now
from .lark_events import LarkEventContext
from .task_delivery_execution import (
    active_delivery_execution,
    active_delivery_execution_in,
    rebind_active_delivery_execution,
    stop_active_delivery_execution,
)
from .task_routing import (
    WorkflowLoader,
    current_dispatch,
    dispatch_event_state,
    dispatch_states,
    render_task_continuation_prompt,
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


def recover_retryable_failed_task_deliveries(
    context: LarkEventContext,
    *,
    max_turn_attempts: int,
    load_workflow: WorkflowLoader,
) -> int:
    states = dispatch_states(
        context.workflow_key,
        load_workflow=load_workflow,
    )
    recovered = 0
    with connect(context.db_path) as conn:
        rows = conn.execute(
            """
            SELECT delivery.id, delivery.event_key, delivery.turn_id,
                   delivery.last_error
            FROM task_event_deliveries AS delivery
            WHERE delivery.status = 'failed'
              AND delivery.turn_status IN (
                'interrupted', 'cancelled', 'canceled', 'unconfirmed'
              )
            ORDER BY delivery.completed_at, delivery.id
            """,
        ).fetchall()
        for row in rows:
            event = conn.execute(
                "SELECT * FROM task_events WHERE event_key = ?",
                (row["event_key"],),
            ).fetchone()
            current = current_dispatch(conn, event, states) if event else None
            if not current or current["event"]["event_key"] != row["event_key"]:
                reason = (
                    f"{row['last_error'] or 'delivery failed'}; "
                    "task no longer needs this delivery"
                )
                if row["turn_id"]:
                    stop_active_delivery_execution(
                        conn,
                        delivery_id=int(row["id"]),
                        turn_id=str(row["turn_id"]),
                        reason=reason,
                    )
                conn.execute(
                    """
                    UPDATE task_event_deliveries
                    SET status = 'canceled', last_error = ?,
                        next_attempt_at = NULL,
                        completed_at = COALESCE(completed_at, ?)
                    WHERE id = ? AND status = 'failed'
                    """,
                    (reason, now(), row["id"]),
                )
                continue
            turn_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM task_delivery_turns
                WHERE delivery_id = ?
                """,
                (row["id"],),
            ).fetchone()[0]
            if int(turn_count) >= max_turn_attempts:
                continue
            if active_delivery_execution_in(
                conn,
                delivery_id=int(row["id"]),
                workflow_key=context.workflow_key,
                load_workflow=load_workflow,
                turn_id=str(row["turn_id"] or "") or None,
            ) is not None:
                continue
            cursor = conn.execute(
                """
                UPDATE task_event_deliveries
                SET status = 'retry',
                    next_attempt_at = NULL,
                    completed_at = NULL,
                    delivered_at = NULL
                WHERE id = ? AND status = 'failed'
                """,
                (row["id"],),
            )
            recovered += int(cursor.rowcount)
    return recovered


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
            continuation = (
                active_delivery_execution_in(
                    conn,
                    delivery_id=int(row["id"]),
                    workflow_key=context.workflow_key,
                    load_workflow=load_workflow,
                    turn_id=str(row["turn_id"] or "") or None,
                )
                if row["status"] == "retry" and row["turn_id"]
                else None
            )
            if continuation is None and (
                not current or current["event"]["event_key"] != row["event_key"]
            ):
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
            task = continuation["task"] if continuation else current["task"]
            target = (
                str(row["role_key"])
                if continuation
                else target_role(
                    context.workflow_key,
                    current["state"]["key"],
                    task,
                    load_workflow=load_workflow,
                )
            )
            if not continuation and target != row["role_key"]:
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
            prompt = (
                render_task_continuation_prompt(
                    context,
                    workflow_key=context.workflow_key,
                    role_name=str(row["role_name"] or row["role_key"]),
                    task=task,
                )
                if continuation
                else render_task_prompt(
                    context,
                    event_type=str(row["event_type"]),
                    event_key=str(row["event_key"]),
                    workflow_key=context.workflow_key,
                    role_name=str(row["role_name"] or row["role_key"]),
                    task=task,
                    load_workflow=load_workflow,
                )
            )
            client_message_id = str(row["client_message_id"] or "")
            if not client_message_id or row["turn_id"] is not None:
                client_message_id = str(uuid.uuid4())
            cursor = conn.execute(
                """
                UPDATE task_event_deliveries
                SET status = 'processing',
                    attempts = attempts + 1,
                    turn_id = CASE WHEN ? THEN turn_id ELSE NULL END,
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
                    int(continuation is not None),
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
                item["continuation_turn_id"] = (
                    str(continuation["turn_id"]) if continuation else None
                )
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


def fail_claimed_task_delivery(
    context: LarkEventContext,
    *,
    delivery_id: int,
    turn_id: str,
    turn_status: str,
    reason: str,
) -> bool:
    timestamp = now()
    with connect(context.db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE task_event_deliveries
            SET status = 'failed', turn_status = ?, last_error = ?,
                next_attempt_at = NULL, completed_at = ?, delivered_at = NULL
            WHERE id = ? AND status = 'processing' AND turn_id = ?
              AND EXISTS (
                SELECT 1
                FROM task_events AS event
                JOIN task_executions AS execution
                  ON execution.record_id = event.record_id
                WHERE event.event_key = task_event_deliveries.event_key
                  AND execution.agent_id = task_event_deliveries.agent_id
                  AND execution.session_id = task_event_deliveries.session_id
                  AND execution.turn_id = task_event_deliveries.turn_id
                  AND execution.state = 'active'
              )
            """,
            (turn_status, reason, timestamp, delivery_id, turn_id),
        )
        if cursor.rowcount != 1:
            return False
        if not stop_active_delivery_execution(
            conn,
            delivery_id=delivery_id,
            turn_id=turn_id,
            reason=reason,
        ):
            raise RuntimeError("TeamFlow active execution changed during finalization")
    return True


def mark_task_delivery_waiting_for_permission(
    context: LarkEventContext,
    *,
    delivery_id: int,
    error: Exception,
    continuation: bool = False,
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
              AND (turn_id IS NULL OR ?)
            """,
            (str(error), delivery_id, int(continuation)),
        )


def mark_task_delivery_waiting_for_session(
    context: LarkEventContext,
    *,
    delivery_id: int,
    error: Exception,
    continuation: bool = False,
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
              AND (turn_id IS NULL OR ?)
            """,
            (
                str(error),
                delivery_id,
                int(continuation),
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
    previous_turn_id: str | None = None,
    require_execution_rebind: bool = False,
) -> str:
    timestamp = now()
    with connect(context.db_path) as conn:
        if require_execution_rebind:
            if not previous_turn_id:
                raise ValueError("TeamFlow continuation needs its previous turn ID")
            rebind_active_delivery_execution(
                conn,
                delivery_id=delivery_id,
                previous_turn_id=previous_turn_id,
                turn_id=turn_id,
            )
        cursor = conn.execute(
            """
            UPDATE task_event_deliveries
            SET turn_id = ?,
                turn_status = 'inProgress',
                started_at = ?,
                next_attempt_at = ?
            WHERE id = ? AND status = 'processing'
              AND (? IS NULL OR turn_id = ?)
            """,
            (
                turn_id,
                timestamp,
                (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(),
                delivery_id,
                previous_turn_id,
                previous_turn_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("TeamFlow delivery changed before its turn started")
        conn.execute(
            """
            INSERT OR IGNORE INTO task_delivery_turns (
              delivery_id, turn_id, created_at
            ) VALUES (?, ?, ?)
            """,
            (delivery_id, turn_id, timestamp),
        )
    return timestamp


def _mark_task_delivery_queue_state(
    context: LarkEventContext,
    *,
    delivery_id: int,
    state: str,
    previous_turn_id: str | None = None,
) -> None:
    with connect(context.db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE task_event_deliveries
            SET turn_status = ?,
                next_attempt_at = ?
            WHERE id = ? AND status = 'processing'
              AND (
                (? IS NULL AND turn_id IS NULL)
                OR (? IS NOT NULL AND turn_id = ?)
              )
            """,
            (
                state,
                (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(),
                delivery_id,
                previous_turn_id,
                previous_turn_id,
                previous_turn_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("TeamFlow delivery changed before queue acceptance")


def mark_task_delivery_queueing(
    context: LarkEventContext,
    *,
    delivery_id: int,
    previous_turn_id: str | None = None,
) -> None:
    _mark_task_delivery_queue_state(
        context,
        delivery_id=delivery_id,
        state="queueing",
        previous_turn_id=previous_turn_id,
    )


def mark_task_delivery_queued(
    context: LarkEventContext,
    *,
    delivery_id: int,
    previous_turn_id: str | None = None,
) -> None:
    _mark_task_delivery_queue_state(
        context,
        delivery_id=delivery_id,
        state="queued",
        previous_turn_id=previous_turn_id,
    )


def clear_task_delivery_queueing(
    context: LarkEventContext,
    *,
    delivery_id: int,
    previous_turn_id: str | None = None,
) -> None:
    with connect(context.db_path) as conn:
        conn.execute(
            """
            UPDATE task_event_deliveries
            SET turn_status = NULL
            WHERE id = ? AND status = 'processing'
              AND turn_status = 'queueing'
              AND (
                (? IS NULL AND turn_id IS NULL)
                OR (? IS NOT NULL AND turn_id = ?)
              )
            """,
            (
                delivery_id,
                previous_turn_id,
                previous_turn_id,
                previous_turn_id,
            ),
        )


def task_delivery_turn_count(
    context: LarkEventContext,
    *,
    delivery_id: int,
) -> int:
    with connect(context.db_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM task_delivery_turns
            WHERE delivery_id = ?
            """,
            (delivery_id,),
        ).fetchone()
    return int(row["count"] if row else 0)


def acknowledge_task_delivery_turn(
    workspace_root: str,
    *,
    turn_id: str,
    agent_id: str,
    record_id: str,
) -> bool:
    if not turn_id or not agent_id or not record_id:
        return False
    db_path = resolve_workspace_paths(workspace_root).db_path
    timestamp = now()
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE task_delivery_turns
            SET acknowledged_at = COALESCE(acknowledged_at, ?)
            WHERE turn_id = ?
              AND delivery_id = (
                SELECT delivery.id
                FROM task_event_deliveries AS delivery
                JOIN task_events AS event
                  ON event.event_key = delivery.event_key
                WHERE delivery.status = 'processing'
                  AND delivery.turn_id = ?
                  AND delivery.agent_id = ?
                  AND event.record_id = ?
                ORDER BY delivery.id DESC
                LIMIT 1
              )
            """,
            (
                timestamp,
                turn_id,
                turn_id,
                agent_id,
                record_id,
            ),
        )
    return cursor.rowcount == 1


def task_delivery_turn_acknowledged(
    context: LarkEventContext,
    *,
    delivery_id: int,
    turn_id: str,
) -> bool:
    if not turn_id:
        return False
    with connect(context.db_path) as conn:
        row = conn.execute(
            """
            SELECT acknowledged_at
            FROM task_delivery_turns
            WHERE delivery_id = ? AND turn_id = ?
            """,
            (delivery_id, turn_id),
        ).fetchone()
    return bool(row and row["acknowledged_at"])


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


def processing_task_delivery(
    context: LarkEventContext,
    *,
    delivery_id: int,
) -> dict[str, Any] | None:
    with connect(context.db_path) as conn:
        row = conn.execute(
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
            WHERE delivery.id = ? AND delivery.status = 'processing'
            """,
            (delivery_id,),
        ).fetchone()
    return dict(row) if row else None


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
            LEFT JOIN task_delivery_turns AS delivery_turn
              ON delivery_turn.delivery_id = delivery.id
             AND delivery_turn.turn_id = ?
            WHERE (delivery.turn_id = ? OR delivery_turn.turn_id IS NOT NULL)
              AND delivery.agent_id = ?
            ORDER BY delivery.started_at DESC, delivery.id DESC
            LIMIT 1
            """,
            (turn_id, turn_id, agent_id),
        ).fetchone()
    return str(row["record_id"]) if row else None


def task_delivery_turn_is_current(
    context: LarkEventContext,
    *,
    turn_id: str,
    agent_id: str,
    load_workflow: WorkflowLoader,
    session_id: str | None = None,
    turn_id_for_client_message: Callable[[str, str], str | None] | None = None,
) -> bool | None:
    with connect(context.db_path) as conn:
        row = _task_delivery_for_turn_in(
            conn,
            turn_id=turn_id,
            agent_id=agent_id,
            session_id=session_id,
        )
        pending = None
        if row is None and session_id and turn_id_for_client_message:
            pending = conn.execute(
                """
                SELECT id, turn_id, client_message_id
                FROM task_event_deliveries
                WHERE status = 'processing'
                  AND turn_status IN ('queueing', 'queued')
                  AND agent_id = ? AND session_id = ?
                  AND client_message_id IS NOT NULL
                ORDER BY started_at DESC, id DESC
                LIMIT 1
                """,
                (agent_id, session_id),
            ).fetchone()
        if row is None and pending is None:
            return None
    if pending is not None:
        client_message_id = str(pending["client_message_id"] or "")
        previous_turn_id = str(pending["turn_id"] or "") or None
        pending_is_current = task_delivery_is_current(
            context,
            delivery_id=int(pending["id"]),
            load_workflow=load_workflow,
        )
        if not pending_is_current and previous_turn_id:
            pending_is_current = task_delivery_has_active_execution(
                context,
                delivery_id=int(pending["id"]),
                load_workflow=load_workflow,
                turn_id=previous_turn_id,
            )
        if not pending_is_current:
            return False
        if turn_id_for_client_message(session_id, client_message_id) != turn_id:
            return None
        require_execution_rebind = bool(
            previous_turn_id
            and task_delivery_has_active_execution(
                context,
                delivery_id=int(pending["id"]),
                load_workflow=load_workflow,
                turn_id=previous_turn_id,
            )
        )
        try:
            mark_task_delivery_turn_started(
                context,
                delivery_id=int(pending["id"]),
                turn_id=turn_id,
                previous_turn_id=(
                    previous_turn_id if require_execution_rebind else None
                ),
                require_execution_rebind=require_execution_rebind,
            )
        except ValueError:
            pass
        return task_delivery_turn_is_current(
            context,
            turn_id=turn_id,
            agent_id=agent_id,
            load_workflow=load_workflow,
            session_id=session_id,
        )
    with connect(context.db_path) as conn:
        row = _task_delivery_for_turn_in(
            conn,
            turn_id=turn_id,
            agent_id=agent_id,
            session_id=session_id,
        )
        if row is None:
            return None
        if str(row["turn_id"] or "") != turn_id:
            return False
        execution = active_delivery_execution_in(
            conn,
            delivery_id=int(row["id"]),
            workflow_key=context.workflow_key,
            load_workflow=load_workflow,
            turn_id=turn_id,
        )
        if execution is not None:
            return True
        if row["status"] != "processing":
            return False
    return task_delivery_is_current(
        context,
        delivery_id=int(row["id"]),
        load_workflow=load_workflow,
    )


def _task_delivery_for_turn_in(
    conn: Any,
    *,
    turn_id: str,
    agent_id: str,
    session_id: str | None = None,
) -> Any:
    return conn.execute(
        """
        SELECT delivery.id, delivery.status, delivery.turn_id, event.record_id
        FROM task_event_deliveries AS delivery
        JOIN task_events AS event
          ON event.event_key = delivery.event_key
        LEFT JOIN task_delivery_turns AS delivery_turn
          ON delivery_turn.delivery_id = delivery.id
         AND delivery_turn.turn_id = ?
        WHERE (delivery.turn_id = ? OR delivery_turn.turn_id IS NOT NULL)
          AND delivery.agent_id = ?
          AND (? IS NULL OR delivery.session_id = ?)
        ORDER BY delivery.started_at DESC, delivery.id DESC
        LIMIT 1
        """,
        (turn_id, turn_id, agent_id, session_id, session_id),
    ).fetchone()


def task_delivery_has_active_execution(
    context: LarkEventContext,
    *,
    delivery_id: int,
    load_workflow: WorkflowLoader,
    turn_id: str | None = None,
) -> bool:
    if active_delivery_execution(
        context,
        delivery_id=delivery_id,
        load_workflow=load_workflow,
        turn_id=turn_id,
    ) is not None:
        return True
    return False


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
    expected_status: str,
    expected_turn_id: str | None,
    expected_turn_status: str | None,
    turn_status: str = "missing",
    allow_active_execution_stop: bool = False,
) -> bool:
    with connect(context.db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE task_event_deliveries
            SET status = 'canceled',
                turn_status = ?,
                last_error = ?,
                next_attempt_at = NULL,
                completed_at = ?
            WHERE id = ?
              AND status = ?
              AND turn_id IS ?
              AND turn_status IS ?
              AND (
                ?
                OR NOT EXISTS (
                  SELECT 1
                  FROM task_events AS event
                  JOIN task_executions AS execution
                    ON execution.record_id = event.record_id
                  WHERE event.event_key = task_event_deliveries.event_key
                    AND execution.agent_id = task_event_deliveries.agent_id
                    AND execution.session_id = task_event_deliveries.session_id
                    AND execution.state = 'active'
                )
              )
            """,
            (
                turn_status,
                reason,
                now(),
                delivery_id,
                expected_status,
                expected_turn_id,
                expected_turn_status,
                int(allow_active_execution_stop),
            ),
        )
        if cursor.rowcount != 1:
            return False
        if allow_active_execution_stop and expected_turn_id:
            stop_active_delivery_execution(
                conn,
                delivery_id=delivery_id,
                turn_id=expected_turn_id,
                reason=reason,
            )
    return True


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
