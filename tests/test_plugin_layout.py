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
SETUP_SKILL = SKILLS_DIR / "teamflow-setup" / "SKILL.md"
SOFTWARE_DEVELOPMENT_REFERENCE = REFERENCES_DIR / "software-development.md"
GENERAL_TASK_REFERENCE = REFERENCES_DIR / "general-task.md"
TOP_LEVEL_SKILLS = {"teamflow-setup", "teamflow-agent"}
WORKFLOW_KEYS = {"software-development", "general-task"}
RELATIVE_LINK = re.compile(r"\[[^\]]*\]\((?!https?://|#)([^)]+)\)")
REFERENCE_ROW = re.compile(r"\|\s*`([^`]+)`\s*\|\s*\[[^\]]*\]\(references/([^)]+)\.md\)\s*\|")


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

    def test_agent_skill_references_every_installed_workflow(self):
        installed = set(load_workflow_definitions(WORKFLOWS_DIR))

        self.assertEqual({path.stem for path in REFERENCES_DIR.glob("*.md")}, installed)

        skill = (AGENT_SKILL / "SKILL.md").read_text(encoding="utf-8")

        self.assertEqual(
            dict(REFERENCE_ROW.findall(skill)),
            {key: key for key in installed},
        )

    def test_every_relative_link_in_the_skills_resolves(self):
        for document in sorted(SKILLS_DIR.rglob("*.md")):
            for target in RELATIVE_LINK.findall(document.read_text(encoding="utf-8")):
                resolved = (document.parent / target.split("#", 1)[0]).resolve()
                self.assertTrue(
                    resolved.exists(), f"{document} links to missing {target}"
                )

    def test_setup_skill_uses_the_installed_plugin_launcher_and_completes_setup(self):
        skill = SETUP_SKILL.read_text(encoding="utf-8")

        self.assertIn('TEAMFLOW_ROOT=$(CDPATH= cd "$(dirname "$SKILL_FILE")/../.." && pwd)', skill)
        self.assertIn('TF="$TEAMFLOW_ROOT/teamflow"', skill)
        self.assertIn("LARK_CLI_BRAND=feishu", skill)
        self.assertIn('*.larksuite.com 使用 lark', skill)
        self.assertIn('"$TF" initialize-lark-board', skill)
        self.assertIn('"$TF" daemon enable --workspace "$PROJECT_ROOT"', skill)
        self.assertIn("UI **不会**初始化任务表", skill)
        self.assertIn("不会仅因打开页面就启用 daemon workspace", skill)
        self.assertIn('URL 显式带 `table` 时', skill)

    def test_setup_skill_covers_user_auth_and_codex_authorization_boundaries(self):
        skill = SETUP_SKILL.read_text(encoding="utf-8")

        self.assertIn('"$LARK_CLI" config init', skill)
        self.assertIn("--app-secret-stdin", skill)
        self.assertIn('--brand "$LARK_CLI_BRAND"', skill)
        self.assertIn("完整权限修复链接", skill)
        self.assertIn("插件页没有逐 MCP 工具授权开关", skill)
        self.assertIn("授权后先重启当前正在运行的 Codex 客户端", skill)

    def test_software_development_reference_preserves_evidence_and_positive_flow_semantics(self):
        reference = SOFTWARE_DEVELOPMENT_REFERENCE.read_text(encoding="utf-8")

        self.assertIn("`result_evidence` 是替换字段", reference)
        self.assertIn("另建一张短期 promotion 卡", reference)
        self.assertIn("成功路径不对 gate 调用 `rework`", reference)
        self.assertIn("该提交会改变候选内容", reference)
        self.assertIn("这是写作约定，不是访问控制", reference)
        self.assertIn("取消已阻塞的旧 promotion 卡", reference)
        self.assertIn("旧 QA 结论不得沿用", reference)
        self.assertNotIn("QA 已通过，转 TL 执行晋升", reference)

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

    def test_general_task_reference_does_not_fake_blocking_for_routing(self):
        reference = GENERAL_TASK_REFERENCE.read_text(encoding="utf-8")

        self.assertIn("不要为了换职责伪造阻塞", reference)
        self.assertNotIn("先阻塞再解除", reference)


if __name__ == "__main__":
    unittest.main()
