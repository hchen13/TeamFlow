from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


RUNTIME_STATUSES = {
    "SessionStart": "idle",
    "UserPromptSubmit": "active",
    "Stop": "idle",
}


def record_runtime_event(
    hook: dict[str, Any],
    *,
    process_info: Callable[[int], tuple[int, str, str] | None] | None = None,
) -> None:
    event = str(hook.get("hook_event_name") or "")
    if event not in {*RUNTIME_STATUSES, "SessionEnd"}:
        return
    # An automatic compact raises SessionStart in the middle of a running turn, so the
    # session keeps whatever state it already had until that turn reaches Stop.
    if event == "SessionStart" and str(hook.get("source") or "") == "compact":
        return
    session_id = str(hook.get("session_id") or "").strip()
    if not session_id:
        return
    owner = find_codex_owner(process_info=process_info)
    if owner is None:
        return
    owner_pid, owner_started_at = owner
    root = runtime_root()
    path = runtime_record_path(root, session_id, owner_pid, owner_started_at)
    if event == "SessionEnd":
        try:
            path.unlink()
        except OSError:
            pass
        return
    payload = {
        "schema_version": 1,
        "session_id": session_id,
        "owner_pid": owner_pid,
        "owner_started_at": owner_started_at,
        "status": RUNTIME_STATUSES[event],
        "cwd": hook.get("cwd"),
        "model": hook.get("model"),
        "updated_at_ms": int(time.time() * 1000),
    }
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = root / f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass
    except OSError:
        pass


def find_codex_owner(
    *,
    start_pid: int | None = None,
    process_info: Callable[[int], tuple[int, str, str] | None] | None = None,
) -> tuple[int, str] | None:
    lookup = process_info or read_process_info
    pid = start_pid or os.getpid()
    visited: set[int] = set()
    for _ in range(16):
        if pid <= 1 or pid in visited:
            return None
        visited.add(pid)
        info = lookup(pid)
        if info is None:
            return None
        parent_pid, started_at, command = info
        executable = command.strip().split(maxsplit=1)[0] if command.strip() else ""
        if Path(executable).name.lower() in {"codex", "codex.exe"}:
            return pid, started_at
        pid = parent_pid
    return None


def read_process_info(pid: int) -> tuple[int, str, str] | None:
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "ppid=", "-o", "lstart=", "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    fields = result.stdout.strip().split(maxsplit=6)
    if result.returncode != 0 or len(fields) != 7:
        return None
    try:
        parent_pid = int(fields[0])
    except ValueError:
        return None
    return parent_pid, " ".join(fields[1:6]), fields[6]


def runtime_root() -> Path:
    home = Path(os.environ.get("TEAMFLOW_HOME", "~/.teamflow")).expanduser()
    return home / "codex-runtime"


def runtime_record_path(root: Path, session_id: str, owner_pid: int, owner_started_at: str) -> Path:
    safe_session_id = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)[:96] or "session"
    owner_token = hashlib.sha256(owner_started_at.encode()).hexdigest()[:12]
    return root / f"{safe_session_id}.{owner_pid}.{owner_token}.json"
