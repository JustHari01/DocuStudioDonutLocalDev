"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const { recoverInterruptedTurn } = require("../src/automation.cjs");
const url = "https://chatgpt.com/c/history-proof";
const receipt = { schemaVersion: 2, assistantCount: 3, userCount: 3,
  assistantKey: "assistant-before", userKey: "user-before", submissionAccepted: false };
function windowFor(changes = {}, page = {}) {
  return { isDestroyed: () => false, webContents: { getURL: () => url,
    executeJavaScript: async source => {
      if (source.includes("assistantCount: assistants.length")) return {
        url, readyState: "complete", visibilityState: "visible", assistantCount: 3, userCount: 3,
        assistants: [{ key: "assistant-before" }], users: [{ key: "user-before" }],
        stopControlCount: 0, streamingMarkerCount: 0, completionActionLabels: [], ...changes };
      if (source.includes("visibleComposers.length") && source.includes("authenticated:")) return {
        url, authenticated: true, ready: true, ...page };
      assert.fail("An uncertain page must not reach the answer reader or submit input");
    } } };
}
test("conversation load errors cannot authorize release of accepted or pre-submit receipts", async () => {
  for (const submissionAccepted of [true, false]) {
    await assert.rejects(recoverInterruptedTurn(windowFor({ conversationLoadFailed: true }), {
      submissionEvidence: { ...receipt, submissionAccepted } }), /CHATGPT_CONVERSATION_LOAD_FAILED/);
  }
});
test("history behind the pre-send baseline cannot prove a turn was absent", async () => {
  await assert.rejects(recoverInterruptedTurn(windowFor({ userCount: 2 }), {
    submissionEvidence: receipt }), /CHATGPT_SUBMISSION_UNCONFIRMED/);
});
test("a missing, signed-out, quota-limited or wrong conversation composer cannot unlock a send", async () => {
  for (const page of [{ ready: false }, { authenticated: false }, { rateLimited: true },
    { url: "https://chatgpt.com/c/another-chat" }]) {
    await assert.rejects(recoverInterruptedTurn(windowFor({}, page), {
      submissionEvidence: receipt }), /CHATGPT_SUBMISSION_UNCONFIRMED/);
  }
});
