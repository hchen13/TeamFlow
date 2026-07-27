from __future__ import annotations

from typing import Any

from .workflow import blank, task_name, workflow_definition_for_assignment
from .workflow_contract import ACTION_TO_TOOL, runtime_action_actor_allowed
from .workflow_responses import workflow_error


def prepare_runtime_action(
    assignment: dict[str, Any],
    *,
    action_key: str,
    task: dict[str, Any],
    payload: dict[str, Any],
    confirmed: bool,
) -> dict[str, Any]:
    definition = workflow_definition_for_assignment(assignment)
    action = definition.get("runtime_actions", {}).get(action_key)
    if not action:
        return workflow_error(
            assignment,
            definition,
            action_key=action_key,
            task=task,
            category="validation",
            code="unsupported_action",
            message=f"协作模式 {definition['key']} 不支持运行时动作 {action_key}。",
        )
    if action.get("confirmation_required") and not confirmed:
        return workflow_error(
            assignment,
            definition,
            action_key=action_key,
            task=task,
            category="business_rule",
            code="confirmation_required",
            message=(
                f"任务 {task_name(task)} 的停止执行尚未确认。"
                "请确认需要中断当前执行后，以 confirmed=true 重试。"
            ),
            details={"required": ["confirmed"]},
        )
    if task.get("status") not in action["states"]:
        return workflow_error(
            assignment,
            definition,
            action_key=action_key,
            task=task,
            category="business_rule",
            code="invalid_state",
            message=(
                f"任务 {task_name(task)} 当前状态为 {task.get('status') or 'unknown'}，"
                f"不能执行 {ACTION_TO_TOOL[action_key]}。"
            ),
            details={"allowed_states": action["states"]},
        )
    if not runtime_action_actor_allowed(assignment, definition, action, task):
        return workflow_error(
            assignment,
            definition,
            action_key=action_key,
            task=task,
            category="permission",
            code="permission_denied",
            message=(
                f"当前 Agent 的职责 {assignment['role_key']} 无权对任务 {task_name(task)} "
                f"执行 {ACTION_TO_TOOL[action_key]}。"
            ),
        )
    missing_inputs = [
        field for field in action.get("required_inputs", [])
        if blank(payload.get(field))
    ]
    if missing_inputs:
        return workflow_error(
            assignment,
            definition,
            action_key=action_key,
            task=task,
            category="validation",
            code="missing_fields",
            message=(
                f"{ACTION_TO_TOOL[action_key]} 缺少必填输入："
                f"{', '.join(missing_inputs)}。补齐后重试。"
            ),
            details={"missing_fields": missing_inputs},
        )
    missing_task_fields = [
        field for field in action.get("required_task_fields", [])
        if blank(task.get(field))
    ]
    if missing_task_fields:
        return workflow_error(
            assignment,
            definition,
            action_key=action_key,
            task=task,
            category="business_rule",
            code="task_not_ready",
            message=(
                f"任务 {task_name(task)} 缺少执行上下文："
                f"{', '.join(missing_task_fields)}。请先核对卡片。"
            ),
            details={"missing_task_fields": missing_task_fields},
        )
    return {
        "ok": True,
        "action": action_key,
        "tool": ACTION_TO_TOOL[action_key],
        "message": f"任务 {task_name(task)} 已通过停止执行规则校验。",
        "task": task,
        "runtime_facts": list(action.get("produces", [])),
    }


def runtime_action_error(
    assignment: dict[str, Any],
    *,
    action_key: str,
    task: dict[str, Any],
    category: str,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    return workflow_error(
        assignment,
        workflow_definition_for_assignment(assignment),
        action_key=action_key,
        task=task,
        category=category,
        code=code,
        message=message,
        details=details,
        retryable=retryable,
    )
