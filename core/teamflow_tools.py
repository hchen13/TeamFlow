from __future__ import annotations

import json
import time
from typing import Any

from .config import resolve_workspace_paths
from .db import bootstrap_workspace, connect
from .lark_board import get_lark_task, upsert_lark_task
from .workflow import same_value, workflow_definition_for_assignment
from .workflow_contract import (
    ACTION_TO_TOOL,
    LIFECYCLE_ACTION_TO_TOOL,
    MUTATING_TOOL_NAMES,
    RUNTIME_ACTION_TO_TOOL,
    RUNTIME_TOOL_NAMES,
    TOOL_NAMES,
    available_task_actions,
    rule_role_applicable,
    workflow_contract,
)
from .workflow_lifecycle import (
    normalize_record_id,
    prepare_create_action,
    prepare_existing_action,
    provided_fields,
)
from .delivery import (
    claim_baseline,
    completion_failure,
    append_resources,
    resolve_create_mode,
    resolve_transition_mode,
)
from .workflow_responses import (
    action_success,
    delivery_incomplete_error,
    input_error,
    task_changed_error,
    write_not_visible_error,
)
from .workflow_runtime import prepare_runtime_action, runtime_action_error


def list_available_tasks(assignment: dict[str, Any]) -> dict[str, Any]:
    definition = _definition(assignment)
    claim_states = sorted({
        state
        for rule in definition["lifecycle"]["actions"]["claim"]["rules"]
        if rule_role_applicable(assignment, definition, rule)
        for state in rule.get("from", [])
    })
    if not claim_states:
        return {"ok": True, "count": 0, "tasks": []}
    paths = resolve_workspace_paths(assignment["workspace_root"])
    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        rows = conn.execute(
            f"""
            SELECT snapshot_json
            FROM lark_task_state
            WHERE status IN ({", ".join("?" for _ in claim_states)})
            ORDER BY updated_at, record_id
            """,
            claim_states,
        ).fetchall()
    tasks = []
    for row in rows:
        task = json.loads(row["snapshot_json"])
        if not any(
            action["tool"] == "claim_task"
            for action in available_task_actions(
                assignment,
                task,
                definition=definition,
            )
        ):
            continue
        tasks.append({
            "record_id": task.get("record_id"),
            "task_id": task.get("task_id"),
            "title": task.get("title"),
            "priority": task.get("priority"),
            "type": task.get("type"),
            "status": task.get("status"),
            "role": task.get("role"),
        })
    return {"ok": True, "count": len(tasks), "tasks": tasks}


def get_task(assignment: dict[str, Any], *, record_id: str) -> dict[str, Any]:
    record_id = normalize_record_id(record_id)
    task = get_lark_task(assignment["workspace_root"], record_id=record_id)["task"]
    return {
        "ok": True,
        "task": task,
        "available_actions": available_task_actions(assignment, task),
    }


