from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import prompt_catalog
from .codex import codex_developer_context_evidence
from .config import resolve_workspace_paths
from .db import bootstrap_workspace, connect, now, workspace_id_for_root


AGENT_CONTEXT_QUERY = """
    SELECT agent.*, workspace.display_name AS workspace_name,
           workflow.key AS workflow_key,
           COALESCE(workflow.display_name_zh, workflow.display_name) AS workflow_name,
           COALESCE(workflow.short_description_zh, workflow.short_description) AS workflow_description,
           COALESCE(role.display_name_zh, role.display_name) AS role_name,
           COALESCE(role.description_zh, role.description) AS role_description
    FROM agents AS agent
    JOIN workspaces AS workspace ON workspace.id = agent.workspace_id
    JOIN workflows AS workflow ON workflow.id = agent.workflow_id
    JOIN roles AS role ON role.id = agent.role_id
"""


def agent_context(
    workspace: str | None,
    *,
    session_id: str,
    consume: bool = False,
    refresh: bool = False,
) -> dict[str, Any] | None:
    paths = resolve_workspace_paths(workspace)
    if not paths.db_path.exists():
        return None
    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, paths.root)
        row = conn.execute(
            f"{AGENT_CONTEXT_QUERY} WHERE agent.workspace_id = ? AND agent.harness_type = 'codex' AND agent.session_id = ?",
            (workspace_id, session_id),
        ).fetchone()
        if row is None:
            return None
        assignment = assignment_from_row(row, paths.root)
        onboarding_context = render_agent_context(assignment, onboarding=True)
        fingerprint = agent_context_fingerprint(assignment, onboarding_context)
        should_inject = row["context_fingerprint"] != fingerprint
        recovery_pending = (
            should_inject
            and row["context_fingerprint"] is None
            and bool(row["context_injected_at"])
        )
        context_kind = "recovery" if recovery_pending or refresh else "onboarding" if should_inject else None
        return {
            "assignment": assignment,
            "context_fingerprint": fingerprint,
            "context_kind": context_kind,
            "context_status": (
                "recovery_pending"
                if recovery_pending
                else "pending"
                if should_inject
                else "injected"
            ),
            "context_injected_at": row["context_injected_at"],
            "additional_context": (
                render_agent_context(
                    assignment,
                    onboarding=context_kind == "onboarding",
                    recovery=context_kind == "recovery",
                )
                if consume and (should_inject or refresh)
                else None
            ),
        }


def confirm_agent_context(
    workspace: str | None,
    *,
    agent_id: str,
    session_id: str,
    assignment_revision: int,
    context_fingerprint: str,
) -> dict[str, Any]:
    paths = resolve_workspace_paths(workspace)
    if not paths.db_path.exists():
        return {"confirmed": False}
    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, paths.root)
        cursor = conn.execute(
            """
            UPDATE agents
            SET context_fingerprint = ?, context_injected_at = ?
            WHERE id = ? AND workspace_id = ? AND harness_type = 'codex'
              AND session_id = ? AND assignment_revision = ?
            """,
            (
                context_fingerprint,
                now(),
                agent_id,
                workspace_id,
                session_id,
                assignment_revision,
            ),
        )
        if cursor.rowcount != 1:
            return {"confirmed": False}
        row = conn.execute(
            f"{AGENT_CONTEXT_QUERY} WHERE agent.id = ?",
            (agent_id,),
        ).fetchone()
        assignment = assignment_from_row(row, paths.root)
        expected = agent_context_fingerprint(
            assignment,
            render_agent_context(assignment, onboarding=True),
        )
        if expected != context_fingerprint:
            conn.execute(
                """
                UPDATE agents
                SET context_fingerprint = NULL, context_injected_at = NULL
                WHERE id = ? AND context_fingerprint = ?
                """,
                (agent_id, context_fingerprint),
            )
            return {"confirmed": False}
        return {
            "confirmed": True,
            "context_injected_at": row["context_injected_at"],
            "assignment": assignment,
        }


def mark_agent_context_recovery_pending(
    workspace: str | None,
    *,
    agent_id: str,
    session_id: str,
    assignment_revision: int,
) -> dict[str, Any]:
    paths = resolve_workspace_paths(workspace)
    if not paths.db_path.exists():
        return {"marked": False}
    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, paths.root)
        cursor = conn.execute(
            """
            UPDATE agents
            SET context_fingerprint = NULL
            WHERE id = ? AND workspace_id = ? AND harness_type = 'codex'
              AND session_id = ? AND assignment_revision = ?
              AND context_injected_at IS NOT NULL
            """,
            (
                agent_id,
                workspace_id,
                session_id,
                assignment_revision,
            ),
        )
        if cursor.rowcount != 1:
            return {"marked": False}
        row = conn.execute(
            f"{AGENT_CONTEXT_QUERY} WHERE agent.id = ?",
            (agent_id,),
        ).fetchone()
        return {
            "marked": True,
            "assignment": assignment_from_row(row, paths.root),
        }


