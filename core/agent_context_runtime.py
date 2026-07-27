from __future__ import annotations

from typing import Any, Callable


class AgentContextRuntime:
    def __init__(
        self,
        *,
        workspaces: Callable[[], list[str]],
        resolve: Callable[[str], Callable[..., Any]],
        emit_log: Callable[..., None],
        style: Callable[[str, str], str],
    ) -> None:
        self.workspaces = workspaces
        self.resolve = resolve
        self.emit_log = emit_log
        self.style = style

    def assignment(
        self,
        *,
        session_id: str,
        cwd: str | None,
        consume: bool,
        refresh: bool = False,
    ) -> dict[str, Any]:
        result = self.resolve("find_agent_assignment")(
            self.workspaces(),
            session_id=session_id,
            cwd=cwd,
            consume=consume,
            refresh=refresh,
        )
        return result or {"assignment": None, "additional_context": None}

    def confirm(
        self,
        *,
        workspace: str,
        agent_id: str,
        session_id: str,
        assignment_revision: int,
        context_fingerprint: str,
        context_kind: str | None,
    ) -> dict[str, Any]:
        result = self.resolve("confirm_agent_context")(
            workspace,
            agent_id=agent_id,
            session_id=session_id,
            assignment_revision=assignment_revision,
            context_fingerprint=context_fingerprint,
        )
        assignment = result.get("assignment")
        if result["confirmed"] and assignment:
            self.emit_log(
                self.style(
                    (
                        "AGENT CONTEXT RESTORED"
                        if context_kind == "recovery"
                        else "AGENT ONBOARDED"
                    ),
                    "1;32",
                ),
                fields=_log_fields(assignment),
            )
        return result

    def mark_compacted(
        self,
        *,
        session_id: str,
        cwd: str | None,
    ) -> dict[str, Any]:
        context = self.assignment(
            session_id=session_id,
            cwd=cwd,
            consume=False,
        )
        assignment = context.get("assignment")
        if not assignment:
            return {"marked": False}
        result = self.resolve("mark_agent_context_recovery_pending")(
            assignment["workspace_root"],
            agent_id=assignment["agent_id"],
            session_id=session_id,
            assignment_revision=assignment["assignment_revision"],
        )
        if result["marked"]:
            self.emit_log(
                self.style("AGENT CONTEXT RECOVERY PENDING", "1;33"),
                fields=_log_fields(assignment),
            )
        return result


def _log_fields(assignment: dict[str, Any]) -> dict[str, Any]:
    return {
        "workspace": assignment["workspace_name"],
        "workflow": assignment["workflow_key"],
        "role": assignment["role_key"],
        "agent": assignment["agent_id"],
        "session": assignment["session_id"],
        "revision": assignment["assignment_revision"],
    }
