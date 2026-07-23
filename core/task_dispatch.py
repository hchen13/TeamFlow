from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import bootstrap_workspace, connect, now, workspace_id_for_root
from .lark_events import LarkEventContext


COORDINATOR_EVENTS = {"review_entered", "blocked_entered"}
ACTIONABLE_STATES = {
    "ready": "ready_entered",
    "review": "review_entered",
    "blocked": "blocked_entered",
}
ACTIONABLE_EVENTS = {event_type: status for status, event_type in ACTIONABLE_STATES.items()}


def prepare_task_deliveries(context: LarkEventContext) -> dict[str, Any]:
    timestamp = now()
    routed = waiting = ignored = deliveries = 0
    outcomes = []
    with connect(context.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, context.workspace_root)
        events = conn.execute(
            "SELECT * FROM task_events WHERE routing_status = 'pending' ORDER BY created_at, event_key"
        ).fetchall()
        for event in events:
            task = json.loads(event["after_json"] or event["before_json"] or "{}")
            expected_status = ACTIONABLE_EVENTS.get(str(event["event_type"]))
            if expected_status and _current_task_status(conn, event) != expected_status:
                _finish_routing(
                    conn,
                    event["event_key"],
                    "ignored",
                    f"task is no longer {expected_status}",
                    timestamp,
                )
                outcomes.append({
                    "source_event_id": event["source_event_id"],
                    "event_type": event["event_type"],
                    "record_id": event["record_id"],
                    "task": task,
                    "result": "not-required",
                    "target": None,
                })
                ignored += 1
                continue
            target_role = _target_role(conn, event["workflow_id"], event["event_type"], task)
            if not target_role:
                _finish_routing(conn, event["event_key"], "ignored", "event does not notify an agent", timestamp)
                outcomes.append({
                    "source_event_id": event["source_event_id"],
                    "event_type": event["event_type"],
                    "record_id": event["record_id"],
                    "task": task,
                    "result": "not-required",
                    "target": None,
                })
                ignored += 1
                continue
            agents = conn.execute(
                """
                SELECT agents.*, COALESCE(roles.display_name_zh, roles.display_name) AS role_name
                FROM agents
                JOIN roles ON roles.id = agents.role_id
                WHERE agents.workspace_id = ? AND agents.workflow_id = ? AND agents.role_key = ?
                ORDER BY agents.created_at, agents.id
                """,
                (workspace_id, event["workflow_id"], target_role),
            ).fetchall()
            if not agents:
                conn.execute(
                    "UPDATE task_events SET routing_note = ? WHERE event_key = ?",
                    (f"no registered agent for role {target_role}", event["event_key"]),
                )
                outcomes.append({
                    "source_event_id": event["source_event_id"],
                    "event_type": event["event_type"],
                    "record_id": event["record_id"],
                    "task": task,
                    "result": "waiting",
                    "target": target_role,
                })
                waiting += 1
                continue
            workflow = conn.execute("SELECT key FROM workflows WHERE id = ?", (event["workflow_id"],)).fetchone()
            for agent in agents:
                prompt = render_task_prompt(
                    context,
                    event_type=str(event["event_type"]),
                    event_key=str(event["event_key"]),
                    workflow_key=str(workflow["key"]),
                    role_name=str(agent["role_name"] or agent["role_key"]),
                    task=task,
                )
                deliveries += _insert_delivery(conn, event, agent, prompt, timestamp)
            _finish_routing(conn, event["event_key"], "routed", None, timestamp)
            outcomes.append({
                "source_event_id": event["source_event_id"],
                "event_type": event["event_type"],
                "record_id": event["record_id"],
                "task": task,
                "result": "routed",
                "target": target_role,
            })
            routed += 1
    return {
        "routed": routed,
        "waiting": waiting,
        "ignored": ignored,
        "deliveries": deliveries,
        "outcomes": outcomes,
    }


