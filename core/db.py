from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlencode, urlparse

from .codex import codex_thread_error, codex_thread_name, list_codex_threads, read_codex_thread
from .config import ensure_workspace_gitignore, parse_lark_bitable_url, resolve_workspace_paths
from .migrations import MIGRATIONS
from .workflow import load_workflow_definitions, sync_workflow_definitions


SCHEMA_VERSION = MIGRATIONS[-1].ID
DEFAULT_WORKFLOW_KEY = "software-development"
SUPPORTED_HARNESS_TYPES = ("codex",)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_workspace(workspace: str | None, display_name: str | None = None, write_gitignore: bool = False) -> dict[str, Any]:
    paths = resolve_workspace_paths(workspace)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.state_dir, 0o700)

    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        upsert_workspace(conn, paths.root, display_name)

    os.chmod(paths.db_path, 0o600)
    gitignore_updated = ensure_workspace_gitignore(paths) if write_gitignore else False
    return {
        "ok": True,
        "workspace_root": str(paths.root),
        "state_dir": str(paths.state_dir),
        "db_path": str(paths.db_path),
        "schema_version": SCHEMA_VERSION,
        "gitignore_updated": gitignore_updated,
    }


def inspect_workspace(workspace: str | None) -> dict[str, Any]:
    paths = resolve_workspace_paths(workspace)
    if not paths.db_path.exists():
        return {
            "ok": True,
            "initialized": False,
            "workspace_root": str(paths.root),
            "state_dir": str(paths.state_dir),
            "db_path": str(paths.db_path),
        }

    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_row = conn.execute(
            "SELECT * FROM workspaces WHERE root_path = ?",
            (str(paths.root),),
        ).fetchone()
        if workspace_row is None:
            workspace_row = conn.execute("SELECT * FROM workspaces ORDER BY created_at LIMIT 1").fetchone()

        workspace_id = workspace_row["id"] if workspace_row else None
        board_row = conn.execute("SELECT * FROM lark_boards WHERE workspace_id = ?", (workspace_id,)).fetchone()
        access_rows = fetch_all(
            conn,
            """
            SELECT access.*
            FROM lark_board_identity_access AS access
            JOIN lark_boards AS board ON board.id = access.board_id
            WHERE board.workspace_id = ?
            ORDER BY access.identity_id
            """,
            workspace_id,
        )
        for access in access_rows:
            access["missing_scopes"] = json.loads(access["missing_scopes"] or "[]")
        return {
            "ok": True,
            "initialized": True,
            "workspace_root": str(paths.root),
            "state_dir": str(paths.state_dir),
            "db_path": str(paths.db_path),
            "schema_version": current_schema_version(conn),
            "workspace": row_dict(workspace_row),
            "lark_identities": redact_rows(fetch_all(conn, "SELECT * FROM lark_identities WHERE workspace_id = ? ORDER BY is_default DESC, updated_at DESC", workspace_id)),
            "lark_board": row_dict(board_row),
            "lark_board_access": access_rows,
            "current_workflow": row_dict(current_workflow(conn, workspace_row)),
            "workflows": fetch_all(conn, "SELECT * FROM workflows ORDER BY key"),
            "roles": fetch_all(conn, """
                SELECT roles.*, workflows.key AS workflow_key
                FROM roles
                JOIN workflows ON workflows.id = roles.workflow_id
                ORDER BY workflows.key, roles.role_key
            """),
            "task_types": fetch_all(conn, """
                SELECT task_types.*, workflows.key AS workflow_key
                FROM task_types
                JOIN workflows ON workflows.id = task_types.workflow_id
                ORDER BY workflows.key, task_types.type_key
            """),
            "agents": fetch_all(conn, """
                SELECT agents.*, workflows.key AS workflow_key
                FROM agents
                LEFT JOIN workflows ON workflows.id = agents.workflow_id
                WHERE agents.workspace_id = ?
                ORDER BY agents.role_key, agents.updated_at DESC
            """, workspace_id),
        }


def configure_lark_identity(
    workspace: str | None,
    *,
    app_id: str,
    app_secret: str,
    domain: str,
    write_gitignore: bool = False,
) -> dict[str, Any]:
    init_result = init_workspace(workspace, write_gitignore=write_gitignore)
    paths = resolve_workspace_paths(workspace)
    app_id = app_id.strip()
    if not app_id or not app_secret:
        raise ValueError("app_id and app_secret are required")
    app_name, app_avatar_url, error = fetch_lark_app_info(app_id, app_secret, domain)

    with connect(paths.db_path) as conn:
        workspace_id = workspace_id_for_root(conn, paths.root)
        timestamp = now()
        identity_id = upsert_lark_identity(
            conn,
            workspace_id=workspace_id,
            auth_mode="bot",
            app_id=app_id,
            app_name=app_name,
            app_avatar_url=app_avatar_url,
            app_name_synced_at=timestamp if app_name else None,
            is_default=None,
            app_secret=app_secret,
            access_token=None,
            refresh_token=None,
        )
        if error:
            conn.execute(
                "UPDATE lark_identities SET last_error = ?, updated_at = ? WHERE id = ?",
                (error, timestamp, identity_id),
            )

    return {
        "ok": True,
        **init_result,
        "lark_identity_id": identity_id,
        "app_name": app_name,
        "app_avatar_url": app_avatar_url,
        "metadata_error": error,
        "access_status": "unverified",
    }


