"use strict";
const assert=require("node:assert/strict");
const fs=require("node:fs");
const os=require("node:os");
const path=require("node:path");
const {app,BrowserWindow,session}=require("electron");
const {executePageProbe}=require("../src/automation.cjs");
const {retireFailedRenderer,suspendFailedConversationReader}=require("../src/renderer-recovery.cjs");
const proof=process.env.DOCURANDOM_RENDERER_PROOF;
if(!proof)throw new Error("An isolated proof path is required");
app.setPath("userData",fs.mkdtempSync(path.join(os.tmpdir(),"DocuRandomRendererRecovery-")));
app.commandLine.appendSwitch("disable-gpu-shader-disk-cache");
app.on("window-all-closed",()=>{});
async function main(){
  await app.whenReady();
  const create=()=>new BrowserWindow({show:false,webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false}});
  const first=create();let second,transient;
  try{
    await first.loadURL("data:text/html;charset=utf-8,"+encodeURIComponent("<main>First</main>"));
    const pending={submissionAccepted:true,assistantCount:4};
    const context={window:first,chatUrl:"https://chatgpt.com/c/exact-owned-chat",pendingSubmission:pending};
    let failure;
    try{await executePageProbe(first,"new Promise(() => {})",{timeoutMs:25});}catch(error){failure=error;}
    assert.equal(failure.code,"CHATGPT_PAGE_PROBE_TIMEOUT");
    assert.equal(retireFailedRenderer(context,failure),true);
    assert.equal(first.isDestroyed(),true);assert.equal(context.pendingSubmission,pending);
    assert.equal(context.chatUrl,"https://chatgpt.com/c/exact-owned-chat");
    second=create();await second.loadURL("data:text/html;charset=utf-8,"+encodeURIComponent("<main>再開しました</main>"));
    assert.equal(await executePageProbe(second,"document.querySelector('main').textContent"),"再開しました");
    const serverUrl="https://chatgpt.com/c/server-saved";
    session.defaultSession.protocol.handle("https",()=>new Response("<main>Fixture only</main>",
      {headers:{"Content-Type":"text/html;charset=utf-8"}}));
    await session.defaultSession.cookies.set({url:serverUrl,name:"fixture-login",value:"kept",httpOnly:true});
    for(const [recover,error] of [
      [retireFailedRenderer,{code:"CHATGPT_PAGE_PROBE_TIMEOUT"}],
      [suspendFailedConversationReader,new Error("CHATGPT_CONVERSATION_LOAD_FAILED")],
    ]){
      transient=create();
      await transient.loadURL("https://chatgpt.com/c/local-chatgpt%3Afixture");
      const receipt={submissionAccepted:true};
      const local={window:transient,chatUrl:serverUrl,pendingSubmission:receipt};
      assert.equal(recover(local,error),true);
      assert.equal(local.chatUrl,serverUrl);
      assert.equal(local.pendingSubmission,receipt);
      assert.equal(transient.isDestroyed(),true);
    }
    assert.equal((await session.defaultSession.cookies.get({url:serverUrl,name:"fixture-login"}))[0].value,"kept");
    fs.writeFileSync(proof,JSON.stringify({poisonedRendererRetired:true,submissionLockPreserved:true,newReaderResponded:true,
      provisionalUrlCannotOverwriteServerUrl:true,cookieKept:true,promptsSubmitted:0,externalRequests:0}));
  }finally{if(!first.isDestroyed())first.destroy();if(second&&!second.isDestroyed())second.destroy();if(transient&&!transient.isDestroyed())transient.destroy();}
}
main().then(()=>app.quit()).catch(error=>{fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error)}));app.exit(1);});
