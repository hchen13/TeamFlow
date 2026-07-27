# TeamFlow 真实链路验收手册

## 1. 用途

本文供测试 Agent 或人工测试者执行真实 Codex、真实飞书看板和真实后台调度程序验收。它不替代单元测试，也不要求新增 CLI 测试命令。

执行原则：

- 飞书多维表格是任务事实源。
- Agent 只能通过 TeamFlow MCP 读取和变更卡片。
- `upsert-lark-task` 只允许在明确模拟“人类直接编辑看板”的事件入口用例中使用。
- 不以“消息已发送”“后台已接受”或单条成功日志代替完整业务结果。
- 每个用例必须同时核对后台日志、Agent Session、看板最终状态和错误反馈。
- 测试过程中不修改产品代码；发现问题后停止当前用例并记录证据。

## 2. 自动测试与手工验收边界

以下内容已经沉淀为自动测试，每次改动都必须执行：

| 范围 | 自动测试 |
| --- | --- |
| Workflow 统一语法与状态图 | `tests/test_workflow_validation.py` |
| 固定 MCP 映射与规则补丁语义 | `tests/test_workflow_contract.py` |
| 生命周期权限、字段约束、状态转移、错误结构、幂等与冲突 | `tests/test_teamflow_tools.py` |
| 飞书事件收件箱、去重、派发、后注册补发、串行、恢复、MCP 鉴权与 Hook | `tests/test_lark_events.py` |
| Codex IPC、未加载 Session 回退、长 turn 和 rollout 证据 | `tests/test_codex.py` |
| 看板初始化、字段、身份与任务读写 | `tests/test_lark_board.py` |
| UI Codex 状态聚合 | `ui/lib/codex-ipc.test.cjs` |

自动门禁：

```bash
uv run --locked python -m unittest discover -s tests -v
npm --prefix ui test
npm --prefix ui run build
./teamflow self-check
```

手工验收只保留自动测试无法证明的真实边界：

- 飞书开放平台配置和真实 WebSocket；
- Codex Desktop/VS Code 的实时可见性；
- 插件、MCP 和 Hook 的实际加载与授权；
- 真实 Agent 是否按工具反馈修正行为；
- 看板历史是否形成预期业务留痕；
- UI 在真实客户端启停和设置变化下的表现。

## 3. 环境准备

在插件仓库中设置小写临时变量：

```bash
tf=/absolute/path/to/teamflow/teamflow
ws=/absolute/path/to/test-workspace
```

测试工作区必须已经完成：

1. `software-development` 协作模式已选择。
2. 飞书身份、看板访问和看板监听全部验证通过。
3. 看板已完成 TeamFlow 结构初始化。
4. PM、技术负责人和 QA 分别绑定不同 Codex Session。
5. 当前插件代码已刷新到本机插件缓存，并重启 Codex Desktop。
6. TeamFlow MCP 可在 Agent Session 中调用。
7. `UserPromptSubmit` 与 `PostCompact` Hook 已授权。

终端一启动后台调度程序：

```bash
$tf daemon run
```

通过条件：

```text
DAEMON RUNNING
[工作区 @协作模式] WORKSPACE ENABLED
DAEMON LISTENING apps=... workspaces=...
```

终端二按需启动 UI：

```bash
$tf serve-ui --workspace "$ws" --port 12347
```

所有测试产物只能写入：

```text
$ws/tmp/teamflow-acceptance/
```

## 4. 证据记录格式

每个用例记录：

```text
用例：
结论：通过 / 失败 / 阻塞
前置状态：
执行动作：
后台日志：
Agent Session：
看板最终状态：
看板历史：
错误码与修正提示：
遗留问题：
```

失败时保留：

- 任务 ID 与飞书记录 ID；
- 完整事件 ID、turn ID、Agent ID 和 Session ID；
- 从首次 `RECEIVED` 到最终结果的连续后台日志；
- Agent 发起的 MCP 工具名、业务参数和完整返回；
- 卡片当前字段与相关历史。

## 5. 用例一：插件、MCP 与入职上下文

### 步骤

1. 【人工】在 Codex 插件页确认 TeamFlow 已启用，MCP 与两个 Hook 已授权。
2. 新注册一个测试 Agent Session，UI 应显示“待入职”。
3. 点击入职状态，核对展示的职责上下文。
4. 在该 Session 发送一条普通消息。
5. 运行：

```bash
$tf inspect-agent-context --workspace "$ws" --role tl
```

6. 让该 Agent 调用 `get_assignment`。

### 通过条件

- Session 中没有额外可见的“入职消息”及其回复。
- 后台出现一次 `AGENT ONBOARDED`。
- UI 变为“已入职”。
- `inspect-agent-context` 显示 `Onboarding: injected` 和已验证的 Codex rollout 证据。
- `get_assignment` 返回该 Session 实际绑定的工作区、职责和 Agent ID。

