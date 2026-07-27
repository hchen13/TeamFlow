from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from copy import deepcopy
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import RequestParams

from core import agent_runtime as teamflow_agent_runtime
from core import mcp_server as teamflow_mcp_server
from core.agent_runtime import (
    agent_context,
    confirm_agent_context,
    inspect_agent_contexts,
    mark_agent_context_recovery_pending,
)
from core.config import resolve_workspace_paths
from core.codex_permissions import (
    TEAMFLOW_MCP_TOOLS,
    CodexBackgroundMcpPermissionRequired,
)
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
from core.delivery_runtime import DeliveryRuntime
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
    due_processing_task_deliveries,
    finish_task_delivery,
    mark_task_delivery_turn_started,
    prepare_agent_catchup_deliveries,
    prepare_task_deliveries,
    recover_task_deliveries,
    render_task_prompt,
    task_delivery_is_current,
    task_delivery_turn_is_current,
)
from core.teamflow_tools import list_available_tasks
from core.workflow import load_workflow_definition, validate_workflow_definition
from scripts.teamflow import (
    cmd_inspect_agent_context,
    cmd_serve_ui,
    cmd_verify_lark_user_identity,
    ui_dist_dir,
)


ROOT = Path(__file__).resolve().parents[1]


def renamed_workflow_state(source: str, target: str) -> dict:
    definition = deepcopy(load_workflow_definition("software-development"))
    for state in definition["lifecycle"]["states"]:
        if state["key"] == source:
            state["key"] = target
    for action in definition["lifecycle"]["actions"].values():
        for rule in action["rules"]:
            rule["from"] = [
                target if state == source else state
                for state in rule.get("from", [])
            ]
            if rule.get("to") == source:
                rule["to"] = target
    for action in definition.get("runtime_actions", {}).values():
        action["states"] = [
            target if state == source else state
            for state in action["states"]
        ]
    return definition


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
        try:
            event_loop = asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            return
        if not event_loop.is_running():
            event_loop.close()
            asyncio.set_event_loop(None)

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

    def test_daemon_worker_replacement_uses_worker_facade(self):
        runtime = TeamFlowDaemon()
        previous = self.context()
        replacement = LarkEventContext(**{
            **previous.__dict__,
            "app_secret": "replacement-secret",
        })
        previous_worker = {
            "context": previous,
            "credentials": (
                previous.app_id,
                previous.app_secret,
                previous.brand,
            ),
            "process": Mock(),
            "ready": Mock(),
            "errors": Mock(),
        }
        ready = Mock()
        ready.wait.return_value = True
        errors = Mock()
        errors.get_nowait.return_value = None
        process = Mock()
        process.is_alive.return_value = True
        runtime.workers[runtime.app_key(previous)] = previous_worker

        with (
            patch.object(runtime, "_stop_worker") as stop_worker,
            patch.object(runtime.mp, "Event", return_value=ready),
            patch.object(runtime.mp, "Queue", return_value=errors),
            patch.object(runtime.mp, "Process", return_value=process),
        ):
            runtime._ensure_app(replacement)

        runtime.workers.clear()
        runtime.close()
        stop_worker.assert_called_once_with(previous_worker)

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

    def test_daemon_close_uses_worker_facade(self):
        runtime = TeamFlowDaemon()
        worker = {"process": Mock(), "errors": Mock()}
        runtime.workers["app"] = worker

        with patch.object(runtime, "_stop_worker") as stop_worker:
            runtime.close()

        stop_worker.assert_called_once_with(worker)

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

    def test_claimable_queue_and_dispatch_prompt_use_workflow_state_definition(self):
        definition = renamed_workflow_state("ready", "queued")
        queued = next(
            state
            for state in definition["lifecycle"]["states"]
            if state["key"] == "queued"
        )
        queued["dispatch_instructions"]["zh-CN"] = "按自定义队列规则处理。"
        validate_workflow_definition(
            definition,
            Path("/tmp/software-development/workflow.json"),
        )
        task = {
            "record_id": "recQueued",
            "task_id": "TF-QUEUE",
            "title": "Custom queue",
            "status": "queued",
            "role": "tl",
        }
        save_task_snapshot(
            self.context(),
            record_id="recQueued",
            task=task,
            source_event_id="evtQueued",
            source_revision="1",
        )
        caller = {
            "agent_id": "agent_queue",
            "agent_name": "Queue TL",
            "workspace_root": self.workspace,
            "workflow_key": "software-development",
            "role_key": "tl",
        }

        with (
            patch("core.teamflow_tools._definition", return_value=definition),
            patch("core.task_dispatch.load_workflow_definition", return_value=definition),
        ):
            available = list_available_tasks(caller)
            prompt = render_task_prompt(
                self.context(),
                event_type="queued_entered",
                event_key="evtQueued",
                workflow_key="software-development",
                role_name="技术负责人",
                task=task,
            )

        self.assertEqual(available["count"], 1)
        self.assertEqual(available["tasks"][0]["record_id"], "recQueued")
        self.assertTrue(prompt.endswith("按自定义队列规则处理。"))

    def test_execution_control_state_is_loaded_from_the_workflow(self):
        definition = renamed_workflow_state("in_progress", "working")
        caller = {
            "agent_id": "agent_working",
            "agent_name": "Working TL",
            "workspace_root": self.workspace,
            "workflow_key": "software-development",
            "role_key": "tl",
        }
        runtime = TeamFlowDaemon()
        try:
            with patch("core.daemon.load_workflow_definition", return_value=definition):
                runtime._sync_task_execution_activity(
                    caller,
                    tool_name="get_task",
                    result={
                        "ok": True,
                        "task": {
                            "record_id": "recWorking",
                            "status": "working",
                            "agent_id": "agent_working",
                        },
                    },
                    session_id="session_working",
                    turn_id="turn_working",
                )
                with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
                    execution = conn.execute(
                        "SELECT * FROM task_executions WHERE record_id = 'recWorking'"
                    ).fetchone()
                runtime._sync_task_execution_activity(
                    caller,
                    tool_name="get_task",
                    result={
                        "ok": True,
                        "task": {
                            "record_id": "recWorking",
                            "status": "done",
                            "agent_id": "agent_working",
                        },
                    },
                    session_id="session_working",
                    turn_id="turn_working",
                )
        finally:
            runtime.close()

        self.assertEqual(execution["state"], "active")
        self.assertEqual(execution["turn_id"], "turn_working")
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            cleared = conn.execute(
                "SELECT * FROM task_executions WHERE record_id = 'recWorking'"
            ).fetchone()
        self.assertIsNone(cleared)

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
        self.assertEqual(claim_task_deliveries(context), [])
        finish_task_delivery(
            context,
            delivery_id=first[0]["id"],
            result={"ok": True, "status": "completed"},
        )
        self.assertEqual(len(claim_task_deliveries(context)), 1)

    def test_unconfirmed_delivery_keeps_its_message_id_until_reconciled(self):
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
                ) VALUES ('agent_message', ?, ?, ?, 'tl', 'codex',
                          'session_message', 'Message TL', ?, ?)
                """,
                (
                    workspace["id"],
                    workspace["current_workflow_id"],
                    role["id"],
                    now(),
                    now(),
                ),
            )
        save_task_snapshot(
            context,
            record_id="recMessage",
            task={
                "record_id": "recMessage",
                "task_id": "TF-MESSAGE",
                "title": "Preserve delivery intent",
                "status": "ready",
                "role": "tl",
            },
            source_event_id="evtMessage",
            source_revision="22",
        )
        prepare_task_deliveries(context)

        first = claim_task_deliveries(context)[0]
        recover_task_deliveries(context)
        self.assertEqual(claim_task_deliveries(context), [])
        recovered = due_processing_task_deliveries(context)[0]
        self.assertEqual(
            recovered["client_message_id"],
            first["client_message_id"],
        )

        mark_task_delivery_turn_started(
            context,
            delivery_id=recovered["id"],
            turn_id="turn_message",
        )
        finish_task_delivery(
            context,
            delivery_id=recovered["id"],
            error=ValueError("MCP unavailable"),
            retry=True,
        )
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            conn.execute(
                "UPDATE task_event_deliveries SET next_attempt_at = NULL"
            )
        retried = claim_task_deliveries(context)[0]
        self.assertNotEqual(
            retried["client_message_id"],
            first["client_message_id"],
        )

    def test_delivery_schedule_reserves_processing_sessions_in_other_workspaces(self):
        first = Mock()
        second = Mock()
        runtime = DeliveryRuntime(
            sync_lock=threading.RLock(),
            stopping=threading.Event(),
            routes_ready=threading.Event(),
            wakeup=threading.Event(),
            active_sessions=set(),
            workers={},
            contexts=lambda: [first, second],
            reserved_sessions=lambda: {"shared_session"},
            get_task=Mock(),
            run_turn=Mock(),
            read_thread=Mock(),
            stop_turn=Mock(),
            find_turn=Mock(),
            find_turn_by_client_message_id=Mock(),
            unresolved_mcp_failures=Mock(),
            delivery_error_is_terminal=Mock(),
            log_dispatch=Mock(),
        )

        with patch(
            "core.delivery_runtime.claim_task_deliveries",
            return_value=[],
        ) as claim:
            runtime.schedule(second)

        claim.assert_called_once_with(
            second,
            exclude_session_ids={"shared_session"},
        )

    def test_daemon_reserves_processing_sessions_from_an_unloaded_workspace(self):
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
                ) VALUES ('agent_unloaded', ?, ?, ?, 'tl', 'codex',
                          'session_unloaded', 'Unloaded TL', ?, ?)
                """,
                (
                    workspace["id"],
                    workspace["current_workflow_id"],
                    role["id"],
                    now(),
                    now(),
                ),
            )
        save_task_snapshot(
            context,
            record_id="recUnloaded",
            task={
                "record_id": "recUnloaded",
                "task_id": "TF-UNLOADED",
                "title": "Reserve an unloaded workspace session",
                "status": "ready",
                "role": "tl",
            },
            source_event_id="evtUnloaded",
            source_revision="73",
        )
        prepare_task_deliveries(context)
        claim_task_deliveries(context)
        register_workspace(self.workspace, enabled=True)

        runtime = TeamFlowDaemon()
        try:
            self.assertNotIn(self.workspace, runtime.routes)
            self.assertIn(
                "session_unloaded",
                runtime._reserved_delivery_sessions(),
            )
        finally:
            runtime.close()

    def test_delivery_runtime_executes_different_sessions_in_parallel(self):
        context = self.context()
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            roles = {
                row["role_key"]: row
                for row in conn.execute(
                    "SELECT * FROM roles WHERE workflow_id = ? AND role_key IN ('pm', 'tl')",
                    (workspace["current_workflow_id"],),
                )
            }
            for agent_id, role_key, session_id in (
                ("agent_parallel_pm", "pm", "session_parallel_pm"),
                ("agent_parallel_tl", "tl", "session_parallel_tl"),
            ):
                conn.execute(
                    """
                    INSERT INTO agents (
                      id, workspace_id, workflow_id, role_id, role_key,
                      harness_type, session_id, display_name, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'codex', ?, ?, ?, ?)
                    """,
                    (
                        agent_id,
                        workspace["id"],
                        workspace["current_workflow_id"],
                        roles[role_key]["id"],
                        role_key,
                        session_id,
                        f"Parallel {role_key.upper()}",
                        now(),
                        now(),
                    ),
                )
        for index, role_key in enumerate(("pm", "tl"), start=1):
            save_task_snapshot(
                context,
                record_id=f"recParallel{index}",
                task={
                    "record_id": f"recParallel{index}",
                    "task_id": f"TF-002{index}",
                    "title": f"Parallel task {index}",
                    "status": "ready",
                    "role": role_key,
                },
                source_event_id=f"evtParallel{index}",
                source_revision=str(30 + index),
            )
        prepare_task_deliveries(context)

        release = threading.Event()
        both_started = threading.Event()
        started_sessions = set()
        started_lock = threading.Lock()

        def get_task(_workspace, *, record_id):
            role_key = "pm" if record_id == "recParallel1" else "tl"
            return {
                "task": {
                    "record_id": record_id,
                    "task_id": (
                        "TF-0021"
                        if role_key == "pm"
                        else "TF-0022"
                    ),
                    "title": f"Parallel {role_key.upper()}",
                    "status": "ready",
                    "role": role_key,
                }
            }

        def run_turn(
            session_id,
            _prompt,
            *,
            client_message_id,
            on_started,
            stop_event,
        ):
            self.assertTrue(client_message_id)
            on_started(f"turn_{session_id}")
            with started_lock:
                started_sessions.add(session_id)
                if len(started_sessions) == 2:
                    both_started.set()
            if not release.wait(2):
                raise TimeoutError("parallel delivery test did not release workers")
            return {
                "ok": True,
                "status": "completed",
                "turn_id": f"turn_{session_id}",
                "transport": "test",
            }

        active_sessions = set()
        workers = {}
        runtime = DeliveryRuntime(
            sync_lock=threading.RLock(),
            stopping=threading.Event(),
            routes_ready=threading.Event(),
            wakeup=threading.Event(),
            active_sessions=active_sessions,
            workers=workers,
            contexts=lambda: [context],
            reserved_sessions=lambda: set(),
            get_task=get_task,
            run_turn=run_turn,
            read_thread=lambda *_args, **_kwargs: {},
            stop_turn=lambda *_args, **_kwargs: {},
            find_turn=lambda *_args: None,
            find_turn_by_client_message_id=lambda *_args: None,
            unresolved_mcp_failures=lambda _turn: [],
            delivery_error_is_terminal=lambda _error: False,
            log_dispatch=lambda *_args, **_kwargs: None,
        )

        runtime.schedule(context)

        self.assertTrue(both_started.wait(2))
        self.assertEqual(
            started_sessions,
            {"session_parallel_pm", "session_parallel_tl"},
        )
        self.assertEqual(active_sessions, started_sessions)
        running_workers = list(workers.values())
        release.set()
        for worker in running_workers:
            worker.join(2)
        self.assertTrue(all(not worker.is_alive() for worker in running_workers))
        self.assertEqual(active_sessions, set())

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

    def test_only_the_latest_reentry_event_is_delivered(self):
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
                ) VALUES ('agent_reentry', ?, ?, ?, 'tl', 'codex',
                          'session_reentry', 'Reentry TL', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )
        task = {
            "record_id": "recReentry",
            "task_id": "TF-0030",
            "title": "Deliver only the current entry",
            "role": "tl",
        }
        save_task_snapshot(
            context,
            record_id="recReentry",
            task={**task, "status": "ready"},
            source_event_id="evtReentryOne",
            source_revision="50",
        )
        save_task_snapshot(
            context,
            record_id="recReentry",
            task={**task, "status": "in_progress"},
            source_event_id="evtReentryClaim",
            source_revision="51",
        )
        save_task_snapshot(
            context,
            record_id="recReentry",
            task={**task, "status": "ready"},
            source_event_id="evtReentryTwo",
            source_revision="52",
        )

        prepare_task_deliveries(context)
        deliveries = claim_task_deliveries(context)

        self.assertEqual(len(deliveries), 1)
        self.assertIn(":52:recReentry:ready_entered", deliveries[0]["event_key"])
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            old_entry = conn.execute(
                """
                SELECT routing_status, routing_note
                FROM task_events
                WHERE event_key LIKE '%:50:recReentry:ready_entered'
                """
            ).fetchone()
        self.assertEqual(old_entry["routing_status"], "ignored")
        self.assertEqual(old_entry["routing_note"], "task has a newer ready dispatch event")

    def test_processing_delivery_is_stale_after_same_role_state_transition(self):
        context = self.context()
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'pm'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES ('agent_generation', ?, ?, ?, 'pm', 'codex',
                          'session_generation', 'Generation PM', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )
        task = {
            "record_id": "recGeneration",
            "task_id": "TF-0032",
            "title": "Do not deliver an old generation",
            "status": "review",
            "role": "tl",
        }
        save_task_snapshot(
            context,
            record_id="recGeneration",
            task=task,
            source_event_id="evtGenerationReview",
            source_revision="60",
        )
        prepare_task_deliveries(context)
        old_delivery = claim_task_deliveries(context)[0]
        mark_task_delivery_turn_started(
            context,
            delivery_id=old_delivery["id"],
            turn_id="turn_generation",
        )
        save_task_snapshot(
            context,
            record_id="recGeneration",
            task={
                **task,
                "status": "blocked",
                "blocked_reason": "等待范围确认",
                "waiting_on": "stakeholder",
                "next_action": "PM 与项目决策人确认",
            },
            source_event_id="evtGenerationBlocked",
            source_revision="61",
        )
        prepare_task_deliveries(context)

        self.assertFalse(
            task_delivery_is_current(
                context,
                delivery_id=old_delivery["id"],
            )
        )
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            conn.execute(
                """
                UPDATE task_event_deliveries
                SET next_attempt_at = NULL
                WHERE id = ?
                """,
                (old_delivery["id"],),
            )
            pending = conn.execute(
                """
                SELECT COUNT(*)
                FROM task_event_deliveries AS delivery
                JOIN task_events AS event ON event.event_key = delivery.event_key
                WHERE event.record_id = 'recGeneration'
                  AND event.event_type = 'blocked_entered'
                  AND delivery.status = 'pending'
                """
            ).fetchone()[0]
        self.assertEqual(pending, 1)

        runtime = TeamFlowDaemon()
        try:
            with (
                patch(
                    "core.daemon.read_codex_thread",
                    return_value={
                        "status": {"type": "active"},
                        "turns": [{
                            "id": "turn_generation",
                            "status": "inProgress",
                        }],
                    },
                ),
                patch(
                    "core.daemon.stop_codex_turn",
                    side_effect=ValueError(
                        "Codex task turn turn_generation is not terminal, "
                        "but the session has no active turn"
                    ),
                ) as stop_turn,
            ):
                runtime._reconcile_task_deliveries(context)
        finally:
            runtime.close()

        stop_turn.assert_called_once_with(
            "session_generation",
            expected_turn_id="turn_generation",
        )
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            stale = conn.execute(
                """
                SELECT status, turn_status
                FROM task_event_deliveries
                WHERE id = ?
                """,
                (old_delivery["id"],),
            ).fetchone()
        self.assertEqual(
            (stale["status"], stale["turn_status"]),
            ("canceled", "inactive"),
        )

    def test_pending_delivery_renders_the_latest_task_snapshot(self):
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
                ) VALUES ('agent_snapshot', ?, ?, ?, 'tl', 'codex',
                          'session_snapshot', 'Snapshot TL', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )
        initial = {
            "record_id": "recSnapshot",
            "task_id": "TF-0031",
            "title": "Old title",
            "status": "ready",
            "role": "tl",
            "description": "Old description",
        }
        save_task_snapshot(
            context,
            record_id="recSnapshot",
            task=initial,
            source_event_id="evtSnapshotOne",
            source_revision="60",
        )
        prepare_task_deliveries(context)
        save_task_snapshot(
            context,
            record_id="recSnapshot",
            task={
                **initial,
                "title": "Current title",
                "description": "Current description",
            },
            source_event_id="evtSnapshotTwo",
            source_revision="61",
        )
        prepare_task_deliveries(context)

        deliveries = claim_task_deliveries(context)

        self.assertEqual(len(deliveries), 1)
        self.assertIn("任务：TF-0031 Current title", deliveries[0]["prompt"])
        self.assertIn("任务描述：Current description", deliveries[0]["prompt"])
        self.assertNotIn("Old description", deliveries[0]["prompt"])

    def test_ready_role_change_cancels_the_old_target_and_routes_the_new_one(self):
        context = self.context()
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            roles = {
                row["role_key"]: row
                for row in conn.execute(
                    "SELECT * FROM roles WHERE workflow_id = ? AND role_key IN ('tl', 'qa')",
                    (workspace["current_workflow_id"],),
                )
            }
            for role_key in ("tl", "qa"):
                conn.execute(
                    """
                    INSERT INTO agents (
                      id, workspace_id, workflow_id, role_id, role_key,
                      harness_type, session_id, display_name, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'codex', ?, ?, ?, ?)
                    """,
                    (
                        f"agent_role_{role_key}",
                        workspace["id"],
                        workspace["current_workflow_id"],
                        roles[role_key]["id"],
                        role_key,
                        f"session_role_{role_key}",
                        f"Role {role_key.upper()}",
                        now(),
                        now(),
                    ),
                )
        task = {
            "record_id": "recRoleChange",
            "task_id": "TF-0032",
            "title": "Route the current owner",
            "status": "ready",
            "role": "tl",
        }
        save_task_snapshot(
            context,
            record_id="recRoleChange",
            task=task,
            source_event_id="evtRoleTl",
            source_revision="70",
        )
        prepare_task_deliveries(context)
        save_task_snapshot(
            context,
            record_id="recRoleChange",
            task={**task, "role": "qa"},
            source_event_id="evtRoleQa",
            source_revision="71",
        )
        prepare_task_deliveries(context)

        deliveries = claim_task_deliveries(context)

        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["agent_id"], "agent_role_qa")
        self.assertEqual(deliveries[0]["role_key"], "qa")
        self.assertIn("当前负责人：qa", deliveries[0]["prompt"])
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            old_delivery = conn.execute(
                """
                SELECT status, last_error
                FROM task_event_deliveries
                WHERE agent_id = 'agent_role_tl'
                """
            ).fetchone()
        self.assertEqual(old_delivery["status"], "canceled")
        self.assertEqual(old_delivery["last_error"], "task has a newer ready dispatch event")

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

    def test_agent_context_requires_confirmation_and_restores_after_compaction(self):
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
        self.assertEqual(second["context_fingerprint"], first["context_fingerprint"])
        self.assertIn("你已被注册为 TeamFlow Agent", second["additional_context"])

        confirmed = confirm_agent_context(
            self.workspace,
            agent_id=first["assignment"]["agent_id"],
            session_id=first["assignment"]["session_id"],
            assignment_revision=first["assignment"]["assignment_revision"],
            context_fingerprint=first["context_fingerprint"],
        )
        after_confirmation = agent_context(
            self.workspace,
            session_id="session_context",
            consume=True,
        )
        recovery_mark = mark_agent_context_recovery_pending(
            self.workspace,
            agent_id=first["assignment"]["agent_id"],
            session_id="session_context",
            assignment_revision=first["assignment"]["assignment_revision"],
        )
        recovered = agent_context(
            self.workspace,
            session_id="session_context",
            consume=True,
        )
        recovery_confirmation = confirm_agent_context(
            self.workspace,
            agent_id=recovered["assignment"]["agent_id"],
            session_id=recovered["assignment"]["session_id"],
            assignment_revision=recovered["assignment"]["assignment_revision"],
            context_fingerprint=recovered["context_fingerprint"],
        )
        after_recovery = agent_context(
            self.workspace,
            session_id="session_context",
            consume=True,
        )

        self.assertIn("技术负责人", first["additional_context"])
        self.assertTrue(confirmed["confirmed"])
        self.assertIsNone(after_confirmation["additional_context"])
        self.assertTrue(recovery_mark["marked"])
        self.assertEqual(recovered["context_status"], "recovery_pending")
        self.assertIn("会话压缩后恢复", recovered["additional_context"])
        self.assertIn("技术负责人", recovered["additional_context"])
        self.assertTrue(recovery_confirmation["confirmed"])
        self.assertIsNone(after_recovery["additional_context"])

    def test_stale_context_confirmation_cannot_onboard_a_reassigned_session(self):
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
                ) VALUES ('agent_context_stale', ?, ?, ?, 'tl', 'codex', 'session_context_old', 'Stale TL', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )

        old_context = agent_context(
            self.workspace,
            session_id="session_context_old",
            consume=True,
        )
        with patch("core.db.verify_agent", return_value={"ok": True}):
            update_agent(
                self.workspace,
                agent_id="agent_context_stale",
                session_id="session_context_new",
            )

        stale_confirmation = confirm_agent_context(
            self.workspace,
            agent_id=old_context["assignment"]["agent_id"],
            session_id=old_context["assignment"]["session_id"],
            assignment_revision=old_context["assignment"]["assignment_revision"],
            context_fingerprint=old_context["context_fingerprint"],
        )
        reassigned = agent_context(
            self.workspace,
            session_id="session_context_new",
            consume=True,
        )

        self.assertFalse(stale_confirmation["confirmed"])
        self.assertEqual(reassigned["assignment"]["assignment_revision"], 2)
        self.assertEqual(reassigned["context_status"], "pending")
        self.assertIn("你已被注册为 TeamFlow Agent", reassigned["additional_context"])

    def test_agent_context_fingerprint_reacts_to_rendered_instruction_changes(self):
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
                ) VALUES ('agent_context_template', ?, ?, ?, 'tl', 'codex', 'session_context_template', 'Template TL', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )

        initial = agent_context(
            self.workspace,
            session_id="session_context_template",
            consume=True,
        )
        confirm_agent_context(
            self.workspace,
            agent_id=initial["assignment"]["agent_id"],
            session_id=initial["assignment"]["session_id"],
            assignment_revision=initial["assignment"]["assignment_revision"],
            context_fingerprint=initial["context_fingerprint"],
        )
        original_render = teamflow_agent_runtime.render_agent_context

        with patch(
            "core.agent_runtime.render_agent_context",
            side_effect=lambda assignment, **options: (
                f"{original_render(assignment, **options)}\n新增职责规则。"
            ),
        ):
            changed = agent_context(
                self.workspace,
                session_id="session_context_template",
                consume=True,
            )
            inspected = inspect_agent_contexts(
                self.workspace,
                agent_id="agent_context_template",
            )

        self.assertNotEqual(changed["context_fingerprint"], initial["context_fingerprint"])
        self.assertEqual(changed["context_status"], "pending")
        self.assertIn("新增职责规则", changed["additional_context"])
        self.assertEqual(inspected[0]["status"], "pending")

    def test_inspect_agent_context_cli_lists_every_agent_in_a_role(self):
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'tl'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            for suffix in ("a", "b"):
                conn.execute(
                    """
                    INSERT INTO agents (
                      id, workspace_id, workflow_id, role_id, role_key,
                      harness_type, session_id, display_name, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'tl', 'codex', ?, ?, ?, ?)
                    """,
                    (
                        f"agent_context_{suffix}",
                        workspace["id"],
                        workspace["current_workflow_id"],
                        role["id"],
                        f"session_context_{suffix}",
                        f"Context TL {suffix.upper()}",
                        now(),
                        now(),
                    ),
                )

        output = io.StringIO()
        args = Mock(
            workspace=self.workspace,
            agent_id=None,
            role="tl",
            session_id=None,
            all=False,
            json=True,
        )
        with redirect_stdout(output):
            result = cmd_inspect_agent_context(args)
        payload = json.loads(output.getvalue())

        self.assertEqual(result, 0)
        self.assertEqual(payload["count"], 2)
        self.assertEqual({agent["role_key"] for agent in payload["agents"]}, {"tl"})
        self.assertTrue(all(agent["status"] == "pending" for agent in payload["agents"]))

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
                invocation_id="invocation_assignment",
                session_id="session_grant",
                cwd=self.workspace,
                turn_id="turn_grant",
                tool_name="mcp__teamflow__get_assignment",
                tool_input={},
            )
            result = runtime.invoke_tool(
                invocation_id="invocation_assignment",
                grant=authorized["grant"],
                tool_name="get_assignment",
                arguments={},
            )
            self.assertEqual(result["assignment"]["agent_id"], "agent_grant")
            with self.assertRaisesRegex(ValueError, "missing or expired"):
                runtime.invoke_tool(
                    invocation_id="invocation_assignment",
                    grant=authorized["grant"],
                    tool_name="get_assignment",
                    arguments={},
                )
            authorized = runtime.authorize_tool(
                invocation_id="invocation_get_task",
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
                    invocation_id="invocation_get_task",
                    grant=authorized["grant"],
                    tool_name="get_task",
                    arguments={"record_id": "recGrant"},
                )
            self.assertEqual(result["task"]["title"], "Full task")
            read_task.assert_called_once()
        finally:
            runtime.close()

    def test_successful_handoff_closes_further_teamflow_calls_in_the_turn(self):
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'pm'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES ('agent_handoff', ?, ?, ?, 'pm', 'codex',
                          'session_handoff', 'Handoff PM', ?, ?)
                """,
                (
                    workspace["id"],
                    workspace["current_workflow_id"],
                    role["id"],
                    now(),
                    now(),
                ),
            )
        register_workspace(self.workspace, enabled=True)
        runtime = TeamFlowDaemon()
        runtime.routes[self.workspace] = self.context()
        arguments = {"record_id": "recHandoff", "role": "tl"}
        result = {
            "ok": True,
            "task": {
                "record_id": "recHandoff",
                "status": "ready",
                "role": "tl",
            },
            "turn_control": {
                "action": "end_turn",
                "reason": "handoff complete",
            },
        }
        try:
            with (
                patch.object(
                    runtime,
                    "_invoke_teamflow_tool",
                    return_value=result,
                ) as invoke,
                patch.object(
                    runtime.tool_runtime,
                    "sync_task_activity",
                ),
                patch.object(
                    runtime.tool_runtime,
                    "delivery_record_id",
                    return_value="recHandoff",
                ),
            ):
                grant = runtime.authorize_tool(
                    invocation_id="invocation_handoff",
                    session_id="session_handoff",
                    cwd=self.workspace,
                    turn_id="turn_handoff",
                    tool_name="mcp__teamflow__route_task",
                    tool_input=arguments,
                )
                delayed_grant = runtime.authorize_tool(
                    invocation_id="invocation_delayed_after_handoff",
                    session_id="session_handoff",
                    cwd=self.workspace,
                    turn_id="turn_handoff",
                    tool_name="mcp__teamflow__get_assignment",
                    tool_input={},
                )
                first = runtime.invoke_tool(
                    invocation_id="invocation_handoff",
                    grant=grant["grant"],
                    tool_name="route_task",
                    arguments=arguments,
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "handoff is complete",
                ):
                    runtime.invoke_tool(
                        invocation_id="invocation_delayed_after_handoff",
                        grant=delayed_grant["grant"],
                        tool_name="get_assignment",
                        arguments={},
                    )
                retry_grant = runtime.authorize_tool(
                    invocation_id="invocation_handoff",
                    session_id="session_handoff",
                    cwd=self.workspace,
                    turn_id="turn_handoff",
                    tool_name="mcp__teamflow__route_task",
                    tool_input=arguments,
                )
                retried = runtime.invoke_tool(
                    invocation_id="invocation_handoff",
                    grant=retry_grant["grant"],
                    tool_name="route_task",
                    arguments=arguments,
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "handoff is complete",
                ):
                    runtime.authorize_tool(
                        invocation_id="invocation_after_handoff",
                        session_id="session_handoff",
                        cwd=self.workspace,
                        turn_id="turn_handoff",
                        tool_name="mcp__teamflow__get_assignment",
                        tool_input={},
                    )

            self.assertEqual(first, result)
            self.assertEqual(retried, result)
            invoke.assert_called_once()
        finally:
            runtime.close()

    def test_completed_handoff_remains_closed_after_daemon_restart(self):
        context = self.context()
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'pm'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES ('agent_durable_handoff', ?, ?, ?, 'pm', 'codex',
                          'session_durable_handoff', 'Durable Handoff PM', ?, ?)
                """,
                (
                    workspace["id"],
                    workspace["current_workflow_id"],
                    role["id"],
                    now(),
                    now(),
                ),
            )
        save_task_snapshot(
            context,
            record_id="recDurableHandoff",
            task={
                "record_id": "recDurableHandoff",
                "task_id": "TF-DURABLE-HANDOFF",
                "title": "Stop the old PM turn",
                "status": "ready",
                "role": "pm",
            },
            source_event_id="evtDurableHandoffPm",
            source_revision="70",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        mark_task_delivery_turn_started(
            context,
            delivery_id=delivery["id"],
            turn_id="turn_durable_handoff",
        )
        save_task_snapshot(
            context,
            record_id="recDurableHandoff",
            task={
                "record_id": "recDurableHandoff",
                "task_id": "TF-DURABLE-HANDOFF",
                "title": "Stop the old PM turn",
                "status": "ready",
                "role": "tl",
            },
            source_event_id="evtDurableHandoffTl",
            source_revision="71",
        )
        prepare_task_deliveries(context)
        register_workspace(self.workspace, enabled=True)

        runtime = TeamFlowDaemon()
        runtime.routes[self.workspace] = context
        try:
            with self.assertRaisesRegex(ValueError, "handoff is complete"):
                runtime.authorize_tool(
                    invocation_id="invocation_after_restart",
                    session_id="session_durable_handoff",
                    cwd=self.workspace,
                    turn_id="turn_durable_handoff",
                    tool_name="mcp__teamflow__get_assignment",
                    tool_input={},
                )
        finally:
            runtime.close()

    def test_claimed_execution_keeps_delivery_turn_open(self):
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
                ) VALUES ('agent_claim_turn', ?, ?, ?, 'tl', 'codex',
                          'session_claim_turn', 'Claim Turn TL', ?, ?)
                """,
                (
                    workspace["id"],
                    workspace["current_workflow_id"],
                    role["id"],
                    now(),
                    now(),
                ),
            )
        ready = {
            "record_id": "recClaimTurn",
            "task_id": "TF-CLAIM-TURN",
            "title": "Keep working after claim",
            "status": "ready",
            "role": "tl",
        }
        save_task_snapshot(
            context,
            record_id="recClaimTurn",
            task=ready,
            source_event_id="evtClaimTurnReady",
            source_revision="72",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        mark_task_delivery_turn_started(
            context,
            delivery_id=delivery["id"],
            turn_id="turn_claim_execution",
        )

        claimed = {
            **ready,
            "status": "in_progress",
            "agent": "Claim Turn TL",
            "agent_id": "agent_claim_turn",
        }
        save_task_snapshot(
            context,
            record_id="recClaimTurn",
            task=claimed,
            source_event_id="evtClaimTurnInProgress",
            source_revision="73",
        )
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            conn.execute(
                """
                INSERT INTO task_executions (
                  record_id, agent_id, session_id, turn_id, state, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?)
                """,
                (
                    "recClaimTurn",
                    "agent_claim_turn",
                    "session_claim_turn",
                    "turn_claim_execution",
                    now(),
                ),
            )

        self.assertFalse(task_delivery_is_current(
            context,
            delivery_id=delivery["id"],
        ))
        self.assertTrue(task_delivery_turn_is_current(
            context,
            turn_id="turn_claim_execution",
            agent_id="agent_claim_turn",
        ))

        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            conn.execute(
                """
                UPDATE task_executions
                SET state = 'stopped', updated_at = ?
                WHERE record_id = ?
                """,
                (now(), "recClaimTurn"),
            )
        self.assertFalse(task_delivery_turn_is_current(
            context,
            turn_id="turn_claim_execution",
            agent_id="agent_claim_turn",
        ))

    def test_routing_a_child_task_does_not_close_the_parent_task_turn(self):
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'pm'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES ('agent_child_route', ?, ?, ?, 'pm', 'codex',
                          'session_child_route', 'Child Route PM', ?, ?)
                """,
                (
                    workspace["id"],
                    workspace["current_workflow_id"],
                    role["id"],
                    now(),
                    now(),
                ),
            )
        register_workspace(self.workspace, enabled=True)
        runtime = TeamFlowDaemon()
        runtime.routes[self.workspace] = self.context()
        arguments = {"record_id": "recChild", "role": "tl"}
        try:
            with (
                patch.object(
                    runtime,
                    "_invoke_teamflow_tool",
                    return_value={
                        "ok": True,
                        "task": {
                            "record_id": "recChild",
                            "status": "ready",
                            "role": "tl",
                        },
                    },
                ),
                patch.object(
                    runtime.tool_runtime,
                    "sync_task_activity",
                ),
                patch.object(
                    runtime.tool_runtime,
                    "delivery_record_id",
                    return_value="recParent",
                ),
            ):
                grant = runtime.authorize_tool(
                    invocation_id="invocation_child_route",
                    session_id="session_child_route",
                    cwd=self.workspace,
                    turn_id="turn_parent",
                    tool_name="mcp__teamflow__route_task",
                    tool_input=arguments,
                )
                result = runtime.invoke_tool(
                    invocation_id="invocation_child_route",
                    grant=grant["grant"],
                    tool_name="route_task",
                    arguments=arguments,
                )
                next_grant = runtime.authorize_tool(
                    invocation_id="invocation_continue_parent",
                    session_id="session_child_route",
                    cwd=self.workspace,
                    turn_id="turn_parent",
                    tool_name="mcp__teamflow__get_assignment",
                    tool_input={},
                )

            self.assertNotIn("turn_control", result)
            self.assertTrue(next_grant["grant"])
        finally:
            runtime.close()

    def test_retried_invocation_reuses_the_cached_mutation_result(self):
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
                ) VALUES ('agent_cached', ?, ?, ?, 'tl', 'codex',
                          'session_cached', 'Cached TL', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )
        register_workspace(self.workspace, enabled=True)
        runtime = TeamFlowDaemon()
        runtime.routes[self.workspace] = self.context()
        arguments = {"record_id": "recCached"}
        try:
            with patch(
                "core.daemon.claim_task",
                return_value={
                    "ok": True,
                    "task": {
                        "record_id": "recCached",
                        "status": "in_progress",
                        "agent_id": "agent_cached",
                    },
                },
            ) as mutation:
                first_grant = runtime.authorize_tool(
                    invocation_id="invocation_cached",
                    session_id="session_cached",
                    cwd=self.workspace,
                    turn_id="turn_cached",
                    tool_name="mcp__teamflow__claim_task",
                    tool_input=arguments,
                )
                first = runtime.invoke_tool(
                    invocation_id="invocation_cached",
                    grant=first_grant["grant"],
                    tool_name="claim_task",
                    arguments=arguments,
                )
                second_grant = runtime.authorize_tool(
                    invocation_id="invocation_cached",
                    session_id="session_cached",
                    cwd=self.workspace,
                    turn_id="turn_cached",
                    tool_name="mcp__teamflow__claim_task",
                    tool_input=arguments,
                )
                second = runtime.invoke_tool(
                    invocation_id="invocation_cached",
                    grant=second_grant["grant"],
                    tool_name="claim_task",
                    arguments=arguments,
                )

            self.assertEqual(first, second)
            mutation.assert_called_once()
            with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
                execution = conn.execute(
                    "SELECT * FROM task_executions WHERE record_id = 'recCached'"
                ).fetchone()
            self.assertEqual(execution["agent_id"], "agent_cached")
            self.assertEqual(execution["session_id"], "session_cached")
            self.assertEqual(execution["turn_id"], "turn_cached")
            self.assertEqual(execution["state"], "active")
        finally:
            runtime.close()

    def test_concurrent_retried_invocation_executes_one_mutation(self):
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
                ) VALUES ('agent_concurrent', ?, ?, ?, 'tl', 'codex',
                          'session_concurrent', 'Concurrent TL', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )
        register_workspace(self.workspace, enabled=True)
        runtime = TeamFlowDaemon()
        runtime.routes[self.workspace] = self.context()
        arguments = {"record_id": "recConcurrent"}
        grants = [
            runtime.authorize_tool(
                invocation_id="invocation_concurrent",
                session_id="session_concurrent",
                cwd=self.workspace,
                turn_id="turn_concurrent",
                tool_name="mcp__teamflow__claim_task",
                tool_input=arguments,
            )
            for _ in range(2)
        ]
        results = []
        try:
            with patch(
                "core.daemon.claim_task",
                return_value={"ok": True, "task": {"record_id": "recConcurrent"}},
            ) as mutation:
                threads = [
                    threading.Thread(
                        target=lambda grant=grant: results.append(runtime.invoke_tool(
                            invocation_id="invocation_concurrent",
                            grant=grant["grant"],
                            tool_name="claim_task",
                            arguments=arguments,
                        ))
                    )
                    for grant in grants
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=2)

            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], results[1])
            mutation.assert_called_once()
        finally:
            runtime.close()

    def test_stop_execution_does_not_hold_the_workspace_mutation_lock(self):
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            roles = {
                row["role_key"]: row
                for row in conn.execute(
                    "SELECT * FROM roles WHERE workflow_id = ? AND role_key IN ('pm', 'tl')",
                    (workspace["current_workflow_id"],),
                )
            }
            timestamp = now()
            for agent_id, role_key, session_id in (
                ("agent_lock_pm", "pm", "session_lock_pm"),
                ("agent_lock_tl", "tl", "session_lock_tl"),
            ):
                conn.execute(
                    """
                    INSERT INTO agents (
                      id, workspace_id, workflow_id, role_id, role_key,
                      harness_type, session_id, display_name, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'codex', ?, ?, ?, ?)
                    """,
                    (
                        agent_id,
                        workspace["id"],
                        workspace["current_workflow_id"],
                        roles[role_key]["id"],
                        role_key,
                        session_id,
                        agent_id,
                        timestamp,
                        timestamp,
                    ),
                )
        register_workspace(self.workspace, enabled=True)
        runtime = TeamFlowDaemon()
        runtime.routes[self.workspace] = self.context()
        stop_arguments = {
            "record_id": "recLock",
            "reason": "停止测试",
            "confirmed": True,
        }
        update_arguments = {
            "record_id": "recLock",
            "fields": {"progress": "仍可写入"},
        }
        stop_grant = runtime.authorize_tool(
            invocation_id="invocation_stop_lock",
            session_id="session_lock_pm",
            cwd=self.workspace,
            turn_id="turn_lock_pm",
            tool_name="mcp__teamflow__stop_task_execution",
            tool_input=stop_arguments,
        )
        update_grant = runtime.authorize_tool(
            invocation_id="invocation_update_lock",
            session_id="session_lock_tl",
            cwd=self.workspace,
            turn_id="turn_lock_tl",
            tool_name="mcp__teamflow__update_task",
            tool_input=update_arguments,
        )
        stop_started = threading.Event()
        update_finished = threading.Event()
        results = []

        def invoke(assignment, tool_name, arguments, *, invocation_id):
            if tool_name == "stop_task_execution":
                stop_started.set()
                if not update_finished.wait(timeout=2):
                    raise AssertionError("update_task was blocked by stop_task_execution")
            elif tool_name == "update_task":
                update_finished.set()
            return {"ok": True}

        try:
            with patch.object(runtime, "_invoke_teamflow_tool", side_effect=invoke):
                stop_worker = threading.Thread(
                    target=lambda: results.append(runtime.invoke_tool(
                        invocation_id="invocation_stop_lock",
                        grant=stop_grant["grant"],
                        tool_name="stop_task_execution",
                        arguments=stop_arguments,
                    ))
                )
                update_worker = threading.Thread(
                    target=lambda: results.append(runtime.invoke_tool(
                        invocation_id="invocation_update_lock",
                        grant=update_grant["grant"],
                        tool_name="update_task",
                        arguments=update_arguments,
                    ))
                )
                stop_worker.start()
                self.assertTrue(stop_started.wait(timeout=2))
                update_worker.start()
                stop_worker.join(timeout=3)
                update_worker.join(timeout=3)
        finally:
            runtime.close()

        self.assertFalse(stop_worker.is_alive())
        self.assertFalse(update_worker.is_alive())
        self.assertEqual(results, [{"ok": True}, {"ok": True}])

    def test_stop_execution_records_a_project_scoped_receipt(self):
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            roles = {
                row["role_key"]: row
                for row in conn.execute(
                    "SELECT * FROM roles WHERE workflow_id = ? AND role_key IN ('pm', 'tl')",
                    (workspace["current_workflow_id"],),
                )
            }
            timestamp = now()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES ('agent_stop_pm', ?, ?, ?, 'pm', 'codex',
                          'session_stop_pm', 'Stop PM', ?, ?)
                """,
                (
                    workspace["id"],
                    workspace["current_workflow_id"],
                    roles["pm"]["id"],
                    timestamp,
                    timestamp,
                ),
            )
            conn.execute(
                """
                INSERT INTO task_executions (
                  record_id, agent_id, session_id, turn_id, state, updated_at
                ) VALUES (
                  'recStop', 'agent_stop_tl', 'session_stop_tl',
                  'turn_stop', 'active', ?
                )
                """,
                (timestamp,),
            )
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES ('agent_stop_tl', ?, ?, ?, 'tl', 'codex',
                          'session_stop_tl', 'Stop TL', ?, ?)
                """,
                (
                    workspace["id"],
                    workspace["current_workflow_id"],
                    roles["tl"]["id"],
                    timestamp,
                    timestamp,
                ),
            )
        pm = agent_context(self.workspace, session_id="session_stop_pm")["assignment"]
        task = {
            "record_id": "recStop",
            "task_id": "TF-STOP",
            "title": "Stop work",
            "status": "in_progress",
            "type": "development",
            "priority": "P1",
            "role": "tl",
            "description": "Work",
            "acceptance_criteria": "Stopped",
            "agent": "Stop TL",
            "agent_id": "agent_stop_tl",
        }
        runtime = TeamFlowDaemon()
        try:
            with (
                patch(
                    "core.daemon.get_lark_task",
                    return_value={"ok": True, "task": task},
                ),
                patch(
                    "core.daemon.stop_codex_turn",
                    return_value={
                        "ok": True,
                        "thread_id": "session_stop_tl",
                        "turn_id": "turn_stop",
                        "status": "interrupted",
                        "already_stopped": False,
                        "transport": "codex-ipc",
                    },
                ) as stop_turn,
            ):
                result = runtime._invoke_teamflow_tool(
                    pm,
                    "stop_task_execution",
                    {
                        "record_id": "recStop",
                        "reason": "需求撤销",
                        "confirmed": True,
                    },
                    invocation_id="invocation_stop",
                )
                facts = runtime._task_runtime_facts(pm, task)
                repeated = runtime._invoke_teamflow_tool(
                    pm,
                    "stop_task_execution",
                    {
                        "record_id": "recStop",
                        "reason": "需求撤销",
                        "confirmed": True,
                    },
                    invocation_id="invocation_stop_repeated",
                )
                with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
                    stopped_receipt = dict(conn.execute(
                        "SELECT * FROM task_executions WHERE record_id = 'recStop'"
                    ).fetchone())
                tl = agent_context(
                    self.workspace,
                    session_id="session_stop_tl",
                )["assignment"]
                runtime._sync_task_execution_activity(
                    tl,
                    tool_name="get_task",
                    result={"ok": True, "task": task},
                    session_id="session_stop_tl",
                    turn_id="turn_stop_new",
                )
                refreshed_facts = runtime._task_runtime_facts(pm, task)
        finally:
            runtime.close()

        self.assertTrue(result["ok"])
        self.assertIn("execution_stopped", facts)
        self.assertTrue(repeated["already_applied"])
        self.assertNotIn("execution_stopped", refreshed_facts)
        self.assertEqual(stopped_receipt["turn_id"], "turn_stop")
        self.assertEqual(stopped_receipt["state"], "stopped")
        self.assertEqual(stopped_receipt["stopped_by_agent_id"], "agent_stop_pm")
        stop_turn.assert_called_once_with(
            "session_stop_tl",
            expected_turn_id="turn_stop",
        )
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            receipt = conn.execute(
                "SELECT * FROM task_executions WHERE record_id = 'recStop'"
            ).fetchone()
        self.assertEqual(receipt["agent_id"], "agent_stop_tl")
        self.assertEqual(receipt["session_id"], "session_stop_tl")
        self.assertEqual(receipt["turn_id"], "turn_stop_new")
        self.assertEqual(receipt["state"], "active")
        self.assertIsNone(receipt["stopped_by_agent_id"])

    def test_signed_grant_is_rejected_after_workspace_is_disabled(self):
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
                ) VALUES ('agent_disabled', ?, ?, ?, 'tl', 'codex',
                          'session_disabled', 'Disabled TL', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )
        register_workspace(self.workspace, enabled=True)
        runtime = TeamFlowDaemon()
        runtime.routes[self.workspace] = self.context()
        try:
            authorized = runtime.authorize_tool(
                invocation_id="invocation_disabled",
                session_id="session_disabled",
                cwd=self.workspace,
                turn_id="turn_disabled",
                tool_name="mcp__teamflow__get_assignment",
                tool_input={},
            )
            runtime.disable_workspace(self.workspace)
            with self.assertRaisesRegex(ValueError, "workspace was disabled"):
                runtime.invoke_tool(
                    invocation_id="invocation_disabled",
                    grant=authorized["grant"],
                    tool_name="get_assignment",
                    arguments={},
                )
        finally:
            runtime.close()

    def test_create_task_retries_with_the_same_remote_idempotency_token(self):
        context = Mock()
        context.request_context.meta = RequestParams.Meta.model_validate(
            {
                "threadId": "session_create",
                "x-codex-turn-metadata": json.dumps({
                    "thread_id": "session_create",
                    "turn_id": "turn_create",
                }),
            }
        )
        invocation_id = "11111111-1111-4111-8111-111111111111"
        with (
            patch("core.mcp_server.uuid.uuid4", return_value=invocation_id),
            patch("core.mcp_server.time.sleep"),
            patch(
                "core.mcp_server._daemon_request",
                side_effect=[
                    {"grant": "grant_create", "expires_in": 60},
                    ConnectionResetError("daemon restarted"),
                    {"grant": "grant_create_retry", "expires_in": 60},
                    {"ok": True, "task": {"record_id": "recCreated"}},
                ],
            ) as daemon_request,
        ):
            result = teamflow_mcp_server.create_task("Idempotent create", context)

        self.assertTrue(result["ok"])
        self.assertEqual(result["task"]["record_id"], "recCreated")
        self.assertEqual(daemon_request.call_count, 4)
        self.assertEqual(
            {call.args[0]["invocation_id"] for call in daemon_request.call_args_list},
            {invocation_id},
        )

    def test_plugin_context_hook_and_mcp_metadata_resolve_the_registered_agent(self):
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
            repeated_prompt_hook = subprocess.run(
                ["python3", str(ROOT / "hooks" / "user_prompt_submit.py")],
                input=json.dumps({
                    "session_id": "session_hook",
                    "cwd": self.workspace,
                    "turn_id": "turn_hook_2",
                    "hook_event_name": "UserPromptSubmit",
                }),
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(repeated_prompt_hook.stdout, "")
            compact_hook = subprocess.run(
                ["python3", str(ROOT / "hooks" / "user_prompt_submit.py")],
                input=json.dumps({
                    "session_id": "session_hook",
                    "cwd": self.workspace,
                    "turn_id": "turn_compact",
                    "hook_event_name": "PostCompact",
                    "trigger": "manual",
                }),
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(compact_hook.stdout, "")
            compacted = agent_context(
                self.workspace,
                session_id="session_hook",
                consume=False,
            )
            self.assertEqual(
                compacted["context_status"],
                "recovery_pending",
            )
            recovery_hook = subprocess.run(
                ["python3", str(ROOT / "hooks" / "user_prompt_submit.py")],
                input=json.dumps({
                    "session_id": "session_hook",
                    "cwd": self.workspace,
                    "turn_id": "turn_after_compact",
                    "hook_event_name": "UserPromptSubmit",
                }),
                capture_output=True,
                text=True,
                check=True,
            )
            recovery_output = json.loads(recovery_hook.stdout)
            self.assertEqual(
                recovery_output["hookSpecificOutput"]["hookEventName"],
                "UserPromptSubmit",
            )
            self.assertIn(
                "会话压缩后恢复",
                recovery_output["hookSpecificOutput"]["additionalContext"],
            )

            async def call_assignment():
                parameters = StdioServerParameters(
                    command=sys.executable,
                    args=[str(ROOT / "scripts" / "teamflow.py"), "mcp-server"],
                    cwd=ROOT,
                    env=dict(os.environ),
                )
                async with stdio_client(parameters) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        return await session.call_tool(
                            "get_assignment",
                            {},
                            meta={
                                "threadId": "session_hook",
                                "x-codex-turn-metadata": {
                                    "thread_id": "session_hook",
                                    "turn_id": "turn_hook",
                                },
                            },
                        )

            try:
                default_loop = asyncio.get_event_loop_policy().get_event_loop()
            except RuntimeError:
                default_loop = None
            try:
                result = anyio.run(call_assignment)
            finally:
                if default_loop is not None:
                    default_loop.close()

            self.assertFalse(result.isError)
            self.assertEqual(result.structuredContent["assignment"]["agent_id"], "agent_hook")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            runtime.close()

    def test_mcp_tools_only_expose_business_arguments(self):
        tools = {
            tool.name: tool.parameters
            for tool in teamflow_mcp_server.mcp._tool_manager.list_tools()
        }

        self.assertEqual(tools["get_assignment"]["properties"], {})
        self.assertEqual(tools["list_available_tasks"]["properties"], {})
        self.assertEqual(set(tools["get_task"]["properties"]), {"record_id"})
        self.assertEqual(set(tools["claim_task"]["properties"]), {"record_id"})
        self.assertEqual(
            set(tools),
            set(TEAMFLOW_MCP_TOOLS),
        )
        self.assertEqual(
            set(tools["stop_task_execution"]["properties"]),
            {"record_id", "reason", "confirmed"},
        )
        self.assertEqual(
            set(tools["submit_task"]["properties"]),
            {"record_id", "outcome", "result_evidence", "progress", "next_action"},
        )
        self.assertEqual(
            set(tools["review_task"]["properties"]),
            {"record_id", "decision", "result_evidence", "role", "next_action"},
        )

    def test_mcp_rejects_missing_or_inconsistent_codex_identity(self):
        missing = Mock()
        missing.request_context.meta = None
        with self.assertRaisesRegex(ValueError, "requires Codex MCP request metadata"):
            teamflow_mcp_server.get_assignment(missing)

        inconsistent = Mock()
        inconsistent.request_context.meta = RequestParams.Meta.model_validate(
            {
                "threadId": "session_a",
                "x-codex-turn-metadata": json.dumps({
                    "thread_id": "session_b",
                    "turn_id": "turn_mismatch",
                }),
            }
        )
        with self.assertRaisesRegex(ValueError, "inconsistent Codex thread metadata"):
            teamflow_mcp_server.get_assignment(inconsistent)

    def test_mcp_internal_grant_uses_codex_thread_and_turn_metadata(self):
        context = Mock()
        context.request_context.meta = RequestParams.Meta.model_validate(
            {
                "threadId": "session_metadata",
                "x-codex-turn-metadata": json.dumps({
                    "thread_id": "session_metadata",
                    "turn_id": "turn_metadata",
                }),
            }
        )
        with patch(
            "core.mcp_server._daemon_request",
            side_effect=[
                {"grant": "grant_metadata", "expires_in": 60},
                {"ok": True, "task": {"record_id": "recMetadata"}},
            ],
        ) as daemon_request:
            result = teamflow_mcp_server.get_task("recMetadata", context)

        self.assertEqual(result["task"]["record_id"], "recMetadata")
        invocation_id = daemon_request.call_args_list[0].args[0]["invocation_id"]
        self.assertTrue(invocation_id)
        self.assertEqual(
            daemon_request.call_args_list[0].args[0],
            {
                "action": "authorize_tool",
                "invocation_id": invocation_id,
                "session_id": "session_metadata",
                "turn_id": "turn_metadata",
                "tool_name": "mcp__teamflow__get_task",
                "tool_input": {"record_id": "recMetadata"},
            },
        )
        self.assertEqual(
            daemon_request.call_args_list[1].args[0],
            {
                "action": "invoke_tool",
                "invocation_id": invocation_id,
                "grant": "grant_metadata",
                "tool_name": "get_task",
                "arguments": {"record_id": "recMetadata"},
            },
        )

    def test_mcp_retries_the_same_invocation_while_daemon_restarts(self):
        context = Mock()
        context.request_context.meta = RequestParams.Meta.model_validate(
            {
                "threadId": "session_retry",
                "x-codex-turn-metadata": json.dumps({
                    "thread_id": "session_retry",
                    "turn_id": "turn_retry",
                }),
            }
        )
        invocation_uuid = "22222222-2222-4222-8222-222222222222"
        with (
            patch("core.mcp_server.uuid.uuid4", return_value=invocation_uuid),
            patch("core.mcp_server.time.sleep"),
            patch(
                "core.mcp_server._daemon_request",
                side_effect=[
                    FileNotFoundError("daemon.sock"),
                    {"grant": "grant_retry", "expires_in": 60},
                    {"ok": True, "assignment": {"agent_id": "agent_retry"}},
                ],
            ) as daemon_request,
        ):
            result = teamflow_mcp_server.get_assignment(context)

        self.assertEqual(result["assignment"]["agent_id"], "agent_retry")
        self.assertEqual(daemon_request.call_count, 3)
        self.assertEqual(
            daemon_request.call_args_list[1].args[0]["invocation_id"],
            invocation_uuid,
        )
        self.assertEqual(
            daemon_request.call_args_list[2].args[0]["invocation_id"],
            invocation_uuid,
        )

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
                "description": "Old task snapshot",
            },
            source_event_id="evtTurn",
            source_revision="30",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        runtime = TeamFlowDaemon()
        runtime.active_sessions.add("session_turn")

        def complete_turn(
            thread_id,
            prompt,
            *,
            client_message_id,
            on_started,
            stop_event,
            required_mcp_tools,
        ):
            self.assertTrue(required_mcp_tools)
            self.assertEqual(client_message_id, delivery["client_message_id"])
            self.assertIn("Latest task snapshot", prompt)
            self.assertNotIn("Old task snapshot", prompt)
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
                patch("core.daemon.get_lark_task", return_value={
                    "task": {
                        **json.loads(delivery["after_json"]),
                        "description": "Latest task snapshot",
                    }
                }),
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

    def test_daemon_cancels_delivery_when_live_task_no_longer_targets_the_agent(self):
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
                ) VALUES ('agent_stale', ?, ?, ?, 'tl', 'codex',
                          'session_stale', 'Stale TL', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )
        save_task_snapshot(
            context,
            record_id="recStale",
            task={
                "record_id": "recStale",
                "task_id": "TF-STALE",
                "title": "Stale delivery",
                "status": "ready",
                "role": "tl",
            },
            source_event_id="evtStale",
            source_revision="31",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        runtime = TeamFlowDaemon()
        runtime.active_sessions.add("session_stale")
        output = io.StringIO()
        try:
            with (
                patch("core.daemon.get_lark_task", return_value={
                    "task": {
                        **json.loads(delivery["after_json"]),
                        "status": "backlog",
                    }
                }),
                patch("core.daemon.run_codex_turn") as run_turn,
                redirect_stdout(output),
            ):
                runtime._execute_task_delivery(context, delivery)
        finally:
            runtime.close()

        run_turn.assert_not_called()
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            saved = conn.execute(
                "SELECT status, last_error FROM task_event_deliveries"
            ).fetchone()
        self.assertEqual(saved["status"], "canceled")
        self.assertIn("更新的状态事件", saved["last_error"])
        self.assertIn("DISPATCH NOT-REQUIRED", output.getvalue())

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

        def interrupt_turn(
            thread_id,
            prompt,
            *,
            client_message_id,
            on_started,
            stop_event,
            required_mcp_tools,
        ):
            self.assertTrue(required_mcp_tools)
            self.assertEqual(client_message_id, delivery["client_message_id"])
            on_started("turn_interrupted")
            stop_event.set()
            raise RuntimeError("plugin warning from app-server stderr")

        try:
            output = io.StringIO()
            with (
                patch("core.daemon.get_lark_task", return_value={
                    "task": json.loads(delivery["after_json"])
                }),
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
        self.assertEqual(
            saved["last_error"],
            "TeamFlow daemon stopped while the Codex turn was running",
        )
        self.assertIn("DISPATCH RECONCILING", output.getvalue())
        self.assertIn("turn=turn_interrupted", output.getvalue())
        self.assertNotIn("plugin warning", output.getvalue())
        self.assertNotIn("DISPATCH RETRY", output.getvalue())

        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            conn.execute(
                "UPDATE task_event_deliveries SET next_attempt_at = NULL"
            )
        runtime = TeamFlowDaemon()
        retry_output = io.StringIO()
        try:
            with (
                patch("core.daemon.read_codex_thread", return_value={
                    "status": {"type": "idle"},
                    "turns": [{
                        "id": "turn_interrupted",
                        "status": "interrupted",
                    }],
                }),
                redirect_stdout(retry_output),
            ):
                runtime._reconcile_task_deliveries(context)
        finally:
            runtime.close()

        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            retried = conn.execute(
                """
                SELECT status, turn_id, turn_status
                FROM task_event_deliveries
                """
            ).fetchone()
        self.assertEqual(
            (retried["status"], retried["turn_id"], retried["turn_status"]),
            ("retry", "turn_interrupted", "interrupted"),
        )
        self.assertIn("DISPATCH RETRY", retry_output.getvalue())

        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            conn.execute(
                """
                UPDATE task_event_deliveries
                SET status = 'processing',
                    attempts = 3,
                    turn_status = 'inProgress',
                    next_attempt_at = NULL
                """
            )
        runtime = TeamFlowDaemon()
        failed_output = io.StringIO()
        try:
            with (
                patch("core.daemon.read_codex_thread", return_value={
                    "status": {"type": "idle"},
                    "turns": [{
                        "id": "turn_interrupted",
                        "status": "interrupted",
                    }],
                }),
                redirect_stdout(failed_output),
            ):
                runtime._reconcile_task_deliveries(context)
        finally:
            runtime.close()

        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            exhausted = conn.execute(
                """
                SELECT status, attempts, turn_id, turn_status
                FROM task_event_deliveries
                """
            ).fetchone()
        self.assertEqual(
            (
                exhausted["status"],
                exhausted["attempts"],
                exhausted["turn_id"],
                exhausted["turn_status"],
            ),
            ("failed", 3, "turn_interrupted", "interrupted"),
        )
        self.assertIn("DISPATCH FAILED", failed_output.getvalue())
        self.assertNotIn("DISPATCH RETRY", failed_output.getvalue())

    def test_daemon_retries_without_a_turn_when_the_codex_session_is_busy(self):
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
                ) VALUES ('agent_busy', ?, ?, ?, 'tl', 'codex',
                          'session_busy', 'Busy TL', ?, ?)
                """,
                (
                    workspace["id"],
                    workspace["current_workflow_id"],
                    role["id"],
                    now(),
                    now(),
                ),
            )
        save_task_snapshot(
            context,
            record_id="recBusy",
            task={
                "record_id": "recBusy",
                "task_id": "TF-BUSY",
                "title": "Wait for the busy session",
                "status": "ready",
                "role": "tl",
            },
            source_event_id="evtBusy",
            source_revision="49",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        runtime = TeamFlowDaemon()
        runtime.active_sessions.add("session_busy")

        try:
            output = io.StringIO()
            with (
                patch("core.daemon.get_lark_task", return_value={
                    "task": json.loads(delivery["after_json"])
                }),
                patch(
                    "core.daemon.run_codex_turn",
                    side_effect=ValueError("Codex agent is busy"),
                ),
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
            (
                saved["status"],
                saved["attempts"],
                saved["turn_id"],
                saved["turn_status"],
            ),
            ("retry", 1, None, None),
        )
        self.assertIn("Codex agent is busy", saved["last_error"])
        self.assertNotIn("session_busy", runtime.active_sessions)
        self.assertIn("DISPATCH RETRY", output.getvalue())
        self.assertNotIn("DISPATCH STARTED", output.getvalue())

    def test_daemon_waits_for_background_mcp_authorization_before_starting_turn(self):
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
                ) VALUES ('agent_permission', ?, ?, ?, 'tl', 'codex',
                          'session_permission', 'Permission TL', ?, ?)
                """,
                (
                    workspace["id"],
                    workspace["current_workflow_id"],
                    role["id"],
                    now(),
                    now(),
                ),
            )
        save_task_snapshot(
            context,
            record_id="recPermission",
            task={
                "record_id": "recPermission",
                "task_id": "TF-PERMISSION",
                "title": "Wait for MCP authorization",
                "status": "ready",
                "role": "tl",
            },
            source_event_id="evtPermission",
            source_revision="50",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        runtime = TeamFlowDaemon()
        runtime.active_sessions.add("session_permission")
        runtime.delivery_runtime.background_mcp_ready = lambda: {
            "authorized": False,
            "configured": False,
            "missing_tools": ["update_task"],
        }
        output = io.StringIO()
        try:
            with (
                patch("core.daemon.get_lark_task", return_value={
                    "task": json.loads(delivery["after_json"])
                }),
                patch("core.daemon.run_codex_turn") as run_turn,
                redirect_stdout(output),
            ):
                runtime._execute_task_delivery(context, delivery)

            with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
                waiting = conn.execute(
                    """
                    SELECT status, turn_id, last_error
                    FROM task_event_deliveries
                    """
                ).fetchone()
            self.assertEqual(waiting["status"], "waiting_permission")
            self.assertIsNone(waiting["turn_id"])
            self.assertIn("update_task", waiting["last_error"])
            self.assertIn("DISPATCH WAITING", output.getvalue())
            self.assertNotIn("DISPATCH STARTED", output.getvalue())
            run_turn.assert_not_called()

            runtime.delivery_runtime.resume_permission_waiting(context)
            with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT status FROM task_event_deliveries"
                    ).fetchone()["status"],
                    "waiting_permission",
                )

            runtime.delivery_runtime.background_mcp_ready = lambda: {
                "authorized": True,
                "configured": True,
                "missing_tools": [],
            }
            runtime.delivery_runtime.resume_permission_waiting(context)
            with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT status FROM task_event_deliveries"
                    ).fetchone()["status"],
                    "retry",
                )
        finally:
            runtime.close()

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

    def test_daemon_recovers_an_unconfirmed_turn_by_client_message_id(self):
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
                ) VALUES ('agent_message_recovery', ?, ?, ?, 'tl', 'codex',
                          'session_message_recovery', 'Message Recovery TL', ?, ?)
                """,
                (
                    workspace["id"],
                    workspace["current_workflow_id"],
                    role["id"],
                    now(),
                    now(),
                ),
            )
        save_task_snapshot(
            context,
            record_id="recMessageRecovery",
            task={
                "record_id": "recMessageRecovery",
                "task_id": "TF-MESSAGE-RECOVERY",
                "title": "Recover unknown IPC acceptance",
                "status": "ready",
                "role": "tl",
            },
            source_event_id="evtMessageRecovery",
            source_revision="72",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            conn.execute(
                "UPDATE task_event_deliveries SET next_attempt_at = NULL"
            )

        runtime = TeamFlowDaemon()
        output = io.StringIO()
        try:
            with (
                patch("core.daemon.read_codex_thread", return_value={
                    "status": {"type": "idle"},
                    "turns": [{
                        "id": "turn_message_recovery",
                        "status": "completed",
                        "items": [{
                            "type": "userMessage",
                            "clientId": delivery["client_message_id"],
                        }],
                    }],
                }),
                redirect_stdout(output),
            ):
                runtime._reconcile_task_deliveries(context)
        finally:
            runtime.close()

        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            saved = conn.execute(
                """
                SELECT status, turn_id, turn_status
                FROM task_event_deliveries
                """
            ).fetchone()
        self.assertEqual(
            (saved["status"], saved["turn_id"], saved["turn_status"]),
            ("completed", "turn_message_recovery", "completed"),
        )
        self.assertIn("DISPATCH STARTED", output.getvalue())
        self.assertIn("recovered from client message ID", output.getvalue())
        self.assertIn("DISPATCH RECOVERED", output.getvalue())

    def test_daemon_defers_then_retries_an_unconfirmed_turn(self):
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
                ) VALUES ('agent_missing', ?, ?, ?, 'tl', 'codex',
                          'session_missing', 'Missing TL', ?, ?)
                """,
                (
                    workspace["id"],
                    workspace["current_workflow_id"],
                    role["id"],
                    now(),
                    now(),
                ),
            )
        save_task_snapshot(
            context,
            record_id="recMissing",
            task={
                "record_id": "recMissing",
                "task_id": "TF-MISSING",
                "title": "Retry a missing turn",
                "status": "ready",
                "role": "tl",
            },
            source_event_id="evtMissing",
            source_revision="50",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        mark_task_delivery_turn_started(
            context,
            delivery_id=delivery["id"],
            turn_id="turn_missing",
        )
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            conn.execute("UPDATE task_event_deliveries SET next_attempt_at = NULL")

        runtime = TeamFlowDaemon()
        try:
            output = io.StringIO()
            with (
                patch("core.daemon.read_codex_thread", return_value={
                    "status": {"type": "idle"},
                    "turns": [],
                }),
                redirect_stdout(output),
            ):
                runtime._reconcile_task_deliveries(context)
        finally:
            runtime.close()

        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            saved = conn.execute(
                """
                SELECT status, turn_id, turn_status, last_error
                FROM task_event_deliveries
                """
            ).fetchone()
        self.assertEqual(
            (saved["status"], saved["turn_id"], saved["turn_status"]),
            ("processing", "turn_missing", "inProgress"),
        )
        self.assertIn("not visible", saved["last_error"])
        self.assertNotIn("DISPATCH RETRY", output.getvalue())

        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            conn.execute(
                """
                UPDATE task_event_deliveries
                SET started_at = '2000-01-01T00:00:00+00:00',
                    next_attempt_at = NULL
                """
            )
        runtime = TeamFlowDaemon()
        try:
            expired_output = io.StringIO()
            with (
                patch("core.daemon.read_codex_thread", return_value={
                    "status": {"type": "active"},
                    "turns": [],
                }),
                redirect_stdout(expired_output),
            ):
                runtime._reconcile_task_deliveries(context)
        finally:
            runtime.close()

        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            expired = conn.execute(
                """
                SELECT status, turn_id, turn_status, last_error,
                       client_message_id
                FROM task_event_deliveries
                """
            ).fetchone()
        self.assertEqual(
            (expired["status"], expired["turn_id"], expired["turn_status"]),
            ("retry", "turn_missing", "unconfirmed"),
        )
        self.assertIsNone(expired["client_message_id"])
        self.assertIn("remained unconfirmed", expired["last_error"])
        self.assertIn("DISPATCH RETRY", expired_output.getvalue())
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            conn.execute(
                "UPDATE task_event_deliveries SET next_attempt_at = NULL"
            )
        retried = claim_task_deliveries(context)[0]
        self.assertNotEqual(
            retried["client_message_id"],
            delivery["client_message_id"],
        )

    def test_daemon_retries_a_completed_turn_with_interrupted_mcp_calls(self):
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
                ) VALUES ('agent_mcp_restart', ?, ?, ?, 'tl', 'codex',
                          'session_mcp_restart', 'MCP Restart TL', ?, ?)
                """,
                (workspace["id"], workspace["current_workflow_id"], role["id"], now(), now()),
            )
        save_task_snapshot(
            context,
            record_id="recMcpRestart",
            task={
                "record_id": "recMcpRestart",
                "task_id": "TF-0022",
                "title": "Retry interrupted MCP work",
                "status": "ready",
                "role": "tl",
            },
            source_event_id="evtMcpRestart",
            source_revision="51",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        mark_task_delivery_turn_started(
            context,
            delivery_id=delivery["id"],
            turn_id="turn_mcp_restart",
        )
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            conn.execute("UPDATE task_event_deliveries SET next_attempt_at = NULL")
        runtime = TeamFlowDaemon()
        try:
            output = io.StringIO()
            with (
                patch("core.daemon.read_codex_thread", return_value={
                    "turns": [{
                        "id": "turn_mcp_restart",
                        "status": "completed",
                        "items": [{
                            "type": "mcpToolCall",
                            "server": "teamflow",
                            "tool": "get_task",
                            "status": "failed",
                            "arguments": {"record_id": "recMcpRestart"},
                            "result": {
                                "content": [{
                                    "type": "text",
                                    "text": "Error executing tool get_task: [Errno 2] No such file or directory",
                                }]
                            },
                        }],
                    }]
                }),
                redirect_stdout(output),
            ):
                runtime._reconcile_task_deliveries(context)
        finally:
            runtime.close()

        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            saved = conn.execute(
                "SELECT status, turn_id, turn_status, last_error FROM task_event_deliveries"
            ).fetchone()
        self.assertEqual(
            (saved["status"], saved["turn_id"], saved["turn_status"]),
            ("retry", "turn_mcp_restart", "completed"),
        )
        self.assertIn("get_task", saved["last_error"])
        self.assertIn("DISPATCH RETRY", output.getvalue())
        self.assertNotIn("DISPATCH RECOVERED", output.getvalue())

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
            with (
                patch("core.daemon.get_lark_task", return_value={
                    "task": json.loads(delivery["after_json"])
                }),
                patch(
                    "core.daemon.run_codex_turn",
                    side_effect=ValueError("no rollout found for thread id session_deleted"),
                ),
                redirect_stdout(io.StringIO()),
            ):
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

    def test_ui_development_output_is_outside_the_production_build_directory(self):
        dist_dir = ui_dist_dir(self.workspace)

        self.assertTrue(dist_dir.startswith(".next-workspaces/"))
        self.assertFalse(dist_dir.startswith(".next/"))
        self.assertEqual(dist_dir, ui_dist_dir(self.workspace))
        self.assertNotEqual(dist_dir, ui_dist_dir(f"{self.workspace}-other"))

    def test_ui_stops_cleanly_on_keyboard_interrupt(self):
        args = Mock(workspace=self.workspace, host="127.0.0.1", port=12346)
        with patch("scripts.teamflow.init_workspace"), patch("scripts.teamflow.register_workspace"), patch(
            "scripts.teamflow.ensure_ui_dependencies"
        ), patch("scripts.teamflow.subprocess.call", side_effect=KeyboardInterrupt):
            result = cmd_serve_ui(args)

        self.assertEqual(result, 130)

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
