from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from typing import Any, Callable

from . import prompt_catalog
from .agent_runtime import find_agent_assignment
from .global_db import workspace_enabled
from .teamflow_tools import MUTATING_TOOL_NAMES, RUNTIME_TOOL_NAMES, TOOL_NAMES


HANDOFF_TOOL_NAMES = {
    "route_task",
    "submit_task",
    "block_task",
    "review_task",
    "cancel_task",
}
READ_ONLY_TOOL_NAMES = TOOL_NAMES - MUTATING_TOOL_NAMES - RUNTIME_TOOL_NAMES


def tool_input_hash(value: Any) -> str:
    payload = json.dumps(
        value if isinstance(value, dict) else {},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class ToolRuntime:
    def __init__(
        self,
        *,
        sync_lock: threading.RLock,
        assignment_context: Callable[..., dict[str, Any]],
        workspace_active: Callable[[str], bool],
        invoke_tool: Callable[..., dict[str, Any]],
        sync_task_activity: Callable[..., None],
        delivery_record_id: Callable[..., str | None],
        delivery_turn_is_current: Callable[..., bool | None],
        acknowledge_delivery: Callable[..., bool] = lambda *args, **kwargs: False,
    ) -> None:
        self.sync_lock = sync_lock
        self.assignment_context = assignment_context
        self.workspace_active = workspace_active
        self.invoke_teamflow_tool = invoke_tool
        self.sync_task_activity = sync_task_activity
        self.delivery_record_id = delivery_record_id
        self.delivery_turn_is_current = delivery_turn_is_current
        self.acknowledge_delivery = acknowledge_delivery
        self.grants: dict[str, dict[str, Any]] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.closed_turns: dict[tuple[str, str], float] = {}
        self.task_locks: dict[str, threading.Lock] = {}
        self.runtime_locks: dict[tuple[str, str], threading.Lock] = {}

    def authorize(
        self,
        *,
        invocation_id: str,
        session_id: str,
        cwd: str | None,
        turn_id: str | None,
        tool_name: str,
        tool_input: Any,
    ) -> dict[str, Any]:
        if not invocation_id:
            raise ValueError("TeamFlow MCP invocation ID is required")
        short_name = tool_name.rsplit("__", 1)[-1]
        if short_name not in TOOL_NAMES:
            raise ValueError(f"unsupported TeamFlow tool: {short_name}")
        context = self.assignment_context(
            session_id=session_id,
            cwd=cwd,
            consume=False,
        )
        assignment = context.get("assignment")
        if not assignment:
            raise ValueError(
                "this Codex session is not a registered agent in an enabled "
                "TeamFlow workspace"
            )
        token = secrets.token_urlsafe(32)
        timestamp = time.monotonic()
        cached_invocation = False
        with self.sync_lock:
            self.grants = {
                key: value
                for key, value in self.grants.items()
                if float(value["expires_at"]) > timestamp
            }
            self.results = {
                key: value
                for key, value in self.results.items()
                if float(value["expires_at"]) > timestamp
            }
            self.closed_turns = {
                key: expires_at
                for key, expires_at in self.closed_turns.items()
                if expires_at > timestamp
            }
            turn_key = (session_id, str(turn_id or ""))
            cached_invocation = invocation_id in self.results
            if (
                turn_id
                and short_name not in READ_ONLY_TOOL_NAMES
                and turn_key in self.closed_turns
                and not cached_invocation
            ):
                raise ValueError(
                    "TeamFlow handoff is complete for this turn. End the current "
                    "reply now; the daemon will start a new turn when more work "
                    "is available."
                )
        if (
            turn_id
            and short_name not in READ_ONLY_TOOL_NAMES
            and not cached_invocation
            and self.delivery_turn_is_current(
                assignment,
                turn_id=str(turn_id),
            ) is False
        ):
            raise ValueError(
                "TeamFlow delivery for this turn is no longer active. Read-only "
                "tools remain available, but lifecycle and mutating tools require "
                "the current daemon delivery or continuation."
            )
        with self.sync_lock:
            self.grants[token] = {
                "invocation_id": invocation_id,
                "session_id": session_id,
                "turn_id": turn_id,
                "tool_name": short_name,
                "input_hash": tool_input_hash(tool_input),
                "assignment": assignment,
                "expires_at": timestamp + 60,
            }
        return {"grant": token, "expires_in": 60}

    def invoke(
        self,
        *,
        invocation_id: str,
        grant: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        with self.sync_lock:
            authorized = self.grants.pop(grant, None)
        self._validate_grant(
            authorized,
            invocation_id=invocation_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        cached = self._cached_result(invocation_id, authorized, tool_name)
        if cached is not None:
            return cached
        self._validate_turn_open(authorized, tool_name=tool_name)

        assignment = authorized["assignment"]
        workspace_root = str(assignment["workspace_root"])
        self._validate_workspace(workspace_root)
        current = self._current_assignment(authorized, assignment, workspace_root)

        if tool_name in RUNTIME_TOOL_NAMES:
            lock_key = (workspace_root, str(arguments.get("record_id") or ""))
            with self.sync_lock:
                runtime_lock = self.runtime_locks.setdefault(
                    lock_key,
                    threading.Lock(),
                )
            with runtime_lock:
                cached = self._cached_result(invocation_id, authorized, tool_name)
                if cached is not None:
                    return cached
                self._validate_turn_open(authorized, tool_name=tool_name)
                self._validate_workspace(
                    workspace_root,
                    message=(
                        "the TeamFlow assignment or workspace changed before "
                        "the tool ran"
                    ),
                )
                current = self._current_assignment(
                    authorized,
                    assignment,
                    workspace_root,
                    message=(
                        "the TeamFlow assignment or workspace changed before "
                        "the tool ran"
                    ),
                )
                result = self.invoke_teamflow_tool(
                    current["assignment"],
                    tool_name,
                    arguments,
                    invocation_id=invocation_id,
                )
                self.cache_result(
                    invocation_id,
                    authorized=authorized,
                    tool_name=tool_name,
                    result=result,
                )
                self._seal_turn_if_requested(authorized, result)
                return result

        if tool_name in MUTATING_TOOL_NAMES:
            with self.sync_lock:
                task_lock = self.task_locks.setdefault(
                    workspace_root,
                    threading.Lock(),
                )
            with task_lock:
                cached = self._cached_result(invocation_id, authorized, tool_name)
                if cached is not None:
                    return cached
                self._validate_turn_open(authorized, tool_name=tool_name)
                self._validate_workspace(workspace_root)
                current = self._current_assignment(
                    authorized,
                    assignment,
                    workspace_root,
                )
                result = self.invoke_teamflow_tool(
                    current["assignment"],
                    tool_name,
                    arguments,
                    invocation_id=invocation_id,
                )
                self._apply_turn_control(authorized, tool_name, result)
                self.sync_task_activity(
                    current["assignment"],
                    tool_name=tool_name,
                    result=result,
                    session_id=str(authorized["session_id"]),
                    turn_id=str(authorized.get("turn_id") or "") or None,
                )
                self._acknowledge_delivery(
                    authorized,
                    current["assignment"],
                    result=result,
                )
                self.cache_result(
                    invocation_id,
                    authorized=authorized,
                    tool_name=tool_name,
                    result=result,
                )
                self._seal_turn_if_requested(authorized, result)
                return result

        result = self.invoke_teamflow_tool(
            current["assignment"],
            tool_name,
            arguments,
            invocation_id=invocation_id,
        )
        self.sync_task_activity(
            current["assignment"],
            tool_name=tool_name,
            result=result,
            session_id=str(authorized["session_id"]),
            turn_id=str(authorized.get("turn_id") or "") or None,
        )
        self.cache_result(
            invocation_id,
            authorized=authorized,
            tool_name=tool_name,
            result=result,
        )
        self._seal_turn_if_requested(authorized, result)
        return result

    def _acknowledge_delivery(
        self,
        authorized: dict[str, Any],
        assignment: dict[str, Any],
        *,
        result: dict[str, Any],
    ) -> None:
        task = result.get("task")
        turn_id = str(authorized.get("turn_id") or "")
        if not result.get("ok") or not turn_id or not isinstance(task, dict):
            return
        self.acknowledge_delivery(
            str(assignment["workspace_root"]),
            turn_id=turn_id,
            agent_id=str(assignment["agent_id"]),
            record_id=str(task.get("record_id") or ""),
        )

    def _validate_turn_open(
        self,
        authorized: dict[str, Any],
        *,
        tool_name: str,
    ) -> None:
        if tool_name in READ_ONLY_TOOL_NAMES:
            return
        turn_id = str(authorized.get("turn_id") or "")
        if not turn_id:
            return
        with self.sync_lock:
            closed_until = self.closed_turns.get(
                (str(authorized["session_id"]), turn_id)
            )
        if closed_until and closed_until > time.monotonic():
            raise ValueError(
                "TeamFlow handoff is complete for this turn. End the current "
                "reply now; the daemon will start a new turn when more work "
                "is available."
            )
        if self.delivery_turn_is_current(
            authorized["assignment"],
            turn_id=turn_id,
        ) is False:
            raise ValueError(
                "TeamFlow delivery for this turn is no longer active. Read-only "
                "tools remain available, but lifecycle and mutating tools require "
                "the current daemon delivery or continuation."
            )

    def cache_result(
        self,
        invocation_id: str,
        *,
        authorized: dict[str, Any],
        tool_name: str,
        result: dict[str, Any],
    ) -> None:
        with self.sync_lock:
            self.results[invocation_id] = {
                "session_id": authorized["session_id"],
                "tool_name": tool_name,
                "input_hash": authorized["input_hash"],
                "result": result,
                "expires_at": time.monotonic() + 300,
            }

    def workspace_lock(self, workspace_root: str) -> threading.Lock:
        with self.sync_lock:
            return self.task_locks.setdefault(workspace_root, threading.Lock())

    def _seal_turn_if_requested(
        self,
        authorized: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        control = result.get("turn_control")
        turn_id = str(authorized.get("turn_id") or "")
        if (
            not result.get("ok")
            or not isinstance(control, dict)
            or control.get("action") != "end_turn"
            or not turn_id
        ):
            return
        with self.sync_lock:
            self.closed_turns[
                (str(authorized["session_id"]), turn_id)
            ] = time.monotonic() + 3600

    def _apply_turn_control(
        self,
        authorized: dict[str, Any],
        tool_name: str,
        result: dict[str, Any],
    ) -> None:
        turn_id = str(authorized.get("turn_id") or "")
        task = result.get("task")
        if (
            tool_name not in HANDOFF_TOOL_NAMES
            or not result.get("ok")
            or not turn_id
            or not isinstance(task, dict)
        ):
            return
        assignment = authorized["assignment"]
        current_record_id = self.delivery_record_id(
            assignment,
            turn_id=turn_id,
        )
        if current_record_id != str(task.get("record_id") or ""):
            return
        result["turn_control"] = {
            "action": "end_turn",
            "reason": prompt_catalog.render(
                "turn-control.reason",
                surface="tool_result_turn_control",
                trigger="handoff_complete",
            ),
            "instruction": prompt_catalog.render(
                "turn-control.instruction",
                surface="tool_result_turn_control",
                trigger="handoff_complete",
            ),
        }

    def _validate_grant(
        self,
        authorized: dict[str, Any] | None,
        *,
        invocation_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        if not authorized or float(authorized["expires_at"]) <= time.monotonic():
            raise ValueError("TeamFlow tool authorization is missing or expired")
        if authorized["invocation_id"] != invocation_id:
            raise ValueError(
                "TeamFlow tool authorization does not match this invocation"
            )
        if authorized["tool_name"] != tool_name:
            raise ValueError("TeamFlow tool authorization does not match this tool")
        if authorized["input_hash"] != tool_input_hash(arguments):
            raise ValueError(
                "TeamFlow tool arguments changed after authorization"
            )

    def _cached_result(
        self,
        invocation_id: str,
        authorized: dict[str, Any],
        tool_name: str,
    ) -> dict[str, Any] | None:
        with self.sync_lock:
            cached = self.results.get(invocation_id)
        if not cached:
            return None
        if (
            cached["session_id"] != authorized["session_id"]
            or cached["tool_name"] != tool_name
            or cached["input_hash"] != authorized["input_hash"]
        ):
            raise ValueError(
                "TeamFlow invocation result does not match this tool call"
            )
        return cached["result"]

    def _validate_workspace(
        self,
        workspace_root: str,
        *,
        message: str = "the TeamFlow workspace was disabled before the tool ran",
    ) -> None:
        if not self.workspace_active(workspace_root) or not workspace_enabled(
            workspace_root
        ):
            raise ValueError(message)

    @staticmethod
    def _current_assignment(
        authorized: dict[str, Any],
        assignment: dict[str, Any],
        workspace_root: str,
        *,
        message: str = "the TeamFlow agent assignment changed before the tool ran",
    ) -> dict[str, Any]:
        current = find_agent_assignment(
            [workspace_root],
            session_id=str(authorized["session_id"]),
        )
        if not current or current["assignment"]["agent_id"] != assignment["agent_id"]:
            raise ValueError(message)
        return current
