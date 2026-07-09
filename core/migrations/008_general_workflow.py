from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


ID = "008_general_workflow"


def apply(conn: sqlite3.Connection) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    workflow_key = "general-task"
    workflow_id = "workflow_general_task"
    conn.execute(
        """
        INSERT INTO workflows (id, key, display_name, short_description, description, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE
        SET display_name = excluded.display_name,
            short_description = excluded.short_description,
            description = excluded.description,
            updated_at = excluded.updated_at
        """,
        (
            workflow_id,
            workflow_key,
            "General task",
            "Coordinate ordinary work with one owner, executors, and reviewers.",
            "Lightweight workflow for non-software tasks. Owner keeps the goal and acceptance clear, Executors complete the work, and Reviewers check quality before the task is considered done.",
            timestamp,
            timestamp,
        ),
    )
    for role_key, display_name, description, allow_multiple in (
        ("owner", "Owner", "Owns the goal, priority, decision making, and final acceptance.", False),
        ("executor", "Executor", "Carries out the task, reports progress, and resolves assigned work.", True),
        ("reviewer", "Reviewer", "Checks the result, catches risks, and confirms the output is ready.", True),
    ):
        conn.execute(
            """
            INSERT INTO roles (id, workflow_id, role_key, display_name, description, allow_multiple, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workflow_id, role_key) DO UPDATE
            SET display_name = excluded.display_name,
                description = excluded.description,
                allow_multiple = excluded.allow_multiple,
                updated_at = excluded.updated_at
            """,
            (f"role_{workflow_key}_{role_key}", workflow_id, role_key, display_name, description, int(allow_multiple), timestamp, timestamp),
        )
