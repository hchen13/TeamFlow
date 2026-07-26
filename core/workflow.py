from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFINITIONS_DIR = Path(__file__).resolve().parents[1] / "skills"
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
})

PRIORITIES = (
    ("P0", {"zh-CN": "P0", "en": "P0"}, "Red", "Light"),
    ("P1", {"zh-CN": "P1", "en": "P1"}, "Orange", "Light"),
    ("P2", {"zh-CN": "P2", "en": "P2"}, "Blue", "Lighter"),
    ("P3", {"zh-CN": "P3", "en": "P3"}, "Gray", "Lighter"),
)
WAITING_ON = (
    ("pm", {"zh-CN": "PM", "en": "PM"}, "Blue", "Lighter"),
    ("stakeholder", {"zh-CN": "项目决策人", "en": "Stakeholder"}, "Purple", "Lighter"),
)
FIELD_DEFINITIONS = (
    ("task_id", {"zh-CN": "任务 ID", "en": "Task ID"}, "auto_number"),
    ("status", {"zh-CN": "状态", "en": "Status"}, "select"),
    ("type", {"zh-CN": "任务类型", "en": "Type"}, "select"),
    ("priority", {"zh-CN": "优先级", "en": "Priority"}, "select"),
    ("role", {"zh-CN": "负责人", "en": "Owner"}, "select"),
    ("agent", {"zh-CN": "执行智能体", "en": "Agent"}, "text"),
    ("agent_id", {"zh-CN": "执行智能体 ID", "en": "Agent ID"}, "text"),
    ("description", {"zh-CN": "任务描述", "en": "Description"}, "text"),
    ("context", {"zh-CN": "补充上下文", "en": "Context"}, "text"),
    ("acceptance_criteria", {"zh-CN": "验收标准", "en": "Acceptance Criteria"}, "text"),
    ("dependencies", {"zh-CN": "依赖任务", "en": "Dependencies"}, "text"),
    ("progress", {"zh-CN": "当前进展", "en": "Progress"}, "text"),
    ("next_action", {"zh-CN": "下一步", "en": "Next Action"}, "text"),
    ("result_evidence", {"zh-CN": "结果与证据", "en": "Result / Evidence"}, "text"),
    ("blocked_reason", {"zh-CN": "阻塞原因", "en": "Blocked Reason"}, "text"),
    ("waiting_on", {"zh-CN": "等待对象", "en": "Waiting On"}, "select"),
)
OPTION_COLORS = (
    ("Purple", "Lighter"),
    ("Blue", "Lighter"),
    ("Green", "Lighter"),
    ("Orange", "Lighter"),
    ("Wathet", "Lighter"),
    ("Carmine", "Lighter"),
    ("Gray", "Lighter"),
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


def load_workflow_definitions(root: Path | None = None) -> dict[str, dict[str, Any]]:
    definitions: dict[str, dict[str, Any]] = {}
    for path in sorted((root or DEFINITIONS_DIR).glob("*/workflow.json")):
        try:
            definition = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid workflow definition {path}: {error}") from error
        validate_workflow_definition(definition, path)
        key = definition["key"]
        if key in definitions:
            raise ValueError(f"duplicate workflow definition: {key}")
        definitions[key] = definition
    if not definitions:
        raise ValueError(f"no workflow definitions found in {root or DEFINITIONS_DIR}")
    return definitions


def load_workflow_definition(key: str) -> dict[str, Any]:
    definitions = load_workflow_definitions()
    try:
        return definitions[key]
    except KeyError as error:
        raise ValueError(f"workflow definition is not installed: {key}") from error


def validate_workflow_definition(definition: Any, path: Path) -> None:
    if not isinstance(definition, dict):
        raise ValueError(f"workflow definition must be an object: {path}")
    if definition.get("schema_version") != 2:
        raise ValueError(f"unsupported workflow schema version: {path}")
    key = _required_text(definition, "key", path)
    if key != path.parent.name:
        raise ValueError(f"workflow key must match its directory name: {path}")
    _localized(definition.get("labels"), f"{key}.labels")
    _localized(definition.get("short_descriptions"), f"{key}.short_descriptions")

    roles = _keyed_items(definition.get("roles"), f"{key}.roles")
    for role in roles.values():
        _localized(role.get("labels"), f"{key}.roles.{role['key']}.labels")
        _localized(role.get("descriptions"), f"{key}.roles.{role['key']}.descriptions")
        if not isinstance(role.get("allow_multiple"), bool):
            raise ValueError(f"{key}.roles.{role['key']}.allow_multiple must be boolean")

    coordinator = _required_text(definition, "coordinator_role", path)
    if coordinator not in roles:
        raise ValueError(f"unknown coordinator role {coordinator}: {path}")

    task_types = _keyed_items(definition.get("task_types"), f"{key}.task_types")
    for task_type in task_types.values():
        _localized(task_type.get("labels"), f"{key}.task_types.{task_type['key']}.labels")
        _localized(task_type.get("descriptions"), f"{key}.task_types.{task_type['key']}.descriptions")
        if task_type.get("default_role") not in roles:
            raise ValueError(f"unknown default role for task type {task_type['key']}: {path}")

    schema = definition.get("task_schema")
    if not isinstance(schema, dict) or schema.get("base") != "teamflow-task-v1":
        raise ValueError(f"{key}.task_schema.base must be teamflow-task-v1")
    task_id = schema.get("task_id")
    if not isinstance(task_id, dict):
        raise ValueError(f"{key}.task_schema.task_id must be an object")
    length = task_id.get("sequence_length")
    if not isinstance(length, int) or not 1 <= length <= 9:
        raise ValueError(f"{key}.task_schema.task_id.sequence_length must be between 1 and 9")

    _validate_lifecycle(definition, path, roles, task_types)
    _validate_runtime_actions(definition, roles)

    policies = definition.get("policies")
    if not isinstance(policies, dict):
        raise ValueError(f"{key}.policies must be an object")
    for policy in ("review_role", "stakeholder_escalation_role"):
        if policies.get(policy) not in roles:
            raise ValueError(f"unknown role in {key}.policies.{policy}")


def _validate_lifecycle(
    definition: dict[str, Any],
    path: Path,
    roles: dict[str, dict[str, Any]],
    task_types: dict[str, dict[str, Any]],
) -> None:
    key = definition["key"]
    lifecycle = definition.get("lifecycle")
    if not isinstance(lifecycle, dict):
        raise ValueError(f"{key}.lifecycle must be an object")
    states = _keyed_items(lifecycle.get("states"), f"{key}.lifecycle.states")
    for state in states.values():
        name = f"{key}.lifecycle.states.{state['key']}"
        _localized(state.get("labels"), f"{name}.labels")
        color = state.get("color")
        if (
            not isinstance(color, dict)
            or not isinstance(color.get("hue"), str)
            or not color["hue"].strip()
            or not isinstance(color.get("lightness"), str)
            or not color["lightness"].strip()
        ):
            raise ValueError(f"{name}.color must define hue and lightness")
        if state.get("dispatch") not in WORKFLOW_DISPATCH_TARGETS:
            raise ValueError(f"{name}.dispatch is invalid")
        instructions = state.get("dispatch_instructions")
        if state["dispatch"] == "none":
            if instructions is not None:
                _localized(instructions, f"{name}.dispatch_instructions")
        else:
            _localized(instructions, f"{name}.dispatch_instructions")
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
        _localized(action.get("labels"), f"{action_name}.labels")
        if "confirmation_required" in action and not isinstance(action["confirmation_required"], bool):
            raise ValueError(f"{action_name}.confirmation_required must be boolean")
        rules = _keyed_items(action.get("rules"), f"{action_name}.rules")
        for rule in rules.values():
            _validate_action_rule(
                action_key,
                rule,
                name=f"{action_name}.rules.{rule['key']}",
                states=states,
                roles=roles,
                task_types=task_types,
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
        _localized(action.get("labels"), f"{name}.labels")
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
    initial_state: str,
    terminal_states: set[str],
) -> None:
    _localized(rule.get("labels"), f"{name}.labels")
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
        allowed_fields = DIRECTLY_WRITABLE_FIELDS
        if any(field not in allowed_fields for field in mapping):
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
        "waiting_on": {item[0] for item in WAITING_ON},
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


def sync_workflow_definitions(conn: sqlite3.Connection, definitions: dict[str, dict[str, Any]]) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    for key, definition in definitions.items():
        labels = definition["labels"]
        descriptions = definition["short_descriptions"]
        row = conn.execute("SELECT id FROM workflows WHERE key = ?", (key,)).fetchone()
        workflow_id = row["id"] if row else f"workflow_{key.replace('-', '_')}"
        conn.execute(
            """
            INSERT INTO workflows
              (id, key, display_name, short_description, description,
               display_name_zh, display_name_en, short_description_zh, short_description_en,
               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              display_name = excluded.display_name,
              short_description = excluded.short_description,
              description = excluded.description,
              display_name_zh = excluded.display_name_zh,
              display_name_en = excluded.display_name_en,
              short_description_zh = excluded.short_description_zh,
              short_description_en = excluded.short_description_en,
              updated_at = excluded.updated_at
            """,
            (
                workflow_id,
                key,
                labels["en"],
                descriptions["en"],
                descriptions["en"],
                labels["zh-CN"],
                labels["en"],
                descriptions["zh-CN"],
                descriptions["en"],
                timestamp,
                timestamp,
            ),
        )

        for role in definition["roles"]:
            role_row = conn.execute(
                "SELECT id FROM roles WHERE workflow_id = ? AND role_key = ?",
                (workflow_id, role["key"]),
            ).fetchone()
            role_id = role_row["id"] if role_row else f"role_{key}_{role['key']}"
            conn.execute(
                """
                INSERT INTO roles
                  (id, workflow_id, role_key, display_name, description, allow_multiple,
                   display_name_zh, display_name_en, description_zh, description_en, is_coordinator,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id, role_key) DO UPDATE SET
                  display_name = excluded.display_name,
                  description = excluded.description,
                  allow_multiple = excluded.allow_multiple,
                  display_name_zh = excluded.display_name_zh,
                  display_name_en = excluded.display_name_en,
                  description_zh = excluded.description_zh,
                  description_en = excluded.description_en,
                  is_coordinator = excluded.is_coordinator,
                  updated_at = excluded.updated_at
                """,
                (
                    role_id,
                    workflow_id,
                    role["key"],
                    role["labels"]["en"],
                    role["descriptions"]["en"],
                    int(role["allow_multiple"]),
                    role["labels"]["zh-CN"],
                    role["labels"]["en"],
                    role["descriptions"]["zh-CN"],
                    role["descriptions"]["en"],
                    int(role["key"] == definition["coordinator_role"]),
                    timestamp,
                    timestamp,
                ),
            )

        for task_type in definition["task_types"]:
            type_row = conn.execute(
                "SELECT id FROM task_types WHERE workflow_id = ? AND type_key = ?",
                (workflow_id, task_type["key"]),
            ).fetchone()
            type_id = type_row["id"] if type_row else f"task_type_{key}_{task_type['key']}"
            conn.execute(
                """
                INSERT INTO task_types
                  (id, workflow_id, type_key, display_name, description, default_role_key,
                   display_name_zh, display_name_en, description_zh, description_en,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id, type_key) DO UPDATE SET
                  display_name = excluded.display_name,
                  description = excluded.description,
                  default_role_key = excluded.default_role_key,
                  display_name_zh = excluded.display_name_zh,
                  display_name_en = excluded.display_name_en,
                  description_zh = excluded.description_zh,
                  description_en = excluded.description_en,
                  updated_at = excluded.updated_at
                """,
                (
                    type_id,
                    workflow_id,
                    task_type["key"],
                    task_type["labels"]["en"],
                    task_type["descriptions"]["en"],
                    task_type["default_role"],
                    task_type["labels"]["zh-CN"],
                    task_type["labels"]["en"],
                    task_type["descriptions"]["zh-CN"],
                    task_type["descriptions"]["en"],
                    timestamp,
                    timestamp,
                ),
            )


def task_field_specs(
    definition: dict[str, Any],
    locale: str,
    *,
    task_prefix: str | None = None,
) -> dict[str, dict[str, Any]]:
    locale = _locale(locale)
    options = task_option_definitions(definition)
    specs: dict[str, dict[str, Any]] = {}
    for key, labels, field_type in FIELD_DEFINITIONS:
        spec: dict[str, Any] = {"name": labels[locale], "type": field_type}
        if key == "task_id" and task_prefix:
            spec["style"] = {
                "rules": [
                    {"type": "text", "text": f"{task_prefix}-"},
                    {"type": "incremental_number", "length": definition["task_schema"]["task_id"]["sequence_length"]},
                ]
            }
        elif key in options:
            spec["multiple"] = False
            spec["options"] = [
                {"name": item["labels"][locale], "hue": item["hue"], "lightness": item["lightness"]}
                for item in options[key]
            ]
        specs[key] = spec
    return specs


def task_field_aliases() -> dict[str, tuple[str, ...]]:
    aliases = {key: tuple(dict.fromkeys(labels.values())) for key, labels, _ in FIELD_DEFINITIONS}
    aliases["role"] = (*aliases["role"], "Role")
    return aliases


def task_option_maps(definition: dict[str, Any], locale: str) -> dict[str, dict[str, str]]:
    locale = _locale(locale)
    return {
        field: {item["key"]: item["labels"][locale] for item in items}
        for field, items in task_option_definitions(definition).items()
    }


def task_option_aliases(definition: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {
        field: {
            label: item["key"]
            for item in items
            for label in item["labels"].values()
        }
        for field, items in task_option_definitions(definition).items()
    }


def task_option_definitions(definition: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        "status": task_state_definitions(definition),
        "type": _definition_options(definition["task_types"]),
        "priority": _fixed_options(PRIORITIES),
        "role": _definition_options(definition["roles"]),
        "waiting_on": _fixed_options(WAITING_ON),
    }


def task_state_definitions(definition: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "key": state["key"],
            "labels": state["labels"],
            "hue": state["color"]["hue"],
            "lightness": state["color"]["lightness"],
        }
        for state in definition["lifecycle"]["states"]
    ]


def _definition_options(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    options = []
    for index, item in enumerate(items):
        hue, lightness = OPTION_COLORS[index % len(OPTION_COLORS)]
        options.append({"key": item["key"], "labels": item["labels"], "hue": hue, "lightness": lightness})
    return options


def _fixed_options(items: tuple[tuple[str, dict[str, str], str, str], ...]) -> list[dict[str, Any]]:
    return [
        {"key": key, "labels": labels, "hue": hue, "lightness": lightness}
        for key, labels, hue, lightness in items
    ]


def _required_text(value: dict[str, Any], key: str, path: Path) -> str:
    text = value.get(key)
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{key} is required: {path}")
    return text


def _localized(value: Any, name: str) -> None:
    if not isinstance(value, dict) or any(not isinstance(value.get(locale), str) or not value[locale].strip() for locale in LOCALES):
        raise ValueError(f"{name} must define non-empty zh-CN and en values")


def _keyed_items(value: Any, name: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a non-empty list")
    items: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not item["key"].strip():
            raise ValueError(f"{name} contains an invalid item")
        if item["key"] in items:
            raise ValueError(f"{name} contains duplicate key {item['key']}")
        items[item["key"]] = item
    return items


def _locale(locale: str) -> str:
    if locale not in LOCALES:
        raise ValueError(f"unsupported workflow locale: {locale}")
    return locale
