from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import resolve_workspace_paths
from core.db import SCHEMA_VERSION, init_workspace, inspect_workspace, now
from core.global_db import connect_global, global_database_path, record_lark_event, retry_lark_event
from core.migrations import MIGRATIONS
from core.schema_guard import SchemaCompatibilityError


ROOT = Path(__file__).resolve().parents[1]
UNKNOWN_MIGRATION = "999_unreleased_experiment"


def apply_unknown_migration(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO migrations (id, applied_at) VALUES (?, ?)",
            (UNKNOWN_MIGRATION, now()),
        )
    conn.close()


class SchemaGuardTest(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.home = tempfile.TemporaryDirectory(prefix="teamflow-home-", dir=ROOT / "tmp")
        self.home_env = patch.dict(os.environ, {"TEAMFLOW_HOME": self.home.name})
        self.home_env.start()
        self.temp = tempfile.TemporaryDirectory(prefix="schema-guard-", dir=ROOT / "tmp")
        self.workspace = self.temp.name

    def tearDown(self):
        self.temp.cleanup()
        self.home_env.stop()
        self.home.cleanup()

    def test_a_workspace_database_with_unknown_migrations_fails_fast(self):
        init_workspace(self.workspace)
        db_path = resolve_workspace_paths(self.workspace).db_path
        apply_unknown_migration(db_path)

        with self.assertRaises(SchemaCompatibilityError) as failure:
            inspect_workspace(self.workspace)

        message = str(failure.exception)
        self.assertIn(str(db_path), message)
        self.assertIn(UNKNOWN_MIGRATION, message)
        self.assertIn(SCHEMA_VERSION, message)
        self.assertIn("restore the database", message.lower())

    def test_the_guard_runs_before_any_business_write(self):
        init_workspace(self.workspace)
        db_path = resolve_workspace_paths(self.workspace).db_path
        apply_unknown_migration(db_path)
        with sqlite3.connect(db_path) as before:
            workspaces = before.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]

        with self.assertRaises(SchemaCompatibilityError):
            init_workspace(self.workspace, display_name="Renamed workspace")

        with sqlite3.connect(db_path) as after:
            self.assertEqual(after.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0], workspaces)
            self.assertEqual(
                after.execute(
                    "SELECT COUNT(*) FROM workspaces WHERE display_name = 'Renamed workspace'"
                ).fetchone()[0],
                0,
            )

    def test_the_global_database_is_guarded_on_every_connection(self):
        with connect_global():
            pass
        apply_unknown_migration(global_database_path())

        with self.assertRaises(SchemaCompatibilityError) as failure:
            with connect_global():
                pass

        message = str(failure.exception)
        self.assertIn(str(global_database_path()), message)
        self.assertIn(UNKNOWN_MIGRATION, message)

    def test_a_schema_mismatch_is_not_a_retryable_event(self):
        record_lark_event(
            event_id="event_schema",
            brand="feishu",
            app_id="cli_test",
            event_type="drive.file.bitable_record_changed_v1",
            file_token="bascnTest",
            table_id="tblTest",
            source_revision="1",
            payload={},
        )

        self.assertEqual(retry_lark_event("event_schema", ValueError("transient")), "retry")
        self.assertEqual(
            retry_lark_event("event_schema", SchemaCompatibilityError("schema mismatch")),
            "failed",
        )

    def test_a_database_behind_this_build_still_upgrades(self):
        with patch("core.db.MIGRATIONS", MIGRATIONS[:-1]):
            init_workspace(self.workspace)
            self.assertEqual(
                inspect_workspace(self.workspace)["schema_version"],
                MIGRATIONS[-2].ID,
            )

        self.assertEqual(inspect_workspace(self.workspace)["schema_version"], SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