def prepare_agent_catchup_deliveries(context: LarkEventContext) -> int:
    timestamp = now()
    deliveries = 0
    with connect(context.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, context.workspace_root)
        states = conn.execute(
            """
            SELECT board_id, table_id, record_id, status, snapshot_json
            FROM lark_task_state
            WHERE status IN ('ready', 'review', 'blocked')
            """
        ).fetchall()
        for state in states:
            event_type = ACTIONABLE_STATES[str(state["status"])]
            event = conn.execute(
                """
                SELECT * FROM task_events
                WHERE board_id = ? AND table_id = ? AND record_id = ? AND event_type = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (state["board_id"], state["table_id"], state["record_id"], event_type),
            ).fetchone()
            if event is None:
                continue
            task = json.loads(state["snapshot_json"])
            target_role = _target_role(conn, event["workflow_id"], event_type, task)
            if not target_role:
                continue
            workflow = conn.execute("SELECT key FROM workflows WHERE id = ?", (event["workflow_id"],)).fetchone()
            agents = conn.execute(
                """
                SELECT agents.*, COALESCE(roles.display_name_zh, roles.display_name) AS role_name
                FROM agents
                JOIN roles ON roles.id = agents.role_id
                WHERE agents.workspace_id = ? AND agents.workflow_id = ? AND agents.role_key = ?
                ORDER BY agents.created_at, agents.id
                """,
                (workspace_id, event["workflow_id"], target_role),
            ).fetchall()
            for agent in agents:
                prompt = render_task_prompt(
                    context,
                    event_type=event_type,
                    event_key=str(event["event_key"]),
                    workflow_key=str(workflow["key"]),
                    role_name=str(agent["role_name"] or agent["role_key"]),
                    task=task,
                )
                deliveries += _insert_delivery(conn, event, agent, prompt, timestamp)
    return deliveries


def recover_task_deliveries(context: LarkEventContext) -> None:
    with connect(context.db_path) as conn:
        conn.execute(
            """
            UPDATE task_event_deliveries
            SET status = 'pending', started_at = NULL, next_attempt_at = NULL
            WHERE status = 'processing' AND turn_id IS NULL
            """
        )
        conn.execute(
            """
            UPDATE task_event_deliveries
            SET next_attempt_at = COALESCE(next_attempt_at, ?)
            WHERE status = 'processing' AND turn_id IS NOT NULL
            """,
            (now(),),
        )


def claim_task_deliveries(
    context: LarkEventContext,
    *,
    limit: int = 100,
    exclude_session_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    claimed = []
    reserved_sessions = set(exclude_session_ids or ())
    timestamp = now()
    with connect(context.db_path) as conn:
        rows = conn.execute(
            """
            SELECT delivery.*, event.event_type, event.record_id, event.source_event_id,
                   event.after_json, event.before_json,
                   agent.display_name, agent.role_key,
                   state.status AS current_task_status
            FROM task_event_deliveries AS delivery
            JOIN task_events AS event ON event.event_key = delivery.event_key
            JOIN agents AS agent ON agent.id = delivery.agent_id
            LEFT JOIN lark_task_state AS state
              ON state.board_id = event.board_id
             AND state.table_id = event.table_id
             AND state.record_id = event.record_id
            WHERE delivery.status IN ('pending', 'retry')
              AND (delivery.next_attempt_at IS NULL OR delivery.next_attempt_at <= ?)
            ORDER BY delivery.created_at, delivery.event_key, delivery.agent_id
            LIMIT ?
            """,
            (timestamp, max(limit * 4, limit)),
        ).fetchall()
        for row in rows:
            expected_status = ACTIONABLE_EVENTS.get(str(row["event_type"]))
            if expected_status and row["current_task_status"] != expected_status:
                conn.execute(
                    """
                    UPDATE task_event_deliveries
                    SET status = 'canceled', last_error = ?, completed_at = ?
                    WHERE event_key = ? AND agent_id = ?
                      AND status IN ('pending', 'retry')
                    """,
                    (
                        f"task is no longer {expected_status}",
                        timestamp,
                        row["event_key"],
                        row["agent_id"],
                    ),
                )
                continue
            session_id = str(row["session_id"])
            if session_id in reserved_sessions:
                continue
            cursor = conn.execute(
                """
                UPDATE task_event_deliveries
                SET status = 'processing', attempts = attempts + 1,
                    turn_id = NULL, turn_status = NULL, last_error = NULL,
                    next_attempt_at = NULL, started_at = ?, completed_at = NULL
                WHERE event_key = ? AND agent_id = ?
                  AND status IN ('pending', 'retry')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                """,
                (timestamp, row["event_key"], row["agent_id"], timestamp),
            )
            if cursor.rowcount == 1:
                item = dict(row)
                item["attempts"] = int(item["attempts"]) + 1
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
) -> None:
    timestamp = now()
    if retry:
        status = "retry"
        next_attempt_at = _retry_at(context, delivery_id)
        completed_at = None
        delivered_at = None
    else:
        status = "completed" if error is None and (result or {}).get("ok", True) else "failed"
        next_attempt_at = None
        completed_at = timestamp
        delivered_at = timestamp if status == "completed" else None
    with connect(context.db_path) as conn:
        conn.execute(
            """
            UPDATE task_event_deliveries
            SET status = ?, turn_status = COALESCE(?, turn_status), last_error = ?,
                next_attempt_at = ?, completed_at = ?, delivered_at = ?
            WHERE id = ?
            """,
            (
                status,
                (result or {}).get("status"),
                str(error) if error else (result or {}).get("error"),
                next_attempt_at,
                completed_at,
                delivered_at,
                delivery_id,
            ),
        )


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
            SET turn_id = ?, turn_status = 'inProgress', next_attempt_at = ?
            WHERE id = ? AND status = 'processing'
            """,
            (
                turn_id,
                (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(),
                delivery_id,
            ),
        )


def due_processing_task_deliveries(context: LarkEventContext) -> list[dict[str, Any]]:
    with connect(context.db_path) as conn:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT delivery.*, event.record_id, event.source_event_id,
                       event.after_json, event.before_json,
                       agent.display_name, agent.role_key
                FROM task_event_deliveries AS delivery
                JOIN task_events AS event ON event.event_key = delivery.event_key
                LEFT JOIN agents AS agent ON agent.id = delivery.agent_id
                WHERE delivery.status = 'processing' AND delivery.turn_id IS NOT NULL
                  AND (delivery.next_attempt_at IS NULL OR delivery.next_attempt_at <= ?)
                ORDER BY delivery.started_at, delivery.event_key, delivery.agent_id
                """,
                (now(),),
            )
        ]


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


def render_task_prompt(
    context: LarkEventContext,
    *,
    event_type: str,
    event_key: str,
    workflow_key: str,
    role_name: str,
    task: dict[str, Any],
) -> str:
    task_id = str(task.get("task_id") or task.get("record_id") or "-")
    title = str(task.get("title") or "未命名任务")
    header = [
        "你收到了一个 TeamFlow 任务事件。",
        "",
        f"协作模式：{workflow_key}",
        f"事件：{event_type}",
        f"事件键：{event_key}",
        f"目标职责：{role_name}",
        f"任务：{task_id} {title}",
        f"记录 ID：{task.get('record_id') or '-'}",
        f"当前状态：{task.get('status') or '-'}",
        f"当前负责人：{task.get('role') or '-'}",
        f"多维表格：{context.board_url}",
        "",
        "当前卡片快照：",
    ]
    fields = (
        ("任务类型", "type"),
        ("优先级", "priority"),
        ("任务描述", "description"),
        ("背景信息", "context"),
        ("依赖关系", "dependencies"),
        ("验收标准", "acceptance_criteria"),
        ("当前进展", "progress"),
        ("下一步", "next_action"),
        ("结果与证据", "result_evidence"),
        ("阻塞原因", "blocked_reason"),
        ("等待对象", "waiting_on"),
    )
    for label, key in fields:
        value = task.get(key)
        if value in (None, "", []):
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        header.append(f"{label}：{value}")
    header.extend((
        "",
        "以上是 TeamFlow 派发前读取的卡片快照。需要重新确认或变更任务时，只能使用 TeamFlow MCP 工具；如果工具不可用，请明确报告，禁止降级调用 Lark CLI、飞书 API 或底层多维表格接口。",
        "",
    ))
    if event_type == "ready_entered":
        instruction = "这是一项等待认领的可执行任务。请读取完整卡片并判断是否接手；仅在决定执行后通过 TeamFlow 工具认领，收到通知本身不代表已经认领。"
    elif event_type == "review_entered":
        instruction = "该任务已进入待评审。请读取结果与证据，按 PM 职责完成评审并决定下一步。"
    else:
        instruction = "该任务刚进入已阻塞。请读取阻塞原因、等待对象和下一步，按 PM 职责处理本次阻塞。"
    return "\n".join((*header, instruction))


def _target_role(conn: Any, workflow_id: str, event_type: str, task: dict[str, Any]) -> str | None:
    if event_type == "ready_entered":
        return str(task.get("role") or "") or None
    if event_type not in COORDINATOR_EVENTS:
        return None
    coordinator = conn.execute(
        "SELECT role_key FROM roles WHERE workflow_id = ? AND is_coordinator = 1",
        (workflow_id,),
    ).fetchone()
    return str(coordinator["role_key"]) if coordinator else None


def _finish_routing(conn: Any, event_key: str, status: str, note: str | None, timestamp: str) -> None:
    conn.execute(
        """
        UPDATE task_events
        SET routing_status = ?, routing_note = ?, routed_at = ?
        WHERE event_key = ?
        """,
        (status, note, timestamp, event_key),
    )


def _current_task_status(conn: Any, event: Any) -> str | None:
    row = conn.execute(
        """
        SELECT status
        FROM lark_task_state
        WHERE board_id = ? AND table_id = ? AND record_id = ?
        """,
        (event["board_id"], event["table_id"], event["record_id"]),
    ).fetchone()
    return str(row["status"]) if row and row["status"] else None


def _insert_delivery(conn: Any, event: Any, agent: Any, prompt: str, timestamp: str) -> int:
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO task_event_deliveries (
          event_key, agent_id, assignment_revision, harness_type, session_id, prompt, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event["event_key"],
            agent["id"],
            agent["assignment_revision"],
            agent["harness_type"],
            agent["session_id"],
            prompt,
            timestamp,
        ),
    )
    return cursor.rowcount


def _retry_at(context: LarkEventContext, delivery_id: int) -> str:
    with connect(context.db_path) as conn:
        row = conn.execute(
            "SELECT attempts FROM task_event_deliveries WHERE id = ?",
            (delivery_id,),
        ).fetchone()
    attempts = int(row["attempts"]) if row else 1
    delay = min(60, 2 ** min(attempts, 6))
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
