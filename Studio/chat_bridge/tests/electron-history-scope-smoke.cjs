"use strict";
const {app,BrowserWindow,session}=require('electron');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {ConversationHealth}=require('../src/conversation-health.cjs');
const {providerHold}=require('../src/stalled-image-recovery.cjs');
const {chatTurnSnapshot,pageSnapshot}=require('../src/automation.cjs');
app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'DocuRandomHistoryScope-')));
app.on('window-all-closed',()=>{});
const out=process.argv[2];
async function main(){
 await app.whenReady();
 const cid='6ad1dfd2-e594-83ec-9e92-d807176f1231',url='https://chatgpt.com/c/'+cid;
 const local=session.fromPartition('history-scope-fixture'),health=new ConversationHealth();
 local.webRequest.onCompleted(details=>health.record(details));
 local.protocol.handle('https',request=>{
  const address=new URL(request.url);
  if(address.pathname.startsWith('/backend-api/'))return new Response('{}',{status:address.pathname==='/backend-api/conversation/'+cid?200:429,headers:{'Content-Type':'application/json'}});
  return new Response(`<main><div data-message-author-role="user" data-message-id="u">Request</div><div data-message-author-role="assistant" data-message-id="a"><div class="markdown">Finished response</div><button aria-label="Copy">Copy</button></div><form><div contenteditable="true" role="textbox" aria-label="Ask ChatGPT" style="height:30px"></div></form></main>`,{headers:{'Content-Type':'text/html'}});
 });
 const window=new BrowserWindow({show:false,width:900,height:700,webPreferences:{session:local,sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
 try{
  await window.loadURL(url);
  await window.webContents.executeJavaScript(`Promise.all([fetch('/backend-api/conversation/${cid}'),fetch('/backend-api/conversations?limit=28')]).then(()=>true)`);
  await new Promise(resolve=>setTimeout(resolve,100));
  const facts=health.snapshot(url,window.webContents.id),snap=await chatTurnSnapshot(window),page=await pageSnapshot(window);
  assert.equal(facts.classification,'loaded');assert.equal(facts.history_throttle.statusCode,429);
  assert.equal(page.ready,true);assert.ok(snap.completionActionCount>0);assert.doesNotThrow(()=>providerHold(facts));
  await window.webContents.executeJavaScript(`fetch('/backend-api/f/conversation',{method:'POST'}).then(()=>true)`);
  await new Promise(resolve=>setTimeout(resolve,100));
  assert.throws(()=>providerHold(health.snapshot(url,window.webContents.id)),/HTTP 429/);
  if(out)fs.writeFileSync(out,JSON.stringify({healthyConversationPreserved:true,sidebarThrottleScoped:true,actualSubmissionThrottleEnforced:true,externalRequests:0},null,2));
 }finally{window.destroy();}
}
main().then(()=>app.quit()).catch(error=>{if(out)fs.writeFileSync(out,JSON.stringify({failed:String(error.stack||error)}));app.exit(1);});
