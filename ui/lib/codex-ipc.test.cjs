const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const test = require("node:test");

// The UI package is CommonJS, so its ES modules are loaded as data: URLs. Those cannot resolve a
// relative import, so a module's own dependencies are inlined as nested data: URLs first.
const dataUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const read = (specifier) => readFileSync(require.resolve(specifier), "utf8");
const load = (specifier, imports = {}) => import(
  dataUrl(
    Object.entries(imports).reduce(
      (source, [from, target]) => source.replaceAll(`"${from}"`, JSON.stringify(dataUrl(read(target)))),
      read(specifier)
    )
  )
);
const runtimePromise = load("./codex-runtime-state.js");
const modulePromise = load("./codex-ipc.js", {
  "./codex-runtime-state": "./codex-runtime-state.js"
});
const rulesPromise = load("./agent-runtime-rules.js");
const mutationsPromise = load("./agent-mutations.js", {
  "./agent-runtime-rules": "./agent-runtime-rules.js"
});

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
    streamed([{ path: ["threadRuntimeStatus", "type"], value: "systemError" }, turnPatch("completed")]),
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

test("hook runtime accepts only registered workspace sessions owned by a live Codex process", async () => {
  const { aggregateCodexHookRuntime } = await runtimePromise;
  const record = (overrides = {}) => ({
    schema_version: 1,
    session_id: "thread-1",
    owner_pid: 4100,
    owner_started_at: "owner-start",
    status: "idle",
    cwd: "/workspace/project",
    updated_at_ms: 10,
    ...overrides
  });
  const owners = new Map([
    [4100, "owner-start"],
    [4200, "different-start"]
  ]);

  const runtime = aggregateCodexHookRuntime([
    record(),
    record({ status: "active", updated_at_ms: 5 }),
    record({ owner_pid: 4200, owner_started_at: "stale-start" }),
    record({ session_id: "unregistered" }),
    record({ cwd: "/other/project" })
  ], {
    threadIds: ["thread-1"],
    workspace: "/workspace",
    inspectProcess: (pid) => owners.get(pid) || null
  });

  assert.deepEqual([...runtime.values()], [{
    threadId: "thread-1",
    status: "active",
    cwd: "/workspace/project"
  }]);
});

test("official hook lifecycle state overrides a stale private IPC not-loaded report", async () => {
  const { CodexBridge } = await modulePromise;
  const bridge = Object.create(CodexBridge.prototype);
  bridge.connected = true;
  bridge.knownThreads = new Set(["thread-1"]);
  bridge.runtimeBySource = new Map([
    ["desktop", new Map([["thread-1", { threadId: "thread-1", status: "notLoaded" }]])]
  ]);
  bridge.hookRuntime = new Map([
    ["thread-1", { threadId: "thread-1", status: "idle" }]
  ]);
  bridge.pendingThreads = new Set();
  bridge.unconfirmedThreads = new Set();

  assert.equal(bridge.aggregateRuntime().get("thread-1").status, "idle");

  bridge.hookRuntime.set("thread-1", { threadId: "thread-1", status: "active" });
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
  const { acceptRuntimeSequence, createRuntimeSequence } = await rulesPromise;
  const tracker = createRuntimeSequence();

  assert.equal(acceptRuntimeSequence(tracker, { epoch: "epoch-a", revision: 6 }), true);
  // The POST left at revision 4 and returned after the stream already delivered revision 6.
  assert.equal(acceptRuntimeSequence(tracker, { epoch: "epoch-a", revision: 4 }), false);
  assert.equal(acceptRuntimeSequence(tracker, { epoch: "epoch-a", revision: 6 }), true);
  assert.equal(acceptRuntimeSequence(tracker, { epoch: "epoch-a", revision: 7 }), true);
  // A payload from a bridge that predates the sequence carries no ordering claim.
  assert.equal(acceptRuntimeSequence(tracker, {}), true);
  assert.equal(tracker.epoch, "epoch-a");
  assert.equal(tracker.revision, 7);
});

