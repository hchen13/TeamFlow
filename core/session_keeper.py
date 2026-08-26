from __future__ import annotations

import threading
import time
from typing import Any, Callable


# A plain client disconnect drops this client's follows at once. The 5000ms
# followerReconnectGraceMs only covers an IPC connection reset, during which the owner holds
# the follows as pending. Reconnecting inside it is what avoids restarting the owner's
# inactivity clock, so the first attempts are dense before settling to a low rate.
_RECONNECT_DELAYS = (0.0, 0.25, 0.5, 1.0, 2.0)
_RETRY_INTERVAL = 5.0
_DRAIN_TIMEOUT = 0.5
_REFRESH_INTERVAL = 60.0


class SessionKeeper:
    """Keeps declaring `following` for registered Codex sessions over one IPC connection.

    Following only exempts an already loaded session from the owner's idle unsubscribe; it
    cannot load a session that is gone. Nothing here decides whether a delivery may run.
    """

    def __init__(
        self,
        *,
        desired_sessions: Callable[[], set[str]],
        connect: Callable[[], Any],
        stopping: threading.Event,
        emit_log: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.desired_sessions = desired_sessions
        self.connect = connect
        self.stopping = stopping
        self.emit_log = emit_log
        self.wakeup = threading.Event()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.connection: Any | None = None
        self.following: set[str] = set()
        self.desired: set[str] = set()
        self.connects = 0
        self.last_error: str | None = None
        self.closed = False
        self.attempt = 0
        self.dirty = True
        self.refresh_at = 0.0

    def start(self) -> None:
        with self.lock:
            if self.thread is not None or self.closed:
                return
            self.thread = threading.Thread(target=self._run, name="teamflow-keeper", daemon=True)
            thread = self.thread
        thread.start()

    def wake(self) -> None:
        self.dirty = True
        self.wakeup.set()

    def close(self) -> None:
        with self.lock:
            already = self.closed
            self.closed = True
            thread = self.thread
        self.wakeup.set()
        if already:
            return
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._disconnect(unfollow=True)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "connected": self.connection is not None,
                # These are follow declarations, not proof that Desktop currently has the
                # sessions loaded. Keep the protocol names below for compatibility.
                "declared_sessions": len(self.following),
                "registered_sessions": len(self.desired),
                "following": len(self.following),
                "desired": len(self.desired),
                "connects": self.connects,
                "last_error": self.last_error,
            }

    def _run(self) -> None:
        try:
            while not self._stopping():
                try:
                    delay = self._tick()
                except Exception as error:
                    # A keeper failure must never reach the daemon: deliveries keep waiting
                    # on the existing owner probe whether or not the follow is held.
                    self._fail(error)
                    self._disconnect(unfollow=False)
                    delay = self._reconnect_delay()
                if self.wakeup.wait(delay):
                    self.wakeup.clear()
        finally:
            self._disconnect(unfollow=True)

    def _tick(self) -> float:
        # Draining paces the loop in tenths of a second; re-reading the registry that often
        # would open SQLite dozens of times a second for a set that changes on registration.
        if self.dirty or time.monotonic() >= self.refresh_at:
            self.dirty = False
            self.refresh_at = time.monotonic() + _REFRESH_INTERVAL
            refreshed = self._read_desired()
            with self.lock:
                self.desired = refreshed
        with self.lock:
            desired = set(self.desired)
        if self.connection is None and not self._open():
            return self._reconnect_delay()
        if self._stopping():
            return 0.0
        self._apply(desired)
        self._drain()
        return 0.0

    def _read_desired(self) -> set[str]:
        try:
            return {str(s).strip() for s in self.desired_sessions() if str(s or "").strip()}
        except Exception as error:
            self._fail(error)
            return set(self.desired)

    def _open(self) -> bool:
        try:
            connection = self.connect()
        except Exception as error:
            self.attempt += 1
            self._fail(error)
            return False
        if self._stopping():
            # close() ran while connect() was blocked: this connection was never announced,
            # so it is dropped without a single follow.
            try:
                connection.close()
            except Exception:
                pass
            return False
        with self.lock:
            self.connection = connection
            self.following = set()
            self.connects += 1
            self.last_error = None
        self.attempt = 0
        return True

    def _reconnect_delay(self) -> float:
        exhausted = self.attempt >= len(_RECONNECT_DELAYS)
        return _RETRY_INTERVAL if exhausted else _RECONNECT_DELAYS[self.attempt]

    def _apply(self, desired: set[str]) -> None:
        with self.lock:
            following = set(self.following)
        for session_id in sorted(desired - following):
            self.connection.follow(session_id)
        for session_id in sorted(following - desired):
            self.connection.unfollow(session_id)
        with self.lock:
            self.following = set(desired)

    def _redeclare(
        self,
        sessions: set[str],
        *,
        target_client_ids: list[str] | None = None,
    ) -> None:
        with self.lock:
            wanted = sorted(sessions & self.desired)
        for session_id in wanted:
            self.connection.follow(
                session_id,
                force=True,
                target_client_ids=target_client_ids,
            )

    def _drain(self) -> None:
        deadline = time.monotonic() + _DRAIN_TIMEOUT
        while not self._stopping():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            message = self.connection._receive_once(min(0.1, remaining))
            if not isinstance(message, dict):
                # select already waited out the slice, so a quiet socket ends the drain.
                return
            self._handle(message)

    def _handle(self, message: dict[str, Any]) -> None:
        if message.get("type") != "broadcast":
            return
        method = message.get("method")
        params = message.get("params") or {}
        # Re-announcing costs one broadcast and is idempotent, so it runs whatever version
        # the notice carries: missing the reset notice would cost the 5s follower grace.
        if method == "ipc-connection-reset":
            self._redeclare(set(self.desired))
            return
        if method != "thread-stream-following-status-requested":
            return
        if message.get("version") != 1 or params.get("hostId") != "local":
            return
        source_client_id = str(message.get("sourceClientId") or "").strip()
        session_id = str(params.get("conversationId") or params.get("threadId") or "").strip()
        if source_client_id and session_id:
            self._redeclare({session_id}, target_client_ids=[source_client_id])

    def _disconnect(self, *, unfollow: bool) -> None:
        with self.lock:
            connection = self.connection
            following = sorted(self.following)
            self.connection = None
            self.following = set()
        if connection is None:
            return
        if unfollow:
            for session_id in following:
                try:
                    connection.unfollow(session_id)
                except Exception:
                    pass
        try:
            connection.close()
        except Exception:
            pass

    def _stopping(self) -> bool:
        return self.closed or self.stopping.is_set()

    def _fail(self, error: BaseException) -> None:
        description = f"{type(error).__name__}: {error}"
        with self.lock:
            changed = description != self.last_error
            self.last_error = description
        if changed and self.emit_log:
            self.emit_log("SESSION KEEPER DEGRADED", {"reason": description})
