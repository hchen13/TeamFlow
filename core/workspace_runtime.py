from __future__ import annotations

import os
import threading
from dataclasses import replace
from typing import Any, Callable

from .lark_events import LarkEventContext


class WorkspaceSynchronizer:
    def __init__(
        self,
        *,
        routes: dict[str, LarkEventContext],
        sync_lock: threading.RLock,
        app_key: Callable[[LarkEventContext], str],
        ensure_app: Callable[[LarkEventContext], None],
        stop_unused_app: Callable[[LarkEventContext], None],
        reconcile_workspace: Callable[..., dict[str, int]],
        consume_task_events: Callable[..., dict[str, int]],
        log_dispatch: Callable[..., None],
        resolve: Callable[[str], Any],
    ) -> None:
        self.routes = routes
        self.sync_lock = sync_lock
        self.app_key = app_key
        self.ensure_app = ensure_app
        self.stop_unused_app = stop_unused_app
        self.reconcile_workspace = reconcile_workspace
        self.consume_task_events = consume_task_events
        self.log_dispatch = log_dispatch
        self.resolve = resolve

    def sync(
        self,
        workspace: str | None,
        *,
        identity_id: str | None = None,
        reconcile: bool = True,
    ) -> dict[str, Any]:
        with self.sync_lock:
            context = self.resolve("lark_event_context")(
                workspace,
                identity_id=identity_id,
            )
            previous = self.routes.get(context.workspace_root)
            try:
                self.ensure_app(context)
                context = self.resolve("resolve_lark_event_table")(context)
                already_subscribed = self.resolve(
                    "ensure_lark_board_subscription"
                )(context)
                route_changed = previous is None or (
                    self.app_key(previous),
                    previous.file_token,
                    previous.table_id,
                ) != (
                    self.app_key(context),
                    context.file_token,
                    context.table_id,
                )
                if (
                    previous
                    and not route_changed
                    and previous.board_name not in {"", previous.file_token}
                    and previous.table_name not in {"", previous.table_id}
                ):
                    context = replace(
                        context,
                        board_name=previous.board_name,
                        table_name=previous.table_name,
                    )
                elif not context.board_name or not context.table_name:
                    context = self._resolve_names(context)
                reconciliation = (
                    self.reconcile_workspace(context)
                    if reconcile and route_changed
                    else None
                )
            except Exception:
                if previous is None or self.app_key(previous) != self.app_key(context):
                    self.stop_unused_app(context)
                raise
            self.routes[context.workspace_root] = context
            if previous and self.app_key(previous) != self.app_key(context):
                self.stop_unused_app(previous)
            dispatch = self._dispatch(context, previous)
            return {
                "ok": True,
                "already_subscribed": already_subscribed,
                "daemon_pid": os.getpid(),
                "reconciliation": reconciliation,
                "dispatch": dispatch,
                **context.public(),
            }

    def _resolve_names(self, context: LarkEventContext) -> LarkEventContext:
        try:
            details = self.resolve("lark_listener_details")(context)
            return replace(
                context,
                board_name=str(details["board"]["name"]),
                table_name=str(details["board"]["table_name"]),
            )
        except Exception:
            return replace(
                context,
                board_name=context.file_token,
                table_name=context.table_id,
            )

    def _dispatch(
        self,
        context: LarkEventContext,
        previous: LarkEventContext | None,
    ) -> dict[str, Any] | None:
        if not self.resolve("workspace_enabled")(context.workspace_root):
            return None
        try:
            if previous is None:
                self.resolve("recover_task_deliveries")(context)
            return self.consume_task_events(context)
        except Exception as error:
            self.log_dispatch(
                context,
                "failed",
                event_id=None,
                task={},
                reason=str(error),
            )
            return {"error": str(error)}
