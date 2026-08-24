from __future__ import annotations

import json
import socket
import struct
import threading
import unittest

from core import session_keeper as module
from core.codex_ipc import CodexIpcConnection
from core.session_keeper import SessionKeeper


ALLOWED_CALLS = {"follow", "unfollow", "close", "_receive_once"}


class FakeConnection:
    """Only the calls a keeper is allowed to make exist; anything else is an error."""

    def __init__(self, frames: list[dict] | None = None) -> None:
        self.calls: list[tuple] = []
        self.frames = list(frames or [])
        self.closed = False

    def follow(self, thread_id, *, force=False, target_client_ids=None):
        self.calls.append(("follow", thread_id, force))

    def unfollow(self, thread_id):
        self.calls.append(("unfollow", thread_id))

    def close(self):
        self.closed = True
        self.calls.append(("close",))

    def _receive_once(self, timeout):
        self.calls.append(("_receive_once",))
        return self.frames.pop(0) if self.frames else None

    def __getattr__(self, name):
        raise AssertionError(f"the keeper must not call {name}")

    def sent(self, kind):
        return [call[1] for call in self.calls if call[0] == kind]


def keeper(sessions, connection=None, connect=None):
    holder = {"sessions": set(sessions)}
    made = connection if connection is not None else FakeConnection()
    instance = SessionKeeper(
        desired_sessions=lambda: holder["sessions"],
        connect=connect or (lambda: made),
        stopping=threading.Event(),
    )
    return instance, holder, made


class SessionKeeperTest(unittest.TestCase):
    def test_the_first_connection_follows_only_the_registered_sessions(self):
        keep, _, connection = keeper({"session_a", "session_b"})

        keep._tick()

        self.assertEqual(connection.sent("follow"), ["session_a", "session_b"])
        self.assertEqual(connection.sent("unfollow"), [])

    def test_an_unchanged_refresh_sends_nothing_further(self):
        keep, _, connection = keeper({"session_a"})
        keep._tick()

        keep.wake()
        keep._tick()
        keep.wake()
        keep._tick()

        self.assertEqual(connection.sent("follow"), ["session_a"])
        self.assertEqual(connection.sent("unfollow"), [])

    def test_rebinding_unregistering_and_disabling_produce_the_right_difference(self):
        keep, holder, connection = keeper({"session_a", "session_b"})
        keep._tick()

        for sessions in ({"session_c", "session_b"}, {"session_b"}, set()):
            holder["sessions"] = sessions
            keep.wake()
            keep._tick()

        self.assertEqual(connection.sent("follow"), ["session_a", "session_b", "session_c"])
        self.assertEqual(connection.sent("unfollow"), ["session_a", "session_c", "session_b"])
        self.assertEqual(keep.snapshot()["following"], 0)

    def test_a_connection_reset_notice_redeclares_every_desired_session(self):
        connection = FakeConnection([
            {"type": "broadcast", "method": "ipc-connection-reset", "version": 1},
        ])
        keep, _, _ = keeper({"session_a", "session_b"}, connection=connection)

        keep._tick()

        self.assertEqual(
            [call for call in connection.calls if call[0] == "follow" and call[2]],
            [("follow", "session_a", True), ("follow", "session_b", True)],
        )

    def test_a_following_status_request_redeclares_only_a_registered_session(self):
        def request(session_id):
            return {
                "type": "broadcast",
                "method": "thread-stream-following-status-requested",
                "version": 1,
                "params": {"conversationId": session_id},
            }

        connection = FakeConnection([request("session_a"), request("session_unknown")])
        keep, _, _ = keeper({"session_a", "session_b"}, connection=connection)

        keep._tick()

        self.assertEqual(
            [call for call in connection.calls if call[0] == "follow" and call[2]],
            [("follow", "session_a", True)],
        )

    def test_a_dropped_connection_reconnects_fast_and_refollows_everything(self):
        second = FakeConnection()
        made = [FakeConnection(), second]
        keep, _, _ = keeper({"session_a", "session_b"}, connect=lambda: made.pop(0))
        keep._tick()
        keep._disconnect(unfollow=False)

        immediate = keep._reconnect_delay()
        keep._tick()

        self.assertEqual(second.sent("follow"), ["session_a", "session_b"])
        self.assertEqual(immediate, 0.0)
        # The dense attempts must all fit inside Desktop's 5s follower reconnect grace.
        self.assertLess(sum(module._RECONNECT_DELAYS), 5.0)

    def test_an_unavailable_socket_waits_quietly_without_stopping_the_daemon(self):
        def refuse():
            raise OSError("no codex ipc")

        keep, _, _ = keeper({"session_a"}, connect=refuse)

        delays = [keep._tick() for _ in range(7)]

        self.assertEqual(delays[:4], [0.25, 0.5, 1.0, 2.0])
        self.assertEqual(delays[4:], [5.0, 5.0, 5.0])
        # Everything before the slow tail has to fit in Desktop's 5s follower grace.
        self.assertLess(sum(delays[:4]), 5.0)
        self.assertFalse(keep.stopping.is_set())
        self.assertIn("no codex ipc", keep.snapshot()["last_error"])

    def test_a_failing_provider_keeps_the_last_known_set_and_stays_alive(self):
        connection = FakeConnection()
        broken = {"fail": False}

        def sessions():
            if broken["fail"]:
                raise RuntimeError("workspace unreadable")
            return {"session_a"}

        keep = SessionKeeper(
            desired_sessions=sessions,
            connect=lambda: connection,
            stopping=threading.Event(),
        )
        keep._tick()
        broken["fail"] = True
        keep._tick()

        self.assertEqual(connection.sent("follow"), ["session_a"])
        self.assertEqual(connection.sent("unfollow"), [])
        self.assertFalse(keep.stopping.is_set())

    def test_closing_unfollows_best_effort_and_is_idempotent(self):
        keep, _, connection = keeper({"session_a", "session_b"})
        keep._tick()

        keep.close()
        keep.close()

        self.assertEqual(connection.sent("unfollow"), ["session_a", "session_b"])
        self.assertTrue(connection.closed)

    def test_the_keeper_only_ever_follows_unfollows_drains_and_closes(self):
        connection = FakeConnection([
            {"type": "broadcast", "method": "ipc-connection-reset", "version": 1},
        ])
        keep, holder, _ = keeper({"session_a"}, connection=connection)
        keep._tick()
        holder["sessions"] = {"session_b"}
        keep._tick()
        keep.close()

        self.assertEqual({call[0] for call in connection.calls}, ALLOWED_CALLS)


