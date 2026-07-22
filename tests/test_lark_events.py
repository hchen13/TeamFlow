from __future__ import annotations

import asyncio
import io
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from core.config import resolve_workspace_paths
from core.daemon import (
    DaemonServer,
    TeamFlowDaemon,
    _daemon_request,
    _preview_task_delivery,
    _style,
    _styled_task_change,
    register_workspace,
    registered_workspaces,
    run_daemon,
)
from core.db import connect, configure_lark_board, configure_lark_identity, init_workspace
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

    def test_ready_event_previews_the_exact_codex_delivery_once(self):
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
            },
            source_event_id="evtReady",
            source_revision="11",
        )

        result = prepare_task_deliveries(context)
        deliveries = claim_task_deliveries(context)
        output = io.StringIO()
        with redirect_stdout(output):
            _preview_task_delivery(context, deliveries[0])
        finish_task_delivery(
            context,
            event_key=deliveries[0]["event_key"],
            agent_id=deliveries[0]["agent_id"],
        )

        outcomes = result.pop("outcomes")
        self.assertEqual(result, {"routed": 1, "waiting": 0, "ignored": 1, "deliveries": 1})
        self.assertCountEqual([item["result"] for item in outcomes], ["not-required", "routed"])
        self.assertEqual(len(deliveries), 1)
        self.assertIn("session: session_tl", output.getvalue())
        self.assertIn("收到通知本身不代表已经认领", output.getvalue())
        next_result = prepare_task_deliveries(context)
        self.assertEqual(next_result.pop("outcomes"), [])
        self.assertEqual(next_result, {"routed": 0, "waiting": 0, "ignored": 0, "deliveries": 0})
        self.assertEqual(claim_task_deliveries(context), [])

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
