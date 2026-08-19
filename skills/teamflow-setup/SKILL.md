---
name: teamflow-setup
description: Use when setting up or operating TeamFlow itself - initializing a workspace, opening the configuration UI, connecting Lark identities and boards, registering agents, authorizing Codex tools, managing the daemon, or diagnosing configuration problems.
---

# TeamFlow 安装与运行管理

本 Skill 只负责 TeamFlow 自身的配置、启动、诊断与运行管理。它不处理业务任务卡片；已注册 Agent 执行看板任务时使用 `teamflow-agent`。

## 先判定操作范围

用户明确告诉当前 Session“你是本项目的 TeamFlow QA/TL/Design/PM”等角色时，只授权**当前 Session 的角色注册**，不等于授权它接管整个项目的 TeamFlow 配置。代号或显示名可以同时指定，也可以省略。此时走下面的受限流程：

1. 只读确认当前目录已有 TeamFlow 工作区、已选择协作模式，且用户指定的角色确实存在。当前 Session ID 无法准确识别时停止，不猜测或绑定其他 Session。
2. 先用 `inspect-agent-context --session-id CURRENT_THREAD_ID` 检查当前 Session。已有相同绑定时保持不变；没有绑定时，只用第 7 节的 `register-agent` 绑定当前 Session、明确角色和可选代号。已有不同绑定、角色冲突或需要替换时停止并交给用户或 PM 处理，不默认使用 `--replace-role`。
3. 再次用 `inspect-agent-context --session-id CURRENT_THREAD_ID` 确认本地注册记录。工作区尚未启用时，状态保持待入职是正常结果。
4. 注册成功后立即结束本轮。不要在同一轮继续初始化或修复工作区，也不要处理业务卡片；下一次真实用户消息或 TeamFlow 投递会完成隐藏职责注入。

这个受限流程不得执行 `init`、选择协作模式、配置飞书身份或看板、修改应用权限、`initialize-lark-board`、验证监听、授权 Codex 工具、启动或启用 daemon，也不得注册其他 Session。缺少任何这些前置条件时，只说明需要由 PM/协调者与用户完成什么，不自行补齐。

只有用户明确要求 PM/协调者 Session 初始化、配置、修复或运行 TeamFlow 时，才进入后面的完整流程。单纯指定角色不是完整 setup 请求；已注册的非协调者发现配置缺口时只报告给用户和 PM/协调者，不自行补齐。

不要假设用户项目里存在 `./teamflow`。从 Codex 加载本 Skill 时给出的绝对 `SKILL.md` 路径推导插件根目录；无论源码态还是安装缓存态，插件根目录都是该路径的上两级：

```bash
SKILL_FILE=/absolute/path/shown-for-this-skill/SKILL.md
TEAMFLOW_ROOT=$(CDPATH= cd "$(dirname "$SKILL_FILE")/../.." && pwd)
TF="$TEAMFLOW_ROOT/teamflow"
LARK_CLI="$TEAMFLOW_ROOT/ui/node_modules/.bin/lark-cli"
LARK_CLI_BRAND=feishu  # *.larksuite.com 使用 lark
PROJECT_ROOT=/absolute/path/to/project
```

`--workspace` 不会向父目录自动发现项目，始终传项目绝对路径。`self-check`、`authorize-codex-tools` 与内部 stdio 入口 `mcp-server` 不接受 `--workspace`；`daemon run` / `start` / `status` / `stop` 接受但不使用它。其余命令的 `--workspace` 默认值是当前执行目录。

## 需要用户亲自完成的步骤

