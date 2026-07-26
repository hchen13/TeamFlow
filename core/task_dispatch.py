from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import bootstrap_workspace, connect, now, workspace_id_for_root
from .lark_events import LarkEventContext
from .workflow import load_workflow_definition


def prepare_task_deliveries(context: LarkEventContext) -> dict[str, Any]:
    timestamp = now()
    routed = waiting = ignored = deliveries = 0
    outcomes = []
    dispatch_states = _dispatch_states(context.workflow_key)
    with connect(context.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, context.workspace_root)
        events = conn.execute(
            "SELECT * FROM task_events WHERE routing_status = 'pending' ORDER BY created_at, event_key"
        ).fetchall()
        for event in events:
            task = json.loads(event["after_json"] or event["before_json"] or "{}")
            event_state = _dispatch_event_state(dispatch_states, event)
            if event_state:
                current = _current_dispatch(conn, event, dispatch_states)
                if current:
                    task = current["task"]
                if not current or current["event"]["event_key"] != event["event_key"]:
                    expected_status = event_state["key"]
                    current_status = current["state"]["key"] if current else _current_task_status(conn, event)
                    note = (
                        f"task is no longer {expected_status}"
                        if current_status != expected_status
                        else f"task has a newer {expected_status} dispatch event"
                    )
                    _finish_routing(
                        conn,
                        event["event_key"],
                        "ignored",
                        note,
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
            else:
                event_state = None
            target_role = _target_role(
                conn,
                event["workflow_id"],
                context.workflow_key,
                event_state["key"] if event_state else None,
                task,
            )
            if not target_role:
                _finish_routing(
                    conn,
                    event["event_key"],
                    "ignored",
                    "event does not notify an agent",
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
    actionable_states = _actionable_states(context.workflow_key)
    if not actionable_states:
        return 0
    placeholders = ", ".join("?" for _ in actionable_states)
    with connect(context.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, context.workspace_root)
        states = conn.execute(
            """
            SELECT board_id, table_id, record_id, status, snapshot_json
            FROM lark_task_state
            WHERE status IN ({placeholders})
            """.format(placeholders=placeholders),
            tuple(actionable_states),
        ).fetchall()
        for state in states:
            event_type = actionable_states[str(state["status"])]
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
            target_role = _target_role(
                conn,
                event["workflow_id"],
                context.workflow_key,
                str(state["status"]),
                task,
            )
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
    dispatch_states = _dispatch_states(context.workflow_key)
    with connect(context.db_path) as conn:
        rows = conn.execute(
            """
            SELECT delivery.*, event.board_id, event.table_id, event.workflow_id,
                   event.event_type, event.record_id, event.source_event_id,
                   event.after_json, event.before_json,
                   agent.display_name, agent.role_key,
                   COALESCE(roles.display_name_zh, roles.display_name) AS role_name,
                   state.status AS current_task_status,
                   state.snapshot_json AS current_snapshot_json
            FROM task_event_deliveries AS delivery
            JOIN task_events AS event ON event.event_key = delivery.event_key
            JOIN agents AS agent ON agent.id = delivery.agent_id
            JOIN roles ON roles.id = agent.role_id
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
            current = _current_dispatch(conn, row, dispatch_states)
            if not current or current["event"]["event_key"] != row["event_key"]:
                expected_status = (
                    (_dispatch_event_state(dispatch_states, row) or {}).get("key")
                    or "an actionable state"
                )
                reason = (
                    f"task is no longer {expected_status}"
                    if not current or current["state"]["key"] != expected_status
                    else f"task has a newer {expected_status} dispatch event"
                )
                conn.execute(
                    """
                    UPDATE task_event_deliveries
                    SET status = 'canceled', last_error = ?, completed_at = ?
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
            target_role = _target_role(
                conn,
                row["workflow_id"],
                context.workflow_key,
                current["state"]["key"],
                task,
            )
            if target_role != row["role_key"]:
                conn.execute(
                    """
                    UPDATE task_event_deliveries
                    SET status = 'canceled', last_error = ?, completed_at = ?
                    WHERE event_key = ? AND agent_id = ?
                      AND status IN ('pending', 'retry')
                    """,
                    (
                        f"task now targets role {target_role or 'none'}",
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
            )
            cursor = conn.execute(
                """
                UPDATE task_event_deliveries
                SET status = 'processing', attempts = attempts + 1,
                    turn_id = NULL, turn_status = NULL, last_error = NULL,
                    next_attempt_at = NULL, started_at = ?, completed_at = NULL,
                    prompt = ?
                WHERE event_key = ? AND agent_id = ?
                  AND status IN ('pending', 'retry')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                """,
                (timestamp, prompt, row["event_key"], row["agent_id"], timestamp),
            )
            if cursor.rowcount == 1:
                item = dict(row)
                item["attempts"] = int(item["attempts"]) + 1
                item["prompt"] = prompt
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
                       COALESCE(state.snapshot_json, event.after_json) AS after_json,
                       event.before_json,
                       agent.display_name, agent.role_key
                FROM task_event_deliveries AS delivery
                JOIN task_events AS event ON event.event_key = delivery.event_key
                LEFT JOIN agents AS agent ON agent.id = delivery.agent_id
                LEFT JOIN lark_task_state AS state
                  ON state.board_id = event.board_id
                 AND state.table_id = event.table_id
                 AND state.record_id = event.record_id
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
            SET status = 'canceled', last_error = ?, completed_at = ?
            WHERE id = ? AND status = 'processing' AND turn_id IS NULL
            """,
            (reason, now(), delivery_id),
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
            WHERE id = ? AND status = 'processing' AND turn_id IS NULL
            """,
            (prompt, delivery_id),
        )


def task_delivery_is_current(
    context: LarkEventContext,
    *,
    delivery_id: int,
) -> bool:
    dispatch_states = _dispatch_states(context.workflow_key)
    with connect(context.db_path) as conn:
        row = conn.execute(
            """
            SELECT event.*
            FROM task_event_deliveries AS delivery
            JOIN task_events AS event ON event.event_key = delivery.event_key
            WHERE delivery.id = ?
            """,
            (delivery_id,),
        ).fetchone()
        if row is None:
            return False
        current = _current_dispatch(conn, row, dispatch_states)
        return bool(
            current
            and current["event"]["event_key"] == row["event_key"]
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
    definition = load_workflow_definition(workflow_key)
    state = next(
        (
            state
            for state in definition["lifecycle"]["states"]
            if state["key"] == task.get("status")
        ),
        None,
    )
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
    instruction = (
        (state or {}).get("dispatch_instructions", {}).get("zh-CN")
        or "请读取完整卡片，并按当前协作模式返回的合法动作处理本次任务事件。"
    )
    return "\n".join((*header, instruction))


def task_dispatch_target(
    workflow_key: str,
    task: dict[str, Any],
) -> str | None:
    definition = load_workflow_definition(workflow_key)
    state = next(
        (
            state
            for state in definition["lifecycle"]["states"]
            if state["key"] == task.get("status")
        ),
        None,
    )
    if state is None or state["dispatch"] == "none":
        return None
    if state["dispatch"] == "task_role":
        return str(task.get("role") or "") or None
    return str(definition["coordinator_role"])


def _target_role(
    conn: Any,
    workflow_id: str,
    workflow_key: str,
    state_key: str | None,
    task: dict[str, Any],
) -> str | None:
    return task_dispatch_target(
        workflow_key,
        {**task, "status": state_key},
    )


def _actionable_states(workflow_key: str) -> dict[str, str]:
    definition = load_workflow_definition(workflow_key)
    return {
        state["key"]: f"{state['key']}_entered"
        for state in definition["lifecycle"]["states"]
        if state["dispatch"] != "none"
    }


def _dispatch_states(workflow_key: str) -> dict[str, dict[str, Any]]:
    definition = load_workflow_definition(workflow_key)
    return {
        state["key"]: state
        for state in definition["lifecycle"]["states"]
        if state["dispatch"] != "none"
    }


def _dispatch_event_state(
    dispatch_states: dict[str, dict[str, Any]],
    event: Any,
) -> dict[str, Any] | None:
    event_type = str(event["event_type"])
    for state_key, state in dispatch_states.items():
        if event_type == f"{state_key}_entered":
            return state
        if event_type != f"{state_key}_updated" or state["dispatch"] != "task_role":
            continue
        before = json.loads(event["before_json"] or "{}")
        after = json.loads(event["after_json"] or "{}")
        if before.get("role") != after.get("role"):
            return state
    return None


def _current_dispatch(
    conn: Any,
    event: Any,
    dispatch_states: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    state_row = conn.execute(
        """
        SELECT status, snapshot_json
        FROM lark_task_state
        WHERE board_id = ? AND table_id = ? AND record_id = ?
        """,
        (event["board_id"], event["table_id"], event["record_id"]),
    ).fetchone()
    if state_row is None or state_row["status"] not in dispatch_states:
        return None
    state = dispatch_states[str(state_row["status"])]
    candidates = conn.execute(
        """
        SELECT *
        FROM task_events
        WHERE board_id = ? AND table_id = ? AND record_id = ?
          AND event_type IN (?, ?)
        ORDER BY rowid DESC
        """,
        (
            event["board_id"],
            event["table_id"],
            event["record_id"],
            f"{state['key']}_entered",
            f"{state['key']}_updated",
        ),
    ).fetchall()
    trigger = next(
        (
            candidate
            for candidate in candidates
            if (_dispatch_event_state(dispatch_states, candidate) or {}).get("key")
            == state["key"]
        ),
        None,
    )
    if trigger is None:
        return None
    return {
        "state": state,
        "event": trigger,
        "task": json.loads(state_row["snapshot_json"]),
    }


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
