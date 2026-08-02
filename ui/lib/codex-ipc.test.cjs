const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const test = require("node:test");

const source = readFileSync(require.resolve("./codex-ipc.js"), "utf8");
const modulePromise = import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

test("extracts snapshot and patch runtime metadata", async () => {
  const { codexThreadMetadata } = await modulePromise;
  assert.deepEqual(codexThreadMetadata({
    conversationId: "thread-1",
    change: {
      type: "snapshot",
      conversationState: {
        id: "thread-1",
        title: "Session one",
        cwd: "/workspace",
        threadRuntimeStatus: { type: "idle" },
        latestThreadSettings: {
          model: "gpt-5.6-sol",
          effort: "high",
          serviceTier: "priority"
        }
      }
    }
  }), {
    threadId: "thread-1",
    status: "idle",
    cwd: "/workspace",
    title: "Session one",
    model: "gpt-5.6-sol",
    effort: "high",
    serviceTier: "priority"
  });

  assert.equal(codexThreadMetadata({
    conversationId: "thread-1",
    change: { patches: [{ path: ["threadRuntimeStatus", "type"], value: "active" }] }
  }).status, "active");

  assert.equal(codexThreadMetadata({
    conversationId: "thread-1",
    change: {
      type: "snapshot",
      conversationState: {
        threadRuntimeStatus: { type: "idle" },
        turnHistory: {
          history: {
            entitiesByKey: {
              pending: { status: "inProgress", items: [] }
            }
          }
        }
      }
    }
  }).status, "active");

  assert.equal(codexThreadMetadata({
    conversationId: "thread-1",
    change: {
      type: "patches",
      patches: [{
        path: ["turnHistory", "history", "entitiesByKey", "turn-1", "status"],
        value: "completed"
      }]
    }
  }).status, "idle");

  assert.equal(codexThreadMetadata({
    conversationId: "thread-1",
    change: {
      type: "snapshot",
      conversationState: {
        threadRuntimeStatus: { type: "notLoaded" },
        turnHistory: {
          history: {
            entitiesByKey: {
              stale: { status: "inProgress", items: [] }
            }
          }
        }
      }
    }
  }).status, "notLoaded");

  assert.equal(codexThreadMetadata({
    conversationId: "thread-1",
    change: {
      type: "patches",
      patches: [
        {
          path: ["turnHistory", "history", "entitiesByKey", "old", "status"],
          value: "completed"
        },
        {
          path: ["turnHistory", "history", "entitiesByKey", "new", "status"],
          value: "inProgress"
        }
      ]
    }
  }).status, "active");

  assert.deepEqual(codexThreadMetadata({
    conversationId: "thread-1",
    change: {
      patches: [
        { path: ["latestThreadSettings", "model"], value: "gpt-5.6-luna" },
        { path: ["latestThreadSettings", "effort"], value: "low" },
        { path: ["latestThreadSettings", "serviceTier"], value: null }
      ]
    }
  }), {
    threadId: "thread-1",
    model: "gpt-5.6-luna",
    effort: "low",
    serviceTier: null
  });

  assert.equal(codexThreadMetadata({
    conversationId: "thread-1",
    change: {
      patches: [{ path: ["threadRuntimeStatus"], value: { type: "systemError" } }],
      turns: [{ error: { message: "context window exceeded" } }]
    }
  }).error, "context window exceeded");
});

