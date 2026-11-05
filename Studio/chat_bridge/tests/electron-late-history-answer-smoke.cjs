"use strict";
const {app,BrowserWindow,session}=require('electron');
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {waitForAnswer}=require('../src/automation.cjs');
const proof=process.env.DOCURANDOM_LATE_HISTORY_PROOF;
app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'DocuRandomLateHistory-')));
app.on('window-all-closed',()=>{});
async function main(){
  await app.whenReady();const isolated=session.fromPartition('late-history-proof');
  const url='https://chatgpt.com/c/6acd08c9-49e4-83ec-b47f-0eda02c1b3a8';
  isolated.protocol.handle('https',()=>new Response('<main><div data-message-author-role="assistant" data-message-id="old-one"><div class="markdown">Prior answer</div></div><div data-message-author-role="assistant" data-message-id="late-old"><div class="markdown">Old history, wrong topic</div><button aria-label="Copy">Copy</button></div><div data-message-author-role="user" data-message-id="accepted-user">Current prompt</div></main>',{headers:{'Content-Type':'text/html'}}));
  const window=new BrowserWindow({show:false,width:900,height:700,webPreferences:{session:isolated,sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
  const partial=[];
  try{
    await window.loadURL(url);
    const pending=waitForAnswer(window,1,{submissionEvidence:{assistantCount:1,assistantKey:'data-message-id:old-one',submissionAccepted:true},
      persistedConversationUrl:url,requireExactConversation:true,pollIntervalMs:10,maxTimeoutMs:5000,
      evidenceOptions:{completionSettleMs:20},onPartial:text=>partial.push(text)});
    await new Promise(resolve=>setTimeout(resolve,250));
    assert.deepEqual(partial,[]);
    await window.webContents.executeJavaScript("document.querySelector('main').insertAdjacentHTML('beforeend','<div data-message-author-role=\"assistant\" data-message-id=\"current-answer\"><div class=\"markdown\">Current answer</div><button aria-label=\"Copy\">Copy</button></div>');void 0;");
    assert.equal(await pending,'Current answer');assert.ok(partial.every(text=>text==='Current answer'));
    fs.writeFileSync(proof,JSON.stringify({actualElectron:true,lateOlderAssistantCountAdvanced:true,
      oldTopicNeverEmitted:true,onlyAnswerAfterCurrentUserAccepted:true,promptsSubmitted:0,externalRequests:0},null,2));
  }finally{window.destroy();}
}
main().then(()=>app.quit()).catch(error=>{if(proof)fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error)}));app.exit(1);});
