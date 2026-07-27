---
name: software-development
description: Use when a Codex session is registered as a TeamFlow software-development agent and needs to inspect, claim, execute, review, block, or complete work from the shared TeamFlow board.
---

# TeamFlow 软件开发协作

以 TeamFlow Hook 注入的工作区、协作模式和职责为准。不要自行假设另一种职责，也不要代替其他职责处理任务。

## 开始工作

1. 调用 `get_assignment` 确认当前职责。
2. 收到可执行任务通知时，先读取卡片并判断是否适合接手；通知不等于认领。
3. 调用 `list_available_tasks` 查看当前职责可认领的任务，再调用 `get_task` 读取候选任务的完整内容。
4. 决定亲自执行后调用 `claim_task`。只有工具返回成功，任务才归当前 Agent。PM 收到需要委派的可执行任务时，不要认领；应补齐当前卡片后直接用 `route_task` 转交目标职责。
5. 通过 TeamFlow MCP 工具读取或变更卡片。不要绕过工具直接调用 Lark CLI、飞书 API 或底层多维表格接口。

## 任务流转

- PM 可以使用 `create_task` 创建待规划任务，或直接补齐收到的当前任务；使用 `update_task` 补齐普通字段，再使用 `route_task` 将同一任务放入目标职责的可执行队列。
- 执行者使用 `claim_task` 认领任务；需要阶段性同步时使用 `update_task` 更新进展。工作已经完成时，直接使用 `submit_task` 一并提交进展、下一步和结果证据，不要先做一次仅为提交服务的 `update_task`；无法继续时使用 `block_task`。
- PM 使用 `review_task` 验收通过、打回返工或转交 QA。取消进行中任务时，先以 `stop_task_execution` 明确停止当前执行；确定并安排好收尾后，再使用 `cancel_task` 取消任务。
- `get_task` 返回当前调用者可执行的动作。工具拒绝操作时，依据结构化错误中的当前状态、失败原因、合法字段、合法选项和可执行下一步修正操作。
- 任务一旦通过 TeamFlow 交给其他职责，立即结束当前 turn，不要轮询卡片等待对方完成。后续状态需要当前职责处理时，TeamFlow daemon 会创建新的通知 turn。

需要理解完整状态流转、取消与阻塞规则时，读取 [协作模式定义](../../docs/workflows/software-development.md)。
