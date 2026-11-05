"use strict";
const {app,BrowserWindow,session}=require("electron");
const fs=require("node:fs"),crypto=require("node:crypto"),assert=require("node:assert/strict");
const {chatTurnSnapshot,pageSnapshot,recoverInterruptedTurn}=require("../src/automation.cjs");
const {checkpointReceiptProof,receiptFromCheckpoint}=require("../src/checkpoint-receipt.cjs");
const proof=process.argv[2];app.on("window-all-closed",()=>{});
async function main(){
  await app.whenReady();
  const url="https://chatgpt.com/c/6acb3b2e-1a24-83ec-b788-16ce5cf15033";
  const prompt="Bổ sung đúng phần thoại còn thiếu. "+"日本の工場で検証した。".repeat(130);
  const answer="既存の回答を読み戻します。".repeat(130);
  const browserSession=session.fromPartition("checkpoint-receipt-fixture");
  let requests=0;
  browserSession.protocol.handle("https",()=>{requests++;return new Response(`<main>
    <div data-message-author-role="user" data-message-id="u1">Old request</div>
    <div data-message-author-role="assistant" data-message-id="a1">Old answer</div>
    <div data-message-author-role="user" data-message-id="u2">${prompt}</div>
    <div data-message-author-role="assistant" data-message-id="a2"><div class="markdown">${answer}</div><button aria-label="Copy">Copy</button></div>
    <form><div contenteditable="true" role="textbox" aria-label="Ask ChatGPT" style="height:30px"></div><button data-testid="send-button" onclick="window.sends++">Send</button></form></main><script>window.sends=0</script>`,{headers:{"Content-Type":"text/html; charset=utf-8"}});});
  const window=new BrowserWindow({show:false,width:900,height:700,webPreferences:{session:browserSession,sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
  try{
    await window.loadURL(url);
    const hash=crypto.createHash("sha256").update(prompt.normalize("NFC").replace(/\s+/g," ").trim()).digest("hex");
    const snap=await chatTurnSnapshot(window),page=await pageSnapshot(window);
    assert.equal(snap.latestUserTextSha256,hash);assert.equal(snap.latestAssistantAfterUser,true);
    assert.ok(checkpointReceiptProof(snap,page,{chatUrl:url,promptSha256:hash}));
    assert.equal(checkpointReceiptProof(snap,page,{chatUrl:url,promptSha256:"0".repeat(64)}),null);
    const result=await recoverInterruptedTurn(window,{submissionEvidence:receiptFromCheckpoint(snap),persistedConversationUrl:url});
    assert.equal(result,answer);assert.equal(await window.webContents.executeJavaScript("window.sends"),0);
    fs.writeFileSync(proof,JSON.stringify({fullPromptHashMatched:true,existingFullReplyRecovered:true,
      wrongHashRejected:true,promptsSubmitted:0,externalRequests:0,fixtureRequests:requests},null,2));
  }finally{if(!window.isDestroyed())window.destroy();}
}
main().then(()=>app.quit()).catch(error=>{if(proof)fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack||error)}));app.exit(1);});
