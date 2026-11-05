"use strict";
const {app,BrowserWindow,session}=require('electron');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),vm=require('node:vm'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const src=process.env.DOCURANDOM_DIAGNOSTIC_TEST_SRC || path.join(__dirname,'../src');
const {chatTurnSnapshot,pageSnapshot,composerValue,prepareWindowForBackground}=require(path.join(src,'automation.cjs'));
const code=fs.readFileSync(path.join(src,'main.cjs'),'utf8');
const start=code.indexOf('  if (request.method === "POST" && url.pathname === "/v1/session/diagnostic")');
const end=code.indexOf('  if (request.method === "POST" && url.pathname === "/v1/session/existing-image")',start);
assert(start>=0 && end>start);
const proof=process.argv[2];
app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'DocuRandomDiagnostic-')));
app.on('window-all-closed',()=>{});
async function main(){
 await app.whenReady();
 const url='https://chatgpt.com/c/6ace7777-1234-83ec-a358-abcdef123456';
 const isolated=session.fromPartition('diagnostic-route');
 isolated.protocol.handle('https',()=>new Response('<main><div data-message-author-role="user" data-message-id="script-user">A script request</div><div data-message-author-role="assistant" data-message-id="script-answer"><div class="markdown">Script complete</div></div><form><div id="prompt-textarea" role="textbox" contenteditable="true" style="height:50px">Owned draft</div><button data-testid="send-button" onclick="window.sends++">Send</button></form></main><script>window.sends=0</script>',{headers:{'Content-Type':'text/html;charset=utf-8'}}));
 await isolated.cookies.set({url,name:'fixture-login',value:'kept',httpOnly:true});
 const window=new BrowserWindow({show:false,width:900,height:700,webPreferences:{session:isolated,sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
 try{
  await window.loadURL(url);await prepareWindowForBackground(window);
  const identity='thread:diagnostic-fixture',context={window,chatUrl:url,pendingSubmission:null,busy:false};
  let body,status,result;
  const scope={request:{method:'POST'},url:{pathname:'/v1/session/diagnostic'},response:{},readJson:async()=>body,
   normalizeTurnIdentity:v=>v?'thread:'+v:null,turnConversations:new Map([[identity,context]]),savedTurns:new Map([[identity,{chatUrl:url}]]),turnControllers:new Map(),busyCount:0,
   ensureConversationWindow:async()=>{},contextWindowUsable:()=>true,chatTurnSnapshot,pageSnapshot,composerValue,
   retireFailedRenderer:()=>false,saveTurnContext:()=>{},persistState:()=>{},suspendFailedConversationReader:()=>{},
   connectionHealth:()=>({classification:'healthy'}),sendJson:(_res,s,v)=>{status=s;result=v}};
  const run=()=>vm.runInNewContext('(async()=>{'+code.slice(start,end)+'})()',scope);
  body={thread_id:'diagnostic-fixture'};await run();assert.equal(status,200);assert.equal(result.snapshot.userCount,1);assert.equal(result.composer_snapshot,undefined);
  body={thread_id:'diagnostic-fixture',include_composer:true};await run();assert.equal(status,200);
  assert.equal(result.composer_snapshot.actualLength,11);
  assert.equal(result.composer_snapshot.sha256,crypto.createHash('sha256').update('Owned draft').digest('hex'));
  assert.equal(result.composer_snapshot.value,null,'diagnosis must not return draft plaintext');
  assert.equal(result.composer_snapshot.url,url);assert.equal(result.composer_snapshot.visibleComposers,1);
  context.busy=true;await run();assert.equal(status,200);assert.equal(result.diagnostic_deferred,'active-turn-owner');assert.equal(result.snapshot,null);assert.equal(result.composer_snapshot,undefined);
  context.busy=false;body={};await run();assert.equal(status,400);
  assert.equal(await window.webContents.executeJavaScript('window.sends'),0);
  assert.equal(await window.webContents.executeJavaScript('document.querySelector("#prompt-textarea").innerText'),'Owned draft');
  assert.equal((await isolated.cookies.get({url,name:'fixture-login'}))[0].value,'kept');
  fs.writeFileSync(proof,JSON.stringify({actualElectron:true,productionDiagnosticRoute:true,installedAsarExercised:src.includes('app.asar'),defaultAndComposerDiagnostics:true,busyOwnerDeferred:true,invalidIdentityRejected:true,draftUnchanged:true,draftPlaintextExcluded:true,cookieKept:true,promptsSubmitted:0,externalRequests:0},null,2));
 }finally{window.destroy();}
}
main().then(()=>app.quit()).catch(error=>{fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error)}));app.exit(1);});
