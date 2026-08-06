from __future__ import annotations

from pathlib import Path
from typing import Any


LOCALES = ("zh-CN", "en")
TASK_FIELD_KEYS = frozenset({
    "title",
    "task_id",
    "status",
    "type",
    "priority",
    "role",
    "agent",
    "agent_id",
    "description",
    "context",
    "acceptance_criteria",
    "dependencies",
    "progress",
    "next_action",
    "result_evidence",
    "blocked_reason",
    "waiting_on",
    "delivery_mode",
    "target_branch",
    "base_sha",
    "candidate_sha",
    "verified_sha",
    "promoted_sha",
    "delivery_resources",
})
DELIVERY_MODES = (
    ("standard", {"zh-CN": "标准交付", "en": "Standard"}, "Gray", "Lighter"),
    ("repository", {"zh-CN": "仓库交付", "en": "Repository"}, "Blue", "Lighter"),
)
PRIORITIES = (
    ("P0", {"zh-CN": "P0", "en": "P0"}, "Red", "Light"),
    ("P1", {"zh-CN": "P1", "en": "P1"}, "Orange", "Light"),
    ("P2", {"zh-CN": "P2", "en": "P2"}, "Blue", "Lighter"),
    ("P3", {"zh-CN": "P3", "en": "P3"}, "Gray", "Lighter"),
)
WORKFLOW_ACTION_KEYS = frozenset({
    "create",
    "update",
    "route",
    "claim",
    "submit",
    "block",
    "review",
    "cancel",
})
WORKFLOW_ACTORS = frozenset({"coordinator", "task_role", "assigned_agent"})
WORKFLOW_DISPATCH_TARGETS = frozenset({"none", "task_role", "coordinator"})
WORKFLOW_GUARDS = frozenset({"executor_unavailable", "execution_stopped"})
WORKFLOW_GUARD_ACTIONS = {
    "executor_unavailable": frozenset({"route"}),
    "execution_stopped": frozenset({"cancel"}),
}
RUNTIME_ACTION_SPECS = {
    "stop_execution": {
        "required_inputs": {"reason"},
        "produces": {"execution_stopped"},
    },
}
ACTOR_FIELD_BINDINGS = {
    "agent": "agent_name",
    "agent_id": "agent_id",
}
DIRECTLY_WRITABLE_FIELDS = TASK_FIELD_KEYS - {"task_id", "status", "agent", "agent_id"}
CLEARABLE_FIELDS = TASK_FIELD_KEYS - {"task_id", "status", "title"}
WORKFLOW_DEFINITION_FIELDS = frozenset({
    "schema_version",
    "key",
    "labels",
    "short_descriptions",
    "coordinator_role",
    "roles",
    "task_types",
    "waiting_targets",
    "task_schema",
    "lifecycle",
    "runtime_actions",
})
ROLE_FIELDS = frozenset({"key", "labels", "descriptions", "allow_multiple"})
TASK_TYPE_FIELDS = frozenset({"key", "labels", "descriptions", "default_role"})
WAITING_TARGET_FIELDS = frozenset({"key", "labels", "color"})
TASK_SCHEMA_FIELDS = frozenset({"base", "task_id"})
TASK_ID_FIELDS = frozenset({"sequence_length"})
LIFECYCLE_FIELDS = frozenset({"initial_state", "terminal_states", "completion_states", "states", "actions"})
STATE_FIELDS = frozenset({
    "key",
    "labels",
    "color",
    "dispatch",
    "dispatch_instructions",
    "required_fields",
})
COLOR_FIELDS = frozenset({"hue", "lightness"})
ACTION_FIELDS = frozenset({"labels", "confirmation_required", "rules"})
RULE_FIELDS = frozenset({
    "key",
    "labels",
    "actors",
    "roles",
    "from",
    "to",
    "writable_fields",
    "required_inputs",
    "required_task_fields",
    "defaults",
    "fixed_fields",
    "clear_fields",
    "field_values",
    "field_prefixes",
    "actor_fields",
    "guards",
})
RUNTIME_ACTION_FIELDS = frozenset({
    "labels",
    "actors",
    "roles",
    "states",
    "required_inputs",
    "required_task_fields",
    "confirmation_required",
    "produces",
})


