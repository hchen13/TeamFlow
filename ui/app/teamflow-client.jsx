"use client";

import { useEffect, useMemo, useState } from "react";
import { useFormStatus } from "react-dom";

const FEISHU_APP_URL = "https://open.feishu.cn/app";
const FEISHU_CREATE_APP_URL = "https://open.feishu.cn/page/launcher?from=backend_oneclick";
const LARK_APP_URL = "https://open.larksuite.com/app";
const LARK_CREATE_APP_URL = "https://open.larksuite.com/page/launcher?from=backend_oneclick";

const text = {
  zh: {
    brand: "TeamFlow 同舟",
    language: "EN",
    larkTab: "飞书",
    agentTab: "Agent",
    workspace: "Workspace",
    workflow: "Workflow",
    saved: "已保存",
    notConfigured: "未配置",
    larkTitle: "飞书",
    larkSubtitle: "先选择协作模式，再配置操作身份和协作看板。",
    workflowTitle: "协作模式",
    workflowSubtitle: "决定当前项目使用哪些角色和协作规则。",
    stepWorkflow: "Step 1",
    identityTitle: "身份",
    identitySubtitle: "Agent 在飞书里以什么身份操作。",
    stepIdentity: "Step 2",
    stepBoard: "Step 3",
    previousStep: "上一步",
    nextStep: "下一步",
    larkBoard: "多维表格",
    baseUrl: "多维表格 URL",
    baseUrlHint: "多维表格会作为 TeamFlow 的协作看板，用来保存任务、状态和操作记录。",
    boardName: "看板名称",
    createBoardWithIdentity: "用默认身份创建",
    accessMode: "身份",
    bot: "应用身份",
    user: "用户身份",
    botSummary: "创建一个飞书智能体应用；后续 TeamFlow 会以这个机器人身份在飞书中操作。",
    userSummary: "使用当前飞书账号授权；后续 TeamFlow 会以你的用户身份在飞书中操作。",
    appId: "App ID",
    appNameSyncedAt: "名称更新时间",
    appSecret: "App Secret",
    openPlatform: "开放平台",
    createApp: "一键创建",
    createAppQuestion: "还没有智能体应用？",
    createAppNote: "创建后，把 App ID 和 App Secret 复制回来。",
    botApps: "已保存应用身份",
    emptyBotApps: "还没有保存应用身份。",
    appNameMissing: "无法读取应用名称，需要开通应用信息权限。",
    appInfoIncomplete: "应用信息未完整读取，可能缺少权限，或应用还没有设置头像。",
    fixAppInfo: "点击这里设置",
    appNameUnknown: "应用名称未读取",
    defaultIdentity: "默认",
    openPermission: "开通权限",
    permissionScopes: "需要 admin:app.info:readonly 或 application:application:self_manage。",
    refresh: "刷新",
    setDefault: "设为默认",
    startAuth: "生成授权链接",
    openAuth: "打开授权页面",
    authReady: "授权链接已生成",
    authExpires: "有效期约 {seconds} 秒；页面后台正在等待授权完成。",
    saveIdentity: "保存身份",
    saveBoard: "保存多维表格",
    agentTitle: "Agent",
    agentSubtitle: "按当前 workflow 的预定义角色注册 session。",
    role: "角色",
    harness: "Harness",
    sessionId: "Session ID",
    displayName: "显示名",
    replaceRole: "替换该角色已有 agent",
    addAgent: "添加 Agent",
    cancel: "取消",
    register: "注册",
    remove: "移除",
    deleteIdentity: "删除",
    emptyTitle: "暂无 Agent",
    emptyAgents: "添加一个 session 后，它会按角色出现在这里。",
    status: "状态",
    name: "名称",
    currentWorkflow: "当前 Workflow",
    selected: "当前",
    roles: "角色",
    newAgent: "新增 Agent",
    session: "Session",
    configuredAgents: "已注册 Agent"
  },
  en: {
    brand: "TeamFlow",
    language: "中文",
    larkTab: "Lark",
    agentTab: "Agent",
    workspace: "Workspace",
    workflow: "Workflow",
    saved: "Saved",
    notConfigured: "Not configured",
    larkTitle: "Lark",
    larkSubtitle: "Choose the collaboration mode, then configure identity and board.",
    workflowTitle: "Collaboration mode",
    workflowSubtitle: "Controls the roles and collaboration rules for this project.",
    stepWorkflow: "Step 1",
    identityTitle: "Identity",
    identitySubtitle: "How agents operate inside Lark.",
    stepIdentity: "Step 2",
    stepBoard: "Step 3",
    previousStep: "Previous",
    nextStep: "Next",
    larkBoard: "Base",
    baseUrl: "Base URL",
    baseUrlHint: "The Base is the TeamFlow board for tasks, states, and activity history.",
    boardName: "Board name",
    createBoardWithIdentity: "Create with default identity",
    accessMode: "Identity",
    bot: "Bot",
    user: "User",
    botSummary: "Create a Lark bot app. TeamFlow will operate in Lark as that bot.",
    userSummary: "Authorize your current Lark account. TeamFlow will operate in Lark as your user identity.",
    appId: "App ID",
    appNameSyncedAt: "Name synced",
    appSecret: "App Secret",
    openPlatform: "Platform",
    createApp: "Create now",
    createAppQuestion: "No bot app yet?",
    createAppNote: "After creating it, paste the App ID and App Secret here.",
    botApps: "Saved bot identities",
    emptyBotApps: "No bot identities saved yet.",
    appNameMissing: "Cannot read the app name. Enable app info permissions.",
    appInfoIncomplete: "App information is incomplete. Permissions may be missing, or the app has no avatar.",
    fixAppInfo: "Open settings",
    appNameUnknown: "App name not read",
    defaultIdentity: "Default",
    openPermission: "Enable permissions",
    permissionScopes: "Requires admin:app.info:readonly or application:application:self_manage.",
    refresh: "Refresh",
    setDefault: "Set default",
    startAuth: "Generate auth link",
    openAuth: "Open authorization page",
    authReady: "Authorization link generated",
    authExpires: "Valid for about {seconds} seconds. The page is waiting for approval in the background.",
    saveIdentity: "Save identity",
    saveBoard: "Save Base",
    agentTitle: "Agent",
    agentSubtitle: "Register sessions against predefined roles in the selected workflow.",
    role: "Role",
    harness: "Harness",
    sessionId: "Session ID",
    displayName: "Display name",
    replaceRole: "Replace existing agents for this role",
    addAgent: "Add Agent",
    cancel: "Cancel",
    register: "Register",
    remove: "Remove",
    deleteIdentity: "Delete",
    emptyTitle: "No agents",
    emptyAgents: "Add a session and it will appear here under its role.",
    status: "Status",
    name: "Name",
    currentWorkflow: "Current Workflow",
    selected: "Selected",
    roles: "Roles",
    newAgent: "New Agent",
    session: "Session",
    configuredAgents: "Registered agents"
  }
};

