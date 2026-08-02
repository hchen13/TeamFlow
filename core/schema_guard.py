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
    migrations = list(migrations)
    supported = [migration.ID for migration in migrations]
    applied = set(applied)
    ledger = applied - _pending_replacements(migrations, applied)
    # The ledger a build can continue from is exactly a prefix of its own migration sequence.
    # A gap means an id ran out of order or was removed, which no forward migration can repair.
    expected = supported[: len(ledger)]
    unknown = sorted(ledger - set(supported))
    missing = sorted(set(expected) - ledger)
    if not unknown and not missing:
        return
    raise SchemaCompatibilityError(
        "TeamFlow database schema does not match this build.\n"
        f"database: {database_path(conn)}\n"
        f"unknown migrations: {', '.join(unknown) or 'none'}\n"
        f"missing migrations before the applied ones: {', '.join(missing) or 'none'}\n"
        f"latest supported migration: {supported[-1]}\n"
        "Run the TeamFlow build that applied these migrations, or restore the database from "
        "a backup taken before they were applied. TeamFlow never downgrades a database."
    )


def _pending_replacements(migrations: list[Any], applied: set[str]) -> set[str]:
    # A migration that deletes historical ledger rows declares them in REPLACES. Until it runs,
    # those ids are a legitimate part of the ledger; once it has run they are unknown again.
    return {
        replaced
        for migration in migrations
        if migration.ID not in applied
        for replaced in getattr(migration, "REPLACES", ())
    }


def verified_commit(conn: sqlite3.Connection, migrations: Iterable[Any]) -> None:
    # The only sanctioned way to commit early. The ledger is re-read inside the write transaction
    # being committed, so work started under one schema can never be persisted onto another.
    verify_installed_migrations(conn, migrations)
    conn.commit()


def database_data_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA data_version").fetchone()[0])


def database_path(conn: sqlite3.Connection) -> str:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return row[2] or ":memory:"
    return ":memory:"
