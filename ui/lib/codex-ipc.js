import { randomUUID } from "node:crypto";
import { EventEmitter } from "node:events";
import { existsSync, statSync, watch } from "node:fs";
import { homedir, tmpdir } from "node:os";
import path from "node:path";
import { createConnection } from "node:net";

const STREAM_VERSION = 11;
const ARCHIVED_VERSION = 2;
const UNARCHIVED_VERSION = 1;
const RUNTIME_STATUSES = new Set(["active", "idle", "notLoaded", "systemError"]);
const BRIDGE_VERSION = 5;
const globalKey = Symbol.for("teamflow.codexBridge");

class CodexBridge extends EventEmitter {
  constructor() {
    super();
    this.version = BRIDGE_VERSION;
    this.workspace = path.resolve(process.env.TEAMFLOW_WORKSPACE || "..");
    this.codexHome = path.resolve(process.env.CODEX_HOME || path.join(homedir(), ".codex"));
    this.connected = false;
    this.connecting = false;
    this.buffer = Buffer.alloc(0);
    this.runtimeBySource = new Map();
    this.knownThreads = new Set();
    this.watchers = [];
    this.startWatchers();
    this.connect();
  }

  dispose() {
    this.disposed = true;
    clearTimeout(this.catalogTimer);
    clearTimeout(this.reconnectTimer);
    this.socket?.destroy();
    this.watchers.forEach((watcher) => watcher.close());
    this.removeAllListeners();
  }

  snapshot() {
    return {
      connected: this.connected,
      sessions: [...this.aggregateRuntime().values()]
    };
  }

  subscribe(listener) {
    this.on("event", listener);
    return () => this.off("event", listener);
  }

  track(threadIds) {
    this.knownThreads = new Set(threadIds.filter(Boolean));
  }

  async connect() {
    if (this.connected || this.connecting) {
      return;
    }
    this.connecting = true;
    for (const socketPath of this.socketPaths()) {
      try {
        const socket = await openSocket(socketPath);
        this.attach(socket);
        this.connecting = false;
        return;
      } catch {
        // Try the next known Codex IPC location.
      }
    }
    this.connecting = false;
    this.scheduleReconnect();
  }

  socketPaths() {
    if (process.platform === "win32") {
      return ["\\\\.\\pipe\\codex-ipc"];
    }
    return [
      path.join(this.codexHome, "ipc", "ipc.sock"),
      path.join(tmpdir(), "codex-ipc", `ipc-${process.getuid()}.sock`),
      path.join(tmpdir(), "codex-ipc", "ipc-0.sock")
    ].filter((candidate, index, all) => all.indexOf(candidate) === index && ownedSocket(candidate));
  }

  attach(socket) {
    this.socket = socket;
    this.buffer = Buffer.alloc(0);
    this.connected = true;
    this.emit("event", { type: "bridge", connected: true });
    socket.on("data", (chunk) => this.onData(chunk));
    socket.on("close", () => this.disconnect());
    socket.on("error", () => socket.destroy());
    this.send({
      type: "request",
      requestId: randomUUID(),
      sourceClientId: "initializing-client",
      version: 0,
      method: "initialize",
      params: { clientType: "teamflow" }
    });
  }

  disconnect() {
    if (this.disposed) {
      return;
    }
    if (!this.connected) {
      return;
    }
    this.connected = false;
    this.socket = null;
    this.runtimeBySource.clear();
    this.emit("event", { type: "bridge", connected: false });
    this.scheduleReconnect();
  }