export default function TeamFlowClient({ actions, authExpires, authUrl, currentRoles, error, initialLang, initialStep, initialTab, message, state }) {
  const [lang, setLang] = useState(initialLang === "en" ? "en" : "zh");
  const [tab, setTab] = useState(initialTab === "agent" ? "agent" : "lark");
  const [authMode, setAuthMode] = useState(authUrl ? "user" : state.lark_identities?.[0]?.auth_mode || "bot");
  const [agentFormOpen, setAgentFormOpen] = useState(false);
  const [noticeVisible, setNoticeVisible] = useState(Boolean(message));
  const t = text[lang];
  const board = state.lark_board || {};
  const botConnections = state.lark_identities?.filter((identity) => identity.auth_mode === "bot" && identity.app_id) || [];
  const currentWorkflow = state.current_workflow || state.workflows[0] || {};
  const tabMessage = message && ((tab === "agent") === (initialTab === "agent"));
  const roleOptions = useMemo(() => currentRoles, [currentRoles]);
  const appUrl = lang === "zh" ? FEISHU_APP_URL : LARK_APP_URL;
  const createAppUrl = lang === "zh" ? FEISHU_CREATE_APP_URL : LARK_CREATE_APP_URL;

  useEffect(() => {
    setNoticeVisible(Boolean(message));
    if (!message) {
      return undefined;
    }
    const timer = setTimeout(() => setNoticeVisible(false), 3600);
    return () => clearTimeout(timer);
  }, [message]);

  return (
    <main className="appShell">
      <aside className="rail">
        <div className="railBrand">
          <h1>{t.brand}</h1>
          <span>{state.current_workflow?.display_name || "Software Development"}</span>
        </div>
        <div className="workspaceBlock">
          <span>{t.workspace}</span>
          <code>{state.workspace_root}</code>
        </div>
        <TabNav tab={tab} setTab={setTab} t={t} />
      </aside>

      <section className="workbench">
        <header className="mobileTop">
          <h1>{t.brand}</h1>
          <button className="langButton" type="button" onClick={() => setLang(lang === "zh" ? "en" : "zh")}>
            {t.language}
          </button>
        </header>
        <nav className="mobileTabs">
          <TabNav tab={tab} setTab={setTab} t={t} />
        </nav>

        <header className="pageHeader">
          <div>
            <span className="eyebrow">TeamFlow</span>
            <h2>{tab === "lark" ? t.larkTitle : t.agentTitle}</h2>
            <p>{tab === "lark" ? t.larkSubtitle : t.agentSubtitle}</p>
          </div>
          <button className="langButton desktopLang" type="button" onClick={() => setLang(lang === "zh" ? "en" : "zh")}>
            {t.language}
          </button>
        </header>

        {noticeVisible && tabMessage ? <p className={error ? "banner error" : "banner"}>{message}</p> : null}

        {tab === "lark" ? (
          <LarkPanel
            actions={actions}
            appUrl={appUrl}
            createAppUrl={createAppUrl}
            authExpires={authExpires}
            authMode={authMode}
            authUrl={authUrl}
            board={board}
            botConnections={botConnections}
            currentWorkflow={currentWorkflow}
            initialStep={initialStep}
            lang={lang}
            setAuthMode={setAuthMode}
            state={state}
            t={t}
          />
        ) : (
          <AgentPanel
            actions={actions}
            agentFormOpen={agentFormOpen}
            currentRoles={currentRoles}
            currentWorkflow={currentWorkflow}
            lang={lang}
            roleOptions={roleOptions}
            setAgentFormOpen={setAgentFormOpen}
            state={state}
            t={t}
          />
        )}
      </section>
    </main>
  );
}

