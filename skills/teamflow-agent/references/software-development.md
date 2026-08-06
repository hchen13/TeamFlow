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

对修改仓库内容的开发、缺陷、工程事务和 integration/validation gate，只有**技术交付证据**和**独立 QA 证据**都成立时，才能最终 `approve`。需求、决策、设计等非代码任务按自身验收标准收口；只有其验收标准或风险明确要求 QA 时才强制转交 QA。

**turn 纪律**

路由或交接成功后立即结束 turn，依赖 daemon 后续唤醒，不要在同一个 turn 里轮询等待其他 Agent。

## 2. TL

对**正式、风险较高或规模较大**的开发任务，默认执行完整过程：

1. **三方案并行分析。** 派出三个**相互独立、只读**的分析/技术方案 SubAgent，各自从需求与代码库出发，独立提出实现方案与风险。三者之间不共享中间结论，避免相互锚定。
2. **收敛为一个方案。** TL 汇总三份输入，形成一个明确方案，写清采纳理由、被否方案的取舍点和已识别风险。
3. **单一实现者。** 只指定**一个实现 SubAgent** 写入该任务的 worktree。同一时刻只有这一个写入者。
4. **对抗式评审。** 指定**另一个独立、只读**的 Review SubAgent，审查正确性、回归风险、简洁性和 code smell。评审者不得是实现者，也不得在评审期间获得该 worktree 的写入权限。
5. **回原实现者修复。** 确认的问题交回**原实现者**修复，再复审；循环到 TL 认为可以提交为止。中途换写入者要显式交接（见 6.1 不变量 1、2）。

这里的分析、实现和 Review SubAgent 是当前 harness 内部的临时工作者，不是 TeamFlow 看板职责，不注册 Agent，也不自行调用 TeamFlow MCP 改卡。TL 对它们的结果和最终提交负责。正式高风险任务若当前 harness 无法提供独立只读 reviewer，应 `block_task` 说明缺失的质量门槛；不要把同一人的自查伪装成独立评审。

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
- 覆盖不足时，QA 安排一个 harness 内部的 TestEngineer SubAgent 编写两类资产；TestEngineer 不是 TeamFlow 看板职责，不独立改卡：
  - **programmatic**：项目原生的 Python / JS / Playwright 等自动化测试，放在项目既有测试位置，遵循项目既有命名与发现方式；
  - **agentic**：以 Markdown 描述的自然语言验收用例，放在项目既有 acceptance 测试位置。
- 测试资产同样遵循**单写入者**：由一个 TestEngineer 完成。
- 再由**独立只读 Review SubAgent** 对抗式检查：覆盖目标是否对齐验收标准、边界条件是否遗漏、有没有发生需求漂移（测的是实现而不是需求）。
- 发现问题回到**原 TestEngineer** 修改，不换人。
- **通过审查后才执行测试。** 先跑未审查的测试等于用未验证的尺子量东西。
- agentic 用例至少写清前置条件、自然语言步骤、期望结果、证据要求和清理方式；执行时派一个新的隔离 SubAgent 只读取该用例和必要项目事实，QA 根据产出的原始证据判定，不让执行者自行宣布通过。
- 需要持久化的新测试资产提交在 `qa/<task-id>-tests` 上，得到 `Q = 产品实现 + 测试`。**最终结论必须针对 Q**，不得在测试提交之前的旧 candidate SHA 上宣布通过。Q 验证通过即成为新的可晋升候选，不需要为了把测试并回任务分支而阻塞交回 TL；完整规则见 6.3。

**结论有效期**

候选内容一旦变化，既有 QA 结论**立即失效**，必须针对新 commit 重新验证。不要复用旧 SHA 上的通过结论。目标分支前移不改变原 candidate，但集成后产生的 integrated SHA 必须重新 QA（见 6.4）。

## 4. Design

负责用户体验、交互、视觉和用户文案。交付可复查的产物（稿件、原型、文案清单）与验收证据，经 PM 路由接受任务、经 PM 评审收口。不越权承担实现、验证或技术决策。

## 5. 任务流转与工具用法

### 5.1 运行时合同优先

合法动作、参数、字段和值以 `get_assignment` 返回的 workflow contract 和 `get_task.available_actions` 为准；本 reference 只定义协作方法，不是状态机事实源。当前内置定义中，TL/PM/Design 提交完成用 `completed`，QA 用 `passed` 或 `failed`；PM 只有 `approve`、`rework`、`send_to_qa` 三种评审 decision。`rework` 只表示真实返工，不用于表达成功后的正向交接。

