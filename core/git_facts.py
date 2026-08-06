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


def object_id_length(repo: str | Path) -> int:
    _, output = _run(repo, "rev-parse", "--show-object-format")
    return 64 if output.strip() == "sha256" else 40


def commit_exists(repo: str | Path, object_id: str) -> bool:
    """True only when `object_id` is a full commit id that this repository holds."""
    value = str(object_id or "").strip()
    # A 40-digit id is an abbreviation in a sha256 repository, and git resolves it
    # happily, so length has to be judged against the repository's own format.
    if not is_object_id(value) or len(value) != object_id_length(repo):
        return False
    code, output = _run(repo, "cat-file", "-t", value, allowed=(0, 1, 128))
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


def fast_forwarded_to(repo: str | Path, branch: str, target: str) -> bool:
    """True when the branch's own history shows it moved onto `target` without discarding work.

    Containment alone cannot tell a fast-forward from a force push that threw away
    whatever the branch already pointed at, so the reflog is the only local evidence.
    """
    code, output = _run(
        repo, "reflog", "show", "--no-abbrev", "--format=%H", f"refs/heads/{branch}", allowed=(0, 128)
    )
    if code != 0:
        return False
    updates = [line.strip() for line in output.splitlines() if line.strip()]
    for index, new in enumerate(updates):
        if new != target or index + 1 >= len(updates):
            continue
        # Reaching the candidate once proves nothing on its own: the branch can be
        # rolled back onto it afterwards. Every update from that entry up to the
        # current tip has to have moved forward for the promotion to still stand.
        chain = updates[: index + 2]
        if all(is_ancestor(repo, chain[step + 1], chain[step]) for step in range(len(chain) - 1)):
            return True
    return False


def worktree_paths(repo: str | Path) -> set[str]:
    _, output = _run(repo, "worktree", "list", "--porcelain", "-z")
    return {
        str(Path(entry[len("worktree ") :]).resolve())
        for entry in output.split("\0")
        if entry.startswith("worktree ")
    }
