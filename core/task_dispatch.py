from __future__ import annotations

import json
from typing import Any

from .db import bootstrap_workspace, connect, now, workspace_id_for_root
from .lark_events import LarkEventContext


COORDINATOR_EVENTS = {"review_entered", "blocked_entered"}


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
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO task_event_deliveries (
                      event_key, agent_id, prompt, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (event["event_key"], agent["id"], prompt, timestamp),
                )
                deliveries += cursor.rowcount
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


def recover_task_deliveries(context: LarkEventContext) -> None:
    with connect(context.db_path) as conn:
        conn.execute(
            "UPDATE task_event_deliveries SET status = 'pending' WHERE status = 'processing'"
        )


def claim_task_deliveries(context: LarkEventContext, *, limit: int = 100) -> list[dict[str, Any]]:
    claimed = []
    with connect(context.db_path) as conn:
        rows = conn.execute(
            """
            SELECT delivery.*, event.event_type, event.record_id, event.source_event_id,
                   event.after_json, event.before_json,
                   agent.harness_type, agent.session_id, agent.display_name, agent.role_key
            FROM task_event_deliveries AS delivery
            JOIN task_events AS event ON event.event_key = delivery.event_key
            JOIN agents AS agent ON agent.id = delivery.agent_id
            WHERE delivery.status = 'pending'
            ORDER BY delivery.created_at, delivery.event_key, delivery.agent_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for row in rows:
            cursor = conn.execute(
                """
                UPDATE task_event_deliveries
                SET status = 'processing', attempts = attempts + 1, last_error = NULL
                WHERE event_key = ? AND agent_id = ? AND status = 'pending'
                """,
                (row["event_key"], row["agent_id"]),
            )
            if cursor.rowcount == 1:
                claimed.append(dict(row))
    return claimed


def finish_task_delivery(
    context: LarkEventContext,
    *,
    event_key: str,
    agent_id: str,
    error: Exception | None = None,
) -> None:
    with connect(context.db_path) as conn:
        conn.execute(
            """
            UPDATE task_event_deliveries
            SET status = ?, last_error = ?, delivered_at = ?
            WHERE event_key = ? AND agent_id = ?
            """,
            (
                "failed" if error else "previewed",
                str(error) if error else None,
                now(),
                event_key,
                agent_id,
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
    ]
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
