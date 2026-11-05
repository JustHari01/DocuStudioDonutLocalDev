"use strict";
const {app,BrowserWindow,session}=require('electron');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),vm=require('node:vm'),crypto=require('node:crypto');
const {chatTurnSnapshot,pageSnapshot,prepareWindowForBackground}=require('../src/automation.cjs');
const {queuedTurnProof,baselineUserHash,completedBaselineProof}=require('../src/queued-turn-proof.cjs');
const {checkpointReceiptProof}=require('../src/checkpoint-receipt.cjs');
const proof=process.env.DOCURANDOM_QUEUED_PROOF;
const hash=text=>crypto.createHash('sha256').update(text.normalize('NFC').replace(/\s+/g,' ').trim()).digest('hex');
app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'DocuRandomQueuedProof-')));
app.on('window-all-closed',()=>{});
async function main(){
  await app.whenReady();const isolated=session.fromPartition('queued-proof');
  const url='https://chatgpt.com/c/6acd08c9-49e4-83ec-b47f-0eda02c1b3a8';
  isolated.protocol.handle('https',()=>new Response('<main><div data-message-author-role="user" data-message-id="last-user">Confirm checklist</div><div data-message-author-role="assistant" data-message-id="last-assistant"><div class="markdown">ĐÃ XONG</div></div><button aria-label="Copy">Copy</button><form><div id="prompt-textarea" contenteditable="true" role="textbox" style="height:50px"> </div><button data-testid="send-button" onclick="window.sends++">Send</button></form></main><script>window.sends=0</script>',{headers:{'Content-Type':'text/html;charset=utf-8'}}));
  await isolated.cookies.set({url,name:'fixture-login',value:'kept',httpOnly:true});
  const window=new BrowserWindow({show:false,width:900,height:700,webPreferences:{session:isolated,sandbox:true,nodeIntegration:false,contextIsolation:true,backgroundThrottling:false}});
  try{
    await window.loadURL(url);
    await prepareWindowForBackground(window);
    const snapshot=await chatTurnSnapshot(window);
    assert.equal(snapshot.latestUserTextSha256,hash('Confirm checklist'));
    assert.equal(snapshot.latestAssistantTextSha256,hash('ĐÃ XONG'));
    const identity='thread:queued-fixture';const context={window,chatUrl:url,pendingSubmission:null,pendingAttachment:null,busy:false};
    const body={thread_id:'queued-fixture',pending_prompt_sha256:hash('BẮT ĐẦU'),
      completed_turn_baseline:{user_sha256:hash('Confirm checklist'),assistant_sha256:hash('ĐÃ XONG')}};
    const code=fs.readFileSync(path.join(__dirname,'../src/main.cjs'),'utf8');
    const start=code.indexOf('  if (request.method === "POST" && url.pathname === "/v1/turns/recover")');
    const end=code.indexOf('  if (request.method === "POST" && url.pathname === "/v1/turns/cancel")',start);
    let response;
    const scope={request:{method:'POST'},url:{pathname:'/v1/turns/recover'},response:{},readJson:async()=>body,
      existingImageReaders:new Set(),turnControllers:new Map(),turnConversations:new Map([[identity,context]]),
      savedTurns:new Map([[identity,{chatUrl:url,pendingSubmission:null}]]),Date,setTimeout,
      ensureConversationWindow:async()=>{},checkpointReceiptProof,queuedTurnProof,baselineUserHash,completedBaselineProof,crypto,chatTurnSnapshot,pageSnapshot,
      recoverConversationLoad:async(_context,operation)=>operation(),
      sendJson:(_res,status,value)=>{assert.equal(status,200);response=value;}};
    await vm.runInNewContext('(async()=>{'+code.slice(start,end)+'})()',scope);
    assert.equal(response.proof,'queued-after-completed-turn');assert.equal(response.safe_to_resend,true);
    assert.equal(context.pendingSubmission,null);assert.equal(context.busy,false);
    assert.equal(await window.webContents.executeJavaScript('window.sends'),0);
    body.pending_prompt_sha256=body.completed_turn_baseline.user_sha256;
    await vm.runInNewContext('(async()=>{'+code.slice(start,end)+'})()',scope);
    assert.equal(response.safe_to_resend,undefined);
    body.allow_missing_content_repair=true;
    await vm.runInNewContext('(async()=>{'+code.slice(start,end)+'})()',scope);
    assert.equal(response.status,'recovery-baseline');assert.equal(response.safe_to_request_missing,true);
    assert.equal(response.safe_to_resend,false);assert.equal(context.pendingSubmission,null);
    context.pendingAttachment={fixture:'manual-unsubmitted-attachment'};
    await vm.runInNewContext('(async()=>{'+code.slice(start,end)+'})()',scope);
    assert.equal(response.safe_to_request_missing,undefined);context.pendingAttachment=null;
    assert.equal(await window.webContents.executeJavaScript('window.sends'),0);
    assert.equal((await isolated.cookies.get({url,name:'fixture-login'}))[0].value,'kept');
    fs.writeFileSync(proof,JSON.stringify({actualElectron:true,productionRecoveryRoute:true,
      fullBaselineHashesVerified:true,queuedDifferentPromptProvedUnsent:true,sameRepeatedPromptRemainsLocked:true,
      idleBaselineAllowsOnlyDistinctRepair:true,pendingAttachmentBlocksRepair:true,
      cookieKept:true,promptsSubmitted:0,externalRequests:0},null,2));
  }finally{window.destroy();}
}
main().then(()=>app.quit()).catch(error=>{if(proof)fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error)}));app.exit(1);});
