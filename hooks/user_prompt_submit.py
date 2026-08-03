from __future__ import annotations

from codex_runtime import record_runtime_event
from teamflow_hook import inject_assignment_context, read_input


def main() -> None:
    hook = read_input()
    record_runtime_event(hook)
    if str(hook.get("hook_event_name") or "UserPromptSubmit") != "UserPromptSubmit":
        return
    inject_assignment_context(hook, event_name="UserPromptSubmit")


if __name__ == "__main__":
    main()