test("a retired bridge epoch can never flow back over the current one", async () => {
  const { acceptRuntimeSequence, createRuntimeSequence } = await rulesPromise;
  const tracker = createRuntimeSequence();
  const applied = [];
  const apply = (sequence) => {
    if (acceptRuntimeSequence(tracker, sequence)) {
      applied.push(sequence);
    }
  };

  apply({ epoch: "old", revision: 9 });
  apply({ epoch: "new", revision: 1 });
  // The rebuilt bridge is current, so a late payload from the retired instance is dropped even
  // though its revision is higher than the one now applied.
  apply({ epoch: "old", revision: 8 });
  apply({ epoch: "old", revision: 99 });

  assert.deepEqual(applied.at(-1), { epoch: "new", revision: 1 });
  assert.equal(tracker.epoch, "new");
  assert.equal(tracker.revision, 1);

  apply({ epoch: "new", revision: 2 });
  assert.deepEqual(applied.at(-1), { epoch: "new", revision: 2 });
});

test("agent mutations are allowed only for a bridge-confirmed idle or unloaded session", async () => {
  const { agentMutationAllowed, runtimeStatusAllowsMutation } = await rulesPromise;
  const snapshotFor = (status) => ({ sessions: [{ threadId: "thread-1", status }] });

  for (const status of ["idle", "notLoaded"]) {
    assert.equal(runtimeStatusAllowsMutation(status), true, status);
    assert.equal(agentMutationAllowed(snapshotFor(status), "thread-1"), true, status);
  }
  for (const status of ["active", "systemError", "checking", "unconfirmed", "", undefined]) {
    assert.equal(runtimeStatusAllowsMutation(status), false, String(status));
    assert.equal(agentMutationAllowed(snapshotFor(status), "thread-1"), false, String(status));
  }
  assert.equal(agentMutationAllowed({ sessions: [] }, "thread-1"), false);
  assert.equal(agentMutationAllowed({}, "thread-1"), false);
  assert.equal(agentMutationAllowed(snapshotFor("idle"), "thread-other"), false);
  assert.equal(agentMutationAllowed(snapshotFor("idle"), ""), false);
});

test("a running turn outranks a systemError from another source", async () => {
  const { CodexBridge } = await modulePromise;
  const bridge = Object.create(CodexBridge.prototype);
  bridge.connected = true;
  bridge.knownThreads = new Set(["thread-1"]);
  bridge.runtimeBySource = new Map([
    ["vscode", new Map([["thread-1", { threadId: "thread-1", status: "systemError" }]])],
    ["desktop", new Map([["thread-1", { threadId: "thread-1", status: "active" }]])]
  ]);
  bridge.pendingThreads = new Set();
  bridge.unconfirmedThreads = new Set();

  assert.equal(bridge.aggregateRuntime().get("thread-1").status, "active");

  bridge.runtimeBySource = new Map([
    ["desktop", new Map([["thread-1", { threadId: "thread-1", status: "systemError" }]])],
    ["vscode", new Map([["thread-1", { threadId: "thread-1", status: "idle" }]])]
  ]);
  assert.equal(bridge.aggregateRuntime().get("thread-1").status, "systemError");
});

test("a systemError reported alongside a running turn still reads as active", async () => {
  const { codexThreadMetadata } = await modulePromise;

  assert.equal(codexThreadMetadata({
    conversationId: "thread-1",
    change: {
      type: "patches",
      patches: [
        { path: ["threadRuntimeStatus", "type"], value: "systemError" },
        { path: ["turnHistory", "history", "entitiesByKey", "turn-1", "status"], value: "inProgress" }
      ]
    }
  }).status, "active");

  assert.equal(codexThreadMetadata({
    conversationId: "thread-1",
    change: {
      type: "snapshot",
      conversationState: {
        threadRuntimeStatus: { type: "systemError" },
        turnHistory: { history: { entitiesByKey: { running: { status: "inProgress" } } } }
      }
    }
  }).status, "active");

  assert.equal(codexThreadMetadata({
    conversationId: "thread-1",
    change: {
      type: "snapshot",
      conversationState: { threadRuntimeStatus: { type: "systemError" } }
    }
  }).status, "systemError");
});

