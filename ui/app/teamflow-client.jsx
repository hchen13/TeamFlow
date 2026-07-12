"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useFormStatus } from "react-dom";
import { useRouter } from "next/navigation";

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
    boardUrl: "多维表格 URL",
    boardUrlHint: "请先在飞书中创建一个多维表格，再将链接粘贴到这里。TeamFlow 会将它作为协作看板。",
    boardName: "看板名称",
    boardCreateHint: "TeamFlow 将使用当前默认身份创建一个新的多维表格。",
    boardCreatePrompt: "不想手动创建？",
    createBoardWithIdentity: "用默认身份创建",
    createBoard: "创建多维表格",
    verifyBoardUrl: "验证并使用",
    boardUrlVerified: "链接已验证",
    boardAccessVerified: "访问已验证",
    boardAccessChecking: "正在验证身份访问",
    boardAccessPending: "已保存，尚未验证",
    boardAccessFailed: "验证失败",
    boardUnavailableHint: "当前多维表格不可用。请更换链接，或用默认身份创建新表。",
    boardAccessPartial: "部分身份可用",
    boardAccessTitle: "身份访问",
    boardAccessSummary: "{verified} / {total} 个身份可用",
    verifyAllIdentities: "验证全部身份",
    reverifyAllIdentities: "全部重新验证",
    verifyingAllIdentities: "正在验证",
    accessIdentity: "身份",
    accessAuth: "认证",
    accessApi: "API 权限",
    accessCollaborator: "协作者",
    accessRead: "读取",
    accessWrite: "写入",
    accessCleanup: "清理",
    accessUnverified: "尚未检查",
    accessRunning: "正在检查",
    accessWaiting: "等待写入验证",
    accessPassed: "已通过",
    accessBlocked: "等待前置检查",
    accessFailed: "检查失败",
    primaryIdentity: "主身份",
    botIdentityType: "应用身份",
    userIdentityType: "用户身份",
    grantAccess: "授权访问",
    retryIdentity: "重新验证",
    accessMissingScope: "缺少多维表格 API 权限",
    accessNotCollaborator: "当前身份没有多维表格的 API 编辑权限",
    accessAuthExpired: "用户授权已过期",
    accessAuthFailed: "身份认证失败",
    accessReadFailed: "无法读取多维表格",
    accessWriteFailed: "可以访问，但无法完成写入验证",
    accessCleanupFailed: "测试记录未能清理",
    accessGenericFailed: "身份访问验证失败",
    requiredScopes: "需要权限",
    verificationStreamFailed: "无法完成身份访问验证，请重新检查。",
    openBoard: "打开多维表格",
    accessMode: "身份",
    bot: "应用身份",
    user: "用户身份",
    botSummary: "创建一个飞书智能体应用；后续 TeamFlow 会以这个机器人身份在飞书中操作。",
    userSummary: "使用当前飞书账号授权；后续 TeamFlow 会以你的用户身份在飞书中操作。",
    userAccounts: "已授权用户身份",
    emptyUserAccounts: "还没有完成用户授权。",
    userIdentityLabel: "飞书用户",
    checkAuth: "检查授权状态",
    reauthorize: "重新授权",
    connected: "已连接",
    expired: "已过期",
    disconnected: "未连接",
    lastVerified: "验证时间",
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
    agentTitle: "Agent",
    agentSubtitle: "连接当前协作模式需要的 Agent session，并检查它们是否可用。",
    role: "角色",
    harness: "Harness",
    sessionId: "Session",
    chooseSession: "选择当前项目的 Codex Session",
    manualSession: "手动输入 Session ID",
    chooseFromSessions: "从 Session 列表选择",
    noWorkspaceSessions: "当前项目没有可选 Session，请手动输入 ID。",
    sessionListUnavailable: "暂时无法读取 Codex Session，请手动输入 ID。",
    unnamedSession: "未命名 Session",
    displayName: "显示名（可选）",
    addAgent: "添加 Agent",
    cancel: "取消",
    register: "注册",
    save: "保存",
    switchSession: "切换",
    remove: "移除",
    deleteIdentity: "删除",
    emptyTitle: "暂无 Agent",
    emptyAgents: "添加一个 session，并把它分配给当前协作模式中的角色。",
    status: "状态",
    name: "名称",
    currentWorkflow: "当前 Workflow",
    selected: "当前",
    roles: "角色",
    newAgent: "新增 Agent",
    session: "Session",
    configuredAgents: "已注册 Agent",
    healthHealthy: "正常",
    healthActive: "正在运行",
    healthActiveHint: "此 Codex Session 正在处理任务。",
    healthIdle: "空闲",
    healthIdleHint: "此 Codex Session 已加载，当前空闲。",
    healthNotLoaded: "未加载",
    healthNotLoadedHint: "此 Codex Session 当前未被 Codex 客户端加载，但仍可使用。",
    healthArchived: "已归档",
    healthArchivedHint: "此 Codex Session 已归档；取消归档后会自动恢复。",
    healthDeleted: "已删除",
    healthDeletedHint: "Codex 中已找不到此 Session，建议移除此 Agent。",
    healthUnverified: "未检查",
    healthUnavailable: "连接失败",
    healthUnavailableHint: "TeamFlow 暂时无法连接 Codex app-server。",
    healthSystemError: "系统错误",
    healthSystemErrorHint: "Codex Session 遇到系统错误，请切换 Session 或移除此 Agent。",
    healthUnhealthy: "异常",
    healthCheckedAt: "检查时间",
    modelLabel: "模型",
    thinkingLabel: "Thinking",
    speedLabel: "速度",
    fast: "Fast",
    defaultValue: "默认",
    sessionSettingsUnavailable: "设置未加载",
    agentBusyActionHint: "Agent 正在工作，完成后才能切换或移除。",
    changeWorkflow: "在飞书设置中更改",
    registeredCount: "已注册 {count} 个",
    singleAgentRole: "此角色仅允许一个 Agent；注册后可在列表中切换 Session。",
    multiAgentRole: "此角色允许注册多个 Agent。"
  },
  en: {
    brand: "TeamFlow",
    language: "中文",
    larkTab: "Lark",
    agentTab: "Agent",
    workspace: "Workspace",
    workflow: "Workflow",
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
    larkBoard: "Bitable",
    boardUrl: "Bitable URL",
    boardUrlHint: "Create a Bitable in Lark, then paste its link here. TeamFlow will use it as the collaboration board.",
    boardName: "Board name",
    boardCreateHint: "TeamFlow will create a new Bitable with the current default identity.",
    boardCreatePrompt: "Prefer not to create one manually?",
    createBoardWithIdentity: "Create with default identity",
    createBoard: "Create Bitable",
    verifyBoardUrl: "Verify and use",
    boardUrlVerified: "Link verified",
    boardAccessVerified: "Access verified",
    boardAccessChecking: "Verifying identity access",
    boardAccessPending: "Saved, not verified",
    boardAccessFailed: "Verification failed",
    boardUnavailableHint: "The current Bitable is unavailable. Replace the link or create a new one with the default identity.",
    boardAccessPartial: "Some identities available",
    boardAccessTitle: "Identity access",
    boardAccessSummary: "{verified} of {total} identities available",
    verifyAllIdentities: "Verify all identities",
    reverifyAllIdentities: "Verify all again",
    verifyingAllIdentities: "Verifying",
    accessIdentity: "Identity",
    accessAuth: "Authentication",
    accessApi: "API access",
    accessCollaborator: "Collaborator",
    accessRead: "Read",
    accessWrite: "Write",
    accessCleanup: "Cleanup",
    accessUnverified: "Not checked",
    accessRunning: "Checking",
    accessWaiting: "Waiting for write check",
    accessPassed: "Passed",
    accessBlocked: "Waiting for prerequisite",
    accessFailed: "Failed",
    primaryIdentity: "Primary",
    botIdentityType: "Bot identity",
    userIdentityType: "User identity",
    grantAccess: "Grant access",
    retryIdentity: "Verify again",
    accessMissingScope: "Required Bitable API permissions are missing",
    accessNotCollaborator: "This identity does not have API edit access to the Bitable",
    accessAuthExpired: "User authorization has expired",
    accessAuthFailed: "Identity authentication failed",
    accessReadFailed: "Cannot read this Bitable",
    accessWriteFailed: "Access works, but the write check failed",
    accessCleanupFailed: "The verification record could not be removed",
    accessGenericFailed: "Identity access verification failed",
    requiredScopes: "Required permissions",
    verificationStreamFailed: "Identity access verification could not finish. Try again.",
    openBoard: "Open Bitable",
    accessMode: "Identity",
    bot: "Bot",
    user: "User",
    botSummary: "Create a Lark bot app. TeamFlow will operate in Lark as that bot.",
    userSummary: "Authorize your current Lark account. TeamFlow will operate in Lark as your user identity.",
    userAccounts: "Authorized user identities",
    emptyUserAccounts: "No user authorization has been completed.",
    userIdentityLabel: "Lark user",
    checkAuth: "Check authorization",
    reauthorize: "Authorize again",
    connected: "Connected",
    expired: "Expired",
    disconnected: "Not connected",
    lastVerified: "Verified",
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
    agentTitle: "Agent",
    agentSubtitle: "Connect the agent sessions required by the current collaboration mode and verify their availability.",
    role: "Role",
    harness: "Harness",
    sessionId: "Session",
    chooseSession: "Choose a Codex session in this project",
    manualSession: "Enter a session ID manually",
    chooseFromSessions: "Choose from session list",
    noWorkspaceSessions: "No sessions were found for this project. Enter an ID manually.",
    sessionListUnavailable: "Codex sessions are temporarily unavailable. Enter an ID manually.",
    unnamedSession: "Unnamed session",
    displayName: "Display name (optional)",
    addAgent: "Add Agent",
    cancel: "Cancel",
    register: "Register",
    save: "Save",
    switchSession: "Switch",
    remove: "Remove",
    deleteIdentity: "Delete",
    emptyTitle: "No agents",
    emptyAgents: "Add a session and assign it to a role in the current collaboration mode.",
    status: "Status",
    name: "Name",
    currentWorkflow: "Current Workflow",
    selected: "Selected",
    roles: "Roles",
    newAgent: "New Agent",
    session: "Session",
    configuredAgents: "Registered agents",
    healthHealthy: "Ready",
    healthActive: "Active",
    healthActiveHint: "This Codex session is currently working.",
    healthIdle: "Idle",
    healthIdleHint: "This Codex session is loaded and idle.",
    healthNotLoaded: "Not loaded",
    healthNotLoadedHint: "This Codex session is not loaded by a client, but remains available.",
    healthArchived: "Archived",
    healthArchivedHint: "This Codex session is archived. It will recover automatically after unarchiving.",
    healthDeleted: "Deleted",
    healthDeletedHint: "Codex can no longer find this session. Remove this agent assignment.",
    healthUnverified: "Not checked",
    healthUnavailable: "Connection failed",
    healthUnavailableHint: "TeamFlow cannot currently connect to Codex app-server.",
    healthSystemError: "System error",
    healthSystemErrorHint: "This Codex session hit a system error. Switch sessions or remove this agent.",
    healthUnhealthy: "Unhealthy",
    healthCheckedAt: "Checked",
    modelLabel: "Model",
    thinkingLabel: "Thinking",
    speedLabel: "Speed",
    fast: "Fast",
    defaultValue: "Default",
    sessionSettingsUnavailable: "Settings not loaded",
    agentBusyActionHint: "This agent is working. Wait until it finishes before switching or removing it.",
    changeWorkflow: "Change in Lark setup",
    registeredCount: "{count} registered",
    singleAgentRole: "This role allows one agent. Switch its session from the registered-agent list.",
    multiAgentRole: "This role allows multiple agents."
  }
};