def validate_workflow_definition(definition: Any, path: Path) -> None:
    if not isinstance(definition, dict):
        raise ValueError(f"workflow definition must be an object: {path}")
    _reject_unknown_fields(definition, WORKFLOW_DEFINITION_FIELDS, str(path))
    _require_fields(definition, WORKFLOW_DEFINITION_FIELDS, str(path))
    if definition.get("schema_version") != 2:
        raise ValueError(f"unsupported workflow schema version: {path}")
    key = _required_text(definition, "key", path)
    if key != path.parent.name:
        raise ValueError(f"workflow key must match its directory name: {path}")
    validate_localized(definition.get("labels"), f"{key}.labels")
    validate_localized(definition.get("short_descriptions"), f"{key}.short_descriptions")

    roles = keyed_items(definition.get("roles"), f"{key}.roles")
    for role in roles.values():
        _reject_unknown_fields(role, ROLE_FIELDS, f"{key}.roles.{role['key']}")
        validate_localized(role.get("labels"), f"{key}.roles.{role['key']}.labels")
        validate_localized(role.get("descriptions"), f"{key}.roles.{role['key']}.descriptions")
        if not isinstance(role.get("allow_multiple"), bool):
            raise ValueError(f"{key}.roles.{role['key']}.allow_multiple must be boolean")

    coordinator = _required_text(definition, "coordinator_role", path)
    if coordinator not in roles:
        raise ValueError(f"unknown coordinator role {coordinator}: {path}")

    task_types = keyed_items(definition.get("task_types"), f"{key}.task_types")
    for task_type in task_types.values():
        _reject_unknown_fields(
            task_type,
            TASK_TYPE_FIELDS,
            f"{key}.task_types.{task_type['key']}",
        )
        validate_localized(task_type.get("labels"), f"{key}.task_types.{task_type['key']}.labels")
        validate_localized(task_type.get("descriptions"), f"{key}.task_types.{task_type['key']}.descriptions")
        if task_type.get("default_role") not in roles:
            raise ValueError(f"unknown default role for task type {task_type['key']}: {path}")

    waiting_targets = keyed_items(
        definition.get("waiting_targets"),
        f"{key}.waiting_targets",
    )
    for target in waiting_targets.values():
        name = f"{key}.waiting_targets.{target['key']}"
        _reject_unknown_fields(target, WAITING_TARGET_FIELDS, name)
        validate_localized(target.get("labels"), f"{name}.labels")
        _validate_color(target.get("color"), f"{name}.color")

    schema = definition.get("task_schema")
    if not isinstance(schema, dict) or schema.get("base") != "teamflow-task-v1":
        raise ValueError(f"{key}.task_schema.base must be teamflow-task-v1")
    _reject_unknown_fields(schema, TASK_SCHEMA_FIELDS, f"{key}.task_schema")
    task_id = schema.get("task_id")
    if not isinstance(task_id, dict):
        raise ValueError(f"{key}.task_schema.task_id must be an object")
    _reject_unknown_fields(task_id, TASK_ID_FIELDS, f"{key}.task_schema.task_id")
    length = task_id.get("sequence_length")
    if not isinstance(length, int) or not 1 <= length <= 9:
        raise ValueError(f"{key}.task_schema.task_id.sequence_length must be between 1 and 9")

    _validate_lifecycle(definition, path, roles, task_types, waiting_targets)
    _validate_runtime_actions(definition, roles)


def keyed_items(value: Any, name: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a non-empty list")
    result: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not item["key"].strip():
            raise ValueError(f"{name} contains an item without a valid key")
        key = item["key"]
        if key in result:
            raise ValueError(f"{name} contains duplicate key {key}")
        result[key] = item
    return result


def validate_localized(value: Any, name: str) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != set(LOCALES)
        or any(
            not isinstance(value.get(locale), str) or not value[locale].strip()
            for locale in LOCALES
        )
    ):
        raise ValueError(f"{name} must define non-empty zh-CN and en values")