test("keeps a live active turn ahead of a racing not-loaded report", async () => {
  const { codexThreadMetadata } = await modulePromise;
  const turnPatch = (status) => ({
    path: ["turnHistory", "history", "entitiesByKey", "turn-1", "status"],
    value: status
  });
  const streamed = (patches) => codexThreadMetadata({
    conversationId: "thread-1",
    change: { type: "patches", patches }
  }).status;

  assert.equal(
    streamed([{ path: ["threadRuntimeStatus", "type"], value: "notLoaded" }, turnPatch("inProgress")]),
    "active"
  );
  assert.equal(
    streamed([{ path: ["threadRuntimeStatus", "type"], value: "notLoaded" }, turnPatch("completed")]),
    "notLoaded"
  );
  assert.equal(
    streamed([{ path: ["threadRuntimeStatus", "type"], value: "systemError" }, turnPatch("inProgress")]),
    "systemError"
  );
  assert.equal(streamed([turnPatch("inProgress")]), "active");
  assert.equal(streamed([turnPatch("completed")]), "idle");
  assert.equal(streamed([{ path: ["latestThreadSettings", "model"], value: "gpt-5.6-luna" }]), undefined);

  assert.equal(codexThreadMetadata({
    conversationId: "thread-1",
    change: {
      type: "snapshot",
      conversationState: {
        threadRuntimeStatus: { type: "notLoaded" },
        turnHistory: { history: { entitiesByKey: { stale: { status: "inProgress", items: [] } } } }
      }
    }
  }).status, "notLoaded");

  assert.equal(codexThreadMetadata({
    conversationId: "thread-1",
    change: { type: "snapshot", conversationState: { threadRuntimeStatus: { type: "idle" } } }
  }).status, "idle");
});

test("aggregates a live active turn over a not-loaded source", async () => {
  const { CodexBridge } = await modulePromise;
  const bridge = Object.create(CodexBridge.prototype);
  bridge.workspace = "/workspace";
  bridge.knownThreads = new Set(["thread-1"]);
  bridge.runtimeBySource = new Map([
    ["vscode", new Map([["thread-1", { threadId: "thread-1", status: "notLoaded" }]])]
  ]);
  bridge.pendingThreads = new Set();
  bridge.unconfirmedThreads = new Set();
  bridge.followTimers = new Map();
  bridge.emit = () => {};

  bridge.updateRuntime("desktop", {
    conversationId: "thread-1",
    change: {
      type: "patches",
      patches: [
        { path: ["threadRuntimeStatus", "type"], value: "notLoaded" },
        { path: ["turnHistory", "history", "entitiesByKey", "turn-1", "status"], value: "inProgress" }
      ]
    }
  });

  assert.equal(bridge.aggregateRuntime().get("thread-1").status, "active");
});

test("a targeted re-follow keeps runtime other sources already reported", async () => {
  const { CodexBridge } = await modulePromise;
  const bridge = Object.create(CodexBridge.prototype);
  const sent = [];
  const events = [];
  bridge.clientId = "teamflow";
  bridge.revision = 0;
  bridge.knownThreads = new Set(["thread-1"]);
  bridge.runtimeBySource = new Map([
    ["desktop", new Map([["thread-1", { threadId: "thread-1", status: "active" }]])]
  ]);
  bridge.pendingThreads = new Set();
  bridge.unconfirmedThreads = new Set();
  bridge.followTimers = new Map();
  bridge.emit = (...args) => events.push(args);
  bridge.send = (payload) => sent.push(payload);

  bridge.onMessage({
    method: "thread-stream-following-status-requested",
    version: 1,
    sourceClientId: "vscode",
    params: { conversationId: "thread-1", hostId: "local" }
  });

  assert.equal(bridge.aggregateRuntime().get("thread-1").status, "active");
  assert.equal(bridge.runtimeBySource.get("desktop").get("thread-1").status, "active");
  assert.equal(bridge.pendingThreads.size, 0);
  assert.equal(bridge.followTimers.size, 0);
  assert.deepEqual(events, []);
  assert.deepEqual(sent.at(-1).targetClientIds, ["vscode"]);
  assert.equal(sent.at(-1).params.following, true);
});

