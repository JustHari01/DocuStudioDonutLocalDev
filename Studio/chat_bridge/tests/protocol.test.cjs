"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  codexTurnIdentity,
  codexModelCatalog,
  isCodexResponsesRequest,
  latestUserInput,
  requestInput,
} = require("../src/protocol.cjs");

test("Responses input accepts only local text payload shapes", () => {
  assert.equal(requestInput("hello"), "hello");
  assert.equal(requestInput(["first", { content: "second" }]), "first\n\nsecond");
  assert.equal(
    requestInput([{ role: "user", content: [{ type: "input_text", text: "nested" }] }]),
    "nested",
  );
  assert.throws(() => requestInput({ url: "https://example.com" }), /must be text/);
  assert.throws(() => requestInput([{ image_url: "file:///secret" }]), /contain text/);
});

test("Codex model catalog advertises the isolated web model and large prompt window", () => {
  const catalog = codexModelCatalog();
  assert.equal(catalog.models.length, 1);
  assert.equal(catalog.models[0].slug, "chatgpt-web/auto");
  assert.equal(catalog.models[0].default_reasoning_level, "none");
  assert.equal(catalog.models[0].context_window, 1_000_000);
  assert.equal(catalog.models[0].tool_mode, null);
  assert.equal(catalog.models[0].support_verbosity, true);
  assert.equal(catalog.models[0].default_verbosity, "low");
  assert.equal(catalog.models[0].default_reasoning_summary, "none");
  assert.deepEqual(catalog.models[0].truncation_policy, { mode: "tokens", limit: 10_000 });
  assert.equal(catalog.models[0].node_repl_disabled, true);
  const currentCodexFixture = JSON.parse(fs.readFileSync(
    path.join(__dirname, "..", "..", "tests", "fixtures", "codex-chatgpt-web-model-catalog.json"),
    "utf8",
  ));
  assert.deepEqual(catalog, currentCodexFixture);
});

test("Codex input forwards only the newest human message", () => {
  const input = [
    { role: "developer", content: [{ type: "input_text", text: "transport" }] },
    { role: "user", content: [{ type: "input_text", text: "MASTER" }] },
    { role: "assistant", content: [{ type: "output_text", text: "ĐÃ NẠP" }] },
    { type: "message", role: "user", content: [{ type: "input_text", text: "TIẾP" }] },
  ];
  assert.equal(latestUserInput(input), "TIẾP");
});

test("Codex requests and stable turn identities are detected", () => {
  const headers = {
    accept: "text/event-stream",
    "x-codex-turn-metadata": JSON.stringify({ thread_id: "thread-123", turn_id: "turn-1" }),
  };
  assert.equal(isCodexResponsesRequest({ headers }, { input: "hello" }), true);
  assert.equal(codexTurnIdentity(headers, { prompt_cache_key: "fallback" }), "thread:thread-123");
  assert.equal(codexTurnIdentity({}, { prompt_cache_key: "fallback" }), "cache:fallback");
});

test("packaged bridge security contract stays fail-closed", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "src", "main.cjs"), "utf8");
  assert.match(source, /server\.listen\(port, LOOPBACK_HOST/);
  assert.match(source, /nodeIntegration:\s*false/);
  assert.match(source, /contextIsolation:\s*true/);
  assert.match(source, /sandbox:\s*true/);
  assert.match(source, /webSecurity:\s*true/);
  assert.match(source, /backgroundThrottling:\s*false/);
  assert.match(source, /paintWhenInitiallyHidden:\s*true/);
  assert.match(source, /setPermissionRequestHandler\([^\n]+callback\(false\)/);
  assert.match(source, /tokenMatches\(accessToken, request\.headers\.authorization\)/);
  assert.doesNotMatch(source, /CODEX_HOME|openai_base_url|setWindowOpenHandler\([^]*action:\s*"allow"/);
});
