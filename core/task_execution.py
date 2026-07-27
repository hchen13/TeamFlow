from __future__ import annotations

from typing import Any, Callable

from .config import resolve_workspace_paths
from .db import bootstrap_workspace, connect, now
from .teamflow_tools import (
    available_task_actions,
    prepare_runtime_action,
    runtime_action_error,
)


class TaskExecutionRuntime:
    def __init__(
        self,
        *,
        get_task: Callable[..., dict[str, Any]],
        stop_turn: Callable[..., dict[str, Any]],
        read_thread: Callable[..., dict[str, Any]],
        thread_permanently_unavailable: Callable[[Exception], bool],
        active_sessions: Callable[[], set[str]],
        load_workflow: Callable[[str], dict[str, Any]],
    ) -> None:
        self.get_task = get_task
        self.stop_turn = stop_turn
        self.read_thread = read_thread
        self.thread_permanently_unavailable = thread_permanently_unavailable
        self.active_sessions = active_sessions
        self.load_workflow = load_workflow

    def sync_activity(
        self,
        assignment: dict[str, Any],
        *,
        tool_name: str,
        result: dict[str, Any],
        session_id: str,
        turn_id: str | None,
    ) -> None:
        if tool_name == "stop_task_execution" or not result.get("ok"):
            return
        task = result.get("task")
        if not isinstance(task, dict) or not task.get("record_id"):
            return
        definition = self.load_workflow(str(assignment["workflow_key"]))
        execution_states = set(
            definition.get("runtime_actions", {})
            .get("stop_execution", {})
            .get("states", [])
        )
        paths = resolve_workspace_paths(assignment["workspace_root"])
        with connect(paths.db_path) as conn:
            bootstrap_workspace(conn)
            if (
                task.get("status") in execution_states
                and task.get("agent_id") == assignment["agent_id"]
            ):
                conn.execute(
                    """
                    INSERT INTO task_executions (
                      record_id, agent_id, session_id, turn_id, state,
                      stop_status, stopped_by_agent_id, stop_reason,
                      updated_at, stopped_at
                    ) VALUES (?, ?, ?, ?, 'active', NULL, NULL, NULL, ?, NULL)
                    ON CONFLICT(record_id) DO UPDATE SET
                      agent_id = excluded.agent_id,
                      session_id = excluded.session_id,
                      turn_id = COALESCE(excluded.turn_id, task_executions.turn_id),
                      state = 'active',
                      stop_status = NULL,
                      stopped_by_agent_id = NULL,
                      stop_reason = NULL,
                      updated_at = excluded.updated_at,
                      stopped_at = NULL
                    """,
                    (
                        task["record_id"],
                        assignment["agent_id"],
                        session_id,
                        turn_id,
                        now(),
                    ),
                )
            elif task.get("status") not in execution_states:
                conn.execute(
                    "DELETE FROM task_executions WHERE record_id = ?",
                    (task["record_id"],),
                )

    def stop_execution(
        self,
        assignment: dict[str, Any],
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        record_id = str(arguments.get("record_id") or "")
        task = self.get_task(
            assignment["workspace_root"],
            record_id=record_id,
        )["task"]
        prepared = prepare_runtime_action(
            assignment,
            action_key="stop_execution",
            task=task,
            payload={"reason": str(arguments.get("reason") or "")},
            confirmed=bool(arguments.get("confirmed")),
        )
        if not prepared["ok"]:
            return prepared
        agent_id = str(task["agent_id"])
        paths = resolve_workspace_paths(assignment["workspace_root"])
        with connect(paths.db_path) as conn:
            bootstrap_workspace(conn)
            agent = conn.execute(
                "SELECT harness_type, session_id FROM agents WHERE id = ?",
                (agent_id,),
            ).fetchone()
        if agent is None:
            return runtime_action_error(
                assignment,
                action_key="stop_execution",
                task=task,
                category="business_rule",
                code="executor_unavailable",
                message=(
                    f"任务 {task.get('task_id') or record_id} 的原执行 Agent 已不存在，"
                    "无需发送停止请求；请直接按恢复或取消规则处理。"
                ),
            )
        harness_type = str(agent["harness_type"])
        if harness_type != "codex":
            return runtime_action_error(
                assignment,
                action_key="stop_execution",
                task=task,
                category="runtime",
                code="unsupported_harness",
                message=f"当前尚不能停止 {harness_type} Session；本次未记录停止事实。",
            )
        session_id = str(agent["session_id"])
        with connect(paths.db_path) as conn:
            execution = conn.execute(
                """
                SELECT *
                FROM task_executions
                WHERE record_id = ? AND agent_id = ? AND session_id = ?
                """,
                (record_id, agent_id, session_id),
            ).fetchone()
        if execution is None or not str(execution["turn_id"] or "").strip():
            return runtime_action_error(
                assignment,
                action_key="stop_execution",
                task=task,
                category="runtime",
                code="execution_turn_unknown",
                message=(
                    f"无法确认任务 {task.get('task_id') or record_id} 当前对应的 Codex turn，"
                    "因此不会冒险中断该 Session 的其他工作。"
                    "请让执行 Agent 先通过 TeamFlow 工具读取该任务后再重试。"
                ),
                retryable=True,
            )
        expected_turn_id = str(execution["turn_id"])
        if execution["state"] == "stopped":
            return {
                **prepared,
                "message": (
                    f"任务 {task.get('task_id') or record_id} 的执行已处于停止状态，"
                    "无需重复中断。完成或安排好收尾后，可调用 cancel_task 并显式确认取消。"
                ),
                "already_applied": True,
                "stop": {
                    "thread_id": session_id,
                    "turn_id": expected_turn_id,
                    "status": execution["stop_status"],
                    "already_stopped": True,
                },
                "available_actions": available_task_actions(assignment, task),
            }
        try:
            stopped = self.stop_turn(
                session_id,
                expected_turn_id=expected_turn_id,
            )
        except Exception as error:
            return runtime_action_error(
                assignment,
                action_key="stop_execution",
                task=task,
                category="runtime",
                code="stop_failed",
                message=(
                    f"未能确认任务 {task.get('task_id') or record_id} 的执行已停止：{error}。"
                    "请保持 Codex 客户端可用后重试。"
                ),
                retryable=True,
            )
        latest = self.get_task(
            assignment["workspace_root"],
            record_id=record_id,
        )["task"]
        if (
            latest.get("status") != task.get("status")
            or latest.get("agent_id") != task.get("agent_id")
        ):
            return runtime_action_error(
                assignment,
                action_key="stop_execution",
                task=latest,
                category="conflict",
                code="task_changed_after_stop",
                message=(
                    "执行已停止，但卡片的状态或执行 Agent 同时发生了变化，"
                    "本次未记录停止事实。请先读取最新卡片再决定下一步。"
                ),
                details={
                    "previous_state": task.get("status"),
                    "current_state": latest.get("status"),
                    "previous_agent_id": task.get("agent_id"),
                    "current_agent_id": latest.get("agent_id"),
                    "stop": stopped,
                },
                retryable=True,
            )
        with connect(paths.db_path) as conn:
            bootstrap_workspace(conn)
            stopped_at = now()
            cursor = conn.execute(
                """
                UPDATE task_executions
                SET state = 'stopped',
                    stop_status = ?,
                    stopped_by_agent_id = ?,
                    stop_reason = ?,
                    updated_at = ?,
                    stopped_at = ?
                WHERE record_id = ? AND agent_id = ? AND session_id = ?
                  AND turn_id = ? AND state = 'active'
                """,
                (
                    str(stopped.get("status") or "stopped"),
                    assignment["agent_id"],
                    str(arguments.get("reason") or "").strip(),
                    stopped_at,
                    stopped_at,
                    record_id,
                    agent_id,
                    session_id,
                    expected_turn_id,
                ),
            )
        if cursor.rowcount != 1:
            return runtime_action_error(
                assignment,
                action_key="stop_execution",
                task=latest,
                category="conflict",
                code="execution_changed_after_stop",
                message=(
                    "目标 turn 已停止，但任务执行上下文同时发生了变化，"
                    "本次未记录停止事实。请先读取最新任务再决定下一步。"
                ),
                details={
                    "expected_turn_id": expected_turn_id,
                    "stop": stopped,
                },
                retryable=True,
            )
        return {
            **prepared,
            "message": (
                f"任务 {task.get('task_id') or record_id} 的执行已确认停止。"
                "完成或安排好收尾后，可调用 cancel_task 并显式确认取消。"
            ),
            "stop": stopped,
            "available_actions": available_task_actions(assignment, task),
        }

    def runtime_facts(
        self,
        assignment: dict[str, Any],
        task: dict[str, Any],
    ) -> set[str]:
        definition = self.load_workflow(str(assignment["workflow_key"]))
        guarded_states = {
            state
            for action in definition["lifecycle"]["actions"].values()
            for rule in action["rules"]
            if rule.get("guards")
            for state in rule.get("from", [])
        }
        if task.get("status") not in guarded_states:
            return set()
        agent_id = str(task.get("agent_id") or "")
        if not agent_id:
            return set()
        paths = resolve_workspace_paths(assignment["workspace_root"])
        with connect(paths.db_path) as conn:
            bootstrap_workspace(conn)
            agent = conn.execute(
                "SELECT harness_type, session_id FROM agents WHERE id = ?",
                (agent_id,),
            ).fetchone()
        if agent is None:
            return {"executor_unavailable", "execution_stopped"}
        if str(agent["harness_type"]) != "codex":
            return set()
        session_id = str(agent["session_id"] or "")
        with connect(paths.db_path) as conn:
            receipt = conn.execute(
                """
                SELECT 1 FROM task_executions
                WHERE record_id = ? AND agent_id = ? AND session_id = ?
                  AND state = 'stopped' AND turn_id IS NOT NULL
                """,
                (task.get("record_id"), agent_id, session_id),
            ).fetchone()
        if receipt:
            return {"execution_stopped"}
        if session_id in self.active_sessions():
            return set()
        try:
            self.read_thread(session_id)
        except Exception as error:
            if self.thread_permanently_unavailable(error):
                return {"executor_unavailable", "execution_stopped"}
        return set()