function TabNav({ tab, setTab, t }) {
  return (
    <div className="navStack">
      <button className={tab === "lark" ? "active" : ""} type="button" onClick={() => setTab("lark")}>
        {t.larkTab}
      </button>
      <button className={tab === "agent" ? "active" : ""} type="button" onClick={() => setTab("agent")}>
        {t.agentTab}
      </button>
    </div>
  );
}

function LarkPanel({ actions, appUrl, authExpires, authMode, authUrl, board, botConnections, currentWorkflow, createAppUrl, initialStep, lang, setAuthMode, state, t }) {
  const larkDomain = lang === "en" ? "larksuite" : "feishu";
  const hasIdentity = Boolean(state.lark_identities?.length);
  const [activeStep, setActiveStep] = useState(() => initialLarkStep(initialStep, hasIdentity));
  const boardName = defaultBoardName(state, lang);

  useEffect(() => {
    if (!hasIdentity && activeStep === "board") {
      setActiveStep("identity");
    }
  }, [activeStep, hasIdentity]);

  return (
    <div className="setupFlow">
      <nav className="setupSteps" aria-label={t.larkTitle}>
        <button className={activeStep === "workflow" ? "active" : ""} type="button" onClick={() => setActiveStep("workflow")}>
          <span>1</span>
          <strong>{t.workflowTitle}</strong>
        </button>
        <button className={activeStep === "identity" ? "active" : ""} type="button" onClick={() => setActiveStep("identity")}>
          <span>2</span>
          <strong>{t.identityTitle}</strong>
        </button>
        <button className={activeStep === "board" ? "active" : ""} disabled={!hasIdentity} type="button" onClick={() => setActiveStep("board")}>
          <span>3</span>
          <strong>{t.larkBoard}</strong>
        </button>
      </nav>

      <section className="panel mainPanel">
        {activeStep === "workflow" ? (
          <WorkflowStep actions={actions} currentWorkflow={currentWorkflow} lang={lang} state={state} t={t} />
        ) : activeStep === "identity" ? (
          <IdentityStep
            actions={actions}
            appUrl={appUrl}
            authExpires={authExpires}
            authMode={authMode}
            authUrl={authUrl}
            botConnections={botConnections}
            createAppUrl={createAppUrl}
            lang={lang}
            larkDomain={larkDomain}
            setAuthMode={setAuthMode}
            t={t}
          />
        ) : (
          <BoardStep actions={actions} board={board} boardName={boardName} canCreateBoard={botConnections.length > 0} lang={lang} larkDomain={larkDomain} t={t} />
        )}
        <StepFooter activeStep={activeStep} hasIdentity={hasIdentity} setActiveStep={setActiveStep} t={t} />
      </section>
    </div>
  );
}

