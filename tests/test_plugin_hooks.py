from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PluginHookCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        hooks = self.hooks
        self.command = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.runtime_commands = [
            hooks["hooks"][event][0]["hooks"][0]["command"]
            for event in ("SessionStart", "SessionEnd", "Stop")
        ]

    def test_the_setup_skill_asks_users_to_trust_every_registered_hook(self):
        guidance = (ROOT / "skills" / "teamflow-setup" / "SKILL.md").read_text(encoding="utf-8")
        registered = set(self.hooks["hooks"])

        named = {event for event in registered if f"`{event}`" in guidance}

        self.assertEqual(named, registered)
        self.assertNotIn("PostCompact", guidance)

    def test_compaction_recovery_is_driven_by_session_start_alone(self):
        events = self.hooks["hooks"]

        self.assertNotIn("PostCompact", events)
        self.assertIn("compact", events["SessionStart"][0]["matcher"])
        self.assertGreaterEqual(events["SessionStart"][0]["hooks"][0]["timeout"], 60)

    def test_uses_the_configured_plugin_runtime_when_it_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache_root = Path(temporary) / "teamflow"
            configured = self._runtime(cache_root, "0.1.0+codex.older")
            self._runtime(cache_root, "0.1.0+codex.newer")

            result = self._run(configured, Path(temporary))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"--project\n{configured}\n", result.stdout)

    def test_falls_back_to_the_latest_valid_cached_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache_root = Path(temporary) / "teamflow"
            self._runtime(cache_root, "0.1.0+codex.20260728000000")
            latest = self._runtime(cache_root, "0.1.0+codex.20260729000000")
            (cache_root / "0.1.0+codex.20260730000000").mkdir(parents=True)

            result = self._run(cache_root / "0.1.0+codex.removed", Path(temporary))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"--project\n{latest}\n", result.stdout)
        self.assertNotIn("has no effect when used outside of a project", result.stderr)

    def test_reports_when_no_plugin_runtime_is_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache_root = Path(temporary) / "teamflow"
            cache_root.mkdir()

            result = self._run(cache_root / "0.1.0+codex.removed", Path(temporary))

        self.assertEqual(result.returncode, 1)
        self.assertIn("TeamFlow hook runtime is unavailable", result.stderr)

    def test_runtime_hooks_fall_back_to_the_latest_valid_cached_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache_root = Path(temporary) / "teamflow"
            self._runtime(cache_root, "0.1.0+codex.20260728000000")
            latest = self._runtime(cache_root, "0.1.0+codex.20260729000000")

            results = [
                self._run(cache_root / "0.1.0+codex.removed", Path(temporary), command)
                for command in self.runtime_commands
            ]

        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"--project\n{latest}\n", result.stdout)

    def _runtime(self, cache_root: Path, version: str) -> Path:
        runtime = cache_root / version
        script = runtime / "hooks" / "user_prompt_submit.py"
        script.parent.mkdir(parents=True)
        script.touch()
        (runtime / "hooks" / "session_runtime.py").touch()
        return runtime

    def _run(
        self,
        plugin_root: Path,
        temporary: Path,
        command_template: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        fake_bin = temporary / "bin"
        fake_bin.mkdir(exist_ok=True)
        uv = fake_bin / "uv"
        uv.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
        uv.chmod(0o755)
        command = (command_template or self.command).replace("${PLUGIN_ROOT}", str(plugin_root))
        env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}")
        return subprocess.run(command, shell=True, capture_output=True, text=True, env=env)


if __name__ == "__main__":
    unittest.main()
