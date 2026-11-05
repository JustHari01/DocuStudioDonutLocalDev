"use strict";
const {app, BrowserWindow, session} = require("electron");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), vm = require("node:vm"), assert = require("node:assert/strict");
const {chatTurnSnapshot, pageSnapshot} = require("../src/automation.cjs");
const {withConversationLoadRecovery} = require("../src/conversation-load-recovery.cjs");
const proof = process.env.DOCURANDOM_PARTIAL_TEST_PROOF;
const code = fs.readFileSync(path.join(__dirname,"../src/main.cjs"),"utf8").replace(/\r\n/g,"\n");
const route = code.split('  if (request.method === "POST" && url.pathname === "/v1/session/repair-conversation") {')[1]?.split('  if (request.method === "POST" && url.pathname === "/v1/session/open-conversation") {')[0];
assert.ok(route);
app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(),"DocuRandomPartialHistory-")));
app.on("window-all-closed",()=>{});
async function main() {
  await app.whenReady();
  const url = "https://chatgpt.com/c/6acb3b2e-1a24-83ec-b788-16ce5cf15033";
  const isolated = session.fromPartition("partial-history-fixture");
  let requests = 0, repaired = false;
  isolated.protocol.handle("https",()=>{
    requests++;
    const turns = repaired
      ? '<div data-message-author-role="user">Accepted prompt</div><div data-message-author-role="assistant">Existing response</div>'
      : '<p>Unable to load history</p><p>Connection interrupted. Waiting for the complete answer</p>'
        + Array(9).fill('<div data-message-author-role="user">TIẾP</div>').join("");
    return new Response('<main>'+turns+'<form><div contenteditable="true" role="textbox" aria-label="Ask ChatGPT" style="height:30px"></div><button data-testid="send-button" onclick="window.sends++">Send</button></form></main><script>window.sends=0;</script>',{headers:{"Content-Type":"text/html"}});
  });
  await isolated.cookies.set({url,name:"fixture-login",value:"kept",httpOnly:true});
  const windows = [];
  const create = async()=>{
    const w = new BrowserWindow({show:false,width:900,height:700,webPreferences:{session:isolated,sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
    windows.push(w); await w.loadURL(url); return w;
  };
  const receipt={submissionAccepted:true,userCount:9,assistantCount:1};
  const attachment={status:"selected"};
  const context={window:await create(),chatUrl:url,pendingSubmission:receipt,pendingAttachment:attachment,busy:false};
  const old = context.window;
  const partial = await chatTurnSnapshot(old);
  assert.equal(partial.userCount,9); assert.equal(partial.assistantCount,0);
  assert.equal(partial.conversationLoadFailed,true);
  assert.equal((await pageSnapshot(old)).ready,false);
  const identity="thread:fixture-partial";
  let replaced=0, status=0, output;
  const scope=vm.createContext({Date,Boolean,Error,
    request:{method:"POST"},url:{pathname:"/v1/session/repair-conversation"},response:{},
    readJson:async()=>({thread_id:"fixture-partial"}), normalizeTurnIdentity:id=>"thread:"+id,
    savedTurns:new Map([[identity,{chatUrl:url,pendingSubmission:receipt}]]),turnConversations:new Map([[identity,context]]),
    existingImageReaders:new Set(),turnControllers:new Map(),isExactChatConversationUrl:value=>value===url,
    ensureConversationWindow:async()=>{},chatTurnSnapshot,pageSnapshot,
    saveTurnContext:()=>{},persistState:()=>{},sendJson:(_response,s,value)=>{status=s;output=value;},
    recoverConversationLoad:(c,op,options)=>withConversationLoadRecovery(c,op,{...options,
      reopen:async()=>{replaced++; c.window.destroy();repaired=true;c.window=await create();}}),
  });
  vm.runInContext('async function repairRoute(){ if (request.method === "POST" && url.pathname === "/v1/session/repair-conversation") {'+route+'\n}',scope);
  try {
    context.busy=true; await scope.repairRoute(); assert.equal(status,409);assert.equal(replaced,0);
    context.busy=false; await scope.repairRoute(); assert.equal(status,200);assert.equal(output.status,"ready");
    assert.equal(output.prompt_submitted,false);assert.equal(output.pending_submission,true);
    assert.equal(old.isDestroyed(),true);assert.equal(context.pendingSubmission,receipt);assert.equal(context.pendingAttachment,attachment);
    assert.equal((await isolated.cookies.get({url,name:"fixture-login"}))[0].value,"kept");
    assert.equal(await context.window.webContents.executeJavaScript("window.sends"),0);
    await scope.repairRoute();assert.equal(replaced,1,"healthy readers must not be replaced");
    fs.writeFileSync(proof,JSON.stringify({productionRepairRoute:true,partialHistoryDetected:true,
      healthyReaderKept:true,activeOwnerRejected:true,cookieAndReceiptKept:true,promptsSubmitted:0,externalRequests:0,fixtureRequests:requests},null,2));
  } finally {for(const w of windows)if(!w.isDestroyed())w.destroy();}
}
main().then(()=>app.quit()).catch(error=>{if(proof)fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error)}));app.exit(1);});
