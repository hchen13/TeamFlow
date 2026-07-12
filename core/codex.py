from __future__ import annotations

import json
import os
import select
import subprocess
import time
from typing import Any


def read_codex_thread(thread_id: str, *, include_turns: bool = False) -> dict[str, Any]:
    result = _request("thread/read", {"threadId": thread_id, "includeTurns": include_turns})
    thread = result.get("thread")
    if not isinstance(thread, dict):
        raise ValueError("Codex app-server did not return a thread")
    return thread


def list_codex_threads(cwd: str, *, archived: bool = False) -> list[dict[str, Any]]:
    threads: list[dict[str, Any]] = []
    cursor = None
    while True:
        params: dict[str, Any] = {
            "archived": archived,
            "cwd": cwd,
            "limit": 100,
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "useStateDbOnly": True,
        }
        if cursor:
            params["cursor"] = cursor
        result = _request("thread/list", params)
        page = result.get("data")
        if not isinstance(page, list):
            raise ValueError("Codex app-server did not return a thread list")
        threads.extend(thread for thread in page if isinstance(thread, dict))
        cursor = result.get("nextCursor")
        if not cursor:
            return threads


def codex_thread_name(thread: dict[str, Any]) -> str | None:
    name = str(thread.get("name") or "").strip()
    if name:
        return name
    preview = str(thread.get("preview") or "").strip()
    return preview.splitlines()[0] if preview else None


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


def _request(method: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        process = subprocess.Popen(
            [os.environ.get("CODEX_CLI", "codex"), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except OSError as error:
        raise ValueError(f"failed to start Codex app-server: {error}") from error
    try:
        _send(process, {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "teamflow", "version": "0.1"}}})
        initialized = _response_for(process, 1)
        if initialized.get("error"):
            raise ValueError(initialized["error"].get("message") or "Codex app-server initialization failed")
        _send(process, {"method": "initialized", "params": {}})
        _send(process, {"id": 2, "method": method, "params": params})
        response = _response_for(process, 2)
        if response.get("error"):
            raise ValueError(response["error"].get("message") or f"Codex {method} failed")
        result = response.get("result")
        if not isinstance(result, dict):
            raise ValueError(f"Codex {method} returned an invalid result")
        return result
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _send(process: subprocess.Popen[bytes], payload: dict[str, Any]) -> None:
    if process.stdin is None:
        raise ValueError("Codex app-server stdin is unavailable")
    try:
        process.stdin.write((json.dumps(payload) + "\n").encode())
        process.stdin.flush()
    except OSError as error:
        raise ValueError(f"Codex app-server connection failed: {error}") from error


def _response_for(process: subprocess.Popen[bytes], request_id: int) -> dict[str, Any]:
    if process.stdout is None:
        raise ValueError("Codex app-server stdout is unavailable")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
        if not ready:
            break
        line = process.stdout.readline()
        if not line:
            break
        try:
            payload = json.loads(line.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Codex app-server returned invalid JSON") from error
        if payload.get("id") == request_id:
            return payload
    if process.poll() is not None:
        stderr = process.stderr.read().decode(errors="replace").strip() if process.stderr else ""
        raise ValueError(stderr or "Codex app-server exited unexpectedly")
    raise ValueError("Codex app-server timed out")
