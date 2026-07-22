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

test("reports a pending follower as checking rather than not loaded", async () => {
  const { CodexBridge } = await modulePromise;
  const bridge = Object.create(CodexBridge.prototype);
  bridge.runtimeBySource = new Map();
  bridge.pendingThreads = new Set(["thread-1"]);
  bridge.unconfirmedThreads = new Set();

  assert.deepEqual([...bridge.aggregateRuntime().values()], [{ threadId: "thread-1", status: "checking" }]);
});

test("does not infer not loaded when Codex returns no snapshot", async () => {
  const { CodexBridge } = await modulePromise;
  const bridge = Object.create(CodexBridge.prototype);
  bridge.runtimeBySource = new Map();
  bridge.pendingThreads = new Set();
  bridge.unconfirmedThreads = new Set(["thread-1"]);

  assert.deepEqual([...bridge.aggregateRuntime().values()], [{ threadId: "thread-1", status: "unconfirmed" }]);
});