function StepFooter({ activeStep, hasIdentity, setActiveStep, t }) {
  const previousStep = activeStep === "board" ? "identity" : activeStep === "identity" ? "workflow" : "";
  const nextStep = activeStep === "workflow" ? "identity" : activeStep === "identity" ? "board" : "";
  const canGoNext = activeStep === "workflow" || (activeStep === "identity" && hasIdentity);
  const className = previousStep && nextStep ? "stepFooter" : previousStep ? "stepFooter startOnly" : "stepFooter endOnly";
  return (
    <div className={className}>
      {previousStep ? (
        <button className="secondary" type="button" onClick={() => setActiveStep(previousStep)}>
          ← {t.previousStep}
        </button>
      ) : null}
      {nextStep ? (
        <button className="primary" disabled={!canGoNext} type="button" onClick={() => setActiveStep(nextStep)}>
          {t.nextStep} →
        </button>
      ) : null}
    </div>
  );
}

function WorkflowStep({ actions, currentWorkflow, lang, state, t }) {
  return (
    <div className="configStep">
      <div className="sectionHeader">
        <div>
          <span className="stepLabel">{t.stepWorkflow}</span>
          <h3>{t.workflowTitle}</h3>
          <p>{t.workflowSubtitle}</p>
        </div>
      </div>
      <div className="workflowCards">
        {state.workflows.map((workflow) => (
          <form action={actions.selectWorkflow} key={workflow.id}>
            <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
            <input name="step" type="hidden" value="workflow" suppressHydrationWarning />
            <input name="tab" type="hidden" value="lark" suppressHydrationWarning />
            <input name="workflow" type="hidden" value={workflow.key} suppressHydrationWarning />
            <button
              className={workflow.key === currentWorkflow.key ? "workflowCard active" : "workflowCard"}
              disabled={workflow.key === currentWorkflow.key}
              type="submit"
            >
              <span className="workflowCardHeader">
                <strong>{workflow.display_name}</strong>
                {workflow.key === currentWorkflow.key ? <em>{t.selected}</em> : null}
              </span>
              <span className="workflowSummary">{workflow.short_description || workflow.description}</span>
              <span className="workflowRoles">
                {state.roles.filter((role) => role.workflow_key === workflow.key).sort(roleSort).map((role) => (
                  <span className="workflowRole" key={role.id}>
                    <strong>{role.display_name}</strong>
                    <small>{role.description}</small>
                  </span>
                ))}
              </span>
            </button>
          </form>
        ))}
      </div>
    </div>
  );
}

