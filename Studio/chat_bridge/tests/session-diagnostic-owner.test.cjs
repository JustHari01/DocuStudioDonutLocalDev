"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");
const source=fs.readFileSync(require.resolve("../src/main.cjs"),"utf8");
const start=source.indexOf('  if (request.method === "POST" && url.pathname === "/v1/session/diagnostic")');
const end=source.indexOf('  if (request.method === "POST" && url.pathname === "/v1/session/existing-image")',start);
assert.ok(start>=0&&end>start);
const handler=source.slice(start,end);

for (const busy of [true,false]) {
  test(`diagnostic of ${busy?"busy context":"active controller"} returns activity without DOM IPC`,async()=>{
    let result;
    const pending={submissionAccepted:true,userKey:"accepted-user",outputMode:"voicevox_audit_json"};
    const url="https://chatgpt.com/c/exact-audit";
    const context={busy,chatUrl:url,pendingSubmission:pending,window:{webContents:{getURL:()=>url},destroy:()=>assert.fail("diagnostic destroyed audit")}};
    const sandbox={request:{method:"POST"},url:{pathname:"/v1/session/diagnostic"},response:{},
      readJson:async()=>({thread_id:"audit"}),normalizeTurnIdentity:()=>"thread:audit",
      turnConversations:new Map([["thread:audit",context]]),savedTurns:new Map(),
      turnControllers:new Map(busy?[]:[["thread:audit",{}]]),contextWindowUsable:()=>true,
      connectionHealth:()=>({classification:"unknown",conversation:[],session:[]}),
      sendJson:(_response,status,body)=>{assert.equal(status,200);result=body;},
      ensureConversationWindow:()=>assert.fail("active window reopened"),
      chatTurnSnapshot:()=>assert.fail("concurrent DOM probe"),
      createConversation:()=>assert.fail("replacement conversation created")};
    await vm.runInNewContext(`(async()=>{${handler}})()`,sandbox);
    assert.equal(result.context_busy,true);
    assert.equal(result.pending_submission_details,pending);
    assert.equal(result.diagnostic_deferred,"active-turn-owner");
    assert.equal(result.snapshot,null);
  });
}
