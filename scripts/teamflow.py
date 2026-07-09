#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import (
    DEFAULT_WORKFLOW_KEY,
    configure_lark,
    create_lark_board,
    init_workspace,
    inspect_workspace,
    refresh_lark_app_name,
    register_agent,
    remove_lark_connection,
    select_workflow,
    set_default_lark_connection,
    unregister_agent,
)


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

    lark_parser = subparsers.add_parser("configure-lark", help="Store Lark board and credential settings locally.")
    add_workspace_args(lark_parser)
    lark_parser.add_argument("--label", default="default", help="Connection label.")
    lark_parser.add_argument("--base-url", help="Lark Base URL.")
    lark_parser.add_argument("--base-token", help="Lark Base app_token/base_token.")
    lark_parser.add_argument("--table-id", help="Lark Base table ID.")
    lark_parser.add_argument("--view-id", help="Lark Base view ID.")
    lark_parser.add_argument("--auth-mode", choices=["bot", "user"], default="bot", help="Lark access identity.")
    lark_parser.add_argument("--app-id", help="Lark app ID.")
    lark_parser.add_argument("--app-name", help="Lark app name.")
    lark_parser.add_argument("--app-avatar-url", help="Lark app avatar URL.")
    lark_parser.add_argument("--app-secret-env", help="Environment variable containing Lark app secret.")
    lark_parser.add_argument("--access-token-env", help="Environment variable containing user access token.")
    lark_parser.add_argument("--refresh-token-env", help="Environment variable containing user refresh token.")
    lark_parser.add_argument("--write-gitignore", action="store_true", help="Add .teamflow/ to the workspace .gitignore.")
    lark_parser.set_defaults(func=cmd_configure_lark)

    refresh_lark_parser = subparsers.add_parser("refresh-lark-app-name", help="Refresh a saved Lark app name.")
    add_workspace_args(refresh_lark_parser)
    refresh_lark_parser.add_argument("--connection-id", required=True, help="Lark connection ID.")
    refresh_lark_parser.add_argument("--domain", choices=["feishu", "larksuite"], default="feishu", help="Open platform domain.")
    refresh_lark_parser.set_defaults(func=cmd_refresh_lark_app_name)

    remove_lark_parser = subparsers.add_parser("remove-lark-connection", help="Remove a saved Lark connection.")
    add_workspace_args(remove_lark_parser)
    remove_lark_parser.add_argument("--connection-id", required=True, help="Lark connection ID.")
    remove_lark_parser.set_defaults(func=cmd_remove_lark_connection)

    default_lark_parser = subparsers.add_parser("set-default-lark-connection", help="Set the default Lark identity.")
    add_workspace_args(default_lark_parser)
    default_lark_parser.add_argument("--connection-id", required=True, help="Lark connection ID.")
    default_lark_parser.set_defaults(func=cmd_set_default_lark_connection)

    create_board_parser = subparsers.add_parser("create-lark-board", help="Create a Lark Base with the default bot identity.")
    add_workspace_args(create_board_parser)
    create_board_parser.add_argument("--domain", choices=["feishu", "larksuite"], default="feishu", help="Open platform domain.")
    create_board_parser.add_argument("--name", default="", help="Base name.")
    create_board_parser.set_defaults(func=cmd_create_lark_board)

    register_parser = subparsers.add_parser("register-agent", help="Register a role-bound agent session locally.")
    add_workspace_args(register_parser)
    register_parser.add_argument("--workflow", help="Workflow key. Defaults to the workspace workflow.")
    register_parser.add_argument("--role", required=True, help="Role key in the workflow, such as pm, qa, tl, or design.")
    register_parser.add_argument("--harness-type", required=True, help="Harness type, such as codex, claude-code, opencode.")
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

    workflow_parser = subparsers.add_parser("select-workflow", help="Select the workspace workflow.")
    add_workspace_args(workflow_parser)
    workflow_parser.add_argument("--workflow", required=True, help="Workflow key.")
    workflow_parser.set_defaults(func=cmd_select_workflow)

    ui_parser = subparsers.add_parser("serve-ui", help="Start the local TeamFlow configuration UI.")
    add_workspace_args(ui_parser)
    ui_parser.add_argument("--host", help="Bind host. Defaults to teamflow.config.json.")
    ui_parser.add_argument("--port", type=int, help="Bind port. Defaults to teamflow.config.json.")
    ui_parser.set_defaults(func=cmd_serve_ui)

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


def cmd_configure_lark(args: argparse.Namespace) -> int:
    result = configure_lark(
        args.workspace,
        label=args.label,
        base_url=args.base_url,
        base_token=args.base_token,
        table_id=args.table_id,
        view_id=args.view_id,
        auth_mode=args.auth_mode,
        app_id=args.app_id,
        app_name=args.app_name,
        app_avatar_url=args.app_avatar_url,
        app_secret=env_value(args.app_secret_env),
        access_token=env_value(args.access_token_env),
        refresh_token=env_value(args.refresh_token_env),
        write_gitignore=args.write_gitignore,
    )
    print_json(result)
    if result["missing"]:
        print(f"teamflow: Lark config is stored but incomplete: {', '.join(result['missing'])}", file=sys.stderr)
    return 0


