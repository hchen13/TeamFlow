from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


# SHA-1 and SHA-256 repositories name commits with 40 and 64 hex digits respectively.
OBJECT_ID = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class GitFactError(ValueError):
    """A git probe could not answer, so callers must not assume anything."""


def _run(repo: str | Path, *args: str, allowed: tuple[int, ...] = (0,)) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            # Replacement objects can rewrite history for every read command, so an
            # attacker-supplied refs/replace entry could forge ancestry for this gate.
            env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise GitFactError(f"git could not run in {repo}: {error}") from error
    if result.returncode not in allowed:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise GitFactError(
            f"git {' '.join(args)} failed in {repo}: {detail[0] if detail else result.returncode}"
        )
    return result.returncode, result.stdout


def ensure_repository(repo: str | Path) -> None:
    """Raise unless `repo` is a readable git repository, so later probes mean what they say."""
    _run(repo, "rev-parse", "--git-dir")


def is_object_id(value: str) -> bool:
    return bool(OBJECT_ID.fullmatch(str(value or "").strip()))


def commit_exists(repo: str | Path, object_id: str) -> bool:
    """True only when `object_id` is a full commit id that this repository holds."""
    if not is_object_id(object_id):
        return False
    code, output = _run(repo, "cat-file", "-t", object_id.strip(), allowed=(0, 1, 128))
    return code == 0 and output.strip() == "commit"


def branch_sha(repo: str | Path, branch: str) -> str | None:
    code, output = _run(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", allowed=(0, 1))
    return output.strip() or None if code == 0 else None


def branch_exists(repo: str | Path, branch: str) -> bool:
    code, _ = _run(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", allowed=(0, 1))
    return code == 0


def is_ancestor(repo: str | Path, ancestor: str, descendant: str) -> bool:
    code, _ = _run(repo, "merge-base", "--is-ancestor", ancestor, descendant, allowed=(0, 1))
    return code == 0


def worktree_paths(repo: str | Path) -> set[str]:
    _, output = _run(repo, "worktree", "list", "--porcelain", "-z")
    return {
        str(Path(entry[len("worktree ") :]).resolve())
        for entry in output.split("\0")
        if entry.startswith("worktree ")
    }