function IdentityStep({ actions, appUrl, authExpires, authMode, authUrl, botConnections, createAppUrl, lang, larkDomain, setAuthMode, t }) {
  return (
    <div className="configStep">
      <div className="sectionHeader">
        <div>
          <span className="stepLabel">{t.stepIdentity}</span>
          <h3>{t.identityTitle}</h3>
          <p>{t.identitySubtitle}</p>
        </div>
      </div>

      <form action={actions.configureLark} className="stackForm">
        <input name="scope" type="hidden" value="identity" suppressHydrationWarning />
        <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
        <input name="lark_domain" type="hidden" value={larkDomain} suppressHydrationWarning />
        <input name="step" type="hidden" value="identity" suppressHydrationWarning />

        <fieldset>
          <legend>{t.accessMode}</legend>
          <div className="identitySwitch" role="radiogroup" aria-label={t.accessMode}>
            <label className={authMode === "bot" ? "selected" : ""}>
              <input checked={authMode === "bot"} name="auth_mode" onChange={() => setAuthMode("bot")} type="radio" value="bot" suppressHydrationWarning />
              <span>{t.bot}</span>
            </label>
            <label className={authMode === "user" ? "selected" : ""}>
              <input checked={authMode === "user"} name="auth_mode" onChange={() => setAuthMode("user")} type="radio" value="user" suppressHydrationWarning />
              <span>{t.user}</span>
            </label>
          </div>
        </fieldset>

        <p className="modeNote">{authMode === "bot" ? t.botSummary : t.userSummary}</p>

        {authMode === "bot" ? (
          <>
            <div className="linkStrip">
              <div>
                <strong>{t.openPlatform}</strong>
                <span>{t.createAppNote}</span>
              </div>
              <a className="linkButton" href={appUrl} rel="noreferrer" target="_blank">{t.openPlatform}</a>
            </div>
            <p className="inlineHint">
              {t.createAppQuestion} <a href={createAppUrl} rel="noreferrer" target="_blank">{t.createApp}</a>
            </p>
            <div className="twoCols">
              <label className="field">
                {t.appId}
                <input name="app_id" suppressHydrationWarning />
              </label>
              <label className="field">
                {t.appSecret}
                <input name="app_secret" type="password" suppressHydrationWarning />
              </label>
            </div>
            <div className="formFooter">
              <PendingSubmitButton className="primary" label={t.saveIdentity} />
            </div>
          </>
        ) : (
          <div className="authBlock">
            <button className="secondary" formAction={actions.startLarkUserAuth} type="submit">
              {t.startAuth}
            </button>
            {authUrl ? (
              <div className="authResult">
                <div>
                  <strong>{t.authReady}</strong>
                  <span>{t.authExpires.replace("{seconds}", authExpires || "600")}</span>
                </div>
                <a className="linkButton" href={authUrl} rel="noreferrer" target="_blank">{t.openAuth}</a>
              </div>
            ) : null}
          </div>
        )}
      </form>

      {authMode === "bot" ? (
        <div className="connectionList">
          <h4>{t.botApps}</h4>
          {botConnections.length ? (
            botConnections.map((connection) => (
              <BotConnectionRow actions={actions} connection={connection} key={connection.id} lang={lang} larkDomain={larkDomain} t={t} />
            ))
          ) : (
            <p className="emptyInline">{t.emptyBotApps}</p>
          )}
        </div>
      ) : null}
    </div>
  );
}

function PendingSubmitButton({ className, label }) {
  const { pending } = useFormStatus();
  return (
    <button className={pending ? `${className} pending` : className} disabled={pending} type="submit">
      {pending ? <span className="buttonSpinner" aria-hidden="true" /> : null}
      <span>{label}</span>
    </button>
  );
}

function BotConnectionRow({ actions, connection, lang, larkDomain, t }) {
  const hasName = Boolean(connection.app_name);
  const hasAvatar = Boolean(connection.app_avatar_url);
  return (
    <div className="connectionRow">
      <div className="connectionAvatar">
        {connection.app_avatar_url ? (
          <img alt="" src={connection.app_avatar_url} />
        ) : (
          <DefaultBotAvatar />
        )}
      </div>
      <div className="connectionMain">
        <div className="connectionTitle">
          <strong>{connection.app_name || t.appNameUnknown}</strong>
          {connection.is_default ? <span className="defaultMark">{t.defaultIdentity}</span> : null}
        </div>
        <span className="connectionMeta">{t.appId}: <code>{connection.app_id}</code></span>
        {hasName ? (
          <span>{t.appNameSyncedAt}: {shortDate(connection.app_name_synced_at)}</span>
        ) : (
          <p className="permissionHint">
            {t.appNameMissing} <a href={permissionUrl(connection.app_id, larkDomain)} rel="noreferrer" target="_blank">{t.openPermission}</a>
            <small>{t.permissionScopes}</small>
          </p>
        )}
        {hasName && !hasAvatar ? (
          <p className="appInfoWarning">
            <span title={t.appInfoIncomplete}>!</span>
            {t.appInfoIncomplete} <a href={permissionUrl(connection.app_id, larkDomain)} rel="noreferrer" target="_blank">{t.fixAppInfo}</a>
          </p>
        ) : null}
      </div>
      <div className="connectionActions">
        <form action={actions.refreshLarkIdentity}>
          <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
          <input name="lark_domain" type="hidden" value={larkDomain} suppressHydrationWarning />
          <input name="connection_id" type="hidden" value={connection.id} suppressHydrationWarning />
          <button className="secondary mini" type="submit">{t.refresh}</button>
        </form>
        {!connection.is_default ? (
          <form action={actions.setDefaultLarkIdentity}>
            <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
            <input name="connection_id" type="hidden" value={connection.id} suppressHydrationWarning />
            <button className="secondary mini" type="submit">{t.setDefault}</button>
          </form>
        ) : null}
        <form action={actions.removeLarkIdentity}>
          <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
          <input name="connection_id" type="hidden" value={connection.id} suppressHydrationWarning />
          <button className="ghost mini" type="submit">{t.deleteIdentity}</button>
        </form>
      </div>
    </div>
  );
}

