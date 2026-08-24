from __future__ import annotations

from typing import Any, Callable


class TeamFlowToolDispatcher:
    def __init__(
        self,
        *,
        resolve: Callable[[str], Callable[..., Any]],
        runtime_facts: Callable[[dict[str, Any], dict[str, Any]], set[str]],
        stop_execution: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    ) -> None:
        self.resolve = resolve
        self.runtime_facts = runtime_facts
        self.stop_execution = stop_execution

    def invoke(
        self,
        assignment: dict[str, Any],
        tool_name: str,
        arguments: dict[str, Any],
        *,
        invocation_id: str,
    ) -> dict[str, Any]:
        record_id = str(arguments.get("record_id") or "")
        if tool_name == "get_assignment":
            return {
                "ok": True,
                "assignment": assignment,
                "workflow": self.resolve("workflow_contract")(assignment),
            }
        if tool_name == "list_tasks":
            return self.resolve("list_tasks")(
                assignment,
                status=_optional_text(arguments.get("status")),
                role=_optional_text(arguments.get("role")),
                task_id=_optional_text(arguments.get("task_id")),
                limit=_integer(arguments.get("limit"), default=50),
                offset=_integer(arguments.get("offset"), default=0),
            )
        if tool_name == "list_available_tasks":
            return self.resolve("list_available_tasks")(assignment)
        if tool_name == "get_task":
            return self.resolve("get_task")(
                assignment,
                record_id=record_id or None,
                task_id=_optional_text(arguments.get("task_id")),
            )
        if tool_name == "create_task":
            return self.resolve("create_task")(
                assignment,
                title=str(arguments.get("title") or ""),
                task_type=_optional_text(arguments.get("task_type")),
                priority=_optional_text(arguments.get("priority")),
                role=_optional_text(arguments.get("role")),
                description=_optional_text(arguments.get("description")),
                context=_optional_text(arguments.get("context")),
                acceptance_criteria=_optional_text(
                    arguments.get("acceptance_criteria")
                ),
                dependencies=_optional_text(arguments.get("dependencies")),
                delivery_mode=_optional_text(arguments.get("delivery_mode")),
                invocation_id=invocation_id,
            )
        if tool_name == "update_task":
            fields = arguments.get("fields")
            return self.resolve("update_task")(
                assignment,
                record_id=record_id,
                fields=fields if isinstance(fields, dict) else {},
                delivery=_optional_delivery(arguments.get("delivery")),
                invocation_id=invocation_id,
            )
        if tool_name == "route_task":
            task = self._task(assignment, record_id)
            return self.resolve("route_task")(
                assignment,
                record_id=record_id,
                role=str(arguments.get("role") or ""),
                runtime_facts=self.runtime_facts(assignment, task),
                current_task=task,
                invocation_id=invocation_id,
            )
        if tool_name == "claim_task":
            return self.resolve("claim_task")(
                assignment,
                record_id=record_id,
                invocation_id=invocation_id,
            )
        if tool_name == "submit_task":
            return self.resolve("submit_task")(
                assignment,
                record_id=record_id,
                outcome=str(arguments.get("outcome") or ""),
                result_evidence=str(arguments.get("result_evidence") or ""),
                progress=_optional_text(arguments.get("progress")),
                next_action=_optional_text(arguments.get("next_action")),
                delivery=_optional_delivery(arguments.get("delivery")),
                invocation_id=invocation_id,
            )
        if tool_name == "block_task":
            return self.resolve("block_task")(
                assignment,
                record_id=record_id,
                waiting_on=str(arguments.get("waiting_on") or ""),
                blocked_reason=str(arguments.get("blocked_reason") or ""),
                next_action=str(arguments.get("next_action") or ""),
                progress=_optional_text(arguments.get("progress")),
                invocation_id=invocation_id,
            )
        if tool_name == "review_task":
            return self.resolve("review_task")(
                assignment,
                record_id=record_id,
                decision=str(arguments.get("decision") or ""),
                result_evidence=str(arguments.get("result_evidence") or ""),
                role=_optional_text(arguments.get("role")),
                next_action=_optional_text(arguments.get("next_action")),
                invocation_id=invocation_id,
            )
        if tool_name == "stop_task_execution":
            return self.stop_execution(assignment, arguments)
        if tool_name == "cancel_task":
            task = self._task(assignment, record_id)
            return self.resolve("cancel_task")(
                assignment,
                record_id=record_id,
                result_evidence=str(arguments.get("result_evidence") or ""),
                confirmed=bool(arguments.get("confirmed")),
                runtime_facts=self.runtime_facts(assignment, task),
                current_task=task,
                invocation_id=invocation_id,
            )
        raise ValueError(f"unsupported TeamFlow tool: {tool_name}")

    def _task(
        self,
        assignment: dict[str, Any],
        record_id: str,
    ) -> dict[str, Any]:
        result = self.resolve("get_lark_task")(
            assignment["workspace_root"],
            record_id=record_id,
        )
        return result["task"]


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_delivery(value: Any) -> dict[str, Any] | None:
    """Forward the delivery object untouched; delivery.py owns what is acceptable."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("delivery must be an object")
    return value


def _integer(value: Any, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("pagination values must be integers")
    return value
