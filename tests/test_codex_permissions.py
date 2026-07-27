from __future__ import annotations

import os
import tempfile
import tomllib
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from core.codex_permissions import (
    TEAMFLOW_MCP_TOOLS,
    authorize_teamflow_mcp,
    inspect_teamflow_mcp_authorization,
)
from scripts.teamflow import cmd_register_agent, cmd_update_agent


class CodexPermissionTest(unittest.TestCase):
    def test_missing_config_is_not_authorized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"

            result = inspect_teamflow_mcp_authorization(path)

        self.assertFalse(result["authorized"])
        self.assertEqual(result["source"], "missing_config")
        self.assertEqual(result["missing_tools"], list(TEAMFLOW_MCP_TOOLS))

    def test_per_tool_approvals_are_recognized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            sections = [
                '[plugins."teamflow@teamflow"]',
                "enabled = true",
                "",
            ]
            for tool in TEAMFLOW_MCP_TOOLS:
                sections.extend([
                    (
                        '[plugins."teamflow@teamflow".mcp_servers.'
                        f"teamflow.tools.{tool}]"
                    ),
                    'approval_mode = "approve"',
                    "",
                ])
            path.write_text("\n".join(sections), encoding="utf-8")

            result = inspect_teamflow_mcp_authorization(path)

        self.assertTrue(result["authorized"])
        self.assertEqual(result["source"], "per_tool")
        self.assertEqual(result["missing_tools"], [])

    def test_tool_override_takes_precedence_over_server_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                "\n".join([
                    '[plugins."teamflow@teamflow"]',
                    "enabled = true",
                    "",
                    '[plugins."teamflow@teamflow".mcp_servers.teamflow]',
                    'default_tools_approval_mode = "approve"',
                    "",
                    (
                        '[plugins."teamflow@teamflow".mcp_servers.'
                        "teamflow.tools.get_task]"
                    ),
                    'approval_mode = "prompt"',
                    "",
                ]),
                encoding="utf-8",
            )

            result = inspect_teamflow_mcp_authorization(path)

        self.assertFalse(result["authorized"])
        self.assertEqual(result["source"], "missing_tools")
        self.assertEqual(result["missing_tools"], ["get_task"])

    def test_authorization_approves_current_tools_and_preserves_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            original = "\n".join([
                'model = "gpt-5"',
                "",
                '[plugins."teamflow@teamflow"]',
                "enabled = true",
                "",
                (
                    '[plugins."teamflow@teamflow".mcp_servers.'
                    "teamflow.tools.get_task]"
                ),
                'approval_mode = "prompt"',
                "",
            ])
            path.write_text(original, encoding="utf-8")

            with patch(
                "core.codex_permissions._codex_client_process_ids",
                return_value=set(),
            ):
                result = authorize_teamflow_mcp(
                    confirmed=True,
                    config_path=path,
                )

            parsed = tomllib.loads(path.read_text(encoding="utf-8"))
            server = (
                parsed["plugins"]["teamflow@teamflow"]["mcp_servers"]["teamflow"]
            )
            backup = Path(result["backup_path"])
            self.assertEqual(backup.read_text(encoding="utf-8"), original)
            self.assertNotIn("default_tools_approval_mode", server)
            self.assertTrue(all(
                server["tools"][tool]["approval_mode"] == "approve"
                for tool in TEAMFLOW_MCP_TOOLS
            ))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

        self.assertTrue(result["authorized"])
        self.assertTrue(result["configured"])
        self.assertTrue(result["changed"])
        self.assertFalse(result["restart_required"])

    def test_authorization_uses_background_runtime_until_codex_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.toml"
            ipc_path = root / "ipc.sock"
            path.write_text(
                "\n".join([
                    '[plugins."teamflow@teamflow"]',
                    "enabled = true",
                    "",
                ]),
                encoding="utf-8",
            )
            ipc_path.write_text("first runtime", encoding="utf-8")

            with patch(
                "core.codex_permissions._codex_client_process_ids",
                return_value=None,
            ):
                pending = authorize_teamflow_mcp(
                    confirmed=True,
                    config_path=path,
                    ipc_path=ipc_path,
                )

            self.assertTrue(pending["configured"])
            self.assertTrue(pending["authorized"])
            self.assertTrue(pending["activation_pending"])
            self.assertTrue(pending["restart_required"])
            self.assertEqual(
                pending["source"],
                "background_active_restart_pending",
            )
            self.assertIn("app-server", pending["warning"])
            replacement = root / "new-ipc.sock"
            replacement.write_text("second runtime", encoding="utf-8")
            os.replace(replacement, ipc_path)

            with patch(
                "core.codex_permissions._codex_client_process_ids",
                return_value=None,
            ):
                active = inspect_teamflow_mcp_authorization(
                    path,
                    ipc_path=ipc_path,
                )

        self.assertTrue(active["configured"])
        self.assertTrue(active["authorized"])
        self.assertFalse(active["activation_pending"])
        self.assertFalse(active["restart_required"])

    def test_authorization_waits_until_every_loaded_codex_client_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                "\n".join([
                    '[plugins."teamflow@teamflow"]',
                    "enabled = true",
                    "",
                ]),
                encoding="utf-8",
            )
            with patch(
                "core.codex_permissions._codex_client_process_ids",
                return_value={101, 202},
            ):
                pending = authorize_teamflow_mcp(
                    confirmed=True,
                    config_path=path,
                )
            with patch(
                "core.codex_permissions._codex_client_process_ids",
                return_value={202, 303},
            ):
                one_stale_client = inspect_teamflow_mcp_authorization(path)
            with patch(
                "core.codex_permissions._codex_client_process_ids",
                return_value={303},
            ):
                active = inspect_teamflow_mcp_authorization(path)
            marker = path.with_name(".teamflow-mcp-authorization.json")

        self.assertTrue(pending["activation_pending"])
        self.assertTrue(one_stale_client["activation_pending"])
        self.assertFalse(active["activation_pending"])
        self.assertFalse(marker.exists())

    def test_authorization_requires_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"

            with self.assertRaisesRegex(ValueError, "明确确认"):
                authorize_teamflow_mcp(
                    confirmed=False,
                    config_path=path,
                )

            self.assertFalse(path.exists())

    def test_authorization_does_not_create_a_missing_plugin_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('model = "gpt-5"\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "安装并启用"):
                authorize_teamflow_mcp(
                    confirmed=True,
                    config_path=path,
                )

            self.assertEqual(
                path.read_text(encoding="utf-8"),
                'model = "gpt-5"\n',
            )

    def test_register_agent_cannot_bypass_authorization(self):
        args = Namespace(
            workspace="/workspace",
            workflow="software-development",
            role="tl",
            harness_type="codex",
            session_id="session",
            display_name="TL",
            replace_role=False,
        )
        with patch(
            "scripts.teamflow.require_teamflow_mcp_authorization",
            side_effect=ValueError("authorization required"),
        ), patch("scripts.teamflow.register_agent") as register:
            with self.assertRaisesRegex(ValueError, "authorization required"):
                cmd_register_agent(args)

        register.assert_not_called()

    def test_update_agent_cannot_bypass_authorization(self):
        args = Namespace(
            workspace="/workspace",
            agent_id="agent",
            session_id="session",
        )
        with patch(
            "scripts.teamflow.require_teamflow_mcp_authorization",
            side_effect=ValueError("authorization required"),
        ), patch("scripts.teamflow.update_agent") as update:
            with self.assertRaisesRegex(ValueError, "authorization required"):
                cmd_update_agent(args)

        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
