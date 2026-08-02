from __future__ import annotations

from typing import Any

from .workflow import (
    same_value,
    task_name,
    workflow_definition_for_assignment,
)
from .workflow_contract import (
    ACTION_TO_TOOL,
    VARIANT_ACTIONS,
    available_task_actions,
)


def action_success(
    assignment: dict[str, Any],
    definition: dict[str, Any],
    *,
    action_key: str,
    rule: dict[str, Any],
    before: dict[str, Any] | None,
    task: dict[str, Any],
    already_applied: bool,
) -> dict[str, Any]:
    return {
        "ok": True,
        "action": action_key,
        "tool": ACTION_TO_TOOL[action_key],
        "option": rule["key"] if action_key in VARIANT_ACTIONS else None,
        "message": (
            f"任务 {task_name(task)} 已处于目标结果，无需重复修改。"
            if already_applied
            else f"任务 {task_name(task)} 已完成动作：{rule['labels']['zh-CN']}。"
        ),
        "already_applied": already_applied,
        "transition": {
            "from": before.get("status") if before else None,
            "to": task.get("status"),
        },
        "task": task,
        "available_actions": available_task_actions(assignment, task),
    }


def task_changed_error(
    assignment: dict[str, Any],
    definition: dict[str, Any],
    *,
    action_key: str,
    variant: str | None,
    expected: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    changed_fields = sorted({
        field
        for field in set(expected) | set(current)
        if not same_value(expected.get(field), current.get(field))
    })
    return workflow_error(
        assignment,
        definition,
        action_key=action_key,
        variant=variant,
        task=current,
        category="conflict",
        code="task_changed",
        message=(
            f"任务 {task_name(current)} 在 {ACTION_TO_TOOL[action_key]} 执行前已发生变化，"
            "本次操作未写入。请先调用 get_task 读取最新卡片，再根据返回的合法动作重试。"
        ),
        retryable=True,
        details={
            "changed_fields": changed_fields,
            "expected_state": expected.get("status"),
            "current_state": current.get("status"),
        },
    )


def write_not_visible_error(
    assignment: dict[str, Any],
    definition: dict[str, Any],
    *,
    action_key: str,
    variant: str | None,
    patch: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    pending_fields = sorted(
        field for field, value in patch.items() if not same_value(current.get(field), value)
    )
    return workflow_error(
        assignment,
        definition,
        action_key=action_key,
        variant=variant,
        task=current,
        category="conflict",
        code="write_not_visible",
        message=(
            f"任务 {task_name(current)} 的 {ACTION_TO_TOOL[action_key]} 写入尚未在看板上可见，"
            "本次操作不能视为成功。请调用 get_task 确认最新卡片，再根据返回的合法动作重试。"
        ),
        retryable=True,
        details={
            "pending_fields": pending_fields,
            "expected_state": patch.get("status"),
            "current_state": current.get("status"),
        },
    )


def workflow_error(
    assignment: dict[str, Any],
    definition: dict[str, Any],
    *,
    action_key: str,
    category: str,
    code: str,
    message: str,
    task: dict[str, Any] | None,
    variant: str | None = None,
    details: dict[str, Any] | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "category": category,
            "code": code,
            "message": message,
            "retryable": retryable,
            "attempted_action": action_key,
            "attempted_option": variant,
            "record_id": task.get("record_id") if task else None,
            "task_id": task.get("task_id") if task else None,
            "current_state": task.get("status") if task else None,
            "details": details or {},
            "available_actions": available_task_actions(assignment, task) if task else [],
        },
    }


def input_error(
    assignment: dict[str, Any],
    *,
    action_key: str,
    code: str,
    message: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    return workflow_error(
        assignment,
        workflow_definition_for_assignment(assignment),
        action_key=action_key,
        category="validation",
        code=code,
        message=message,
        task=None,
        details=details,
    )
