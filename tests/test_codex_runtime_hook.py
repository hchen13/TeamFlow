from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hooks.codex_runtime import find_codex_owner, record_runtime_event


class CodexRuntimeHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.teamflow_home = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"TEAMFLOW_HOME": str(self.teamflow_home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.owner_pid = 4100
        self.owner_started_at = "Sat Aug  2 10:00:00 2026"

    def test_finds_the_nearest_codex_process_owner(self) -> None:
        process = os.getpid()
        processes = {
            process: (4000, "Sat Aug  2 10:00:02 2026", "/usr/bin/python hook.py"),
            4000: (self.owner_pid, "Sat Aug  2 10:00:01 2026", "/bin/sh -c hook"),
            self.owner_pid: (1, self.owner_started_at, "/Applications/ChatGPT.app/codex app-server"),
        }

        owner = find_codex_owner(process_info=processes.get)

        self.assertEqual(owner, (self.owner_pid, self.owner_started_at))

    def test_records_turn_lifecycle_and_removes_ended_owner(self) -> None:
        process_info = self._process_info()
        base = {
            "session_id": "thread-1",
            "cwd": "/workspace",
            "model": "gpt-5.6-sol",
        }

        record_runtime_event(
            {**base, "hook_event_name": "UserPromptSubmit"},
            process_info=process_info,
        )
        record = self._only_record()
        self.assertEqual(record["status"], "active")
        self.assertEqual(record["owner_pid"], self.owner_pid)
        self.assertEqual(record["owner_started_at"], self.owner_started_at)

        record_runtime_event({**base, "hook_event_name": "Stop"}, process_info=process_info)
        self.assertEqual(self._only_record()["status"], "idle")

        record_runtime_event({**base, "hook_event_name": "SessionEnd"}, process_info=process_info)
        self.assertEqual(list((self.teamflow_home / "codex-runtime").glob("*.json")), [])

    def test_ignores_events_without_a_codex_owner(self) -> None:
        record_runtime_event(
            {"session_id": "thread-1", "hook_event_name": "SessionStart"},
            process_info=lambda _pid: None,
        )

        self.assertFalse((self.teamflow_home / "codex-runtime").exists())

    def test_status_reporting_never_breaks_a_codex_turn_when_storage_is_unavailable(self) -> None:
        with patch.object(Path, "mkdir", side_effect=PermissionError("read only")):
            record_runtime_event(
                {"session_id": "thread-1", "hook_event_name": "UserPromptSubmit"},
                process_info=self._process_info(),
            )

    def _process_info(self):
        process = os.getpid()
        processes = {
            process: (self.owner_pid, "Sat Aug  2 10:00:01 2026", "/usr/bin/python hook.py"),
            self.owner_pid: (1, self.owner_started_at, "/Applications/ChatGPT.app/codex app-server"),
        }
        return processes.get

    def _only_record(self) -> dict[str, object]:
        records = list((self.teamflow_home / "codex-runtime").glob("*.json"))
        self.assertEqual(len(records), 1)
        return json.loads(records[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
