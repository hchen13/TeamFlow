#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import (
    DEFAULT_WORKFLOW_KEY,
    configure_lark_board,
    configure_lark_identity,
    create_lark_board,
    init_workspace,
    inspect_workspace,
    refresh_lark_identity,
    register_agent,
    remove_lark_identity,
    run_lark_cli_json,
    select_workflow,
    set_default_lark_identity,
    unregister_agent,
    verify_lark_user_identity,
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

    refresh_lark_parser = subparsers.add_parser("refresh-lark-identity", help="Refresh a saved Lark identity's app information.")
    add_workspace_args(refresh_lark_parser)
    refresh_lark_parser.add_argument("--identity-id", required=True, help="Lark identity ID.")
    refresh_lark_parser.add_argument("--domain", choices=["feishu", "larksuite"], default="feishu", help="Open platform domain.")
    refresh_lark_parser.set_defaults(func=cmd_refresh_lark_identity)

    remove_lark_parser = subparsers.add_parser("remove-lark-identity", help="Remove a saved Lark identity.")
    add_workspace_args(remove_lark_parser)
    remove_lark_parser.add_argument("--identity-id", required=True, help="Lark identity ID.")
    remove_lark_parser.set_defaults(func=cmd_remove_lark_identity)

    default_lark_parser = subparsers.add_parser("set-default-lark-identity", help="Set the default Lark identity.")
    add_workspace_args(default_lark_parser)
    default_lark_parser.add_argument("--identity-id", required=True, help="Lark identity ID.")
    default_lark_parser.set_defaults(func=cmd_set_default_lark_identity)

    create_board_parser = subparsers.add_parser("create-lark-board", help="Create a Feishu/Lark Bitable with the default identity.")
    add_workspace_args(create_board_parser)
    create_board_parser.add_argument("--domain", choices=["feishu", "larksuite"], default="feishu", help="Open platform domain.")
    create_board_parser.add_argument("--name", default="", help="Bitable file name.")
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


def cmd_configure_lark_identity(args: argparse.Namespace) -> int:
    result = configure_lark_identity(
        args.workspace,
        app_id=args.app_id,
        app_secret=env_value(args.app_secret_env),
        domain=args.domain,
        write_gitignore=args.write_gitignore,
    )
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
    print_json(configure_lark_board(args.workspace, board_url=args.url, write_gitignore=args.write_gitignore))
    return 0


def cmd_refresh_lark_identity(args: argparse.Namespace) -> int:
    result = refresh_lark_identity(args.workspace, identity_id=args.identity_id, domain=args.domain)
    print_json(result)
    return 0 if result["ok"] else 1


def cmd_remove_lark_identity(args: argparse.Namespace) -> int:
    print_json(remove_lark_identity(args.workspace, identity_id=args.identity_id))
    return 0


def cmd_set_default_lark_identity(args: argparse.Namespace) -> int:
    print_json(set_default_lark_identity(args.workspace, identity_id=args.identity_id))
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
        assert [identity["auth_mode"] for identity in expired_state["lark_identities"] if identity["is_default"]] == ["bot"]
        expired_user = next(identity for identity in expired_state["lark_identities"] if identity["auth_mode"] == "user")
        try:
            set_default_lark_identity(workspace, identity_id=expired_user["id"])
        except ValueError as error:
            assert str(error) == "lark identity is unavailable"
        else:
            raise AssertionError("an expired user identity cannot become the default")
        verify_lark_user_identity(workspace, status=user_status, profile=user_profile)
        with patch("core.db.run_lark_cli_json", return_value={"tokenStatus": "expired", "note": "authorization expired"}):
            try:
                create_lark_board(workspace, domain="feishu", name="Expired user board")
            except ValueError as error:
                assert str(error) == "authorization expired"
            else:
                raise AssertionError("Bitable creation should fail when user authorization expires")
        create_expired_state = inspect_workspace(workspace)
        assert [identity["auth_mode"] for identity in create_expired_state["lark_identities"] if identity["is_default"]] == ["bot"]
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
        assert configured_board["identity_id"] == configured_user["id"]
        with patch(
            "core.db.run_lark_cli_json",
            side_effect=[user_status, {"base": {"base_token": "bascnUser", "url": "https://example.feishu.cn/base/bascnUser"}}],
        ) as lark_cli:
            create_lark_board(workspace, domain="feishu", name="")
        assert lark_cli.call_count == 2
        assert lark_cli.call_args_list[1].args[0] == [
            "base",
            "+base-create",
            "--as",
            "user",
            "--name",
            f"{Path(workspace).name}项目看板",
        ]
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
    bot_identity = next(identity for identity in result["lark_identities"] if identity["auth_mode"] == "bot")
    user_identity = next(identity for identity in result["lark_identities"] if identity["auth_mode"] == "user")
    board = result["lark_board"]
    assert result["initialized"] is True
    assert result["schema_version"] == "011_lark_user_avatar_url"
    assert {workflow["key"] for workflow in result["workflows"]} == {DEFAULT_WORKFLOW_KEY, "general-task"}
    assert all(workflow["short_description"] for workflow in result["workflows"])
    assert result["current_workflow"]["key"] == DEFAULT_WORKFLOW_KEY
    assert {role["role_key"] for role in result["roles"]} == {"pm", "qa", "tl", "design", "owner", "executor", "reviewer"}
    assert [role["display_name"] for role in result["roles"] if role["role_key"] == "tl"] == ["Technical Lead"]
    assert all(role["description"] for role in result["roles"])
    assert bot_identity["app_secret"] == "<stored>"
    assert bot_identity["app_name"] == "Check app"
    assert bot_identity["app_avatar_url"] == "https://example.com/avatar.png"
    assert user_identity["user_open_id"] == "ou_check"
    assert user_identity["user_name"] == "Profile user"
    assert user_identity["user_avatar_url"] == "https://example.com/user-avatar.png"
    assert user_identity["access_token"] is None
    assert user_identity["refresh_token"] is None
    assert user_identity["access_status"] == "verified"
    assert user_identity["is_default"] == 1
    assert board["base_token"] == "bascnUser"
    assert board["identity_id"] == user_identity["id"]
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
