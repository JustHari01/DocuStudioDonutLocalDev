"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { runImageTurnWithOneRetry, retryableImageFailure, existingImageResult } = require("../src/image-retry.cjs");
const failed = { terminalError: "ChatGPT không tạo được ảnh: yêu cầu ảnh nguồn", text: "KHÔNG TẠO ĐƯỢC" };
const idle = { readyState: "complete", assistantCount: 1, stopControlCount: 0, streamingMarkerCount: 0 };
test("finished source-image failure gets one same-conversation follow-up", async () => {
  const runs = []; let reserved = 0;
  const image = { generatedImage: { data_base64: "image" } };
  const result = await runImageTurnWithOneRetry(async retry => { runs.push(retry); return retry ? image : failed; },
    { diagnose: async () => idle, beforeRetry: async () => { reserved++; } });
  assert.equal(result, image); assert.deepEqual(runs, [false, true]); assert.equal(reserved, 1);
});
test("a second negative result never loops", async () => {
  let runs = 0;
  await runImageTurnWithOneRetry(async () => { runs++; return failed; }, { diagnose: async () => idle });
  assert.equal(runs, 2);
});

test("the observed new-image initialization refusal receives exactly one idle retry", async () => {
  const result = { terminalError: "CHATGPT_TERMINAL_RESPONSE_NO_IMAGE",
    text: "KHÔNG TẠO ĐƯỢC: Công cụ tạo ảnh không thực hiện được yêu cầu tạo ảnh mới từ đầu trong lượt này." };
  const calls = [];
  await runImageTurnWithOneRetry(async retry => { calls.push(retry); return result; },
    { diagnose: async () => idle });
  assert.deepEqual(calls, [false, true]);
  assert.equal(retryableImageFailure({...result,text:result.text+" Đã hết lượt quota."}, idle), false);
  assert.equal(retryableImageFailure(result, {...idle,stopControlCount:1}), false);
});
test("streaming, limits, unknown completion and existing images cannot trigger resend", () => {
  for (const changed of [{ stopControlCount: 1 }, { streamingMarkerCount: 1 }, { assistantCount: 0 }, { readyState: "loading" }]) {
    assert.equal(retryableImageFailure(failed, { ...idle, ...changed }), false);
  }
  for (const detail of ["quota", "rate limit", "chính sách", "captcha"]) {
    assert.equal(retryableImageFailure({ ...failed, text: detail }, idle), false);
  }
  assert.equal(retryableImageFailure({ ...failed, generatedImage: {} }, idle), false);
  assert.equal(retryableImageFailure({ terminalError: "transport lost" }, idle), false);
  assert.equal(retryableImageFailure({ terminalError: "Công cụ tạo ảnh nhầm yêu cầu tạo ảnh mới thành chỉnh sửa ảnh có sẵn" }, idle), true);
});
test("cancellation and interrupted retry preserve uncertainty instead of a third send", async () => {
  const controller = new AbortController(); let runs = 0;
  await runImageTurnWithOneRetry(async () => { runs++; return failed; }, {
    diagnose: async () => idle, beforeRetry: async () => controller.abort(), signal: controller.signal,
  });
  assert.equal(runs, 1);
  runs = 0;
  await assert.rejects(runImageTurnWithOneRetry(async retry => { runs++; if (retry) throw Error("transport lost"); return failed; },
    { diagnose: async () => idle }), /transport lost/);
  assert.equal(runs, 2);
});
test("manual completed image is collected; incomplete turns cannot return old images", () => {
  const snapshot = { ...idle, userCount: 1 };
  const image = { ready: true, afterLatestUser: true, dataBase64: "img", width: 1280, height: 720 };
  assert.equal(existingImageResult(snapshot, image).status, "ready");
  assert.equal(existingImageResult({...snapshot,userCount:2},image).status, "ready",
    "a completed image-only card can follow two prompts and one textual answer");
  for (const change of [{stopControlCount:1}, {streamingMarkerCount:1}]) {
    assert.equal(existingImageResult({...snapshot,...change},image).status, "pending");
  }
  assert.equal(existingImageResult(snapshot, {...image,busy:true}).status,"pending");
  assert.equal(existingImageResult(snapshot, {...image,afterLatestUser:false}).status,"pending");
});
test("only the latest explicit failure is terminal; old body text cannot authorize a resend", () => {
  const snapshot = { ...idle, userCount: 1, bodyTail: "Image generation failed" };
  assert.equal(existingImageResult(snapshot,{candidateCount:0,assistantText:""}).status,"pending");
  assert.equal(existingImageResult(snapshot,{candidateCount:0,latestAssistantAfterUser:true,assistantText:"KHÔNG TẠO ĐƯỢC"}).status,"no-image");
  assert.equal(existingImageResult(snapshot,{candidateCount:0,latestAssistantAfterUser:false,assistantText:"KHÔNG TẠO ĐƯỢC"}).status,"pending");
  assert.equal(existingImageResult(snapshot,{candidateCount:1,fetchError:"HTTP 503"}).status,"pending");
});
