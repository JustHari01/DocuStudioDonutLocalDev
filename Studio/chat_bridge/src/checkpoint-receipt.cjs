"use strict";
const {conversationId}=require("./conversation-health.cjs");

function checkpointReceiptProof(snapshot,page,{chatUrl,promptSha256}={}){
  if(!/^[a-f0-9]{64}$/.test(promptSha256||"") || !conversationId(chatUrl))return null;
  if(conversationId(snapshot?.url)!==conversationId(chatUrl) || conversationId(page?.url)!==conversationId(chatUrl)
    || snapshot?.readyState!=="complete" || page?.authenticated!==true || page?.ready!==true
    || snapshot?.conversationLoadFailed || snapshot?.accountLoadFailed || page?.conversationLoadFailed
    || page?.accountLoadFailed || page?.rateLimited || snapshot?.latestAssistantAfterUser!==true
    || snapshot?.latestUserTextSha256!==promptSha256 || Number(snapshot?.completionActionCount)<=0
    || Number(snapshot?.stopControlCount)!==0 || Number(snapshot?.streamingMarkerCount)!==0)return null;
  const users=snapshot.users,assistants=snapshot.assistants;
  if(!Array.isArray(users)||!users.length||!Array.isArray(assistants)||!assistants.length)return null;
  const userCount=snapshot.userCount,assistantCount=snapshot.assistantCount;
  if(!Number.isSafeInteger(userCount)||userCount<1||!Number.isSafeInteger(assistantCount)||assistantCount<1)return null;
  const lastUser=users.at(-1),lastAssistant=assistants.at(-1);
  if(!lastUser?.key || !lastAssistant?.key || !String(lastAssistant.text||"").trim())return null;
  return JSON.stringify([conversationId(chatUrl),promptSha256,userCount,assistantCount,
    lastUser.key,lastAssistant.key,lastAssistant.text]);
}

function receiptFromCheckpoint(snapshot,{outputMode="text",now=Date.now}={}){
  return {schemaVersion:2,sendAttempted:true,submissionAccepted:true,
    assistantCount:snapshot.assistantCount-1,userCount:snapshot.userCount-1,
    assistantKey:snapshot.assistants.at(-2)?.key||"",userKey:snapshot.users.at(-2)?.key||"",
    generatedImageKeys:[],outputMode,captureGeneratedImage:outputMode==="generated_image",
    createdAt:now(),acceptedAt:now(),acceptanceSignal:"exact-checkpoint-dom"};
}
module.exports={checkpointReceiptProof,receiptFromCheckpoint};
