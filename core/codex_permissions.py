from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


TEAMFLOW_PLUGIN_ID = "teamflow@teamflow"
TEAMFLOW_MCP_SERVER = "teamflow"
TEAMFLOW_MCP_TOOLS = (
    "get_assignment",
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
_AUTHORIZATION_MARKER_NAME = ".teamflow-mcp-authorization.json"


class CodexBackgroundMcpPermissionRequired(ValueError):
    def __init__(
        self,
        missing_tools: Iterable[str],
        *,
        activation_pending: bool = False,
    ) -> None:
        self.missing_tools = tuple(sorted(set(missing_tools)))
        self.activation_pending = activation_pending
        if activation_pending:
            super().__init__(
                "Codex 已写入 TeamFlow 工具授权，但当前客户端尚未加载。"
                "请完全退出并重新打开 Codex；等待中的任务会在授权生效后自动恢复。"
            )
            return
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
    ipc_path: str | Path | None = None,
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
    result = _authorization_result(
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
    if not result["configured"]:
        return result
    return _with_runtime_activation(
        result,
        path,
        ipc_path=ipc_path,
    )


def require_teamflow_mcp_authorization(
    required_tools: Iterable[str] = TEAMFLOW_MCP_TOOLS,
) -> dict[str, Any]:
    status = inspect_teamflow_mcp_authorization(required_tools=required_tools)
    if not status["authorized"]:
        raise CodexBackgroundMcpPermissionRequired(
            status["missing_tools"],
            activation_pending=bool(status["activation_pending"]),
        )
    return status


def authorize_teamflow_mcp(
    *,
    confirmed: bool,
    config_path: str | Path | None = None,
    ipc_path: str | Path | None = None,
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
    current = inspect_teamflow_mcp_authorization(path, ipc_path=ipc_path)
    if current["configured"]:
        return {
            **current,
            "changed": False,
            "backup_path": None,
            "restart_required": bool(current["activation_pending"]),
        }
    updated = _set_tool_approvals(source)
    if updated == source:
        status = inspect_teamflow_mcp_authorization(path, ipc_path=ipc_path)
        return {
            **status,
            "changed": False,
            "backup_path": None,
            "restart_required": bool(status["activation_pending"]),
        }

    tomllib.loads(updated)
    backup_path = _backup_config(path) if path.exists() else None
    _atomic_write(path, updated)
    _record_runtime_before_authorization(path, ipc_path=ipc_path)
    status = inspect_teamflow_mcp_authorization(path, ipc_path=ipc_path)
    if not status["configured"]:
        raise ValueError(status["error"] or "Codex MCP 授权配置未生效")
    return {
        **status,
        "changed": True,
        "backup_path": str(backup_path) if backup_path else None,
        "restart_required": bool(status["activation_pending"]),
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
        "activation_pending": False,
        "restart_required": False,
        "warning": None,
        "config_path": str(path),
        "plugin_id": TEAMFLOW_PLUGIN_ID,
        "server": TEAMFLOW_MCP_SERVER,
        "source": source,
        "approved_tools": list(approved_tools) if missing else list(TEAMFLOW_MCP_TOOLS),
        "missing_tools": missing,
        "error": error,
    }


def _with_runtime_activation(
    result: dict[str, Any],
    config_path: Path,
    *,
    ipc_path: str | Path | None,
) -> dict[str, Any]:
    marker = _read_authorization_marker(config_path)
    previous_processes = {
        int(pid)
        for pid in ((marker or {}).get("codex_client_pids") or [])
        if isinstance(pid, int) or str(pid).isdigit()
    }
    if previous_processes:
        current_processes = _codex_client_process_ids()
        if (
            current_processes is not None
            and previous_processes.isdisjoint(current_processes)
        ):
            _clear_authorization_marker(config_path)
            return result
        if current_processes is not None:
            return _pending_runtime_result(result)

    previous_runtime = marker.get("ipc_socket") if marker else None
    if not isinstance(previous_runtime, dict):
        return result
    current_runtime = _ipc_socket_fingerprint(
        _resolve_ipc_path(config_path, ipc_path)
    )
    if current_runtime != previous_runtime:
        _clear_authorization_marker(config_path)
        return result
    return _pending_runtime_result(result)


def _pending_runtime_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        **result,
        "authorized": True,
        "activation_pending": True,
        "restart_required": True,
        "source": "background_active_restart_pending",
        "warning": (
            "TeamFlow 后台执行已使用新配置；授权时已运行的 Codex 客户端退出前，"
            "后台任务将通过独立 app-server 运行"
        ),
    }


def _record_runtime_before_authorization(
    config_path: Path,
    *,
    ipc_path: str | Path | None,
) -> None:
    marker_path = config_path.with_name(_AUTHORIZATION_MARKER_NAME)
    client_pids = _codex_client_process_ids()
    fingerprint = _ipc_socket_fingerprint(
        _resolve_ipc_path(config_path, ipc_path)
    )
    if client_pids == []:
        marker_path.unlink(missing_ok=True)
        return
    if client_pids is None and fingerprint is None:
        marker_path.unlink(missing_ok=True)
        return
    payload = {
        "configured_at": datetime.now().astimezone().isoformat(),
        "ipc_socket": fingerprint,
        "codex_client_pids": (
            sorted(client_pids)
            if client_pids is not None
            else None
        ),
    }
    _atomic_write(
        marker_path,
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
    )


def _read_authorization_marker(config_path: Path) -> dict[str, Any] | None:
    marker_path = config_path.with_name(_AUTHORIZATION_MARKER_NAME)
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _clear_authorization_marker(config_path: Path) -> None:
    config_path.with_name(_AUTHORIZATION_MARKER_NAME).unlink(missing_ok=True)


def _resolve_ipc_path(
    config_path: Path,
    ipc_path: str | Path | None,
) -> Path:
    if ipc_path is not None:
        return Path(ipc_path).expanduser().resolve()
    return config_path.parent / "ipc" / "ipc.sock"


def _ipc_socket_fingerprint(path: Path) -> dict[str, Any] | None:
    try:
        metadata = path.stat()
    except OSError:
        return None
    return {
        "path": str(path),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "ctime_ns": metadata.st_ctime_ns,
    }


def _codex_client_process_ids() -> set[int] | None:
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    processes: dict[int, tuple[int, str]] = {}
    for line in completed.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        try:
            pid = int(parts[0])
            parent_pid = int(parts[1])
        except ValueError:
            continue
        processes[pid] = (parent_pid, parts[2])

    clients = set()
    for pid, (parent_pid, command) in processes.items():
        normalized = command.lower()
        if (
            " app-server" not in normalized
            or "features.code_mode_host=true" not in normalized
            or " app-server daemon " in normalized
            or " app-server proxy" in normalized
        ):
            continue
        parent = processes.get(parent_pid, (0, ""))[1].lower()
        if "teamflow" in parent or "python" in parent:
            continue
        clients.add(pid)
    return clients


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
