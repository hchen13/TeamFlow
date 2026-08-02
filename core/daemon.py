from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .agent_context_runtime import AgentContextRuntime
from .agent_runtime import (
    confirm_agent_context,
    find_agent_assignment,
    mark_agent_context_recovery_pending,
)
from .codex import (
    codex_delivery_error_is_terminal,
    codex_thread_is_permanently_unavailable,
    codex_turn,
    codex_turn_by_client_message_id,
    codex_turn_unresolved_teamflow_mcp_failures,
    read_codex_thread,
    run_codex_turn,
    stop_codex_turn,
)
from .codex_permissions import (
    TEAMFLOW_MCP_TOOLS,
    inspect_teamflow_mcp_authorization,
)
from .config import resolve_workspace_paths
from .critical_runtime import CriticalComponents
from .daemon_logging import (
    emit_log as _emit_log,
    log_dispatch,
    log_received,
    style as _style,
    styled_task_change as _styled_task_change,
)
from .daemon_monitor import DaemonMonitor
from .daemon_ipc import (
    DaemonServer,
    read_response as _ipc_read_response,
    request as _ipc_request,
)
from .db import now
from .delivery_runtime import DeliveryRuntime
from .event_runtime import EventRuntime
from .global_db import (
    cleanup_lark_events,
    due_lark_event_ids,
    lark_event_counts,
    recover_lark_events,
    register_workspace,
    registered_workspaces,
    teamflow_home,
    workspace_enabled,
)
from .lark_board import get_lark_task, list_lark_tasks
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
    save_listener_result,
)
from .lark_listener_verifier import LarkListenerVerifier
from .lark_worker_runtime import (
    LarkWorkerRuntime,
    lark_app_worker as _lark_app_worker,
)
from .task_dispatch import (
    processing_task_delivery_sessions_for_workspace,
    recover_task_deliveries,
    task_delivery_record_id,
    task_delivery_turn_is_current,
)
from .task_execution import TaskExecutionRuntime
from .teamflow_tool_dispatcher import TeamFlowToolDispatcher
from .tool_runtime import ToolRuntime
from .teamflow_tools import (
    block_task,
    cancel_task,
    claim_task,
    create_task,
    get_task,
    list_available_tasks,
    review_task,
    route_task,
    submit_task,
    update_task,
    workflow_contract,
)
from .workflow import load_workflow_definition
from .workspace_runtime import WorkspaceSynchronizer


ROOT = Path(__file__).resolve().parents[1]


def daemon_socket_path() -> Path:
    return teamflow_home() / "daemon.sock"


