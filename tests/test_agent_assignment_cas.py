from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import resolve_workspace_paths
from core.db import connect, init_workspace, register_agent, unregister_agent, update_agent


ROOT = Path(__file__).resolve().parents[1]


def codex_thread(thread_id: str, *, include_turns: bool = False) -> dict[str, object]:
    return {
        "id": thread_id,
        "name": f"Session {thread_id}",
        "status": {"type": "notLoaded"},
        "cwd": None,
    }


class AgentAssignmentRevisionTest(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="agent-cas-", dir=ROOT / "tmp")
        self.workspace = self.temp.name
        init_workspace(self.workspace)
        self.threads = patch("core.db.read_codex_thread", side_effect=codex_thread)
        self.threads.start()
        self.agent_id = register_agent(
            self.workspace,
            role="pm",
            harness_type="codex",
            session_id="thread-one",
        )["agent_id"]

    def tearDown(self):
        self.threads.stop()
        self.temp.cleanup()

    def revision(self) -> int:
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            return int(
                conn.execute(
                    "SELECT assignment_revision FROM agents WHERE id = ?", (self.agent_id,)
                ).fetchone()[0]
            )

    def session(self) -> str:
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            return str(
                conn.execute(
                    "SELECT session_id FROM agents WHERE id = ?", (self.agent_id,)
                ).fetchone()[0]
            )

    def test_an_update_with_the_checked_revision_succeeds(self):
        update_agent(
            self.workspace,
            agent_id=self.agent_id,
            session_id="thread-two",
            expected_revision=self.revision(),
        )

        self.assertEqual(self.session(), "thread-two")

    def test_an_update_from_a_stale_revision_is_rejected(self):
        stale = self.revision()
        update_agent(self.workspace, agent_id=self.agent_id, session_id="thread-two")
        self.assertEqual(self.session(), "thread-two")

        with self.assertRaises(ValueError) as failure:
            update_agent(
                self.workspace,
                agent_id=self.agent_id,
                session_id="thread-three",
                expected_revision=stale,
            )

        self.assertIn("agent assignment changed", str(failure.exception))
        self.assertEqual(self.session(), "thread-two")

    def test_a_removal_from_a_stale_revision_is_rejected(self):
        stale = self.revision()
        update_agent(self.workspace, agent_id=self.agent_id, session_id="thread-two")

        with self.assertRaises(ValueError) as failure:
            unregister_agent(self.workspace, agent_id=self.agent_id, expected_revision=stale)

        self.assertIn("agent assignment changed", str(failure.exception))
        self.assertEqual(self.session(), "thread-two")

        unregister_agent(
            self.workspace,
            agent_id=self.agent_id,
            expected_revision=self.revision(),
        )
        with connect(resolve_workspace_paths(self.workspace).db_path) as conn:
            self.assertIsNone(
                conn.execute("SELECT 1 FROM agents WHERE id = ?", (self.agent_id,)).fetchone()
            )

    def test_a_selector_removal_cannot_claim_a_revision(self):
        update_agent(self.workspace, agent_id=self.agent_id, session_id="thread-two")
        stale = self.revision() - 1

        with self.assertRaises(ValueError) as failure:
            unregister_agent(
                self.workspace,
                role="pm",
                workflow="software-development",
                harness_type="codex",
                session_id="thread-two",
                expected_revision=stale,
            )

        self.assertIn("expected_revision requires agent_id", str(failure.exception))
        self.assertEqual(self.session(), "thread-two")

        # The current revision is refused too: a selector may match a different agent than the one
        # the caller checked, so the combination is rejected outright rather than guessed at.
        with self.assertRaises(ValueError):
            unregister_agent(
                self.workspace,
                role="pm",
                workflow="software-development",
                harness_type="codex",
                session_id="thread-two",
                expected_revision=self.revision(),
            )
        self.assertEqual(self.session(), "thread-two")

    def test_a_selector_removal_without_a_revision_still_works(self):
        update_agent(self.workspace, agent_id=self.agent_id, session_id="thread-two")

        removed = unregister_agent(
            self.workspace,
            role="pm",
            workflow="software-development",
            harness_type="codex",
            session_id="thread-two",
        )

        self.assertEqual(removed["deleted"], 1)

    def test_callers_that_pass_no_revision_keep_working(self):
        update_agent(self.workspace, agent_id=self.agent_id, session_id="thread-two")
        self.assertEqual(self.session(), "thread-two")
        self.assertEqual(unregister_agent(self.workspace, agent_id=self.agent_id)["deleted"], 1)
        self.assertEqual(unregister_agent(self.workspace, agent_id=self.agent_id)["deleted"], 0)

    def test_a_removal_of_a_vanished_agent_reports_the_changed_assignment(self):
        revision = self.revision()
        unregister_agent(self.workspace, agent_id=self.agent_id)

        with self.assertRaises(ValueError) as failure:
            unregister_agent(self.workspace, agent_id=self.agent_id, expected_revision=revision)

        self.assertIn("agent assignment changed", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
