from __future__ import annotations

import os
import queue
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.config import resolve_workspace_paths
from core.daemon_monitor import DaemonMonitor
from core.event_runtime import EventRuntime
from core.db import SCHEMA_VERSION, connect, init_workspace, inspect_workspace, now
from core.global_db import (
    EVENT_RETRY_WINDOW,
    SCHEMA_RETRY_DELAY,
    connect_global,
    due_lark_event_ids,
    global_database_path,
    record_lark_event,
    register_workspace,
    retry_lark_event,
)
from core.lark_worker_runtime import LarkWorkerRuntime
from core.migrations import MIGRATIONS
from core.schema_guard import (
    SchemaCompatibilityError,
    verified_commit,
    verify_migration_compatibility,
)
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

    def test_a_ledger_with_a_gap_is_rejected_even_though_every_id_is_known(self):
        init_workspace(self.workspace)
        db_path = resolve_workspace_paths(self.workspace).db_path
        conn = sqlite3.connect(db_path)
        with conn:
            conn.execute("DELETE FROM migrations WHERE id = ?", (MIGRATIONS[5].ID,))
        conn.close()

        with self.assertRaises(SchemaCompatibilityError) as failure:
            inspect_workspace(self.workspace)

        message = str(failure.exception)
        self.assertIn(MIGRATIONS[5].ID, message)
        self.assertIn("missing migrations", message)

    def test_ids_a_pending_migration_removes_are_tolerated_only_until_it_runs(self):
        replaced = next(migration for migration in MIGRATIONS if getattr(migration, "REPLACES", ()))
        supported = [migration.ID for migration in MIGRATIONS]
        older = supported[: supported.index(replaced.ID)]
        conn = sqlite3.connect(":memory:")
        try:
            verify_migration_compatibility(conn, MIGRATIONS, [*older, *replaced.REPLACES])

            with self.assertRaises(SchemaCompatibilityError) as failure:
                verify_migration_compatibility(
                    conn,
                    MIGRATIONS,
                    [*older, replaced.ID, *replaced.REPLACES],
                )
            for legacy in replaced.REPLACES:
                self.assertIn(legacy, str(failure.exception))
        finally:
            conn.close()

    def test_a_migration_applied_while_a_connection_works_rolls_that_work_back(self):
        init_workspace(self.workspace)
        db_path = resolve_workspace_paths(self.workspace).db_path

        with self.assertRaises(SchemaCompatibilityError):
            with connect(db_path) as conn:
                apply_unknown_migration(db_path)
                conn.execute("UPDATE workspaces SET display_name = 'raced'")

        with sqlite3.connect(db_path) as after:
            self.assertEqual(
                after.execute(
                    "SELECT COUNT(*) FROM workspaces WHERE display_name = 'raced'"
                ).fetchone()[0],
                0,
            )

    def consume_until_fatal(self, error: BaseException) -> tuple[dict[str, str], list[dict], bool]:
        failure: dict[str, str] = {}
        stopping = threading.Event()
        logs: list[dict] = []
        fatal = threading.Event()
        events: queue.Queue = queue.Queue()
        events.put("tick")

        def resolve(name):
            if name == "due_lark_event_ids":
                def raise_error():
                    raise error
                return raise_error
            if name == "emit_log":
                return lambda message, **fields: logs.append({"message": message, **fields})
            if name == "style":
                return lambda message, _: message
            raise AssertionError(f"unexpected resolve: {name}")

        runtime = LarkWorkerRuntime(
            mp=None,
            event_queue=events,
            workers={},
            routes={},
            stopping=stopping,
            routes_ready=threading.Event(),
            app_key=lambda context: "",
            publish=lambda *args: None,
            process_event=lambda *args: None,
            stop_worker=lambda worker: None,
            resolve=resolve,
            consumer_failure=failure,
            on_fatal=fatal.set,
        )
        runtime.routes_ready.set()
        # The real daemon runs this loop on its own thread; an exception there is exactly the
        # failure that used to disappear, so the test has to observe the thread actually ending.
        thread = threading.Thread(target=runtime.consume_events, name="test-consumer")
        thread.start()
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive(), "the consumer thread must terminate")
        self.assertTrue(stopping.is_set())
        self.assertTrue(fatal.wait(1), "the daemon must be asked to shut down")
        return failure, logs, True

    def health_of(self, failure: dict[str, str]) -> dict:
        return DaemonMonitor(
            routes={},
            workers={},
            active_sessions=set(),
            stopping=threading.Event(),
            sync_lock=threading.RLock(),
            app_key=lambda context: "",
            consumer_failure=failure,
            resolve=lambda name: (lambda: {}),
        ).status()

    def test_a_global_schema_mismatch_stops_the_consumer_and_the_health_report(self):
        failure, logs, _ = self.consume_until_fatal(
            SchemaCompatibilityError("global ledger mismatch\ndetails follow")
        )

        self.assertIn("global ledger mismatch", failure["error"])
        self.assertIn("SchemaCompatibilityError", failure["error"])
        self.assertEqual(logs[0]["message"], "LISTENER FATAL")
        self.assertEqual(logs[0]["fields"]["type"], "SchemaCompatibilityError")

        status = self.health_of(failure)
        self.assertIs(status["healthy"], False)
        self.assertIn("global ledger mismatch", status["consumer_error"])

    def test_any_consumer_exception_is_fatal_and_visible(self):
        for error in (
            sqlite3.DatabaseError("database disk image is malformed"),
            RuntimeError("dictionary changed size during iteration"),
        ):
            with self.subTest(error=type(error).__name__):
                failure, logs, _ = self.consume_until_fatal(error)

                self.assertIn(type(error).__name__, failure["error"])
                self.assertIn(str(error), failure["error"])
                self.assertEqual(logs[0]["fields"]["type"], type(error).__name__)
                self.assertIs(self.health_of(failure)["healthy"], False)

    def test_the_global_database_rolls_back_work_after_a_concurrent_migration(self):
        register_workspace("/tmp/teamflow-schema-guard")

        with self.assertRaises(SchemaCompatibilityError):
            with connect_global() as conn:
                apply_unknown_migration(global_database_path())
                conn.execute("UPDATE workspaces SET enabled = 1")

        with sqlite3.connect(global_database_path()) as after:
            self.assertEqual(after.execute("SELECT enabled FROM workspaces").fetchone()[0], 0)

    def test_an_early_commit_cannot_outrun_the_compatibility_check(self):
        init_workspace(self.workspace)
        db_path = resolve_workspace_paths(self.workspace).db_path

        with self.assertRaises(SchemaCompatibilityError):
            with connect(db_path) as conn:
                apply_unknown_migration(db_path)
                conn.execute("UPDATE workspaces SET display_name = 'early'")
                verified_commit(conn, MIGRATIONS)

        with sqlite3.connect(db_path) as after:
            self.assertEqual(
                after.execute(
                    "SELECT COUNT(*) FROM workspaces WHERE display_name = 'early'"
                ).fetchone()[0],
                0,
            )

    def test_an_event_blocked_by_a_workspace_mismatch_is_delivered_after_the_fix(self):
        context = SimpleNamespace(
            workspace_root=self.workspace,
            workspace_name="schema-guard",
            workflow_key="software-development",
            app_id="cli_test",
            brand="feishu",
            app_name="Test app",
            public=lambda: {"file_token": "bascnTest", "table_id": "tblTest"},
        )
        attempts: list[str] = []

        def process_workspace_event(_context, _payload):
            attempts.append("attempt")
            if len(attempts) == 1:
                raise SchemaCompatibilityError("workspace ledger mismatch")
            return [{"task": {}, "record_id": "recTest"}]

        runtime = EventRuntime(
            sync_lock=threading.RLock(),
            routes={self.workspace: context},
            workers={},
            verifying_workspaces=set(),
            probe_records={},
            delivery_wakeup=threading.Event(),
            get_task=lambda *args, **kwargs: {},
            list_tasks=lambda *args, **kwargs: {},
            log_received=lambda *args, **kwargs: None,
            log_dispatch=lambda *args, **kwargs: None,
        )
        runtime.process_workspace_event = process_workspace_event
        runtime.consume_workspace_task_events = lambda *args, **kwargs: None

        record_lark_event(
            event_id="event_recovery",
            brand="feishu",
            app_id="cli_test",
            event_type="drive.file.bitable_record_changed_v1",
            file_token="bascnTest",
            table_id="tblTest",
            source_revision="1",
            payload={"event": {"file_token": "bascnTest", "table_id": "tblTest"}},
        )
        register_workspace(self.workspace, enabled=True)

        with patch("core.event_runtime.event_matches_board", return_value=True):
            runtime.process_event("event_recovery")
            self.assertEqual(self.event_status("event_recovery"), "retry")
            self.assertEqual(due_lark_event_ids(), [])

            # The database was restored, so the event that was held back becomes due again and is
            # delivered without ever having been discarded.
            self.clear_backoff("event_recovery")
            due = due_lark_event_ids()
            self.assertEqual(due, ["event_recovery"])
            runtime.process_event(due[0])

        self.assertEqual(attempts, ["attempt", "attempt"])
        self.assertEqual(self.event_status("event_recovery"), "processed")

    def event_status(self, event_id: str) -> str:
        with connect_global() as conn:
            return str(
                conn.execute(
                    "SELECT status FROM lark_event_inbox WHERE event_id = ?", (event_id,)
                ).fetchone()[0]
            )

    def clear_backoff(self, event_id: str) -> None:
        with connect_global() as conn:
            conn.execute(
                "UPDATE lark_event_inbox SET next_attempt_at = NULL WHERE event_id = ?",
                (event_id,),
            )

    def test_no_module_commits_without_the_verified_helper(self):
        offenders = [
            f"{path.name}:{number}"
            for path in sorted((ROOT / "core").glob("*.py"))
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if (".commit()" in line or 'execute("COMMIT' in line)
            and path.name != "schema_guard.py"
        ]

        self.assertEqual(offenders, [], "commit only through schema_guard.verified_commit")

    def test_a_workspace_schema_mismatch_keeps_the_event_recoverable(self):
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
            "retry",
        )

        with connect_global() as conn:
            row = conn.execute(
                "SELECT status, next_attempt_at, processed_at FROM lark_event_inbox WHERE event_id = ?",
                ("event_schema",),
            ).fetchone()
            conn.execute(
                "UPDATE lark_event_inbox SET received_at = ? WHERE event_id = ?",
                ((datetime.now(timezone.utc) - EVENT_RETRY_WINDOW * 2).isoformat(), "event_schema"),
            )

        self.assertEqual(row["status"], "retry")
        self.assertIsNone(row["processed_at"])
        backoff = datetime.fromisoformat(row["next_attempt_at"]) - datetime.now(timezone.utc)
        self.assertGreater(backoff.total_seconds(), 0)
        self.assertLessEqual(backoff.total_seconds(), SCHEMA_RETRY_DELAY)

        # An unrelated failure past the retry window is still given up on, but a schema mismatch
        # stays recoverable so a restored database can still deliver the board change.
        self.assertEqual(retry_lark_event("event_schema", ValueError("transient")), "failed")
        self.assertEqual(
            retry_lark_event("event_schema", SchemaCompatibilityError("schema mismatch")),
            "retry",
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
