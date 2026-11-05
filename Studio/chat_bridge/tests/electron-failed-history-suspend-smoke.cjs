"use strict";
const {app,BrowserWindow,session}=require("electron");
const fs=require("node:fs"),path=require("node:path"),vm=require("node:vm"),assert=require("node:assert/strict");
const {withConversationLoadRecovery}=require("../src/conversation-load-recovery.cjs");
const {suspendFailedConversationReader}=require("../src/renderer-recovery.cjs");
const proof=process.argv[2];
const source=fs.readFileSync(path.join(__dirname,"../src/main.cjs"),"utf8");
const actual=source.slice(source.indexOf("async function recoverConversationLoad("),source.indexOf("function latestActiveTurnActivityUpdatedAt("));
app.on("window-all-closed",()=>{});
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function main(){
  await app.whenReady();
  const browserSession=session.fromPartition("failed-history-suspend-fixture");
  const url="https://chatgpt.com/c/6acb3b2e-1a24-83ec-b788-16ce5cf15033";
  let historyRequests=0;
  browserSession.protocol.handle("https",request=>{
    if(request.url.includes("/backend-api/")){historyRequests++;return new Response("limited",{status:429});}
    return new Response('<main>Unable to load history</main><script>setInterval(()=>fetch("/backend-api/conversations/test").catch(()=>{}),25);</script>',{headers:{"Content-Type":"text/html"}});
  });
  await browserSession.cookies.set({url,name:"fixture-login",value:"kept",httpOnly:true});
  const window=new BrowserWindow({show:false,webPreferences:{session:browserSession,nodeIntegration:false,contextIsolation:true,sandbox:true,backgroundThrottling:false}});
  await window.loadURL(url);await pause(200);
  assert.ok(historyRequests>0);
  const receipt={submissionAccepted:true},attachment={status:"confirmed"};
  const context={window,chatUrl:url,pendingSubmission:receipt,pendingAttachment:attachment};
  const scope=vm.createContext({withConversationLoadRecovery,suspendFailedConversationReader,
    connectionHealth:()=>({classification:"rate-limited",blocking:{statusCode:429}}),
    suspendConversationWindow:()=>assert.fail("known throttle must not reload"),
    ensureConversationWindow:()=>assert.fail("known throttle must not reopen")});
  vm.runInContext(actual,scope);
  await assert.rejects(scope.recoverConversationLoad(context,()=>{throw new Error("CHATGPT_CONVERSATION_LOAD_FAILED");}));
  assert.equal(window.isDestroyed(),true);assert.equal(context.window,null);
  await pause(100);const after=historyRequests;await pause(200);
  assert.equal(historyRequests,after,"idle failed SPA must stop polling through backoff");
  assert.equal(context.pendingSubmission,receipt);assert.equal(context.pendingAttachment,attachment);
  assert.equal((await browserSession.cookies.get({url,name:"fixture-login"}))[0].value,"kept");
  fs.writeFileSync(proof,JSON.stringify({productionRecoveryWrapper:true,failedReaderClosed:true,
    backgroundHistoryRequestsStopped:true,cookieAndReceiptKept:true,promptsSubmitted:0,externalRequests:0},null,2));
}
main().then(()=>app.quit()).catch(error=>{if(proof)fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error)}));app.exit(1);});
