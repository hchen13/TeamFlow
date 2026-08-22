from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.teamflow_tools import (
    block_task,
    cancel_task,
    claim_task,
    create_task,
    prepare_runtime_action,
    review_task,
    route_task,
    submit_task,
    update_task,
    workflow_contract,
)
from core.task_execution import TaskExecutionRuntime
from core.workflow_validation import WORKFLOW_SCHEMA_VERSION
from core.workflow import (
    load_workflow_definition,
    task_option_definitions,
    validate_workflow_definition,
)


def assignment(
    role: str,
    agent: str,
    workflow: str = "software-development",
) -> dict[str, object]:
    return {
        "agent_id": agent,
        "agent_name": f"{role.upper()} Agent",
        "workspace_root": "/workspace",
        "workflow_key": workflow,
        "role_key": role,
    }


class FakeTaskBoard:
    def __init__(self, task: dict[str, object] | None = None):
        self.task = deepcopy(task) if task else None
        self.client_tokens: list[str | None] = []

    def read(self, workspace: str, *, record_id: str) -> dict[str, object]:
        if self.task is None or self.task["record_id"] != record_id:
            raise ValueError("task not found")
        return {"ok": True, "task": deepcopy(self.task)}

    def write(
        self,
        workspace: str,
        *,
        task: dict[str, object],
        record_id: str | None = None,
        client_token: str | None = None,
    ) -> dict[str, object]:
        self.client_tokens.append(client_token)
        if record_id is None:
            self.task = {
                "record_id": "recFlow",
                "task_id": "TF-0001",
                "agent": None,
                "agent_id": None,
                "progress": None,
                "next_action": None,
                "result_evidence": None,
                "blocked_reason": None,
                "waiting_on": None,
                **task,
            }
        else:
            if self.task is None or self.task["record_id"] != record_id:
                raise ValueError("task not found")
            self.task.update(task)
        return {"ok": True, "task": deepcopy(self.task)}

    def patches(self):
        return (
            patch("core.teamflow_tools.get_lark_task", side_effect=self.read),
            patch("core.teamflow_tools.upsert_lark_task", side_effect=self.write),
        )


