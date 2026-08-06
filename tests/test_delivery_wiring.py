from __future__ import annotations

import unittest

from core.mcp_server import mcp
from core.teamflow_tool_dispatcher import TeamFlowToolDispatcher
from core.workflow_contract import workflow_contract


ASSIGNMENT = {
    "agent_id": "agent_pm",
    "agent_name": "PM Agent",
    "workspace_root": ".",
    "workflow_key": "software-development",
    "role_key": "pm",
}


def tool_parameters(name: str) -> dict[str, object]:
    tool = mcp._tool_manager.get_tool(name)
    return tool.parameters["properties"]


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def resolve(self, name: str):
        def call(assignment, **kwargs):
            self.calls.append((name, kwargs))
            return {"ok": True}

        return call

    def dispatcher(self) -> TeamFlowToolDispatcher:
        return TeamFlowToolDispatcher(
            resolve=self.resolve,
            runtime_facts=lambda *_: set(),
            stop_execution=lambda *_: {"ok": True},
        )


class DeliveryWiringTest(unittest.TestCase):
    def test_the_mcp_tools_expose_the_delivery_parameters(self):
        self.assertIn("delivery_mode", tool_parameters("create_task"))

        update = tool_parameters("update_task")
        self.assertIn("delivery", update)
        self.assertIn("fields", update)

        self.assertIn("delivery", tool_parameters("submit_task"))

    def test_update_task_accepts_a_delivery_only_call(self):
        required = mcp._tool_manager.get_tool("update_task").parameters.get("required", [])

        self.assertIn("record_id", required)
        self.assertNotIn("fields", required)

    def test_the_dispatcher_forwards_delivery_untouched(self):
        recorder = Recorder()
        delivery = {"candidate_sha": "a" * 40, "resources": {"branches": ["teamflow/T-1/task"]}}

        recorder.dispatcher().invoke(
            ASSIGNMENT,
            "update_task",
            {"record_id": "rec1", "delivery": delivery},
            invocation_id="inv-1",
        )
        recorder.dispatcher().invoke(
            ASSIGNMENT,
            "submit_task",
            {
                "record_id": "rec1",
                "outcome": "completed",
                "result_evidence": "done",
                "delivery": delivery,
            },
            invocation_id="inv-2",
        )
        recorder.dispatcher().invoke(
            ASSIGNMENT,
            "create_task",
            {"title": "t", "delivery_mode": "repository"},
            invocation_id="inv-3",
        )

        forwarded = dict(recorder.calls[0][1]), dict(recorder.calls[1][1]), dict(recorder.calls[2][1])
        self.assertEqual(forwarded[0]["delivery"], delivery)
        self.assertEqual(forwarded[0]["fields"], {})
        self.assertEqual(forwarded[1]["delivery"], delivery)
        self.assertEqual(forwarded[2]["delivery_mode"], "repository")

    def test_the_dispatcher_rejects_a_delivery_that_is_not_an_object(self):
        recorder = Recorder()

        with self.assertRaisesRegex(ValueError, "delivery must be an object"):
            recorder.dispatcher().invoke(
                ASSIGNMENT,
                "update_task",
                {"record_id": "rec1", "delivery": "candidate=abc"},
                invocation_id="inv-4",
            )

    def test_omitted_delivery_stays_none_rather_than_an_empty_object(self):
        recorder = Recorder()

        recorder.dispatcher().invoke(
            ASSIGNMENT,
            "update_task",
            {"record_id": "rec1", "fields": {"progress": "x"}},
            invocation_id="inv-5",
        )

        self.assertIsNone(dict(recorder.calls[0][1])["delivery"])

    def test_the_contract_tells_agents_the_delivery_constraints(self):
        delivery = workflow_contract(ASSIGNMENT)["delivery"]

        self.assertEqual(delivery["target_branch"], "main")
        self.assertEqual(delivery["modes"], ["standard", "repository"])
        self.assertEqual(delivery["completion_states"], ["done"])
        self.assertEqual(delivery["system_fields"], ["target_branch", "base_sha"])
        self.assertIn("candidate_sha", delivery["submittable_fields"])
        self.assertIn("resources", delivery["submittable_fields"])
        self.assertIsInstance(delivery["version_control_enabled"], bool)


if __name__ == "__main__":
    unittest.main()
