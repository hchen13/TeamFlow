from __future__ import annotations

import unittest
from copy import deepcopy
from unittest.mock import patch

from core import teamflow_tools
from core.teamflow_tools import claim_task, create_task, route_task


def assignment(role: str, agent: str) -> dict[str, object]:
    return {
        "agent_id": agent,
        "agent_name": f"{role.upper()} Agent",
        "workspace_root": "/workspace",
        "workflow_key": "software-development",
        "role_key": role,
    }


READY_TASK = {
    "record_id": "recReady",
    "task_id": "TF-0059",
    "title": "Ship the change",
    "status": "ready",
    "type": "development",
    "priority": "P1",
    "role": "tl",
    "agent": None,
    "agent_id": None,
    "description": "Do the work",
    "context": None,
    "acceptance_criteria": "Tests pass",
    "dependencies": None,
    "progress": None,
    "next_action": None,
    "result_evidence": None,
    "blocked_reason": None,
    "waiting_on": None,
}


class EventuallyConsistentBoard:
    """Lark applies the write, then serves the record as it was for a while.

    This is the shape of the real failure: the PUT succeeds, but the write response and the reads
    that follow it still describe the previous revision.
    """

    def __init__(self, task: dict[str, object], *, stale_reads: int) -> None:
        self.task = deepcopy(task)
        self.visible = deepcopy(task)
        self.stale_reads = stale_reads
        self.writes = 0
        self.client_tokens: list[str | None] = []

    def _settle(self) -> None:
        if self.stale_reads > 0:
            self.stale_reads -= 1
        else:
            self.visible = deepcopy(self.task)

    def read(self, workspace: str, *, record_id: str) -> dict[str, object]:
        self._settle()
        if self.visible["record_id"] != record_id:
            raise ValueError("task not found")
        return {"ok": True, "task": deepcopy(self.visible)}

    def write(
        self,
        workspace: str,
        *,
        task: dict[str, object],
        record_id: str | None = None,
        client_token: str | None = None,
    ) -> dict[str, object]:
        self.writes += 1
        self.client_tokens.append(client_token)
        if record_id is None:
            self.task = {**deepcopy(READY_TASK), "record_id": "recNew", **task}
            # A brand new record exists, but its fields have not materialised yet.
            self.visible = {**{key: None for key in self.task}, "record_id": "recNew"}
        else:
            self.task.update(task)
        if self.stale_reads == 0:
            self.visible = deepcopy(self.task)
        # The write response echoes the revision Lark can currently see.
        return {"ok": True, "created": record_id is None, "task": deepcopy(self.visible)}

    def patches(self):
        return (
            patch("core.teamflow_tools.get_lark_task", side_effect=self.read),
            patch("core.teamflow_tools.upsert_lark_task", side_effect=self.write),
            patch("core.teamflow_tools._sleep"),
        )


class WriteVisibilityTest(unittest.TestCase):
    def setUp(self):
        self.tl = assignment("tl", "agent_tl")
        self.pm = assignment("pm", "agent_pm")

    def test_a_claim_waits_for_the_write_to_become_visible(self):
        board = EventuallyConsistentBoard(READY_TASK, stale_reads=4)
        read, write, sleeper = board.patches()

        with read, write, sleeper as slept:
            result = claim_task(self.tl, record_id="recReady")

        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["task"]["status"], "in_progress")
        self.assertEqual(result["task"]["agent_id"], "agent_tl")
        self.assertEqual(result["task"]["agent"], "TL Agent")
        self.assertEqual(result["transition"], {"from": "ready", "to": "in_progress"})
        self.assertFalse(result["already_applied"])
        self.assertNotIn("claim", [action["action"] for action in result["available_actions"]])
        self.assertEqual(board.writes, 1, "the record must not be written more than once")
        self.assertTrue(slept.called, "a stale read must be retried rather than trusted")

    def test_a_claim_that_never_becomes_visible_is_not_reported_as_success(self):
        board = EventuallyConsistentBoard(READY_TASK, stale_reads=10_000)
        read, write, sleeper = board.patches()

        with read, write, sleeper, patch.object(teamflow_tools, "WRITE_VISIBILITY_TIMEOUT", 0.05):
            result = claim_task(self.tl, record_id="recReady")

        self.assertFalse(result["ok"])
        error = result["error"]
        self.assertEqual(error["code"], "write_not_visible")
        self.assertIs(error["retryable"], True)
        self.assertEqual(error["current_state"], "ready")
        self.assertIn("status", error["details"]["pending_fields"])
        self.assertIn("agent_id", error["details"]["pending_fields"])
        self.assertEqual(error["details"]["expected_state"], "in_progress")
        self.assertIn("get_task", error["message"])
        self.assertIn("claim", [action["action"] for action in error["available_actions"]])

    def test_a_write_that_lands_late_is_recognised_as_already_applied(self):
        board = EventuallyConsistentBoard(READY_TASK, stale_reads=10_000)
        read, write, sleeper = board.patches()

        with read, write, sleeper, patch.object(teamflow_tools, "WRITE_VISIBILITY_TIMEOUT", 0.05):
            timed_out = claim_task(self.tl, record_id="recReady")
        self.assertFalse(timed_out["ok"])
        self.assertEqual(board.writes, 1)

        # The write did land; the board simply took longer than the caller could wait.
        board.stale_reads = 0
        with read, write, sleeper:
            retried = claim_task(self.tl, record_id="recReady", invocation_id="invocation-claim")

        self.assertTrue(retried["ok"])
        self.assertTrue(retried["already_applied"])
        self.assertEqual(retried["task"]["agent_id"], "agent_tl")
        self.assertEqual(board.writes, 1, "an already applied claim must not write again")

    def test_the_write_response_is_trusted_when_it_already_shows_the_patch(self):
        board = EventuallyConsistentBoard(READY_TASK, stale_reads=0)
        read, write, sleeper = board.patches()

        with read, write, sleeper as slept:
            result = claim_task(self.tl, record_id="recReady")

        self.assertTrue(result["ok"])
        self.assertEqual(result["task"]["status"], "in_progress")
        self.assertFalse(slept.called, "a complete write response needs no reread")

    def test_other_mutations_share_the_same_visibility_boundary(self):
        board = EventuallyConsistentBoard(READY_TASK, stale_reads=3)
        read, write, sleeper = board.patches()

        with read, write, sleeper:
            routed = route_task(self.pm, record_id="recReady", role="qa")

        self.assertTrue(routed["ok"], routed.get("error"))
        self.assertEqual(routed["task"]["role"], "qa")

        board = EventuallyConsistentBoard(READY_TASK, stale_reads=10_000)
        read, write, sleeper = board.patches()
        with read, write, sleeper, patch.object(teamflow_tools, "WRITE_VISIBILITY_TIMEOUT", 0.05):
            blocked = route_task(self.pm, record_id="recReady", role="qa")

        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"]["code"], "write_not_visible")

    def test_creating_a_task_also_waits_for_the_record_to_be_visible(self):
        board = EventuallyConsistentBoard(READY_TASK, stale_reads=10_000)
        read, write, sleeper = board.patches()

        with read, write, sleeper, patch.object(teamflow_tools, "WRITE_VISIBILITY_TIMEOUT", 0.05):
            created = create_task(
                self.pm,
                title="A brand new task",
                task_type="development",
                priority="P2",
                role="tl",
                description="Something to do",
                acceptance_criteria="It works",
            )

        self.assertFalse(created["ok"])
        self.assertEqual(created["error"]["code"], "write_not_visible")


if __name__ == "__main__":
    unittest.main()
