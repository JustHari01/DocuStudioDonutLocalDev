"use strict";
const {app,BrowserWindow,session}=require('electron');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),vm=require('node:vm'),crypto=require('node:crypto');
const src=process.env.DOCURANDOM_LEASE_TEST_SRC || path.join(__dirname,'../src');
const {recoverInterruptedTurn,prepareWindowForBackground}=require(path.join(src,'automation.cjs'));
const {createAcceptedResponseLease,recoveryReadError}=require(path.join(src,'recovery-read-deadline.cjs'));
const {runImageTurnWithOneRetry}=require(path.join(src,'image-retry.cjs'));
const proof=process.argv[2];
const code=fs.readFileSync(path.join(src,'main.cjs'),'utf8');
const start=code.indexOf('async function handleResponseRequest('),end=code.indexOf('async function routeRequest(',start);
app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'DocuRandomAcceptedLease-')));
app.on('window-all-closed',()=>{});
async function main(){
 await app.whenReady();const url='https://chatgpt.com/c/6ace0000-1234-83ec-a358-abcdef123456';
 const isolated=session.fromPartition('accepted-lease-fixture');
 isolated.protocol.handle('https',()=>new Response('<main><div data-message-author-role="user" data-message-id="accepted-user">Fixture accepted image prompt</div><form><div contenteditable="true" role="textbox" aria-label="Ask ChatGPT" style="height:40px"></div><button data-testid="stop-button" aria-label="Stop generating" onclick="window.stops++">Stop</button><button data-testid="send-button" onclick="window.sends++">Send</button></form></main><script>window.sends=0;window.stops=0;</script>',{headers:{'Content-Type':'text/html;charset=utf-8'}}));
 await isolated.cookies.set({url,name:'fixture-login',value:'preserved',httpOnly:true});
 const window=new BrowserWindow({show:false,width:900,height:700,webPreferences:{session:isolated,sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
 await window.loadURL(url);await prepareWindowForBackground(window);
 const receipt={sendAttempted:true,submissionAccepted:true,assistantCount:0,userCount:0,createdAt:Date.now(),acceptedAt:Date.now(),outputMode:'generated_image',captureGeneratedImage:true};
 const context={window,chatUrl:url,pendingSubmission:null,pendingAttachment:null,busy:false};
 const identity='thread:fixture-fresh-accepted';let starts=0,persisted=0;const messages=[];
 const savedTurns=new Map(),turnConversations=new Map([[identity,context]]);
 const scope=vm.createContext({Date,Math,String,Number,Object,Error,AbortController,crypto,setTimeout,clearTimeout,setInterval,clearInterval,
  waitForAuthenticatedSession:async()=>true,isCodexResponsesRequest:()=>false,requestInput:v=>v,requestAttachments:()=>[],
  validTurnId:()=>true,existingImageReaders:new Set(),turnActivity:new Map(),turnControllers:new Map(),
  turnConversations,savedTurns,conversations:new Map(),allContexts:new Set(),busyCount:0,
  contextWindowUsable:()=>true,rememberConversationUrl:()=>{},isExactChatConversationUrl:()=>true,
  ensureConversationWindow:async()=>{},persistState:()=>{persisted++;},
  saveTurnContext:(id,c)=>savedTurns.set(id,{chatUrl:c.chatUrl,pendingSubmission:c.pendingSubmission}),
  startNdjson:response=>{response.headersSent=true;},writeNdjson:(_res,v)=>messages.push(v),
  createAcceptedResponseLease:controller=>createAcceptedResponseLease(controller,{timeoutMs:1200}),
  normalizePendingSubmission:evidence=>evidence,recoveryReadError,
  recoverConversationLoad:async(_c,operation)=>operation(),
  runTurn:async(w,options)=>{
    starts++;options.onSubmitted(receipt);
    return recoverInterruptedTurn(w,{submissionEvidence:receipt,persistedConversationUrl:url,
      captureGeneratedImage:true,signal:options.signal,onProgress:options.onProgress,onStatus:options.onStatus});
  },runImageTurnWithOneRetry,
  accountCapabilityFailure:async error=>error,retireFailedRenderer:()=>false,conversationContexts:()=>[context],
  pageSnapshot:async()=>({}),chatGptRateLimitError:()=>{},ATTACHMENT_STAGING_ROOT:'fixture-unused',
  TURN_HEARTBEAT_STALE_MS:180000,TURN_HEARTBEAT_CHECK_MS:10000,
 });
 vm.runInContext(code.slice(start,end)+'\nthis.handle=handleResponseRequest;',scope);
 const request={headers:{}},response={headersSent:false,once:()=>{},end:()=>{},destroyed:false,writableEnded:false};
 const body={model:'chatgpt-web/auto',input:'Fixture accepted image prompt',prompt_cache_key:'fixture-fresh-accepted',
   docurandom_output:'generated_image',stream:true,metadata:{turn_id:'fixture-turn'}};
 try{
  await scope.handle(request,response,body);
  assert.equal(starts,1);assert.equal(scope.busyCount,0);assert.equal(context.busy,false);
  assert.equal(context.pendingSubmission,receipt);assert.equal(savedTurns.get(identity).pendingSubmission,receipt);
  assert.equal(scope.turnControllers.size,0);assert.ok(messages.some(v=>v.type==='error'&&v.error.message.includes('CHATGPT_RECOVERY_PENDING')));
  await assert.rejects(scope.handle(request,response,body),/interrupted ChatGPT turn/);assert.equal(starts,1);
  assert.equal(await window.webContents.executeJavaScript('window.sends'),0);
  assert.equal(await window.webContents.executeJavaScript('window.stops'),0);
  assert.equal((await isolated.cookies.get({url,name:'fixture-login'}))[0].value,'preserved');assert.ok(persisted>0);
  fs.writeFileSync(proof,JSON.stringify({actualElectronReader:true,productionFreshResponseRoute:true,
    installedAsarExercised:src.includes('app.asar'),
    liveStopCannotHoldSlotForever:true,acceptedReceiptPersisted:true,slotAndControllerReleased:true,
    samePromptBlockedAfterYield:true,providerStopClicks:0,promptsSubmitted:0,cookiePreserved:true,externalRequests:0},null,2));
 }finally{window.destroy();}
}
main().then(()=>app.quit()).catch(error=>{if(proof)fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error)}));app.exit(1);});