取消进行中的任务必须先 `stop_task_execution` 明确停止，再按运行时合同 `cancel_task`。其他调用被拒时，使用结构化错误给出的合法下一步，不根据本文静态猜测。

### 5.2 硬约束

- 认领前卡片必须已有 `type`、`priority`、`role`、`description`、`acceptance_criteria`，否则 `claim_task` 被拒。
- 不存在 `ready → done` 或 `in_progress → done`，所有工作都必须经过 `review`。
- `backlog → ready` 之后不能退回 `backlog`。
- `ready → in_progress` 只能由实际执行者 `claim_task` 触发；daemon 通知不改状态。
- 所有 `review` 一律派发给 PM，与卡片 `role` 无关。
- 非 PM 不得修改 `role` 或跨职责转派。
- `result_evidence` 是替换字段，不是追加日志。每次 `submit_task` 或 `review_task` 前先读取当前值，把已有证据与本次新增证据按小节合并后整体传回；不得丢掉 candidate SHA、QA 验证 SHA 或既有结论。
- `approve` 会清空 `role`、`agent`、`agent_id`、`progress`、`next_action`；终态卡片上读不到这些字段，追溯要靠累计的 `result_evidence` 与飞书字段修改历史。
- `review` 只由 PM 处置：最终验收、真实返工和转交 QA 分别使用 `review_task(approve)`、`review_task(rework)`、`review_task(send_to_qa)`；`rework` 只代表真实返工。阶段性交付已经确认、但同一张卡仍需其他职责继续执行正向后续工作时，PM 使用通用的 `route_task(role=…)` 将卡片带回 `ready`，并清空 `agent` / `agent_id`。PM 另可在 `review` 上 `block_task` 等待项目决策人，或 `cancel_task` 作废。不存在 `promote` / `preflight` / `gate` 一类专用动作或状态。
- 工作已完成时直接 `submit_task` 一并提交 `progress`、`next_action`、`result_evidence`，不要先做一次仅为提交服务的 `update_task`。**例外**：记录已经发生的不可逆 Git 事实（如目标分支更新后的精确 SHA、刚建或刚删的 branch/worktree）必须在事实发生后立即 `update_task` 落盘，那不是「仅为提交服务」。

## 6. Git 协作协议

**本节只属于 `software-development` 协作模式，本轮不由程序强制**——违反不会被 MCP 拒绝，但会破坏交接可追溯性和 QA 结论的有效性。它只规定 **Git 安全不变量、角色责任和交接内容**；看板状态、卡片写法和合法动作一律以 `get_assignment` 的 workflow contract 与 `get_task.available_actions` 为准。本节**不规定必须创建什么类型的卡**，也不用卡片模拟锁、队列、事务或回调。

### 6.1 边界与不变量

- **commit SHA 是交付单位。** branch 与 worktree 都是可丢弃的临时现场；未提交的工作树内容不构成交接。
- **项目根 checkout 保持稳定**，不作为日常实现目录。
- 项目已有 `AGENTS.md`、`CONTRIBUTING.md` 等 Git 规范**优先**，可以覆盖分支命名、目标分支、PR/merge 方式和 worktree 路径，但**不能覆盖**下列五条：
  1. 一个共享 ref 同一时刻只有一个写入者。
  2. 跨 Agent 只交接**已提交的准确 SHA**。
  3. QA 结论绑定它实际验证的那个 SHA。
  4. 不 force、不 rebase 已经交接过的历史。
  5. 同一目标分支一次只允许一个集成者更新。
- 冲突时在卡片里写明依据。默认只处理本地对象；远端 push / PR / 远端 branch 删除服从项目规范。

### 6.2 TL 开发与交接

- **认领之后**才从目标分支基准创建独立的 task branch 与 worktree；创建它们不产生 commit。默认命名形态是 `teamflow/<task_id>/<workstream>` 与 `.teamflow/worktrees/<task_id>/<workstream>`。`<workstream>` 是**安全 slug**，不是固定枚举：推荐用 `task`、`qa`、`integration` 打头，按实际工作可以写成 `task-api`、`qa-2`。
- **同一张卡的普通返工复用原来的 task branch 与 worktree**；只有真的需要另一个独立物理现场时才另起后缀。
- 每次新建或删除 branch / worktree，都把**实际对象**记进卡片的 `delivery_resources`（它是追加去重的历史清单，清空不等于清理完成）。最终的完成门禁以这份清单里记录的**精确对象**为事实，不按模板猜测。
- 实现、内部评审、修复、提交都在自己的 worktree 内完成，同一时刻只有一个写入者。
- 交接时必须给出这些事实：**candidate SHA**、目标分支及其起始 SHA、branch 与 worktree 位置、工作树是否干净、验证证据、尚需处理的风险。使用仓库交付的卡片把这些写进 `candidate_sha` / `target_branch` / `base_sha` 等交付字段，TeamFlow 只读取和校验，从不代为 merge、ff、删分支或删目录。
- 交接之后**冻结该 candidate**；要继续写，等返工回来再说。

