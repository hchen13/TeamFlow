from __future__ import annotations

import json
from typing import Any, Callable

from .lark_events import LarkEventContext


WorkflowLoader = Callable[[str], dict[str, Any]]


def render_task_prompt(
    context: LarkEventContext,
    *,
    event_type: str,
    event_key: str,
    workflow_key: str,
    role_name: str,
    task: dict[str, Any],
    load_workflow: WorkflowLoader,
) -> str:
    definition = load_workflow(workflow_key)
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
            value = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        header.append(f"{label}：{value}")
    header.extend((
        "",
        "以上是 TeamFlow 派发前读取的卡片快照。需要重新确认或变更任务时，只能使用 "
        "TeamFlow MCP 工具；如果工具不可用，请明确报告，禁止降级调用 Lark CLI、飞书 "
        "API 或底层多维表格接口。",
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
    *,
    load_workflow: WorkflowLoader,
) -> str | None:
    definition = load_workflow(workflow_key)
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


def target_role(
    workflow_key: str,
    state_key: str | None,
    task: dict[str, Any],
    *,
    load_workflow: WorkflowLoader,
) -> str | None:
    return task_dispatch_target(
        workflow_key,
        {**task, "status": state_key},
        load_workflow=load_workflow,
    )


def actionable_states(
    workflow_key: str,
    *,
    load_workflow: WorkflowLoader,
) -> dict[str, str]:
    definition = load_workflow(workflow_key)
    return {
        state["key"]: f"{state['key']}_entered"
        for state in definition["lifecycle"]["states"]
        if state["dispatch"] != "none"
    }


def dispatch_states(
    workflow_key: str,
    *,
    load_workflow: WorkflowLoader,
) -> dict[str, dict[str, Any]]:
    definition = load_workflow(workflow_key)
    return {
        state["key"]: state
        for state in definition["lifecycle"]["states"]
        if state["dispatch"] != "none"
    }


def dispatch_event_state(
    states: dict[str, dict[str, Any]],
    event: Any,
) -> dict[str, Any] | None:
    event_type = str(event["event_type"])
    for state_key, state in states.items():
        if event_type == f"{state_key}_entered":
            return state
        if (
            event_type != f"{state_key}_updated"
            or state["dispatch"] != "task_role"
        ):
            continue
        before = json.loads(event["before_json"] or "{}")
        after = json.loads(event["after_json"] or "{}")
        if before.get("role") != after.get("role"):
            return state
    return None


def current_dispatch(
    conn: Any,
    event: Any,
    states: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    state_row = conn.execute(
        """
        SELECT status, snapshot_json
        FROM lark_task_state
        WHERE board_id = ? AND table_id = ? AND record_id = ?
        """,
        (event["board_id"], event["table_id"], event["record_id"]),
    ).fetchone()
    if state_row is None or state_row["status"] not in states:
        return None
    state = states[str(state_row["status"])]
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
            if (dispatch_event_state(states, candidate) or {}).get("key")
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


def finish_routing(
    conn: Any,
    event_key: str,
    status: str,
    note: str | None,
    timestamp: str,
) -> None:
    conn.execute(
        """
        UPDATE task_events
        SET routing_status = ?, routing_note = ?, routed_at = ?
        WHERE event_key = ?
        """,
        (status, note, timestamp, event_key),
    )


def current_task_status(conn: Any, event: Any) -> str | None:
    row = conn.execute(
        """
        SELECT status
        FROM lark_task_state
        WHERE board_id = ? AND table_id = ? AND record_id = ?
        """,
        (event["board_id"], event["table_id"], event["record_id"]),
    ).fetchone()
    return str(row["status"]) if row and row["status"] else None


def insert_delivery(
    conn: Any,
    event: Any,
    agent: Any,
    prompt: str,
    timestamp: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO task_event_deliveries (
          event_key, agent_id, assignment_revision, harness_type,
          session_id, prompt, created_at
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
