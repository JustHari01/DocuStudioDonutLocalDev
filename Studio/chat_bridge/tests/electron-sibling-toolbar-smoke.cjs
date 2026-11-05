"use strict";
const {app, BrowserWindow, session} = require('electron');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chatTurnSnapshot, pageSnapshot, recoverInterruptedTurn} = require('../src/automation.cjs');
const {checkpointReceiptProof, receiptFromCheckpoint} = require('../src/checkpoint-receipt.cjs');
app.setPath('userData', fs.mkdtempSync(path.join(os.tmpdir(), 'DocuRandomSiblingToolbar-')));
app.on('window-all-closed', () => {});
const proofPath = process.argv[2];
async function main() {
  await app.whenReady();
  const url = 'https://chatgpt.com/c/6ad1dfd2-e594-83ec-9e92-d807176f1231';
  const prompt = 'Chỉ bổ sung phần còn thiếu, giữ nguyên kịch bản.';
  const answer = 'ĐÃ XONG';
  const hash = crypto.createHash('sha256').update(prompt).digest('hex');
  const user = `<div data-chatgpt-search-message-ids="user"><div class="group/user-message">${prompt}</div></div>`;
  const response = `<div data-chatgpt-selection-message-id="assistant"><div data-markdown-text-style="true">${answer}</div></div>`;
  const action = '<div role="toolbar"><button aria-label="Copy response">Copy</button><button aria-label="Good response">Good</button></div>';
  const composer = '<form><div contenteditable="true" role="textbox" aria-label="Ask ChatGPT" style="height:30px"></div><button data-testid="send-button" onclick="window.sends++">Send</button><button aria-label="Copy">Unrelated</button></form>';
  let fixture = '';
  const local = session.fromPartition('sibling-toolbar-fixture');
  local.protocol.handle('https', () => new Response(`<main>${fixture}${composer}</main><script>window.sends=0</script>`, {headers: {'Content-Type':'text/html; charset=utf-8'}}));
  const window = new BrowserWindow({show:false, width:900, height:700, webPreferences:{session:local, sandbox:true, contextIsolation:true, nodeIntegration:false, backgroundThrottling:false}});
  const results = [];
  async function check(name, html, expected) {
    fixture = html;
    await window.loadURL(url);
    const snap = await chatTurnSnapshot(window), page = await pageSnapshot(window);
    assert.equal(snap.latestUserTextSha256, hash, name);
    assert.equal(Boolean(checkpointReceiptProof(snap, page, {chatUrl:url, promptSha256:hash})), expected, name + JSON.stringify(snap.completionScopeDiagnostics));
    assert.equal(await window.webContents.executeJavaScript('window.sends'), 0);
    results.push({name, completionActions:snap.completionActionCount, accepted:expected});
    return snap;
  }
  try {
    const snap = await check('sibling toolbar belongs to exact answer', user + `<section><div><div>${response}</div></div>${action}</section>`, true);
    const result = await recoverInterruptedTurn(window, {submissionEvidence:receiptFromCheckpoint(snap), persistedConversationUrl:url});
    assert.equal(result, answer);
    await check('composer and previous response cannot complete latest answer',
      `<section><div data-chatgpt-selection-message-id="previous"><div data-markdown-text-style="true">Old answer</div></div>${action}</section>` + user + `<section>${response}</section>`, false);
    await check('button embedded in answer is not completion', user + `<section>${response.replace(answer, answer + '<button aria-label="Copy">Copy code</button>')}</section>`, false);
    await check('hidden sibling toolbar is not completion', user + `<section>${response}${action.replace('<button', '<button style="display:none"').replace('<button aria-label="Good', '<button style="display:none" aria-label="Good')}</section>`, false);
    await check('next user prevents borrowing later toolbar', user + `<section>${response}<div class="group/user-message">${prompt}</div>${action}</section>`, false);
    await check('streaming response cannot be reconstructed', user + `<section>${response}${action}<span data-is-streaming="true">Generating</span></section><button data-testid="stop-button">Stop</button>`, false);
    await check('sibling streaming marker blocks completion without global stop', user + `<section>${response}${action}<span data-is-streaming="true">Generating</span></section>`, false);
    if (proofPath) fs.writeFileSync(proofPath, JSON.stringify({passed:results, recoveredExistingReply:true, promptsSubmitted:0}, null, 2));
  } finally { window.destroy(); }
}
main().then(() => app.quit()).catch(error => { if (proofPath) fs.writeFileSync(proofPath, JSON.stringify({failed:String(error.stack || error)})); app.exit(1); });
