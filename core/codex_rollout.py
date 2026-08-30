from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


def codex_thread_name(thread: dict[str, Any]) -> str | None:
    name = str(thread.get("name") or "").strip()
    if name:
        return name
    preview = str(thread.get("preview") or "").strip()
    return preview.splitlines()[0] if preview else None


def codex_thread_settings(thread: dict[str, Any]) -> dict[str, str]:
    path_value = str(thread.get("path") or "").strip()
    if not path_value:
        return {}
    fallback = {}
    try:
        lines = _reverse_lines(Path(path_value).expanduser())
        for raw_line in lines:
            if b'"thread_settings"' not in raw_line and b'"turn_context"' not in raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            settings = payload.get("thread_settings")
            if isinstance(settings, dict):
                return {
                    key: str(value)
                    for key, value in {
                        "model": settings.get("model"),
                        "effort": settings.get("reasoning_effort") or settings.get("effort"),
                        "service_tier": settings.get("service_tier"),
                    }.items()
                    if value is not None
                }
            if record.get("type") == "turn_context" and not fallback:
                fallback = {
                    key: str(value)
                    for key, value in {
                        "model": payload.get("model"),
                        "effort": payload.get("effort") or payload.get("reasoning_effort"),
                        "service_tier": payload.get("service_tier"),
                    }.items()
                    if value is not None
                }
    except OSError:
        return {}
    return fallback


def codex_background_turn_permissions(thread_id: str) -> dict[str, Any]:
    permission_profile = _codex_explicit_permission_profile_id(thread_id)
    if permission_profile is None:
        sandbox_type = _codex_persisted_sandbox_type(thread_id)
        if sandbox_type in {"disabled", "dangerFullAccess", "danger-full-access"}:
            permission_profile = ":danger-full-access"
        else:
            permission_profile = ":workspace"
    return {
        "approvalPolicy": "never",
        "permissions": permission_profile,
    }


def codex_developer_context_evidence(
    thread_id: str,
    expected_contexts: dict[str, str],
    *,
    injected_at: str | None = None,
) -> dict[str, Any]:
    rollout_path = _codex_rollout_path(thread_id)
    if rollout_path is None:
        return {"status": "unavailable"}
    threshold = _parse_timestamp(injected_at)
    if threshold is not None:
        threshold -= timedelta(seconds=5)
    try:
        for raw_line in _reverse_lines(rollout_path):
            if b"developer" not in raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            timestamp = _parse_timestamp(record.get("timestamp"))
            if threshold is not None and timestamp is not None and timestamp < threshold:
                break
            payload = record.get("payload")
            if record.get("type") != "response_item" or not isinstance(payload, dict):
                continue
            if payload.get("type") != "message" or payload.get("role") != "developer":
                continue
            text = "\n".join(
                str(item.get("text") or "")
                for item in payload.get("content") or []
                if isinstance(item, dict) and item.get("type") == "input_text"
            )
            context_kind = next(
                (kind for kind, expected in expected_contexts.items() if text == expected),
                None,
            )
            if context_kind is None:
                continue
            metadata = payload.get("internal_chat_message_metadata_passthrough")
            turn_id = metadata.get("turn_id") if isinstance(metadata, dict) else None
            return {
                "status": "verified",
                "context_kind": context_kind,
                "turn_id": str(turn_id) if turn_id else None,
                "timestamp": record.get("timestamp"),
                "rollout_path": str(rollout_path),
            }
    except OSError:
        return {"status": "unavailable"}
    return {
        "status": "missing",
        "rollout_path": str(rollout_path),
    }


def codex_thread_error(thread: dict[str, Any]) -> str | None:
    for turn in reversed(thread.get("turns") or []):
        error = turn.get("error") if isinstance(turn, dict) else None
        if not isinstance(error, dict):
            continue
        message = str(error.get("message") or "").strip()
        details = str(error.get("additionalDetails") or "").strip()
        if message and details and details != message:
            return f"{message}: {details}"
        if message or details:
            return message or details
    return None


def codex_thread_is_permanently_unavailable(error: Exception) -> bool:
    message = str(error).lower()
    return any(fragment in message for fragment in (
        "no rollout found for thread id",
        "thread not found",
        "thread has been deleted",
    ))


