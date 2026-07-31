# 软件开发协作（software-development）

协调负责人是 `pm`。项目决策人（stakeholder）在职责之外，不注册 Agent，只通过 PM 参与。

| 稳定键 | 职责 | 数量 | 交付什么 |
| --- | --- | --- | --- |
| `pm` | PM | 单个 | 目标、范围、优先级、验收标准、路由决策、最终验收 |
| `tl` | 技术负责人 | 可多个 | 技术方案、实现、代码质量、技术集成与冲突解决 |
| `qa` | QA | 可多个 | 独立验证证据、缺陷、测试资产的组织 |
| `design` | 设计 | 可多个 | 交互、视觉、用户体验、用户文案 |

- 任务类型：`requirement`、`decision`、`design`、`development`、`bug`、`validation`、`chore`。
- 状态：`backlog`、`ready`、`in_progress`、`review`、`blocked`、`done`、`canceled`。
- 等待对象：`pm`、`stakeholder`。
- 派发规则：`ready` 派发给卡片当前 `role`；`review` 和 `blocked` 一律派发给 PM；其余状态不派发。

## 1. PM

**做什么**

- 面向项目决策人管理目标、范围和优先级，并把它们落成**可验证**的验收标准。「体验更好」不是验收标准，「登录失败时 3 秒内出现明确错误提示且不清空已填表单」才是。
- 负责拆卡、路由、业务初审、最终验收，以及所有对外决策沟通。
- 补齐卡片让下游能开工：`claim_task` 要求卡片已有 `type`、`priority`、`role`、`description`、`acceptance_criteria`，缺一项 TL/QA/Design 就认领不了。

**不做什么**

- 不代替已委派的 TL、QA、Design 做具体交付。收到需要委派的可执行任务时**不要认领**，补齐卡片后直接 `route_task` 转交目标职责。
- 不决定技术方案。PM 只决定业务优先级和是否进入候选批次。

**验收门槛**

只有**技术交付证据**和**独立 QA 证据**都成立时，才能最终 `approve`。缺技术证据走 `rework`，缺独立验证走 `send_to_qa`，不要提前收口。

**turn 纪律**

路由或交接成功后立即结束 turn，依赖 daemon 后续唤醒，不要在同一个 turn 里轮询等待其他 Agent。

## 2. TL

对**正式、风险较高或规模较大**的开发任务，默认执行完整过程：

1. **三方案并行分析。** 派出三个**相互独立、只读**的分析/技术方案 SubAgent，各自从需求与代码库出发，独立提出实现方案与风险。三者之间不共享中间结论，避免相互锚定。
2. **收敛为一个方案。** TL 汇总三份输入，形成一个明确方案，写清采纳理由、被否方案的取舍点和已识别风险。
3. **单一实现者。** 只指定**一个实现 SubAgent** 写入该任务的 worktree。同一时刻只有这一个写入者。
4. **对抗式评审。** 指定**另一个独立、只读**的 Review SubAgent，审查正确性、回归风险、简洁性和 code smell。评审者不得是实现者，也不得在评审期间获得该 worktree 的写入权限。
5. **回原实现者修复。** 确认的问题交回**原实现者**修复，再复审；循环到 TL 认为可以提交为止。中途换写入者要显式交接（见 6.2 第 3 条）。

**规模与风险成比例**

小而低风险的任务允许轻量流程：直接实现 + 一次只读评审，甚至只做自查。不要把三方案流程机械套用到一行修改上。判断依据是影响面、回滚成本和不确定性，不是卡片字数。典型轻量场景：单点文案调整、明确的配置项变更、有现成测试覆盖的一行修复。典型必须走完整流程的场景：跨模块改动、状态机或数据契约变更、涉及并发或迁移、缺乏既有测试覆盖。

**集成职责**

TL 负责技术集成和冲突解决，包括候选分支的合并顺序与冲突落点选择。

## 3. QA

**定位**

QA 是**质量负责人，不是实现者**。QA 不修改产品实现；发现缺陷只提交证据和缺陷描述，由 PM 路由给 TL 修复。

**验证基准**

在**独立的 worktree / session** 中，针对**准确的候选快照（明确 commit SHA）**验证。证据里必须写明验证所用的 SHA 与 worktree 路径，否则结论无法复现。

**测试资产**

- **先复用已有测试资产。** 先看项目里已有什么，不足才补。
- 覆盖不足时，QA 安排 TestEngineer 编写两类资产：
  - **programmatic**：项目原生的 Python / JS / Playwright 等自动化测试，放在项目既有测试位置，遵循项目既有命名与发现方式；
  - **agentic**：以 Markdown 描述的自然语言验收用例，放在项目既有 acceptance 测试位置。
- 测试资产同样遵循**单写入者**：由一个 TestEngineer 完成。
- 再由**独立只读 Review SubAgent** 对抗式检查：覆盖目标是否对齐验收标准、边界条件是否遗漏、有没有发生需求漂移（测的是实现而不是需求）。
- 发现问题回到**原 TestEngineer** 修改，不换人。
- **通过审查后才执行测试。** 先跑未审查的测试等于用未验证的尺子量东西。

