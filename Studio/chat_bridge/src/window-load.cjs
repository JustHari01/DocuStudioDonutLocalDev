"use strict";

const CONVERSATION_LOAD_TIMEOUT_MS = 60_000;

async function loadConversationUrl(window, url, { timeoutMs = CONVERSATION_LOAD_TIMEOUT_MS } = {}) {
  let timer;
  try {
    await Promise.race([
      Promise.resolve().then(() => window.loadURL(url)),
      new Promise((_, reject) => {
        timer = setTimeout(() => {
          const error = new Error(
            `ChatGPT không hoàn tất tải cuộc hội thoại sau ${timeoutMs} ms; chưa gửi prompt.`,
          );
          error.code = "CHATGPT_CONVERSATION_LOAD_TIMEOUT";
          reject(error);
        }, timeoutMs);
      }),
    ]);
  } catch (error) {
    // Account refresh can cancel the initial home navigation while Electron
    // has already followed it back to the same authenticated SPA. Never use
    // this for an exact /c/... route or another navigation failure.
    if (url === 'https://chatgpt.com/' &&
        /^ERR_ABORTED \(-3\) loading 'https:\/\/chatgpt\.com\/\?refresh_account=true&startup_account_refresh=true'/.test(String(error?.message || error))) {
      const deadline = Date.now() + Math.min(5000, timeoutMs);
      let stable = 0;
      while (Date.now() < deadline && !window.isDestroyed()) {
        const wc = window.webContents;
        if (wc.getURL() === url && !wc.isLoadingMainFrame()) {
          if (++stable >= 2) return;
        } else stable = 0;
        await new Promise(resolve => setTimeout(resolve, 100));
      }
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

module.exports = { CONVERSATION_LOAD_TIMEOUT_MS, loadConversationUrl };