test("a targeted re-follow still checks a thread no source reported", async () => {
  const { CodexBridge } = await modulePromise;
  const bridge = Object.create(CodexBridge.prototype);
  const events = [];
  bridge.clientId = "teamflow";
  bridge.revision = 0;
  bridge.knownThreads = new Set(["thread-1"]);
  bridge.runtimeBySource = new Map();
  bridge.pendingThreads = new Set();
  bridge.unconfirmedThreads = new Set();
  bridge.followTimers = new Map();
  bridge.emit = (...args) => events.push(args);
  bridge.send = () => {};

  bridge.onMessage({
    method: "thread-stream-following-status-requested",
    version: 1,
    sourceClientId: "vscode",
    params: { conversationId: "thread-1", hostId: "local" }
  });

  assert.equal(bridge.pendingThreads.has("thread-1"), true);
  assert.equal(events.at(-1)[1].status, "checking");
  for (const timer of bridge.followTimers.values()) {
    clearTimeout(timer);
  }
});

test("every published event and snapshot carries a monotonic revision", async () => {
  const { CodexBridge } = await modulePromise;
  const bridge = Object.create(CodexBridge.prototype);
  const events = [];
  bridge.connected = true;
  bridge.revision = 0;
  bridge.knownThreads = new Set(["thread-1"]);
  bridge.runtimeBySource = new Map();
  bridge.pendingThreads = new Set();
  bridge.unconfirmedThreads = new Set();
  bridge.followTimers = new Map();
  bridge.emit = (...args) => events.push(args);

  bridge.updateRuntime("desktop", {
    conversationId: "thread-1",
    change: { patches: [{ path: ["threadRuntimeStatus", "type"], value: "idle" }] }
  });
  const stale = bridge.snapshot();
  bridge.updateRuntime("desktop", {
    conversationId: "thread-1",
    change: { patches: [{ path: ["threadRuntimeStatus", "type"], value: "active" }] }
  });
  const fresh = bridge.snapshot();

  assert.equal(events[0][1].revision, 1);
  assert.equal(events[1][1].revision, 2);
  assert.equal(stale.revision, 1);
  assert.equal(fresh.revision, 2);
  assert.ok(fresh.revision > stale.revision);
  assert.equal(stale.sessions[0].status, "idle");
  assert.equal(fresh.sessions[0].status, "active");
});

test("a stale polled snapshot never rolls back a newer streamed update", async () => {
  const { acceptsRuntimeRevision } = await import(
    `data:text/javascript;base64,${Buffer.from(readFileSync(require.resolve("./runtime-revision.js"), "utf8")).toString("base64")}`
  );
  // The POST left at revision 4 and returned after the stream already delivered revision 6.
  assert.equal(acceptsRuntimeRevision(6, 4), false);
  assert.equal(acceptsRuntimeRevision(6, 6), true);
  assert.equal(acceptsRuntimeRevision(6, 7), true);
  assert.equal(acceptsRuntimeRevision(-1, 0), true);
  // A payload from a bridge that predates revisions must still be applied.
  assert.equal(acceptsRuntimeRevision(6, undefined), true);
});

test("agent mutations are allowed only for a bridge-confirmed safe status", async () => {
  const { agentMutationAllowed } = await modulePromise;
  const snapshotFor = (status) => ({ sessions: [{ threadId: "thread-1", status }] });

  for (const status of ["idle", "notLoaded", "systemError"]) {
    assert.equal(agentMutationAllowed(snapshotFor(status), "thread-1"), true, status);
  }
  for (const status of ["active", "checking", "unconfirmed", "", undefined]) {
    assert.equal(agentMutationAllowed(snapshotFor(status), "thread-1"), false, String(status));
  }
  assert.equal(agentMutationAllowed({ sessions: [] }, "thread-1"), false);
  assert.equal(agentMutationAllowed({}, "thread-1"), false);
  assert.equal(agentMutationAllowed(snapshotFor("idle"), "thread-other"), false);
});