def codex_delivery_error_is_terminal(error: Exception) -> bool:
    return codex_thread_is_permanently_unavailable(error)


def codex_turn(thread: dict[str, Any], turn_id: str) -> dict[str, Any] | None:
    for item in thread.get("turns") or []:
        if isinstance(item, dict) and str(item.get("id") or "") == turn_id:
            return item
    return None


def codex_turn_by_client_message_id(
    thread: dict[str, Any],
    client_message_id: str,
) -> dict[str, Any] | None:
    if not client_message_id:
        return None
    for turn in reversed(thread.get("turns") or []):
        if not isinstance(turn, dict):
            continue
        for item in turn.get("items") or []:
            if (
                isinstance(item, dict)
                and item.get("type") == "userMessage"
                and str(item.get("clientId") or "") == client_message_id
            ):
                return turn
    return None


def codex_turn_id_by_client_message_id(
    thread_id: str,
    client_message_id: str,
) -> str | None:
    rollout_path = _codex_rollout_path(thread_id)
    if rollout_path is None or not client_message_id:
        return None
    found_message = False
    try:
        for raw_line in _reverse_lines(rollout_path):
            if b'"event_msg"' not in raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            payload = record.get("payload")
            if record.get("type") != "event_msg" or not isinstance(payload, dict):
                continue
            if not found_message:
                found_message = bool(
                    payload.get("type") == "user_message"
                    and str(payload.get("client_id") or "") == client_message_id
                )
                continue
            if payload.get("type") == "task_started" and payload.get("turn_id"):
                return str(payload["turn_id"])
    except OSError:
        return None
    return None


def _codex_turn_lifecycle_event(thread_id: str, turn_id: str) -> str | None:
    rollout_path = _codex_rollout_path(thread_id)
    if rollout_path is None or not turn_id:
        return None
    try:
        for raw_line in _reverse_lines(rollout_path):
            if (
                b'"task_complete"' not in raw_line
                and b'"task_started"' not in raw_line
            ):
                continue
            try:
                record = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            payload = record.get("payload")
            if record.get("type") != "event_msg" or not isinstance(payload, dict):
                continue
            if str(payload.get("turn_id") or "") != turn_id:
                continue
            event_type = str(payload.get("type") or "")
            if event_type in {"task_started", "task_complete"}:
                return event_type
    except OSError:
        return None
    return None


def codex_turn_completed(thread_id: str, turn_id: str) -> bool:
    return _codex_turn_lifecycle_event(thread_id, turn_id) == "task_complete"


def codex_turn_started(thread_id: str, turn_id: str) -> bool:
    return _codex_turn_lifecycle_event(thread_id, turn_id) is not None


def codex_rollout_runtime(thread_id: str) -> dict[str, Any] | None:
    """Return the latest durable turn lifecycle without loading the rollout into memory."""
    rollout_path = _codex_rollout_path(thread_id)
    if rollout_path is None:
        return None
    try:
        for raw_line in _reverse_lines(rollout_path):
            if (
                b'"task_started"' not in raw_line
                and b'"task_complete"' not in raw_line
                and b'"turn_aborted"' not in raw_line
            ):
                continue
            try:
                record = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            payload = record.get("payload")
            if record.get("type") != "event_msg" or not isinstance(payload, dict):
                continue
            event = str(payload.get("type") or "")
            if event not in {"task_started", "task_complete", "turn_aborted"}:
                continue
            timestamp_key = "started_at" if event == "task_started" else "completed_at"
            observed_at = _epoch_milliseconds(payload.get(timestamp_key), record.get("timestamp"))
            return {
                "status": "active" if event == "task_started" else "idle",
                "event": event,
                "turn_id": str(payload.get("turn_id") or "") or None,
                "observed_at_ms": observed_at,
            }
    except OSError:
        return None
    return None


def _epoch_milliseconds(value: Any, timestamp: Any) -> int:
    try:
        return int(float(value) * 1000)
    except (TypeError, ValueError):
        parsed = _parse_timestamp(timestamp)
        return int(parsed.timestamp() * 1000) if parsed is not None else 0


