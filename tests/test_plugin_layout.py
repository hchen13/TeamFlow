from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from core.db import DEFAULT_WORKFLOW_KEY
from core.workflow import DEFINITIONS_DIR, load_workflow_definitions


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = ROOT / "workflows"
SKILLS_DIR = ROOT / "skills"
AGENT_SKILL = SKILLS_DIR / "teamflow-agent"
REFERENCES_DIR = AGENT_SKILL / "references"
TOP_LEVEL_SKILLS = {"teamflow-setup", "teamflow-agent"}
WORKFLOW_KEYS = {"software-development", "general-task"}
RELATIVE_LINK = re.compile(r"\[[^\]]*\]\((?!https?://|#)([^)]+)\)")


def frontmatter_name(skill: Path) -> str:
    lines = skill.read_text(encoding="utf-8").splitlines()
    if lines[0] != "---":
        raise AssertionError(f"{skill} does not start with YAML frontmatter")
    for line in lines[1:]:
        if line == "---":
            break
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"{skill} has no frontmatter name")


def lifecycle_rule(definition: dict, action: str, key: str) -> dict:
    rules = definition["lifecycle"]["actions"][action]["rules"]
    return next(rule for rule in rules if rule["key"] == key)


def local_markdown_links(document: Path, root: Path) -> set[Path]:
    links: set[Path] = set()
    for target in RELATIVE_LINK.findall(document.read_text(encoding="utf-8")):
        resolved = (document.parent / target.split("#", 1)[0]).resolve()
        if resolved.suffix == ".md" and resolved.is_relative_to(root):
            links.add(resolved)
    return links


class PluginLayoutTests(unittest.TestCase):
    def test_workflow_definitions_live_under_the_workflows_directory(self):
        self.assertEqual(DEFINITIONS_DIR, WORKFLOWS_DIR)

        definitions = load_workflow_definitions(WORKFLOWS_DIR)

        self.assertEqual(set(definitions), WORKFLOW_KEYS)
        self.assertIn(DEFAULT_WORKFLOW_KEY, definitions)
        self.assertEqual(
            {definition["key"] for definition in definitions.values()},
            {path.parent.name for path in WORKFLOWS_DIR.glob("*/workflow.json")},
        )

    def test_workflow_definitions_are_no_longer_exposed_as_skills(self):
        self.assertEqual(list(SKILLS_DIR.rglob("workflow.json")), [])
        self.assertFalse((SKILLS_DIR / "software-development").exists())
        self.assertFalse((SKILLS_DIR / "general-task").exists())

    def test_only_the_two_declared_top_level_skills_are_exposed(self):
        manifest = json.loads(
            (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )

        self.assertEqual(manifest["skills"], "./skills/")
        self.assertEqual(
            {path.parent.name for path in SKILLS_DIR.glob("*/SKILL.md")},
            TOP_LEVEL_SKILLS,
        )
        self.assertEqual(TOP_LEVEL_SKILLS & WORKFLOW_KEYS, set())
        for name in TOP_LEVEL_SKILLS:
            self.assertEqual(frontmatter_name(SKILLS_DIR / name / "SKILL.md"), name)

    def test_every_installed_workflow_has_one_reference_entrypoint(self):
        installed = set(load_workflow_definitions(WORKFLOWS_DIR))

        for key in installed:
            entrypoint = REFERENCES_DIR / key / "overview.md"
            self.assertTrue(entrypoint.is_file(), f"missing {entrypoint}")
            self.assertFalse((REFERENCES_DIR / f"{key}.md").exists())

    def test_every_relative_link_in_the_skills_resolves(self):
        for document in sorted(SKILLS_DIR.rglob("*.md")):
            for target in RELATIVE_LINK.findall(document.read_text(encoding="utf-8")):
                resolved = (document.parent / target.split("#", 1)[0]).resolve()
                self.assertTrue(
                    resolved.exists(), f"{document} links to missing {target}"
                )

    def test_every_skill_markdown_is_reachable_from_its_entrypoint(self):
        for skill_file in sorted(SKILLS_DIR.glob("*/SKILL.md")):
            root = skill_file.parent.resolve()
            reachable: set[Path] = set()
            pending = [skill_file.resolve()]
            while pending:
                document = pending.pop()
                if document in reachable:
                    continue
                reachable.add(document)
                pending.extend(local_markdown_links(document, root) - reachable)

            self.assertEqual(
                reachable,
                {path.resolve() for path in root.rglob("*.md")},
            )

    def test_software_development_contract_supports_gate_recovery(self):
        definition = load_workflow_definitions(WORKFLOWS_DIR)["software-development"]

        self.assertIn("chore", {item["key"] for item in definition["task_types"]})
        resume = lifecycle_rule(definition, "route", "resume")
        self.assertEqual((resume["from"], resume["to"]), (["blocked"], "ready"))
        self.assertIn("role", resume["required_inputs"])
        rework = lifecycle_rule(definition, "review", "rework")
        self.assertEqual((rework["from"], rework["to"]), (["review"], "ready"))
        cancel = lifecycle_rule(definition, "cancel", "cancel")
        self.assertIn("blocked", cancel["from"])

if __name__ == "__main__":
    unittest.main()
