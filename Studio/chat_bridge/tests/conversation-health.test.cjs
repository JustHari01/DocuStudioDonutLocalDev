"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { ConversationHealth } = require("../src/conversation-health.cjs");
const { withConversationLoadRecovery, RECOVERY_COOLDOWN_MS, preSubmitSendFailure } = require("../src/conversation-load-recovery.cjs");
test('submission HTTP outcome survives sidebar bursts without claiming a missing history',()=>{
 const h=new ConversationHealth({maxEvents:256});
 h.record({method:'POST',url:'https://chatgpt.com/backend-api/f/conversation?private-secret',webContentsId:12,statusCode:404,responseHeaders:{'Set-Cookie':['secret']}});
 for(let i=0;i<300;i++)h.record({method:'GET',url:'https://chatgpt.com/backend-api/settings/user',webContentsId:12,statusCode:200});
 const s=h.snapshot('https://chatgpt.com/c/6ace7777-1234-83ec-a358-abcdef123456',12);
 assert.equal(s.submission.length,1);assert.equal(s.submission[0].statusCode,404);
 assert.equal(s.classification,'unknown');assert.doesNotMatch(JSON.stringify(s),/private|secret|Set-Cookie/);
 assert.equal(h.snapshot('https://chatgpt.com/c/6ace7777-1234-83ec-a358-abcdef123456',13).submission.length,0);
 h.record({method:'POST',url:'https://chatgpt.com/backend-api/f/conversation',webContentsId:12,statusCode:429,responseHeaders:{'Retry-After':['3600']}});
 const limited=h.snapshot('https://chatgpt.com/c/6ace7777-1234-83ec-a358-abcdef123456',12);
 assert.equal(limited.classification,'rate-limited');assert.equal(limited.blocking.retryAfterSeconds,3600);
});

test('an interrupted pre-submit Send UI enters the existing bounded exact-reader recovery',async()=>{
  for(const reason of ['send button not found','send button disabled']){
    const error=preSubmitSendFailure({ok:false,reason,connectionInterrupted:true});
    assert.equal(error.code,'CHATGPT_CONVERSATION_LOAD_FAILED');
    const context={chatUrl};let attempts=0,reopened=0;
    const result=await withConversationLoadRecovery(context,async()=>{if(++attempts===1)throw error;return 'sent once after stable reader';},
      {reopen:async()=>{reopened++;}});
    assert.equal(result,'sent once after stable reader');assert.equal(attempts,2);assert.equal(reopened,1);
  }
});

test('normal disabled Send, missing selectors and already dispatched input cannot authorize a pre-submit retry',()=>{
  assert.equal(preSubmitSendFailure({ok:false,reason:'send button disabled',connectionInterrupted:false}),null);
  assert.equal(preSubmitSendFailure({ok:false,reason:'send button not found'}),null);
  assert.equal(preSubmitSendFailure({ok:true,connectionInterrupted:true}),null);
  assert.equal(preSubmitSendFailure({ok:false,reason:'composer disappeared',connectionInterrupted:true}),null);
  assert.equal(preSubmitSendFailure({ok:false,reason:'send button not found',connectionInterrupted:true},{sendAttempted:true}),null);
});
const id = "6acb56c9-3b84-83ec-9326-1534ff3eea19";
const chatUrl = `https://chatgpt.com/c/${id}`;
test('sidebar 429 does not override a successful exact conversation or block generation', () => {
  const health = new ConversationHealth();
  health.record(request(200));
  health.record(request(429, {url:'https://chatgpt.com/backend-api/conversations?limit=28'}));
  const facts = health.snapshot(chatUrl, 12);
  assert.equal(facts.classification, 'loaded');
  assert.equal(facts.history_throttle.statusCode, 429);
  assert.doesNotThrow(() => require('../src/stalled-image-recovery.cjs').providerHold(facts));
  health.record(request(429, {method:'POST', url:'https://chatgpt.com/backend-api/f/conversation'}));
  assert.throws(() => require('../src/stalled-image-recovery.cjs').providerHold(health.snapshot(chatUrl, 12)), /HTTP 429/);
});

test('unloaded history still backs off and preserves receipts after sidebar throttling', async () => {
  const receipt = {sendAttempted:true, submissionAccepted:true};
  const context = {chatUrl, pendingSubmission:receipt};
  const health = new ConversationHealth();
  health.record(request(429, {url:'https://chatgpt.com/backend-api/conversations'}));
  await assert.rejects(withConversationLoadRecovery(context, () => {throw new Error('CHATGPT_CONVERSATION_LOAD_FAILED');}, {
    health:() => health.snapshot(chatUrl, 12), reopen:() => assert.fail('failed history must stop polling'),
  }), /connection=history-rate-limited;http=429/);
  assert.equal(context.pendingSubmission, receipt);
});
const failed = () => new Error("CHATGPT_CONVERSATION_LOAD_FAILED: checkpoint kept");
const request = (statusCode, extra = {}) => ({ method: "GET", webContentsId: 12,
  url: `https://chatgpt.com/backend-api/conversation/${id}?secret=never-log`,
  statusCode, requestHeaders: { Authorization: "never-log" }, ...extra });

test("health retains only exact recent conversation facts without secrets or other tabs", () => {
  let now = 1000;
  const health = new ConversationHealth({ now: () => now, maxAgeMs: 100 });
  health.record(request(404));
  health.record(request(200, { webContentsId: 13 }));
  health.record(request(401, { url: "https://evil.test/backend-api/conversation/" + id }));
  health.record(request(403, { method: "POST" }));
  const result = health.snapshot(chatUrl, 12);
  assert.equal(result.classification, "conversation-unavailable");
  assert.equal(result.conversation.length, 1);
  assert.doesNotMatch(JSON.stringify(health.events), /never-log|Authorization|secret|evil/);
  now += 101;
  assert.equal(health.snapshot(chatUrl, 12).classification, "unknown");
});