export default function TeamFlowClient({ actions, authExpires, authUrl, boardUrlDraft, codexSessionError, codexSessions, currentRoles, error, initialAuthMode, initialLang, initialStep, initialTab, message, state }) {
  const [lang, setLang] = useState(initialLang === "en" ? "en" : "zh");
  const [tab, setTab] = useState(initialTab === "agent" ? "agent" : "lark");
  const [authMode, setAuthMode] = useState(initialAuthMode === "user" || authUrl ? "user" : state.lark_identities?.[0]?.auth_mode || "bot");
  const [agentFormOpen, setAgentFormOpen] = useState(false);
  const [noticeVisible, setNoticeVisible] = useState(Boolean(message));
  const [liveAgents, setLiveAgents] = useState(state.agents || []);
  const [liveCodexSessions, setLiveCodexSessions] = useState(codexSessions);
  const [liveCodexSessionError, setLiveCodexSessionError] = useState(codexSessionError);
  const [runtimeBySession, setRuntimeBySession] = useState({});
  const [lifecycleBySession, setLifecycleBySession] = useState({});
  const refreshInFlight = useRef(null);
  const t = text[lang];
  const board = state.lark_board || {};
  const botIdentities = state.lark_identities?.filter((identity) => identity.auth_mode === "bot" && identity.app_id) || [];
  const userIdentities = state.lark_identities?.filter((identity) => identity.auth_mode === "user") || [];
  const currentWorkflow = state.current_workflow || state.workflows[0] || {};
  const currentAgents = liveAgents.filter((agent) => agent.workflow_key === currentWorkflow.key);
  const tabMessage = message && ((tab === "agent") === (initialTab === "agent"));
  const appUrl = lang === "zh" ? FEISHU_APP_URL : LARK_APP_URL;
  const createAppUrl = lang === "zh" ? FEISHU_CREATE_APP_URL : LARK_CREATE_APP_URL;

  const refreshCodexState = useCallback(() => {
    if (refreshInFlight.current) {
      return refreshInFlight.current;
    }
    const request = fetch("/api/codex", { method: "POST" })
      .then((response) => {
        if (!response.ok) {
          throw new Error(`Codex refresh failed: ${response.status}`);
        }
        return response.json();
      })
      .then((payload) => {
        const agents = payload.agents || [];
        setLiveAgents(agents);
        setLiveCodexSessions(payload.sessions || []);
        setLiveCodexSessionError(Boolean(payload.sessionError));
        setRuntimeBySession(sessionMap(payload.runtime?.sessions || []));
        setLifecycleBySession((current) => settledLifecycle(current, agents));
      })
      .catch(() => setLiveCodexSessionError(true))
      .finally(() => {
        refreshInFlight.current = null;
      });
    refreshInFlight.current = request;
    return request;
  }, []);

  useEffect(() => setLiveAgents(state.agents || []), [state.agents]);
  useEffect(() => setLiveCodexSessions(codexSessions), [codexSessions]);
  useEffect(() => setLiveCodexSessionError(codexSessionError), [codexSessionError]);

  useEffect(() => {
    if (tab !== "agent") {
      return undefined;
    }
    refreshCodexState();
    const source = new EventSource("/api/codex");
    source.onmessage = ({ data }) => {
      let event;
      try {
        event = JSON.parse(data);
      } catch {
        return;
      }
      if (event.type === "snapshot") {
        setRuntimeBySession(sessionMap(event.sessions || []));
      } else if (event.type === "runtime") {
        setRuntimeBySession((current) => updateRuntime(current, event));
        if (event.status === "systemError") {
          refreshCodexState();
        }
      } else if (event.type === "lifecycle") {
        setLifecycleBySession((current) => ({ ...current, [event.threadId]: event.status }));
        refreshCodexState();
      } else if (event.type === "catalog") {
        refreshCodexState();
      } else if (event.type === "bridge" && !event.connected) {
        setRuntimeBySession({});
      }
    };
    return () => source.close();
  }, [refreshCodexState, tab]);

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
            boardAccess={state.lark_board_access || []}
            boardUrlDraft={boardUrlDraft}
            botIdentities={botIdentities}
            userIdentities={userIdentities}
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
            agents={currentAgents}
            codexSessionError={liveCodexSessionError}
            codexSessions={liveCodexSessions}
            currentRoles={currentRoles}
            currentWorkflow={currentWorkflow}
            lifecycleBySession={lifecycleBySession}
            lang={lang}
            refreshCodexState={refreshCodexState}
            runtimeBySession={runtimeBySession}
            setAgentFormOpen={setAgentFormOpen}
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

function LarkPanel({ actions, appUrl, authExpires, authMode, authUrl, board, boardAccess, boardUrlDraft, botIdentities, currentWorkflow, createAppUrl, initialStep, lang, setAuthMode, state, t, userIdentities }) {
  const larkDomain = lang === "en" ? "larksuite" : "feishu";
  const hasIdentity = botIdentities.length > 0 || userIdentities.some((identity) => identity.access_status === "verified");
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
            botIdentities={botIdentities}
            createAppUrl={createAppUrl}
            lang={lang}
            larkDomain={larkDomain}
            setAuthMode={setAuthMode}
            t={t}
            userIdentities={userIdentities}
          />
        ) : (
          <BoardStep
            actions={actions}
            board={board}
            boardAccess={boardAccess}
            boardName={boardName}
            boardUrlDraft={boardUrlDraft}
            canCreateBoard={hasIdentity}
            identities={[...userIdentities, ...botIdentities]}
            lang={lang}
            larkDomain={larkDomain}
            t={t}
          />
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

function IdentityStep({ actions, appUrl, authExpires, authMode, authUrl, botIdentities, createAppUrl, lang, larkDomain, setAuthMode, t, userIdentities }) {
  return (
    <div className="configStep">
      <div className="sectionHeader">
        <div>
          <span className="stepLabel">{t.stepIdentity}</span>
          <h3>{t.identityTitle}</h3>
          <p>{t.identitySubtitle}</p>
        </div>
      </div>

      <form action={actions.configureLarkIdentity} className="stackForm">
        <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
        <input name="lark_domain" type="hidden" value={larkDomain} suppressHydrationWarning />

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
            <div className="authActions">
              <PendingActionButton action={actions.startLarkUserAuth} className="secondary" label={userIdentities.length ? t.reauthorize : t.startAuth} />
              <PendingActionButton action={actions.verifyLarkUserIdentity} className="primary" label={t.checkAuth} />
            </div>
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
          {botIdentities.length ? (
            botIdentities.map((identity) => (
              <BotIdentityRow actions={actions} identity={identity} key={identity.id} lang={lang} larkDomain={larkDomain} t={t} />
            ))
          ) : (
            <p className="emptyInline">{t.emptyBotApps}</p>
          )}
        </div>
      ) : (
        <div className="connectionList">
          <h4>{t.userAccounts}</h4>
          {userIdentities.length ? (
            userIdentities.map((identity) => (
              <UserIdentityRow actions={actions} identity={identity} key={identity.id} lang={lang} t={t} />
            ))
          ) : (
            <p className="emptyInline">{t.emptyUserAccounts}</p>
          )}
        </div>
      )}
    </div>
  );
}

