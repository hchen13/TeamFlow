from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from core.agent_runtime import agent_context
from core.config import resolve_workspace_paths
from core.daemon import (
    DaemonServer,
    TeamFlowDaemon,
    _daemon_request,
    _style,
    _styled_task_change,
    register_workspace,
    registered_workspaces,
    run_daemon,
)
from core.db import connect, configure_lark_board, configure_lark_identity, init_workspace, now, update_agent
from core.global_db import (
    claim_lark_event,
    cleanup_lark_events,
    finish_lark_event,
    lark_event_counts,
    record_lark_event,
    recover_lark_events,
    retry_lark_event,
)
from core.lark_events import (
    LarkEventContext,
    event_matches_board,
    event_record_actions,
    event_record_ids,
    lark_event_metadata,
    lark_listener_details,
    listen_lark_board_events,
    run_lark_app_worker,
    save_listener_result,
    save_task_snapshot,
    subscribe_lark_board_events,
    verify_lark_board_listener,
)
from core.task_dispatch import (
    claim_task_deliveries,
    finish_task_delivery,
    mark_task_delivery_turn_started,
    prepare_agent_catchup_deliveries,
    prepare_task_deliveries,
)
from scripts.teamflow import cmd_verify_lark_user_identity


ROOT = Path(__file__).resolve().parents[1]


