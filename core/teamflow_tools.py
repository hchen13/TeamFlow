from __future__ import annotations

import json
from typing import Any

from .config import resolve_workspace_paths
from .db import bootstrap_workspace, connect
from .lark_board import get_lark_task, upsert_lark_task
from .workflow import load_workflow_definition, task_option_definitions


LIFECYCLE_ACTION_TO_TOOL = {
    "create": "create_task",
    "update": "update_task",
    "route": "route_task",
    "claim": "claim_task",
    "submit": "submit_task",
    "block": "block_task",
    "review": "review_task",
    "cancel": "cancel_task",
}
RUNTIME_ACTION_TO_TOOL = {
    "stop_execution": "stop_task_execution",
}
ACTION_TO_TOOL = {
    **LIFECYCLE_ACTION_TO_TOOL,
    **RUNTIME_ACTION_TO_TOOL,
}
MUTATING_TOOL_NAMES = frozenset(LIFECYCLE_ACTION_TO_TOOL.values())
RUNTIME_TOOL_NAMES = frozenset(RUNTIME_ACTION_TO_TOOL.values())
TOOL_NAMES = frozenset({
    "get_assignment",
    "list_available_tasks",
    "get_task",
    *MUTATING_TOOL_NAMES,
    *RUNTIME_TOOL_NAMES,
})
VARIANT_ACTIONS = frozenset({"submit", "review"})


def workflow_contract(assignment: dict[str, Any]) -> dict[str, Any]:
    definition = _definition(assignment)
    lifecycle = definition["lifecycle"]
    actions = []
    for action_key, action in lifecycle["actions"].items():
        options = [
            _contract_rule(definition, action_key, action, rule)
            for rule in action["rules"]
            if _rule_role_applicable(assignment, definition, rule)
        ]
        if options:
            actions.append({
                "action": action_key,
                "tool": ACTION_TO_TOOL[action_key],
                "name": action["labels"]["zh-CN"],
                "options": options,
            })
    return {
        "key": definition["key"],
        "name": definition["labels"]["zh-CN"],
        "coordinator_role": definition["coordinator_role"],
        "initial_state": lifecycle["initial_state"],
        "terminal_states": lifecycle["terminal_states"],
        "states": [
            {
                "key": state["key"],
                "name": state["labels"]["zh-CN"],
                "dispatch": state["dispatch"],
                "required_fields": state.get("required_fields", []),
            }
            for state in lifecycle["states"]
        ],
        "actions": actions,
        "runtime_actions": [
            _runtime_action_contract(action_key, action)
            for action_key, action in definition.get("runtime_actions", {}).items()
            if _runtime_action_role_applicable(assignment, definition, action)
        ],
    }


def list_available_tasks(assignment: dict[str, Any]) -> dict[str, Any]:
    definition = _definition(assignment)
    claim_states = sorted({
        state
        for rule in definition["lifecycle"]["actions"]["claim"]["rules"]
        if _rule_role_applicable(assignment, definition, rule)
        for state in rule.get("from", [])
    })
    if not claim_states:
        return {"ok": True, "count": 0, "tasks": []}
    paths = resolve_workspace_paths(assignment["workspace_root"])
    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        rows = conn.execute(
            f"""
            SELECT snapshot_json
            FROM lark_task_state
            WHERE status IN ({", ".join("?" for _ in claim_states)})
            ORDER BY updated_at, record_id
            """,
            claim_states,
        ).fetchall()
    tasks = []
    for row in rows:
        task = json.loads(row["snapshot_json"])
        if not any(
            action["tool"] == "claim_task"
            for action in available_task_actions(assignment, task)
        ):
            continue
        tasks.append({
            "record_id": task.get("record_id"),
            "task_id": task.get("task_id"),
            "title": task.get("title"),
            "priority": task.get("priority"),
            "type": task.get("type"),
            "status": task.get("status"),
            "role": task.get("role"),
        })
    return {"ok": True, "count": len(tasks), "tasks": tasks}


def get_task(assignment: dict[str, Any], *, record_id: str) -> dict[str, Any]:
    record_id = _record_id(record_id)
    task = get_lark_task(assignment["workspace_root"], record_id=record_id)["task"]
    return {
        "ok": True,
        "task": task,
        "available_actions": available_task_actions(assignment, task),
    }