def _validate_lifecycle(
    definition: dict[str, Any],
    path: Path,
    roles: dict[str, dict[str, Any]],
    task_types: dict[str, dict[str, Any]],
    waiting_targets: dict[str, dict[str, Any]],
) -> None:
    key = definition["key"]
    lifecycle = definition.get("lifecycle")
    if not isinstance(lifecycle, dict):
        raise ValueError(f"{key}.lifecycle must be an object")
    _reject_unknown_fields(lifecycle, LIFECYCLE_FIELDS, f"{key}.lifecycle")
    states = keyed_items(lifecycle.get("states"), f"{key}.lifecycle.states")
    for state in states.values():
        name = f"{key}.lifecycle.states.{state['key']}"
        _reject_unknown_fields(state, STATE_FIELDS, name)
        validate_localized(state.get("labels"), f"{name}.labels")
        _validate_color(state.get("color"), f"{name}.color")
        if state.get("dispatch") not in WORKFLOW_DISPATCH_TARGETS:
            raise ValueError(f"{name}.dispatch is invalid")
        instructions = state.get("dispatch_instructions")
        if state["dispatch"] == "none":
            if instructions is not None:
                validate_localized(instructions, f"{name}.dispatch_instructions")
        else:
            validate_localized(instructions, f"{name}.dispatch_instructions")
        _field_list(state.get("required_fields"), f"{name}.required_fields")

    initial_state = lifecycle.get("initial_state")
    if initial_state not in states:
        raise ValueError(f"{key}.lifecycle.initial_state is invalid")
    terminal_states = lifecycle.get("terminal_states")
    if (
        not isinstance(terminal_states, list)
        or not terminal_states
        or any(state not in states for state in terminal_states)
        or len(set(terminal_states)) != len(terminal_states)
    ):
        raise ValueError(f"{key}.lifecycle.terminal_states is invalid")
    completion_states = lifecycle.get("completion_states")
    if (
        not isinstance(completion_states, list)
        or not completion_states
        or any(state not in terminal_states for state in completion_states)
        or len(set(completion_states)) != len(completion_states)
    ):
        raise ValueError(
            f"{key}.lifecycle.completion_states must be a non-empty subset of terminal_states"
        )
    if initial_state in terminal_states:
        raise ValueError(f"{key}.lifecycle.initial_state cannot be terminal")
    for terminal_state in terminal_states:
        if states[terminal_state]["dispatch"] != "none":
            raise ValueError(f"{key}.lifecycle terminal state {terminal_state} cannot dispatch work")
    actions = lifecycle.get("actions")
    if not isinstance(actions, dict):
        raise ValueError(f"{key}.lifecycle.actions must be an object")
    action_keys = set(actions)
    if action_keys != WORKFLOW_ACTION_KEYS:
        missing = sorted(WORKFLOW_ACTION_KEYS - action_keys)
        extra = sorted(action_keys - WORKFLOW_ACTION_KEYS)
        raise ValueError(
            f"{key}.lifecycle.actions must define the TeamFlow action set"
            f" (missing={missing}, extra={extra})"
        )
    for action_key, action in actions.items():
        action_name = f"{key}.lifecycle.actions.{action_key}"
        if not isinstance(action, dict):
            raise ValueError(f"{action_name} must be an object")
        _reject_unknown_fields(action, ACTION_FIELDS, action_name)
        validate_localized(action.get("labels"), f"{action_name}.labels")
        if "confirmation_required" in action and not isinstance(action["confirmation_required"], bool):
            raise ValueError(f"{action_name}.confirmation_required must be boolean")
        if action.get("confirmation_required") and action_key != "cancel":
            raise ValueError(
                f"{action_name}.confirmation_required is only supported for cancel"
            )
        rules = keyed_items(action.get("rules"), f"{action_name}.rules")
        if action_key == "create" and len(rules) != 1:
            raise ValueError(f"{action_name} must define exactly one rule")
        for rule in rules.values():
            _validate_action_rule(
                action_key,
                rule,
                name=f"{action_name}.rules.{rule['key']}",
                states=states,
                roles=roles,
                task_types=task_types,
                waiting_targets=waiting_targets,
                initial_state=str(initial_state),
                terminal_states=set(terminal_states),
            )
    _validate_state_graph(
        key,
        states=set(states),
        actions=actions,
        initial_state=str(initial_state),
        terminal_states=set(terminal_states),
    )


