"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), crypto = require("node:crypto");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os");
const {captureEarlyMasterFailure, normalizeEarlyMasterFailure, earlyMasterProof} = require("../src/early-master-recovery.cjs");
const {reserveEmptyAttachmentRestage} = require("../src/pre-submit-restage.cjs");
const bytes = Buffer.alloc(70000, 65), sha = crypto.createHash("sha256").update(bytes).digest("hex");
const input = () => ({error: new Error("Giữ lựa chọn model ChatGPT: ChatGPT renderer còn một probe DOM chưa kết thúc; tool dừng probe mới"),
  prompt: "Exact pending file caption", attachments: [{name: `DocuRandom-Master-Prompt-${sha.slice(0, 12)}.txt`, content_base64: bytes.toString("base64")}],
  chatUrl: "https://chatgpt.com/", submissionSeen: false, attachmentSeen: false, now: () => 12345});
const live = record => ({expected: {name: record.attachmentName, sha256: record.attachmentSha256, size: record.attachmentSize},
  snapshot: {url: record.chatUrl, readyState: "complete", visibilityState: "visible", userCount: 0, assistantCount: 0,
    stopControlCount: 0, streamingMarkerCount: 0},
  page: {url: record.chatUrl, authenticated: true, ready: true, rateLimited: false, visibleComposers: 1,
    selectedFileCount: 0, composerCardCount: 0, composerDraftLength: 1}});

test("only the observed initial Master model-probe failure records an unsubmitted claim", () => {
  const record = captureEarlyMasterFailure(input());
  assert.ok(record); assert.equal(record.origin, "bridge");
  for (const changed of [{submissionSeen: true}, {attachmentSeen: true}, {pendingSubmission: {sendAttempted: true}},
    {pendingAttachment: {}}, {outputMode: "generated_image"}, {chatUrl: "https://chatgpt.com/c/owned-chat"},
    {error: new Error("Gửi nội dung: ChatGPT renderer không phản hồi probe DOM trong 5000 ms")},
    {error: new Error("Giữ lựa chọn model ChatGPT: CHATGPT_MODEL_UNAVAILABLE: High is limited")},
    {error: new Error("CHATGPT_RATE_LIMITED: HTTP 429")}, {attachments: []}]) {
    assert.equal(captureEarlyMasterFailure({...input(), ...changed}), null, JSON.stringify(changed));
  }
});
test("capture validates attachment bytes and identity and survives a JSON state round trip", () => {
  const record = captureEarlyMasterFailure(input());
  assert.deepEqual(normalizeEarlyMasterFailure(JSON.parse(JSON.stringify(record))), record);
  assert.equal(captureEarlyMasterFailure({...input(), attachments: [{...input().attachments[0], content_base64: Buffer.alloc(70000, 66).toString("base64")}]}), null);
  assert.equal(normalizeEarlyMasterFailure({...record, failedAt: -1}), null);
  assert.equal(normalizeEarlyMasterFailure({...record, origin: "arbitrary"}), null);
  assert.equal(normalizeEarlyMasterFailure({...record, origin: "verified-diagnostic-log"}), null);
  assert.ok(normalizeEarlyMasterFailure({...record, origin: "verified-diagnostic-log", diagnosticSha256: "a".repeat(64)}));
});
test("live proof requires an authenticated empty idle home and exact wire/file hashes", () => {
  const record = captureEarlyMasterFailure(input()), f = live(record);
  const proof = (r = record, e = f.expected, s = f.snapshot, p = f.page, h = record.promptSha256) => earlyMasterProof(r, e, h, s, p);
  assert.ok(proof());
  for (const delta of [{userCount: 1}, {assistantCount: 1}, {stopControlCount: 1}, {streamingMarkerCount: 1},
    {conversationLoadFailed: true}, {accountLoadFailed: true}, {connectionInterrupted: true}, {url: "https://chatgpt.com/c/old"}])
    assert.equal(proof(record, f.expected, {...f.snapshot, ...delta}), null);
  for (const delta of [{authenticated: false}, {ready: false}, {rateLimited: true}, {visibleComposers: 2},
    {composerDraftLength: 5}, {selectedFileCount: 1}, {composerCardCount: 1}, {composerDraftLength: undefined}])
    assert.equal(proof(record, f.expected, f.snapshot, {...f.page, ...delta}), null);
  assert.equal(proof(record, {...f.expected, sha256: "f".repeat(64)}), null);
  assert.equal(proof(record, f.expected, f.snapshot, f.page, "f".repeat(64)), null);
});
test("one first-Master recovery reservation cannot repeat after reload", t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "early-master-test-"));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const record = captureEarlyMasterFailure(input()), f = live(record), store = path.join(root, "reservations.json");
  const proof = earlyMasterProof(record, f.expected, record.promptSha256, f.snapshot, f.page);
  const write = (p, value) => fs.writeFileSync(p, JSON.stringify(value));
  assert.equal(reserveEmptyAttachmentRestage(store, "thread:thread_early_master_fixture", sha, proof, write), true);
  assert.equal(reserveEmptyAttachmentRestage(store, "thread:thread_early_master_fixture", sha, proof, write, () => 999999), false);
});


test('account refresh abort before renderer input is recoverable but submitted/other URLs are not',()=>{
 const error=new Error("ERR_ABORTED (-3) loading 'https://chatgpt.com/?refresh_account=true&startup_account_refresh=true'");
 assert.ok(captureEarlyMasterFailure({...input(),error}));
 assert.equal(captureEarlyMasterFailure({...input(),error,submissionSeen:true}),null);
 assert.equal(captureEarlyMasterFailure({...input(),error,attachmentSeen:true}),null);
 assert.equal(captureEarlyMasterFailure({...input(),error:new Error("ERR_ABORTED (-3) loading 'https://chatgpt.com/c/old'")}),null);
});

test('new Master visibility probe failure is pre-input only and never permits accepted attachment replay',()=>{
 const error=new Error('ChatGPT renderer vẫn ở trạng thái ẩn sau khi mở nền (probe=ChatGPT renderer không phản hồi probe DOM trong 750 ms)');
 assert.ok(captureEarlyMasterFailure({...input(),error}));
 for(const change of [{submissionSeen:true},{attachmentSeen:true},{pendingSubmission:{sendAttempted:true}},{pendingAttachment:{status:'confirmed'}},{chatUrl:'https://chatgpt.com/c/existing'}])
  assert.equal(captureEarlyMasterFailure({...input(),error,...change}),null);
});
