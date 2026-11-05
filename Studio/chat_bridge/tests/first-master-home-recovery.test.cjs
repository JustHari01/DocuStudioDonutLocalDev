"use strict";
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), crypto = require('node:crypto');
const { firstMasterProofHash, recoverFirstMasterHome } = require('../src/first-master-home-recovery.cjs');
const { masterPromptCaption } = require('../src/prompt-transport.cjs');
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const normalize = value => value.normalize('NFC').replace(/\s+/g, ' ').trim();
const urls = [1, 2, 3, 4].map(n => `https://chatgpt.com/c/6ace000${n}-1234-83ec-a358-abcdef123456`);

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'DocuRandomFirstMaster-'));
  const text = '日本語'.repeat(23000), bytes = Buffer.from(text);
  const sha256 = hash(bytes), source = `DocuRandom-Master-Prompt-${sha256.slice(0, 12)}.txt`;
  const target = source.replace('.txt', '-abcdef123456.txt'), file = path.join(root, target);
  fs.writeFileSync(file, bytes);
  t.after(() => { fs.unlinkSync(file); fs.rmdirSync(root); });
  const attachment = { files: [{ source, target, path: file, size: bytes.length, sha256 }] };
  const expected = { name: source, size: bytes.length, sha256 };
  const proofHash = hash(normalize(masterPromptCaption(target, text.length, sha256)));
  const context = { busy: false, chatUrl: 'https://chatgpt.com/' };
  const pending = { submissionAccepted: true, sendAttempted: true, assistantCount: 0, userCount: 0 };
  const options = { pending, attachment, expected, stagingRoot: root, usedUrls: new Set(),
    inventory: async () => ({ recentConversationLinks: urls.map(url => ({ url })) }),
    open: async url => ({ chatUrl: url }),
    probe: async reader => ({ snapshot: { url: reader.chatUrl, readyState: 'complete', userCount: 1,
      users: [{ key: 'exact-first-master' }], latestUserTextSha256: proofHash },
      page: { url: reader.chatUrl, authenticated: true, ready: true } }),
    pause: async () => {}, discard: () => {} };
  return { root, file, context, options, attachment, expected, text, proofHash };
}

test('Master proof binds the verified bytes and unique file nonce, including UTF-16 character length', t => {
  const f = fixture(t);
  assert.equal(firstMasterProofHash(f.attachment, f.expected, f.root), f.proofHash);
  const changedNonce = structuredClone(f.attachment);
  changedNonce.files[0].target = changedNonce.files[0].source;
  assert.equal(firstMasterProofHash(changedNonce, f.expected, f.root), null);
});

test('wrong expected bytes, modified file, outside path or mismatched filename cannot identify a chat', t => {
  const f = fixture(t);
  assert.equal(firstMasterProofHash(f.attachment, { ...f.expected, sha256: 'a'.repeat(64) }, f.root), null);
  assert.equal(firstMasterProofHash(f.attachment, { ...f.expected, size: f.expected.size + 1 }, f.root), null);
  assert.equal(firstMasterProofHash(f.attachment, f.expected, path.join(f.root, 'missing')), null);
  const outside = structuredClone(f.attachment);
  outside.files[0].path = path.join(f.root, '..', outside.files[0].target);
  assert.equal(firstMasterProofHash(outside, f.expected, f.root), null);
  const otherName = structuredClone(f.attachment);
  otherName.files[0].target = otherName.files[0].target.replace('abcdef', '123456');
  assert.equal(firstMasterProofHash(otherName, f.expected, f.root), null);
  fs.appendFileSync(f.file, 'changed');
  assert.equal(firstMasterProofHash(f.attachment, f.expected, f.root), null);
});

test('accepted first Master can recover an unbound exact chat using two stable read proofs', async t => {
  const f = fixture(t), opened = [], probes = [], discarded = [];
  const receiptBefore = JSON.stringify(f.options.pending), attachmentBefore = JSON.stringify(f.attachment);
  f.options.usedUrls.add(urls[0] + '?old-reader=1');
  f.options.open = async url => { assert.equal(f.context.busy, true); opened.push(url); return { chatUrl: url }; };
  const probe = f.options.probe;
  f.options.probe = async reader => { probes.push(reader.chatUrl); return probe(reader); };
  f.options.discard = reader => discarded.push(reader.chatUrl);
  const reader = await recoverFirstMasterHome(f.context, f.options);
  assert.equal(reader.chatUrl, urls[1]);
  assert.deepEqual(opened, [urls[1]]); assert.deepEqual(probes, [urls[1], urls[1]]);
  assert.deepEqual(discarded, []); assert.equal(f.context.busy, false);
  assert.equal(JSON.stringify(f.options.pending), receiptBefore);
  assert.equal(JSON.stringify(f.attachment), attachmentBefore);
  assert.equal(f.context.chatUrl, 'https://chatgpt.com/');
});

test('busy, ordinary later turns, exact chat or unaccepted prompt never scan history', async t => {
  const f = fixture(t);
  f.options.inventory = async () => { assert.fail('must not scan'); };
  const cases = [
    [{ ...f.context, busy: true }, f.options],
    [{ ...f.context, chatUrl: urls[0] }, f.options],
    [f.context, { ...f.options, pending: { ...f.options.pending, assistantCount: 1 } }],
    [f.context, { ...f.options, pending: { ...f.options.pending, userCount: null } }],
    [f.context, { ...f.options, pending: { ...f.options.pending, submissionAccepted: false } }],
    [f.context, { ...f.options, expected: { ...f.expected, size: 1 } }],
  ];
  for (const [context, options] of cases) assert.equal(await recoverFirstMasterHome(context, options), null);
});

test('same master template from another nonce and unstable first-user identity cannot bind', async t => {
  const f = fixture(t), discarded = [];
  f.options.inventory = async () => ({ recentConversationLinks: [{ url: urls[0] }] });
  const original = f.options.probe;
  f.options.probe = async reader => { const value = await original(reader); value.snapshot.latestUserTextSha256 = 'a'.repeat(64); return value; };
  f.options.discard = reader => discarded.push(reader.chatUrl);
  assert.equal(await recoverFirstMasterHome(f.context, f.options), null);
  let count = 0;
  f.options.probe = async reader => { const value = await original(reader); value.snapshot.users[0].key = 'key-' + (++count); return value; };
  assert.equal(await recoverFirstMasterHome(f.context, f.options), null);
  assert.deepEqual(discarded, [urls[0], urls[0]]); assert.equal(f.context.busy, false);
});

test('failed inventory is a bounded miss and always releases the workspace reader', async t => {
  const f = fixture(t);
  f.options.inventory = async () => { throw Error('renderer unavailable'); };
  assert.equal(await recoverFirstMasterHome(f.context, f.options), null);
  assert.equal(f.context.busy, false);
});

test('at most three candidates are opened and partial or failed readers are discarded', async t => {
  const f = fixture(t), opened = [], discarded = [];
  f.options.open = async url => { opened.push(url); return { chatUrl: url }; };
  f.options.probe = async () => { throw Error('partial DOM'); };
  f.options.discard = reader => { discarded.push(reader.chatUrl); if (reader.chatUrl === urls[0]) throw Error('destroyed already'); };
  assert.equal(await recoverFirstMasterHome(f.context, f.options), null);
  assert.deepEqual(opened, urls.slice(0, 3)); assert.deepEqual(discarded, opened);
  assert.equal(f.context.busy, false);
});
