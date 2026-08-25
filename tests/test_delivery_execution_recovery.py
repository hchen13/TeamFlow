from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.agent_runtime import (
    agent_context,
    confirm_agent_context,
    mark_agent_context_recovery_pending,
)
from core.codex_ipc import CodexIpcNoOwner, CodexTurnAcceptanceUnknown
from core.codex_rollout import codex_turn_completed
from core.config import resolve_workspace_paths
from core.db import (
    configure_lark_board,
    configure_lark_identity,
    connect,
    init_workspace,
    now,
)
from core.delivery_runtime import DeliveryRuntime
from core.global_db import register_workspace
from core.lark_events import LarkEventContext, save_task_snapshot
from core.task_dispatch import (
    claim_task_deliveries,
    fail_claimed_task_delivery,
    finish_task_delivery,
    mark_task_delivery_queueing,
    mark_task_delivery_queued,
    mark_task_delivery_waiting_for_session,
    mark_task_delivery_turn_started,
    prepare_task_deliveries,
    recover_retryable_failed_task_deliveries,
    task_delivery_record_id,
    task_delivery_turn_count,
    task_delivery_turn_is_current,
)
from core.tool_runtime import ToolRuntime


ROOT = Path(__file__).resolve().parents[1]


class ClaimedExecutionRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.home = tempfile.TemporaryDirectory(
            prefix="execution-recovery-home-",
            dir=ROOT / "tmp",
        )
        self.home_env = patch.dict(os.environ, {"TEAMFLOW_HOME": self.home.name})
        self.home_env.start()
        self.temp = tempfile.TemporaryDirectory(
            prefix="execution-recovery-",
            dir=ROOT / "tmp",
        )
        self.workspace = self.temp.name
        init_workspace(self.workspace)
        with patch(
            "core.db.fetch_lark_app_info",
            return_value=("Test app", None, None),
        ):
            identity = configure_lark_identity(
                self.workspace,
                app_id="cli_test",
                app_secret="secret",
                domain="feishu",
            )
        configure_lark_board(
            self.workspace,
            board_url="https://example.feishu.cn/base/bascnTest?table=tblTest",
        )
        register_workspace(self.workspace, enabled=True)
        self.identity_id = identity["lark_identity_id"]
        self.db_path = resolve_workspace_paths(self.workspace).db_path
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE lark_boards SET primary_identity_id = ?",
                (self.identity_id,),
            )
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'tl'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES (
                  'agent_recovery', ?, ?, ?, 'tl', 'codex',
                  'session_recovery', 'Recovery TL', ?, ?
                )
                """,
                (
                    workspace["id"],
                    workspace["current_workflow_id"],
                    role["id"],
                    now(),
                    now(),
                ),
            )

    def tearDown(self) -> None:
        self.temp.cleanup()
        self.home_env.stop()
        self.home.cleanup()

    def context(self) -> LarkEventContext:
        return LarkEventContext(
            workspace_root=self.workspace,
            db_path=str(self.db_path),
            identity_id=self.identity_id,
            identity_name="Test app",
            app_id="cli_test",
            app_name="Test app",
            app_secret="secret",
            auth_mode="bot",
            user_open_id="",
            board_url="https://example.feishu.cn/base/bascnTest?table=tblTest",
            file_token="bascnTest",
            table_id="tblTest",
            brand="feishu",
            workspace_name="test-workspace",
            workflow_key="software-development",
            board_name="Project board",
            table_name="Tasks",
        )

    def ready_task(self) -> dict[str, str]:
        return {
            "record_id": "recRecovery",
            "task_id": "TF-RECOVERY",
            "title": "Continue claimed work",
            "status": "ready",
            "role": "tl",
        }

    def start_delivery(self, *, turn_id: str = "turn_original") -> dict:
        context = self.context()
        save_task_snapshot(
            context,
            record_id="recRecovery",
            task=self.ready_task(),
            source_event_id="evtRecoveryReady",
            source_revision="1",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        mark_task_delivery_turn_started(
            context,
            delivery_id=int(delivery["id"]),
            turn_id=turn_id,
        )
        delivery["turn_id"] = turn_id
        return delivery

    def claim_execution(self, *, turn_id: str = "turn_original") -> None:
        context = self.context()
        claimed = {
            **self.ready_task(),
            "status": "in_progress",
            "agent": "Recovery TL",
            "agent_id": "agent_recovery",
        }
        save_task_snapshot(
            context,
            record_id="recRecovery",
            task=claimed,
            source_event_id="evtRecoveryClaimed",
            source_revision="2",
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO task_executions (
                  record_id, agent_id, session_id, turn_id, state, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?)
                ON CONFLICT(record_id) DO UPDATE SET
                  agent_id = excluded.agent_id,
                  session_id = excluded.session_id,
                  turn_id = excluded.turn_id,
                  state = 'active',
                  updated_at = excluded.updated_at
                """,
                (
                    "recRecovery",
                    "agent_recovery",
                    "session_recovery",
                    turn_id,
                    now(),
                ),
            )

    def advance_to_review(self) -> None:
        save_task_snapshot(
            self.context(),
            record_id="recRecovery",
            task={
                **self.ready_task(),
                "status": "review",
                "agent": "Recovery TL",
                "agent_id": "agent_recovery",
            },
            source_event_id="evtRecoveryReview",
            source_revision="3",
        )

    def runtime(self, **overrides) -> DeliveryRuntime:
        values = {
            "sync_lock": threading.RLock(),
            "stopping": threading.Event(),
            "routes_ready": threading.Event(),
            "wakeup": threading.Event(),
            "active_sessions": set(),
            "workers": {},
            "contexts": lambda: [self.context()],
            "reserved_sessions": lambda: set(),
            "get_task": lambda *_args, **_kwargs: {"task": self.ready_task()},
            "run_turn": lambda *_args, **_kwargs: {},
            "read_thread": lambda *_args, **_kwargs: {},
            "stop_turn": lambda *_args, **_kwargs: {},
            "find_turn": lambda thread, turn_id: next(
                (
                    turn
                    for turn in thread.get("turns", [])
                    if turn.get("id") == turn_id
                ),
                None,
            ),
            "find_turn_by_client_message_id": lambda thread, client_id: next(
                (
                    turn
                    for turn in reversed(thread.get("turns", []))
                    if any(
                        item.get("type") == "userMessage"
                        and item.get("clientId") == client_id
                        for item in turn.get("items", [])
                    )
                ),
                None,
            ),
            "unresolved_mcp_failures": lambda _turn: [],
            "delivery_error_is_terminal": lambda _error: False,
            "log_dispatch": lambda *_args, **_kwargs: None,
            "session_has_owner": lambda _session_id: True,
            "background_mcp_ready": lambda: True,
            "turn_completed": lambda _session_id, _turn_id: False,
        }
        values.update(overrides)
        return DeliveryRuntime(**values)

    def test_terminal_delivery_does_not_close_its_exact_active_execution(self) -> None:
        context = self.context()
        delivery = self.start_delivery()
        self.claim_execution()
        finish_task_delivery(
            context,
            delivery_id=int(delivery["id"]),
            result={"ok": False, "status": "interrupted"},
            error=ValueError("interrupted"),
        )

        self.assertTrue(task_delivery_turn_is_current(
            context,
            turn_id="turn_original",
            agent_id="agent_recovery",
        ))

        assignment = {
            "workspace_root": self.workspace,
            "workflow_key": "software-development",
            "agent_id": "agent_recovery",
        }
        tool_runtime = ToolRuntime(
            sync_lock=threading.RLock(),
            assignment_context=lambda **_kwargs: {"assignment": assignment},
            workspace_active=lambda _workspace: True,
            invoke_tool=lambda *_args, **_kwargs: {
                "ok": True,
                "task": {"record_id": "recRecovery", "status": "review"},
            },
            sync_task_activity=lambda *_args, **_kwargs: None,
            delivery_record_id=lambda *_args, **_kwargs: "recRecovery",
            delivery_turn_is_current=lambda _assignment, **kwargs: (
                task_delivery_turn_is_current(
                    context,
                    agent_id="agent_recovery",
                    **kwargs,
                )
            ),
        )
        grant = tool_runtime.authorize(
            invocation_id="submit-after-interrupt",
            session_id="session_recovery",
            cwd=self.workspace,
            turn_id="turn_original",
            tool_name="mcp__teamflow__submit_task",
            tool_input={"record_id": "recRecovery", "outcome": "completed"},
        )
        result = tool_runtime.invoke(
            invocation_id="submit-after-interrupt",
            grant=grant["grant"],
            tool_name="submit_task",
            arguments={"record_id": "recRecovery", "outcome": "completed"},
        )
        self.assertTrue(result["ok"])

    def test_interrupted_dispatch_with_a_claim_stays_reconcilable(self) -> None:
        context = self.context()
        save_task_snapshot(
            context,
            record_id="recRecovery",
            task=self.ready_task(),
            source_event_id="evtRecoveryReady",
            source_revision="1",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]

        def run_turn(
            _session_id,
            _prompt,
            *,
            client_message_id,
            on_queued,
            on_started,
            stop_event,
        ):
            self.assertEqual(client_message_id, delivery["client_message_id"])
            self.assertFalse(stop_event.is_set())
            on_started("turn_original")
            self.claim_execution()
            return {
                "ok": False,
                "turn_id": "turn_original",
                "status": "interrupted",
                "error": "interrupted",
                "transport": "codex-ipc",
            }

        runtime = self.runtime(run_turn=run_turn)
        runtime.execute(context, delivery)

        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT status, turn_status, last_error FROM task_event_deliveries"
            ).fetchone()
        self.assertEqual(row["status"], "processing")
        self.assertEqual(row["turn_status"], "inProgress")
        self.assertIn("interrupted", row["last_error"])

    def test_queued_delivery_survives_restart_and_binds_once_materialized(self) -> None:
        context = self.context()
        save_task_snapshot(
            context,
            record_id="recRecovery",
            task=self.ready_task(),
            source_event_id="evtRecoveryReady",
            source_revision="1",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        mark_task_delivery_queued(
            context,
            delivery_id=int(delivery["id"]),
        )
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE task_event_deliveries SET next_attempt_at = NULL"
            )

        runtime = self.runtime(
            read_thread=lambda *_args, **_kwargs: self.fail(
                "queued reconciliation must not start app-server before materialization"
            ),
        )
        runtime.reconcile(context)
        with connect(self.db_path) as conn:
            queued = conn.execute(
                "SELECT status, turn_status, turn_id FROM task_event_deliveries"
            ).fetchone()
            conn.execute(
                "UPDATE task_event_deliveries SET next_attempt_at = NULL"
            )
        self.assertEqual(
            (queued["status"], queued["turn_status"], queued["turn_id"]),
            ("processing", "queued", None),
        )

        runtime = self.runtime(
            turn_id_for_client_message=lambda *_args: "turn_queued",
            read_thread=lambda *_args, **_kwargs: {
                "status": "active",
                "turns": [{
                    "id": "turn_queued",
                    "status": "inProgress",
                    "items": [{
                        "type": "userMessage",
                        "clientId": delivery["client_message_id"],
                    }],
                }],
            },
        )
        runtime.reconcile(context)
        with connect(self.db_path) as conn:
            started = conn.execute(
                "SELECT status, turn_status, turn_id FROM task_event_deliveries"
            ).fetchone()
        self.assertEqual(
            (started["status"], started["turn_status"], started["turn_id"]),
            ("processing", "inProgress", "turn_queued"),
        )
        self.assertEqual(
            task_delivery_turn_count(context, delivery_id=int(delivery["id"])),
            1,
        )

    def test_queued_rollout_binding_does_not_depend_on_app_server_visibility(self) -> None:
        context = self.context()
        save_task_snapshot(
            context,
            record_id="recRecovery",
            task=self.ready_task(),
            source_event_id="evtRecoveryReady",
            source_revision="1",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        mark_task_delivery_queued(context, delivery_id=int(delivery["id"]))
        with connect(self.db_path) as conn:
            conn.execute("UPDATE task_event_deliveries SET next_attempt_at = NULL")

        self.runtime(
            turn_id_for_client_message=lambda *_args: "turn_rollout",
            read_thread=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ValueError("Desktop-owned turn is not visible to this app-server")
            ),
        ).reconcile(context)

        with connect(self.db_path) as conn:
            saved = conn.execute(
                "SELECT status, turn_status, turn_id FROM task_event_deliveries"
            ).fetchone()
        self.assertEqual(
            (saved["status"], saved["turn_status"], saved["turn_id"]),
            ("processing", "inProgress", "turn_rollout"),
        )

    def test_tool_admission_binds_fast_queued_turn_before_handoff(self) -> None:
        context = self.context()
        save_task_snapshot(
            context,
            record_id="recRecovery",
            task=self.ready_task(),
            source_event_id="evtRecoveryReady",
            source_revision="1",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        mark_task_delivery_queued(context, delivery_id=int(delivery["id"]))
        mapper = lambda _session, client_id: (
            "turn_fast" if client_id == delivery["client_message_id"] else None
        )

        self.assertTrue(task_delivery_turn_is_current(
            context,
            turn_id="turn_fast",
            agent_id="agent_recovery",
            session_id="session_recovery",
            turn_id_for_client_message=mapper,
        ))
        self.assertEqual(
            task_delivery_record_id(
                self.workspace,
                turn_id="turn_fast",
                agent_id="agent_recovery",
            ),
            "recRecovery",
        )

        assignment = {
            "workspace_root": self.workspace,
            "workflow_key": "software-development",
            "agent_id": "agent_recovery",
            "session_id": "session_recovery",
        }
        runtime = ToolRuntime(
            sync_lock=threading.RLock(),
            assignment_context=lambda **_kwargs: {"assignment": assignment},
            workspace_active=lambda _workspace: True,
            invoke_tool=lambda *_args, **_kwargs: {
                "ok": True,
                "task": {"record_id": "recRecovery", "status": "review"},
            },
            sync_task_activity=lambda *_args, **_kwargs: None,
            delivery_record_id=lambda assignment, **kwargs: task_delivery_record_id(
                assignment["workspace_root"],
                agent_id=assignment["agent_id"],
                **kwargs,
            ),
            delivery_turn_is_current=lambda _assignment, **kwargs: (
                task_delivery_turn_is_current(
                    context,
                    agent_id="agent_recovery",
                    **kwargs,
                )
            ),
        )
        grant = runtime.authorize(
            invocation_id="fast-submit",
            session_id="session_recovery",
            cwd=self.workspace,
            turn_id="turn_fast",
            tool_name="mcp__teamflow__submit_task",
            tool_input={"record_id": "recRecovery", "outcome": "completed"},
        )
        result = runtime.invoke(
            invocation_id="fast-submit",
            grant=grant["grant"],
            tool_name="submit_task",
            arguments={"record_id": "recRecovery", "outcome": "completed"},
        )
        self.assertEqual(result["turn_control"]["action"], "end_turn")

    def test_queueing_state_recovers_after_daemon_restart(self) -> None:
        context = self.context()
        save_task_snapshot(
            context,
            record_id="recRecovery",
            task=self.ready_task(),
            source_event_id="evtRecoveryReady",
            source_revision="1",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        mark_task_delivery_queueing(context, delivery_id=int(delivery["id"]))
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE task_event_deliveries "
                "SET started_at = '2000-01-01T00:00:00+00:00', next_attempt_at = NULL"
            )

        self.runtime(
            queued_message_exists=lambda *_args: True,
        ).reconcile(context)

        with connect(self.db_path) as conn:
            saved = conn.execute(
                "SELECT status, turn_status, client_message_id "
                "FROM task_event_deliveries"
            ).fetchone()
        self.assertEqual(
            (saved["status"], saved["turn_status"]),
            ("processing", "queued"),
        )
        self.assertEqual(saved["client_message_id"], delivery["client_message_id"])

    def test_unconfirmed_queueing_state_retries_once_after_lease(self) -> None:
        context = self.context()
        save_task_snapshot(
            context,
            record_id="recRecovery",
            task=self.ready_task(),
            source_event_id="evtRecoveryReady",
            source_revision="1",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        mark_task_delivery_queueing(context, delivery_id=int(delivery["id"]))
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE task_event_deliveries "
                "SET started_at = '2000-01-01T00:00:00+00:00', next_attempt_at = NULL"
            )

        self.runtime(
            queued_message_exists=lambda *_args: False,
        ).reconcile(context)

        with connect(self.db_path) as conn:
            saved = conn.execute(
                "SELECT status, turn_status, client_message_id, attempts "
                "FROM task_event_deliveries"
            ).fetchone()
        self.assertEqual(saved["status"], "retry")
        self.assertEqual(saved["turn_status"], "unconfirmed")
        self.assertIsNone(saved["client_message_id"])
        self.assertEqual(saved["attempts"], 1)

    def test_claimed_continuation_queues_and_rebinds_its_execution(self) -> None:
        context = self.context()
        delivery = self.start_delivery()
        self.claim_execution()
        finish_task_delivery(
            context,
            delivery_id=int(delivery["id"]),
            result={"ok": False, "status": "interrupted"},
            error=ValueError("interrupted"),
            retry=True,
        )
        with connect(self.db_path) as conn:
            conn.execute("UPDATE task_event_deliveries SET next_attempt_at = NULL")
        continuation = claim_task_deliveries(context)[0]

        def run_turn(
            _session_id,
            _prompt,
            *,
            client_message_id,
            on_queued,
            on_started,
            stop_event,
        ):
            self.assertEqual(client_message_id, continuation["client_message_id"])
            self.assertIsNotNone(on_started)
            self.assertFalse(stop_event.is_set())
            on_queued("queue_continuation")
            return {"ok": True, "status": "queued", "turn_id": None}

        claimed_task = {
            **self.ready_task(),
            "status": "in_progress",
            "agent": "Recovery TL",
            "agent_id": "agent_recovery",
        }
        self.runtime(
            run_turn=run_turn,
            get_task=lambda *_args, **_kwargs: {"task": claimed_task},
        ).execute(context, continuation)
        with connect(self.db_path) as conn:
            queued = conn.execute(
                "SELECT status, turn_status, turn_id FROM task_event_deliveries"
            ).fetchone()
            conn.execute("UPDATE task_event_deliveries SET next_attempt_at = NULL")
        self.assertEqual(
            (queued["status"], queued["turn_status"], queued["turn_id"]),
            ("processing", "queued", "turn_original"),
        )

        self.runtime(
            turn_id_for_client_message=lambda *_args: "turn_continuation",
            read_thread=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ValueError("Desktop-owned turn is not visible to this app-server")
            ),
        ).reconcile(context)

        with connect(self.db_path) as conn:
            saved = conn.execute(
                "SELECT turn_status, turn_id FROM task_event_deliveries"
            ).fetchone()
            execution = conn.execute(
                "SELECT state, turn_id FROM task_executions WHERE record_id = ?",
                ("recRecovery",),
            ).fetchone()
        self.assertEqual(
            (saved["turn_status"], saved["turn_id"]),
            ("inProgress", "turn_continuation"),
        )
        self.assertEqual(
            (execution["state"], execution["turn_id"]),
            ("active", "turn_continuation"),
        )

    def test_daemon_start_recovers_legacy_owner_waiting_delivery_once(self) -> None:
        context = self.context()
        save_task_snapshot(
            context,
            record_id="recRecovery",
            task=self.ready_task(),
            source_event_id="evtRecoveryReady",
            source_revision="1",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        mark_task_delivery_waiting_for_session(
            context,
            delivery_id=int(delivery["id"]),
            error=CodexIpcNoOwner("not loaded"),
        )
        runtime = self.runtime()

        runtime.recover_waiting_sessions_for_queue(context)
        runtime.recover_waiting_sessions_for_queue(context)

        with connect(self.db_path) as conn:
            saved = conn.execute(
                "SELECT status, attempts, turn_id FROM task_event_deliveries"
            ).fetchone()
        self.assertEqual(
            (saved["status"], saved["attempts"], saved["turn_id"]),
            ("retry", 1, None),
        )

    def test_execute_persists_queue_acceptance_without_retrying(self) -> None:
        context = self.context()
        save_task_snapshot(
            context,
            record_id="recRecovery",
            task=self.ready_task(),
            source_event_id="evtRecoveryReady",
            source_revision="1",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]

        def run_turn(
            _session_id,
            _prompt,
            *,
            client_message_id,
            on_queued,
            on_started,
            stop_event,
        ):
            self.assertEqual(client_message_id, delivery["client_message_id"])
            self.assertIsNotNone(on_started)
            self.assertFalse(stop_event.is_set())
            on_queued("queue_accepted")
            return {
                "ok": True,
                "turn_id": None,
                "status": "queued",
                "transport": "codex-queue",
            }

        self.runtime(run_turn=run_turn).execute(context, delivery)

        with connect(self.db_path) as conn:
            saved = conn.execute(
                "SELECT status, turn_status, turn_id, attempts "
                "FROM task_event_deliveries"
            ).fetchone()
        self.assertEqual(
            (
                saved["status"],
                saved["turn_status"],
                saved["turn_id"],
                saved["attempts"],
            ),
            ("processing", "queued", None, 1),
        )

    def test_queue_callback_failure_keeps_the_original_delivery_reconcilable(self) -> None:
        context = self.context()
        save_task_snapshot(
            context,
            record_id="recRecovery",
            task=self.ready_task(),
            source_event_id="evtRecoveryReady",
            source_revision="1",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]

        def run_turn(_session_id, _prompt, **_kwargs):
            raise CodexTurnAcceptanceUnknown(
                "Codex accepted the queue but the callback failed"
            )

        self.runtime(run_turn=run_turn).execute(context, delivery)

        with connect(self.db_path) as conn:
            saved = conn.execute(
                "SELECT status, turn_status, turn_id, attempts "
                "FROM task_event_deliveries"
            ).fetchone()
        self.assertEqual(
            (
                saved["status"],
                saved["turn_status"],
                saved["turn_id"],
                saved["attempts"],
            ),
            ("processing", "queueing", None, 1),
        )

    def test_stale_queued_delivery_is_removed_before_local_cancellation(self) -> None:
        context = self.context()
        save_task_snapshot(
            context,
            record_id="recRecovery",
            task=self.ready_task(),
            source_event_id="evtRecoveryReady",
            source_revision="1",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        mark_task_delivery_queued(
            context,
            delivery_id=int(delivery["id"]),
        )
        self.advance_to_review()
        prepare_task_deliveries(context)
        with connect(self.db_path) as conn:
            next_attempt_at = conn.execute(
                "SELECT next_attempt_at FROM task_event_deliveries WHERE id = ?",
                (delivery["id"],),
            ).fetchone()[0]
        self.assertIsNone(next_attempt_at)
        removed = []

        self.runtime(
            turn_id_for_client_message=lambda *_args: self.fail(
                "a stale queued delivery must be rejected before turn binding"
            ),
            read_thread=lambda *_args, **_kwargs: self.fail(
                "a non-materialized queued turn should not start app-server"
            ),
            cancel_queued_message=lambda session_id, client_id: (
                removed.append((session_id, client_id)) or True
            ),
        ).reconcile(context)

        with connect(self.db_path) as conn:
            saved = conn.execute(
                "SELECT status, turn_status FROM task_event_deliveries"
            ).fetchone()
        self.assertEqual(saved["status"], "canceled")
        self.assertEqual(saved["turn_status"], "missing")
        self.assertEqual(
            removed,
            [("session_recovery", delivery["client_message_id"])],
        )

    def test_materialized_stale_queue_is_stopped_without_turn_binding(self) -> None:
        context = self.context()
        save_task_snapshot(
            context,
            record_id="recRecovery",
            task=self.ready_task(),
            source_event_id="evtRecoveryReady",
            source_revision="1",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        mark_task_delivery_queued(context, delivery_id=int(delivery["id"]))
        self.advance_to_review()
        prepare_task_deliveries(context)
        stopped = []

        self.runtime(
            turn_id_for_client_message=lambda *_args: "turn_stale",
            cancel_queued_message=lambda *_args: False,
            stop_turn=lambda session_id, *, expected_turn_id: stopped.append(
                (session_id, expected_turn_id)
            ),
        ).reconcile(context)

        with connect(self.db_path) as conn:
            saved = conn.execute(
                "SELECT status, turn_id, next_attempt_at FROM task_event_deliveries "
                "WHERE id = ?",
                (delivery["id"],),
            ).fetchone()
        self.assertEqual(saved["status"], "canceled")
        self.assertIsNone(saved["turn_id"])
        self.assertIsNone(saved["next_attempt_at"])
        self.assertEqual(stopped, [("session_recovery", "turn_stale")])

    def test_tool_admission_does_not_bind_a_stale_queued_turn(self) -> None:
        context = self.context()
        save_task_snapshot(
            context,
            record_id="recRecovery",
            task=self.ready_task(),
            source_event_id="evtRecoveryReady",
            source_revision="1",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        mark_task_delivery_queued(context, delivery_id=int(delivery["id"]))
        self.advance_to_review()
        mapper_called = False

        def mapper(*_args):
            nonlocal mapper_called
            mapper_called = True
            return "turn_stale"

        self.assertFalse(task_delivery_turn_is_current(
            context,
            turn_id="turn_stale",
            agent_id="agent_recovery",
            session_id="session_recovery",
            turn_id_for_client_message=mapper,
        ))
        self.assertFalse(mapper_called)
        with connect(self.db_path) as conn:
            saved = conn.execute(
                "SELECT turn_id FROM task_event_deliveries WHERE id = ?",
                (delivery["id"],),
            ).fetchone()
        self.assertIsNone(saved["turn_id"])
        self.assertEqual(
            task_delivery_turn_count(context, delivery_id=int(delivery["id"])),
            0,
        )

    def test_blocked_queue_is_revoked_when_the_task_becomes_ready(self) -> None:
        context = self.context()
        with connect(self.db_path) as conn:
            workspace = conn.execute("SELECT * FROM workspaces LIMIT 1").fetchone()
            role = conn.execute(
                "SELECT * FROM roles WHERE workflow_id = ? AND role_key = 'pm'",
                (workspace["current_workflow_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO agents (
                  id, workspace_id, workflow_id, role_id, role_key,
                  harness_type, session_id, display_name, created_at, updated_at
                ) VALUES (
                  'agent_pm', ?, ?, ?, 'pm', 'codex',
                  'session_pm', 'PM', ?, ?
                )
                """,
                (
                    workspace["id"],
                    workspace["current_workflow_id"],
                    role["id"],
                    now(),
                    now(),
                ),
            )
        blocked = {
            **self.ready_task(),
            "status": "blocked",
            "blocked_reason": "Waiting for a decision",
            "waiting_on": "stakeholder",
            "next_action": "PM resolves the blocker",
        }
        save_task_snapshot(
            context,
            record_id="recRecovery",
            task=blocked,
            source_event_id="evtRecoveryBlocked",
            source_revision="1",
        )
        prepare_task_deliveries(context)
        blocked_delivery = claim_task_deliveries(context)[0]
        self.assertEqual(blocked_delivery["session_id"], "session_pm")
        mark_task_delivery_queued(
            context,
            delivery_id=int(blocked_delivery["id"]),
        )

        save_task_snapshot(
            context,
            record_id="recRecovery",
            task=self.ready_task(),
            source_event_id="evtRecoveryReady",
            source_revision="2",
        )
        prepare_task_deliveries(context)
        with connect(self.db_path) as conn:
            stale = conn.execute(
                "SELECT next_attempt_at FROM task_event_deliveries WHERE id = ?",
                (blocked_delivery["id"],),
            ).fetchone()
        self.assertIsNone(stale["next_attempt_at"])

        removed = []
        self.runtime(
            turn_id_for_client_message=lambda *_args: None,
            cancel_queued_message=lambda session_id, client_id: (
                removed.append((session_id, client_id)) or True
            ),
        ).reconcile(context)

        ready_delivery = claim_task_deliveries(context)[0]
        self.assertEqual(ready_delivery["session_id"], "session_recovery")
        with connect(self.db_path) as conn:
            stale = conn.execute(
                "SELECT status FROM task_event_deliveries WHERE id = ?",
                (blocked_delivery["id"],),
            ).fetchone()
        self.assertEqual(stale["status"], "canceled")
        self.assertEqual(
            removed,
            [("session_pm", blocked_delivery["client_message_id"])],
        )

    def test_consumer_schedules_before_reconciling_old_deliveries(self) -> None:
        runtime = self.runtime()
        runtime.routes_ready.set()
        calls = []
        runtime.recover_waiting_sessions_for_queue = lambda _context: None
        runtime.resume_permission_waiting = lambda _context: None
        runtime.resume_session_waiting = lambda _context: None
        runtime.schedule = lambda _context: calls.append("schedule")

        def reconcile(_context):
            calls.append("reconcile")
            runtime.stopping.set()
            runtime.wakeup.set()

        runtime.reconcile = reconcile
        runtime.consume()

        self.assertEqual(calls, ["schedule", "reconcile"])

    def test_interrupted_dispatch_before_claim_stays_reconcilable(self) -> None:
        context = self.context()
        save_task_snapshot(
            context,
            record_id="recRecovery",
            task=self.ready_task(),
            source_event_id="evtRecoveryReady",
            source_revision="1",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]

        def run_turn(
            _session_id,
            _prompt,
            *,
            client_message_id,
            on_queued,
            on_started,
            stop_event,
        ):
            self.assertEqual(client_message_id, delivery["client_message_id"])
            self.assertFalse(stop_event.is_set())
            on_started("turn_original")
            return {
                "ok": False,
                "turn_id": "turn_original",
                "status": "interrupted",
                "error": "interrupted",
                "transport": "codex-ipc",
            }

        self.runtime(run_turn=run_turn).execute(context, delivery)

        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT status, turn_status, last_error FROM task_event_deliveries"
            ).fetchone()
        self.assertEqual(row["status"], "processing")
        self.assertEqual(row["turn_status"], "inProgress")
        self.assertIn("interrupted", row["last_error"])
        self.assertTrue(task_delivery_turn_is_current(
            context,
            turn_id="turn_original",
            agent_id="agent_recovery",
        ))

    def test_owner_wait_attempts_do_not_exhaust_the_turn_retry_budget(self) -> None:
        context = self.context()
        save_task_snapshot(
            context,
            record_id="recRecovery",
            task=self.ready_task(),
            source_event_id="evtRecoveryReady",
            source_revision="1",
        )
        prepare_task_deliveries(context)
        delivery = claim_task_deliveries(context)[0]
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE task_event_deliveries SET attempts = 55"
            )
        delivery["attempts"] = 55

        def run_turn(
            _session_id,
            _prompt,
            *,
            client_message_id,
            on_queued,
            on_started,
            stop_event,
        ):
            self.assertEqual(client_message_id, delivery["client_message_id"])
            self.assertFalse(stop_event.is_set())
            on_started("turn_original")
            return {
                "ok": False,
                "turn_id": "turn_original",
                "status": "interrupted",
                "error": "interrupted",
                "transport": "codex-ipc",
            }

        self.runtime(
            run_turn=run_turn,
            turn_completed=lambda *_args, **_kwargs: True,
        ).execute(context, delivery)

        with connect(self.db_path) as conn:
            saved = conn.execute(
                "SELECT status, attempts FROM task_event_deliveries"
            ).fetchone()
        self.assertEqual((saved["status"], saved["attempts"]), ("retry", 55))
        self.assertEqual(
            task_delivery_turn_count(context, delivery_id=int(delivery["id"])),
            1,
        )

    def test_same_turn_handoff_after_interrupt_converges_delivery(self) -> None:
        context = self.context()
        delivery = self.start_delivery()
        self.claim_execution()
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE task_event_deliveries SET next_attempt_at = NULL"
            )
            conn.execute(
                "DELETE FROM task_executions WHERE record_id = ?",
                ("recRecovery",),
            )
        save_task_snapshot(
            context,
            record_id="recRecovery",
            task={
                **self.ready_task(),
                "status": "review",
                "agent": "Recovery TL",
                "agent_id": "agent_recovery",
            },
            source_event_id="evtRecoverySubmitted",
            source_revision="3",
        )
        thread = {
            "status": {"type": "idle"},
            "turns": [{
                "id": "turn_original",
                "status": "completed",
                "items": [{
                    "type": "userMessage",
                    "clientId": delivery["client_message_id"],
                }],
            }],
        }
        self.runtime(read_thread=lambda *_args, **_kwargs: thread).reconcile(context)

        with connect(self.db_path) as conn:
            saved = conn.execute(
                "SELECT status, turn_status FROM task_event_deliveries"
            ).fetchone()
        self.assertEqual((saved["status"], saved["turn_status"]), ("completed", "completed"))

    def test_late_interrupted_turn_stays_admitted_until_rollout_completion(self) -> None:
        context = self.context()
        delivery = self.start_delivery(turn_id="turn_placeholder")
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE task_event_deliveries
                SET attempts = 4,
                    next_attempt_at = NULL,
                    started_at = '2000-01-01T00:00:00+00:00'
                """
            )

        thread = {
            "status": {"type": "active"},
            "turns": [{
                "id": "turn_visible",
                "status": "interrupted",
                "items": [{
                    "type": "userMessage",
                    "clientId": delivery["client_message_id"],
                }],
            }],
        }
        runtime = self.runtime(
            read_thread=lambda *_args, **_kwargs: thread,
            turn_completed=lambda *_args, **_kwargs: False,
        )
        runtime.reconcile(context)

        with connect(self.db_path) as conn:
            pending = conn.execute(
                """
                SELECT status, turn_id, turn_status
                FROM task_event_deliveries
                """
            ).fetchone()
        self.assertEqual(
            (pending["status"], pending["turn_id"], pending["turn_status"]),
            ("processing", "turn_visible", "inProgress"),
        )
        self.assertFalse(task_delivery_turn_is_current(
            context,
            turn_id="turn_placeholder",
            agent_id="agent_recovery",
        ))
        self.assertTrue(task_delivery_turn_is_current(
            context,
            turn_id="turn_visible",
            agent_id="agent_recovery",
        ))

        assignment = {
            "workspace_root": self.workspace,
            "workflow_key": "software-development",
            "agent_id": "agent_recovery",
        }
        tool_runtime = ToolRuntime(
            sync_lock=threading.RLock(),
            assignment_context=lambda **_kwargs: {"assignment": assignment},
            workspace_active=lambda _workspace: True,
            invoke_tool=lambda *_args, **_kwargs: {"ok": True},
            sync_task_activity=lambda *_args, **_kwargs: None,
            delivery_record_id=lambda *_args, **_kwargs: "recRecovery",
            delivery_turn_is_current=lambda _assignment, **kwargs: (
                task_delivery_turn_is_current(
                    context,
                    agent_id="agent_recovery",
                    **kwargs,
                )
            ),
        )
        grant = tool_runtime.authorize(
            invocation_id="read-after-late-materialization",
            session_id="session_recovery",
            cwd=self.workspace,
            turn_id="turn_visible",
            tool_name="mcp__teamflow__get_task",
            tool_input={"record_id": "recRecovery"},
        )
        self.assertTrue(grant["grant"])

        thread["status"] = {"type": "idle"}
        thread["turns"][0]["status"] = "completed"
        runtime.turn_completed = lambda *_args, **_kwargs: True
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE task_event_deliveries SET next_attempt_at = NULL"
            )
        runtime.reconcile(context)

        with connect(self.db_path) as conn:
            completed = conn.execute(
                "SELECT status, turn_id, turn_status FROM task_event_deliveries"
            ).fetchone()
        self.assertEqual(
            (completed["status"], completed["turn_id"], completed["turn_status"]),
            ("retry", "turn_visible", "completed"),
        )
        self.assertFalse(task_delivery_turn_is_current(
            context,
            turn_id="turn_visible",
            agent_id="agent_recovery",
        ))

    def test_failed_unclaimed_current_delivery_is_recovered(self) -> None:
        context = self.context()
        delivery = self.start_delivery()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE task_event_deliveries
                SET status = 'failed', attempts = 55,
                    turn_status = 'interrupted', completed_at = ?
                """,
                (now(),),
            )

        recovered = recover_retryable_failed_task_deliveries(
            context,
            max_turn_attempts=3,
        )

        with connect(self.db_path) as conn:
            saved = conn.execute(
                """
                SELECT status, attempts, completed_at
                FROM task_event_deliveries
                """
            ).fetchone()
        self.assertEqual(recovered, 1)
        self.assertEqual((saved["status"], saved["attempts"]), ("retry", 55))
        self.assertIsNone(saved["completed_at"])
        self.assertEqual(
            task_delivery_turn_count(context, delivery_id=int(delivery["id"])),
            1,
        )

    def test_stale_interrupted_delivery_is_canceled_after_task_advances(self) -> None:
        context = self.context()
        started = self.start_delivery()
        self.claim_execution()
        self.advance_to_review()
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE task_event_deliveries SET next_attempt_at = NULL"
            )
        thread = {
            "status": {"type": "idle"},
            "turns": [{
                "id": "turn_original",
                "status": "interrupted",
                "items": [{
                    "type": "userMessage",
                    "clientId": started["client_message_id"],
                }],
            }],
        }

        self.runtime(
            read_thread=lambda *_args, **_kwargs: thread,
            turn_completed=lambda *_args, **_kwargs: True,
        ).reconcile(context)

        with connect(self.db_path) as conn:
            delivery = conn.execute(
                "SELECT status, turn_status, last_error FROM task_event_deliveries"
            ).fetchone()
            execution = conn.execute(
                "SELECT state FROM task_executions WHERE record_id = ?",
                ("recRecovery",),
            ).fetchone()
        self.assertEqual(
            (delivery["status"], delivery["turn_status"]),
            ("canceled", "interrupted"),
        )
        self.assertIn("no longer needs", delivery["last_error"])
        self.assertEqual(execution["state"], "stopped")

    def test_restart_cancels_stale_failed_delivery_and_execution(self) -> None:
        context = self.context()
        delivery = self.start_delivery()
        self.claim_execution()
        self.advance_to_review()
        finish_task_delivery(
            context,
            delivery_id=int(delivery["id"]),
            result={"ok": False, "status": "interrupted"},
            error=ValueError("interrupted"),
        )

        recovered = recover_retryable_failed_task_deliveries(
            context,
            max_turn_attempts=3,
        )

        with connect(self.db_path) as conn:
            saved = conn.execute(
                "SELECT status, turn_status, last_error FROM task_event_deliveries"
            ).fetchone()
            execution = conn.execute(
                "SELECT state FROM task_executions WHERE record_id = ?",
                ("recRecovery",),
            ).fetchone()
        self.assertEqual(recovered, 0)
        self.assertEqual(
            (saved["status"], saved["turn_status"]),
            ("canceled", "interrupted"),
        )
        self.assertIn("no longer needs", saved["last_error"])
        self.assertEqual(execution["state"], "stopped")

    def test_active_interrupted_unclaimed_turn_outlives_lease(self) -> None:
        context = self.context()
        delivery = self.start_delivery()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE task_event_deliveries
                SET attempts = 4,
                    next_attempt_at = NULL,
                    started_at = '2000-01-01T00:00:00+00:00'
                """
            )
        thread = {
            "status": {"type": "active"},
            "turns": [{
                "id": "turn_original",
                "status": "interrupted",
                "items": [{
                    "type": "userMessage",
                    "clientId": delivery["client_message_id"],
                }],
            }],
        }

        self.runtime(
            read_thread=lambda *_args, **_kwargs: thread,
            turn_completed=lambda *_args, **_kwargs: False,
        ).reconcile(context)

        with connect(self.db_path) as conn:
            pending = conn.execute(
                "SELECT status, turn_status FROM task_event_deliveries"
            ).fetchone()
        self.assertEqual(
            (pending["status"], pending["turn_status"]),
            ("processing", "inProgress"),
        )

    def test_claimed_turn_outlives_stale_snapshot_and_daemon_restart(self) -> None:
        context = self.context()
        delivery = self.start_delivery()
        self.claim_execution()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE task_event_deliveries
                SET attempts = 3,
                    next_attempt_at = NULL,
                    started_at = '2000-01-01T00:00:00+00:00'
                """
            )

        thread = {
            "status": {"type": "idle"},
            "turns": [{
                "id": "turn_original",
                "status": "interrupted",
                "items": [{
                    "type": "userMessage",
                    "clientId": delivery["client_message_id"],
                }],
            }],
        }
        runtime = self.runtime(
            read_thread=lambda *_args, **_kwargs: thread,
            turn_completed=lambda *_args, **_kwargs: False,
        )
        runtime.reconcile(context)
        # A daemon restart rebuilds the runtime from the same durable delivery and
        # execution rows. The stale owner snapshot must still not revoke the turn.
        self.runtime(
            read_thread=lambda *_args, **_kwargs: thread,
            turn_completed=lambda *_args, **_kwargs: False,
        ).reconcile(context)

        with connect(self.db_path) as conn:
            pending = conn.execute(
                "SELECT status, turn_status FROM task_event_deliveries"
            ).fetchone()
            execution = conn.execute(
                """
                SELECT state, stop_status
                FROM task_executions WHERE record_id = ?
                """,
                ("recRecovery",),
            ).fetchone()
        self.assertEqual(
            (pending["status"], pending["turn_status"]),
            ("processing", "inProgress"),
        )
        self.assertEqual(
            (execution["state"], execution["stop_status"]),
            ("active", None),
        )
        self.assertTrue(task_delivery_turn_is_current(
            context,
            turn_id="turn_original",
            agent_id="agent_recovery",
        ))
        assignment = {
            "workspace_root": self.workspace,
            "workflow_key": "software-development",
            "agent_id": "agent_recovery",
        }
        tool_runtime = ToolRuntime(
            sync_lock=threading.RLock(),
            assignment_context=lambda **_kwargs: {"assignment": assignment},
            workspace_active=lambda _workspace: True,
            invoke_tool=lambda *_args, **_kwargs: {"ok": True},
            sync_task_activity=lambda *_args, **_kwargs: None,
            delivery_record_id=lambda *_args, **_kwargs: "recRecovery",
            delivery_turn_is_current=lambda _assignment, **kwargs: (
                task_delivery_turn_is_current(
                    context,
                    agent_id="agent_recovery",
                    **kwargs,
                )
            ),
        )
        grant = tool_runtime.authorize(
            invocation_id="submit-after-compaction-and-restart",
            session_id="session_recovery",
            cwd=self.workspace,
            turn_id="turn_original",
            tool_name="mcp__teamflow__submit_task",
            tool_input={"record_id": "recRecovery", "outcome": "completed"},
        )
        self.assertTrue(grant["grant"])

    def test_completed_interrupted_turn_rebinds_one_continuation_atomically(self) -> None:
        context = self.context()
        delivery = self.start_delivery()
        self.claim_execution()
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE task_event_deliveries SET next_attempt_at = NULL"
            )

        thread = {
            "status": {"type": "idle"},
            "turns": [{
                "id": "turn_original",
                "status": "interrupted",
                "items": [{
                    "type": "userMessage",
                    "clientId": delivery["client_message_id"],
                }],
            }],
        }
        runtime = self.runtime(
            read_thread=lambda *_args, **_kwargs: thread,
            turn_completed=lambda _session_id, turn_id: turn_id == "turn_original",
        )
        runtime.reconcile(context)
        runtime.reconcile(context)

        with connect(self.db_path) as conn:
            retried = conn.execute(
                "SELECT status, turn_id, attempts FROM task_event_deliveries"
            ).fetchone()
        self.assertEqual(
            (retried["status"], retried["turn_id"], retried["attempts"]),
            ("retry", "turn_original", 1),
        )

        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE task_event_deliveries SET next_attempt_at = NULL"
            )
        continuation = claim_task_deliveries(context)[0]
        self.assertEqual(continuation["continuation_turn_id"], "turn_original")
        self.assertEqual(claim_task_deliveries(context), [])
        mark_task_delivery_turn_started(
            context,
            delivery_id=int(continuation["id"]),
            turn_id="turn_continuation",
            previous_turn_id="turn_original",
            require_execution_rebind=True,
        )

        with connect(self.db_path) as conn:
            current = conn.execute(
                "SELECT turn_id, state FROM task_executions WHERE record_id = ?",
                ("recRecovery",),
            ).fetchone()
            saved = conn.execute(
                "SELECT turn_id, attempts FROM task_event_deliveries"
            ).fetchone()
            history = conn.execute(
                "SELECT turn_id FROM task_delivery_turns ORDER BY created_at, turn_id"
            ).fetchall()
        self.assertEqual((current["turn_id"], current["state"]), ("turn_continuation", "active"))
        self.assertEqual((saved["turn_id"], saved["attempts"]), ("turn_continuation", 2))
        self.assertEqual({row["turn_id"] for row in history}, {
            "turn_original",
            "turn_continuation",
        })
        self.assertFalse(task_delivery_turn_is_current(
            context,
            turn_id="turn_original",
            agent_id="agent_recovery",
        ))
        self.assertTrue(task_delivery_turn_is_current(
            context,
            turn_id="turn_continuation",
            agent_id="agent_recovery",
        ))

    def test_exhausted_continuation_stops_execution_before_delivery_fails(self) -> None:
        context = self.context()
        delivery = self.start_delivery()
        self.claim_execution()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE task_event_deliveries
                SET attempts = 3, next_attempt_at = NULL
                """
            )
            delivery_id = int(delivery["id"])
            conn.executemany(
                """
                INSERT INTO task_delivery_turns (delivery_id, turn_id, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    (delivery_id, "turn_retry_1", now()),
                    (delivery_id, "turn_retry_2", now()),
                ),
            )
        thread = {
            "status": {"type": "idle"},
            "turns": [{
                "id": "turn_original",
                "status": "interrupted",
                "items": [{
                    "type": "userMessage",
                    "clientId": delivery["client_message_id"],
                }],
            }],
        }
        runtime = self.runtime(
            read_thread=lambda *_args, **_kwargs: thread,
            turn_completed=lambda *_args, **_kwargs: True,
        )
        runtime.reconcile(context)

        with connect(self.db_path) as conn:
            delivery_row = conn.execute(
                "SELECT status, turn_status FROM task_event_deliveries"
            ).fetchone()
            execution = conn.execute(
                "SELECT state, stop_status FROM task_executions WHERE record_id = ?",
                ("recRecovery",),
            ).fetchone()
            contradiction = conn.execute(
                """
                SELECT 1
                FROM task_event_deliveries AS delivery
                JOIN task_events AS event ON event.event_key = delivery.event_key
                JOIN task_executions AS execution
                  ON execution.record_id = event.record_id
                 AND execution.agent_id = delivery.agent_id
                 AND execution.session_id = delivery.session_id
                 AND execution.turn_id = delivery.turn_id
                WHERE delivery.status IN ('failed', 'canceled')
                  AND execution.state = 'active'
                """
            ).fetchone()
        self.assertEqual((delivery_row["status"], delivery_row["turn_status"]), ("failed", "interrupted"))
        self.assertEqual((execution["state"], execution["stop_status"]), ("stopped", "continuation_exhausted"))
        self.assertIsNone(contradiction)

    def test_stale_turn_cannot_terminalize_a_rebound_execution(self) -> None:
        context = self.context()
        delivery = self.start_delivery()
        self.claim_execution()

        finalized = fail_claimed_task_delivery(
            context,
            delivery_id=int(delivery["id"]),
            turn_id="turn_stale",
            turn_status="interrupted",
            reason="stale reconciliation",
        )

        with connect(self.db_path) as conn:
            delivery_row = conn.execute(
                "SELECT status, turn_id FROM task_event_deliveries"
            ).fetchone()
            execution = conn.execute(
                "SELECT state, turn_id FROM task_executions WHERE record_id = ?",
                ("recRecovery",),
            ).fetchone()
        self.assertFalse(finalized)
        self.assertEqual(
            (delivery_row["status"], delivery_row["turn_id"]),
            ("processing", "turn_original"),
        )
        self.assertEqual(
            (execution["state"], execution["turn_id"]),
            ("active", "turn_original"),
        )

    def test_recovered_continuation_turn_rebinds_execution(self) -> None:
        context = self.context()
        delivery = self.start_delivery()
        self.claim_execution()
        finish_task_delivery(
            context,
            delivery_id=int(delivery["id"]),
            result={"ok": False, "status": "interrupted"},
            error=ValueError("interrupted"),
            retry=True,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE task_event_deliveries SET next_attempt_at = NULL"
            )
        continuation = claim_task_deliveries(context)[0]
        thread = {
            "status": {"type": "active"},
            "turns": [{
                "id": "turn_recovered",
                "status": "inProgress",
                "items": [{
                    "type": "userMessage",
                    "clientId": continuation["client_message_id"],
                }],
            }],
        }
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE task_event_deliveries SET next_attempt_at = NULL"
            )
        self.runtime(read_thread=lambda *_args, **_kwargs: thread).reconcile(context)

        with connect(self.db_path) as conn:
            delivery_row = conn.execute(
                "SELECT status, turn_id FROM task_event_deliveries"
            ).fetchone()
            execution = conn.execute(
                "SELECT state, turn_id FROM task_executions WHERE record_id = ?",
                ("recRecovery",),
            ).fetchone()
        self.assertEqual(
            (delivery_row["status"], delivery_row["turn_id"]),
            ("processing", "turn_recovered"),
        )
        self.assertEqual(
            (execution["state"], execution["turn_id"]),
            ("active", "turn_recovered"),
        )
        self.assertFalse(task_delivery_turn_is_current(
            context,
            turn_id="turn_original",
            agent_id="agent_recovery",
        ))
        self.assertTrue(task_delivery_turn_is_current(
            context,
            turn_id="turn_recovered",
            agent_id="agent_recovery",
        ))

    def test_context_recovery_does_not_change_the_claimed_turn_binding(self) -> None:
        context = self.context()
        self.start_delivery()
        self.claim_execution()
        onboarding = agent_context(
            self.workspace,
            session_id="session_recovery",
            consume=True,
        )
        confirm_agent_context(
            self.workspace,
            agent_id="agent_recovery",
            session_id="session_recovery",
            assignment_revision=onboarding["assignment"]["assignment_revision"],
            context_fingerprint=onboarding["context_fingerprint"],
        )
        marked = mark_agent_context_recovery_pending(
            self.workspace,
            agent_id="agent_recovery",
            session_id="session_recovery",
            assignment_revision=onboarding["assignment"]["assignment_revision"],
        )
        self.assertTrue(marked["marked"])
        recovery = agent_context(
            self.workspace,
            session_id="session_recovery",
            consume=True,
        )
        confirm_agent_context(
            self.workspace,
            agent_id="agent_recovery",
            session_id="session_recovery",
            assignment_revision=recovery["assignment"]["assignment_revision"],
            context_fingerprint=recovery["context_fingerprint"],
        )
        self.assertTrue(task_delivery_turn_is_current(
            context,
            turn_id="turn_original",
            agent_id="agent_recovery",
        ))


class CodexRolloutCompletionTest(unittest.TestCase):
    def test_completion_is_read_from_the_exact_rollout_turn(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rollout-completion-", dir=ROOT / "tmp") as temp:
            path = Path(temp) / "rollout.jsonl"
            path.write_text(
                "\n".join((
                    json.dumps({
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-a"},
                    }),
                    json.dumps({
                        "type": "event_msg",
                        "payload": {"type": "task_complete", "turn_id": "turn-a"},
                    }),
                )),
                encoding="utf-8",
            )
            with patch("core.codex_rollout._codex_rollout_path", return_value=path):
                self.assertTrue(codex_turn_completed("session-a", "turn-a"))
                self.assertFalse(codex_turn_completed("session-a", "turn-b"))


if __name__ == "__main__":
    unittest.main()