**结论有效期**

候选内容一旦变化，既有 QA 结论**立即失效**，必须针对新 commit 重新验证。不要复用旧 SHA 上的通过结论。

## 4. Design

负责用户体验、交互、视觉和用户文案。交付可复查的产物（稿件、原型、文案清单）与验收证据，经 PM 路由接受任务、经 PM 评审收口。不越权承担实现、验证或技术决策。

## 5. 任务流转与工具用法

### 5.1 动作取值

- `submit_task` 的 `outcome`：
  - `pm` / `tl` / `design` 用 `completed`；
  - `qa` 只能用 `passed` 或 `failed`，工具会自动加上「QA 结论：通过（Passed）」或「QA 结论：未通过（Failed）」前缀。
  - 角色与 outcome 不匹配会被直接拒绝。
- `review_task` 的 `decision`（仅 PM）：
  - `approve` → `done`；
  - `rework` → `ready`，必须同时传 `role` 和 `result_evidence`，可指定任意目标职责；
  - `send_to_qa` → `ready`，`role` 被强制为 `qa`，不接受自定义 `role`。
  - 三条都要求非空 `result_evidence`。
- `route_task`（仅 PM，四条都要求传 `role`）：
  - `prepare`：`backlog` → `ready`；
  - `delegate`：`ready` → `ready`，换执行职责；
  - `resume`：`blocked` → `ready`；
  - `recover`：`in_progress` → `ready`，仅用于执行 Session 确认不可用后的恢复。
- `block_task`：TL / QA / Design 只能把 `waiting_on` 设为 `pm`；只有 PM 能设为 `stakeholder`。必填 `blocked_reason`、`waiting_on`、`next_action`。
- `cancel_task` 需要显式确认与 `result_evidence`。取消**进行中**的任务必须先 `stop_task_execution`（需 `reason`，需确认）取得 `execution_stopped`，再 `cancel_task`。

### 5.2 硬约束

- 认领前卡片必须已有 `type`、`priority`、`role`、`description`、`acceptance_criteria`，否则 `claim_task` 被拒。
- 不存在 `ready → done` 或 `in_progress → done`，所有工作都必须经过 `review`。
- `backlog → ready` 之后不能退回 `backlog`。
- `ready → in_progress` 只能由实际执行者 `claim_task` 触发；daemon 通知不改状态。
- 所有 `review` 一律派发给 PM，与卡片 `role` 无关。
- 非 PM 不得修改 `role` 或跨职责转派。
- `approve` 会清空 `role`、`agent`、`agent_id`、`progress`、`next_action`；终态卡片上读不到这些字段，追溯要靠 `result_evidence` 与飞书字段修改历史。
- `review` 上的评审结论只能由 `review_task` 给出（PM 另可在 `review` 上 `block_task` 等待项目决策人，或 `cancel_task` 作废）。**不存在**「转交 TL」的专用 decision，也不存在 `promote` / `preflight` / `gate` 一类动作或状态。
- 工作已完成时直接 `submit_task` 一并提交 `progress`、`next_action`、`result_evidence`，不要先做一次仅为提交服务的 `update_task`。

## 6. Git 协作协议

**本节只属于 `software-development` 协作模式，不是跨 Workflow 规则。本轮不由程序强制**——违反不会被 MCP 拒绝，但会破坏交接可追溯性和 QA 结论的有效性。

### 6.1 优先级

用户项目已有 `AGENTS.md`、`CONTRIBUTING.md` 或其他明确 Git 规范时，**优先遵循项目规范**。只有在没有既有规范时，才采用下面的 TeamFlow 默认协议。两者冲突时以项目规范为准，并在卡片里说明依据。

### 6.2 默认协议

1. **项目根 checkout 保持稳定**，不作为 Agent 的日常写入目录。
2. **每张需要修改仓库的任务卡使用一个短期任务分支和一个隔离 worktree**，worktree 位于项目根目录内。默认路径 `.teamflow/worktrees/<task-id>/`；分支与目录命名都应包含稳定 task ID，便于从卡片反查工作现场。
3. **一个任务分支同一时刻只有一个写入者。** 规划、评审和 QA 默认只读。更换写入者必须显式交接，不能靠默契。
4. **交接必须基于干净工作树和明确 commit SHA。** 提交保持单一目的，不混入无关改动，不随意重写已共享的历史。
5. **默认只保留一个长期目标分支**，代表已集成、已验证、可发布的状态。实际发布由 tag / version / deployment record 表示，不靠额外长期分支。项目既有规则另有 dev / release 分支时服从项目规则。
6. **多个经 PM 初审通过的任务可以组成一个临时 integration/validation gate**，流程见 6.3。
7. **需求卡与内部卡分层写作。** 项目决策人可见的需求卡只写业务目标、范围和验收标准，不塞入 MCP、daemon、worktree 等 TeamFlow 内部术语。内部 gate 卡和结果证据里可以写分支、commit SHA 和测试信息。
8. **不把这些 Git 规则上升为跨 Workflow 规则**，也不抽取成全局 reference。