function PendingSubmitButton({ className, disabled = false, label, title }) {
  const { pending } = useFormStatus();
  return (
    <button className={pending ? `${className} pending` : className} disabled={pending || disabled} title={title} type="submit">
      {pending ? <span className="buttonSpinner" aria-hidden="true" /> : null}
      <span>{label}</span>
    </button>
  );
}

function PendingActionButton({ action, className, label }) {
  const { action: pendingAction, pending } = useFormStatus();
  const active = pending && pendingAction === action;
  return (
    <button className={active ? `${className} pending` : className} disabled={pending} formAction={action} type="submit">
      {active ? <span className="buttonSpinner" aria-hidden="true" /> : null}
      <span>{label}</span>
    </button>
  );
}

function BotIdentityRow({ actions, identity, lang, larkDomain, t }) {
  const hasName = Boolean(identity.app_name);
  const hasAvatar = Boolean(identity.app_avatar_url);
  return (
    <div className="connectionRow">
      <div className="connectionAvatar">
        {identity.app_avatar_url ? (
          <img alt="" src={identity.app_avatar_url} />
        ) : (
          <DefaultBotAvatar />
        )}
      </div>
      <div className="connectionMain">
        <div className="connectionTitle">
          <strong>{identity.app_name || t.appNameUnknown}</strong>
          {identity.is_default ? <span className="defaultMark">{t.defaultIdentity}</span> : null}
        </div>
        <span className="connectionMeta">{t.appId}: <code>{identity.app_id}</code></span>
        {hasName ? (
          <span>{t.appNameSyncedAt}: {shortDate(identity.app_name_synced_at)}</span>
        ) : (
          <p className="permissionHint">
            {t.appNameMissing} <a href={permissionUrl(identity.app_id, larkDomain)} rel="noreferrer" target="_blank">{t.openPermission}</a>
            <small>{t.permissionScopes}</small>
          </p>
        )}
        {hasName && !hasAvatar ? (
          <p className="appInfoWarning">
            <span title={t.appInfoIncomplete}>!</span>
            {t.appInfoIncomplete} <a href={permissionUrl(identity.app_id, larkDomain)} rel="noreferrer" target="_blank">{t.fixAppInfo}</a>
          </p>
        ) : null}
      </div>
      <div className="connectionActions">
        <form action={actions.refreshLarkIdentity}>
          <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
          <input name="lark_domain" type="hidden" value={larkDomain} suppressHydrationWarning />
          <input name="identity_id" type="hidden" value={identity.id} suppressHydrationWarning />
          <button className="secondary mini" type="submit">{t.refresh}</button>
        </form>
        {!identity.is_default ? (
          <form action={actions.setDefaultLarkIdentity}>
            <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
            <input name="identity_id" type="hidden" value={identity.id} suppressHydrationWarning />
            <button className="secondary mini" type="submit">{t.setDefault}</button>
          </form>
        ) : null}
        <form action={actions.removeLarkIdentity}>
          <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
          <input name="identity_id" type="hidden" value={identity.id} suppressHydrationWarning />
          <button className="ghost mini" type="submit">{t.deleteIdentity}</button>
        </form>
      </div>
    </div>
  );
}

