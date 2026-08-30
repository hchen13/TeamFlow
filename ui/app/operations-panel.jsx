"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import styles from "./operations-panel.module.css";

const copy = {
  zh: {
    activeSessions: "处理中 Session",
    agents: "Agent 状态",
    apps: "飞书连接",
    connected: "已连接",
    daemon: "守护进程",
    failedEvents: "失败事件",
    following: "保持中的 Session",
    healthy: "运行正常",
    loading: "正在读取状态",
    lastUpdated: "更新时间",
    logConnecting: "正在连接日志...",
    logs: "守护进程实时日志",
    noAgents: "当前 Workflow 尚未注册 Agent。",
    noLogs: "暂无日志输出。",
    offline: "未运行",
    pause: "暂停滚动",
    paused: "继续滚动",
    pid: "PID",
    ready: "已就绪",
    restart: "重启守护进程",
    restartConfirm: "重启期间任务投递会短暂停止。确认重启 TeamFlow 守护进程？",
    restarting: "正在重启...",
    starting: "正在启动",
    statusError: "状态读取失败",
    stopping: "正在停止",
    unhealthy: "运行异常",
    workspaces: "已启用项目"
  },
  en: {
    activeSessions: "Active sessions",
    agents: "Agent status",
    apps: "Lark connections",
    connected: "Connected",
    daemon: "Daemon",
    failedEvents: "Failed events",
    following: "Kept sessions",
    healthy: "Healthy",
    loading: "Loading status",
    lastUpdated: "Updated",
    logConnecting: "Connecting to logs...",
    logs: "Live daemon log",
    noAgents: "No agents are registered for this workflow.",
    noLogs: "No log output yet.",
    offline: "Offline",
    pause: "Pause",
    paused: "Resume",
    pid: "PID",
    ready: "Ready",
    restart: "Restart daemon",
    restartConfirm: "Task delivery will pause briefly. Restart the TeamFlow daemon?",
    restarting: "Restarting...",
    starting: "Starting",
    statusError: "Status unavailable",
    stopping: "Stopping",
    unhealthy: "Unhealthy",
    workspaces: "Enabled projects"
  }
};

const runtimeLabels = {
  zh: { active: "正在运行", checking: "正在确认", idle: "空闲", notLoaded: "未加载", systemError: "系统错误", unconfirmed: "状态未知" },
  en: { active: "Running", checking: "Checking", idle: "Idle", notLoaded: "Not loaded", systemError: "System error", unconfirmed: "Unknown" }
};

export default function OperationsPanel({ agents, lang, runtimeBySession }) {
  const t = copy[lang];
  const [status, setStatus] = useState(null);
  const [statusCheckedAt, setStatusCheckedAt] = useState(null);
  const [statusError, setStatusError] = useState("");
  const [restarting, setRestarting] = useState(false);
  const [logs, setLogs] = useState([]);
  const [paused, setPaused] = useState(false);
  const [logConnected, setLogConnected] = useState(false);
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
    const source = new EventSource("/api/operations/logs");
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
  }, []);

  useEffect(() => {
    if (!paused && logViewportRef.current) {
      logViewportRef.current.scrollTop = logViewportRef.current.scrollHeight;
    }
  }, [logs, paused]);

  const togglePaused = () => {
    setPaused((current) => !current);
  };

  const restart = async () => {
    if (!window.confirm(t.restartConfirm)) return;
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
  const desiredSessions = status?.session_keeper?.desired ?? status?.session_keeper?.declared_sessions ?? 0;
  const followingSessions = status?.session_keeper?.following || 0;

  return (
    <div className={styles.operations}>
      <section className={styles.statusPanel}>
        <header className={styles.sectionHeader}>
          <div>
            <span className={styles.kicker}>{t.daemon}</span>
            <strong className={styles.daemonState} data-tone={daemonState.tone}>{daemonState.label}</strong>
          </div>
          <button className={styles.restartButton} disabled={restarting} type="button" onClick={restart}>
            {restarting ? t.restarting : t.restart}
          </button>
        </header>

        <div className={styles.metrics}>
          <Metric label={t.apps} value={status ? `${connectedApps} / ${status.apps?.length || 0}` : "-"} />
          <Metric label={t.following} value={status ? `${followingSessions} / ${desiredSessions}` : "-"} />
          <Metric label={t.activeSessions} value={status ? status.active_sessions?.length || 0 : "-"} />
          <Metric label={t.workspaces} value={status ? status.workspaces?.length || 0 : "-"} />
          <Metric label={t.failedEvents} tone={status?.inbox?.failed ? "warning" : "normal"} value={status ? status.inbox?.failed || 0 : "-"} />
        </div>

        <footer className={styles.statusMeta}>
          <span>{t.pid}: {status?.pid || "-"}</span>
          <span>{t.lastUpdated}: {statusCheckedAt ? statusCheckedAt.toLocaleTimeString(lang === "zh" ? "zh-CN" : "en-US") : "-"}</span>
          {statusError ? <span className={styles.errorText}>{statusError}</span> : null}
        </footer>
      </section>

      <section className={styles.agentPanel}>
        <header className={styles.compactHeader}>
          <h3>{t.agents}</h3>
          <span>{agents.length}</span>
        </header>
        {agents.length ? (
          <div className={styles.agentList}>
            {agents.map((agent) => {
              const runtime = runtimeBySession[agent.session_id];
              const runtimeStatus = runtime?.status || agent.health?.runtime_status || "unconfirmed";
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
          <button className={styles.logButton} type="button" onClick={togglePaused}>{paused ? t.paused : t.pause}</button>
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

function Metric({ label, tone = "normal", value }) {
  return (
    <div className={styles.metric} data-tone={tone}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
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
