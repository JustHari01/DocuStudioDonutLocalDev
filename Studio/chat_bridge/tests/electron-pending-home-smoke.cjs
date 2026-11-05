"use strict";
const {app,BrowserWindow,session}=require('electron');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {pageSnapshot,waitForComposer,chooseModel,submitPrompt,withTrustedWindowStage}=require('../src/automation.cjs');
const proof=process.env.DOCURANDOM_PENDING_HOME_PROOF;
app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'DocuRandomPendingHome-')));
app.on('window-all-closed',()=>{});
async function main(){
  await app.whenReady();const isolated=session.fromPartition('pending-home-proof');
  const url='https://chatgpt.com/';let requests=0;
  isolated.protocol.handle('https',()=>{requests++;return new Response('<main><textarea id="pending-home-input" aria-label="Ask ChatGPT">Loading</textarea><button aria-label="Open profile menu">Loading profile</button></main>',{headers:{'Content-Type':'text/html'}});});
  await isolated.cookies.set({url,name:'fixture-login',value:'kept',httpOnly:true});
  const window=new BrowserWindow({show:false,width:900,height:700,webPreferences:{session:isolated,sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
  try{
    await window.loadURL(url);
    const loading=await pageSnapshot(window);
    assert.equal(loading.ready,false);assert.equal(loading.authenticated,false);assert.equal(loading.visibleComposers,0);
    await assert.rejects(waitForComposer(window,{timeoutMs:100,maxPageReloads:0}));
    assert.equal(await window.webContents.executeJavaScript('document.querySelector("textarea").value'),'Loading');
    await window.webContents.executeJavaScript(`document.body.innerHTML='<main><form><div id="prompt-textarea" role="textbox" contenteditable="true" style="min-height:80px;width:600px"></div><button type="button" data-testid="send-button">Send</button><button type="button" aria-label="Select ChatGPT model">High</button></form></main>';window.fixtureSends=0;document.querySelector('[data-testid="send-button"]').onclick=()=>{window.fixtureSends++;const user=document.createElement('div');user.setAttribute('data-message-author-role','user');user.textContent=document.querySelector('#prompt-textarea').textContent;document.querySelector('main').append(user);};void 0;`);
    const ready=await waitForComposer(window,{timeoutMs:1000});assert.equal(ready.ready,true);
    await chooseModel(window,'chatgpt-web/auto');
    await withTrustedWindowStage(window,undefined,()=>submitPrompt(window,'Fixture single accepted request'));
    assert.equal(await window.webContents.executeJavaScript('window.fixtureSends'),1);
    assert.equal(await window.webContents.executeJavaScript('document.querySelector("[data-message-author-role=user]").textContent'),'Fixture single accepted request');
    assert.equal((await isolated.cookies.get({url,name:'fixture-login'}))[0].value,'kept');
    fs.writeFileSync(proof,JSON.stringify({actualElectron:true,pendingHomeNeverReady:true,loadingDraftUntouched:true,
      hydratedComposerAcceptedOnce:true,cookiePreserved:true,modelHighPreserved:true,fixtureRequests:requests,externalRequests:0},null,2));
  }finally{window.destroy();}
}
main().then(()=>app.quit()).catch(error=>{if(proof)fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error)}));app.exit(1);});