1. **创建并发布一个飞书/Lark 开放平台应用**。TeamFlow 不能代为创建。
2. **为应用开通当前版本要求的完整权限**。不要使用本 Skill 里的静态 scope 子集；在 UI 验证失败后使用它生成的完整权限修复链接，链接由当前代码中的权限事实源生成，包含记录读写、字段、视图、协作者、事件订阅和云空间元数据权限。权限变更后必须发布应用版本。
3. **若要使用事件监听**：在开放平台把事件订阅方式设为长连接，添加以下两个事件，并**发布应用版本**。三步缺一不可，且只能由用户在飞书界面完成：

   ```text
   drive.file.bitable_record_changed_v1
   drive.file.bitable_field_changed_v1
   ```

4. **在 Codex 中安装并启用 TeamFlow 插件**，并在插件页信任 TeamFlow 列出的**全部** Hook，当前为 `SessionStart`、`SessionEnd`、`UserPromptSubmit`、`Stop`。少信任任何一个都会让职责注入或会话状态上报失效；以插件页实际列出的为准。插件页没有逐 MCP 工具授权开关；后台 MCP 自动批准在第 8 步通过 TeamFlow UI 或 `authorize-codex-tools` 配置。
5. **版本控制开关**在 UI 第一步「协作模式」里，默认启用。启用后，选择 `repository` 交付的任务必须先把候选晋升到 `main` 并清理自己声明的临时 branch/worktree 才能完成；关闭只是不再允许**新**选择 `repository`，已锁定的任务仍受门禁约束。TeamFlow 不会自动 `git init`，也不代为执行任何 Git 变更。
6. **应用密钥由用户从开放平台取得并 export 到环境变量**。CLI 只接收环境变量名，不接收密钥本身。

## 1. 初始化工作区

```bash
"$TF" init \
  --workspace "$PROJECT_ROOT" \
  --display-name "My project" \
  --write-gitignore
```

`--write-gitignore` 是显式选项，把 `.teamflow/` 写入项目 `.gitignore`。项目状态位于 `<project>/.teamflow/teamflow.db`。首次运行由 uv 安装锁定依赖。

## 2. 打开配置 UI

```bash
"$TF" serve-ui --workspace "$PROJECT_ROOT"
```

默认地址取自 `teamflow.config.json`，通常是 `http://127.0.0.1:13145/`；可用 `--host` / `--port` 覆盖。首次启动会自动执行 `npm ci`。

UI 是推荐路径，覆盖：选择协作模式 → 连接飞书身份 → 配置看板 → 验证访问权限并选主身份 → 验证看板监听 → 授权 Codex 工具 → 绑定 Agent Session。UI **不会**初始化任务表，也不会仅因打开页面就启用 daemon workspace；完成 UI 配置后仍要执行第 5 步的 `initialize-lark-board` 和第 9 步的 `daemon enable`。下面的 CLI 用于补齐这两步、脚本化或诊断。

协作模式也可以直接切换：

```bash
"$TF" select-workflow --workspace "$PROJECT_ROOT" --workflow software-development
```

## 3. 配置飞书身份

用户身份（推荐通过 UI 发起设备授权）：

首次 `serve-ui` 完成 `npm ci` 后，先确认 `lark-cli` 使用同一个应用。UI 的设备授权依赖 `lark-cli` 当前全局配置，但保存 TeamFlow 应用身份不会自动改写它。若 `"$LARK_CLI" config show` 的 App ID 不一致，必须先取得用户同意，再用 App Secret 的标准输入初始化，禁止把 Secret 放进命令行参数：

```bash
printf '%s' "$TEAMFLOW_LARK_APP_SECRET" | "$LARK_CLI" config init \
  --app-id cli_xxx \
  --app-secret-stdin \
  --brand "$LARK_CLI_BRAND"
```

`LARK_CLI_BRAND` 对飞书中国版取 `feishu`，对 Lark 国际版取 `lark`；它与 TeamFlow CLI 的 `--domain feishu|larksuite` 是两套取值，不要混用。`lark-cli` 配置是全局的；切换应用可能影响其他工作区，不得静默覆盖。随后由用户在设备授权页同意 UI 请求的完整 OAuth scope，再验证并保存身份：

