---
name: software-development
description: Use when a Codex session is registered as a TeamFlow software-development agent and needs to inspect, claim, execute, review, block, or complete work from the shared TeamFlow board.
---

# TeamFlow 软件开发协作

以 UserPromptSubmit 注入的工作区、协作模式和职责为准。不要自行假设另一种职责，也不要代替其他职责处理任务。

## 开始工作

1. 调用 `get_assignment` 确认当前职责。
2. 收到可执行任务通知时，先读取卡片并判断是否适合接手；通知不等于认领。
3. 调用 `list_available_tasks` 查看当前职责可认领的任务，再调用 `get_task` 读取候选任务的完整内容。
4. 决定执行后调用 `claim_task`。只有工具返回成功，任务才归当前 Agent。
5. 通过 TeamFlow MCP 工具读取或变更卡片。不要绕过工具直接调用 Lark CLI、飞书 API 或底层多维表格接口。

当前工具尚未覆盖某个合法流转动作时，保留任务现状并明确报告缺失能力，不要直接改表规避限制。

需要理解完整状态流转、取消与阻塞规则时，读取 [协作模式定义](../../docs/workflows/software-development.md)。
