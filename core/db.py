from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ensure_workspace_gitignore, parse_lark_bitable_url, resolve_workspace_paths
from .migrations import MIGRATIONS


SCHEMA_VERSION = MIGRATIONS[-1].ID
DEFAULT_WORKFLOW_KEY = "software-development"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_workspace(workspace: str | None, display_name: str | None = None, write_gitignore: bool = False) -> dict[str, Any]:
    paths = resolve_workspace_paths(workspace)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.state_dir, 0o700)

    with connect(paths.db_path) as conn:
        run_migrations(conn)
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
        run_migrations(conn)
        workspace_row = conn.execute(
            "SELECT * FROM workspaces WHERE root_path = ?",
            (str(paths.root),),
        ).fetchone()
        if workspace_row is None:
            workspace_row = conn.execute("SELECT * FROM workspaces ORDER BY created_at LIMIT 1").fetchone()

        workspace_id = workspace_row["id"] if workspace_row else None
        return {
            "ok": True,
            "initialized": True,
            "workspace_root": str(paths.root),
            "state_dir": str(paths.state_dir),
            "db_path": str(paths.db_path),
            "schema_version": current_schema_version(conn),
            "workspace": row_dict(workspace_row),
            "lark_identities": redact_rows(fetch_all(conn, "SELECT * FROM lark_identities WHERE workspace_id = ? ORDER BY is_default DESC, updated_at DESC", workspace_id)),
            "lark_board": row_dict(conn.execute("SELECT * FROM lark_boards WHERE workspace_id = ?", (workspace_id,)).fetchone()),
            "current_workflow": row_dict(current_workflow(conn, workspace_row)),
            "workflows": fetch_all(conn, "SELECT * FROM workflows ORDER BY key"),
            "roles": fetch_all(conn, """
                SELECT roles.*, workflows.key AS workflow_key
                FROM roles
                JOIN workflows ON workflows.id = roles.workflow_id
                ORDER BY workflows.key, roles.role_key
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
    if not parsed["base_token"]:
        raise ValueError("a valid Feishu/Lark Bitable URL is required")

    with connect(paths.db_path) as conn:
        workspace_id = workspace_id_for_root(conn, paths.root)
        board_id = upsert_lark_board(
            conn,
            workspace_id=workspace_id,
            identity_id=default_lark_identity_id(conn, workspace_id),
            base_url=board_url,
            base_token=parsed["base_token"],
            table_id=parsed["table_id"],
            view_id=parsed["view_id"],
        )

    return {
        "ok": True,
        **init_result,
        "lark_board_id": board_id,
        "access_status": "unverified",
    }


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
        workspace_id = workspace_id_for_root(conn, paths.root)
        row = conn.execute(
            "SELECT id FROM lark_identities WHERE workspace_id = ? AND id = ?",
            (workspace_id, identity_id),
        ).fetchone()
        if row is None:
            raise ValueError("lark identity not found")
        conn.execute("UPDATE lark_identities SET is_default = 0 WHERE workspace_id = ?", (workspace_id,))
        conn.execute("UPDATE lark_identities SET is_default = 1, updated_at = ? WHERE id = ?", (now(), identity_id))
    return {"ok": True, "identity_id": identity_id}


def refresh_lark_identity(workspace: str | None, *, identity_id: str, domain: str) -> dict[str, Any]:
    paths = resolve_workspace_paths(workspace)
    with connect(paths.db_path) as conn:
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
        workspace_row = conn.execute("SELECT display_name FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        board_name = name.strip() or f"{(workspace_row['display_name'] if workspace_row and workspace_row['display_name'] else paths.root.name)} 项目看板"
        identity = conn.execute(
            """
            SELECT * FROM lark_identities
            WHERE workspace_id = ? AND auth_mode = 'bot' AND app_id IS NOT NULL AND app_secret IS NOT NULL
            ORDER BY is_default DESC, updated_at DESC
            LIMIT 1
            """,
            (workspace_id,),
        ).fetchone()
        if identity is None:
            raise ValueError("save a bot identity before creating a Bitable")
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
        base = data.get("base") or data
        base_url = base.get("url") or base.get("app_url")
        base_token = base.get("base_token") or base.get("app_token") or base.get("token")
        board_id = upsert_lark_board(
            conn,
            workspace_id=workspace_id,
            identity_id=identity["id"],
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

    return {
        "ok": True,
        **init_result,
        "agent_id": agent_id,
        "workflow_key": workflow_key,
        "role_key": role_key,
        "harness_type": harness,
        "session_id": session,
        "replaced_role": replace_role,
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


def upsert_lark_board(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    identity_id: str | None,
    base_url: str | None,
    base_token: str | None,
    table_id: str | None,
    view_id: str | None,
) -> str:
    existing = conn.execute("SELECT id FROM lark_boards WHERE workspace_id = ?", (workspace_id,)).fetchone()
    timestamp = now()
    if existing:
        conn.execute(
            """
            UPDATE lark_boards
            SET identity_id = COALESCE(?, identity_id),
                base_url = ?, base_token = ?, table_id = ?, view_id = ?,
                access_status = 'unverified', last_verified_at = NULL, last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (identity_id, base_url, base_token, table_id, view_id, timestamp, existing["id"]),
        )
        return existing["id"]

    board_id = f"board_{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO lark_boards
          (id, workspace_id, identity_id, base_url, base_token, table_id, view_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (board_id, workspace_id, identity_id, base_url, base_token, table_id, view_id, timestamp, timestamp),
    )
    return board_id


def has_default_lark_identity(conn: sqlite3.Connection, workspace_id: str) -> bool:
    return conn.execute(
        "SELECT id FROM lark_identities WHERE workspace_id = ? AND auth_mode = 'bot' AND app_id IS NOT NULL AND is_default = 1",
        (workspace_id,),
    ).fetchone() is not None


def ensure_default_lark_identity(conn: sqlite3.Connection, workspace_id: str) -> None:
    if has_default_lark_identity(conn, workspace_id):
        return
    row = conn.execute(
        "SELECT id FROM lark_identities WHERE workspace_id = ? AND auth_mode = 'bot' AND app_id IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
        (workspace_id,),
    ).fetchone()
    if row:
        conn.execute("UPDATE lark_identities SET is_default = 1 WHERE id = ?", (row["id"],))


def default_lark_identity_id(conn: sqlite3.Connection, workspace_id: str) -> str | None:
    row = conn.execute(
        """
        SELECT id FROM lark_identities
        WHERE workspace_id = ? AND auth_mode = 'bot' AND app_id IS NOT NULL
        ORDER BY is_default DESC, updated_at DESC
        LIMIT 1
        """,
        (workspace_id,),
    ).fetchone()
    return row["id"] if row else None


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
            "UPDATE agents SET display_name = COALESCE(?, display_name), status = 'registered', updated_at = ? WHERE id = ?",
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