```bash
"$TF" verify-lark-user-identity --workspace "$PROJECT_ROOT"
```

该命令只验证 `lark-cli` 登录态并保存身份元数据，**不会主动发起登录**。未登录时需要用户先在飞书完成设备授权。

应用（Bot）身份：

```bash
export TEAMFLOW_LARK_APP_SECRET='replace-me'

"$TF" configure-lark-identity \
  --workspace "$PROJECT_ROOT" \
  --app-id cli_xxx \
  --app-secret-env TEAMFLOW_LARK_APP_SECRET \
  --domain feishu
```

`--domain` 取 `feishu` 或 `larksuite`。维护已保存身份：

```bash
"$TF" refresh-lark-identity --workspace "$PROJECT_ROOT" --identity-id ID
"$TF" remove-lark-identity  --workspace "$PROJECT_ROOT" --identity-id ID
```

## 4. 配置并验证看板

连接已有多维表格，或用已保存身份新建：

```bash
"$TF" configure-lark-board \
  --workspace "$PROJECT_ROOT" \
  --url 'https://example.feishu.cn/base/BASE_TOKEN?table=TABLE_ID&view=VIEW_ID'

"$TF" create-lark-board \
  --workspace "$PROJECT_ROOT" \
  --identity-id ID \
  --domain feishu \
  --name "TeamFlow board"
```

`configure-lark-board` 也接受 `/wiki/...` 链接，但解析 Wiki 节点需要一个可用身份。

补授协作者与验证访问：

```bash
"$TF" grant-lark-board-access --workspace "$PROJECT_ROOT" --identity-id ID
"$TF" verify-lark-board --workspace "$PROJECT_ROOT" --stream
```

`verify-lark-board` 不是纯读操作：它会创建、读取并清理临时记录以确认真实写权限。`--identity-id` 可只验证单个身份。看板需要绑定一个具备管理权限的主身份；无法通过 `grant-lark-board-access` 取得时，需要用户在飞书侧手工授予。

## 5. 初始化任务表

配置并验证多维表格之后才执行：

```bash
"$TF" initialize-lark-board \
  --workspace "$PROJECT_ROOT" \
  --task-prefix TF
```

`--task-prefix` 为 1–5 个字母、数字或中文字符；省略时从项目显示名或目录名推导。任务 ID 由飞书生成，例如 `TF-0001`。初始化是增量的：补充缺失字段、选项和视图，不删除已有业务字段。URL 显式带 `table` 时，被选中的数据表会被增量改造成 TeamFlow 任务表；未指定 `table` 时，优先复用已有 TeamFlow schema 或空表并清掉空白占位记录，非空的无关数据表不会被改造，而是另建任务表。

## 6. 验证事件监听

```bash
"$TF" verify-lark-listener --workspace "$PROJECT_ROOT"
"$TF" listen-lark-events   --workspace "$PROJECT_ROOT"
```

`verify-lark-listener` 最多创建并清理三组临时记录，确认长连接确实收到记录变更事件；运行它的主身份需要多维表格管理权限。`--identity-id` 用于在验证后显式绑定主身份。`listen-lark-events` 以 NDJSON 输出指定工作区的实时事件流，仅用于诊断。

这一步依赖“需要用户亲自完成的步骤”第 3 项已在开放平台完成，否则不会有事件到达。

## 7. 注册 Agent

Codex Session 必须先由用户在 Codex 中创建，CLI 只做发现与绑定：

```bash
"$TF" list-codex-sessions --workspace "$PROJECT_ROOT"

"$TF" register-agent \
  --workspace "$PROJECT_ROOT" \
  --workflow software-development \
  --role pm \
  --harness-type codex \
  --session-id THREAD_ID \
  --display-name "PM"

"$TF" verify-agent --workspace "$PROJECT_ROOT"
```

