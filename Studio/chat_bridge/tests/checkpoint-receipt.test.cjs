"use strict";
const test=require("node:test"),assert=require("node:assert/strict");
const {checkpointReceiptProof,receiptFromCheckpoint}=require("../src/checkpoint-receipt.cjs");
const url="https://chatgpt.com/c/6acb3b2e-1a24-83ec-b788-16ce5cf15033",hash="a".repeat(64);
const snapshot={url,readyState:"complete",userCount:2,assistantCount:2,
  users:[{key:"u1"},{key:"u2"}],assistants:[{key:"a1",text:"old"},{key:"a2",text:"exact completed response"}],
  latestUserTextSha256:hash,latestAssistantAfterUser:true,completionActionCount:1,stopControlCount:0,streamingMarkerCount:0};
const page={url,authenticated:true,ready:true};
test("exact checkpoint hash plus completed matching DOM can reconstruct a read-only receipt",()=>{
  assert.ok(checkpointReceiptProof(snapshot,page,{chatUrl:url,promptSha256:hash}));
  const receipt=receiptFromCheckpoint(snapshot,{now:()=>1});
  assert.equal(receipt.submissionAccepted,true);assert.equal(receipt.assistantCount,1);
  assert.equal(receipt.userCount,1);assert.equal(receipt.assistantKey,"a1");
  assert.equal(receipt.acceptanceSignal,"exact-checkpoint-dom");
});
test("partial, wrong, streaming or failed history never reconstructs or authorizes resend",()=>{
  for(const changes of [{latestUserTextSha256:"b".repeat(64)},{latestAssistantAfterUser:false},
    {completionActionCount:0},{stopControlCount:1},{streamingMarkerCount:1},
    {conversationLoadFailed:true},{assistants:[]},{readyState:"loading"},
    {url:"https://chatgpt.com/"}]){
    assert.equal(checkpointReceiptProof({...snapshot,...changes},page,{chatUrl:url,promptSha256:hash}),null);
  }
  assert.equal(checkpointReceiptProof(snapshot,{...page,rateLimited:true},{chatUrl:url,promptSha256:hash}),null);
});
