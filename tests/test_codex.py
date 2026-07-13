from __future__ import annotations

import unittest
from unittest.mock import call, patch

from core.codex import run_codex_turn


class CodexTurnTest(unittest.TestCase):
    def test_resumes_thread_waits_for_completion_and_declines_approval(self):
        process = object()
        completed = [
            {
                "id": 42,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "thread_1", "turnId": "turn_1"},
            },
            {
                "method": "item/completed",
                "params": {"item": {"type": "agentMessage", "phase": "final_answer", "text": "TEAMFLOW_ACK"}},
            },
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn_1", "status": "completed", "error": None}},
            },
        ]
        with (
            patch("core.codex._start_app_server", return_value=process),
            patch("core.codex._stop_app_server") as stop,
            patch("core.codex._call", side_effect=[{"thread": {"id": "thread_1"}}, {"turn": {"id": "turn_1"}}]) as request,
            patch("core.codex._read_payload", side_effect=completed),
            patch("core.codex._send") as send,
        ):
            result = run_codex_turn("thread_1", "Reply with TEAMFLOW_ACK")

        self.assertTrue(result["ok"])
        self.assertEqual(result["response"], "TEAMFLOW_ACK")
        self.assertEqual(result["declined_requests"], ["item/commandExecution/requestApproval"])
        self.assertEqual(request.call_args_list[0].args[2:], ("thread/resume", {"threadId": "thread_1"}))
        self.assertEqual(
            request.call_args_list[1].args[2:],
            ("turn/start", {"threadId": "thread_1", "input": [{"type": "text", "text": "Reply with TEAMFLOW_ACK"}]}),
        )
        send.assert_called_once_with(process, {"id": 42, "result": {"decision": "decline"}})
        stop.assert_has_calls([call(process)])

    def test_rejects_an_active_thread(self):
        process = object()
        with (
            patch("core.codex._start_app_server", return_value=process),
            patch("core.codex._stop_app_server") as stop,
            patch("core.codex._call", return_value={"thread": {"status": {"type": "active"}}}),
        ):
            with self.assertRaisesRegex(ValueError, "Codex agent is busy"):
                run_codex_turn("thread_1", "New work")

        stop.assert_called_once_with(process)


if __name__ == "__main__":
    unittest.main()
