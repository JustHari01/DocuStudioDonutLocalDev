"use strict";
const { app, BrowserWindow, session } = require('electron');
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const os = require('node:os'), crypto = require('node:crypto'), vm = require('node:vm');
const src = process.env.DOCURANDOM_HOME_RECOVERY_TEST_SRC || path.join(__dirname, '../src');
const { recoverFirstMasterHome } = require(path.join(src, 'first-master-home-recovery.cjs'));
const { conversationId } = require(path.join(src, 'conversation-health.cjs'));
const { masterPromptCaption } = require(path.join(src, 'prompt-transport.cjs'));
const { chatTurnSnapshot, pageSnapshot, recoverInterruptedTurn, waitForComposer, prepareWindowForBackground } = require(path.join(src, 'automation.cjs'));
const { beginRecoveryReadDeadline, recoveryReadError } = require(path.join(src, 'recovery-read-deadline.cjs'));
const proof = process.argv[2], code = fs.readFileSync(path.join(src, 'main.cjs'), 'utf8');
const start = code.indexOf('  if (request.method === "POST" && url.pathname === "/v1/turns/recover")');
const end = code.indexOf('  if (request.method === "POST" && url.pathname === "/v1/turns/cancel")', start);
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'DocuRandomMasterHomeUI-'));
app.setPath('userData', path.join(temp, 'profile'));
app.on('window-all-closed', () => {});
const digest = value => crypto.createHash('sha256').update(value).digest('hex');
const html = text => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
let debug = {};