test("only a bounded Retry-After delay survives response-header collection", () => {
  const health = new ConversationHealth({now:()=>1000});
  health.record(request(429,{responseHeaders:{"Retry-After":["1200"],"Set-Cookie":["private-secret"]}}));
  assert.equal(health.snapshot(chatUrl,12).conversation[0].retryAfterSeconds,1200);
  assert.doesNotMatch(JSON.stringify(health.events),/Set-Cookie|private-secret|responseHeaders/);
});

test("network failures and HTTP outcomes are distinguished for actual backend routes", () => {
  for (const [status, expected] of [[200, "loaded"], [401, "authentication-required"],
    [403, "access-rejected"], [404, "conversation-unavailable"], [429, "rate-limited"], [503, "server-error"]]) {
    const health = new ConversationHealth();
    health.record(request(status, { url: `https://chatgpt.com/backend-api/f/conversation/${id}` }));
    assert.equal(health.snapshot(chatUrl, 12).classification, expected);
  }
  const health = new ConversationHealth();
  health.record(request(0, { error: "net::ERR_CONNECTION_RESET" }));
  assert.equal(health.snapshot(chatUrl, 12).classification, "network-error");
});

test("history and server-rendered page failures remain separate from conversation-read proof", () => {
  const health = new ConversationHealth();
  health.record(request(200, { url: chatUrl + "?private=omitted" }));
  health.record(request(403, { url: "https://chatgpt.com/backend-api/accounts/check?token=omitted" }));
  health.record(request(503, { url: "https://chatgpt.com/backend-api/new-private-route/private-secret-id" }));
  const result = health.snapshot(chatUrl, 12);
  assert.equal(result.classification, "unknown", "HTTP 200 HTML alone is not a successfully loaded conversation");
  assert.equal(result.navigation[0].statusCode, 200);
  assert.equal(result.backend[0].family, "accounts/check");
  assert.equal(result.backend[1].family, "other/other");
  assert.doesNotMatch(JSON.stringify(result), /omitted|private|secret|token/);
});

test("background request bursts do not erase another workspace's only load failure", () => {
  const health = new ConversationHealth({ maxEvents: 256 });
  health.record(request(404));
  for (let i = 0; i < 500; i++) health.record(request(200, {
    webContentsId: 13, url: "https://chatgpt.com/backend-api/settings/user" }));
  assert.equal(health.snapshot(chatUrl, 12).classification, "conversation-unavailable");
  assert.equal(health.events.length, 25);
});

test("fresh reader never submits again after an accepted or uncertain Send", async () => {
  for (const accepted of [true, false]) {
    let submits = 0, reopened = 0, reads = 0;
    const receipt = { submissionAccepted: accepted, sendAttempted: true };
    const context = { chatUrl, pendingSubmission: receipt, pendingAttachment: { status: "selected" } };
    const result = await withConversationLoadRecovery(context, async () => { submits++; throw failed(); }, {
      now: () => 1000, reopen: async () => { reopened++; },
      recoverAccepted: async evidence => { assert.equal(evidence, receipt); reads++; return "existing answer"; },
    });
    assert.equal(result, "existing answer");
    assert.equal(submits, 1); assert.equal(reads, 1); assert.equal(reopened, 1);
    assert.equal(context.pendingSubmission, receipt);
    assert.equal(context.pendingAttachment.status, "selected");
  }
});

test("before Send one fresh renderer can retry, repeated failures have a cooldown", async () => {
  const context = { chatUrl }; let operations = 0, reopened = 0;
  const options = { now: () => 1000, reopen: async () => { reopened++; } };
  await assert.rejects(withConversationLoadRecovery(context, async () => { operations++; throw failed(); }, options));
  assert.equal(operations, 2); assert.equal(reopened, 1);
  await assert.rejects(withConversationLoadRecovery(context, async () => { operations++; throw failed(); }, options));
  assert.equal(operations, 3); assert.equal(reopened, 1);
  assert.equal(RECOVERY_COOLDOWN_MS, 900_000);
});

test("unavailable, denied, auth and quota failures preserve the renderer and report HTTP evidence", async () => {
  for (const classification of ["conversation-unavailable", "access-rejected", "authentication-required", "rate-limited"]) {
    await assert.rejects(withConversationLoadRecovery({ chatUrl }, async () => { throw failed(); }, {
      health: () => ({ classification, conversation: [{ statusCode: 404 }] }),
      reopen: () => assert.fail("known remote rejection must not repeatedly reopen"),
    }), error => error.message.includes(`connection=${classification};http=404`));
  }
});

test("home, cancellation, unrelated failures and unconfirmed recovery never release send locks", async () => {
  const receipt = { submissionAccepted: true };
  for (const context of [{ chatUrl: "https://chatgpt.com/" }, { chatUrl }]) {
    const signal = { aborted: true };
    await assert.rejects(withConversationLoadRecovery(context, async () => { throw failed(); }, {
      signal, reopen: () => assert.fail("cancelled/home reader reopened"),
    }));
  }
  const context = { chatUrl, pendingSubmission: receipt };
  await assert.rejects(withConversationLoadRecovery(context, async () => { throw failed(); }, {
    reopen: async () => {}, recoverAccepted: async () => ({ status: "not-submitted" }),
  }));
  assert.equal(context.pendingSubmission, receipt);
});