class WorkflowActionTest(unittest.TestCase):
    def setUp(self):
        self.pm = assignment("pm", "agent_pm")
        self.tl = assignment("tl", "agent_tl")
        self.qa = assignment("qa", "agent_qa")
        self.owner = assignment("owner", "agent_owner", "general-task")
        self.executor = assignment("executor", "agent_executor", "general-task")
        self.reviewer = assignment("reviewer", "agent_reviewer", "general-task")

    def test_definition_drives_statuses_dispatch_and_actions(self):
        definition = load_workflow_definition("software-development")
        statuses = {
            item["key"]: item
            for item in task_option_definitions(definition)["status"]
        }

        self.assertEqual(definition["schema_version"], WORKFLOW_SCHEMA_VERSION)
        self.assertEqual(
            set(definition["lifecycle"]["actions"]),
            {"create", "update", "route", "claim", "submit", "block", "review", "cancel"},
        )
        self.assertEqual(statuses["ready"]["labels"]["zh-CN"], "可执行")
        states = {
            state["key"]: state["dispatch"]
            for state in definition["lifecycle"]["states"]
        }
        self.assertEqual(states["ready"], "task_role")
        self.assertEqual(states["review"], "coordinator")
        self.assertEqual(states["done"], "none")

        tl_contract = workflow_contract(self.tl)
        tl_actions = {
            action["action"]: action
            for action in tl_contract["actions"]
        }
        self.assertNotIn("create", tl_actions)
        self.assertNotIn("route", tl_actions)
        self.assertNotIn("review", tl_actions)
        self.assertNotIn("cancel", tl_actions)
        self.assertEqual(
            tl_actions["submit"]["options"][0]["allowed_values"],
            {},
        )

        pm_contract = workflow_contract(self.pm)
        pm_actions = {
            action["action"]: action
            for action in pm_contract["actions"]
        }
        self.assertEqual(
            pm_actions["route"]["options"][0]["allowed_values"]["role"],
            ["pm", "tl", "qa", "design"],
        )
        self.assertTrue(
            pm_actions["cancel"]["options"][0]["confirmation_required"]
        )
        self.assertEqual(
            pm_contract["runtime_actions"],
            [{
                "action": "stop_execution",
                "tool": "stop_task_execution",
                "name": "停止执行",
                "states": ["in_progress"],
                "required_fields": ["reason"],
                "required_task_fields": ["agent_id"],
                "confirmation_required": True,
                "runtime_facts": ["execution_stopped"],
            }],
        )
        self.assertEqual(tl_contract["runtime_actions"], [])

    def test_definition_rejects_terminal_sources_and_protected_defaults(self):
        path = Path("/tmp/software-development/workflow.json")
        terminal_source = deepcopy(load_workflow_definition("software-development"))
        terminal_source["lifecycle"]["actions"]["route"]["rules"][0]["from"] = ["done"]
        with self.assertRaisesRegex(ValueError, "cannot leave a terminal state"):
            validate_workflow_definition(terminal_source, path)

        protected_default = deepcopy(load_workflow_definition("software-development"))
        protected_default["lifecycle"]["actions"]["create"]["rules"][0]["defaults"]["status"] = "backlog"
        with self.assertRaisesRegex(ValueError, "contains a protected field"):
            validate_workflow_definition(protected_default, path)

        unsupported_runtime_input = deepcopy(
            load_workflow_definition("software-development")
        )
        unsupported_runtime_input["runtime_actions"]["stop_execution"][
            "required_inputs"
        ].append("unsupported")
        with self.assertRaisesRegex(ValueError, "required_inputs is invalid"):
            validate_workflow_definition(unsupported_runtime_input, path)

        missing_dispatch_instruction = deepcopy(
            load_workflow_definition("software-development")
        )
        del next(
            state
            for state in missing_dispatch_instruction["lifecycle"]["states"]
            if state["key"] == "ready"
        )["dispatch_instructions"]
        with self.assertRaisesRegex(ValueError, "dispatch_instructions"):
            validate_workflow_definition(missing_dispatch_instruction, path)

    def test_complete_software_delivery_flow(self):
        board = FakeTaskBoard()
        read, write = board.patches()
        with read, write:
            created = create_task(
                self.pm,
                title="实现排序算法",
                task_type="development",
                priority="P1",
                description="在临时目录中实现排序算法。",
                acceptance_criteria="测试覆盖空数组、重复值和逆序输入。",
                delivery_mode="standard",
            )
            self.assertTrue(created["ok"])
            self.assertEqual(created["task"]["status"], "backlog")
            self.assertEqual(created["task"]["role"], "tl")

            routed = route_task(self.pm, record_id="recFlow", role="tl")
            self.assertEqual(routed["transition"], {"from": "backlog", "to": "ready"})

            claimed = claim_task(self.tl, record_id="recFlow")
            self.assertEqual(claimed["transition"], {"from": "ready", "to": "in_progress"})
            self.assertEqual(claimed["task"]["agent_id"], "agent_tl")

            submitted = submit_task(
                self.tl,
                record_id="recFlow",
                outcome="completed",
                result_evidence="实现完成，单元测试通过。",
            )
            self.assertEqual(submitted["transition"], {"from": "in_progress", "to": "review"})

            qa_routed = review_task(
                self.pm,
                record_id="recFlow",
                decision="send_to_qa",
                result_evidence="代码评审通过，转 QA 验证。",
                next_action="执行验收测试。",
            )
            self.assertEqual(qa_routed["task"]["status"], "ready")
            self.assertEqual(qa_routed["task"]["role"], "qa")
            self.assertIsNone(qa_routed["task"]["agent_id"])

            qa_claimed = claim_task(self.qa, record_id="recFlow")
            self.assertEqual(qa_claimed["task"]["agent_id"], "agent_qa")

            qa_submitted = submit_task(
                self.qa,
                record_id="recFlow",
                outcome="passed",
                result_evidence="验收用例全部通过。",
                progress="QA 已完成全部验收用例。",
                next_action="请 PM 进行最终验收。",
            )
            self.assertEqual(qa_submitted["task"]["status"], "review")
            self.assertEqual(qa_submitted["task"]["role"], "qa")
            self.assertEqual(qa_submitted["task"]["agent"], "QA Agent")
            self.assertEqual(qa_submitted["task"]["agent_id"], "agent_qa")
            self.assertEqual(
                qa_submitted["task"]["progress"],
                "QA 已完成全部验收用例。",
            )
            self.assertEqual(
                qa_submitted["task"]["next_action"],
                "请 PM 进行最终验收。",
            )

            completed = review_task(
                self.pm,
                record_id="recFlow",
                decision="approve",
                result_evidence="PM 确认验收通过。",
            )
            self.assertEqual(completed["task"]["status"], "done")
            for field in ("role", "agent", "agent_id", "progress", "next_action"):
                self.assertIsNone(completed["task"][field])
            self.assertEqual(
                completed["task"]["result_evidence"],
                "PM 确认验收通过。",
            )
            self.assertEqual(completed["available_actions"], [])

    def test_review_hands_off_to_tl_and_keeps_existing_review_decisions(self):
        reviewed = {
            "record_id": "recHandoff",
            "task_id": "TF-0042",
            "title": "集成并清理",
            "status": "review",
            "delivery_mode": "standard",
            "type": "development",
            "priority": "P1",
            "role": "qa",
            "agent": "QA Agent",
            "agent_id": "agent_qa",
            "description": "把已通过 QA 的候选晋升到目标分支。",
            "context": None,
            "acceptance_criteria": "目标分支 HEAD 等于已验证的候选。",
            "dependencies": None,
            "progress": None,
            "next_action": None,
            "result_evidence": "QA 验证通过。",
            "blocked_reason": None,
            "waiting_on": None,
        }

        for actor in (self.tl, self.qa):
            board = FakeTaskBoard(reviewed)
            read, write = board.patches()
            with read, write:
                denied = route_task(actor, record_id="recHandoff", role="tl")
            self.assertFalse(denied["ok"])
            self.assertEqual(board.task["status"], "review")
            self.assertEqual(board.task["agent_id"], "agent_qa")

        board = FakeTaskBoard(reviewed)
        read, write = board.patches()
        with read, write:
            handed_off = route_task(self.pm, record_id="recHandoff", role="tl")
            self.assertTrue(handed_off["ok"], handed_off.get("error"))
            self.assertEqual(handed_off["transition"], {"from": "review", "to": "ready"})
            self.assertEqual(handed_off["task"]["role"], "tl")
            self.assertIsNone(handed_off["task"]["agent"])
            self.assertIsNone(handed_off["task"]["agent_id"])
            self.assertEqual(handed_off["task"]["result_evidence"], "QA 验证通过。")

            claimed = claim_task(self.tl, record_id="recHandoff")
            self.assertEqual(claimed["transition"], {"from": "ready", "to": "in_progress"})
            self.assertEqual(claimed["task"]["agent_id"], "agent_tl")
            self.assertEqual(claimed["task"]["agent"], "TL Agent")

        for decision, extra, expected in (
            ("send_to_qa", {}, ("ready", "qa")),
            ("rework", {"role": "tl"}, ("ready", "tl")),
            ("approve", {}, ("done", None)),
        ):
            board = FakeTaskBoard(reviewed)
            read, write = board.patches()
            with read, write:
                decided = review_task(
                    self.pm,
                    record_id="recHandoff",
                    decision=decision,
                    result_evidence="评审结论。",
                    **extra,
                )
            self.assertTrue(decided["ok"], decided.get("error"))
            self.assertEqual(
                (decided["task"]["status"], decided["task"]["role"]),
                expected,
                decision,
            )

    def test_complete_general_task_delivery_flow(self):
        board = FakeTaskBoard()
        read, write = board.patches()
        with read, write:
            created = create_task(
                self.owner,
                title="整理调研结论",
                task_type="research",
                priority="P1",
                description="比较候选方案并整理证据。",
                acceptance_criteria="结论可复查，取舍明确。",
                delivery_mode="standard",
            )
            self.assertEqual(created["task"]["status"], "backlog")
            self.assertEqual(created["task"]["role"], "executor")

            routed = route_task(
                self.owner,
                record_id="recFlow",
                role="executor",
            )
            self.assertEqual(
                routed["transition"],
                {"from": "backlog", "to": "ready"},
            )

            claimed = claim_task(self.executor, record_id="recFlow")
            self.assertEqual(claimed["task"]["agent_id"], "agent_executor")

            submitted = submit_task(
                self.executor,
                record_id="recFlow",
                outcome="completed",
                result_evidence="已比较候选方案并整理来源。",
            )
            self.assertEqual(submitted["task"]["status"], "review")

            reviewer_routed = review_task(
                self.owner,
                record_id="recFlow",
                decision="send_to_reviewer",
                result_evidence="负责人请求独立复核。",
                next_action="核对来源与结论一致性。",
            )
            self.assertEqual(reviewer_routed["task"]["status"], "ready")
            self.assertEqual(reviewer_routed["task"]["role"], "reviewer")
            self.assertIsNone(reviewer_routed["task"]["agent_id"])

            reviewer_claimed = claim_task(self.reviewer, record_id="recFlow")
            self.assertEqual(
                reviewer_claimed["task"]["agent_id"],
                "agent_reviewer",
            )

            reviewed = submit_task(
                self.reviewer,
                record_id="recFlow",
                outcome="reviewed",
                result_evidence="来源与结论一致。",
            )
            self.assertEqual(reviewed["task"]["status"], "review")
            self.assertTrue(
                reviewed["task"]["result_evidence"].startswith(
                    "评审结论：已审阅（Reviewed）"
                )
            )

            completed = review_task(
                self.owner,
                record_id="recFlow",
                decision="approve",
                result_evidence="负责人确认验收通过。",
            )
            self.assertEqual(completed["task"]["status"], "done")
            self.assertEqual(completed["available_actions"], [])

    def test_errors_are_structured_and_actionable(self):
        board = FakeTaskBoard({
            "record_id": "recRules",
            "task_id": "TF-0002",
            "title": "规则校验",
            "status": "backlog",
            "type": None,
            "priority": "P2",
            "role": "tl",
            "description": None,
            "acceptance_criteria": None,
            "agent": None,
            "agent_id": None,
        })
        read, write = board.patches()
        with read, write:
            denied = route_task(self.tl, record_id="recRules", role="tl")
            self.assertFalse(denied["ok"])
            self.assertEqual(denied["error"]["category"], "permission")
            self.assertEqual(denied["error"]["code"], "permission_denied")

            incomplete = route_task(self.pm, record_id="recRules", role="tl")
            self.assertFalse(incomplete["ok"])
            self.assertEqual(incomplete["error"]["code"], "task_not_ready")
            self.assertEqual(
                set(incomplete["error"]["details"]["missing_task_fields"]),
                {"type", "description", "acceptance_criteria"},
            )
            self.assertFalse(incomplete["error"]["retryable"])
            self.assertIn("update_task", incomplete["error"]["message"])

    def test_claim_is_idempotent_and_role_is_enforced(self):
        board = FakeTaskBoard(self._ready_task())
        read, write = board.patches()
        with read, write:
            wrong_role = claim_task(self.qa, record_id="recReady")
            self.assertEqual(wrong_role["error"]["code"], "permission_denied")

            first = claim_task(self.tl, record_id="recReady")
            second = claim_task(self.tl, record_id="recReady")
            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            self.assertTrue(second["already_applied"])
            self.assertEqual(second["task"]["agent_id"], "agent_tl")

    def test_pm_can_delegate_the_current_ready_task_without_claiming_it(self):
        board = FakeTaskBoard({
            **self._ready_task(),
            "role": "pm",
        })
        read, write = board.patches()
        with read, write:
            delegated = route_task(self.pm, record_id="recReady", role="tl")

        self.assertTrue(delegated["ok"])
        self.assertEqual(delegated["transition"], {"from": "ready", "to": "ready"})
        self.assertEqual(delegated["task"]["role"], "tl")
        self.assertIsNone(delegated["task"]["agent"])
        self.assertIsNone(delegated["task"]["agent_id"])

    def test_submit_outcomes_and_block_targets_follow_role_rules(self):
        board = FakeTaskBoard({
            **self._ready_task(),
            "status": "in_progress",
            "agent": "TL Agent",
            "agent_id": "agent_tl",
        })
        read, write = board.patches()
        with read, write:
            wrong_outcome = submit_task(
                self.tl,
                record_id="recReady",
                outcome="passed",
                result_evidence="不应接受。",
            )
            self.assertEqual(wrong_outcome["error"]["code"], "permission_denied")

            invalid_outcome = submit_task(
                self.tl,
                record_id="recReady",
                outcome="unknown",
                result_evidence="不应接受。",
            )
            self.assertEqual(invalid_outcome["error"]["code"], "invalid_option")
            self.assertEqual(
                invalid_outcome["error"]["details"]["allowed_options"],
                ["completed"],
            )

            wrong_waiting_target = block_task(
                self.tl,
                record_id="recReady",
                waiting_on="stakeholder",
                blocked_reason="需求不明确。",
                next_action="确认需求。",
            )
            self.assertEqual(wrong_waiting_target["error"]["code"], "invalid_value")
            self.assertEqual(
                wrong_waiting_target["error"]["details"]["allowed_values"],
                {"waiting_on": ["pm"]},
            )

            blocked = block_task(
                self.tl,
                record_id="recReady",
                waiting_on="pm",
                blocked_reason="需求不明确。",
                next_action="请 PM 明确边界。",
            )
            self.assertTrue(blocked["ok"])
            self.assertEqual(blocked["task"]["status"], "blocked")

    def test_qa_outcome_is_persisted_and_idempotent_without_cross_matching(self):
        board = FakeTaskBoard({
            **self._ready_task(),
            "status": "in_progress",
            "role": "qa",
            "agent": "QA Agent",
            "agent_id": "agent_qa",
        })
        read, write = board.patches()
        with read, write:
            passed = submit_task(
                self.qa,
                record_id="recReady",
                outcome="passed",
                result_evidence="全部验收用例通过。",
            )
            repeated = submit_task(
                self.qa,
                record_id="recReady",
                outcome="passed",
                result_evidence="全部验收用例通过。",
            )
            opposite = submit_task(
                self.qa,
                record_id="recReady",
                outcome="failed",
                result_evidence="全部验收用例通过。",
            )

        self.assertEqual(
            passed["task"]["result_evidence"],
            "QA 结论：通过（Passed）\n全部验收用例通过。",
        )
        self.assertTrue(repeated["already_applied"])
        self.assertEqual(opposite["error"]["code"], "invalid_state")

    def test_whitespace_only_required_input_is_rejected(self):
        board = FakeTaskBoard()
        read, write = board.patches()
        with read, write:
            result = create_task(self.pm, title="   ")

        self.assertEqual(result["error"]["code"], "missing_fields")
        self.assertEqual(result["error"]["details"]["missing_fields"], ["title"])

    def test_stop_execution_rule_requires_pm_confirmation_and_in_progress_task(self):
        task = {
            **self._ready_task(),
            "status": "in_progress",
            "agent": "TL Agent",
            "agent_id": "agent_tl",
        }
        unconfirmed = prepare_runtime_action(
            self.pm,
            action_key="stop_execution",
            task=task,
            payload={"reason": "需求撤销"},
            confirmed=False,
        )
        wrong_role = prepare_runtime_action(
            self.tl,
            action_key="stop_execution",
            task=task,
            payload={"reason": "需求撤销"},
            confirmed=True,
        )
        allowed = prepare_runtime_action(
            self.pm,
            action_key="stop_execution",
            task=task,
            payload={"reason": "需求撤销"},
            confirmed=True,
        )

        self.assertEqual(unconfirmed["error"]["code"], "confirmation_required")
        self.assertEqual(wrong_role["error"]["code"], "permission_denied")
        self.assertTrue(allowed["ok"])
        self.assertEqual(allowed["runtime_facts"], ["execution_stopped"])

    def test_orphaned_in_progress_task_can_recover_without_overriding_active_execution(self):
        runtime = TaskExecutionRuntime(
            get_task=lambda *args, **kwargs: {},
            stop_turn=lambda *args, **kwargs: {},
            read_thread=lambda *args, **kwargs: {},
            thread_permanently_unavailable=lambda error: False,
            active_sessions=lambda: set(),
            load_workflow=load_workflow_definition,
        )
        task = {
            **self._ready_task(),
            "status": "in_progress",
            "agent": None,
            "agent_id": None,
        }
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.__exit__.return_value = False
        with (
            patch("core.task_execution.connect", return_value=connection),
            patch("core.task_execution.bootstrap_workspace"),
        ):
            connection.execute.return_value.fetchone.return_value = None
            facts = runtime.runtime_facts(self.pm, task)
            connection.execute.return_value.fetchone.return_value = (1,)
            active_facts = runtime.runtime_facts(self.pm, task)

        board = FakeTaskBoard(task)
        read, write = board.patches()
        with read, write:
            recovered = route_task(
                self.pm,
                record_id="recReady",
                role="design",
                runtime_facts=facts,
            )

        self.assertEqual(facts, {"executor_unavailable", "execution_stopped"})
        self.assertEqual(active_facts, set())
        self.assertTrue(recovered["ok"])
        self.assertEqual(recovered["transition"], {"from": "in_progress", "to": "ready"})
        self.assertEqual(recovered["task"]["role"], "design")

        stopped_board = FakeTaskBoard({
            **task,
            "agent": "Design Agent",
            "agent_id": "agent_design",
        })
        read, write = stopped_board.patches()
        with read, write:
            stopped_recovery = route_task(
                self.pm,
                record_id="recReady",
                role="design",
                runtime_facts={"execution_stopped"},
            )
        self.assertTrue(stopped_recovery["ok"])
        self.assertEqual(
            stopped_recovery["transition"],
            {"from": "in_progress", "to": "ready"},
        )

    def test_lifecycle_fields_and_terminal_states_cannot_be_bypassed(self):
        board = FakeTaskBoard(self._ready_task())
        read, write = board.patches()
        with read, write:
            bypass = update_task(
                self.pm,
                record_id="recReady",
                fields={"status": "done", "agent_id": "forged"},
            )
            self.assertEqual(bypass["error"]["code"], "invalid_fields")
            self.assertEqual(
                set(bypass["error"]["details"]["invalid_fields"]),
                {"status", "agent_id"},
            )
            invalid_current_state_field = update_task(
                self.pm,
                record_id="recReady",
                fields={"waiting_on": "stakeholder"},
            )
            self.assertEqual(invalid_current_state_field["error"]["code"], "invalid_fields")

            board.task["status"] = "done"
            reopen = route_task(self.pm, record_id="recReady", role="tl")
            self.assertEqual(reopen["error"]["code"], "invalid_state")
            self.assertEqual(reopen["error"]["available_actions"], [])

    def test_cancel_requires_confirmation_and_stopped_execution(self):
        board = FakeTaskBoard({
            **self._ready_task(),
            "status": "in_progress",
            "agent": "TL Agent",
            "agent_id": "agent_tl",
        })
        read, write = board.patches()
        with read, write:
            unconfirmed = cancel_task(
                self.pm,
                record_id="recReady",
                result_evidence="需求撤销，已安排收尾。",
                confirmed=False,
            )
            self.assertEqual(unconfirmed["error"]["code"], "confirmation_required")

            active = cancel_task(
                self.pm,
                record_id="recReady",
                result_evidence="需求撤销，已安排收尾。",
                confirmed=True,
            )
            self.assertEqual(active["error"]["code"], "precondition_failed")
            self.assertEqual(
                active["error"]["details"]["missing_preconditions"],
                ["execution_stopped"],
            )

            stopped = cancel_task(
                self.pm,
                record_id="recReady",
                result_evidence="需求撤销，已安排收尾。",
                confirmed=True,
                runtime_facts={"execution_stopped"},
            )
            self.assertTrue(stopped["ok"])
            self.assertEqual(stopped["task"]["status"], "canceled")

    def test_update_preserves_current_state_invariants(self):
        board = FakeTaskBoard(self._ready_task())
        read, write = board.patches()
        with read, write:
            incomplete_ready = update_task(
                self.pm,
                record_id="recReady",
                fields={"acceptance_criteria": ""},
            )
            self.assertEqual(incomplete_ready["error"]["code"], "task_not_ready")
            self.assertEqual(
                incomplete_ready["error"]["details"]["missing_task_fields"],
                ["acceptance_criteria"],
            )

            board.task.update({
                "status": "blocked",
                "blocked_reason": "等待决策",
                "waiting_on": "stakeholder",
                "next_action": "请项目决策人确认。",
            })
            incomplete_blocked = update_task(
                self.pm,
                record_id="recReady",
                fields={"blocked_reason": ""},
            )
            self.assertEqual(incomplete_blocked["error"]["code"], "task_not_ready")
            self.assertEqual(
                incomplete_blocked["error"]["details"]["missing_task_fields"],
                ["blocked_reason"],
            )

    def test_mutation_aborts_when_the_task_changes_before_write(self):
        original = self._ready_task()
        changed = {**original, "status": "done", "result_evidence": "由人工完成。"}
        with (
            patch(
                "core.teamflow_tools.get_lark_task",
                side_effect=[
                    {"ok": True, "task": deepcopy(original)},
                    {"ok": True, "task": deepcopy(changed)},
                ],
            ),
            patch("core.teamflow_tools.upsert_lark_task") as write,
        ):
            result = claim_task(self.tl, record_id="recReady")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["category"], "conflict")
        self.assertEqual(result["error"]["code"], "task_changed")
        self.assertTrue(result["error"]["retryable"])
        self.assertEqual(result["error"]["current_state"], "done")
        self.assertIn("status", result["error"]["details"]["changed_fields"])
        write.assert_not_called()

    def test_mutation_forwards_the_invocation_as_lark_idempotency_token(self):
        board = FakeTaskBoard(self._ready_task())
        read, write = board.patches()
        invocation_id = "33333333-3333-4333-8333-333333333333"
        with read, write:
            result = claim_task(
                self.tl,
                record_id="recReady",
                invocation_id=invocation_id,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(board.client_tokens, [invocation_id])

    @staticmethod
    def _ready_task() -> dict[str, object]:
        return {
            "record_id": "recReady",
            "task_id": "TF-0003",
            "title": "可执行任务",
            "status": "ready",
            "delivery_mode": "standard",
            "type": "development",
            "priority": "P1",
            "role": "tl",
            "description": "实现功能。",
            "acceptance_criteria": "测试通过。",
            "context": None,
            "dependencies": None,
            "progress": None,
            "next_action": None,
            "result_evidence": None,
            "blocked_reason": None,
            "waiting_on": None,
            "agent": None,
            "agent_id": None,
        }


if __name__ == "__main__":
    unittest.main()
