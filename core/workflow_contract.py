from __future__ import annotations

from typing import Any

from .workflow import task_option_definitions, workflow_definition_for_assignment


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
    definition = workflow_definition_for_assignment(assignment)
    lifecycle = definition["lifecycle"]
    actions = []
    for action_key, action in lifecycle["actions"].items():
        options = [
            _contract_rule(definition, action_key, action, rule)
            for rule in action["rules"]
            if rule_role_applicable(assignment, definition, rule)
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


def available_task_actions(
    assignment: dict[str, Any],
    task: dict[str, Any],
    *,
    definition: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    definition = definition or workflow_definition_for_assignment(assignment)
    status = str(task.get("status") or "")
    available = []
    for action_key, action in definition["lifecycle"]["actions"].items():
        if action_key == "create":
            continue
        for rule in action["rules"]:
            if status not in rule.get("from", []):
                continue
            if not rule_actor_allowed(assignment, definition, rule, task):
                continue
            available.append({
                "action": action_key,
                "tool": ACTION_TO_TOOL[action_key],
                "option": rule["key"] if action_key in VARIANT_ACTIONS else None,
                "name": rule["labels"]["zh-CN"],
                "to": rule.get("to"),
                "required_fields": rule.get("required_inputs", []),
                "required_task_fields": required_task_fields(definition, rule, task),
                "writable_fields": rule.get("writable_fields", []),
                "allowed_values": _rule_allowed_values(definition, rule),
                "confirmation_required": bool(action.get("confirmation_required")),
                "preconditions": rule.get("guards", []),
            })
    for action_key, action in definition.get("runtime_actions", {}).items():
        if status not in action["states"]:
            continue
        if not runtime_action_actor_allowed(assignment, definition, action, task):
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


def rule_actor_allowed(
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


def rule_role_applicable(
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


def runtime_action_actor_allowed(
    assignment: dict[str, Any],
    definition: dict[str, Any],
    action: dict[str, Any],
    task: dict[str, Any],
) -> bool:
    if action.get("roles") and assignment["role_key"] not in action["roles"]:
        return False
    return rule_actor_allowed(
        assignment,
        definition,
        {"actors": action["actors"], "roles": action.get("roles", [])},
        task,
    )


def required_task_fields(
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
        "required_task_fields": required_task_fields(definition, rule),
        "writable_fields": rule.get("writable_fields", []),
        "allowed_values": _rule_allowed_values(definition, rule),
        "confirmation_required": bool(action.get("confirmation_required")),
        "preconditions": rule.get("guards", []),
        "option": rule["key"] if action_key in VARIANT_ACTIONS else None,
    }


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
