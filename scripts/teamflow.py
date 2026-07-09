#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import configure_lark, init_workspace, inspect_workspace


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
    lark_parser.add_argument("--app-secret-env", help="Environment variable containing Lark app secret.")
    lark_parser.add_argument("--access-token-env", help="Environment variable containing user access token.")
    lark_parser.add_argument("--refresh-token-env", help="Environment variable containing user refresh token.")
    lark_parser.add_argument("--write-gitignore", action="store_true", help="Add .teamflow/ to the workspace .gitignore.")
    lark_parser.set_defaults(func=cmd_configure_lark)

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
    print(f"Lark connections: {len(result['lark_connections'])}")
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
        app_secret=env_value(args.app_secret_env),
        access_token=env_value(args.access_token_env),
        refresh_token=env_value(args.refresh_token_env),
        write_gitignore=args.write_gitignore,
    )
    print_json(result)
    if result["missing"]:
        print(f"teamflow: Lark config is stored but incomplete: {', '.join(result['missing'])}", file=sys.stderr)
    return 0


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
