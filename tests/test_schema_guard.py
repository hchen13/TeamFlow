from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.config import resolve_workspace_paths
from core.db import SCHEMA_VERSION, init_workspace, inspect_workspace, now
from core.global_db import connect_global, global_database_path, record_lark_event, retry_lark_event
from core.migrations import MIGRATIONS
from core.schema_guard import SchemaCompatibilityError
from core.task_delivery_store import (
    finish_task_delivery,
    processing_task_delivery_sessions_for_workspace,
)


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

    def test_a_mutating_entry_point_that_skips_bootstrap_still_fails_fast(self):
        init_workspace(self.workspace)
        db_path = resolve_workspace_paths(self.workspace).db_path
        with sqlite3.connect(db_path) as seed:
            seed.execute(
                """
                INSERT INTO task_event_deliveries
                  (id, event_key, agent_id, assignment_revision, harness_type,
                   session_id, prompt, status, created_at)
                VALUES (1, 'event_guard', 'agent_guard', 1, 'codex', 'session_guard', 'prompt', 'processing', ?)
                """,
                (now(),),
            )
        apply_unknown_migration(db_path)

        with self.assertRaises(SchemaCompatibilityError) as failure:
            finish_task_delivery(
                SimpleNamespace(db_path=db_path),
                delivery_id=1,
                result={"ok": True},
            )

        self.assertIn(str(db_path), str(failure.exception))
        with sqlite3.connect(db_path) as after:
            self.assertEqual(
                after.execute(
                    "SELECT status, completed_at, delivered_at FROM task_event_deliveries WHERE id = 1"
                ).fetchone(),
                ("processing", None, None),
            )

    def test_a_rejected_connection_is_closed(self):
        init_workspace(self.workspace)
        apply_unknown_migration(resolve_workspace_paths(self.workspace).db_path)
        opened: list[sqlite3.Connection] = []
        real_connect = sqlite3.connect

        with patch("core.db.sqlite3.connect", lambda *a, **kw: opened.append(real_connect(*a, **kw)) or opened[-1]):
            with self.assertRaises(SchemaCompatibilityError):
                inspect_workspace(self.workspace)

        self.assertTrue(opened)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[-1].execute("SELECT 1")

    def test_every_workspace_connection_goes_through_the_guarded_boundary(self):
        openers = {
            path.name: sorted(
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if "sqlite3.connect" in line
            )
            for path in sorted((ROOT / "core").glob("*.py"))
            if "sqlite3.connect" in path.read_text(encoding="utf-8")
        }

        self.assertEqual(set(openers), {"db.py", "global_db.py", "codex_rollout.py"})
        self.assertTrue(all("mode=ro" in line for line in openers["codex_rollout.py"]))

    def test_a_reader_outside_core_db_is_guarded_by_the_connection_boundary(self):
        init_workspace(self.workspace)
        db_path = resolve_workspace_paths(self.workspace).db_path
        self.assertEqual(processing_task_delivery_sessions_for_workspace(self.workspace), set())
        apply_unknown_migration(db_path)

        with self.assertRaises(SchemaCompatibilityError) as failure:
            processing_task_delivery_sessions_for_workspace(self.workspace)

        self.assertIn(UNKNOWN_MIGRATION, str(failure.exception))

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
