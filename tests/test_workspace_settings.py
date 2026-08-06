from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.config import resolve_workspace_paths
from core.db import init_workspace, inspect_workspace
from core.workspace_settings import (
    read_workspace_settings,
    set_version_control,
    version_control_enabled,
)


ROOT = Path(__file__).resolve().parents[1]


class WorkspaceSettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="settings-", dir=ROOT / "tmp")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = self.temporary.name
        self.path = resolve_workspace_paths(self.workspace).settings_path

    def test_a_new_workspace_enables_version_control_and_init_is_idempotent(self):
        created = init_workspace(self.workspace, "Settings Demo")

        self.assertEqual(created["settings"]["version_control"]["enabled"], True)
        self.assertEqual(created["settings"]["schema_version"], 1)

        set_version_control(self.workspace, enabled=False)
        again = init_workspace(self.workspace, "Settings Demo")

        self.assertEqual(again["settings"]["version_control"]["enabled"], False)

    def test_inspect_reports_the_stored_setting_before_and_after_init(self):
        self.assertEqual(
            inspect_workspace(self.workspace)["settings"]["version_control"]["enabled"],
            True,
        )

        init_workspace(self.workspace, "Settings Demo")
        set_version_control(self.workspace, enabled=False)

        inspected = inspect_workspace(self.workspace)
        self.assertTrue(inspected["initialized"])
        self.assertEqual(inspected["settings"]["version_control"]["enabled"], False)
        self.assertFalse(version_control_enabled(self.workspace))

    def test_unreadable_or_malformed_settings_raise_instead_of_defaulting(self):
        init_workspace(self.workspace, "Settings Demo")

        self.path.write_text("{ not json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unreadable"):
            read_workspace_settings(self.workspace)

        self.path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "version_control must be an object"):
            read_workspace_settings(self.workspace)

        self.path.write_text(
            json.dumps({"schema_version": 1, "version_control": {"enabled": "yes"}}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            read_workspace_settings(self.workspace)

        self.path.write_text(
            json.dumps({"schema_version": 99, "version_control": {"enabled": True}}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "schema version"):
            read_workspace_settings(self.workspace)


if __name__ == "__main__":
    unittest.main()
