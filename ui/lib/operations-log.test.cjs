const assert = require("node:assert/strict");
const test = require("node:test");

const { visibleForWorkspace, workspaceFromLine } = require("./operations-log.cjs");

test("workspace logs expose global and current-project lines only", () => {
  const aliases = ["电子宠物", "pet", "/Users/ethan/projects/small-businesses/pet"];

  assert.equal(visibleForWorkspace("2026-08-30 DAEMON RUNNING pid=42", aliases), true);
  assert.equal(
    visibleForWorkspace("2026-08-30 [电子宠物 @software-development] DISPATCH STARTED", aliases),
    true
  );
  assert.equal(
    visibleForWorkspace('2026-08-30 AGENT CONTEXT RESTORED workspace="电子宠物" role=pm', aliases),
    true
  );
  assert.equal(
    visibleForWorkspace("2026-08-30 [alpha191-quant @software-development] DISPATCH STARTED", aliases),
    false
  );
});

test("workspace extraction supports namespace and structured fields", () => {
  assert.equal(workspaceFromLine("[电子宠物 @software-development] DISPATCH"), "电子宠物");
  assert.equal(workspaceFromLine('AGENT CONTEXT workspace="alpha191-quant"'), "alpha191-quant");
  assert.equal(workspaceFromLine("DAEMON LISTENING apps=1"), null);
});
