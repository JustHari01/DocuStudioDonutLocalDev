"use strict";
const {chatTurnSnapshot, generatedImageSnapshot, executePageProbe} = require('./automation.cjs');
const {existingImageResult} = require('./image-retry.cjs');

// Read only: only expand display text and scroll the owning chat. Never send a
// prompt, click Retry, change the selected model or stop a generation.
async function scrollHistory(window, action, saved = {}) {
  return executePageProbe(window, `(() => {
    const role=document.querySelector('main [data-message-author-role],main [data-turn],main article,main [data-chatgpt-search-message-ids],main [class~="group/user-message"]');
    let scroller=role?.parentElement;
    while(scroller && !(scroller.scrollHeight>scroller.clientHeight+4
      && /auto|scroll/.test(getComputedStyle(scroller).overflowY))) scroller=scroller.parentElement;
    scroller ||= [...document.querySelectorAll('main,main *,[role="main"],[role="main"] *')]
      .filter(n=>n.scrollHeight>n.clientHeight+4&&n.clientHeight>100&&n.clientWidth>300
        && /auto|scroll/.test(getComputedStyle(n).overflowY))
      .sort((a,b)=>b.clientWidth-a.clientWidth)[0];
    scroller ||= document.scrollingElement;
    if(!scroller)return {available:false};
    const before=scroller.scrollTop, maximum=Math.max(0,scroller.scrollHeight-scroller.clientHeight);
    const initial={top:before,atBottom:maximum-before<8};
    const action=${JSON.stringify(action)}, saved=${JSON.stringify(saved)};
    if(action==='start')scroller.scrollTop=0;
    else if(action==='next')scroller.scrollTop=Math.min(maximum,before+Math.max(100,scroller.clientHeight*0.75));
    else if(action==='restore')scroller.scrollTop=saved.atBottom?maximum:Math.min(maximum,Number(saved.top)||0);
    return {available:true,...initial,topAfter:scroller.scrollTop,maximum,
      atEnd:maximum-scroller.scrollTop<4,changed:Math.abs(scroller.scrollTop-before)>1,
      olderLoading:/loading older messages/i.test(document.querySelector('main')?.innerText||'')};
  })()`);
}

async function expandThumbnailPrompts(window){
  return executePageProbe(window,`(()=>{
    let expanded=0;
    for(const n of document.querySelectorAll('main [data-message-author-role="user"],main [data-turn="user"],main [class~="group/user-message"]')){
      if(!String(n.textContent||'').includes('[DOCURANDOM_STAGE=thumbnail]'))continue;
      for(const b of n.querySelectorAll('button,[role="button"]')){
        if(/^(show more|read more|hiển thị thêm|xem thêm)$/i.test((b.getAttribute('aria-label')||b.innerText||'').trim())){b.click();expanded++;}
      }
    }
    return expanded;
  })()`);
}

function idle(snapshot) {
  return snapshot?.readyState === 'complete' && !snapshot.pageError
    && !snapshot.accountLoadFailed && !snapshot.conversationLoadFailed
    && Number(snapshot.stopControlCount) === 0 && Number(snapshot.streamingMarkerCount) === 0;
}
function tail(snapshot) {
  return [snapshot.latestUserTextSha256, snapshot.latestAssistantTextSha256].join(':');
}

async function readHistoricalImage(window, {expectedUserPromptSha256, timeoutMs=18_000,
  pollMs=180, maxSteps=48} = {}) {
  if(!/^[a-f0-9]{64}$/.test(expectedUserPromptSha256 || ''))return null;
  const savedUrl=window.webContents.getURL();
  if(!/^https:\/\/chatgpt\.com\/c\/[a-zA-Z0-9-]+(?:[?#].*)?$/.test(savedUrl))return null;
  let before=await chatTurnSnapshot(window);
  if(!idle(before))return null;
  await expandThumbnailPrompts(window);
  before=await chatTurnSnapshot(window);
  if(!idle(before) || window.webContents.getURL()!==savedUrl)return null;
  let scroll, recovered, complete=false, scans=0, matchedPrompts=0, observedImages=0;
  const deadline=Date.now()+timeoutMs;
  try {
    scroll=await scrollHistory(window,'start');
    for(;scans<maxSteps && Date.now()<deadline;scans++) {
      await new Promise(resolve=>setTimeout(resolve,pollMs));
      if(window.webContents.getURL()!==savedUrl)throw Error('CHATGPT_CONVERSATION_CHANGED: Preserve the thumbnail receipt');
      await expandThumbnailPrompts(window);
      const snapshot=await chatTurnSnapshot(window);
      if(!idle(snapshot))break;
      const image=await generatedImageSnapshot(window,{expectedUserPromptSha256,allowEarlierMatchingPrompts:true});
      matchedPrompts=Math.max(matchedPrompts,Number(image.matchedPromptCount)||0);
      observedImages=Math.max(observedImages,Number(image.historyImageCount)||0);
      const result=existingImageResult(snapshot,image,{expectedUserPromptSha256});
      if(result.status==='ready'){recovered=result;break;}
      if(image.busy)break;
      const step=await scrollHistory(window,'next');
      if(!step.available || step.atEnd && !step.changed){complete=!step.olderLoading;break;}
    }
  } finally {
    if(scroll?.available && window.webContents.getURL()===savedUrl)
      await scrollHistory(window,'restore',scroll);
  }
  await new Promise(resolve=>setTimeout(resolve,pollMs));
  const after=await chatTurnSnapshot(window);
  const stable=window.webContents.getURL()===savedUrl && idle(after) && tail(before)===tail(after);
  const proof={scans:scans+1,complete,stable,matchedPrompts,observedImages,promptsSubmitted:0};
  if(!stable)return {status:'pending',history_scan:proof,detail:'Historical image read could not verify an unchanged idle conversation'};
  if(recovered)return {...recovered,history_scan:proof};
  return {status:complete?'no-image':'pending',history_scan:proof,
    detail:complete?'No image found for the exact thumbnail prompt in the loaded history':'Historical thumbnail read is incomplete; preserve checkpoint'};
}
module.exports={readHistoricalImage,scrollHistory};