  scheduleReconnect() {
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => this.connect(), 1000);
    this.reconnectTimer.unref?.();
  }

  onData(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    while (this.buffer.length >= 4) {
      const size = this.buffer.readUInt32LE(0);
      if (size > 256 * 1024 * 1024) {
        this.socket?.destroy();
        return;
      }
      if (this.buffer.length < size + 4) {
        return;
      }
      const frame = this.buffer.subarray(4, size + 4);
      this.buffer = this.buffer.subarray(size + 4);
      try {
        this.onMessage(JSON.parse(frame.toString("utf8")));
      } catch {
        // Ignore malformed third-party IPC frames and keep the stream alive.
      }
    }
  }

  onMessage(message) {
    const method = message?.method;
    const params = message?.params ?? message?.payload ?? message?.result ?? {};
    if (method === "client-discovery-request") {
      this.respond(message, { canHandle: false });
      return;
    }
    if (message?.type === "request") {
      this.respond(message, null, "no-handler-for-request");
      return;
    }
    if (method === "thread-stream-state-changed" && message.version === STREAM_VERSION) {
      this.updateRuntime(message.sourceClientId || "unknown", params);
      return;
    }
    if (method === "thread-archived" && message.version === ARCHIVED_VERSION) {
      this.lifecycle("archived", params);
      return;
    }
    if (method === "thread-unarchived" && message.version === UNARCHIVED_VERSION) {
      this.lifecycle("unarchived", params);
      return;
    }
    if (method === "client-status-changed") {
      this.updateClientStatus(params);
    }
  }

  updateRuntime(sourceClientId, params) {
    const metadata = codexThreadMetadata(params);
    if (!metadata.threadId || !["status", "model", "effort", "serviceTier", "error"].some((key) => metadata[key] !== undefined)) {
      return;
    }
    const existing = this.runtimeBySource.get(sourceClientId)?.get(metadata.threadId);
    const cwd = metadata.cwd || existing?.cwd;
    if ((!existing && !cwd && !this.knownThreads.has(metadata.threadId)) || (cwd && !insideWorkspace(cwd, this.workspace))) {
      return;
    }
    const sourceRuntime = this.runtimeBySource.get(sourceClientId) || new Map();
    const next = { ...existing };
    for (const [key, value] of Object.entries(metadata)) {
      if (value !== undefined) {
        next[key] = value;
      }
    }
    if (metadata.status && metadata.status !== "systemError") {
      delete next.error;
    }
    next.cwd = cwd;
    next.title = metadata.title || existing?.title;
    sourceRuntime.set(metadata.threadId, next);
    this.runtimeBySource.set(sourceClientId, sourceRuntime);
    const runtime = this.aggregateRuntime().get(metadata.threadId);
    if (runtime) {
      this.emit("event", { type: "runtime", ...runtime });
    }
  }

  lifecycle(status, params) {
    const threadId = findValue(params, ["conversationId", "threadId", "thread_id", "id"]);
    if (!threadId || !this.hasThread(threadId)) {
      return;
    }
    if (status === "archived") {
      for (const sessions of this.runtimeBySource.values()) {
        const current = sessions.get(threadId);
        if (current) {
          sessions.set(threadId, { ...current, status: "notLoaded" });
        }
      }
    }
    this.emit("event", { type: "lifecycle", threadId, status });
  }

  updateClientStatus(params) {
    const status = findValue(params, ["status", "state"]);
    const clientId = findValue(params, ["clientId", "client_id", "sourceClientId"]);
    if (!clientId || !["disconnected", "closed"].includes(status)) {
      return;
    }
    const affected = [...(this.runtimeBySource.get(clientId)?.keys() || [])];
    this.runtimeBySource.delete(clientId);
    const aggregate = this.aggregateRuntime();
    for (const threadId of affected) {
      const runtime = aggregate.get(threadId);
      this.emit("event", runtime ? { type: "runtime", ...runtime } : { type: "runtime", threadId, removed: true });
    }
  }

  hasThread(threadId) {
    return this.knownThreads.has(threadId) || [...this.runtimeBySource.values()].some((sessions) => sessions.has(threadId));
  }

  aggregateRuntime() {
    const byThread = new Map();
    const rank = { systemError: 4, active: 3, idle: 2, notLoaded: 1 };
    for (const sessions of this.runtimeBySource.values()) {
      for (const [threadId, metadata] of sessions) {
        const current = byThread.get(threadId) || {};
        const merged = { ...current };
        for (const [key, value] of Object.entries(metadata)) {
          if (value !== undefined && key !== "status") {
            merged[key] = value;
          }
        }
        if ((rank[metadata.status] || 0) > (rank[current.status] || 0) || !current.status) {
          merged.status = metadata.status;
        }
        byThread.set(threadId, merged);
      }
    }
    return byThread;
  }

  respond(message, result, error) {
    this.send({
      type: "response",
      requestId: message.requestId,
      sourceClientId: message.targetClientId || "teamflow",
      targetClientId: message.sourceClientId,
      version: message.version,
      method: message.method,
      ...(error ? { error: { code: error, message: error } } : { result })
    });
  }

  send(payload) {
    if (!this.socket?.writable) {
      return;
    }
    const body = Buffer.from(JSON.stringify(payload));
    const header = Buffer.allocUnsafe(4);
    header.writeUInt32LE(body.length);
    this.socket.write(Buffer.concat([header, body]));
  }

  startWatchers() {
    for (const directory of ["sessions", "archived_sessions"].map((name) => path.join(this.codexHome, name))) {
      if (!existsSync(directory)) {
        continue;
      }
      try {
        const watcher = watch(directory, { recursive: true }, (eventType) => {
          if (eventType !== "rename") {
            return;
          }
          clearTimeout(this.catalogTimer);
          this.catalogTimer = setTimeout(() => this.emit("event", { type: "catalog" }), 300);
          this.catalogTimer.unref?.();
        });
        watcher.unref?.();
        this.watchers.push(watcher);
      } catch {
        // Dropdown-open refresh remains available where recursive watch is unsupported.
      }
    }
  }
}

