from __future__ import annotations

import json
from typing import Any

from .workflow import blank, same_value, task_name, task_option_definitions
from .workflow_contract import (
    ACTION_TO_TOOL,
    required_task_fields,
    rule_actor_allowed,
)
from .workflow_responses import workflow_error as _workflow_error


def validate_rule(
    assignment: dict[str, Any],
    definition: dict[str, Any],
    *,
    action_key: str,
    rule: dict[str, Any],
    task: dict[str, Any] | None,
    payload: dict[str, Any],
    runtime_facts: set[str],
) -> dict[str, Any] | None:
    if not rule_actor_allowed(assignment, definition, rule, task):
        return _workflow_error(
            assignment,
            definition,
            action_key=action_key,
            variant=rule["key"],
            task=task,
            category="permission",
            code="permission_denied",
            message=f"当前 Agent 的职责 {assignment['role_key']} 无权执行 {ACTION_TO_TOOL[action_key]}。",
        )
    invalid_fields = sorted(set(payload) - set(rule.get("writable_fields", [])))
    if invalid_fields:
        return _workflow_error(
            assignment,
            definition,
            action_key=action_key,
            variant=rule["key"],
            task=task,
            category="validation",
            code="invalid_fields",
            message=f"{ACTION_TO_TOOL[action_key]} 不能修改字段：{', '.join(invalid_fields)}。",
            details={"invalid_fields": invalid_fields},
        )
    missing_inputs = [
        field
        for field in rule.get("required_inputs", [])
        if blank(payload.get(field))
    ]
    if missing_inputs:
        return _workflow_error(
            assignment,
            definition,
            action_key=action_key,
            variant=rule["key"],
            task=task,
            category="validation",
            code="missing_fields",
            message=(
                f"{ACTION_TO_TOOL[action_key]} 缺少必填输入："
                f"{', '.join(missing_inputs)}。补齐后重试。"
            ),
            details={"missing_fields": missing_inputs},
        )
    if not rule_values_allowed(rule, payload):
        allowed_values = merged_allowed_values([rule])
        return _workflow_error(
            assignment,
            definition,
            action_key=action_key,
            variant=rule["key"],
            task=task,
            category="validation",
            code="invalid_value",
            message=f"字段值不符合当前动作约束：{json.dumps(allowed_values, ensure_ascii=False)}。",
            details={"allowed_values": allowed_values},
        )
    patch = action_patch(assignment, rule, payload)
    invalid_values = _invalid_option_values(definition, patch)
    if invalid_values:
        return _workflow_error(
            assignment,
            definition,
            action_key=action_key,
            variant=rule["key"],
            task=task,
            category="validation",
            code="invalid_value",
            message=(
                "存在不受当前协作模式支持的字段值："
                f"{json.dumps(invalid_values, ensure_ascii=False)}。"
            ),
            details={"invalid_values": invalid_values},
        )
    merged = {**(task or {}), **patch}
    missing_task_fields = [
        field
        for field in required_task_fields(definition, rule, merged)
        if blank(merged.get(field))
    ]
    if missing_task_fields:
        return _workflow_error(
            assignment,
            definition,
            action_key=action_key,
            variant=rule["key"],
            task=task,
            category="business_rule",
            code="task_not_ready",
            message=(
                f"任务 {task_name(task)} 尚不满足该动作条件，缺少："
                f"{', '.join(missing_task_fields)}。请先用 update_task 补齐。"
            ),
            details={"missing_task_fields": missing_task_fields},
        )
    missing_guards = [
        guard
        for guard in rule.get("guards", [])
        if guard not in runtime_facts
    ]
    if missing_guards:
        guard_messages = {
            "executor_unavailable": (
                "原执行 Agent 的 Session 仍存在，或当前无法确认它已经永久不可用。"
                "只有 Session 被永久删除或执行 Agent 已不存在时，才能恢复入队。"
            ),
            "execution_stopped": (
                "尚未取得当前执行 turn 已停止的可靠确认。"
                "请先停止执行并确认停止结果，再重试。"
            ),
        }
        return _workflow_error(
            assignment,
            definition,
            action_key=action_key,
            variant=rule["key"],
            task=task,
            category="business_rule",
            code="precondition_failed",
            message=f"任务 {task_name(task)} 不满足前置条件：{guard_messages[missing_guards[0]]}",
            details={"missing_preconditions": missing_guards},
        )
    return None


def matching_applied_rule(
    assignment: dict[str, Any],
    definition: dict[str, Any],
    rules: list[dict[str, Any]],
    task: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    for rule in rules:
        if not rule.get("to") or task.get("status") != rule["to"]:
            continue
        if not rule_actor_allowed(assignment, definition, rule, task):
            continue
        if set(payload) - set(rule.get("writable_fields", [])):
            continue
        if not rule_values_allowed(rule, payload):
            continue
        expected = action_patch(assignment, rule, payload)
        if all(same_value(task.get(field), value) for field, value in expected.items()):
            return rule
    if rules and all(rule.get("to") is None for rule in rules):
        for rule in rules:
            if not rule_actor_allowed(assignment, definition, rule, task):
                continue
            if set(payload) - set(rule.get("writable_fields", [])):
                continue
            if all(same_value(task.get(field), value) for field, value in payload.items()):
                return rule
    return None


def action_patch(
    assignment: dict[str, Any],
    rule: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    patch = dict(rule.get("defaults", {}))
    patch.update(payload)
    patch.update(rule.get("fixed_fields", {}))
    for field, prefix in rule.get("field_prefixes", {}).items():
        value = patch.get(field)
        if not blank(value) and not str(value).startswith(prefix):
            patch[field] = f"{prefix}{value}"
    for field, source in rule.get("actor_fields", {}).items():
        patch[field] = assignment[source]
    for field in rule.get("clear_fields", []):
        patch[field] = None
    if rule.get("to"):
        patch["status"] = rule["to"]
    return patch


def rule_values_allowed(rule: dict[str, Any], payload: dict[str, Any]) -> bool:
    return all(
        field not in payload or payload[field] in values
        for field, values in rule.get("field_values", {}).items()
    )


def _invalid_option_values(
    definition: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    options = {
        field: {item["key"] for item in items}
        for field, items in task_option_definitions(definition).items()
    }
    invalid = {}
    for field, allowed in options.items():
        value = patch.get(field)
        if blank(value) or value in allowed:
            continue
        invalid[field] = {
            "received": value,
            "allowed": sorted(allowed),
        }
    return invalid


def merged_allowed_values(rules: list[dict[str, Any]]) -> dict[str, list[str]]:
    merged: dict[str, set[str]] = {}
    for rule in rules:
        for field, values in rule.get("field_values", {}).items():
            merged.setdefault(field, set()).update(values)
    return {field: sorted(values) for field, values in merged.items()}
