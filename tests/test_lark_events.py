from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.config import resolve_workspace_paths
from core.daemon import DaemonServer, TeamFlowDaemon, _daemon_request, register_workspace, registered_workspaces
from core.db import connect, configure_lark_board, configure_lark_identity, init_workspace
from core.lark_events import (
    LarkEventContext,
    event_matches_board,
    event_record_ids,
    lark_listener_details,
    listen_lark_board_events,
    run_lark_app_worker,
    subscribe_lark_board_events,
    verify_lark_board_listener,
)


ROOT = Path(__file__).resolve().parents[1]


class LarkEventsTest(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
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
            runtime.sync_workspace(self.workspace)
        runtime.workers.clear()
        runtime.close()

        stop_worker.assert_called_once_with(previous_worker)

    def test_global_database_tracks_workspace_paths(self):
        with tempfile.TemporaryDirectory(prefix="teamflow-home-", dir=ROOT / "tmp") as home, patch.dict(
            os.environ, {"TEAMFLOW_HOME": home}
        ):
            register_workspace(self.workspace)
            register_workspace(self.workspace)

            self.assertEqual(registered_workspaces(), [str(Path(self.workspace).resolve())])
            self.assertTrue((Path(home) / "teamflow.db").exists())
            self.assertFalse((Path(home) / "registry.db").exists())

    def test_ipc_sends_sync_to_the_running_daemon(self):
        with tempfile.TemporaryDirectory(prefix="teamflow-ipc-", dir=ROOT / "tmp") as home:
            socket_path = Path(home) / "daemon.sock"
            runtime = Mock()
            runtime.sync_workspace.return_value = {"workspace_root": self.workspace, "daemon_pid": 123}
            server = DaemonServer(str(socket_path), runtime)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch("core.daemon.daemon_socket_path", return_value=socket_path):
                    result = _daemon_request(
                        {"action": "sync_workspace", "workspace": self.workspace, "identity_id": self.identity_id},
                        timeout=2,
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual(result["daemon_pid"], 123)
        runtime.sync_workspace.assert_called_once_with(self.workspace, identity_id=self.identity_id)

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
