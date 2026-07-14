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

STATUSES = (
    ("backlog", {"zh-CN": "待规划", "en": "Backlog"}, "Gray", "Lighter"),
    ("ready", {"zh-CN": "可执行", "en": "Ready"}, "Blue", "Lighter"),
    ("in_progress", {"zh-CN": "进行中", "en": "In Progress"}, "Orange", "Light"),
    ("review", {"zh-CN": "待评审", "en": "Review"}, "Purple", "Lighter"),
    ("blocked", {"zh-CN": "已阻塞", "en": "Blocked"}, "Red", "Lighter"),
    ("done", {"zh-CN": "已完成", "en": "Done"}, "Green", "Light"),
    ("canceled", {"zh-CN": "已取消", "en": "Canceled"}, "Gray", "Standard"),
)
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
    if definition.get("schema_version") != 1:
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
    required = schema.get("required_for_ready")
    if not isinstance(required, list) or any(item not in TASK_FIELD_KEYS for item in required):
        raise ValueError(f"{key}.task_schema.required_for_ready contains an unknown field")
    task_id = schema.get("task_id")
    if not isinstance(task_id, dict):
        raise ValueError(f"{key}.task_schema.task_id must be an object")
    length = task_id.get("sequence_length")
    if not isinstance(length, int) or not 1 <= length <= 9:
        raise ValueError(f"{key}.task_schema.task_id.sequence_length must be between 1 and 9")

    policies = definition.get("policies")
    if not isinstance(policies, dict):
        raise ValueError(f"{key}.policies must be an object")
    for policy in ("review_role", "stakeholder_escalation_role"):
        if policies.get(policy) not in roles:
            raise ValueError(f"unknown role in {key}.policies.{policy}")


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
        "status": _fixed_options(STATUSES),
        "type": _definition_options(definition["task_types"]),
        "priority": _fixed_options(PRIORITIES),
        "role": _definition_options(definition["roles"]),
        "waiting_on": _fixed_options(WAITING_ON),
    }


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
