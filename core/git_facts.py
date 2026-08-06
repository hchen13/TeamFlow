from __future__ import annotations

import subprocess
from pathlib import Path


def _run(repo: str | Path, *args: str) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"git is unavailable for {repo}: {error}") from error
    return result.returncode, result.stdout


def is_git_repository(repo: str | Path) -> bool:
    code, output = _run(repo, "rev-parse", "--is-inside-work-tree")
    return code == 0 and output.strip() == "true"


def resolve_commit(repo: str | Path, revision: str) -> str | None:
    """Return the full SHA when `revision` names a real commit in `repo`."""
    if not str(revision or "").strip():
        return None
    code, output = _run(repo, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
    return output.strip() or None if code == 0 else None


def branch_sha(repo: str | Path, branch: str) -> str | None:
    code, output = _run(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return output.strip() or None if code == 0 else None


def branch_exists(repo: str | Path, branch: str) -> bool:
    code, _ = _run(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    return code == 0


def worktree_paths(repo: str | Path) -> set[str]:
    code, output = _run(repo, "worktree", "list", "--porcelain", "-z")
    if code != 0:
        return set()
    return {
        str(Path(entry[len("worktree ") :]).resolve())
        for entry in output.split("\0")
        if entry.startswith("worktree ")
    }
