from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .codex_ipc import CodexIpcNoOwner, CodexTurnAcceptanceUnknown
from .codex_permissions import (
    TEAMFLOW_MCP_TOOLS,
    CodexBackgroundMcpPermissionRequired,
)
from .lark_events import LarkEventContext, save_task_snapshot
from .task_dispatch import (
    cancel_reconciled_task_delivery,
    cancel_task_delivery,
    claim_task_deliveries,
    defer_task_delivery_reconciliation,
    due_processing_task_deliveries,
    fail_claimed_task_delivery,
    finish_task_delivery,
    has_task_deliveries_waiting_for_permission,
    mark_task_delivery_waiting_for_permission,
    mark_task_delivery_waiting_for_session,
    mark_task_delivery_turn_started,
    prepare_task_deliveries,
    refresh_task_delivery_prompt,
    render_task_continuation_prompt,
    render_task_prompt,
    resume_task_deliveries_waiting_for_permission,
    resume_task_deliveries_waiting_for_session,
    task_delivery_is_current,
    task_delivery_has_active_execution,
    task_delivery_sessions_waiting_for_owner,
    task_dispatch_target,
)


_UNCONFIRMED_TURN_LEASE = timedelta(minutes=10)
_MAX_ACCEPTED_TURN_ATTEMPTS = 3
_SESSION_OWNER_CHECK_INTERVAL = 2.0


