from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.codex import run_codex_delivery_turn
from core.codex_ipc import CodexTurnAcceptanceUnknown
from core.codex_queue import (
    CodexQueueUnsupported,
    codex_queued_message_exists,
    delete_codex_queued_message,
    enqueue_codex_turn,
    run_codex_queued_turn,
)
from core.codex_rollout import codex_turn_id_by_client_message_id


class CodexQueueTest(unittest.TestCase):
    def test_enqueues_with_the_stable_client_message_id(self) -> None:
        calls = []

        def request(method, params):
            calls.append((method, params))
            return {"queuedSubmission": {"id": "queue_1"}}

        result = enqueue_codex_turn(
            request,
            "thread_1",
            "New work",
            "message_1",
        )

        self.assertEqual(result, {"id": "queue_1"})
        self.assertEqual(calls[0][0], "thread/queue/add")
        self.assertEqual(calls[0][1]["threadId"], "thread_1")
        self.assertEqual(calls[0][1]["clientUserMessageId"], "message_1")

    def test_reports_queue_protocol_unsupported_for_ipc_fallback(self) -> None:
        def request(_method, _params):
            raise ValueError("method not found: thread/queue/add")

        with self.assertRaises(CodexQueueUnsupported):
            enqueue_codex_turn(request, "thread_1", "New work", "message_1")

    def test_returns_after_persisting_queue_acceptance(self) -> None:
        events = []

        result = run_codex_queued_turn(
            "thread_1",
            "New work",
            client_message_id="message_1",
            enqueue=lambda *_args: {"id": "queue_1"},
            on_queued=lambda queue_id: events.append(("queued", queue_id)),
            stop_event=threading.Event(),
        )

        self.assertEqual(events, [("queued", "queue_1")])
        self.assertEqual(result["transport"], "codex-queue")
        self.assertEqual(result["status"], "queued")
        self.assertIsNone(result["turn_id"])
        self.assertTrue(result["ok"])

    def test_preserves_unknown_acceptance_after_queue_callback_failure(self) -> None:
        def fail_callback(_queue_id):
            raise ValueError("database changed")

        with self.assertRaises(CodexTurnAcceptanceUnknown):
            run_codex_queued_turn(
                "thread_1",
                "New work",
                client_message_id="message_1",
                enqueue=lambda *_args: {"id": "queue_1"},
                on_queued=fail_callback,
                stop_event=threading.Event(),
            )

    def test_daemon_stop_prevents_queue_side_effect(self) -> None:
        stopping = threading.Event()
        stopping.set()
        enqueue = Mock(return_value={"id": "queue_1"})

        with self.assertRaises(CodexTurnAcceptanceUnknown):
            run_codex_queued_turn(
                "thread_1",
                "New work",
                client_message_id="message_1",
                enqueue=enqueue,
                on_queued=lambda _queue_id: None,
                stop_event=stopping,
            )
        enqueue.assert_not_called()

    def test_deletes_a_queued_message_by_client_message_id(self) -> None:
        calls = []

        def request(method, params):
            calls.append((method, params))
            if method == "thread/queue/list":
                return {
                    "data": [{
                        "id": "queue_1",
                        "clientUserMessageId": "message_1",
                    }]
                }
            return {"deleted": True}

        self.assertTrue(delete_codex_queued_message(
            request,
            "thread_1",
            "message_1",
        ))
        self.assertEqual(
            calls,
            [
                ("thread/queue/list", {"threadId": "thread_1", "limit": 100}),
                (
                    "thread/queue/delete",
                    {
                        "threadId": "thread_1",
                        "queuedSubmissionId": "queue_1",
                    },
                ),
            ],
        )

    def test_detects_a_queued_message_by_client_message_id(self) -> None:
        def request(method, params):
            self.assertEqual(method, "thread/queue/list")
            self.assertEqual(params, {"threadId": "thread_1", "limit": 100})
            return {
                "data": [
                    {"id": "queue_1", "clientUserMessageId": "other"},
                    {"id": "queue_2", "clientUserMessageId": "message_1"},
                ]
            }

        self.assertTrue(codex_queued_message_exists(
            request,
            "thread_1",
            "message_1",
        ))
        self.assertFalse(codex_queued_message_exists(
            request,
            "thread_1",
            "missing",
        ))

    def test_delivery_prefers_queue_and_only_falls_back_when_unsupported(self) -> None:
        queued_result = {"ok": True, "transport": "codex-queue"}
        with (
            patch("core.codex.run_codex_queued_turn", return_value=queued_result),
            patch("core.codex._run_codex_ipc_turn") as ipc,
        ):
            self.assertIs(
                run_codex_delivery_turn("thread_1", "New work"),
                queued_result,
            )
        ipc.assert_not_called()

        ipc_result = {"ok": True, "transport": "codex-ipc"}
        with (
            patch(
                "core.codex.run_codex_queued_turn",
                side_effect=CodexQueueUnsupported("unsupported"),
            ),
            patch("core.codex._run_codex_ipc_turn", return_value=ipc_result) as ipc,
        ):
            self.assertIs(
                run_codex_delivery_turn("thread_1", "New work"),
                ipc_result,
            )
        ipc.assert_called_once()

    def test_maps_the_queued_client_message_to_its_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            rollout = Path(temp) / "rollout.jsonl"
            records = [
                {
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn_1"},
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "client_id": "message_1",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn_1"},
                },
            ]
            rollout.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            with patch(
                "core.codex_rollout._codex_rollout_path",
                return_value=rollout,
            ):
                self.assertEqual(
                    codex_turn_id_by_client_message_id("thread_1", "message_1"),
                    "turn_1",
                )
                self.assertIsNone(
                    codex_turn_id_by_client_message_id("thread_1", "missing")
                )


if __name__ == "__main__":
    unittest.main()
