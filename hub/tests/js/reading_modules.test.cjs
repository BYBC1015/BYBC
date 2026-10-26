"use strict";

const assert = require("node:assert/strict");
const markdown = require("../../codex_hub/static/codex-markdown.js");
const video = require("../../codex_hub/static/codex-video-reader.js");

function memoryStorage() {
  const values = new Map();
  return {
    getItem: (key) => values.has(key) ? values.get(key) : null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  };
}

const blocks = markdown.parseMarkdown([
  "# Release notes",
  "",
  "> Read before installing.",
  "",
  "- Safe",
  "- Lazy",
  "",
  "| Feature | State |",
  "| :--- | ---: |",
  "| Markdown | ready |",
  "",
  "```js",
  "const answer = 42; // highlighted",
  "```",
].join("\n"));

assert.deepEqual(blocks.map((block) => block.type), [
  "heading", "blockquote", "list", "table", "code",
]);
assert.equal(blocks[3].alignment[0], "left");
assert.equal(blocks[3].alignment[1], "right");
assert.equal(blocks[4].language, "js");
assert.ok(markdown.tokenizeCode(blocks[4].text, "js").some((token) => token.type === "keyword"));
assert.ok(markdown.tokenizeCode(blocks[4].text, "js").some((token) => token.type === "comment"));

assert.equal(markdown.sanitizeUrl("https://example.com/docs"), "https://example.com/docs");
assert.equal(markdown.sanitizeUrl("/projects/local"), "/projects/local");
assert.equal(markdown.sanitizeUrl("javascript:alert(1)"), null);
assert.equal(markdown.sanitizeUrl("java\nscript:alert(1)"), null);
assert.equal(markdown.sanitizeUrl("data:text/html,<script>alert(1)</script>"), null);

const unsafe = markdown.parseMarkdown([
  "<img src=x onerror=alert(1)>",
  "[bad](javascript:alert(1))",
  "[good](https://example.com)",
].join("\n"));
const inline = unsafe[0].lines.flat();
assert.ok(inline.some((token) => token.type === "text" && token.text.includes("<img")));
assert.ok(!JSON.stringify(unsafe).includes('"href":"javascript:'));
assert.ok(JSON.stringify(unsafe).includes('"href":"https://example.com"'));

for (const value of ['image_code file_code total_orders', '{"image_code":"RIVER 7149","file_code":"PINE-6284"}', '文件_名称_保留']) {
  const parsed = markdown.parseMarkdown(value)[0].lines.flat();
  assert.ok(parsed.every((token) => token.type === 'text'));
  assert.equal(parsed.map((token) => token.text).join(''), value);
}
assert.ok(markdown.parseMarkdown('_italic_ and *also italic*')[0].lines.flat().filter((token) => token.type === 'em').length === 2);

assert.equal(video.isVideoAttachment("clip.mp4", "application/octet-stream"), true);
assert.equal(video.isVideoAttachment("clip.mp4", "text/html"), false);
assert.equal(video.isVideoAttachment("report.pdf", "application/pdf"), false);
assert.equal(video.isVideoAttachment("mislabelled.pdf", "video/mp4"), false);
assert.equal(video.isVideoAttachment("sheet.xlsx", "application/vnd.ms-excel"), false);
assert.equal(video.safeMediaUrl("javascript:alert(1)"), null);
assert.equal(video.safeMediaUrl("/attachments/7"), "/attachments/7");
assert.equal(video.appendInlineHint("/attachments/7?download=0"), "/attachments/7?download=0&inline=1");

const storage = memoryStorage();
const key = video.progressStorageKey("project/attachment/7");
assert.equal(video.writeProgress(storage, key, 73.5, 300, 1000), true);
assert.deepEqual(video.readProgress(storage, key, 2000), {
  time: 73.5,
  duration: 300,
  updatedAt: 1000,
});
assert.equal(video.resumePosition(video.readProgress(storage, key, 2000), 300), 73.5);
assert.equal(video.resumePosition({ time: 297, duration: 300 }, 300), 0);
assert.equal(video.resumePosition({ time: 3, duration: 300 }, 300), 0);
assert.equal(video.formatBytes(3 * 1024 * 1024), "3.0 MB");
assert.equal(video.formatTime(73.5), "1:13");

console.log("reading module JavaScript tests passed");
