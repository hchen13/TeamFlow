---
name: teamflow-setup
description: Use when setting up or operating TeamFlow itself - initializing a workspace, opening the configuration UI, connecting Lark identities and boards, registering agents, authorizing Codex tools, managing the daemon, or diagnosing configuration problems.
---

# TeamFlow 安装与运行管理

本 Skill 只负责 TeamFlow 自身的配置、启动、诊断与运行管理。它不处理业务任务卡片；已注册 Agent 执行看板任务时使用 `teamflow-agent`。

所有命令都从 TeamFlow 插件仓库根目录运行。`--workspace` 不会向父目录自动发现项目，始终传绝对路径：

```bash
PROJECT_ROOT=/absolute/path/to/project
```

`self-check` 与 `authorize-codex-tools` 不接受 `--workspace`；`daemon run` / `start` / `status` / `stop` 接受但不使用它。其余命令的 `--workspace` 默认值是当前执行目录。

## 前置条件（需要用户亲自完成）

1. **创建并发布一个飞书/Lark 开放平台应用**。TeamFlow 不能代为创建。
2. **在开放平台完成用户授权**。授权页会申请以下 scope，必须由用户在飞书界面同意：

   ```text
   bitable:app
   docs:event:subscribe
   docs:permission.member:auth
   docs:permission.member:create
   drive:drive.metadata:readonly
   offline_access
   ```

3. **若要使用事件监听**：在开放平台把事件订阅方式设为长连接，添加以下两个事件，并**发布应用版本**。三步缺一不可，且只能由用户在飞书界面完成：

   ```text
   drive.file.bitable_record_changed_v1
   drive.file.bitable_field_changed_v1
   ```

4. **在 Codex 中安装并启用 TeamFlow 插件**，并在插件页确认 MCP 与 `UserPromptSubmit`、`PostCompact` 两个 Hook 已授权。CLI 无法替用户安装、启用或授权；插件缺失或被禁用时 `authorize-codex-tools` 会直接报错。
5. **应用密钥由用户从开放平台取得并 export 到环境变量**。CLI 只接收环境变量名，不接收密钥本身。

## 1. 初始化工作区

```bash
./teamflow init \
  --workspace "$PROJECT_ROOT" \
  --display-name "My project" \
  --write-gitignore
```

`--write-gitignore` 是显式选项，把 `.teamflow/` 写入项目 `.gitignore`。项目状态位于 `<project>/.teamflow/teamflow.db`。首次运行由 uv 安装锁定依赖。

## 2. 打开配置 UI

```bash
./teamflow serve-ui --workspace "$PROJECT_ROOT"
```

默认地址取自 `teamflow.config.json`，通常是 `http://127.0.0.1:13145/`；可用 `--host` / `--port` 覆盖。首次启动会自动执行 `npm ci`。

UI 是推荐路径，按顺序覆盖：选择协作模式 → 连接飞书身份 → 配置看板 → 验证访问权限并选主身份 → 验证看板监听 → 绑定 Agent Session。下面的 CLI 步骤与 UI 等价，用于脚本化或诊断。

协作模式也可以直接切换：

```bash
./teamflow select-workflow --workspace "$PROJECT_ROOT" --workflow software-development
```

## 3. 配置飞书身份

用户身份（推荐通过 UI 发起设备授权）：

```bash
./teamflow verify-lark-user-identity --workspace "$PROJECT_ROOT"
```

该命令只验证 `lark-cli` 登录态并保存身份元数据，**不会主动发起登录**。未登录时需要用户先在飞书完成设备授权。

应用（Bot）身份：

```bash
export TEAMFLOW_LARK_APP_SECRET='replace-me'

./teamflow configure-lark-identity \
  --workspace "$PROJECT_ROOT" \
  --app-id cli_xxx \
  --app-secret-env TEAMFLOW_LARK_APP_SECRET \
  --domain feishu
```

`--domain` 取 `feishu` 或 `larksuite`。维护已保存身份：

```bash
./teamflow refresh-lark-identity --workspace "$PROJECT_ROOT" --identity-id ID
./teamflow remove-lark-identity  --workspace "$PROJECT_ROOT" --identity-id ID
```

## 4. 配置并验证看板

连接已有多维表格，或用已保存身份新建：

```bash
./teamflow configure-lark-board \
  --workspace "$PROJECT_ROOT" \
  --url 'https://example.feishu.cn/base/BASE_TOKEN?table=TABLE_ID&view=VIEW_ID'

./teamflow create-lark-board \
  --workspace "$PROJECT_ROOT" \
  --identity-id ID \
  --domain feishu \
  --name "TeamFlow board"
```

`configure-lark-board` 也接受 `/wiki/...` 链接，但解析 Wiki 节点需要一个可用身份。

补授协作者与验证访问：

```bash
./teamflow grant-lark-board-access --workspace "$PROJECT_ROOT" --identity-id ID
./teamflow verify-lark-board --workspace "$PROJECT_ROOT" --stream
```

`verify-lark-board` 不是纯读操作：它会创建、读取并清理临时记录以确认真实写权限。`--identity-id` 可只验证单个身份。看板需要绑定一个具备管理权限的主身份；无法通过 `grant-lark-board-access` 取得时，需要用户在飞书侧手工授予。

