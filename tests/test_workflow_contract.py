from __future__ import annotations

import unittest
from unittest.mock import patch

from core import mcp_server
from core.workflow import load_workflow_definitions
from core.workflow_contract import (
    ACTION_TO_TOOL,
    LIFECYCLE_ACTION_TO_TOOL,
    RUNTIME_ACTION_TO_TOOL,
    TOOL_NAMES,
    rule_role_applicable,
    workflow_contract,
)
from core.workflow_rule_validation import action_patch
from core.workflow_validation import WORKFLOW_ACTION_KEYS


def assignment(role_key: str, workflow_key: str) -> dict[str, str]:
    return {
        "agent_id": f"agent_{role_key}",
        "agent_name": f"{role_key.upper()} Agent",
        "workspace_root": "/workspace",
        "workflow_key": workflow_key,
        "role_key": role_key,
    }


class WorkflowContractTest(unittest.TestCase):
    def test_fixed_mcp_tools_match_the_workflow_action_vocabulary(self):
        self.assertEqual(set(LIFECYCLE_ACTION_TO_TOOL), WORKFLOW_ACTION_KEYS)
        self.assertEqual(
            set(ACTION_TO_TOOL),
            WORKFLOW_ACTION_KEYS | set(RUNTIME_ACTION_TO_TOOL),
        )
        self.assertEqual(
            TOOL_NAMES,
            {
                "get_assignment",
                "list_available_tasks",
                "get_task",
                *ACTION_TO_TOOL.values(),
            },
        )
        self.assertEqual(
            {tool.name for tool in mcp_server.mcp._tool_manager.list_tools()},
            TOOL_NAMES,
        )

    def test_each_role_contract_only_exposes_declared_actions(self):
        for workflow_key, definition in load_workflow_definitions().items():
            with patch(
                "core.workflow_contract.workflow_definition_for_assignment",
                return_value=definition,
            ):
                for role in definition["roles"]:
                    role_key = role["key"]
                    caller = assignment(role_key, workflow_key)
                    expected = {
                        action_key: {
                            rule["key"]
                            for rule in action["rules"]
                            if rule_role_applicable(caller, definition, rule)
                        }
                        for action_key, action in definition[
                            "lifecycle"
                        ]["actions"].items()
                    }
                    expected = {
                        action_key: options
                        for action_key, options in expected.items()
                        if options
                    }
                    with self.subTest(
                        workflow=workflow_key,
                        role=role_key,
                    ):
                        contract = workflow_contract(caller)
                        actual = {
                            action["action"]: {
                                option["key"]
                                for option in action["options"]
                            }
                            for action in contract["actions"]
                        }
                        self.assertEqual(actual, expected)
                        for action in contract["actions"]:
                            self.assertEqual(
                                action["tool"],
                                LIFECYCLE_ACTION_TO_TOOL[action["action"]],
                            )
                        for action in contract["runtime_actions"]:
                            self.assertEqual(
                                action["tool"],
                                RUNTIME_ACTION_TO_TOOL[action["action"]],
                            )

    def test_rule_patch_precedence_is_stable(self):
        result = action_patch(
            assignment("tl", "software-development"),
            {
                "defaults": {
                    "context": "default",
                    "result_evidence": "default",
                },
                "fixed_fields": {"context": "fixed"},
                "field_prefixes": {"result_evidence": "Evidence: "},
                "actor_fields": {
                    "agent": "agent_name",
                    "agent_id": "agent_id",
                },
                "clear_fields": ["next_action"],
                "to": "review",
            },
            {
                "context": "payload",
                "result_evidence": "actual",
                "next_action": "later",
            },
        )

        self.assertEqual(result, {
            "context": "fixed",
            "result_evidence": "Evidence: actual",
            "next_action": None,
            "agent": "TL Agent",
            "agent_id": "agent_tl",
            "status": "review",
        })


if __name__ == "__main__":
    unittest.main()
