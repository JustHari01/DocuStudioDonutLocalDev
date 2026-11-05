"use strict";
const {app,BrowserWindow,session}=require('electron');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict');
const src=process.env.DOCURANDOM_SUBMISSION_HOLD_TEST_SRC || path.join(__dirname,'../src');
const {submitPrompt,prepareWindowForBackground}=require(path.join(src,'automation.cjs'));
const {providerHold}=require(path.join(src,'stalled-image-recovery.cjs'));
const proof=process.argv[2];
app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'DocuRandomSubmissionHold-')));
app.on('window-all-closed',()=>{});
async function main(){
 await app.whenReady();const cases=[];
 for(const mode of ['before-dispatch','after-dispatch']){
  const url='https://chatgpt.com/c/6ace4444-1234-83ec-a358-abcdef123456';
  const isolated=session.fromPartition('submission-hold-'+mode);
  isolated.protocol.handle('https',()=>new Response('<main><div data-message-author-role="user" data-message-id="old">Earlier completed prompt</div><div data-message-author-role="assistant" data-message-id="old-answer"><div class="markdown">Earlier answer</div><button aria-label="Copy">Copy</button></div><form><div id="prompt-textarea" contenteditable="true" role="textbox" style="height:40px"></div><button data-testid="send-button" aria-label="Send" onclick="window.sends++">Send</button></form></main><script>window.sends=0;</script>',{headers:{'Content-Type':'text/html;charset=utf-8'}}));
  await isolated.cookies.set({url,name:'fixture-login',value:'kept',httpOnly:true});
  const window=new BrowserWindow({show:false,width:900,height:700,webPreferences:{session:isolated,sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
  await window.loadURL(url);await prepareWindowForBackground(window);
  let checks=0,receipt,checkpointed=false,reloads=0;
  window.webContents.on('did-start-navigation',()=>{reloads++;});
  try{
   await assert.rejects(submitPrompt(window,'Fixture new image prompt',{
    captureGeneratedImage:true,outputMode:'generated_image',submissionTimeoutMs:500,
    checkProviderAvailability:()=>{
     checks++;providerHold({classification:mode==='before-dispatch'||checks>1?'rate-limited':'unknown',blocking:{retryAfterSeconds:90}});
    },
    onSubmitted:evidence=>{receipt=evidence;},
    onStatus:stage=>{if(stage==='submission-checkpointed')checkpointed=true;},
   }),/CHATGPT_RATE_LIMITED: HTTP 429; retry-after-seconds=90/);
   const sends=await window.webContents.executeJavaScript('window.sends');
   assert.equal(sends,mode==='before-dispatch'?0:1);
   assert.equal(reloads,0);assert.equal(checkpointed,mode==='after-dispatch');
   if(mode==='after-dispatch'){assert.equal(receipt.sendAttempted,true);assert.equal(receipt.submissionAccepted,false);}
   else assert.equal(receipt,undefined);
   assert.equal((await isolated.cookies.get({url,name:'fixture-login'}))[0].value,'kept');
   cases.push({mode,sendClicks:sends,receiptKept:!!receipt,reloads,cookieKept:true});
  }finally{window.destroy();}
 }
 fs.writeFileSync(proof,JSON.stringify({actualElectron:true,actualSubmitPrompt:true,installedAsarExercised:src.includes('app.asar'),privateFixtures:true,externalRequests:0,cases},null,2));
}
main().then(()=>app.quit()).catch(error=>{fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error)}));app.exit(1);});
