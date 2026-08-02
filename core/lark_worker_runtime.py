from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

from .lark_events import LarkEventContext


class LarkWorkerRuntime:
    def __init__(
        self,
        *,
        mp: Any,
        event_queue: Any,
        workers: dict[str, dict[str, Any]],
        routes: dict[str, LarkEventContext],
        stopping: threading.Event,
        routes_ready: threading.Event,
        app_key: Callable[[LarkEventContext], str],
        publish: Callable[[str, dict[str, Any]], None],
        process_event: Callable[[str], None],
        stop_worker: Callable[[dict[str, Any]], None],
        resolve: Callable[[str], Any],
        consumer_failure: dict[str, Any],
        on_fatal: Callable[[], None],
    ) -> None:
        self.mp = mp
        self.event_queue = event_queue
        self.workers = workers
        self.routes = routes
        self.stopping = stopping
        self.routes_ready = routes_ready
        self.app_key = app_key
        self.publish = publish
        self.process_event = process_event
        self.stop_worker_facade = stop_worker
        self.resolve = resolve
        self.consumer_failure = consumer_failure
        self.on_fatal = on_fatal
        self.last_cleanup = 0.0

    def ensure_app(self, context: LarkEventContext) -> None:
        app_key = self.app_key(context)
        credentials = (context.app_id, context.app_secret, context.brand)
        worker = self.workers.get(app_key)
        if worker and worker["process"].is_alive() and worker["credentials"] == credentials:
            return
        if worker:
            self.stop_worker_facade(worker)

        ready = self.mp.Event()
        errors = self.mp.Queue()
        process = self.mp.Process(
            target=self.resolve("_lark_app_worker"),
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
        if not ready.wait(self.resolve("LISTENER_CONNECT_TIMEOUT")):
            self.stop_worker_facade(worker)
            self.workers.pop(app_key, None)
            raise ValueError("timed out while connecting to the Lark event stream")
        try:
            worker_error = errors.get_nowait()
        except queue.Empty:
            worker_error = None
        if worker_error or not process.is_alive():
            self.stop_worker_facade(worker)
            self.workers.pop(app_key, None)
            raise ValueError(
                worker_error or "the Lark event stream stopped before synchronization"
            )

    def stop_worker(self, worker: dict[str, Any]) -> None:
        process = worker["process"]
        if process.is_alive():
            process.terminate()
        process.join(timeout=2)
        worker["errors"].close()

    def stop_unused_app(self, context: LarkEventContext) -> None:
        app_key = self.app_key(context)
        if any(self.app_key(route) == app_key for route in self.routes.values()):
            return
        worker = self.workers.pop(app_key, None)
        if worker:
            self.stop_worker_facade(worker)

    def consume_events(self) -> None:
        try:
            self._consume_events()
        except BaseException as error:
            # Per-event business failures are already handled inside the event runtime, so anything
            # reaching here broke the loop itself. Whatever it was, the inbox stops draining, and a
            # daemon that keeps answering as a healthy listener would be lying about that.
            self.consumer_failure["error"] = f"{type(error).__name__}: {error}"
            self.stopping.set()
            self.resolve("emit_log")(
                self.resolve("style")("LISTENER FATAL", "1;31"),
                fields={
                    "type": type(error).__name__,
                    "reason": str(error).splitlines()[0] if str(error) else "",
                },
            )
            self.on_fatal()

    def _consume_events(self) -> None:
        while True:
            try:
                message = self.event_queue.get(timeout=1)
            except queue.Empty:
                message = None
            if message is None:
                if self.stopping.is_set():
                    return
            elif isinstance(message, dict) and isinstance(message.get("payload"), dict):
                self.publish(str(message.get("app_key") or ""), message["payload"])
                if self.routes_ready.is_set() and message.get("event_id"):
                    self.process_event(str(message["event_id"]))
            if not self.routes_ready.is_set():
                continue
            for event_id in self.resolve("due_lark_event_ids")():
                self.process_event(event_id)
            if time.monotonic() - self.last_cleanup >= 86400:
                self.resolve("cleanup_lark_events")()
                self.last_cleanup = time.monotonic()

def lark_app_worker(
    context: LarkEventContext,
    app_key: str,
    events: Any,
    ready: Any,
    errors: Any,
) -> None:
    try:
        def checkpoint(payload: dict[str, Any]) -> None:
            from .global_db import record_lark_event
            from .lark_events import lark_event_metadata

            metadata = lark_event_metadata(payload)
            record_lark_event(
                event_id=str(metadata["event_id"]),
                brand=context.brand,
                app_id=context.app_id,
                event_type=str(metadata["event_type"]),
                file_token=metadata["file_token"],
                table_id=metadata["table_id"],
                source_revision=metadata["source_revision"],
                payload=payload,
            )
            events.put({
                "app_key": app_key,
                "event_id": metadata["event_id"],
                "payload": payload,
            })

        from .lark_events import run_lark_app_worker

        run_lark_app_worker(
            context,
            emit=checkpoint,
            ready=ready.set,
        )
    except Exception as error:
        errors.put(str(error))
        ready.set()
