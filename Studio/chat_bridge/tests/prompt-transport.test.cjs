"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  FAST_PROMPT_ATTACHMENT_THRESHOLD_CHARS,
  prepareCodexPromptTransport,
} = require("../src/prompt-transport.cjs");

test("small Codex prompts stay inline without changing a character", () => {
  const prompt = "BẮT ĐẦU\r\n日本語";
  const value = prepareCodexPromptTransport(prompt);
  assert.equal(value.mode, "inline");
  assert.equal(value.prompt, prompt);
  assert.deepEqual(value.attachments, []);
});

test("large Codex prompt becomes one exact UTF-8 attachment", () => {
  const prompt = (`MASTER\r\n日本語 😀\n${"x".repeat(100)}\n`)
    .repeat(Math.ceil(FAST_PROMPT_ATTACHMENT_THRESHOLD_CHARS / 110));
  const value = prepareCodexPromptTransport(prompt);
  const bytes = Buffer.from(prompt, "utf8");
  const expectedHash = crypto.createHash("sha256").update(bytes).digest("hex");

  assert.equal(value.mode, "attachment");
  assert.equal(value.charLength, prompt.length);
  assert.equal(value.byteLength, bytes.length);
  assert.equal(value.sha256, expectedHash);
  assert.equal(value.attachments.length, 1);
  assert.equal(
    Buffer.from(value.attachments[0].content_base64, "base64").toString("utf8"),
    prompt,
  );
  assert.match(value.attachments[0].name, new RegExp(expectedHash.slice(0, 12)));
  assert.ok(value.prompt.length < 1_000);
  assert.match(value.prompt, /đọc toàn bộ tệp/i);
});

test("threshold is inclusive for fast attachment transport", () => {
  const prompt = "a".repeat(FAST_PROMPT_ATTACHMENT_THRESHOLD_CHARS);
  assert.equal(prepareCodexPromptTransport(prompt).mode, "attachment");
});

test("prompt larger than the attachment safety limit remains exact inline", () => {
  const prompt = "日本".repeat(40_000);
  const value = prepareCodexPromptTransport(prompt, { maxAttachmentBytes: 100 });
  assert.equal(value.mode, "inline");
  assert.equal(value.prompt, prompt);
  assert.deepEqual(value.attachments, []);
});

test("Codex request handler actually uses the smart prompt transport", () => {
  const mainSource = fs.readFileSync(path.join(__dirname, "..", "src", "main.cjs"), "utf8");
  assert.match(mainSource, /prepareCodexPromptTransport\(requestedPrompt/);
  assert.match(mainSource, /const attachments = transport\.attachments/);
  assert.match(mainSource, /transport\.mode === "attachment"/);
});
