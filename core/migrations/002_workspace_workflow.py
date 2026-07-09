from __future__ import annotations

import sqlite3


ID = "002_workspace_current_workflow"
DEFAULT_WORKFLOW_KEY = "software-development"


def apply(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(workspaces)")}
    if "current_workflow_id" not in columns:
        conn.execute("ALTER TABLE workspaces ADD COLUMN current_workflow_id TEXT")
    workflow = conn.execute("SELECT id FROM workflows WHERE key = ?", (DEFAULT_WORKFLOW_KEY,)).fetchone()
    if workflow:
        conn.execute("UPDATE workspaces SET current_workflow_id = COALESCE(current_workflow_id, ?)", (workflow["id"],))
        conn.execute(
            "UPDATE roles SET display_name = ? WHERE workflow_id = ? AND role_key = ?",
            ("Technical Lead", workflow["id"], "tl"),
        )
