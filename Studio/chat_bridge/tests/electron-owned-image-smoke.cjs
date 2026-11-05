"use strict";
const {app,BrowserWindow,session}=require('electron');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),crypto=require('node:crypto');
const src=process.env.DOCURANDOM_OWNED_IMAGE_TEST_SRC || path.join(__dirname,'../src');
const {generatedImageSnapshot,chatTurnSnapshot,prepareWindowForBackground}=require(path.join(src,'automation.cjs'));
const {existingImageResult}=require(path.join(src,'image-retry.cjs'));
const {readHistoricalImage}=require(path.join(src,'historical-image-read.cjs'));
const proof=process.argv[2],prompt='[DOCURANDOM_STAGE=thumbnail] Create the owned red thumbnail.';
const hash=crypto.createHash('sha256').update(prompt).digest('hex');
app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'DocuRandomOwnedImage-')));
app.on('window-all-closed',()=>{});
async function main(){
 await app.whenReady();const url='https://chatgpt.com/c/6ace5555-1234-83ec-a358-abcdef123456';
 const isolated=session.fromPartition('owned-image');isolated.protocol.handle('https',()=>new Response('<main></main>',{headers:{'Content-Type':'text/html;charset=utf-8'}}));
 await isolated.cookies.set({url,name:'fixture-login',value:'kept',httpOnly:true});
 const window=new BrowserWindow({show:false,width:900,height:700,webPreferences:{session:isolated,sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
 try{
  await window.loadURL(url);await prepareWindowForBackground(window);
  const red=await window.webContents.executeJavaScript(`(async()=>{
   const c=document.createElement('canvas');c.width=1280;c.height=720;const g=c.getContext('2d');
   g.fillStyle='red';g.fillRect(0,0,1280,720);const red=c.toDataURL('image/png');
   g.fillStyle='blue';g.fillRect(0,0,1280,720);const blue=c.toDataURL('image/png');
   document.querySelector('main').innerHTML='<div data-message-author-role="user" data-message-id="owned-user"><div data-user-message-bubble><div class="whitespace-pre-wrap" dir="auto">'+${JSON.stringify(prompt)}+'</div></div><span>… Show more</span></div>'
    +'<div data-message-author-role="assistant" data-message-id="owned-image"><img width="320" height="180" src="'+red+'"></div>'
    +'<div data-message-author-role="user" data-message-id="other-user">Another video request <img width="320" height="180" src="'+blue+'"></div>'
    +'<div data-message-author-role="assistant" data-message-id="other-image"><img width="320" height="180" src="'+blue+'"></div>'
    +'<div data-message-author-role="user" data-message-id="confirmation-user">Did you create it?</div>'
    +'<div data-message-author-role="assistant" data-message-id="confirmation"><div class="markdown">KHÔNG TẠO ĐƯỢC: Image not visible</div></div>';
   await Promise.all([...document.images].map(image=>image.decode()));return red.split(',')[1];
  })()`);
  let snapshot=await chatTurnSnapshot(window),image=await generatedImageSnapshot(window);
  assert.equal(existingImageResult(snapshot,image).status,'no-image');
  image=await generatedImageSnapshot(window,{expectedUserPromptSha256:hash});
  assert.equal(image.candidateCount,1);assert.equal(image.dataBase64,red);assert.equal(image.afterLatestUser,false);
  assert.equal(existingImageResult(snapshot,image,{expectedUserPromptSha256:hash}).status,'ready');
  assert.equal(existingImageResult(snapshot,image,{expectedUserPromptSha256:'f'.repeat(64)}).status,'pending');
  await window.webContents.executeJavaScript(`const node=document.createElement('div');node.setAttribute('data-message-author-role','user');node.setAttribute('data-message-id','new-duplicate');node.textContent=${JSON.stringify(prompt)};document.querySelector('main').append(node);void 0;`);
  image=await generatedImageSnapshot(window,{expectedUserPromptSha256:hash});
  assert.equal(image.candidateCount,0,'a newer identical request must not reuse an earlier result');
  await window.webContents.executeJavaScript(`(()=>{const node=document.createElement('div');node.setAttribute('data-message-author-role','assistant');node.setAttribute('data-message-id','new-duplicate-failure');node.textContent='KHÔNG TẠO ĐƯỢC: Image generation failed';document.querySelector('main').append(node);})();`);
  image=await generatedImageSnapshot(window,{expectedUserPromptSha256:hash,allowEarlierMatchingPrompts:true});
  assert.equal(image.dataBase64,red,'explicit historical reader finds the earlier exact prompt, excluding unrelated blue outputs');
  const historical=await readHistoricalImage(window,{expectedUserPromptSha256:hash,pollMs:10});
  assert.equal(historical.status,'ready');assert.equal(historical.generated_image.data_base64,red);
  assert.equal(historical.history_scan.promptsSubmitted,0);
  await window.webContents.executeJavaScript(`(()=>{const user=document.querySelector('[data-message-id="owned-user"]');const content=user.querySelector('.whitespace-pre-wrap');content.textContent='[DOCURANDOM_STAGE=thumbnail] Create...';const button=document.createElement('button');button.textContent='Show more';button.onclick=()=>{content.textContent=${JSON.stringify(prompt)};button.remove();};user.append(button);return true;})()`);
  const expandedHistory=await readHistoricalImage(window,{expectedUserPromptSha256:hash,pollMs:10});
  assert.equal(expandedHistory.status,'ready','collapsed history text must be expanded before matching the exact prompt');
  assert.equal(expandedHistory.generated_image.data_base64,red);
  assert.equal((await isolated.cookies.get({url,name:'fixture-login'}))[0].value,'kept');
  fs.writeFileSync(proof,JSON.stringify({actualElectron:true,installedAsarExercised:src.includes('.asar'),privateFixtures:true,externalRequests:0,exactPromptImageRecoveredBeforeConfirmation:true,laterImageAndAttachmentExcluded:true,newerIdenticalRequestCannotReuseOlderImage:true,explicitReadOnlyHistoryRecoversEarlierExactPrompt:true,collapsedThumbnailPromptRecovered:true,cookieKept:true},null,2));
 }finally{window.destroy();}
}
main().then(()=>app.exit(0)).catch(error=>{try{fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error)}));}finally{app.exit(1);}});
