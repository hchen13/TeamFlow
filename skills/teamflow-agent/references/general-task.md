# 通用任务协作（general-task）

协调负责人是 `owner`。三个职责：

- **负责人（`owner`）**：创建、更新、路由、评审、停止执行和取消任务；负责目标、范围、优先级、最终验收和外部决策沟通。
- **执行者（`executor`）**：查看可认领的工作，只认领分派给执行者职责的任务，完成工作并提交结果与证据。
- **评审者（`reviewer`）**：认领评审工作，独立检查结果、识别风险，并提交评审证据。

任务类型：`task`、`research`、`content`、`decision`、`review`、`chore`。默认职责中 `decision` 归 `owner`，`review` 归 `reviewer`，其余归 `executor`。

## 动作取值

- `submit_task` 的 `outcome`：`owner` 与 `executor` 用 `completed`；`reviewer` 用 `reviewed`。
- `review_task` 的 `decision`：`approve`（→ `done`）、`rework`（→ `ready`，必须同时传 `role` 和 `result_evidence`）、`send_to_reviewer`（→ `ready`，`role` 被强制为 `reviewer`）。
- `route_task` 只有 `prepare`（`backlog` → `ready`）、`resume`（`blocked` → `ready`）和 `recover`（`in_progress` → `ready`，需要执行 Session 确认不可用）。本协作模式**没有** `ready → ready` 的转交规则。不要为了换职责伪造阻塞；尚未执行且职责错误时，由负责人取消并按正确职责新建任务，已有真实交付时再通过评审决定下一步。
- `block_task`：执行者与评审者只能把 `waiting_on` 设为 `owner`；只有负责人能设为 `stakeholder`。
- 取消进行中的任务必须先 `stop_task_execution` 明确停止执行，再 `cancel_task`。

## 纪律

- 认领前卡片必须已具备 `type`、`priority`、`role`、`description`、`acceptance_criteria`；缺失时由负责人先用 `update_task` 补齐。
- 工作完成时直接用 `submit_task` 一并提交进展、下一步和结果证据，不要先做一次仅为提交服务的 `update_task`。无法继续时用 `block_task` 写清阻塞原因、等待对象和下一步。
- 所有工作都必须经过 `review` 状态，不存在从 `ready` 或 `in_progress` 直接到 `done` 的路径。
