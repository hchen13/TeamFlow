#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.codex import run_codex_turn
from core.daemon import daemon_status, ensure_daemon, register_workspace, run_daemon, stop_daemon, sync_daemon_workspace
from core.db import (
    DEFAULT_WORKFLOW_KEY,
    SUPPORTED_HARNESS_TYPES,
    configure_lark_board,
    configure_lark_identity,
    create_lark_board,
    init_workspace,
    inspect_workspace,
    list_codex_sessions,
    refresh_lark_identity,
    register_agent,
    remove_lark_identity,
    run_lark_cli_json,
    select_workflow,
    unregister_agent,
    update_agent,
    verify_agents,
    verify_lark_user_identity,
)
from core.lark_board import (
    get_lark_task,
    grant_lark_board_access,
    initialize_lark_board,
    list_lark_tasks,
    upsert_lark_task,
    verify_lark_board,
)
from core.lark_events import listen_lark_board_events, verify_lark_board_listener


CONFIG_PATH = ROOT / "teamflow.config.json"
DEFAULT_UI_HOST = "127.0.0.1"
DEFAULT_UI_PORT = 8766


def main() -> int:
    parser = argparse.ArgumentParser(prog="teamflow", description="TeamFlow local configuration CLI.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize a project-local TeamFlow SQLite database.")
    add_workspace_args(init_parser)
    init_parser.add_argument("--display-name", help="Human-readable workspace name.")
    init_parser.add_argument("--write-gitignore", action="store_true", help="Add .teamflow/ to the workspace .gitignore.")
    init_parser.set_defaults(func=cmd_init)

    inspect_parser = subparsers.add_parser("inspect", help="Inspect local TeamFlow configuration state.")
    add_workspace_args(inspect_parser)
    inspect_parser.add_argument("--json", action="store_true", help="Print raw JSON.")
    inspect_parser.set_defaults(func=cmd_inspect)

    identity_parser = subparsers.add_parser("configure-lark-identity", help="Store a Lark bot identity locally.")
    add_workspace_args(identity_parser)
    identity_parser.add_argument("--app-id", required=True, help="Lark app ID.")
    identity_parser.add_argument("--app-secret-env", required=True, help="Environment variable containing the Lark app secret.")
    identity_parser.add_argument("--domain", choices=["feishu", "larksuite"], default="feishu", help="Open platform domain.")
    identity_parser.add_argument("--write-gitignore", action="store_true", help="Add .teamflow/ to the workspace .gitignore.")
    identity_parser.set_defaults(func=cmd_configure_lark_identity)

    user_identity_parser = subparsers.add_parser("verify-lark-user-identity", help="Verify and save the current Lark user identity.")
    add_workspace_args(user_identity_parser)
    user_identity_parser.set_defaults(func=cmd_verify_lark_user_identity)

    board_parser = subparsers.add_parser("configure-lark-board", help="Store a Feishu/Lark Bitable URL locally.")
    add_workspace_args(board_parser)
    board_parser.add_argument("--url", required=True, help="Full Bitable URL.")
    board_parser.add_argument("--write-gitignore", action="store_true", help="Add .teamflow/ to the workspace .gitignore.")
    board_parser.set_defaults(func=cmd_configure_lark_board)

    verify_board_parser = subparsers.add_parser("verify-lark-board", help="Verify access to the configured Lark Bitable.")
    add_workspace_args(verify_board_parser)
    verify_board_parser.add_argument("--identity-id", help="Verify only one saved Lark identity.")
    verify_board_parser.add_argument("--stream", action="store_true", help="Print progress as NDJSON.")
    verify_board_parser.set_defaults(func=cmd_verify_lark_board)

    grant_board_parser = subparsers.add_parser("grant-lark-board-access", help="Add a saved identity as a Bitable collaborator.")
    add_workspace_args(grant_board_parser)
    grant_board_parser.add_argument("--identity-id", required=True, help="Identity to add as a collaborator.")
    grant_board_parser.set_defaults(func=cmd_grant_lark_board_access)

    initialize_board_parser = subparsers.add_parser("initialize-lark-board", help="Initialize the TeamFlow task table and board view.")
    add_workspace_args(initialize_board_parser)
    initialize_board_parser.add_argument("--task-prefix", help="Task ID prefix, such as TF. Defaults to the workspace name.")
    initialize_board_parser.set_defaults(func=cmd_initialize_lark_board)

    list_tasks_parser = subparsers.add_parser("list-lark-tasks", help="List normalized TeamFlow tasks from the configured Bitable.")
    add_workspace_args(list_tasks_parser)
    list_tasks_parser.add_argument("--limit", type=int, default=100, help="Number of tasks to return, from 1 to 200.")
    list_tasks_parser.add_argument("--offset", type=int, default=0, help="Task offset for pagination.")
    list_tasks_parser.set_defaults(func=cmd_list_lark_tasks)

    get_task_parser = subparsers.add_parser("get-lark-task", help="Get a TeamFlow task by Lark record ID.")
    add_workspace_args(get_task_parser)
    get_task_parser.add_argument("--record-id", required=True, help="Lark record ID.")
    get_task_parser.set_defaults(func=cmd_get_lark_task)

    upsert_task_parser = subparsers.add_parser("upsert-lark-task", help="Create or update a normalized TeamFlow task.")
    add_workspace_args(upsert_task_parser)
    upsert_task_parser.add_argument("--record-id", help="Lark record ID. Omit to create a task.")
    upsert_task_parser.add_argument("--json", required=True, help="Task JSON using stable TeamFlow field and option keys. Task ID is generated by Lark.")
    upsert_task_parser.set_defaults(func=cmd_upsert_lark_task)

    listen_events_parser = subparsers.add_parser("listen-lark-events", help="Subscribe to and stream Bitable changes for the current workspace.")
    add_workspace_args(listen_events_parser)
    listen_events_parser.set_defaults(func=cmd_listen_lark_events)

    verify_listener_parser = subparsers.add_parser("verify-lark-listener", help="Verify Bitable event delivery for the current workspace.")
    add_workspace_args(verify_listener_parser)
    verify_listener_parser.add_argument("--identity-id", help="Explicit owner or manager identity to bind after verification.")
    verify_listener_parser.set_defaults(func=cmd_verify_lark_listener)

    refresh_lark_parser = subparsers.add_parser("refresh-lark-identity", help="Refresh a saved Lark identity's app information.")
    add_workspace_args(refresh_lark_parser)
    refresh_lark_parser.add_argument("--identity-id", required=True, help="Lark identity ID.")
    refresh_lark_parser.add_argument("--domain", choices=["feishu", "larksuite"], default="feishu", help="Open platform domain.")
    refresh_lark_parser.set_defaults(func=cmd_refresh_lark_identity)

    remove_lark_parser = subparsers.add_parser("remove-lark-identity", help="Remove a saved Lark identity.")
    add_workspace_args(remove_lark_parser)
    remove_lark_parser.add_argument("--identity-id", required=True, help="Lark identity ID.")
    remove_lark_parser.set_defaults(func=cmd_remove_lark_identity)

    create_board_parser = subparsers.add_parser("create-lark-board", help="Create a Feishu/Lark Bitable with a saved identity.")
    add_workspace_args(create_board_parser)
    create_board_parser.add_argument("--identity-id", required=True, help="Identity that will own the new Bitable.")
    create_board_parser.add_argument("--domain", choices=["feishu", "larksuite"], default="feishu", help="Open platform domain.")
    create_board_parser.add_argument("--name", default="", help="Bitable file name.")
    create_board_parser.set_defaults(func=cmd_create_lark_board)

    register_parser = subparsers.add_parser("register-agent", help="Register a role-bound agent session locally.")
    add_workspace_args(register_parser)
    register_parser.add_argument("--workflow", help="Workflow key. Defaults to the workspace workflow.")
    register_parser.add_argument("--role", required=True, help="Role key in the workflow, such as pm, qa, tl, or design.")
    register_parser.add_argument("--harness-type", choices=SUPPORTED_HARNESS_TYPES, required=True, help="Agent harness type.")
    register_parser.add_argument("--session-id", required=True, help="Harness session/thread ID.")
    register_parser.add_argument("--display-name", help="Human-readable agent name.")
    register_parser.add_argument("--replace-role", action="store_true", help="Replace all existing agents for this role.")
    register_parser.set_defaults(func=cmd_register_agent)

    unregister_parser = subparsers.add_parser("unregister-agent", help="Remove a registered agent session.")
    add_workspace_args(unregister_parser)
    unregister_parser.add_argument("--agent-id", help="Agent ID returned by register-agent or inspect.")
    unregister_parser.add_argument("--workflow", help="Workflow key used when agent-id is omitted.")
    unregister_parser.add_argument("--role", help="Role key used when agent-id is omitted.")
    unregister_parser.add_argument("--harness-type", help="Harness type used when agent-id is omitted.")
    unregister_parser.add_argument("--session-id", help="Session ID used when agent-id is omitted.")
    unregister_parser.set_defaults(func=cmd_unregister_agent)

    update_agent_parser = subparsers.add_parser("update-agent", help="Assign a different session to a registered agent.")
    add_workspace_args(update_agent_parser)
    update_agent_parser.add_argument("--agent-id", required=True, help="Registered agent ID.")
    update_agent_parser.add_argument("--session-id", required=True, help="Replacement harness session/thread ID.")
    update_agent_parser.set_defaults(func=cmd_update_agent)

    verify_agent_parser = subparsers.add_parser("verify-agent", help="Verify registered Codex agent sessions.")
    add_workspace_args(verify_agent_parser)
    verify_agent_parser.add_argument("--agent-id", help="Agent ID. Omit to verify every registered Codex agent.")
    verify_agent_parser.set_defaults(func=cmd_verify_agent)

    send_agent_parser = subparsers.add_parser("send-agent", help="Send a message to a registered Codex agent and wait for completion.")
    add_workspace_args(send_agent_parser)
    send_agent_parser.add_argument("--agent-id", required=True, help="Registered agent ID.")
    send_agent_parser.add_argument("--message", required=True, help="Message to send to the agent session.")
    send_agent_parser.set_defaults(func=cmd_send_agent)

    sessions_parser = subparsers.add_parser("list-codex-sessions", help="List Codex sessions for the current workspace.")
    add_workspace_args(sessions_parser)
    sessions_parser.set_defaults(func=cmd_list_codex_sessions)

    workflow_parser = subparsers.add_parser("select-workflow", help="Select the workspace workflow.")
    add_workspace_args(workflow_parser)
    workflow_parser.add_argument("--workflow", required=True, help="Workflow key.")
    workflow_parser.set_defaults(func=cmd_select_workflow)

    ui_parser = subparsers.add_parser("serve-ui", help="Start the local TeamFlow configuration UI.")
    add_workspace_args(ui_parser)
    ui_parser.add_argument("--host", help="Bind host. Defaults to teamflow.config.json.")
    ui_parser.add_argument("--port", type=int, help="Bind port. Defaults to teamflow.config.json.")
    ui_parser.set_defaults(func=cmd_serve_ui)

    daemon_parser = subparsers.add_parser("daemon", help="Manage the global TeamFlow daemon.")
    daemon_parser.add_argument("action", choices=("run", "start", "status", "stop", "sync"), help="Daemon action.")
    add_workspace_args(daemon_parser)
    daemon_parser.add_argument("--identity-id", help="Owner or manager identity used when synchronizing a workspace.")
    daemon_parser.set_defaults(func=cmd_daemon)

    check_parser = subparsers.add_parser("self-check", help="Run the local SQLite configuration self-check.")
    check_parser.set_defaults(func=cmd_self_check)

    args = parser.parse_args()
    try:
        return args.func(args)
    except Exception as error:
        print(f"teamflow: {error}", file=sys.stderr)
        return 1


