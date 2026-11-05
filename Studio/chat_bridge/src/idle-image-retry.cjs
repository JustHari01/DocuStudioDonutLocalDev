"use strict";
const {conversationId}=require('./conversation-health.cjs');
function idleImageRetryProof(snapshot,page,pending,{chatUrl,promptSha256,now=Date.now()}={}){
 if(!pending?.submissionAccepted || !pending.sendAttempted || pending.outputMode!=='generated_image'
   || !Number.isFinite(pending.createdAt) || pending.createdAt<=0 || now-pending.createdAt<15*60_000
   || !conversationId(chatUrl) || conversationId(snapshot?.url)!==conversationId(chatUrl)
   || conversationId(page?.url)!==conversationId(chatUrl)
   || !/^[a-f0-9]{64}$/.test(promptSha256||'') || snapshot?.latestUserTextSha256!==promptSha256
   || snapshot?.readyState!=='complete' || page?.authenticated!==true || page?.ready!==true
   || snapshot?.conversationLoadFailed || snapshot?.accountLoadFailed || snapshot?.connectionInterrupted
   || page?.conversationLoadFailed || page?.accountLoadFailed || page?.rateLimited
   || snapshot?.userCount!==pending.userCount+1 || snapshot?.assistantCount!==pending.assistantCount
   || Number(snapshot?.stopControlCount)!==0 || Number(snapshot?.streamingMarkerCount)!==0
   || !Array.isArray(snapshot?.imageNodes) || snapshot.imageNodes.length!==0)return null;
 const user=snapshot.users?.at(-1),assistant=snapshot.assistants?.at(-1);
 if(!user?.key || (pending.userKey && user.key===pending.userKey)
   || (assistant?.key||'')!==(pending.assistantKey||''))return null;
 return JSON.stringify([conversationId(chatUrl),pending.createdAt,user.key,promptSha256]);
}
module.exports={idleImageRetryProof};
