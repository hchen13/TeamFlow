from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import queue
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import resolve_workspace_paths
from .db import now
from .lark_events import (
    LISTENER_CONNECT_TIMEOUT,
    LISTENER_EVENT_TIMEOUT,
    LISTENER_PROBE_ATTEMPTS,
    LarkEventContext,
    context_client,
    ensure_lark_board_subscription,
    event_matches_board,
    event_record_ids,
    lark_event_context,
    lark_listener_details,
    listener_failure,
    resolve_lark_event_table,
    run_lark_app_worker,
    save_listener_result,
)


ROOT = Path(__file__).resolve().parents[1]
MAX_IPC_MESSAGE_BYTES = 1024 * 1024


def teamflow_home() -> Path:
    return Path(os.environ.get("TEAMFLOW_HOME") or Path.home() / ".teamflow").expanduser().resolve()


def daemon_socket_path() -> Path:
    return teamflow_home() / "daemon.sock"


def register_workspace(workspace: str | None) -> str:
    root = str(resolve_workspace_paths(workspace).root)
    home = _ensure_home()
    database = home / "teamflow.db"
    with sqlite3.connect(database) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workspaces (
              root_path TEXT PRIMARY KEY,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO workspaces (root_path, updated_at) VALUES (?, ?)
            ON CONFLICT(root_path) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (root, datetime.now(timezone.utc).isoformat()),
        )
    os.chmod(database, 0o600)
    return root