function DefaultBotAvatar() {
  return (
    <svg aria-hidden="true" className="defaultBotAvatar" viewBox="0 0 1024 1024">
      <path
        d="M770.08 96.32c1.728 0.64 3.072 1.984 3.712 3.712l38.848 107.584c0.64 1.728 1.984 3.104 3.712 3.712l107.584 38.848a6.144 6.144 0 0 1 0 11.584l-107.584 38.848a6.144 6.144 0 0 0-3.712 3.712l-38.848 107.584a6.144 6.144 0 0 1-11.584 0L723.36 304.32a6.144 6.144 0 0 0-3.712-3.712L612.064 261.76a6.144 6.144 0 0 1 0-11.584l107.584-38.848a6.144 6.144 0 0 0 3.712-3.712l38.848-107.584c1.184-3.2 4.704-4.8 7.872-3.68zM576 160h-192Q264.704 160 180.352 244.352 96 328.704 96 448v192q0 119.296 84.352 203.648Q264.704 928 384 928h256q119.296 0 203.648-84.352Q928 759.296 928 640v-128h-64v128q0 92.8-65.6 158.4Q732.8 864 640 864h-256q-92.8 0-158.4-65.6Q160 732.8 160 640v-192q0-92.8 65.6-158.4Q291.2 224 384 224h192V160z m96 248.224L568.224 512 672 615.776l45.248-45.28L658.752 512l58.496-58.496L672 408.224zM320 608v-160h64v160h-64z"
        fill="currentColor"
      />
    </svg>
  );
}

function BoardStep({ actions, board, boardName, canCreateBoard, lang, larkDomain, t }) {
  const configured = Boolean(board.base_token || board.base_url);
  return (
    <form action={actions.configureLark} className="stackForm">
      <input name="scope" type="hidden" value="board" suppressHydrationWarning />
      <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
      <input name="lark_domain" type="hidden" value={larkDomain} suppressHydrationWarning />
      <input name="auth_mode" type="hidden" value="user" suppressHydrationWarning />
      <input name="step" type="hidden" value="board" suppressHydrationWarning />
      <div className="configStep">
        <div className="sectionHeader">
          <div>
            <span className="stepLabel">{t.stepBoard}</span>
            <h3>{t.larkBoard}</h3>
            <p>{t.baseUrlHint}</p>
          </div>
          <span className={configured ? "statusBadge saved" : "statusBadge"}>{configured ? t.saved : t.notConfigured}</span>
        </div>
        {canCreateBoard ? (
          <div className="boardCreate">
            <label className="field">
              {t.boardName}
              <input name="board_name" defaultValue={boardName} suppressHydrationWarning />
            </label>
            <button className="secondary" formAction={actions.createLarkBoard} type="submit">{t.createBoardWithIdentity}</button>
          </div>
        ) : null}
        <label className="field">
          {t.baseUrl}
          <input name="base_url" defaultValue={board.base_url || ""} placeholder="https://.../base/..." suppressHydrationWarning />
        </label>
        <div className="formFooter">
          <button className="primary" type="submit">{t.saveBoard}</button>
        </div>
      </div>
    </form>
  );
}

