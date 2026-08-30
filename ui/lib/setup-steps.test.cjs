const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const test = require("node:test");

// The UI package is CommonJS, so its ES modules are loaded as data: URLs.
const modulePromise = import(
  `data:text/javascript;base64,${Buffer.from(readFileSync(require.resolve("./setup-steps.js"), "utf8")).toString("base64")}`
);

test("a workspace with no identity starts on the workflow step", async () => {
  const { initialLarkStep } = await modulePromise;

  assert.equal(initialLarkStep("", false), "workflow");
  assert.equal(initialLarkStep(undefined, false), "workflow");
});

test("a configured workspace still starts on the board step", async () => {
  const { initialLarkStep } = await modulePromise;

  assert.equal(initialLarkStep("", true), "board");
});

test("every explicit setup step remains directly navigable", async () => {
  const { initialLarkStep } = await modulePromise;

  assert.equal(initialLarkStep("identity", false), "identity");
  assert.equal(initialLarkStep("workflow", true), "workflow");
  assert.equal(initialLarkStep("board", true), "board");
  assert.equal(initialLarkStep("board", false), "board");
  assert.equal(initialLarkStep("agent", false), "agent");
  assert.equal(initialLarkStep("agent", true), "agent");
});

test("the checkbox submits enabled only when it is checked", async () => {
  const { versionControlArgument } = await modulePromise;

  assert.equal(versionControlArgument("true"), "--enable");
  assert.equal(versionControlArgument(""), "--disable");
  assert.equal(versionControlArgument(undefined), "--disable");
});
