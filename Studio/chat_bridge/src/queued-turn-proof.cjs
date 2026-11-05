"use strict";
const {checkpointReceiptProof}=require('./checkpoint-receipt.cjs');
const crypto=require('node:crypto');
const {masterPromptCaption}=require('./prompt-transport.cjs');
function baselineUserHash(snapshot,{userSha256,userAttachment}={}){
  if(snapshot?.latestUserTextSha256===userSha256)return userSha256;
  const sha=userAttachment?.sha256,chars=userAttachment?.char_length;
  if(!/^[a-f0-9]{64}$/.test(sha||'')||!Number.isSafeInteger(chars)||chars<64000||chars>8*1024*1024)return null;
  const names=[...new Set(String(snapshot?.users?.at(-1)?.text||'')
    .match(/DocuRandom-Master-Prompt-[a-f0-9]{12}(?:-[a-f0-9]{10,32})?\.txt/g)||[])];
  if(names.length!==1||!names[0].startsWith('DocuRandom-Master-Prompt-'+sha.slice(0,12)))return null;
  const caption=masterPromptCaption(names[0],chars,sha).normalize('NFC').replace(/\s+/g,' ').trim();
  const hash=crypto.createHash('sha256').update(caption).digest('hex');
  return snapshot.latestUserTextSha256===hash?hash:null;
}
function queuedTurnProof(snapshot,page,{chatUrl,pendingPromptSha256,userSha256,assistantSha256,userAttachment}={}){
  if(!/^[a-f0-9]{64}$/.test(pendingPromptSha256||'') || !/^[a-f0-9]{64}$/.test(assistantSha256||'')
    || pendingPromptSha256===userSha256 || snapshot?.latestAssistantTextSha256!==assistantSha256
    || snapshot?.connectionInterrupted===true)return null;
  // The full assistant hash is a reply that the caller already received and
  // checkpointed. ChatGPT can render its action row outside the semantic
  // assistant node; that layout must not invalidate this completed baseline.
  // Unknown replies still require their real completion controls elsewhere.
  const baselineHash=baselineUserHash(snapshot,{userSha256,userAttachment});
  if(!baselineHash||pendingPromptSha256===baselineHash)return null;
  const completed=checkpointReceiptProof({...snapshot,completionActionCount:1},page,{chatUrl,promptSha256:baselineHash});
  return completed ? JSON.stringify([completed,assistantSha256,pendingPromptSha256]) : null;
}
function completedBaselineProof(snapshot,page,{chatUrl,userSha256,assistantSha256,userAttachment}={}){
  // This proves a known reply is still the idle baseline, NOT that the
  // ambiguous repeated prompt was never sent. Only a distinct repair request
  // may use this result; queuedTurnProof retains its stricter no-repeat rule.
  if(!/^[a-f0-9]{64}$/.test(assistantSha256||'')
    || snapshot?.latestAssistantTextSha256!==assistantSha256
    || snapshot?.connectionInterrupted===true || page?.connectionInterrupted===true
    || snapshot?.visibilityState!=='visible' || Number(page?.visibleComposers)!==1
    || !Number.isSafeInteger(page?.composerDraftLength) || page.composerDraftLength>1
    || Number(page?.selectedFileCount)!==0 || Number(page?.composerCardCount)!==0)return null;
  const baselineHash=baselineUserHash(snapshot,{userSha256,userAttachment});
  if(!baselineHash)return null;
  const completed=checkpointReceiptProof({...snapshot,completionActionCount:1},page,
    {chatUrl,promptSha256:baselineHash});
  return completed ? JSON.stringify([completed,assistantSha256,'missing-content-followup']) : null;
}
module.exports={queuedTurnProof,baselineUserHash,completedBaselineProof};
