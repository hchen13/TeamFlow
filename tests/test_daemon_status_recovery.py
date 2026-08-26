from __future__ import annotations

import contextlib
import fcntl
import io
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.daemon import (
    TeamFlowDaemon,
    _daemon_request,
    daemon_socket_path,
    daemon_status,
    ensure_daemon,
    run_daemon,
    stop_daemon,
)
from core.global_db import teamflow_home
from core.schema_guard import SchemaCompatibilityError


ROOT = Path(__file__).resolve().parents[1]
RUNNING = {"running": True, "healthy": True, "stopping": False, "inbox": {}}
STOPPING = {
    "running": True,
    "healthy": False,
    "stopping": True,
    "failed_component": None,
    "consumer_error": None,
    "inbox": {},
}
OFFLINE = {"running": False, "healthy": False, "stopping": False, "inbox": {}}


class DaemonStatusRecoveryTest(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.home = tempfile.TemporaryDirectory(prefix="daemon-status-", dir=ROOT / "tmp")
        self.home_env = patch.dict(os.environ, {"TEAMFLOW_HOME": self.home.name})
        self.home_env.start()
        self.held = None

    def tearDown(self):
        if self.held:
            self.held.close()
        self.home_env.stop()
        self.home.cleanup()

    def hold_lock(self) -> None:
        # A running daemon owns its lifecycle lock, so a replacement cannot spawn while it lives.
        lock_path = teamflow_home() / "daemon.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.held = lock_path.open("a+", encoding="utf-8")
        fcntl.flock(self.held, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_an_unreadable_inbox_degrades_the_status_instead_of_failing_the_ping(self):
        socket_path = daemon_socket_path()
        pid_path = teamflow_home() / "daemon.pid"
        reported: list[dict] = []

        with contextlib.redirect_stdout(io.StringIO()):
            server = threading.Thread(target=run_daemon, daemon=True)
            server.start()
            self.until(lambda: socket_path.exists())
            with patch(
                "core.daemon.lark_event_counts",
                side_effect=SchemaCompatibilityError("global ledger mismatch"),
            ):
                reported.append(daemon_status())
            _daemon_request({"action": "shutdown"}, timeout=5)
            server.join(timeout=20)

        status = reported[0]
        # The daemon still answers, and says why it is not healthy.
        self.assertIs(status["running"], True)
        self.assertIs(status["healthy"], False)
        self.assertEqual(status["inbox"], {})
        self.assertEqual(status["failed_component"], "global-database")
        self.assertIn("global ledger mismatch", status["consumer_error"])
        self.assertIn("pid", status)

        self.assertFalse(server.is_alive())
        self.assertFalse(socket_path.exists(), "the daemon socket must still be released")
        self.assertFalse(pid_path.exists(), "the daemon pid file must still be released")
        self.assert_lock_free()

    def test_an_unreadable_inbox_keeps_a_component_failure_already_recorded(self):
        from core.daemon_monitor import DaemonMonitor

        monitor = DaemonMonitor(
            routes={},
            workers={},
            active_sessions=set(),
            stopping=threading.Event(),
            sync_lock=threading.RLock(),
            app_key=lambda context: "",
            read_failure=lambda: {"component": "deliveries", "error": "SystemExit: 23"},
            resolve=lambda name: (lambda: (_ for _ in ()).throw(OSError("database is locked"))),
        )

        status = monitor.status()

        self.assertEqual(status["failed_component"], "deliveries")
        self.assertEqual(status["consumer_error"], "SystemExit: 23")
        self.assertEqual(status["inbox"], {})
        self.assertIs(status["healthy"], False)

    def test_startup_status_does_not_wait_for_the_workspace_sync_lock(self):
        from core.daemon_monitor import DaemonMonitor

        sync_lock = threading.RLock()
        held = threading.Event()
        release = threading.Event()

        def hold_sync_lock() -> None:
            with sync_lock:
                held.set()
                release.wait()

        holder = threading.Thread(target=hold_sync_lock, daemon=True)
        holder.start()
        self.assertTrue(held.wait(1))
        monitor = DaemonMonitor(
            routes={},
            workers={},
            active_sessions=set(),
            stopping=threading.Event(),
            sync_lock=sync_lock,
            app_key=lambda context: "",
            read_failure=lambda: {},
            resolve=lambda name: (lambda: {}),
        )

        result: list[dict] = []
        finished = threading.Event()

        def read_status() -> None:
            result.append(monitor.status(include_topology=False))
            finished.set()

        reader = threading.Thread(target=read_status, daemon=True)
        reader.start()
        try:
            self.assertTrue(finished.wait(0.5), "startup status waited for workspace sync")
        finally:
            release.set()
            holder.join(1)
            reader.join(1)

        status = result[0]
        self.assertEqual(status["apps"], [])
        self.assertEqual(status["workspaces"], [])
        self.assertIs(status["healthy"], True)

    def test_real_daemon_reports_starting_while_workspace_sync_holds_the_lock(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_sync(runtime, workspace):
            with runtime.sync_lock:
                entered.set()
                release.wait()
            return {"workspace_root": workspace}

        with patch("core.daemon.registered_workspaces", return_value=["/slow"]), patch.object(
            TeamFlowDaemon,
            "sync_workspace",
            autospec=True,
            side_effect=slow_sync,
        ), contextlib.redirect_stdout(io.StringIO()):
            server = threading.Thread(target=run_daemon, daemon=True)
            server.start()
            try:
                self.until(lambda: daemon_socket_path().exists() and entered.is_set())

                status = daemon_status()

                self.assertIs(status["running"], True)
                self.assertIs(status["healthy"], True)
                self.assertIs(status["ready"], False)
                self.assertEqual(
                    status["startup"],
                    {
                        "state": "starting",
                        "total_workspaces": 1,
                        "completed_workspaces": 0,
                        "failed_workspaces": 0,
                        "current_workspace": "/slow",
                    },
                )
            finally:
                release.set()
                self.until(lambda: daemon_status().get("ready") is True)
                _daemon_request({"action": "shutdown"}, timeout=5)
                server.join(timeout=20)

        self.assertFalse(server.is_alive())

    def test_an_offline_status_never_reads_the_database(self):
        calls: list[int] = []

        with patch("core.daemon.lark_event_counts", side_effect=lambda: calls.append(1) or {}):
            status = daemon_status()

        self.assertEqual(calls, [], "an offline daemon must not be described from the database")
        self.assertIs(status["running"], False)
        self.assertEqual(status["inbox"], {})

    def test_a_daemon_that_is_already_stopping_is_not_asked_to_stop_again(self):
        self.hold_lock()
        with patch("core.daemon.daemon_status", return_value=STOPPING), patch(
            "core.daemon.stop_daemon"
        ) as stop, patch("core.daemon.LISTENER_CONNECT_TIMEOUT", 0.2), patch(
            "core.daemon.subprocess.Popen"
        ) as popen:
            with self.assertRaises(ValueError):
                ensure_daemon()

        stop.assert_not_called()
        popen.assert_not_called()

    def test_an_unhealthy_daemon_that_is_not_stopping_is_asked_once(self):
        self.hold_lock()
        unhealthy = {**STOPPING, "stopping": False, "consumer_error": "RuntimeError: dead"}

        with patch("core.daemon.daemon_status", return_value=unhealthy), patch(
            "core.daemon.stop_daemon"
        ) as stop, patch("core.daemon.LISTENER_CONNECT_TIMEOUT", 0.2), patch(
            "core.daemon.subprocess.Popen"
        ) as popen:
            with self.assertRaises(ValueError):
                ensure_daemon()

        stop.assert_called_once_with()
        popen.assert_not_called()

    def test_stopping_a_daemon_whose_socket_vanished_is_not_an_error(self):
        for error in (ConnectionRefusedError(61, "refused"), FileNotFoundError(2, "missing")):
            with self.subTest(error=type(error).__name__):
                with patch(
                    "core.daemon.daemon_status", side_effect=[RUNNING, OFFLINE]
                ), patch("core.daemon._daemon_request", side_effect=error):
                    self.assertEqual(stop_daemon(), OFFLINE)

    def test_stopping_an_offline_or_stopping_daemon_is_idempotent(self):
        with patch("core.daemon.daemon_status", return_value=OFFLINE), patch(
            "core.daemon._daemon_request"
        ) as request:
            self.assertEqual(stop_daemon(), {"running": False, "stopping": False})
        request.assert_not_called()

        with patch("core.daemon.daemon_status", return_value=STOPPING), patch(
            "core.daemon._daemon_request"
        ) as request:
            self.assertIs(stop_daemon()["stopping"], True)
        request.assert_not_called()

    def test_a_real_shutdown_failure_still_reaches_the_caller(self):
        for error in (ValueError("unknown TeamFlow daemon action"), TimeoutError("timed out")):
            with self.subTest(error=type(error).__name__):
                with patch("core.daemon.daemon_status", return_value=RUNNING), patch(
                    "core.daemon._daemon_request", side_effect=error
                ):
                    with self.assertRaises(type(error)):
                        stop_daemon()

    def assert_lock_free(self) -> None:
        with (teamflow_home() / "daemon.lock").open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def until(self, check, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.05)
        raise AssertionError("timed out waiting for the daemon to start")


if __name__ == "__main__":
    unittest.main()
