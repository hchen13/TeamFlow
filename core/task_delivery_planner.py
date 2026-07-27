from __future__ import annotations

import json
from typing import Any

from .db import bootstrap_workspace, connect, now, workspace_id_for_root
from .lark_events import LarkEventContext
from .task_routing import (
    WorkflowLoader,
    actionable_states,
    current_dispatch,
    current_task_status,
    dispatch_event_state,
    dispatch_states,
    finish_routing,
    insert_delivery,
    render_task_prompt,
    target_role,
)


def prepare_task_deliveries(
    context: LarkEventContext,
    *,
    load_workflow: WorkflowLoader,
) -> dict[str, Any]:
    timestamp = now()
    routed = waiting = ignored = deliveries = 0
    outcomes = []
    states = dispatch_states(
        context.workflow_key,
        load_workflow=load_workflow,
    )
    with connect(context.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, context.workspace_root)
        events = conn.execute(
            """
            SELECT *
            FROM task_events
            WHERE routing_status = 'pending'
            ORDER BY created_at, event_key
            """
        ).fetchall()
        for event in events:
            task = json.loads(
                event["after_json"] or event["before_json"] or "{}"
            )
            event_state = dispatch_event_state(states, event)
            if event_state:
                current = current_dispatch(conn, event, states)
                if current:
                    task = current["task"]
                if (
                    not current
                    or current["event"]["event_key"] != event["event_key"]
                ):
                    expected_status = event_state["key"]
                    current_status = (
                        current["state"]["key"]
                        if current
                        else current_task_status(conn, event)
                    )
                    note = (
                        f"task is no longer {expected_status}"
                        if current_status != expected_status
                        else (
                            f"task has a newer {expected_status} "
                            "dispatch event"
                        )
                    )
                    finish_routing(
                        conn,
                        event["event_key"],
                        "ignored",
                        note,
                        timestamp,
                    )
                    outcomes.append({
                        "source_event_id": event["source_event_id"],
                        "event_type": event["event_type"],
                        "record_id": event["record_id"],
                        "task": task,
                        "result": "not-required",
                        "target": None,
                    })
                    ignored += 1
                    continue
            else:
                event_state = None
            target = target_role(
                context.workflow_key,
                event_state["key"] if event_state else None,
                task,
                load_workflow=load_workflow,
            )
            if not target:
                finish_routing(
                    conn,
                    event["event_key"],
                    "ignored",
                    "event does not notify an agent",
                    timestamp,
                )
                outcomes.append({
                    "source_event_id": event["source_event_id"],
                    "event_type": event["event_type"],
                    "record_id": event["record_id"],
                    "task": task,
                    "result": "not-required",
                    "target": None,
                })
                ignored += 1
                continue
            agents = conn.execute(
                """
                SELECT agents.*,
                       COALESCE(
                         roles.display_name_zh,
                         roles.display_name
                       ) AS role_name
                FROM agents
                JOIN roles ON roles.id = agents.role_id
                WHERE agents.workspace_id = ?
                  AND agents.workflow_id = ?
                  AND agents.role_key = ?
                ORDER BY agents.created_at, agents.id
                """,
                (workspace_id, event["workflow_id"], target),
            ).fetchall()
            if not agents:
                conn.execute(
                    "UPDATE task_events SET routing_note = ? WHERE event_key = ?",
                    (
                        f"no registered agent for role {target}",
                        event["event_key"],
                    ),
                )
                outcomes.append({
                    "source_event_id": event["source_event_id"],
                    "event_type": event["event_type"],
                    "record_id": event["record_id"],
                    "task": task,
                    "result": "waiting",
                    "target": target,
                })
                waiting += 1
                continue
            workflow = conn.execute(
                "SELECT key FROM workflows WHERE id = ?",
                (event["workflow_id"],),
            ).fetchone()
            for agent in agents:
                prompt = render_task_prompt(
                    context,
                    event_type=str(event["event_type"]),
                    event_key=str(event["event_key"]),
                    workflow_key=str(workflow["key"]),
                    role_name=str(
                        agent["role_name"] or agent["role_key"]
                    ),
                    task=task,
                    load_workflow=load_workflow,
                )
                deliveries += insert_delivery(
                    conn,
                    event,
                    agent,
                    prompt,
                    timestamp,
                )
            finish_routing(
                conn,
                event["event_key"],
                "routed",
                None,
                timestamp,
            )
            outcomes.append({
                "source_event_id": event["source_event_id"],
                "event_type": event["event_type"],
                "record_id": event["record_id"],
                "task": task,
                "result": "routed",
                "target": target,
            })
            routed += 1
    return {
        "routed": routed,
        "waiting": waiting,
        "ignored": ignored,
        "deliveries": deliveries,
        "outcomes": outcomes,
    }


def prepare_agent_catchup_deliveries(
    context: LarkEventContext,
    *,
    load_workflow: WorkflowLoader,
) -> int:
    timestamp = now()
    deliveries = 0
    states = actionable_states(
        context.workflow_key,
        load_workflow=load_workflow,
    )
    if not states:
        return 0
    placeholders = ", ".join("?" for _ in states)
    with connect(context.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, context.workspace_root)
        task_states = conn.execute(
            """
            SELECT board_id, table_id, record_id, status, snapshot_json
            FROM lark_task_state
            WHERE status IN ({placeholders})
            """.format(placeholders=placeholders),
            tuple(states),
        ).fetchall()
        for state in task_states:
            event_type = states[str(state["status"])]
            event = conn.execute(
                """
                SELECT *
                FROM task_events
                WHERE board_id = ? AND table_id = ? AND record_id = ?
                  AND event_type = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (
                    state["board_id"],
                    state["table_id"],
                    state["record_id"],
                    event_type,
                ),
            ).fetchone()
            if event is None:
                continue
            task = json.loads(state["snapshot_json"])
            target = target_role(
                context.workflow_key,
                str(state["status"]),
                task,
                load_workflow=load_workflow,
            )
            if not target:
                continue
            workflow = conn.execute(
                "SELECT key FROM workflows WHERE id = ?",
                (event["workflow_id"],),
            ).fetchone()
            agents = conn.execute(
                """
                SELECT agents.*,
                       COALESCE(
                         roles.display_name_zh,
                         roles.display_name
                       ) AS role_name
                FROM agents
                JOIN roles ON roles.id = agents.role_id
                WHERE agents.workspace_id = ?
                  AND agents.workflow_id = ?
                  AND agents.role_key = ?
                ORDER BY agents.created_at, agents.id
                """,
                (workspace_id, event["workflow_id"], target),
            ).fetchall()
            for agent in agents:
                prompt = render_task_prompt(
                    context,
                    event_type=event_type,
                    event_key=str(event["event_key"]),
                    workflow_key=str(workflow["key"]),
                    role_name=str(
                        agent["role_name"] or agent["role_key"]
                    ),
                    task=task,
                    load_workflow=load_workflow,
                )
                deliveries += insert_delivery(
                    conn,
                    event,
                    agent,
                    prompt,
                    timestamp,
                )
    return deliveries