该用例证明实际 LLM turn 使用了隐藏职责上下文，而不只证明数据库状态发生变化。

## 6. 用例二：完整软件开发闭环

测试目标是在真实 Agent 间完成一次最小开发流程。

### 任务内容

PM 创建一张开发任务，要求技术负责人：

- 在 `$ws/tmp/teamflow-acceptance/sort_task/` 实现一个独立的排序函数；
- 覆盖空数组、重复值和逆序输入；
- 不修改该目录之外的文件；
- 在结果与证据中写明文件和测试命令；
- 技术实现完成后必须由 QA 独立运行测试并确认通过，PM 才能最终验收。

### 步骤与通过条件

1. PM 调用 `create_task`。
   - 卡片只能创建为初始状态。
   - 任务类型、优先级、负责人、描述和验收标准正确。
2. PM 必要时调用 `update_task` 补齐字段，再调用 `route_task`。
   - 卡片进入当前定义的可派发状态。
   - 后台只产生一次有效派发。
3. 技术负责人收到通知后依次调用 `get_task`、`claim_task`。
   - 收到通知不等于认领。
   - 认领成功后卡片进入执行状态并写入真实 Agent 与 Agent ID。
4. 技术负责人实现并测试，再调用 `submit_task(outcome="completed")`。
   - 结果与证据非空。
   - 卡片进入评审状态。
5. PM 调用 `get_task` 后执行 `review_task(decision="send_to_qa")`。
   - 卡片回到可由 QA 认领的状态。
   - 负责人变为 QA，旧执行 Agent 被清空。
6. QA 调用 `get_task`、`claim_task`，独立运行测试后调用 `submit_task(outcome="passed")`。
   - 结果与证据包含稳定的 QA 通过前缀。
   - 卡片回到评审状态。
7. PM 核对证据后调用 `review_task(decision="approve")`。
   - 卡片进入严格终态。
   - 后续合法动作为空，不能重新打开。

最终同时检查：

- 临时目录中的实现和测试证据存在；
- 后台每次派发不漏不重；
- 看板历史保留负责人、执行 Agent、状态和结果变化；
- 没有 Agent 降级调用 Lark CLI 或飞书 API。

## 7. 用例三：权限和状态机拒绝

使用独立测试卡，不破坏完整闭环用例。

依次验证：

| 非法动作 | 预期 |
| --- | --- |
| QA 认领负责人为技术负责人的任务 | `permission_denied` |
| 技术负责人调用 `route_task` | `permission_denied` |
| `update_task` 修改 `status` 或 `agent_id` | `invalid_fields` |
| 缺少必填字段时路由任务 | `task_not_ready`，列出缺失字段并建议先更新 |
| `submit_task` 使用当前职责不支持的结果 | `invalid_option` 或 `permission_denied`，列出合法选项 |
| 未确认取消 | `confirmation_required` |
| 执行中的任务未停止 turn 就取消 | `precondition_failed` |
| 对已完成或已取消任务重新路由 | `invalid_state`，合法动作为空 |

通过条件：

- 每次非法操作都未改变看板。
- 返回包含稳定错误码、具体原因、是否可重试和合法修正方式。
- Agent 根据错误修正下一步，不尝试绕过 MCP。

## 8. 用例四：事件入口、去重与后注册补发

### 普通更新

修改一张处于可派发状态卡片的普通字段。

通过条件：

```text
FEISHU WEBSOCKET 记录变更 RECEIVED
DISPATCH NOT-REQUIRED reason="当前变更不通知 Agent"
```

不得产生新的 Agent turn。

### 状态进入可派发

把一张完整的待规划卡移入可派发状态。

通过条件：

- 一次有效状态进入只出现一次 `DISPATCH STARTED` 和一个终局结果。
- 重复推送、自动编号连带更新和字段事件不形成重复任务通知。

### 后注册补发

1. 创建一张指向未注册职责的可派发任务。
2. 确认 `DISPATCH WAITING`，原因是未注册对应 Agent。
3. 注册该职责 Agent，不再修改卡片。

通过条件：

- 注册后立即补发当前仍可认领的任务。
- 卡片已经离开可派发状态时不得补发。

## 9. 用例五：同 Session 串行、不同 Session 并行

准备四张待规划卡：

| 卡片 | 负责人 | 指令 |
| --- | --- | --- |
| 串行一 | 技术负责人 | 等待 20 秒后回复固定标识，不修改看板 |
| 串行二 | 技术负责人 | 立即回复另一固定标识，不修改看板 |
| 并行 PM | PM | 等待 20 秒后回复固定标识，不修改看板 |
| 并行 TL | 技术负责人 | 等待 20 秒后回复固定标识，不修改看板 |

### 串行

快速把两张技术负责人卡移入可派发状态。

通过条件：

- 飞书事件按实际到达顺序处理，不要求任务编号顺序。
- 第二个 `DISPATCH STARTED` 必须晚于第一个 `DISPATCH SUCCEEDED`。
- 两张卡不漏不重。

