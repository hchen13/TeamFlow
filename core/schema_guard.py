from __future__ import annotations

import sqlite3
from typing import Any, Iterable


class SchemaCompatibilityError(RuntimeError):
    """A database carries migrations that this build does not know about."""


def verify_installed_migrations(conn: sqlite3.Connection, migrations: Iterable[Any]) -> None:
    installed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'migrations'"
    ).fetchone()
    if not installed:
        return
    applied = {row[0] for row in conn.execute("SELECT id FROM migrations")}
    verify_migration_compatibility(conn, migrations, applied)


def verify_migration_compatibility(
    conn: sqlite3.Connection,
    migrations: Iterable[Any],
    applied: Iterable[str],
) -> None:
    supported = [migration.ID for migration in migrations]
    unknown = sorted(set(applied) - set(supported))
    if not unknown:
        return
    raise SchemaCompatibilityError(
        "TeamFlow database schema does not match this build.\n"
        f"database: {database_path(conn)}\n"
        f"unknown migrations: {', '.join(unknown)}\n"
        f"latest supported migration: {supported[-1]}\n"
        "Run the TeamFlow build that applied these migrations, or restore the database from "
        "a backup taken before they were applied. TeamFlow never downgrades a database."
    )


def database_path(conn: sqlite3.Connection) -> str:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return row[2] or ":memory:"
    return ":memory:"
