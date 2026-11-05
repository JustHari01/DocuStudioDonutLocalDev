"use strict";
const {executePageProbe,chatTurnSnapshot,withRenderedWindowStage,withTrustedWindowStage,activateSendButton}=require('./automation.cjs');
const {scrollHistory}=require('./historical-image-read.cjs');

// Only display controls in an exact, idle saved chat. There is deliberately no
// selector, JavaScript, navigation URL, composer input or retry action API.
const VIEW_ACTIONS=Object.freeze({
  expand: /^(show more|read more|hiển thị thêm|xem thêm)$/i,
  canvas: /^(canvas|canvas view|switch to canvas view)$/i,
  previous: /^(previous response|previous image|phản hồi trước|ảnh trước)$/i,
  image: /^(open image|view image|generated image|image created|xem ảnh|mở ảnh)$/i,
});
function permitted(action,label){return Boolean(VIEW_ACTIONS[action]?.test(String(label||'').trim()));}
function idle(snapshot){return snapshot?.readyState==='complete' && !snapshot.pageError
  && !snapshot.accountLoadFailed && !snapshot.conversationLoadFailed
  && Number(snapshot.stopControlCount)===0 && Number(snapshot.streamingMarkerCount)===0;}
function assertOwned(window,url){if(window.webContents.getURL()!==url)throw Error('CHATGPT_CONVERSATION_CHANGED: Preserve the thumbnail receipt');}
async function viewSnapshot(window){
  return executePageProbe(window,`(async()=>{
    const visible=n=>{const r=n.getBoundingClientRect(),s=getComputedStyle(n);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'};
    const controls=[...document.querySelectorAll('button,[role="button"],a')].filter(visible).map((n,index)=>{
      const r=n.getBoundingClientRect();return {index,label:(n.getAttribute('aria-label')||n.getAttribute('title')||n.innerText||'').trim().slice(0,200),
        testid:n.getAttribute('data-testid')||'',role:n.getAttribute('role')||n.tagName.toLowerCase(),
        disabled:n.disabled===true||n.getAttribute('aria-disabled')==='true',x:r.x+r.width/2,y:r.y+r.height/2};});
    const users=[...document.querySelectorAll('[data-message-author-role="user"],[data-turn="user"],[class~="group/user-message"]')]
      .filter(n=>!n.parentElement?.closest('[data-message-author-role="user"],[data-turn="user"],[class~="group/user-message"]'));
    const userPrompts=[];
    for(const n of users){
      const content=n.querySelector('[data-user-message-bubble] .whitespace-pre-wrap,.whitespace-pre-wrap[dir="auto"]')||n;
      const copy=content.cloneNode(true);copy.querySelectorAll('button,[role="button"],svg,script,style').forEach(x=>x.remove());
      const text=String(copy.textContent||'').normalize('NFC').replace(/\\s+/g,' ').trim();
      const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text));
      userPrompts.push({length:text.length,sha256:[...new Uint8Array(digest)].map(b=>b.toString(16).padStart(2,'0')).join(''),prefix:text.slice(0,140),suffix:text.slice(-140)});
    }
    const images=[...document.querySelectorAll('img')].filter(visible).map(n=>({alt:n.alt||'',width:n.naturalWidth,height:n.naturalHeight,
      srcScheme:(n.currentSrc||n.src||'').split(':')[0],inUser:Boolean(n.closest('[data-message-author-role="user"],[data-turn="user"],[class~="group/user-message"]'))}));
    return {url:location.href,title:document.title,controls:controls.slice(0,300),userPrompts: userPrompts.slice(0,100),images:images.slice(0,100),bodyText:(document.querySelector('main')?.innerText||'').slice(0,18000)};
  })()`);
}
async function inspectImageHistory(window,{savedUrl,action='inspect',control}={}){
  if(!/^https:\/\/chatgpt\.com\/c\/[a-zA-Z0-9-]+(?:[?#].*)?$/.test(savedUrl||''))throw Error('Exact saved image conversation required');
  return withRenderedWindowStage(window,undefined,async()=>{
    assertOwned(window,savedUrl);
    const before=await chatTurnSnapshot(window);
    if(!idle(before))throw Error('Image history view requires an idle conversation');
    let view=await viewSnapshot(window);
    if(action==='history-top'||action==='history-next'){
      await scrollHistory(window,action==='history-top'?'start':'next');
      await new Promise(r=>setTimeout(r,1000));
      assertOwned(window,savedUrl);
      view=await viewSnapshot(window);
    }else if(action!=='inspect'){
      if(!control || !permitted(action,control.label))throw Error('Only observed image display controls are allowed');
      const found=view.controls.find(c=>c.index===control.index&&c.label===control.label&&c.testid===control.testid&&!c.disabled);
      if(!found)throw Error('Image display control changed; inspect again');
      assertOwned(window,savedUrl);
      // This is a display action on an already observed exact control. Bring
      // only that element into view and re-read its location before clicking.
      await executePageProbe(window,`(()=>{const nodes=[...document.querySelectorAll('button,[role="button"],a')].filter(n=>{const r=n.getBoundingClientRect(),s=getComputedStyle(n);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'});
        const n=nodes[${JSON.stringify(found.index)}];if(n&&(n.getAttribute('aria-label')||n.getAttribute('title')||n.innerText||'').trim()===${JSON.stringify(found.label)})n.scrollIntoView({block:'center'});return true;})()`);
      const fresh=(await viewSnapshot(window)).controls.find(c=>c.index===found.index&&c.label===found.label&&c.testid===found.testid&&!c.disabled);
      if(!fresh || fresh.y<0)throw Error('Display control moved; inspect again');
      await withTrustedWindowStage(window,undefined,()=>activateSendButton(window,fresh));
      await new Promise(r=>setTimeout(r,500));
      assertOwned(window,savedUrl);
      if(!idle(await chatTurnSnapshot(window)))throw Error('Image display changed the idle state; preserve checkpoint');
      view=await viewSnapshot(window);
    }
    assertOwned(window,savedUrl);
    let screenshot;
    try {
      screenshot=await withTrustedWindowStage(window,undefined,async()=>{
        const opacity=window.getOpacity();
        try {window.setOpacity(1);await new Promise(r=>setTimeout(r,200));
          const png=await window.webContents.capturePage(undefined,{stayAwake:true});
          return {screenshot_base64:png.toPNG().toString('base64')};
        } finally {window.setOpacity(opacity);}
      });
    } catch(error){screenshot={screenshot_error:String(error?.message||error).slice(0,300)};}
    assertOwned(window,savedUrl);
    return {...view,...screenshot,promptsSubmitted:0};
  });
}
module.exports={inspectImageHistory,permitted,idle};