def add_workspace_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", default=".", help="Project root that owns .teamflow/teamflow.db.")


def cmd_init(args: argparse.Namespace) -> int:
    result = init_workspace(args.workspace, display_name=args.display_name, write_gitignore=args.write_gitignore)
    register_workspace(args.workspace)
    print_json(result)
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    result = inspect_workspace(args.workspace)
    if args.json:
        print_json(result)
        return 0

    print(f"Workspace: {result['workspace_root']}")
    print(f"DB: {result['db_path']}")
    print(f"Initialized: {result['initialized']}")
    if not result["initialized"]:
        return 0
    print(f"Schema: {result['schema_version']}")
    current = result.get("current_workflow") or {}
    print(f"Current workflow: {current.get('key') or '-'}")
    print(f"Lark identities: {len(result['lark_identities'])}")
    print(f"Lark board: {'yes' if result.get('lark_board') else 'no'}")
    print(f"Workflows: {len(result['workflows'])}")
    print(f"Roles: {len(result['roles'])}")
    print(f"Agents: {len(result['agents'])}")
    return 0


def cmd_configure_lark_identity(args: argparse.Namespace) -> int:
    result = configure_lark_identity(
        args.workspace,
        app_id=args.app_id,
        app_secret=env_value(args.app_secret_env),
        domain=args.domain,
        write_gitignore=args.write_gitignore,
    )
    register_workspace(args.workspace)
    print_json(result)
    return 0


