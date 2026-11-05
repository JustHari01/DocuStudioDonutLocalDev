"use strict";

const { app, BrowserWindow, session } = require("electron");
const assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), vm = require("node:vm");
const src = process.env.DOCURANDOM_REPAIR_TEST_SRC || path.join(__dirname, "../src");
const proof = process.env.DOCURANDOM_REPAIR_TEST_PROOF;
const { executePageProbe, pageSnapshot } = require(path.join(src, "automation.cjs"));
const code = fs.readFileSync(path.join(src, "main.cjs"), "utf8").replace(/\r\n/g, "\n");
const body = code.split("async function repairBrowserSession() {")[1]?.split("\n}\n")[0];
assert.ok(body, "Production repair function must be tested");
app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "DocuRandomRepairProof-")));
app.on("window-all-closed", () => {});

async function main() {
  await app.whenReady();
  const partition = "session-repair-fixture";
  const isolated = session.fromPartition(partition);
  const url = "https://chatgpt.com/";
  isolated.protocol.handle("https", () => new Response('<main><form><div contenteditable="true" role="textbox" aria-label="Ask ChatGPT" style="height:30px"></div><button data-testid="send-button">Send</button></form></main>', { headers: { "Content-Type": "text/html" } }));
  await isolated.cookies.set({ url, name: "fixture-login", value: "preserved", httpOnly: true });
  const windows = [];
  let loaded;
  const createMainWindow = () => {
    const window = new BrowserWindow({ show: false, width: 900, height: 700,
      webPreferences: { session: isolated, sandbox: true, contextIsolation: true, nodeIntegration: false, backgroundThrottling: false } });
    windows.push(window);
    loaded = window.loadURL(url);
    return window;
  };
  const old = createMainWindow();
  await loaded;
  await assert.rejects(executePageProbe(old, "new Promise(() => {})", { timeoutMs: 100 }), { code: "CHATGPT_PAGE_PROBE_TIMEOUT" });
  await assert.rejects(pageSnapshot(old), { code: "CHATGPT_PAGE_PROBE_IN_FLIGHT" });
  const receipt = { submissionAccepted: true, assistantCount: 4 };
  const attachment = { status: "selected" };
  const conversation = { pendingSubmission: receipt, pendingAttachment: attachment, chatUrl: url + "c/exact-fixture" };
  let suspended = 0, persisted = 0;
  let freshProbes = 0;
  const sandbox = { busyCount: 1, mainWindow: old, authenticated: true, PARTITION: partition,
    CHAT_URL: url, session, Date, Object, setTimeout, createMainWindow,
    conversationContexts: () => [conversation],
    suspendConversationWindow: () => { suspended++; },
    persistState: () => { persisted++; },
    pageSnapshot: async (window, options) => {
      await loaded;
      freshProbes++;
      if (freshProbes === 1) throw Object.assign(new Error("Authentication probe still running"), { code: "CHATGPT_PAGE_PROBE_IN_FLIGHT" });
      return pageSnapshot(window, options);
    },
  };
  const context = vm.createContext(sandbox);
  vm.runInContext("async function repairBrowserSession() {" + body + "\n}\n", context);
  try {
    await assert.rejects(context.repairBrowserSession(), error => error.statusCode === 409);
    assert.equal(context.mainWindow, old); assert.equal(suspended, 0);
    context.busyCount = 0;
    const result = await context.repairBrowserSession();
    assert.equal(result.ready, true); assert.equal(result.authenticated, true);
    assert.notEqual(context.mainWindow, old); assert.equal(old.isDestroyed(), true);
    assert.equal(context.mainWindow.webContents.session, isolated);
    assert.equal((await isolated.cookies.get({ url, name: "fixture-login" }))[0].value, "preserved");
    assert.equal(conversation.pendingSubmission, receipt); assert.equal(conversation.pendingAttachment, attachment);
    assert.equal(conversation.chatUrl, url + "c/exact-fixture");
    assert.equal(persisted, 1); assert.equal(suspended, 1);
    assert.equal(freshProbes, 2);
    const resultProof = { actualElectronRendererReplaced: true, staleProbeReproduced: true,
      persistentSessionAndCookieKept: true, pendingReceiptsAndConversationKept: true,
      busyRepairRejected: true, concurrentAuthenticationProbeRetried: true,
      externalRequests: 0, promptsSubmitted: 0 };
    if (proof) fs.writeFileSync(proof, JSON.stringify(resultProof, null, 2));
  } finally { for (const window of windows) if (!window.isDestroyed()) window.destroy(); }
}

main().then(() => app.quit()).catch(error => {
  if (proof) fs.writeFileSync(proof, JSON.stringify({ failed: String(error.stack || error) }));
  app.exit(1);
});
