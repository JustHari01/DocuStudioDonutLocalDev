"use strict";
const {conversationId}=require('./conversation-health.cjs');

function homeConversationProof(snapshot,page,{url,promptSha256}={}){
  if(!conversationId(url) || !/^[a-f0-9]{64}$/.test(promptSha256||'')
    || conversationId(snapshot?.url)!==conversationId(url)
    || conversationId(page?.url)!==conversationId(url)
    || snapshot?.readyState!=='complete' || page?.authenticated!==true || page?.ready!==true
    || snapshot?.conversationLoadFailed || snapshot?.accountLoadFailed
    || page?.conversationLoadFailed || page?.accountLoadFailed || page?.rateLimited
    || snapshot?.connectionInterrupted
    || ![snapshot?.latestUserTextSha256,snapshot?.latestUserContentSha256].includes(promptSha256)
    || snapshot?.userCount!==1 || !snapshot?.users?.[0]?.key)return null;
  return JSON.stringify([conversationId(url),promptSha256,snapshot.users[0].key]);
}
module.exports={homeConversationProof};
