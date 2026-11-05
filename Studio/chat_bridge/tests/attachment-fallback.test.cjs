"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const { attachmentImportFallback, verifiedMasterPromptText } = require("../src/automation.cjs");
function attachment(bytes) {
  const sha = crypto.createHash("sha256").update(bytes).digest("hex");
  return [{ name: `DocuRandom-Master-Prompt-${sha.slice(0,12)}.txt`,
    content_base64: bytes.toString("base64") }];
}
test("the private attachment transport restores the entire original Unicode message only after an absence proof", () => {
  const original = "Nạp nguyên văn 日本語 và Tiếng Việt\n".repeat(6000);
  const files = attachment(Buffer.from(original));
  for (const error of [new Error("ChatGPT file input was not found"),
    Object.assign(new Error("import inactive for five minutes"), {code:"CHATGPT_ATTACHMENT_IMPORT_UNCERTAIN",fileSelectionAttempted:true})]) {
    const result=attachmentImportFallback(error,"read the file",files,"text");
    assert.equal(result.kind,"inline");
    assert.equal(result.prompt,original);
    assert.equal(result.requiresClearedAttachment,true);
  }
});
test("mismatched hashes, invalid UTF-8, unrelated files, multiple files and excessive messages remain locked", () => {
  const valid=attachment(Buffer.from("x".repeat(70000)));
  const corrupt=[{...valid[0],content_base64:Buffer.from("different bytes").toString("base64")}];
  for (const files of [corrupt,attachment(Buffer.from([0xff,0xfe])),
    attachment(Buffer.from("x".repeat(512001))),[{...valid[0],name:"master.txt"}], [valid[0],valid[0]]]) {
    assert.equal(verifiedMasterPromptText(files,"text"),null);
    const result=attachmentImportFallback(Object.assign(new Error("uncertain"),{code:"CHATGPT_ATTACHMENT_IMPORT_UNCERTAIN"}),"read file",files,"text");
    assert.equal(result.kind,"stop");
  }
  assert.equal(verifiedMasterPromptText(valid,"generated_image"),null);
});
test("account limits and duplicate-file rejection cannot trigger the large master fallback", () => {
  const files=attachment(Buffer.from("x".repeat(70000)));
  for (const error of [Object.assign(new Error("duplicate"),{code:"CHATGPT_DUPLICATE_ATTACHMENT"}),
    new Error("quota rate limit"),new Error("captcha"),new Error("giới hạn tài khoản")]) {
    assert.equal(attachmentImportFallback(error,"read file",files,"text").kind,"stop");
  }
});
