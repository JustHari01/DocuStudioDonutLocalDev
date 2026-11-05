"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { loadConversationUrl } = require("../src/window-load.cjs");

test("a completed conversation navigation stays on its exact URL", async () => {
  const urls = [];
  await loadConversationUrl({ loadURL: async url => { urls.push(url); } }, "https://chatgpt.com/c/exact", {
    timeoutMs: 100,
  });
  assert.deepEqual(urls, ["https://chatgpt.com/c/exact"]);
});

test("a stuck pre-submit navigation stops before the caller heartbeat deadline", async () => {
  await assert.rejects(
    loadConversationUrl({ loadURL: () => new Promise(() => {}) }, "https://chatgpt.com/c/exact", {
      timeoutMs: 10,
    }),
    error => error.code === "CHATGPT_CONVERSATION_LOAD_TIMEOUT"
      && /chưa gửi prompt/.test(error.message),
  );
});


test('startup account refresh abort only accepts a stable canonical home, never an exact saved chat',async()=>{
 const message="ERR_ABORTED (-3) loading 'https://chatgpt.com/?refresh_account=true&startup_account_refresh=true'";
 const window={loadURL:async()=>{throw new Error(message)},isDestroyed:()=>false,
  webContents:{getURL:()=> 'https://chatgpt.com/',isLoadingMainFrame:()=>false}};
 await loadConversationUrl(window,'https://chatgpt.com/');
 await assert.rejects(loadConversationUrl(window,'https://chatgpt.com/c/old'),/ERR_ABORTED/);
 window.webContents.getURL=()=> 'https://accounts.google.com/signin';
 await assert.rejects(loadConversationUrl(window,'https://chatgpt.com/',{timeoutMs:10}),/ERR_ABORTED/);
});
