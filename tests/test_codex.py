from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.codex import (
    _CodexIpcConnection,
    _CodexIpcNoOwner,
    _CodexThreadStream,
    _call,
    _interrupt_competing_codex_turn,
    _notify_codex_clients_thread_changed,
    _start_app_server,
    codex_developer_context_evidence,
    codex_thread_is_permanently_unavailable,
    codex_thread_settings,
    codex_turn,
    codex_turn_by_client_message_id,
    codex_turn_unresolved_teamflow_mcp_failures,
    read_codex_thread,
    run_codex_turn,
    stop_codex_turn,
    _run_codex_app_server_turn,
    _stop_app_server,
    codex_delivery_error_is_terminal,
)
from core.codex_ipc import (
    CodexIpcClientUnavailable,
    CodexIpcEmptyTurn,
    CodexIpcOwnerUnconfirmed,
    CodexIpcSessionNotLoaded,
    CodexIpcUnavailable,
    CodexTurnAcceptanceUnknown,
    codex_ipc_session_has_owner,
)
from core.codex_permissions import CodexBackgroundMcpPermissionRequired


class CodexTurnTest(unittest.TestCase):
    def test_start_app_server_uses_codex_facade_dependencies(self):
        process = Mock()
        with (
            patch(
                "core.codex_app_server_protocol.subprocess.Popen",
                return_value=process,
            ) as popen,
            patch(
                "core.codex._codex_executable",
                return_value="/tmp/teamflow-codex",
            ),
            patch("core.codex._send") as send,
            patch("core.codex._response_for", return_value={}) as response,
        ):
            result = _start_app_server()

        self.assertIs(result, process)
        self.assertEqual(
            popen.call_args.args[0][0],
            "/tmp/teamflow-codex",
        )
        self.assertEqual(send.call_count, 2)
        response.assert_called_once_with(process, 1)

    def test_call_uses_codex_facade_dependencies(self):
        process = Mock()
        with (
            patch("core.codex._send") as send,
            patch(
                "core.codex._response_for",
                return_value={"result": {"ok": True}},
            ) as response,
        ):
            result = _call(
                process,
                2,
                "thread/read",
                {"threadId": "thread_1"},
            )

        self.assertEqual(result, {"ok": True})
        send.assert_called_once_with(
            process,
            {
                "id": 2,
                "method": "thread/read",
                "params": {"threadId": "thread_1"},
            },
        )
        response.assert_called_once_with(process, 2, pending=None)

    def test_stopping_app_server_closes_all_process_pipes(self):
        process = Mock()
        process.poll.return_value = None

        _stop_app_server(process)

        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=2)
        process.stdin.close.assert_called_once_with()
        process.stdout.close.assert_called_once_with()
        process.stderr.close.assert_called_once_with()

    def test_connection_refusal_is_reported_as_ipc_unavailable(self):
        metadata = Mock(st_mode=0, st_uid=1000)
        client_socket = Mock()
        client_socket.connect.side_effect = ConnectionRefusedError(61, "Connection refused")
        with (
            patch("core.codex.os.stat", return_value=metadata),
            patch("core.codex.stat.S_ISSOCK", return_value=True),
            patch("core.codex.os.getuid", return_value=1000),
            patch("core.codex.socket.socket", return_value=client_socket),
        ):
            with self.assertRaises(CodexIpcClientUnavailable):
                _CodexIpcConnection.connect()

        client_socket.close.assert_called_once_with()

    def test_discovers_the_owner_and_starts_v2(self):
        connection = _CodexIpcConnection(Mock(), "teamflow-client")
        connection.streams["thread_1"] = _CodexThreadStream()
        connection.streams["thread_1"].initialized = True
        connection.owner_client_id = "desktop-client"
        with (
            patch.object(connection, "_send") as send,
            patch.object(
                connection,
                "_wait_for_response",
                return_value={
                    "resultType": "success",
                    "handledByClientId": "desktop-client",
                    "result": {"result": {"turn": {"id": "turn_1"}}},
                },
            ),
        ):
            turn_id = connection.start_turn(
                "thread_1",
                "New work",
                client_message_id="message_1",
                stop_event=None,
            )

        self.assertEqual(turn_id, "turn_1")
        self.assertFalse(connection.followers)
        self.assertEqual(connection.owner_client_id, "desktop-client")
        self.assertEqual(
            [call.args[0]["method"] for call in send.call_args_list],
            [
                "thread-stream-following-changed",
                "thread-follower-start-turn",
            ],
        )
        start_request = send.call_args_list[1].args[0]
        self.assertEqual(start_request["targetClientId"], "desktop-client")
        self.assertEqual(start_request["version"], 2)
        self.assertEqual(
            start_request["params"],
            {
                "conversationId": "thread_1",
                "turnStart": {
                    "request": {
                        "threadId": "thread_1",
                        "input": [
                            {
                                "type": "text",
                                "text": "New work",
                                "text_elements": [],
                            }
                        ],
                        "clientUserMessageId": "message_1",
                    },
                    "context": {"inheritThreadSettings": True},
                },
            },
        )

    def test_reports_an_explicitly_unowned_session(self):
        connection = _CodexIpcConnection(Mock(), "teamflow-client")
        with (
            patch.object(connection, "_send") as send,
            patch.object(connection, "_wait_for_response", return_value={
                "resultType": "error",
                "error": "no-client-found",
            }),
            self.assertRaisesRegex(
                CodexIpcSessionNotLoaded,
                "Codex is running.*has loaded",
            ),
        ):
            connection.discover_owner("thread_1", stop_event=None)

        self.assertEqual(send.call_args.args[0]["method"], "thread-owner-discovery")

    def test_liveness_probe_waits_beyond_the_old_250ms_cutoff(self):
        connection = Mock()

        def discover_owner(_thread_id, *, stop_event, timeout):
            self.assertIsNone(stop_event)
            self.assertIsNone(timeout)
            time.sleep(0.3)
            return "desktop-client"

        connection.discover_owner.side_effect = discover_owner
        connection_type = Mock()
        connection_type.connect.return_value = connection

        self.assertTrue(codex_ipc_session_has_owner(
            "thread_1",
            connection_type=connection_type,
        ))
        connection.unfollow.assert_called_once_with("thread_1")
        connection.close.assert_called_once_with()

    def test_discovers_owner_without_waiting_for_a_stream_snapshot(self):
        connection = _CodexIpcConnection(Mock(), "teamflow-client")
        with (
            patch.object(connection, "_send") as send,
            patch.object(connection, "_wait_for_response", return_value={
                "resultType": "success",
                "handledByClientId": "desktop-client",
                "result": {},
            }),
        ):
            owner = connection.discover_owner("thread_1", stop_event=None)

        self.assertEqual(owner, "desktop-client")
        self.assertEqual(connection.owner_client_id, "desktop-client")
        request = send.call_args.args[0]
        self.assertEqual(request["method"], "thread-owner-discovery")
        self.assertEqual(request["version"], 1)
        self.assertEqual(request["params"], {
            "conversationId": "thread_1",
            "hostId": "local",
        })

    def test_owner_discovery_timeout_becomes_a_waiting_condition(self):
        connection = _CodexIpcConnection(Mock(), "teamflow-client")
        with (
            patch.object(connection, "_send"),
            patch.object(
                connection,
                "_wait_for_response",
                side_effect=CodexIpcUnavailable("request timed out"),
            ),
            self.assertRaisesRegex(
                CodexIpcOwnerUnconfirmed,
                "owner could not be confirmed",
            ),
        ):
            connection.discover_owner("thread_1", stop_event=None)

    def test_liveness_probe_treats_an_unconfirmed_owner_as_unavailable(self):
        connection = Mock()
        connection.discover_owner.side_effect = CodexIpcOwnerUnconfirmed(
            "owner could not be confirmed"
        )
        connection_type = Mock()
        connection_type.connect.return_value = connection

        self.assertFalse(codex_ipc_session_has_owner(
            "thread_1",
            connection_type=connection_type,
        ))
        connection.unfollow.assert_called_once_with("thread_1")
        connection.close.assert_called_once_with()

    def test_records_the_owner_from_its_stream_snapshot(self):
        connection = _CodexIpcConnection(Mock(), "teamflow-client")
        message = {
            "type": "broadcast",
            "method": "thread-stream-state-changed",
            "sourceClientId": "desktop-client",
            "version": 11,
            "params": {
                "conversationId": "thread_1",
                "change": {
                    "type": "snapshot",
                    "revision": 1,
                    "conversationState": {"requests": []},
                },
            },
        }
        with (
            patch("core.codex_ipc.select.select", return_value=([connection.connection], [], [])),
            patch.object(connection, "_receive", return_value=message),
        ):
            connection._receive_once(0)

        self.assertEqual(connection.owner_client_id, "desktop-client")
        self.assertTrue(connection.streams["thread_1"].initialized)

    def test_propagates_owner_rejection_when_the_session_is_active(self):
        connection = _CodexIpcConnection(Mock(), "teamflow-client")
        connection.streams["thread_1"] = _CodexThreadStream()
        connection.streams["thread_1"].initialized = True
        connection.owner_client_id = "desktop-client"
        connection.streams["thread_1"].entries["active"] = {
            "turnId": "turn_active",
            "status": "inProgress",
            "items": {},
        }
        with (
            patch.object(connection, "_send") as send,
            patch.object(
                connection,
                "_wait_for_response",
                return_value={
                    "resultType": "error",
                    "error": "Codex agent is busy",
                },
            ),
            self.assertRaisesRegex(ValueError, "Codex agent is busy"),
        ):
            connection.start_turn(
                "thread_1",
                "New work",
                client_message_id="message_1",
                stop_event=None,
            )

        self.assertEqual(send.call_count, 2)
        self.assertEqual(
            send.call_args_list[1].args[0]["method"],
            "thread-follower-start-turn",
        )

    def test_treats_an_in_progress_placeholder_as_a_busy_session(self):
        stream = _CodexThreadStream()
        stream.entries["placeholder"] = {
            "turnId": None,
            "status": "inProgress",
            "items": {},
        }

        self.assertTrue(stream.has_active_turn())

    def test_starts_without_loading_the_owner_history(self):
        connection = _CodexIpcConnection(Mock(), "teamflow-client")
        connection.streams["thread_1"] = _CodexThreadStream()

        with (
            patch.object(
                connection,
                "discover_owner",
                return_value="desktop-client",
            ),
            patch.object(connection, "_send") as send,
            patch.object(
                connection,
                "_wait_for_response",
                return_value={
                    "resultType": "success",
                    "handledByClientId": "desktop-client",
                    "result": {"result": {"turn": {"id": "turn_1"}}},
                },
            ),
        ):
            turn_id = connection.start_turn(
                "thread_1",
                "New work",
                client_message_id="message_1",
                stop_event=None,
            )

        self.assertEqual(turn_id, "turn_1")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(
            send.call_args.args[0]["method"],
            "thread-follower-start-turn",
        )

    def test_treats_a_transport_failure_after_start_as_unknown_acceptance(self):
        connection = _CodexIpcConnection(Mock(), "teamflow-client")
        connection.streams["thread_1"] = _CodexThreadStream()
        connection.streams["thread_1"].initialized = True

        with (
            patch.object(
                connection,
                "discover_owner",
                return_value="desktop-client",
            ),
            patch.object(
                connection,
                "_send",
                side_effect=CodexIpcUnavailable("connection closed"),
            ),
            self.assertRaisesRegex(
                CodexTurnAcceptanceUnknown,
                "could not be confirmed",
            ),
        ):
            connection.start_turn(
                "thread_1",
                "New work",
                client_message_id="message_1",
                stop_event=None,
            )

    def test_treats_a_success_without_turn_id_as_unknown_acceptance(self):
        connection = _CodexIpcConnection(Mock(), "teamflow-client")
        connection.streams["thread_1"] = _CodexThreadStream()
        connection.streams["thread_1"].initialized = True

        with (
            patch.object(
                connection,
                "discover_owner",
                return_value="desktop-client",
            ),
            patch.object(connection, "_send"),
            patch.object(
                connection,
                "_wait_for_response",
                return_value={
                    "resultType": "success",
                    "handledByClientId": "desktop-client",
                    "result": {},
                },
            ),
            self.assertRaisesRegex(
                CodexTurnAcceptanceUnknown,
                "without returning a turn ID",
            ),
        ):
            connection.start_turn(
                "thread_1",
                "New work",
                client_message_id="message_1",
                stop_event=None,
            )

    def test_interrupts_a_turn_through_the_codex_owner_client(self):
        connection = _CodexIpcConnection(Mock(), "teamflow-client")
        connection.streams["thread_1"] = _CodexThreadStream()
        connection.streams["thread_1"].initialized = True
        connection.following_threads.add("thread_1")
        connection.owner_client_id = "desktop-client"
        with (
            patch.object(connection, "_send") as send,
            patch.object(
                connection,
                "_wait_for_response",
                return_value={
                    "resultType": "success",
                    "handledByClientId": "desktop-client",
                    "result": {
                        "result": {
                            "ok": True,
                            "interruptedTurnId": "turn_1",
                        }
                    },
                },
            ),
        ):
            result = connection.interrupt_turn(
                "thread_1",
                expected_turn_id="turn_1",
            )

        request = send.call_args_list[0].args[0]
        self.assertEqual(request["method"], "thread-follower-interrupt-turn")
        self.assertEqual(request["targetClientId"], "desktop-client")
        self.assertEqual(request["version"], 4)
        self.assertEqual(
            request["params"],
            {
                "conversationId": "thread_1",
                "mode": "user",
                "expectedTurnId": "turn_1",
            },
        )
        self.assertEqual(result["turn_id"], "turn_1")
        self.assertEqual(result["status"], "interrupted")

    def test_stops_the_registered_app_server_turn(self):
        process = object()
        started = threading.Event()
        calls = {"read": 0}
        result_holder = {}
        turn_start_params = {}

        def call(process_value, request_id, method, params, *, pending=None):
            if method == "thread/resume":
                return {"thread": {"status": {"type": "idle"}}}
            if method == "turn/start":
                turn_start_params.update(params)
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
                client_message_id="message_app",
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
        self.assertEqual(turn_start_params["approvalPolicy"], "never")
        self.assertEqual(turn_start_params["permissions"], ":workspace")
        self.assertNotIn("sandboxPolicy", turn_start_params)
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

    def test_stop_without_a_registered_turn_uses_interrupt_facade(self):
        expected = {
            "ok": True,
            "thread_id": "thread_unregistered",
            "turn_id": "turn_unregistered",
            "status": "interrupted",
        }
        with (
            patch(
                "core.codex._stop_codex_ipc_turn",
                side_effect=_CodexIpcNoOwner("no owner"),
            ),
            patch(
                "core.codex._interrupt_codex_app_server_turn",
                return_value=expected,
            ) as interrupt,
        ):
            result = stop_codex_turn(
                "thread_unregistered",
                expected_turn_id="turn_unregistered",
            )

        self.assertIs(result, expected)
        interrupt.assert_called_once_with(
            "thread_unregistered",
            expected_turn_id="turn_unregistered",
        )

    def test_stops_an_owner_managed_turn_through_ipc(self):
        expected = {
            "ok": True,
            "thread_id": "thread_owner",
            "turn_id": "turn_owner",
            "status": "interrupted",
            "transport": "codex-ipc",
        }
        with (
            patch(
                "core.codex._stop_codex_ipc_turn",
                return_value=expected,
            ) as stop_ipc,
            patch("core.codex._interrupt_codex_app_server_turn") as app_server,
        ):
            result = stop_codex_turn(
                "thread_owner",
                expected_turn_id="turn_owner",
            )

        self.assertIs(result, expected)
        stop_ipc.assert_called_once_with(
            "thread_owner",
            expected_turn_id="turn_owner",
        )
        app_server.assert_not_called()

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
        with patch(
            "core.codex._CodexIpcConnection.connect",
            return_value=connection,
        ):
            started = Mock()
            result = run_codex_turn(
                "thread_1",
                "Reply with TEAMFLOW_ACK",
                client_message_id="message_1",
                on_started=started,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["response"], "TEAMFLOW_ACK")
        self.assertEqual(result["transport"], "codex-ipc")
        started.assert_called_once_with("turn_1")
        connection.follow.assert_called_once_with("thread_1")
        connection.start_turn.assert_called_once_with(
            "thread_1",
            "Reply with TEAMFLOW_ACK",
            client_message_id="message_1",
            stop_event=None,
        )
        connection.wait_for_turn_started.assert_called_once_with(
            "thread_1",
            "turn_1",
            client_message_id="message_1",
            stop_event=None,
        )
        connection.wait_for_turn.assert_called_once_with(
            "thread_1",
            "turn_1",
            interrupt_competing_turn=_interrupt_competing_codex_turn,
            stop_event=None,
        )
        connection.unfollow.assert_called_once_with("thread_1")
        connection.close.assert_called_once_with()

    def test_persists_an_owner_accepted_ipc_turn_before_it_materializes(self):
        connection = Mock()
        connection.start_turn.return_value = "turn_ghost"
        connection.wait_for_turn_started.side_effect = ValueError(
            "Codex turn turn_ghost did not materialize in session thread_1"
        )
        started = Mock()
        with (
            patch("core.codex._CodexIpcConnection.connect", return_value=connection),
            self.assertRaisesRegex(ValueError, "did not materialize"),
        ):
            run_codex_turn(
                "thread_1",
                "New work",
                client_message_id="message_ghost",
                on_started=started,
            )

        started.assert_called_once_with("turn_ghost")
        connection.wait_for_turn.assert_not_called()
        connection.unfollow.assert_called_once_with("thread_1")
        connection.close.assert_called_once_with()

    def test_reconciles_without_fallback_after_owner_completes_an_empty_turn(self):
        connection = Mock()
        connection.start_turn.return_value = "turn_empty"
        connection.wait_for_turn_started.side_effect = CodexIpcEmptyTurn(
            "Codex owner completed a turn without the requested message"
        )
        started = Mock()
        with (
            patch(
                "core.codex._CodexIpcConnection.connect",
                return_value=connection,
            ),
            patch(
                "core.codex._run_codex_app_server_turn",
            ) as fallback,
            self.assertRaisesRegex(
                CodexTurnAcceptanceUnknown,
                "could not be confirmed",
            ),
        ):
            run_codex_turn(
                "thread_1",
                "New work",
                client_message_id="message_empty",
                on_started=started,
            )

        started.assert_called_once_with("turn_empty")
        fallback.assert_not_called()
        connection.wait_for_turn.assert_not_called()
        connection.unfollow.assert_called_once_with("thread_1")
        connection.close.assert_called_once_with()

    def test_does_not_fall_back_after_an_accepted_ipc_turn_loses_connection(self):
        connection = Mock()
        connection.start_turn.return_value = "turn_unknown"
        connection.wait_for_turn_started.side_effect = CodexIpcUnavailable(
            "Codex client IPC connection closed"
        )
        started = Mock()
        with (
            patch(
                "core.codex._CodexIpcConnection.connect",
                return_value=connection,
            ),
            patch("core.codex._run_codex_app_server_turn") as fallback,
            self.assertRaisesRegex(
                CodexTurnAcceptanceUnknown,
                "could not be confirmed",
            ),
        ):
            run_codex_turn(
                "thread_1",
                "New work",
                client_message_id="message_unknown",
                on_started=started,
            )

        started.assert_called_once_with("turn_unknown")
        fallback.assert_not_called()
        connection.wait_for_turn.assert_not_called()
        connection.unfollow.assert_called_once_with("thread_1")
        connection.close.assert_called_once_with()

    def test_does_not_fall_back_when_no_client_owns_an_unfocused_thread(self):
        with (
            patch(
                "core.codex._run_codex_ipc_turn",
                side_effect=_CodexIpcNoOwner("no owner"),
            ),
            patch("core.codex._run_codex_app_server_turn") as fallback,
            self.assertRaisesRegex(_CodexIpcNoOwner, "no owner"),
        ):
            run_codex_turn(
                "thread_1",
                "New work",
                client_message_id="message_1",
            )

        fallback.assert_not_called()

    def test_does_not_fall_back_when_owner_discovery_is_uncertain(self):
        with (
            patch(
                "core.codex._run_codex_ipc_turn",
                side_effect=CodexIpcUnavailable("owner discovery timed out"),
            ),
            patch("core.codex._run_codex_app_server_turn") as fallback,
            self.assertRaisesRegex(CodexIpcUnavailable, "timed out"),
        ):
            run_codex_turn(
                "thread_1",
                "New work",
                client_message_id="message_1",
            )

        fallback.assert_not_called()

    def test_background_mcp_authorization_is_required_before_ipc_dispatch(self):
        with (
            patch(
                "core.codex._run_codex_ipc_turn",
            ) as ipc,
            patch(
                "core.codex.require_teamflow_mcp_authorization",
                side_effect=CodexBackgroundMcpPermissionRequired(
                    ["update_task"],
                ),
            ) as authorize,
            patch("core.codex._run_codex_app_server_turn") as fallback,
            self.assertRaisesRegex(
                CodexBackgroundMcpPermissionRequired,
                "update_task",
            ),
        ):
            run_codex_turn(
                "thread_1",
                "New work",
                client_message_id="message_1",
                required_mcp_tools=["update_task"],
            )

        authorize.assert_called_once_with(("update_task",))
        ipc.assert_not_called()
        fallback.assert_not_called()

    def test_global_authorization_uses_live_ipc_when_session_is_loaded(self):
        expected = {
            "ok": True,
            "turn_id": "turn_1",
            "transport": "codex-ipc",
        }
        with (
            patch(
                "core.codex.require_teamflow_mcp_authorization",
                return_value={"authorized": True},
            ) as authorize,
            patch(
                "core.codex._run_codex_ipc_turn",
                return_value=expected,
            ) as ipc,
            patch("core.codex._run_codex_app_server_turn") as app_server,
        ):
            result = run_codex_turn(
                "thread_1",
                "New work",
                client_message_id="message_1",
                required_mcp_tools=["update_task"],
            )

        self.assertIs(result, expected)
        authorize.assert_called_once_with(("update_task",))
        ipc.assert_called_once()
        app_server.assert_not_called()

    def test_does_not_fall_back_when_ipc_acceptance_is_unknown(self):
        with (
            patch(
                "core.codex._run_codex_ipc_turn",
                side_effect=CodexTurnAcceptanceUnknown(
                    "Codex turn acceptance could not be confirmed"
                ),
            ),
            patch("core.codex._run_codex_app_server_turn") as fallback,
            self.assertRaisesRegex(
                CodexTurnAcceptanceUnknown,
                "could not be confirmed",
            ),
        ):
            run_codex_turn(
                "thread_1",
                "New work",
                client_message_id="message_1",
            )

        fallback.assert_not_called()

    def test_uses_the_owner_snapshot_instead_of_an_app_server_preflight(self):
        with (
            patch("core.codex.read_codex_thread") as read_thread,
            patch(
                "core.codex._run_codex_ipc_turn",
                side_effect=ValueError("Codex agent is busy"),
            ) as ipc,
            patch("core.codex._run_codex_app_server_turn") as app_server,
            self.assertRaisesRegex(ValueError, "Codex agent is busy"),
        ):
            run_codex_turn(
                "thread_1",
                "New work",
                client_message_id="message_1",
            )

        read_thread.assert_not_called()
        ipc.assert_called_once()
        app_server.assert_not_called()

    def test_finds_a_persisted_turn_by_id(self):
        expected = {"id": "turn_2", "status": "completed"}
        thread = {"turns": [{"id": "turn_1"}, expected]}

        self.assertIs(codex_turn(thread, "turn_2"), expected)
        self.assertIsNone(codex_turn(thread, "missing"))

    def test_finds_a_persisted_turn_by_client_message_id(self):
        expected = {
            "id": "turn_2",
            "status": "completed",
            "items": [
                {
                    "type": "userMessage",
                    "clientId": "message_2",
                    "content": [{"type": "text", "text": "work"}],
                }
            ],
        }
        thread = {
            "turns": [
                {
                    "id": "turn_1",
                    "items": [
                        {
                            "type": "userMessage",
                            "clientId": "message_1",
                        }
                    ],
                },
                expected,
            ]
        }

        self.assertIs(
            codex_turn_by_client_message_id(thread, "message_2"),
            expected,
        )
        self.assertIsNone(
            codex_turn_by_client_message_id(thread, "missing"),
        )

    def test_interrupts_only_the_teamflow_turn_after_a_concurrent_turn_appears(self):
        connection = _CodexIpcConnection(Mock(), "teamflow-client")
        stream = _CodexThreadStream()
        stream.initialized = True
        stream.entries["teamflow"] = {
            "turnId": "turn_teamflow",
            "status": "inProgress",
            "items": {},
        }
        stream.entries["user"] = {
            "turnId": "turn_user",
            "status": "inProgress",
            "items": {},
        }
        connection.streams["thread_1"] = stream
        interrupt = Mock()

        with self.assertRaisesRegex(
            ValueError,
            "started another turn concurrently",
        ):
            connection.ensure_exclusive_turn(
                "thread_1",
                "turn_teamflow",
                interrupt_competing_turn=interrupt,
            )

        interrupt.assert_called_once_with("thread_1", "turn_teamflow")

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

    def test_matches_the_exact_client_message_in_an_ipc_turn(self):
        stream = _CodexThreadStream()
        stream.entries["turn"] = {
            "turnId": "turn_1",
            "status": "inProgress",
            "items": {
                0: {
                    "type": "userMessage",
                    "clientId": "message_1",
                    "content": [{"type": "text", "text": "New work"}],
                }
            },
        }

        self.assertTrue(stream.contains_client_message("turn_1", "message_1"))
        self.assertFalse(stream.contains_client_message("turn_1", "other"))
        self.assertFalse(stream.is_terminal("turn_1"))
        stream.entries["turn"]["status"] = "completed"
        self.assertTrue(stream.is_terminal("turn_1"))

    def test_classifies_only_terminal_thread_lookup_errors_as_permanent(self):
        self.assertTrue(codex_thread_is_permanently_unavailable(
            ValueError("no rollout found for thread id thread_1")
        ))
        self.assertFalse(codex_thread_is_permanently_unavailable(ValueError("thread is archived")))
        self.assertFalse(codex_thread_is_permanently_unavailable(ValueError("Codex app-server timed out")))
        self.assertFalse(codex_delivery_error_is_terminal(
            ValueError("Codex turn turn_1 did not materialize in session thread_1")
        ))

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
