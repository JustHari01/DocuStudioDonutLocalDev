"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const {app, BrowserWindow} = require("electron");
const {insertPromptText, withTrustedWindowStage} = require("../src/automation.cjs");
app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "DocuRandomInputLeaseSmoke-")));
const fixture = 'data:text/html;charset=utf-8,' + encodeURIComponent(
  '<textarea id="prompt-textarea" style="width:600px;height:300px"></textarea>');
const create = async () => {
  const w = new BrowserWindow({show:false, webPreferences:{backgroundThrottling:false}});
  await w.loadURL(fixture);
  return w;
};
(async () => {
  await app.whenReady();
  const first = await create();
  let nativeCalls = 0;
  const stalled = {
    isDestroyed: () => first.isDestroyed(),
    isVisible: () => true,
    destroy: () => first.destroy(),
    webContents: {
      getURL: () => "https://chatgpt.com/c/input-lease-smoke",
      executeJavaScript: (...args) => first.webContents.executeJavaScript(...args),
      insertText: () => {nativeCalls += 1; return new Promise(() => {});},
    },
  };
  const pending = withTrustedWindowStage(stalled, undefined, () => insertPromptText(stalled, "pending", {
    expectedBefore:"", expectedAfter:"pending", nativeInputTimeoutMs:50,
  }));
  const failed = assert.rejects(pending, /CHATGPT_NATIVE_INPUT_TIMEOUT/);
  failed.catch(() => {});
  const second = await create();
  const recovered = {isDestroyed:()=>second.isDestroyed(), isVisible:()=>true, webContents:second.webContents};
  await withTrustedWindowStage(recovered, undefined, () => insertPromptText(recovered, "日本語の確認", {
    expectedBefore:"", expectedAfter:"日本語の確認",
  }));
  await failed;
  assert.equal(first.isDestroyed(), true);
  assert.equal(nativeCalls, 1);
  assert.equal(await second.webContents.executeJavaScript('document.querySelector("textarea").value'), "日本語の確認");
  second.destroy();
  const proof = {hungRendererRetired:true,nextConversationInputSucceeded:true,nativeCalls};
  if (process.env.DOCURANDOM_INPUT_SMOKE_REPORT) {
    fs.writeFileSync(process.env.DOCURANDOM_INPUT_SMOKE_REPORT, JSON.stringify(proof));
  }
  app.exit(0);
})().catch(error => {
  if (process.env.DOCURANDOM_INPUT_SMOKE_REPORT) {
    fs.writeFileSync(process.env.DOCURANDOM_INPUT_SMOKE_REPORT, JSON.stringify({error:String(error)}));
  }
  app.exit(1);
});