def verify_lark_user_identity(
    workspace: str | None,
    *,
    status: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    paths = resolve_workspace_paths(workspace)
    timestamp = now()
    try:
        app_id, user_open_id, user_name = lark_user_status_values(status)
    except ValueError as error:
        if paths.db_path.exists():
            with connect(paths.db_path) as conn:
                bootstrap_workspace(conn)
                workspace_id = workspace_id_for_root(conn, paths.root)
                conn.execute(
                    """
                    UPDATE lark_identities
                    SET access_status = ?, last_verified_at = ?, last_error = ?, is_default = 0, updated_at = ?
                    WHERE workspace_id = ? AND auth_mode = 'user'
                    """,
                    (status.get("tokenStatus") or "unavailable", timestamp, str(error), timestamp, workspace_id),
                )
                ensure_default_lark_identity(conn, workspace_id)
        raise

    profile_user = ((profile or {}).get("data") or {}).get("user") or {}
    profile_open_id = str(profile_user.get("open_id") or "").strip()
    if profile_open_id and profile_open_id != user_open_id:
        raise ValueError("the active Lark profile does not match the authorized user")
    user_name = str(profile_user.get("name") or user_name).strip()
    user_avatar_url = str(
        profile_user.get("avatar_middle")
        or profile_user.get("avatar_url")
        or profile_user.get("avatar_big")
        or profile_user.get("avatar_thumb")
        or ""
    ).strip()

    init_result = init_workspace(workspace)
    with connect(paths.db_path) as conn:
        workspace_id = workspace_id_for_root(conn, paths.root)
        identity_id = upsert_lark_user_identity(
            conn,
            workspace_id=workspace_id,
            app_id=app_id,
            user_open_id=user_open_id,
            user_name=user_name,
            user_avatar_url=user_avatar_url,
        )

    return {
        "ok": True,
        **init_result,
        "lark_identity_id": identity_id,
        "auth_mode": "user",
        "user_open_id": user_open_id,
        "user_name": user_name,
        "user_avatar_url": user_avatar_url or None,
        "access_status": "verified",
    }


def configure_lark_board(
    workspace: str | None,
    *,
    board_url: str,
    write_gitignore: bool = False,
) -> dict[str, Any]:
    init_result = init_workspace(workspace, write_gitignore=write_gitignore)
    paths = resolve_workspace_paths(workspace)
    board_url = board_url.strip()
    parsed = parse_lark_bitable_url(board_url)

    try:
        with connect(paths.db_path) as conn:
            workspace_id = workspace_id_for_root(conn, paths.root)
            identity = default_lark_identity(conn, workspace_id)
            base_token = parsed["base_token"]
            if not base_token and parsed["wiki_token"]:
                base_token = resolve_lark_wiki_bitable(identity, parsed["wiki_token"], board_url)
            if not base_token:
                raise ValueError("a valid Feishu/Lark Bitable URL is required")
            board_id = upsert_lark_board(
                conn,
                workspace_id=workspace_id,
                primary_identity_id=identity["id"] if identity else None,
                base_url=board_url,
                base_token=base_token,
                table_id=parsed["table_id"],
                view_id=parsed["view_id"],
            )
    except ValueError as error:
        message = str(error)
        if "131005" in message and "not found" in message.lower():
            with connect(paths.db_path) as conn:
                workspace_id = workspace_id_for_root(conn, paths.root)
                board = conn.execute("SELECT * FROM lark_boards WHERE workspace_id = ?", (workspace_id,)).fetchone()
                if board:
                    current = parse_lark_bitable_url(board["base_url"])
                    same_resource = (
                        parsed["wiki_token"] and parsed["wiki_token"] == current["wiki_token"]
                    ) or (
                        parsed["base_token"] and parsed["base_token"] == board["base_token"]
                    )
                    if same_resource:
                        timestamp = now()
                        conn.execute("DELETE FROM lark_board_identity_access WHERE board_id = ?", (board["id"],))
                        conn.execute(
                            "UPDATE lark_boards SET access_status = 'unavailable', last_verified_at = NULL, last_error = ?, updated_at = ? WHERE id = ?",
                            (message, timestamp, board["id"]),
                        )
        raise

    return {
        "ok": True,
        **init_result,
        "lark_board_id": board_id,
        "access_status": "unverified",
    }


def resolve_lark_wiki_bitable(identity: sqlite3.Row | None, wiki_token: str, board_url: str) -> str:
    if identity is None:
        raise ValueError("save an available Lark identity before using a Wiki Bitable URL")
    if identity["auth_mode"] == "user":
        payload = run_lark_cli_json([
            "wiki",
            "spaces",
            "get_node",
            "--params",
            json.dumps({"token": wiki_token}, separators=(",", ":")),
            "--as",
            "user",
        ])
    else:
        host = urlparse(board_url).hostname or ""
        origin = "https://open.larksuite.com" if host.endswith("larksuite.com") else "https://open.feishu.cn"
        token_payload, error = post_json(
            f"{origin}/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": identity["app_id"], "app_secret": identity["app_secret"]},
            {},
        )
        token = token_payload.get("tenant_access_token")
        if error or not token:
            raise ValueError(error or token_payload.get("msg") or "failed to get tenant access token")
        payload, error = get_json(
            f"{origin}/open-apis/wiki/v2/spaces/get_node?{urlencode({'token': wiki_token})}",
            {"Authorization": f"Bearer {token}"},
        )
        if error:
            raise ValueError(error)
    node = (payload.get("data") or {}).get("node") or payload.get("node") or {}
    if node.get("obj_type") != "bitable" or not node.get("obj_token"):
        raise ValueError("the Wiki URL does not point to a Bitable")
    return str(node["obj_token"])