class TeamFlowDaemon:
    def __init__(self) -> None:
        self.mp = multiprocessing.get_context("spawn")
        self.event_queue = self.mp.Queue()
        self.workers: dict[str, dict[str, Any]] = {}
        self.routes: dict[str, LarkEventContext] = {}
        self.probe_records: dict[str, float] = {}
        self.verifying_workspaces: set[str] = set()
        self.sync_lock = threading.RLock()
        self.stopping = threading.Event()
        self.routes_ready = threading.Event()
        self.delivery_wakeup = threading.Event()
        self.active_sessions: set[str] = set()
        self.delivery_workers: dict[str, threading.Thread] = {}
        self.consumer_failure: dict[str, Any] = {}
        self.critical = CriticalComponents(
            failure=self.consumer_failure,
            stopping=self.stopping,
            emit_log=_emit_log,
            style=_style,
            on_fatal=lambda: self.on_fatal(),
        )
        self.fatal_lock = threading.Lock()
        self.fatal_handler: Callable[[], None] | None = None
        self.fatal_requested = False
        self.monitor = DaemonMonitor(
            routes=self.routes,
            workers=self.workers,
            active_sessions=self.active_sessions,
            stopping=self.stopping,
            sync_lock=self.sync_lock,
            app_key=self.app_key,
            read_failure=self.critical.snapshot,
            resolve=lambda name: globals()[name],
        )
        self.lark_workers = LarkWorkerRuntime(
            mp=self.mp,
            event_queue=self.event_queue,
            workers=self.workers,
            routes=self.routes,
            stopping=self.stopping,
            routes_ready=self.routes_ready,
            app_key=self.app_key,
            publish=self.publish,
            process_event=self._process_event,
            stop_worker=lambda worker: self._stop_worker(worker),
            resolve=lambda name: globals()[name],
            sync_lock=self.sync_lock,
        )
        self.workspace_synchronizer = WorkspaceSynchronizer(
            routes=self.routes,
            sync_lock=self.sync_lock,
            app_key=self.app_key,
            ensure_app=lambda context: self._ensure_app(context),
            stop_unused_app=lambda context: self._stop_unused_app(context),
            reconcile_workspace=lambda *args, **kwargs: (
                self._reconcile_workspace(*args, **kwargs)
            ),
            consume_task_events=lambda *args, **kwargs: (
                self._consume_workspace_task_events(*args, **kwargs)
            ),
            log_dispatch=lambda *args, **kwargs: self._log_dispatch(
                *args,
                **kwargs,
            ),
            resolve=lambda name: globals()[name],
        )
        self.listener_verifier = LarkListenerVerifier(
            routes=self.routes,
            verifying_workspaces=self.verifying_workspaces,
            probe_records=self.probe_records,
            sync_lock=self.sync_lock,
            sync_workspace=lambda *args, **kwargs: self.sync_workspace(
                *args,
                **kwargs,
            ),
            release_workspace=lambda *args, **kwargs: (
                self.release_ephemeral_workspace(*args, **kwargs)
            ),
            cursor=lambda: self.cursor(),
            wait_for_records=lambda *args, **kwargs: self.wait_for_records(
                *args,
                **kwargs,
            ),
            resolve=lambda name: globals()[name],
        )
        self.task_execution = TaskExecutionRuntime(
            get_task=lambda *args, **kwargs: get_lark_task(*args, **kwargs),
            stop_turn=lambda *args, **kwargs: stop_codex_turn(*args, **kwargs),
            read_thread=lambda *args, **kwargs: read_codex_thread(*args, **kwargs),
            thread_permanently_unavailable=lambda error: (
                codex_thread_is_permanently_unavailable(error)
            ),
            active_sessions=lambda: set(self.active_sessions),
            load_workflow=lambda workflow_key: load_workflow_definition(workflow_key),
        )
        self.agent_context = AgentContextRuntime(
            workspaces=self._assignment_workspaces,
            resolve=lambda name: globals()[name],
            emit_log=_emit_log,
            style=_style,
        )
        self.event_runtime = EventRuntime(
            sync_lock=self.sync_lock,
            routes=self.routes,
            workers=self.workers,
            verifying_workspaces=self.verifying_workspaces,
            probe_records=self.probe_records,
            delivery_wakeup=self.delivery_wakeup,
            get_task=lambda *args, **kwargs: get_lark_task(*args, **kwargs),
            list_tasks=lambda *args, **kwargs: list_lark_tasks(*args, **kwargs),
            log_received=lambda *args, **kwargs: self._log_received(
                *args,
                **kwargs,
            ),
            log_dispatch=lambda *args, **kwargs: self._log_dispatch(
                *args,
                **kwargs,
            ),
        )
        self.tool_runtime = ToolRuntime(
            sync_lock=self.sync_lock,
            assignment_context=lambda **kwargs: self.assignment_context(**kwargs),
            workspace_active=self._workspace_active,
            invoke_tool=lambda *args, **kwargs: self._invoke_teamflow_tool(
                *args,
                **kwargs,
            ),
            sync_task_activity=lambda *args, **kwargs: (
                self._sync_task_execution_activity(*args, **kwargs)
            ),
            delivery_record_id=lambda assignment, **kwargs: (
                task_delivery_record_id(
                    str(assignment["workspace_root"]),
                    agent_id=str(assignment["agent_id"]),
                    **kwargs,
                )
            ),
            delivery_turn_is_current=lambda assignment, **kwargs: (
                self._delivery_turn_is_current(assignment, **kwargs)
            ),
        )
        self.tool_dispatcher = TeamFlowToolDispatcher(
            resolve=lambda name: globals()[name],
            runtime_facts=self._task_runtime_facts,
            stop_execution=self.task_execution.stop_execution,
        )
        self.delivery_runtime = DeliveryRuntime(
            sync_lock=self.sync_lock,
            stopping=self.stopping,
            routes_ready=self.routes_ready,
            wakeup=self.delivery_wakeup,
            active_sessions=self.active_sessions,
            workers=self.delivery_workers,
            contexts=self._delivery_contexts,
            reserved_sessions=self._reserved_delivery_sessions,
            get_task=lambda *args, **kwargs: get_lark_task(*args, **kwargs),
            run_turn=lambda *args, **kwargs: run_codex_turn(
                *args,
                required_mcp_tools=TEAMFLOW_MCP_TOOLS,
                **kwargs,
            ),
            read_thread=lambda *args, **kwargs: read_codex_thread(*args, **kwargs),
            stop_turn=lambda *args, **kwargs: stop_codex_turn(*args, **kwargs),
            find_turn=lambda *args, **kwargs: codex_turn(*args, **kwargs),
            find_turn_by_client_message_id=lambda *args, **kwargs: (
                codex_turn_by_client_message_id(*args, **kwargs)
            ),
            unresolved_mcp_failures=lambda *args, **kwargs: (
                codex_turn_unresolved_teamflow_mcp_failures(*args, **kwargs)
            ),
            delivery_error_is_terminal=lambda error: (
                codex_delivery_error_is_terminal(error)
            ),
            background_mcp_ready=inspect_teamflow_mcp_authorization,
            log_dispatch=lambda *args, **kwargs: self._log_dispatch(
                *args,
                **kwargs,
            ),
        )
        # Both loops are critical: without either one the daemon stops doing the only thing it is
        # for, so both exit through the same fail-fast path.
        self.event_thread = threading.Thread(
            target=self.critical.guard("lark-events", self._consume_events),
            name="teamflow-lark-events",
            daemon=True,
        )
        self.delivery_thread = threading.Thread(
            target=self.critical.guard("deliveries", self._consume_deliveries),
            name="teamflow-deliveries",
            daemon=True,
        )
        self.event_thread.start()
        self.delivery_thread.start()

    # The consumer threads start inside __init__, before the server that knows how to shut the
    # daemon down exists. A fatal consumer in that window records the request instead of calling a
    # placeholder, and installing the handler honours anything already pending.
    def on_fatal(self) -> None:
        with self.fatal_lock:
            self.fatal_requested = True
        self.request_stop()

    def request_stop(self) -> None:
        with self.fatal_lock:
            handler = self.fatal_handler
        if handler:
            handler()

    def set_fatal_shutdown(self, handler: Callable[[], None]) -> None:
        with self.fatal_lock:
            self.fatal_handler = handler
            pending = self.fatal_requested
        if pending:
            handler()

    def begin_shutdown(self) -> None:
        self.stopping.set()

    @staticmethod
    def app_key(context: LarkEventContext) -> str:
        return f"{context.brand}:{context.app_id}"

    def _workspace_active(self, workspace_root: str) -> bool:
        with self.sync_lock:
            return workspace_root in self.routes

    def _delivery_contexts(self) -> list[LarkEventContext]:
        with self.sync_lock:
            return [
                context
                for context in self.routes.values()
                if workspace_enabled(context.workspace_root)
            ]

    def _assignment_workspaces(self) -> list[str]:
        with self.sync_lock:
            return [
                root
                for root in self.routes
                if workspace_enabled(root)
            ]

    def _reserved_delivery_sessions(self) -> set[str]:
        reserved: set[str] = set()
        for root in registered_workspaces(enabled_only=True):
            reserved.update(
                processing_task_delivery_sessions_for_workspace(root)
            )
        return reserved

    def _delivery_turn_is_current(
        self,
        assignment: dict[str, Any],
        *,
        turn_id: str,
    ) -> bool | None:
        workspace_root = str(assignment["workspace_root"])
        with self.sync_lock:
            context = self.routes.get(workspace_root)
        if context is None:
            return None
        return task_delivery_turn_is_current(
            context,
            turn_id=turn_id,
            agent_id=str(assignment["agent_id"]),
        )

    def sync_workspace(
        self,
        workspace: str | None,
        *,
        identity_id: str | None = None,
        reconcile: bool = True,
    ) -> dict[str, Any]:
        return self.workspace_synchronizer.sync(
            workspace,
            identity_id=identity_id,
            reconcile=reconcile,
        )

    def enable_workspace(self, workspace: str | None, *, identity_id: str | None = None) -> dict[str, Any]:
        root = register_workspace(workspace, enabled=True)
        return {"enabled": True, **self.sync_workspace(root, identity_id=identity_id)}

    def disable_workspace(self, workspace: str | None) -> dict[str, Any]:
        root = register_workspace(workspace, enabled=False)
        task_lock = self.tool_runtime.workspace_lock(root)
        with task_lock:
            with self.sync_lock:
                context = self.routes.pop(root, None)
                if context:
                    self._stop_unused_app(context)
        return {"ok": True, "enabled": False, "workspace_root": root, "daemon_pid": os.getpid()}

    def finish_startup(self) -> None:
        _emit_log("DAEMON LISTENING", fields={"apps": len(self.workers), "workspaces": len(self.routes)})
        self.routes_ready.set()
        self.delivery_wakeup.set()

    def release_ephemeral_workspace(self, workspace: str | None) -> None:
        root = str(resolve_workspace_paths(workspace).root)
        if workspace_enabled(root):
            return
        with self.sync_lock:
            context = self.routes.pop(root, None)
            if context:
                self._stop_unused_app(context)

    def verify_workspace(self, workspace: str | None, *, identity_id: str | None = None) -> dict[str, Any]:
        return self.listener_verifier.verify(
            workspace,
            identity_id=identity_id,
        )

    def listener_details(self, workspace: str | None) -> dict[str, Any]:
        root = str(resolve_workspace_paths(workspace).root)
        context = self.routes.get(root)
        if context is None:
            self.sync_workspace(root, reconcile=False)
            context = self.routes[root]
        return lark_listener_details(context)

    def cursor(self) -> int:
        return self.monitor.cursor()

    def wait_for_records(self, context: LarkEventContext, record_ids: set[str], cursor: int) -> bool:
        return self.monitor.wait_for_records(context, record_ids, cursor)

    def wait_for_workspace_event(self, workspace: str, cursor: int, timeout: float = 1.0) -> tuple[int, dict[str, Any]] | None:
        return self.monitor.wait_for_workspace_event(
            workspace,
            cursor,
            timeout,
        )

    def publish(self, app_key: str, payload: dict[str, Any]) -> None:
        self.monitor.publish(app_key, payload)

    def status(self) -> dict[str, Any]:
        return self.monitor.status()

    def assignment_context(
        self,
        *,
        session_id: str,
        cwd: str | None,
        consume: bool,
        refresh: bool = False,
    ) -> dict[str, Any]:
        return self.agent_context.assignment(
            session_id=session_id,
            cwd=cwd,
            consume=consume,
            refresh=refresh,
        )

    def confirm_assignment_context(
        self,
        *,
        workspace: str,
        agent_id: str,
        session_id: str,
        assignment_revision: int,
        context_fingerprint: str,
        context_kind: str | None,
    ) -> dict[str, Any]:
        return self.agent_context.confirm(
            workspace=workspace,
            agent_id=agent_id,
            session_id=session_id,
            assignment_revision=assignment_revision,
            context_fingerprint=context_fingerprint,
            context_kind=context_kind,
        )

    def compact_assignment_context(
        self,
        *,
        session_id: str,
        cwd: str | None,
    ) -> dict[str, Any]:
        return self.agent_context.mark_compacted(
            session_id=session_id,
            cwd=cwd,
        )

    def authorize_tool(
        self,
        *,
        invocation_id: str,
        session_id: str,
        cwd: str | None,
        turn_id: str | None,
        tool_name: str,
        tool_input: Any,
    ) -> dict[str, Any]:
        return self.tool_runtime.authorize(
            invocation_id=invocation_id,
            session_id=session_id,
            cwd=cwd,
            turn_id=turn_id,
            tool_name=tool_name,
            tool_input=tool_input,
        )

    def invoke_tool(
        self,
        *,
        invocation_id: str,
        grant: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        return self.tool_runtime.invoke(
            invocation_id=invocation_id,
            grant=grant,
            tool_name=tool_name,
            arguments=arguments,
        )

    def _cache_tool_result(
        self,
        invocation_id: str,
        *,
        authorized: dict[str, Any],
        tool_name: str,
        result: dict[str, Any],
    ) -> None:
        self.tool_runtime.cache_result(
            invocation_id,
            authorized=authorized,
            tool_name=tool_name,
            result=result,
        )

    def _sync_task_execution_activity(
        self,
        assignment: dict[str, Any],
        *,
        tool_name: str,
        result: dict[str, Any],
        session_id: str,
        turn_id: str | None,
    ) -> None:
        self.task_execution.sync_activity(
            assignment,
            tool_name=tool_name,
            result=result,
            session_id=session_id,
            turn_id=turn_id,
        )

    def _invoke_teamflow_tool(
        self,
        assignment: dict[str, Any],
        tool_name: str,
        arguments: dict[str, Any],
        *,
        invocation_id: str,
    ) -> dict[str, Any]:
        return self.tool_dispatcher.invoke(
            assignment,
            tool_name,
            arguments,
            invocation_id=invocation_id,
        )

    def _task_runtime_facts(
        self,
        assignment: dict[str, Any],
        task: dict[str, Any],
    ) -> set[str]:
        return self.task_execution.runtime_facts(assignment, task)

    def close(self) -> None:
        self.stopping.set()
        self.delivery_wakeup.set()
        self.monitor.notify_all()
        with self.sync_lock:
            for worker in self.workers.values():
                self._stop_worker(worker)
            self.workers.clear()
        self.event_queue.put(None)
        self.event_thread.join(timeout=2)
        self.delivery_thread.join(timeout=2)
        for worker in list(self.delivery_workers.values()):
            worker.join(timeout=2)
        self.event_queue.close()

    def _ensure_app(self, context: LarkEventContext) -> None:
        self.lark_workers.ensure_app(context)

    def _stop_worker(self, worker: dict[str, Any]) -> None:
        self.lark_workers.stop_worker(worker)

    def _stop_unused_app(self, context: LarkEventContext) -> None:
        app_key = self.app_key(context)
        if any(self.app_key(route) == app_key for route in self.routes.values()):
            return
        worker = self.workers.pop(app_key, None)
        if worker:
            self._stop_worker(worker)

    def _consume_events(self) -> None:
        self.lark_workers.consume_events()

    def _consume_deliveries(self) -> None:
        self.delivery_runtime.consume()

    def _schedule_task_deliveries(self, context: LarkEventContext) -> None:
        self.delivery_runtime.schedule(context)

    def _execute_task_delivery(self, context: LarkEventContext, delivery: dict[str, Any]) -> None:
        self.delivery_runtime.execute(context, delivery)

    def _reconcile_task_deliveries(self, context: LarkEventContext) -> None:
        self.delivery_runtime.reconcile(context)

    def _process_event(self, event_id: str) -> None:
        self.event_runtime.process_event(event_id)

    def _log_received(
        self,
        context: LarkEventContext,
        item: dict[str, Any],
        summary: dict[str, Any],
    ) -> None:
        log_received(context, item, summary)

    def _log_dispatch(
        self,
        context: LarkEventContext,
        result: str,
        *,
        event_id: str | None,
        task: dict[str, Any],
        record_id: str | None = None,
        target: str | None = None,
        agent: str | None = None,
        session: str | None = None,
        reason: str | None = None,
        attempt: int | None = None,
        turn: str | None = None,
        transport: str | None = None,
    ) -> None:
        log_dispatch(
            context,
            result,
            event_id=event_id,
            task=task,
            record_id=record_id,
            target=target,
            agent=agent,
            session=session,
            reason=reason,
            attempt=attempt,
            turn=turn,
            transport=transport,
        )

    def _process_workspace_event(
        self,
        context: LarkEventContext,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return self.event_runtime.process_workspace_event(context, payload)

    def _reconcile_workspace(
        self,
        context: LarkEventContext,
        *,
        source_event_id: str | None = None,
        source_revision: str | None = None,
    ) -> dict[str, int]:
        return self.event_runtime.reconcile_workspace(
            context,
            source_event_id=source_event_id,
            source_revision=source_revision,
        )

    def _consume_workspace_task_events(
        self,
        context: LarkEventContext,
        *,
        trigger_event_id: str | None = None,
    ) -> dict[str, int]:
        return self.event_runtime.consume_workspace_task_events(
            context,
            trigger_event_id=trigger_event_id,
        )

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
        recover_lark_events()
        cleanup_lark_events()
        if socket_path.exists():
            socket_path.unlink()
        runtime = TeamFlowDaemon()
        server = DaemonServer(str(socket_path), runtime)
        # The thread that performs the shutdown exists before anything can need it. Creating one
        # while handling a failure would make the exit depend on the runtime still being able to
        # start threads, which is exactly what cannot be assumed at that moment.
        stop_requested = threading.Event()
        threading.Thread(
            target=_await_daemon_stop(stop_requested, server, runtime),
            name="teamflow-daemon-stop",
            daemon=True,
        ).start()
        runtime.set_fatal_shutdown(stop_requested.set)
        os.chmod(socket_path, 0o600)
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
        os.chmod(pid_path, 0o600)
        _emit_log("DAEMON RUNNING", fields={"pid": os.getpid(), "socket": socket_path})
        threading.Thread(target=_sync_registered_workspaces, args=(runtime,), name="teamflow-workspace-sync", daemon=True).start()
        interrupted = False
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            interrupted = True
            _emit_log("DAEMON STOPPING")
        finally:
            # A close that fails must not keep the socket, the pid file, or the lifecycle lock:
            # whatever is left behind would make the next daemon unstartable.
            stop_requested.set()
            try:
                runtime.close()
            finally:
                _release_daemon_lifecycle(server, socket_path, pid_path)
    return 130 if interrupted else 0


def _await_daemon_stop(
    requested: threading.Event,
    server: DaemonServer,
    runtime: TeamFlowDaemon,
) -> Callable[[], None]:
    def wait() -> None:
        requested.wait()
        runtime.begin_shutdown()
        # Harmless once serve_forever has already returned, which is how normal teardown releases
        # this thread.
        server.shutdown()
    return wait


def _release_daemon_lifecycle(server: DaemonServer, socket_path: Path, pid_path: Path) -> None:
    try:
        server.server_close()
    finally:
        for path in (socket_path, pid_path):
            if path.exists():
                path.unlink()


def await_daemon_lifecycle_lock(lock_path: Path, deadline: float) -> dict[str, Any] | None:
    # A daemon holds this lock for its whole life, so acquiring it is the only proof that the
    # previous process is gone. Its socket disappears earlier than that, which is why waiting on
    # the socket would start a replacement that then loses the lock and exits.
    while True:
        with lock_path.open("a+", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)
                return None
        # Someone else holds the lock. If they are already serving a healthy daemon, that is the
        # daemon this call wanted.
        status = daemon_status()
        if status["running"] and status.get("healthy", True):
            return status
        if time.monotonic() >= deadline:
            raise ValueError("TeamFlow daemon is still shutting down; try again")
        time.sleep(0.05)


def ensure_daemon() -> dict[str, Any]:
    status = daemon_status()
    if status["running"] and status.get("healthy", True):
        return status
    home = _ensure_home()
    if status["running"] and not status.get("stopping"):
        # An unhealthy daemon cannot recover on its own, so it is replaced rather than reused. One
        # that is already leaving needs no second request; waiting for its lock is enough.
        stop_daemon()
    running = await_daemon_lifecycle_lock(
        home / "daemon.lock",
        time.monotonic() + LISTENER_CONNECT_TIMEOUT,
    )
    if running:
        return running
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
        if status["running"] and status.get("healthy", True):
            return status
        time.sleep(0.1)
    raise ValueError(f"TeamFlow daemon did not start; see {log_path}")


def daemon_status() -> dict[str, Any]:
    try:
        return _daemon_request({"action": "ping"}, timeout=1)
    except (OSError, ValueError, TimeoutError):
        return {
            "running": False,
            "healthy": False,
            "stopping": False,
            "failed_component": None,
            "consumer_error": None,
            "pid": None,
            "apps": [],
            "workspaces": [],
            # Reporting an offline daemon must not depend on the database it could not reach.
            "inbox": {},
        }


def stop_daemon() -> dict[str, Any]:
    status = daemon_status()
    if not status["running"] or status.get("stopping"):
        # Already gone, or already leaving: asking again would only race its own socket.
        return status if status["running"] else {"running": False, "stopping": False}
    try:
        return _daemon_request({"action": "shutdown"}, timeout=2)
    except (ConnectionRefusedError, FileNotFoundError):
        # The daemon closed its socket between the status read and this request. That is the
        # outcome this call wanted, not a failure the caller has to interpret.
        return daemon_status()


def sync_daemon_workspace(workspace: str | None, *, identity_id: str | None = None) -> dict[str, Any]:
    ensure_daemon()
    root = register_workspace(workspace)
    return _daemon_request({"action": "sync_workspace", "workspace": root, "identity_id": identity_id}, timeout=30)


def enable_daemon_workspace(workspace: str | None, *, identity_id: str | None = None) -> dict[str, Any]:
    root = register_workspace(workspace, enabled=True)
    try:
        ensure_daemon()
        return _daemon_request(
            {"action": "enable_workspace", "workspace": root, "identity_id": identity_id},
            timeout=60,
        )
    except Exception:
        register_workspace(root, enabled=False)
        raise


def disable_daemon_workspace(workspace: str | None) -> dict[str, Any]:
    root = register_workspace(workspace, enabled=False)
    if not daemon_status()["running"]:
        return {"ok": True, "running": False, "enabled": False, "workspace_root": root}
    return _daemon_request({"action": "disable_workspace", "workspace": root}, timeout=10)


def verify_daemon_workspace(workspace: str | None, *, identity_id: str | None = None) -> dict[str, Any]:
    was_running = daemon_status()["running"]
    ensure_daemon()
    root = register_workspace(workspace)
    try:
        return _daemon_request(
            {"action": "verify_listener", "workspace": root, "identity_id": identity_id},
            timeout=90,
        )
    finally:
        if not was_running and not registered_workspaces(enabled_only=True):
            stop_daemon()


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
    return _ipc_request(
        daemon_socket_path(),
        payload,
        timeout=timeout,
    )


def _read_response(line: bytes) -> dict[str, Any]:
    return _ipc_read_response(line)


def _sync_registered_workspaces(runtime: TeamFlowDaemon) -> None:
    try:
        for workspace in registered_workspaces(enabled_only=True):
            try:
                result = runtime.sync_workspace(workspace)
                _emit_log(
                    "WORKSPACE ENABLED",
                    context=runtime.routes.get(str(result["workspace_root"])),
                    fields={"board": result.get("board_name"), "table": result.get("table_name")},
                )
            except Exception as error:
                _emit_log(
                    _style("WORKSPACE SKIPPED", "1;31"),
                    fields={"workspace": workspace, "reason": str(error)},
                )
    finally:
        runtime.finish_startup()


def _ensure_home() -> Path:
    home = teamflow_home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    return home
