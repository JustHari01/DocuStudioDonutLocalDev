"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { accountCapabilityFailure, readAccountCapabilities } = require("../src/account-capability.cjs");
const { chatGptRateLimitError } = require("../src/automation.cjs");
const context = { busy: false, window: { isDestroyed: () => false } };

test("a reduced-quality banner cannot turn a missing model into account-wide 429", async () => {
  const original = new Error("CHATGPT_MODEL_UNAVAILABLE: High absent");
  const result = await accountCapabilityFailure(original, [context], async () => ({
    rateLimited: true,
    rateLimitText: "Capabilities reduced until 3:59 PM. Responses may have lower quality.",
  }), chatGptRateLimitError);
  assert.equal(result, original);
});

test("a hard limit on an idle sibling is still recognized", async () => {
  const result = await accountCapabilityFailure(
    new Error("CHATGPT_MODEL_UNAVAILABLE: High absent"), [context],
    async () => ({rateLimited: true, rateLimitText: 'Too many requests'}), chatGptRateLimitError,
  );
  assert.equal(result.statusCode, 429);
});

test("read-only account evidence distinguishes advisory and hard limit", async () => {
  const main = {isDestroyed: () => false};
  const soft = {authenticated: true, ready: true, rateLimited: false,
    capabilitiesReduced: true, capabilityNotice: 'Capabilities reduced until tomorrow at 11:45 PM.'};
  const snapshots = new Map([[main, soft], [context.window, soft]]);
  let state = await readAccountCapabilities([context], main, async window => snapshots.get(window));
  assert.equal(state.complete, true);
  assert.equal(state.ready, true);
  assert.equal(state.capabilities_reduced, true);
  assert.equal(state.hard_rate_limited, false);
  assert.equal(state.inspected_pages, 2);
  snapshots.set(context.window, {authenticated: true, ready: false, rateLimited: true, rateLimitText: 'Too many requests'});
  state = await readAccountCapabilities([context], main, async window => snapshots.get(window));
  assert.equal(state.ready, false);
  assert.equal(state.hard_rate_limited, true);
});

test("busy or unreadable pages cannot authorize legacy migration", async () => {
  const main = {isDestroyed: () => false};
  const soft = {authenticated: true, ready: true, rateLimited: false};
  const busy = await readAccountCapabilities([{...context, busy: true}], main, async () => soft);
  assert.equal(busy.complete, false);
  const broken = await readAccountCapabilities([context], main, async window => {
    if (window === context.window) throw new Error('renderer unavailable');
    return soft;
  });
  assert.equal(broken.complete, false);
  const empty = await readAccountCapabilities([], null, async () => assert.fail('no page'));
  assert.equal(empty.complete, false);
});

test("a quote, unrelated error or unreadable page cannot establish account cooldown", async () => {
  const original = new Error("CHATGPT_MODEL_UNAVAILABLE: High absent");
  assert.equal(await accountCapabilityFailure(original, [context], async () => ({
    rateLimited: false, rateLimitText: "Capabilities reduced until 3:59 PM.",
  }), chatGptRateLimitError), original);
  assert.equal(await accountCapabilityFailure(original, [context], async () => {
    throw new Error("renderer unavailable");
  }, chatGptRateLimitError), original);
  const network = new Error("network error");
  assert.equal(await accountCapabilityFailure(network, [context], async () => {
    assert.fail("unrelated failures must not inspect account pages");
  }, chatGptRateLimitError), network);
});

test("active turns are not probed when classifying a sibling failure", async () => {
  const original = new Error("CHATGPT_MODEL_UNAVAILABLE: High absent");
  assert.equal(await accountCapabilityFailure(original, [{...context, busy: true}], async () => {
    assert.fail("do not interfere with an active turn");
  }, chatGptRateLimitError), original);
});
