from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .git_facts import branch_exists, branch_sha, resolve_commit, worktree_paths
from .workspace_settings import version_control_enabled


DELIVERY_MODES = ("standard", "repository")
DEFAULT_TARGET_BRANCH = "main"
SHA_FIELDS = ("base_sha", "candidate_sha", "verified_sha", "promoted_sha")
RESOURCE_KINDS = ("branches", "worktrees")


def completion_states(definition: dict[str, Any]) -> tuple[str, ...]:
    return tuple(definition["lifecycle"].get("completion_states", ()))


def delivery_constraints(workspace: str | None, definition: dict[str, Any]) -> dict[str, Any]:
    return {
        "version_control_enabled": version_control_enabled(workspace),
        "modes": list(DELIVERY_MODES),
        "target_branch": DEFAULT_TARGET_BRANCH,
        "completion_states": list(completion_states(definition)),
    }


def parse_resources(raw: Any) -> dict[str, list[str]]:
    if not str(raw or "").strip():
        return {kind: [] for kind in RESOURCE_KINDS}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"delivery_resources is not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError("delivery_resources must be a JSON object")
    resources: dict[str, list[str]] = {}
    for kind in RESOURCE_KINDS:
        values = parsed.get(kind, [])
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ValueError(f"delivery_resources.{kind} must be an array of strings")
        resources[kind] = [item.strip() for item in values if item.strip()]
    return resources


def render_resources(resources: dict[str, list[str]]) -> str:
    return json.dumps(
        {kind: resources.get(kind, []) for kind in RESOURCE_KINDS},
        ensure_ascii=False,
        sort_keys=True,
    )


def append_resources(current: Any, added: dict[str, list[str]] | None) -> str:
    """Declared resources are an append-only history; clearing them is not a cleanup."""
    resources = parse_resources(current)
    for kind in RESOURCE_KINDS:
        for item in (added or {}).get(kind, []):
            value = str(item).strip()
            if value and value not in resources[kind]:
                resources[kind].append(value)
    return render_resources(resources)


def mode_of(task: dict[str, Any] | None) -> str:
    return str((task or {}).get("delivery_mode") or "").strip()


def resolve_create_mode(workspace: str | None, requested: str | None) -> str:
    enabled = version_control_enabled(workspace)
    mode = str(requested or "").strip()
    if not mode:
        return "" if enabled else "standard"
    if mode not in DELIVERY_MODES:
        raise ValueError(f"delivery_mode must be one of {', '.join(DELIVERY_MODES)}")
    if mode == "repository" and not enabled:
        raise ValueError(
            "this workspace has version control disabled, so repository delivery is unavailable"
        )
    return mode


def resolve_transition_mode(
    workspace: str | None,
    definition: dict[str, Any],
    task: dict[str, Any],
    requested: str | None,
    *,
    target_state: str | None,
) -> str | None:
    """Return the delivery_mode to write, or None when nothing should change."""
    enabled = version_control_enabled(workspace)
    current = mode_of(task)
    requested = str(requested or "").strip()
    initial_state = definition["lifecycle"]["initial_state"]
    in_initial = str(task.get("status") or "") == initial_state

    if requested:
        if requested not in DELIVERY_MODES:
            raise ValueError(f"delivery_mode must be one of {', '.join(DELIVERY_MODES)}")
        if requested == "repository" and not enabled:
            raise ValueError(
                "this workspace has version control disabled, so repository delivery is unavailable"
            )
        if current and current != requested and not in_initial:
            raise ValueError(
                f"delivery_mode is locked to {current} once the task leaves {initial_state}"
            )
        return requested if requested != current else None

    if current:
        return None
    if not enabled:
        return "standard"
    if target_state and _dispatches_work(definition, target_state):
        raise ValueError(
            "choose a delivery_mode (standard or repository) before this task enters "
            f"{target_state}"
        )
    return None


def _dispatches_work(definition: dict[str, Any], state_key: str) -> bool:
    return any(
        state["key"] == state_key and state["dispatch"] != "none"
        for state in definition["lifecycle"]["states"]
    )


def claim_baseline(workspace: str | None, task: dict[str, Any]) -> dict[str, Any]:
    """Pin the target branch and its exact SHA the first time a repository task is claimed."""
    if mode_of(task) != "repository":
        return {}
    if str(task.get("target_branch") or "").strip() and str(task.get("base_sha") or "").strip():
        return {}
    sha = branch_sha(workspace or ".", DEFAULT_TARGET_BRANCH)
    if not sha:
        raise ValueError(
            f"repository delivery needs an existing {DEFAULT_TARGET_BRANCH} branch in {workspace}"
        )
    return {"target_branch": DEFAULT_TARGET_BRANCH, "base_sha": sha}


def completion_failure(
    workspace: str | None,
    definition: dict[str, Any],
    task: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any] | None:
    """Repository facts that must hold before a task may enter a completion state."""
    merged = {**task, **patch}
    if str(patch.get("status") or "") not in completion_states(definition):
        return None
    if not version_control_enabled(workspace) or mode_of(merged) != "repository":
        return None

    repo = str(Path(workspace or ".").expanduser().resolve())
    branch = str(merged.get("target_branch") or DEFAULT_TARGET_BRANCH).strip()
    failures: list[dict[str, Any]] = []

    resolved = {}
    for field in ("candidate_sha", "verified_sha", "promoted_sha"):
        value = str(merged.get(field) or "").strip()
        if not value:
            failures.append({"check": field, "current": None, "expected": "a recorded commit"})
            continue
        commit = resolve_commit(repo, value)
        if commit is None:
            failures.append({"check": field, "current": value, "expected": "a commit that exists"})
            continue
        resolved[field] = commit

    if len(resolved) == 3 and len(set(resolved.values())) != 1:
        failures.append({
            "check": "verified_candidate",
            "current": dict(resolved),
            "expected": "candidate_sha, verified_sha and promoted_sha resolve to one commit",
        })

    head = branch_sha(repo, branch)
    promoted = resolved.get("promoted_sha")
    if promoted and head != promoted:
        failures.append({
            "check": "target_branch",
            "current": {"branch": branch, "sha": head},
            "expected": {"branch": branch, "sha": promoted},
        })

    try:
        resources = parse_resources(merged.get("delivery_resources"))
    except ValueError as error:
        failures.append({"check": "delivery_resources", "current": str(error), "expected": "valid JSON"})
        resources = {kind: [] for kind in RESOURCE_KINDS}

    leftover_branches = [name for name in resources["branches"] if branch_exists(repo, name)]
    if leftover_branches:
        failures.append({
            "check": "declared_branches",
            "current": leftover_branches,
            "expected": "every declared branch is deleted",
        })

    registered = worktree_paths(repo)
    leftover_worktrees = [
        path
        for path in resources["worktrees"]
        if str(Path(path).expanduser().resolve()) in registered
        or Path(path).expanduser().exists()
    ]
    if leftover_worktrees:
        failures.append({
            "check": "declared_worktrees",
            "current": leftover_worktrees,
            "expected": "every declared worktree is removed",
        })

    if not failures:
        return None
    return {
        "failures": failures,
        "target_branch": branch,
        "leftover_resources": {
            "branches": leftover_branches,
            "worktrees": leftover_worktrees,
        },
    }
