"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { awaitInputOperation } = require("../src/input-lease.cjs");
const { insertPromptText, withTrustedWindowStage } = require("../src/automation.cjs");

const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const windowForLane = () => ({
  isDestroyed: () => false,
  isVisible: () => true,
  webContents: {
    getURL: () => "https://chatgpt.com/c/input-lease-fixture",
    executeJavaScript: async () => ({ready: true, rateLimited: false}),
  },
});

test("hung native input retires its renderer without fallback, duplicate insertion or Send", async () => {
  let inputs = 0, fallback = 0, destroyed = 0;
  const window = {
    destroy: () => { destroyed += 1; },
    webContents: {
      executeJavaScript: async () => ({value: "", anchored: true, visibleComposers: 1}),
      insertText: () => { inputs += 1; return new Promise(() => {}); },
      debugger: { sendCommand: () => { fallback += 1; } },
    },
  };
  await assert.rejects(insertPromptText(window, "one exact prompt", {
    expectedBefore: "", expectedAfter: "one exact prompt", nativeInputTimeoutMs: 20,
  }), {code: "CHATGPT_NATIVE_INPUT_TIMEOUT"});
  assert.deepEqual({inputs, fallback, destroyed}, {inputs: 1, fallback: 0, destroyed: 1});
});

test("cancelling hung IPC returns promptly and consumes a late transport failure", async () => {
  const controller = new AbortController();
  let rejectNative;
  const pending = awaitInputOperation(() => new Promise((_, reject) => {
    rejectNative = reject;
  }), {signal: controller.signal, timeoutMs: 1000});
  await pause(5);
  controller.abort();
  await assert.rejects(pending, /Request cancelled/);
  rejectNative(new Error("renderer closed late"));
  await pause(5);
});

for (const method of ["abort", "timeout"]) {
  test(`a queued ${method} releases its caller but preserves FIFO behind the active input owner`, async () => {
    let releaseOwner, ownerEntered;
    const entered = new Promise(resolve => {ownerEntered = resolve;});
    const owner = withTrustedWindowStage(windowForLane(), undefined, async () => {
      ownerEntered(); await new Promise(resolve => {releaseOwner = resolve;});
    });
    await entered;
    const controller = new AbortController();
    let cancelledEntered = false, successorEntered = false;
    const waiter = withTrustedWindowStage(windowForLane(), controller.signal,
      async () => {cancelledEntered = true;}, {queueTimeoutMs: method === "timeout" ? 15 : 1000});
    const successor = withTrustedWindowStage(windowForLane(), undefined,
      async () => {successorEntered = true;});
    const rejection = assert.rejects(waiter,
      method === "abort" ? /Request cancelled/ : /CHATGPT_INPUT_QUEUE_TIMEOUT/);
    if (method === "abort") controller.abort();
    await rejection;
    await pause(10);
    assert.equal(cancelledEntered, false);
    assert.equal(successorEntered, false, "cancelled waiter cannot release the owner's lock");
    releaseOwner();
    await Promise.all([owner, successor]);
    assert.equal(successorEntered, true);
  });
}

test("a hung input owner times out and the next conversation can acquire the lane", async () => {
  const first = withTrustedWindowStage(windowForLane(), undefined,
    () => awaitInputOperation(() => new Promise(() => {}), {timeoutMs: 20}));
  const rejected = assert.rejects(first, /CHATGPT_NATIVE_INPUT_TIMEOUT/);
  let entered = false;
  const next = withTrustedWindowStage(windowForLane(), undefined, async () => {entered = true;});
  await Promise.all([rejected, next]);
  assert.equal(entered, true);
});