## 5. 初始化任务表

配置并验证多维表格之后才执行：

```bash
./teamflow initialize-lark-board \
  --workspace "$PROJECT_ROOT" \
  --task-prefix TF
```

`--task-prefix` 为 1–5 个字母、数字或中文字符；省略时从项目显示名或目录名推导。任务 ID 由飞书生成，例如 `TF-0001`。初始化是增量的：补充缺失字段、选项和视图，不删除已有业务字段；已有默认数据表为空时复用并清掉空白占位记录，非空默认表不会被改造。

## 6. 验证事件监听

```bash
./teamflow verify-lark-listener --workspace "$PROJECT_ROOT"
./teamflow listen-lark-events   --workspace "$PROJECT_ROOT"
```

`verify-lark-listener` 最多创建并清理三组临时记录，确认长连接确实收到记录变更事件；运行它的主身份需要多维表格管理权限。`--identity-id` 用于在验证后显式绑定主身份。`listen-lark-events` 以 NDJSON 输出指定工作区的实时事件流，仅用于诊断。

这一步依赖前置条件 3 已在开放平台完成，否则不会有事件到达。

## 7. 注册 Agent

Codex Session 必须先由用户在 Codex 中创建，CLI 只做发现与绑定：

```bash
./teamflow list-codex-sessions --workspace "$PROJECT_ROOT"

./teamflow register-agent \
  --workspace "$PROJECT_ROOT" \
  --workflow software-development \
  --role pm \
  --harness-type codex \
  --session-id THREAD_ID \
  --display-name "PM"

./teamflow verify-agent --workspace "$PROJECT_ROOT"
```

`--workflow` 省略时取工作区当前协作模式，`--harness-type` 目前只支持 `codex`，`--replace-role` 替换该职责下已有的全部 Agent。换绑 Session 或注销：

```bash
./teamflow update-agent     --workspace "$PROJECT_ROOT" --agent-id AGENT_ID --session-id NEW_THREAD_ID
./teamflow unregister-agent --workspace "$PROJECT_ROOT" --agent-id AGENT_ID
```

`unregister-agent` 省略 `--agent-id` 时可用 `--workflow` / `--role` / `--harness-type` / `--session-id` 定位。

向空闲 Agent 发送一轮消息（诊断用）：

```bash
./teamflow send-agent --workspace "$PROJECT_ROOT" --agent-id AGENT_ID --message "检查任务并给出下一步"
```

## 8. 授权 Codex 工具

无人值守执行需要显式授权 TeamFlow MCP 工具：

```bash
./teamflow authorize-codex-tools --confirmed
```

`--confirmed` 表示用户已明确同意修改 Codex 配置；不传会直接拒绝。该命令写入 `$CODEX_HOME/config.toml`（默认 `~/.codex/config.toml`），覆盖 12 个工具：`get_assignment`、`list_available_tasks`、`get_task`、`claim_task`、`cancel_task`、`stop_task_execution`、`create_task`、`update_task`、`route_task`、`block_task`、`review_task`、`submit_task`。

前提是插件已在 Codex 中安装并启用（见前置条件 4）。插件代码更新后需要刷新本机插件缓存并重启 Codex Desktop，Skill 变更还需要新开会话才生效。

## 9. 管理 daemon

**安装插件不会让 daemon 常驻。** daemon 是全局单进程，必须显式启动，或由 `serve-ui`、`verify-lark-listener`、`listen-lark-events` 按需拉起。

```bash
./teamflow daemon start
./teamflow daemon sync   --workspace "$PROJECT_ROOT"
./teamflow daemon status
./teamflow daemon enable  --workspace "$PROJECT_ROOT"
./teamflow daemon disable --workspace "$PROJECT_ROOT"
./teamflow daemon stop
```

子命令为 `run`、`start`、`status`、`stop`、`enable`、`disable`、`sync`；`sync` 可用 `--identity-id` 指定同步所用的拥有者或管理员身份。同一 `brand + app_id` 只启动一个飞书 WebSocket worker，不同工作区按 `file_token + table_id` 路由事件。身份或看板配置变化后执行 `daemon sync` 刷新路由。退出 UI 或事件流不会停止全局 daemon。

## 10. 诊断

```bash
./teamflow inspect --workspace "$PROJECT_ROOT" --json
./teamflow inspect-agent-context --workspace "$PROJECT_ROOT" --all --json
./teamflow self-check
```

`inspect` 会顺带执行待应用的数据库迁移并同步协作模式定义，`--json` 输出已脱敏。`inspect-agent-context` 必须四选一：`--agent-id`、`--role`、`--session-id`、`--all`。`self-check` 在临时工作区跑完整配置冒烟，不接受 `--workspace`。

排查顺序建议：`inspect` 看工作区/身份/看板/Agent 状态 → `verify-lark-board` 看权限 → `daemon status` 与 `verify-lark-listener` 看事件链路 → `verify-agent` 看 Session 可用性。

## 边界

- 本 Skill 不创建、认领、评审或推进任何任务卡片。这些操作属于 `teamflow-agent`。
- 需要用户在飞书开放平台或 Codex 界面完成的授权步骤不能绕过，也不能用 CLI 伪造。
- 不要臆造命令或参数；不确定时先执行 `./teamflow COMMAND --help`。
