"use strict";
const {isExplicitImageFailure}=require('./image-terminal.cjs');

function retryableImageFailure(result, snapshot) {
  const text = String(result?.terminalError || "") + " " + String(result?.text || "");
  return Boolean(result?.terminalError
    && !result?.generatedImage
    && /ảnh nguồn|source image|không khởi tạo|initiali[sz]|nhầm.{0,100}chỉnh sửa|mistak.{0,100}edit|công cụ tạo ảnh không thực hiện được.{0,100}tạo ảnh mới/i.test(text)
    && !/policy|chính sách|quota|rate.?limit|giới hạn|hết lượt|captcha|không được phép/i.test(text)
    && snapshot?.readyState === "complete"
    && Number(snapshot?.assistantCount) > 0
    && Number(snapshot?.stopControlCount) === 0
    && Number(snapshot?.streamingMarkerCount) === 0);
}

async function runImageTurnWithOneRetry(run, { diagnose, beforeRetry, signal } = {}) {
  const first = await run(false);
  if (!first?.terminalError || signal?.aborted) return first;
  const snapshot = await diagnose();
  if (!retryableImageFailure(first, snapshot)) return first;
  await beforeRetry?.();
  if (signal?.aborted) return first;
  // The second run gets its own trusted-send checkpoint. No third run here.
  return run(true);
}

function existingImageResult(snapshot, image, {expectedUserPromptSha256 = ""} = {}) {
  const pending = Number(snapshot?.stopControlCount) > 0 || Number(snapshot?.streamingMarkerCount) > 0
    || image?.busy === true || snapshot?.readyState !== "complete";
  const exactHistoricalImage = /^[a-f0-9]{64}$/.test(expectedUserPromptSha256)
    && image?.matchedUserPromptSha256 === expectedUserPromptSha256;
  if (!pending && image?.ready && image?.dataBase64 && (expectedUserPromptSha256 ? exactHistoricalImage : image?.afterLatestUser === true)) {
    return { status: "ready", generated_image: {
      data_base64: image.dataBase64, mime_type: image.mimeType,
      width: image.width, height: image.height, size: image.size,
    } };
  }
  const failed = !pending && image?.latestAssistantAfterUser === true && Number(image?.candidateCount) === 0
    && isExplicitImageFailure(image?.assistantText);
  return { status: failed ? "no-image" : "pending",
    assistant_count: snapshot?.assistantCount, user_count: snapshot?.userCount,
    image_ready: image?.ready === true, image_busy: image?.busy === true,
    candidate_count: Number(image?.candidateCount) || 0,
    after_latest_user: image?.afterLatestUser === true,
    latest_assistant_after_user: image?.latestAssistantAfterUser === true,
    detail: String(image?.fetchError || image?.assistantText || "").slice(0, 1000) };
}
module.exports = { retryableImageFailure, runImageTurnWithOneRetry, existingImageResult };
