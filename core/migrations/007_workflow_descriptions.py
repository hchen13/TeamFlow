from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


ID = "007_workflow_descriptions"
DEFAULT_WORKFLOW_KEY = "software-development"


def apply(conn: sqlite3.Connection) -> None:
    add_column(conn, "workflows", "short_description", "TEXT")
    add_column(conn, "workflows", "description", "TEXT")
    add_column(conn, "roles", "description", "TEXT")
    seed_descriptions(conn)


def add_column(conn: sqlite3.Connection, table: str, column: str, kind: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def seed_descriptions(conn: sqlite3.Connection) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    workflow_id = "workflow_software_development"
    conn.execute(
        """
        UPDATE workflows
        SET short_description = ?,
            description = ?,
            updated_at = ?
        WHERE key = ?
        """,
        (
            "Plan, build, review, and verify software work with PM, TL, QA, and Design roles.",
            "Default workflow for product and engineering collaboration. PM owns scope and acceptance, TL turns scope into technical execution, Design shapes the user-facing experience, and QA verifies behavior before handoff.",
            timestamp,
            DEFAULT_WORKFLOW_KEY,
        ),
    )
    for role_key, description in (
        ("pm", "Owns scope, priority, acceptance criteria, and stakeholder alignment."),
        ("qa", "Verifies behavior, records evidence, and catches regressions before handoff."),
        ("tl", "Leads technical design, task breakdown, implementation quality, and code review."),
        ("design", "Shapes product interaction, visual direction, and user-facing copy before build."),
    ):
        conn.execute(
            """
            UPDATE roles
            SET description = ?,
                updated_at = ?
            WHERE workflow_id = ? AND role_key = ?
            """,
            (description, timestamp, workflow_id, role_key),
        )
