/*
 * Zero-dependency unit tests for content-core.js's pure functions
 * (prioritize, computeLabelPosition). No browser, no jsdom -- these operate
 * on plain {kind, rect} data, never a real DOM element, so plain
 * `node --test` is sufficient. DOM-touching functions (isVisible,
 * discoverCandidates) are exercised separately by the Playwright-driven
 * Python tests, which need real getBoundingClientRect()/getComputedStyle()
 * behavior.
 *
 * Run: node --test browser_extension/content-core.test.js
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("./content-core.js");

test("prioritize: text-entry/button/link kinds rank before generic aria", () => {
  const candidates = [
    { kind: "aria", rect: { x: 10, y: 10, width: 20, height: 20 }, ref: "aria-1" },
    { kind: "link", rect: { x: 10, y: 10, width: 20, height: 20 }, ref: "link-1" },
    { kind: "button", rect: { x: 10, y: 10, width: 20, height: 20 }, ref: "button-1" },
    { kind: "input", rect: { x: 10, y: 10, width: 20, height: 20 }, ref: "input-1" },
  ];
  const out = core.prioritize(candidates, { x: 15, y: 15 }, 10);
  assert.deepEqual(
    out.map((c) => c.ref),
    ["input-1", "button-1", "link-1", "aria-1"]
  );
});

test("prioritize: within the same kind, closer to viewport center wins", () => {
  const candidates = [
    { kind: "button", rect: { x: 900, y: 900, width: 10, height: 10 }, ref: "far" },
    { kind: "button", rect: { x: 500, y: 500, width: 10, height: 10 }, ref: "near" },
  ];
  const out = core.prioritize(candidates, { x: 505, y: 505 }, 10);
  assert.deepEqual(
    out.map((c) => c.ref),
    ["near", "far"]
  );
});

test("prioritize: caps output at maxCount", () => {
  const candidates = [];
  for (let i = 0; i < 20; i++) {
    candidates.push({
      kind: "button",
      rect: { x: i * 10, y: i * 10, width: 10, height: 10 },
      ref: "b" + i,
    });
  }
  const out = core.prioritize(candidates, { x: 0, y: 0 }, 5);
  assert.equal(out.length, 5);
});

test("prioritize: does not mutate input array or touch ref beyond carrying it through", () => {
  const candidates = [
    { kind: "button", rect: { x: 0, y: 0, width: 1, height: 1 }, ref: { marker: true } },
  ];
  const out = core.prioritize(candidates, { x: 0, y: 0 }, 5);
  assert.equal(out[0].ref, candidates[0].ref); // same object identity, untouched
  assert.equal(candidates.length, 1);
});

test("computeLabelPosition: places label above the element by default", () => {
  const pos = core.computeLabelPosition({ x: 100, y: 100, width: 50, height: 20 }, 1000, 1000);
  assert.ok(pos.top < 100, "label should sit above the element");
});

test("computeLabelPosition: falls inside the element when there's no room above", () => {
  const pos = core.computeLabelPosition({ x: 100, y: 2, width: 50, height: 20 }, 1000, 1000);
  assert.ok(pos.top >= 0, "label must not render off the top of the viewport");
});

test("computeLabelPosition: clamps within the viewport horizontally", () => {
  const pos = core.computeLabelPosition({ x: -10, y: 100, width: 50, height: 20 }, 1000, 1000);
  assert.ok(pos.left >= 0);
  const pos2 = core.computeLabelPosition({ x: 995, y: 100, width: 50, height: 20 }, 1000, 1000);
  assert.ok(pos2.left + 22 <= 1000);
});
