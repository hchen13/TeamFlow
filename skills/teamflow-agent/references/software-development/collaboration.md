# 协作与交接协议

本文是所有软件开发职责共享的最小协作合同。它说明谁向谁交付什么；各职责的内部工作方法见各自文档。

## 标准交接

| 交接 | 必须包含的事实 | 下一责任方 |
| --- | --- | --- |
| PM -> TL | 需求、范围、验收标准、优先级 | TL 实现 |
| TL -> PM | candidate SHA（如适用）、技术证据、风险 | PM 初审 |
| PM -> QA | 冻结的候选、验证目标、已有证据 | QA 独立验证 |
| QA -> PM | verified SHA（如适用）、测试证据、通过或失败结论 | PM 决策 |
| PM -> TL | Integration & Cleanup 指令、已验证候选、目标分支事实 | TL 集成收尾 |
| TL -> PM | promoted SHA（如适用）、集成与清理证据 | PM 最终验收 |

路由或交接成功后立即结束 turn，不在同一个 turn 里轮询其他 Agent。后续需要当前职责处理时，由 daemon 再次通知。

## 任务流转纪律

- 通知不等于认领。实际执行者只有在 `claim_task` 成功后才开始工作。
- 认领前卡片必须已有 `type`、`priority`、`role`、`description`、`acceptance_criteria`。
- 不存在 `ready -> done` 或 `in_progress -> done`；所有工作都经过 `review`。
- `backlog -> ready` 后不能退回 `backlog`。
- `ready -> in_progress` 只能由实际执行者 `claim_task` 触发；daemon 通知不改状态。
- 所有 `review` 都由 PM 处置，与卡片当前 `role` 无关。
- 非 PM 不得修改 `role` 或跨职责转派。
- 阶段性交付已确认、但同一张卡仍需其他职责继续正向工作时，PM 使用 `route_task(role=...)` 将它带回 `ready`；不使用 `rework` 伪造正向交接，也不另建卡模拟队列。
- 工作完成时直接 `submit_task` 一并提交进展、下一步和证据。记录已经发生的不可逆外部事实时，可以立即 `update_task` 落盘。
- `result_evidence` 是替换字段。写入前先读取现值，把旧证据与新证据合并后整体传回。
- `approve` 会清空当前职责、Agent、进展和下一步；终态追溯依赖累计的 `result_evidence` 与飞书字段历史。
- 工具拒绝时，以结构化错误给出的当前状态、合法选项和下一步为准，不绕过 MCP 直接改表。

## 阻塞、取消与恢复

- 阻塞必须同时写明 `waiting_on`、`blocked_reason` 和 `next_action`。非 PM 等待 PM；PM 无法决策时才等待 stakeholder。
- 任务去重、拆分或替代且不改变已确认业务结果时，PM 可以取消；需求、目标或范围变化必须先取得 stakeholder 明确同意。
- 取消进行中的任务前，PM 必须先用 `stop_task_execution` 停止当前 turn，再安排已有产物的收尾并调用 `cancel_task`。
- Session 投递故障不是业务阻塞，不能据此把卡片改为 `blocked`。
- `notLoaded` 与 `idle` 都是可继续使用的正常状态；`active` 表示忙碌；`archived` 可人工恢复；`deleted` 才能直接认定不可用。`systemError`、读取失败或连接失败必须重试后再判断。
- daemon 只通知执行 Session 故障，不自动恢复任务或更换 Agent。确认 Session 不可用后，由 PM 按运行时合同把任务恢复到 `ready`，并处理已有工作区改动。

