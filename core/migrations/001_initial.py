from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


ID = "001_initial"
DEFAULT_WORKFLOW_KEY = "software-development"


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS workspaces (
          id TEXT PRIMARY KEY,
          root_path TEXT NOT NULL UNIQUE,
          display_name TEXT,
          current_workflow_id TEXT REFERENCES workflows(id),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS lark_identities (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          auth_mode TEXT NOT NULL CHECK (auth_mode IN ('bot', 'user')),
          app_id TEXT,
          app_name TEXT,
          app_avatar_url TEXT,
          app_name_synced_at TEXT,
          is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
          app_secret TEXT,
          access_token TEXT,
          refresh_token TEXT,
          access_status TEXT NOT NULL DEFAULT 'unverified',
          last_verified_at TEXT,
          last_error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(workspace_id, auth_mode, app_id)
        );

        CREATE TABLE IF NOT EXISTS lark_boards (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL UNIQUE REFERENCES workspaces(id) ON DELETE CASCADE,
          identity_id TEXT REFERENCES lark_identities(id) ON DELETE SET NULL,
          base_url TEXT,
          base_token TEXT,
          table_id TEXT,
          view_id TEXT,
          access_status TEXT NOT NULL DEFAULT 'unverified',
          last_verified_at TEXT,
          last_error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS workflows (
          id TEXT PRIMARY KEY,
          key TEXT NOT NULL UNIQUE,
          display_name TEXT NOT NULL,
          short_description TEXT,
          description TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS roles (
          id TEXT PRIMARY KEY,
          workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
          role_key TEXT NOT NULL,
          display_name TEXT NOT NULL,
          description TEXT,
          allow_multiple INTEGER NOT NULL CHECK (allow_multiple IN (0, 1)),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(workflow_id, role_key)
        );

        CREATE TABLE IF NOT EXISTS agents (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          workflow_id TEXT REFERENCES workflows(id),
          role_id TEXT REFERENCES roles(id),
          role_key TEXT NOT NULL,
          harness_type TEXT NOT NULL,
          session_id TEXT NOT NULL,
          display_name TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(workspace_id, role_id, harness_type, session_id)
        );
        """
    )
    ensure_agent_columns(conn)
    seed_default_workflow(conn)
    seed_general_workflow(conn)


def ensure_agent_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(agents)")}
    if "workflow_id" not in columns:
        conn.execute("ALTER TABLE agents ADD COLUMN workflow_id TEXT")
    if "role_id" not in columns:
        conn.execute("ALTER TABLE agents ADD COLUMN role_id TEXT")


def seed_default_workflow(conn: sqlite3.Connection) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    workflow_id = "workflow_software_development"
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
            DEFAULT_WORKFLOW_KEY,
            "Software development",
            "Plan, build, review, and verify software work with PM, TL, QA, and Design roles.",
            "Default workflow for product and engineering collaboration. PM owns scope and acceptance, TL turns scope into technical execution, Design shapes the user-facing experience, and QA verifies behavior before handoff.",
            timestamp,
            timestamp,
        ),
    )
    for role_key, display_name, description, allow_multiple in (
        ("pm", "PM", "Owns scope, priority, acceptance criteria, and stakeholder alignment.", False),
        ("qa", "QA", "Verifies behavior, records evidence, and catches regressions before handoff.", True),
        ("tl", "Technical Lead", "Leads technical design, task breakdown, implementation quality, and code review.", True),
        ("design", "Design", "Shapes product interaction, visual direction, and user-facing copy before build.", True),
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
            (f"role_{DEFAULT_WORKFLOW_KEY}_{role_key}", workflow_id, role_key, display_name, description, int(allow_multiple), timestamp, timestamp),
        )
    conn.execute(
        """
        UPDATE agents
        SET workflow_id = ?,
            role_id = (
              SELECT id FROM roles
              WHERE workflow_id = ? AND role_key = agents.role_key
            )
        WHERE workflow_id IS NULL OR role_id IS NULL
        """,
        (workflow_id, workflow_id),
    )


def seed_general_workflow(conn: sqlite3.Connection) -> None:
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
