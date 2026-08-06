from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from core.delivery import (
    append_resources,
    claim_baseline,
    completion_failure,
    resolve_transition_mode,
)
from core.workflow import load_workflow_definition
from core.workspace_settings import set_version_control


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
        task = repository_task(delivery_mode="standard")

        self.assertIsNone(self.blocked(task))

    def test_disabling_the_switch_does_not_release_an_already_locked_task(self):
        set_version_control(str(self.repo), enabled=False)

        self.assertIsNotNone(self.blocked(repository_task(candidate_sha=self.base)))
        self.assertIsNone(self.blocked(repository_task(delivery_mode="standard")))

    def test_a_fully_promoted_and_cleaned_task_passes(self):
        task = repository_task(
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
            self.checks(repository_task()),
            {"candidate_sha", "verified_sha", "promoted_sha"},
        )
        self.assertIn(
            "candidate_sha",
            self.checks(repository_task(candidate_sha="0" * 40, verified_sha=self.base, promoted_sha=self.base)),
        )

    def test_a_new_candidate_invalidates_the_earlier_verification(self):
        rebuilt = self.commit("rebuilt")
        git(self.repo, "update-ref", "refs/heads/main", self.base)
        task = repository_task(
            candidate_sha=rebuilt,
            verified_sha=self.base,
            promoted_sha=self.base,
        )

        self.assertIn("verified_candidate", self.checks(task))

    def test_a_target_branch_that_does_not_point_at_the_candidate_blocks(self):
        promoted = self.commit("feature")
        git(self.repo, "update-ref", "refs/heads/main", self.base)
        task = repository_task(
            candidate_sha=promoted,
            verified_sha=promoted,
            promoted_sha=promoted,
        )

        failure = self.blocked(task)
        self.assertIn("target_branch", {item["check"] for item in failure["failures"]})
        self.assertEqual(failure["target_branch"], "main")

    def test_a_declared_branch_that_still_exists_blocks(self):
        git(self.repo, "branch", "teamflow/TF-0100/task")
        task = repository_task(
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
        task = repository_task(
            candidate_sha=self.base,
            verified_sha=self.base,
            promoted_sha=self.base,
            delivery_resources=append_resources(None, {"worktrees": [str(worktree)]}),
        )

        failure = self.blocked(task)
        self.assertIn("declared_worktrees", {item["check"] for item in failure["failures"]})

        git(self.repo, "worktree", "remove", str(worktree))
        self.assertIsNone(self.blocked(task))

    def test_the_gate_only_runs_for_transitions_into_a_completion_state(self):
        task = repository_task()

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


if __name__ == "__main__":
    unittest.main()
