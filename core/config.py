from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse


TEAMFLOW_DIR = ".teamflow"
DB_NAME = "teamflow.db"


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path
    state_dir: Path
    db_path: Path


def resolve_workspace_paths(workspace: str | None) -> WorkspacePaths:
    root = Path(workspace or ".").expanduser().resolve()
    state_dir = root / TEAMFLOW_DIR
    return WorkspacePaths(root=root, state_dir=state_dir, db_path=state_dir / DB_NAME)


def parse_lark_base_url(base_url: str | None) -> dict[str, str | None]:
    if not base_url:
        return {"base_token": None, "table_id": None, "view_id": None}

    parsed = urlparse(base_url)
    parts = [part for part in parsed.path.split("/") if part]
    base_token = None
    if len(parts) >= 2 and parts[0] == "base":
        base_token = parts[1]

    query = parse_qs(parsed.query)
    table_id = first_query_value(query, "table")
    view_id = first_query_value(query, "view")

    return {
        "base_token": base_token,
        "table_id": table_id,
        "view_id": view_id,
    }


def first_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    value = values[0].strip() if values else ""
    return value or None


def ensure_workspace_gitignore(paths: WorkspacePaths) -> bool:
    gitignore_path = paths.root / ".gitignore"
    entry = f"{TEAMFLOW_DIR}/"

    if gitignore_path.exists():
        text = gitignore_path.read_text(encoding="utf-8")
        lines = [line.strip() for line in text.splitlines()]
        if entry in lines or TEAMFLOW_DIR in lines:
            return False
        suffix = "" if text.endswith("\n") or not text else "\n"
        gitignore_path.write_text(f"{text}{suffix}{entry}\n", encoding="utf-8")
        return True

    gitignore_path.write_text(f"{entry}\n", encoding="utf-8")
    return True