### 并行

快速把 PM 和技术负责人卡移入可派发状态。

通过条件：

- 两个 `DISPATCH STARTED` 都早于任一任务的完成时间。
- 两个不同 Session 的 turn 有时间重叠。
- 各自回复和任务身份对应正确。

## 10. 用例六：后台调度程序中途重启

该用例分开验证“投递对账”和“MCP 重连”，避免把两种现象混在一起。

### 投递对账

1. 先在 Codex Desktop 或 VS Code 中加载目标 Agent Session，确认本次投递使用实时 Codex IPC。
2. 创建一张指令为“等待 30 秒后只回复固定标识，不调用 MCP”的可派发卡。
3. 看到 `DISPATCH STARTED` 后立即按 `Ctrl-C` 停止后台调度程序。
4. 等待 Agent turn 在已加载的 Codex 客户端中完成。
5. 重新运行 `$tf daemon run`。

通过条件：

- 停止时出现 `DISPATCH RECONCILING`。
- 重启后使用原 `turn_id` 对账。
- 已完成 turn 出现 `DISPATCH RECOVERED`，不重复注入。
- 未完成或永久失败按真实状态重试或失败，不能误报恢复成功。

目标 Session 未加载时，TeamFlow 会使用 daemon 持有的独立 app-server。停止 daemon 会同时中断该 turn；此时重启后应记录 `DISPATCH RETRY`，并在新 turn 中只产生一次最终回复，不能误报 `DISPATCH RECOVERED`。

### MCP 重连

1. 向一个 Agent 发送“等待 10 秒后调用 `get_assignment`”的任务。
2. 在等待期间停止后台调度程序。
3. 在 MCP 的恢复超时内重启后台调度程序。

通过条件：

- Agent 侧的同一次 MCP 调用保持等待，并在后台恢复后只返回一次结果。
- 后台恢复后调用成功。
- 不产生重复远端写入。
- 超过恢复窗口时返回明确连接错误，不伪装成功。

“重试复用同一内部调用标识”由 `tests/test_lark_events.py` 的自动测试验证；该标识不属于面向测试者的 CLI 或日志协议，手工验收不把不可观察的内部值列为通过条件。

## 11. 用例七：压缩恢复

1. 选择一个已经入职的 Agent Session。
2. 【人工】执行 `/compact`。
3. 在下一条用户消息或 TeamFlow 派发后观察后台。
4. 再调用 `get_assignment`。

通过条件：

- `PostCompact` 被授权并实际触发。
- 后台先出现 `AGENT CONTEXT RECOVERY PENDING`。
- 下一条真实 turn 注入恢复上下文后出现 `AGENT CONTEXT RESTORED`。
- Session 中没有可见的重复入职消息。
- `inspect-agent-context` 显示 `Evidence kind: recovery` 和实际 turn ID。
- MCP 仍识别同一职责。

若没有任何 Hook 日志，也没有授权提示，应判为失败，不得把“执行了 `/compact`”本身判为通过。

## 12. 用例八：UI 真实状态

该用例由人工执行，测试 Agent负责记录。

| 场景 | 预期 |
| --- | --- |
| Session 未被 Codex 客户端加载 | “未加载”，仍展示最近模型、推理强度和速度 |
| 已加载且空闲 | “空闲” |
| turn 执行中 | “正在运行” |
| 所有 Codex 客户端关闭 | “状态未知”，不能伪装为“未加载” |
| Desktop 修改 Session 名称或运行设置 | UI 近实时更新 |
| Agent 正在运行 | 禁止切换或移除 |
| 窄屏 | 徽标保持内容宽度，控件和文字不重叠 |

刷新页面、点击空白处或切换浏览器焦点不能让状态长期卡在“正在确认”，也不能形成持续无意义的 `/api/codex` 请求。

## 13. 结束条件

满足以下条件才能判定一个版本通过真实链路验收：

1. 自动门禁全部通过。
2. 完整软件开发闭环通过。
3. 权限拒绝不会改变看板。
4. 事件不漏不重，串行和并行符合 Session 边界。
5. 后台重启可对账，MCP 可在恢复窗口内重连。
6. 入职和压缩恢复有 rollout 证据。
7. UI 状态与真实 Codex 客户端状态一致。
8. 所有失败项都有可复现步骤、任务 ID 和连续证据。

真实验收结束后，能稳定脱离飞书、Codex 或 UI 的失败场景应继续下沉为 Python 或 Node 自动测试；只依赖第三方真实运行时和人工视觉判断的部分保留在本文。

验收卡片不得从飞书删除。未完成的测试卡由 PM 通过 TeamFlow MCP 写明原因后取消；已完成卡保留结果与证据。`tmp/teamflow-acceptance/` 中的文件保留到验收结论确认后，再由用户决定是否清理。
