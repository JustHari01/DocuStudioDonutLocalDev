"use strict";
const {chatTurnSnapshot,generatedImageSnapshot,withRenderedWindowStage,reloadSavedChatPage}=require("./automation.cjs");
const {existingImageResult}=require("./image-retry.cjs");
const {readHistoricalImage}=require("./historical-image-read.cjs");
async function readExistingImage(window,{timeoutMs=20000,pollMs=300,reloadOnFailure=true,expectedUserPromptSha256=""}={}){
  return withRenderedWindowStage(window,undefined,()=>readImageDOM(window,{timeoutMs,pollMs,reloadOnFailure,expectedUserPromptSha256}));
}
async function readImageDOM(window,{timeoutMs,pollMs,reloadOnFailure,expectedUserPromptSha256}){
  let deadline=Date.now()+timeoutMs;
  const savedUrl=window.webContents.getURL();
  let snapshot;
  let reloadUsed=false;
  do{
    snapshot=await chatTurnSnapshot(window);
    if(window.webContents.getURL()!==savedUrl)throw new Error('CHATGPT_CONVERSATION_CHANGED: Preserve the submitted image receipt');
    if(snapshot.accountLoadFailed)throw new Error("ChatGPT could not load the saved image conversation; preserve checkpoint");
    if(snapshot.conversationLoadFailed){
      if(!reloadOnFailure)throw new Error('CHATGPT_CONVERSATION_LOAD_FAILED: Preserve the submitted image receipt');
      if(!reloadUsed){
        reloadUsed=true;
        await reloadSavedChatPage(window,savedUrl,{allowHomeReload:false,reason:'Read the same saved image conversation after a loading failure'});
        // The /c/ URL and document can be ready before the conversation SPA.
        // Give the same page time to hydrate rather than treating its first
        // post-reload error banner as a second independent failure.
        deadline=Date.now()+timeoutMs;
      }else{
        if(Date.now()>=deadline)throw new Error('CHATGPT_CONVERSATION_LOAD_FAILED: Preserve the submitted image receipt');
        await new Promise(resolve=>setTimeout(resolve,pollMs));
      }
      continue;
    }
    // Reading a completed image does not require an editable composer. Image
    // cards can remain valid while ChatGPT hides or rebuilds its chat input.
    if(Number(snapshot.userCount)>0 || Number(snapshot.assistantCount)>0)break;
    if(Date.now()>=deadline)break;
    await new Promise(resolve=>setTimeout(resolve,pollMs));
  }while(true);
  const collect=async()=>existingImageResult(snapshot,await generatedImageSnapshot(window,{expectedUserPromptSha256}),{expectedUserPromptSha256});
  let result=await collect();
  while(result.status==='pending' && !result.image_busy && Date.now()<deadline){
    await new Promise(resolve=>setTimeout(resolve,pollMs));
    snapshot=await chatTurnSnapshot(window);
    if(window.webContents.getURL()!==savedUrl)throw new Error('CHATGPT_CONVERSATION_CHANGED: Preserve the submitted image receipt');
    result=await collect();
  }
  if(result.status!=='ready' && !result.image_busy && expectedUserPromptSha256) {
    const historical=await readHistoricalImage(window,{expectedUserPromptSha256,timeoutMs:10000});
    if(historical)return {...result,...historical};
  }
  return result;
}
module.exports={readExistingImage};
