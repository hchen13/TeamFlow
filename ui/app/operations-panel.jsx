"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import styles from "./operations-panel.module.css";

const copy = {
  zh: {
    activeSessions: "正在运行",
    agents: "Agent",
    apps: "飞书连接",
    connected: "已连接",
    daemon: "守护进程",
    failedEvents: "全局失败事件",
    following: "已纳入保持",
    followingHint: "表示 Session 已纳入防释放保持，不代表当前已经加载。",
    globalLogs: "全部项目",
    globalService: "全局服务",
    healthy: "运行正常",
    loading: "正在读取状态",
    lastUpdated: "更新时间",
    logConnecting: "正在连接日志...",
    logs: "实时日志",
    keeperRefresh: "待刷新",
    noAgents: "当前 Workflow 尚未注册 Agent。",
    noLogs: "暂无日志输出。",
    offline: "未运行",
    pause: "暂停滚动",
    paused: "继续滚动",
    pid: "PID",
    ready: "已就绪",
    registeredAgents: "注册 Agent",
    restart: "重启全局守护进程",
    restartConfirm: "重启期间，{count} 个已启用项目的任务投递都会短暂停止。确认重启全局守护进程？",
    restarting: "正在重启...",
    starting: "正在启动",
    statusError: "状态读取失败",
    stopping: "正在停止",
    unhealthy: "运行异常",
    currentProject: "当前项目",
    workspaceLogs: "当前项目",
    workspaces: "已启用项目"
  },
  en: {
    activeSessions: "Running now",
    agents: "Agents",
    apps: "Lark connections",
    connected: "Connected",
    daemon: "Daemon",
    failedEvents: "Global failures",
    following: "Kept",
    followingHint: "These sessions are protected from idle release; this does not mean they are loaded.",
    globalLogs: "All projects",
    globalService: "Global service",
    healthy: "Healthy",
    loading: "Loading status",
    lastUpdated: "Updated",
    logConnecting: "Connecting to logs...",
    logs: "Live log",
    keeperRefresh: "Refresh needed",
    noAgents: "No agents are registered for this workflow.",
    noLogs: "No log output yet.",
    offline: "Offline",
    pause: "Pause",
    paused: "Resume",
    pid: "PID",
    ready: "Ready",
    registeredAgents: "Registered agents",
    restart: "Restart global daemon",
    restartConfirm: "Delivery for all {count} enabled projects will pause briefly. Restart the global daemon?",
    restarting: "Restarting...",
    starting: "Starting",
    statusError: "Status unavailable",
    stopping: "Stopping",
    unhealthy: "Unhealthy",
    currentProject: "Current project",
    workspaceLogs: "Current project",
    workspaces: "Enabled projects"
  }
};

const runtimeLabels = {
  zh: { active: "正在运行", checking: "正在确认", idle: "空闲", notLoaded: "未加载", systemError: "系统错误", unconfirmed: "状态未知" },
  en: { active: "Running", checking: "Checking", idle: "Idle", notLoaded: "Not loaded", systemError: "System error", unconfirmed: "Unknown" }
};