def _validate_runtime_actions(
    definition: dict[str, Any],
    roles: dict[str, dict[str, Any]],
) -> None:
    key = definition["key"]
    states = {
        state["key"]
        for state in definition["lifecycle"]["states"]
    }
    actions = definition.get("runtime_actions", {})
    if not isinstance(actions, dict):
        raise ValueError(f"{key}.runtime_actions must be an object")
    for action_key, action in actions.items():
        name = f"{key}.runtime_actions.{action_key}"
        spec = RUNTIME_ACTION_SPECS.get(action_key)
        if spec is None or not isinstance(action, dict):
            raise ValueError(f"{name} is invalid")
        _reject_unknown_fields(action, RUNTIME_ACTION_FIELDS, name)
        validate_localized(action.get("labels"), f"{name}.labels")
        actors = action.get("actors")
        if (
            not isinstance(actors, list)
            or not actors
            or any(actor not in WORKFLOW_ACTORS for actor in actors)
            or len(set(actors)) != len(actors)
        ):
            raise ValueError(f"{name}.actors is invalid")
        scoped_roles = action.get("roles", [])
        if (
            not isinstance(scoped_roles, list)
            or any(role not in roles for role in scoped_roles)
            or len(set(scoped_roles)) != len(scoped_roles)
        ):
            raise ValueError(f"{name}.roles is invalid")
        action_states = action.get("states")
        if (
            not isinstance(action_states, list)
            or not action_states
            or any(state not in states for state in action_states)
            or len(set(action_states)) != len(action_states)
        ):
            raise ValueError(f"{name}.states is invalid")
        required_inputs = action.get("required_inputs", [])
        if (
            not isinstance(required_inputs, list)
            or any(not isinstance(field, str) or not field.strip() for field in required_inputs)
            or len(set(required_inputs)) != len(required_inputs)
            or set(required_inputs) != spec["required_inputs"]
        ):
            raise ValueError(f"{name}.required_inputs is invalid")
        _field_list(action.get("required_task_fields"), f"{name}.required_task_fields")
        if (
            "confirmation_required" in action
            and not isinstance(action["confirmation_required"], bool)
        ):
            raise ValueError(f"{name}.confirmation_required must be boolean")
        produces = action.get("produces", [])
        if (
            not isinstance(produces, list)
            or any(fact not in WORKFLOW_GUARDS for fact in produces)
            or len(set(produces)) != len(produces)
            or set(produces) != spec["produces"]
        ):
            raise ValueError(f"{name}.produces is invalid")


def _validate_action_rule(
    action_key: str,
    rule: dict[str, Any],
    *,
    name: str,
    states: dict[str, dict[str, Any]],
    roles: dict[str, dict[str, Any]],
    task_types: dict[str, dict[str, Any]],
    waiting_targets: dict[str, dict[str, Any]],
    initial_state: str,
    terminal_states: set[str],
) -> None:
    _reject_unknown_fields(rule, RULE_FIELDS, name)
    validate_localized(rule.get("labels"), f"{name}.labels")
    actors = rule.get("actors")
    if (
        not isinstance(actors, list)
        or not actors
        or any(not isinstance(actor, str) or actor not in WORKFLOW_ACTORS for actor in actors)
        or len(set(actors)) != len(actors)
    ):
        raise ValueError(f"{name}.actors is invalid")
    scoped_roles = rule.get("roles", [])
    if (
        not isinstance(scoped_roles, list)
        or any(not isinstance(role, str) or role not in roles for role in scoped_roles)
        or len(set(scoped_roles)) != len(scoped_roles)
    ):
        raise ValueError(f"{name}.roles is invalid")

    from_states = rule.get("from", [])
    if (
        not isinstance(from_states, list)
        or any(not isinstance(state, str) or state not in states for state in from_states)
        or len(set(from_states)) != len(from_states)
    ):
        raise ValueError(f"{name}.from is invalid")
    if set(from_states) & terminal_states:
        raise ValueError(f"{name} cannot leave a terminal state")
    if action_key == "create":
        if from_states or rule.get("to") != initial_state:
            raise ValueError(f"{name} must create the lifecycle initial state")
    elif action_key == "update":
        if not from_states or "to" in rule:
            raise ValueError(f"{name} must update existing non-terminal states without a transition")
    elif not from_states or rule.get("to") not in states:
        raise ValueError(f"{name} must define valid from and to states")

    writable = _field_list(rule.get("writable_fields"), f"{name}.writable_fields")
    if any(field not in DIRECTLY_WRITABLE_FIELDS for field in writable):
        raise ValueError(f"{name}.writable_fields contains a protected or unknown field")
    required_inputs = _field_list(rule.get("required_inputs"), f"{name}.required_inputs")
    if any(field not in writable for field in required_inputs):
        raise ValueError(f"{name}.required_inputs must be writable fields")
    _field_list(rule.get("required_task_fields"), f"{name}.required_task_fields")
    clear_fields = _field_list(rule.get("clear_fields"), f"{name}.clear_fields")
    if any(field not in CLEARABLE_FIELDS for field in clear_fields):
        raise ValueError(f"{name}.clear_fields contains a protected field")

    for mapping_name in ("defaults", "fixed_fields"):
        mapping = rule.get(mapping_name, {})
        if not isinstance(mapping, dict) or any(field not in TASK_FIELD_KEYS for field in mapping):
            raise ValueError(f"{name}.{mapping_name} contains an unknown field")
        if any(field not in DIRECTLY_WRITABLE_FIELDS for field in mapping):
            raise ValueError(f"{name}.{mapping_name} contains a protected field")
    field_prefixes = rule.get("field_prefixes", {})
    if (
        not isinstance(field_prefixes, dict)
        or any(field not in writable for field in field_prefixes)
        or any(
            not isinstance(prefix, str) or not prefix.strip()
            for prefix in field_prefixes.values()
        )
    ):
        raise ValueError(f"{name}.field_prefixes is invalid")
    actor_fields = rule.get("actor_fields", {})
    if (
        not isinstance(actor_fields, dict)
        or any(
            ACTOR_FIELD_BINDINGS.get(field) != source
            for field, source in actor_fields.items()
        )
    ):
        raise ValueError(f"{name}.actor_fields is invalid")
    option_values = {
        "status": set(states),
        "role": set(roles),
        "type": set(task_types),
        "priority": {item[0] for item in PRIORITIES},
        "waiting_on": set(waiting_targets),
    }
    for mapping_name in ("defaults", "fixed_fields"):
        for field, value in rule.get(mapping_name, {}).items():
            if field in option_values and value not in option_values[field]:
                raise ValueError(f"{name}.{mapping_name}.{field} is invalid")
    field_values = rule.get("field_values", {})
    if not isinstance(field_values, dict):
        raise ValueError(f"{name}.field_values must be an object")
    for field, values in field_values.items():
        if (
            field not in writable
            or not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) or not value for value in values)
        ):
            raise ValueError(f"{name}.field_values.{field} is invalid")
        if field in option_values and any(value not in option_values[field] for value in values):
            raise ValueError(f"{name}.field_values.{field} contains an unsupported option")
    guards = rule.get("guards", [])
    if (
        not isinstance(guards, list)
        or any(not isinstance(guard, str) or guard not in WORKFLOW_GUARDS for guard in guards)
        or len(set(guards)) != len(guards)
    ):
        raise ValueError(f"{name}.guards is invalid")
    unsupported_guards = [
        guard
        for guard in guards
        if action_key not in WORKFLOW_GUARD_ACTIONS[guard]
    ]
    if unsupported_guards:
        raise ValueError(
            f"{name}.guards are not supported by {action_key}: "
            f"{', '.join(unsupported_guards)}"
        )