def find_agent_assignment(
    workspaces: list[str],
    *,
    session_id: str,
    cwd: str | None = None,
    consume: bool = False,
    refresh: bool = False,
) -> dict[str, Any] | None:
    candidates = []
    current = Path(cwd).expanduser().resolve() if cwd else None
    for workspace in workspaces:
        root = Path(workspace).expanduser().resolve()
        if current and current != root and root not in current.parents:
            continue
        assignment = agent_context(str(root), session_id=session_id)
        if assignment:
            candidates.append(assignment)
    if not candidates:
        return None
    if len(candidates) > 1:
        roots = ", ".join(item["assignment"]["workspace_root"] for item in candidates)
        raise ValueError(f"Codex session is registered in multiple enabled workspaces: {roots}")
    selected = candidates[0]
    if not consume:
        return selected
    return agent_context(
        selected["assignment"]["workspace_root"],
        session_id=session_id,
        consume=True,
        refresh=refresh,
    )


def inspect_agent_contexts(
    workspace: str | None,
    *,
    agent_id: str | None = None,
    role_key: str | None = None,
    session_id: str | None = None,
    include_evidence: bool = False,
) -> list[dict[str, Any]]:
    paths = resolve_workspace_paths(workspace)
    if not paths.db_path.exists():
        return []
    filters = ["agent.workspace_id = ?"]
    values: list[Any] = []
    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        values.append(workspace_id_for_root(conn, paths.root))
        if agent_id:
            filters.append("agent.id = ?")
            values.append(agent_id)
        if role_key:
            filters.append("agent.role_key = ?")
            values.append(role_key)
        if session_id:
            filters.append("agent.session_id = ?")
            values.append(session_id)
        rows = conn.execute(
            f"{AGENT_CONTEXT_QUERY} WHERE {' AND '.join(filters)} ORDER BY agent.role_key, agent.updated_at DESC",
            values,
        ).fetchall()

    contexts = []
    for row in rows:
        assignment = assignment_from_row(row, paths.root)
        context = render_agent_context(assignment, onboarding=True)
        fingerprint = agent_context_fingerprint(assignment, context)
        status = (
            "injected"
            if row["context_fingerprint"] == fingerprint
            else "recovery_pending"
            if row["context_fingerprint"] is None and row["context_injected_at"]
            else "pending"
        )
        item = {
            "agent_id": assignment["agent_id"],
            "agent_name": assignment["agent_name"],
            "role_key": assignment["role_key"],
            "role_name": assignment["role_name"],
            "session_id": assignment["session_id"],
            "assignment_revision": assignment["assignment_revision"],
            "status": status,
            "context": context,
            "context_fingerprint": fingerprint,
            "injected_fingerprint": row["context_fingerprint"],
            "injected_at": row["context_injected_at"],
        }
        if include_evidence and row["context_injected_at"]:
            item["evidence"] = codex_developer_context_evidence(
                assignment["session_id"],
                {
                    "onboarding": context,
                    "recovery": render_agent_context(
                        assignment,
                        onboarding=False,
                        recovery=True,
                    ),
                },
                injected_at=row["context_injected_at"],
            )
        contexts.append(item)
    return contexts


def attach_agent_contexts(workspace: str | None, state: dict[str, Any]) -> dict[str, Any]:
    contexts = {
        item["agent_id"]: item
        for item in inspect_agent_contexts(workspace)
    }
    for agent in state.get("agents") or []:
        agent["onboarding"] = contexts.get(agent["id"])
    return state


def assignment_from_row(row: Any, workspace_root: Path) -> dict[str, Any]:
    return {
        "agent_id": str(row["id"]),
        "workspace_root": str(workspace_root),
        "workspace_name": str(row["workspace_name"] or workspace_root.name),
        "workflow_key": str(row["workflow_key"]),
        "workflow_name": str(row["workflow_name"]),
        "role_key": str(row["role_key"]),
        "role_name": str(row["role_name"]),
        "role_description": str(row["role_description"] or ""),
        "agent_name": str(row["display_name"] or row["role_name"]),
        "session_id": str(row["session_id"]),
        "assignment_revision": int(row["assignment_revision"]),
    }


def agent_context_fingerprint(assignment: dict[str, Any], context: str) -> str:
    payload = {
        "agent_id": assignment["agent_id"],
        "session_id": assignment["session_id"],
        "assignment_revision": assignment["assignment_revision"],
        "context": context,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def render_agent_context(
    assignment: dict[str, Any],
    *,
    onboarding: bool,
    recovery: bool = False,
) -> str:
    prompt_id, trigger = (
        ("assignment-context.onboarding", "onboarding")
        if onboarding
        else ("assignment-context.recovery", "compact_recovery")
        if recovery
        else ("assignment-context.current", "status_snapshot")
    )
    variables = {
        key: assignment[key]
        for key in prompt_catalog.entry(prompt_id)["required_variables"]
    }
    return prompt_catalog.render(
        prompt_id,
        surface="hook_additional_context",
        trigger=trigger,
        variables=variables,
    )
