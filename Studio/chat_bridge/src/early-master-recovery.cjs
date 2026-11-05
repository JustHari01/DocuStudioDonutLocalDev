"use strict";
const crypto = require("node:crypto");
const home = value => {
  try { const u = new URL(value); return u.origin === "https://chatgpt.com" && u.pathname === "/" && !u.search && !u.hash; }
  catch { return false; }
};
const sha = value => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const earlyError = /^(?:Giữ lựa chọn model ChatGPT: ChatGPT renderer (?:còn một probe DOM chưa kết thúc|không phản hồi probe DOM trong [0-9]+ ms)|ChatGPT renderer vẫn ở trạng thái ẩn sau khi mở nền \(probe=ChatGPT renderer không phản hồi probe DOM trong [0-9]+ ms\)|ERR_ABORTED \(-3\) loading 'https:\/\/chatgpt\.com\/\?refresh_account=true&startup_account_refresh=true')/;

function normalizeEarlyMasterFailure(value) {
  if (!value || value.version !== 1 || !home(value.chatUrl)
      || !sha(value.promptSha256) || !sha(value.attachmentSha256)
      || value.attachmentName !== `DocuRandom-Master-Prompt-${value.attachmentSha256.slice(0, 12)}.txt`
      || !Number.isSafeInteger(value.attachmentSize) || value.attachmentSize < 64000 || value.attachmentSize > 8 * 1024 * 1024
      || !Number.isSafeInteger(value.failedAt) || value.failedAt <= 0 || !earlyError.test(value.error || "")
      || !["bridge", "verified-diagnostic-log"].includes(value.origin)) return null;
  if (value.origin === "verified-diagnostic-log" && !sha(value.diagnosticSha256)) return null;
  return {version: 1, chatUrl: value.chatUrl, promptSha256: value.promptSha256,
    attachmentName: value.attachmentName, attachmentSha256: value.attachmentSha256,
    attachmentSize: value.attachmentSize, failedAt: value.failedAt, origin: value.origin,
    error: value.error.slice(0, 700),
    ...(value.origin === "verified-diagnostic-log" ? {diagnosticSha256: value.diagnosticSha256} : {})};
}

function captureEarlyMasterFailure({error, prompt, attachments, outputMode = "text", chatUrl,
  submissionSeen, attachmentSeen, pendingSubmission, pendingAttachment, now = Date.now,
  origin = "bridge", diagnosticSha256} = {}) {
  if (submissionSeen !== false || attachmentSeen !== false || pendingSubmission || pendingAttachment
      || !["", "text"].includes(outputMode) || !home(chatUrl) || !earlyError.test(String(error?.message || error || ""))
      || !Array.isArray(attachments) || attachments.length !== 1) return null;
  const file = attachments[0];
  const bytes = Buffer.from(String(file?.content_base64 || ""), "base64");
  const digest = crypto.createHash("sha256").update(bytes).digest("hex");
  return normalizeEarlyMasterFailure({version: 1, chatUrl,
    promptSha256: crypto.createHash("sha256").update(String(prompt || "").normalize("NFC").replace(/\s+/g, " ").trim()).digest("hex"),
    attachmentName: file?.name, attachmentSha256: digest, attachmentSize: bytes.length,
    failedAt: now(), error: String(error?.message || error || ""), origin, diagnosticSha256});
}

function earlyMasterProof(value, expected, promptSha256, snapshot, page) {
  const record = normalizeEarlyMasterFailure(value);
  if (!record || !expected || record.promptSha256 !== promptSha256
      || record.attachmentName !== expected.name || record.attachmentSha256 !== expected.sha256
      || record.attachmentSize !== expected.size || !home(snapshot?.url) || !home(page?.url)
      || snapshot.readyState !== "complete" || snapshot.visibilityState !== "visible"
      || snapshot.userCount !== 0 || snapshot.assistantCount !== 0 || snapshot.stopControlCount !== 0
      || snapshot.streamingMarkerCount !== 0 || snapshot.accountLoadFailed || snapshot.conversationLoadFailed
      || snapshot.connectionInterrupted || page.authenticated !== true || page.ready !== true
      || page.rateLimited !== false || page.accountLoadFailed || page.conversationLoadFailed || page.connectionInterrupted
      || page.visibleComposers !== 1 || page.selectedFileCount !== 0 || page.composerCardCount !== 0
      || !Number.isSafeInteger(page.composerDraftLength) || page.composerDraftLength > 1) return null;
  return JSON.stringify([record.promptSha256, record.attachmentSha256, record.failedAt,
    snapshot.url, page.composerDraftLength, "failed-before-master-input"]);
}
module.exports = {captureEarlyMasterFailure, normalizeEarlyMasterFailure, earlyMasterProof};

