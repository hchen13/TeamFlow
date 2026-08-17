# TeamFlow 协作模式定义规范

## 1. 文档定位

本文定义所有 TeamFlow `workflow.json` 必须遵循的统一语法。它面向协作模式作者、核心运行时维护者和评审者，不描述某一种具体协作模式的产品规则。

规范由三部分共同约束：

1. 本文给出人可读语义。
2. [`core/workflow_validation.py`](../../core/workflow_validation.py) 是可执行校验器。
3. [`tests/test_workflow_validation.py`](../../tests/test_workflow_validation.py) 和 [`tests/test_workflow_contract.py`](../../tests/test_workflow_contract.py) 防止语法校验器、固定 MCP 工具和定义文件发生漂移。

三者不一致时不能通过新增协作模式的评审。不得为某个协作模式在 MCP、后台调度程序或界面中增加按协作模式名称判断的专用分支。

## 2. 统一模型

每个协作模式都由同一种模型表达：

```text
公共任务字段
+ 固定业务动作
+ 协作模式声明的职责、任务类型和等待对象
+ 协作模式声明的状态图
+ 每个动作在当前状态图中的规则
= 一个可由通用 MCP 执行的协作模式
```

其中：

- `workflows/<workflow-key>/workflow.json` 是协作模式机器定义源。
- `skills/teamflow-agent/references/<workflow-key>/overview.md` 是面向 Agent 的协作指引入口，但不能改变机器权限或状态机。复杂协作模式可以在同一目录内拆分共享合同和职责文档；协作模式不再各自暴露顶层 Skill，插件只暴露 `teamflow-setup` 和 `teamflow-agent`。
- 飞书多维表格保存任务事实，不保存协作模式定义。
- SQLite 只保存定义投影和运行实例，不成为规则源。
- MCP 工具集合固定；不同协作模式只改变工具在当前任务上的合法规则和执行效果。

工作区和界面只暴露当前插件中存在有效 `workflow.json` 的协作模式、职责和任务类型。旧迁移留下的物理行可以为历史引用保留，但不能继续成为可选择或可执行定义。

## 3. 固定 MCP 工具

所有协作模式共享以下工具，不得自行增加同义工具或改名。

### 3.1 读取工具

| 工具 | 作用 |
| --- | --- |
| `get_assignment` | 返回可信调用身份、工作区、协作模式、职责和 Agent |
| `list_available_tasks` | 列出当前 Agent 按定义可以认领的任务 |
| `get_task` | 读取完整卡片和当前合法动作 |

### 3.2 生命周期工具

| 定义动作 | MCP 工具 | 固定语义 |
| --- | --- | --- |
| `create` | `create_task` | 创建处于初始状态的任务 |
| `update` | `update_task` | 不改变状态地修改当前规则允许的普通字段 |
| `route` | `route_task` | 将任务路由或恢复到可由某职责处理的状态 |
| `claim` | `claim_task` | 由实际执行 Agent 原子认领任务 |
| `submit` | `submit_task` | 执行 Agent 按结果类型提交工作 |
| `block` | `block_task` | 记录阻塞原因、等待对象和下一步 |
| `review` | `review_task` | 协调负责人按评审结论推进任务 |
| `cancel` | `cancel_task` | 经确认并满足前置条件后取消任务 |

每个定义必须完整声明这八类动作，即使某个职责不能使用其中部分动作。权限由动作规则表达，不通过删除工具表达。

`submit_task.outcome` 必须对应 `submit.rules[].key`；`review_task.decision` 必须对应 `review.rules[].key`。其他生命周期工具由当前状态、调用者、输入字段和字段值自动选择适用规则。

### 3.3 运行时工具

不直接修改卡片状态、但为状态动作提供可靠事实的操作放在 `runtime_actions`。

当前规范只定义：

| 定义动作 | MCP 工具 | 产出事实 |
| --- | --- | --- |
| `stop_execution` | `stop_task_execution` | `execution_stopped` |

某个协作模式可以不支持该运行时动作。工具仍存在，但调用时返回该协作模式不支持此动作。

## 4. 公共任务协议

`task_schema.base` 在当前规范中必须为 `teamflow-task-v1`。公共字段键固定如下：