function openSocket(socketPath) {
  return new Promise((resolve, reject) => {
    const socket = createConnection(socketPath);
    socket.once("connect", () => resolve(socket));
    socket.once("error", reject);
  });
}

function ownedSocket(socketPath) {
  try {
    const stat = statSync(socketPath);
    return process.getuid === undefined || stat.uid === process.getuid();
  } catch {
    return false;
  }
}

function insideWorkspace(cwd, workspace) {
  const relative = path.relative(workspace, path.resolve(cwd));
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

export function codexThreadMetadata(value) {
  const runtimeObject = findObjectWithRuntime(value);
  const settings = runtimeObject?.latestThreadSettings;
  const threadId = findValue(value, ["conversationId", "threadId", "thread_id", "sessionId"])
    || runtimeObject?.id;
  const status = runtimeStatus(runtimeObject?.threadRuntimeStatus) || findPatchStatus(value);
  const metadata = {
    threadId: threadId || undefined,
    status,
    cwd: findValue(value, ["cwd"]) || undefined,
    title: findValue(value, ["title", "name"]) || undefined,
    model: settings ? settings.model : findSettingsPatch(value, "model"),
    effort: settings ? settings.effort : findSettingsPatch(value, "effort"),
    serviceTier: settings ? settings.serviceTier : findSettingsPatch(value, "serviceTier"),
    error: status === "systemError" ? findErrorMessage(value) : undefined
  };
  return Object.fromEntries(Object.entries(metadata).filter(([, item]) => item !== undefined));
}

function findObjectWithRuntime(value, depth = 0) {
  if (!value || typeof value !== "object" || depth > 5) {
    return null;
  }
  if (runtimeStatus(value.threadRuntimeStatus)) {
    return value;
  }
  for (const [key, nested] of Object.entries(value)) {
    if (["turns", "messages", "items", "history"].includes(key)) {
      continue;
    }
    const found = findObjectWithRuntime(nested, depth + 1);
    if (found) {
      return found;
    }
  }
  return null;
}

function findPatchStatus(value, depth = 0) {
  if (!value || typeof value !== "object" || depth > 5) {
    return undefined;
  }
  const patchPath = Array.isArray(value.path) ? value.path.join("/") : value.path;
  const status = runtimeStatus(value.value);
  if (typeof patchPath === "string" && patchPath.includes("threadRuntimeStatus") && status) {
    return status;
  }
  for (const nested of Object.values(value)) {
    const found = findPatchStatus(nested, depth + 1);
    if (found) {
      return found;
    }
  }
  return undefined;
}

function findSettingsPatch(value, field, depth = 0) {
  if (!value || typeof value !== "object" || depth > 6) {
    return undefined;
  }
  const patchPath = Array.isArray(value.path) ? value.path.join("/") : value.path;
  if (typeof patchPath === "string" && patchPath.includes("latestThreadSettings")) {
    if (patchPath.endsWith(`/${field}`)) {
      return value.value;
    }
    if (patchPath.endsWith("latestThreadSettings") && value.value && typeof value.value === "object") {
      return value.value[field];
    }
  }
  for (const nested of Object.values(value)) {
    const found = findSettingsPatch(nested, field, depth + 1);
    if (found !== undefined) {
      return found;
    }
  }
  return undefined;
}

function findErrorMessage(value, depth = 0) {
  if (!value || typeof value !== "object" || depth > 8) {
    return undefined;
  }
  if (value.error && typeof value.error === "object") {
    const message = String(value.error.message || value.error.additionalDetails || "").trim();
    if (message) {
      return message.slice(0, 500);
    }
  }
  for (const [key, nested] of Object.entries(value)) {
    if (["content", "messages", "text"].includes(key)) {
      continue;
    }
    const found = findErrorMessage(nested, depth + 1);
    if (found) {
      return found;
    }
  }
  return undefined;
}

function runtimeStatus(value) {
  const status = typeof value === "string" ? value : value?.type;
  return RUNTIME_STATUSES.has(status) ? status : undefined;
}

function findValue(value, keys, depth = 0) {
  if (!value || typeof value !== "object" || depth > 5) {
    return null;
  }
  for (const key of keys) {
    if (typeof value[key] === "string" && value[key]) {
      return value[key];
    }
  }
  for (const [key, nested] of Object.entries(value)) {
    if (["turns", "messages", "items", "history"].includes(key)) {
      continue;
    }
    const found = findValue(nested, keys, depth + 1);
    if (found) {
      return found;
    }
  }
  return null;
}

export function getCodexBridge() {
  if (!globalThis[globalKey] || globalThis[globalKey].version !== BRIDGE_VERSION || typeof globalThis[globalKey].track !== "function") {
    globalThis[globalKey]?.dispose?.();
    globalThis[globalKey] = new CodexBridge();
  }
  return globalThis[globalKey];
}
