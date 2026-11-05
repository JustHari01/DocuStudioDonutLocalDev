"use strict";
const {app,BrowserWindow,session}=require('electron');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),vm=require('node:vm'),crypto=require('node:crypto');
const {chatTurnSnapshot,pageSnapshot}=require('../src/automation.cjs');
const {idleImageRetryProof}=require('../src/idle-image-retry.cjs');
const {isExactChatConversationUrl}=require('../src/navigation.cjs');
const proof=process.env.DOCURANDOM_IDLE_IMAGE_PROOF;
app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'DocuRandomIdleImage-')));
app.on('window-all-closed',()=>{});
async function main(){
 await app.whenReady();const isolated=session.fromPartition('idle-image');
 const url='https://chatgpt.com/c/6acd0d03-cf90-83ec-a71d-03b170c2116b';
 const prompt='[DOCURANDOM_STAGE=thumbnail]\nGenerate exactly the original thumbnail';
 let sends=0;
 isolated.protocol.handle('https',()=>new Response('<main><div class="group/user-message" data-chatgpt-search-unit-key="fallback-turn-0:0:user" data-chatgpt-search-message-ids="first-user"><div data-user-message-bubble="true"><div class="whitespace-pre-wrap" dir="auto">'+prompt+'</div><span aria-hidden="true">…</span><button>Show more</button></div></div><form><div id="prompt-textarea" contenteditable="true" role="textbox" style="height:50px"> </div><button data-testid="send-button" onclick="console.log(\'fixture-send\')">Send</button></form></main>',{headers:{'Content-Type':'text/html;charset=utf-8'}}));
 await isolated.cookies.set({url,name:'fixture-login',value:'kept',httpOnly:true});
 const window=new BrowserWindow({show:false,width:900,height:700,webPreferences:{session:isolated,sandbox:true,nodeIntegration:false,contextIsolation:true,backgroundThrottling:false}});
 window.webContents.on('console-message',(_event,...args)=>{if(args.some(value=>value==='fixture-send'))sends++;});
 try{
  await window.loadURL(url);
  const pending={createdAt:Date.now()-20*60_000,submissionAccepted:true,sendAttempted:true,
    outputMode:'generated_image',userCount:0,assistantCount:0,userKey:'',assistantKey:''};
  const identity='thread:idle-image-fixture',context={window,chatUrl:url,pendingSubmission:pending,busy:false};
  const savedTurns=new Map([[identity,{chatUrl:url,pendingSubmission:pending}]]),retiredImageReceipts=new Map();
  const body={thread_id:'idle-image-fixture',created_at:pending.createdAt,
    prompt_sha256:crypto.createHash('sha256').update(prompt.replace(/\s+/g,' ').trim()).digest('hex')};
  assert.equal((await chatTurnSnapshot(window)).latestUserTextSha256,body.prompt_sha256);
  const code=fs.readFileSync(path.join(__dirname,'../src/main.cjs'),'utf8');
  const start=code.indexOf('  if (request.method === "POST" && url.pathname === "/v1/session/retire-idle-image-turn")');
  const end=code.indexOf('  if (request.method === "POST" && url.pathname === "/v1/session/recover-home-conversation")',start);
  let status,result;
  const scope={request:{method:'POST'},url:{pathname:'/v1/session/retire-idle-image-turn'},response:{},readJson:async()=>body,
    normalizeTurnIdentity:thread=>'thread:'+thread,savedTurns,retiredImageReceipts,turnConversations:new Map([[identity,context]]),
    turnControllers:new Map(),existingImageReaders:new Set(),isExactChatConversationUrl,Date,setTimeout,
    ensureConversationWindow:async()=>{},chatTurnSnapshot,pageSnapshot,idleImageRetryProof,persistState:()=>{},
    saveTurnContext:(key,ctx)=>savedTurns.set(key,{chatUrl:ctx.chatUrl,pendingSubmission:ctx.pendingSubmission}),
    sendJson:(_res,httpStatus,value)=>{status=httpStatus;result=value;}};
  const run=()=>vm.runInNewContext('(async()=>{'+code.slice(start,end)+'})()',scope);
  body.prompt_sha256='0'.repeat(64);await run();assert.equal(status,409);assert.equal(context.pendingSubmission,pending);assert.equal(retiredImageReceipts.size,0);
  body.prompt_sha256=crypto.createHash('sha256').update(prompt.replace(/\s+/g,' ').trim()).digest('hex');
  await run();assert.equal(status,200);assert.equal(result.receipt_preserved,true);assert.equal(context.pendingSubmission,null);
  assert.equal(JSON.stringify([...retiredImageReceipts.values()][0].pendingSubmission),JSON.stringify(pending));
  assert.equal(savedTurns.get(identity).chatUrl,url);assert.equal(retiredImageReceipts.size,1);
  await run();assert.equal(status,409);assert.equal(retiredImageReceipts.size,1);
  assert.equal((await isolated.cookies.get({url,name:'fixture-login'}))[0].value,'kept');assert.equal(sends,0);
  fs.writeFileSync(proof,JSON.stringify({actualElectron:true,productionRecoveryRoute:true,completePromptExcludesUI:true,
    wrongPromptRefused:true,oldReceiptArchived:true,sameConversationKept:true,repeatedRetirementRefused:true,cookieKept:true,promptsSubmitted:sends},null,2));
 }finally{window.destroy();}
}
main().then(()=>app.quit()).catch(error=>{if(proof)fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error)}));app.exit(1);});
