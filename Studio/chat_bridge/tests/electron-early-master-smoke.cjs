"use strict";
const fs=require("node:fs"),os=require("node:os"),path=require("node:path"),crypto=require("node:crypto"),assert=require("node:assert/strict");
const {app,BrowserWindow}=require("electron");
const {pageSnapshot,chatTurnSnapshot}=require("../src/automation.cjs");
const {captureEarlyMasterFailure,earlyMasterProof}=require("../src/early-master-recovery.cjs");
app.setPath("userData",fs.mkdtempSync(path.join(os.tmpdir(),"DocuRandomEarlyMasterSmoke-")));
let w;
(async()=>{
 await app.whenReady();
 w=new BrowserWindow({show:false,width:1000,height:700,webPreferences:{sandbox:true,contextIsolation:true,backgroundThrottling:false,paintWhenInitiallyHidden:true}});
 await w.webContents.session.protocol.handle("https",()=>new Response('<main><form><div id="prompt-textarea" role="textbox" contenteditable="true" style="min-height:120px;width:800px"><p><br></p></div><button data-testid="send-button">Send</button></form></main>',{headers:{"Content-Type":"text/html; charset=utf-8"}}));
 await w.loadURL("https://chatgpt.com/");
 const bytes=Buffer.alloc(70000,65),sha=crypto.createHash("sha256").update(bytes).digest("hex");
 const record=captureEarlyMasterFailure({error:new Error("Giữ lựa chọn model ChatGPT: ChatGPT renderer không phản hồi probe DOM trong 5000 ms"),
   prompt:"Exact first Master caption",attachments:[{name:`DocuRandom-Master-Prompt-${sha.slice(0,12)}.txt`,content_base64:bytes.toString("base64")}],
   chatUrl:"https://chatgpt.com/",submissionSeen:false,attachmentSeen:false});
 const expected={name:record.attachmentName,sha256:sha,size:bytes.length};
 const first=earlyMasterProof(record,expected,record.promptSha256,await chatTurnSnapshot(w),await pageSnapshot(w));
 assert.ok(first,"a real empty native editor must match the preserved pre-input failure");
 await w.webContents.executeJavaScript("document.querySelector('#prompt-textarea').textContent='A draft must be preserved'");
 assert.equal(earlyMasterProof(record,expected,record.promptSha256,await chatTurnSnapshot(w),await pageSnapshot(w)),null);
 await w.webContents.executeJavaScript("document.querySelector('#prompt-textarea').textContent='';const u=document.createElement('div');u.dataset.messageAuthorRole='user';u.textContent='A real sent turn';document.querySelector('main').append(u)");
 assert.equal(earlyMasterProof(record,expected,record.promptSha256,await chatTurnSnapshot(w),await pageSnapshot(w)),null);
 if(process.env.DOCURANDOM_EARLY_MASTER_REPORT)fs.writeFileSync(process.env.DOCURANDOM_EARLY_MASTER_REPORT,JSON.stringify({nativeEmptyEditorVerified:true,existingDraftBlocked:true,existingUserTurnBlocked:true}));
 w.destroy();app.exit(0);
})().catch(error=>{if(process.env.DOCURANDOM_EARLY_MASTER_REPORT)fs.writeFileSync(process.env.DOCURANDOM_EARLY_MASTER_REPORT,JSON.stringify({error:String(error),stack:error.stack}));if(w&&!w.isDestroyed())w.destroy();app.exit(1);});
