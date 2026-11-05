"use strict";
const { isExactChatConversationUrl } = require("./navigation.cjs");

function rendererProbeFailed(error) {
  return ["CHATGPT_PAGE_PROBE_TIMEOUT", "CHATGPT_PAGE_PROBE_IN_FLIGHT", "CHATGPT_INPUT_NATIVE_TIMEOUT"]
    .includes(error?.code)
    || /renderer (?:không phản hồi probe DOM|còn một probe DOM chưa kết thúc)/i.test(String(error?.message || error || ""));
}

function retireFailedRenderer(context, error, { protectActiveTurn = false } = {}) {
  if (protectActiveTurn) return false;
  if (!rendererProbeFailed(error)) return false;
  const window = context?.window;
  if (!window || window.isDestroyed()) return false;
  const url = String(window.webContents?.getURL?.() || "");
  if (isExactChatConversationUrl(url)) context.chatUrl = url;
  // Keep the logical conversation and durable send locks. Replace only the
  // disposable reader; re-opening it does not authorize another submission.
  window.destroy();
  context.window = null;
  return true;
}

function suspendFailedConversationReader(context, error, { protectActiveTurn = false } = {}) {
  if (protectActiveTurn || !/CHATGPT_CONVERSATION_LOAD_FAILED/.test(String(error?.message || error || ""))) return false;
  const window = context?.window;
  if (!window || window.isDestroyed()) return false;
  const url = String(window.webContents?.getURL?.() || "");
  if (isExactChatConversationUrl(url)) context.chatUrl = url;
  // A failed idle SPA continues retrying its own history requests even while
  // Studio is in backoff. Close only that reader; keep all accepted receipts,
  // attachment staging and the persistent browser session for the next lease.
  window.destroy();
  context.window = null;
  return true;
}

function replaceFailedIdleReader(window, error, { protectActiveTurn = false, createWindow } = {}) {
  if (protectActiveTurn || !window || window.isDestroyed()) return window;
  // The main page's authentication check can own a brief probe immediately
  // after opening a chat. That collision is not a frozen renderer.
  if (error?.code === "CHATGPT_PAGE_PROBE_IN_FLIGHT"
      && !(Number(error.pendingForMs) >= 5_000)) return window;
  if (!rendererProbeFailed(error) && !/CHATGPT_CONVERSATION_LOAD_FAILED/.test(String(error?.message || error || ""))) return window;
  // Create first: replacing the main reader must not trigger windowless quit.
  // The factory shares the existing persistent partition; durable receipts
  // live on the logical contexts and are never cleared here.
  const replacement = createWindow();
  window.destroy();
  return replacement;
}

module.exports = { rendererProbeFailed, retireFailedRenderer, suspendFailedConversationReader, replaceFailedIdleReader };