def select_workflow(workspace: str | None, *, workflow: str) -> dict[str, Any]:
    init_result = init_workspace(workspace)
    paths = resolve_workspace_paths(workspace)
    workflow_key = normalize_key(workflow, "workflow")

    with connect(paths.db_path) as conn:
        workspace_id = workspace_id_for_root(conn, paths.root)
        row = workflow_for_key(conn, workflow_key)
        conn.execute(
            "UPDATE workspaces SET current_workflow_id = ?, updated_at = ? WHERE id = ?",
            (row["id"], now(), workspace_id),
        )

    return {"ok": True, **init_result, "workflow_key": workflow_key}


def remove_lark_identity(workspace: str | None, *, identity_id: str) -> dict[str, Any]:
    paths = resolve_workspace_paths(workspace)
    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, paths.root)
        cursor = conn.execute(
            "DELETE FROM lark_identities WHERE workspace_id = ? AND id = ?",
            (workspace_id, identity_id),
        )
        ensure_default_lark_identity(conn, workspace_id)
    return {"ok": True, "deleted": cursor.rowcount}


def set_default_lark_identity(workspace: str | None, *, identity_id: str) -> dict[str, Any]:
    paths = resolve_workspace_paths(workspace)
    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, paths.root)
        row = conn.execute(
            "SELECT * FROM lark_identities WHERE workspace_id = ? AND id = ?",
            (workspace_id, identity_id),
        ).fetchone()
        if row is None:
            raise ValueError("lark identity not found")
        if not lark_identity_is_usable(row):
            raise ValueError("lark identity is unavailable")
        conn.execute("UPDATE lark_identities SET is_default = 0 WHERE workspace_id = ?", (workspace_id,))
        conn.execute("UPDATE lark_identities SET is_default = 1, updated_at = ? WHERE id = ?", (now(), identity_id))
    return {"ok": True, "identity_id": identity_id}


def refresh_lark_identity(workspace: str | None, *, identity_id: str, domain: str) -> dict[str, Any]:
    paths = resolve_workspace_paths(workspace)
    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, paths.root)
        row = conn.execute(
            "SELECT * FROM lark_identities WHERE workspace_id = ? AND id = ?",
            (workspace_id, identity_id),
        ).fetchone()
        if row is None:
            raise ValueError("lark identity not found")
        if not row["app_id"] or not row["app_secret"]:
            raise ValueError("app_id and app_secret are required")
        timestamp = now()
        app_name, app_avatar_url, error = fetch_lark_app_info(row["app_id"], row["app_secret"], domain)
        if app_name:
            conn.execute(
                "UPDATE lark_identities SET app_name = ?, app_avatar_url = ?, app_name_synced_at = ?, last_error = NULL, updated_at = ? WHERE id = ?",
                (app_name, app_avatar_url, timestamp, timestamp, identity_id),
            )
            return {"ok": True, "identity_id": identity_id, "app_name": app_name, "app_avatar_url": app_avatar_url}
        conn.execute(
            "UPDATE lark_identities SET last_error = ?, updated_at = ? WHERE id = ?",
            (error or "failed to read app name", timestamp, identity_id),
        )
    return {"ok": False, "identity_id": identity_id, "last_error": error or "failed to read app name"}


def create_lark_board(workspace: str | None, *, domain: str, name: str) -> dict[str, Any]:
    init_workspace(workspace)
    paths = resolve_workspace_paths(workspace)
    with connect(paths.db_path) as conn:
        workspace_id = workspace_id_for_root(conn, paths.root)
        project_name = paths.root.name
        board_name = name.strip() or f"{project_name}{'看板' if project_name.endswith('项目') else '项目看板'}"
        identity = default_lark_identity(conn, workspace_id)
        if identity is None:
            raise ValueError("save an available Lark identity before creating a Bitable")
        if identity["auth_mode"] == "user":
            status = run_lark_cli_json(["auth", "status", "--verify"])
            try:
                lark_user_status_values(status, expected_user_open_id=identity["user_open_id"])
            except ValueError as error:
                timestamp = now()
                conn.execute(
                    """
                    UPDATE lark_identities
                    SET access_status = ?, last_verified_at = ?, last_error = ?, is_default = 0, updated_at = ?
                    WHERE id = ?
                    """,
                    (status.get("tokenStatus") or "unavailable", timestamp, str(error), timestamp, identity["id"]),
                )
                ensure_default_lark_identity(conn, workspace_id)
                conn.commit()
                raise
            base_payload = run_lark_cli_json(["base", "+base-create", "--as", "user", "--name", board_name])
        else:
            origin = "https://open.larksuite.com" if domain == "larksuite" else "https://open.feishu.cn"
            token_payload, error = post_json(
                f"{origin}/open-apis/auth/v3/tenant_access_token/internal",
                {"app_id": identity["app_id"], "app_secret": identity["app_secret"]},
                {},
            )
            token = token_payload.get("tenant_access_token")
            if error or not token:
                raise ValueError(error or token_payload.get("msg") or "failed to get tenant access token")
            base_payload, error = post_json(
                f"{origin}/open-apis/base/v3/bases",
                {"name": board_name},
                {"Authorization": f"Bearer {token}"},
            )
            if error:
                raise ValueError(error)
        data = base_payload.get("data") or {}
        base = base_payload.get("base") or data.get("base") or data
        base_url = base.get("url") or base.get("app_url")
        base_token = base.get("base_token") or base.get("app_token") or base.get("token")
        if not base_token:
            raise ValueError("Bitable creation did not return a token")
        board_id = upsert_lark_board(
            conn,
            workspace_id=workspace_id,
            primary_identity_id=identity["id"],
            base_url=base_url,
            base_token=base_token,
            table_id=None,
            view_id=None,
        )
    return {"ok": True, "lark_board_id": board_id, "board_url": base_url}


