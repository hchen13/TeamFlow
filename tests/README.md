# TeamFlow 测试导读

## 1. 测试边界

TeamFlow 的验证分为两层：

1. 自动测试验证确定性的代码契约，包括协作模式语法、状态机、权限、字段约束、事件去重、投递恢复和 Codex 协议处理。
2. 真实链路验收验证外部系统和用户可见行为，包括飞书 WebSocket、Codex Desktop、插件加载、MCP 与 Hook 授权、真实 Agent 行为和界面状态。

自动测试全部通过不代表真实链路已经完成。它不能单独证明飞书应用已正确配置、Codex 客户端已实时同步、插件权限已授予，或真实模型会按 MCP 反馈修正行为。

## 2. 运行方式

从插件仓库根目录执行：

```bash
cd /Users/ethan/playground/alpha191-quant/plugins/teamflow
uv run --locked python -m unittest discover -s tests -v
npm --prefix ui test
npm --prefix ui run build
./teamflow self-check
```

各命令分别验证：

| 命令 | 主要范围 |
| --- | --- |
| Python 测试 | 核心模型、数据库、看板、事件、调度、MCP、Codex 接入 |
| UI 测试 | Codex Session 状态聚合和前端辅助逻辑 |
| UI build | Next.js 路由、组件和静态构建完整性 |
| `self-check` | 从空工作区迁移、定义投影、身份、看板和 Agent 配置的组合冒烟 |

## 3. 推荐阅读顺序

### 第一层：协作模式语言

1. `test_workflow_validation.py`
   - 遍历所有已安装的 `workflow.json`。
   - 验证字段白名单、双语文本、职责、任务类型、等待对象和状态图。
   - 验证每个非终态都能到达终态。
   - 验证每条生命周期规则至少存在一组真实可执行的职责和输入。
   - 验证核心不依赖 `pm`、`tl`、`ready` 等某一种协作模式的名称。
2. `test_workflow_contract.py`
   - 验证固定 MCP 工具集合与八类生命周期动作一一对应。
   - 验证各职责只获得 `workflow.json` 声明的动作。
   - 验证规则生成字段补丁时的覆盖顺序。

先结合 `docs/workflows/workflow-definition.md` 阅读这两组测试。文档解释语义，校验器和测试提供可执行约束。

### 第二层：MCP 业务执行

`test_teamflow_tools.py` 验证：

- 可信调用身份和职责匹配；
- 创建、更新、路由、认领、提交、阻塞、评审和取消；
- 必填字段、合法选项和等待对象；
- 原子认领、幂等调用和冲突处理；
- 稳定错误码、可重试标记和修正提示；
- 严格终态和运行中取消的前置条件。

这一层证明通用 MCP 引擎会执行当前协作模式的规则，而不是把软件开发规则写死在工具代码中。

### 第三层：飞书事件与后台调度

`test_lark_events.py` 验证：

- WebSocket 事件进入持久化收件箱；
- 重复事件、字段连带更新和普通更新不会重复派发；
- 未注册职责进入等待，注册后按当前卡片状态补发；
- 同一 Session 串行、不同 Session 并行；
- 后台调度程序重启后的 turn 对账和恢复；
- MCP 调用身份、入职上下文和压缩恢复信号。

`test_lark_board.py` 验证：

- 飞书身份、协作者和读写权限处理；
- 空表复用、字段和看板视图初始化；
- 所有已安装协作模式的 SQLite 投影；
- 任务读取、创建和更新的数据映射。

### 第四层：Codex 与界面

`test_codex.py` 验证 Codex Desktop IPC、独立 app-server 回退、长 turn、rollout 证据和 Session 元数据读取。

`ui/lib/codex-ipc.test.cjs` 验证界面使用的 Codex 状态聚合。真实客户端启停、名称和模型设置变化仍需手工验收。

## 4. 新增协作模式时的自动门禁

新增 `workflows/<workflow-key>/workflow.json` 后，至少确认：

1. `key` 与目录名一致。
2. 八类生命周期动作全部存在。
3. 所有状态可达，所有非终态可结束。
4. 每条规则至少存在一个可执行成功路径。
5. 角色、状态和任务类型名称没有进入核心分支。
6. 定义能投影到新工作区并能被选择。
7. 同一组固定 MCP 工具能按新定义返回正确权限和合法选项。
8. `skills/teamflow-agent/references/<workflow-key>/overview.md` 存在，`SKILL.md` 已链接它，且目录内所有 Markdown 都能从入口到达。

现有 Workflow 测试会自动扫描新目录，不需要复制一套按协作模式命名的测试框架。`test_plugin_layout.py` 会检查插件暴露面与 reference 覆盖。只有新定义引入了此前没有覆盖的语义时，才补充通用测试。

## 5. 手工验收

真实链路步骤和证据格式见 `acceptance/manual.md`。建议由一个独立测试 Session 或 SubAgent 执行，并要求它只按手册操作、不修改产品代码。

手工录制可以沉淀为可复用流程，但录屏本身不是可执行规范。推荐过程：

1. 人工执行一次，并录制画面和口述操作意图。
2. 同时保存后台日志、任务 ID、Session ID、turn ID、MCP 返回和看板历史。
3. Codex 根据这些证据修订 `acceptance/manual.md`，将隐含步骤变成明确前置条件、动作和通过标准。
4. 由一个没有参与实现的测试 Agent 只读手册复现。
5. 流程连续稳定后，再提炼成仓库内的验收 Skill；Skill 负责组织执行，手册继续作为验收事实源。

录屏适合教授界面操作。Hook 是否触发、MCP 是否使用可信身份、事件是否去重等不可见事实，必须继续由日志、数据库状态或 rollout 证据验证，不能只靠画面判断。
