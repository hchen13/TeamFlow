from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .lark_events import LarkEventContext


QUOTED_LOG_FIELDS = {
    "agent",
    "app",
    "board",
    "reason",
    "socket",
    "table",
    "title",
    "workspace",
}


def style(text: str, code: str) -> str:
    if not sys.stdout.isatty() or "NO_COLOR" in os.environ:
        return text
    return f"\033[{code}m{text}\033[0m"


def local_timestamp(value: str | None = None) -> str:
    moment = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value
        else datetime.now().astimezone()
    )
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def event_source(brand: str, event_type: str) -> str:
    source = "LARK" if brand == "larksuite" else "FEISHU"
    if event_type == "drive.file.bitable_record_changed_v1":
        change = "RECORD CHANGE" if brand == "larksuite" else "记录变更"
    elif event_type == "drive.file.bitable_field_changed_v1":
        change = "FIELD CHANGE" if brand == "larksuite" else "字段变更"
    else:
        change = event_type
    return f"{source} WEBSOCKET {change}"


def emit_log(
    label: str,
    *,
    context: LarkEventContext | None = None,
    timestamp: str | None = None,
    fields: dict[str, Any] | None = None,
) -> None:
    parts = [style(local_timestamp(timestamp), "2")]
    if context:
        workspace = context.workspace_name or Path(context.workspace_root).name
        namespace = f"[{workspace} @{context.workflow_key or '-'}]"
        parts.append(style(namespace, "1"))
    parts.append(label)
    for key, value in (fields or {}).items():
        if value is None or value == "":
            continue
        rendered = (
            json.dumps(str(value), ensure_ascii=False)
            if key in QUOTED_LOG_FIELDS
            else str(value)
        )
        parts.append(f"{key}={rendered}")
    print(" ".join(parts), flush=True)


def task_change(event_types: list[str]) -> str:
    if "task_created" in event_types:
        return "created"
    if "task_deleted" in event_types:
        return "deleted"
    return "updated" if event_types else "unchanged"


def styled_task_change(change: str | None) -> str | None:
    if not change:
        return None
    return style(
        change,
        {
            "created": "1;32",
            "updated": "1;33",
            "deleted": "1;31",
            "unchanged": "2",
        }.get(change, "2"),
    )


def log_received(
    context: LarkEventContext,
    item: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    task = summary.get("task") or {}
    task_id = task.get("task_id")
    emit_log(
        f"{style(event_source(context.brand, str(item['event_type'])), '1;36')} RECEIVED",
        context=context,
        timestamp=str(item["received_at"]),
        fields={
            "event": item["event_id"],
            "board": context.board_name or context.file_token,
            "table": context.table_name or context.table_id,
            "task": task_id,
            "record": None if task_id else summary.get("record_id"),
            "title": task.get("title"),
            "change": styled_task_change(summary.get("change")),
            "status": task.get("status"),
        },
    )


def log_dispatch(
    context: LarkEventContext,
    result: str,
    *,
    event_id: str | None,
    task: dict[str, Any],
    record_id: str | None = None,
    target: str | None = None,
    agent: str | None = None,
    session: str | None = None,
    reason: str | None = None,
    attempt: int | None = None,
    turn: str | None = None,
    transport: str | None = None,
) -> None:
    styles = {
        "not-required": "2",
        "waiting": "1;33",
        "started": "1;34",
        "retry": "1;33",
        "reconciling": "1;33",
        "recovered": "1;32",
        "succeeded": "1;32",
        "failed": "1;31",
    }
    task_id = task.get("task_id")
    emit_log(
        style(f"DISPATCH {result.upper()}", styles[result]),
        context=context,
        fields={
            "event": event_id,
            "task": task_id,
            "record": None if task_id else (record_id or task.get("record_id")),
            "target": target,
            "agent": agent,
            "session": session,
            "turn": turn,
            "transport": transport,
            "attempt": attempt,
            "reason": reason,
        },
    )