async function main() {
  await app.whenReady();
  const root = path.join(temp, 'pending-attachments'); fs.mkdirSync(root);
  const master = '日本語'.repeat(23000), sha256 = digest(Buffer.from(master));
  const source = `DocuRandom-Master-Prompt-${sha256.slice(0, 12)}.txt`;
  const target = source.replace('.txt', '-abcdef123456.txt'), file = path.join(root, target);
  fs.writeFileSync(file, master);
  const attachment = { files: [{ source, target, path: file, size: Buffer.byteLength(master), sha256 }] };
  const expected = { name: source, size: Buffer.byteLength(master), sha256 };
  const owned = 'https://chatgpt.com/c/6ace1111-1234-83ec-a358-abcdef123456';
  const wrong = 'https://chatgpt.com/c/6ace2222-1234-83ec-a358-abcdef123456';
  const exact = 'https://chatgpt.com/c/6ace3333-1234-83ec-a358-abcdef123456';
  const isolated = session.fromPartition('first-master-home-private');
  const loads = [], closed = [], bindings = [], statuses = [], receipt = { sendAttempted: true, submissionAccepted: true,
    assistantCount: 0, userCount: 0, assistantKey: '', userKey: '', outputMode: 'text', createdAt: Date.now() - 180000 };
  const receiptBefore = JSON.stringify(receipt);
  isolated.protocol.handle('https', request => {
    loads.push(request.url);
    const caption = masterPromptCaption(request.url === wrong ? target.replace('abcdef', '123456') : target, master.length, sha256);
    const turns = request.url === 'https://chatgpt.com/' ? [owned, wrong, exact].map(url => `<a href="${url}">Fixture history</a>`).join('')
      : `<div data-message-author-role="user" data-message-id="first-master"><div>${html(caption)}</div></div><div data-message-author-role="assistant" data-message-id="ack"><div class="markdown">MASTER ACK VERIFIED</div><button aria-label="Copy">Copy</button></div>`;
    return new Response(`<main>${turns}<form><div id="prompt-textarea" contenteditable="true" role="textbox" style="height:35px"></div><button data-testid="send-button" onclick="window.sends++">Send</button><button aria-label="fixture Stop" style="display:none" onclick="window.stops++">Unused</button></form></main><script>window.sends=0;window.stops=0;</script>`, { headers: { 'Content-Type': 'text/html;charset=utf-8', 'Cache-Control': 'no-store' } });
  });
  await isolated.cookies.set({ url: exact, name: 'fixture-login', value: 'kept', httpOnly: true });
  const allContexts = new Set();
  async function createConversation(url) {
    const window = new BrowserWindow({ show: false, width: 900, height: 700, webPreferences: {
      session: isolated, sandbox: true, contextIsolation: true, nodeIntegration: false, backgroundThrottling: false } });
    await window.loadURL(url); await prepareWindowForBackground(window);
    const context = { window, chatUrl: url, busy: false }; allContexts.add(context); return context;
  }
  const context = await createConversation('https://chatgpt.com/');
  context.pendingSubmission = receipt; context.pendingAttachment = attachment;
  const identity = 'thread:first-master-fixture', savedTurns = new Map([
    [identity, { chatUrl: context.chatUrl, pendingSubmission: receipt, pendingAttachment: attachment }],
    ['thread:other-fixture', { chatUrl: owned + '?old-reader=1' }],
  ]);
  const turnConversations = new Map([[identity, context]]); let result;
  const scope = { Date, Math, String, Number, Object, Error, Set, AbortController,
    request: { method: 'POST' }, url: { pathname: '/v1/turns/recover' }, response: { once: () => {} },
    readJson: async () => ({ thread_id: 'first-master-fixture', pending_attachment: expected }),
    existingImageReaders: new Set(), turnControllers: new Map(), turnActivity: new Map(), turnConversations, savedTurns, allContexts, busyCount: 0,
    ...require(path.join(src, 'early-master-recovery.cjs')),
    recoverFirstMasterHome, ATTACHMENT_STAGING_ROOT: root, isExactChatConversationUrl: url => !!conversationId(url),
    createConversation, ensureConversationWindow: async () => {}, waitForComposer, chatTurnSnapshot, pageSnapshot,
    suspendConversationWindow: reader => { closed.push(reader.chatUrl); reader.window.destroy(); },
    persistState: () => {}, saveTurnContext: (id, reader) => { const value = { chatUrl: reader.chatUrl,
      pendingSubmission: reader.pendingSubmission, pendingAttachment: reader.pendingAttachment };
      savedTurns.set(id, value); bindings.push(structuredClone(value)); },
    discardPendingAttachment: reader => { reader.pendingAttachment = null; }, retireFailedRenderer: () => false,
    beginRecoveryReadDeadline: controller => beginRecoveryReadDeadline(controller, { timeoutMs: 20000 }), recoveryReadError,
    RECOVERY_CONNECTION_STALE_MS: 30000, RECOVERY_NO_RESPONSE_STALE_MS: 45000,
    recoverInterruptedTurn: (window, options) => recoverInterruptedTurn(window, { ...options,
      onStatus: (stage, message) => { statuses.push({ stage, message }); options.onStatus?.(stage, message); } }),
    recoverConversationLoad: async (_reader, operation) => operation(),
    sendJson: (_response, status, value) => { assert.equal(status, 200); result = value; },
  };
  try {
    await vm.runInNewContext('(async()=>{' + code.slice(start, end) + '})()', scope);
    const found = turnConversations.get(identity);
    assert.equal(result.output_text, 'MASTER ACK VERIFIED'); assert.equal(found.chatUrl, exact);
    assert.equal(JSON.stringify(bindings[0].pendingSubmission), receiptBefore);
    assert.equal(found.pendingSubmission, null); assert.equal(found.busy, false); assert.equal(scope.busyCount, 0);
    assert.deepEqual(loads, ['https://chatgpt.com/', wrong, exact]); assert.deepEqual(closed, [wrong, 'https://chatgpt.com/']);
    assert.equal(await found.window.webContents.executeJavaScript('window.sends'), 0);
    assert.equal(await found.window.webContents.executeJavaScript('window.stops'), 0);
    assert.equal((await isolated.cookies.get({ url: exact, name: 'fixture-login' }))[0].value, 'kept');
    fs.writeFileSync(proof, JSON.stringify({ actualElectronReader: true, productionRecoveryRoute: true,
      installedAsarExercised: src.includes('app.asar'), exactUniqueMasterChatRecovered: true, acknowledgementRecovered: true,
      priorAcceptedReceiptKeptDuringBinding: true, otherWorkspaceURLExcluded: true, wrongNonceReaderDiscarded: true,
      noNewPrompt: true, noProviderStop: true, cookieKept: true, externalRequests: 0 }, null, 2));
  } finally { debug = { loads, closed, statuses, snapshot: await chatTurnSnapshot(turnConversations.get(identity).window).catch(() => null) };
    for (const reader of allContexts) if (!reader.window.isDestroyed()) reader.window.destroy(); }
}
main().then(() => app.quit()).catch(error => { fs.writeFileSync(proof, JSON.stringify({ failed: String(error.stack || error), debug })); app.exit(1); });

