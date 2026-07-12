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
