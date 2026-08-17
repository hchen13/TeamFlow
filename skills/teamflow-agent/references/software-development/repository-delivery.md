# 仓库交付协议

本文只属于 `software-development` 协作模式，适用于 `delivery_mode=repository` 的任务。它规定 Git 安全不变量、角色责任和交接事实；看板状态与合法动作仍以 workflow contract 为准。

## 边界与不变量

- commit SHA 是交付单位。branch 与 worktree 是可丢弃的临时现场；未提交内容不构成交接。
- 项目根 checkout 保持稳定，不作为日常实现目录。
- 项目已有 `AGENTS.md`、`CONTRIBUTING.md` 等规范优先，可以覆盖命名、目标分支、PR/merge 方式和 worktree 路径，但不能覆盖：
  1. 一个共享 ref 同一时刻只有一个写入者。
  2. 跨 Agent 只交接已提交的准确 SHA。
  3. QA 结论绑定它实际验证的 SHA。
  4. 不 force、不 rebase 已经交接过的历史。
  5. 同一目标分支一次只允许一个集成者更新。
- 冲突时在卡片证据中写明依据。默认只处理本地对象；远端 push、PR 和远端 branch 删除服从项目规范。

## TL 开发与交接

- 认领后才从目标分支基准创建 task branch 与 worktree。默认命名是 `teamflow/<task_id>/<workstream>` 与 `.teamflow/worktrees/<task_id>/<workstream>`；`<workstream>` 是安全 slug，不是固定枚举。
- 同一张卡的普通返工复用原 task branch 与 worktree；只有需要另一个独立物理现场时才另起后缀。
- 每次新建或删除 branch / worktree，都把实际对象追加到卡片的 `delivery_resources`。清空字段不等于完成清理，最终门禁按记录的精确对象检查。
- 实现、内部评审、修复和提交都在任务 worktree 内完成，同一时刻只有一个写入者。
- workspace 启用版本控制时，卡片进入会派发工作的状态或完成状态之前必须先定下 `delivery_mode`（`standard` 或 `repository`）。PM 用一次只带 `delivery` 的 `update_task` 记录；定下后不能改变。
- `candidate_sha`、`verified_sha`、`promoted_sha` 通过 `update_task` 或 `submit_task` 的 `delivery` 提交，必须是本仓库 object format 下的完整 commit id。分支名、`HEAD`、tag、缩写和 rev 表达式都不合法。
- `target_branch` 固定为 `main`，`base_sha` 在首次认领时自动固定；两者都不由 Agent 写入。
- 新建或删除临时对象时用 `delivery.resources` 追加声明，只接受 `branches` 与 `worktrees` 两个字符串数组；相对路径按 workspace 根解析。
- `delivery_incomplete` 会列出缺失 SHA、三者是否一致、base 是否为候选祖先、main 是否包含候选，以及哪些声明的 branch/worktree 仍存在。按 failures 逐项处理并补录事实，再重试原动作。TeamFlow 不代为 merge、fast-forward 或清理。
- 技术交接必须包含 candidate SHA、目标分支及其起始 SHA、branch/worktree 位置、工作树是否干净、验证证据和未解决风险。交接后冻结 candidate，等返工明确返回后才能继续写。

## QA 与测试资产

- 在独立 worktree / session 中验证准确的 candidate SHA，证据写明 SHA 与现场位置。
- 不需要写入时使用 detached worktree。
- 需要沉淀测试资产时创建 QA 分支并提交；新的 tip 成为新的 candidate，测试资产不能只留在 detached 现场。
- 失败结论交回 PM，由 PM 转给 TL。TL 必须在包含 QA 测试提交的历史之上修复，形成新 candidate，再做 fresh QA。
- candidate 变化后，旧 QA 结论立即失效。

## 并发与串行集成

- 多个功能可以在各自 branch/worktree 并行开发和 QA，证据分别绑定候选 SHA。
- 同一目标分支同一时刻只允许一个候选进入集成阶段；其他候选保持原看板状态等待，不用卡片模拟锁或队列。
- 正常末段：QA `submit_task(passed)` -> `review` -> PM 核验证据后 `route_task(role="tl")` -> `ready` -> TL `claim_task` -> `in_progress` -> Integration & Cleanup -> TL `submit_task(completed)` -> `review` -> PM `approve` -> `done`。
- PM 路由集成前，把 candidate SHA、目标分支当前 SHA、QA 证据和下一步写进已有字段，不发明新字段。
- 目标分支未变化且候选可 fast-forward 时，由 TL 做 ff-only 更新，并核对目标分支 HEAD 精确等于 QA 通过的 candidate；不相等则不能交付。
- 目标分支已前移、多个候选需要合并或存在冲突时，由 TL 从当前目标 tip 创建独立 integration branch/worktree，在那里真正 merge 并解决冲突，且必须保留原 candidate 为祖先，得到 integrated SHA。
- integrated SHA 是新的 candidate，旧 QA 结论失效。PM 用 `send_to_qa` 做 fresh QA，通过后再路由给 TL 完成不改变内容的 ff-only 晋升与清理。
- 目标分支更新后只核对 SHA，不再执行可能失败的业务验收；业务验收必须发生在更新之前。
- 不在目标分支上解决冲突，不 force，不重写共享历史。

daemon 不会重复投递没有变化的 `review` 卡。PM 暂缓某个候选后，应在当前集成任务重新进入 `review`、daemon 再次唤醒时继续处理仍在等待的候选；不使用额外卡片模拟队列。

## 清理与异常

- 成功晋升后，由负责集成的 TL 或明确接手的人清理交接事实中列明的短命 worktree 与 branch；不扫描或批量删除整个目录。
- 删除普通 branch 前确认它的 tip 已被目标分支包含。脏 worktree 不强删，保留现场并说明。
- 取消且未晋升时，先保证需要保留的 commit 仍被某个 ref 指向，再删除普通 branch 与 worktree。
- 清理必须幂等；对象已经不存在即视为已清理。
- 阻塞、取消和返工使用现有合法动作，不发明状态或专用清理工具。

