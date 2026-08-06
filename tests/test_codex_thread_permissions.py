from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.codex import _run_codex_app_server_turn
from core.codex_rollout import codex_background_turn_permissions


class CodexThreadPermissionsTest(unittest.TestCase):
    def test_maps_persisted_full_access_to_unattended_full_access(self):
        permissions = self._permissions_for({"type": "disabled"})

        self.assertEqual(permissions, {
            "approvalPolicy": "never",
            "permissions": ":danger-full-access",
        })

    def test_maps_persisted_workspace_access_to_unattended_workspace_write(self):
        permissions = self._permissions_for({"type": "workspaceWrite"})

        self.assertEqual(permissions, {
            "approvalPolicy": "never",
            "permissions": ":workspace",
        })

    def test_defaults_unknown_or_missing_settings_to_workspace_write(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"CODEX_HOME": directory}):
                missing = codex_background_turn_permissions("missing")

        unknown = self._permissions_for({"type": "readOnly"})

        expected = {
            "approvalPolicy": "never",
            "permissions": ":workspace",
        }
        self.assertEqual(missing, expected)
        self.assertEqual(unknown, expected)

    def test_prefers_latest_explicit_profile_over_background_policy_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            rollout = codex_home / "sessions" / "rollout-thread_1.jsonl"
            rollout.parent.mkdir()
            rollout.write_text("\n".join([
                json.dumps({
                    "type": "response_item",
                    "payload": {
                        "thread_settings": {
                            "permission_profile": {"type": "disabled"},
                            "active_permission_profile": {"id": ":danger-full-access"},
                        }
                    },
                }),
                json.dumps({
                    "type": "response_item",
                    "payload": {
                        "thread_settings": {
                            "permission_profile": {"type": "managed"},
                        }
                    },
                }),
            ]), encoding="utf-8")
            database = codex_home / "state_5.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, sandbox_policy TEXT)"
                )
                connection.execute(
                    "INSERT INTO threads (id, rollout_path, sandbox_policy) VALUES (?, ?, ?)",
                    ("thread_1", str(rollout), json.dumps({"type": "managed"})),
                )
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                permissions = codex_background_turn_permissions("thread_1")

        self.assertEqual(permissions, {
            "approvalPolicy": "never",
            "permissions": ":danger-full-access",
        })

    def test_app_server_turn_receives_the_persisted_background_permissions(self):
        expected = {"ok": True, "transport": "app-server"}
        permissions = {
            "approvalPolicy": "never",
            "permissions": ":danger-full-access",
        }
        with (
            patch(
                "core.codex.codex_background_turn_permissions",
                return_value=permissions,
            ) as persisted,
            patch(
                "core.codex._APP_SERVER_RUNTIME.run_turn",
                return_value=expected,
            ) as run_turn,
        ):
            result = _run_codex_app_server_turn(
                "thread_1",
                "work",
                client_message_id="message_1",
                on_started=None,
                stop_event=None,
            )

        self.assertIs(result, expected)
        persisted.assert_called_once_with("thread_1")
        run_turn.assert_called_once_with(
            "thread_1",
            "work",
            client_message_id="message_1",
            on_started=None,
            stop_event=None,
            approval_policy="never",
            permission_profile=":danger-full-access",
        )

    def _permissions_for(self, sandbox_policy: dict[str, str]) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            database = codex_home / "state_5.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE threads (id TEXT PRIMARY KEY, sandbox_policy TEXT)"
                )
                connection.execute(
                    "INSERT INTO threads (id, sandbox_policy) VALUES (?, ?)",
                    ("thread_1", json.dumps(sandbox_policy)),
                )
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                return codex_background_turn_permissions("thread_1")


if __name__ == "__main__":
    unittest.main()
