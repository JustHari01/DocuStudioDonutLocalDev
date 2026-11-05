const {app,BrowserWindow,session}=require('electron');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict');
const src=process.env.DOCURANDOM_PROVIDER_UNCERTAINTY_SRC||path.join(__dirname,'../src');
const {chatTurnSnapshot,recoverInterruptedTurn,prepareWindowForBackground}=require(path.join(src,'automation.cjs'));
const {noteProviderDispatch,providerDispatchFields}=require(path.join(src,'submission-dispatch.cjs'));
const proof=process.argv[2];app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'DocuRandomProviderUncertain-')));app.on('window-all-closed',()=>{});
async function main(){
 await app.whenReady();const url='https://chatgpt.com/c/6ace8888-1234-83ec-a358-abcdef123456';
 const s=session.fromPartition('provider-uncertain');s.protocol.handle('https',()=>new Response('<main><div data-message-author-role="user" data-message-id="before-user">Previous prompt</div><div data-message-author-role="assistant" data-message-id="before-answer"><div class="markdown">Previous answer</div></div><form><div id="prompt-textarea" role="textbox" contenteditable="true" style="height:40px"></div><button data-testid="send-button" onclick="window.sends++">Send</button></form></main><script>window.sends=0</script>',{headers:{'Content-Type':'text/html'}}));
 await s.cookies.set({url,name:'fixture-login',value:'kept',httpOnly:true});
 const w=new BrowserWindow({show:false,width:900,height:700,webPreferences:{session:s,sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
 try{
  await w.loadURL(url);await prepareWindowForBackground(w);const snap=await chatTurnSnapshot(w);
  const receipt={sendAttempted:true,submissionAccepted:false,createdAt:Date.now()-1000,assistantCount:snap.assistantCount,userCount:snap.userCount,assistantKey:snap.assistants.at(-1).key,userKey:snap.users.at(-1).key};
  assert.equal((await recoverInterruptedTurn(w,{submissionEvidence:receipt,persistedConversationUrl:url})).safeToResend,true);
  const event={url:'https://chatgpt.com/backend-api/f/conversation',method:'POST',id:99,statusCode:200};
  noteProviderDispatch(receipt,event);noteProviderDispatch(receipt,{...event,error:'net::ERR_FAILED'});
  // Same DOM absence is no longer evidence of an unsent request. Exercise the
  // production pending-reader with the persisted receipt after a restart.
  const restored=JSON.parse(JSON.stringify({...receipt,...providerDispatchFields(receipt)}));
  await assert.rejects(recoverInterruptedTurn(w,{submissionEvidence:restored,persistedConversationUrl:url}),/CHATGPT_SUBMISSION_TRANSPORT_UNCERTAIN/);
  assert.equal(await w.webContents.executeJavaScript('window.sends'),0);
  assert.equal((await s.cookies.get({url,name:'fixture-login'}))[0].value,'kept');
  fs.writeFileSync(proof,JSON.stringify({actualElectron:true,productionPendingReader:true,installedAsarExercised:src.includes('app.asar'),sameDomAbsenceCannotClearProviderAttempt:true,receiptSurvivesRestart:true,promptsSubmitted:0,cookieKept:true,externalRequests:0},null,2));
 }finally{w.destroy();}
}
main().then(()=>app.quit()).catch(e=>{fs.writeFileSync(proof,JSON.stringify({failed:String(e.stack||e)}));app.exit(1);});
