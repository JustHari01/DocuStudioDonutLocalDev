"use strict";
const {app,BrowserWindow,session}=require('electron');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),vm=require('node:vm');
const src=process.env.DOCURANDOM_RECOVERY_TEST_SRC || path.join(__dirname,'../src');
const {recoverInterruptedTurn,prepareWindowForBackground}=require(path.join(src,'automation.cjs'));
const {beginRecoveryReadDeadline,recoveryReadError}=require(path.join(src,'recovery-read-deadline.cjs'));
const proof=process.argv[2],code=fs.readFileSync(path.join(src,'main.cjs'),'utf8');
let debug={};
const start=code.indexOf('  if (request.method === "POST" && url.pathname === "/v1/turns/recover")');
const end=code.indexOf('  if (request.method === "POST" && url.pathname === "/v1/turns/cancel")',start);
app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'DocuRandomRecoveryProgress-')));
app.on('window-all-closed',()=>{});
async function main(){
 await app.whenReady();const results=[];
 for(const mode of ['interrupted-stop','empty-response']){
  debug={mode,statuses:[]};
  const url='https://chatgpt.com/c/6ace0000-1234-83ec-a358-abcdef123456';
  const isolated=session.fromPartition('recovery-progress-'+mode);let loads=0;
  isolated.protocol.handle('https',()=>{
   loads++;const text=loads>1?'<div data-message-author-role="assistant" data-message-id="new-answer"><div class="markdown">Recovered final reply</div><button aria-label="Copy">Copy</button></div>':mode==='interrupted-stop'?'<p>Connection interrupted. Waiting for the complete answer.</p><button data-testid="stop-button" aria-label="Stop generating" onclick="window.stops++">Stop</button>':'';
   return new Response('<main><div data-message-author-role="assistant" data-message-id="old"><div class="markdown">Earlier completed answer</div><button aria-label="Copy">Copy</button></div><div data-message-author-role="user" data-message-id="accepted-user">Accepted fixture prompt</div>'+text+'<form><div id="prompt-textarea" contenteditable="true" role="textbox" style="height:35px"></div><button data-testid="send-button" onclick="window.sends++">Send</button></form></main><script>window.sends=0;window.stops=0;</script>',{headers:{'Content-Type':'text/html;charset=utf-8','Cache-Control':'no-store'}});
  });
  await isolated.cookies.set({url,name:'fixture-login',value:'kept',httpOnly:true});
  const window=new BrowserWindow({show:false,width:900,height:700,webPreferences:{session:isolated,sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
  await window.loadURL(url);await prepareWindowForBackground(window);
  const receipt={sendAttempted:true,submissionAccepted:true,assistantCount:1,userCount:0,assistantKey:'data-message-id:old',userKey:'',createdAt:Date.now()-180000};
  const context={window,chatUrl:url,pendingSubmission:receipt,pendingAttachment:null,busy:false};
  const identity='thread:recovery-progress',savedTurns=new Map([[identity,{chatUrl:url,pendingSubmission:receipt}]]);let result;
  const scope={Date,Math,String,Number,Object,Error,AbortController,
   request:{method:'POST'},url:{pathname:'/v1/turns/recover'},response:{once:()=>{}},readJson:async()=>({thread_id:'recovery-progress'}),
   existingImageReaders:new Set(),turnControllers:new Map(),turnConversations:new Map([[identity,context]]),savedTurns,turnActivity:new Map(),busyCount:0,
   ensureConversationWindow:async()=>{},persistState:()=>{},saveTurnContext:(id,c)=>savedTurns.set(id,{chatUrl:c.chatUrl,pendingSubmission:c.pendingSubmission}),
   sendJson:(_r,status,value)=>{assert.equal(status,200);result=value;},discardPendingAttachment:()=>{},retireFailedRenderer:()=>false,
   beginRecoveryReadDeadline:c=>beginRecoveryReadDeadline(c,{timeoutMs:20000}),recoveryReadError,
   recoverInterruptedTurn:(w,options)=>recoverInterruptedTurn(w,{...options,onStatus:(stage,message)=>{debug.statuses.push({stage,message});options.onStatus?.(stage,message);}}),
   RECOVERY_CONNECTION_STALE_MS:100,RECOVERY_NO_RESPONSE_STALE_MS:100,
   recoverConversationLoad:async(_c,operation)=>operation(),
  };
  try{
   await vm.runInNewContext('(async()=>{'+code.slice(start,end)+'})()',scope);
   assert.equal(result.output_text,'Recovered final reply');assert.equal(loads,2);
   assert.equal(context.pendingSubmission,null);assert.equal(context.busy,false);assert.equal(scope.busyCount,0);
   assert.equal(await window.webContents.executeJavaScript('window.sends'),0);
   assert.equal(await window.webContents.executeJavaScript('window.stops'),0);
   assert.equal((await isolated.cookies.get({url,name:'fixture-login'}))[0].value,'kept');
   results.push({mode,exactReloads:1,oldReplyRecovered:true,noNewPrompt:true,noProviderStop:true,cookieKept:true});
  }finally{debug.loads=loads;window.destroy();}
 }
 fs.writeFileSync(proof,JSON.stringify({actualElectronReader:true,productionRecoveryRoute:true,installedAsarExercised:src.includes('app.asar'),cases:results,externalRequests:0},null,2));
}
main().then(()=>app.quit()).catch(error=>{if(proof)fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error),debug}));app.exit(1);});
