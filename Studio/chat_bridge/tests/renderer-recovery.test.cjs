"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { retireFailedRenderer, suspendFailedConversationReader } = require("../src/renderer-recovery.cjs");
const { generatedImageSnapshot } = require("../src/automation.cjs");

test("poisoned DOM reader is retired while exact URL and send locks survive", () => {
  let destroyed=0;
  const pending={submissionAccepted:true,assistantCount:4};
  const attachment={status:"selected"};
  const context={window:{isDestroyed:()=>false,destroy:()=>destroyed++,webContents:{getURL:()=>"https://chatgpt.com/c/exact"}},
    chatUrl:"https://chatgpt.com/",pendingSubmission:pending,pendingAttachment:attachment};
  assert.equal(retireFailedRenderer(context,{code:"CHATGPT_PAGE_PROBE_TIMEOUT"}),true);
  assert.equal(destroyed,1);assert.equal(context.window,null);
  assert.equal(context.chatUrl,"https://chatgpt.com/c/exact");
  assert.equal(context.pendingSubmission,pending);assert.equal(context.pendingAttachment,attachment);
});

test("ordinary failures and quota errors do not destroy a conversation", () => {
  for(const message of ["ChatGPT rate limited", "unknown upload state", "Request cancelled"]){
    const context={window:{isDestroyed:()=>false,destroy:()=>assert.fail("unrelated page destroyed")}};
    assert.equal(retireFailedRenderer(context,new Error(message)),false);
  }
});

test("a non-owning diagnostic preserves the active renderer and accepted receipt", () => {
  const pending={submissionAccepted:true,assistantCount:4};
  const window={isDestroyed:()=>false,destroy:()=>assert.fail("active audit destroyed")};
  const context={busy:true,window,pendingSubmission:pending};
  assert.equal(retireFailedRenderer(context,{code:"CHATGPT_PAGE_PROBE_TIMEOUT"},{protectActiveTurn:true}),false);
  assert.equal(context.window,window);
  assert.equal(context.pendingSubmission,pending);
});

test("image DOM IPC cannot hold a workspace indefinitely", async () => {
  let calls=0;
  const window={webContents:{executeJavaScript:()=>{calls++;return new Promise(()=>{});}}};
  await assert.rejects(generatedImageSnapshot(window,{timeoutMs:15}),error=>error.code==="CHATGPT_PAGE_PROBE_TIMEOUT");
  await assert.rejects(generatedImageSnapshot(window,{timeoutMs:15}),error=>error.code==="CHATGPT_PAGE_PROBE_IN_FLIGHT");
  assert.equal(calls,1);
});

test("failed history reader stops background requests but keeps accepted state", () => {
  const receipt={submissionAccepted:true}, attachment={status:"confirmed"};
  let closed=0;
  const context={chatUrl:"https://chatgpt.com/",pendingSubmission:receipt,pendingAttachment:attachment,
    window:{isDestroyed:()=>false,destroy:()=>closed++,webContents:{getURL:()=>"https://chatgpt.com/c/exact-chat"}}};
  assert.equal(suspendFailedConversationReader(context,new Error("CHATGPT_CONVERSATION_LOAD_FAILED: [connection=rate-limited;http=429]")),true);
  assert.equal(closed,1);assert.equal(context.window,null);
  assert.equal(context.chatUrl,"https://chatgpt.com/c/exact-chat");
  assert.equal(context.pendingSubmission,receipt);assert.equal(context.pendingAttachment,attachment);
});

test("suspension cannot disturb an active owner or unrelated error", () => {
  const context={window:{isDestroyed:()=>false,destroy:()=>assert.fail("owner closed")}};
  assert.equal(suspendFailedConversationReader(context,new Error("CHATGPT_CONVERSATION_LOAD_FAILED"),{protectActiveTurn:true}),false);
  assert.equal(suspendFailedConversationReader(context,new Error("CHATGPT_RECOVERY_PENDING")),false);
});

test("retiring or suspending a local route preserves the last server URL and send lock", () => {
  for (const [recover, error] of [
    [retireFailedRenderer, {code:"CHATGPT_PAGE_PROBE_TIMEOUT"}],
    [suspendFailedConversationReader, new Error("CHATGPT_CONVERSATION_LOAD_FAILED")],
  ]) {
    for (const transient of ["local-chatgpt:abc", "local-chatgpt%3Aabc", "LOCAL-CHATGPT%3aabc"]) {
      const receipt={submissionAccepted:true};
      const context={chatUrl:"https://chatgpt.com/c/server-saved",pendingSubmission:receipt,
        window:{isDestroyed:()=>false,destroy:()=>{},webContents:{getURL:()=>"https://chatgpt.com/c/"+transient}}};
      assert.equal(recover(context,error),true);
      assert.equal(context.chatUrl,"https://chatgpt.com/c/server-saved");
      assert.equal(context.pendingSubmission,receipt);
    }
  }
});