def create_task(
    assignment: dict[str, Any],
    *,
    title: str,
    task_type: str | None = None,
    priority: str | None = None,
    role: str | None = None,
    description: str | None = None,
    context: str | None = None,
    acceptance_criteria: str | None = None,
    dependencies: str | None = None,
    delivery_mode: str | None = None,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    try:
        mode = resolve_create_mode(assignment["workspace_root"], delivery_mode)
    except ValueError as error:
        return input_error(
            assignment,
            action_key="create",
            code="invalid_delivery_mode",
            message=str(error),
            details={"delivery_mode": delivery_mode},
        )
    prepared = prepare_create_action(
        assignment,
        {
            "title": title,
            "type": task_type,
            "priority": priority,
            "role": role,
            "description": description,
            "context": context,
            "acceptance_criteria": acceptance_criteria,
            "dependencies": dependencies,
        },
    )
    if not prepared["ok"]:
        return prepared
    if mode:
        prepared["patch"]["delivery_mode"] = mode
    result = upsert_lark_task(
        assignment["workspace_root"],
        task=prepared["patch"],
        client_token=invocation_id,
    )
    written, visible = _visible_write(assignment, prepared["patch"], result["task"])
    if not visible:
        return write_not_visible_error(
            assignment,
            prepared["definition"],
            action_key="create",
            variant=None,
            patch=prepared["patch"],
            current=written,
        )
    return action_success(
        assignment,
        prepared["definition"],
        action_key="create",
        rule=prepared["rule"],
        before=None,
        task=written,
        already_applied=False,
    )


def update_task(
    assignment: dict[str, Any],
    *,
    record_id: str,
    fields: dict[str, Any],
    delivery: dict[str, Any] | None = None,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(fields, dict) or not fields:
        return input_error(
            assignment,
            action_key="update",
            code="fields_required",
            message="update_task 至少需要一个待更新字段。",
            details={"required": ["fields"]},
        )
    return _execute_existing_action(
        assignment,
        action_key="update",
        record_id=record_id,
        payload=fields,
        delivery=delivery,
        invocation_id=invocation_id,
    )


def route_task(
    assignment: dict[str, Any],
    *,
    record_id: str,
    role: str,
    runtime_facts: set[str] | None = None,
    current_task: dict[str, Any] | None = None,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    return _execute_existing_action(
        assignment,
        action_key="route",
        record_id=record_id,
        payload={"role": role},
        runtime_facts=runtime_facts,
        current_task=current_task,
        invocation_id=invocation_id,
    )


def claim_task(
    assignment: dict[str, Any],
    *,
    record_id: str,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    return _execute_existing_action(
        assignment,
        action_key="claim",
        record_id=record_id,
        payload={},
        invocation_id=invocation_id,
    )


def submit_task(
    assignment: dict[str, Any],
    *,
    record_id: str,
    outcome: str,
    result_evidence: str,
    progress: str | None = None,
    next_action: str | None = None,
    delivery: dict[str, Any] | None = None,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    return _execute_existing_action(
        assignment,
        action_key="submit",
        variant=outcome,
        record_id=record_id,
        payload=provided_fields({
            "result_evidence": result_evidence,
            "progress": progress,
            "next_action": next_action,
        }),
        delivery=delivery,
        invocation_id=invocation_id,
    )


def block_task(
    assignment: dict[str, Any],
    *,
    record_id: str,
    waiting_on: str,
    blocked_reason: str,
    next_action: str,
    progress: str | None = None,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    return _execute_existing_action(
        assignment,
        action_key="block",
        record_id=record_id,
        payload=provided_fields({
            "waiting_on": waiting_on,
            "blocked_reason": blocked_reason,
            "next_action": next_action,
            "progress": progress,
        }),
        invocation_id=invocation_id,
    )


def review_task(
    assignment: dict[str, Any],
    *,
    record_id: str,
    decision: str,
    result_evidence: str,
    role: str | None = None,
    next_action: str | None = None,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    return _execute_existing_action(
        assignment,
        action_key="review",
        variant=decision,
        record_id=record_id,
        payload=provided_fields({
            "result_evidence": result_evidence,
            "role": role,
            "next_action": next_action,
        }),
        invocation_id=invocation_id,
    )


def cancel_task(
    assignment: dict[str, Any],
    *,
    record_id: str,
    result_evidence: str,
    confirmed: bool,
    runtime_facts: set[str] | None = None,
    current_task: dict[str, Any] | None = None,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    return _execute_existing_action(
        assignment,
        action_key="cancel",
        record_id=record_id,
        payload={"result_evidence": result_evidence},
        confirmed=confirmed,
        runtime_facts=runtime_facts,
        current_task=current_task,
        invocation_id=invocation_id,
    )


def _execute_existing_action(
    assignment: dict[str, Any],
    *,
    action_key: str,
    record_id: str,
    payload: dict[str, Any],
    variant: str | None = None,
    delivery: dict[str, Any] | None = None,
    confirmed: bool = True,
    runtime_facts: set[str] | None = None,
    current_task: dict[str, Any] | None = None,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    record_id = normalize_record_id(record_id)
    task = get_lark_task(assignment["workspace_root"], record_id=record_id)["task"]
    definition = _definition(assignment)
    if current_task is not None and current_task != task:
        return task_changed_error(
            assignment,
            definition,
            action_key=action_key,
            variant=variant,
            expected=current_task,
            current=task,
        )
    prepared = prepare_existing_action(
        assignment,
        action_key=action_key,
        task=task,
        payload=payload,
        variant=variant,
        confirmed=confirmed,
        runtime_facts=runtime_facts,
    )
    if not prepared["ok"]:
        return prepared
    if prepared["already_applied"]:
        return action_success(
            assignment,
            prepared["definition"],
            action_key=action_key,
            rule=prepared["rule"],
            before=task,
            task=task,
            already_applied=True,
        )
    try:
        _apply_delivery(
            assignment,
            prepared,
            action_key=action_key,
            task=task,
            delivery=delivery,
        )
    except ValueError as error:
        return input_error(
            assignment,
            action_key=action_key,
            code="invalid_delivery",
            message=str(error),
            details={"delivery": delivery},
        )
    blocked = completion_failure(
        assignment["workspace_root"],
        prepared["definition"],
        task,
        prepared["patch"],
    )
    if blocked:
        return delivery_incomplete_error(
            assignment,
            prepared["definition"],
            action_key=action_key,
            variant=variant,
            task=task,
            blocked=blocked,
        )
    latest = get_lark_task(assignment["workspace_root"], record_id=record_id)["task"]
    if latest != task:
        return task_changed_error(
            assignment,
            prepared["definition"],
            action_key=action_key,
            variant=variant,
            expected=task,
            current=latest,
        )
    result = upsert_lark_task(
        assignment["workspace_root"],
        record_id=record_id,
        task=prepared["patch"],
        client_token=invocation_id,
    )
    written, visible = _visible_write(assignment, prepared["patch"], result["task"])
    if not visible:
        return write_not_visible_error(
            assignment,
            prepared["definition"],
            action_key=action_key,
            variant=variant,
            patch=prepared["patch"],
            current=written,
        )
    return action_success(
        assignment,
        prepared["definition"],
        action_key=action_key,
        rule=prepared["rule"],
        before=task,
        task=written,
        already_applied=False,
    )


# Lark is read-after-write eventually consistent, so a write response or an immediate reread can
# still describe the record as it was. A mutation is only reported as successful once the fields it
# wrote are actually observable, which is what the caller and the runtime both act on.
WRITE_VISIBILITY_TIMEOUT = 10.0
WRITE_VISIBILITY_INITIAL_DELAY = 0.25
WRITE_VISIBILITY_MAX_DELAY = 1.0
_sleep = time.sleep
_monotonic = time.monotonic


def _patch_visible(task: dict[str, Any], patch: dict[str, Any]) -> bool:
    return all(same_value(task.get(field), value) for field, value in patch.items())


def _visible_write(
    assignment: dict[str, Any],
    patch: dict[str, Any],
    written: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Return the task once the patch is observable, and whether it ever became so.

    The write response is trusted only when it already carries every written field; otherwise the
    record is reread from Lark on a bounded backoff.
    """
    if _patch_visible(written, patch):
        return written, True
    record_id = str(written.get("record_id") or "")
    if not record_id:
        return written, False
    latest = written
    deadline = _monotonic() + WRITE_VISIBILITY_TIMEOUT
    delay = WRITE_VISIBILITY_INITIAL_DELAY
    while _monotonic() < deadline:
        _sleep(delay)
        latest = get_lark_task(assignment["workspace_root"], record_id=record_id)["task"]
        if _patch_visible(latest, patch):
            return latest, True
        delay = min(delay * 2, WRITE_VISIBILITY_MAX_DELAY)
    return latest, False


def _definition(assignment: dict[str, Any]) -> dict[str, Any]:
    return workflow_definition_for_assignment(assignment)


def _apply_delivery(
    assignment: dict[str, Any],
    prepared: dict[str, Any],
    *,
    action_key: str,
    task: dict[str, Any],
    delivery: dict[str, Any] | None,
) -> None:
    """Merge the delivery facts an action may record into the rule's own patch."""
    workspace = assignment["workspace_root"]
    definition = prepared["definition"]
    patch = prepared["patch"]
    delivery = delivery or {}
    if not isinstance(delivery, dict):
        raise ValueError("delivery must be an object")

    mode = resolve_transition_mode(
        workspace,
        definition,
        task,
        delivery.get("delivery_mode"),
        target_state=prepared["rule"].get("to"),
    )
    if mode:
        patch["delivery_mode"] = mode

    for field in ("target_branch", "base_sha", "candidate_sha", "verified_sha", "promoted_sha"):
        if field in delivery and str(delivery[field] or "").strip():
            patch[field] = str(delivery[field]).strip()

    resources = delivery.get("resources")
    if resources is not None:
        if not isinstance(resources, dict):
            raise ValueError("delivery.resources must be an object")
        patch["delivery_resources"] = append_resources(
            task.get("delivery_resources"),
            resources,
        )

    if action_key == "claim":
        patch.update(claim_baseline(workspace, {**task, **patch}))
