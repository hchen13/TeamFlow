from __future__ import annotations

import re
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


def default_task_prefix(display_name: str | None, workspace: str | Path) -> str:
    name = (display_name or "").strip() or Path(workspace).name
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    words = re.findall(r"[^\W_]+", name)
    if not words:
        raise ValueError("workspace name must contain a letter or number")
    prefix = "".join(word[0] for word in words) if len(words) > 1 else words[0][:3]
    return normalize_task_prefix(prefix[:5])


def normalize_task_prefix(prefix: str) -> str:
    normalized = prefix.strip().upper()
    if not 1 <= len(normalized) <= 5 or not normalized.isalnum():
        raise ValueError("task prefix must contain 1 to 5 letters, numbers, or Chinese characters")
    return normalized


def parse_lark_bitable_url(board_url: str | None) -> dict[str, str | None]:
    if not board_url:
        return {"base_token": None, "wiki_token": None, "table_id": None, "view_id": None}

    parsed = urlparse(board_url)
    parts = [part for part in parsed.path.split("/") if part]
    base_token = None
    wiki_token = None
    if len(parts) >= 2 and parts[0] == "base":
        base_token = parts[1]
    elif len(parts) >= 2 and parts[0] == "wiki":
        wiki_token = parts[1]

    query = parse_qs(parsed.query)
    table_id = first_query_value(query, "table")
    view_id = first_query_value(query, "view")

    return {
        "base_token": base_token,
        "wiki_token": wiki_token,
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