def register_agent(
    workspace: str | None,
    *,
    role: str,
    workflow: str | None = None,
    harness_type: str,
    session_id: str,
    display_name: str | None = None,
    replace_role: bool = False,
) -> dict[str, Any]:
    init_result = init_workspace(workspace)
    paths = resolve_workspace_paths(workspace)
    role_key = normalize_key(role, "role")
    harness = normalize_key(harness_type, "harness_type")
    if harness not in SUPPORTED_HARNESS_TYPES:
        raise ValueError(f"unsupported harness type: {harness}. Supported harness types: {', '.join(SUPPORTED_HARNESS_TYPES)}")
    session = session_id.strip()
    if not session:
        raise ValueError("session_id is required")

    with connect(paths.db_path) as conn:
        workspace_id = workspace_id_for_root(conn, paths.root)
        workflow_key = normalize_key(workflow or current_workflow_key(conn, workspace_id), "workflow")
        role_row = role_for_key(conn, workflow_key, role_key)
        if replace_role:
            conn.execute("DELETE FROM agents WHERE workspace_id = ? AND role_id = ?", (workspace_id, role_row["id"]))
        elif not role_row["allow_multiple"]:
            assert_single_agent_role_available(conn, workspace_id, role_row, harness, session)
        agent_id = upsert_agent(
            conn,
            workspace_id=workspace_id,
            role=role_row,
            harness_type=harness,
            session_id=session,
            display_name=display_name,
        )

    health = verify_agent(workspace, agent_id=agent_id)

    return {
        "ok": True,
        **init_result,
        "agent_id": agent_id,
        "workflow_key": workflow_key,
        "role_key": role_key,
        "harness_type": harness,
        "session_id": session,
        "replaced_role": replace_role,
        "health": health,
    }


def list_codex_sessions(workspace: str | None) -> dict[str, Any]:
    paths = resolve_workspace_paths(workspace)
    return {
        "ok": True,
        "workspace_root": str(paths.root),
        "sessions": codex_session_rows(list_codex_threads(str(paths.root))),
    }


def verify_agents(workspace: str | None, *, agent_id: str | None = None) -> dict[str, Any]:
    paths = resolve_workspace_paths(workspace)
    if not paths.db_path.exists():
        raise ValueError("TeamFlow workspace is not initialized")

    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, paths.root)
        query = "SELECT * FROM agents WHERE workspace_id = ? AND harness_type = 'codex'"
        params: tuple[Any, ...] = (workspace_id,)
        if agent_id:
            query += " AND id = ?"
            params += (agent_id,)
        query += " ORDER BY updated_at DESC"
        agents = conn.execute(query, params).fetchall()
        if agent_id and not agents:
            raise ValueError("agent not found")

    checked_at = now()
    try:
        active_threads = list_codex_threads(str(paths.root))
        archived_threads = list_codex_threads(str(paths.root), archived=True)
    except ValueError as error:
        results = [
            agent_health_result(agent, status="unavailable", checked_at=checked_at, error=str(error))
            for agent in agents
        ]
        return {
            "ok": False,
            "checked": len(results),
            "results": results,
            "sessions": [],
            "session_error": str(error),
        }

    active_by_id = {thread.get("id"): thread for thread in active_threads}
    archived_by_id = {thread.get("id"): thread for thread in archived_threads}
    results = []
    for agent in agents:
        thread = active_by_id.get(agent["session_id"])
        thread_archived = False
        if thread is None:
            thread = archived_by_id.get(agent["session_id"])
            thread_archived = thread is not None

        if thread is None:
            try:
                thread = read_codex_thread(agent["session_id"])
            except ValueError:
                results.append(agent_health_result(
                    agent,
                    status="deleted",
                    checked_at=checked_at,
                    error="Codex thread no longer exists",
                ))
                continue

        runtime_status = (thread.get("status") or {}).get("type")
        if runtime_status == "systemError" and not thread_archived:
            try:
                thread = read_codex_thread(agent["session_id"], include_turns=True)
            except ValueError:
                pass
            runtime_status = (thread.get("status") or {}).get("type")
        thread_cwd = str(thread.get("cwd") or "").strip()
        thread_path = Path(thread_cwd).expanduser().resolve() if thread_cwd else None
        if thread_path is None or (thread_path != paths.root and paths.root not in thread_path.parents):
            status = "unhealthy"
            error = f"Codex thread belongs to a different workspace: {thread_cwd or 'unknown'}"
        elif thread_archived:
            status = "archived"
            error = "Codex thread is archived"
        elif runtime_status == "systemError":
            status = "system_error"
            error = codex_thread_error(thread) or "Codex session entered a system error state"
        else:
            status = "healthy"
            error = None
        results.append(agent_health_result(
            agent,
            status=status,
            checked_at=checked_at,
            session_name=codex_thread_name(thread),
            error=error,
            runtime_status=runtime_status,
            thread_cwd=thread_cwd,
            thread_archived=thread_archived,
        ))

    return {
        "ok": all(result["ok"] for result in results),
        "checked": len(results),
        "results": results,
        "sessions": codex_session_rows(active_threads),
        "session_error": None,
    }


