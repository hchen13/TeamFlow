from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .git_facts import (
    GitFactError,
    ensure_repository,
    branch_exists,
    branch_sha,
    commit_exists,
    is_ancestor,
    is_object_id,
    worktree_paths,
)
from .workspace_settings import version_control_enabled


DELIVERY_MODES = ("standard", "repository")
DEFAULT_TARGET_BRANCH = "main"
AGENT_SHA_FIELDS = ("candidate_sha", "verified_sha", "promoted_sha")
SYSTEM_FIELDS = ("target_branch", "base_sha")
RESOURCE_KINDS = ("branches", "worktrees")


def completion_states(definition: dict[str, Any]) -> tuple[str, ...]:
    return tuple(definition["lifecycle"].get("completion_states", ()))


def delivery_constraints(workspace: str | None, definition: dict[str, Any]) -> dict[str, Any]:
    return {
        "version_control_enabled": version_control_enabled(workspace),
        "modes": list(DELIVERY_MODES),
        "target_branch": DEFAULT_TARGET_BRANCH,
        "completion_states": list(completion_states(definition)),
        "submittable_fields": ["delivery_mode", *AGENT_SHA_FIELDS, "resources"],
        "system_fields": list(SYSTEM_FIELDS),
        "resource_kinds": list(RESOURCE_KINDS),
        "sha_format": "full 40-character commit object id",
    }


def normalize_delivery_input(delivery: Any, *, workspace: str | None) -> dict[str, Any]:
    """Reject anything an agent must not set, and anything git cannot trust."""
    if delivery is None:
        return {}
    if not isinstance(delivery, dict):
        raise ValueError("delivery must be an object")
    unknown = set(delivery) - {"delivery_mode", *AGENT_SHA_FIELDS, "resources"}
    if unknown:
        overridden = sorted(unknown & set(SYSTEM_FIELDS))
        if overridden:
            raise ValueError(
                f"{', '.join(overridden)} is set by TeamFlow and cannot be supplied: "
                f"the target branch is always {DEFAULT_TARGET_BRANCH} and the baseline is "
                "pinned at the first claim"
            )
        raise ValueError(f"unknown delivery fields: {', '.join(sorted(unknown))}")
    normalized: dict[str, Any] = {}
    if "delivery_mode" in delivery:
        normalized["delivery_mode"] = delivery["delivery_mode"]
    for field in AGENT_SHA_FIELDS:
        if field not in delivery:
            continue
        value = str(delivery[field] or "").strip()
        if not value:
            continue
        if not is_object_id(value):
            raise ValueError(
                f"{field} must be a full 40-character commit id; "
                f"branches, tags, HEAD, abbreviated ids and rev expressions are rejected"
            )
        normalized[field] = value
    if "resources" in delivery:
        normalized["resources"] = normalize_resource_input(delivery["resources"], workspace=workspace)
    return normalized


def normalize_resource_input(resources: Any, *, workspace: str | None) -> dict[str, list[str]]:
    if not isinstance(resources, dict):
        raise ValueError("delivery.resources must be an object")
    unknown = set(resources) - set(RESOURCE_KINDS)
    if unknown:
        raise ValueError(
            f"delivery.resources only accepts {', '.join(RESOURCE_KINDS)}; "
            f"got {', '.join(sorted(unknown))}"
        )
    root = Path(workspace or ".").expanduser().resolve()
    normalized: dict[str, list[str]] = {}
    for kind in RESOURCE_KINDS:
        values = resources.get(kind, [])
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ValueError(f"delivery.resources.{kind} must be an array of strings")
        cleaned = []
        for item in values:
            value = item.strip()
            if not value:
                continue
            if kind == "worktrees":
                value = str((root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve())
            cleaned.append(value)
        normalized[kind] = cleaned
    return normalized


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
        if requested == "repository" and not enabled and current != "repository":
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
    if target_state and (
        _dispatches_work(definition, target_state)
        or target_state in completion_states(definition)
    ):
        raise ValueError(
            "choose a delivery_mode (standard or repository) before this task enters "
            f"{target_state}; PM can record it with a delivery-only update_task"
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
    """Immutable Git facts that must hold before a task may enter a completion state."""
    merged = {**task, **patch}
    if str(patch.get("status") or "") not in completion_states(definition):
        return None
    # A task already locked to repository delivery keeps its gate even if the
    # workspace switch is turned off afterwards.
    if mode_of(merged) != "repository":
        return None

    repo = str(Path(workspace or ".").expanduser().resolve())
    branch = DEFAULT_TARGET_BRANCH
    failures: list[dict[str, Any]] = []
    leftover_branches: list[str] = []
    leftover_worktrees: list[str] = []

    try:
        ensure_repository(repo)
        resolved: dict[str, str] = {}
        for field in AGENT_SHA_FIELDS:
            value = str(merged.get(field) or "").strip()
            if not value:
                failures.append({"check": field, "current": None, "expected": "a recorded commit id"})
            elif not commit_exists(repo, value):
                failures.append({
                    "check": field,
                    "current": value,
                    "expected": "a full commit id that exists in this repository",
                })
            else:
                resolved[field] = value

        if len(resolved) == len(AGENT_SHA_FIELDS) and len(set(resolved.values())) != 1:
            failures.append({
                "check": "verified_candidate",
                "current": dict(resolved),
                "expected": "candidate_sha, verified_sha and promoted_sha are the same commit",
            })

        candidate = resolved.get("candidate_sha")
        base = str(merged.get("base_sha") or "").strip()
        if candidate and base:
            if not commit_exists(repo, base):
                failures.append({"check": "base_sha", "current": base, "expected": "a commit that exists"})
            elif not is_ancestor(repo, base, candidate):
                failures.append({
                    "check": "base_ancestry",
                    "current": {"base_sha": base, "candidate_sha": candidate},
                    "expected": "base_sha is an ancestor of candidate_sha",
                })

        head = branch_sha(repo, branch)
        if candidate:
            if head is None:
                failures.append({
                    "check": "target_branch",
                    "current": {"branch": branch, "sha": None},
                    "expected": f"{branch} exists",
                })
            elif not is_ancestor(repo, candidate, head):
                # main may move on after this task is promoted, but it must not
                # have lost the candidate.
                failures.append({
                    "check": "target_branch",
                    "current": {"branch": branch, "sha": head},
                    "expected": f"{branch} contains candidate_sha {candidate}",
                })

        try:
            resources = parse_resources(merged.get("delivery_resources"))
        except ValueError as error:
            failures.append({
                "check": "delivery_resources",
                "current": str(error),
                "expected": "valid JSON with branches and worktrees arrays",
            })
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
    except GitFactError as error:
        # A probe that cannot answer is not evidence of a clean repository.
        return {
            "failures": [{
                "check": "git_probe",
                "current": str(error),
                "expected": "git can be read in the workspace repository",
            }],
            "target_branch": branch,
            "leftover_resources": {"branches": [], "worktrees": []},
        }

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
