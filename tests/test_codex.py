from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.codex import (
    _CodexIpcConnection,
    _CodexIpcUnavailable,
    _CodexThreadStream,
    _notify_codex_clients_thread_changed,
    codex_developer_context_evidence,
    codex_thread_is_permanently_unavailable,
    codex_thread_settings,
    codex_turn,
    codex_turn_unresolved_teamflow_mcp_failures,
    read_codex_thread,
    run_codex_turn,
    stop_codex_turn,
    _run_codex_app_server_turn,
    _stop_app_server,
)


class CodexTurnTest(unittest.TestCase):
    def test_stopping_app_server_closes_all_process_pipes(self):
        process = Mock()
        process.poll.return_value = None

        _stop_app_server(process)

        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=2)
        process.stdin.close.assert_called_once_with()
        process.stdout.close.assert_called_once_with()
        process.stderr.close.assert_called_once_with()

    def test_stale_ipc_socket_falls_back_instead_of_leaking_connection_refused(self):
        metadata = Mock(st_mode=0, st_uid=1000)
        client_socket = Mock()
        client_socket.connect.side_effect = ConnectionRefusedError(61, "Connection refused")
        with (
            patch("core.codex.os.stat", return_value=metadata),
            patch("core.codex.stat.S_ISSOCK", return_value=True),
            patch("core.codex.os.getuid", return_value=1000),
            patch("core.codex.socket.socket", return_value=client_socket),
        ):
            with self.assertRaises(_CodexIpcUnavailable):
                _CodexIpcConnection.connect()

        client_socket.close.assert_called_once_with()

    def test_requests_current_follower_status_before_starting_a_turn(self):
        connection = _CodexIpcConnection(Mock(), "teamflow-client")
        connection.followers["thread_1"] = {"desktop-client"}
        with (
            patch.object(connection, "_send") as send,
            patch.object(connection, "_collect_followers"),
            patch.object(connection, "_wait_for_response", return_value={
                "resultType": "success",
                "handledByClientId": "desktop-client",
                "result": {"result": {"turn": {"id": "turn_1"}}},
            }),
        ):
            turn_id = connection.start_turn("thread_1", "New work", stop_event=None)

        self.assertEqual(turn_id, "turn_1")
        self.assertEqual(
            send.call_args_list[0].args[0]["method"],
            "thread-stream-following-status-requested",
        )

    def test_interrupts_a_turn_through_the_codex_owner_client(self):
        connection = _CodexIpcConnection(Mock(), "teamflow-client")
        connection.followers["thread_1"] = {"desktop-client"}
        with (
            patch.object(connection, "_send") as send,
            patch.object(connection, "_collect_followers"),
            patch.object(connection, "_wait_for_response", return_value={
                "resultType": "success",
                "handledByClientId": "desktop-client",
                "result": {
                    "result": {
                        "ok": True,
                        "interruptedTurnId": "turn_1",
                    }
                },
            }),
        ):
            result = connection.interrupt_turn("thread_1")

        request = send.call_args_list[1].args[0]
        self.assertEqual(request["method"], "thread-follower-interrupt-turn")
        self.assertEqual(request["version"], 3)
        self.assertEqual(
            request["params"],
            {"conversationId": "thread_1", "mode": "user"},
        )
        self.assertEqual(result["turn_id"], "turn_1")
        self.assertEqual(result["status"], "interrupted")

    def test_stops_the_registered_app_server_turn(self):
        process = object()
        started = threading.Event()
        calls = {"read": 0}
        result_holder = {}

        def call(process_value, request_id, method, params, *, pending=None):
            if method == "thread/resume":
                return {"thread": {"status": {"type": "idle"}}}
            if method == "turn/start":
                return {"turn": {"id": "turn_app"}}
            raise AssertionError(method)

        def read_turn(process_value, stop_event, *, wake_event=None):
            if calls["read"] == 0:
                started.set()
                self.assertTrue(wake_event.wait(timeout=2))
                calls["read"] += 1
                return {}
            if calls["read"] == 1:
                calls["read"] += 1
                return {"id": 4, "result": {}}
            return {
                "method": "turn/completed",
                "params": {
                    "turn": {
                        "id": "turn_app",
                        "status": "interrupted",
                    }
                },
            }

        def run_turn():
            result_holder["turn"] = _run_codex_app_server_turn(
                "thread_app",
                "work",
                on_started=None,
                stop_event=threading.Event(),
            )

        with (
            patch("core.codex._start_app_server", return_value=process),
            patch("core.codex._stop_app_server"),
            patch("core.codex._call", side_effect=call),
            patch("core.codex._read_turn_payload", side_effect=read_turn),
            patch("core.codex._send") as send,
            patch("core.codex._notify_codex_clients_thread_changed"),
        ):
            worker = threading.Thread(target=run_turn)
            worker.start()
            self.assertTrue(started.wait(timeout=2))
            stopped = stop_codex_turn(
                "thread_app",
                expected_turn_id="turn_app",
            )
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(stopped["turn_id"], "turn_app")
        self.assertEqual(stopped["status"], "interrupted")
        self.assertEqual(result_holder["turn"]["status"], "interrupted")
        interrupt = next(
            call.args[1]
            for call in send.call_args_list
            if call.args[1].get("method") == "turn/interrupt"
        )
        self.assertEqual(
            interrupt["params"],
            {"threadId": "thread_app", "turnId": "turn_app"},
        )

    def test_refuses_to_stop_a_different_registered_app_server_turn(self):
        from core.codex import _ACTIVE_APP_SERVER_TURNS, _ACTIVE_APP_SERVER_TURNS_LOCK

        active = {
            "turn_id": "turn_other",
            "interrupt_requested": threading.Event(),
            "completed": threading.Event(),
            "result": None,
            "error": None,
        }
        with _ACTIVE_APP_SERVER_TURNS_LOCK:
            _ACTIVE_APP_SERVER_TURNS["thread_app"] = active
        try:
            with self.assertRaisesRegex(ValueError, "not TeamFlow task turn turn_expected"):
                stop_codex_turn(
                    "thread_app",
                    expected_turn_id="turn_expected",
                )
        finally:
            with _ACTIVE_APP_SERVER_TURNS_LOCK:
                _ACTIVE_APP_SERVER_TURNS.pop("thread_app", None)

        self.assertFalse(active["interrupt_requested"].is_set())

    def test_background_turn_notifies_connected_codex_clients(self):
        connection = Mock()
        connection.client_id = "teamflow-client"
        with patch("core.codex._CodexIpcConnection.connect", return_value=connection):
            _notify_codex_clients_thread_changed("thread_1")

        methods = [call.args[0]["method"] for call in connection._send.call_args_list]
        self.assertEqual(methods, ["thread-read-state-changed", "query-cache-invalidate"])
        connection.close.assert_called_once_with()

    def test_routes_turn_through_the_codex_owner_client(self):
        connection = Mock()
        connection.start_turn.return_value = "turn_1"
        connection.wait_for_turn.return_value = {
            "status": "completed",
            "response": "TEAMFLOW_ACK",
            "error": None,
        }
        with patch("core.codex._CodexIpcConnection.connect", return_value=connection):
            started = Mock()
            result = run_codex_turn("thread_1", "Reply with TEAMFLOW_ACK", on_started=started)

        self.assertTrue(result["ok"])
        self.assertEqual(result["response"], "TEAMFLOW_ACK")
        self.assertEqual(result["transport"], "codex-ipc")
        started.assert_called_once_with("turn_1")
        connection.follow.assert_called_once_with("thread_1")
        connection.start_turn.assert_called_once_with(
            "thread_1",
            "Reply with TEAMFLOW_ACK",
            stop_event=None,
        )
        connection.wait_for_turn.assert_called_once_with(
            "thread_1",
            "turn_1",
            stop_event=None,
        )
        connection.unfollow.assert_called_once_with("thread_1")
        connection.close.assert_called_once_with()

    def test_falls_back_to_app_server_when_no_client_owns_an_unfocused_thread(self):
        expected = {"ok": True, "turn_id": "turn_1"}
        with (
            patch(
                "core.codex._run_codex_ipc_turn",
                side_effect=_CodexIpcUnavailable("no owner"),
            ),
            patch("core.codex._run_codex_app_server_turn", return_value=expected) as fallback,
        ):
            result = run_codex_turn("thread_1", "New work")

        self.assertIs(result, expected)
        fallback.assert_called_once_with(
            "thread_1",
            "New work",
            on_started=None,
            stop_event=None,
        )

    def test_falls_back_when_a_stale_follower_cannot_own_the_session(self):
        expected = {"ok": True, "turn_id": "turn_1"}
        with (
            patch(
                "core.codex._run_codex_ipc_turn",
                side_effect=_CodexIpcUnavailable("no owner"),
            ),
            patch("core.codex._run_codex_app_server_turn", return_value=expected) as fallback,
        ):
            result = run_codex_turn("thread_1", "New work")

        self.assertIs(result, expected)
        fallback.assert_called_once_with(
            "thread_1",
            "New work",
            on_started=None,
            stop_event=None,
        )

    def test_finds_a_persisted_turn_by_id(self):
        expected = {"id": "turn_2", "status": "completed"}
        thread = {"turns": [{"id": "turn_1"}, expected]}

        self.assertIs(codex_turn(thread, "turn_2"), expected)
        self.assertIsNone(codex_turn(thread, "missing"))

    def test_read_resumes_a_not_loaded_thread_before_loading_turns(self):
        process = object()
        expected = {"id": "thread_1", "turns": [{"id": "turn_1"}]}
        with (
            patch("core.codex._start_app_server", return_value=process),
            patch("core.codex._stop_app_server") as stop,
            patch(
                "core.codex._call",
                side_effect=[
                    ValueError("thread not loaded: thread_1"),
                    {"thread": {"id": "thread_1"}},
                    {"thread": expected},
                ],
            ) as request,
        ):
            result = read_codex_thread("thread_1", include_turns=True)

        self.assertEqual(result, expected)
        self.assertEqual(
            [item.args[2] for item in request.call_args_list],
            ["thread/read", "thread/resume", "thread/read"],
        )
        stop.assert_called_once_with(process)

    def test_reads_latest_persisted_thread_settings_without_loading_the_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / "rollout.jsonl"
            rollout.write_text("\n".join([
                json.dumps({
                    "type": "event_msg",
                    "payload": {
                        "thread_settings": {
                            "model": "gpt-5.6-luna",
                            "reasoning_effort": "high",
                            "service_tier": "default",
                        }
                    },
                }),
                json.dumps({"type": "response_item", "payload": {"type": "message"}}),
                json.dumps({
                    "type": "event_msg",
                    "payload": {
                        "thread_settings": {
                            "model": "gpt-5.6-sol",
                            "reasoning_effort": "ultra",
                            "service_tier": "priority",
                        }
                    },
                }),
                "",
            ]))

            settings = codex_thread_settings({"path": str(rollout)})

        self.assertEqual(settings, {
            "model": "gpt-5.6-sol",
            "effort": "ultra",
            "service_tier": "priority",
        })

    def test_verifies_developer_context_in_the_persisted_codex_rollout(self):
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            rollout = codex_home / "sessions" / "rollout-thread_context.jsonl"
            rollout.parent.mkdir()
            rollout.write_text(json.dumps({
                "timestamp": "2026-07-26T06:47:50.132Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{
                        "type": "input_text",
                        "text": "TeamFlow onboarding context",
                    }],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "turn_context",
                    },
                },
            }))
            database = codex_home / "state_5.sqlite"
            with sqlite3.connect(database) as conn:
                conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT)")
                conn.execute(
                    "INSERT INTO threads (id, rollout_path) VALUES (?, ?)",
                    ("thread_context", str(rollout)),
                )

            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                evidence = codex_developer_context_evidence(
                    "thread_context",
                    {
                        "onboarding": "TeamFlow onboarding context",
                        "recovery": "TeamFlow recovery context",
                    },
                    injected_at="2026-07-26T06:47:50.116557+00:00",
                )

        self.assertEqual(evidence["status"], "verified")
        self.assertEqual(evidence["context_kind"], "onboarding")
        self.assertEqual(evidence["turn_id"], "turn_context")
        self.assertEqual(evidence["timestamp"], "2026-07-26T06:47:50.132Z")
        self.assertEqual(evidence["rollout_path"], str(rollout))

    def test_extracts_completion_and_response_from_ipc_stream_patches(self):
        stream = _CodexThreadStream()
        key = "tail:0:local:test"
        stream.apply({
            "type": "patches",
            "patches": [{
                "op": "add",
                "path": ["turnHistory", "history", "entitiesByKey", key],
                "value": {"turnId": None, "status": "inProgress", "items": []},
            }],
        })
        stream.apply({
            "type": "patches",
            "patches": [
                {
                    "op": "replace",
                    "path": ["turnHistory", "history", "entitiesByKey", key, "turnId"],
                    "value": "turn_1",
                },
                {
                    "op": "add",
                    "path": ["turnHistory", "history", "entitiesByKey", key, "items", 0],
                    "value": {"type": "agentMessage", "text": "TEAMFLOW_ACK"},
                },
                {
                    "op": "replace",
                    "path": ["turnHistory", "history", "entitiesByKey", key, "status"],
                    "value": "completed",
                },
            ],
        })

        self.assertEqual(
            stream.result("turn_1"),
            {"status": "completed", "response": "TEAMFLOW_ACK", "error": None},
        )

    def test_classifies_only_terminal_thread_lookup_errors_as_permanent(self):
        self.assertTrue(codex_thread_is_permanently_unavailable(
            ValueError("no rollout found for thread id thread_1")
        ))
        self.assertFalse(codex_thread_is_permanently_unavailable(ValueError("thread is archived")))
        self.assertFalse(codex_thread_is_permanently_unavailable(ValueError("Codex app-server timed out")))

    def test_finds_only_unresolved_teamflow_daemon_failures_in_a_turn(self):
        failed = {
            "type": "mcpToolCall",
            "server": "teamflow",
            "tool": "get_task",
            "status": "failed",
            "arguments": {"record_id": "rec_1"},
            "result": {
                "content": [{
                    "type": "text",
                    "text": "Error executing tool get_task: [Errno 2] No such file or directory",
                }]
            },
        }
        business_failure = {
            "type": "mcpToolCall",
            "server": "teamflow",
            "tool": "claim_task",
            "status": "failed",
            "arguments": {"record_id": "rec_2"},
            "result": {
                "content": [{
                    "type": "text",
                    "text": "task TF-0002 is in_progress, not ready",
                }]
            },
        }
        turn = {"items": [failed, business_failure]}

        self.assertEqual(
            codex_turn_unresolved_teamflow_mcp_failures(turn),
            [{
                "tool": "get_task",
                "arguments": {"record_id": "rec_1"},
                "error": "Error executing tool get_task: [Errno 2] No such file or directory",
            }],
        )

        turn["items"].append({
            **failed,
            "status": "completed",
            "result": {"structuredContent": {"ok": True}},
        })
        self.assertEqual(codex_turn_unresolved_teamflow_mcp_failures(turn), [])


if __name__ == "__main__":
    unittest.main()