export default function OperationsPanel({ agents, lang, runtimeBySession, workspaceName, workspaceRoot }) {
  const t = copy[lang];
  const [status, setStatus] = useState(null);
  const [statusCheckedAt, setStatusCheckedAt] = useState(null);
  const [statusError, setStatusError] = useState("");
  const [restarting, setRestarting] = useState(false);
  const [logs, setLogs] = useState([]);
  const [paused, setPaused] = useState(false);
  const [logConnected, setLogConnected] = useState(false);
  const [logScope, setLogScope] = useState("workspace");
  const logViewportRef = useRef(null);

  const refreshStatus = useCallback(async () => {
    try {
      const response = await fetch("/api/operations/status", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setStatus(await response.json());
      setStatusCheckedAt(new Date());
      setStatusError("");
    } catch (error) {
      setStatusError(error.message || String(error));
    }
  }, []);

  useEffect(() => {
    void refreshStatus();
    const timer = setInterval(refreshStatus, 3000);
    return () => clearInterval(timer);
  }, [refreshStatus]);

  useEffect(() => {
    setLogs([]);
    setLogConnected(false);
    const params = new URLSearchParams({ scope: logScope });
    if (workspaceName) params.set("workspace", workspaceName);
    const source = new EventSource(`/api/operations/logs?${params.toString()}`);
    source.onopen = () => setLogConnected(true);
    source.onerror = () => setLogConnected(false);
    source.onmessage = ({ data }) => {
      try {
        const next = JSON.parse(data).lines || [];
        setLogs((current) => [...current, ...next].slice(-500));
      } catch {
        // Ignore malformed transport frames; raw daemon lines remain untouched.
      }
    };
    return () => source.close();
  }, [logScope, workspaceName]);

  useEffect(() => {
    if (!paused && logViewportRef.current) {
      logViewportRef.current.scrollTop = logViewportRef.current.scrollHeight;
    }
  }, [logs, paused]);

  const togglePaused = () => {
    setPaused((current) => !current);
  };

  const restart = async () => {
    const projectCount = status?.workspaces?.length || 0;
    if (!window.confirm(t.restartConfirm.replace("{count}", String(projectCount)))) return;
    setRestarting(true);
    try {
      const response = await fetch("/api/operations/status", { method: "POST" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      setStatus(payload);
      setStatusCheckedAt(new Date());
      setStatusError("");
    } catch (error) {
      setStatusError(error.message || String(error));
    } finally {
      setRestarting(false);
      void refreshStatus();
    }
  };

  const daemonState = describeDaemon(status, statusError, t);
  const connectedApps = status?.apps?.filter((app) => app.connected).length || 0;
  const hasWorkspaceKeeper = Boolean(status?.session_keeper?.by_workspace);
  const projectKeeper = status?.session_keeper?.by_workspace?.[workspaceRoot];
  const registeredSessions = projectKeeper?.registered ?? agents.filter((agent) => agent.session_id).length;
  const legacyKeeperComplete = !hasWorkspaceKeeper
    && status?.session_keeper?.following === status?.session_keeper?.desired
    && status?.session_keeper?.desired === status?.session_keeper?.registered_sessions;
  const followingSessions = projectKeeper?.following ?? (legacyKeeperComplete ? registeredSessions : undefined);
  const activeSessions = agents.filter((agent) => agentRuntimeStatus(agent, runtimeBySession) === "active").length;
  const projectLabel = workspaceName || workspaceRoot?.split(/[\\/]/).filter(Boolean).at(-1) || t.currentProject;

  return (
    <div className={styles.operations}>
      <section className={styles.statusPanel}>
        <header className={styles.sectionHeader}>
          <div>
            <span className={styles.kicker}>{t.globalService} · {t.daemon}</span>
            <strong className={styles.daemonState} data-tone={daemonState.tone}>{daemonState.label}</strong>
          </div>
          <button className={styles.restartButton} disabled={restarting} type="button" onClick={restart}>
            {restarting ? t.restarting : t.restart}
          </button>
        </header>

        <div className={styles.globalMetrics}>
          <Metric label={t.apps} value={status ? `${connectedApps} / ${status.apps?.length || 0}` : "-"} />
          <Metric label={t.workspaces} value={status ? status.workspaces?.length || 0 : "-"} />
          <Metric label={t.failedEvents} tone={status?.inbox?.failed ? "warning" : "normal"} value={status ? status.inbox?.failed || 0 : "-"} />
        </div>

        <footer className={styles.statusMeta}>
          <span>{t.pid}: {status?.pid || "-"}</span>
          <span>{t.lastUpdated}: {statusCheckedAt ? statusCheckedAt.toLocaleTimeString(lang === "zh" ? "zh-CN" : "en-US") : "-"}</span>
          {statusError ? <span className={styles.errorText}>{statusError}</span> : null}
        </footer>
      </section>

      <section className={styles.projectPanel}>
        <header className={styles.projectHeader}>
          <div>
            <span className={styles.kicker}>{t.currentProject}</span>
            <h3>{projectLabel}</h3>
          </div>
        </header>
        <div className={styles.projectMetrics}>
          <Metric
            label={t.following}
            title={t.followingHint}
            value={status ? (followingSessions === undefined ? t.keeperRefresh : `${followingSessions} / ${registeredSessions}`) : "-"}
          />
          <Metric label={t.activeSessions} value={status ? activeSessions : "-"} />
          <Metric label={t.registeredAgents} value={agents.length} />
        </div>
        <header className={styles.agentHeader}>
          <h3>{t.agents}</h3>
          <span>{agents.length}</span>
        </header>
        {agents.length ? (
          <div className={styles.agentList}>
            {agents.map((agent) => {
              const runtimeStatus = agentRuntimeStatus(agent, runtimeBySession);
              return (
                <div className={styles.agentRow} key={agent.id}>
                  <div>
                    <strong>{agent.display_name || agent.role_key}</strong>
                    <span>{agent.role_key} · {agent.health?.session_name || agent.session_id}</span>
                  </div>
                  <span className={styles.runtime} data-status={runtimeStatus}>{runtimeLabels[lang][runtimeStatus] || runtimeStatus}</span>
                </div>
              );
            })}
          </div>
        ) : <p className={styles.empty}>{t.noAgents}</p>}
      </section>

      <section className={styles.logPanel}>
        <header className={styles.logHeader}>
          <div>
            <span className={styles.connectionDot} data-connected={logConnected} />
            <h3>{t.logs}</h3>
          </div>
          <div className={styles.logControls}>
            <div className={styles.scopeSwitch}>
              <button aria-pressed={logScope === "workspace"} type="button" onClick={() => setLogScope("workspace")}>{t.workspaceLogs}</button>
              <button aria-pressed={logScope === "global"} type="button" onClick={() => setLogScope("global")}>{t.globalLogs}</button>
            </div>
            <button className={styles.logButton} type="button" onClick={togglePaused}>{paused ? t.paused : t.pause}</button>
          </div>
        </header>
        <div className={styles.logViewport} aria-live="polite" ref={logViewportRef} role="log">
          {logs.length ? logs.map((line, index) => <div key={`${index}-${line}`}>{line}</div>) : (
            <span className={styles.logEmpty}>{logConnected ? t.noLogs : t.logConnecting}</span>
          )}
        </div>
      </section>
    </div>
  );
}

function Metric({ label, title, tone = "normal", value }) {
  return (
    <div className={styles.metric} data-tone={tone} title={title}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function agentRuntimeStatus(agent, runtimeBySession) {
  return runtimeBySession[agent.session_id]?.status || agent.health?.runtime_status || "unconfirmed";
}

function describeDaemon(status, error, t) {
  if (error && !status) return { label: t.statusError, tone: "error" };
  if (!status) return { label: t.loading, tone: "muted" };
  if (!status?.running) return { label: t.offline, tone: "muted" };
  if (status.stopping) return { label: t.stopping, tone: "warning" };
  if (!status.healthy) return { label: t.unhealthy, tone: "error" };
  if (!status.ready) return { label: t.starting, tone: "warning" };
  return { label: t.healthy, tone: "healthy" };
}
