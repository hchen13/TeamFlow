import { execFileSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";

const LIVE_STATUSES = new Set(["active", "idle"]);
const PROCESS_CACHE_MS = 1000;

export function codexRuntimeRoot() {
  const teamflowHome = path.resolve(process.env.TEAMFLOW_HOME || path.join(homedir(), ".teamflow"));
  return path.join(teamflowHome, "codex-runtime");
}

export function readCodexHookRuntime({
  root = codexRuntimeRoot(),
  threadIds,
  workspace,
  processCache = new Map(),
  inspectProcess = defaultProcessInspector
}) {
  if (!existsSync(root)) {
    return new Map();
  }
  const records = [];
  let names;
  try {
    names = readdirSync(root);
  } catch {
    return new Map();
  }
  for (const name of names) {
    if (!name.endsWith(".json")) {
      continue;
    }
    try {
      records.push(JSON.parse(readFileSync(path.join(root, name), "utf8")));
    } catch {
      // A concurrent atomic replace or a stale malformed file cannot define live state.
    }
  }
  return aggregateCodexHookRuntime(records, {
    threadIds,
    workspace,
    processCache,
    inspectProcess
  });
}

export function aggregateCodexHookRuntime(records, {
  threadIds,
  workspace,
  processCache = new Map(),
  inspectProcess = defaultProcessInspector,
  now = Date.now()
}) {
  const knownThreads = new Set(threadIds);
  const owners = new Map();
  const result = new Map();
  for (const record of records) {
    if (!validRecord(record) || !knownThreads.has(record.session_id)) {
      continue;
    }
    if (record.cwd && !insideWorkspace(record.cwd, workspace)) {
      continue;
    }
    let startedAt = owners.get(record.owner_pid);
    if (startedAt === undefined) {
      startedAt = inspectProcess(record.owner_pid, processCache, now);
      owners.set(record.owner_pid, startedAt);
    }
    if (!startedAt || startedAt !== record.owner_started_at) {
      continue;
    }
    const runtime = {
      threadId: record.session_id,
      status: record.status,
      ...(record.cwd ? { cwd: record.cwd } : {}),
      ...(record.model ? { model: record.model } : {})
    };
    const current = result.get(record.session_id);
    if (!current || statusRank(runtime.status) > statusRank(current.status)
      || (statusRank(runtime.status) === statusRank(current.status)
        && Number(record.updated_at_ms || 0) >= Number(current.updatedAt || 0))) {
      result.set(record.session_id, { ...runtime, updatedAt: Number(record.updated_at_ms || 0) });
    }
  }
  for (const [threadId, runtime] of result) {
    const { updatedAt, ...publicRuntime } = runtime;
    result.set(threadId, publicRuntime);
  }
  return result;
}

function validRecord(record) {
  return record?.schema_version === 1
    && typeof record.session_id === "string"
    && Number.isInteger(record.owner_pid)
    && record.owner_pid > 1
    && typeof record.owner_started_at === "string"
    && LIVE_STATUSES.has(record.status);
}

function defaultProcessInspector(pid, cache, now) {
  const cached = cache.get(pid);
  if (cached && now - cached.checkedAt < PROCESS_CACHE_MS && processAlive(pid)) {
    return cached.startedAt;
  }
  if (!processAlive(pid)) {
    cache.delete(pid);
    return null;
  }
  if (process.platform !== "darwin" && process.platform !== "linux") {
    return cached?.startedAt || null;
  }
  try {
    const startedAt = execFileSync("/bin/ps", ["-p", String(pid), "-o", "lstart="], {
      encoding: "utf8",
      timeout: 1000
    }).trim();
    if (!startedAt) {
      cache.delete(pid);
      return null;
    }
    cache.set(pid, { checkedAt: now, startedAt });
    return startedAt;
  } catch {
    cache.delete(pid);
    return null;
  }
}

function processAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function insideWorkspace(cwd, workspace) {
  const relative = path.relative(path.resolve(workspace), path.resolve(cwd));
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

function statusRank(status) {
  return status === "active" ? 2 : 1;
}