def cmd_verify_lark_user_identity(args: argparse.Namespace) -> int:
    status = run_lark_cli_json(["auth", "status", "--verify"])
    profile = None
    if status.get("tokenStatus") == "valid":
        try:
            profile = run_lark_cli_json(["contact", "+get-user", "--as", "user"])
        except ValueError:
            pass
    print_json(verify_lark_user_identity(args.workspace, status=status, profile=profile))
    return 0


def cmd_configure_lark_board(args: argparse.Namespace) -> int:
    result = configure_lark_board(args.workspace, board_url=args.url, write_gitignore=args.write_gitignore)
    register_workspace(args.workspace)
    print_json(result)
    return 0


def cmd_verify_lark_board(args: argparse.Namespace) -> int:
    if args.stream:
        verify_lark_board(args.workspace, identity_id=args.identity_id, emit=print_ndjson)
    else:
        print_json(verify_lark_board(args.workspace, identity_id=args.identity_id))
    return 0


def cmd_grant_lark_board_access(args: argparse.Namespace) -> int:
    print_json(grant_lark_board_access(args.workspace, identity_id=args.identity_id))
    return 0


def cmd_initialize_lark_board(args: argparse.Namespace) -> int:
    print_json(initialize_lark_board(args.workspace, task_prefix=args.task_prefix))
    return 0


