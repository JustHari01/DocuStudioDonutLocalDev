"use strict";
const {app,BrowserWindow,session}=require('electron');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const src=process.env.DOCURANDOM_PRE_SUBMIT_TEST_SRC || path.join(__dirname,'../src');
const {submitPrompt,prepareWindowForBackground}=require(path.join(src,'automation.cjs'));
const {withConversationLoadRecovery}=require(path.join(src,'conversation-load-recovery.cjs'));
const proof=process.argv[2];
app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'DocuRandomPreSubmitRecovery-')));
app.on('window-all-closed',()=>{});
async function main(){
 await app.whenReady();const url='https://chatgpt.com/c/6ace0000-1234-83ec-a358-abcdef123456';
 const isolated=session.fromPartition('pre-submit-interruption');let freshReader=false,oldLoads=0;
 isolated.protocol.handle('https',()=>{
   if(!freshReader)oldLoads++;
   const status=freshReader?'':'<p>Connection interrupted. Waiting for the complete answer.</p>';
   const button=freshReader?`<button data-testid="send-button" aria-label="Send" onclick="window.sends++;document.querySelector('main').insertAdjacentHTML('beforeend','<div data-message-author-role=user data-message-id=new-user>Thumbnail fixture prompt</div><div data-message-author-role=assistant data-message-id=new-answer><div class=markdown>Fixture accepted</div><button aria-label=Copy>Copy</button></div>');">Send</button>`:'<button aria-label="Stop generating" data-testid="stop-button" onclick="window.stops++">Stop</button>';
   return new Response('<main><div data-message-author-role="user" data-message-id="old-user">Earlier completed prompt</div><div data-message-author-role="assistant" data-message-id="old-answer"><div class="markdown">Earlier answer</div><button aria-label="Copy">Copy</button></div>'+status+'<form><div id="prompt-textarea" contenteditable="true" role="textbox" style="height:45px"></div>'+button+'</form></main><script>window.sends=0;window.stops=0;</script>',{headers:{'Content-Type':'text/html;charset=utf-8'}});
 });
 await isolated.cookies.set({url,name:'fixture-login',value:'kept',httpOnly:true});
 const create=async()=>{const w=new BrowserWindow({show:false,width:900,height:700,webPreferences:{session:isolated,sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});await w.loadURL(url);await prepareWindowForBackground(w);return w;};
 const context={window:await create(),chatUrl:url,pendingSubmission:null};let reopened=0,oldSends=0,oldStops=0;
 try{
  await withConversationLoadRecovery(context,()=>submitPrompt(context.window,'Thumbnail fixture prompt',
    {onSubmitted:evidence=>{context.pendingSubmission=evidence;}}),{
      reopen:async()=>{reopened++;const old=context.window;oldSends=await old.webContents.executeJavaScript('window.sends');oldStops=await old.webContents.executeJavaScript('window.stops');freshReader=true;context.window=await create();old.destroy();},
      health:()=>({classification:'unknown'}),recoverAccepted:()=>assert.fail('Old reader never attempted Send'),
  });
  assert.equal(reopened,1);assert.ok(oldLoads>=2);assert.equal(oldSends,0);assert.equal(oldStops,0);
  assert.equal(await context.window.webContents.executeJavaScript('window.sends'),1);
  assert.equal(context.pendingSubmission.submissionAccepted,true);
  assert.equal((await isolated.cookies.get({url,name:'fixture-login'}))[0].value,'kept');
  fs.writeFileSync(proof,JSON.stringify({actualElectron:true,actualSubmitPrompt:true,installedAsarExercised:src.includes('app.asar'),
    normalReloadAttempted:true,failedIdleReaderReplacedOnce:true,sameConversationAndCookie:true,
    oldReaderSendClicks:oldSends,providerStopClicks:oldStops,trustedSendClicks:1,acceptedReceiptKept:true,externalRequests:0},null,2));
 }finally{if(context.window&&!context.window.isDestroyed())context.window.destroy();}
}
main().then(()=>app.quit()).catch(error=>{if(proof)fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error)}));app.exit(1);});
