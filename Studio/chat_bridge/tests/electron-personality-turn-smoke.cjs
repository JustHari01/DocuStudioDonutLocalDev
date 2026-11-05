"use strict";
const {app,BrowserWindow,session}=require('electron');
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const {chatTurnSnapshot,pageSnapshot,recoverInterruptedTurn}=require('../src/automation.cjs');
const {queuedTurnProof}=require('../src/queued-turn-proof.cjs');
const {receiptFromCheckpoint}=require('../src/checkpoint-receipt.cjs');
const {masterPromptCaption}=require('../src/prompt-transport.cjs');
const vm=require('node:vm');
const {baselineUserHash}=require('../src/queued-turn-proof.cjs');
const {checkpointReceiptProof}=require('../src/checkpoint-receipt.cjs');
const proof=process.argv[2];app.setPath('userData',path.join(path.dirname(proof),'personality-fixture-profile'));
app.on('window-all-closed',()=>{});
async function main(){
  await app.whenReady();
  const url='https://chatgpt.com/c/6acb3b2e-1a24-83ec-b788-16ce5cf15033';
  const raw='日本の技術を検証します。'.repeat(5500);
  const hash=value=>crypto.createHash('sha256').update(value).digest('hex');
  const normalized=value=>value.normalize('NFC').replace(/\s+/g,' ').trim();
  const attachment={sha256:hash(raw),char_length:raw.length};
  const file='DocuRandom-Master-Prompt-'+attachment.sha256.slice(0,12)+'-a9ff467cf6.txt';
  const caption=masterPromptCaption(file,raw.length,attachment.sha256);
  const answer='ĐÃ NẠP MASTER PROMPT.';
  const isolated=session.fromPartition('personality-turn-regression');
  isolated.protocol.handle('https',()=>new Response(`<main>
    <div data-chatgpt-search-message-ids="u1"><div class="group/user-message"><button>${file}</button>Document<div class="whitespace-pre-wrap" dir="auto">${caption}</div></div></div>
    <div data-chatgpt-selection-message-id="a1"><div data-markdown-text-style>${answer}</div><button aria-label="Copy">Copy</button></div>
    <div aria-label="Do you like this personality?">Do you like this personality?<button>Yes</button><button>No</button></div>
    <form><div role="textbox" contenteditable="true" style="height:30px">draft</div><button data-testid="send-button" onclick="window.sends++">Send</button></form>
    </main><script>window.sends=0</script>`,{headers:{'Content-Type':'text/html; charset=utf-8'}}));
  const w=new BrowserWindow({show:false,width:1000,height:720,webPreferences:{session:isolated,sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
  try{
    await w.loadURL(url);
    const snap=await chatTurnSnapshot(w),page=await pageSnapshot(w);
    assert.equal(snap.userCount,1);assert.equal(snap.assistantCount,1);assert.equal(snap.latestAssistantAfterUser,true);
    assert.equal(snap.latestUserTextSha256,hash(normalized(caption)));
    const options={chatUrl:url,userSha256:hash(normalized(raw)),assistantSha256:hash(normalized(answer)),
      pendingPromptSha256:hash('Next requested marker'),userAttachment:attachment};
    assert.ok(queuedTurnProof(snap,page,options));
    assert.equal(await recoverInterruptedTurn(w,{submissionEvidence:receiptFromCheckpoint(snap),persistedConversationUrl:url}),answer);
    const code=fs.readFileSync(path.join(__dirname,'../src/main.cjs'),'utf8');
    const start=code.indexOf('  if (request.method === "POST" && url.pathname === "/v1/turns/recover")');
    const end=code.indexOf('  if (request.method === "POST" && url.pathname === "/v1/turns/cancel")',start);
    const identity='thread:master-caption-fixture',context={window:w,chatUrl:url,busy:false};
    let routeResult;
    const scope={request:{method:'POST'},url:{pathname:'/v1/turns/recover'},response:{},
      readJson:async()=>({thread_id:'master-caption-fixture',pending_prompt_sha256:options.pendingPromptSha256,
        completed_turn_baseline:{user_sha256:options.userSha256,assistant_sha256:options.assistantSha256,user_attachment:attachment}}),
      turnConversations:new Map([[identity,context]]),savedTurns:new Map([[identity,{chatUrl:url}]]),
      existingImageReaders:new Set(),turnControllers:new Map(),chatTurnSnapshot,pageSnapshot,queuedTurnProof,baselineUserHash,
      checkpointReceiptProof,receiptFromCheckpoint,Date,setTimeout,ensureConversationWindow:async()=>{},
      recoverConversationLoad:async(_context,operation)=>operation(),
      sendJson:(_response,status,value)=>{assert.equal(status,200);routeResult=value;}};
    const runRoute=()=>vm.runInNewContext('(async()=>{'+code.slice(start,end)+'})()',scope);
    await runRoute();assert.equal(routeResult.proof,'queued-after-completed-turn');assert.equal(routeResult.safe_to_resend,true);
    await w.webContents.executeJavaScript(`document.querySelector('main').insertAdjacentHTML('beforeend','<div data-message-author-role="user" data-message-id="real-u2">Do you like this personality?</div>');void 0;`);
    const after=await chatTurnSnapshot(w);
    assert.equal(after.userCount,2);assert.equal(after.latestAssistantAfterUser,false);
    assert.equal(queuedTurnProof(after,await pageSnapshot(w),options),null);
    await runRoute();assert.equal(routeResult.safe_to_resend,undefined);
    assert.equal(await w.webContents.executeJavaScript('window.sends'),0);
    fs.writeFileSync(proof,JSON.stringify({personalityChromeExcluded:true,realUserQuotePreserved:true,
      exactMasterCaptionBound:true,productionRecoveryRoute:true,existingReplyRecovered:true,newUserBlocksOldReply:true,promptsSubmitted:0,externalRequests:0},null,2));
  }finally{w.destroy();}
}
main().then(()=>app.quit()).catch(error=>{fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error)}));app.exit(1);});
