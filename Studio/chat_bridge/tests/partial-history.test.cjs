"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { chatGptConversationLoadFailure } = require("../src/automation.cjs");
const { ConversationHealth } = require("../src/conversation-health.cjs");
test("partial history and disconnected generation are not ready despite mounted user messages", () => {
  assert.equal(chatGptConversationLoadFailure("Unable to load history\nRetry\nYou said: TIẾP", 0, 9), true);
  assert.equal(chatGptConversationLoadFailure("Loading older messages…\nConnection interrupted. Waiting for the complete answer", 0, 10), true);
  assert.equal(chatGptConversationLoadFailure("Unable to load history\nConnection interrupted. Waiting for the complete answer", 1, 9), true);
  assert.equal(chatGptConversationLoadFailure("Unable to load history\nA complete response", 5, 5), false);
  assert.equal(chatGptConversationLoadFailure("A complete response", 0, 9), false);
  assert.equal(chatGptConversationLoadFailure("Unable to load this conversation", 2, 2), true);
});
test("provider history throttling has bounded cooldown without treating unrelated 404 as missing chat", () => {
  let now = 1000;
  const health = new ConversationHealth({ now: () => now });
  const url = "https://chatgpt.com/c/6acb3b2e-1a24-83ec-b788-16ce5cf15033";
  health.record({url:"https://chatgpt.com/backend-api/conversations/private?cursor=secret", method:"GET", webContentsId:11, statusCode:429});
  assert.equal(health.snapshot(url, 11).classification, "unknown");
  assert.equal(health.snapshot(url, 11).history_throttle.statusCode, 429);
  assert.equal(health.snapshot(url, 12).classification, "unknown");
  assert.doesNotMatch(JSON.stringify(health.events), /private|cursor|secret/);
  now += 120001;
  assert.equal(health.snapshot(url, 11).classification, "unknown");
  assert.equal(health.snapshot(url, 11).history_throttle, undefined);
  health.record({url:"https://chatgpt.com/backend-api/conversations/private", method:"GET", webContentsId:11, statusCode:404});
  assert.equal(health.snapshot(url, 11).classification, "unknown");
});
