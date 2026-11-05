"use strict";
const {app,BrowserWindow,session}=require('electron');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const src=process.env.DOCURANDOM_OWNED_IMAGE_TEST_SRC || path.join(__dirname,'../src');
const {inspectImageHistory}=require(path.join(src,'image-history-view.cjs'));
const proof=process.argv[2];
app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'DocuRandomImageView-')));
app.on('window-all-closed',()=>{});
async function main(){
 await app.whenReady();const url='https://chatgpt.com/c/6ace5555-1234-83ec-a358-abcdef123456';
 const isolated=session.fromPartition('image-view-test');
 isolated.protocol.handle('https',()=>new Response('<main><div data-message-author-role="user"><div class="whitespace-pre-wrap" dir="auto">Short</div><button id="expand">Show more</button></div><div data-message-author-role="assistant">Done</div><button id="retry">Try again</button><button id="send">Send</button></main>',{headers:{'Content-Type':'text/html;charset=utf-8'}}));
 const window=new BrowserWindow({show:false,width:900,height:700,webPreferences:{session:isolated,sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
 try{
  await window.loadURL(url);
  await window.webContents.executeJavaScript(`(()=>{window.sends=0;document.querySelector('#expand').onclick=()=>{document.querySelector('.whitespace-pre-wrap').textContent='Expanded exact thumbnail prompt';};document.querySelector('#send').onclick=document.querySelector('#retry').onclick=()=>window.sends++;return true;})()`);
  const original=await inspectImageHistory(window,{savedUrl:url});
  assert(Buffer.from(original.screenshot_base64,'base64').subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])));
  const expanded=await inspectImageHistory(window,{savedUrl:url,action:'expand',control:original.controls.find(c=>c.label==='Show more')});
  assert(expanded.userPrompts.some(p=>p.prefix==='Expanded exact thumbnail prompt'));
  await assert.rejects(inspectImageHistory(window,{savedUrl:url,action:'image',control:original.controls.find(c=>c.label==='Try again')}),/display controls/);
  assert.equal(await window.webContents.executeJavaScript('window.sends'),0);
  assert.equal(window.webContents.getURL(),url);
  fs.writeFileSync(proof,JSON.stringify({actualElectron:true,asarExercised:src.includes('.asar'),exactChatPreserved:true,expandedUserMessage:true,realPngCaptured:true,generationButtonsRejected:true,promptsSubmitted:0},null,2));
 }finally{window.destroy();}
}
main().then(()=>app.exit(0)).catch(e=>{fs.writeFileSync(proof,JSON.stringify({failed:String(e.stack||e)}));app.exit(1);});