test("a submitted session cannot redirect the check away from the acted-on agent", async () => {
  const { agentForMutation, agentMutationAllowed } = await rulesPromise;
  const state = {
    agents: [
      { id: "agent-busy", session_id: "thread-busy", assignment_revision: 4 },
      { id: "agent-free", session_id: "thread-free", assignment_revision: 2 }
    ]
  };
  const snapshot = {
    sessions: [
      { threadId: "thread-busy", status: "active" },
      { threadId: "thread-free", status: "idle" }
    ]
  };

  // A forged form names the busy agent while pointing the check at the idle session.
  const acted = agentForMutation(state, "agent-busy");
  assert.equal(acted.session_id, "thread-busy");
  assert.equal(acted.assignment_revision, 4);
  assert.equal(agentMutationAllowed(snapshot, acted.session_id), false);

  const free = agentForMutation(state, "agent-free");
  assert.equal(free.session_id, "thread-free");
  assert.equal(agentMutationAllowed(snapshot, free.session_id), true);
  assert.equal(agentForMutation(state, "agent-missing"), null);
  assert.equal(agentForMutation({}, "agent-busy"), null);
});

test("a forged form cannot move the check or the revision off the server-side agent", async () => {
  const { planAgentMutation } = await mutationsPromise;
  const checked = [];
  const deps = {
    readState: async () => ({
      agents: [
        { id: "agent-busy", session_id: "thread-busy", assignment_revision: 7 },
        { id: "agent-free", session_id: "thread-free", assignment_revision: 2 }
      ]
    }),
    readSnapshot: () => {
      checked.push("snapshot");
      return {
        sessions: [
          { threadId: "thread-busy", status: "active" },
          { threadId: "thread-free", status: "idle" }
        ]
      };
    }
  };
  const forged = (agentId) => new Map([
    ["agent_id", agentId],
    ["session_id", "thread-free"],
    ["current_session_id", "thread-free"],
    ["runtime_status", "idle"],
    ["expected_revision", "1"]
  ]);

  const blocked = await planAgentMutation(deps, "unregister-agent", forged("agent-busy"));
  assert.equal(blocked.blocked, true);
  assert.equal(blocked.checkedSession, "thread-busy", "the busy agent's own session must be checked");
  assert.equal(blocked.args, undefined);
  assert.equal(checked.length, 1);

  const allowed = await planAgentMutation(deps, "unregister-agent", forged("agent-free"));
  assert.equal(allowed.blocked, false);
  assert.equal(allowed.checkedSession, "thread-free");
  assert.deepEqual(allowed.args, ["--agent-id", "agent-free", "--expected-revision", "2"]);

  const updated = await planAgentMutation(deps, "update-agent", forged("agent-free"));
  assert.deepEqual(updated.args, [
    "--agent-id",
    "agent-free",
    "--session-id",
    "thread-free",
    "--expected-revision",
    "2"
  ]);
  // The submitted revision is never echoed back to the CLI.
  assert.equal(updated.args.includes("1"), false);

  const missing = await planAgentMutation(deps, "unregister-agent", forged("agent-gone"));
  assert.deepEqual(missing, { blocked: true, checkedSession: null });
});

test("the agent actions delegate to the injectable mutation planner", async () => {
  const actions = readFileSync(require.resolve("./actions.js"), "utf8");
  const client = readFileSync(require.resolve("../app/teamflow-client.jsx"), "utf8");
  const mutate = actions.slice(actions.indexOf("async function mutateAgent"));

  assert.match(mutate.slice(0, mutate.indexOf("\n}\n")), /planAgentMutation\(agentMutationDeps/);
  assert.doesNotMatch(actions, /current_session_id|runtime_status/);
  assert.doesNotMatch(client, /name="runtime_status"|name="current_session_id"/);
});

test("an outdated bridge singleton is replaced", async () => {
  const { BRIDGE_VERSION, bridgeNeedsReplacement } = await modulePromise;

  assert.ok(BRIDGE_VERSION > 16, "BRIDGE_VERSION must move past the previous runtime contract");
  assert.equal(bridgeNeedsReplacement(undefined), true);
  assert.equal(bridgeNeedsReplacement({ version: BRIDGE_VERSION - 1, track: () => {} }), true);
  assert.equal(bridgeNeedsReplacement({ version: BRIDGE_VERSION }), true);
  assert.equal(bridgeNeedsReplacement({ version: BRIDGE_VERSION, track: () => {} }), false);
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
  bridge.epoch = "epoch-test";
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
    { type: "runtime", threadId: "thread-1", status: "idle", epoch: "epoch-test", revision: 1 }
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
