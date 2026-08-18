from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import prompt_catalog
from core.agent_runtime import agent_context_fingerprint, render_agent_context
from core.lark_events import LarkEventContext
from core.mcp_server import mcp
from core.prompt_catalog import PromptError
from core.task_routing import render_task_prompt
from core.workflow import load_workflow_definition


ROOT = Path(__file__).resolve().parents[1]
ASSIGNMENT = {
    "agent_id": "agent_tl",
    "session_id": "session_tl",
    "assignment_revision": 1,
    "workspace_name": "Workspace",
    "workspace_root": "/workspace",
    "workflow_name": "软件研发",
    "workflow_key": "software-development",
    "role_name": "技术负责人",
    "role_key": "tl",
    "role_description": "实现并交付任务",
}
CONTEXT_VARIANTS = {
    "assignment-context.onboarding": {"onboarding": True},
    "assignment-context.recovery": {"onboarding": False, "recovery": True},
    "assignment-context.current": {"onboarding": False},
}


def event_context() -> LarkEventContext:
    return LarkEventContext(
        workspace_root="/workspace",
        db_path="/workspace/db",
        identity_id="identity",
        identity_name="Identity",
        app_id="app",
        app_name="App",
        app_secret="secret",
        auth_mode="bot",
        user_open_id="open",
        board_url="https://board.example",
        file_token="token",
        table_id="table",
        brand="feishu",
    )


class CatalogStructureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = prompt_catalog.load_catalog()

    def test_every_declared_prompt_has_a_template_and_a_complete_contract(self):
        for prompt_id, declared in self.catalog["prompts"].items():
            with self.subTest(prompt_id=prompt_id):
                self.assertEqual(
                    set(declared) >= {
                        "template_file",
                        "injection_surface",
                        "allowed_triggers",
                        "required_variables",
                    },
                    True,
                )
                self.assertTrue(declared["allowed_triggers"])
                self.assertTrue((prompt_catalog.PROMPTS_DIR / declared["template_file"]).is_file())

    def test_every_template_uses_exactly_the_variables_the_catalog_requires(self):
        for prompt_id, declared in self.catalog["prompts"].items():
            with self.subTest(prompt_id=prompt_id):
                template = (prompt_catalog.PROMPTS_DIR / declared["template_file"]).read_text(
                    encoding="utf-8"
                )
                self.assertEqual(
                    set(prompt_catalog.PLACEHOLDER.findall(template)),
                    set(declared["required_variables"]),
                )

    def test_every_prompt_renders_for_each_trigger_it_declares(self):
        for prompt_id, declared in self.catalog["prompts"].items():
            variables = {name: "x" for name in declared["required_variables"]}
            for trigger in declared["allowed_triggers"]:
                with self.subTest(prompt_id=prompt_id, trigger=trigger):
                    rendered = prompt_catalog.render(
                        prompt_id, trigger=trigger, variables=variables
                    )
                    self.assertTrue(rendered)
                    self.assertFalse(rendered.endswith("\n"))


class RendererRefusalTest(unittest.TestCase):
    def test_an_unknown_prompt_id_is_refused(self):
        with self.assertRaises(PromptError):
            prompt_catalog.render("no-such-prompt", trigger="onboarding", variables={})

    def test_a_trigger_the_prompt_does_not_declare_is_refused(self):
        with self.assertRaises(PromptError):
            prompt_catalog.render(
                "assignment-context.onboarding",
                trigger="compact_recovery",
                variables={name: "x" for name in ASSIGNMENT},
            )

    def test_missing_and_extra_variables_are_both_refused(self):
        complete = {
            name: "x"
            for name in prompt_catalog.entry("assignment-context.onboarding")["required_variables"]
        }

        with self.assertRaises(PromptError):
            prompt_catalog.render(
                "assignment-context.onboarding",
                trigger="onboarding",
                variables={key: value for key, value in list(complete.items())[:-1]},
            )
        with self.assertRaises(PromptError):
            prompt_catalog.render(
                "assignment-context.onboarding",
                trigger="onboarding",
                variables={**complete, "unexpected": "x"},
            )

    def test_a_template_placeholder_the_catalog_never_declared_is_refused(self):
        temporary = tempfile.TemporaryDirectory(prefix="prompts-", dir=ROOT / "tmp")
        self.addCleanup(temporary.cleanup)
        staged = Path(temporary.name) / "prompts"
        shutil.copytree(prompt_catalog.PROMPTS_DIR, staged)
        template = staged / prompt_catalog.entry("turn-control.reason")["template_file"]
        leaked = f"{template.read_text(encoding='utf-8')}{{{{leaked}}}}\n"
        template.write_text(leaked, encoding="utf-8")

        with patch.object(prompt_catalog, "PROMPTS_DIR", staged):
            with self.assertRaises(PromptError):
                prompt_catalog.render("turn-control.reason", trigger="handoff_complete")

    def test_an_unreadable_catalog_refuses_instead_of_rendering_nothing(self):
        temporary = tempfile.TemporaryDirectory(prefix="prompts-", dir=ROOT / "tmp")
        self.addCleanup(temporary.cleanup)

        with patch.object(prompt_catalog, "PROMPTS_DIR", Path(temporary.name)):
            with self.assertRaises(PromptError):
                prompt_catalog.render("turn-control.reason", trigger="handoff_complete")


