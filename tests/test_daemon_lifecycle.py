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
from unittest.mock import Mock, patch

from core.daemon import (
    TeamFlowDaemon,
    _daemon_request,
    daemon_socket_path,
    ensure_daemon,
    run_daemon,
)
from core.delivery_runtime import DeliveryRuntime
from core.global_db import teamflow_home
from core.lark_events import LarkEventContext


ROOT = Path(__file__).resolve().parents[1]
HEALTHY = {"running": True, "healthy": True, "consumer_error": None}
UNHEALTHY = {"running": True, "healthy": False, "consumer_error": "SchemaCompatibilityError: x"}
STOPPED = {"running": False, "healthy": False, "consumer_error": None}
CONTEXT = LarkEventContext(
    workspace_root="/workspace",
    db_path="/workspace/.teamflow/teamflow.db",
    identity_id="identity",
    identity_name="Identity",
    app_id="cli_test",
    app_name="Test app",
    app_secret="secret",
    auth_mode="bot",
    user_open_id="",
    board_url="https://example.feishu.cn/base/bascnTest?table=tblTest",
    file_token="bascnTest",
    table_id="tblTest",
    brand="feishu",
    workspace_name="workspace",
    workflow_key="software-development",
)


def dead_worker(reason: str) -> dict:
    errors = Mock()
    errors.get_nowait.return_value = reason
    process = Mock()
    process.is_alive.return_value = False
    return {
        "context": CONTEXT,
        "credentials": (CONTEXT.app_id, CONTEXT.app_secret, CONTEXT.brand),
        "process": process,
        "ready": Mock(),
        "errors": errors,
    }


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

    def test_a_real_consumer_failure_is_reported_and_shuts_the_daemon_down(self):
        # Nothing here is stubbed except the call that fails: the daemon builds its own worker
        # runtime, so a report that only works with a hand-made resolver would not be logged.
        socket_path = daemon_socket_path()
        pid_path = teamflow_home() / "daemon.pid"
        output = io.StringIO()
        exit_code: list[int] = []

        with patch(
            "core.daemon.due_lark_event_ids",
            side_effect=RuntimeError("the inbox query failed"),
        ):
            with contextlib.redirect_stdout(output):
                server = threading.Thread(
                    target=lambda: exit_code.append(run_daemon()),
                    daemon=True,
                )
                server.start()
                server.join(timeout=20)

        logged = output.getvalue()
        self.assertFalse(server.is_alive(), "run_daemon must return after a fatal consumer")
        self.assertEqual(exit_code, [0])
        self.assertIn("COMPONENT FATAL", logged)
        self.assertIn("RuntimeError", logged)
        self.assertIn("the inbox query failed", logged)
        self.assertFalse(socket_path.exists(), "the daemon socket must be removed")
        self.assertFalse(pid_path.exists(), "the daemon pid file must be removed")
        self.hold_lock()

    def test_an_interrupt_in_the_consumer_still_shuts_the_real_daemon_down(self):
        # SystemExit is not an Exception, but it leaves the inbox just as undrained.
        logged, exit_code, server = self.run_daemon_until_exit(
            patch("core.daemon.due_lark_event_ids", side_effect=SystemExit(1))
        )

        self.assertFalse(server.is_alive())
        self.assertEqual(exit_code, [0])
        self.assertIn("COMPONENT FATAL", logged)
        self.assertIn("SystemExit", logged)
        self.assertFalse(daemon_socket_path().exists())
        self.assertFalse((teamflow_home() / "daemon.pid").exists())
        self.hold_lock()

    def test_a_fatal_consumer_exits_without_creating_a_thread(self):
        socket_path = daemon_socket_path()
        pid_path = teamflow_home() / "daemon.pid"
        running = threading.Event()
        real_start = threading.Thread.start

        def refuse_new_threads(thread):
            # Every thread the daemon needs was started before this point. Unrelated runtime
            # internals keep working so the test isolates the shutdown path.
            if running.is_set() and str(thread.name).startswith("teamflow-"):
                raise RuntimeError("thread creation is unavailable")
            return real_start(thread)

        with contextlib.redirect_stdout(io.StringIO()) as output:
            server = threading.Thread(target=run_daemon, daemon=True)
            server.start()
            self.until(lambda: socket_path.exists() and pid_path.exists())
            time.sleep(0.3)
            running.set()
            with patch.object(threading.Thread, "start", refuse_new_threads), patch(
                "core.daemon.due_lark_event_ids",
                side_effect=RuntimeError("the inbox query failed"),
            ):
                server.join(timeout=20)

        self.assertFalse(server.is_alive(), "the exit must not depend on starting a thread")
        self.assertIn("COMPONENT FATAL", output.getvalue())
        self.assertFalse(socket_path.exists(), "the daemon socket must be released")
        self.assertFalse(pid_path.exists(), "the daemon pid file must be released")
        self.hold_lock()

    def test_a_dead_delivery_scheduler_shuts_the_real_daemon_down(self):
        logged, exit_code, server = self.run_daemon_until_exit(
            patch.object(DeliveryRuntime, "consume", side_effect=SystemExit(23))
        )

        self.assertFalse(server.is_alive(), "a dead delivery scheduler must not be survivable")
        self.assertEqual(exit_code, [0])
        self.assertIn("COMPONENT FATAL", logged)
        self.assertIn("component=deliveries", logged)
        self.assertIn("SystemExit", logged)
        self.assertFalse(daemon_socket_path().exists())
        self.assertFalse((teamflow_home() / "daemon.pid").exists())
        self.hold_lock()

    def test_a_routed_lark_worker_that_dies_at_runtime_shuts_the_daemon_down(self):
        created: list[TeamFlowDaemon] = []

        class Recording(TeamFlowDaemon):
            def __init__(self) -> None:
                super().__init__()
                created.append(self)

        def route_a_dead_worker() -> None:
            runtime = created[0]
            # The worker connected and was routed, then its process exited on its own. Nothing in
            # the daemon reads its error queue again after the initial handshake.
            with runtime.sync_lock:
                runtime.workers[runtime.app_key(CONTEXT)] = dead_worker("websocket closed")
                runtime.routes[CONTEXT.workspace_root] = CONTEXT
            runtime.routes_ready.set()

        with patch("core.daemon.TeamFlowDaemon", Recording):
            logged, exit_code, server = self.run_daemon_until_exit(
                after_start=lambda: (self.until(lambda: bool(created)), route_a_dead_worker())
            )

        self.assertFalse(server.is_alive(), "a dead board listener must not be survivable")
        self.assertEqual(exit_code, [0])
        self.assertIn("COMPONENT FATAL", logged)
        self.assertIn("component=lark-events", logged)
        self.assertIn("websocket closed", logged)
        self.assertFalse(daemon_socket_path().exists())
        self.hold_lock()

    def test_workers_this_daemon_stopped_on_purpose_are_not_fatal(self):
        runtime = TeamFlowDaemon()
        try:
            worker = dead_worker("terminated on purpose")
            with runtime.sync_lock:
                runtime.workers[runtime.app_key(CONTEXT)] = worker
                runtime.routes[CONTEXT.workspace_root] = CONTEXT

            # Replacing credentials, disabling a workspace, and closing down all go through this.
            runtime.lark_workers.stop_worker(worker)
            runtime.lark_workers.check_workers()

            # A worker that is no longer routed is not watched either.
            with runtime.sync_lock:
                runtime.workers[runtime.app_key(CONTEXT)] = dead_worker("unrouted")
                runtime.routes.clear()
            runtime.lark_workers.check_workers()

            self.assertEqual(runtime.consumer_failure, {})
            self.assertIs(runtime.status()["healthy"], True)
        finally:
            with runtime.sync_lock:
                runtime.workers.clear()
                runtime.routes.clear()
            runtime.close()

    def test_a_daemon_that_accepted_a_shutdown_stops_reporting_itself_healthy(self):
        response: list[dict] = []

        def request_shutdown() -> None:
            response.append(_daemon_request({"action": "shutdown"}, timeout=5))

        logged, exit_code, server = self.run_daemon_until_exit(after_start=request_shutdown)

        self.assertFalse(server.is_alive())
        self.assertEqual(exit_code, [0])
        self.assertIs(response[0]["stopping"], True)
        self.assertIs(response[0]["running"], True)
        # A concurrent ensure_daemon reading this reply must not adopt a daemon on its way out.
        self.assertIs(response[0]["healthy"], False)

    def test_an_accepted_shutdown_still_exits_when_its_response_fails(self):
        created: list[TeamFlowDaemon] = []

        class Recording(TeamFlowDaemon):
            def __init__(self) -> None:
                super().__init__()
                created.append(self)

        socket_path = daemon_socket_path()
        pid_path = teamflow_home() / "daemon.pid"
        response: list[str] = []
        exit_code: list[int] = []

        with patch("core.daemon.TeamFlowDaemon", Recording):
            with contextlib.redirect_stdout(io.StringIO()):
                server = threading.Thread(
                    target=lambda: exit_code.append(run_daemon()),
                    daemon=True,
                )
                server.start()
                self.until(lambda: bool(created) and socket_path.exists())
                # Only reading the status back fails. The shutdown was already accepted, so the
                # process still owes its exit; nothing here calls server.shutdown directly.
                with patch.object(
                    Recording,
                    "status",
                    side_effect=ValueError("status failed after shutdown began"),
                ):
                    try:
                        _daemon_request({"action": "shutdown"}, timeout=5)
                    except Exception as error:
                        response.append(str(error))
                server.join(timeout=20)

        self.assertEqual(response, ["status failed after shutdown began"])
        self.assertFalse(server.is_alive(), "an accepted shutdown must still exit")
        self.assertEqual(exit_code, [0])
        self.assertTrue(created[0].stopping.is_set())
        self.assertFalse(socket_path.exists(), "the daemon socket must be removed")
        self.assertFalse(pid_path.exists(), "the daemon pid file must be removed")
        self.hold_lock()

    def test_a_stopping_daemon_is_never_adopted_while_it_holds_the_lock(self):
        self.hold_lock()
        stopping = {"running": True, "healthy": False, "stopping": True, "consumer_error": None}

        with patch("core.daemon.daemon_status", return_value=stopping), patch(
            "core.daemon.stop_daemon", return_value={"stopping": True}
        ), patch("core.daemon.LISTENER_CONNECT_TIMEOUT", 0.3), patch(
            "core.daemon.subprocess.Popen"
        ) as popen:
            with self.assertRaises(ValueError) as failure:
                ensure_daemon()

        self.assertIn("still shutting down", str(failure.exception))
        popen.assert_not_called()

    def test_a_fatal_before_the_shutdown_handler_exists_is_not_lost(self):
        runtime = TeamFlowDaemon()
        try:
            # This is the window between the consumer threads starting and run_daemon installing
            # the handler that knows how to stop the server.
            runtime.on_fatal()
            installed: list[str] = []
            runtime.set_fatal_shutdown(lambda: installed.append("shutdown"))

            self.assertEqual(installed, ["shutdown"], "a pending fatal must be honoured")
        finally:
            runtime.close()

    def test_lifecycle_files_are_released_even_when_close_fails(self):
        socket_path = daemon_socket_path()
        pid_path = teamflow_home() / "daemon.pid"
        failures: list[BaseException] = []

        def run() -> None:
            try:
                run_daemon()
            except BaseException as error:
                failures.append(error)

        with patch.object(TeamFlowDaemon, "close", side_effect=RuntimeError("close failed")):
            with contextlib.redirect_stdout(io.StringIO()):
                server = threading.Thread(target=run, daemon=True)
                server.start()
                self.until(lambda: socket_path.exists() and pid_path.exists())
                _daemon_request({"action": "shutdown"}, timeout=5)
                server.join(timeout=20)

        self.assertFalse(server.is_alive())
        self.assertEqual([str(error) for error in failures], ["close failed"])
        self.assertFalse(socket_path.exists(), "the socket must be removed even when close fails")
        self.assertFalse(pid_path.exists(), "the pid file must be removed even when close fails")
        self.hold_lock()

    def run_daemon_until_exit(self, patched=None, *, after_start=None):
        output = io.StringIO()
        exit_code: list[int] = []
        with contextlib.ExitStack() as stack:
            if patched:
                stack.enter_context(patched)
            stack.enter_context(contextlib.redirect_stdout(output))
            server = threading.Thread(target=lambda: exit_code.append(run_daemon()), daemon=True)
            server.start()
            if after_start:
                self.until(lambda: daemon_socket_path().exists())
                after_start()
            server.join(timeout=20)
        return output.getvalue(), exit_code, server

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
