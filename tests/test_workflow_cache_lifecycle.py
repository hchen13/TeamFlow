from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from core import prompt_catalog, workflow
from core.db import resolve_lark_cli_command
from core.teamflow_tool_dispatcher import TeamFlowToolDispatcher
from core.teamflow_tools import claim_task, submit_task, workflow_contract
from core.tool_runtime import ToolRuntime


ROOT = Path(__file__).resolve().parents[1]
READY_TASK = {
    "record_id": "recCache",
    "task_id": "TF-0099",
    "title": "Survive a plugin refresh",
    "status": "ready",
    "delivery_mode": "standard",
    "type": "development",
    "priority": "P1",
    "role": "tl",
    "agent": None,
    "agent_id": None,
    "description": "Exercise the workflow after its cache root disappears.",
    "context": None,
    "acceptance_criteria": "The claimed task can still be submitted.",
    "dependencies": None,
    "progress": None,
    "next_action": None,
    "result_evidence": None,
    "blocked_reason": None,
    "waiting_on": None,
}


def assignment(role: str, agent_id: str) -> dict[str, str]:
    return {
        "agent_id": agent_id,
        "agent_name": f"{role.upper()} Agent",
        "workspace_root": "/workspace",
        "workflow_key": "software-development",
        "role_key": role,
    }


class Board:
    def __init__(self) -> None:
        self.task = deepcopy(READY_TASK)
        self.writes = 0

    def read(self, workspace: str, *, record_id: str) -> dict[str, object]:
        if record_id != self.task["record_id"]:
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
        if record_id != self.task["record_id"]:
            raise ValueError("task not found")
        self.writes += 1
        self.task.update(task)
        return {"ok": True, "task": deepcopy(self.task)}


class WorkflowCacheLifecycleTest(unittest.TestCase):
    def test_claimed_task_survives_removal_of_its_plugin_cache_root(self) -> None:
        tl = assignment("tl", "agent_tl")
        other_tl = assignment("tl", "agent_other_tl")
        pm = assignment("pm", "agent_pm")
        assignments = {
            "session_tl": tl,
            "session_other_tl": other_tl,
            "session_pm": pm,
        }
        board = Board()

        with tempfile.TemporaryDirectory(prefix="teamflow-cache-") as temporary:
            plugin_root = Path(temporary) / "plugin"
            definitions_root = plugin_root / "workflows"
            prompts_root = plugin_root / "prompts"
            shutil.copytree(ROOT / "workflows", definitions_root)
            shutil.copytree(ROOT / "prompts", prompts_root)
            snapshot = workflow.load_workflow_definitions(definitions_root)
            prompt_snapshot = prompt_catalog._load_prompt_bundle(prompts_root)
            dispatcher = TeamFlowToolDispatcher(
                resolve=lambda name: {
                    "workflow_contract": workflow_contract,
                    "claim_task": claim_task,
                    "submit_task": submit_task,
                }[name],
                runtime_facts=lambda assignment, task: set(),
                stop_execution=lambda assignment, arguments: {},
            )
            runtime = ToolRuntime(
                sync_lock=threading.RLock(),
                assignment_context=lambda session_id, **kwargs: {
                    "assignment": assignments.get(session_id)
                },
                workspace_active=lambda workspace: True,
                invoke_tool=dispatcher.invoke,
                sync_task_activity=lambda *args, **kwargs: None,
            )

            invocation = 0

            def invoke(
                session_id: str,
                turn_id: str,
                tool_name: str,
                arguments: dict[str, object],
            ) -> dict[str, object]:
                nonlocal invocation
                invocation += 1
                invocation_id = f"invocation-{invocation}"
                grant = runtime.authorize(
                    invocation_id=invocation_id,
                    session_id=session_id,
                    cwd="/workspace",
                    turn_id=turn_id,
                    tool_name=f"mcp__teamflow__{tool_name}",
                    tool_input=arguments,
                )
                return runtime.invoke(
                    invocation_id=invocation_id,
                    grant=grant["grant"],
                    tool_name=tool_name,
                    arguments=arguments,
                )

            with (
                patch.object(workflow, "_RUNTIME_WORKFLOW_DEFINITIONS", snapshot),
                patch.object(prompt_catalog, "PROMPTS_DIR", prompts_root),
                patch.object(prompt_catalog, "_RUNTIME_PROMPT_ROOT", prompts_root.resolve()),
                patch.object(prompt_catalog, "_RUNTIME_PROMPT_BUNDLE", prompt_snapshot),
                patch("core.teamflow_tools.get_lark_task", side_effect=board.read),
                patch("core.teamflow_tools.upsert_lark_task", side_effect=board.write),
                patch(
                    "core.tool_runtime.find_agent_assignment",
                    side_effect=lambda workspaces, session_id: {
                        "assignment": assignments[session_id]
                    },
                ),
                patch("core.tool_runtime.workspace_enabled", return_value=True),
            ):
                assignment_before_refresh = invoke(
                    "session_tl", "turn_tl", "get_assignment", {}
                )
                claimed = invoke(
                    "session_tl",
                    "turn_tl",
                    "claim_task",
                    {"record_id": "recCache"},
                )

                shutil.rmtree(plugin_root)

                denied = invoke(
                    "session_other_tl",
                    "turn_other_tl",
                    "submit_task",
                    {
                        "record_id": "recCache",
                        "outcome": "completed",
                        "result_evidence": "Wrong Agent must not submit.",
                    },
                )
                submitted = invoke(
                    "session_tl",
                    "turn_tl_continuation",
                    "submit_task",
                    {
                        "record_id": "recCache",
                        "outcome": "completed",
                        "result_evidence": "Candidate is complete.",
                    },
                )
                assignment_after_refresh = invoke(
                    "session_pm", "turn_pm_new", "get_assignment", {}
                )

        self.assertEqual(
            assignment_before_refresh["workflow"]["key"], "software-development"
        )
        self.assertEqual(claimed["transition"], {"from": "ready", "to": "in_progress"})
        self.assertEqual(denied["error"]["code"], "permission_denied")
        self.assertEqual(submitted["transition"], {"from": "in_progress", "to": "review"})
        self.assertNotIn("turn_control", submitted)
        self.assertEqual(
            assignment_after_refresh["workflow"]["key"], "software-development"
        )
        self.assertEqual(board.writes, 2)

    def test_callers_cannot_mutate_the_runtime_snapshot(self) -> None:
        definition = workflow.load_workflow_definition("software-development")
        definition["labels"]["en"] = "Changed"

        fresh = workflow.load_workflow_definition("software-development")

        self.assertNotEqual(fresh["labels"]["en"], "Changed")

    def test_removed_bundled_lark_cli_falls_back_to_global_install(self) -> None:
        missing = "/old/plugin/ui/node_modules/.bin/lark-cli"
        with (
            patch.dict("os.environ", {"LARK_CLI": missing}),
            patch("core.db.shutil.which", return_value="/usr/local/bin/lark-cli"),
        ):
            command = resolve_lark_cli_command()

        self.assertEqual(command, "/usr/local/bin/lark-cli")


if __name__ == "__main__":
    unittest.main()