class InjectionSurfaceTest(unittest.TestCase):
    """The three live surfaces must reach the model through the catalog, not around it."""

    def test_each_assignment_context_variant_renders_its_own_catalog_prompt(self):
        for prompt_id, options in CONTEXT_VARIANTS.items():
            with self.subTest(prompt_id=prompt_id):
                declared = prompt_catalog.entry(prompt_id)
                expected = prompt_catalog.render(
                    prompt_id,
                    trigger=declared["allowed_triggers"][0],
                    variables={
                        name: ASSIGNMENT[name] for name in declared["required_variables"]
                    },
                )

                self.assertEqual(render_agent_context(ASSIGNMENT, **options), expected)

    def test_the_fingerprint_hashes_the_same_render_the_agent_receives(self):
        with patch.object(prompt_catalog, "render", return_value="rendered context") as rendered:
            context = render_agent_context(ASSIGNMENT, onboarding=True)
            fingerprint = agent_context_fingerprint(ASSIGNMENT, context)

        self.assertEqual(context, "rendered context")
        self.assertEqual(rendered.call_count, 1)
        self.assertEqual(fingerprint, agent_context_fingerprint(ASSIGNMENT, "rendered context"))

    def test_the_task_event_prompt_is_rendered_by_the_catalog(self):
        task = {
            "task_id": "TF-0001",
            "title": "标题",
            "record_id": "rec1",
            "status": "ready",
            "role": "tl",
            "priority": "P1",
        }

        with patch.object(prompt_catalog, "render", return_value="rendered prompt") as rendered:
            prompt = render_task_prompt(
                event_context(),
                event_type="ready_entered",
                event_key="evt1",
                workflow_key="software-development",
                role_name="技术负责人",
                task=task,
                load_workflow=load_workflow_definition,
            )

        self.assertEqual(prompt, "rendered prompt")
        self.assertEqual(rendered.call_args.args, ("task-event.dispatch",))
        self.assertEqual(rendered.call_args.kwargs["trigger"], "task_event")

    def test_the_task_snapshot_carries_every_populated_catalog_field(self):
        labels = dict(
            (key, label)
            for label, key in prompt_catalog.entry("task-event.dispatch")["snapshot_fields"]
        )
        task = {"record_id": "rec1", "status": "ready", **{key: f"值-{key}" for key in labels}}

        prompt = render_task_prompt(
            event_context(),
            event_type="ready_entered",
            event_key="evt1",
            workflow_key="software-development",
            role_name="技术负责人",
            task=task,
            load_workflow=load_workflow_definition,
        )

        for key, label in labels.items():
            self.assertIn(f"{label}：值-{key}", prompt)

    def test_an_empty_task_produces_no_snapshot_lines(self):
        labels = [
            label
            for label, _ in prompt_catalog.entry("task-event.dispatch")["snapshot_fields"]
        ]
        prompt = render_task_prompt(
            event_context(),
            event_type="ready_entered",
            event_key="evt1",
            workflow_key="software-development",
            role_name="技术负责人",
            task={"record_id": "rec1", "status": "ready"},
            load_workflow=load_workflow_definition,
        )

        for label in labels:
            self.assertNotIn(f"{label}：", prompt)

    def test_the_workflow_still_supplies_the_dispatch_instruction(self):
        definition = load_workflow_definition("software-development")
        state = next(
            item for item in definition["lifecycle"]["states"] if item["key"] == "ready"
        )
        instruction = state["dispatch_instructions"]["zh-CN"]

        prompt = render_task_prompt(
            event_context(),
            event_type="ready_entered",
            event_key="evt1",
            workflow_key="software-development",
            role_name="技术负责人",
            task={"record_id": "rec1", "status": "ready"},
            load_workflow=load_workflow_definition,
        )

        self.assertTrue(prompt.endswith(instruction))

    def test_a_state_without_an_instruction_falls_back_to_the_catalog_default(self):
        default = prompt_catalog.render(
            "task-event.default-instruction", trigger="task_event"
        )

        prompt = render_task_prompt(
            event_context(),
            event_type="ready_entered",
            event_key="evt1",
            workflow_key="software-development",
            role_name="技术负责人",
            task={"record_id": "rec1", "status": "no-such-state"},
            load_workflow=load_workflow_definition,
        )

        self.assertTrue(prompt.endswith(default))


class ToolDescriptionTest(unittest.TestCase):
    def test_every_registered_tool_takes_its_description_from_the_asset(self):
        asset = json.loads(
            (
                prompt_catalog.PROMPTS_DIR
                / prompt_catalog.load_catalog()["tool_descriptions_file"]
            ).read_text(encoding="utf-8")
        )
        live = {name: tool.description for name, tool in mcp._tool_manager._tools.items()}

        self.assertEqual(len(live), 12)
        self.assertEqual(live, asset)

    def test_no_tool_falls_back_to_a_docstring(self):
        import core.mcp_server as server

        for name in mcp._tool_manager._tools:
            with self.subTest(tool=name):
                self.assertIsNone(getattr(server, name).__doc__)


if __name__ == "__main__":
    unittest.main()
