"use strict";

const { conversationId } = require("./conversation-health.cjs");
const RECOVERY_COOLDOWN_MS = 15 * 60_000;

function preSubmitSendFailure(state, { sendAttempted = false } = {}) {
  if (sendAttempted || state?.ok !== false || state?.connectionInterrupted !== true
      || !['send button not found', 'send button disabled'].includes(state?.reason)) return null;
  return Object.assign(new Error(
    'CHATGPT_CONVERSATION_LOAD_FAILED: Giao diện giữ trạng thái mất kết nối trước khi gửi; '
    + 'nút Send chưa sẵn sàng sau khi tải lại. Giữ draft/checkpoint và thay trang đọc đúng hội thoại, chưa gửi prompt.',
  ), {code:'CHATGPT_CONVERSATION_LOAD_FAILED',statusCode:409});
}

function loadFailed(error) {
  return /CHATGPT_CONVERSATION_LOAD_FAILED/.test(String(error?.message || error || ""));
}

function annotate(error, health) {
  if (!loadFailed(error) || !health || health.classification === "unknown") return error;
  const detail = health.blocking || health.conversation?.at(-1);
  const message = String(error.message || error);
  const suffix = ` [connection=${health.classification};http=${detail?.statusCode || 0}${detail?.error ? ";net=" + detail.error : ""}${detail?.retryAfterSeconds ? ";retry-after-seconds=" + detail.retryAfterSeconds : ""}]`;
  const result = new Error(message + suffix);
  result.code = "CHATGPT_CONVERSATION_LOAD_FAILED";
  return result;
}

async function withConversationLoadRecovery(context, operation, {
  reopen, recoverAccepted, health, signal, onStatus, now = Date.now,
} = {}) {
  try { return await operation(); } catch (error) {
    if (!loadFailed(error) || !conversationId(context?.chatUrl) || signal?.aborted) throw error;
    const facts = health?.();
    if (["authentication-required", "access-rejected", "conversation-unavailable", "rate-limited"]
      .includes(facts?.classification)) throw annotate(error, facts);
    if (facts?.history_throttle) throw annotate(error, {
      classification: 'history-rate-limited', blocking: facts.history_throttle,
    });
    if (context.loadRecoveryAt && now() - context.loadRecoveryAt < RECOVERY_COOLDOWN_MS) {
      throw annotate(error, facts);
    }
    context.loadRecoveryAt = now();
    onStatus?.("conversation-renderer-recovery", "Đang thay trang đọc bị lỗi và mở lại đúng hội thoại; giữ tệp và checkpoint.");
    await reopen();
    if (signal?.aborted) throw error;
    try {
      // Once Send was attempted, never call the submission operation again.
      // The replacement reader may only recover the existing accepted turn.
      if (context.pendingSubmission) {
        if (!recoverAccepted) throw error;
        const recovered = await recoverAccepted(context.pendingSubmission);
        if (recovered?.status === "not-submitted") throw error;
        return recovered;
      }
      return await operation();
    } catch (retryError) { throw annotate(retryError, health?.()); }
  }
}

module.exports = { withConversationLoadRecovery, RECOVERY_COOLDOWN_MS, preSubmitSendFailure };
