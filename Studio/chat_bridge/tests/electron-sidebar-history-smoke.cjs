"use strict";
const {app,BrowserWindow,session}=require('electron');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chatTurnSnapshot,pageSnapshot}=require('../src/automation.cjs');
const proof=process.argv[2];app.setPath('userData',path.join(path.dirname(proof),'sidebar-fixture-profile'));
app.on('window-all-closed',()=>{});
async function main(){
  await app.whenReady();const isolated=session.fromPartition('sidebar-history-fixture');
  isolated.protocol.handle('https',()=>new Response(`<aside>Recents<p>Unable to load history</p><button>Retry</button></aside>
    <main><h1>What’s on your mind today?</h1><form><div id="prompt-textarea" contenteditable="true" role="textbox" style="height:80px;width:600px"></div><button data-testid="send-button" onclick="window.sends++">Send</button></form></main><script>window.sends=0</script>`,{headers:{'Content-Type':'text/html; charset=utf-8'}}));
  const w=new BrowserWindow({show:false,width:1000,height:720,webPreferences:{session:isolated,sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
  try{
    await w.loadURL('https://chatgpt.com/');
    const page=await pageSnapshot(w),snap=await chatTurnSnapshot(w);
    assert.equal(page.ready,true);assert.equal(page.authenticated,true);
    assert.equal(page.conversationLoadFailed,false);assert.equal(snap.conversationLoadFailed,false);
    await w.webContents.executeJavaScript(`document.querySelector('main').innerHTML='<p>Could not load this ChatGPT conversation</p><button>Retry</button>';void 0;`);
    assert.equal((await pageSnapshot(w)).conversationLoadFailed,true);
    assert.equal((await chatTurnSnapshot(w)).conversationLoadFailed,true);
    await w.webContents.executeJavaScript(`document.body.innerHTML='<p>Unable to load history</p><button>Retry</button>';void 0;`);
    assert.equal((await pageSnapshot(w)).conversationLoadFailed,true);
    assert.equal(await w.webContents.executeJavaScript('window.sends'),0);
    fs.writeFileSync(proof,JSON.stringify({sidebarFailureDoesNotBlockReadyHome:true,
      actualConversationFailureStillBlocks:true,incompleteShellStillBlocks:true,promptsSubmitted:0,externalRequests:0},null,2));
  }finally{w.destroy();}
}
main().then(()=>app.quit()).catch(error=>{fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error)}));app.exit(1);});