def codex_turn_unresolved_teamflow_mcp_failures(
    turn: dict[str, Any],
) -> list[dict[str, Any]]:
    unresolved: dict[tuple[str, str], dict[str, Any]] = {}
    for item in turn.get("items") or []:
        if not isinstance(item, dict) or item.get("type") != "mcpToolCall":
            continue
        if item.get("server") != "teamflow" and item.get("pluginId") != "teamflow@teamflow":
            continue
        tool = str(item.get("tool") or "")
        arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        signature = (
            tool,
            json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
        status = str(item.get("status") or "")
        if status in {"completed", "success"}:
            unresolved.pop(signature, None)
            continue
        error = _mcp_tool_call_error(item)
        if status == "failed" and _is_daemon_connection_error(error):
            unresolved[signature] = {
                "tool": tool,
                "arguments": arguments,
                "error": error,
            }
    return list(unresolved.values())


def _reverse_lines(path: Path, *, chunk_size: int = 65536) -> Iterator[bytes]:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell()
        remainder = b""
        while position:
            size = min(chunk_size, position)
            position -= size
            stream.seek(position)
            parts = (stream.read(size) + remainder).split(b"\n")
            remainder = parts[0]
            for line in reversed(parts[1:]):
                if line:
                    yield line.rstrip(b"\r")
        if remainder:
            yield remainder.rstrip(b"\r")


def _codex_rollout_path(thread_id: str) -> Path | None:
    codex_home = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()
    for database in _codex_state_databases(codex_home):
        try:
            with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    "SELECT rollout_path FROM threads WHERE id = ?",
                    (thread_id,),
                ).fetchone()
        except (OSError, sqlite3.Error):
            continue
        if row and row[0]:
            path = Path(str(row[0])).expanduser()
            if path.is_file():
                return path
    for directory_name in ("sessions", "archived_sessions"):
        directory = codex_home / directory_name
        if not directory.is_dir():
            continue
        match = next(directory.rglob(f"*{thread_id}.jsonl"), None)
        if match is not None:
            return match
    return None


def _codex_persisted_sandbox_type(thread_id: str) -> str | None:
    codex_home = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()
    for database in _codex_state_databases(codex_home):
        try:
            with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    "SELECT sandbox_policy FROM threads WHERE id = ?",
                    (thread_id,),
                ).fetchone()
        except (OSError, sqlite3.Error):
            continue
        if row is None:
            continue
        try:
            policy = json.loads(str(row[0] or ""))
        except json.JSONDecodeError:
            return None
        if not isinstance(policy, dict):
            return None
        sandbox_type = str(policy.get("type") or "").strip()
        return sandbox_type or None
    return None


def _codex_explicit_permission_profile_id(thread_id: str) -> str | None:
    rollout_path = _codex_rollout_path(thread_id)
    if rollout_path is None:
        return None
    try:
        for raw_line in _reverse_lines(rollout_path):
            if b'"thread_settings"' not in raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            settings = payload.get("thread_settings")
            if not isinstance(settings, dict):
                continue
            active = settings.get("active_permission_profile")
            if not isinstance(active, dict):
                continue
            profile_id = str(active.get("id") or "").strip()
            if profile_id:
                return profile_id
    except OSError:
        return None
    return None


def _codex_state_databases(codex_home: Path) -> list[Path]:
    databases = [
        *codex_home.glob("state_*.sqlite"),
        *(codex_home / "sqlite").glob("state_*.sqlite"),
    ]

    def modified_at(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return -1

    return sorted(set(databases), key=modified_at, reverse=True)


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mcp_tool_call_error(item: dict[str, Any]) -> str:
    parts: list[str] = []
    error = item.get("error")
    if isinstance(error, dict):
        parts.extend(str(error.get(key) or "") for key in ("message", "additionalDetails"))
    elif error:
        parts.append(str(error))
    result = item.get("result")
    if isinstance(result, dict):
        for content in result.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "text" and content.get("text"):
                parts.append(str(content["text"]))
    return " ".join(part.strip() for part in parts if part and part.strip())


def _is_daemon_connection_error(error: str) -> bool:
    message = error.lower()
    return any(fragment in message for fragment in (
        "closed the connection without a response",
        "authorization is missing or expired",
        "connection refused",
        "daemon.sock",
        "no such file or directory",
    ))