| 字段键 | 含义 | 生命周期保护 |
| --- | --- | --- |
| `title` | 标题 | 不可清空 |
| `task_id` | 看板内自动编号 | 只由看板产生 |
| `status` | 当前状态 | 只由规则的 `to` 写入 |
| `type` | 任务类型 | 值来自 `task_types` |
| `priority` | 优先级 | 固定为 `P0`、`P1`、`P2`、`P3` |
| `role` | 当前负责职责 | 值来自 `roles` |
| `agent` | 执行 Agent 显示名 | 只由 `actor_fields` 写入 |
| `agent_id` | 执行 Agent 稳定标识 | 只由 `actor_fields` 写入 |
| `description` | 任务描述 | 普通字段 |
| `context` | 补充上下文 | 普通字段 |
| `acceptance_criteria` | 验收标准 | 普通字段 |
| `dependencies` | 依赖任务 | 普通字段 |
| `progress` | 当前进展 | 普通字段 |
| `next_action` | 下一步 | 普通字段 |
| `result_evidence` | 结果与证据 | 普通字段 |
| `blocked_reason` | 阻塞原因 | 普通字段 |
| `waiting_on` | 等待对象 | 值来自 `waiting_targets` |

规则不能把 `task_id`、`status`、`agent` 或 `agent_id` 放入 `writable_fields`、`defaults` 或 `fixed_fields`。这样 Agent 不能伪造编号、绕过状态机或冒充执行者。

## 5. 顶层结构

当前规范的顶层只允许以下字段；出现其他字段直接校验失败。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `schema_version` | 是 | 固定为 `3` |
| `key` | 是 | 稳定标识，必须与所在目录名一致 |
| `labels` | 是 | 双语名称 |
| `short_descriptions` | 是 | 双语短描述 |
| `coordinator_role` | 是 | 协调负责人的职责键 |
| `roles` | 是 | 职责定义 |
| `task_types` | 是 | 任务类型定义 |
| `waiting_targets` | 是 | 等待对象定义 |
| `task_schema` | 是 | 公共任务协议与编号规则 |
| `lifecycle` | 是 | 状态和生命周期动作 |
| `runtime_actions` | 是 | 不直接转移状态的运行时动作，可为空对象 |

所有 `labels`、`descriptions`、`short_descriptions` 和 `dispatch_instructions` 都只允许且必须同时提供：

```json
{
  "zh-CN": "中文",
  "en": "English"
}
```

程序逻辑只能使用稳定键，不能依赖显示名称。

## 6. 职责、任务类型与等待对象

### 6.1 `roles`

每一项只允许：

```json
{
  "key": "tl",
  "labels": {"zh-CN": "技术负责人", "en": "Technical Lead"},
  "descriptions": {"zh-CN": "……", "en": "..."},
  "allow_multiple": true
}
```

- `key` 在当前协作模式中唯一。
- `coordinator_role` 必须引用其中一项。
- `allow_multiple=false` 表示该职责只注册一个 Agent。

### 6.2 `task_types`

每一项只允许：

```json
{
  "key": "development",
  "labels": {"zh-CN": "开发", "en": "Development"},
  "descriptions": {"zh-CN": "……", "en": "..."},
  "default_role": "tl"
}
```

`default_role` 必须引用已声明职责。创建任务未显式提供 `role` 时，执行器使用任务类型的默认职责。

### 6.3 `waiting_targets`

每一项只允许：

```json
{
  "key": "stakeholder",
  "labels": {"zh-CN": "项目决策人", "en": "Stakeholder"},
  "color": {"hue": "Purple", "lightness": "Lighter"}
}
```

等待对象由每个协作模式自行声明，不由核心写死为 `pm`。规则通过 `field_values.waiting_on` 进一步限制某一职责可以选择哪些等待对象。

### 6.4 `task_schema`

```json
{
  "base": "teamflow-task-v1",
  "task_id": {
    "sequence_length": 4
  }
}
```

`sequence_length` 允许 `1` 至 `9`，只控制看板自动编号的数字长度；项目前缀属于工作区初始化参数，不写入协作模式定义。

## 7. 状态机

### 7.1 `lifecycle`

```json
{
  "initial_state": "backlog",
  "terminal_states": ["done", "canceled"],
  "states": [],
  "actions": {}
}
```

统一校验要求：

- 初始状态和所有终态必须已声明。
- 初始状态不能同时是终态。
- 每个状态必须能从初始状态到达。
- 每个非终态必须至少存在一条最终到达任一终态的路径。
- 终态不能作为任何动作的来源状态。
- 终态的 `dispatch` 必须为 `none`。

### 7.2 状态定义

每个状态只允许：

```json
{
  "key": "ready",
  "labels": {"zh-CN": "可执行", "en": "Ready"},
  "color": {"hue": "Blue", "lightness": "Lighter"},
  "dispatch": "task_role",
  "dispatch_instructions": {
    "zh-CN": "……",
    "en": "..."
  },
  "required_fields": ["title", "role"]
}
```

`dispatch` 只允许：

| 值 | 行为 |
| --- | --- |
| `none` | 不产生 Agent 任务通知 |
| `task_role` | 通知卡片 `role` 对应职责 |
| `coordinator` | 通知 `coordinator_role` |

