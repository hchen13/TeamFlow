---
name: general-task
description: Use when a Codex session is registered as a TeamFlow general-task agent and needs to inspect, claim, execute, review, block, or complete work from the shared TeamFlow board.
---

# TeamFlow General Task

Use TeamFlow MCP tools as the only interface for reading or changing task cards. Do not fall back to Lark CLI, Feishu APIs, or direct Base operations.

Start by calling `get_assignment`. Then:

- Owners create, update, route, review, stop, or cancel tasks.
- Executors inspect available work, claim only tasks assigned to the executor role, perform the work, and submit evidence.
- Reviewers claim review work, independently verify the result, and submit review evidence.

Receiving a ready-task notification does not claim the task. Read the full card with `get_task` before deciding, and call `claim_task` only when execution will begin.

Treat `done` and `canceled` as strict terminal states. When a tool rejects an operation, follow its error code, legal options, and suggested correction instead of bypassing the workflow.
