from __future__ import annotations

import sqlite3


ID = "014_workflow_definitions"


def apply(conn: sqlite3.Connection) -> None:
    for table, column, kind in (
        ("workflows", "display_name_zh", "TEXT"),
        ("workflows", "display_name_en", "TEXT"),
        ("workflows", "short_description_zh", "TEXT"),
        ("workflows", "short_description_en", "TEXT"),
        ("roles", "display_name_zh", "TEXT"),
        ("roles", "display_name_en", "TEXT"),
        ("roles", "description_zh", "TEXT"),
        ("roles", "description_en", "TEXT"),
        ("roles", "is_coordinator", "INTEGER NOT NULL DEFAULT 0 CHECK (is_coordinator IN (0, 1))"),
    ):
        add_column(conn, table, column, kind)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_types (
          id TEXT PRIMARY KEY,
          workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
          type_key TEXT NOT NULL,
          display_name TEXT NOT NULL,
          description TEXT,
          default_role_key TEXT NOT NULL,
          display_name_zh TEXT,
          display_name_en TEXT,
          description_zh TEXT,
          description_en TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(workflow_id, type_key)
        );
        """
    )
    conn.execute(
        """
        UPDATE workflows
        SET display_name_en = COALESCE(display_name_en, display_name),
            short_description_en = COALESCE(short_description_en, short_description)
        """
    )
    conn.execute(
        """
        UPDATE roles
        SET display_name_en = COALESCE(display_name_en, display_name),
            description_en = COALESCE(description_en, description)
        """
    )


def add_column(conn: sqlite3.Connection, table: str, column: str, kind: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