当 `dispatch` 不是 `none` 时，必须提供双语 `dispatch_instructions`。后台调度程序读取该字段构造任务提示，不按具体状态名写死。

`required_fields` 是进入或保持该状态必须完整的任务字段。动作自身的 `required_task_fields` 会与目标状态的必填字段合并。

## 8. 生命周期动作规则

每个动作对象只允许：

```json
{
  "labels": {"zh-CN": "认领任务", "en": "Claim task"},
  "confirmation_required": false,
  "rules": []
}
```

`create` 必须且只能包含一条规则。其他动作至少包含一条规则。

`confirmation_required` 当前只允许在 `cancel` 动作中设为 `true`，因为固定生命周期工具中只有 `cancel_task` 接受该确认输入。停止执行的确认属于 `runtime_actions.stop_execution`，由 `stop_task_execution` 单独处理。定义不能声明运行时无法传入的确认条件。

### 8.1 调用者语义

规则的 `actors` 只允许：

| 值 | 判断方式 |
| --- | --- |
| `coordinator` | 调用 Agent 的职责等于 `coordinator_role` |
| `task_role` | 调用 Agent 的职责等于卡片当前 `role` |
| `assigned_agent` | 调用 Agent 的 `agent_id` 等于卡片当前 `agent_id` |

可选的 `roles` 对上述条件进一步收窄。例如 `actors=["assigned_agent"]`、`roles=["qa"]` 表示只有已认领该任务的 QA Agent 可以执行。

Agent 身份来自 MCP 可信调用上下文，不是工具参数，不能由模型自行填写。

### 8.2 规则字段

每条规则只允许以下字段：

| 字段 | 说明 |
| --- | --- |
| `key` | 在当前动作内唯一；也是 `submit`/`review` 的选项值 |
| `labels` | 双语规则名称 |
| `actors` | 合法调用者语义，至少一项 |
| `roles` | 可选职责白名单 |
| `from` | 来源状态列表 |
| `to` | 目标状态 |
| `writable_fields` | 调用方可提供的普通字段 |
| `required_inputs` | 必须提供且非空的输入，必须属于 `writable_fields` |
| `required_task_fields` | 执行动作前卡片必须已经具备的字段 |
| `defaults` | 调用方未提供时使用的默认值 |
| `fixed_fields` | 无条件覆盖为定义值 |
| `clear_fields` | 无条件清空的字段 |
| `field_values` | 对可写枚举字段进一步限制合法值 |
| `field_prefixes` | 为非空文本补充稳定前缀 |
| `actor_fields` | 从可信 Agent 身份写入执行者字段 |
| `guards` | 必须已经取得的运行时事实 |

当前规范中运行时事实与固定工具严格对应：

| 运行时事实 | 允许使用的生命周期动作 | 事实来源 |
| --- | --- | --- |
| `executor_unavailable` | `route` | 后台确认原执行 Session 永久不可用 |
| `execution_stopped` | `cancel` | `stop_task_execution` 的可靠停止结果 |

其他动作不得声明 `guards`。新增事实或把事实用于新的动作需要先扩展固定 MCP 运行时契约，不能只修改 JSON。

动作形状固定：

- `create`：`from` 必须为空，`to` 必须等于 `initial_state`。
- `update`：必须有 `from`，不得有 `to`。
- 其他六类动作：必须同时有非空 `from` 和合法 `to`。

### 8.3 写入合并顺序

执行器按以下顺序形成最终补丁，后一步覆盖前一步：

```text
defaults
→ 调用方输入
→ fixed_fields
→ field_prefixes
→ actor_fields
→ clear_fields
→ to 写入 status
```

因此：

- `fixed_fields` 可以保证路由到固定职责。
- `actor_fields` 可以保证执行者来自可信身份。
- `clear_fields` 可以在返工、恢复或取消时清掉旧执行者与旧阻塞信息。
- `to` 是唯一合法的状态写入来源。

### 8.4 规则选择

执行器先按来源状态和调用者筛选，再按输入字段和枚举值筛选。

- `submit` 使用 `outcome` 精确选择规则。
- `review` 使用 `decision` 精确选择规则。
- 其他动作在剩余规则中按定义顺序选择第一条匹配规则。

因此同一动作中可能同时匹配的规则必须产生等价效果，或者通过 `roles`、`from`、`writable_fields`、`field_values` 明确消除重叠。规则数组顺序不能被格式化工具任意重排。

## 9. 运行时动作

`runtime_actions.stop_execution` 只允许：