class LightweightConnectionTest(unittest.TestCase):
    """The keeper's connection must not grow with the number of turns it observes."""

    def connection(self, *, lightweight):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        return CodexIpcConnection(left, "client-teamflow", lightweight=lightweight), right

    def push(self, sock, payload):
        body = json.dumps(payload).encode()
        sock.sendall(struct.pack("<I", len(body)) + body)

    def stream_frame(self, index, version):
        change = {"type": "snapshot", "turns": [{"id": f"turn_{index}"}]}
        return {"type": "broadcast", "method": "thread-stream-state-changed",
                "version": version, "sourceClientId": "client-owner",
                "params": {"conversationId": f"session_{index % 3}", "change": change}}

    def test_a_lightweight_connection_discards_stream_content(self):
        connection, peer = self.connection(lightweight=True)
        for index in range(200):
            self.push(peer, self.stream_frame(index, 11))
            connection._receive_once(0.5)

        self.assertEqual(connection.streams, {})
        self.assertEqual(connection.followers, {})

    def test_a_lightweight_connection_survives_an_unknown_stream_version(self):
        connection, peer = self.connection(lightweight=True)
        self.push(peer, self.stream_frame(0, 999))

        message = connection._receive_once(0.5)

        self.assertEqual(message["method"], "thread-stream-state-changed")
        self.assertEqual(connection.streams, {})

    def test_the_turn_path_still_rejects_an_unknown_stream_version(self):
        connection, peer = self.connection(lightweight=False)
        self.push(peer, self.stream_frame(0, 999))

        with self.assertRaises(ValueError):
            connection._receive_once(0.5)

    def test_the_turn_path_still_accumulates_the_stream_it_needs(self):
        connection, peer = self.connection(lightweight=False)
        self.push(peer, self.stream_frame(0, 11))

        connection._receive_once(0.5)

        self.assertIn("session_0", connection.streams)


if __name__ == "__main__":
    unittest.main()
