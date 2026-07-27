from __future__ import annotations

import json
from typing import Any

from .workflow import workflow_definition_for_assignment
from .workflow_contract import ACTION_TO_TOOL, rule_actor_allowed
from .workflow_responses import workflow_error
from .workflow_rule_validation import (
    action_patch,
    matching_applied_rule,
    merged_allowed_values,
    rule_values_allowed,
    task_name,
    validate_rule,
)


def prepare_create_action(
    assignment: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    definition = workflow_definition_for_assignment(assignment)
    payload = provided_fields(payload)
    if not payload.get("role") and payload.get("type"):
        task_type = next(
            (item for item in definition["task_types"] if item["key"] == payload["type"]),
            None,
        )
        if task_type:
            payload["role"] = task_type["default_role"]
    rule = definition["lifecycle"]["actions"]["create"]["rules"][0]
    error = validate_rule(
        assignment,
        definition,
        action_key="create",
        rule=rule,
        task=None,
        payload=payload,
        runtime_facts=set(),
    )
    if error:
        return error
    return {
        "ok": True,
        "definition": definition,
        "rule": rule,
        "patch": action_patch(assignment, rule, payload),
    }


def prepare_existing_action(
    assignment: dict[str, Any],
    *,
    action_key: str,
    task: dict[str, Any],
    payload: dict[str, Any],
    variant: str | None = None,
    confirmed: bool = True,
    runtime_facts: set[str] | None = None,
) -> dict[str, Any]:
    definition = workflow_definition_for_assignment(assignment)
    action = definition["lifecycle"]["actions"][action_key]
    if action.get("confirmation_required") and not confirmed:
        return workflow_error(
            assignment,
            definition,
            action_key=action_key,
            variant=variant,
            task=task,
            category="business_rule",
            code="confirmation_required",
            message=(
                f"任务 {task_name(task)} 的取消尚未确认。"
                "请先确认取消依据与收尾安排，再以 confirmed=true 重试。"
            ),
            details={"required": ["confirmed"]},
        )

    rules = action["rules"]
    if variant is not None:
        variant_rules = [rule for rule in rules if rule["key"] == variant]
        if not variant_rules:
            allowed_options = [
                rule["key"]
                for rule in rules
                if task.get("status") in rule.get("from", [])
                and rule_actor_allowed(assignment, definition, rule, task)
            ]
            return workflow_error(
                assignment,
                definition,
                action_key=action_key,
                variant=variant,
                task=task,
                category="validation",
                code="invalid_option",
                message=(
                    f"{ACTION_TO_TOOL[action_key]} 不支持选项 {variant!r}。"
                    f"当前状态与职责下的合法选项：{', '.join(allowed_options) or '无'}。"
                ),
                details={"allowed_options": allowed_options},
            )
        rules = variant_rules

    applied_rule = matching_applied_rule(
        assignment,
        definition,
        rules,
        task,
        payload,
    )
    if applied_rule:
        return {
            "ok": True,
            "definition": definition,
            "rule": applied_rule,
            "patch": {},
            "already_applied": True,
        }

    source_rules = [rule for rule in rules if task.get("status") in rule.get("from", [])]
    if not source_rules:
        return workflow_error(
            assignment,
            definition,
            action_key=action_key,
            variant=variant,
            task=task,
            category="business_rule",
            code="invalid_state",
            message=(
                f"任务 {task_name(task)} 当前状态为 {task.get('status') or 'unknown'}，"
                f"不能执行 {ACTION_TO_TOOL[action_key]}。"
            ),
            details={
                "allowed_from": sorted({
                    state
                    for rule in rules
                    for state in rule.get("from", [])
                }),
            },
        )
    actor_rules = [
        rule
        for rule in source_rules
        if rule_actor_allowed(assignment, definition, rule, task)
    ]
    if not actor_rules:
        return workflow_error(
            assignment,
            definition,
            action_key=action_key,
            variant=variant,
            task=task,
            category="permission",
            code="permission_denied",
            message=(
                f"当前 Agent 的职责 {assignment['role_key']} 无权对任务 {task_name(task)} "
                f"执行 {ACTION_TO_TOOL[action_key]}。"
            ),
            details={
                "agent_role": assignment["role_key"],
                "task_role": task.get("role"),
                "task_agent_id": task.get("agent_id"),
            },
        )

    payload_fields = set(payload)
    writable_rules = [
        rule
        for rule in actor_rules
        if payload_fields <= set(rule.get("writable_fields", []))
    ]
    if not writable_rules:
        allowed_fields = sorted({
            field
            for rule in actor_rules
            for field in rule.get("writable_fields", [])
        })
        invalid_fields = sorted(payload_fields - set(allowed_fields))
        return workflow_error(
            assignment,
            definition,
            action_key=action_key,
            variant=variant,
            task=task,
            category="validation",
            code="invalid_fields",
            message=(
                f"{ACTION_TO_TOOL[action_key]} 不能修改字段：{', '.join(invalid_fields)}。"
                f"当前合法字段：{', '.join(allowed_fields) or '无'}。"
            ),
            details={
                "invalid_fields": invalid_fields,
                "allowed_fields": allowed_fields,
            },
        )

    value_rules = [
        rule for rule in writable_rules if rule_values_allowed(rule, payload)
    ]
    if not value_rules:
        allowed_values = merged_allowed_values(writable_rules)
        return workflow_error(
            assignment,
            definition,
            action_key=action_key,
            variant=variant,
            task=task,
            category="validation",
            code="invalid_value",
            message=(
                f"{ACTION_TO_TOOL[action_key]} 收到了当前职责不允许的字段值。"
                f"合法值：{json.dumps(allowed_values, ensure_ascii=False)}。"
            ),
            details={"allowed_values": allowed_values},
        )

    rule = value_rules[0]
    error = validate_rule(
        assignment,
        definition,
        action_key=action_key,
        rule=rule,
        task=task,
        payload=payload,
        runtime_facts=runtime_facts or set(),
    )
    if error:
        return error
    return {
        "ok": True,
        "definition": definition,
        "rule": rule,
        "patch": action_patch(assignment, rule, payload),
        "already_applied": False,
    }


def normalize_record_id(record_id: str) -> str:
    value = str(record_id or "").strip()
    if not value:
        raise ValueError("record_id is required")
    return value


def provided_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in fields.items()
        if value is not None and value != ""
    }
