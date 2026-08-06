from __future__ import annotations

import json
import os
from typing import Any
from uuid import uuid4

from .config import resolve_workspace_paths


SETTINGS_SCHEMA_VERSION = 1


def default_settings() -> dict[str, Any]:
    return {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "version_control": {"enabled": True},
    }


def ensure_workspace_settings(workspace: str | None) -> dict[str, Any]:
    paths = resolve_workspace_paths(workspace)
    if paths.settings_path.exists():
        return read_workspace_settings(workspace)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    return _write(workspace, default_settings())


def read_workspace_settings(workspace: str | None) -> dict[str, Any]:
    paths = resolve_workspace_paths(workspace)
    if not paths.settings_path.exists():
        return default_settings()
    try:
        settings = json.loads(paths.settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"TeamFlow workspace settings are unreadable: {paths.settings_path}: {error}"
        ) from error
    _validate(settings, paths.settings_path)
    return settings


def set_version_control(workspace: str | None, *, enabled: bool) -> dict[str, Any]:
    """An explicit switch also repairs a corrupt file; ordinary reads still fail closed."""
    try:
        settings = ensure_workspace_settings(workspace)
    except ValueError:
        settings = default_settings()
    settings["version_control"]["enabled"] = bool(enabled)
    return _write(workspace, settings)


def version_control_enabled(workspace: str | None) -> bool:
    return bool(read_workspace_settings(workspace)["version_control"]["enabled"])


def _validate(settings: Any, path: Any) -> None:
    if not isinstance(settings, dict):
        raise ValueError(f"TeamFlow workspace settings must be an object: {path}")
    if settings.get("schema_version") != SETTINGS_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported TeamFlow workspace settings schema version: {path}"
        )
    version_control = settings.get("version_control")
    if not isinstance(version_control, dict):
        raise ValueError(f"{path}: version_control must be an object")
    if not isinstance(version_control.get("enabled"), bool):
        raise ValueError(f"{path}: version_control.enabled must be a boolean")


def _write(workspace: str | None, settings: dict[str, Any]) -> dict[str, Any]:
    paths = resolve_workspace_paths(workspace)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    temporary = paths.settings_path.with_name(
        f".{paths.settings_path.name}.{os.getpid()}.{uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        # Replace atomically so a crashed write never leaves a truncated file behind.
        temporary.replace(paths.settings_path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    return settings
