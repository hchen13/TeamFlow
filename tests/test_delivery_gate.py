from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from core.delivery import (
    append_resources,
    normalize_delivery_input,
    claim_baseline,
    completion_failure,
    resolve_transition_mode,
)
from core.workflow import load_workflow_definition
from core.teamflow_tools import cancel_task, claim_task, route_task, submit_task, update_task
from core.workspace_settings import set_version_control
from tests.test_teamflow_tools import FakeTaskBoard, assignment


ROOT = Path(__file__).resolve().parents[1]
DEFINITION = load_workflow_definition("software-development")


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(repo),
            "GIT_AUTHOR_NAME": "TeamFlow Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "TeamFlow Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    return result.stdout.strip()


def repository_task(**overrides: object) -> dict[str, object]:
    task = {
        "record_id": "recRepo",
        "task_id": "TF-0100",
        "title": "仓库交付任务",
        "status": "review",
        "delivery_mode": "repository",
        "target_branch": "main",
        "delivery_resources": append_resources(None, {"branches": [], "worktrees": []}),
    }
    task.update(overrides)
    return task


class DeliveryGateTest(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="delivery-", dir=ROOT / "tmp")
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        git(self.repo, "init", "--quiet", "--initial-branch=main")
        (self.repo / "README.md").write_text("teamflow\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "--quiet", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        set_version_control(str(self.repo), enabled=True)

    def task(self, **overrides: object) -> dict[str, object]:
        return repository_task(**{"base_sha": self.base, **overrides})

    def commit(self, message: str) -> str:
        (self.repo / f"{message}.txt").write_text(message, encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "--quiet", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def blocked(self, task: dict[str, object]) -> dict[str, object] | None:
        return completion_failure(str(self.repo), DEFINITION, task, {"status": "done"})

    def checks(self, task: dict[str, object]) -> set[str]:
        failure = self.blocked(task)
        return {item["check"] for item in (failure or {}).get("failures", [])}

    def test_a_standard_task_is_never_checked_against_the_repository(self):
        task = self.task(delivery_mode="standard")

        self.assertIsNone(self.blocked(task))

    def test_disabling_the_switch_does_not_release_an_already_locked_task(self):
        set_version_control(str(self.repo), enabled=False)

        self.assertIsNotNone(self.blocked(self.task(candidate_sha=self.base)))
        self.assertIsNone(self.blocked(self.task(delivery_mode="standard")))

    def test_a_fully_promoted_and_cleaned_task_passes(self):
        task = self.task(
            candidate_sha=self.base,
            verified_sha=self.base,
            promoted_sha=self.base,
            delivery_resources=append_resources(
                None,
                {"branches": ["teamflow/TF-0100/task"], "worktrees": [str(self.repo / "gone")]},
            ),
        )

        self.assertIsNone(self.blocked(task))

    def test_missing_or_unknown_shas_are_reported_per_field(self):
        self.assertEqual(
            self.checks(self.task()),
            {"candidate_sha", "verified_sha", "promoted_sha"},
        )
        self.assertIn(
            "candidate_sha",
            self.checks(self.task(candidate_sha="0" * 40, verified_sha=self.base, promoted_sha=self.base)),
        )

    def test_a_new_candidate_invalidates_the_earlier_verification(self):
        rebuilt = self.commit("rebuilt")
        git(self.repo, "update-ref", "refs/heads/main", self.base)
        task = self.task(
            candidate_sha=rebuilt,
            verified_sha=self.base,
            promoted_sha=self.base,
        )

        self.assertIn("verified_candidate", self.checks(task))

    def test_a_target_branch_that_does_not_point_at_the_candidate_blocks(self):
        promoted = self.commit("feature")
        git(self.repo, "update-ref", "refs/heads/main", self.base)
        task = self.task(
            candidate_sha=promoted,
            verified_sha=promoted,
            promoted_sha=promoted,
        )

        failure = self.blocked(task)
        self.assertIn("target_branch", {item["check"] for item in failure["failures"]})
        self.assertEqual(failure["target_branch"], "main")

    def test_a_declared_branch_that_still_exists_blocks(self):
        git(self.repo, "branch", "teamflow/TF-0100/task")
        task = self.task(
            candidate_sha=self.base,
            verified_sha=self.base,
            promoted_sha=self.base,
            delivery_resources=append_resources(None, {"branches": ["teamflow/TF-0100/task"]}),
        )

        failure = self.blocked(task)
        self.assertIn("declared_branches", {item["check"] for item in failure["failures"]})
        self.assertEqual(failure["leftover_resources"]["branches"], ["teamflow/TF-0100/task"])

    def test_a_declared_worktree_that_still_exists_blocks(self):
        worktree = self.repo / ".teamflow" / "worktrees" / "TF-0100" / "task"
        git(self.repo, "worktree", "add", "--detach", str(worktree), self.base)
        task = self.task(
            candidate_sha=self.base,
            verified_sha=self.base,
            promoted_sha=self.base,
            delivery_resources=append_resources(None, {"worktrees": [str(worktree)]}),
        )

        failure = self.blocked(task)
        self.assertIn("declared_worktrees", {item["check"] for item in failure["failures"]})

        git(self.repo, "worktree", "remove", str(worktree))
        self.assertIsNone(self.blocked(task))

    def test_a_legacy_relative_worktree_path_is_read_against_the_repository(self):
        worktree = self.repo / ".teamflow" / "worktrees" / "TF-0100" / "task"
        git(self.repo, "worktree", "add", "--detach", str(worktree), self.base)
        task = self.task(
            candidate_sha=self.base,
            verified_sha=self.base,
            promoted_sha=self.base,
            delivery_resources=json.dumps({
                "branches": [],
                "worktrees": [".teamflow/worktrees/TF-0100/task"],
            }),
        )

        elsewhere = Path(self.temporary.name).parent
        previous = os.getcwd()
        os.chdir(elsewhere)
        self.addCleanup(os.chdir, previous)

        self.assertIn("declared_worktrees", self.checks(task))

        git(self.repo, "worktree", "remove", str(worktree))
        self.assertIsNone(self.blocked(task))

    def test_the_gate_only_runs_for_transitions_into_a_completion_state(self):
        task = self.task()

        self.assertIsNone(completion_failure(str(self.repo), DEFINITION, task, {"status": "ready"}))
        self.assertIsNone(completion_failure(str(self.repo), DEFINITION, task, {"status": "canceled"}))
        self.assertIsNotNone(completion_failure(str(self.repo), DEFINITION, task, {"status": "done"}))

    def test_declared_resources_accumulate_and_cannot_be_erased(self):
        first = append_resources(None, {"branches": ["a"], "worktrees": ["/w/a"]})
        second = append_resources(first, {"branches": ["a", "b"]})

        self.assertEqual(json.loads(second)["branches"], ["a", "b"])
        self.assertEqual(json.loads(second)["worktrees"], ["/w/a"])
        self.assertEqual(json.loads(append_resources(second, {}))["branches"], ["a", "b"])

    def test_repository_mode_is_rejected_when_version_control_is_disabled(self):
        set_version_control(str(self.repo), enabled=False)

        with self.assertRaisesRegex(ValueError, "version control disabled"):
            resolve_transition_mode(
                str(self.repo),
                DEFINITION,
                {"status": "backlog"},
                "repository",
                target_state="ready",
            )

    def test_the_mode_is_locked_once_the_task_leaves_the_initial_state(self):
        backlog = {"status": "backlog", "delivery_mode": "standard"}
        self.assertEqual(
            resolve_transition_mode(str(self.repo), DEFINITION, backlog, "repository", target_state="ready"),
            "repository",
        )

        running = {"status": "in_progress", "delivery_mode": "standard"}
        with self.assertRaisesRegex(ValueError, "locked to standard"):
            resolve_transition_mode(str(self.repo), DEFINITION, running, "repository", target_state="review")

    def test_the_first_claim_pins_the_target_branch_and_its_exact_sha(self):
        moved = self.commit("later")
        task = repository_task(status="ready", target_branch=None, base_sha=None)

        baseline = claim_baseline(str(self.repo), task)

        self.assertEqual(baseline, {"target_branch": "main", "base_sha": moved})
        self.assertEqual(claim_baseline(str(self.repo), {**task, **baseline}), {})
        self.assertEqual(claim_baseline(str(self.repo), {**task, "delivery_mode": "standard"}), {})

    def test_a_legacy_task_without_a_mode_may_be_filled_in_once(self):
        legacy = {"status": "review", "delivery_mode": None}

        self.assertEqual(
            resolve_transition_mode(str(self.repo), DEFINITION, legacy, "standard", target_state="ready"),
            "standard",
        )
        with self.assertRaisesRegex(ValueError, "choose a delivery_mode"):
            resolve_transition_mode(str(self.repo), DEFINITION, legacy, None, target_state="ready")

    def test_system_owned_fields_cannot_be_supplied_by_an_agent(self):
        for field in ("target_branch", "base_sha"):
            with self.assertRaisesRegex(ValueError, "set by TeamFlow"):
                normalize_delivery_input({field: "main"}, workspace=str(self.repo))

    def test_only_full_commit_ids_are_accepted_as_delivery_shas(self):
        for rejected in ("main", "HEAD", "v1.0", self.base[:8], "HEAD~1", "main@{0}", "A" * 40, "a" * 41):
            with self.assertRaises(ValueError):
                normalize_delivery_input({"candidate_sha": rejected}, workspace=str(self.repo))

        for accepted in (self.base, "c" * 64):
            normalized = normalize_delivery_input({"candidate_sha": accepted}, workspace=str(self.repo))
            self.assertEqual(normalized["candidate_sha"], accepted)

    def test_a_full_id_that_is_not_a_commit_in_this_repository_blocks(self):
        task = self.task(
            candidate_sha="b" * 40,
            verified_sha="b" * 40,
            promoted_sha="b" * 40,
        )

        self.assertIn("candidate_sha", self.checks(task))

    def test_main_may_move_on_after_the_candidate_is_promoted(self):
        promoted = self.commit("feature")
        self.commit("unrelated")
        task = self.task(
            base_sha=self.base,
            candidate_sha=promoted,
            verified_sha=promoted,
            promoted_sha=promoted,
        )

        self.assertIsNone(self.blocked(task))

    def test_a_task_without_a_pinned_baseline_cannot_complete(self):
        promoted = self.commit("feature")
        task = self.task(
            base_sha=None,
            candidate_sha=promoted,
            verified_sha=promoted,
            promoted_sha=promoted,
        )

        self.assertIn("base_sha", self.checks(task))
        self.assertIsNone(self.blocked({**task, "base_sha": self.base}))

    def test_a_baseline_that_is_not_a_commit_here_cannot_complete(self):
        promoted = self.commit("feature")
        task = self.task(
            base_sha="d" * 40,
            candidate_sha=promoted,
            verified_sha=promoted,
            promoted_sha=promoted,
        )

        self.assertIn("base_sha", self.checks(task))

    def test_a_replacement_object_cannot_forge_the_baseline_ancestry(self):
        promoted = self.commit("feature")
        orphan = git(self.repo, "commit-tree", "-m", "orphan", f"{self.base}^{{tree}}")
        # Rewriting the candidate itself is what forges ancestry: the replacement
        # commit claims the orphan as its parent, so an ordinary walk believes it.
        forged = git(
            self.repo, "commit-tree", "-m", "forged", "-p", orphan, f"{promoted}^{{tree}}"
        )
        git(self.repo, "replace", "-f", promoted, forged)
        task = self.task(
            base_sha=orphan,
            candidate_sha=promoted,
            verified_sha=promoted,
            promoted_sha=promoted,
        )

        self.assertIn("base_ancestry", self.checks(task))

    def test_a_sha_256_repository_records_its_longer_commit_ids(self):
        sha256 = Path(self.temporary.name) / "sha256"
        sha256.mkdir()
        git(sha256, "init", "--quiet", "--initial-branch=main", "--object-format=sha256")
        (sha256 / "README.md").write_text("teamflow\n", encoding="utf-8")
        git(sha256, "add", "README.md")
        git(sha256, "commit", "--quiet", "-m", "base")
        base = git(sha256, "rev-parse", "HEAD")
        set_version_control(str(sha256), enabled=True)
        self.assertEqual(len(base), 64)

        task = repository_task(
            base_sha=base,
            candidate_sha=base,
            verified_sha=base,
            promoted_sha=base,
        )

        self.assertIsNone(completion_failure(str(sha256), DEFINITION, task, {"status": "done"}))

    def test_a_base_that_is_not_an_ancestor_of_the_candidate_blocks(self):
        promoted = self.commit("feature")
        orphan = git(self.repo, "commit-tree", "-m", "orphan", f"{self.base}^{{tree}}")
        task = self.task(
            base_sha=orphan,
            candidate_sha=promoted,
            verified_sha=promoted,
            promoted_sha=promoted,
        )

        self.assertIn("base_ancestry", self.checks(task))

    def test_resources_reject_unknown_keys_and_non_string_entries(self):
        with self.assertRaisesRegex(ValueError, "only accepts branches, worktrees"):
            normalize_delivery_input({"resources": {"tags": []}}, workspace=str(self.repo))
        with self.assertRaisesRegex(ValueError, "must be an array of strings"):
            normalize_delivery_input({"resources": {"branches": "one"}}, workspace=str(self.repo))
        with self.assertRaisesRegex(ValueError, "must be an array of strings"):
            normalize_delivery_input({"resources": {"worktrees": [1]}}, workspace=str(self.repo))
        with self.assertRaisesRegex(ValueError, "resources must be an object"):
            normalize_delivery_input({"resources": []}, workspace=str(self.repo))

    def test_a_relative_worktree_path_resolves_against_the_workspace(self):
        normalized = normalize_delivery_input(
            {"resources": {"worktrees": [".teamflow/worktrees/T-1/task"]}},
            workspace=str(self.repo),
        )

        self.assertEqual(
            normalized["resources"]["worktrees"],
            [str((self.repo / ".teamflow/worktrees/T-1/task").resolve())],
        )

    def test_a_repository_that_cannot_be_read_fails_closed(self):
        broken = self.repo / "vanished"
        task = self.task(
            candidate_sha=self.base,
            verified_sha=self.base,
            promoted_sha=self.base,
            delivery_resources=append_resources(None, {"branches": ["teamflow/T-1/task"]}),
        )

        failure = completion_failure(str(broken), DEFINITION, task, {"status": "done"})

        self.assertIsNotNone(failure)
        self.assertEqual([item["check"] for item in failure["failures"]], ["git_probe"])

    def test_a_missing_delivery_mode_blocks_entry_into_a_completion_state(self):
        legacy = {"status": "review", "delivery_mode": None}

        with self.assertRaisesRegex(ValueError, "before this task enters done"):
            resolve_transition_mode(str(self.repo), DEFINITION, legacy, None, target_state="done")

class DeliveryWriteTest(unittest.TestCase):
    """Facts are checked when they are recorded, not only when the task completes."""

    def setUp(self) -> None:
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="delivery-write-", dir=ROOT / "tmp")
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        git(self.repo, "init", "--quiet", "--initial-branch=main")
        (self.repo / "README.md").write_text("teamflow\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "--quiet", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        set_version_control(str(self.repo), enabled=True)
        self.tl = assignment("tl", "agent_tl") | {"workspace_root": str(self.repo)}
        self.board = FakeTaskBoard({
            "record_id": "recRepo",
            "task_id": "TF-0100",
            "title": "仓库交付任务",
            "status": "in_progress",
            "delivery_mode": "repository",
            "target_branch": "main",
            "base_sha": self.base,
            "type": "development",
            "priority": "P1",
            "role": "tl",
            "description": "做事。",
            "acceptance_criteria": "通过。",
            "agent": "TL Agent",
            "agent_id": "agent_tl",
            "progress": None,
            "next_action": None,
            "result_evidence": None,
            "blocked_reason": None,
            "waiting_on": None,
        })

    def record(self, sha: str) -> dict[str, object]:
        read, write = self.board.patches()
        with read, write:
            return update_task(self.tl, record_id="recRepo", delivery={"candidate_sha": sha})

    def test_a_commit_id_that_does_not_exist_here_is_refused_on_the_spot(self):
        refused = self.record("e" * 40)

        self.assertFalse(refused["ok"])
        self.assertIsNone(self.board.task.get("candidate_sha"))

    def test_a_workflow_that_declares_the_system_fields_writable_is_still_refused(self):
        forced = deepcopy(DEFINITION)
        for rule in forced["lifecycle"]["actions"]["update"]["rules"]:
            rule["writable_fields"] = [
                *rule["writable_fields"], "target_branch", "base_sha", "delivery_resources"
            ]
        ledger = append_resources(None, {"branches": ["teamflow/TF-0100/task"]})
        self.board.task["delivery_resources"] = ledger

        read, write = self.board.patches()
        with read, write, patch(
            "core.teamflow_tools.workflow_definition_for_assignment", return_value=forced
        ), patch(
            "core.workflow_lifecycle.workflow_definition_for_assignment", return_value=forced
        ):
            overwritten = update_task(
                self.tl,
                record_id="recRepo",
                fields={"base_sha": "f" * 40, "target_branch": "release"},
            )
            cleared = update_task(
                self.tl,
                record_id="recRepo",
                fields={"delivery_resources": ""},
            )

        self.assertFalse(overwritten["ok"])
        self.assertFalse(cleared["ok"])
        self.assertEqual(self.board.task["base_sha"], self.base)
        self.assertEqual(self.board.task["target_branch"], "main")
        self.assertEqual(self.board.task["delivery_resources"], ledger)

    def test_a_real_commit_is_recorded(self):
        recorded = self.record(self.base)

        self.assertTrue(recorded["ok"], recorded.get("error"))
        self.assertEqual(self.board.task["candidate_sha"], self.base)


class LegacyModeGateTest(unittest.TestCase):
    """A card created before delivery_mode existed must pick one before it moves on."""

    def setUp(self) -> None:
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="legacy-mode-", dir=ROOT / "tmp")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = self.temporary.name
        set_version_control(self.workspace, enabled=True)
        self.pm = assignment("pm", "agent_pm") | {"workspace_root": self.workspace}
        self.tl = assignment("tl", "agent_tl") | {"workspace_root": self.workspace}

    def board(self, **overrides: object) -> FakeTaskBoard:
        task = {
            "record_id": "recLegacy",
            "task_id": "TF-0900",
            "title": "旧卡",
            "status": "ready",
            "delivery_mode": None,
            "type": "development",
            "priority": "P1",
            "role": "tl",
            "description": "旧数据。",
            "acceptance_criteria": "通过。",
            "context": None,
            "dependencies": None,
            "progress": None,
            "next_action": None,
            "result_evidence": "既有证据。",
            "blocked_reason": None,
            "waiting_on": None,
            "agent": None,
            "agent_id": None,
        }
        task.update(overrides)
        return FakeTaskBoard(task)

    def test_claim_does_not_move_a_legacy_card_into_progress(self):
        board = self.board()
        read, write = board.patches()
        with read, write:
            claimed = claim_task(self.tl, record_id="recLegacy")

        self.assertFalse(claimed["ok"])
        self.assertEqual(board.task["status"], "ready")
        self.assertIsNone(board.task["agent_id"])

    def test_route_submit_and_review_are_all_blocked(self):
        board = self.board()
        read, write = board.patches()
        with read, write:
            self.assertFalse(route_task(self.pm, record_id="recLegacy", role="qa")["ok"])
        self.assertEqual(board.task["status"], "ready")

        board = self.board(status="in_progress", agent="TL Agent", agent_id="agent_tl")
        read, write = board.patches()
        with read, write:
            submitted = submit_task(
                self.tl,
                record_id="recLegacy",
                outcome="completed",
                result_evidence="做完了。",
            )
        self.assertFalse(submitted["ok"])
        self.assertEqual(board.task["status"], "in_progress")

        board = self.board(status="review", agent="TL Agent", agent_id="agent_tl")
        read, write = board.patches()
        with read, write:
            from core.teamflow_tools import review_task

            approved = review_task(
                self.pm,
                record_id="recLegacy",
                decision="approve",
                result_evidence="验收通过。",
            )
        self.assertFalse(approved["ok"])
        self.assertEqual(board.task["status"], "review")

    def test_a_delivery_only_update_records_the_mode_and_unblocks_the_card(self):
        board = self.board()
        read, write = board.patches()
        with read, write:
            recorded = update_task(
                self.pm,
                record_id="recLegacy",
                delivery={"delivery_mode": "standard"},
            )
            self.assertTrue(recorded["ok"], recorded.get("error"))
            self.assertEqual(board.task["delivery_mode"], "standard")
            self.assertEqual(board.task["status"], "ready")

            claimed = claim_task(self.tl, record_id="recLegacy")

        self.assertTrue(claimed["ok"], claimed.get("error"))
        self.assertEqual(board.task["status"], "in_progress")

    def test_cancelling_a_legacy_card_never_needs_a_mode(self):
        board = self.board(status="review")
        read, write = board.patches()
        with read, write:
            canceled = cancel_task(
                self.pm,
                record_id="recLegacy",
                result_evidence="不做了。",
                confirmed=True,
            )

        self.assertTrue(canceled["ok"], canceled.get("error"))
        self.assertEqual(board.task["status"], "canceled")

    def test_standard_delivery_refuses_repository_only_facts(self):
        board = self.board(delivery_mode="standard")
        read, write = board.patches()
        with read, write:
            rejected = update_task(
                self.pm,
                record_id="recLegacy",
                delivery={"candidate_sha": "a" * 40},
            )
            resources = update_task(
                self.pm,
                record_id="recLegacy",
                delivery={"resources": {"branches": ["x"]}},
            )

        self.assertFalse(rejected["ok"])
        self.assertFalse(resources["ok"])
        self.assertIsNone(board.task.get("candidate_sha"))


if __name__ == "__main__":
    unittest.main()