def cmd_list_lark_tasks(args: argparse.Namespace) -> int:
    print_json(list_lark_tasks(args.workspace, limit=args.limit, offset=args.offset))
    return 0


def cmd_get_lark_task(args: argparse.Namespace) -> int:
    print_json(get_lark_task(args.workspace, record_id=args.record_id))
    return 0


def cmd_upsert_lark_task(args: argparse.Namespace) -> int:
    print_json(upsert_lark_task(args.workspace, task=json_object(args.json), record_id=args.record_id))
    return 0


def cmd_listen_lark_events(args: argparse.Namespace) -> int:
    try:
        listen_lark_board_events(args.workspace, emit=print_ndjson, ready=print_lark_listener_ready)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_verify_lark_listener(args: argparse.Namespace) -> int:
    result = verify_lark_board_listener(args.workspace, identity_id=args.identity_id)
    print_json(result)
    return 0 if result["ok"] else 1


def cmd_refresh_lark_identity(args: argparse.Namespace) -> int:
    result = refresh_lark_identity(args.workspace, identity_id=args.identity_id, domain=args.domain)
    print_json(result)
    return 0 if result["ok"] else 1


def cmd_remove_lark_identity(args: argparse.Namespace) -> int:
    print_json(remove_lark_identity(args.workspace, identity_id=args.identity_id))
    return 0


def cmd_create_lark_board(args: argparse.Namespace) -> int:
    print_json(create_lark_board(args.workspace, identity_id=args.identity_id, domain=args.domain, name=args.name))
    return 0


def cmd_register_agent(args: argparse.Namespace) -> int:
    result = register_agent(
        args.workspace,
        workflow=args.workflow,
        role=args.role,
        harness_type=args.harness_type,
        session_id=args.session_id,
        display_name=args.display_name,
        replace_role=args.replace_role,
    )
    print_json(result)
    return 0


def cmd_unregister_agent(args: argparse.Namespace) -> int:
    result = unregister_agent(
        args.workspace,
        agent_id=args.agent_id,
        workflow=args.workflow,
        role=args.role,
        harness_type=args.harness_type,
        session_id=args.session_id,
    )
    print_json(result)
    return 0


def cmd_update_agent(args: argparse.Namespace) -> int:
    print_json(update_agent(args.workspace, agent_id=args.agent_id, session_id=args.session_id))
    return 0