### 6.3 QA 与测试资产

- 在**独立的 worktree / session** 中验证**准确的 candidate SHA**，证据写明验证所用的 SHA 与现场位置。
- 不需要写入时，detached 现场就够了。
- 需要沉淀测试资产时创建 QA 测试分支并提交，**新的 tip 成为新的 candidate**；不得把测试资产只留在 detached 现场。
- 结论失败时交回 PM，由 PM 转给 TL。**TL 必须在包含 QA 测试提交的历史之上修复**，形成新 candidate，再走一次 fresh QA。
- candidate 一旦变化，旧 QA 结论立即失效。

### 6.4 并发与串行集成

- 多个功能的 branch / worktree 可以**并行**开发与 QA，各自证据绑定各自的 candidate SHA。
- **同一目标分支同一时刻只允许一个候选进入集成阶段。** 其余候选保持它们原本的看板状态等待——本节不定义队列，也不用卡片模拟锁。
- **正常末段**：QA `submit_task(passed)` → 卡片进 `review`，PM 核验证据后用 `route_task(role="tl")` 把同一张卡带回 `ready`（`agent` / `agent_id` 被清空）→ TL `claim_task` 进 `in_progress` → 执行 Integration & Cleanup → TL `submit_task(completed)` → `review` → PM `approve` → `done`。路由之前，PM 把 **candidate SHA、目标分支及其当前 SHA、QA 证据和下一步**写进现有字段（`result_evidence` / `next_action` 等），不发明新字段。
- **目标分支未变化且候选可 fast-forward 时**：由 TL 做 ff-only 更新，完成后核对目标分支 HEAD **精确等于**已 QA 的 candidate；不相等就不算成功，交回 PM。
- **目标分支已前移、多个候选需要合并、或出现冲突时**：由 TL 从**当前目标 tip** 建独立 integration branch 与 worktree，在那里**真正 merge** 候选并解决冲突，**必须保留候选为祖先**，得到新的 integrated SHA。
- **integrated SHA 是新的 candidate，旧 QA 结论随即失效**：PM 用 `send_to_qa` 走一次 fresh QA，通过后再 `route_task(role="tl")` 交回 TL 完成**不改变内容**的最终 ff-only 晋升与清理。
- 目标分支更新之后**只核对 SHA**，不再做可能失败的业务验收——验收必须发生在更新之前。
- 不在目标分支上解决冲突，不 force，不重写共享历史。

> 当前 runtime 无法重新发现处于等待状态的 `review` 卡，等待中的候选只能靠 PM 后续主动复核推进；这是已知的 runtime 缺口，不在本节用额外卡片去补。

### 6.5 清理与异常

- 成功晋升之后，由负责集成的 TL 或明确接手的人清理**交接事实中已列明**的短命 worktree 与 branch；**不扫描、不批量删除整个目录**。
- 删除普通 branch 前先确认它的 tip 已被目标分支包含。脏 worktree 不强删，保留现场并说明。
- 取消且未晋升时，先确保需要保留的 commit 仍被某个 ref 指向，再删普通 branch 与 worktree。
- 清理应当**幂等**：对象已经不存在就视为已清理，重复执行不产生新错误。
- 清理责任作为**交接事实**说明即可。
- 阻塞、取消、返工都用现有合法动作交给相应角色，不发明新状态。

## 7. 完整规则出处

取消决策权限（哪些取消 PM 可以自行决定、哪些必须先取得项目决策人明确同意、未取得同意时任务保持 `blocked`）、进行中取消的收尾流程、阻塞处理规则，以及「执行 Session 确认不可用」的判定标准（`notLoaded` 与 `idle` 都是正常状态，`systemError` 与读取失败必须重试后才能认定不可用），见 [协作模式规格](../../../docs/workflows/software-development.md)。需要做这几类判断时读取它，不要凭本文件的机械前置条件推断。
