"use strict";

const crypto = require("node:crypto");

// A six-figure prompt makes ChatGPT's Lexical composer progressively rebuild
// an ever-larger document. Above this boundary, preserve the exact UTF-8 input
// as one text attachment and keep only a short instruction in the composer.
const FAST_PROMPT_ATTACHMENT_THRESHOLD_CHARS = 64_000;

function masterPromptCaption(name, charLength, sha256) {
  return [
    "Toàn bộ nội dung yêu cầu của lượt này nằm nguyên vẹn trong tệp UTF-8 đính kèm.",
    `Tệp: ${name}`,
    `Độ dài: ${charLength.toLocaleString("en-US")} ký tự; SHA-256: ${sha256}.`,
    "Hãy đọc toàn bộ tệp từ ký tự đầu tiên đến ký tự cuối cùng và xử lý như thể toàn bộ nội dung tệp được dán trực tiếp vào tin nhắn này.",
    "Không tóm tắt, không bỏ qua, không thay đổi thứ tự và tuân thủ đúng mọi chỉ dẫn nằm trong tệp.",
  ].join("\n");
}

function prepareCodexPromptTransport(
  prompt,
  {
    thresholdChars = FAST_PROMPT_ATTACHMENT_THRESHOLD_CHARS,
    maxAttachmentBytes = 8 * 1024 * 1024,
  } = {},
) {
  const exactPrompt = String(prompt ?? "");
  const bytes = Buffer.from(exactPrompt, "utf8");
  if (
    exactPrompt.length < thresholdChars
    || bytes.length === 0
    || bytes.length > maxAttachmentBytes
  ) {
    return {
      mode: "inline",
      prompt: exactPrompt,
      attachments: [],
      charLength: exactPrompt.length,
      byteLength: bytes.length,
      sha256: "",
    };
  }

  const sha256 = crypto.createHash("sha256").update(bytes).digest("hex");
  const name = `DocuRandom-Master-Prompt-${sha256.slice(0, 12)}.txt`;
  const composerPrompt = masterPromptCaption(name, exactPrompt.length, sha256);

  return {
    mode: "attachment",
    prompt: composerPrompt,
    attachments: [{
      name,
      mime_type: "text/plain; charset=utf-8",
      content_base64: bytes.toString("base64"),
    }],
    charLength: exactPrompt.length,
    byteLength: bytes.length,
    sha256,
  };
}

module.exports = {
  FAST_PROMPT_ATTACHMENT_THRESHOLD_CHARS,
  masterPromptCaption,
  prepareCodexPromptTransport,
};
