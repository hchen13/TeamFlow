from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .workflow_validation import (
    LOCALES,
    PRIORITIES,
    DELIVERY_MODES,
    TASK_FIELD_KEYS,
    validate_workflow_definition,
)

DEFINITIONS_DIR = Path(__file__).resolve().parents[1] / "workflows"
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
    ("delivery_mode", {"zh-CN": "交付方式", "en": "Delivery Mode"}, "select"),
    ("target_branch", {"zh-CN": "目标分支", "en": "Target Branch"}, "text"),
    ("base_sha", {"zh-CN": "基线 SHA", "en": "Base SHA"}, "text"),
    ("candidate_sha", {"zh-CN": "候选 SHA", "en": "Candidate SHA"}, "text"),
    ("verified_sha", {"zh-CN": "已验证 SHA", "en": "Verified SHA"}, "text"),
    ("promoted_sha", {"zh-CN": "已晋升 SHA", "en": "Promoted SHA"}, "text"),
    ("delivery_resources", {"zh-CN": "交付资源", "en": "Delivery Resources"}, "text"),
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


def workflow_definition_for_assignment(
    assignment: dict[str, Any],
) -> dict[str, Any]:
    return load_workflow_definition(str(assignment["workflow_key"]))


def task_name(task: dict[str, Any] | None) -> str:
    if not task:
        return "新任务"
    return str(task.get("task_id") or task.get("record_id") or "unknown")


def blank(value: Any) -> bool:
    if isinstance(value, str):
        return not value.strip()
    return value is None or value == [] or value == {}


def same_value(current: Any, expected: Any) -> bool:
    return blank(current) and blank(expected) or current == expected


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
        "waiting_on": _colored_definition_options(definition["waiting_targets"]),
        "delivery_mode": _fixed_options(DELIVERY_MODES),
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


def _colored_definition_options(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "key": item["key"],
            "labels": item["labels"],
            "hue": item["color"]["hue"],
            "lightness": item["color"]["lightness"],
        }
        for item in items
    ]


def _fixed_options(items: tuple[tuple[str, dict[str, str], str, str], ...]) -> list[dict[str, Any]]:
    return [
        {"key": key, "labels": labels, "hue": hue, "lightness": lightness}
        for key, labels, hue, lightness in items
    ]


def _locale(locale: str) -> str:
    if locale not in LOCALES:
        raise ValueError(f"unsupported workflow locale: {locale}")
    return locale