### 6.3 integration/validation gate

**职责分工**

- PM 决定业务优先级和纳入哪些任务。
- TL 从最新目标分支创建临时 merge-group 候选分支，按依赖顺序合入各任务分支。
- 冲突在任务分支或候选分支解决，**不直接污染目标分支**。
- TL 运行技术 preflight，提交明确的 candidate SHA。
- QA 从**准确的 candidate SHA** 建独立 worktree，验证这一批任务**组合后**的真实状态，而不是逐个任务分支的状态。
- **candidate 发生任何变化，QA 结论作废。**
- QA 通过后由 TL 把该准确候选晋升到目标分支；PM 再关闭 gate 卡以及它覆盖的功能卡。
- QA 失败时目标分支保持不变，PM 把受影响任务打回；修复后重建候选。
- QA 期间目标分支前移时，同样重建候选并重新执行要求的验证。

**卡片流转**

只能用现有状态机与现有 MCP 工具表达，**不新增状态、字段或 MCP**。用一张贯穿卡表达：`type` 取 `development`；若这批交付物本质是发布检查则取 `validation`。`dependencies` 用自由文本写本批候选任务编号。

| # | 角色 | 调用 | 关键参数 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | PM | `create_task` | `title`、`task_type`（卡片上的字段名是 `type`）、`priority`、`description`（批次范围）、`acceptance_criteria`（gate 判据）、`dependencies`（候选任务编号） | → `backlog` |
| 2 | PM | `route_task` | 规则 `prepare`，`role="tl"` | `backlog` → `ready` |
| 3 | TL | `claim_task` | 无业务参数 | `ready` → `in_progress` |
| 4 | TL | `submit_task` | `outcome="completed"`，`result_evidence` 写候选分支、candidate SHA、preflight 命令与输出 | `in_progress` → `review` |
| 5 | PM | `review_task` | `decision="send_to_qa"`，`result_evidence` 写初审结论，`next_action` 写需 QA 验证的范围 | `review` → `ready`（`role` 强制 `qa`） |
| 6 | QA | `claim_task` | 无业务参数 | `ready` → `in_progress` |
| 7 | QA | `submit_task` | `outcome="passed"` 或 `"failed"`，`result_evidence` 写独立 worktree、验证所用 candidate SHA 与测试证据 | `in_progress` → `review` |
| 8 | PM | `review_task` | `decision="rework"`，`role="tl"`，`result_evidence` 写「QA 已通过，转 TL 执行晋升」，`next_action` 写晋升动作 | `review` → `ready` |
| 9 | TL | `claim_task` | 无业务参数 | `ready` → `in_progress` |
| 10 | TL | `submit_task` | `outcome="completed"`，`result_evidence` 写晋升记录（目标分支新 SHA、tag 或发布产物） | `in_progress` → `review` |
| 11 | PM | `review_task` | `decision="approve"`，`result_evidence` 写关闭结论 | `review` → `done` |

**第 8 步的表达落差要直说**：从 `review` 指定任意目标职责的唯一手段就是 `decision="rework"`，尽管它的标签是「打回返工」。当前没有更贴切的 decision，这是最简单可行的现有流程，不要为此虚构新动作或新状态。

**失败与异常分支**

- 第 7 步 `outcome="failed"` 时，第 8 步 PM 同样用 `rework` + `role="tl"` 让 TL 修复，然后重走 3 → 7。这条分支上 `rework` 与语义一致。
- 执行中受阻：TL / QA 用 `block_task`（`waiting_on` 只能填 `pm`）；PM 用 `route_task` 的 `resume` 规则解除，需传 `role`。
- PM 需要项目决策人拍板：PM 在 `in_progress` 或 `review` 上 `block_task`，`waiting_on="stakeholder"`。
- 批次作废：`backlog` / `ready` / `review` / `blocked` 上直接 `cancel_task`；卡在 `in_progress` 时先 `stop_task_execution` 再 `cancel_task`。
- 批次归属只能用自由文本 `dependencies` 表达，没有结构化父子关系。不要为了结构化而新增字段。

## 7. 完整规则出处

取消决策权限（哪些取消 PM 可以自行决定、哪些必须先取得项目决策人明确同意、未取得同意时任务保持 `blocked`）、进行中取消的收尾流程、阻塞处理规则，以及「执行 Session 确认不可用」的判定标准（`notLoaded` 与 `idle` 都是正常状态，`systemError` 与读取失败必须重试后才能认定不可用），见 [协作模式规格](../../../docs/workflows/software-development.md)。需要做这几类判断时读取它，不要凭本文件的机械前置条件推断。
