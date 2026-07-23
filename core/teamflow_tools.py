from __future__ import annotations

import json
from typing import Any

from .config import resolve_workspace_paths
from .db import bootstrap_workspace, connect
from .lark_board import get_lark_task, upsert_lark_task


def list_available_tasks(assignment: dict[str, Any]) -> dict[str, Any]:
    paths = resolve_workspace_paths(assignment["workspace_root"])
    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        rows = conn.execute(
            "SELECT snapshot_json FROM lark_task_state WHERE status = 'ready' ORDER BY updated_at, record_id"
        ).fetchall()
    tasks = []
    for row in rows:
        task = json.loads(row["snapshot_json"])
        if task.get("role") != assignment["role_key"]:
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
    record_id = record_id.strip()
    if not record_id:
        raise ValueError("record_id is required")
    return get_lark_task(assignment["workspace_root"], record_id=record_id)


def claim_task(assignment: dict[str, Any], *, record_id: str) -> dict[str, Any]:
    record_id = record_id.strip()
    if not record_id:
        raise ValueError("record_id is required")
    current = get_lark_task(assignment["workspace_root"], record_id=record_id)["task"]
    if current.get("status") != "ready":
        raise ValueError(
            f"task {current.get('task_id') or record_id} is {current.get('status') or 'unknown'}, not ready"
        )
    if current.get("role") != assignment["role_key"]:
        raise ValueError(
            f"task belongs to {current.get('role') or 'no role'}, but this agent is {assignment['role_key']}"
        )
    result = upsert_lark_task(
        assignment["workspace_root"],
        record_id=record_id,
        task={
            "status": "in_progress",
            "agent": assignment["agent_name"],
            "agent_id": assignment["agent_id"],
        },
    )
    return {"ok": True, "claimed": True, "task": result["task"]}
