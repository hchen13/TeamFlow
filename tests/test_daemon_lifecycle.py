from __future__ import annotations

import fcntl
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.daemon import (
    TeamFlowDaemon,
    daemon_socket_path,
    ensure_daemon,
    run_daemon,
)
from core.global_db import teamflow_home


ROOT = Path(__file__).resolve().parents[1]
HEALTHY = {"running": True, "healthy": True, "consumer_error": None}
UNHEALTHY = {"running": True, "healthy": False, "consumer_error": "SchemaCompatibilityError: x"}
STOPPED = {"running": False, "healthy": False, "consumer_error": None}


class DaemonLifecycleTest(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.home = tempfile.TemporaryDirectory(prefix="daemon-lifecycle-", dir=ROOT / "tmp")
        self.home_env = patch.dict(os.environ, {"TEAMFLOW_HOME": self.home.name})
        self.home_env.start()
        self.lock_path = teamflow_home() / "daemon.lock"
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.held = None

    def tearDown(self):
        self.release_lock()
        self.home_env.stop()
        self.home.cleanup()

    def hold_lock(self) -> None:
        self.held = self.lock_path.open("a+", encoding="utf-8")
        fcntl.flock(self.held, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release_lock(self) -> None:
        if self.held:
            self.held.close()
            self.held = None

    def test_a_replacement_never_starts_while_the_old_daemon_holds_the_lock(self):
        self.hold_lock()
        # The previous daemon already answered the shutdown request and closed its socket, but it
        # still owns the lifecycle lock while it winds down.
        statuses = [UNHEALTHY, STOPPED, STOPPED, STOPPED]

        with patch("core.daemon.daemon_status", side_effect=lambda: statuses.pop(0) if statuses else STOPPED), patch(
            "core.daemon.stop_daemon", return_value={"stopping": True}
        ) as stop, patch("core.daemon.LISTENER_CONNECT_TIMEOUT", 0.3), patch(
            "core.daemon.subprocess.Popen"
        ) as popen:
            with self.assertRaises(ValueError) as failure:
                ensure_daemon()

        self.assertIn("still shutting down", str(failure.exception))
        stop.assert_called_once_with()
        popen.assert_not_called()

    def test_a_replacement_starts_once_the_lock_is_released(self):
        self.hold_lock()
        released = threading.Event()

        def status():
            return HEALTHY if released.is_set() else STOPPED

        def release_then_spawn(*args, **kwargs):
            released.set()
            return None

        with patch("core.daemon.daemon_status", side_effect=status), patch(
            "core.daemon.LISTENER_CONNECT_TIMEOUT", 3
        ), patch("core.daemon.subprocess.Popen", side_effect=release_then_spawn) as popen:
            threading.Timer(0.2, self.release_lock).start()
            result = ensure_daemon()

        popen.assert_called_once()
        self.assertEqual(result, HEALTHY)

    def test_a_concurrent_caller_that_already_started_a_healthy_daemon_is_reused(self):
        # Another process holds the lock and is serving; this call must adopt it, not spawn again.
        self.hold_lock()
        statuses = [STOPPED, HEALTHY]

        with patch("core.daemon.daemon_status", side_effect=lambda: statuses.pop(0) if statuses else HEALTHY), patch(
            "core.daemon.LISTENER_CONNECT_TIMEOUT", 3
        ), patch("core.daemon.subprocess.Popen") as popen:
            result = ensure_daemon()

        popen.assert_not_called()
        self.assertEqual(result, HEALTHY)

    def test_a_healthy_daemon_is_returned_without_touching_the_lock(self):
        self.hold_lock()

        with patch("core.daemon.daemon_status", return_value=HEALTHY), patch(
            "core.daemon.subprocess.Popen"
        ) as popen:
            self.assertEqual(ensure_daemon(), HEALTHY)

        popen.assert_not_called()

    def test_a_fatal_consumer_stops_the_real_daemon_and_frees_its_lifecycle_files(self):
        created: list[TeamFlowDaemon] = []

        class Recording(TeamFlowDaemon):
            def __init__(self) -> None:
                super().__init__()
                created.append(self)

        socket_path = daemon_socket_path()
        pid_path = teamflow_home() / "daemon.pid"
        exit_code: list[int] = []

        with patch("core.daemon.TeamFlowDaemon", Recording):
            server = threading.Thread(target=lambda: exit_code.append(run_daemon()), daemon=True)
            server.start()
            self.until(lambda: created and socket_path.exists() and pid_path.exists())

            # This is the hook the event consumer calls when its loop dies.
            created[0].on_fatal()
            server.join(timeout=15)

        self.assertFalse(server.is_alive(), "run_daemon must return after a fatal consumer")
        self.assertEqual(exit_code, [0])
        self.assertFalse(socket_path.exists(), "the daemon socket must be removed")
        self.assertFalse(pid_path.exists(), "the daemon pid file must be removed")
        # The replacement can only start because the lifecycle lock came back.
        self.hold_lock()

    def until(self, check, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.05)
        raise AssertionError("timed out waiting for the daemon to start")


if __name__ == "__main__":
    unittest.main()
