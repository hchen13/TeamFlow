# 软件开发协作（software-development）

协调负责人是 `pm`。项目决策人（stakeholder）在职责之外，不注册 Agent，只通过 PM 参与。

| 稳定键 | 职责 | 数量 | 主要责任 |
| --- | --- | --- | --- |
| `pm` | PM | 单个 | 目标、范围、优先级、任务路由、最终验收 |
| `tl` | 技术负责人 | 可多个 | 技术方案、实现、代码质量、技术集成 |
| `qa` | QA | 可多个 | 独立验证、缺陷证据、测试资产 |
| `design` | 设计 | 可多个 | 交互、视觉、用户体验、用户文案 |

## 读取顺序

1. 先读本文的共享协作协议。
2. 再读当前职责的工作方法：[PM](pm.md)、[TL](tl.md)、[QA](qa.md) 或 [Design](design.md)。评审、交接或异常处理确实需要了解对方责任时，可以补读相关职责文档；不要无目的加载全部文档。
3. 仅当卡片的 `delivery_mode` 是 `repository` 时，再读[仓库交付协议](repository-delivery.md)。

合法动作、参数、字段和值始终以 `get_assignment` 返回的 workflow contract 和 `get_task.available_actions` 为准。本目录说明协作方法，不能改变机器权限或状态机。

## 共享交接

| 交接 | 适用条件 | 必须包含的事实 | 下一责任方 |
| --- | --- | --- | --- |
| PM -> TL | 需要技术实现或集成 | 需求、范围、验收标准、优先级 | TL |
| TL -> PM | 技术工作完成 | candidate SHA（如适用）、技术证据、风险 | PM 初审 |
| PM -> QA | 风险或验收标准要求独立验证 | 冻结候选、验证目标、已有证据 | QA |
| QA -> PM | 独立验证完成 | verified SHA（如适用）、测试证据、结论 | PM 决策 |
| PM -> TL | `repository` 候选已经验证但尚未晋升 | Integration & Cleanup 指令、已验证候选、目标分支事实 | TL |
| TL -> PM | 集成收尾完成 | promoted SHA（如适用）、集成与清理证据 | PM 最终验收 |

路由或交接成功后立即结束 turn，不在同一个 turn 里轮询其他 Agent；后续需要当前职责处理时，等待新的 TeamFlow 通知。

## 交付模式

- workspace 启用版本控制时，每张卡在进入可执行状态前必须选择 `standard` 或 `repository`。
- `standard` 表示验收结果不包含对版本控制项目文件的持久修改。可以读取、分析、运行检查和使用临时现场，但不创建用于交付的 branch、worktree 或 commit，也不直接修改 `main`。
- `repository` 表示验收结果包含需要保留的仓库修改，必须遵守候选提交、独立现场、晋升和清理规则。改动很小不等于 `standard`。
- workspace 未启用版本控制时只有 `standard`；任务可以按项目自身约定修改文件，但 TeamFlow 不提供 branch、SHA、晋升或清理保证。
- 在启用版本控制的 workspace 中，交付模式在任务离开 backlog 后锁定。执行者发现 `standard` 任务实际需要持久修改仓库时，不在项目根目录继续编辑；应使用 `block_task(waiting_on="pm")` 说明误分类和所需下一步。PM 取消并按 `repository` 重建任务，或保留原卡作为分析交付并另建关联的实施任务。
- `repository` 规定如何交付修改，不自动决定是否需要独立 QA。验证深度由 PM 按影响面、回滚成本和不确定性判断。

## 任务流转纪律

- 通知不等于认领。实际执行者只有在 `claim_task` 成功后才开始工作。
- 认领前卡片必须已有 `type`、`priority`、`role`、`description`、`acceptance_criteria`。
- 不存在 `ready -> done` 或 `in_progress -> done`；所有工作都经过 `review`。
- `backlog -> ready` 后不能退回 `backlog`。
- `ready -> in_progress` 只能由实际执行者 `claim_task` 触发；任务通知本身不改状态。
- 所有 `review` 都由 PM 处置，与卡片当前 `role` 无关。
- 非 PM 不得修改 `role` 或跨职责转派。
- 阶段性交付已确认、但同一张卡仍需其他职责继续正向工作时，PM 使用 `route_task(role=...)` 将它带回 `ready`；不使用 `rework` 伪造正向交接，也不另建卡模拟队列。
- 工作完成时直接 `submit_task`，一并提交进展、下一步和证据。记录已经发生的不可逆外部事实时，可以立即 `update_task` 落盘。
- `result_evidence` 是替换字段。写入前先读取现值，把旧证据与新证据合并后整体传回。
- `approve` 会清空当前职责、Agent、进展和下一步；终态追溯依赖累计的 `result_evidence` 与飞书字段历史。
- 工具拒绝时，以结构化错误给出的当前状态、合法选项和下一步为准，不绕过 MCP 直接改表。

## 阻塞、取消与恢复

- 阻塞必须同时写明 `waiting_on`、`blocked_reason` 和 `next_action`。非 PM 等待 PM；PM 无法决策时才等待 stakeholder。
- 任务去重、拆分或替代且不改变已确认业务结果时，PM 可以取消；需求、目标或范围变化必须先取得 stakeholder 明确同意。
- 取消进行中的任务前，PM 必须先用 `stop_task_execution` 停止当前 turn，再安排已有产物的收尾并调用 `cancel_task`。
- 任务投递或 Session 故障不是业务阻塞，不能据此把卡片改为 `blocked`。确认执行 Session 无法继续使用后，由 PM 按合法动作把任务恢复到 `ready`，并处理已有工作区改动。