def registered_workspaces() -> list[str]:
    database = teamflow_home() / "teamflow.db"
    if not database.exists():
        return []
    with sqlite3.connect(database) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workspaces (
              root_path TEXT PRIMARY KEY,
              updated_at TEXT NOT NULL
            )
            """
        )
        return [str(row[0]) for row in conn.execute("SELECT root_path FROM workspaces ORDER BY updated_at")]


class TeamFlowDaemon:
    def __init__(self) -> None:
        self.mp = multiprocessing.get_context("spawn")
        self.event_queue = self.mp.Queue()
        self.workers: dict[str, dict[str, Any]] = {}
        self.routes: dict[str, LarkEventContext] = {}
        self.recent: deque[tuple[int, str, dict[str, Any]]] = deque(maxlen=1000)
        self.sequence = 0
        self.condition = threading.Condition()
        self.sync_lock = threading.RLock()
        self.stopping = threading.Event()
        self.event_thread = threading.Thread(target=self._consume_events, name="teamflow-lark-events", daemon=True)
        self.event_thread.start()

    @staticmethod
    def app_key(context: LarkEventContext) -> str:
        return f"{context.brand}:{context.app_id}"

    def sync_workspace(self, workspace: str | None, *, identity_id: str | None = None) -> dict[str, Any]:
        with self.sync_lock:
            context = lark_event_context(workspace, identity_id=identity_id)
            self._ensure_app(context)
            context = resolve_lark_event_table(context)
            already_subscribed = ensure_lark_board_subscription(context)
            previous = self.routes.get(context.workspace_root)
            self.routes[context.workspace_root] = context
            if previous and self.app_key(previous) != self.app_key(context):
                previous_key = self.app_key(previous)
                if all(self.app_key(route) != previous_key for route in self.routes.values()):
                    worker = self.workers.pop(previous_key, None)
                    if worker:
                        self._stop_worker(worker)
            return {
                "ok": True,
                "already_subscribed": already_subscribed,
                "daemon_pid": os.getpid(),
                **context.public(),
            }

    def verify_workspace(self, workspace: str | None, *, identity_id: str | None = None) -> dict[str, Any]:
        context = None
        already_subscribed = False
        client = None
        record_id = ""
        cleaned = False
        probe_record_ids: set[str] = set()
        error = None
        try:
            synced = self.sync_workspace(workspace, identity_id=identity_id)
            context = self.routes[synced["workspace_root"]]
            already_subscribed = bool(synced["already_subscribed"])
            client = context_client(context)
            cursor = self.cursor()
            for _ in range(LISTENER_PROBE_ATTEMPTS):
                cleaned = False
                created = client.upsert_record(context.table_id, {})
                record_id = str(created.get("record_id") or created.get("id") or "")
                if not record_id:
                    raise ValueError("Lark did not return the listener verification record ID")
                probe_record_ids.add(record_id)
                client.delete_record(context.table_id, record_id)
                cleaned = True
                record_id = ""
                if self.wait_for_records(context, probe_record_ids, cursor):
                    break
            else:
                raise ValueError("the app did not receive the Bitable record change event")
        except Exception as caught:
            error = caught
        finally:
            if record_id and client and not cleaned:
                try:
                    client.delete_record(context.table_id, record_id)
                except Exception as cleanup_error:
                    error = ValueError(f"listener probe record cleanup failed: {cleanup_error}")

        timestamp = now()
        if error:
            result = {
                "ok": False,
                "status": "failed",
                "last_verified_at": timestamp,
                "already_subscribed": already_subscribed,
                **listener_failure(error, context),
            }
        else:
            result = {
                "ok": True,
                "status": "verified",
                "failure_kind": None,
                "last_error": None,
                "last_verified_at": timestamp,
                "repair_url": None,
                "already_subscribed": already_subscribed,
                **context.public(),
            }
        if context and not result.get("workspace_root"):
            result.update(context.public())
        save_listener_result(workspace, context.identity_id if context else identity_id, result)
        return result

    def listener_details(self, workspace: str | None) -> dict[str, Any]:
        root = str(resolve_workspace_paths(workspace).root)
        context = self.routes.get(root)
        if context is None:
            self.sync_workspace(root)
            context = self.routes[root]
        return lark_listener_details(context)

    def cursor(self) -> int:
        with self.condition:
            return self.sequence

    def wait_for_records(self, context: LarkEventContext, record_ids: set[str], cursor: int) -> bool:
        deadline = time.monotonic() + LISTENER_EVENT_TIMEOUT
        app_key = self.app_key(context)
        with self.condition:
            while True:
                for sequence, event_app_key, payload in self.recent:
                    if (
                        sequence > cursor
                        and event_app_key == app_key
                        and event_matches_board(payload, context.public())
                        and record_ids.intersection(event_record_ids(payload))
                    ):
                        return True
                worker = self.workers.get(app_key)
                if not worker or not worker["process"].is_alive():
                    raise ValueError("the TeamFlow Lark listener stopped before receiving the test event")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(min(remaining, 0.5))

    def wait_for_workspace_event(self, workspace: str, cursor: int, timeout: float = 1.0) -> tuple[int, dict[str, Any]] | None:
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                context = self.routes.get(workspace)
                if context is None:
                    raise ValueError("workspace is not synchronized with the TeamFlow daemon")
                app_key = self.app_key(context)
                for sequence, event_app_key, payload in self.recent:
                    if sequence > cursor and event_app_key == app_key and event_matches_board(payload, context.public()):
                        return sequence, payload
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.stopping.is_set():
                    return None
                self.condition.wait(remaining)

    def publish(self, app_key: str, payload: dict[str, Any]) -> None:
        with self.condition:
            self.sequence += 1
            self.recent.append((self.sequence, app_key, payload))
            self.condition.notify_all()

    def status(self) -> dict[str, Any]:
        with self.sync_lock:
            apps = [
                {
                    "app_id": worker["context"].app_id,
                    "app_name": worker["context"].app_name,
                    "brand": worker["context"].brand,
                    "connected": worker["process"].is_alive() and worker["ready"].is_set(),
                }
                for worker in self.workers.values()
            ]
            routes = [
                {
                    "workspace_root": root,
                    "app_id": context.app_id,
                    "file_token": context.file_token,
                    "table_id": context.table_id,
                }
                for root, context in self.routes.items()
            ]
        return {"running": True, "pid": os.getpid(), "apps": apps, "workspaces": routes}

    def close(self) -> None:
        self.stopping.set()
        with self.condition:
            self.condition.notify_all()
        with self.sync_lock:
            for worker in self.workers.values():
                self._stop_worker(worker)
            self.workers.clear()
        self.event_queue.put(None)
        self.event_thread.join(timeout=2)
        self.event_queue.close()

    def _ensure_app(self, context: LarkEventContext) -> None:
        app_key = self.app_key(context)
        credentials = (context.app_id, context.app_secret, context.brand)
        worker = self.workers.get(app_key)
        if worker and worker["process"].is_alive() and worker["credentials"] == credentials:
            return
        if worker:
            self._stop_worker(worker)

        ready = self.mp.Event()
        errors = self.mp.Queue()
        process = self.mp.Process(
            target=_lark_app_worker,
            args=(context, app_key, self.event_queue, ready, errors),
            daemon=True,
        )
        process.start()
        worker = {
            "context": context,
            "credentials": credentials,
            "process": process,
            "ready": ready,
            "errors": errors,
        }
        self.workers[app_key] = worker
        if not ready.wait(LISTENER_CONNECT_TIMEOUT):
            self._stop_worker(worker)
            self.workers.pop(app_key, None)
            raise ValueError("timed out while connecting to the Lark event stream")
        try:
            worker_error = errors.get_nowait()
        except queue.Empty:
            worker_error = None
        if worker_error or not process.is_alive():
            self._stop_worker(worker)
            self.workers.pop(app_key, None)
            raise ValueError(worker_error or "the Lark event stream stopped before synchronization")

    def _stop_worker(self, worker: dict[str, Any]) -> None:
        process = worker["process"]
        if process.is_alive():
            process.terminate()
        process.join(timeout=2)
        worker["errors"].close()

    def _consume_events(self) -> None:
        while True:
            message = self.event_queue.get()
            if message is None:
                return
            if isinstance(message, dict) and isinstance(message.get("payload"), dict):
                self.publish(str(message.get("app_key") or ""), message["payload"])


def _lark_app_worker(context: LarkEventContext, app_key: str, events: Any, ready: Any, errors: Any) -> None:
    try:
        run_lark_app_worker(
            context,
            emit=lambda payload: events.put({"app_key": app_key, "payload": payload}),
            ready=ready.set,
        )
    except Exception as error:
        errors.put(str(error))
        ready.set()


class DaemonServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, runtime: TeamFlowDaemon):
        self.runtime = runtime
        super().__init__(path, DaemonRequestHandler)


class DaemonRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            line = self.rfile.readline(MAX_IPC_MESSAGE_BYTES + 1)
            if not line or len(line) > MAX_IPC_MESSAGE_BYTES:
                self._write({"ok": False, "error": "invalid TeamFlow daemon request"})
                return
            request = json.loads(line)
            action = request.get("action")
            if action == "listen":
                self._listen(request)
                return
            if action == "ping":
                result = self.server.runtime.status()
            elif action == "sync_workspace":
                result = self.server.runtime.sync_workspace(request.get("workspace"), identity_id=request.get("identity_id"))
            elif action == "verify_listener":
                result = self.server.runtime.verify_workspace(request.get("workspace"), identity_id=request.get("identity_id"))
            elif action == "shutdown":
                result = {"stopping": True, **self.server.runtime.status()}
                self._write({"ok": True, "result": result})
                threading.Thread(target=self.server.shutdown, name="teamflow-daemon-stop", daemon=True).start()
                return
            else:
                raise ValueError(f"unknown TeamFlow daemon action: {action}")
            self._write({"ok": True, "result": result})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            try:
                self._write({"ok": False, "error": str(error)})
            except (BrokenPipeError, ConnectionResetError):
                return

    def _listen(self, request: dict[str, Any]) -> None:
        workspace = str(resolve_workspace_paths(request.get("workspace")).root)
        details = self.server.runtime.listener_details(workspace)
        self._write({"ok": True, "result": {"details": details}})
        cursor = self.server.runtime.cursor()
        while not self.server.runtime.stopping.is_set():
            item = self.server.runtime.wait_for_workspace_event(workspace, cursor)
            if item is None:
                continue
            cursor, payload = item
            try:
                self._write({"ok": True, "event": payload})
            except (BrokenPipeError, ConnectionResetError):
                return

    def _write(self, payload: dict[str, Any]) -> None:
        self.wfile.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
        self.wfile.flush()


def run_daemon() -> int:
    home = _ensure_home()
    lock_path = home / "daemon.lock"
    socket_path = daemon_socket_path()
    pid_path = home / "daemon.pid"
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("TeamFlow daemon is already running") from error
        if socket_path.exists():
            socket_path.unlink()
        runtime = TeamFlowDaemon()
        server = DaemonServer(str(socket_path), runtime)
        os.chmod(socket_path, 0o600)
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
        os.chmod(pid_path, 0o600)
        threading.Thread(target=_sync_registered_workspaces, args=(runtime,), name="teamflow-workspace-sync", daemon=True).start()
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            runtime.close()
            server.server_close()
            if socket_path.exists():
                socket_path.unlink()
            if pid_path.exists():
                pid_path.unlink()
    return 0


def ensure_daemon() -> dict[str, Any]:
    status = daemon_status()
    if status["running"]:
        return status
    home = _ensure_home()
    log_path = home / "daemon.log"
    with log_path.open("ab", buffering=0) as log:
        subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "teamflow.py"), "daemon", "run"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=os.environ.copy(),
        )
    os.chmod(log_path, 0o600)
    deadline = time.monotonic() + LISTENER_CONNECT_TIMEOUT
    while time.monotonic() < deadline:
        status = daemon_status()
        if status["running"]:
            return status
        time.sleep(0.1)
    raise ValueError(f"TeamFlow daemon did not start; see {log_path}")


def daemon_status() -> dict[str, Any]:
    try:
        return _daemon_request({"action": "ping"}, timeout=1)
    except (OSError, ValueError, TimeoutError):
        return {"running": False, "pid": None, "apps": [], "workspaces": []}


def stop_daemon() -> dict[str, Any]:
    if not daemon_status()["running"]:
        return {"running": False, "stopping": False}
    return _daemon_request({"action": "shutdown"}, timeout=2)


def sync_daemon_workspace(workspace: str | None, *, identity_id: str | None = None) -> dict[str, Any]:
    ensure_daemon()
    root = register_workspace(workspace)
    return _daemon_request({"action": "sync_workspace", "workspace": root, "identity_id": identity_id}, timeout=30)


def verify_daemon_workspace(workspace: str | None, *, identity_id: str | None = None) -> dict[str, Any]:
    ensure_daemon()
    root = register_workspace(workspace)
    return _daemon_request({"action": "verify_listener", "workspace": root, "identity_id": identity_id}, timeout=90)


def stream_daemon_events(
    workspace: str | None,
    *,
    emit: Callable[[dict[str, Any]], None],
    ready: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    synced = sync_daemon_workspace(workspace)
    request = {"action": "listen", "workspace": synced["workspace_root"]}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(daemon_socket_path()))
        stream = client.makefile("rwb")
        stream.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        stream.flush()
        first = _read_response(stream.readline())
        if ready:
            ready(first["result"]["details"])
        for line in stream:
            response = _read_response(line)
            emit(response["event"])


def _daemon_request(payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(daemon_socket_path()))
        stream = client.makefile("rwb")
        stream.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        stream.flush()
        return _read_response(stream.readline())["result"]


def _read_response(line: bytes) -> dict[str, Any]:
    if not line:
        raise ValueError("TeamFlow daemon closed the connection without a response")
    response = json.loads(line)
    if not response.get("ok"):
        raise ValueError(response.get("error") or "TeamFlow daemon request failed")
    return response


def _sync_registered_workspaces(runtime: TeamFlowDaemon) -> None:
    for workspace in registered_workspaces():
        try:
            runtime.sync_workspace(workspace)
        except Exception as error:
            print(f"teamflow daemon: skipped {workspace}: {error}", flush=True)


def _ensure_home() -> Path:
    home = teamflow_home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    return home