function AgentPanel({ actions, agentFormOpen, currentRoles, currentWorkflow, lang, roleOptions, setAgentFormOpen, state, t }) {
  return (
    <div className="contentGrid agentGrid">
      <section className="panel mainPanel">
        <div className="sectionHeader splitHeader">
          <div>
            <h3>{t.configuredAgents}</h3>
            <p>{t.agentSubtitle}</p>
          </div>
          <button className="primary compact" type="button" onClick={() => setAgentFormOpen(true)}>
            + {t.addAgent}
          </button>
        </div>

        <form action={actions.selectWorkflow} className="workflowStrip">
          <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
          <label className="field">
            {t.currentWorkflow}
            <select name="workflow" defaultValue={currentWorkflow.key} onChange={(event) => event.currentTarget.form?.requestSubmit()} suppressHydrationWarning>
              {state.workflows.map((workflow) => (
                <option key={workflow.id} value={workflow.key}>{workflow.display_name}</option>
              ))}
            </select>
          </label>
          <p>{currentWorkflow.short_description || currentWorkflow.description}</p>
        </form>

        {agentFormOpen ? (
          <form action={actions.registerAgent} className="agentEditor">
            <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
            <input name="workflow" type="hidden" value={currentWorkflow.key} suppressHydrationWarning />
            <div className="editorHeader">
              <h3>{t.newAgent}</h3>
              <button className="ghost" type="button" onClick={() => setAgentFormOpen(false)}>{t.cancel}</button>
            </div>
            <div className="twoCols">
              <label className="field">
                {t.role}
                <select name="role" suppressHydrationWarning>
                  {roleOptions.map((role) => (
                    <option key={role.id} value={role.role_key}>{role.display_name}</option>
                  ))}
                </select>
              </label>
              <label className="field">
                {t.harness}
                <input name="harness_type" placeholder="codex" suppressHydrationWarning />
              </label>
            </div>
            <label className="field">
              {t.sessionId}
              <input name="session_id" suppressHydrationWarning />
            </label>
            <label className="field">
              {t.displayName}
              <input name="display_name" suppressHydrationWarning />
            </label>
            <label className="checkLine">
              <input name="replace_role" type="checkbox" suppressHydrationWarning />
              {t.replaceRole}
            </label>
            <button className="primary" type="submit">{t.register}</button>
          </form>
        ) : null}

        <div className="agentTable">
          {state.agents.length ? (
            state.agents.map((agent) => (
              <div className="agentRow" key={agent.id}>
                <div>
                  <strong>{roleName(currentRoles, agent.role_key)}</strong>
                  <span>{agent.harness_type}</span>
                </div>
                <code>{agent.session_id}</code>
                <span>{agent.display_name || "-"}</span>
                <span className="statusBadge saved">{agent.status}</span>
                <form action={actions.unregisterAgent}>
                  <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
                  <input name="agent_id" type="hidden" value={agent.id} suppressHydrationWarning />
                  <button className="ghost" type="submit">{t.remove}</button>
                </form>
              </div>
            ))
          ) : (
            <div className="emptyState">
              <strong>{t.emptyTitle}</strong>
              <span>{t.emptyAgents}</span>
            </div>
          )}
        </div>
      </section>

      <aside className="sidePanel">
        <h3>{t.roles}</h3>
        <div className="roleList">
          {currentRoles.map((role) => (
            <div className="roleItem" key={role.id}>
              <strong>{role.display_name}</strong>
              <span>{role.allow_multiple ? "multi-agent" : "single-agent"}</span>
            </div>
          ))}
        </div>
      </aside>
    </div>
  );
}

function roleName(roles, key) {
  return roles.find((role) => role.role_key === key)?.display_name || key.toUpperCase();
}

function initialLarkStep(step, hasIdentity) {
  if (step === "workflow" || step === "identity") {
    return step;
  }
  if (step === "board" && hasIdentity) {
    return "board";
  }
  return hasIdentity ? "board" : "identity";
}

function roleSort(a, b) {
  const rank = { pm: 0, owner: 0, qa: 1, executor: 1, tl: 2, reviewer: 2, design: 3 };
  return (rank[a.role_key] ?? 99) - (rank[b.role_key] ?? 99) || a.role_key.localeCompare(b.role_key);
}

function defaultBoardName(state, lang) {
  const rootName = state.workspace_root?.split(/[\\/]/).filter(Boolean).at(-1) || "Project";
  const projectName = state.workspace?.display_name || rootName;
  return lang === "zh" ? `${projectName} 项目看板` : `${projectName} Project Board`;
}

function permissionUrl(appId, larkDomain) {
  const origin = larkDomain === "larksuite" ? "https://open.larksuite.com" : "https://open.feishu.cn";
  return `${origin}/app/${encodeURIComponent(appId)}/auth?q=admin:app.info:readonly,application:application:self_manage&op_from=openapi&token_type=tenant`;
}

function shortDate(value) {
  return value ? value.slice(0, 10) : "-";
}