def cmd_refresh_lark_app_name(args: argparse.Namespace) -> int:
    result = refresh_lark_app_name(args.workspace, connection_id=args.connection_id, domain=args.domain)
    print_json(result)
    return 0 if result["ok"] else 1


def cmd_remove_lark_connection(args: argparse.Namespace) -> int:
    print_json(remove_lark_connection(args.workspace, connection_id=args.connection_id))
    return 0


def cmd_set_default_lark_connection(args: argparse.Namespace) -> int:
    print_json(set_default_lark_connection(args.workspace, connection_id=args.connection_id))
    return 0


def cmd_create_lark_board(args: argparse.Namespace) -> int:
    print_json(create_lark_board(args.workspace, domain=args.domain, name=args.name))
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


def cmd_select_workflow(args: argparse.Namespace) -> int:
    result = select_workflow(args.workspace, workflow=args.workflow)
    print_json(result)
    return 0


def cmd_serve_ui(args: argparse.Namespace) -> int:
    init_workspace(args.workspace)
    config = load_ui_config()
    host = args.host or config["host"]
    port = args.port or config["port"]
    env = os.environ.copy()
    env["TEAMFLOW_CLI"] = str(ROOT / "scripts" / "teamflow.py")
    env["TEAMFLOW_WORKSPACE"] = str(Path(args.workspace).expanduser().resolve())
    print(f"TeamFlow UI: http://{host}:{port}/")
    return subprocess.call(
        ["npm", "run", "dev", "--", "--hostname", host, "--port", str(port)],
        cwd=ROOT / "ui",
        env=env,
    )


def cmd_self_check(args: argparse.Namespace) -> int:
    checks_dir = ROOT / ".teamflow" / "checks"
    checks_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="workspace-", dir=checks_dir) as workspace:
        init_workspace(workspace, display_name="TeamFlow check")
        configure_lark(
            workspace,
            label="bot:cli_check",
            base_url=None,
            base_token=None,
            table_id=None,
            view_id=None,
            auth_mode="bot",
            app_id="cli_check",
            app_name="Check app",
            app_avatar_url="https://example.com/avatar.png",
            app_secret="dummy",
            access_token=None,
            refresh_token=None,
        )
        configure_lark(
            workspace,
            label="board",
            base_url="https://example.feishu.cn/base/bascnCheck?table=tblCheck&view=vewCheck",
            base_token=None,
            table_id=None,
            view_id=None,
            auth_mode="user",
            app_id=None,
            app_name=None,
            app_avatar_url=None,
            app_secret=None,
            access_token=None,
            refresh_token=None,
        )
        register_agent(workspace, role="pm", harness_type="codex", session_id="thread_pm")
        try:
            register_agent(workspace, role="pm", harness_type="codex", session_id="thread_pm_2")
        except ValueError as error:
            assert "allows only one agent" in str(error)
        else:
            raise AssertionError("second pm registration should fail")
        register_agent(workspace, role="pm", harness_type="codex", session_id="thread_pm_2", replace_role=True)
        removed = register_agent(workspace, role="qa", harness_type="claude-code", session_id="session_qa_1")
        register_agent(workspace, role="qa", harness_type="claude-code", session_id="session_qa_2")
        register_agent(workspace, role="tl", harness_type="opencode", session_id="session_tl_1")
        register_agent(workspace, role="design", harness_type="codex", session_id="thread_design_1")
        unregister_agent(workspace, agent_id=removed["agent_id"])
        result = inspect_workspace(workspace)

    agents = result["agents"]
    identity = result["lark_identities"][0]
    board = result["lark_board"]
    assert result["initialized"] is True
    assert result["schema_version"] == "009_lark_app_avatar_url"
    assert {workflow["key"] for workflow in result["workflows"]} == {DEFAULT_WORKFLOW_KEY, "general-task"}
    assert all(workflow["short_description"] for workflow in result["workflows"])
    assert result["current_workflow"]["key"] == DEFAULT_WORKFLOW_KEY
    assert {role["role_key"] for role in result["roles"]} == {"pm", "qa", "tl", "design", "owner", "executor", "reviewer"}
    assert [role["display_name"] for role in result["roles"] if role["role_key"] == "tl"] == ["Technical Lead"]
    assert all(role["description"] for role in result["roles"])
    assert identity["app_secret"] == "<stored>"
    assert identity["app_name"] == "Check app"
    assert identity["app_avatar_url"] == "https://example.com/avatar.png"
    assert board["base_token"] == "bascnCheck"
    assert len([agent for agent in agents if agent["role_key"] == "qa"]) == 1
    assert len([agent for agent in agents if agent["role_key"] == "pm"]) == 1
    assert len([agent for agent in agents if agent["role_key"] == "tl"]) == 1
    assert len([agent for agent in agents if agent["role_key"] == "design"]) == 1
    assert isinstance(load_ui_config()["port"], int)
    assert load_ui_config()["port"] > 0
    print("OK: TeamFlow local config self-check passed")
    return 0


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


if __name__ == "__main__":
    raise SystemExit(main())
