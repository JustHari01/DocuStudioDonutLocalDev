"use strict";
const {app,BrowserWindow,session}=require('electron');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),vm=require('node:vm'),crypto=require('node:crypto');
const {chatTurnSnapshot,pageSnapshot,waitForComposer}=require('../src/automation.cjs');
const {homeConversationProof}=require('../src/home-conversation-proof.cjs');
const {isExactChatConversationUrl}=require('../src/navigation.cjs');
const proof=process.env.DOCURANDOM_HOME_RECOVERY_PROOF;
app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'DocuRandomHomeRecovery-')));
app.on('window-all-closed',()=>{});
async function main(){
 await app.whenReady();const isolated=session.fromPartition('home-recovery');
 const good='https://chatgpt.com/c/6acd08c9-49e4-83ec-b47f-0eda02c1b3a8';
 const bad='https://chatgpt.com/c/6acd0854-dc98-83ec-9ded-74fa78591da2';
 const prompt='Create the exact approved DaiTa8 thumbnail';
 let sends=0;
 isolated.protocol.handle('https',request=>new Response('<main>'+ (request.url==='https://chatgpt.com/'
   ? `<a href="${good}">Tạo thumbnail YouTube</a><a href="${bad}">Another chat</a>`
   : `<div data-message-author-role="user" data-message-id="first-user">${request.url===good?prompt:'Another prompt'}<button>Show more</button></div><div data-message-author-role="assistant"><div class="markdown">ĐÃ TẠO</div></div>`)
   +'<form><div id="prompt-textarea" contenteditable="true" role="textbox" style="height:50px"> </div><button data-testid="send-button" onclick="console.log(\'fixture-send\')">Send</button></form></main>',{headers:{'Content-Type':'text/html;charset=utf-8'}}));
 await isolated.cookies.set({url:good,name:'fixture-login',value:'kept',httpOnly:true});
 const allContexts=new Set();
 async function createConversation(url){
   const window=new BrowserWindow({show:false,width:900,height:700,webPreferences:{session:isolated,sandbox:true,nodeIntegration:false,contextIsolation:true,backgroundThrottling:false}});
   window.webContents.on('console-message',(_event,...args)=>{if(args.some(value=>value==='fixture-send'))sends++;});
   await window.loadURL(url);
   const context={window,chatUrl:url,busy:false};allContexts.add(context);return context;
 }
 const context=await createConversation('https://chatgpt.com/');
 const pending={submissionAccepted:true,userCount:0,assistantCount:0,outputMode:'generated_image'};
 context.pendingSubmission=pending;
 const identity='thread:home-fixture';
 const savedTurns=new Map([[identity,{chatUrl:context.chatUrl,pendingSubmission:pending}]]);
 const turnConversations=new Map([[identity,context]]);
 const body={thread_id:'home-fixture',url:bad,prompt_sha256:crypto.createHash('sha256').update(prompt).digest('hex')};
 const code=fs.readFileSync(path.join(__dirname,'../src/main.cjs'),'utf8');
 const start=code.indexOf('  if (request.method === "POST" && url.pathname === "/v1/session/recover-home-conversation")');
 const end=code.indexOf('  if (request.method === "POST" && url.pathname === "/v1/session/open-conversation")',start);
 let result,status;
 const scope={request:{method:'POST'},url:{pathname:'/v1/session/recover-home-conversation'},response:{},readJson:async()=>body,
   normalizeTurnIdentity:thread=>'thread:'+thread,savedTurns,turnConversations,turnControllers:new Map(),existingImageReaders:new Set(),
   allContexts,isExactChatConversationUrl,chatTurnSnapshot,pageSnapshot,homeConversationProof,waitForComposer,
   createConversation,ensureConversationWindow:async()=>{},Date,setTimeout,
   suspendConversationWindow:ctx=>ctx.window.destroy(),persistState:()=>{},
   saveTurnContext:(key,ctx)=>savedTurns.set(key,{chatUrl:ctx.chatUrl,pendingSubmission:ctx.pendingSubmission}),
   sendJson:(_res,httpStatus,value)=>{status=httpStatus;result=value;}};
 try{
  const run=()=>vm.runInNewContext('(async()=>{'+code.slice(start,end)+'})()',scope);
  await run();assert.equal(status,409);assert.equal(savedTurns.get(identity).chatUrl,'https://chatgpt.com/');
  assert.equal(savedTurns.get(identity).pendingSubmission,pending);
  body.url=good;await run();assert.equal(status,200);assert.equal(result.status,'rebound');
  assert.equal(savedTurns.get(identity).chatUrl,good);assert.equal(savedTurns.get(identity).pendingSubmission,pending);
  assert.equal((await isolated.cookies.get({url:good,name:'fixture-login'}))[0].value,'kept');
  fs.writeFileSync(proof,JSON.stringify({actualElectron:true,productionRecoveryRoute:true,
    wrongPromptRefused:true,receiptPreserved:true,exactFirstPromptBound:true,cookieKept:true,promptsSubmitted:sends,externalRequests:0},null,2));
 }finally{for(const ctx of allContexts)if(!ctx.window.isDestroyed())ctx.window.destroy();}
}
main().then(()=>app.quit()).catch(error=>{if(proof)fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error)}));app.exit(1);});