def verify_agent(workspace: str | None, *, agent_id: str) -> dict[str, Any]:
    return verify_agents(workspace, agent_id=agent_id)["results"][0]


def update_agent(workspace: str | None, *, agent_id: str, session_id: str) -> dict[str, Any]:
    paths = resolve_workspace_paths(workspace)
    if not paths.db_path.exists():
        raise ValueError("TeamFlow workspace is not initialized")
    session = session_id.strip()
    if not session:
        raise ValueError("session_id is required")

    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, paths.root)
        agent = conn.execute(
            "SELECT * FROM agents WHERE workspace_id = ? AND id = ?",
            (workspace_id, agent_id),
        ).fetchone()
        if agent is None:
            raise ValueError("agent not found")
        duplicate = conn.execute(
            """
            SELECT id FROM agents
            WHERE workspace_id = ? AND role_id = ? AND harness_type = ? AND session_id = ? AND id != ?
            """,
            (workspace_id, agent["role_id"], agent["harness_type"], session, agent_id),
        ).fetchone()
        if duplicate:
            raise ValueError("this session is already registered for the role")
        timestamp = now()
        conn.execute(
            """
            UPDATE agents
            SET session_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (session, timestamp, agent_id),
        )

    return {
        "ok": True,
        "agent_id": agent_id,
        "session_id": session,
        "health": verify_agent(workspace, agent_id=agent_id),
    }


def codex_session_rows(threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sessions = []
    for thread in threads:
        thread_id = str(thread.get("id") or "").strip()
        if thread_id:
            sessions.append({
                "session_id": thread_id,
                "name": codex_thread_name(thread),
                "status": (thread.get("status") or {}).get("type"),
                "updated_at": thread.get("updatedAt"),
            })
    return sessions


def agent_health_result(
    agent: sqlite3.Row,
    *,
    status: str,
    checked_at: str,
    session_name: str | None = None,
    error: str | None = None,
    runtime_status: str | None = None,
    thread_cwd: str | None = None,
    thread_archived: bool | None = None,
) -> dict[str, Any]:
    return {
        "ok": status == "healthy",
        "agent_id": agent["id"],
        "harness_type": agent["harness_type"],
        "session_id": agent["session_id"],
        "session_name": session_name,
        "status": status,
        "runtime_status": runtime_status,
        "thread_cwd": thread_cwd,
        "thread_archived": thread_archived,
        "checked_at": checked_at,
        "error": error,
    }


def unregister_agent(
    workspace: str | None,
    *,
    agent_id: str | None = None,
    role: str | None = None,
    workflow: str | None = None,
    harness_type: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    paths = resolve_workspace_paths(workspace)
    if not paths.db_path.exists():
        return {"ok": True, "deleted": 0}

    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, paths.root)
        if agent_id:
            cursor = conn.execute("DELETE FROM agents WHERE workspace_id = ? AND id = ?", (workspace_id, agent_id))
        else:
            if not (role and harness_type and session_id):
                raise ValueError("agent_id or role+harness_type+session_id is required")
            workflow_key = normalize_key(workflow or current_workflow_key(conn, workspace_id), "workflow")
            role_row = role_for_key(conn, workflow_key, normalize_key(role, "role"))
            cursor = conn.execute(
                "DELETE FROM agents WHERE workspace_id = ? AND role_id = ? AND harness_type = ? AND session_id = ?",
                (workspace_id, role_row["id"], normalize_key(harness_type, "harness_type"), session_id.strip()),
            )

    return {"ok": True, "deleted": cursor.rowcount}


def run_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row["id"] for row in conn.execute("SELECT id FROM migrations")}
    for migration in MIGRATIONS:
        if migration.ID in applied:
            continue
        migration.apply(conn)
        conn.execute(
            "INSERT INTO migrations (id, applied_at) VALUES (?, ?)",
            (migration.ID, now()),
        )


def bootstrap_workspace(conn: sqlite3.Connection) -> None:
    run_migrations(conn)
    sync_workflow_definitions(conn, load_workflow_definitions())


def upsert_workspace(conn: sqlite3.Connection, root: Path, display_name: str | None) -> str:
    existing = conn.execute("SELECT id FROM workspaces WHERE root_path = ?", (str(root),)).fetchone()
    timestamp = now()
    if existing:
        conn.execute(
            """
            UPDATE workspaces
            SET display_name = COALESCE(?, display_name),
                current_workflow_id = COALESCE(current_workflow_id, (SELECT id FROM workflows WHERE key = ?)),
                updated_at = ?
            WHERE id = ?
            """,
            (display_name, DEFAULT_WORKFLOW_KEY, timestamp, existing["id"]),
        )
        return existing["id"]

    workspace_id = f"ws_{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO workspaces (id, root_path, display_name, current_workflow_id, created_at, updated_at)
        VALUES (?, ?, ?, (SELECT id FROM workflows WHERE key = ?), ?, ?)
        """,
        (workspace_id, str(root), display_name or root.name, DEFAULT_WORKFLOW_KEY, timestamp, timestamp),
    )
    return workspace_id


