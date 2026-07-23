from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import resolve_workspace_paths
from .db import bootstrap_workspace, connect, now, workspace_id_for_root


def agent_context(
    workspace: str | None,
    *,
    session_id: str,
    consume: bool = False,
) -> dict[str, Any] | None:
    paths = resolve_workspace_paths(workspace)
    if not paths.db_path.exists():
        return None
    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, paths.root)
        row = conn.execute(
            """
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
            WHERE agent.workspace_id = ? AND agent.harness_type = 'codex' AND agent.session_id = ?
            """,
            (workspace_id, session_id),
        ).fetchone()
        if row is None:
            return None
        revision = int(row["assignment_revision"])
        should_inject = revision > int(row["context_applied_revision"])
        if should_inject and consume:
            cursor = conn.execute(
                """
                UPDATE agents
                SET context_applied_revision = assignment_revision, context_applied_at = ?
                WHERE id = ? AND context_applied_revision < assignment_revision
                """,
                (now(), row["id"]),
            )
            should_inject = cursor.rowcount == 1
        assignment = {
            "agent_id": str(row["id"]),
            "workspace_root": str(paths.root),
            "workspace_name": str(row["workspace_name"] or Path(paths.root).name),
            "workflow_key": str(row["workflow_key"]),
            "workflow_name": str(row["workflow_name"]),
            "role_key": str(row["role_key"]),
            "role_name": str(row["role_name"]),
            "role_description": str(row["role_description"] or ""),
            "agent_name": str(row["display_name"] or row["role_name"]),
            "assignment_revision": revision,
        }
        return {
            "assignment": assignment,
            "additional_context": (
                render_agent_context(assignment, onboarding=should_inject)
                if consume
                else None
            ),
        }


def find_agent_assignment(
    workspaces: list[str],
    *,
    session_id: str,
    cwd: str | None = None,
    consume: bool = False,
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
    )


def render_agent_context(assignment: dict[str, Any], *, onboarding: bool) -> str:
    lines = [
        "TeamFlow 当前职责上下文：",
        f"工作区：{assignment['workspace_name']} ({assignment['workspace_root']})",
        f"协作模式：{assignment['workflow_name']} ({assignment['workflow_key']})",
        f"职责：{assignment['role_name']} ({assignment['role_key']})",
        "TeamFlow 看板是团队共享事实源。读取或变更任务时使用 TeamFlow MCP 工具，不要直接调用底层 Lark CLI 或飞书 API。",
        "收到可执行任务通知不代表已经认领；只有 claim_task 成功后才开始执行。",
    ]
    if onboarding:
        lines[0] = "你已被注册为 TeamFlow Agent。"
        lines.insert(4, f"职责说明：{assignment['role_description']}")
        lines.append("如果工具拒绝某个状态转换，保留错误信息并按协作模式选择合法下一步，不要绕过工具直接改表。")
    return "\n".join(lines)
