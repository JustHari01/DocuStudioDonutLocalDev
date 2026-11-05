"use strict";
const assert=require("node:assert/strict");
const fs=require("node:fs");
const os=require("node:os");
const path=require("node:path");
const {app,BrowserWindow}=require("electron");
const {generatedImageSnapshot,waitForAnswer,chatTurnSnapshot}=require("../src/automation.cjs");
const {readExistingImage}=require("../src/existing-image-read.cjs");
const proof=process.env.DOCURANDOM_WORKSPACE_PROOF;
if(!proof)throw new Error("An isolated proof file is required");
app.setPath("userData",fs.mkdtempSync(path.join(os.tmpdir(),"DocuRandomStalledWorkspace-")));
app.commandLine.appendSwitch("disable-gpu-shader-disk-cache");
app.on("window-all-closed",()=>{});
async function main(){
  await app.whenReady();
  const w=new BrowserWindow({show:false,width:900,height:700,webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
  try{
    await w.loadURL("data:text/html;charset=utf-8,"+encodeURIComponent('<main></main>'));
    await w.webContents.executeJavaScript(`(async()=>{
      const c=document.createElement('canvas');c.width=1280;c.height=720;c.getContext('2d').fillRect(0,0,1280,720);
      const src=c.toDataURL('image/png');
      const preview='<div data-testid="generated-image-preview" style="display:none"><img alt="Generated image 1" src="'+src+'"></div>';
      document.querySelector('main').innerHTML=preview+'<div data-message-author-role="user">Create image'+preview+'</div>'+preview;
      await Promise.all([...document.images].map(img=>img.decode()));
    })()`);
    const image=await generatedImageSnapshot(w);
    assert.equal(image.candidateCount,1,"only the generated preview after the latest user is eligible");
    assert.equal(image.afterLatestUser,true);assert.ok(image.dataBase64);
    assert.equal((await readExistingImage(w)).status,"ready","completed images are readable without a composer");
    await w.webContents.executeJavaScript("document.querySelector('main').lastElementChild.remove()");
    assert.equal((await generatedImageSnapshot(w)).candidateCount,0,"hidden old images and user attachments are excluded");
    await w.loadURL("data:text/html;charset=utf-8,"+encodeURIComponent(`
      <div data-chatgpt-search-message-ids="user1"><div class="group/user-message">Begin</div></div>
      <section id="failure"><p>Message delivery timed out. Please try again.</p>
      <button onclick="window.retryClicks=(window.retryClicks||0)+1;document.querySelector('#failure').remove();document.body.insertAdjacentHTML('beforeend','<div data-message-author-role=assistant><div class=markdown>DONE</div><button aria-label=Copy>Copy</button></div>')">Retry</button></section>`));
    const baseline=await chatTurnSnapshot(w);
    const evidence={submissionAccepted:true,assistantCount:0,assistantKey:"",userCount:1};
    let reserved=0;
    const answer=await waitForAnswer(w,0,{timeoutMs:8000,maxTimeoutMs:10000,maxRecoveryReloads:0,maxPageRecoveryReloads:0,
      allowNoResponseEvidenceRecovery:false,expectedExactText:"DONE",exactSettleMs:20,pollIntervalMs:50,
      submissionEvidence:evidence,onStatus:stage=>{if(stage==='delivery-retry-reserved')reserved++;}});
    assert.equal(answer,"DONE");assert.equal(evidence.deliveryRetryCount,1);assert.equal(reserved,1);
    assert.equal(await w.webContents.executeJavaScript("window.retryClicks"),1);
    const recovered=await chatTurnSnapshot(w);
    assert.equal(recovered.userCount,baseline.userCount,"native Retry must not create an extra user prompt");
    await w.loadURL("data:text/html;charset=utf-8,"+encodeURIComponent('<main>Could not load this ChatGPT conversation<button>Retry</button></main>'));
    assert.equal((await chatTurnSnapshot(w)).conversationLoadFailed,true,"a provider loading error is not a generating turn");
    await w.webContents.executeJavaScript("document.body.innerHTML='<div data-message-author-role=user>Could not load this ChatGPT conversation</div>'");
    assert.equal((await chatTurnSnapshot(w)).conversationLoadFailed,false,"quoted failure text in a user message is not a loading failure");
    fs.writeFileSync(proof,JSON.stringify({hiddenGeneratedImageRecovered:true,oldImagesAndAttachmentsExcluded:true,nativeDeliveryRetryRecovered:true,retryReservationPersisted:true,noExtraPrompt:true}));
  }finally{w.destroy();}
}
main().then(()=>app.quit()).catch(error=>{fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error)}));app.exit(1);});
