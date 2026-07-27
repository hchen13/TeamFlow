from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .lark_events import LarkEventContext


class LarkListenerVerifier:
    def __init__(
        self,
        *,
        routes: dict[str, LarkEventContext],
        verifying_workspaces: set[str],
        probe_records: dict[str, float],
        sync_lock: threading.RLock,
        sync_workspace: Callable[..., dict[str, Any]],
        release_workspace: Callable[[str | None], None],
        cursor: Callable[[], int],
        wait_for_records: Callable[
            [LarkEventContext, set[str], int],
            bool,
        ],
        resolve: Callable[[str], Any],
    ) -> None:
        self.routes = routes
        self.verifying_workspaces = verifying_workspaces
        self.probe_records = probe_records
        self.sync_lock = sync_lock
        self.sync_workspace = sync_workspace
        self.release_workspace = release_workspace
        self.cursor = cursor
        self.wait_for_records = wait_for_records
        self.resolve = resolve

    def verify(
        self,
        workspace: str | None,
        *,
        identity_id: str | None = None,
    ) -> dict[str, Any]:
        context = None
        already_subscribed = False
        client = None
        record_id = ""
        cleaned = False
        probe_record_ids: set[str] = set()
        error = None
        try:
            synced = self.sync_workspace(
                workspace,
                identity_id=identity_id,
                reconcile=False,
            )
            context = self.routes[synced["workspace_root"]]
            with self.sync_lock:
                self.verifying_workspaces.add(context.workspace_root)
            already_subscribed = bool(synced["already_subscribed"])
            client = self.resolve("context_client")(context)
            cursor = self.cursor()
            for _ in range(self.resolve("LISTENER_PROBE_ATTEMPTS")):
                cleaned = False
                created = client.upsert_record(context.table_id, {})
                record_id = str(created.get("record_id") or created.get("id") or "")
                if not record_id:
                    raise ValueError(
                        "Lark did not return the listener verification record ID"
                    )
                probe_record_ids.add(record_id)
                with self.sync_lock:
                    self.probe_records[record_id] = time.monotonic() + 300
                client.delete_record(context.table_id, record_id)
                cleaned = True
                record_id = ""
                if self.wait_for_records(context, probe_record_ids, cursor):
                    break
            else:
                raise ValueError(
                    "the app did not receive the Bitable record change event"
                )
        except Exception as caught:
            error = caught
        finally:
            if context:
                with self.sync_lock:
                    self.verifying_workspaces.discard(context.workspace_root)
            if record_id and client and not cleaned:
                try:
                    client.delete_record(context.table_id, record_id)
                except Exception as cleanup_error:
                    error = ValueError(
                        f"listener probe record cleanup failed: {cleanup_error}"
                    )

        timestamp = self.resolve("now")()
        if error:
            result = {
                "ok": False,
                "status": "failed",
                "last_verified_at": timestamp,
                "already_subscribed": already_subscribed,
                **self.resolve("listener_failure")(error, context),
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
        try:
            self.resolve("save_listener_result")(
                workspace,
                context.identity_id if context else identity_id,
                result,
            )
        finally:
            self.release_workspace(workspace)
        return result
