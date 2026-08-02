from __future__ import annotations

import json

from codex_runtime import record_runtime_event
from teamflow_hook import read_input


def main() -> None:
    hook = read_input()
    record_runtime_event(hook)
    if hook.get("hook_event_name") == "Stop":
        print(json.dumps({}))


if __name__ == "__main__":
    main()