def upsert_lark_identity(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    auth_mode: str,
    app_id: str | None,
    app_name: str | None,
    app_avatar_url: str | None,
    app_name_synced_at: str | None,
    is_default: int | None,
    app_secret: str | None,
    access_token: str | None,
    refresh_token: str | None,
) -> str:
    existing = conn.execute(
        """
        SELECT id FROM lark_identities
        WHERE workspace_id = ? AND auth_mode = ? AND COALESCE(app_id, '') = COALESCE(?, '')
        """,
        (workspace_id, auth_mode, app_id),
    ).fetchone()
    timestamp = now()
    if existing:
        conn.execute(
            """
            UPDATE lark_identities
            SET app_name = COALESCE(?, app_name),
                app_avatar_url = COALESCE(?, app_avatar_url),
                app_name_synced_at = COALESCE(?, app_name_synced_at), is_default = COALESCE(?, is_default), app_secret = COALESCE(?, app_secret),
                access_token = COALESCE(?, access_token), refresh_token = COALESCE(?, refresh_token),
                access_status = 'unverified', last_verified_at = NULL, last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (app_name, app_avatar_url, app_name_synced_at, is_default, app_secret, access_token, refresh_token, timestamp, existing["id"]),
        )
        ensure_default_lark_identity(conn, workspace_id)
        return existing["id"]

    identity_id = f"lark_{uuid.uuid4().hex}"
    default_value = is_default if is_default is not None else int(auth_mode == "bot" and app_id and not has_default_lark_identity(conn, workspace_id))
    conn.execute(
        """
        INSERT INTO lark_identities
          (id, workspace_id, auth_mode, app_id, app_name, app_avatar_url, app_name_synced_at, is_default, app_secret, access_token, refresh_token, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (identity_id, workspace_id, auth_mode, app_id, app_name, app_avatar_url, app_name_synced_at, default_value, app_secret, access_token, refresh_token, timestamp, timestamp),
    )
    if default_value:
        conn.execute("UPDATE lark_identities SET is_default = 0 WHERE workspace_id = ? AND id != ?", (workspace_id, identity_id))
    return identity_id


def upsert_lark_user_identity(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    app_id: str,
    user_open_id: str,
    user_name: str,
    user_avatar_url: str,
) -> str:
    existing = conn.execute(
        "SELECT id FROM lark_identities WHERE workspace_id = ? AND auth_mode = 'user' ORDER BY updated_at DESC LIMIT 1",
        (workspace_id,),
    ).fetchone()
    timestamp = now()
    conn.execute("UPDATE lark_identities SET is_default = 0 WHERE workspace_id = ?", (workspace_id,))
    if existing:
        conn.execute(
            """
            UPDATE lark_identities
            SET app_id = ?, user_open_id = ?, user_name = ?,
                user_avatar_url = COALESCE(?, user_avatar_url), is_default = 1,
                access_token = NULL, refresh_token = NULL, access_status = 'verified',
                last_verified_at = ?, last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (app_id, user_open_id, user_name, user_avatar_url or None, timestamp, timestamp, existing["id"]),
        )
        return existing["id"]

    identity_id = f"lark_user_{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO lark_identities
          (id, workspace_id, auth_mode, app_id, user_open_id, user_name, user_avatar_url, is_default,
           access_status, last_verified_at, created_at, updated_at)
        VALUES (?, ?, 'user', ?, ?, ?, ?, 1, 'verified', ?, ?, ?)
        """,
        (identity_id, workspace_id, app_id, user_open_id, user_name, user_avatar_url or None, timestamp, timestamp, timestamp),
    )
    return identity_id


def upsert_lark_board(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    primary_identity_id: str | None,
    base_url: str | None,
    base_token: str | None,
    table_id: str | None,
    view_id: str | None,
) -> str:
    existing = conn.execute("SELECT * FROM lark_boards WHERE workspace_id = ?", (workspace_id,)).fetchone()
    timestamp = now()
    if existing:
        changed = any((existing[key] or None) != value for key, value in (
            ("base_token", base_token),
            ("table_id", table_id),
            ("view_id", view_id),
        ))
        stable_primary_id = primary_identity_id if changed or not existing["primary_identity_id"] else existing["primary_identity_id"]
        conn.execute(
            """
            UPDATE lark_boards
            SET primary_identity_id = ?,
                base_url = ?, base_token = ?, table_id = ?, view_id = ?,
                access_status = 'unverified', last_verified_at = NULL, last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (stable_primary_id, base_url, base_token, table_id, view_id, timestamp, existing["id"]),
        )
        if changed:
            conn.execute("DELETE FROM lark_board_identity_access WHERE board_id = ?", (existing["id"],))
        return existing["id"]

    board_id = f"board_{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO lark_boards
          (id, workspace_id, primary_identity_id, base_url, base_token, table_id, view_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (board_id, workspace_id, primary_identity_id, base_url, base_token, table_id, view_id, timestamp, timestamp),
    )
    return board_id


def has_default_lark_identity(conn: sqlite3.Connection, workspace_id: str) -> bool:
    return conn.execute(
        """
        SELECT id FROM lark_identities
        WHERE workspace_id = ? AND is_default = 1
          AND ((auth_mode = 'bot' AND app_id IS NOT NULL AND app_secret IS NOT NULL)
            OR (auth_mode = 'user' AND user_open_id IS NOT NULL AND access_status = 'verified'))
        """,
        (workspace_id,),
    ).fetchone() is not None


def lark_identity_is_usable(identity: sqlite3.Row) -> bool:
    if identity["auth_mode"] == "bot":
        return bool(identity["app_id"] and identity["app_secret"])
    return bool(identity["user_open_id"] and identity["access_status"] == "verified")


def ensure_default_lark_identity(conn: sqlite3.Connection, workspace_id: str) -> None:
    if has_default_lark_identity(conn, workspace_id):
        return
    row = conn.execute(
        """
        SELECT id FROM lark_identities
        WHERE workspace_id = ?
          AND ((auth_mode = 'bot' AND app_id IS NOT NULL AND app_secret IS NOT NULL)
            OR (auth_mode = 'user' AND user_open_id IS NOT NULL AND access_status = 'verified'))
        ORDER BY updated_at DESC LIMIT 1
        """,
        (workspace_id,),
    ).fetchone()
    if row:
        conn.execute("UPDATE lark_identities SET is_default = 0 WHERE workspace_id = ?", (workspace_id,))
        conn.execute("UPDATE lark_identities SET is_default = 1 WHERE id = ?", (row["id"],))


def default_lark_identity(conn: sqlite3.Connection, workspace_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM lark_identities
        WHERE workspace_id = ?
          AND ((auth_mode = 'bot' AND app_id IS NOT NULL AND app_secret IS NOT NULL)
            OR (auth_mode = 'user' AND user_open_id IS NOT NULL AND access_status = 'verified'))
        ORDER BY is_default DESC, updated_at DESC
        LIMIT 1
        """,
        (workspace_id,),
    ).fetchone()


def upsert_agent(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    role: sqlite3.Row,
    harness_type: str,
    session_id: str,
    display_name: str | None,
) -> str:
    existing = conn.execute(
        "SELECT id FROM agents WHERE workspace_id = ? AND role_id = ? AND harness_type = ? AND session_id = ?",
        (workspace_id, role["id"], harness_type, session_id),
    ).fetchone()
    timestamp = now()
    if existing:
        conn.execute(
            """
            UPDATE agents
            SET display_name = COALESCE(?, display_name), updated_at = ?
            WHERE id = ?
            """,
            (display_name, timestamp, existing["id"]),
        )
        return existing["id"]

    agent_id = f"agent_{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO agents
          (id, workspace_id, workflow_id, role_id, role_key, harness_type, session_id, display_name, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (agent_id, workspace_id, role["workflow_id"], role["id"], role["role_key"], harness_type, session_id, display_name, timestamp, timestamp),
    )
    return agent_id


def role_for_key(conn: sqlite3.Connection, workflow_key: str, role_key: str) -> sqlite3.Row:
    role = conn.execute(
        """
        SELECT roles.*, workflows.key AS workflow_key
        FROM roles
        JOIN workflows ON workflows.id = roles.workflow_id
        WHERE workflows.key = ? AND roles.role_key = ?
        """,
        (workflow_key, role_key),
    ).fetchone()
    if role is None:
        supported = [
            f"{row['workflow_key']}:{row['role_key']}"
            for row in conn.execute(
                """
                SELECT workflows.key AS workflow_key, roles.role_key
                FROM roles
                JOIN workflows ON workflows.id = roles.workflow_id
                ORDER BY workflows.key, roles.role_key
                """
            )
        ]
        raise ValueError(f"unsupported role: {workflow_key}:{role_key}. Supported roles: {', '.join(supported)}")
    return role


def workflow_for_key(conn: sqlite3.Connection, workflow_key: str) -> sqlite3.Row:
    workflow = conn.execute("SELECT * FROM workflows WHERE key = ?", (workflow_key,)).fetchone()
    if workflow is None:
        supported = [row["key"] for row in conn.execute("SELECT key FROM workflows ORDER BY key")]
        raise ValueError(f"unsupported workflow: {workflow_key}. Supported workflows: {', '.join(supported)}")
    return workflow


def current_workflow(conn: sqlite3.Connection, workspace_row: sqlite3.Row | None) -> sqlite3.Row | None:
    if workspace_row and "current_workflow_id" in workspace_row.keys() and workspace_row["current_workflow_id"]:
        workflow = conn.execute("SELECT * FROM workflows WHERE id = ?", (workspace_row["current_workflow_id"],)).fetchone()
        if workflow:
            return workflow
    return conn.execute("SELECT * FROM workflows WHERE key = ?", (DEFAULT_WORKFLOW_KEY,)).fetchone()


def current_workflow_key(conn: sqlite3.Connection, workspace_id: str) -> str:
    workspace = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    workflow = current_workflow(conn, workspace)
    if workflow is None:
        raise ValueError("no workflow is configured")
    return workflow["key"]


def assert_single_agent_role_available(
    conn: sqlite3.Connection,
    workspace_id: str,
    role: sqlite3.Row,
    harness_type: str,
    session_id: str,
) -> None:
    existing = conn.execute(
        """
        SELECT id, harness_type, session_id FROM agents
        WHERE workspace_id = ? AND role_id = ?
          AND NOT (harness_type = ? AND session_id = ?)
        LIMIT 1
        """,
        (workspace_id, role["id"], harness_type, session_id),
    ).fetchone()
    if existing:
        raise ValueError(
            f"role {role['workflow_key']}:{role['role_key']} allows only one agent; existing agent {existing['id']} "
            f"uses {existing['harness_type']} session {existing['session_id']}; use --replace-role to replace it"
        )


def workspace_id_for_root(conn: sqlite3.Connection, root: Path) -> str:
    row = conn.execute("SELECT id FROM workspaces WHERE root_path = ?", (str(root),)).fetchone()
    if row is None:
        raise RuntimeError(f"Workspace is not initialized: {root}")
    return row["id"]


def current_schema_version(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT id FROM migrations ORDER BY id DESC LIMIT 1").fetchone()
    return row["id"] if row else ""


def normalize_key(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized


def lark_user_status_values(status: dict[str, Any], *, expected_user_open_id: str | None = None) -> tuple[str, str, str]:
    if status.get("tokenStatus") != "valid":
        raise ValueError(status.get("note") or "Lark user authorization is unavailable; authorize again")
    app_id = str(status.get("appId") or "").strip()
    user_open_id = str(status.get("userOpenId") or "").strip()
    user_name = str(status.get("userName") or "").strip()
    if not app_id or not user_open_id:
        raise ValueError("Lark user authorization did not return an app or user identity")
    if expected_user_open_id and user_open_id != expected_user_open_id:
        raise ValueError("the active Lark user does not match the TeamFlow identity")
    return app_id, user_open_id, user_name or user_open_id


def fetch_lark_app_info(app_id: str, app_secret: str, domain: str) -> tuple[str | None, str | None, str | None]:
    origin = "https://open.larksuite.com" if domain == "larksuite" else "https://open.feishu.cn"
    lang = "en_us" if domain == "larksuite" else "zh_cn"
    token_payload, error = post_json(
        f"{origin}/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": app_id, "app_secret": app_secret},
        {},
    )
    if error:
        return None, None, error
    token = token_payload.get("tenant_access_token")
    if not token:
        return None, None, token_payload.get("msg") or "failed to get tenant access token"

    app_payload, error = get_json(
        f"{origin}/open-apis/application/v6/applications/{app_id}?lang={lang}",
        {"Authorization": f"Bearer {token}"},
    )
    if error:
        return None, None, error
    data = app_payload.get("data") or {}
    app = data.get("app") or {}
    name = app.get("app_name") or data.get("app_name") or app.get("name") or data.get("name")
    avatar_url = app.get("avatar_url") or data.get("avatar_url")
    return name, avatar_url, None if name else (app_payload.get("msg") or "app name not found")


def run_lark_cli_json(args: list[str]) -> dict[str, Any]:
    result = subprocess.run(
        [os.environ.get("LARK_CLI", "lark-cli"), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout.strip()
    if result.returncode:
        raise ValueError(result.stderr.strip() or output or "lark-cli command failed")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise ValueError("lark-cli did not return JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("lark-cli did not return a JSON object")
    return payload


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[dict[str, Any], str | None]:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers={"content-type": "application/json; charset=utf-8", **headers})
    return read_json(request)


def get_json(url: str, headers: dict[str, str]) -> tuple[dict[str, Any], str | None]:
    return read_json(urllib.request.Request(url, headers=headers))


def read_json(request: urllib.request.Request) -> tuple[dict[str, Any], str | None]:
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        payload = json.loads(error.read() or b"{}")
    except Exception as error:
        return {}, str(error)
    if payload.get("code") not in (None, 0):
        return payload, payload.get("msg") or "Lark API error"
    return payload, None


def fetch_all(conn: sqlite3.Connection, sql: str, *params: Any) -> list[dict[str, Any]]:
    if params == (None,):
        return []
    return [dict(row) for row in conn.execute(sql, params)]


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def redact_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    redacted = []
    for row in rows:
        item = dict(row)
        for key in ("app_secret", "access_token", "refresh_token"):
            if item.get(key):
                item[key] = "<stored>"
        redacted.append(item)
    return redacted