test("the agent mutation guard reads the bridge instead of the submitted form", async () => {
  const actions = readFileSync(require.resolve("./actions.js"), "utf8");
  const guard = actions.slice(actions.indexOf("function blockActiveAgent"));
  const body = guard.slice(0, guard.indexOf("\n}\n"));

  assert.match(body, /agentMutationAllowed\(getCodexBridge\(\)\.snapshot\(\)/);
  assert.doesNotMatch(body, /runtime_status/);
  assert.doesNotMatch(readFileSync(require.resolve("../app/teamflow-client.jsx"), "utf8"), /name="runtime_status"/);
});

test("registers as a follower after IPC initialization", async () => {
  const { CodexBridge } = await modulePromise;
  const bridge = Object.create(CodexBridge.prototype);
  const followed = [];
  bridge.initializeRequestId = "initialize-1";
  bridge.knownThreads = new Set(["thread-1"]);
  bridge.requestFollow = (threadId, targetClientIds) => followed.push({ threadId, targetClientIds });

  bridge.onMessage({
    type: "response",
    requestId: "initialize-1",
    resultType: "success",
    result: { clientId: "teamflow-client" }
  });

  assert.equal(bridge.clientId, "teamflow-client");
  assert.deepEqual(followed, [{ threadId: "thread-1", targetClientIds: undefined }]);
});

test("re-announces a tracked follower when a Codex owner appears", async () => {
  const { CodexBridge } = await modulePromise;
  const bridge = Object.create(CodexBridge.prototype);
  const followed = [];
  bridge.knownThreads = new Set(["thread-1"]);
  bridge.requestFollow = (threadId, targetClientIds) => followed.push({ threadId, targetClientIds });

  bridge.onMessage({
    type: "broadcast",
    method: "thread-stream-following-status-requested",
    version: 1,
    sourceClientId: "codex-owner",
    params: { conversationId: "thread-1", hostId: "local" }
  });

  assert.deepEqual(followed, [{ threadId: "thread-1", targetClientIds: ["codex-owner"] }]);
});

test("removes stale runtime state when a source stops following", async () => {
  const { CodexBridge } = await modulePromise;
  const bridge = Object.create(CodexBridge.prototype);
  const events = [];
  bridge.revision = 0;
  bridge.knownThreads = new Set(["thread-1"]);
  bridge.runtimeBySource = new Map([
    ["desktop", new Map([["thread-1", { threadId: "thread-1", status: "active" }]])],
    ["vscode", new Map([["thread-1", { threadId: "thread-1", status: "idle" }]])]
  ]);
  bridge.pendingThreads = new Set();
  bridge.unconfirmedThreads = new Set();
  bridge.emit = (...args) => events.push(args);

  bridge.onMessage({
    type: "broadcast",
    method: "thread-stream-following-changed",
    version: 1,
    sourceClientId: "desktop",
    params: {
      conversationId: "thread-1",
      hostId: "local",
      following: false
    }
  });

  assert.equal(bridge.runtimeBySource.has("desktop"), false);
  assert.equal(bridge.aggregateRuntime().get("thread-1").status, "idle");
  assert.deepEqual(events.at(-1), [
    "event",
    { type: "runtime", threadId: "thread-1", status: "idle", revision: 1 }
  ]);
});

test("retains an explicit not-loaded state when its source stops following", async () => {
  const { CodexBridge } = await modulePromise;
  const bridge = Object.create(CodexBridge.prototype);
  const events = [];
  bridge.knownThreads = new Set(["thread-1"]);
  bridge.runtimeBySource = new Map([
    ["desktop", new Map([["thread-1", { threadId: "thread-1", status: "notLoaded" }]])]
  ]);
  bridge.pendingThreads = new Set();
  bridge.unconfirmedThreads = new Set();
  bridge.emit = (...args) => events.push(args);

  bridge.onMessage({
    type: "broadcast",
    method: "thread-stream-following-changed",
    version: 1,
    sourceClientId: "desktop",
    params: {
      conversationId: "thread-1",
      hostId: "local",
      following: false
    }
  });

  assert.equal(bridge.aggregateRuntime().get("thread-1").status, "notLoaded");
  assert.deepEqual(events, []);
});

test("reports a pending follower as checking rather than not loaded", async () => {
  const { CodexBridge } = await modulePromise;
  const bridge = Object.create(CodexBridge.prototype);
  bridge.connected = true;
  bridge.runtimeBySource = new Map();
  bridge.pendingThreads = new Set(["thread-1"]);
  bridge.unconfirmedThreads = new Set();

  assert.deepEqual([...bridge.aggregateRuntime().values()], [{ threadId: "thread-1", status: "checking" }]);
});

test("reports an unresponsive tracked thread as not loaded while Codex is connected", async () => {
  const { CodexBridge } = await modulePromise;
  const bridge = Object.create(CodexBridge.prototype);
  bridge.connected = true;
  bridge.runtimeBySource = new Map();
  bridge.pendingThreads = new Set();
  bridge.unconfirmedThreads = new Set(["thread-1"]);

  assert.deepEqual([...bridge.aggregateRuntime().values()], [{ threadId: "thread-1", status: "notLoaded" }]);
});

test("does not restart an unconfirmed follow check when POST tracks the same thread", async () => {
  const { CodexBridge } = await modulePromise;
  const bridge = Object.create(CodexBridge.prototype);
  const followed = [];
  bridge.clientId = "teamflow-client";
  bridge.knownThreads = new Set(["thread-1"]);
  bridge.unconfirmedThreads = new Set(["thread-1"]);
  bridge.requestFollow = (threadId) => followed.push(threadId);

  bridge.track(["thread-1"]);

  assert.deepEqual(followed, []);
  assert.deepEqual(bridge.knownThreads, new Set(["thread-1"]));
});

test("reports tracked threads as unconfirmed while the IPC bridge is disconnected", async () => {
  const { CodexBridge } = await modulePromise;
  const bridge = Object.create(CodexBridge.prototype);
  bridge.connected = false;
  bridge.endpointState = "unconfirmed";
  bridge.knownThreads = new Set(["thread-1"]);
  bridge.runtimeBySource = new Map();
  bridge.pendingThreads = new Set();
  bridge.unconfirmedThreads = new Set();

  assert.deepEqual([...bridge.aggregateRuntime().values()], [{ threadId: "thread-1", status: "unconfirmed" }]);
});

test("reports tracked threads as not loaded when no Codex IPC endpoint exists", async () => {
  const { CodexBridge } = await modulePromise;
  const bridge = Object.create(CodexBridge.prototype);
  bridge.connected = false;
  bridge.endpointState = "absent";
  bridge.knownThreads = new Set(["thread-1"]);
  bridge.runtimeBySource = new Map();
  bridge.pendingThreads = new Set();
  bridge.unconfirmedThreads = new Set();

  assert.deepEqual([...bridge.aggregateRuntime().values()], [{ threadId: "thread-1", status: "notLoaded" }]);
});

test("reports tracked threads as checking while the IPC endpoint is being probed", async () => {
  const { CodexBridge } = await modulePromise;
  const bridge = Object.create(CodexBridge.prototype);
  bridge.connected = false;
  bridge.endpointState = "probing";
  bridge.knownThreads = new Set(["thread-1"]);
  bridge.runtimeBySource = new Map();
  bridge.pendingThreads = new Set();
  bridge.unconfirmedThreads = new Set();

  assert.deepEqual([...bridge.aggregateRuntime().values()], [{ threadId: "thread-1", status: "checking" }]);
});

test("distinguishes an absent IPC listener from a transport failure", async () => {
  const { endpointStateForConnectionErrors } = await modulePromise;

  assert.equal(endpointStateForConnectionErrors([{ code: "ECONNREFUSED" }]), "absent");
  assert.equal(endpointStateForConnectionErrors([{ code: "ENOENT" }]), "absent");
  assert.equal(endpointStateForConnectionErrors([{ code: "EACCES" }]), "unconfirmed");
});

test("refreshes the session catalog when Codex invalidates its task cache", async () => {
  const { CodexBridge } = await modulePromise;
  const bridge = Object.create(CodexBridge.prototype);
  let refreshes = 0;
  bridge.scheduleCatalogRefresh = () => {
    refreshes += 1;
  };

  bridge.onMessage({
    type: "broadcast",
    method: "query-cache-invalidate",
    params: { queryKey: ["tasks"] }
  });

  assert.equal(refreshes, 1);
});
