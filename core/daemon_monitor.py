from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Any, Callable

from .lark_events import LarkEventContext


class DaemonMonitor:
    def __init__(
        self,
        *,
        routes: dict[str, LarkEventContext],
        workers: dict[str, dict[str, Any]],
        active_sessions: set[str],
        stopping: threading.Event,
        sync_lock: threading.RLock,
        app_key: Callable[[LarkEventContext], str],
        resolve: Callable[[str], Any],
    ) -> None:
        self.routes = routes
        self.workers = workers
        self.active_sessions = active_sessions
        self.stopping = stopping
        self.sync_lock = sync_lock
        self.app_key = app_key
        self.resolve = resolve
        self.recent: deque[tuple[int, str, dict[str, Any]]] = deque(maxlen=1000)
        self.sequence = 0
        self.condition = threading.Condition()

    def cursor(self) -> int:
        with self.condition:
            return self.sequence

    def publish(self, app_key: str, payload: dict[str, Any]) -> None:
        with self.condition:
            self.sequence += 1
            self.recent.append((self.sequence, app_key, payload))
            self.condition.notify_all()

    def wait_for_records(
        self,
        context: LarkEventContext,
        record_ids: set[str],
        cursor: int,
    ) -> bool:
        deadline = time.monotonic() + self.resolve("LISTENER_EVENT_TIMEOUT")
        app_key = self.app_key(context)
        with self.condition:
            while True:
                for sequence, event_app_key, payload in self.recent:
                    if (
                        sequence > cursor
                        and event_app_key == app_key
                        and self.resolve("event_matches_board")(
                            payload,
                            context.public(),
                        )
                        and record_ids.intersection(
                            self.resolve("event_record_ids")(payload)
                        )
                    ):
                        return True
                worker = self.workers.get(app_key)
                if not worker or not worker["process"].is_alive():
                    raise ValueError(
                        "the TeamFlow Lark listener stopped before receiving "
                        "the test event"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(min(remaining, 0.5))

    def wait_for_workspace_event(
        self,
        workspace: str,
        cursor: int,
        timeout: float = 1.0,
    ) -> tuple[int, dict[str, Any]] | None:
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                context = self.routes.get(workspace)
                if context is None:
                    raise ValueError(
                        "workspace is not synchronized with the TeamFlow daemon"
                    )
                app_key = self.app_key(context)
                for sequence, event_app_key, payload in self.recent:
                    if (
                        sequence > cursor
                        and event_app_key == app_key
                        and self.resolve("event_matches_board")(
                            payload,
                            context.public(),
                        )
                    ):
                        return sequence, payload
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.stopping.is_set():
                    return None
                self.condition.wait(remaining)

    def status(self) -> dict[str, Any]:
        with self.sync_lock:
            apps = [
                {
                    "app_id": worker["context"].app_id,
                    "app_name": worker["context"].app_name,
                    "brand": worker["context"].brand,
                    "connected": (
                        worker["process"].is_alive()
                        and worker["ready"].is_set()
                    ),
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
        return {
            "running": True,
            "pid": os.getpid(),
            "apps": apps,
            "workspaces": routes,
            "inbox": self.resolve("lark_event_counts")(),
            "active_sessions": sorted(self.active_sessions),
        }

    def notify_all(self) -> None:
        with self.condition:
            self.condition.notify_all()
