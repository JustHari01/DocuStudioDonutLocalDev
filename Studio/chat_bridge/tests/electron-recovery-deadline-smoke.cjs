"use strict";
const { app, BrowserWindow, session } = require("electron");
const assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), vm = require("node:vm");
const src = process.env.DOCURANDOM_RECOVERY_TEST_SRC || path.join(__dirname, "../src");
const proof = process.env.DOCURANDOM_RECOVERY_TEST_PROOF;
const { recoverInterruptedTurn } = require(path.join(src, "automation.cjs"));
const { beginRecoveryReadDeadline, recoveryReadError, RECOVERY_CONNECTION_STALE_MS,
  RECOVERY_NO_RESPONSE_STALE_MS } = require(path.join(src, "recovery-read-deadline.cjs"));
const code = fs.readFileSync(path.join(src, "main.cjs"), "utf8").replace(/\r\n/g, "\n");
const route = code.split('  if (request.method === "POST" && url.pathname === "/v1/turns/recover") {')[1]?.split('  if (request.method === "POST" && url.pathname === "/v1/turns/cancel") {')[0];
assert.ok(route, "The production recovery route must be exercised");
app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "DocuRandomRecoveryDeadline-")));
app.on("window-all-closed", () => {});

async function main() {
  await app.whenReady();
  const url = "https://chatgpt.com/c/exact-recovery-fixture";
  const isolated = session.fromPartition("recovery-deadline-fixture");
  isolated.protocol.handle("https", () => new Response('<main><div data-message-author-role="user" data-message-id="accepted-user">Fixture accepted prompt</div><form><div contenteditable="true" role="textbox" aria-label="Ask ChatGPT" style="height:30px"></div><button data-testid="send-button" onclick="window.fixtureSends++">Send</button></form></main><script>window.fixtureSends=0;</script>', { headers: { "Content-Type": "text/html" } }));
  await isolated.cookies.set({ url, name: "fixture-login", value: "preserved", httpOnly: true });
  const window = new BrowserWindow({ show: false, width: 900, height: 700,
    webPreferences: { session: isolated, sandbox: true, contextIsolation: true, nodeIntegration: false, backgroundThrottling: false } });
  await window.loadURL(url);
  const receipt = { sendAttempted: true, submissionAccepted: true, assistantCount: 0, userCount: 0,
    prompt: "Fixture accepted prompt", createdAt: Date.now() - 30 * 60_000, chatUrl: url };
  const context = { window, chatUrl: url, pendingSubmission: receipt, pendingAttachment: null, busy: false };
  const threadId = "fixture-thread-accepted";
  const identity = `thread:${threadId}`;
  let persisted = 0, sentResponses = 0;
  const sandbox = { Date, Math, String, Number, Object, Error, AbortController,
    request: { method: "POST" }, url: { pathname: "/v1/turns/recover" },
    response: { once: () => {} }, readJson: async () => ({ thread_id: threadId }),
    savedTurns: new Map([[identity, { chatUrl: url, pendingSubmission: receipt }]]),
    turnConversations: new Map([[identity, context]]), existingImageReaders: new Map(), turnControllers: new Map(), turnActivity: new Map(),
    busyCount: 0, ensureConversationWindow: async () => {},
    saveTurnContext: () => {}, persistState: () => { persisted++; },
    sendJson: () => { sentResponses++; }, discardPendingAttachment: () => { throw new Error("No result may discard this receipt"); },
    retireFailedRenderer: () => false,
    beginRecoveryReadDeadline: controller => beginRecoveryReadDeadline(controller, { timeoutMs: 1500 }),
    recoveryReadError, recoverInterruptedTurn,
    RECOVERY_CONNECTION_STALE_MS, RECOVERY_NO_RESPONSE_STALE_MS,
    recoverConversationLoad: async (_context, read) => read(),
  };
  const scope = vm.createContext(sandbox);
  vm.runInContext('async function readExistingRoute() { if (request.method === "POST" && url.pathname === "/v1/turns/recover") {' + route + '\n}', scope);
  try {
    await assert.rejects(scope.readExistingRoute(), error => error.code === "CHATGPT_RECOVERY_PENDING" && error.statusCode === 409);
    assert.equal(scope.busyCount, 0); assert.equal(context.busy, false);
    assert.equal(context.pendingSubmission, receipt); assert.equal(context.chatUrl, url);
    assert.equal(scope.turnActivity.get(identity).active, false);
    assert.equal(scope.turnActivity.get(identity).stage, "recovery-error");
    assert.equal(await window.webContents.executeJavaScript("window.fixtureSends"), 0);
    assert.equal((await isolated.cookies.get({ url, name: "fixture-login" }))[0].value, "preserved");
    assert.equal(sentResponses, 0); assert.ok(persisted > 0);
    fs.writeFileSync(proof, JSON.stringify({ actualElectronReader: true, productionRecoveryRoute: true,
      boundedReadReleasedBusySlot: true, acceptedReceiptAndCookiePreserved: true,
      promptsSubmitted: 0, externalRequests: 0 }, null, 2));
  } finally { if (!window.isDestroyed()) window.destroy(); }
}
main().then(() => app.quit()).catch(error => { if (proof) fs.writeFileSync(proof, JSON.stringify({ failed: String(error.stack || error) })); app.exit(1); });