def cmd_verify_agent(args: argparse.Namespace) -> int:
    print_json(verify_agents(args.workspace, agent_id=args.agent_id))
    return 0


def cmd_send_agent(args: argparse.Namespace) -> int:
    state = inspect_workspace(args.workspace)
    if not state.get("initialized"):
        raise ValueError("TeamFlow workspace is not initialized")
    agent = next((item for item in state["agents"] if item["id"] == args.agent_id), None)
    if agent is None:
        raise ValueError("agent not found")
    if agent["harness_type"] != "codex":
        raise ValueError(f"unsupported harness type: {agent['harness_type']}")
    result = run_codex_turn(agent["session_id"], args.message)
    print_json({
        "agent_id": agent["id"],
        "role_key": agent["role_key"],
        "harness_type": agent["harness_type"],
        "session_id": agent["session_id"],
        **result,
    })
    return 0 if result["ok"] else 1


def cmd_list_codex_sessions(args: argparse.Namespace) -> int:
    print_json(list_codex_sessions(args.workspace))
    return 0


def cmd_select_workflow(args: argparse.Namespace) -> int:
    result = select_workflow(args.workspace, workflow=args.workflow)
    print_json(result)
    return 0


def cmd_serve_ui(args: argparse.Namespace) -> int:
    init_workspace(args.workspace)
    register_workspace(args.workspace)
    ensure_daemon()
    config = load_ui_config()
    host = args.host or config["host"]
    port = args.port or config["port"]
    ui_dir = ROOT / "ui"
    ensure_ui_dependencies(ui_dir)
    env = os.environ.copy()
    env["TEAMFLOW_CLI"] = str(ROOT / "scripts" / "teamflow.py")
    env["TEAMFLOW_WORKSPACE"] = str(Path(args.workspace).expanduser().resolve())
    print(f"TeamFlow UI: http://{host}:{port}/")
    return subprocess.call(
        ["npm", "run", "dev", "--", "--hostname", host, "--port", str(port)],
        cwd=ui_dir,
        env=env,
    )


def cmd_daemon(args: argparse.Namespace) -> int:
    if args.action == "run":
        return run_daemon()
    if args.action == "start":
        print_json(ensure_daemon())
        return 0
    if args.action == "status":
        status = daemon_status()
        print_json(status)
        return 0 if status["running"] else 1
    if args.action == "stop":
        print_json(stop_daemon())
        return 0
    print_json(sync_daemon_workspace(args.workspace, identity_id=args.identity_id))
    return 0


