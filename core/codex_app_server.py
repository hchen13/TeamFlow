from __future__ import annotations

import threading
import time
from typing import Any, Callable


TERMINAL_TURN_STATUSES = {
    "completed",
    "success",
    "failed",
    "interrupted",
    "cancelled",
    "canceled",
}


class CodexAppServerRuntime:
    def __init__(
        self,
        *,
        resolve: Callable[[str], Callable[..., Any]],
    ) -> None:
        self.resolve = resolve
        self.active_lock = threading.Lock()
        self.active_turns: dict[str, dict[str, Any]] = {}

    def run_turn(
        self,
        thread: str,
        prompt: str,
        *,
        client_message_id: str,
        on_started: Callable[[str], None] | None,
        stop_event: threading.Event | None,
        approval_policy: str,
        sandbox_policy: dict[str, Any],
    ) -> dict[str, Any]:
        process = self.resolve("_start_app_server")()
        pending: list[dict[str, Any]] = []
        control: dict[str, Any] | None = None
        try:
            resumed = self.resolve("_call")(
                process,
                2,
                "thread/resume",
                {"threadId": thread},
                pending=pending,
            )
            runtime_status = (
                ((resumed.get("thread") or {}).get("status") or {}).get("type")
            )
            if runtime_status == "active":
                raise ValueError("Codex agent is busy")
            started = self.resolve("_call")(
                process,
                3,
                "turn/start",
                {
                    "threadId": thread,
                    "clientUserMessageId": client_message_id,
                    "approvalPolicy": approval_policy,
                    "sandboxPolicy": sandbox_policy,
                    "input": [
                        {
                            "type": "text",
                            "text": prompt,
                            "text_elements": [],
                        }
                    ],
                },
                pending=pending,
            )
            turn = started.get("turn")
            if not isinstance(turn, dict) or not turn.get("id"):
                raise ValueError("Codex turn/start did not return a turn")
            turn_id = str(turn["id"])
            control = {
                "turn_id": turn_id,
                "interrupt_requested": threading.Event(),
                "completed": threading.Event(),
                "result": None,
                "error": None,
            }
            with self.active_lock:
                self.active_turns[thread] = control
            if on_started:
                on_started(turn_id)
            self.resolve("_notify_codex_clients_thread_changed")(thread)
            return self._wait_for_turn(
                process,
                thread,
                turn_id,
                pending,
                control,
                stop_event,
            )
        except Exception as error:
            if control is not None:
                control["error"] = error
                control["completed"].set()
            raise
        finally:
            if control is not None:
                with self.active_lock:
                    if self.active_turns.get(thread) is control:
                        self.active_turns.pop(thread, None)
            self.resolve("_stop_app_server")(process)

    def stop_turn(
        self,
        thread: str,
        *,
        expected_turn_id: str,
    ) -> dict[str, Any]:
        with self.active_lock:
            active = self.active_turns.get(thread)
        if not active:
            return self.resolve("_interrupt_codex_app_server_turn")(
                thread,
                expected_turn_id=expected_turn_id,
            )
        active_turn = str(active.get("turn_id") or "")
        if active_turn != expected_turn_id:
            raise ValueError(
                f"Codex session is running turn {active_turn or 'unknown'}, "
                f"not TeamFlow task turn {expected_turn_id}"
            )
        active["interrupt_requested"].set()
        if not active["completed"].wait(timeout=20):
            raise ValueError("Codex turn did not stop within 20 seconds")
        if active.get("error"):
            raise ValueError(str(active["error"]))
        result = active.get("result")
        if not isinstance(result, dict):
            raise ValueError(
                "Codex app-server did not confirm the stopped turn"
            )
        return {
            "ok": True,
            "thread_id": thread,
            "turn_id": result.get("turn_id"),
            "status": result.get("status"),
            "already_stopped": result.get("status") != "interrupted",
            "transport": "app-server",
        }

    def interrupt_turn(
        self,
        thread: str,
        *,
        expected_turn_id: str,
    ) -> dict[str, Any]:
        process = self.resolve("_start_app_server")()
        pending: list[dict[str, Any]] = []
        try:
            resumed = self.resolve("_call")(
                process,
                2,
                "thread/resume",
                {"threadId": thread},
                pending=pending,
            )
            thread_state = resumed.get("thread")
            if not isinstance(thread_state, dict):
                raise ValueError("Codex app-server did not return a thread")
            if not thread_state.get("turns"):
                read = self.resolve("_call")(
                    process,
                    3,
                    "thread/read",
                    {"threadId": thread, "includeTurns": True},
                    pending=pending,
                )
                thread_state = read.get("thread")
                if not isinstance(thread_state, dict):
                    raise ValueError(
                        "Codex app-server did not return a thread"
                    )
            turns = [
                turn
                for turn in thread_state.get("turns") or []
                if isinstance(turn, dict) and turn.get("id")
            ]
            expected = next(
                (
                    turn
                    for turn in reversed(turns)
                    if str(turn.get("id") or "") == expected_turn_id
                ),
                None,
            )
            if expected is None:
                raise ValueError(
                    f"Codex task turn {expected_turn_id} is not visible "
                    f"in session {thread}"
                )
            expected_status = _turn_status(expected)
            if expected_status in TERMINAL_TURN_STATUSES:
                return {
                    "ok": True,
                    "thread_id": thread,
                    "turn_id": expected_turn_id,
                    "status": expected_status,
                    "already_stopped": True,
                    "transport": "app-server",
                }
            active = next(
                (
                    turn
                    for turn in reversed(turns)
                    if _turn_status(turn) not in TERMINAL_TURN_STATUSES
                ),
                None,
            )
            if active is None:
                raise ValueError(
                    f"Codex task turn {expected_turn_id} is not terminal, "
                    "but the session has no active turn"
                )
            turn_id = str(active["id"])
            if turn_id != expected_turn_id:
                raise ValueError(
                    f"Codex session is running turn {turn_id}, "
                    f"not TeamFlow task turn {expected_turn_id}"
                )
            self.resolve("_call")(
                process,
                4,
                "turn/interrupt",
                {"threadId": thread, "turnId": turn_id},
                pending=pending,
            )
            return self._wait_for_interruption(
                process,
                thread,
                turn_id,
                pending,
            )
        finally:
            self.resolve("_stop_app_server")(process)

    def interrupt_exact_turn(
        self,
        thread: str,
        *,
        turn_id: str,
    ) -> dict[str, Any]:
        process = self.resolve("_start_app_server")()
        pending: list[dict[str, Any]] = []
        try:
            self.resolve("_call")(
                process,
                2,
                "thread/resume",
                {"threadId": thread},
                pending=pending,
            )
            self.resolve("_call")(
                process,
                3,
                "turn/interrupt",
                {"threadId": thread, "turnId": turn_id},
                pending=pending,
            )
            return self._wait_for_interruption(
                process,
                thread,
                turn_id,
                pending,
            )
        finally:
            self.resolve("_stop_app_server")(process)

    def _wait_for_turn(
        self,
        process: Any,
        thread: str,
        turn_id: str,
        pending: list[dict[str, Any]],
        control: dict[str, Any],
        stop_event: threading.Event | None,
    ) -> dict[str, Any]:
        final_message = None
        last_message = None
        declined_requests: list[str] = []
        interrupt_sent = False
        while True:
            if control["interrupt_requested"].is_set() and not interrupt_sent:
                self.resolve("_send")(process, {
                    "id": 4,
                    "method": "turn/interrupt",
                    "params": {
                        "threadId": thread,
                        "turnId": turn_id,
                    },
                })
                interrupt_sent = True
            payload = (
                pending.pop(0)
                if pending
                else self.resolve("_read_turn_payload")(
                    process,
                    stop_event,
                    wake_event=control["interrupt_requested"],
                )
            )
            method = payload.get("method")
            if payload.get("id") == 4 and not method:
                if payload.get("error"):
                    control["error"] = (
                        payload["error"].get("message")
                        or "Codex turn/interrupt failed"
                    )
                    control["completed"].set()
                continue
            if payload.get("id") is not None and method:
                declined_requests.append(str(method))
                self._decline_request(process, payload)
                continue
            params = payload.get("params") or {}
            if method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and item.get("text"):
                    last_message = str(item["text"])
                    if item.get("phase") == "final_answer":
                        final_message = last_message
                continue
            if method != "turn/completed":
                continue
            completed = params.get("turn") or {}
            if str(completed.get("id") or "") != turn_id:
                continue
            status = str(completed.get("status") or "failed")
            error = completed.get("error") or {}
            error_message = (
                str(
                    error.get("message")
                    or error.get("additionalDetails")
                    or ""
                ).strip()
                or None
            )
            self.resolve("_notify_codex_clients_thread_changed")(thread)
            result = {
                "ok": status == "completed",
                "thread_id": thread,
                "turn_id": turn_id,
                "status": status,
                "response": final_message or last_message,
                "declined_requests": declined_requests,
                "error": error_message,
                "transport": "app-server",
            }
            control["result"] = result
            control["completed"].set()
            return result

    def _decline_request(
        self,
        process: Any,
        payload: dict[str, Any],
    ) -> None:
        method = payload.get("method")
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            self.resolve("_send")(
                process,
                {
                    "id": payload["id"],
                    "result": {"decision": "decline"},
                },
            )
            return
        self.resolve("_send")(
            process,
            {
                "id": payload["id"],
                "error": {
                    "code": -32601,
                    "message": "TeamFlow cannot answer interactive requests",
                },
            },
        )

    def _wait_for_interruption(
        self,
        process: Any,
        thread: str,
        turn_id: str,
        pending: list[dict[str, Any]],
    ) -> dict[str, Any]:
        deadline = time.monotonic() + 20
        while True:
            payload = (
                pending.pop(0)
                if pending
                else self.resolve("_read_payload")(
                    process,
                    deadline,
                    "Codex turn did not stop within 20 seconds",
                )
            )
            if payload.get("method") != "turn/completed":
                continue
            completed = (payload.get("params") or {}).get("turn") or {}
            if str(completed.get("id") or "") != turn_id:
                continue
            self.resolve("_notify_codex_clients_thread_changed")(thread)
            return {
                "ok": True,
                "thread_id": thread,
                "turn_id": turn_id,
                "status": str(completed.get("status") or "interrupted"),
                "already_stopped": False,
                "transport": "app-server",
            }


def _turn_status(turn: dict[str, Any]) -> str:
    status = turn.get("status")
    if isinstance(status, dict):
        return str(status.get("type") or "")
    return str(status or "")
