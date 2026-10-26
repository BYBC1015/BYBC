"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const viewer = require("../codex_hub/static/image-viewer.js");

test("zoom is constrained to the supported range", () => {
  assert.equal(viewer.clampZoom(0.2), 1);
  assert.equal(viewer.clampZoom(2.5), 2.5);
  assert.equal(viewer.clampZoom(9), 6);
});

test("scope can switch between one message and the conversation", () => {
  const items = [
    { entryId: "11", filename: "a.png" },
    { entryId: "12", filename: "b.png" },
    { entryId: "11", filename: "c.png" },
  ];
  assert.deepEqual(
    viewer.scopedItems(items, "11", "message").map((item) => item.filename),
    ["a.png", "c.png"],
  );
  assert.equal(viewer.scopedItems(items, "11", "conversation").length, 3);
});

test("touch gestures distinguish navigation, close, zoomed pan, and noise", () => {
  assert.equal(
    viewer.deriveSwipeAction({ dx: -90, dy: 8, viewportWidth: 400 }),
    "next",
  );
  assert.equal(
    viewer.deriveSwipeAction({ dx: 90, dy: 8, viewportWidth: 400 }),
    "previous",
  );
  assert.equal(
    viewer.deriveSwipeAction({ dx: 10, dy: 110, viewportWidth: 400 }),
    "close",
  );
  assert.equal(
    viewer.deriveSwipeAction({ dx: -90, dy: 8, scale: 2, viewportWidth: 400 }),
    "none",
  );
  assert.equal(
    viewer.deriveSwipeAction({ dx: 12, dy: 14, viewportWidth: 400 }),
    "none",
  );
});

test("only the previous and next images are selected for preloading", () => {
  assert.deepEqual(viewer.adjacentIndexes(5, 0), [1]);
  assert.deepEqual(viewer.adjacentIndexes(5, 2), [1, 3]);
  assert.deepEqual(viewer.adjacentIndexes(5, 4), [3]);
});
