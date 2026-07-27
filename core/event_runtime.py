from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .daemon_logging import emit_log, event_source, style, task_change
from .global_db import (
    claim_lark_event,
    finish_lark_event,
    retry_lark_event,
    workspace_enabled,
)
from .lark_events import (
    LarkEventContext,
    context_client,
    event_matches_board,
    event_record_actions,
    event_record_ids,
    lark_event_metadata,
    save_board_schema_event,
    save_task_snapshot,
    saved_task_record_ids,
    saved_task_snapshot,
)
from .task_dispatch import (
    prepare_agent_catchup_deliveries,
    prepare_task_deliveries,
)


class EventRuntime:
    def __init__(
        self,
        *,
        sync_lock: threading.RLock,
        routes: dict[str, LarkEventContext],
        workers: dict[str, dict[str, Any]],
        verifying_workspaces: set[str],
        probe_records: dict[str, float],
        delivery_wakeup: threading.Event,
        get_task: Callable[..., dict[str, Any]],
        list_tasks: Callable[..., dict[str, Any]],
        log_received: Callable[..., None],
        log_dispatch: Callable[..., None],
    ) -> None:
        self.sync_lock = sync_lock
        self.routes = routes
        self.workers = workers
        self.verifying_workspaces = verifying_workspaces
        self.probe_records = probe_records
        self.delivery_wakeup = delivery_wakeup
        self.get_task = get_task
        self.list_tasks = list_tasks
        self.log_received = log_received
        self.log_dispatch = log_dispatch

    def process_event(self, event_id: str) -> None:
        item = claim_lark_event(event_id)
        if item is None:
            return
        routes = []
        summaries: dict[str, list[dict[str, Any]]] = {}
        try:
            app_key = f"{item['brand']}:{item['app_id']}"
            with self.sync_lock:
                routes = [
                    context
                    for context in self.routes.values()
                    if self._app_key(context) == app_key
                    and event_matches_board(item["payload"], context.public())
                ]
                verifying = any(
                    context.workspace_root in self.verifying_workspaces
                    for context in routes
                )
            if not routes:
                finish_lark_event(event_id, status="ignored")
                worker = self.workers.get(app_key)
                app_name = (
                    worker["context"].app_name
                    if worker
                    else item["app_id"]
                )
                emit_log(
                    style(
                        f"{event_source(str(item['brand']), str(item['event_type']))} "
                        "UNROUTED",
                        "1;31",
                    ),
                    timestamp=str(item["received_at"]),
                    fields={
                        "app": app_name,
                        "app_id": item["app_id"],
                        "file": item["file_token"],
                        "table": item["table_id"],
                        "event": event_id,
                        "reason": "未匹配到 workspace",
                    },
                )
                return
            if verifying:
                raise ValueError(
                    "listener verification is still cleaning up its probe record"
                )
            for context in routes:
                summaries[context.workspace_root] = self.process_workspace_event(
                    context,
                    item["payload"],
                )
                for summary in summaries[context.workspace_root]:
                    self.log_received(context, item, summary)
            finish_lark_event(event_id)
        except Exception as error:
            status = retry_lark_event(event_id, error)
            context = routes[0] if len(routes) == 1 else None
            fields = {
                "event": event_id,
                "attempt": item["attempts"],
                "reason": str(error),
            }
            if context is None:
                fields = {
                    "app_id": item["app_id"],
                    "file": item["file_token"],
                    "table": item["table_id"],
                    **fields,
                }
            emit_log(
                style(
                    f"{event_source(str(item['brand']), str(item['event_type']))} "
                    f"{status.upper()}",
                    "1;31" if status == "failed" else "1;33",
                ),
                context=context,
                fields=fields,
            )
            return
        for context in routes:
            if not workspace_enabled(context.workspace_root):
                entries = summaries.get(context.workspace_root) or [{}]
                for summary in entries:
                    self.log_dispatch(
                        context,
                        "not-required",
                        event_id=event_id,
                        task=summary.get("task") or {},
                        record_id=summary.get("record_id"),
                        reason="workspace 已停用",
                    )
                continue
            try:
                self.consume_workspace_task_events(
                    context,
                    trigger_event_id=event_id,
                )
            except Exception as error:
                self.log_dispatch(
                    context,
                    "failed",
                    event_id=event_id,
                    task={},
                    reason=str(error),
                )

    def process_workspace_event(
        self,
        context: LarkEventContext,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        metadata = lark_event_metadata(payload)
        if metadata["event_type"] == "drive.file.bitable_field_changed_v1":
            created = save_board_schema_event(
                context,
                source_event_id=str(metadata["event_id"]),
                source_revision=metadata["source_revision"],
            )
            self.reconcile_workspace(
                context,
                source_event_id=str(metadata["event_id"]),
                source_revision=metadata["source_revision"],
            )
            return [{
                "record_id": None,
                "task": None,
                "change": None,
                "event_types": ["board_schema_changed"] if created else [],
            }]

        actions = event_record_actions(payload)
        summaries = []
        current_time = time.monotonic()
        with self.sync_lock:
            expired = [
                record_id
                for record_id, expires_at in self.probe_records.items()
                if expires_at <= current_time
            ]
            for record_id in expired:
                self.probe_records.pop(record_id, None)
            probe_records = set(self.probe_records)
        for record_id in sorted(event_record_ids(payload)):
            if record_id in probe_records:
                continue
            action = actions.get(record_id, "").lower()
            deleted = "deleted" in action
            task = (
                saved_task_snapshot(context, record_id)
                if deleted
                else self.get_task(
                    context.workspace_root,
                    record_id=record_id,
                )["task"]
            )
            event_types = save_task_snapshot(
                context,
                record_id=record_id,
                task=None if deleted else task,
                source_event_id=str(metadata["event_id"]),
                source_revision=metadata["source_revision"],
            )
            summaries.append({
                "record_id": record_id,
                "task": task,
                "change": task_change(event_types),
                "event_types": event_types,
            })
        return summaries

    def reconcile_workspace(
        self,
        context: LarkEventContext,
        *,
        source_event_id: str | None = None,
        source_revision: str | None = None,
    ) -> dict[str, int]:
        if source_revision is None:
            table = next(
                (
                    item
                    for item in context_client(context).list_tables()
                    if item["table_id"] == context.table_id
                ),
                None,
            )
            revision = (table or {}).get("revision")
            source_revision = (
                str(revision)
                if revision is not None
                else None
            )

        tasks = []
        offset = 0
        while True:
            page = self.list_tasks(
                context.workspace_root,
                limit=200,
                offset=offset,
            )
            batch = page["tasks"]
            tasks.extend(batch)
            if not page["has_more"]:
                break
            if not batch:
                raise ValueError(
                    "Lark returned an empty task page with has_more=true"
                )
            offset += len(batch)

        current_ids = {
            str(task["record_id"])
            for task in tasks
            if task.get("record_id")
        }
        created_events = 0
        for task in tasks:
            record_id = str(task.get("record_id") or "")
            if not record_id:
                continue
            created_events += len(save_task_snapshot(
                context,
                record_id=record_id,
                task=task,
                source_event_id=source_event_id,
                source_revision=source_revision,
            ))
        deleted = saved_task_record_ids(context) - current_ids
        for record_id in deleted:
            created_events += len(save_task_snapshot(
                context,
                record_id=record_id,
                task=None,
                source_event_id=source_event_id,
                source_revision=source_revision,
            ))
        return {
            "tasks": len(tasks),
            "removed": len(deleted),
            "events": created_events,
        }

    def consume_workspace_task_events(
        self,
        context: LarkEventContext,
        *,
        trigger_event_id: str | None = None,
    ) -> dict[str, int]:
        result = prepare_task_deliveries(context)
        outcomes = result.pop("outcomes")
        grouped: dict[str, list[dict[str, Any]]] = {}
        if trigger_event_id:
            for outcome in outcomes:
                if outcome["source_event_id"] == trigger_event_id:
                    key = str(outcome["record_id"] or "board")
                    grouped.setdefault(key, []).append(outcome)
        for entries in grouped.values():
            selected = next(
                (item for item in entries if item["result"] == "waiting"),
                None,
            )
            if selected:
                target = str(selected["target"])
                reason = (
                    f"未注册 {target.upper()} Agent"
                    if context.brand == "feishu"
                    else f"no {target} agent is registered"
                )
                self.log_dispatch(
                    context,
                    "waiting",
                    event_id=trigger_event_id,
                    task=selected["task"],
                    record_id=selected["record_id"],
                    target=target,
                    reason=reason,
                )
            elif not any(item["result"] == "routed" for item in entries):
                selected = entries[0]
                reason = (
                    "当前变更不通知 Agent"
                    if context.brand == "feishu"
                    else "this change does not notify an agent"
                )
                self.log_dispatch(
                    context,
                    "not-required",
                    event_id=trigger_event_id,
                    task=selected["task"],
                    record_id=selected["record_id"],
                    reason=reason,
                )

        catchup = prepare_agent_catchup_deliveries(context)
        self.delivery_wakeup.set()
        return {
            **result,
            "catchup_deliveries": catchup,
        }

    @staticmethod
    def _app_key(context: LarkEventContext) -> str:
        return f"{context.brand}:{context.app_id}"