def create_task(
    assignment: dict[str, Any],
    *,
    title: str,
    task_type: str | None = None,
    priority: str | None = None,
    role: str | None = None,
    description: str | None = None,
    context: str | None = None,
    acceptance_criteria: str | None = None,
    dependencies: str | None = None,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    definition = _definition(assignment)
    payload = _provided_fields({
        "title": title,
        "type": task_type,
        "priority": priority,
        "role": role,
        "description": description,
        "context": context,
        "acceptance_criteria": acceptance_criteria,
        "dependencies": dependencies,
    })
    if not payload.get("role") and payload.get("type"):
        task_type_definition = next(
            (item for item in definition["task_types"] if item["key"] == payload["type"]),
            None,
        )
        if task_type_definition:
            payload["role"] = task_type_definition["default_role"]
    rule = definition["lifecycle"]["actions"]["create"]["rules"][0]
    error = _validate_rule(
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
    patch = _action_patch(assignment, rule, payload)
    result = upsert_lark_task(
        assignment["workspace_root"],
        task=patch,
        client_token=invocation_id,
    )
    task = result["task"]
    return _success(
        assignment,
        definition,
        action_key="create",
        rule=rule,
        before=None,
        task=task,
        already_applied=False,
    )


def update_task(
    assignment: dict[str, Any],
    *,
    record_id: str,
    fields: dict[str, Any],
    invocation_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(fields, dict) or not fields:
        return _input_error(
            assignment,
            action_key="update",
            code="fields_required",
            message="update_task 至少需要一个待更新字段。",
            details={"required": ["fields"]},
        )
    return _execute_existing_action(
        assignment,
        action_key="update",
        record_id=record_id,
        payload=fields,
        invocation_id=invocation_id,
    )


def route_task(
    assignment: dict[str, Any],
    *,
    record_id: str,
    role: str,
    runtime_facts: set[str] | None = None,
    current_task: dict[str, Any] | None = None,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    return _execute_existing_action(
        assignment,
        action_key="route",
        record_id=record_id,
        payload={"role": role},
        runtime_facts=runtime_facts,
        current_task=current_task,
        invocation_id=invocation_id,
    )


def claim_task(
    assignment: dict[str, Any],
    *,
    record_id: str,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    return _execute_existing_action(
        assignment,
        action_key="claim",
        record_id=record_id,
        payload={},
        invocation_id=invocation_id,
    )


def submit_task(
    assignment: dict[str, Any],
    *,
    record_id: str,
    outcome: str,
    result_evidence: str,
    progress: str | None = None,
    next_action: str | None = None,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    return _execute_existing_action(
        assignment,
        action_key="submit",
        variant=outcome,
        record_id=record_id,
        payload=_provided_fields({
            "result_evidence": result_evidence,
            "progress": progress,
            "next_action": next_action,
        }),
        invocation_id=invocation_id,
    )


def block_task(
    assignment: dict[str, Any],
    *,
    record_id: str,
    waiting_on: str,
    blocked_reason: str,
    next_action: str,
    progress: str | None = None,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    return _execute_existing_action(
        assignment,
        action_key="block",
        record_id=record_id,
        payload=_provided_fields({
            "waiting_on": waiting_on,
            "blocked_reason": blocked_reason,
            "next_action": next_action,
            "progress": progress,
        }),
        invocation_id=invocation_id,
    )


def review_task(
    assignment: dict[str, Any],
    *,
    record_id: str,
    decision: str,
    result_evidence: str,
    role: str | None = None,
    next_action: str | None = None,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    return _execute_existing_action(
        assignment,
        action_key="review",
        variant=decision,
        record_id=record_id,
        payload=_provided_fields({
            "result_evidence": result_evidence,
            "role": role,
            "next_action": next_action,
        }),
        invocation_id=invocation_id,
    )


def cancel_task(
    assignment: dict[str, Any],
    *,
    record_id: str,
    result_evidence: str,
    confirmed: bool,
    runtime_facts: set[str] | None = None,
    current_task: dict[str, Any] | None = None,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    return _execute_existing_action(
        assignment,
        action_key="cancel",
        record_id=record_id,
        payload={"result_evidence": result_evidence},
        confirmed=confirmed,
        runtime_facts=runtime_facts,
        current_task=current_task,
        invocation_id=invocation_id,
    )


def available_task_actions(
    assignment: dict[str, Any],
    task: dict[str, Any],
) -> list[dict[str, Any]]:
    definition = _definition(assignment)
    status = str(task.get("status") or "")
    available = []
    for action_key, action in definition["lifecycle"]["actions"].items():
        if action_key == "create":
            continue
        for rule in action["rules"]:
            if status not in rule.get("from", []):
                continue
            if not _rule_actor_allowed(assignment, definition, rule, task):
                continue
            available.append({
                "action": action_key,
                "tool": ACTION_TO_TOOL[action_key],
                "option": rule["key"] if action_key in VARIANT_ACTIONS else None,
                "name": rule["labels"]["zh-CN"],
                "to": rule.get("to"),
                "required_fields": rule.get("required_inputs", []),
                "required_task_fields": _required_task_fields(definition, rule, task),
                "writable_fields": rule.get("writable_fields", []),
                "allowed_values": _rule_allowed_values(definition, rule),
                "confirmation_required": bool(action.get("confirmation_required")),
                "preconditions": rule.get("guards", []),
            })
    for action_key, action in definition.get("runtime_actions", {}).items():
        if status not in action["states"]:
            continue
        if not _runtime_action_actor_allowed(assignment, definition, action, task):
            continue
        available.append({
            "action": action_key,
            "tool": ACTION_TO_TOOL[action_key],
            "option": None,
            "name": action["labels"]["zh-CN"],
            "to": None,
            "required_fields": action.get("required_inputs", []),
            "required_task_fields": action.get("required_task_fields", []),
            "writable_fields": [],
            "allowed_values": {},
            "confirmation_required": bool(action.get("confirmation_required")),
            "preconditions": [],
            "runtime_facts": action.get("produces", []),
        })
    return available


def prepare_runtime_action(
    assignment: dict[str, Any],
    *,
    action_key: str,
    task: dict[str, Any],
    payload: dict[str, Any],
    confirmed: bool,
) -> dict[str, Any]:
    definition = _definition(assignment)
    action = definition.get("runtime_actions", {}).get(action_key)
    if not action:
        return _workflow_error(
            assignment,
            definition,
            action_key=action_key,
            task=task,
            category="validation",
            code="unsupported_action",
            message=f"协作模式 {definition['key']} 不支持运行时动作 {action_key}。",
        )
    if action.get("confirmation_required") and not confirmed:
        return _workflow_error(
            assignment,
            definition,
            action_key=action_key,
            task=task,
            category="business_rule",
            code="confirmation_required",
            message=(
                f"任务 {_task_name(task)} 的停止执行尚未确认。"
                "请确认需要中断当前执行后，以 confirmed=true 重试。"
            ),
            details={"required": ["confirmed"]},
        )
    if task.get("status") not in action["states"]:
        return _workflow_error(
            assignment,
            definition,
            action_key=action_key,
            task=task,
            category="business_rule",
            code="invalid_state",
            message=(
                f"任务 {_task_name(task)} 当前状态为 {task.get('status') or 'unknown'}，"
                f"不能执行 {ACTION_TO_TOOL[action_key]}。"
            ),
            details={"allowed_states": action["states"]},
        )
    if not _runtime_action_actor_allowed(assignment, definition, action, task):
        return _workflow_error(
            assignment,
            definition,
            action_key=action_key,
            task=task,
            category="permission",
            code="permission_denied",
            message=(
                f"当前 Agent 的职责 {assignment['role_key']} 无权对任务 {_task_name(task)} "
                f"执行 {ACTION_TO_TOOL[action_key]}。"
            ),
        )
    missing_inputs = [
        field for field in action.get("required_inputs", [])
        if _blank(payload.get(field))
    ]
    if missing_inputs:
        return _workflow_error(
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
        if _blank(task.get(field))
    ]
    if missing_task_fields:
        return _workflow_error(
            assignment,
            definition,
            action_key=action_key,
            task=task,
            category="business_rule",
            code="task_not_ready",
            message=(
                f"任务 {_task_name(task)} 缺少执行上下文："
                f"{', '.join(missing_task_fields)}。请先核对卡片。"
            ),
            details={"missing_task_fields": missing_task_fields},
        )
    return {
        "ok": True,
        "action": action_key,
        "tool": ACTION_TO_TOOL[action_key],
        "message": f"任务 {_task_name(task)} 已通过停止执行规则校验。",
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
    return _workflow_error(
        assignment,
        _definition(assignment),
        action_key=action_key,
        task=task,
        category=category,
        code=code,
        message=message,
        details=details,
        retryable=retryable,
    )


def _execute_existing_action(
    assignment: dict[str, Any],
    *,
    action_key: str,
    record_id: str,
    payload: dict[str, Any],
    variant: str | None = None,
    confirmed: bool = True,
    runtime_facts: set[str] | None = None,
    current_task: dict[str, Any] | None = None,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    record_id = _record_id(record_id)
    definition = _definition(assignment)
    task = get_lark_task(assignment["workspace_root"], record_id=record_id)["task"]
    if current_task is not None and current_task != task:
        return _task_changed_error(
            assignment,
            definition,
            action_key=action_key,
            variant=variant,
            expected=current_task,
            current=task,
        )
    action = definition["lifecycle"]["actions"][action_key]
    if action.get("confirmation_required") and not confirmed:
        return _workflow_error(
            assignment,
            definition,
            action_key=action_key,
            variant=variant,
            task=task,
            category="business_rule",
            code="confirmation_required",
            message=(
                f"任务 {_task_name(task)} 的取消尚未确认。"
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
                and _rule_actor_allowed(assignment, definition, rule, task)
            ]
            return _workflow_error(
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

    already = _matching_applied_rule(assignment, definition, rules, task, payload)
    if already:
        return _success(
            assignment,
            definition,
            action_key=action_key,
            rule=already,
            before=task,
            task=task,
            already_applied=True,
        )

    source_rules = [rule for rule in rules if task.get("status") in rule.get("from", [])]
    if not source_rules:
        return _workflow_error(
            assignment,
            definition,
            action_key=action_key,
            variant=variant,
            task=task,
            category="business_rule",
            code="invalid_state",
            message=(
                f"任务 {_task_name(task)} 当前状态为 {task.get('status') or 'unknown'}，"
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
        if _rule_actor_allowed(assignment, definition, rule, task)
    ]
    if not actor_rules:
        return _workflow_error(
            assignment,
            definition,
            action_key=action_key,
            variant=variant,
            task=task,
            category="permission",
            code="permission_denied",
            message=(
                f"当前 Agent 的职责 {assignment['role_key']} 无权对任务 {_task_name(task)} "
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
        return _workflow_error(
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
        rule for rule in writable_rules if _rule_values_allowed(rule, payload)
    ]
    if not value_rules:
        allowed_values = _merged_allowed_values(writable_rules)
        return _workflow_error(
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
    error = _validate_rule(
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
    patch = _action_patch(assignment, rule, payload)
    latest = get_lark_task(assignment["workspace_root"], record_id=record_id)["task"]
    if latest != task:
        return _task_changed_error(
            assignment,
            definition,
            action_key=action_key,
            variant=variant,
            expected=task,
            current=latest,
        )
    result = upsert_lark_task(
        assignment["workspace_root"],
        record_id=record_id,
        task=patch,
        client_token=invocation_id,
    )
    return _success(
        assignment,
        definition,
        action_key=action_key,
        rule=rule,
        before=task,
        task=result["task"],
        already_applied=False,
    )


def _validate_rule(
    assignment: dict[str, Any],
    definition: dict[str, Any],
    *,
    action_key: str,
    rule: dict[str, Any],
    task: dict[str, Any] | None,
    payload: dict[str, Any],
    runtime_facts: set[str],
) -> dict[str, Any] | None:
    if not _rule_actor_allowed(assignment, definition, rule, task):
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
        if _blank(payload.get(field))
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
    if not _rule_values_allowed(rule, payload):
        allowed_values = _merged_allowed_values([rule])
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
    patch = _action_patch(assignment, rule, payload)
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
        for field in _required_task_fields(definition, rule, merged)
        if _blank(merged.get(field))
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
                f"任务 {_task_name(task)} 尚不满足该动作条件，缺少："
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
            message=f"任务 {_task_name(task)} 不满足前置条件：{guard_messages[missing_guards[0]]}",
            details={"missing_preconditions": missing_guards},
        )
    return None


def _matching_applied_rule(
    assignment: dict[str, Any],
    definition: dict[str, Any],
    rules: list[dict[str, Any]],
    task: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    for rule in rules:
        if not rule.get("to") or task.get("status") != rule["to"]:
            continue
        if not _rule_actor_allowed(assignment, definition, rule, task):
            continue
        if set(payload) - set(rule.get("writable_fields", [])):
            continue
        if not _rule_values_allowed(rule, payload):
            continue
        expected = _action_patch(assignment, rule, payload)
        if all(_same_value(task.get(field), value) for field, value in expected.items()):
            return rule
    if rules and all(rule.get("to") is None for rule in rules):
        for rule in rules:
            if not _rule_actor_allowed(assignment, definition, rule, task):
                continue
            if set(payload) - set(rule.get("writable_fields", [])):
                continue
            if all(_same_value(task.get(field), value) for field, value in payload.items()):
                return rule
    return None


def _action_patch(
    assignment: dict[str, Any],
    rule: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    patch = dict(rule.get("defaults", {}))
    patch.update(payload)
    patch.update(rule.get("fixed_fields", {}))
    for field, prefix in rule.get("field_prefixes", {}).items():
        value = patch.get(field)
        if not _blank(value) and not str(value).startswith(prefix):
            patch[field] = f"{prefix}{value}"
    for field, source in rule.get("actor_fields", {}).items():
        patch[field] = assignment[source]
    for field in rule.get("clear_fields", []):
        patch[field] = None
    if rule.get("to"):
        patch["status"] = rule["to"]
    return patch


def _rule_actor_allowed(
    assignment: dict[str, Any],
    definition: dict[str, Any],
    rule: dict[str, Any],
    task: dict[str, Any] | None,
) -> bool:
    if rule.get("roles") and assignment["role_key"] not in rule["roles"]:
        return False
    for actor in rule["actors"]:
        if actor == "coordinator" and assignment["role_key"] == definition["coordinator_role"]:
            return True
        if task is None:
            continue
        if actor == "task_role" and assignment["role_key"] == task.get("role"):
            return True
        if actor == "assigned_agent" and assignment["agent_id"] == task.get("agent_id"):
            return True
    return False


def _rule_role_applicable(
    assignment: dict[str, Any],
    definition: dict[str, Any],
    rule: dict[str, Any],
) -> bool:
    if rule.get("roles") and assignment["role_key"] not in rule["roles"]:
        return False
    return any(
        actor in {"task_role", "assigned_agent"}
        or actor == "coordinator" and assignment["role_key"] == definition["coordinator_role"]
        for actor in rule["actors"]
    )


def _runtime_action_actor_allowed(
    assignment: dict[str, Any],
    definition: dict[str, Any],
    action: dict[str, Any],
    task: dict[str, Any],
) -> bool:
    if action.get("roles") and assignment["role_key"] not in action["roles"]:
        return False
    return _rule_actor_allowed(
        assignment,
        definition,
        {"actors": action["actors"], "roles": action.get("roles", [])},
        task,
    )


def _runtime_action_role_applicable(
    assignment: dict[str, Any],
    definition: dict[str, Any],
    action: dict[str, Any],
) -> bool:
    if action.get("roles") and assignment["role_key"] not in action["roles"]:
        return False
    return any(
        actor in {"task_role", "assigned_agent"}
        or actor == "coordinator" and assignment["role_key"] == definition["coordinator_role"]
        for actor in action["actors"]
    )


def _runtime_action_contract(
    action_key: str,
    action: dict[str, Any],
) -> dict[str, Any]:
    return {
        "action": action_key,
        "tool": ACTION_TO_TOOL[action_key],
        "name": action["labels"]["zh-CN"],
        "states": action["states"],
        "required_fields": action.get("required_inputs", []),
        "required_task_fields": action.get("required_task_fields", []),
        "confirmation_required": bool(action.get("confirmation_required")),
        "runtime_facts": action.get("produces", []),
    }


def _contract_rule(
    definition: dict[str, Any],
    action_key: str,
    action: dict[str, Any],
    rule: dict[str, Any],
) -> dict[str, Any]:
    return {
        "key": rule["key"],
        "name": rule["labels"]["zh-CN"],
        "from": rule.get("from", []),
        "to": rule.get("to"),
        "required_fields": rule.get("required_inputs", []),
        "required_task_fields": _required_task_fields(definition, rule),
        "writable_fields": rule.get("writable_fields", []),
        "allowed_values": _rule_allowed_values(definition, rule),
        "confirmation_required": bool(action.get("confirmation_required")),
        "preconditions": rule.get("guards", []),
        "option": rule["key"] if action_key in VARIANT_ACTIONS else None,
    }


def _rule_values_allowed(rule: dict[str, Any], payload: dict[str, Any]) -> bool:
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
        if _blank(value) or value in allowed:
            continue
        invalid[field] = {
            "received": value,
            "allowed": sorted(allowed),
        }
    return invalid


def _rule_allowed_values(
    definition: dict[str, Any],
    rule: dict[str, Any],
) -> dict[str, list[str]]:
    options = {
        field: [item["key"] for item in items]
        for field, items in task_option_definitions(definition).items()
    }
    explicit = rule.get("field_values", {})
    return {
        field: list(explicit.get(field, options[field]))
        for field in rule.get("writable_fields", [])
        if field in explicit or field in options
    }


def _required_task_fields(
    definition: dict[str, Any],
    rule: dict[str, Any],
    task: dict[str, Any] | None = None,
) -> list[str]:
    fields = list(rule.get("required_task_fields", []))
    state_key = rule.get("to") or (task or {}).get("status")
    state = next(
        (
            state
            for state in definition["lifecycle"]["states"]
            if state["key"] == state_key
        ),
        None,
    )
    if state:
        fields.extend(state.get("required_fields", []))
    return list(dict.fromkeys(fields))


def _success(
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
            f"任务 {_task_name(task)} 已处于目标结果，无需重复修改。"
            if already_applied
            else f"任务 {_task_name(task)} 已完成动作：{rule['labels']['zh-CN']}。"
        ),
        "already_applied": already_applied,
        "transition": {
            "from": before.get("status") if before else None,
            "to": task.get("status"),
        },
        "task": task,
        "available_actions": available_task_actions(assignment, task),
    }


def _task_changed_error(
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
        if not _same_value(expected.get(field), current.get(field))
    })
    return _workflow_error(
        assignment,
        definition,
        action_key=action_key,
        variant=variant,
        task=current,
        category="conflict",
        code="task_changed",
        message=(
            f"任务 {_task_name(current)} 在 {ACTION_TO_TOOL[action_key]} 执行前已发生变化，"
            "本次操作未写入。请先调用 get_task 读取最新卡片，再根据返回的合法动作重试。"
        ),
        retryable=True,
        details={
            "changed_fields": changed_fields,
            "expected_state": expected.get("status"),
            "current_state": current.get("status"),
        },
    )


def _workflow_error(
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


def _input_error(
    assignment: dict[str, Any],
    *,
    action_key: str,
    code: str,
    message: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    return _workflow_error(
        assignment,
        _definition(assignment),
        action_key=action_key,
        category="validation",
        code=code,
        message=message,
        task=None,
        details=details,
    )


def _definition(assignment: dict[str, Any]) -> dict[str, Any]:
    return load_workflow_definition(str(assignment["workflow_key"]))


def _record_id(record_id: str) -> str:
    value = str(record_id or "").strip()
    if not value:
        raise ValueError("record_id is required")
    return value


def _provided_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in fields.items()
        if value is not None and value != ""
    }


def _merged_allowed_values(rules: list[dict[str, Any]]) -> dict[str, list[str]]:
    merged: dict[str, set[str]] = {}
    for rule in rules:
        for field, values in rule.get("field_values", {}).items():
            merged.setdefault(field, set()).update(values)
    return {field: sorted(values) for field, values in merged.items()}


def _task_name(task: dict[str, Any] | None) -> str:
    if not task:
        return "新任务"
    return str(task.get("task_id") or task.get("record_id") or "unknown")


def _blank(value: Any) -> bool:
    if isinstance(value, str):
        return not value.strip()
    return value is None or value == [] or value == {}


def _same_value(current: Any, expected: Any) -> bool:
    return _blank(current) and _blank(expected) or current == expected