def _field_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if (
        not isinstance(value, list)
        or any(not isinstance(field, str) or field not in TASK_FIELD_KEYS for field in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"{name} contains an unknown or duplicate field")
    return value


def _required_text(value: dict[str, Any], key: str, path: Path) -> str:
    text = value.get(key)
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{key} must be a non-empty string: {path}")
    return text


def _validate_color(value: Any, name: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must define hue and lightness")
    _reject_unknown_fields(value, COLOR_FIELDS, name)
    if (
        not isinstance(value.get("hue"), str)
        or not value["hue"].strip()
        or not isinstance(value.get("lightness"), str)
        or not value["lightness"].strip()
    ):
        raise ValueError(f"{name} must define hue and lightness")


def _validate_state_graph(
    key: str,
    *,
    states: set[str],
    actions: dict[str, Any],
    initial_state: str,
    terminal_states: set[str],
) -> None:
    edges = {state: set() for state in states}
    for action_key, action in actions.items():
        if action_key in {"create", "update"}:
            continue
        for rule in action["rules"]:
            destination = rule["to"]
            for source in rule["from"]:
                edges[source].add(destination)

    reachable = _reachable_states({initial_state}, edges)
    unreachable = sorted(states - reachable)
    if unreachable:
        raise ValueError(
            f"{key}.lifecycle contains states unreachable from "
            f"{initial_state}: {unreachable}"
        )

    reverse_edges = {state: set() for state in states}
    for source, destinations in edges.items():
        for destination in destinations:
            reverse_edges[destination].add(source)
    terminating = _reachable_states(terminal_states, reverse_edges)
    nonterminating = sorted((states - terminal_states) - terminating)
    if nonterminating:
        raise ValueError(
            f"{key}.lifecycle contains non-terminal states without a path "
            f"to a terminal state: {nonterminating}"
        )


def _reachable_states(
    roots: set[str],
    edges: dict[str, set[str]],
) -> set[str]:
    reachable = set(roots)
    pending = list(roots)
    while pending:
        state = pending.pop()
        for destination in edges[state]:
            if destination in reachable:
                continue
            reachable.add(destination)
            pending.append(destination)
    return reachable


def _reject_unknown_fields(
    value: dict[str, Any],
    allowed: frozenset[str],
    name: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{name} contains unsupported fields: {unknown}")


def _require_fields(
    value: dict[str, Any],
    required: frozenset[str],
    name: str,
) -> None:
    missing = sorted(required - set(value))
    if missing:
        raise ValueError(f"{name} is missing required fields: {missing}")