class DeliveryRuntime:
    def __init__(
        self,
        *,
        sync_lock: threading.RLock,
        stopping: threading.Event,
        routes_ready: threading.Event,
        wakeup: threading.Event,
        active_sessions: set[str],
        workers: dict[str, threading.Thread],
        contexts: Callable[[], list[LarkEventContext]],
        reserved_sessions: Callable[[], set[str]],
        get_task: Callable[..., dict[str, Any]],
        run_turn: Callable[..., dict[str, Any]],
        read_thread: Callable[..., dict[str, Any]],
        stop_turn: Callable[..., dict[str, Any]],
        find_turn: Callable[[dict[str, Any], str], dict[str, Any] | None],
        find_turn_by_client_message_id: Callable[
            [dict[str, Any], str],
            dict[str, Any] | None,
        ],
        unresolved_mcp_failures: Callable[[dict[str, Any]], list[dict[str, Any]]],
        delivery_error_is_terminal: Callable[[Exception], bool],
        log_dispatch: Callable[..., None],
        turn_completed: Callable[[str, str], bool] = lambda _session_id, _turn_id: False,
        session_has_owner: Callable[[str], bool] = lambda _session_id: False,
        background_mcp_ready: Callable[[], bool | dict[str, Any]] = lambda: True,
    ) -> None:
        self.sync_lock = sync_lock
        self.stopping = stopping
        self.routes_ready = routes_ready
        self.wakeup = wakeup
        self.active_sessions = active_sessions
        self.workers = workers
        self.contexts = contexts
        self.reserved_sessions = reserved_sessions
        self.get_task = get_task
        self.run_turn = run_turn
        self.read_thread = read_thread
        self.stop_turn = stop_turn
        self.find_turn = find_turn
        self.find_turn_by_client_message_id = find_turn_by_client_message_id
        self.unresolved_mcp_failures = unresolved_mcp_failures
        self.delivery_error_is_terminal = delivery_error_is_terminal
        self.log_dispatch = log_dispatch
        self.turn_completed = turn_completed
        self.session_has_owner = session_has_owner
        self.background_mcp_ready = background_mcp_ready
        self._session_owner_checked_at: dict[tuple[str, str], float] = {}

    def consume(self) -> None:
        while not self.stopping.is_set():
            if not self.routes_ready.wait(0.5):
                continue
            for context in self.contexts():
                if self.stopping.is_set():
                    return
                try:
                    self.resume_permission_waiting(context)
                    self.resume_session_waiting(context)
                    self.reconcile(context)
                    self.schedule(context)
                except Exception as error:
                    self.log_dispatch(
                        context,
                        "failed",
                        event_id=None,
                        task={},
                        reason=str(error),
                    )
            self.wakeup.wait(0.5)
            self.wakeup.clear()

    def resume_permission_waiting(
        self,
        context: LarkEventContext,
    ) -> None:
        if (
            has_task_deliveries_waiting_for_permission(context)
            and self._background_mcp_status()["authorized"]
        ):
            resume_task_deliveries_waiting_for_permission(context)

    def resume_session_waiting(self, context: LarkEventContext) -> None:
        waiting_sessions = task_delivery_sessions_waiting_for_owner(context)
        waiting_keys = {
            (context.db_path, session_id)
            for session_id in waiting_sessions
        }
        self._session_owner_checked_at = {
            key: checked_at
            for key, checked_at in self._session_owner_checked_at.items()
            if key[0] != context.db_path or key in waiting_keys
        }
        timestamp = time.monotonic()
        for session_id in waiting_sessions:
            key = (context.db_path, session_id)
            last_checked_at = self._session_owner_checked_at.get(key)
            if (
                last_checked_at is not None
                and timestamp - last_checked_at < _SESSION_OWNER_CHECK_INTERVAL
            ):
                continue
            self._session_owner_checked_at[key] = timestamp
            try:
                owner_available = self.session_has_owner(session_id)
            except Exception:
                owner_available = False
            if owner_available:
                resume_task_deliveries_waiting_for_session(
                    context,
                    session_id=session_id,
                )
                self._session_owner_checked_at.pop(key, None)

    def schedule(self, context: LarkEventContext) -> None:
        with self.sync_lock:
            reserved_sessions = set(self.active_sessions)
            reserved_sessions.update(self.reserved_sessions())
            deliveries = claim_task_deliveries(
                context,
                exclude_session_ids=reserved_sessions,
            )
            for delivery in deliveries:
                session_id = str(delivery["session_id"])
                self.active_sessions.add(session_id)
                worker = threading.Thread(
                    target=self.execute,
                    args=(context, delivery),
                    name=f"teamflow-delivery-{session_id[:8]}",
                    daemon=True,
                )
                self.workers[session_id] = worker
                worker.start()

    def execute(
        self,
        context: LarkEventContext,
        delivery: dict[str, Any],
    ) -> None:
        session_id = str(delivery["session_id"])
        continuation_turn_id = str(delivery.get("continuation_turn_id") or "")
        turn_started = False
        started_turn_id = None
        canceled = False
        cancellation_reason = None
        task = json.loads(delivery["after_json"] or delivery["before_json"] or "{}")

        def save_turn(turn_id: str) -> None:
            nonlocal started_turn_id, turn_started
            mark_task_delivery_turn_started(
                context,
                delivery_id=int(delivery["id"]),
                turn_id=turn_id,
                previous_turn_id=continuation_turn_id or None,
                require_execution_rebind=bool(continuation_turn_id),
            )
            turn_started = True
            started_turn_id = turn_id
            self.log_dispatch(
                context,
                "started",
                event_id=delivery["source_event_id"],
                task=task,
                record_id=delivery["record_id"],
                target=str(delivery["role_key"]),
                agent=str(delivery["display_name"] or delivery["agent_id"]),
                session=session_id,
                turn=turn_id,
            )

        result = None
        error = None
        retry = False
        acceptance_unknown = False
        waiting_permission = False
        waiting_session = False
        claimed_turn_ended = False
        claimed_turn_reconciling = False
        try:
            if delivery["harness_type"] != "codex":
                raise ValueError(
                    f"unsupported task delivery harness: {delivery['harness_type']}"
                )
            task = self.get_task(
                context.workspace_root,
                record_id=str(delivery["record_id"]),
            )["task"]
            inserted_events = save_task_snapshot(
                context,
                record_id=str(delivery["record_id"]),
                task=task,
                source_event_id=f"teamflow-live-read:{delivery['id']}",
                source_revision=None,
            )
            if inserted_events:
                prepare_task_deliveries(context)
            continuation_is_current = bool(
                continuation_turn_id
                and task_delivery_has_active_execution(
                    context,
                    delivery_id=int(delivery["id"]),
                    turn_id=continuation_turn_id,
                )
            )
            if continuation_turn_id and not continuation_is_current:
                canceled = True
                cancellation_reason = "已认领任务的执行归属已经变化，本次继续执行已取消"
                cancel_reconciled_task_delivery(
                    context,
                    delivery_id=int(delivery["id"]),
                    reason=cancellation_reason,
                )
                return
            if not continuation_turn_id and not task_delivery_is_current(
                context,
                delivery_id=int(delivery["id"]),
            ):
                canceled = True
                cancellation_reason = "卡片已有更新的状态事件，本次旧派发已取消"
                cancel_task_delivery(
                    context,
                    delivery_id=int(delivery["id"]),
                    reason=cancellation_reason,
                )
                return
            current_target = task_dispatch_target(context.workflow_key, task)
            if not continuation_turn_id and current_target != delivery["role_key"]:
                canceled = True
                cancellation_reason = (
                    f"卡片当前不再派发给 {delivery['role_key']}，"
                    f"最新目标为 {current_target or '无'}"
                )
                cancel_task_delivery(
                    context,
                    delivery_id=int(delivery["id"]),
                    reason=cancellation_reason,
                )
                return
            permission = self._background_mcp_status()
            if not permission["authorized"]:
                raise CodexBackgroundMcpPermissionRequired(
                    permission["missing_tools"]
                )
            prompt = (
                render_task_continuation_prompt(
                    context,
                    workflow_key=context.workflow_key,
                    role_name=str(delivery["role_name"] or delivery["role_key"]),
                    task=task,
                )
                if continuation_turn_id
                else render_task_prompt(
                    context,
                    event_type=str(delivery["event_type"]),
                    event_key=str(delivery["event_key"]),
                    workflow_key=context.workflow_key,
                    role_name=str(delivery["role_name"] or delivery["role_key"]),
                    task=task,
                )
            )
            refresh_task_delivery_prompt(
                context,
                delivery_id=int(delivery["id"]),
                prompt=prompt,
            )
            result = self.run_turn(
                session_id,
                prompt,
                client_message_id=str(delivery["client_message_id"]),
                on_started=save_turn,
                stop_event=self.stopping,
            )
            if not isinstance(result, dict):
                raise ValueError("Codex delivery returned an invalid result")
            status = _status_type(result.get("status"))
            active_execution = bool(
                started_turn_id
                and task_delivery_has_active_execution(
                    context,
                    delivery_id=int(delivery["id"]),
                    turn_id=started_turn_id,
                )
            )
            if active_execution and status in {
                "completed",
                "success",
                "failed",
                "cancelled",
                "canceled",
                "interrupted",
            }:
                claimed_turn_reconciling = (
                    status in {"cancelled", "canceled", "interrupted"}
                    and not self.turn_completed(session_id, started_turn_id)
                )
                claimed_turn_ended = not claimed_turn_reconciling
                error = ValueError(
                    str(result.get("error") or status or "execution turn ended before handoff")
                )
            elif not result.get("ok"):
                retry = (
                    status in {"cancelled", "canceled", "interrupted"}
                    and int(delivery["attempts"]) < _MAX_ACCEPTED_TURN_ATTEMPTS
                    and task_delivery_is_current(
                        context,
                        delivery_id=int(delivery["id"]),
                    )
                )
        except Exception as caught:
            error = (
                InterruptedError(
                    "TeamFlow daemon stopped while the Codex turn was running"
                )
                if turn_started and self.stopping.is_set()
                else caught
            )
            waiting_permission = isinstance(
                error,
                CodexBackgroundMcpPermissionRequired,
            )
            waiting_session = isinstance(error, CodexIpcNoOwner)
            acceptance_unknown = isinstance(error, CodexTurnAcceptanceUnknown)
            retry = (
                not waiting_permission
                and not turn_started
                and not acceptance_unknown
                and delivery["harness_type"] == "codex"
                and not self.delivery_error_is_terminal(error)
            )
        finally:
            reconciling = bool(
                claimed_turn_reconciling
                or error and (turn_started or acceptance_unknown) and not claimed_turn_ended
            )
            if canceled:
                pass
            elif waiting_permission:
                mark_task_delivery_waiting_for_permission(
                    context,
                    delivery_id=int(delivery["id"]),
                    error=error,
                    continuation=bool(continuation_turn_id),
                )
            elif waiting_session:
                mark_task_delivery_waiting_for_session(
                    context,
                    delivery_id=int(delivery["id"]),
                    error=error,
                    continuation=bool(continuation_turn_id),
                )
            elif reconciling:
                defer_task_delivery_reconciliation(
                    context,
                    delivery_id=int(delivery["id"]),
                    error=error,
                )
            elif claimed_turn_ended:
                retry = self._finish_claimed_turn(
                    context,
                    delivery,
                    turn_id=str(started_turn_id),
                    turn_status=(
                        _status_type((result or {}).get("status")) or "interrupted"
                    ),
                    reason=str(error),
                )
            else:
                finish_task_delivery(
                    context,
                    delivery_id=int(delivery["id"]),
                    result=result,
                    error=error,
                    retry=retry,
                )
            if canceled:
                log_result = "not-required"
            elif waiting_permission or waiting_session:
                log_result = "waiting"
            elif reconciling:
                log_result = "reconciling"
            elif claimed_turn_ended:
                log_result = "retry" if retry else "failed"
            elif retry:
                log_result = "retry"
            else:
                log_result = "succeeded" if (result or {}).get("ok") else "failed"
            self.log_dispatch(
                context,
                log_result,
                event_id=delivery["source_event_id"],
                task=task,
                record_id=delivery["record_id"],
                target=str(delivery["role_key"]),
                agent=str(delivery["display_name"] or delivery["agent_id"]),
                session=session_id,
                turn=(result or {}).get("turn_id") or started_turn_id,
                transport=(result or {}).get("transport"),
                reason=(
                    cancellation_reason
                    if canceled
                    else str(error) if error else (result or {}).get("error")
                ),
                attempt=(
                    int(delivery["attempts"])
                    if int(delivery["attempts"]) > 1 or log_result != "succeeded"
                    else None
                ),
            )
            with self.sync_lock:
                self.active_sessions.discard(session_id)
                self.workers.pop(session_id, None)
            self.wakeup.set()

    def _finish_claimed_turn(
        self,
        context: LarkEventContext,
        delivery: dict[str, Any],
        *,
        turn_id: str,
        turn_status: str,
        reason: str,
    ) -> bool:
        retry = int(delivery["attempts"]) < _MAX_ACCEPTED_TURN_ATTEMPTS
        if retry:
            finish_task_delivery(
                context,
                delivery_id=int(delivery["id"]),
                result={"ok": False, "status": turn_status},
                error=ValueError(reason),
                retry=True,
            )
        else:
            failed = fail_claimed_task_delivery(
                context,
                delivery_id=int(delivery["id"]),
                turn_id=turn_id,
                turn_status=turn_status,
                reason=reason,
            )
            retry = not failed
        return retry

    def _background_mcp_status(self) -> dict[str, Any]:
        value = self.background_mcp_ready()
        if isinstance(value, dict):
            return {
                "authorized": bool(value.get("authorized")),
                "missing_tools": list(
                    value.get("missing_tools") or TEAMFLOW_MCP_TOOLS
                ),
            }
        return {
            "authorized": bool(value),
            "missing_tools": list(TEAMFLOW_MCP_TOOLS),
        }

    def reconcile(self, context: LarkEventContext) -> None:
        for delivery in due_processing_task_deliveries(context):
            session_id = str(delivery["session_id"])
            task = json.loads(delivery["after_json"] or delivery["before_json"] or "{}")
            target = str(delivery.get("role_key") or task.get("role") or "")
            agent = str(delivery.get("display_name") or delivery["agent_id"])
            with self.sync_lock:
                if session_id in self.active_sessions:
                    continue
            try:
                thread = self.read_thread(session_id, include_turns=True)
                turn_id = str(delivery.get("turn_id") or "")
                client_message_id = str(
                    delivery.get("client_message_id") or ""
                )
                turn = (
                    self.find_turn_by_client_message_id(
                        thread,
                        client_message_id,
                    )
                    if client_message_id
                    else self.find_turn(thread, turn_id)
                )
                if turn is not None:
                    recovered_turn_id = str(turn.get("id") or "")
                    if not recovered_turn_id:
                        turn = None
                    elif recovered_turn_id != turn_id:
                        previous_turn_id = turn_id
                        rebind_execution = bool(
                            previous_turn_id
                            and task_delivery_has_active_execution(
                                context,
                                delivery_id=int(delivery["id"]),
                                turn_id=previous_turn_id,
                            )
                        )
                        turn_id = recovered_turn_id
                        mark_task_delivery_turn_started(
                            context,
                            delivery_id=int(delivery["id"]),
                            turn_id=turn_id,
                            previous_turn_id=(
                                previous_turn_id if rebind_execution else None
                            ),
                            require_execution_rebind=rebind_execution,
                        )
                        delivery["turn_id"] = turn_id
                        self.log_dispatch(
                            context,
                            "started",
                            event_id=delivery["source_event_id"],
                            task=task,
                            record_id=delivery["record_id"],
                            target=target,
                            agent=agent,
                            session=session_id,
                            turn=turn_id,
                            reason="recovered from client message ID",
                        )
            except Exception as error:
                if self.delivery_error_is_terminal(error):
                    turn_id = str(delivery.get("turn_id") or "")
                    active_execution = bool(
                        turn_id
                        and task_delivery_has_active_execution(
                            context,
                            delivery_id=int(delivery["id"]),
                            turn_id=turn_id,
                        )
                    )
                    if active_execution:
                        fail_claimed_task_delivery(
                            context,
                            delivery_id=int(delivery["id"]),
                            turn_id=turn_id,
                            turn_status="unavailable",
                            reason=str(error),
                        )
                    else:
                        finish_task_delivery(
                            context,
                            delivery_id=int(delivery["id"]),
                            error=error,
                        )
                    self.log_dispatch(
                        context,
                        "failed",
                        event_id=delivery["source_event_id"],
                        task=task,
                        record_id=delivery["record_id"],
                        target=target,
                        agent=agent,
                        session=session_id,
                        turn=str(delivery["turn_id"]),
                        reason=str(error),
                    )
                else:
                    self._reconcile_unconfirmed_turn(
                        context,
                        delivery,
                        task=task,
                        target=target,
                        agent=agent,
                        thread_status="",
                        reason=str(error),
                    )
                continue
            thread_status = _status_type(thread.get("status"))
            if turn is None:
                self._reconcile_unconfirmed_turn(
                    context,
                    delivery,
                    task=task,
                    target=target,
                    agent=agent,
                    thread_status=thread_status,
                    reason="Codex turn is not visible",
                )
                continue
            status = _status_type(turn.get("status"))
            active_execution = bool(
                turn_id
                and task_delivery_has_active_execution(
                    context,
                    delivery_id=int(delivery["id"]),
                    turn_id=turn_id,
                )
            )
            if (
                status not in {
                    "completed",
                    "success",
                    "failed",
                    "cancelled",
                    "canceled",
                    "interrupted",
                }
                and not active_execution
                and not task_delivery_is_current(
                    context,
                    delivery_id=int(delivery["id"]),
                )
            ):
                reason = "卡片已有更新的状态事件，正在停止本次旧派发"
                stopped_status = "interrupted"
                try:
                    self.stop_turn(
                        session_id,
                        expected_turn_id=turn_id,
                    )
                except Exception as error:
                    if _turn_stop_error_means_inactive(error):
                        stopped_status = "inactive"
                    else:
                        defer_task_delivery_reconciliation(
                            context,
                            delivery_id=int(delivery["id"]),
                            error=error,
                        )
                        self.log_dispatch(
                            context,
                            "reconciling",
                            event_id=delivery["source_event_id"],
                            task=task,
                            record_id=delivery["record_id"],
                            target=target,
                            agent=agent,
                            session=session_id,
                            turn=turn_id,
                            reason=str(error),
                            attempt=int(delivery["attempts"]),
                        )
                        continue
                cancel_reconciled_task_delivery(
                    context,
                    delivery_id=int(delivery["id"]),
                    reason=reason,
                    turn_status=stopped_status,
                )
                self.log_dispatch(
                    context,
                    "not-required",
                    event_id=delivery["source_event_id"],
                    task=task,
                    record_id=delivery["record_id"],
                    target=target,
                    agent=agent,
                    session=session_id,
                    turn=turn_id,
                    reason=reason,
                )
                continue
            if status in {"completed", "success"}:
                interrupted_tools = self.unresolved_mcp_failures(turn)
                if active_execution:
                    reason = (
                        "TeamFlow execution turn ended before a task handoff"
                        if not interrupted_tools
                        else "TeamFlow execution turn ended with unresolved MCP calls"
                    )
                    retry = self._finish_claimed_turn(
                        context,
                        delivery,
                        turn_id=turn_id,
                        turn_status=status,
                        reason=reason,
                    )
                    self.log_dispatch(
                        context,
                        "retry" if retry else "failed",
                        event_id=delivery["source_event_id"],
                        task=task,
                        record_id=delivery["record_id"],
                        target=target,
                        agent=agent,
                        session=session_id,
                        turn=turn_id,
                        reason=reason,
                        attempt=int(delivery["attempts"]),
                    )
                    continue
                if interrupted_tools:
                    tool_names = ", ".join(
                        sorted({
                            str(invocation["tool"])
                            for invocation in interrupted_tools
                        })
                    )
                    error = ValueError(
                        "TeamFlow MCP calls failed while the daemon was "
                        f"unavailable: {tool_names}"
                    )
                    finish_task_delivery(
                        context,
                        delivery_id=int(delivery["id"]),
                        result={"ok": False, "status": status},
                        error=error,
                        retry=True,
                    )
                    self.log_dispatch(
                        context,
                        "retry",
                        event_id=delivery["source_event_id"],
                        task=task,
                        record_id=delivery["record_id"],
                        target=target,
                        agent=agent,
                        session=session_id,
                        turn=str(delivery["turn_id"]),
                        reason=str(error),
                        attempt=int(delivery["attempts"]),
                    )
                    continue
                finish_task_delivery(
                    context,
                    delivery_id=int(delivery["id"]),
                    result={"ok": True, "status": status},
                )
                self.log_dispatch(
                    context,
                    "recovered",
                    event_id=delivery["source_event_id"],
                    task=task,
                    record_id=delivery["record_id"],
                    target=target,
                    agent=agent,
                    session=session_id,
                    turn=str(delivery["turn_id"]),
                )
            elif status in {"failed", "cancelled", "canceled", "interrupted"}:
                error_data = turn.get("error") or {}
                error = ValueError(str(
                    error_data.get("message")
                    or error_data.get("additionalDetails")
                    or status
                ))
                if active_execution:
                    if (
                        status in {"cancelled", "canceled", "interrupted"}
                        and not self.turn_completed(session_id, turn_id)
                    ):
                        defer_task_delivery_reconciliation(
                            context,
                            delivery_id=int(delivery["id"]),
                            error=error,
                        )
                        self.log_dispatch(
                            context,
                            "reconciling",
                            event_id=delivery["source_event_id"],
                            task=task,
                            record_id=delivery["record_id"],
                            target=target,
                            agent=agent,
                            session=session_id,
                            turn=turn_id,
                            reason=str(error),
                            attempt=int(delivery["attempts"]),
                        )
                        continue
                    retry = self._finish_claimed_turn(
                        context,
                        delivery,
                        turn_id=turn_id,
                        turn_status=status,
                        reason=str(error),
                    )
                else:
                    retry = (
                        status in {"cancelled", "canceled", "interrupted"}
                        and int(delivery["attempts"]) < _MAX_ACCEPTED_TURN_ATTEMPTS
                        and task_delivery_is_current(
                            context,
                            delivery_id=int(delivery["id"]),
                        )
                    )
                    finish_task_delivery(
                        context,
                        delivery_id=int(delivery["id"]),
                        result={"ok": False, "status": status},
                        error=error,
                        retry=retry,
                    )
                self.log_dispatch(
                    context,
                    "retry" if retry else "failed",
                    event_id=delivery["source_event_id"],
                    task=task,
                    record_id=delivery["record_id"],
                    target=target,
                    agent=agent,
                    session=session_id,
                    turn=str(delivery["turn_id"]),
                    reason=str(error),
                    attempt=int(delivery["attempts"]) if retry else None,
                )
            elif thread_status != "active":
                self._reconcile_unconfirmed_turn(
                    context,
                    delivery,
                    task=task,
                    target=target,
                    agent=agent,
                    thread_status=thread_status,
                    reason=(
                        f"Codex turn reports {status or 'unknown'} while "
                        f"the session reports {thread_status or 'unknown'}"
                    ),
                )
            else:
                defer_task_delivery_reconciliation(
                    context,
                    delivery_id=int(delivery["id"]),
                )

    def _reconcile_unconfirmed_turn(
        self,
        context: LarkEventContext,
        delivery: dict[str, Any],
        *,
        task: dict[str, Any],
        target: str,
        agent: str,
        thread_status: str,
        reason: str,
    ) -> None:
        turn_id = str(delivery.get("turn_id") or "")
        active_execution = bool(
            turn_id
            and task_delivery_has_active_execution(
                context,
                delivery_id=int(delivery["id"]),
                turn_id=turn_id,
            )
        )
        if active_execution:
            if self.turn_completed(str(delivery["session_id"]), turn_id):
                retry = self._finish_claimed_turn(
                    context,
                    delivery,
                    turn_id=turn_id,
                    turn_status="completed",
                    reason=f"{reason}; execution turn ended before a task handoff",
                )
                self.log_dispatch(
                    context,
                    "retry" if retry else "failed",
                    event_id=delivery["source_event_id"],
                    task=task,
                    record_id=delivery["record_id"],
                    target=target,
                    agent=agent,
                    session=str(delivery["session_id"]),
                    turn=turn_id,
                    reason=reason,
                    attempt=int(delivery["attempts"]),
                )
            else:
                defer_task_delivery_reconciliation(
                    context,
                    delivery_id=int(delivery["id"]),
                    error=ValueError(reason),
                )
            return
        if not task_delivery_is_current(
            context,
            delivery_id=int(delivery["id"]),
        ):
            stale_reason = f"{reason}; task no longer needs this delivery"
            cancel_reconciled_task_delivery(
                context,
                delivery_id=int(delivery["id"]),
                reason=stale_reason,
            )
            self.log_dispatch(
                context,
                "not-required",
                event_id=delivery["source_event_id"],
                task=task,
                record_id=delivery["record_id"],
                target=target,
                agent=agent,
                session=str(delivery["session_id"]),
                turn=str(delivery["turn_id"]),
                reason=stale_reason,
            )
            return
        if not _reconciliation_lease_expired(delivery):
            defer_task_delivery_reconciliation(
                context,
                delivery_id=int(delivery["id"]),
                error=ValueError(reason),
            )
            return
        error = ValueError(
            f"{reason}; acceptance remained unconfirmed for "
            f"{int(_UNCONFIRMED_TURN_LEASE.total_seconds() // 60)} minutes"
        )
        finish_task_delivery(
            context,
            delivery_id=int(delivery["id"]),
            result={"ok": False, "status": "unconfirmed"},
            error=error,
            retry=True,
            reset_client_message_id=True,
        )
        self.log_dispatch(
            context,
            "retry",
            event_id=delivery["source_event_id"],
            task=task,
            record_id=delivery["record_id"],
            target=target,
            agent=agent,
            session=str(delivery["session_id"]),
            turn=str(delivery["turn_id"]),
            reason=str(error),
            attempt=int(delivery["attempts"]),
        )


def _status_type(value: Any) -> str:
    return str(
        value.get("type")
        if isinstance(value, dict)
        else value or ""
    )


def _reconciliation_lease_expired(delivery: dict[str, Any]) -> bool:
    value = str(delivery.get("started_at") or "")
    if not value:
        return True
    try:
        started_at = datetime.fromisoformat(value)
    except ValueError:
        return True
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - started_at >= _UNCONFIRMED_TURN_LEASE


def _turn_stop_error_means_inactive(error: Exception) -> bool:
    message = str(error).lower()
    return any(fragment in message for fragment in (
        "is not visible in session",
        "session has no active turn",
        "not teamflow task turn",
    ))
