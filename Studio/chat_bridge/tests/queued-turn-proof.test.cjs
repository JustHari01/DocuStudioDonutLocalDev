"use strict";
const test=require('node:test'),assert=require('node:assert/strict');
const {queuedTurnProof,completedBaselineProof}=require('../src/queued-turn-proof.cjs');
const {masterPromptCaption}=require('../src/prompt-transport.cjs');
const crypto=require('node:crypto');
const url='https://chatgpt.com/c/6acb3b2e-1a24-83ec-b788-16ce5cf15033';
const snapshot={url,readyState:'complete',userCount:4,assistantCount:4,
  users:[{key:'baseline-user'}],assistants:[{key:'baseline-assistant',text:'ĐÃ XONG'}],
  latestUserTextSha256:'a'.repeat(64),latestAssistantTextSha256:'b'.repeat(64),
  latestAssistantAfterUser:true,completionActionCount:1,stopControlCount:0,streamingMarkerCount:0};
const page={url,authenticated:true,ready:true};
const options={chatUrl:url,pendingPromptSha256:'c'.repeat(64),userSha256:'a'.repeat(64),assistantSha256:'b'.repeat(64)};
test('known idle baseline allows a distinct missing-content followup without granting repeated resend',()=>{
  const s={...snapshot,visibilityState:'visible'};
  const p={...page,visibleComposers:1,composerDraftLength:0,selectedFileCount:0,composerCardCount:0};
  assert.ok(completedBaselineProof(s,p,options));
  assert.equal(queuedTurnProof(s,p,{...options,pendingPromptSha256:options.userSha256}),null);
  for(const change of [{latestAssistantTextSha256:'d'.repeat(64)},{latestUserTextSha256:'d'.repeat(64)},
    {stopControlCount:1},{streamingMarkerCount:1},{connectionInterrupted:true},{conversationLoadFailed:true},
    {visibilityState:'hidden'},{latestAssistantAfterUser:false}])
    assert.equal(completedBaselineProof({...s,...change},p,options),null);
  for(const change of [{ready:false},{rateLimited:true},{authenticated:false},{composerDraftLength:20},
    {visibleComposers:2},{selectedFileCount:1},{composerCardCount:1},{connectionInterrupted:true}])
    assert.equal(completedBaselineProof(s,{...p,...change},options),null);
});
test('exact completed baseline proves a different queued prompt has not appeared',()=>{
  assert.ok(queuedTurnProof(snapshot,page,options));
  assert.ok(queuedTurnProof({...snapshot,completionActionCount:0},page,options));
});
test('same repeated prompt, new reply, partial history or a busy reader cannot release a queued turn',()=>{
  assert.equal(queuedTurnProof(snapshot,page,{...options,pendingPromptSha256:options.userSha256}),null);
  for(const change of [{latestAssistantTextSha256:'d'.repeat(64)},{latestUserTextSha256:'d'.repeat(64)},
    {stopControlCount:1},{streamingMarkerCount:1},{connectionInterrupted:true},{conversationLoadFailed:true},
    {latestAssistantAfterUser:false}]){
    assert.equal(queuedTurnProof({...snapshot,...change},page,options),null);
  }
  assert.equal(queuedTurnProof(snapshot,{...page,ready:false},options),null);
});

test('completed master-file caption binds the raw baseline without weakening queued-turn proof',()=>{
  const attachment={sha256:'e'.repeat(64),char_length:307462};
  const name='DocuRandom-Master-Prompt-eeeeeeeeeeee-a9ff467cf6.txt';
  const caption=masterPromptCaption(name,attachment.char_length,attachment.sha256);
  const uiHash=crypto.createHash('sha256').update(caption.normalize('NFC').replace(/\s+/g,' ').trim()).digest('hex');
  const current={...snapshot,latestUserTextSha256:uiHash,users:[{key:'baseline-user',text:name+' Document '+caption}]};
  const opts={...options,userAttachment:attachment};
  assert.ok(queuedTurnProof(current,page,opts));
  assert.equal(queuedTurnProof(current,page,options),null);
  for(const change of [{sha256:'f'.repeat(64)},{char_length:307463},{char_length:640}])
    assert.equal(queuedTurnProof(current,page,{...opts,userAttachment:{...attachment,...change}}),null);
  assert.equal(queuedTurnProof({...current,users:[{key:'baseline-user',text:'SHA-256: '+attachment.sha256}]},page,opts),null);
  assert.equal(queuedTurnProof({...current,latestAssistantAfterUser:false},page,opts),null);
  assert.equal(queuedTurnProof(current,page,{...opts,pendingPromptSha256:uiHash}),null);
  assert.equal(queuedTurnProof(current,page,{...opts,assistantSha256:'d'.repeat(64)}),null);
});
