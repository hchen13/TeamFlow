from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock

from core.critical_runtime import CriticalComponents, describe_error
from core.lark_events import LarkEventContext
from core.lark_worker_runtime import LarkWorkerRuntime


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
APP_KEY = f"{CONTEXT.brand}:{CONTEXT.app_id}"


class BadStringError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("stringify failed")


class BlockingProcess:
    """Blocks the first liveness question so another thread can act while it is unanswered."""

    def __init__(self, reached: threading.Barrier, released: threading.Event) -> None:
        self.reached = reached
        self.released = released
        self.asked = 0

    def is_alive(self) -> bool:
        self.asked += 1
        if self.asked == 1:
            self.reached.wait(timeout=5)
            self.released.wait(timeout=5)
        return False

    def terminate(self) -> None:
        return None

    def join(self, timeout: float | None = None) -> None:
        return None


def worker_for(process) -> dict:
    errors = Mock()
    errors.get_nowait.return_value = "terminated on purpose"
    return {
        "context": CONTEXT,
        "credentials": (CONTEXT.app_id, CONTEXT.app_secret, CONTEXT.brand),
        "process": process,
        "ready": Mock(),
        "errors": errors,
    }


def worker_runtime(workers: dict, routes: dict, sync_lock: threading.RLock) -> LarkWorkerRuntime:
    return LarkWorkerRuntime(
        mp=None,
        event_queue=None,
        workers=workers,
        routes=routes,
        stopping=threading.Event(),
        routes_ready=threading.Event(),
        app_key=lambda context: f"{context.brand}:{context.app_id}",
        publish=lambda *args: None,
        process_event=lambda *args: None,
        stop_worker=lambda worker: None,
        resolve=lambda name: None,
        sync_lock=sync_lock,
    )


class WorkerCheckRaceTest(unittest.TestCase):
    def test_an_intentional_stop_racing_the_liveness_check_is_not_fatal(self):
        sync_lock = threading.RLock()
        reached = threading.Barrier(2)
        released = threading.Event()
        worker = worker_for(BlockingProcess(reached, released))
        runtime = worker_runtime({APP_KEY: worker}, {CONTEXT.workspace_root: CONTEXT}, sync_lock)
        raised: list[BaseException] = []

        def check() -> None:
            try:
                runtime.check_workers()
            except BaseException as error:
                raised.append(error)

        def stop() -> None:
            # The checker is now blocked with its liveness question unanswered.
            reached.wait(timeout=5)
            runtime.stop_worker(worker)
            released.set()

        checker = threading.Thread(target=check)
        stopper = threading.Thread(target=stop)
        checker.start()
        stopper.start()
        checker.join(timeout=10)
        stopper.join(timeout=10)

        self.assertFalse(checker.is_alive())
        self.assertEqual(raised, [], "a deliberate stop must never be read as a worker failure")

    def test_a_worker_that_really_died_is_still_fatal(self):
        sync_lock = threading.RLock()
        process = Mock()
        process.is_alive.return_value = False
        worker = worker_for(process)
        runtime = worker_runtime({APP_KEY: worker}, {CONTEXT.workspace_root: CONTEXT}, sync_lock)

        with self.assertRaises(RuntimeError) as failure:
            runtime.check_workers()

        self.assertIn(APP_KEY, str(failure.exception))
        self.assertIn("terminated on purpose", str(failure.exception))

    def test_a_worker_replaced_while_the_check_ran_is_not_fatal(self):
        sync_lock = threading.RLock()
        reached = threading.Barrier(2)
        released = threading.Event()
        workers = {APP_KEY: worker_for(BlockingProcess(reached, released))}
        runtime = worker_runtime(workers, {CONTEXT.workspace_root: CONTEXT}, sync_lock)
        raised: list[BaseException] = []

        def check() -> None:
            try:
                runtime.check_workers()
            except BaseException as error:
                raised.append(error)

        def replace() -> None:
            reached.wait(timeout=5)
            live = Mock()
            live.is_alive.return_value = True
            with sync_lock:
                workers[APP_KEY] = worker_for(live)
            released.set()

        checker = threading.Thread(target=check)
        replacer = threading.Thread(target=replace)
        checker.start()
        replacer.start()
        checker.join(timeout=10)
        replacer.join(timeout=10)

        self.assertEqual(raised, [], "a replaced worker is not the one that failed")


