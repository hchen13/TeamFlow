from __future__ import annotations

import threading
from typing import Any, Callable

from .codex_ipc import CodexTurnAcceptanceUnknown


class CodexQueueUnsupported(ValueError):
    pass


def enqueue_codex_turn(
    request: Callable[[str, dict[str, Any]], dict[str, Any]],
    thread_id: str,
    prompt: str,
    client_message_id: str,
) -> dict[str, Any]:
    try:
        result = request(
            "thread/queue/add",
            {
                "threadId": thread_id,
                "input": [{
                    "type": "text",
                    "text": prompt,
                    "text_elements": [],
                }],
                "clientUserMessageId": client_message_id,
            },
        )
    except ValueError as error:
        message = str(error).lower()
        if any(fragment in message for fragment in (
            "method not found",
            "unknown variant `thread/queue/add`",
            "experimental api",
        )):
            raise CodexQueueUnsupported(str(error)) from error
        if "thread not found" in message or "has been deleted" in message:
            raise
        raise CodexTurnAcceptanceUnknown(
            "Codex queue acceptance could not be confirmed"
        ) from error
    queued = result.get("queuedSubmission")
    if not isinstance(queued, dict):
        raise CodexTurnAcceptanceUnknown(
            "Codex queue acceptance could not be confirmed"
        )
    return queued


def run_codex_queued_turn(
    thread_id: str,
    prompt: str,
    *,
    client_message_id: str,
    enqueue: Callable[[str, str, str], dict[str, Any]],
    on_queued: Callable[[str], None] | None,
    stop_event: threading.Event | None,
) -> dict[str, Any]:
    if stop_event is not None and stop_event.is_set():
        raise CodexTurnAcceptanceUnknown(
            "TeamFlow stopped before the Codex message was queued"
        )
    queued = enqueue(thread_id, prompt, client_message_id)
    queue_id = str(queued.get("id") or "")
    if not queue_id:
        raise CodexTurnAcceptanceUnknown(
            "Codex accepted the queue request without returning a submission ID"
        )
    if on_queued:
        try:
            on_queued(queue_id)
        except Exception as error:
            raise CodexTurnAcceptanceUnknown(
                "Codex queued the message, but TeamFlow could not persist acceptance"
            ) from error
    return {
        "ok": True,
        "thread_id": thread_id,
        "turn_id": None,
        "status": "queued",
        "queue_id": queue_id,
        "response": None,
        "declined_requests": [],
        "error": None,
        "transport": "codex-queue",
    }


def delete_codex_queued_message(
    request: Callable[[str, dict[str, Any]], dict[str, Any]],
    thread_id: str,
    client_message_id: str,
) -> bool:
    result = request(
        "thread/queue/list",
        {"threadId": thread_id, "limit": 100},
    )
    for queued in result.get("data") or []:
        if not isinstance(queued, dict):
            continue
        if str(queued.get("clientUserMessageId") or "") != client_message_id:
            continue
        queue_id = str(queued.get("id") or "")
        if not queue_id:
            continue
        deleted = request(
            "thread/queue/delete",
            {
                "threadId": thread_id,
                "queuedSubmissionId": queue_id,
            },
        )
        return bool(deleted.get("deleted"))
    return False


def codex_queued_message_exists(
    request: Callable[[str, dict[str, Any]], dict[str, Any]],
    thread_id: str,
    client_message_id: str,
) -> bool:
    result = request(
        "thread/queue/list",
        {"threadId": thread_id, "limit": 100},
    )
    return any(
        isinstance(queued, dict)
        and str(queued.get("clientUserMessageId") or "") == client_message_id
        for queued in result.get("data") or []
    )
