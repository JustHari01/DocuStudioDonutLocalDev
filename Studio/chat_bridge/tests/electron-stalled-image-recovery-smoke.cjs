"use strict";
const {app,BrowserWindow,session}=require('electron');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),crypto=require('node:crypto'),vm=require('node:vm'),assert=require('node:assert/strict');
const src=process.env.DOCURANDOM_STALLED_IMAGE_TEST_SRC || path.join(__dirname,'../src');
const {chatTurnSnapshot,pageSnapshot,composerValue,stopStalledImage,waitForSubmissionAccepted,prepareWindowForBackground}=require(path.join(src,'automation.cjs'));
const {repairStalledImage}=require(path.join(src,'stalled-image-recovery.cjs'));
const {isExactChatConversationUrl}=require(path.join(src,'navigation.cjs'));
const code=fs.readFileSync(path.join(src,'main.cjs'),'utf8');
const start=code.indexOf("  if (request.method === 'POST' && url.pathname === '/v1/session/repair-stalled-image-turn')");
const end=code.indexOf('  if (request.method === "POST" && url.pathname === "/v1/session/retire-idle-image-turn")',start);
const proof=process.argv[2],digest=v=>crypto.createHash('sha256').update(v).digest('hex');
const normalize=v=>v.normalize('NFC').replace(/\s+/g,' ').trim();
const prompt='[DOCURANDOM_STAGE=thumbnail] '+ 'Create one original industrial documentary image. '.repeat(40);
const promptSha=digest(normalize(prompt)),draftSha=digest(prompt);
const escape=v=>v.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
let debug={};
app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'DocuRandomStalledImage-')));
app.on('window-all-closed',()=>{});
async function main(){
 await app.whenReady();const cases=[];
 for(const mode of ['stalled','legacy-echo','late-image','quota']){
  const url='https://chatgpt.com/c/6ace2222-1234-83ec-a358-abcdef123456';
  const isolated=session.fromPartition('stalled-image-'+mode);
  const userText=mode==='legacy-echo'?'TIẾP':prompt;
  isolated.protocol.handle('https',()=>new Response(`<main><div data-message-author-role="assistant" data-message-id="earlier"><div class="markdown">Earlier script</div><button aria-label="Copy">Copy</button></div><div data-message-author-role="user" data-message-id="image-user">${escape(userText)}</div><form style="position:fixed;bottom:20px;left:20px;background:white;width:700px"><div id="prompt-textarea" contenteditable="true" role="textbox" style="height:35px"></div>${mode==='legacy-echo'?'':'<button id="stop" data-testid="stop-button" aria-label="Stop generating" onclick="window.stops++;this.remove()">Stop</button>'}<button data-testid="send-button" onclick="window.sends++">Send</button></form></main><script>window.sends=0;window.stops=0;</script>`,{headers:{'Content-Type':'text/html;charset=utf-8'}}));
  await isolated.cookies.set({url,name:'fixture-login',value:'kept',httpOnly:true});
  const window=new BrowserWindow({show:false,width:900,height:700,webPreferences:{session:isolated,sandbox:true,
    contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
  await window.loadURL(url);await prepareWindowForBackground(window);
  const initial=await chatTurnSnapshot(window);
  const receipt={schemaVersion:2,sendAttempted:true,submissionAccepted:true,outputMode:'generated_image',
    createdAt:Date.now()-2*60*60_000,userCount:mode==='legacy-echo'?initial.userCount:7,
    assistantCount:mode==='legacy-echo'?initial.assistantCount:8,userKey:mode==='legacy-echo'?initial.users.at(-1).key:'legacy-key',
    assistantKey:mode==='legacy-echo'?initial.assistants.at(-1).key:'legacy-assistant',acceptanceSignal:'user-turn'};
  const original=JSON.stringify(receipt),identity='thread:stalled-image-fixture';
  const context={window,chatUrl:url,pendingSubmission:receipt,busy:false};
  const savedTurns=new Map([[identity,{chatUrl:url,pendingSubmission:receipt}]]),retiredImageReceipts=new Map(),imageRecoveryObservations=new Map();
  let result,status,offset=0,persisted;
  const scope={request:{method:'POST'},url:{pathname:'/v1/session/repair-stalled-image-turn'},response:{},
    readJson:async()=>({thread_id:'stalled-image-fixture',created_at:receipt.createdAt,prompt_sha256:promptSha,draft_sha256:draftSha}),
    normalizeTurnIdentity:thread=>'thread:'+thread,savedTurns,retiredImageReceipts,imageRecoveryObservations,
    turnConversations:new Map([[identity,context]]),turnControllers:new Map(),existingImageReaders:new Set(),
    isExactChatConversationUrl,Date,ensureConversationWindow:async()=>{},chatTurnSnapshot,pageSnapshot,composerValue,
    connectionHealth:()=>({classification:mode==='quota'?'rate-limited':'unknown'}),
    repairStalledImage:options=>repairStalledImage({...options,now:()=>Date.now()+offset}),
    stopStalledImage:async(...args)=>{
      if(mode==='late-image')await window.webContents.executeJavaScript(`const reply=document.createElement('div');reply.setAttribute('data-message-author-role','assistant');reply.innerHTML='<img width="1280" height="720" src="https://chatgpt.com/fixture-late-image">';document.querySelector('main').append(reply);void 0;`);
      return stopStalledImage(...args);
    },
    persistState:()=>{persisted=JSON.stringify({retired:[...retiredImageReceipts],observations:[...imageRecoveryObservations]});},
    saveTurnContext:(key,c)=>savedTurns.set(key,{chatUrl:c.chatUrl,pendingSubmission:c.pendingSubmission}),
    sendJson:(_response,httpStatus,value)=>{status=httpStatus;result=value;},
  };
  const run=()=>vm.runInNewContext('(async()=>{'+code.slice(start,end)+'})()',scope);
  try{
   if(mode==='quota'){
    await assert.rejects(run(),/HTTP 429/);assert.equal(context.pendingSubmission,receipt);
   }else{
    await run();assert.equal(status,200);
    if(mode!=='legacy-echo'){
     assert.equal(result.status,'observing');assert.equal(await window.webContents.executeJavaScript('window.stops'),0);
     // Observation journal survives an app restart; accelerate only its clock.
     const restore=JSON.parse(persisted);imageRecoveryObservations.clear();
     for(const [key,value]of restore.observations)imageRecoveryObservations.set(key,value);
     offset=92000;await run();
    }
    if(mode==='late-image'){
     assert.equal(result.safe_to_retry,false);assert.equal(context.pendingSubmission,receipt);assert.equal(retiredImageReceipts.size,0);
    }else{
     assert.equal(result.safe_to_retry,true);assert.equal(context.pendingSubmission,null);assert.equal(retiredImageReceipts.size,1);
     assert.equal(JSON.stringify([...retiredImageReceipts.values()][0].pendingSubmission),original);
     await run();assert.equal(retiredImageReceipts.size,1);assert.equal(result.safe_to_retry,true);
    }
   }
   const stops=await window.webContents.executeJavaScript('window.stops'),sends=await window.webContents.executeJavaScript('window.sends');
   assert.equal(stops,mode==='stalled'?1:0);assert.equal(sends,0);
   assert.equal(context.busy,false);assert.equal(scope.existingImageReaders.size,0);
   assert.equal((await isolated.cookies.get({url,name:'fixture-login'}))[0].value,'kept');
   cases.push({mode,providerStops:stops,promptsSubmitted:sends,receiptKept:true,cookieKept:true});
  }finally{debug={mode,status,result};window.destroy();}
 }
 const isolated=session.fromPartition('image-exact-echo');
 isolated.protocol.handle('https',()=>new Response('<main><div data-message-author-role="user" data-message-id="hydrated-user">TIẾP</div><form><div id="prompt-textarea" contenteditable="true" style="height:35px"></div></form></main>',{headers:{'Content-Type':'text/html'}}));
 const w=new BrowserWindow({show:false,width:900,height:700,webPreferences:{session:isolated,sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
 try{
  await w.loadURL('https://chatgpt.com/c/6ace3333-1234-83ec-a358-abcdef123456');await prepareWindowForBackground(w);
  await assert.rejects(waitForSubmissionAccepted(w,{beforeUser:0,expectedPromptSha256:promptSha,timeoutMs:300}),/chưa xác nhận/);
  await w.webContents.executeJavaScript('document.querySelector("[data-message-author-role=user]").textContent='+JSON.stringify(prompt)+';void 0;');
  const accepted=await waitForSubmissionAccepted(w,{beforeUser:9,beforeAssistant:9,expectedPromptSha256:promptSha,timeoutMs:1000});
  assert.equal(accepted.signal,'exact-user-prompt');
  await assert.rejects(waitForSubmissionAccepted(w,{beforeUser:9,beforeUserKey:accepted.userKey,
    beforeUserTextSha256:promptSha,expectedPromptSha256:promptSha,timeoutMs:300}),/chưa xác nhận/);
  await w.webContents.executeJavaScript(`const fresh=document.createElement('div');fresh.setAttribute('data-message-author-role','user');fresh.setAttribute('data-message-id','new-image-retry');fresh.textContent=${JSON.stringify(prompt)};document.querySelector('main').append(fresh);void 0;`);
  assert.equal((await waitForSubmissionAccepted(w,{beforeUser:9,beforeUserKey:accepted.userKey,
    beforeUserTextSha256:promptSha,expectedPromptSha256:promptSha,timeoutMs:1000})).signal,'exact-user-prompt');
 }finally{w.destroy();}
 fs.writeFileSync(proof,JSON.stringify({actualElectron:true,productionImageRepairRoute:true,installedAsarExercised:src.includes('app.asar'),
   privateFixtures:true,externalRequests:0,exactImageEchoRequired:true,hydratedCountsCannotConfirmWrongPrompt:true,
   observedReceiptSurvivesRestart:true,cases},null,2));
}
main().then(()=>app.quit()).catch(error=>{fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error),debug}));app.exit(1);});