class CriticalComponentsTest(unittest.TestCase):
    def components(self, **overrides):
        failure = overrides.pop("failure", {})
        stopping = overrides.pop("stopping", threading.Event())
        fatal = threading.Event()
        logs: list[dict] = []

        def emit_log(message, **fields):
            logs.append({"message": message, **fields})

        critical = CriticalComponents(
            failure=failure,
            stopping=stopping,
            emit_log=overrides.pop("emit_log", emit_log),
            style=lambda message, _: message,
            on_fatal=fatal.set,
        )
        return critical, failure, stopping, fatal, logs

    def test_an_error_that_cannot_be_described_still_stops_the_daemon(self):
        critical, failure, stopping, fatal, logs = self.components()
        original = BadStringError()

        def run() -> None:
            raise original

        with self.assertRaises(BadStringError) as raised:
            critical.guard("probe", run)()

        self.assertIs(raised.exception, original, "the original failure must be the one that escapes")
        self.assertEqual(failure["component"], "probe")
        self.assertIn("BadStringError", failure["error"])
        self.assertTrue(stopping.is_set())
        self.assertTrue(fatal.is_set())
        self.assertEqual(logs[0]["message"], "COMPONENT FATAL")

    def test_a_report_that_fails_costs_neither_the_record_nor_the_shutdown(self):
        critical, failure, stopping, fatal, _ = self.components(
            emit_log=Mock(side_effect=OSError("the log stream is gone"))
        )
        original = RuntimeError("consumer died")

        with self.assertRaises(RuntimeError) as raised:
            critical.guard("probe", lambda: (_ for _ in ()).throw(original))()

        self.assertIs(raised.exception, original)
        self.assertEqual(failure, {"component": "probe", "error": "RuntimeError: consumer died"})
        self.assertTrue(stopping.is_set())
        self.assertTrue(fatal.is_set())

    def test_the_first_failure_is_recorded_atomically(self):
        # A second component fails in the middle of the first one's record being written.
        # Both write paths are hooked so the interleaving lands whether the record is committed
        # with an assignment or with setdefault.
        class Interleaving(dict):
            def __init__(self) -> None:
                super().__init__()
                self.reentered = False
                self.other = None

            def interleave(self, key) -> None:
                if key != "component" or self.reentered:
                    return
                self.reentered = True
                thread = threading.Thread(target=self.other)
                thread.start()
                thread.join(timeout=1)

            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                self.interleave(key)

            def setdefault(self, key, value):
                result = super().setdefault(key, value)
                self.interleave(key)
                return result

        failure = Interleaving()
        critical, _, _, _, _ = self.components(failure=failure)
        failure.other = lambda: critical.fail("second", RuntimeError("second failure"))

        critical.fail("first", RuntimeError("first failure"))

        self.assertEqual(failure["component"], "first")
        self.assertEqual(failure["error"], "RuntimeError: first failure")

    def test_two_components_failing_at_once_record_one_consistent_failure(self):
        critical, failure, _, _, _ = self.components()
        start = threading.Barrier(2)

        def fail(component: str) -> None:
            start.wait(timeout=5)
            critical.fail(component, RuntimeError(f"{component} failure"))

        threads = [threading.Thread(target=fail, args=(name,)) for name in ("alpha", "beta")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertIn(failure["component"], {"alpha", "beta"})
        self.assertEqual(failure["error"], f"RuntimeError: {failure['component']} failure")

    def test_a_loop_that_returns_while_the_daemon_runs_is_fatal(self):
        critical, failure, stopping, fatal, logs = self.components()

        with self.assertRaises(RuntimeError) as raised:
            critical.guard("deliveries", lambda: None)()

        self.assertIn("returned while the daemon was running", str(raised.exception))
        self.assertEqual(failure["component"], "deliveries")
        self.assertTrue(stopping.is_set())
        self.assertTrue(fatal.is_set())
        self.assertEqual(logs[0]["fields"]["component"], "deliveries")

    def test_a_loop_that_returns_during_shutdown_is_expected(self):
        stopping = threading.Event()
        stopping.set()
        critical, failure, _, fatal, logs = self.components(stopping=stopping)

        critical.guard("lark-events", lambda: None)()

        self.assertEqual(failure, {})
        self.assertFalse(fatal.is_set())
        self.assertEqual(logs, [])

    def test_describing_an_error_never_raises(self):
        self.assertEqual(describe_error(RuntimeError("plain")), "RuntimeError: plain")
        self.assertEqual(
            describe_error(BadStringError()),
            "BadStringError: <error description unavailable>",
        )


if __name__ == "__main__":
    unittest.main()
