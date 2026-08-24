from __future__ import annotations

import os
import shutil
import tomllib
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


TEAMFLOW_PLUGIN_ID = "teamflow@teamflow"
TEAMFLOW_MCP_SERVER = "teamflow"
TEAMFLOW_MCP_TOOLS = (
    "get_assignment",
    "list_tasks",
    "list_available_tasks",
    "get_task",
    "claim_task",
    "cancel_task",
    "stop_task_execution",
    "create_task",
    "update_task",
    "route_task",
    "block_task",
    "review_task",
    "submit_task",
)
_TOOL_SECTION_PREFIX = (
    f'[plugins."{TEAMFLOW_PLUGIN_ID}".mcp_servers.'
    f"{TEAMFLOW_MCP_SERVER}.tools."
)
class CodexBackgroundMcpPermissionRequired(ValueError):
    def __init__(self, missing_tools: Iterable[str]) -> None:
        self.missing_tools = tuple(sorted(set(missing_tools)))
        detail = ", ".join(self.missing_tools) or "TeamFlow MCP"
        super().__init__(
            "Codex 后台执行尚未获得 TeamFlow MCP 授权："
            f"{detail}。请在 TeamFlow 首次配置中完成一次性授权；"
            "等待中的任务会在授权后自动恢复。"
        )


def codex_config_path() -> Path:
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return home.expanduser().resolve() / "config.toml"


def inspect_teamflow_mcp_authorization(
    config_path: str | Path | None = None,
    *,
    required_tools: Iterable[str] = TEAMFLOW_MCP_TOOLS,
) -> dict[str, Any]:
    path = Path(config_path).expanduser().resolve() if config_path else codex_config_path()
    required = tuple(dict.fromkeys(required_tools))
    if not path.exists():
        return _authorization_result(
            path,
            required,
            source="missing_config",
            error="Codex config.toml 不存在",
        )
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        return _authorization_result(
            path,
            required,
            source="invalid_config",
            error=f"无法读取 Codex 配置：{error}",
        )

    plugins = payload.get("plugins")
    plugin = plugins.get(TEAMFLOW_PLUGIN_ID) if isinstance(plugins, dict) else None
    if not isinstance(plugin, dict):
        return _authorization_result(
            path,
            required,
            source="missing_plugin",
            error="Codex 尚未启用 TeamFlow 插件",
        )
    if plugin.get("enabled") is False:
        return _authorization_result(
            path,
            required,
            source="plugin_disabled",
            error="Codex 中的 TeamFlow 插件已停用",
        )

    servers = plugin.get("mcp_servers")
    server = servers.get(TEAMFLOW_MCP_SERVER) if isinstance(servers, dict) else None
    if not isinstance(server, dict):
        server = {}
    if server.get("enabled") is False:
        return _authorization_result(
            path,
            required,
            source="server_disabled",
            error="TeamFlow MCP server 已停用",
        )
    tool_settings = server.get("tools")
    tool_settings = tool_settings if isinstance(tool_settings, dict) else {}
    default_mode = server.get("default_tools_approval_mode")
    approved = []
    missing = []
    explicit = 0
    for name in required:
        tool = tool_settings.get(name)
        tool_mode = tool.get("approval_mode") if isinstance(tool, dict) else None
        if tool_mode is not None:
            explicit += 1
        if (tool_mode or default_mode) == "approve":
            approved.append(name)
        else:
            missing.append(name)
    source = "missing_tools"
    if not missing:
        source = "per_tool" if explicit == len(required) else "server_default"
    return _authorization_result(
        path,
        missing,
        source=source,
        approved_tools=approved,
        error=(
            None
            if not missing
            else "TeamFlow MCP 工具尚未完成一次性授权"
        ),
    )


def require_teamflow_mcp_authorization(
    required_tools: Iterable[str] = TEAMFLOW_MCP_TOOLS,
) -> dict[str, Any]:
    status = inspect_teamflow_mcp_authorization(required_tools=required_tools)
    if not status["authorized"]:
        raise CodexBackgroundMcpPermissionRequired(status["missing_tools"])
    return status


def authorize_teamflow_mcp(
    *,
    confirmed: bool,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("必须明确确认后才能修改 Codex MCP 授权配置")
    path = Path(config_path).expanduser().resolve() if config_path else codex_config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source = path.read_text(encoding="utf-8") if path.exists() else ""
    payload = tomllib.loads(source) if source else {}
    plugins = payload.get("plugins")
    plugin = plugins.get(TEAMFLOW_PLUGIN_ID) if isinstance(plugins, dict) else None
    if not isinstance(plugin, dict):
        raise ValueError("请先在 Codex 中安装并启用 TeamFlow 插件")
    if plugin.get("enabled") is False:
        raise ValueError("请先在 Codex 中启用 TeamFlow 插件")
    current = inspect_teamflow_mcp_authorization(path)
    if current["configured"]:
        return {
            **current,
            "changed": False,
            "backup_path": None,
        }
    updated = _set_tool_approvals(source)
    if updated == source:
        status = inspect_teamflow_mcp_authorization(path)
        return {
            **status,
            "changed": False,
            "backup_path": None,
        }

    tomllib.loads(updated)
    backup_path = _backup_config(path) if path.exists() else None
    _atomic_write(path, updated)
    status = inspect_teamflow_mcp_authorization(path)
    if not status["configured"]:
        raise ValueError(status["error"] or "Codex MCP 授权配置未生效")
    return {
        **status,
        "changed": True,
        "backup_path": str(backup_path) if backup_path else None,
    }


def _authorization_result(
    path: Path,
    missing_tools: Iterable[str],
    *,
    source: str,
    approved_tools: Iterable[str] = (),
    error: str | None = None,
) -> dict[str, Any]:
    missing = list(missing_tools)
    return {
        "authorized": not missing and error is None,
        "configured": not missing and error is None,
        "config_path": str(path),
        "plugin_id": TEAMFLOW_PLUGIN_ID,
        "server": TEAMFLOW_MCP_SERVER,
        "source": source,
        "approved_tools": list(approved_tools) if missing else list(TEAMFLOW_MCP_TOOLS),
        "missing_tools": missing,
        "error": error,
    }


def _set_tool_approvals(source: str) -> str:
    lines = source.splitlines(keepends=True)
    setting = 'approval_mode = "approve"\n'
    for tool_name in TEAMFLOW_MCP_TOOLS:
        section = f"{_TOOL_SECTION_PREFIX}{tool_name}]"
        section_index = next(
            (
                index
                for index, line in enumerate(lines)
                if line.strip() == section
            ),
            None,
        )
        if section_index is None:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            if lines and lines[-1].strip():
                lines.append("\n")
            lines.extend([f"{section}\n", setting])
            continue

        end = next(
            (
                index
                for index in range(section_index + 1, len(lines))
                if lines[index].lstrip().startswith("[")
            ),
            len(lines),
        )
        setting_index = next(
            (
                index
                for index in range(section_index + 1, end)
                if lines[index].lstrip().startswith("approval_mode")
            ),
            None,
        )
        if setting_index is not None:
            lines[setting_index] = setting
        else:
            lines.insert(section_index + 1, setting)
    return "".join(lines)


def _backup_config(path: Path) -> Path:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.teamflow-backup-{timestamp}")
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)
    return backup


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.teamflow-{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