class LarkEventsTest(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.home = tempfile.TemporaryDirectory(prefix="teamflow-home-", dir=ROOT / "tmp")
        self.home_env = patch.dict(os.environ, {"TEAMFLOW_HOME": self.home.name})
        self.home_env.start()
        self.temp = tempfile.TemporaryDirectory(prefix="lark-events-", dir=ROOT / "tmp")
        self.workspace = self.temp.name
        init_workspace(self.workspace)
        with patch("core.db.fetch_lark_app_info", return_value=("Test app", None, None)):
            configured = configure_lark_identity(
                self.workspace,
                app_id="cli_test",
                app_secret="secret",
                domain="feishu",
            )
        configure_lark_board(
            self.workspace,
            board_url="https://example.feishu.cn/base/bascnTest?table=tblTest",
        )
        self.identity_id = configured["lark_identity_id"]
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            conn.execute("UPDATE lark_boards SET primary_identity_id = ?", (self.identity_id,))

    def tearDown(self):
        self.temp.cleanup()
        self.home_env.stop()
        self.home.cleanup()

    def context(self) -> LarkEventContext:
        return LarkEventContext(
            workspace_root=self.workspace,
            db_path=str(resolve_workspace_paths(self.workspace).db_path),
            identity_id=self.identity_id,
            identity_name="Test app",
            app_id="cli_test",
            app_name="Test app",
            app_secret="secret",
            auth_mode="bot",
            user_open_id="",
            board_url="https://example.feishu.cn/base/bascnTest?table=tblTest",
            file_token="bascnTest",
            table_id="tblTest",
            brand="feishu",
            workspace_name="test-workspace",
            workflow_key="software-development",
            board_name="Project board",
            table_name="Tasks",
        )

    def test_subscribes_configured_board_once(self):
        client = Mock()
        client.file_events_subscribed.return_value = False
        with patch("core.lark_events.context_client", return_value=client):
            result = subscribe_lark_board_events(self.workspace)

        self.assertFalse(result["already_subscribed"])
        self.assertEqual(result["file_token"], "bascnTest")
        client.subscribe_file_events.assert_called_once_with()

    def test_matches_only_the_configured_table(self):
        context = {"file_token": "bascnTest", "table_id": "tblTest"}
        self.assertTrue(event_matches_board(
            {"event": {"file_token": "bascnTest", "table_id": "tblTest"}},
            context,
        ))
        self.assertFalse(event_matches_board(
            {"event": {"file_token": "bascnTest", "table_id": "tblOther"}},
            context,
        ))
        self.assertEqual(
            event_record_ids({"event": {"action_list": [{"record_id": "recAdded"}, {"record_id": "recEdited"}]}}),
            {"recAdded", "recEdited"},
        )

    def test_extracts_stable_event_metadata_and_actions(self):
        payload = {
            "header": {"event_id": "evtOne", "event_type": "drive.file.bitable_record_changed_v1"},
            "event": {
                "file_token": "bascnTest",
                "table_id": "tblTest",
                "revision": 41,
                "action_list": [{"record_id": "recOne", "action": "record_edited"}],
            },
        }

        self.assertEqual(lark_event_metadata(payload)["source_revision"], "41")
        self.assertEqual(event_record_actions(payload), {"recOne": "record_edited"})

    def test_listener_details_include_readable_names(self):
        client = Mock()
        client.get_base.return_value = {"name": "Project board"}
        client.list_tables.return_value = [{"table_id": "tblTest", "table_name": "Tasks"}]
        with patch("core.lark_events.context_client", return_value=client):
            details = lark_listener_details(self.context())

        self.assertEqual(details["board"]["name"], "Project board")
        self.assertEqual(details["board"]["table_name"], "Tasks")
        self.assertEqual(details["identity"]["name"], "Test app")
        self.assertEqual(details["app"]["name"], "Test app")

    def test_failed_listener_probe_keeps_the_selected_manager_identity(self):
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            conn.execute("UPDATE lark_boards SET primary_identity_id = NULL")

        save_listener_result(self.workspace, self.identity_id, {
            "ok": False,
            "status": "failed",
            "last_verified_at": "2026-07-22T04:00:00+00:00",
            "failure_kind": "event_not_received",
            "last_error": "the app did not receive the Bitable record change event",
        })

        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            board = conn.execute(
                "SELECT primary_identity_id, listener_status, listener_failure_kind FROM lark_boards"
            ).fetchone()

        self.assertEqual(board["primary_identity_id"], self.identity_id)
        self.assertEqual(board["listener_status"], "failed")
        self.assertEqual(board["listener_failure_kind"], "event_not_received")

    def test_app_worker_is_ready_only_after_receive_loop_starts(self):
        ready = Mock()

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                asyncio.run(self._receive_message_loop())

            async def _receive_message_loop(self):
                self.receiving = True

        with patch("core.lark_events.lark.ws.Client", Client):
            run_lark_app_worker(self.context(), emit=Mock(), ready=ready)

        ready.assert_called_once_with()

    def test_daemon_probe_uses_its_existing_event_stream(self):
        runtime = TeamFlowDaemon()
        context = self.context()
        client = Mock()

        def create_record(table_id, fields):
            runtime.publish(
                runtime.app_key(context),
                {"event": {"file_token": context.file_token, "table_id": table_id, "record_id": "recTest"}},
            )
            return {"record_id": "recTest"}

        client.upsert_record.side_effect = create_record
        with patch("core.daemon.lark_event_context", return_value=context), patch.object(
            runtime, "_ensure_app"
        ), patch("core.daemon.ensure_lark_board_subscription", return_value=True), patch(
            "core.daemon.context_client", return_value=client
        ), patch("core.daemon.save_listener_result") as save_result:
            result = runtime.verify_workspace(self.workspace)
        self.assertNotIn(self.workspace, runtime.routes)
        runtime.close()

        self.assertTrue(result["ok"])
        client.delete_record.assert_called_once_with("tblTest", "recTest")
        save_result.assert_called_once()

    def test_daemon_probe_accepts_the_cleanup_event(self):
        runtime = TeamFlowDaemon()
        context = self.context()
        client = Mock()
        client.upsert_record.return_value = {"record_id": "recTest"}

        def delete_record(table_id, record_id):
            runtime.publish(
                runtime.app_key(context),
                {"event": {"file_token": context.file_token, "table_id": table_id, "record_id": record_id}},
            )

        client.delete_record.side_effect = delete_record
        with patch("core.daemon.lark_event_context", return_value=context), patch.object(
            runtime, "_ensure_app"
        ), patch("core.daemon.ensure_lark_board_subscription", return_value=True), patch(
            "core.daemon.context_client", return_value=client
        ), patch("core.daemon.save_listener_result"):
            result = runtime.verify_workspace(self.workspace)
        runtime.close()

        self.assertTrue(result["ok"])

    def test_daemon_probe_retries_when_one_event_pair_is_missing(self):
        runtime = TeamFlowDaemon()
        context = self.context()
        client = Mock()
        client.upsert_record.side_effect = [{"record_id": "recOne"}, {"record_id": "recTwo"}]
        with patch("core.daemon.lark_event_context", return_value=context), patch.object(
            runtime, "_ensure_app"
        ), patch("core.daemon.ensure_lark_board_subscription", return_value=True), patch(
            "core.daemon.context_client", return_value=client
        ), patch.object(runtime, "wait_for_records", side_effect=[False, True]) as wait, patch(
            "core.daemon.save_listener_result"
        ):
            result = runtime.verify_workspace(self.workspace)
        runtime.close()

        self.assertTrue(result["ok"])
        self.assertEqual(client.upsert_record.call_count, 2)
        self.assertEqual(wait.call_count, 2)

    def test_daemon_reuses_the_live_worker_for_an_app(self):
        runtime = TeamFlowDaemon()
        context = self.context()
        process = Mock()
        process.is_alive.return_value = True
        runtime.workers[runtime.app_key(context)] = {
            "context": context,
            "credentials": (context.app_id, context.app_secret, context.brand),
            "process": process,
            "ready": Mock(),
            "errors": Mock(),
        }
        with patch.object(runtime.mp, "Process") as process_type:
            runtime._ensure_app(context)
        runtime.workers.clear()
        runtime.close()

        process_type.assert_not_called()

    def test_daemon_stops_an_app_after_the_last_workspace_moves(self):
        runtime = TeamFlowDaemon()
        previous = self.context()
        replacement = LarkEventContext(**{
            **previous.__dict__,
            "app_id": "cli_replacement",
            "app_name": "Replacement",
            "app_secret": "replacement-secret",
        })
        previous_worker = {"process": Mock(), "errors": Mock()}
        runtime.routes[self.workspace] = previous
        runtime.workers[runtime.app_key(previous)] = previous_worker
        with patch("core.daemon.lark_event_context", return_value=replacement), patch.object(
            runtime, "_ensure_app"
        ), patch("core.daemon.ensure_lark_board_subscription", return_value=True), patch.object(
            runtime, "_stop_worker"
        ) as stop_worker:
            runtime.sync_workspace(self.workspace, reconcile=False)
        runtime.workers.clear()
        runtime.close()

        stop_worker.assert_called_once_with(previous_worker)

    def test_failed_initial_reconciliation_does_not_commit_the_route(self):
        runtime = TeamFlowDaemon()
        context = self.context()
        worker = {"process": Mock(), "errors": Mock()}
        runtime.workers[runtime.app_key(context)] = worker
        with patch("core.daemon.lark_event_context", return_value=context), patch.object(
            runtime, "_ensure_app"
        ), patch("core.daemon.ensure_lark_board_subscription", return_value=True), patch.object(
            runtime, "_reconcile_workspace", side_effect=ValueError("reconciliation failed")
        ), patch.object(runtime, "_stop_worker") as stop_worker:
            with self.assertRaisesRegex(ValueError, "reconciliation failed"):
                runtime.sync_workspace(self.workspace)
        runtime.workers.clear()
        runtime.close()

        self.assertNotIn(self.workspace, runtime.routes)
        stop_worker.assert_called_once_with(worker)

    def test_global_database_tracks_workspace_paths(self):
        with tempfile.TemporaryDirectory(prefix="teamflow-home-", dir=ROOT / "tmp") as home, patch.dict(
            os.environ, {"TEAMFLOW_HOME": home}
        ):
            register_workspace(self.workspace)
            register_workspace(self.workspace)

            self.assertEqual(registered_workspaces(), [str(Path(self.workspace).resolve())])
            self.assertEqual(registered_workspaces(enabled_only=True), [])
            register_workspace(self.workspace, enabled=True)
            self.assertEqual(registered_workspaces(enabled_only=True), [str(Path(self.workspace).resolve())])
            self.assertTrue((Path(home) / "teamflow.db").exists())
            self.assertFalse((Path(home) / "registry.db").exists())

    def test_global_migration_keeps_preexisting_workspaces_enabled(self):
        database = Path(self.home.name) / "teamflow.db"
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE workspaces (root_path TEXT PRIMARY KEY, updated_at TEXT NOT NULL)")
            conn.execute("INSERT INTO workspaces VALUES (?, ?)", (self.workspace, "2026-01-01T00:00:00+00:00"))

        self.assertEqual(registered_workspaces(enabled_only=True), [self.workspace])
        with sqlite3.connect(database) as conn:
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lark_event_inbox'"
            ).fetchone())

    def test_global_inbox_deduplicates_delivery_ids(self):
        payload = {
            "header": {"event_id": "evtOne", "event_type": "drive.file.bitable_record_changed_v1"},
            "event": {"file_token": "bascnTest", "table_id": "tblTest", "revision": 1},
        }
        values = {
            "event_id": "evtOne",
            "brand": "feishu",
            "app_id": "cli_test",
            "event_type": "drive.file.bitable_record_changed_v1",
            "file_token": "bascnTest",
            "table_id": "tblTest",
            "source_revision": "1",
            "payload": payload,
        }

        self.assertTrue(record_lark_event(**values))
        self.assertFalse(record_lark_event(**values))
        claimed = claim_lark_event("evtOne")
        self.assertEqual(claimed["payload"], payload)
        recover_lark_events()
        self.assertIsNotNone(claim_lark_event("evtOne"))
        finish_lark_event("evtOne")
        self.assertEqual(lark_event_counts(), {"processed": 1})
        with sqlite3.connect(Path(self.home.name) / "teamflow.db") as conn:
            conn.execute("UPDATE lark_event_inbox SET processed_at = '2000-01-01T00:00:00+00:00'")
        self.assertEqual(cleanup_lark_events(), 1)
        self.assertEqual(lark_event_counts(), {})

    def test_global_inbox_retries_for_one_day_before_failing(self):
        self.assertTrue(record_lark_event(
            event_id="evtRetry",
            brand="feishu",
            app_id="cli_test",
            event_type="drive.file.bitable_record_changed_v1",
            file_token="bascnTest",
            table_id="tblTest",
            source_revision="1",
            payload={},
        ))
        self.assertIsNotNone(claim_lark_event("evtRetry"))
        self.assertEqual(retry_lark_event("evtRetry", ValueError("temporary")), "retry")
        with sqlite3.connect(Path(self.home.name) / "teamflow.db") as conn:
            conn.execute(
                "UPDATE lark_event_inbox SET received_at = '2000-01-01T00:00:00+00:00', next_attempt_at = NULL"
            )
        self.assertIsNotNone(claim_lark_event("evtRetry"))
        self.assertEqual(retry_lark_event("evtRetry", ValueError("permanent")), "failed")
        self.assertEqual(lark_event_counts(), {"failed": 1})

    def test_task_snapshots_preserve_reentry_as_distinct_events(self):
        context = self.context()
        base = {"record_id": "recOne", "title": "Task"}

        save_task_snapshot(
            context,
            record_id="recOne",
            task={**base, "status": "in_progress"},
            source_event_id="evt1",
            source_revision="1",
        )
        save_task_snapshot(
            context,
            record_id="recOne",
            task={**base, "status": "blocked"},
            source_event_id="evt2",
            source_revision="2",
        )
        save_task_snapshot(
            context,
            record_id="recOne",
            task={**base, "status": "ready"},
            source_event_id="evt3",
            source_revision="3",
        )
        save_task_snapshot(
            context,
            record_id="recOne",
            task={**base, "status": "blocked"},
            source_event_id="evt4",
            source_revision="4",
        )
        save_task_snapshot(
            context,
            record_id="recOne",
            task={**base, "status": "done"},
            source_event_id="stale",
            source_revision="2",
        )

        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            event_types = [row[0] for row in conn.execute(
                "SELECT event_type FROM task_events WHERE record_id = 'recOne' ORDER BY created_at, rowid"
            )]
            state = conn.execute("SELECT status, source_revision FROM lark_task_state").fetchone()

        self.assertEqual(event_types.count("blocked_entered"), 2)
        self.assertIn("blocked_left", event_types)
        self.assertEqual((state["status"], state["source_revision"]), ("blocked", "4"))

    def test_ready_event_creates_one_durable_codex_delivery(self):
        context = self.context()
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'tl'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES ('agent_tl', ?, ?, ?, 'tl', 'codex', 'session_tl', 'TL Session', ?, ?)
                """,
                (
                    workspace["id"],
                    workspace["current_workflow_id"],
                    role["id"],
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
        save_task_snapshot(
            context,
            record_id="recReady",
            task={
                "record_id": "recReady",
                "task_id": "TF-0001",
                "title": "Implement dispatcher",
                "status": "ready",
                "role": "tl",
                "description": "Implement the durable dispatcher.",
                "acceptance_criteria": "A restarted daemon must not duplicate the turn.",
            },
            source_event_id="evtReady",
            source_revision="11",
        )

        result = prepare_task_deliveries(context)
        deliveries = claim_task_deliveries(context)
        finish_task_delivery(
            context,
            delivery_id=deliveries[0]["id"],
            result={"ok": True, "status": "completed"},
        )

        outcomes = result.pop("outcomes")
        self.assertEqual(result, {"routed": 1, "waiting": 0, "ignored": 1, "deliveries": 1})
        self.assertCountEqual([item["result"] for item in outcomes], ["not-required", "routed"])
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["session_id"], "session_tl")
        self.assertIn("收到通知本身不代表已经认领", deliveries[0]["prompt"])
        self.assertIn("任务描述：Implement the durable dispatcher.", deliveries[0]["prompt"])
        self.assertIn("验收标准：A restarted daemon must not duplicate the turn.", deliveries[0]["prompt"])
        self.assertIn("禁止降级调用 Lark CLI", deliveries[0]["prompt"])
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            saved = conn.execute(
                "SELECT status, turn_status FROM task_event_deliveries WHERE agent_id = 'agent_tl'"
            ).fetchone()
        self.assertEqual((saved["status"], saved["turn_status"]), ("completed", "completed"))
        next_result = prepare_task_deliveries(context)
        self.assertEqual(next_result.pop("outcomes"), [])
        self.assertEqual(next_result, {"routed": 0, "waiting": 0, "ignored": 0, "deliveries": 0})
        self.assertEqual(claim_task_deliveries(context), [])

    def test_late_agent_receives_a_current_ready_task(self):
        context = self.context()
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'tl'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES ('agent_one', ?, ?, ?, 'tl', 'codex', 'session_one', 'TL One', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )
        save_task_snapshot(
            context,
            record_id="recCatchup",
            task={
                "record_id": "recCatchup",
                "task_id": "TF-0002",
                "title": "Catch up a late agent",
                "status": "ready",
                "role": "tl",
            },
            source_event_id="evtCatchup",
            source_revision="12",
        )
        prepare_task_deliveries(context)
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'tl'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES ('agent_two', ?, ?, ?, 'tl', 'codex', 'session_two', 'TL Two', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )

        self.assertEqual(prepare_agent_catchup_deliveries(context), 1)
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            agents = {row[0] for row in conn.execute(
                "SELECT agent_id FROM task_event_deliveries WHERE event_key LIKE '%ready_entered%'"
            )}
        self.assertEqual(agents, {"agent_one", "agent_two"})

    def test_delivery_claims_only_one_event_per_session(self):
        context = self.context()
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'tl'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES ('agent_serial', ?, ?, ?, 'tl', 'codex', 'session_serial', 'Serial TL', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )
        for index in (1, 2):
            save_task_snapshot(
                context,
                record_id=f"recSerial{index}",
                task={
                    "record_id": f"recSerial{index}",
                    "task_id": f"TF-001{index}",
                    "title": f"Serial task {index}",
                    "status": "ready",
                    "role": "tl",
                },
                source_event_id=f"evtSerial{index}",
                source_revision=str(20 + index),
            )
        prepare_task_deliveries(context)

        first = claim_task_deliveries(context)
        self.assertEqual(len(first), 1)
        finish_task_delivery(
            context,
            delivery_id=first[0]["id"],
            result={"ok": True, "status": "completed"},
        )
        self.assertEqual(len(claim_task_deliveries(context)), 1)

    def test_stale_actionable_event_is_not_prepared(self):
        context = self.context()
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'tl'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES ('agent_stale', ?, ?, ?, 'tl', 'codex', 'session_stale', 'Stale TL', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )
        task = {
            "record_id": "recStale",
            "task_id": "TF-0013",
            "title": "Do not redeliver stale work",
            "role": "tl",
        }
        save_task_snapshot(
            context,
            record_id="recStale",
            task={**task, "status": "ready"},
            source_event_id="evtStaleReady",
            source_revision="31",
        )
        save_task_snapshot(
            context,
            record_id="recStale",
            task={**task, "status": "in_progress"},
            source_event_id="evtStaleClaimed",
            source_revision="32",
        )

        prepare_task_deliveries(context)

        self.assertEqual(claim_task_deliveries(context), [])
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            stale = conn.execute(
                "SELECT routing_status, routing_note FROM task_events WHERE event_type = 'ready_entered'"
            ).fetchone()
        self.assertEqual(stale["routing_status"], "ignored")
        self.assertEqual(stale["routing_note"], "task is no longer ready")

    def test_replacing_an_agent_session_redelivers_current_ready_tasks(self):
        context = self.context()
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'tl'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES ('agent_reassigned', ?, ?, ?, 'tl', 'codex', 'session_old', 'Reassigned TL', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )
        save_task_snapshot(
            context,
            record_id="recReassigned",
            task={
                "record_id": "recReassigned",
                "task_id": "TF-0015",
                "title": "Redeliver after session replacement",
                "status": "ready",
                "role": "tl",
            },
            source_event_id="evtReassigned",
            source_revision="35",
        )
        prepare_task_deliveries(context)
        original = claim_task_deliveries(context)[0]
        finish_task_delivery(
            context,
            delivery_id=original["id"],
            result={"ok": True, "status": "completed"},
        )

        with patch("core.db.verify_agent", return_value={"ok": True}):
            update_agent(self.workspace, agent_id="agent_reassigned", session_id="session_new")
        self.assertEqual(prepare_agent_catchup_deliveries(context), 1)

        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            deliveries = conn.execute(
                """
                SELECT assignment_revision, session_id, status
                FROM task_event_deliveries
                WHERE agent_id = 'agent_reassigned'
                ORDER BY assignment_revision
                """
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in deliveries],
            [(1, "session_old", "completed"), (2, "session_new", "pending")],
        )

    def test_pending_delivery_is_canceled_when_task_is_no_longer_actionable(self):
        context = self.context()
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'tl'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES ('agent_canceled', ?, ?, ?, 'tl', 'codex', 'session_canceled', 'Canceled TL', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )
        task = {
            "record_id": "recCanceled",
            "task_id": "TF-0014",
            "title": "Cancel stale delivery",
            "role": "tl",
        }
        save_task_snapshot(
            context,
            record_id="recCanceled",
            task={**task, "status": "ready"},
            source_event_id="evtCanceledReady",
            source_revision="41",
        )
        prepare_task_deliveries(context)
        save_task_snapshot(
            context,
            record_id="recCanceled",
            task={**task, "status": "in_progress"},
            source_event_id="evtCanceledClaimed",
            source_revision="42",
        )

        self.assertEqual(claim_task_deliveries(context), [])
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            delivery = conn.execute(
                "SELECT status, last_error FROM task_event_deliveries WHERE agent_id = 'agent_canceled'"
            ).fetchone()
        self.assertEqual(delivery["status"], "canceled")
        self.assertEqual(delivery["last_error"], "task is no longer ready")

    def test_agent_context_onboards_once_and_repeats_the_runtime_role(self):
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'tl'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES ('agent_context', ?, ?, ?, 'tl', 'codex', 'session_context', 'Context TL', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )

        first = agent_context(self.workspace, session_id="session_context", consume=True)
        second = agent_context(self.workspace, session_id="session_context", consume=True)

        self.assertIn("你已被注册为 TeamFlow Agent", first["additional_context"])
        self.assertIn("技术负责人", first["additional_context"])
        self.assertNotIn("你已被注册为 TeamFlow Agent", second["additional_context"])
        self.assertIn("TeamFlow 当前职责上下文", second["additional_context"])
        self.assertIn("技术负责人", second["additional_context"])

    def test_tool_grant_is_bound_to_the_session_input_and_single_use(self):
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'tl'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES ('agent_grant', ?, ?, ?, 'tl', 'codex', 'session_grant', 'Grant TL', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )
        register_workspace(self.workspace, enabled=True)
        runtime = TeamFlowDaemon()
        runtime.routes[self.workspace] = self.context()
        try:
            authorized = runtime.authorize_tool(
                session_id="session_grant",
                cwd=self.workspace,
                turn_id="turn_grant",
                tool_name="mcp__teamflow__get_assignment",
                tool_input={},
            )
            result = runtime.invoke_tool(
                grant=authorized["grant"],
                tool_name="get_assignment",
                arguments={},
            )
            self.assertEqual(result["assignment"]["agent_id"], "agent_grant")
            with self.assertRaisesRegex(ValueError, "missing or expired"):
                runtime.invoke_tool(
                    grant=authorized["grant"],
                    tool_name="get_assignment",
                    arguments={},
                )
            authorized = runtime.authorize_tool(
                session_id="session_grant",
                cwd=self.workspace,
                turn_id="turn_grant",
                tool_name="mcp__teamflow__get_task",
                tool_input={"record_id": "recGrant"},
            )
            with patch("core.daemon.get_task", return_value={
                "ok": True,
                "task": {"record_id": "recGrant", "title": "Full task"},
            }) as read_task:
                result = runtime.invoke_tool(
                    grant=authorized["grant"],
                    tool_name="get_task",
                    arguments={"record_id": "recGrant"},
                )
            self.assertEqual(result["task"]["title"], "Full task")
            read_task.assert_called_once()
        finally:
            runtime.close()

    def test_plugin_hooks_inject_context_and_authorize_mcp_input(self):
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'tl'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES ('agent_hook', ?, ?, ?, 'tl', 'codex', 'session_hook', 'Hook TL', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )
        register_workspace(self.workspace, enabled=True)
        runtime = TeamFlowDaemon()
        runtime.routes[self.workspace] = self.context()
        socket_path = Path(self.home.name) / "daemon.sock"
        server = DaemonServer(str(socket_path), runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            prompt_hook = subprocess.run(
                ["python3", str(ROOT / "hooks" / "user_prompt_submit.py")],
                input=json.dumps({"session_id": "session_hook", "cwd": self.workspace, "turn_id": "turn_hook"}),
                capture_output=True,
                text=True,
                check=True,
            )
            prompt_output = json.loads(prompt_hook.stdout)
            self.assertIn(
                "你已被注册为 TeamFlow Agent",
                prompt_output["hookSpecificOutput"]["additionalContext"],
            )
            tool_hook = subprocess.run(
                ["python3", str(ROOT / "hooks" / "pre_tool_use.py")],
                input=json.dumps({
                    "session_id": "session_hook",
                    "cwd": self.workspace,
                    "turn_id": "turn_hook",
                    "tool_name": "mcp__teamflow__get_assignment",
                    "tool_input": {},
                }),
                capture_output=True,
                text=True,
                check=True,
            )
            tool_output = json.loads(tool_hook.stdout)["hookSpecificOutput"]
            self.assertEqual(tool_output["permissionDecision"], "allow")
            self.assertTrue(tool_output["updatedInput"]["teamflow_authorization"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            runtime.close()

    def test_codex_delivery_persists_turn_before_completion(self):
        context = self.context()
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'tl'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES ('agent_turn', ?, ?, ?, 'tl', 'codex', 'session_turn', 'Turn TL', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )
        save_task_snapshot(
            context,
            record_id="recTurn",
            task={
                "record_id": "recTurn",
                "task_id": "TF-0020",
                "title": "Persist the turn",
                "status": "ready",
                "role": "tl",
            },
            source_event_id="evtTurn",
            source_revision="30",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        runtime = TeamFlowDaemon()
        runtime.active_sessions.add("session_turn")

        def complete_turn(thread_id, prompt, *, on_started, stop_event):
            on_started("turn_persisted")
            return {
                "ok": True,
                "thread_id": thread_id,
                "turn_id": "turn_persisted",
                "status": "completed",
                "response": "done",
                "error": None,
                "transport": "codex-ipc",
            }

        try:
            output = io.StringIO()
            with (
                patch("core.daemon.run_codex_turn", side_effect=complete_turn),
                redirect_stdout(output),
            ):
                runtime._execute_task_delivery(context, delivery)
        finally:
            runtime.close()
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            saved = conn.execute(
                "SELECT status, turn_id, turn_status, started_at, completed_at FROM task_event_deliveries"
            ).fetchone()

        self.assertEqual((saved["status"], saved["turn_id"], saved["turn_status"]), (
            "completed", "turn_persisted", "completed"
        ))
        self.assertIsNotNone(saved["started_at"])
        self.assertIsNotNone(saved["completed_at"])
        self.assertIn("transport=codex-ipc", output.getvalue())

    def test_daemon_defers_started_turn_for_reconciliation_when_interrupted(self):
        context = self.context()
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'tl'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES ('agent_interrupted', ?, ?, ?, 'tl', 'codex',
                          'session_interrupted', 'Interrupted TL', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )
        save_task_snapshot(
            context,
            record_id="recInterrupted",
            task={
                "record_id": "recInterrupted",
                "task_id": "TF-0021",
                "title": "Reconcile an interrupted delivery",
                "status": "ready",
                "role": "tl",
            },
            source_event_id="evtInterrupted",
            source_revision="49",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        runtime = TeamFlowDaemon()
        runtime.active_sessions.add("session_interrupted")

        def interrupt_turn(thread_id, prompt, *, on_started, stop_event):
            on_started("turn_interrupted")
            raise RuntimeError("TeamFlow daemon stopped while the Codex turn was running")

        try:
            output = io.StringIO()
            with (
                patch("core.daemon.run_codex_turn", side_effect=interrupt_turn),
                redirect_stdout(output),
            ):
                runtime._execute_task_delivery(context, delivery)
        finally:
            runtime.close()

        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            saved = conn.execute(
                """
                SELECT status, attempts, turn_id, turn_status, last_error
                FROM task_event_deliveries
                """
            ).fetchone()
        self.assertEqual(
            (saved["status"], saved["attempts"], saved["turn_id"], saved["turn_status"]),
            ("processing", 1, "turn_interrupted", "inProgress"),
        )
        self.assertIn("daemon stopped", saved["last_error"])
        self.assertIn("DISPATCH RECONCILING", output.getvalue())
        self.assertIn("turn=turn_interrupted", output.getvalue())
        self.assertNotIn("DISPATCH RETRY", output.getvalue())

    def test_daemon_reconciles_a_completed_turn_after_restart(self):
        context = self.context()
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'tl'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES ('agent_restart', ?, ?, ?, 'tl', 'codex', 'session_restart', 'Restart TL', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )
        save_task_snapshot(
            context,
            record_id="recRestart",
            task={
                "record_id": "recRestart",
                "task_id": "TF-0021",
                "title": "Reconcile after restart",
                "status": "ready",
                "role": "tl",
            },
            source_event_id="evtRestart",
            source_revision="50",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        mark_task_delivery_turn_started(
            context,
            delivery_id=delivery["id"],
            turn_id="turn_restart",
        )
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            conn.execute("UPDATE task_event_deliveries SET next_attempt_at = NULL")
        runtime = TeamFlowDaemon()
        try:
            output = io.StringIO()
            with (
                patch("core.daemon.read_codex_thread", return_value={
                    "turns": [{"id": "turn_restart", "status": "completed"}]
                }) as read_thread,
                redirect_stdout(output),
            ):
                runtime._reconcile_task_deliveries(context)
        finally:
            runtime.close()

        read_thread.assert_called_once_with("session_restart", include_turns=True)
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            saved = conn.execute(
                "SELECT status, turn_id, turn_status FROM task_event_deliveries"
            ).fetchone()
        self.assertEqual(
            (saved["status"], saved["turn_id"], saved["turn_status"]),
            ("completed", "turn_restart", "completed"),
        )
        self.assertIn("DISPATCH RECOVERED", output.getvalue())
        self.assertIn("turn=turn_restart", output.getvalue())

    def test_daemon_fails_a_delivery_when_the_codex_session_was_deleted(self):
        context = self.context()
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'tl'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES ('agent_deleted', ?, ?, ?, 'tl', 'codex', 'session_deleted', 'Deleted TL', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )
        save_task_snapshot(
            context,
            record_id="recDeletedSession",
            task={
                "record_id": "recDeletedSession",
                "task_id": "TF-0022",
                "title": "Handle a deleted session",
                "status": "ready",
                "role": "tl",
            },
            source_event_id="evtDeletedSession",
            source_revision="51",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        runtime = TeamFlowDaemon()
        runtime.active_sessions.add("session_deleted")
        try:
            with patch(
                "core.daemon.run_codex_turn",
                side_effect=ValueError("no rollout found for thread id session_deleted"),
            ), redirect_stdout(io.StringIO()):
                runtime._execute_task_delivery(context, delivery)
        finally:
            runtime.close()

        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            saved = conn.execute(
                "SELECT status, next_attempt_at, last_error FROM task_event_deliveries"
            ).fetchone()
        self.assertEqual(saved["status"], "failed")
        self.assertIsNone(saved["next_attempt_at"])
        self.assertIn("no rollout found", saved["last_error"])

    def test_daemon_rereads_current_task_before_normalizing_event(self):
        runtime = TeamFlowDaemon()
        context = self.context()
        runtime.routes[self.workspace] = context
        payload = {
            "header": {"event_id": "evtRead", "event_type": "drive.file.bitable_record_changed_v1"},
            "event": {
                "file_token": context.file_token,
                "table_id": context.table_id,
                "revision": 9,
                "action_list": [{"record_id": "recRead", "action": "record_edited"}],
            },
        }
        record_lark_event(
            event_id="evtRead",
            brand=context.brand,
            app_id=context.app_id,
            event_type="drive.file.bitable_record_changed_v1",
            file_token=context.file_token,
            table_id=context.table_id,
            source_revision="9",
            payload=payload,
        )
        register_workspace(self.workspace, enabled=True)
        output = io.StringIO()
        with patch("core.daemon.get_lark_task", return_value={
            "task": {"record_id": "recRead", "title": "Latest", "status": "ready", "role": "tl"}
        }) as get_task, redirect_stdout(output):
            runtime._process_event("evtRead")
        runtime.close()

        get_task.assert_called_once_with(self.workspace, record_id="recRead")
        log = output.getvalue()
        self.assertIn("[test-workspace @software-development]", log)
        self.assertIn("FEISHU WEBSOCKET 记录变更 RECEIVED", log)
        self.assertIn("event=evtRead", log)
        self.assertIn('board="Project board" table="Tasks"', log)
        self.assertIn('record=recRead title="Latest" change=created status=ready', log)
        self.assertIn("DISPATCH WAITING", log)
        self.assertIn("target=tl", log)
        self.assertIn('reason="未注册 TL Agent"', log)
        self.assertNotIn("attempt=1", log)
        self.assertNotIn("ignored=", log)
        self.assertNotIn("\033[", log)
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            state = conn.execute("SELECT snapshot_json FROM lark_task_state WHERE record_id = 'recRead'").fetchone()
        self.assertIn('"title":"Latest"', state["snapshot_json"])
        self.assertEqual(lark_event_counts(), {"processed": 1})

    def test_deleted_record_log_uses_the_saved_task_identity(self):
        runtime = TeamFlowDaemon()
        context = self.context()
        save_task_snapshot(
            context,
            record_id="recDeleted",
            task={
                "record_id": "recDeleted",
                "task_id": "AQ-0006",
                "title": "Deleted task",
                "status": "backlog",
                "role": "pm",
            },
            source_event_id="evtCreated",
            source_revision="8",
        )
        payload = {
            "header": {
                "event_id": "evtDeleted",
                "event_type": "drive.file.bitable_record_changed_v1",
            },
            "event": {
                "file_token": context.file_token,
                "table_id": context.table_id,
                "revision": 9,
                "action_list": [{"record_id": "recDeleted", "action": "record_deleted"}],
            },
        }
        output = io.StringIO()

        with patch("core.daemon.get_lark_task") as get_task, redirect_stdout(output):
            summary = runtime._process_workspace_event(context, payload)[0]
            runtime._log_received(context, {
                "event_id": "evtDeleted",
                "event_type": "drive.file.bitable_record_changed_v1",
                "received_at": "2026-07-22T04:00:00+00:00",
            }, summary)
        runtime.close()

        get_task.assert_not_called()
        self.assertIn('task=AQ-0006 title="Deleted task" change=deleted status=backlog', output.getvalue())
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            state = conn.execute(
                "SELECT 1 FROM lark_task_state WHERE record_id = 'recDeleted'"
            ).fetchone()
        self.assertIsNone(state)

    def test_daemon_styles_logs_only_for_interactive_terminals(self):
        terminal = Mock()
        terminal.isatty.return_value = True
        with patch("core.daemon.sys.stdout", terminal), patch.dict(os.environ, {}, clear=True):
            styled = _style("RECORD CHANGE", "1;36")
        with patch("core.daemon.sys.stdout", terminal), patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True):
            plain = _style("RECORD CHANGE", "1;36")

        self.assertEqual(styled, "\033[1;36mRECORD CHANGE\033[0m")
        self.assertEqual(plain, "RECORD CHANGE")
        with patch("core.daemon.sys.stdout", terminal), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_styled_task_change("created"), "\033[1;32mcreated\033[0m")
            self.assertEqual(_styled_task_change("updated"), "\033[1;33mupdated\033[0m")
            self.assertEqual(_styled_task_change("deleted"), "\033[1;31mdeleted\033[0m")
            self.assertEqual(_styled_task_change("unchanged"), "\033[2munchanged\033[0m")

    def test_user_identity_verification_resyncs_an_enabled_running_workspace(self):
        args = Mock(workspace=self.workspace)
        status = {"tokenStatus": "valid"}
        identity = {"ok": True, "lark_identity_id": "identity_user"}
        with patch("scripts.teamflow.run_lark_cli_json", side_effect=[status, {}]), patch(
            "scripts.teamflow.verify_lark_user_identity", return_value=identity
        ) as verify_identity, patch(
            "scripts.teamflow.daemon_status", return_value={"running": True}
        ), patch(
            "scripts.teamflow.workspace_enabled", return_value=True
        ), patch(
            "scripts.teamflow.sync_daemon_workspace"
        ) as sync_workspace, patch("scripts.teamflow.print_json") as print_result:
            result = cmd_verify_lark_user_identity(args)

        self.assertEqual(result, 0)
        verify_identity.assert_called_once_with(self.workspace, status=status, profile={})
        sync_workspace.assert_called_once_with(self.workspace)
        print_result.assert_called_once_with(identity)

    def test_foreground_daemon_stops_cleanly_on_keyboard_interrupt(self):
        runtime = Mock()
        server = Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        output = io.StringIO()

        with patch("core.daemon.TeamFlowDaemon", return_value=runtime), \
             patch("core.daemon.DaemonServer", return_value=server), \
             patch("core.daemon.os.chmod"), \
             patch("core.daemon.threading.Thread"), \
             redirect_stdout(output):
            result = run_daemon()

        self.assertEqual(result, 130)
        self.assertIn("DAEMON STOPPING", output.getvalue())
        self.assertNotIn("teamflow daemon:", output.getvalue())
        runtime.close.assert_called_once_with()
        server.server_close.assert_called_once_with()

    def test_ipc_sends_sync_to_the_running_daemon(self):
        with tempfile.TemporaryDirectory(prefix="teamflow-ipc-", dir=ROOT / "tmp") as home:
            socket_path = Path(home) / "daemon.sock"
            runtime = Mock()
            runtime.sync_workspace.return_value = {"workspace_root": self.workspace, "daemon_pid": 123}
            runtime.enable_workspace.return_value = {"workspace_root": self.workspace, "enabled": True}
            server = DaemonServer(str(socket_path), runtime)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch("core.daemon.daemon_socket_path", return_value=socket_path):
                    result = _daemon_request(
                        {"action": "sync_workspace", "workspace": self.workspace, "identity_id": self.identity_id},
                        timeout=2,
                    )
                    enabled = _daemon_request(
                        {"action": "enable_workspace", "workspace": self.workspace, "identity_id": self.identity_id},
                        timeout=2,
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual(result["daemon_pid"], 123)
        self.assertTrue(enabled["enabled"])
        runtime.sync_workspace.assert_called_once_with(self.workspace, identity_id=self.identity_id)
        runtime.enable_workspace.assert_called_once_with(self.workspace, identity_id=self.identity_id)

    def test_public_listener_commands_delegate_to_daemon(self):
        result = {"ok": True, "status": "verified"}
        with patch("core.daemon.verify_daemon_workspace", return_value=result) as verify:
            self.assertIs(verify_lark_board_listener(self.workspace), result)
        with patch("core.daemon.stream_daemon_events") as stream:
            listen_lark_board_events(self.workspace, emit=Mock(), ready=Mock())

        verify.assert_called_once_with(self.workspace, identity_id=None)
        stream.assert_called_once()


if __name__ == "__main__":
    unittest.main()