def cmd_self_check(args: argparse.Namespace) -> int:
    checks_dir = ROOT / ".teamflow" / "checks"
    checks_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="workspace-", dir=checks_dir) as workspace:
        init_workspace(workspace, display_name="TeamFlow check")
        with patch("core.db.fetch_lark_app_info", return_value=("Check app", "https://example.com/avatar.png", None)):
            configure_lark_identity(
                workspace,
                app_id="cli_check",
                app_secret="dummy",
                domain="feishu",
            )
        user_status = {
            "appId": "cli_check",
            "identity": "user",
            "tokenStatus": "valid",
            "userName": "Check user",
            "userOpenId": "ou_check",
        }
        user_profile = {
            "data": {
                "user": {
                    "avatar_middle": "https://example.com/user-avatar.png",
                    "name": "Profile user",
                    "open_id": "ou_check",
                }
            }
        }
        verify_lark_user_identity(workspace, status=user_status, profile=user_profile)
        try:
            verify_lark_user_identity(workspace, status={"tokenStatus": "expired", "note": "authorization expired"})
        except ValueError as error:
            assert str(error) == "authorization expired"
        else:
            raise AssertionError("expired user authorization should fail")
        expired_state = inspect_workspace(workspace)
        assert [identity["access_status"] for identity in expired_state["lark_identities"] if identity["auth_mode"] == "user"] == ["expired"]
        expired_user = next(identity for identity in expired_state["lark_identities"] if identity["auth_mode"] == "user")
        verify_lark_user_identity(workspace, status=user_status, profile=user_profile)
        with patch("core.db.run_lark_cli_json", return_value={"tokenStatus": "expired", "note": "authorization expired"}):
            try:
                create_lark_board(workspace, identity_id=expired_user["id"], domain="feishu", name="Expired user board")
            except ValueError as error:
                assert str(error) == "authorization expired"
            else:
                raise AssertionError("Bitable creation should fail when user authorization expires")
        create_expired_state = inspect_workspace(workspace)
        assert next(identity for identity in create_expired_state["lark_identities"] if identity["auth_mode"] == "user")["access_status"] == "expired"
        verify_lark_user_identity(workspace, status=user_status, profile=user_profile)
        mismatched_status = {**user_status, "appId": "cli_other"}
        with patch("core.db.run_lark_cli_json", return_value=mismatched_status):
            try:
                create_lark_board(workspace, identity_id=expired_user["id"], domain="feishu", name="Wrong app board")
            except ValueError as error:
                assert "this identity uses cli_check" in str(error)
            else:
                raise AssertionError("Bitable creation should reject a different authorized app")
        verify_lark_user_identity(workspace, status=user_status, profile=user_profile)
        configure_lark_board(
            workspace,
            board_url="https://example.feishu.cn/base/bascnCheck?table=tblCheck&view=vewCheck",
        )
        configured_state = inspect_workspace(workspace)
        configured_board = configured_state["lark_board"]
        configured_user = next(identity for identity in configured_state["lark_identities"] if identity["auth_mode"] == "user")
        assert configured_board["base_token"] == "bascnCheck"
        assert configured_board["table_id"] == "tblCheck"
        assert configured_board["view_id"] == "vewCheck"
        assert configured_board["primary_identity_id"] is None
        with patch("core.db.resolve_lark_wiki_bitable", return_value="bascnCheck"):
            configure_lark_board(workspace, board_url="https://example.feishu.cn/wiki/wikcnCheck")
        with patch("core.db.resolve_lark_wiki_bitable", side_effect=ValueError("API error: [131005] not found")):
            try:
                configure_lark_board(workspace, board_url="https://example.feishu.cn/wiki/wikcnCheck?from=from_copylink")
            except ValueError:
                pass
            else:
                raise AssertionError("a missing configured Bitable should fail")
        unavailable_board = inspect_workspace(workspace)["lark_board"]
        assert unavailable_board["access_status"] == "unavailable"
        assert unavailable_board["last_verified_at"] is None
        with patch(
            "core.db.run_lark_cli_json",
            side_effect=[user_status, {"base": {"base_token": "bascnUser", "url": "https://example.feishu.cn/base/bascnUser"}}],
        ) as lark_cli:
            create_lark_board(workspace, identity_id=configured_user["id"], domain="feishu", name="")
        assert lark_cli.call_count == 2
        assert lark_cli.call_args_list[1].args[0] == [
            "base",
            "+base-create",
            "--as",
            "user",
            "--name",
            f"{Path(workspace).name}项目看板",
        ]
        try:
            register_agent(workspace, role="qa", harness_type="claude-code", session_id="session_qa_unsupported")
        except ValueError as error:
            assert "unsupported harness type" in str(error)
        else:
            raise AssertionError("unsupported harness registration should fail")

        archived_thread_ids: set[str] = set()
        error_thread_ids: set[str] = set()

        def read_check_thread(thread_id: str, *, include_turns: bool = False) -> dict[str, object]:
            if thread_id == "thread_design_1":
                raise ValueError(f"thread not loaded: {thread_id}")
            thread: dict[str, object] = {
                "id": thread_id,
                "name": f"Session {thread_id}",
                "status": {"type": "systemError" if thread_id in error_thread_ids else "notLoaded"},
                "cwd": workspace,
            }
            if include_turns and thread_id in error_thread_ids:
                thread["turns"] = [{"error": {"message": "context window exceeded"}}]
            return thread

        def list_check_threads(cwd: str, *, archived: bool = False) -> list[dict[str, object]]:
            assert cwd == workspace
            thread_ids = ["thread_pm", "thread_pm_2", "session_qa_1", "session_qa_2", "session_qa_3", "session_tl_1"]
            selected = [thread_id for thread_id in thread_ids if (thread_id in archived_thread_ids) is archived]
            return [read_check_thread(thread_id) for thread_id in selected]

        with patch("core.db.read_codex_thread", side_effect=read_check_thread), patch("core.db.list_codex_threads", side_effect=list_check_threads):
            register_agent(workspace, role="pm", harness_type="codex", session_id="thread_pm")
            try:
                register_agent(workspace, role="pm", harness_type="codex", session_id="thread_pm_2")
            except ValueError as error:
                assert "allows only one agent" in str(error)
            else:
                raise AssertionError("second pm registration should fail")
            pm_agent = register_agent(workspace, role="pm", harness_type="codex", session_id="thread_pm_2", replace_role=True)
            removed = register_agent(workspace, role="qa", harness_type="codex", session_id="session_qa_1")
            qa_agent = register_agent(workspace, role="qa", harness_type="codex", session_id="session_qa_2")
            register_agent(workspace, role="tl", harness_type="codex", session_id="session_tl_1")
            register_agent(workspace, role="design", harness_type="codex", session_id="thread_design_1")
            unregister_agent(workspace, agent_id=removed["agent_id"])
            updated = update_agent(workspace, agent_id=qa_agent["agent_id"], session_id="session_qa_3")
            assert updated["health"]["status"] == "healthy"
            assert updated["health"]["session_name"] == "Session session_qa_3"
            assignment_updates = {agent["id"]: agent["updated_at"] for agent in inspect_workspace(workspace)["agents"]}
            health = verify_agents(workspace)
            assert pm_agent["health"]["ok"] is True
            assert health["checked"] == 4
            assert health["ok"] is False
            assert {item["status"] for item in health["results"]} == {"healthy", "deleted"}
            assert next(item for item in health["results"] if item["agent_id"] == pm_agent["agent_id"])["runtime_status"] == "notLoaded"
            assert next(item for item in health["results"] if item["agent_id"] == pm_agent["agent_id"])["thread_cwd"] == workspace
            archived_thread_ids.add("thread_pm_2")
            archived_health = verify_agents(workspace, agent_id=pm_agent["agent_id"])
            assert archived_health["results"][0]["status"] == "archived"
            assert archived_health["results"][0]["thread_archived"] is True
            archived_thread_ids.clear()
            restored_health = verify_agents(workspace, agent_id=pm_agent["agent_id"])
            assert restored_health["results"][0]["status"] == "healthy"
            assert restored_health["results"][0]["thread_archived"] is False
            error_thread_ids.add("thread_pm_2")
            error_health = verify_agents(workspace, agent_id=pm_agent["agent_id"])
            assert error_health["results"][0]["status"] == "system_error"
            assert error_health["results"][0]["error"] == "context window exceeded"
            error_thread_ids.clear()
            verify_agents(workspace, agent_id=pm_agent["agent_id"])
        with patch("core.db.list_codex_threads", side_effect=ValueError("failed to start Codex app-server")):
            unavailable_health = verify_agents(workspace, agent_id=pm_agent["agent_id"])
        assert unavailable_health["results"][0]["status"] == "unavailable"
        with patch("core.db.read_codex_thread", side_effect=read_check_thread), patch("core.db.list_codex_threads", side_effect=list_check_threads):
            verify_agents(workspace, agent_id=pm_agent["agent_id"])
        with patch("core.db.list_codex_threads", return_value=[{
            "id": "thread_pm_2",
            "name": "PM session",
            "status": {"type": "notLoaded"},
            "updatedAt": 123,
        }]):
            sessions = list_codex_sessions(workspace)
        assert sessions["sessions"] == [{
            "session_id": "thread_pm_2",
            "name": "PM session",
            "status": "notLoaded",
            "updated_at": 123,
        }]
        result = inspect_workspace(workspace)

    agents = result["agents"]
    bot_identity = next(identity for identity in result["lark_identities"] if identity["auth_mode"] == "bot")
    user_identity = next(identity for identity in result["lark_identities"] if identity["auth_mode"] == "user")
    board = result["lark_board"]
    assert result["initialized"] is True
    assert result["schema_version"] == "016_lark_board_listener"
    assert {workflow["key"] for workflow in result["workflows"]} == {DEFAULT_WORKFLOW_KEY, "general-task"}
    assert all(workflow["short_description"] for workflow in result["workflows"])
    assert result["current_workflow"]["key"] == DEFAULT_WORKFLOW_KEY
    assert {role["role_key"] for role in result["roles"]} == {"pm", "qa", "tl", "design", "owner", "executor", "reviewer"}
    assert [role["display_name"] for role in result["roles"] if role["role_key"] == "tl"] == ["Technical Lead"]
    assert all(role["description"] for role in result["roles"])
    assert [role["role_key"] for role in result["roles"] if role["is_coordinator"]] == ["pm"]
    assert {task_type["type_key"] for task_type in result["task_types"]} == {
        "requirement", "decision", "design", "development", "bug", "validation", "chore",
    }
    assert bot_identity["app_secret"] == "<stored>"
    assert bot_identity["app_name"] == "Check app"
    assert bot_identity["app_avatar_url"] == "https://example.com/avatar.png"
    assert user_identity["user_open_id"] == "ou_check"
    assert user_identity["user_name"] == "Profile user"
    assert user_identity["user_avatar_url"] == "https://example.com/user-avatar.png"
    assert user_identity["access_token"] is None
    assert user_identity["refresh_token"] is None
    assert user_identity["access_status"] == "verified"
    assert "is_default" not in user_identity
    assert board["base_token"] == "bascnUser"
    assert board["primary_identity_id"] == user_identity["id"]
    assert len([agent for agent in agents if agent["role_key"] == "qa"]) == 1
    assert len([agent for agent in agents if agent["role_key"] == "pm"]) == 1
    assert len([agent for agent in agents if agent["role_key"] == "tl"]) == 1
    assert len([agent for agent in agents if agent["role_key"] == "design"]) == 1
    assert all({"status", "session_name", "last_verified_at", "last_error"}.isdisjoint(agent) for agent in agents)
    assert {agent["id"]: agent["updated_at"] for agent in agents} == assignment_updates
    assert next(agent for agent in agents if agent["role_key"] == "qa")["session_id"] == "session_qa_3"
    assert isinstance(load_ui_config()["port"], int)
    assert load_ui_config()["port"] > 0
    with tempfile.TemporaryDirectory(prefix="ui-", dir=checks_dir) as ui_workspace:
        ui_dir = Path(ui_workspace)
        with patch("subprocess.run") as npm_run:
            ensure_ui_dependencies(ui_dir)
        npm_run.assert_called_once_with(["npm", "ci"], cwd=ui_dir, check=True)
        (ui_dir / "node_modules" / "next").mkdir(parents=True)
        with patch("subprocess.run") as npm_run:
            ensure_ui_dependencies(ui_dir)
        npm_run.assert_not_called()
    print("OK: TeamFlow local config self-check passed")
    return 0