`--workflow` 省略时取工作区当前协作模式，`--harness-type` 目前只支持 `codex`，`--replace-role` 替换该职责下已有的全部 Agent。换绑 Session 或注销：

```bash
"$TF" update-agent     --workspace "$PROJECT_ROOT" --agent-id AGENT_ID --session-id NEW_THREAD_ID
"$TF" unregister-agent --workspace "$PROJECT_ROOT" --agent-id AGENT_ID
```

`unregister-agent` 省略 `--agent-id` 时可用 `--workflow` / `--role` / `--harness-type` / `--session-id` 定位。

向空闲 Agent 发送一轮消息（诊断用）：

```bash
"$TF" send-agent --workspace "$PROJECT_ROOT" --agent-id AGENT_ID --message "检查任务并给出下一步"
```

## 8. 授权 Codex 工具

无人值守执行需要显式授权 TeamFlow MCP 工具：

```bash
"$TF" authorize-codex-tools --confirmed
```

`--confirmed` 表示用户已明确同意修改 Codex 配置；不传会直接拒绝。该命令写入 `$CODEX_HOME/config.toml`（默认 `~/.codex/config.toml`），覆盖 12 个工具：`get_assignment`、`list_available_tasks`、`get_task`、`claim_task`、`cancel_task`、`stop_task_execution`、`create_task`、`update_task`、`route_task`、`block_task`、`review_task`、`submit_task`。

前提是插件已在 Codex 中安装并启用。该命令只改配置文件；已运行的 Codex Desktop、VS Code 插件或 CLI 进程不会可靠热加载新批准规则。授权后先重启当前正在运行的 Codex 客户端，再依赖无人值守派发。插件代码更新后还需要刷新本机插件缓存；Skill 变更使用新会话验收。

## 9. 管理 daemon

**安装插件或仅打开 UI 都不会让 daemon 常驻。** 完成配置后，用 `enable` 原子地登记、启用并同步工作区；它会在需要时启动全局单进程 daemon：

```bash
"$TF" daemon enable --workspace "$PROJECT_ROOT"
"$TF" daemon status
```

维护命令为 `run`、`start`、`status`、`stop`、`enable`、`disable`、`sync`；`sync` 可用 `--identity-id` 指定同步所用的拥有者或管理员身份。`listen-lark-events` 会按需启动并同步 daemon；`verify-lark-listener` 可能临时启动它，并在没有已启用工作区时于验证后停止。身份或看板配置变化后执行 `"$TF" daemon sync --workspace "$PROJECT_ROOT"`。同一 `brand + app_id` 只启动一个飞书 WebSocket worker，不同工作区按 `file_token + table_id` 路由事件。退出 UI 或事件流不会自动禁用工作区；停止长期监听用 `"$TF" daemon disable --workspace "$PROJECT_ROOT"`，停止全局进程用 `"$TF" daemon stop`。

## 10. 诊断

```bash
"$TF" inspect --workspace "$PROJECT_ROOT" --json
"$TF" inspect-agent-context --workspace "$PROJECT_ROOT" --all --json
"$TF" self-check
```

`inspect` 会顺带执行待应用的数据库迁移并同步协作模式定义，`--json` 输出已脱敏。`inspect-agent-context` 必须四选一：`--agent-id`、`--role`、`--session-id`、`--all`。`self-check` 在临时工作区跑完整配置冒烟，不接受 `--workspace`。

排查顺序建议：`inspect` 看工作区/身份/看板/Agent 状态 → `verify-lark-board` 看权限 → `daemon status` 与 `verify-lark-listener` 看事件链路 → `verify-agent` 看 Session 可用性。

## 边界

- 本 Skill 不创建、认领、评审或推进任何任务卡片。这些操作属于 `teamflow-agent`。
- 需要用户在飞书开放平台或 Codex 界面完成的授权步骤不能绕过，也不能用 CLI 伪造。
- 不要臆造命令或参数；不确定时先执行 `"$TF" COMMAND --help`。
