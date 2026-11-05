"use strict";
const {app,BrowserWindow,session}=require('electron');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const src=process.env.DOCURANDOM_SUBMISSION_BASELINE_TEST_SRC||path.join(__dirname,'../src');
const {submitPrompt,prepareWindowForBackground}=require(path.join(src,'automation.cjs'));
const proof=process.argv[2],prompt='[DOCURANDOM_STAGE=thumbnail] Create one original owned thumbnail.';
app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'DocuRandomSubmissionBaseline-')));
app.on('window-all-closed',()=>{});
async function main(){
 await app.whenReady();const cases=[];
 for(const mode of ['noop','accepted','reflow']){
  const url='https://chatgpt.com/c/6ace6666-1234-83ec-a358-abcdef123456';
  const isolated=session.fromPartition('submission-baseline-'+mode);let sends=0,feedback=0;
  isolated.protocol.handle('https',request=>{
   if(request.url.endsWith('/fixture-send')){sends++;return new Response('{}');}
   if(request.url.endsWith('/fixture-feedback')){feedback++;return new Response('{}');}
   return new Response(`<main></main><form onsubmit="return false"><button type="button" aria-label="Send feedback" onclick="fetch('/fixture-feedback')">Feedback</button><div id="prompt-textarea" role="textbox" contenteditable="true" style="height:45px"></div><button type="button" data-testid="composer-submit-button" aria-label="Send" onclick="commit()">Send</button></form><script>
    const prompt=${JSON.stringify(prompt)};let hydrated=false;
    function hydrate(){if(hydrated)return;hydrated=true;const u=document.createElement('div');u.setAttribute('data-message-author-role','user');u.setAttribute('data-message-id','rehydrated-user');u.textContent=prompt;const a=document.createElement('div');a.setAttribute('data-message-author-role','assistant');a.setAttribute('data-message-id','rehydrated-answer');a.innerHTML='<div class="markdown">Earlier completed response</div>';document.querySelector('main').append(u,a);}
    document.querySelector('#prompt-textarea').addEventListener('input',hydrate);
    ${mode==='reflow'?`const originalDigest=crypto.subtle.digest.bind(crypto.subtle);window.moved=false;crypto.subtle.digest=(...args)=>{if(!window.moved && document.activeElement.matches('[data-testid="composer-submit-button"]')){window.moved=true;document.activeElement.style.marginLeft='450px';}return originalDigest(...args);};`:''}
    function commit(){fetch('/fixture-send');${mode!=='noop'?`document.querySelector('#prompt-textarea').textContent='';const u=document.createElement('div');u.setAttribute('data-message-author-role','user');u.setAttribute('data-message-id','new-image-user');u.textContent=prompt;document.querySelector('main').append(u);`:''}}
   </script>`,{headers:{'Content-Type':'text/html;charset=utf-8'}});
  });
  await isolated.cookies.set({url,name:'fixture-login',value:'kept',httpOnly:true});
  const window=new BrowserWindow({show:false,width:900,height:700,webPreferences:{session:isolated,sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
  let receipt;
  try{
   await window.loadURL(url);await prepareWindowForBackground(window);
   const operation=()=>submitPrompt(window,prompt,{captureGeneratedImage:true,outputMode:'generated_image',submissionTimeoutMs:350,onSubmitted:value=>{receipt=value;}});
   if(mode==='noop')await assert.rejects(operation(),/chưa xác nhận/);else await operation();
   assert.equal(feedback,0);assert.equal(sends,1);
   assert.equal(receipt.userKey,'data-message-id:rehydrated-user');assert.equal(receipt.userCount,1);
   assert.equal(receipt.submissionAccepted,mode!=='noop');
   if(mode!=='noop')assert.equal(receipt.acceptedUserKey,'data-message-id:new-image-user');
   if(mode==='reflow')assert.equal(await window.webContents.executeJavaScript('window.moved'),true);
   assert.equal((await isolated.cookies.get({url,name:'fixture-login'}))[0].value,'kept');
   cases.push({mode,sendClicks:sends,feedbackClicks:feedback,baselineHydratedUserCount:receipt.userCount,accepted:receipt.submissionAccepted,cookieKept:true});
  }finally{window.destroy();}
 }
 fs.writeFileSync(proof,JSON.stringify({actualElectron:true,actualSubmitPrompt:true,installedAsarExercised:src.includes('app.asar'),privateFixtures:true,externalRequests:0,cases},null,2));
}
main().then(()=>app.quit()).catch(error=>{fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error)}));app.exit(1);});