function UserIdentityRow({ actions, identity, lang, t }) {
  const verified = identity.access_status === "verified";
  const status = verified ? t.connected : identity.access_status === "expired" ? t.expired : t.disconnected;
  const initial = Array.from(identity.user_name || "U")[0];
  return (
    <div className="connectionRow userConnectionRow">
      <div className="connectionAvatar userIdentityAvatar" aria-hidden="true">
        {identity.user_avatar_url ? <img alt="" src={identity.user_avatar_url} /> : <span>{initial}</span>}
      </div>
      <div className="connectionMain userConnectionMain">
        <div className="connectionTitle userConnectionTitle">
          <strong>{identity.user_name || t.userIdentityLabel}</strong>
          {identity.is_default ? <span className="defaultMark">{t.defaultIdentity}</span> : null}
          <span className={verified ? "statusBadge compact saved" : "statusBadge compact"}>{status}</span>
        </div>
        <span className="userIdentityMeta">{t.lastVerified}: {shortDate(identity.last_verified_at)}</span>
      </div>
      <div className="connectionActions">
        <form action={actions.verifyLarkUserIdentity}>
          <input name="intent" type="hidden" value="refresh" suppressHydrationWarning />
          <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
          <PendingSubmitButton className="secondary mini" label={t.refresh} />
        </form>
        {verified && !identity.is_default ? (
          <form action={actions.setDefaultLarkIdentity}>
            <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
            <input name="identity_id" type="hidden" value={identity.id} suppressHydrationWarning />
            <button className="secondary mini" type="submit">{t.setDefault}</button>
          </form>
        ) : null}
        <form action={actions.removeLarkIdentity}>
          <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
          <input name="identity_id" type="hidden" value={identity.id} suppressHydrationWarning />
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

function BoardStep({ actions, board, boardAccess, boardName, boardUrlDraft, canCreateBoard, identities, lang, larkDomain, t }) {
  const router = useRouter();
  const configured = Boolean(board.base_token || board.base_url);
  const [boardUrl, setBoardUrl] = useState(boardUrlDraft || board.base_url || "");
  const [accessRows, setAccessRows] = useState(() => initialAccessRows(identities, boardAccess));
  const [boardStatus, setBoardStatus] = useState(board.access_status || "unverified");
  const [verificationError, setVerificationError] = useState("");
  const [verificationRunning, setVerificationRunning] = useState(
    configured && board.access_status === "unverified" && identities.length > 0
  );
  const verificationInFlight = useRef(false);
  const verificationAbort = useRef(null);
  const autoVerificationKey = useRef("");
  const savedUrl = configured && boardUrl.trim() === board.base_url;
  const verifiedCount = accessRows.filter((row) => row.status === "verified").length;
  const boardUnavailable = boardStatus === "unavailable";
  const urlVerified = savedUrl && !boardUnavailable && verifiedCount > 0;
  const accessLabel = verificationRunning
    ? t.boardAccessChecking
    : verifiedCount === identities.length && identities.length
      ? t.boardAccessVerified
      : verifiedCount
        ? t.boardAccessPartial
        : boardStatus === "unavailable"
          ? t.boardAccessFailed
          : t.boardAccessPending;
  const identityKey = identities.map((identity) => identity.id).join(",");
  const accessKey = boardAccess.map((access) => `${access.identity_id}:${access.last_verified_at || ""}`).join(",");

  useEffect(() => {
    if (!boardUrlDraft && board.base_url) {
      setBoardUrl(board.base_url);
    }
  }, [board.base_url, boardUrlDraft]);

  useEffect(() => {
    setAccessRows(initialAccessRows(identities, boardAccess));
    setBoardStatus(board.access_status || "unverified");
  }, [accessKey, board.access_status, board.id, identityKey]);

  const runVerification = useCallback(async (identityId = "") => {
    if (verificationInFlight.current) {
      return;
    }
    verificationInFlight.current = true;
    setVerificationRunning(true);
    setVerificationError("");
    const controller = new AbortController();
    verificationAbort.current = controller;
    let streamError = "";
    let completed = false;
    try {
      const response = await fetch("/api/lark/board-access", {
        method: "POST",
        body: JSON.stringify(identityId ? { identity_id: identityId } : {}),
        headers: { "Content-Type": "application/json" },
        signal: controller.signal
      });
      if (!response.ok || !response.body) {
        throw new Error(`Verification request failed: ${response.status}`);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          if (!line.trim()) {
            continue;
          }
          const event = JSON.parse(line);
          if (event.type === "verification_error") {
            streamError = event.error || "Verification failed";
          } else if (event.type === "verification_completed") {
            setBoardStatus(event.access_status || "unverified");
          }
          setAccessRows((current) => applyAccessEvent(current, event));
        }
        if (done) {
          break;
        }
      }
      if (streamError) {
        throw new Error(streamError);
      }
      completed = true;
    } catch (error) {
      if (error.name !== "AbortError") {
        setVerificationError(error.message || String(error));
        setAccessRows(markInterruptedAccess);
      }
    } finally {
      verificationAbort.current = null;
      verificationInFlight.current = false;
      setVerificationRunning(false);
    }
    if (completed) {
      router.refresh();
    }
  }, [router]);

  useEffect(() => () => verificationAbort.current?.abort(), []);

  useEffect(() => {
    const key = `${board.id || ""}:${board.updated_at || ""}`;
    if (!configured || board.access_status !== "unverified" || !identities.length || autoVerificationKey.current === key) {
      return undefined;
    }
    const timer = setTimeout(() => {
      autoVerificationKey.current = key;
      runVerification();
    }, 0);
    return () => clearTimeout(timer);
  }, [board.access_status, board.id, board.updated_at, configured, identities.length, runVerification]);

  return (
    <div className="stackForm">
      <div className="configStep">
        <div className="sectionHeader">
          <div>
            <span className="stepLabel">{t.stepBoard}</span>
            <h3>{t.larkBoard}</h3>
            <p>{t.boardUrlHint}</p>
          </div>
        </div>
        <form action={actions.configureLarkBoard} className="boardUrlRow">
          <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
          <input name="lark_domain" type="hidden" value={larkDomain} suppressHydrationWarning />
          <label className="field boardUrlField">
            {t.boardUrl}
            <span className="boardUrlInput">
              <input
                name="board_url"
                onChange={(event) => setBoardUrl(event.target.value)}
                placeholder="https://.../base/..."
                value={boardUrl}
                suppressHydrationWarning
              />
              {urlVerified ? (
                <span aria-label={t.boardUrlVerified} className="boardUrlCheck" tabIndex={0}>
                  <AccessStatusIcon status="passed" />
                  <span className="boardUrlTooltip" role="tooltip">{t.boardUrlVerified}</span>
                </span>
              ) : null}
            </span>
          </label>
          <PendingSubmitButton className="primary compact boardUrlSubmit" disabled={verificationRunning} label={t.verifyBoardUrl} />
        </form>
        {boardUnavailable ? <p className="accessStreamError">{t.boardUnavailableHint}</p> : null}
        {savedUrl && !boardUnavailable ? (
          <>
            <div className="boardUrlActions">
            <span className={`statusBadge compact ${urlVerified ? "saved" : boardStatus === "unavailable" ? "error" : ""}`}>
              {accessLabel}
            </span>
              <a href={board.base_url} rel="noreferrer" target="_blank">{t.openBoard} ↗</a>
            </div>
            <BoardAccessMatrix
              actions={actions}
              board={board}
              identities={identities}
              lang={lang}
              onVerify={runVerification}
              rows={accessRows}
              running={verificationRunning}
              t={t}
            />
            {verificationError ? <p className="accessStreamError">{t.verificationStreamFailed}</p> : null}
          </>
        ) : null}
        {canCreateBoard ? (
          <details className="boardCreateDisclosure">
            <summary><span>{t.boardCreatePrompt}</span> <strong>{t.createBoardWithIdentity}</strong></summary>
            <p>{t.boardCreateHint}</p>
            <form action={actions.createLarkBoard} className="boardCreate">
              <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
              <input name="lark_domain" type="hidden" value={larkDomain} suppressHydrationWarning />
              <label className="field">
                {t.boardName}
                <input name="board_name" defaultValue={boardName} suppressHydrationWarning />
              </label>
              <PendingSubmitButton className="secondary" label={t.createBoard} />
            </form>
          </details>
        ) : null}
      </div>
    </div>
  );
}

function BoardAccessMatrix({ actions, board, identities, lang, onVerify, rows, running, t }) {
  const verified = rows.filter((row) => row.status === "verified").length;
  const hasChecked = rows.some((row) => row.last_verified_at);
  const canGrant = rows.some((row) => row.status === "verified");
  const labels = [t.accessAuth, t.accessApi, t.accessCollaborator, t.accessRead, t.accessWrite, t.accessCleanup];
  return (
    <section className="accessMatrixSection">
      <header className="accessMatrixTitle">
        <div>
          <h4>{t.boardAccessTitle}</h4>
          <span>{t.boardAccessSummary.replace("{verified}", verified).replace("{total}", rows.length)}</span>
        </div>
        <button className="secondary mini accessRefresh" disabled={running} type="button" onClick={() => onVerify()}>
          {running ? <span className="buttonSpinner" aria-hidden="true" /> : <RefreshIcon />}
          {running ? t.verifyingAllIdentities : hasChecked ? t.reverifyAllIdentities : t.verifyAllIdentities}
        </button>
      </header>
      <div className="accessMatrix">
        <div className="accessMatrixHeader" aria-hidden="true">
          <span>{t.accessIdentity}</span>
          <span className="accessCheckHeaders">{labels.map((label) => <span key={label}>{label}</span>)}</span>
        </div>
        {rows.map((row) => (
          <BoardAccessRow
            actions={actions}
            board={board}
            canGrant={canGrant}
            key={row.id}
            lang={lang}
            onVerify={onVerify}
            row={row}
            running={running}
            t={t}
          />
        ))}
      </div>
    </section>
  );
}

function BoardAccessRow({ actions, board, canGrant, lang, onVerify, row, running, t }) {
  const [expanded, setExpanded] = useState(false);
  const checks = ["auth", "api", "collaborator", "read", "write", "cleanup"];
  const labels = {
    auth: t.accessAuth,
    api: t.accessApi,
    collaborator: t.accessCollaborator,
    read: t.accessRead,
    write: t.accessWrite,
    cleanup: t.accessCleanup
  };
  const failureMessage = accessFailureMessage(row.failure_kind, t);
  const isBot = row.auth_mode === "bot";
  return (
    <div className={expanded ? "accessIdentityRow expanded" : "accessIdentityRow"}>
      <div className="accessIdentityCell">
        <IdentityAccessAvatar identity={row} />
        <span className="accessIdentityText">
          <span className="accessIdentityName">
            <strong>{row.app_name || row.user_name || row.app_id || t.userIdentityLabel}</strong>
            {board.primary_identity_id === row.id ? <em>{t.primaryIdentity}</em> : null}
            {row.is_default ? <em>{t.defaultIdentity}</em> : null}
          </span>
          <small>{isBot ? `${t.botIdentityType} · ${row.app_id}` : t.userIdentityType}</small>
        </span>
      </div>
      <div className="accessChecks">
        {checks.map((check) => (
          <AccessCheck
            check={check}
            key={check}
            label={labels[check]}
            onFailure={() => setExpanded((value) => !value)}
            status={row.checks[check] || "unverified"}
            t={t}
            tooltip={row.checks[check] === "failed" ? failureMessage : ""}
          />
        ))}
      </div>
      {expanded && row.status === "failed" ? (
        <div className="accessRepairPanel">
          <div className="accessRepairMessage">
            <strong>{failureMessage}</strong>
            {row.missing_scopes?.length ? (
              <span className="scopeList">
                <small>{t.requiredScopes}</small>
                {row.missing_scopes.map((scope) => <code key={scope}>{scope}</code>)}
              </span>
            ) : null}
          </div>
          <div className="accessRepairActions">
            {row.failure_kind === "missing_scope" && row.repair_url ? (
              <a className="secondary mini" href={row.repair_url} rel="noreferrer" target="_blank">{t.openPermission}</a>
            ) : null}
            {!isBot && ["auth_expired", "missing_scope"].includes(row.failure_kind) ? (
              <form action={actions.startLarkUserAuth}>
                <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
                <PendingSubmitButton className="secondary mini" label={t.reauthorize} />
              </form>
            ) : null}
            {row.failure_kind === "not_collaborator" && canGrant ? (
              <form action={actions.grantLarkBoardAccess}>
                <input name="identity_id" type="hidden" value={row.id} suppressHydrationWarning />
                <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
                <PendingSubmitButton className="primary mini" label={t.grantAccess} />
              </form>
            ) : null}
            {row.failure_kind === "not_collaborator" ? (
              <a className="secondary mini" href={board.base_url} rel="noreferrer" target="_blank">{t.openBoard}</a>
            ) : null}
            <button className="secondary mini" disabled={running} type="button" onClick={() => onVerify(row.id)}>{t.retryIdentity}</button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function AccessCheck({ label, onFailure, status, t, tooltip }) {
  const statusLabel = accessStatusLabel(status, t);
  const content = (
    <>
      <span className="accessMobileLabel">{label}</span>
      <AccessStatusIcon status={status} />
      <span className="accessTooltip" role="tooltip">{tooltip || statusLabel}</span>
    </>
  );
  return status === "failed" ? (
    <button aria-label={`${label}: ${tooltip || statusLabel}`} className="accessCheck failed" type="button" onClick={onFailure}>{content}</button>
  ) : (
    <span aria-label={`${label}: ${statusLabel}`} className={`accessCheck ${status}`} tabIndex={0}>{content}</span>
  );
}

function AccessStatusIcon({ status }) {
  if (["running", "waiting"].includes(status)) {
    return <span className="accessSpinner" aria-hidden="true" />;
  }
  if (status === "passed") {
    return <svg aria-hidden="true" viewBox="0 0 20 20"><path d="m5 10 3 3 7-7" /></svg>;
  }
  if (status === "failed") {
    return <svg aria-hidden="true" viewBox="0 0 20 20"><path d="m6 6 8 8M14 6l-8 8" /></svg>;
  }
  if (status === "blocked") {
    return <svg aria-hidden="true" viewBox="0 0 20 20"><path d="M6 10h8" /></svg>;
  }
  return <span className="accessUnverifiedDot" aria-hidden="true" />;
}

function IdentityAccessAvatar({ identity }) {
  const image = identity.auth_mode === "bot" ? identity.app_avatar_url : identity.user_avatar_url;
  const initial = Array.from(identity.user_name || "U")[0];
  return (
    <span className={identity.auth_mode === "bot" ? "accessAvatar" : "accessAvatar user"}>
      {image ? <img alt="" src={image} /> : identity.auth_mode === "bot" ? <DefaultBotAvatar /> : <span>{initial}</span>}
    </span>
  );
}

function RefreshIcon() {
  return <svg aria-hidden="true" className="refreshIcon" viewBox="0 0 20 20"><path d="M15 7a6 6 0 1 0 .4 5M15 3v4h-4" /></svg>;
}

function AgentPanel({ actions, agentFormOpen, agents, codexSessionError, codexSessions, currentRoles, currentWorkflow, lifecycleBySession, lang, refreshCodexState, runtimeBySession, setAgentFormOpen, t }) {
  const [selectedRoleKey, setSelectedRoleKey] = useState("");
  const [editingAgentId, setEditingAgentId] = useState("");
  const selectableSessions = codexSessions.filter((session) => session.status !== "systemError");
  const availableRoles = currentRoles.filter((role) => role.allow_multiple || !agents.some((agent) => agent.role_key === role.role_key));
  const effectiveRoleKey = availableRoles.some((role) => role.role_key === selectedRoleKey)
    ? selectedRoleKey
    : availableRoles[0]?.role_key || "";
  const selectedRole = availableRoles.find((role) => role.role_key === effectiveRoleKey);

  return (
    <div className="agentPage">
      <section className="agentContext">
        <div className="agentContextMode">
          <span>{t.workflowTitle}</span>
          <strong>{currentWorkflow.display_name || currentWorkflow.key}</strong>
        </div>
        <div className="agentRoleScope">
          <span>{t.roles}</span>
          <strong>{currentRoles.map((role) => role.display_name).join(" · ") || "-"}</strong>
        </div>
        <a className="agentContextLink" href={`/?tab=lark&lang=${lang}&step=workflow`}>{t.changeWorkflow}</a>
      </section>

      <section className="agentRoster">
        <div className="agentRosterHeader">
          <div>
            <h3>{t.configuredAgents}</h3>
            <span>{t.registeredCount.replace("{count}", String(agents.length))}</span>
          </div>
          {!agentFormOpen && availableRoles.length ? (
            <button className="primary compact" type="button" onClick={() => { setEditingAgentId(""); setAgentFormOpen(true); }}>
              + {t.addAgent}
            </button>
          ) : null}
        </div>

        {agentFormOpen ? (
          <form action={actions.registerAgent} className="agentEditor">
            <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
            <input name="workflow" type="hidden" value={currentWorkflow.key} suppressHydrationWarning />
            <div className="editorHeader">
              <h3>{t.newAgent}</h3>
              <button aria-label={t.cancel} className="iconButton" title={t.cancel} type="button" onClick={() => setAgentFormOpen(false)}>×</button>
            </div>
            <div className="agentFormGrid">
              <div className="field">
                <span className="fieldLabel">{t.role}</span>
                <Dropdown
                  label={t.role}
                  name="role"
                  onChange={setSelectedRoleKey}
                  options={availableRoles.map((role) => ({ label: role.display_name, value: role.role_key }))}
                  required
                  value={effectiveRoleKey}
                />
                <small className="fieldHint">{selectedRole?.allow_multiple ? t.multiAgentRole : t.singleAgentRole}</small>
              </div>
              <div className="field">
                <span className="fieldLabel">{t.harness}</span>
                <Dropdown
                  label={t.harness}
                  name="harness_type"
                  onChange={() => {}}
                  options={[{ label: "Codex", value: "codex" }]}
                  required
                  value="codex"
                />
              </div>
              <SessionField error={codexSessionError} onRefresh={refreshCodexState} sessions={selectableSessions} t={t} />
              <label className="field">
                <span className="fieldLabel">{t.displayName}</span>
                <input name="display_name" suppressHydrationWarning />
              </label>
            </div>
            <div className="agentEditorFooter singleAction">
              <PendingSubmitButton className="primary" label={t.register} />
            </div>
          </form>
        ) : null}

        <div className="agentTable">
          {agents.length ? (
            agents.map((agent) => {
              const runtime = runtimeBySession[agent.session_id];
              const runtimeStatus = runtime?.status || agent.health?.runtime_status;
              const active = runtimeStatus === "active";
              const health = agentHealth(agent, t, runtime, lifecycleBySession[agent.session_id]);
              const assignedRole = roleName(currentRoles, agent.role_key);
              const sessionName = runtime?.title || agent.health?.session_name || t.unnamedSession;
              if (editingAgentId === agent.id) {
                return (
                  <form action={actions.updateAgent} className="agentRow agentRowEditing" key={agent.id}>
                    <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
                    <input name="agent_id" type="hidden" value={agent.id} suppressHydrationWarning />
                    <input name="current_session_id" type="hidden" value={agent.session_id} suppressHydrationWarning />
                    <input name="runtime_status" type="hidden" value={runtimeStatus || ""} suppressHydrationWarning />
                    <div className="agentIdentity">
                      <strong>{agent.display_name || assignedRole}</strong>
                      <span>{agent.display_name ? `${assignedRole} · ${harnessName(agent.harness_type)}` : harnessName(agent.harness_type)}</span>
                    </div>
                    <div className="agentSessionEditor">
                      <SessionField
                        error={codexSessionError}
                        initialValue={agent.session_id}
                        onRefresh={refreshCodexState}
                        sessions={selectableSessions}
                        t={t}
                      />
                    </div>
                    <div className="agentHealth">
                      <span className={`statusBadge compact ${health.className}`} title={health.title}>{health.label}</span>
                    </div>
                    <div className="agentActions" title={active ? t.agentBusyActionHint : undefined}>
                      <button className="secondary mini" type="button" onClick={() => setEditingAgentId("")}>{t.cancel}</button>
                      <PendingSubmitButton className="primary mini" disabled={active} label={t.save} title={active ? t.agentBusyActionHint : undefined} />
                    </div>
                  </form>
                );
              }
              return (
                <div className="agentRow" key={agent.id} tabIndex={0}>
                  <div className="agentIdentity">
                    <strong>{agent.display_name || assignedRole}</strong>
                    <span>{agent.display_name ? `${assignedRole} · ${harnessName(agent.harness_type)}` : harnessName(agent.harness_type)}</span>
                  </div>
                  <div className="agentSession">
                    <strong title={sessionName}>{sessionName}</strong>
                    <code title={agent.session_id}>{agent.session_id}</code>
                    <SessionMetadata runtime={runtime} t={t} />
                  </div>
                  <div className="agentHealth">
                    <span className={`statusBadge compact ${health.className}`} title={health.title}>{health.label}</span>
                  </div>
                  <div className="agentActions" title={active ? t.agentBusyActionHint : undefined}>
                    <button className="secondary mini" disabled={active} type="button" onClick={() => { setAgentFormOpen(false); setEditingAgentId(agent.id); }}>
                      {t.switchSession}
                    </button>
                    <form action={actions.unregisterAgent}>
                      <input name="lang" type="hidden" value={lang} suppressHydrationWarning />
                      <input name="agent_id" type="hidden" value={agent.id} suppressHydrationWarning />
                      <input name="current_session_id" type="hidden" value={agent.session_id} suppressHydrationWarning />
                      <input name="runtime_status" type="hidden" value={runtimeStatus || ""} suppressHydrationWarning />
                      <button className="ghost mini" disabled={active} type="submit">{t.remove}</button>
                    </form>
                  </div>
                </div>
              );
            })
          ) : !agentFormOpen ? (
            <div className="emptyState">
              <strong>{t.emptyTitle}</strong>
              <span>{t.emptyAgents}</span>
            </div>
          ) : null}
        </div>
      </section>
    </div>
  );
}

function SessionField({ error, initialValue = "", onRefresh, sessions, t }) {
  const [manual, setManual] = useState(sessions.length === 0);
  const [sessionId, setSessionId] = useState(initialValue);
  const hint = error ? t.sessionListUnavailable : sessions.length === 0 ? t.noWorkspaceSessions : "";

  useEffect(() => {
    if (sessionId && !sessions.some((session) => session.session_id === sessionId)) {
      setSessionId("");
    }
    if (!sessions.length) {
      setManual(true);
    }
  }, [sessionId, sessions]);

  return (
    <div className="field">
      <span className="fieldLabel">{t.sessionId}</span>
      {manual ? (
        <input id="session-id" name="session_id" required value={sessionId} onChange={(event) => setSessionId(event.target.value)} suppressHydrationWarning />
      ) : (
        <Dropdown
          label={t.sessionId}
          name="session_id"
          onChange={setSessionId}
          onOpen={onRefresh}
          options={sessions.map((session) => ({
            description: session.session_id,
            label: session.name || t.unnamedSession,
            value: session.session_id
          }))}
          placeholder={t.chooseSession}
          required
          value={sessionId}
        />
      )}
      <div className="fieldMeta">
        {hint ? <small className="fieldHint">{hint}</small> : <span />}
        {sessions.length ? (
          <button className="fieldSwitch" type="button" onClick={() => { setManual(!manual); setSessionId(""); }}>
            {manual ? t.chooseFromSessions : t.manualSession}
          </button>
        ) : null}
      </div>
    </div>
  );
}

function Dropdown({ label, name, onChange, onOpen, options, placeholder = "", required = false, value }) {
  const [invalid, setInvalid] = useState(false);
  const [open, setOpen] = useState(false);
  const menuId = useId();
  const rootRef = useRef(null);
  const triggerRef = useRef(null);
  const selected = options.find((option) => option.value === value);

  useEffect(() => {
    function closeOutside(event) {
      if (!rootRef.current?.contains(event.target)) {
        setOpen(false);
      }
    }

    function closeWithEscape(event) {
      if (event.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    }

    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeWithEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeWithEscape);
    };
  }, []);

  function select(option) {
    onChange(option.value);
    setInvalid(false);
    setOpen(false);
    triggerRef.current?.focus();
  }

  return (
    <div className={`dropdown${open ? " open" : ""}${invalid ? " invalid" : ""}`} ref={rootRef}>
      <select
        aria-hidden="true"
        aria-label={label}
        className="dropdownNative"
        name={name}
        onChange={() => {}}
        onInvalid={(event) => {
          event.preventDefault();
          setInvalid(true);
          setOpen(true);
          triggerRef.current?.focus();
        }}
        required={required}
        tabIndex={-1}
        value={value}
      >
        <option value="" />
        {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>
      <button
        aria-controls={menuId}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label={label}
        className="dropdownTrigger"
        onClick={() => {
          const nextOpen = !open;
          setOpen(nextOpen);
          if (nextOpen) {
            onOpen?.();
          }
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") {
            event.preventDefault();
            setOpen(true);
            onOpen?.();
          }
        }}
        ref={triggerRef}
        type="button"
      >
        <span className={selected ? "dropdownValue" : "dropdownPlaceholder"}>{selected?.label || placeholder}</span>
        <span aria-hidden="true" className="dropdownChevron" />
      </button>
      {open ? (
        <div aria-label={label} className="dropdownMenu" id={menuId} role="listbox">
          {options.map((option) => (
            <button
              aria-selected={option.value === value}
              className="dropdownOption"
              key={option.value}
              onClick={() => select(option)}
              role="option"
              type="button"
            >
              <span className="dropdownOptionText">
                <strong>{option.label}</strong>
                {option.description ? <small>{option.description}</small> : null}
              </span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function roleName(roles, key) {
  return roles.find((role) => role.role_key === key)?.display_name || key.toUpperCase();
}

function harnessName(harnessType) {
  return { codex: "Codex" }[harnessType] || harnessType;
}

function SessionMetadata({ runtime, t }) {
  const items = [];
  if (runtime?.model) {
    items.push({ label: readableModel(runtime.model), title: `${t.modelLabel}: ${runtime.model}` });
  }
  if (runtime && Object.hasOwn(runtime, "effort")) {
    const effort = runtime.effort ? readableEffort(runtime.effort) : t.defaultValue;
    items.push({ label: effort, title: `${t.thinkingLabel}: ${effort}`, ultra: runtime.effort === "ultra" });
  }
  if (runtime && Object.hasOwn(runtime, "serviceTier")) {
    const fast = runtime.serviceTier === "priority";
    if (fast) {
      items.push({ title: `${t.speedLabel}: ${t.fast}`, fast: true });
    }
  }
  if (!items.length) {
    return <span className="sessionMetadata unavailable">{t.sessionSettingsUnavailable}</span>;
  }
  return (
    <div className="sessionMetadata">
      {items.map((item) => (
        <span
          aria-label={item.fast ? t.fast : undefined}
          className={item.fast ? "fastIcon" : item.ultra ? "ultra" : ""}
          key={item.title}
          role={item.fast ? "img" : undefined}
          title={item.title}
        >
          {item.fast ? (
            <svg aria-hidden="true" viewBox="0 0 24 24">
              <path d="M13.4 2.2 4.8 13.1a.9.9 0 0 0 .7 1.5h5.1l-.8 6.2a.9.9 0 0 0 1.6.7l7.8-10.7a.9.9 0 0 0-.7-1.4h-4.9l1.3-6.5a.9.9 0 0 0-1.5-.7Z" fill="currentColor" />
            </svg>
          ) : item.label}
        </span>
      ))}
    </div>
  );
}

function readableModel(model) {
  return model.replace(/^gpt-/i, "GPT-").replace(/-([a-z][a-z0-9]*)$/i, (_, suffix) => ` ${suffix[0].toUpperCase()}${suffix.slice(1)}`);
}

function readableEffort(effort) {
  return { minimal: "Minimal", low: "Low", medium: "Medium", high: "High", xhigh: "XHigh", max: "Max", ultra: "Ultra" }[effort] || effort;
}

function agentHealth(agent, t, runtime, lifecycle) {
  const checkedHealth = agent.health;
  const healthStatus = checkedHealth?.status;
  const runtimeStatus = runtime?.status || checkedHealth?.runtime_status;
  const statuses = {
    healthy: { className: "saved", label: t.healthHealthy },
    archived: { className: "archived", label: t.healthArchived },
    deleted: { className: "error", label: t.healthDeleted },
    unavailable: { className: "warning", label: t.healthUnavailable },
    system_error: { className: "error", label: t.healthSystemError },
    unhealthy: { className: "error", label: t.healthUnhealthy },
    unverified: { className: "", label: t.healthUnverified }
  };
  const runtimeStatuses = {
    active: { className: "active", label: t.healthActive, title: t.healthActiveHint },
    idle: { className: "saved", label: t.healthIdle, title: t.healthIdleHint },
    notLoaded: { className: "", label: t.healthNotLoaded, title: t.healthNotLoadedHint },
    systemError: { className: "error", label: t.healthSystemError, title: runtime?.error || checkedHealth?.error || t.healthSystemErrorHint }
  };
  if (lifecycle === "archived" || (healthStatus === "archived" && lifecycle !== "unarchived")) {
    return { ...statuses.archived, title: t.healthArchivedHint };
  }
  if (healthStatus === "deleted") {
    return { ...statuses.deleted, title: t.healthDeletedHint };
  }
  if (["unavailable", "unhealthy"].includes(healthStatus)) {
    const health = statuses[healthStatus];
    return { ...health, title: checkedHealth.error || (healthStatus === "unavailable" ? t.healthUnavailableHint : health.label) };
  }
  if (runtimeStatuses[runtimeStatus]) {
    return runtimeStatuses[runtimeStatus];
  }
  if (lifecycle === "unarchived") {
    return runtimeStatuses.notLoaded;
  }
  const health = statuses[healthStatus] || statuses.unverified;
  const title = checkedHealth?.error || (checkedHealth?.checked_at ? `${t.healthCheckedAt}: ${shortDate(checkedHealth.checked_at)}` : health.label);
  return { ...health, title };
}

function sessionMap(sessions) {
  return Object.fromEntries(sessions.filter((session) => session.threadId).map((session) => [session.threadId, session]));
}

function updateRuntime(current, event) {
  if (event.removed) {
    const next = { ...current };
    delete next[event.threadId];
    return next;
  }
  return { ...current, [event.threadId]: { ...current[event.threadId], ...event } };
}

function settledLifecycle(current, agents) {
  const next = { ...current };
  for (const [threadId, status] of Object.entries(current)) {
    const agent = agents.find((item) => item.session_id === threadId);
    if (!agent || (status === "archived" && agent.health?.status === "archived") || (status === "unarchived" && agent.health?.status !== "archived")) {
      delete next[threadId];
    }
  }
  return next;
}

function initialAccessRows(identities, snapshots) {
  const byIdentity = new Map(snapshots.map((snapshot) => [snapshot.identity_id, snapshot]));
  return identities.map((identity) => {
    const snapshot = byIdentity.get(identity.id) || {};
    const missingScopes = Array.isArray(snapshot.missing_scopes)
      ? snapshot.missing_scopes
      : String(snapshot.missing_scopes || "").split(",").filter(Boolean);
    return {
      ...identity,
      status: snapshot.status || "unverified",
      checks: {
        auth: snapshot.auth_status || "unverified",
        api: snapshot.api_status || "unverified",
        collaborator: snapshot.collaborator_status || "unverified",
        read: snapshot.read_status || "unverified",
        write: snapshot.write_status || "unverified",
        cleanup: snapshot.cleanup_status || "unverified"
      },
      failure_kind: snapshot.failure_kind || null,
      missing_scopes: missingScopes,
      repair_url: snapshot.repair_url || null,
      last_error: snapshot.last_error || null,
      last_verified_at: snapshot.last_verified_at || null
    };
  });
}

function applyAccessEvent(rows, event) {
  if (!event.identity_id && event.type !== "identity_completed") {
    return rows;
  }
  if (event.type === "identity_started") {
    return rows.map((row) => row.id === event.identity_id ? {
      ...row,
      status: "checking",
      checks: Object.fromEntries(Object.keys(row.checks).map((check) => [check, "unverified"])),
      failure_kind: null,
      missing_scopes: [],
      repair_url: null,
      last_error: null
    } : row);
  }
  if (event.type === "check_updated") {
    return rows.map((row) => row.id === event.identity_id ? {
      ...row,
      checks: { ...row.checks, [event.check]: event.status }
    } : row);
  }
  if (event.type === "identity_completed") {
    const result = event.result;
    return rows.map((row) => row.id === result.identity_id ? {
      ...row,
      ...result,
      checks: { ...row.checks, ...result.checks }
    } : row);
  }
  return rows;
}

function markInterruptedAccess(rows) {
  return rows.map((row) => ({
    ...row,
    status: row.status === "checking" ? "unverified" : row.status,
    checks: Object.fromEntries(Object.entries(row.checks).map(([check, status]) => [
      check,
      ["running", "waiting"].includes(status) ? "unverified" : status
    ]))
  }));
}

function accessStatusLabel(status, t) {
  return {
    passed: t.accessPassed,
    failed: t.accessFailed,
    running: t.accessRunning,
    waiting: t.accessWaiting,
    blocked: t.accessBlocked,
    unverified: t.accessUnverified
  }[status] || t.accessUnverified;
}

function accessFailureMessage(kind, t) {
  if (kind === "missing_scope") {
    return t.accessMissingScope;
  }
  if (kind === "not_collaborator") {
    return t.accessNotCollaborator;
  }
  if (kind === "auth_expired") {
    return t.accessAuthExpired;
  }
  if (kind?.startsWith("auth")) {
    return t.accessAuthFailed;
  }
  if (kind?.startsWith("read")) {
    return t.accessReadFailed;
  }
  if (kind?.startsWith("write")) {
    return t.accessWriteFailed;
  }
  if (kind?.startsWith("cleanup")) {
    return t.accessCleanupFailed;
  }
  return t.accessGenericFailed;
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
  if (lang === "zh") {
    return rootName.endsWith("项目") ? `${rootName}看板` : `${rootName}项目看板`;
  }
  return /project$/i.test(rootName) ? `${rootName} Board` : `${rootName} Project Board`;
}

function permissionUrl(appId, larkDomain) {
  const origin = larkDomain === "larksuite" ? "https://open.larksuite.com" : "https://open.feishu.cn";
  return `${origin}/app/${encodeURIComponent(appId)}/auth?q=admin:app.info:readonly,application:application:self_manage&op_from=openapi&token_type=tenant`;
}

function shortDate(value) {
  return value ? value.slice(0, 10) : "-";
}
