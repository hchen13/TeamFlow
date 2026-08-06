from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from core.workflow import (
    load_workflow_definition,
    load_workflow_definitions,
    task_option_definitions,
)
from core.workflow_contract import workflow_contract
from core.workflow_rule_validation import validate_rule
from core.workflow_validation import (
    WORKFLOW_SCHEMA_VERSION,
    WORKFLOW_ACTION_KEYS,
    validate_workflow_definition,
)


WORKFLOW_PATH = Path("/tmp/software-development/workflow.json")


def assignment(
    role_key: str,
    workflow_key: str = "software-development",
) -> dict[str, str]:
    return {
        "agent_id": f"agent_{role_key}",
        "agent_name": f"{role_key.upper()} Agent",
        "workspace_root": "/workspace",
        "workflow_key": workflow_key,
        "role_key": role_key,
    }


class WorkflowValidationTest(unittest.TestCase):
    def test_every_installed_workflow_uses_the_shared_contract(self):
        definitions = load_workflow_definitions()

        self.assertTrue(definitions)
        for key, definition in definitions.items():
            with self.subTest(workflow=key):
                self.assertEqual(definition["schema_version"], WORKFLOW_SCHEMA_VERSION)
                self.assertEqual(
                    set(definition["lifecycle"]["actions"]),
                    WORKFLOW_ACTION_KEYS,
                )
                self.assertTrue(definition["waiting_targets"])

    def test_unknown_fields_are_rejected_at_every_schema_level(self):
        cases = (
            ("definition", lambda value: value.__setitem__("custom", True)),
            ("role", lambda value: value["roles"][0].__setitem__("custom", True)),
            (
                "task_type",
                lambda value: value["task_types"][0].__setitem__("custom", True),
            ),
            (
                "waiting_target",
                lambda value: value["waiting_targets"][0].__setitem__("custom", True),
            ),
            (
                "task_schema",
                lambda value: value["task_schema"].__setitem__("custom", True),
            ),
            (
                "task_id",
                lambda value: value["task_schema"]["task_id"].__setitem__(
                    "custom",
                    True,
                ),
            ),
            (
                "lifecycle",
                lambda value: value["lifecycle"].__setitem__("custom", True),
            ),
            (
                "state",
                lambda value: value["lifecycle"]["states"][0].__setitem__(
                    "custom",
                    True,
                ),
            ),
            (
                "action",
                lambda value: value["lifecycle"]["actions"]["claim"].__setitem__(
                    "custom",
                    True,
                ),
            ),
            (
                "rule",
                lambda value: value["lifecycle"]["actions"]["claim"]["rules"][
                    0
                ].__setitem__("custom", True),
            ),
            (
                "runtime_action",
                lambda value: value["runtime_actions"]["stop_execution"].__setitem__(
                    "custom",
                    True,
                ),
            ),
            (
                "color",
                lambda value: value["waiting_targets"][0]["color"].__setitem__(
                    "custom",
                    True,
                ),
            ),
        )

        for name, mutate in cases:
            with self.subTest(level=name):
                definition = deepcopy(
                    load_workflow_definition("software-development")
                )
                mutate(definition)
                with self.assertRaisesRegex(ValueError, "unsupported fields"):
                    validate_workflow_definition(definition, WORKFLOW_PATH)

        localized = deepcopy(load_workflow_definition("software-development"))
        localized["labels"]["fr"] = "Développement logiciel"
        with self.assertRaisesRegex(ValueError, "non-empty zh-CN and en"):
            validate_workflow_definition(localized, WORKFLOW_PATH)

        missing = deepcopy(load_workflow_definition("software-development"))
        del missing["runtime_actions"]
        with self.assertRaisesRegex(ValueError, "missing required fields"):
            validate_workflow_definition(missing, WORKFLOW_PATH)

    def test_lifecycle_must_be_reachable_and_terminate(self):
        unreachable = deepcopy(load_workflow_definition("software-development"))
        unreachable["lifecycle"]["states"].append(self._state("orphan"))
        with self.assertRaisesRegex(ValueError, "unreachable from backlog"):
            validate_workflow_definition(unreachable, WORKFLOW_PATH)

        dead_end = deepcopy(load_workflow_definition("software-development"))
        dead_end["lifecycle"]["states"].append(self._state("waiting_forever"))
        dead_end["lifecycle"]["actions"]["route"]["rules"].append({
            "key": "wait_forever",
            "labels": {"zh-CN": "永久等待", "en": "Wait forever"},
            "actors": ["coordinator"],
            "from": ["backlog"],
            "to": "waiting_forever",
            "writable_fields": [],
        })
        with self.assertRaisesRegex(
            ValueError,
            "without a path to a terminal state",
        ):
            validate_workflow_definition(dead_end, WORKFLOW_PATH)

        terminal_initial = deepcopy(
            load_workflow_definition("software-development")
        )
        terminal_initial["lifecycle"]["terminal_states"].append("backlog")
        with self.assertRaisesRegex(ValueError, "initial_state cannot be terminal"):
            validate_workflow_definition(terminal_initial, WORKFLOW_PATH)

        duplicate_create = deepcopy(
            load_workflow_definition("software-development")
        )
        second_rule = deepcopy(
            duplicate_create["lifecycle"]["actions"]["create"]["rules"][0]
        )
        second_rule["key"] = "create_again"
        duplicate_create["lifecycle"]["actions"]["create"]["rules"].append(
            second_rule
        )
        with self.assertRaisesRegex(ValueError, "exactly one rule"):
            validate_workflow_definition(duplicate_create, WORKFLOW_PATH)

    def test_guards_and_confirmation_only_use_supported_tool_inputs(self):
        guarded_submit = deepcopy(
            load_workflow_definition("software-development")
        )
        guarded_submit["lifecycle"]["actions"]["submit"]["rules"][0][
            "guards"
        ] = ["execution_stopped"]
        with self.assertRaisesRegex(
            ValueError,
            "guards are not supported by submit",
        ):
            validate_workflow_definition(guarded_submit, WORKFLOW_PATH)

        confirmed_claim = deepcopy(
            load_workflow_definition("software-development")
        )
        confirmed_claim["lifecycle"]["actions"]["claim"][
            "confirmation_required"
        ] = True
        with self.assertRaisesRegex(
            ValueError,
            "confirmation_required is only supported for cancel",
        ):
            validate_workflow_definition(confirmed_claim, WORKFLOW_PATH)

    def test_waiting_targets_are_declared_by_each_workflow(self):
        definition = deepcopy(load_workflow_definition("software-development"))
        definition["waiting_targets"].append({
            "key": "decision_board",
            "labels": {"zh-CN": "决策委员会", "en": "Decision board"},
            "color": {"hue": "Carmine", "lightness": "Lighter"},
        })
        definition["lifecycle"]["actions"]["block"]["rules"][0][
            "field_values"
        ]["waiting_on"] = ["decision_board"]

        validate_workflow_definition(definition, WORKFLOW_PATH)

        options = task_option_definitions(definition)["waiting_on"]
        self.assertEqual(options[-1], {
            "key": "decision_board",
            "labels": {"zh-CN": "决策委员会", "en": "Decision board"},
            "hue": "Carmine",
            "lightness": "Lighter",
        })

    def test_role_names_are_data_not_software_development_branches(self):
        definition = deepcopy(load_workflow_definition("software-development"))
        definition["key"] = "universal-workflow"
        role_mapping = {
            "pm": "owner",
            "tl": "maker",
            "qa": "checker",
            "design": "designer",
        }
        definition["coordinator_role"] = "owner"
        for role in definition["roles"]:
            role["key"] = role_mapping[role["key"]]
        for task_type in definition["task_types"]:
            task_type["default_role"] = role_mapping[task_type["default_role"]]
        definition["waiting_targets"][0]["key"] = "owner"
        for action in definition["lifecycle"]["actions"].values():
            for rule in action["rules"]:
                rule["roles"] = [
                    role_mapping[role]
                    for role in rule.get("roles", [])
                ]
                for mapping_name in ("defaults", "fixed_fields"):
                    mapping = rule.get(mapping_name, {})
                    if "role" in mapping:
                        mapping["role"] = role_mapping[mapping["role"]]
                field_values = rule.get("field_values", {})
                if "role" in field_values:
                    field_values["role"] = [
                        role_mapping[role]
                        for role in field_values["role"]
                    ]
                if "waiting_on" in field_values:
                    field_values["waiting_on"] = [
                        "owner" if target == "pm" else target
                        for target in field_values["waiting_on"]
                    ]

        validate_workflow_definition(
            definition,
            Path("/tmp/universal-workflow/workflow.json"),
        )

        options = task_option_definitions(definition)
        self.assertEqual(
            {item["key"] for item in options["role"]},
            {"owner", "maker", "checker", "designer"},
        )
        self.assertEqual(
            {item["key"] for item in options["waiting_on"]},
            {"owner", "stakeholder"},
        )
        with patch(
            "core.workflow_contract.workflow_definition_for_assignment",
            return_value=definition,
        ):
            contract = workflow_contract({
                **assignment("pm"),
                "role_key": "owner",
            })
        self.assertEqual(contract["coordinator_role"], "owner")
        self.assertIn(
            "review",
            {action["action"] for action in contract["actions"]},
        )

    def test_every_lifecycle_rule_has_a_successful_role_and_input(self):
        for workflow_key, definition in load_workflow_definitions().items():
            for action_key, action in definition["lifecycle"]["actions"].items():
                for rule in action["rules"]:
                    successful_roles = []
                    for role in definition["roles"]:
                        caller = assignment(role["key"], workflow_key)
                        task = (
                            None
                            if action_key == "create"
                            else self._complete_task(
                                definition,
                                caller,
                                status=rule["from"][0],
                            )
                        )
                        error = validate_rule(
                            caller,
                            definition,
                            action_key=action_key,
                            rule=rule,
                            task=task,
                            payload=self._required_payload(definition, rule),
                            runtime_facts=set(rule.get("guards", [])),
                        )
                        if error is None:
                            successful_roles.append(role["key"])

                    with self.subTest(
                        workflow=workflow_key,
                        action=action_key,
                        rule=rule["key"],
                    ):
                        self.assertTrue(
                            successful_roles,
                            (
                                f"{workflow_key}.{action_key}.{rule['key']} "
                                "has no executable role/input"
                            ),
                        )

    def test_state_names_are_data_not_engine_branches(self):
        definition = deepcopy(load_workflow_definition("software-development"))
        mapping = {
            "backlog": "state_a",
            "ready": "state_b",
            "in_progress": "state_c",
            "review": "state_d",
            "blocked": "state_e",
            "done": "state_f",
            "canceled": "state_g",
        }
        definition["lifecycle"]["initial_state"] = mapping[
            definition["lifecycle"]["initial_state"]
        ]
        definition["lifecycle"]["terminal_states"] = [
            mapping[state]
            for state in definition["lifecycle"]["terminal_states"]
        ]
        definition["lifecycle"]["completion_states"] = [
            mapping[state]
            for state in definition["lifecycle"]["completion_states"]
        ]
        for state in definition["lifecycle"]["states"]:
            state["key"] = mapping[state["key"]]
        for action in definition["lifecycle"]["actions"].values():
            for rule in action["rules"]:
                rule["from"] = [
                    mapping[state]
                    for state in rule.get("from", [])
                ]
                if "to" in rule:
                    rule["to"] = mapping[rule["to"]]
        for action in definition["runtime_actions"].values():
            action["states"] = [
                mapping[state]
                for state in action["states"]
            ]

        validate_workflow_definition(definition, WORKFLOW_PATH)

        with patch(
            "core.workflow_contract.workflow_definition_for_assignment",
            return_value=definition,
        ):
            contract = workflow_contract(assignment("pm"))
        self.assertEqual(contract["initial_state"], "state_a")
        self.assertEqual(contract["terminal_states"], ["state_f", "state_g"])

    @staticmethod
    def _state(key: str) -> dict[str, object]:
        return {
            "key": key,
            "labels": {"zh-CN": key, "en": key},
            "color": {"hue": "Gray", "lightness": "Lighter"},
            "dispatch": "none",
            "required_fields": ["title"],
        }

    @staticmethod
    def _complete_task(
        definition: dict[str, object],
        caller: dict[str, str],
        *,
        status: str,
    ) -> dict[str, object]:
        return {
            "record_id": "recContract",
            "task_id": "TF-0001",
            "title": "契约测试任务",
            "status": status,
            "type": definition["task_types"][0]["key"],
            "priority": "P1",
            "role": caller["role_key"],
            "agent": caller["agent_name"],
            "agent_id": caller["agent_id"],
            "description": "验证规则存在成功路径。",
            "context": "测试上下文。",
            "acceptance_criteria": "规则校验通过。",
            "dependencies": "无。",
            "progress": "进行中。",
            "next_action": "继续。",
            "result_evidence": "测试证据。",
            "blocked_reason": "等待输入。",
            "waiting_on": definition["waiting_targets"][0]["key"],
        }

    @staticmethod
    def _required_payload(
        definition: dict[str, object],
        rule: dict[str, object],
    ) -> dict[str, str]:
        values = {
            "title": "契约测试任务",
            "type": definition["task_types"][0]["key"],
            "priority": "P1",
            "role": definition["roles"][0]["key"],
            "description": "验证规则存在成功路径。",
            "context": "测试上下文。",
            "acceptance_criteria": "规则校验通过。",
            "dependencies": "无。",
            "progress": "进行中。",
            "next_action": "继续。",
            "result_evidence": "测试证据。",
            "blocked_reason": "等待输入。",
            "waiting_on": definition["waiting_targets"][0]["key"],
        }
        allowed_values = rule.get("field_values", {})
        return {
            field: allowed_values.get(field, [values[field]])[0]
            for field in rule.get("required_inputs", [])
        }


class DeliverySystemFieldTest(unittest.TestCase):
    """A workflow cannot hand agents a direct write to the fields git pins."""

    def rules(self):
        definition = deepcopy(load_workflow_definition("software-development"))
        return definition, definition["lifecycle"]["actions"]["update"]["rules"][0]

    def test_a_system_field_cannot_be_declared_writable(self):
        for field in ("target_branch", "base_sha", "delivery_resources"):
            definition, rule = self.rules()
            rule["writable_fields"] = [*rule["writable_fields"], field]
            with self.assertRaises(ValueError):
                validate_workflow_definition(definition, WORKFLOW_PATH)

    def test_a_system_field_cannot_be_declared_clearable(self):
        for field in ("target_branch", "base_sha", "delivery_resources"):
            definition, rule = self.rules()
            rule["clear_fields"] = [*rule.get("clear_fields", []), field]
            with self.assertRaises(ValueError):
                validate_workflow_definition(definition, WORKFLOW_PATH)

    def test_an_agent_writable_field_is_still_accepted(self):
        definition, rule = self.rules()
        rule["writable_fields"] = [*rule["writable_fields"], "candidate_sha"]

        validate_workflow_definition(definition, WORKFLOW_PATH)

