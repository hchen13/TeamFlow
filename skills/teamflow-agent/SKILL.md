---
name: teamflow-agent
description: Use when a Codex session registered as a TeamFlow agent needs to inspect, claim, execute, review, block, or complete work from the shared TeamFlow board.
---

# TeamFlow Agent 执行入口

本 Skill 是已注册 TeamFlow Agent 处理看板任务的统一入口。TeamFlow 自身的安装、配置与诊断属于 `teamflow-setup`。

## 第一步：确认身份

永远先调用 `get_assignment`。身份由可信 MCP 调用上下文解析，不需要也不应该自行传入 `agent_id`、`role` 或 `workflow_key`。返回结果给出当前工作区、协作模式、职责和 Agent。

如果工具明确返回当前 Session 尚未注册，而用户本轮已经明确指定了当前 Session 的 TeamFlow 角色（以及可选代号），则转到 `teamflow-setup` 的“当前 Session 角色注册”受限流程。该授权只覆盖本地绑定：注册成功后结束本轮，不继续初始化看板、处理飞书权限、启用 daemon 或执行任务。正式职责上下文由下一次真实用户消息或 TeamFlow 投递隐藏注入。

用户没有明确指定角色、指定角色不存在，或当前 Session ID 无法准确识别时，不自行推断或注册。

## 第二步：按 workflow_key 读取协作模式指引

| `workflow_key` | 指引 |
| --- | --- |
| `software-development` | [软件开发协作](references/software-development/overview.md) |
| `general-task` | [通用任务协作](references/general-task/overview.md) |

先读对应协作模式的 `overview.md`，再按其中的路由只加载当前职责和当前交付模式需要的 reference。`workflow_key` 不在上表时，不要臆造流程：只依据 `get_assignment` 与 `get_task` 返回的合法动作、合法字段和合法选项工作。

## 通用纪律

- **TeamFlow MCP 是读写看板的唯一接口。** 不要降级调用 Lark CLI、飞书 API 或底层多维表格接口，即使 MCP 报错也不要绕过。
- **通知不等于认领。** 收到可执行任务通知后，先用 `get_task` 读完整卡片（必要时用 `list_available_tasks` 查看队列），确认自己要亲自执行，才调用 `claim_task`。只有工具返回成功，任务才归当前 Agent。
- **工具拒绝时保留结构化错误。** 错误里包含当前状态、失败原因、合法字段、合法选项和可执行下一步；按它修正后重试，不要换一条路径绕过规则。
- **交接后立即结束 turn。** 路由、提交、评审或转交成功后不要轮询卡片等待其他 Agent。后续需要当前职责处理时，等待新的 TeamFlow 通知。
- **不要越权。** 只做当前职责的事，不代替其他职责处理任务，也不自行假设另一种职责。
- **`done` 与 `canceled` 是严格终态。** 需要继续工作时新建任务并关联原任务，不要重开终态卡片。
