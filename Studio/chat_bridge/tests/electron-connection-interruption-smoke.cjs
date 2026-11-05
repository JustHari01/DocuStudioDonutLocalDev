"use strict";
const {app,BrowserWindow,session}=require('electron');
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {chatTurnSnapshot,waitForAnswer}=require('../src/automation.cjs');
const {executePageProbe}=require('../src/automation.cjs');
const {readExistingImage}=require('../src/existing-image-read.cjs');
const {replaceFailedIdleReader,retireFailedRenderer}=require('../src/renderer-recovery.cjs');
const vm=require('node:vm');
const proof=process.env.DOCURANDOM_INTERRUPTION_PROOF;
app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'DocuRandomInterruption-')));
app.on('window-all-closed',()=>{});
async function main(){
  await app.whenReady();
  const isolated=session.fromPartition('interruption-proof');
  const url='https://chatgpt.com/c/exact-interruption-fixture';
  let loads=0,sends=0;
  isolated.protocol.handle('https',request=>{
    assert.equal(request.url,url); loads++;
    const response=loads===1
      ? '<div role="status">Connection interrupted. Waiting for the complete answer</div><button data-testid="stop-button" aria-label="Stop generating">Stop</button>'
      : '<div data-message-author-role="assistant" data-message-id="new"><div class="markdown">Recovered final answer</div><button data-testid="copy-turn-action-button" aria-label="Copy">Copy</button></div>';
    return new Response('<main><div data-message-author-role="assistant" data-message-id="old"><div class="markdown">Old answer</div></div><div data-message-author-role="user" data-message-id="latest">Accepted prompt</div>'+response+'<form><div contenteditable="true" role="textbox" style="height:32px"> </div><button onclick="console.log(\'FIXTURE_SEND\')">Send</button></form></main>',{headers:{'Content-Type':'text/html'}});
  });
  await isolated.cookies.set({url,name:'fixture-login',value:'kept',httpOnly:true});
  const window=new BrowserWindow({show:false,width:900,height:700,webPreferences:{session:isolated,sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
  window.webContents.on('console-message',(_e,_level,message)=>{if(message==='FIXTURE_SEND')sends++;});
  try{
    await window.loadURL(url);
    const first=await chatTurnSnapshot(window);
    assert.equal(first.connectionInterrupted,true);assert.equal(first.stopControlCount,1);
    const receipt={submissionAccepted:true,assistantCount:1,userCount:0,assistantKey:'data-message-id:old'};
    const recovered=await waitForAnswer(window,1,{submissionEvidence:receipt,persistedConversationUrl:url,
      requireExactConversation:true,connectionInterruptionStaleMs:100,recoveryReloadDelayMs:10,
      pollIntervalMs:10,evidenceOptions:{completionSettleMs:10},maxTimeoutMs:10000});
    assert.equal(recovered,'Recovered final answer');assert.equal(loads,2);assert.equal(sends,0);
    assert.equal(receipt.submissionAccepted,true);assert.equal(window.webContents.getURL(),url);
    assert.equal((await isolated.cookies.get({url,name:'fixture-login'}))[0].value,'kept');
    await window.webContents.executeJavaScript("document.querySelector('.markdown').textContent='Connection interrupted. Waiting for the complete answer';");
    assert.equal((await chatTurnSnapshot(window)).connectionInterrupted,false);
    await assert.rejects(executePageProbe(window,'new Promise(()=>{})',{timeoutMs:30}),{code:'CHATGPT_PAGE_PROBE_TIMEOUT'});
    await new Promise(resolve=>setTimeout(resolve,5050));
    const code=fs.readFileSync(path.join(__dirname,'../src/main.cjs'),'utf8');
    const start=code.indexOf('  if (request.method === "POST" && url.pathname === "/v1/session/existing-image")');
    const end=code.indexOf('  if (request.method === "POST" && url.pathname === "/v1/session/repair-conversation")',start);
    const context={busy:false,window:null,chatUrl:url,pendingSubmission:receipt};
    const saved={chatUrl:url,pendingSubmission:receipt};
    let fresh;
    const scope=vm.createContext({request:{method:'POST'},url:{pathname:'/v1/session/existing-image'},response:{},
      readJson:async()=>({thread_id:'fixture'}),normalizeTurnIdentity:()=> 'thread:fixture',
      savedTurns:new Map([['thread:fixture',saved]]),turnConversations:new Map([['thread:fixture',context]]),
      turnControllers:new Map(),existingImageReaders:new Set(),mainWindow:window,
      contextWindowUsable:()=>false,isExactChatConversationUrl:()=>true,conversationContexts:()=>new Set([context]),
      replaceFailedIdleReader,retireFailedRenderer,readExistingImage,
      createMainWindow:()=>{fresh=new BrowserWindow({show:false,webPreferences:{session:isolated,sandbox:true}});return fresh;},
      sendJson:()=>assert.fail('poisoned reader returned success'),Date});
    vm.runInContext('async function imageRoute(){'+code.slice(start,end)+'}',scope);
    await assert.rejects(scope.imageRoute(),{code:'CHATGPT_PAGE_PROBE_IN_FLIGHT'});
    assert.equal(window.isDestroyed(),true);assert.equal(scope.mainWindow,fresh);
    assert.equal(context.pendingSubmission,receipt);assert.equal(saved.chatUrl,url);
    assert.equal(context.busy,false);assert.equal(scope.existingImageReaders.size,0);
    assert.equal(fresh.webContents.session,isolated);fresh.destroy();
    fs.writeFileSync(proof,JSON.stringify({actualElectron:true,interruptedStopRecovered:true,exactConversationReloads:1,
      acceptedReceiptAndCookiePreserved:true,quotedAnswerExcluded:true,productionImageRouteReplacedPoisonedMain:true,
      promptsSubmitted:sends,externalRequests:0},null,2));
  }finally{if(!window.isDestroyed())window.destroy();}
}
main().then(()=>app.quit()).catch(error=>{if(proof)fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error)}));app.exit(1);});
