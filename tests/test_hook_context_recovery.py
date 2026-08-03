from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from core.agent_context_runtime import AgentContextRuntime
from core.agent_runtime import (
    agent_context,
    confirm_agent_context,
    find_agent_assignment,
    mark_agent_context_recovery_pending,
)
from core.config import resolve_workspace_paths
from core.db import connect, init_workspace, register_agent


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "hooks") not in sys.path:
    sys.path.insert(0, str(ROOT / "hooks"))

import session_runtime  # noqa: E402
import teamflow_hook  # noqa: E402
import user_prompt_submit  # noqa: E402


SESSION = "session-compact"


def codex_thread(thread_id: str, *, include_turns: bool = False) -> dict[str, object]:
    return {
        "id": thread_id,
        "name": f"Session {thread_id}",
        "status": {"type": "notLoaded"},
        "cwd": None,
    }


class FakeDaemon:
    """Serves the hook over the same runtime object the daemon uses."""

    def __init__(self, workspace: str) -> None:
        self.logs: list[str] = []
        self.calls: list[str] = []
        self.failing: set[str] = set()
        self.before_confirm = None
        self.runtime = AgentContextRuntime(
            workspaces=lambda: [workspace],
            resolve=lambda name: {
                "find_agent_assignment": find_agent_assignment,
                "confirm_agent_context": confirm_agent_context,
                "mark_agent_context_recovery_pending": mark_agent_context_recovery_pending,
            }[name],
            emit_log=lambda label, **_: self.logs.append(label),
            style=lambda text, _code: text,
        )

    def __call__(self, payload: dict, *, timeout: float = 2) -> dict:
        action = payload["action"]
        self.calls.append(action)
        if action in self.failing:
            raise OSError("TeamFlow daemon is unavailable")
        if action == "assignment_context":
            return self.runtime.assignment(
                session_id=payload["session_id"],
                cwd=payload.get("cwd"),
                consume=bool(payload.get("consume")),
                refresh=bool(payload.get("refresh")),
            )
        if action == "compact_assignment_context":
            return self.runtime.mark_compacted(
                session_id=payload["session_id"],
                cwd=payload.get("cwd"),
            )
        if action == "confirm_assignment_context":
            if self.before_confirm is not None:
                self.before_confirm()
            return self.runtime.confirm(
                workspace=payload["workspace"],
                agent_id=payload["agent_id"],
                session_id=payload["session_id"],
                assignment_revision=int(payload["assignment_revision"]),
                context_fingerprint=payload["context_fingerprint"],
                context_kind=payload.get("context_kind"),
            )
        raise AssertionError(f"unexpected daemon action: {action}")


class HookContextRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="hook-recovery-", dir=ROOT / "tmp")
        self.addCleanup(self.temp.cleanup)
        self.workspace = self.temp.name
        init_workspace(self.workspace)
        threads = patch("core.db.read_codex_thread", side_effect=codex_thread)
        threads.start()
        self.addCleanup(threads.stop)
        self.agent_id = register_agent(
            self.workspace,
            role="tl",
            harness_type="codex",
            session_id=SESSION,
        )["agent_id"]
        self.daemon = FakeDaemon(self.workspace)

    def run_hook(self, module, hook: dict) -> str:
        buffer = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(module, "record_runtime_event"))
            stack.enter_context(patch.object(teamflow_hook, "daemon_request", self.daemon))
            if hasattr(module, "daemon_request"):
                stack.enter_context(patch.object(module, "daemon_request", self.daemon))
            stack.enter_context(patch.object(sys, "stdin", io.StringIO(json.dumps(hook))))
            stack.enter_context(redirect_stdout(buffer))
            module.main()
        return buffer.getvalue()

    def prompt(self, **overrides) -> dict:
        return {
            "session_id": SESSION,
            "cwd": self.workspace,
            "hook_event_name": "UserPromptSubmit",
            **overrides,
        }

    def compact_start(self, **overrides) -> dict:
        return {
            "session_id": SESSION,
            "cwd": self.workspace,
            "hook_event_name": "SessionStart",
            "source": "compact",
            **overrides,
        }

    def status(self) -> str:
        return agent_context(self.workspace, session_id=SESSION, consume=False)["context_status"]

    def onboard(self) -> None:
        self.assertNotEqual(self.run_hook(user_prompt_submit, self.prompt()), "")
        self.daemon.calls.clear()
        self.daemon.logs.clear()

    def bump_revision(self) -> None:
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            conn.execute(
                "UPDATE agents SET assignment_revision = assignment_revision + 1 WHERE id = ?",
                (self.agent_id,),
            )

    def test_the_first_user_prompt_still_onboards_a_new_agent(self):
        output = json.loads(self.run_hook(user_prompt_submit, self.prompt()))

        specific = output["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "UserPromptSubmit")
        self.assertIn("你已被注册为 TeamFlow Agent", specific["additionalContext"])
        self.assertEqual(self.daemon.logs, ["AGENT ONBOARDED"])
        self.assertEqual(self.status(), "injected")

    def test_a_compact_session_start_restores_the_context_in_one_invocation(self):
        self.onboard()

        output = json.loads(self.run_hook(session_runtime, self.compact_start()))

        specific = output["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "SessionStart")
        self.assertIn("会话压缩后恢复", specific["additionalContext"])
        self.assertEqual(
            self.daemon.calls,
            ["compact_assignment_context", "assignment_context", "confirm_assignment_context"],
        )
        self.assertEqual(
            self.daemon.logs,
            ["AGENT CONTEXT RECOVERY PENDING", "AGENT CONTEXT RESTORED"],
        )
        self.assertEqual(self.status(), "injected")

    def test_a_restored_session_does_not_inject_again_on_the_next_user_prompt(self):
        self.onboard()
        self.run_hook(session_runtime, self.compact_start())
        self.daemon.calls.clear()

        self.assertEqual(self.run_hook(user_prompt_submit, self.prompt()), "")
        self.assertEqual(self.daemon.calls, ["assignment_context"])

    def test_the_stop_hook_still_answers_with_an_empty_decision(self):
        self.onboard()

        output = self.run_hook(session_runtime, {"session_id": SESSION, "hook_event_name": "Stop"})

        self.assertEqual(output, "{}\n")
        self.assertEqual(self.daemon.calls, [])

    def test_a_stale_post_compact_invocation_no_longer_changes_state(self):
        self.onboard()

        output = self.run_hook(user_prompt_submit, self.prompt(hook_event_name="PostCompact"))

        self.assertEqual(output, "")
        self.assertEqual(self.daemon.calls, [])
        self.assertEqual(self.status(), "injected")

    def test_a_non_compact_session_start_leaves_the_assignment_untouched(self):
        self.onboard()

        for source in ("startup", "resume", "clear"):
            self.assertEqual(self.run_hook(session_runtime, self.compact_start(source=source)), "")
        self.assertEqual(self.daemon.calls, [])
        self.assertEqual(self.status(), "injected")

    def test_an_unconfirmed_recovery_stays_pending_for_the_next_user_prompt(self):
        self.onboard()
        self.daemon.before_confirm = self.bump_revision

        output = json.loads(self.run_hook(session_runtime, self.compact_start()))

        self.assertIn("会话压缩后恢复", output["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("AGENT CONTEXT RESTORED", self.daemon.logs)
        self.assertEqual(self.status(), "recovery_pending")

        self.daemon.before_confirm = None
        retried = json.loads(self.run_hook(user_prompt_submit, self.prompt()))
        self.assertIn("会话压缩后恢复", retried["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.status(), "injected")

    def test_a_daemon_failure_during_recovery_leaves_the_context_pending(self):
        self.onboard()
        self.daemon.failing = {"assignment_context"}

        self.assertEqual(self.run_hook(session_runtime, self.compact_start()), "")
        self.assertEqual(self.status(), "recovery_pending")

        self.daemon.failing = set()
        retried = json.loads(self.run_hook(user_prompt_submit, self.prompt()))
        self.assertIn("会话压缩后恢复", retried["hookSpecificOutput"]["additionalContext"])

    def test_an_unreachable_daemon_at_the_compact_boundary_changes_nothing(self):
        self.onboard()
        self.daemon.failing = {"compact_assignment_context"}

        self.assertEqual(self.run_hook(session_runtime, self.compact_start()), "")
        self.assertEqual(self.daemon.calls, ["compact_assignment_context"])
        self.assertEqual(self.status(), "injected")


if __name__ == "__main__":
    unittest.main()