def ensure_ui_dependencies(ui_dir: Path) -> None:
    if (ui_dir / "node_modules" / "next").exists():
        return
    try:
        subprocess.run(["npm", "ci"], cwd=ui_dir, check=True)
    except FileNotFoundError as error:
        raise ValueError("npm is required to start the TeamFlow UI") from error


def load_ui_config() -> dict[str, int | str]:
    config = {"host": DEFAULT_UI_HOST, "port": DEFAULT_UI_PORT}
    if not CONFIG_PATH.exists():
        return config
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    ui = raw.get("ui", {})
    config["host"] = ui.get("host") or config["host"]
    config["port"] = int(ui.get("port") or config["port"])
    return config


def env_value(name: str | None) -> str | None:
    if not name:
        return None
    value = os.environ.get(name)
    if value is None:
        raise ValueError(f"environment variable is not set: {name}")
    return value


def print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def print_ndjson(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def print_lark_listener_ready(details: dict[str, Any]) -> None:
    board = details["board"]
    identity = details["identity"]
    app = details["app"]
    identity_type = "用户身份" if identity["auth_mode"] == "user" else "应用身份"
    print("TeamFlow 飞书事件监听已就绪", file=sys.stderr)
    print(f"  工作区: {details['workspace_root']}", file=sys.stderr)
    print(f"  多维表格: {board['name']} ({board['file_token']})", file=sys.stderr)
    print(f"  数据表: {board['table_name']} ({board['table_id']})", file=sys.stderr)
    print(f"  链接: {board['url']}", file=sys.stderr)
    print(f"  访问身份: {identity['name']} ({identity_type}, {identity['id']})", file=sys.stderr)
    print(f"  长连接应用: {app['name']} ({app['id']})", file=sys.stderr)
    print("  状态: 正在连接并等待事件，按 Ctrl-C 停止", file=sys.stderr, flush=True)


def json_object(value: str) -> dict[str, object]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("--json must be valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("--json must be a JSON object")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
