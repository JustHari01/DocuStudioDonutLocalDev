"use strict";
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {app, BrowserWindow} = require('electron');
const {pageSnapshot, pendingAttachmentUiState, pendingAttachmentCleared, verifyPendingAttachment} = require(process.env.DOCURANDOM_AUTOMATION_MODULE || '../src/automation.cjs');
const proof = process.env.DOCURANDOM_WORKSPACE_PROOF;
if (!proof) throw new Error('An isolated proof file is required');
app.setPath('userData', fs.mkdtempSync(path.join(os.tmpdir(), 'DocuRandomReliabilityHold-')));
app.on('window-all-closed', () => {});
const notice = "We're doing a quick check to keep ChatGPT reliable. Try again after 10:17\u202fPM.";
async function main() {
  await app.whenReady();
  const window = new BrowserWindow({show:false,width:900,height:700,webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
  const probeWindow = {isDestroyed:()=>window.isDestroyed(),webContents:{getURL:()=> 'https://chatgpt.com/c/exact-chat',
    executeJavaScript:(...args)=>window.webContents.executeJavaScript(...args)}};
  const files = [{target:'audit-abc.txt'}];
  const load = html => window.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent('<main>' + html + '</main>'));
  const composer = '<div role="textbox" aria-label="Ask ChatGPT" contenteditable="true" style="min-height:30px">audit-abc.txt</div>';
  const send = '<button data-testid="send-button">Send</button>';
  try {
    await load('<form><div>' + notice + '</div>' + composer + send + '</form>');
    assert.equal((await pageSnapshot(probeWindow)).rateLimited, true, 'plain provider banner inside composer form is detected');
    for (const quote of ['<div data-message-author-role="user">', '<div data-chatgpt-selection-message-id="assistant"><div class="MarkdownRoot-test">', '<div role="textbox" contenteditable="true" aria-label="Ask ChatGPT">']) {
      await load(quote + notice + '</div></div><form>' + composer + send + '</form>');
      assert.equal((await pageSnapshot(probeWindow)).rateLimited, false, 'quoted warning cannot pause account');
    }
    await load('<form><div data-message-author-role="user">audit-abc.txt</div>' + composer + send + '</form>');
    let state = await pendingAttachmentUiState(probeWindow, files);
    assert.equal(state.storedNameInDraft, true);
    assert.equal(state.storedNameVisible, false, 'file name in draft/history is not a file card');
    assert.equal(pendingAttachmentCleared(state), true);
    assert.equal(await verifyPendingAttachment(probeWindow, files), false);
    await load('<form><div class="new-file-card"><span>audit-abc.txt</span></div>' + composer + send + '</form>');
    state = await pendingAttachmentUiState(probeWindow, files);
    assert.equal(state.storedNameVisible, true, 'plain filename outside draft is a card');
    assert.equal(pendingAttachmentCleared(state), false);
    assert.equal(await verifyPendingAttachment(probeWindow, files), true);
    await window.webContents.executeJavaScript('document.querySelector("form").insertAdjacentHTML("beforeend", "<progress></progress>")');
    assert.equal(await verifyPendingAttachment(probeWindow, files), false, 'in-progress import remains blocked');
    fs.writeFileSync(proof, JSON.stringify({plainReliabilityHoldDetected:true,quotesExcluded:true,draftAndHistoryExcluded:true,plainFileCardRecognized:true,pendingImportBlocked:true}));
  } finally {window.destroy();}
}
main().then(()=>app.quit()).catch(error=>{fs.writeFileSync(proof,JSON.stringify({failed:String(error.stack || error)}));app.exit(1);});