```json
{
  "labels": {"zh-CN": "停止执行", "en": "Stop execution"},
  "actors": ["coordinator"],
  "roles": [],
  "states": ["in_progress"],
  "required_inputs": ["reason"],
  "required_task_fields": ["agent_id"],
  "confirmation_required": true,
  "produces": ["execution_stopped"]
}
```

当前规范的 `required_inputs` 必须正好是 `reason`，`produces` 必须正好是 `execution_stopped`。生命周期规则可以通过 `guards=["execution_stopped"]` 要求该事实。

运行时事实只证明技术前置条件成立，不替代业务决策。例如停止执行不等于取消任务。

## 10. 职责策略的表达方式

当前规范不提供独立的 `policies` 对象。评审、升级和终态决策权限必须直接写在实际生效的动作规则中：

- 评审职责由 `review.rules[].actors` 和 `roles` 表达。
- 阻塞升级由 `block.rules[].actors`、`roles` 和 `field_values.waiting_on` 表达。
- 评审状态通知给谁由状态的 `dispatch` 表达。

这样不会出现一份“策略配置”和一份“实际动作规则”互相矛盾，或某个字段被校验但运行时从未读取的情况。

## 11. 执行与错误反馈

一次变更按以下顺序执行：

```text
从 MCP 元数据识别 Agent
→ 从飞书重新读取当前卡片
→ 读取当前协作模式定义
→ 选择动作规则
→ 校验确认、职责、状态、字段、枚举值、必填项和运行时事实
→ 在工作区写锁内再次读取卡片
→ 发现变化则返回可重试冲突
→ 使用同一调用标识写入飞书
→ 返回采用的规则、状态变化、最新卡片和后续合法动作
```

失败结果必须至少包含：

- 稳定的 `category` 与 `code`；
- 人可读且可执行的 `message`；
- 当前状态及相关失败字段或前置条件；
- `retryable`；
- 当前 Agent 在最新卡片上的 `available_actions`。

定义作者不得依赖自然语言错误来驱动程序分支；Agent 可以使用错误信息修正下一步。

## 12. 新增协作模式流程

1. 在 `workflows/<workflow-key>/` 创建 `workflow.json`，并创建 `skills/teamflow-agent/references/<workflow-key>/overview.md`。
2. 在 `skills/teamflow-agent/SKILL.md` 的映射表中补上该 `workflow_key` 与 reference 的相对链接。不要为新协作模式增加顶层 Skill。
3. 从本规范的字段集合开始，不复制核心代码或 MCP Server。
4. 定义职责、任务类型、等待对象和公共编号长度。
5. 先画完整状态图，再写状态。
6. 为八类生命周期动作逐条声明规则。
7. 检查每个非终态都能到达终态。
8. 检查每条派发状态都提供双语指令。
9. 检查每个规则的调用者、职责范围、字段和值没有重叠歧义。
10. 运行全量测试。
11. 按 [`tests/acceptance/manual.md`](../../tests/acceptance/manual.md) 执行至少一次真实 Agent、真实看板验收。

## 13. 版本变更规则

当前语法版本为 `3`：相对 `2` 增加了 `lifecycle.completion_states`（`terminal_states` 的非空子集，标记「真正完成」而非「终态」）与七个公共交付字段（`delivery_mode`、`target_branch`、`base_sha`、`candidate_sha`、`verified_sha`、`promoted_sha`、`delivery_resources`）。旧定义会被明确拒绝并给出升级提示，本实现不做多版本兼容。

`schema_version` 描述的是机器定义语法，不是文档或单个协作模式的发布版本。以下变化不改变当前语法：

- 新增一个符合本语法的协作模式目录；
- 修改显示文案；
- 在状态图仍完整、现有任务仍可解释的前提下增加职责、任务类型、状态或规则；
- 调整规则权限或效果，并重新完成自动门禁和真实链路验收；
- 在不改变公共语义的前提下修正校验器。

删除或重命名 `workflow`、职责、任务类型、等待对象、状态和规则的稳定键，不应被视为自动兼容。实施前必须先处理现有工作区、Agent、卡片和待处理投递的迁移；运行时不会把数据库中的历史投影当成仍然安装的定义。

以下变化必须提升 `schema_version`，并提供迁移方案：

- 新增顶层、状态、动作或规则字段；
- 改变固定 MCP 工具或动作集合；
- 新增公共任务字段；
- 改变规则合并顺序或调用者语义；
- 新增表达式、脚本或 Python 扩展点；
- 改变已有语法字段或固定动作键的含义。

软件开发协作模式的人类规格见 [`software-development.md`](software-development.md)，机器定义见 [`workflows/software-development/workflow.json`](../../workflows/software-development/workflow.json)，面向 Agent 的指引入口见 [`skills/teamflow-agent/references/software-development/overview.md`](../../skills/teamflow-agent/references/software-development/overview.md)。
