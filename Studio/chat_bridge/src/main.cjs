"use strict";

const { app, BrowserWindow, dialog, Menu, session } = require("electron");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const {
  CHAT_URL,
  chatTurnSnapshot,
  generatedImageSnapshot,
  ensureSavedChat,
  prepareWindowForBackground,
  pageSnapshot,
  chatGptRateLimitError,
  composerValue,
  stopStalledImage,
  pendingAttachmentUiState,
  pendingAttachmentCleared,
  recoverInterruptedTurn,
  reloadChatGptPage,
  runTurn,
  waitForComposer,
} = require("./automation.cjs");
const {
  codexTurnIdentity,
  codexModelCatalog,
  isCodexResponsesRequest,
  latestUserInput,
  requestInput,
} = require("./protocol.cjs");
const { prepareCodexPromptTransport } = require("./prompt-transport.cjs");
const { installNavigationMenu, isExactChatConversationUrl } = require("./navigation.cjs");
const { loadConversationUrl } = require("./window-load.cjs");
const {
  LOOPBACK_HOST,
  assertLoopbackAddress,
  createAccessToken,
  isAllowedNavigation,
  tokenMatches,
} = require("./security.cjs");

const { accountCapabilityFailure, readAccountCapabilities } = require("./account-capability.cjs");
const { runImageTurnWithOneRetry, existingImageResult } = require("./image-retry.cjs");
const { readExistingImage } = require("./existing-image-read.cjs");
const { readConversationImageSource } = require('./conversation-image-source.cjs');
const { inspectImageHistory } = require("./image-history-view.cjs");
const { preSubmitAttachmentProof } = require("./pre-submit-attachment.cjs");
const { retireFailedRenderer, suspendFailedConversationReader, replaceFailedIdleReader } = require("./renderer-recovery.cjs");
const { ConversationHealth } = require("./conversation-health.cjs");
const { withConversationLoadRecovery } = require("./conversation-load-recovery.cjs");
const { beginRecoveryReadDeadline, recoveryReadError, createAcceptedResponseLease,
  RECOVERY_CONNECTION_STALE_MS, RECOVERY_NO_RESPONSE_STALE_MS } = require("./recovery-read-deadline.cjs");
const { checkpointReceiptProof, receiptFromCheckpoint } = require("./checkpoint-receipt.cjs");
const { queuedTurnProof, baselineUserHash, completedBaselineProof } = require("./queued-turn-proof.cjs");
const { recoverFirstMasterHome } = require("./first-master-home-recovery.cjs");
const { captureEarlyMasterFailure, normalizeEarlyMasterFailure, earlyMasterProof } = require("./early-master-recovery.cjs");
const { homeConversationProof } = require("./home-conversation-proof.cjs");
const { idleImageRetryProof } = require("./idle-image-retry.cjs");
const { repairStalledImage, imageReceiptProof, providerHold } = require('./stalled-image-recovery.cjs');
const {providerDispatchFields,noteProviderDispatch}=require('./submission-dispatch.cjs');
const VERSION = "1.0.158";
const DEFAULT_PORT = 17841;
const MAX_BODY_BYTES = 32 * 1024 * 1024;
const MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024;
// Twenty fully isolated pages let a 64 GB production workstation run twenty
// workspaces at once. Logical saved conversations remain unlimited; extra idle
// pages are suspended LRU-style and reopened at their saved chat URL as needed.
const MAX_CONVERSATIONS = 20;
const CHATGPT_AUTH_WAIT_TIMEOUT_MS = 30_000;
const CHATGPT_AUTH_RETRY_DELAY_MS = 250;
// If the local client loses only its HTTP response socket, keep the accepted
// browser turn alive long enough to finish and make the structured result
// retrievable by its exact thread/turn identity.  This prevents a TCP reset
// from forcing the caller to submit the same prompt a second time.
const DISCONNECTED_RESPONSE_GRACE_MS = 10 * 60 * 1000;
// Before onSubmitted, no browser click has been checkpointed.  Do not keep a
// dead client holding the global turn slot for ten minutes in that phase.
const DISCONNECTED_BEFORE_SUBMISSION_GRACE_MS = 90 * 1000;
// This is a renderer/bridge liveness guard, not a response-duration limit.
// ChatGPT may think for an arbitrary amount of time as long as the bridge keeps
// emitting status/progress callbacks.  If every callback stops, release the
// slot instead of leaving one workspace active forever.
const TURN_HEARTBEAT_STALE_MS = 3 * 60 * 1000;
const TURN_HEARTBEAT_CHECK_MS = 5 * 1000;
const TURN_RESULT_TTL_MS = 15 * 60 * 1000;
const PARTITION = "persist:docurandom-chatgpt";
const SMOKE_MODE = process.argv.includes("--bridge-smoke-test");
const CHAT_BRIDGE_HOME = path.resolve(
  process.env.DOCURANDOM_CHAT_BRIDGE_HOME
    || path.join(
      process.env.LOCALAPPDATA || app.getPath("appData"),
      "DocuRandomStudio",
      "chat-bridge",
    ),
);
const PROFILE_HOME = path.resolve(
  process.env.DOCURANDOM_CHAT_BRIDGE_PROFILE
    || path.join(CHAT_BRIDGE_HOME, "profile"),
);
const CONFIG_PATH = path.join(CHAT_BRIDGE_HOME, "config.json");
const DESCRIPTOR_PATH = path.join(CHAT_BRIDGE_HOME, "runtime", "bridge.json");
const STATE_PATH = path.join(CHAT_BRIDGE_HOME, "bridge-state.json");
const conversationLinkJournal=require('./conversation-links.cjs').createConversationLinkJournal(path.join(CHAT_BRIDGE_HOME,'conversation-links.jsonl'));

app.setName("DocuRandom Chat Bridge");
if (process.platform === "win32") app.setAppUserModelId("local.docurandom.chatbridge");
fs.mkdirSync(PROFILE_HOME, { recursive: true, mode: 0o700 });
app.setPath("userData", PROFILE_HOME);
app.commandLine.appendSwitch("disable-features", "HardwareMediaKeyHandling");

let mainWindow = null;
let apiServer = null;
let quitting = false;
let bridgePort = DEFAULT_PORT;
let authenticated = false;
let authenticationWait = null;
let busyCount = 0;
const accessToken = createAccessToken();
const conversations = new Map();
const turnConversations = new Map();
const turnActivity = new Map();
const turnControllers = new Map();
const turnResults = new Map();
const allContexts = new Set();
const savedTurns = new Map();
const imageReadOnlyThreads = new Map();
const retiredImageReceipts = new Map();
const retiredTextReceipts = new Map();
const imageRecoveryObservations = new Map();
const existingImageReaders = new Set();
// Signed-in Web read headers are held only in memory; never logged/persisted.
let conversationReadHeaders = {};
const conversationHealth = new ConversationHealth();

function connectionHealth(context) {
  return conversationHealth.snapshot(context?.chatUrl, context?.window?.webContents?.id);
}

async function recoverConversationLoad(context, operation, options = {}) {
  try { return await withConversationLoadRecovery(context, operation, {
    ...options,
    health: () => connectionHealth(context),
    reopen: async () => {
      // Only the operation owner reaches this path after an explicit load
      // error. Keep all durable locks and the same persistent browser session.
      suspendConversationWindow(context);
      await ensureConversationWindow(context);
    },
  }); } catch (error) {
    // This is the operation owner's failure path, not an external diagnostic
    // racing a live turn. No Send/Stop action or receipt change is performed.
    suspendFailedConversationReader(context, error);
    throw error;
  }
}

function latestActiveTurnActivityUpdatedAt() {
  let latest = 0;
  for (const activity of turnActivity.values()) {
    if (!activity || activity.active !== true) continue;
    const updatedAt = Number(activity.progressUpdatedAt || 0);
    if (Number.isFinite(updatedAt) && updatedAt > latest) latest = updatedAt;
  }
  return latest;
}

function latestActiveTurnUpdatedAt() {
  let latest = 0;
  for (const activity of turnActivity.values()) {
    if (!activity || activity.active !== true) continue;
    const updatedAt = Number(activity.updatedAt || 0);
    if (Number.isFinite(updatedAt) && updatedAt > latest) latest = updatedAt;
  }
  return latest;
}

function latestActiveTurnNoResponseEvidence() {
  for (const activity of turnActivity.values()) {
    if (activity && activity.active === true && activity.noResponseEvidence === true) {
      return true;
    }
  }
  return false;
}

function validTurnIdentity(value) {
  return /^(?:thread|cache):[A-Za-z0-9_-]{8,160}$/.test(String(value || ""));
}

function validTurnId(value) {
  return /^[A-Za-z0-9_-]{8,160}$/.test(String(value || ""));
}

function normalizeTurnIdentity(value) {
  const text = String(value || "").trim();
  if (validTurnIdentity(text)) return text;
  if (validTurnId(text)) return `thread:${text}`;
  return "";
}

function turnResultKey(threadId, turnId) {
  const identity = validTurnIdentity(threadId)
    ? String(threadId)
    : validTurnId(threadId)
      ? `thread:${threadId}`
      : "";
  if (!identity || !validTurnId(turnId)) return "";
  return `${identity}:${turnId}`;
}

function rememberTurnResult(threadId, turnId, value) {
  const key = turnResultKey(threadId, turnId);
  if (!key || !value || typeof value !== "object") return;
  turnResults.set(key, { value, updatedAt: Date.now() });
  const cutoff = Date.now() - TURN_RESULT_TTL_MS;
  for (const [candidate, item] of turnResults) {
    if (Number(item?.updatedAt || 0) < cutoff) turnResults.delete(candidate);
  }
}

function normalizePendingSubmission(value) {
  if (!value || typeof value !== "object") return null;
  const outputMode = String(value.outputMode || "").trim() === "generated_image"
    ? "generated_image"
    : "text";
  return {
    schemaVersion: 2,
    deliveryRetryCount: Math.min(1, Math.max(0, Number(value.deliveryRetryCount) || 0)),
    assistantCount: Math.max(0, Number(value.assistantCount) || 0),
    userCount: Math.max(0, Number(value.userCount) || 0),
    assistantKey: String(value.assistantKey || ""),
    userKey: String(value.userKey || ""),
    generatedImageKeys: Array.isArray(value.generatedImageKeys)
      ? value.generatedImageKeys.map(item => String(item)).slice(0, 200)
      : [],
    outputMode,
    captureGeneratedImage: outputMode === "generated_image"
      || value.captureGeneratedImage === true,
    sendAttempted: value.sendAttempted === true,
    submissionAccepted: value.submissionAccepted === true,
    createdAt: Math.max(0, Number(value.createdAt) || 0),
    acceptedAt: Math.max(0, Number(value.acceptedAt) || 0),
    acceptanceSignal: String(value.acceptanceSignal || "").slice(0, 80),
    ...providerDispatchFields(value),
    ...(/^[a-f0-9]{64}$/.test(value.promptSha256 || '') ? {promptSha256:value.promptSha256} : {}),
    ...(/^[a-f0-9]{64}$/.test(value.requestPromptSha256 || '') ? {requestPromptSha256:value.requestPromptSha256} : {}),
    ...(/^[a-f0-9]{64}$/.test(value.beforeUserTextSha256 || '') ? {beforeUserTextSha256:value.beforeUserTextSha256} : {}),
    ...(typeof value.acceptedUserKey==='string' && value.acceptedUserKey ? {acceptedUserKey:value.acceptedUserKey.slice(0,500)} : {}),
  };
}

const ATTACHMENT_STAGING_ROOT = path.join(CHAT_BRIDGE_HOME, "pending-attachments");

function normalizePendingAttachment(value) {
  if (!value || typeof value !== "object") return null;
  const status = String(value.status || "");
  if (!["prepared", "selection-attempted", "confirmed"].includes(status)) return null;
  const files = Array.isArray(value.files) ? value.files : [];
  if (!files.length || files.length > 8) return null;
  const root = path.resolve(ATTACHMENT_STAGING_ROOT).toLowerCase() + path.sep;
  const safeFiles = files.map(item => ({
    source: String(item?.source || ""),
    target: String(item?.target || ""),
    sha256: String(item?.sha256 || ""),
    size: Number(item?.size),
    path: path.resolve(String(item?.path || "")),
  }));
  if (safeFiles.some(item => !item.source || !item.target
      || !/^[a-f0-9]{64}$/.test(item.sha256)
      || !Number.isSafeInteger(item.size) || item.size < 0
      || !item.path.toLowerCase().startsWith(root))) return null;
  return {
    status,
    chatUrl: String(value.chatUrl || ""),
    files: safeFiles,
    updatedAt: Number(value.updatedAt) || Date.now(),
  };
}

function pendingAttachmentMatches(record, attachments) {
  if (!record || !Array.isArray(attachments) || record.files.length !== attachments.length) return false;
  return attachments.every((item, index) => {
    const bytes = Buffer.from(String(item.content_base64 || ""), "base64");
    return record.files[index].source === String(item.name || "")
      && record.files[index].size === bytes.length
      && record.files[index].sha256 === crypto.createHash("sha256").update(bytes).digest("hex");
  });
}

function pendingAttachmentInCurrentChat(record, window) {
  const current = String(window?.webContents?.getURL?.() || "");
  try {
    const expectedUrl = new URL(record.chatUrl);
    const currentUrl = new URL(current);
    return expectedUrl.origin === currentUrl.origin
      && expectedUrl.pathname === currentUrl.pathname
      && (isExactChatConversationUrl(record.chatUrl) || expectedUrl.pathname === "/");
  } catch { return false; }
}

async function replaceMissingPreSubmitAttachment(context, record) {
  // The staged bytes belong to a different request, and there is no durable
  // send marker. Reload only this same conversation before considering a new
  // file. A blank composer in two probes proves the stale card was cleared;
  // an ambiguous or partial page leaves the checkpoint untouched.
  if (context.pendingSubmission || !pendingAttachmentInCurrentChat(record, context.window)) return false;
  try {
    await reloadChatGptPage(context.window, {
      reason: "Tệp cũ chưa gửi không còn khớp yêu cầu hiện tại",
    });
    await waitForComposer(context.window, { timeoutMs: 20_000, maxPageReloads: 0 });
    const first = await pendingAttachmentUiState(context.window, record.files);
    if (!pendingAttachmentCleared(first)) return false;
    await new Promise(resolve => setTimeout(resolve, 500));
    const second = await pendingAttachmentUiState(context.window, record.files);
    if (!pendingAttachmentCleared(second)) return false;
    // Retain staged bytes for diagnosis; only the obsolete association is
    // removed. The current request will stage its own file exactly once.
    context.pendingAttachment = null;
    return true;
  } catch {
    return false;
  }
}

function discardPendingAttachment(context) {
  const record = context?.pendingAttachment;
  if (!record) return;
  context.pendingAttachment = null;
  for (const folder of new Set(record.files.map(item => path.dirname(item.path)))) {
    try { fs.rmSync(folder, { recursive: true, force: true }); } catch {}
  }
}

function loadSavedTurns() {
  try {
    const value = JSON.parse(fs.readFileSync(STATE_PATH, "utf8"));
    const turns = value && typeof value === "object" ? value.savedTurns : null;
    if (!turns || typeof turns !== "object" || Array.isArray(turns)) return;
    for(const [identity,record] of Object.entries(value.imageReadOnlyThreads||{})) {
      if(validTurnIdentity(identity) && isExactChatConversationUrl(record?.chatUrl)
        && record.chatUrl===turns[identity]?.chatUrl)imageReadOnlyThreads.set(identity,record);
    }
    for(const [key,record] of Object.entries(value.retiredImageReceipts||{})){
      if(record && validTurnIdentity(record.thread) && isExactChatConversationUrl(record.chatUrl)
        && normalizePendingSubmission(record.pendingSubmission))retiredImageReceipts.set(key,record);
    }
    for(const [key,record] of Object.entries(value.retiredTextReceipts||{})){
      if(record&&validTurnIdentity(record.thread)&&isExactChatConversationUrl(record.chatUrl)
        &&normalizePendingSubmission(record.pendingSubmission))retiredTextReceipts.set(key,record);
    }
    for (const [key,record] of Object.entries(value.imageRecoveryObservations || {})) {
      if (record && typeof record.binding === 'string' && Number.isFinite(record.firstSeenAt))
        imageRecoveryObservations.set(key,record);
    }
    for (const [identity, item] of Object.entries(turns)) {
      if (!validTurnIdentity(identity) || !item || typeof item !== "object") continue;
      const chatUrl = String(item.chatUrl || "");
      if (!chatUrl || !isAllowedNavigation(chatUrl)) continue;
      savedTurns.set(identity, {
        chatUrl,
        pendingSubmission: normalizePendingSubmission(item.pendingSubmission),
        pendingAttachment: normalizePendingAttachment(item.pendingAttachment),
        earlyMasterFailure: normalizeEarlyMasterFailure(item.earlyMasterFailure),
        touchedAt: Number(item.touchedAt) || Date.now(),
      });
    }
  } catch {}
}

loadSavedTurns();

function atomicJson(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  const temporary = `${filePath}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
  fs.renameSync(temporary, filePath);
  try { fs.chmodSync(filePath, 0o600); } catch {}
}

function bridgeState() {
  for(const [identity,value]of savedTurns)conversationLinkJournal.record(identity,value.chatUrl);
  const recentSavedTurns = [...savedTurns.entries()]
    .sort((left, right) => Number(right[1]?.touchedAt || 0) - Number(left[1]?.touchedAt || 0))
    .slice(0, 200);
  return {
    version: 1,
    bridgeVersion: VERSION,
    coreSetupComplete: true,
    authenticated,
    busy: busyCount > 0,
    chatMode: "saved",
    port: bridgePort,
    pid: process.pid,
    savedTurns: Object.fromEntries(recentSavedTurns),
    imageReadOnlyThreads: Object.fromEntries([...imageReadOnlyThreads].slice(-200)),
    retiredImageReceipts: Object.fromEntries([...retiredImageReceipts].slice(-200)),
    retiredTextReceipts: Object.fromEntries([...retiredTextReceipts].slice(-200)),
    imageRecoveryObservations: Object.fromEntries([...imageRecoveryObservations].slice(-200)),
    updatedAt: new Date().toISOString(),
  };
}

function persistState() {
  atomicJson(STATE_PATH, bridgeState());
}

function writeRuntimeFiles() {
  atomicJson(CONFIG_PATH, {
    releaseVersion: VERSION,
    host: LOOPBACK_HOST,
    port: bridgePort,
    browserHost: "docurandom",
    mode: "browser-only",
    chatMode: "saved",
    solAvailable: true,
    proAvailable: false,
    availableModels: ["chatgpt-web/auto", "chatgpt-web/high"],
  });
  atomicJson(DESCRIPTOR_PATH, {
    version: 1,
    kind: "docurandom-chat-bridge",
    pid: process.pid,
    control: {
      endpoint: `http://${LOOPBACK_HOST}:${bridgePort}`,
      token: accessToken,
    },
  });
  persistState();
}

function secureWebContents(window) {
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (isAllowedNavigation(url)) {
      void window.loadURL(url);
    }
    return { action: "deny" };
  });
  window.webContents.on("will-navigate", (event, url) => {
    if (!isAllowedNavigation(url)) event.preventDefault();
  });
}

function applyAuthenticationSnapshot(snapshot) {
  if (snapshot?.authenticated === true) {
    authenticated = true;
  } else if (snapshot?.signedOut === true) {
    // Only an explicit login marker may invalidate a previously confirmed
    // session. A missing composer, blank shell, auth redirect, or probe error
    // is a normal renderer transition and must not become a false 401.
    authenticated = false;
  }
  persistState();
  return authenticated;
}

async function updateAuthentication(window = mainWindow) {
  if (!window || window.isDestroyed()) return authenticated;
  try {
    const snapshot = await pageSnapshot(window);
    applyAuthenticationSnapshot(snapshot);
    if (
      !SMOKE_MODE
      && snapshot?.signedOut === true
      && process.argv.includes("--hidden")
    ) window.show();
  } catch {
    // A page probe timeout means the renderer is busy or hydrating. Keep the
    // last confirmed auth state and let the request gate retry below.
  }
  return authenticated;
}

async function waitForAuthenticatedSession({ timeoutMs = CHATGPT_AUTH_WAIT_TIMEOUT_MS } = {}) {
  if (authenticated) return true;
  if (authenticationWait) return authenticationWait;
  authenticationWait = (async () => {
    const deadline = Date.now() + Math.max(1_000, Number(timeoutMs) || CHATGPT_AUTH_WAIT_TIMEOUT_MS);
    while (Date.now() < deadline) {
      if (authenticated) return true;
      if (!mainWindow || mainWindow.isDestroyed()) return false;
      let snapshot = null;
      try {
        snapshot = await pageSnapshot(mainWindow, {
          timeoutMs: Math.min(5_000, Math.max(500, deadline - Date.now())),
        });
      } catch {
        // Keep polling. The page probe timeout is recoverable while the main
        // ChatGPT shell is loading and must not immediately reject a turn.
      }
      if (snapshot?.authenticated === true) {
        applyAuthenticationSnapshot(snapshot);
        return true;
      }
      if (snapshot?.signedOut === true) {
        applyAuthenticationSnapshot(snapshot);
        return false;
      }
      const remaining = deadline - Date.now();
      if (remaining <= 0) break;
      await new Promise(resolve => setTimeout(
        resolve,
        Math.min(CHATGPT_AUTH_RETRY_DELAY_MS, remaining),
      ));
    }
    return authenticated;
  })().finally(() => {
    authenticationWait = null;
  });
  return authenticationWait;
}

async function repairBrowserSession() {
  if (busyCount > 0) {
    throw Object.assign(
      new Error("ChatGPT Web đang tạo nội dung; hãy đợi tác vụ hoàn tất rồi bấm Sửa lại"),
      { statusCode: 409 },
    );
  }
  // Preserve every cookie, login token, local-storage item and saved ChatGPT
  // conversation. Only live renderer pages and disposable HTTP/code cache are
  // rebuilt, which is enough to recover a black, frozen or half-loaded shell.
  for (const context of conversationContexts()) suspendConversationWindow(context);
  const browserSession = session.fromPartition(PARTITION);
  await browserSession.clearCache();
  try { browserSession.flushStorageData(); } catch {}
  // Reloading the same webContents keeps an outstanding DOM probe attached to
  // it. Replace that renderer, preserving the persistent session partition.
  // Create the replacement first so repair never leaves the app windowless.
  const previousMainWindow = mainWindow;
  mainWindow = createMainWindow();
  if (previousMainWindow && !previousMainWindow.isDestroyed()) previousMainWindow.destroy();
  const deadline = Date.now() + 90_000;
  let snapshot = null;
  while (Date.now() < deadline) {
    try {
      snapshot = await pageSnapshot(mainWindow, {
        timeoutMs: Math.min(5_000, Math.max(1, deadline - Date.now())),
      });
    } catch (error) {
      // The new page's authentication check may own its first probe. Wait for
      // that probe within this repair deadline instead of failing the repair.
      if (!["CHATGPT_PAGE_PROBE_IN_FLIGHT", "CHATGPT_PAGE_PROBE_TIMEOUT"].includes(error?.code)) throw error;
    }
    if (snapshot?.ready && snapshot?.authenticated) break;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  authenticated = snapshot?.authenticated === true;
  persistState();
  if (!authenticated) {
    mainWindow.show();
    mainWindow.focus();
  }
  return {
    authenticated,
    ready: snapshot?.ready === true,
    url: String(snapshot?.url || mainWindow.webContents.getURL() || ""),
  };
}

function createChatWindow({ visible = false } = {}) {
  const window = new BrowserWindow({
    width: 1220,
    height: 820,
    minWidth: 900,
    minHeight: 640,
    show: visible,
    title: "DocuRandom · Kết nối ChatGPT",
    autoHideMenuBar: false,
    backgroundColor: "#0b0f18",
    webPreferences: {
      partition: PARTITION,
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      backgroundThrottling: false,
      paintWhenInitiallyHidden: true,
      spellcheck: false,
    },
  });
  secureWebContents(window);
  installNavigationMenu(window, Menu);
  return window;
}

function createMainWindow() {
  const visible = !process.argv.includes("--hidden");
  const window = createChatWindow({ visible });
  window.on("close", (event) => {
    if (quitting) return;
    event.preventDefault();
    window.hide();
  });
  window.webContents.on("did-finish-load", () => { void updateAuthentication(window); });
  void window.loadURL(CHAT_URL);
  return window;
}

async function createConversation(chatUrl = CHAT_URL) {
  const context = {
    window: null,
    chatUrl: isExactChatConversationUrl(chatUrl) ? chatUrl : CHAT_URL,
    pendingSubmission: null,
    pendingAttachment: null,
    busy: false,
    opening: false,
    touchedAt: Date.now(),
  };
  allContexts.add(context);
  try {
    await ensureConversationWindow(context);
    return context;
  } catch (error) {
    allContexts.delete(context);
    throw error;
  }
}

function saveTurnContext(identity, context) {
  if (!validTurnIdentity(identity) || !context) return;
  rememberConversationUrl(context);
  const chatUrl = context.chatUrl && isAllowedNavigation(context.chatUrl)
    ? context.chatUrl
    : CHAT_URL;
  conversationLinkJournal.record(identity,chatUrl);
  savedTurns.set(identity, {
    chatUrl,
    pendingSubmission: normalizePendingSubmission(context.pendingSubmission),
    pendingAttachment: normalizePendingAttachment(context.pendingAttachment),
    earlyMasterFailure: normalizeEarlyMasterFailure(Object.hasOwn(context, "earlyMasterFailure")
      ? context.earlyMasterFailure : savedTurns.get(identity)?.earlyMasterFailure),
    touchedAt: Date.now(),
  });
}

function activeConversationCount() {
  return conversationContexts().size;
}

function conversationContexts() {
  return new Set([
    ...allContexts,
    ...conversations.values(),
    ...turnConversations.values(),
  ]);
}

function contextWindowUsable(context) {
  return Boolean(context?.window && !context.window.isDestroyed());
}

function liveConversationCount() {
  return [...conversationContexts()].filter(contextWindowUsable).length;
}

function rememberConversationUrl(context) {
  if (!contextWindowUsable(context)) return;
  const current = String(context.window.webContents.getURL() || "");
  // The home URL is a transient shell during a new-chat SPA navigation. It
  // must never overwrite a previously known /c/<id> URL, or a later recovery
  // would reopen the home page and lose the workspace's conversation binding.
  if (isExactChatConversationUrl(current)) context.chatUrl = current;
}

function suspendConversationWindow(context) {
  if (!contextWindowUsable(context)) {
    context.window = null;
    return;
  }
  rememberConversationUrl(context);
  context.window.destroy();
  context.window = null;
}

function evictLeastRecentlyUsedIdleWindow(excludedContext = null) {
  const candidate = [...conversationContexts()]
    .filter(context => (
      context !== excludedContext
      && !context.busy
      && !context.opening
      && contextWindowUsable(context)
    ))
    .sort((left, right) => left.touchedAt - right.touchedAt)[0];
  if (!candidate) return false;
  suspendConversationWindow(candidate);
  return true;
}

function canAcceptConversationTurn() {
  return liveConversationCount() < MAX_CONVERSATIONS
    || [...conversationContexts()].some(context => (
      !context.busy && !context.opening && contextWindowUsable(context)
    ));
}

async function ensureConversationWindow(context) {
  if (contextWindowUsable(context)) return context.window;
  context.opening = true;
  while (liveConversationCount() >= MAX_CONVERSATIONS) {
    if (!evictLeastRecentlyUsedIdleWindow(context)) {
      context.opening = false;
      throw Object.assign(
        new Error("All active ChatGPT Web slots are busy; this workspace is waiting for a free slot"),
        { statusCode: 429 },
      );
    }
  }
  const window = createChatWindow({ visible: false });
  context.window = window;
  const remember = () => rememberConversationUrl(context);
  window.webContents.on("did-navigate", remember);
  window.webContents.on("did-navigate-in-page", remember);
  try {
    const target = context.chatUrl && isAllowedNavigation(context.chatUrl)
      ? context.chatUrl
      : CHAT_URL;
    // A navigation promise can remain unresolved while the ChatGPT SPA or
    // network stalls. Bound this pre-submit stage below the caller watchdog;
    // the catch below destroys only this disposable window.
    await loadConversationUrl(window, target);
    // ChatGPT omits parts of its composer/assistant lifecycle from a genuinely
    // hidden Electron page. Keep the window out of the user's way (transparent,
    // click-through and absent from the taskbar), but make the renderer
    // visible to the page so DOM generation and streaming markers hydrate.
    await prepareWindowForBackground(window);
    context.opening = false;
    return window;
  } catch (error) {
    if (!window.isDestroyed()) window.destroy();
    context.window = null;
    context.opening = false;
    throw error;
  }
}

function sendJson(response, status, value) {
  const body = Buffer.from(JSON.stringify(value), "utf8");
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": body.length,
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
  });
  response.end(body);
}

function startNdjson(response) {
  response.writeHead(200, {
    "Content-Type": "application/x-ndjson; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
  });
  response.flushHeaders();
}

function writeNdjson(response, value) {
  if (response.destroyed || response.writableEnded) return false;
  response.write(`${JSON.stringify(value)}\n`, "utf8");
  return true;
}

function startSse(response) {
  response.writeHead(200, {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    Connection: "keep-alive",
  });
  response.flushHeaders();
}

function writeSse(response, type, value, sequence) {
  if (response.destroyed || response.writableEnded) return false;
  const payload = { type, sequence_number: sequence, ...value };
  response.write(`event: ${type}\ndata: ${JSON.stringify(payload)}\n\n`, "utf8");
  return true;
}

function finishSse(response) {
  if (response.destroyed || response.writableEnded) return;
  response.write("data: [DONE]\n\n", "utf8");
  response.end();
}

function readJson(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let total = 0;
    request.on("data", chunk => {
      total += chunk.length;
      if (total > MAX_BODY_BYTES) {
        reject(Object.assign(new Error("Request body exceeds the safety limit"), { statusCode: 413 }));
        request.destroy();
        return;
      }
      chunks.push(chunk);
    });
    request.on("end", () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}"));
      } catch {
        reject(Object.assign(new Error("Request body is not valid JSON"), { statusCode: 400 }));
      }
    });
    request.on("error", reject);
  });
}

function requestAttachments(value,outputMode='') {
  return require('./attachment-request.cjs').requestAttachments(value,outputMode,MAX_ATTACHMENT_BYTES);
}

async function handleResponseRequest(request, response, body) {
  if (typeof body.model !== "string" || !body.model.startsWith("chatgpt-web/")) {
    throw Object.assign(new Error("Unsupported ChatGPT model"), { statusCode: 400 });
  }
  if (!(await waitForAuthenticatedSession())) {
    throw Object.assign(new Error("ChatGPT session is not signed in"), { statusCode: 401 });
  }
  const codexRequest = isCodexResponsesRequest(request, body);
  const wantsGeneratedImage = (
    !codexRequest && body.docurandom_output === "generated_image"
  );
  const expectedExactText = (
    !codexRequest && body.docurandom_expected_exact === "ĐÃ TẠO"
  ) ? "ĐÃ TẠO" : "";
  const requestedPrompt = codexRequest ? latestUserInput(body.input) : requestInput(body.input);
  const transport = codexRequest
    ? prepareCodexPromptTransport(requestedPrompt, { maxAttachmentBytes: MAX_ATTACHMENT_BYTES })
    : {
        mode: "inline",
        prompt: requestedPrompt,
        attachments: requestAttachments(body.attachments,body.docurandom_output),
        charLength: requestedPrompt.length,
        sha256: "",
      };
  const prompt = transport.prompt;
  const attachments = transport.attachments;
  const previousId = typeof body.previous_response_id === "string" ? body.previous_response_id : "";
  const promptCacheKey = typeof body.prompt_cache_key === "string"
    ? body.prompt_cache_key.trim()
    : "";
  // Direct DocuRandom thumbnail requests do not carry Codex's HTTP metadata,
  // but they do carry the same stable prompt_cache_key. Bind them to a saved
  // conversation too, so a broken response socket can be recovered without
  // resending the prompt.
  const turnIdentity = codexRequest
    ? codexTurnIdentity(request.headers, body)
    : (promptCacheKey ? `thread:${promptCacheKey}` : "");
  const rawTurnId = body?.metadata && typeof body.metadata === "object"
    ? body.metadata.turn_id
    : "";
  const turnId = validTurnId(rawTurnId) ? String(rawTurnId) : "";
  if(wantsGeneratedImage && imageReadOnlyThreads.has(turnIdentity)) {
    throw Object.assign(new Error("CHATGPT_IMAGE_READONLY: Only collect the existing thumbnail in this conversation"),{statusCode:409});
  }
  if (existingImageReaders.has(turnIdentity)) {
    throw Object.assign(new Error("This conversation is reading its completed image"), {statusCode:409});
  }
  if (turnIdentity) {
    const now = Date.now();
    turnActivity.set(turnIdentity, {
      active: true,
      stage: "codex-received",
      turn_id: turnId,
       message: transport.mode === "attachment"
         ? `Codex đã chuyển yêu cầu sang chế độ nạp nhanh · ${transport.charLength.toLocaleString("vi-VN")} ký tự…`
         : "Codex đã chuyển yêu cầu sang ChatGPT Web…",
      text: "",
      updatedAt: now,
      progressUpdatedAt: now,
      noResponseEvidence: false,
    });
  }
  const updateTurnActivity = (value, { progress = false } = {}) => {
    if (!turnIdentity) return;
    if (value.stage === 'delivery-retry-reserved') {
      saveTurnContext(turnIdentity, context);
      persistState();
    }
    // A new-chat submission can change the SPA URL after the initial
    // onSubmitted checkpoint was written. Persist that exact /c/<id> route
    // as soon as any later status/progress callback observes it; otherwise a
    // restart/recovery reopens chatgpt.com/ and reads the wrong renderer.
    if (context && contextWindowUsable(context)) {
      rememberConversationUrl(context);
      const saved = savedTurns.get(turnIdentity);
      if (
        isExactChatConversationUrl(context.chatUrl)
        && context.chatUrl !== String(saved?.chatUrl || '')
      ) {
        saveTurnContext(turnIdentity, context);
        persistState();
      }
    }
    const current = turnActivity.get(turnIdentity) || {};
    const now = Date.now();
    turnActivity.set(turnIdentity, {
      ...current,
      ...value,
      updatedAt: now,
      ...(progress ? { progressUpdatedAt: now } : {}),
    });
  };
  let createdContext = false;
  let context = previousId ? conversations.get(previousId) : null;
  if (!context && turnIdentity) context = turnConversations.get(turnIdentity) || null;
  if (!context && turnIdentity) {
    const saved = savedTurns.get(turnIdentity);
    if (saved) {
      context = await createConversation(saved.chatUrl);
      createdContext = true;
      context.pendingSubmission = saved.pendingSubmission || null;
      context.pendingAttachment = saved.pendingAttachment || null;
    }
  }
  if (previousId && !context) {
    throw Object.assign(new Error("The previous ChatGPT response is no longer available"), { statusCode: 409 });
  }
  if (!context) {
    try {
      context = await createConversation();
    } catch (error) {
      // This request has not obtained a renderer yet: no attachment or Send
      // action can have occurred. Preserve exact Master identity even when
      // startup account refresh aborts loadURL before the main turn try/catch.
      const early = captureEarlyMasterFailure({error, prompt, attachments,
        outputMode: body.docurandom_output || 'text', chatUrl: CHAT_URL,
        submissionSeen:false, attachmentSeen:false, pendingSubmission:null, pendingAttachment:null});
      if (turnIdentity && early) {
        const failed = {window:null, chatUrl:CHAT_URL, pendingSubmission:null,
          pendingAttachment:null, earlyMasterFailure:early, busy:false, opening:false, touchedAt:Date.now()};
        turnConversations.set(turnIdentity, failed);
        saveTurnContext(turnIdentity, failed);
        persistState();
      }
      throw error;
    }
    createdContext = true;
  }
  if (context.pendingSubmission) {
    updateTurnActivity({
      active: false,
      stage: "recovery-required",
      message: "Workspace còn phản hồi ChatGPT chưa thu hồi; phải nối DOM trước khi gửi tin mới.",
    });
    throw Object.assign(
      new Error("This workspace has an interrupted ChatGPT turn; recover its DOM before sending another prompt"),
      { statusCode: 409 },
    );
  }
  if (context.busy) throw Object.assign(new Error("This ChatGPT conversation is already running"), { statusCode: 409 });
  await ensureConversationWindow(context);
  let resumeAttachment = null;
  if (context.pendingAttachment) {
    const pending = context.pendingAttachment;
    if (!pendingAttachmentMatches(pending, attachments)
        || !pendingAttachmentInCurrentChat(pending, context.window)) {
      if (!(await replaceMissingPreSubmitAttachment(context, pending))) {
        throw Object.assign(new Error(
          "Previous ChatGPT attachment is unresolved in its exact conversation; no new file or prompt was sent"
        ), { statusCode: 409 });
      }
      if (turnIdentity) saveTurnContext(turnIdentity, context);
      persistState();
    }
    if (context.pendingAttachment && pending.status === "prepared") {
      // The marker was committed before DOM.setFileInputFiles was dispatched.
      discardPendingAttachment(context);
      if (turnIdentity) saveTurnContext(turnIdentity, context);
      persistState();
    } else if (context.pendingAttachment) {
      resumeAttachment = pending;
    }
  }

  context.busy = true;
  context.touchedAt = Date.now();
  busyCount += 1;
  persistState();
  const controller = new AbortController();
  const acceptedResponseLease = createAcceptedResponseLease(controller);
  if (turnIdentity) {
    turnControllers.set(turnIdentity, { controller, turnId });
  }
  let completed = false;
  let submissionSeen = false;
  let attachmentSeen = Boolean(context.pendingAttachment);
  let responseDisconnected = false;
  let disconnectTimer = null;
  let heartbeatTimer = null;
  const streaming = body.stream === true;
  const responseId = `resp_docurandom_${crypto.randomUUID().replaceAll("-", "")}`;
  const createdAt = Math.floor(Date.now() / 1000);
  const messageId = `msg_docurandom_${crypto.randomUUID().replaceAll("-", "")}`;
  let sequence = 0;
  let sseMessageStarted = false;
  let streamedText = "";
  const scheduleDisconnectedAbort = () => {
    if (disconnectTimer !== null) clearTimeout(disconnectTimer);
    // Selecting a file is still pre-submit. A disconnected client must not
    // hold its browser slot for ten minutes merely because the file card was
    // staged; preserve the durable marker and release the turn promptly.
    const graceMs = context.pendingSubmission
      ? DISCONNECTED_RESPONSE_GRACE_MS
      : DISCONNECTED_BEFORE_SUBMISSION_GRACE_MS;
    disconnectTimer = setTimeout(() => {
      if (!completed && responseDisconnected) controller.abort();
    }, graceMs);
  };
  response.once("close", () => {
    if (completed) return;
    responseDisconnected = true;
    updateTurnActivity({
      active: true,
      stage: "client-disconnected",
      message: "Kênh trả kết quả vừa ngắt; tiếp tục giữ đúng lượt ChatGPT để thu hồi kết quả, không gửi lại prompt.",
    });
    scheduleDisconnectedAbort();
  });
  if (turnIdentity) {
    heartbeatTimer = setInterval(() => {
      if (completed || controller.signal.aborted) return;
      const activity = turnActivity.get(turnIdentity);
      const updatedAt = Number(activity?.updatedAt || 0);
      if (!updatedAt || Date.now() - updatedAt < TURN_HEARTBEAT_STALE_MS) return;
      updateTurnActivity({
        active: true,
        stage: "bridge-heartbeat-stale",
        message: "ChatGPT Web Bridge không còn heartbeat; đã dừng đúng lượt để giải phóng hàng đợi, không gửi lại prompt.",
      });
      controller.abort();
    }, TURN_HEARTBEAT_CHECK_MS);
    heartbeatTimer.unref?.();
  }
  try {
    if (streaming) {
      if (codexRequest) {
        startSse(response);
        writeSse(response, "response.created", {
          response: {
            id: responseId,
            object: "response",
            created_at: createdAt,
            status: "in_progress",
            model: body.model,
            output: [],
            usage: null,
          },
        }, sequence++);
      } else {
        startNdjson(response);
      }
    }
    const runCurrentTurn = (imageRetry = false) => recoverConversationLoad(context, () => runTurn(context.window, {
      model: body.model,
      prompt,
      attachments: imageRetry ? [] : attachments,
      captureGeneratedImage: wantsGeneratedImage,
      checkProviderAvailability: wantsGeneratedImage ? () => providerHold(connectionHealth(context)) : undefined,
      checkSubmissionAccepted: async ({promptSha256,baseline,signal}) => {
        const observedUrl=context.window.webContents.getURL();
        const proof=await require('./conversation-image-source.cjs').readConversationSubmissionSource({
          chatUrl:observedUrl,promptSha256,baseline,signal,
          fetch:(address,options)=>session.fromPartition(PARTITION).fetch(address,options),headers:conversationReadHeaders});
        return context.window.webContents.getURL()===observedUrl?proof:null;
      },
      expectedExactText,
      outputMode: typeof body.docurandom_output === "string"
        ? body.docurandom_output
        : "",
      signal: controller.signal,
      resumeAttachment: imageRetry ? null : resumeAttachment,
      recoverPendingAttachment: async record => {
        const cleared = await replaceMissingPreSubmitAttachment(context, record);
        if (cleared) {
          if (turnIdentity) saveTurnContext(turnIdentity, context);
          persistState();
        }
        return cleared;
      },
      attachmentStagingRoot: ATTACHMENT_STAGING_ROOT,
      onAttachmentState: state => {
        if (state) attachmentSeen = true;
        context.pendingAttachment = state ? normalizePendingAttachment({
          ...state,
          chatUrl: String(context.window.webContents.getURL?.() || context.chatUrl),
        }) : null;
        if (state && !context.pendingAttachment) {
          throw new Error("Bridge không lưu được trạng thái tệp đính kèm an toàn");
        }
        if (turnIdentity) {
          turnConversations.set(turnIdentity, context);
          saveTurnContext(turnIdentity, context);
        }
        persistState();
      },
      onProgress: progress => {
        acceptedResponseLease.progress(progress?.text);
        updateTurnActivity({
          active: true,
          noResponseEvidence: progress?.noResponseEvidence === true,
        }, { progress: true });
      },
      onStatus: streaming ? (stage, message) => {
        updateTurnActivity({ active: true, stage, message }, {
          progress: stage === "inline-import-progress",
        });
        if (!codexRequest) {
          writeNdjson(response, {
            type: "response.status",
            stage,
            message,
          });
        }
      } : undefined,
      onPartial: streaming ? partial => {
        updateTurnActivity({ active: true, text: partial }, { progress: true });
        if (partial === streamedText) return;
        if (codexRequest) {
          if (!sseMessageStarted) {
            const item = {
              type: "message", id: messageId, status: "in_progress", role: "assistant", content: [],
            };
            writeSse(response, "response.output_item.added", {
              output_index: 0, item,
            }, sequence++);
            writeSse(response, "response.content_part.added", {
              item_id: messageId,
              output_index: 0,
              content_index: 0,
              part: { type: "output_text", text: "", annotations: [] },
            }, sequence++);
            sseMessageStarted = true;
          }
          const delta = partial.startsWith(streamedText) ? partial.slice(streamedText.length) : partial;
          if (delta) {
            writeSse(response, "response.output_text.delta", {
              item_id: messageId,
              output_index: 0,
              content_index: 0,
              delta,
            }, sequence++);
          }
        } else if (partial.startsWith(streamedText)) {
          writeNdjson(response, {
            type: "response.output_text.delta",
            delta: partial.slice(streamedText.length),
          });
        } else {
          writeNdjson(response, {
            type: "response.output_text.snapshot",
            text: partial,
          });
        }
        streamedText = partial;
      } : undefined,
      onSubmitted: evidence => {
        submissionSeen = true;
        context.earlyMasterFailure = null;
        const dispatch = context.pendingSubmission?.createdAt===evidence.createdAt
          ? providerDispatchFields(context.pendingSubmission) : {};
        context.pendingSubmission = normalizePendingSubmission({...evidence,...dispatch,
          requestPromptSha256:/^[a-f0-9]{64}$/.test(body.docurandom_prompt_sha256||'')?body.docurandom_prompt_sha256:
            crypto.createHash('sha256').update(prompt.normalize('NFC').replace(/\s+/g,' ').trim()).digest('hex')});
        acceptedResponseLease.accept(context.pendingSubmission);
        updateTurnActivity({
          active: true,
          stage: "submitted",
          message: "ChatGPT đã nhận prompt; đang chờ DOM phản hồi, không gửi lại prompt.",
        }, { progress: true });
        if (responseDisconnected) scheduleDisconnectedAbort();
        if (turnIdentity) {
          turnConversations.set(turnIdentity, context);
          saveTurnContext(turnIdentity, context);
          persistState();
        }
      },
    }), {
      signal: controller.signal,
      onStatus: (stage, message) => updateTurnActivity({ active: true, stage, message }),
      recoverAccepted: evidence => recoverInterruptedTurn(context.window, {
        submissionEvidence: evidence,
        persistedConversationUrl: context.chatUrl,
        captureGeneratedImage: wantsGeneratedImage,
        signal: controller.signal,
        onStatus: (stage, message) => updateTurnActivity({ active: true, stage, message }),
        onProgress: progress => {
          acceptedResponseLease.progress(progress?.text);
          updateTurnActivity({ active: true,
            noResponseEvidence: progress?.noResponseEvidence === true }, { progress: true });
        },
      }),
    });
    const turnResult = wantsGeneratedImage && attachments.length === 0
      ? await runImageTurnWithOneRetry(runCurrentTurn, {
          signal: controller.signal,
          diagnose: async () => {
            const own = await chatTurnSnapshot(context.window);
            if (mainWindow && !mainWindow.isDestroyed()
                && mainWindow !== context.window
                && mainWindow.webContents.getURL() === context.window.webContents.getURL()) {
              const visible = await chatTurnSnapshot(mainWindow);
              if (Number(visible.userCount) > Number(own.userCount)) {
                // The user already retried in the visible window. Let image
                // recovery collect that result; do not create a third turn.
                return { ...own, stopControlCount: 1 };
              }
            }
            return own;
          },
          beforeRetry: async () => {
            acceptedResponseLease.pause();
            context.pendingSubmission = null;
            if (turnIdentity) saveTurnContext(turnIdentity, context);
            persistState();
            updateTurnActivity({ active: true, stage: "image-terminal-retry",
              message: "Lượt ảnh nguồn đã thất bại và dừng hẳn · thử tạo ảnh mới thêm một lần trong đúng hội thoại." });
          },
        })
      : await runCurrentTurn();
    let text = typeof turnResult === "string"
      ? turnResult
      : String(turnResult?.text || "");
    let textSourceProof=null;
    if(body.docurandom_output==='verified_json'){
      const bound=await require('./conversation-text-source.cjs').readConversationTextSource({
        chatUrl:context.window.webContents.getURL(),expectedPrompt:prompt,promptSha256:crypto.createHash('sha256').update(prompt.normalize('NFC').replace(/\s+/g,' ').trim()).digest('hex'),
        fetch:(address,options)=>session.fromPartition(PARTITION).fetch(address,options),headers:conversationReadHeaders});
      if(bound?.status!=='completed')throw Error('CHATGPT_EXACT_TEXT_PENDING: exact request response not yet verified; no resend');
      text=bound.output_text;textSourceProof=bound.source_proof;
    }
    const generatedImage = (
      wantsGeneratedImage
      && turnResult
      && typeof turnResult === "object"
      && turnResult.generatedImage
      && typeof turnResult.generatedImage === "object"
    ) ? turnResult.generatedImage : null;
    const terminalError = (
      wantsGeneratedImage
      && turnResult
      && typeof turnResult === "object"
      && typeof turnResult.terminalError === "string"
      && turnResult.terminalError.trim()
    ) ? turnResult.terminalError.trim() : "";
    if (previousId) conversations.delete(previousId);
    rememberConversationUrl(context);
    conversations.set(responseId, context);
    if (turnIdentity) turnConversations.set(turnIdentity, context);
    context.pendingSubmission = null;
    context.earlyMasterFailure = null;
    discardPendingAttachment(context);
    if (turnIdentity) saveTurnContext(turnIdentity, context);
    const outputItem = {
      type: "message",
      id: messageId,
      status: "completed",
      role: "assistant",
      content: [{ type: "output_text", text, annotations: [] }],
    };
    const value = {
      id: responseId,
      object: "response",
      created_at: createdAt,
      status: "completed",
      model: body.model,
      output_text: text,
      output: [outputItem],
      usage: null,
      ...(terminalError ? { recovery_error: terminalError } : {}),
      ...(textSourceProof?{source_proof:textSourceProof}:{}),
      ...(generatedImage ? { generated_image: generatedImage } : {}),
    };
    rememberTurnResult(turnIdentity, turnId, value);
    completed = true;
    updateTurnActivity({
      active: false,
      stage: "completed",
      message: "ChatGPT đã hoàn tất phản hồi; Codex đang chốt lượt.",
      text,
    }, { progress: true });
    if (streaming) {
      if (codexRequest) {
        if (!sseMessageStarted) {
          writeSse(response, "response.output_item.added", {
            output_index: 0,
            item: { ...outputItem, status: "in_progress", content: [] },
          }, sequence++);
          writeSse(response, "response.content_part.added", {
            item_id: messageId,
            output_index: 0,
            content_index: 0,
            part: { type: "output_text", text: "", annotations: [] },
          }, sequence++);
        }
        if (text !== streamedText) {
          writeSse(response, "response.output_text.delta", {
            item_id: messageId,
            output_index: 0,
            content_index: 0,
            delta: text.startsWith(streamedText) ? text.slice(streamedText.length) : text,
          }, sequence++);
        }
        writeSse(response, "response.output_text.done", {
          item_id: messageId, output_index: 0, content_index: 0, text,
        }, sequence++);
        writeSse(response, "response.content_part.done", {
          item_id: messageId,
          output_index: 0,
          content_index: 0,
          part: { type: "output_text", text, annotations: [] },
        }, sequence++);
        writeSse(response, "response.output_item.done", {
          output_index: 0, item: outputItem,
        }, sequence++);
        writeSse(response, "response.completed", { response: value }, sequence++);
        finishSse(response);
      } else {
        if (text !== streamedText) {
        writeNdjson(response, { type: "response.output_text.snapshot", text });
        }
        writeNdjson(response, { type: "response.completed", response: value });
        response.end();
      }
    } else {
      sendJson(response, 200, value);
    }
  } catch (error) {
    error = recoveryReadError(error, controller.signal);
    const earlyFailure = captureEarlyMasterFailure({error, prompt, attachments,
      outputMode: body.docurandom_output || "text", chatUrl: context.chatUrl,
      submissionSeen, attachmentSeen, pendingSubmission: context.pendingSubmission,
      pendingAttachment: context.pendingAttachment});
    if (earlyFailure) context.earlyMasterFailure = earlyFailure;
    error = await accountCapabilityFailure(
      error, conversationContexts(),
      window => pageSnapshot(window, { timeoutMs: 1500 }), chatGptRateLimitError,
    );
    retireFailedRenderer(context, error);
    updateTurnActivity({
      active: false,
      stage: "error",
      message: error instanceof Error ? error.message : String(error),
    }, { progress: true });
    const contextUsable = contextWindowUsable(context);
    if (turnIdentity) {
      if (contextUsable) {
        // A transient DOM/read failure must not detach a saved multi-turn
        // conversation. Retaining this binding lets the next command retry in
        // the same ChatGPT chat instead of silently losing its context.
        turnConversations.set(turnIdentity, context);
        saveTurnContext(turnIdentity, context);
      } else if (context.chatUrl) {
        turnConversations.set(turnIdentity, context);
        saveTurnContext(turnIdentity, context);
      } else if (turnConversations.get(turnIdentity) === context) {
        turnConversations.delete(turnIdentity);
      }
    }
    if (createdContext && !contextUsable && !turnIdentity) allContexts.delete(context);
    completed = true;
    if (streaming && response.headersSent && codexRequest) {
      const failure = {
        id: responseId,
        object: "response",
        created_at: createdAt,
        status: "failed",
        model: body.model,
        output: [],
        error: { code: "bridge_error", message: error instanceof Error ? error.message : String(error) },
      };
      writeSse(response, "response.failed", { response: failure }, sequence++);
      finishSse(response);
      return;
    }
    if (streaming && response.headersSent) {
      writeNdjson(response, { type: "error", error: { message: error instanceof Error ? error.message : String(error) } });
      if (!response.destroyed && !response.writableEnded) response.end();
      return;
    }
    throw error;
  } finally {
    acceptedResponseLease.finish();
    if (disconnectTimer !== null) clearTimeout(disconnectTimer);
    if (heartbeatTimer !== null) clearInterval(heartbeatTimer);
    if (turnIdentity) {
      const current = turnControllers.get(turnIdentity);
      if (current?.controller === controller) turnControllers.delete(turnIdentity);
    }
    context.busy = false;
    context.touchedAt = Date.now();
    busyCount = Math.max(0, busyCount - 1);
    persistState();
  }
}

async function routeRequest(request, response) {
  assertLoopbackAddress(request.socket.remoteAddress);
  const url = new URL(request.url || "/", `http://${LOOPBACK_HOST}`);
  if (request.method === "GET" && url.pathname === "/healthz") {
    sendJson(response, 200, {
      service: "docurandom-chat-bridge",
      status: "ok",
      version: VERSION,
      accepting_turns: authenticated && canAcceptConversationTurn(),
      active_turns: busyCount,
      activity_updated_at: latestActiveTurnActivityUpdatedAt(),
      updated_at: latestActiveTurnUpdatedAt(),
      no_response_evidence: latestActiveTurnNoResponseEvidence(),
      live_conversations: liveConversationCount(),
      saved_conversations: activeConversationCount(),
      max_parallel_turns: MAX_CONVERSATIONS,
    });
    return;
  }
  if (!tokenMatches(accessToken, request.headers.authorization)) {
    sendJson(response, 401, { error: { message: "Local bridge authorization failed" } });
    return;
  }
  if (request.method === "GET" && url.pathname === "/v1/session/capabilities") {
    // Read only: no new chat, reload, model change or prompt submission.
    sendJson(response, 200, await readAccountCapabilities(
      conversationContexts(), mainWindow,
      window => pageSnapshot(window, { timeoutMs: 2_000 }),
    ));
    return;
  }
  if (request.method === "GET" && url.pathname === "/v1/session/status") {
    sendJson(response, 200, {
      authenticated,
      // ``busy`` means capacity is full, not merely that another independent
      // workspace is active. The old global flag serialized every workspace
      // and made a healthy session look unavailable during normal overlap.
      busy: !canAcceptConversationTurn(),
      active_turns: busyCount,
      activity_updated_at: latestActiveTurnActivityUpdatedAt(),
      updated_at: latestActiveTurnUpdatedAt(),
      no_response_evidence: latestActiveTurnNoResponseEvidence(),
      live_conversations: liveConversationCount(),
      saved_conversations: activeConversationCount(),
      max_parallel_turns: MAX_CONVERSATIONS,
      chat_mode: "saved",
      status: authenticated ? "ready" : "signed-out",
    });
    return;
  }
  if (request.method === "POST" && url.pathname === "/v1/session/inspect") {
    const inspectRequest = await readJson(request);
    await waitForAuthenticatedSession();
    let saved = false;
    let snapshot = null;
    if (authenticated && mainWindow && !mainWindow.isDestroyed()) {
      await ensureSavedChat(mainWindow);
      saved = true;
      if (inspectRequest?.detectCapabilities === true) {
        snapshot = await pageSnapshot(mainWindow);
      }
    }
    sendJson(response, 200, {
      authenticated: authenticated && saved,
      saved,
      ...(snapshot ? { capabilities: {
        modelControls: snapshot.modelControls || [],
        fileInputs: snapshot.fileInputs || [],
      } } : {}),
      temporary: false,
      busy: !canAcceptConversationTurn(),
    });
    return;
  }
  if (request.method === "POST" && url.pathname === "/v1/session/exact-text") {
    const body=await readJson(request),identity=normalizeTurnIdentity(body?.thread_id),saved=identity?savedTurns.get(identity):null;
    if(!saved||!isExactChatConversationUrl(saved.chatUrl)||!/^[a-f0-9]{64}$/.test(body?.prompt_sha256||'')){
      sendJson(response,400,{error:{message:'Exact saved chat and prompt hash required'}});return;
    }
    const result=await require('./conversation-text-source.cjs').readConversationTextSource({chatUrl:saved.chatUrl,promptSha256:body.prompt_sha256,
      expectedPrompt:typeof body.expected_prompt==='string'&&body.expected_prompt.length<=65536?body.expected_prompt:undefined,
      fetch:(address,options)=>session.fromPartition(PARTITION).fetch(address,options),headers:conversationReadHeaders});
    const context=turnConversations.get(identity);
    const previous=[...retiredTextReceipts.values()].find(r=>r.thread===identity
      &&r.sourceProof?.user_message_id===result?.source_proof?.user_message_id
      &&r.sourceProof?.prompt_sha256===result?.source_proof?.prompt_sha256);
    const pending=context?.pendingSubmission||saved.pendingSubmission||previous?.pendingSubmission;
    if(!context?.busy&&!turnControllers.has(identity)&&['completed','completed-invalid'].includes(result?.status)
      &&require('./conversation-text-source.cjs').completedTextReceiptMatches(result.source_proof,pending)){
      const attachment=context?.pendingAttachment||saved.pendingAttachment;
      const consumed=require('./conversation-text-source.cjs').consumedAttachmentMatches(result.source_proof,attachment,pending);
      retiredTextReceipts.set(identity+':'+pending.createdAt,{...previous,thread:identity,chatUrl:saved.chatUrl,
        pendingSubmission:{...pending},...(consumed?{pendingAttachment:attachment}:{}),
        sourceProof:result.source_proof,retiredAt:Date.now(),reason:result.status});
      if(context){context.pendingSubmission=null;if(consumed)context.pendingAttachment=null;saveTurnContext(identity,context);}
      else savedTurns.set(identity,{...saved,pendingSubmission:null,...(consumed?{pendingAttachment:null}:{})});
      persistState();
    }
    sendJson(response,200,result||{status:'pending'});return;
  }
  if (request.method === "POST" && url.pathname === "/v1/session/diagnostic") {
    const diagnosticRequest = await readJson(request);
    const identity = normalizeTurnIdentity(diagnosticRequest?.thread_id);
    if (!identity) {
      sendJson(response, 400, { error: { message: "Exact ChatGPT thread_id is required" } });
      return;
    }
    let context = turnConversations.get(identity) || null;
    const saved = savedTurns.get(identity) || null;
    if (!context && saved) {
      context = await createConversation(saved.chatUrl);
      context.pendingSubmission = saved.pendingSubmission || null;
      context.pendingAttachment = saved.pendingAttachment || null;
      turnConversations.set(identity, context);
    }
    if (!context) {
      sendJson(response, 404, { error: { message: "ChatGPT conversation context is not available" } });
      return;
    }
    // A status reader does not own the renderer used by a live submission.
    // Probing it concurrently can collide with the owner's DOM probe and
    // incorrectly retire a healthy window. Return durable activity facts;
    // the caller can collect DOM evidence once the owner has finished.
    if (context.busy || turnControllers.has(identity)) {
      sendJson(response, 200, {
        thread_id: identity,
        saved_url: saved?.chatUrl || context.chatUrl || "",
        current_url: contextWindowUsable(context) ? context.window.webContents.getURL() : "",
        context_busy: true,
        active_controller: turnControllers.has(identity),
        active_turns: turnControllers.size,
        pending_submission: Boolean(context.pendingSubmission),
        pending_attachment: Boolean(context.pendingAttachment),
        pending_submission_details: context.pendingSubmission || null,
        snapshot: null,
        page_snapshot: null,
        connection_health: connectionHealth(context),
        diagnostic_deferred: "active-turn-owner",
      });
      return;
    }
    await ensureConversationWindow(context);
    let snapshot;
    try {
      snapshot = await chatTurnSnapshot(context.window);
    } catch (error) {
      if (retireFailedRenderer(context, error, {
        protectActiveTurn: context.busy || turnControllers.has(identity),
      })) {
        saveTurnContext(identity, context);
        persistState();
      }
      throw error;
    }
    let pageSnapshotValue = null;
    let pageSnapshotError = "";
    let pendingAttachmentUi = null;
    try {
      // Keep this read-only and bounded.  It exposes composer/file-input
      // capability facts, not prompt text, cookies, headers or page HTML.  A
      // workspace-wide diagnosis needs this second snapshot: chatTurnSnapshot
      // can prove that the saved answer finished, while pageSnapshot proves
      // whether the current renderer can accept the next turn.
      pageSnapshotValue = await pageSnapshot(context.window, {
        timeoutMs: 5_000,
        includePickerDiagnostics: diagnosticRequest?.include_picker_dom === true,
      });
      if (context.pendingAttachment) {
        pendingAttachmentUi = await pendingAttachmentUiState(
          context.window, context.pendingAttachment.files,
        );
      }
    } catch (error) {
      pageSnapshotError = String(error?.message || error || "page probe failed").slice(0, 500);
    }
    sendJson(response, 200, {
      thread_id: identity,
      saved_url: String(saved?.chatUrl || ""),
      current_url: String(context.window.webContents.getURL?.() || ""),
        context_busy: context.busy === true,
        active_controller: turnControllers.has(identity),
        active_turns: busyCount,
        pending_submission: Boolean(context.pendingSubmission),
        pending_attachment: Boolean(context.pendingAttachment),
        ...(context.pendingAttachment ? {
          pending_attachment_details: {
            status: context.pendingAttachment.status,
            file_count: context.pendingAttachment.files.length,
            sha256_prefixes: context.pendingAttachment.files.map(item => item.sha256.slice(0, 12)),
            updated_at: context.pendingAttachment.updatedAt,
          },
        } : {}),
        ...(context.pendingSubmission ? {
          pending_submission_details: {
            schema_version: Number(context.pendingSubmission.schemaVersion) || 1,
            output_mode: context.pendingSubmission.outputMode,
            send_attempted: context.pendingSubmission.sendAttempted === true,
            submission_accepted: context.pendingSubmission.submissionAccepted === true,
            assistant_count: context.pendingSubmission.assistantCount,
            user_count: context.pendingSubmission.userCount,
            assistant_key: context.pendingSubmission.assistantKey,
            created_at: context.pendingSubmission.createdAt,
            provider_dispatch: providerDispatchFields(context.pendingSubmission),
          },
        } : {}),
        snapshot,
        ...(diagnosticRequest?.include_composer === true ? {composer_snapshot: await composerValue(context.window)} : {}),
        page_snapshot: pageSnapshotValue,
        connection_health: connectionHealth(context),
        pending_attachment_ui: pendingAttachmentUi,
        ...(pageSnapshotError ? { page_snapshot_error: pageSnapshotError } : {}),
    });
    if (snapshot.conversationLoadFailed || pageSnapshotValue?.conversationLoadFailed) {
      suspendFailedConversationReader(context, new Error("CHATGPT_CONVERSATION_LOAD_FAILED"));
      saveTurnContext(identity, context);
      persistState();
    }
    return;
  }
  if (request.method === "POST" && url.pathname === "/v1/session/image-history-view") {
    const body=await readJson(request),identity=normalizeTurnIdentity(body?.thread_id);
    const saved=identity?savedTurns.get(identity):null;
    if(!saved || !isExactChatConversationUrl(saved.chatUrl)) {
      sendJson(response,404,{error:{message:"Exact saved thumbnail conversation required"}});return;
    }
    let context=turnConversations.get(identity);
    if(context?.busy || turnControllers.has(identity) || existingImageReaders.has(identity)) {
      sendJson(response,409,{error:{message:"Image conversation has an active owner"}});return;
    }
    existingImageReaders.add(identity);
    try {
      if(!context){context=await createConversation(saved.chatUrl);context.pendingSubmission=saved.pendingSubmission||null;
        context.pendingAttachment=saved.pendingAttachment||null;turnConversations.set(identity,context);}
      context.busy=true;
      await ensureConversationWindow(context);
      sendJson(response,200,await inspectImageHistory(context.window,{savedUrl:saved.chatUrl,action:body.action||'inspect',control:body.control}));
    } finally {existingImageReaders.delete(identity);if(context){context.busy=false;context.touchedAt=Date.now();}}
    return;
  }
  if (request.method === "POST" && url.pathname === "/v1/session/existing-image") {
    const body = await readJson(request);
    const expectedUserPromptSha256 = typeof body.image_prompt_sha256 === 'string' && /^[a-f0-9]{64}$/.test(body.image_prompt_sha256) ? body.image_prompt_sha256 : '';
    const identity = normalizeTurnIdentity(body?.thread_id);
    const saved = identity ? savedTurns.get(identity) : null;
    if (!saved || !isExactChatConversationUrl(saved.chatUrl)) {
      sendJson(response, 404, { error: { message: "Exact saved thumbnail conversation is required" } });
      return;
    }
    let context = turnConversations.get(identity);
    if (context?.busy || turnControllers.has(identity) || existingImageReaders.has(identity)) {
      sendJson(response, 409, { error: { message: "Thumbnail conversation is still controlled by an active turn" } });
      return;
    }
    existingImageReaders.add(identity);
    let view;
    let readerJustOpened = !contextWindowUsable(context);
    const retireSourceReceipt=source=>{
      const pending=context?.pendingSubmission||saved.pendingSubmission;
      if(!require('./conversation-image-source.cjs').sourceImageReceiptMatches(source?.source_proof,pending,source?.status))return;
      retiredImageReceipts.set(identity+':'+pending.createdAt,{thread:identity,chatUrl:saved.chatUrl,
        pendingSubmission:{...pending},promptSha256:expectedUserPromptSha256,retiredAt:Date.now(),retryAuthorized:false,
        reason:source.status==='ready'?'complete-source-image-collected':'complete-source-terminal-no-image',sourceProof:source.source_proof});
      if(context)context.pendingSubmission=null;
      savedTurns.set(identity,{...saved,pendingSubmission:null});
      if(context)saveTurnContext(identity,context);persistState();
    };
    try {
    if(expectedUserPromptSha256){
      // Reading the exact server conversation does not need a working SPA.
      // A poisoned renderer must not hide an image which is already complete.
      let source;
      try{source=await readConversationImageSource({chatUrl:saved.chatUrl,promptSha256:expectedUserPromptSha256,
        fetch:(address,options)=>session.fromPartition(PARTITION).fetch(address,options),headers:conversationReadHeaders});}catch(error){if(error?.statusCode===429)throw error;}
      if(source&&(source.status==='ready'||source.status==='no-image')){
        retireSourceReceipt(source);sendJson(response,200,source);return;
      }
      if(body.source_only===true){sendJson(response,200,source||{status:'pending',source_read_unavailable:true});return;}
    }
    view = mainWindow && !mainWindow.isDestroyed()
      && mainWindow.webContents.getURL() === saved.chatUrl ? mainWindow : null;
    if (!view) {
      if (!context) {
        context = await createConversation(saved.chatUrl);
        context.pendingSubmission = saved.pendingSubmission || null;
        context.pendingAttachment = saved.pendingAttachment || null;
        turnConversations.set(identity, context);
      }
      context.busy = true;
      await ensureConversationWindow(context);
      view = context.window;
      if (view.webContents.getURL() !== saved.chatUrl) {
        sendJson(response, 409, { error: { message: "Thumbnail conversation changed; preserve checkpoint" } });
        return;
      }
      // A completed visible/manual turn may be newer than this idle renderer.
      // Refresh only this exact idle reader. Never submit or click Retry here.
      if (!readerJustOpened) await loadConversationUrl(view, saved.chatUrl);
    }
      const collect = () => readExistingImage(context.window, {timeoutMs:6000,reloadOnFailure:false,expectedUserPromptSha256});
      let collected = context && view === context.window
        ? await recoverConversationLoad(context, collect, {recoverAccepted:collect})
        : await readExistingImage(view,{timeoutMs:6000,expectedUserPromptSha256});
      if(collected.status!=='ready' && expectedUserPromptSha256 && !collected.image_busy){
        const before=await chatTurnSnapshot(view);
        if(view.webContents.getURL()===saved.chatUrl && before.readyState==='complete'
          && Number(before.stopControlCount)===0 && Number(before.streamingMarkerCount)===0){
          try {
            const source=await readConversationImageSource({chatUrl:saved.chatUrl,promptSha256:expectedUserPromptSha256,
              fetch:(url,options)=>session.fromPartition(PARTITION).fetch(url,options),headers:conversationReadHeaders});
            const after=await chatTurnSnapshot(view);
            if(source && view.webContents.getURL()===saved.chatUrl && Number(after.stopControlCount)===0
              && Number(after.streamingMarkerCount)===0 && before.latestUserTextSha256===after.latestUserTextSha256
              && before.latestAssistantTextSha256===after.latestAssistantTextSha256){
              collected=source;
              retireSourceReceipt(source);
            }
            if(!source && collected.status!=='ready')collected={...collected,status:'pending',source_read_unavailable:true};
          } catch(error) {
            // Failure of a read endpoint never authorizes resubmission.
            if(error?.statusCode===429)throw error;
            collected={...collected,status:'pending',source_read_unavailable:true};
          }
        }
      }
      sendJson(response,200,collected);
    } catch (error) {
      if (view === mainWindow) {
        mainWindow = replaceFailedIdleReader(view, error, {
          createWindow: createMainWindow,
          protectActiveTurn: [...conversationContexts()].some(item => item.window === view && item.busy),
        });
      }
      if (view === context?.window && retireFailedRenderer(context, error)) {
        saveTurnContext(identity, context);
        persistState();
      }
      throw error;
    } finally {
      existingImageReaders.delete(identity);
      if(context){context.busy=false;context.touchedAt=Date.now();}
    }
    return;
  }
  if (request.method === "POST" && url.pathname === "/v1/session/repair-conversation") {
    const body = await readJson(request);
    const identity = normalizeTurnIdentity(body?.thread_id);
    const saved = identity ? savedTurns.get(identity) : null;
    let context = identity ? turnConversations.get(identity) : null;
    if (!saved || !isExactChatConversationUrl(saved.chatUrl)) {
      sendJson(response, 404, {error: {message: "Exact saved ChatGPT conversation required"}});
      return;
    }
    if (existingImageReaders.has(identity) || context?.busy || turnControllers.has(identity)) {
      sendJson(response, 409, {error: {message: "Active conversation owner; repair deferred"}});
      return;
    }
    if (!context) {
      context = await createConversation(saved.chatUrl);
      context.pendingSubmission = saved.pendingSubmission || null;
      context.pendingAttachment = saved.pendingAttachment || null;
      turnConversations.set(identity, context);
    }
    context.busy = true;
    try {
      await ensureConversationWindow(context);
      const probe = async () => {
        const snapshot = await chatTurnSnapshot(context.window);
        const page = await pageSnapshot(context.window);
        if (snapshot.conversationLoadFailed || page.conversationLoadFailed) {
          throw new Error("CHATGPT_CONVERSATION_LOAD_FAILED: exact conversation history is incomplete");
        }
        return {snapshot, page_snapshot: page};
      };
      // Same cooldown and session as automatic recovery. This action only
      // replaces a failed reader; accepted receipts and attachments survive.
      const result = await recoverConversationLoad(context, probe, {recoverAccepted: probe});
      sendJson(response, 200, {status: result.page_snapshot.ready ? "ready" : "loading",
        thread_id: identity.slice("thread:".length), pending_submission: Boolean(context.pendingSubmission),
        prompt_submitted: false, ...result});
    } finally {
      context.busy = false;
      context.touchedAt = Date.now();
      saveTurnContext(identity, context);
      persistState();
    }
    return;
  }
  if (request.method === 'POST' && url.pathname === '/v1/session/repair-stalled-image-turn') {
    const body=await readJson(request),identity=normalizeTurnIdentity(body?.thread_id);
    const saved=identity ? savedTurns.get(identity) : null;
    let context=identity ? turnConversations.get(identity) : null;
    const key=identity+':'+body?.created_at;
    const retired=retiredImageReceipts.get(key);
    const pending=context?.pendingSubmission || saved?.pendingSubmission ||
      (retired?.retryAuthorized && retired.promptSha256===body?.prompt_sha256 ? retired.pendingSubmission : null);
    const reject=message=>sendJson(response,409,{error:{message:'CHATGPT_IMAGE_RECOVERY_PENDING: '+message}});
    if (!saved || !pending || pending.createdAt!==body?.created_at || !isExactChatConversationUrl(saved.chatUrl)) {
      reject('The exact image receipt changed; it remains protected');return;
    }
    if (context?.busy || turnControllers.has(identity) || existingImageReaders.has(identity)) {
      reject('The image reader is still active');return;
    }
    if (!retired && [...retiredImageReceipts.values()].filter(item=>item.thread===identity).length>=3) {
      reject('The bounded image replacement budget is exhausted');return;
    }
    existingImageReaders.add(identity);
    try {
      if (!context) {context=await createConversation(saved.chatUrl);context.pendingSubmission=saved.pendingSubmission;turnConversations.set(identity,context);}
      context.busy=true;
      await ensureConversationWindow(context);
      const options={chatUrl:saved.chatUrl,promptSha256:body.prompt_sha256,draftSha256:body.draft_sha256};
      const probe=async()=>({snapshot:await chatTurnSnapshot(context.window),page:await pageSnapshot(context.window),
        draft:await composerValue(context.window)});
      const result=await repairStalledImage({pending,options,observations:imageRecoveryObservations,key,probe,
        health:()=>connectionHealth(context),persist:persistState,
        stop:(proof,markStop)=>stopStalledImage(context.window,pending,options,proof,markStop)});
      if (retired && result.binding !== retired.binding) {
        reject('The retry grant no longer matches its original image turn');return;
      }
      if (result.safe_to_retry) {
        if (!retired) retiredImageReceipts.set(key,{thread:identity,chatUrl:saved.chatUrl,
          pendingSubmission:{...pending},promptSha256:body.prompt_sha256,retiredAt:Date.now(),
          retryAuthorized:true,reason:result.reason,binding:result.binding});
        context.pendingSubmission=null;saveTurnContext(identity,context);persistState();
      }
      sendJson(response,200,{...result,receipt_preserved:true,receipt_created_at:pending.createdAt,
        chat_url:saved.chatUrl,prompts_submitted:0});
    } finally {existingImageReaders.delete(identity);if(context){context.busy=false;context.touchedAt=Date.now();}persistState();}
    return;
  }
  if (request.method === "POST" && url.pathname === "/v1/session/retire-idle-image-turn") {
    const body=await readJson(request),identity=normalizeTurnIdentity(body?.thread_id);
    const saved=identity ? savedTurns.get(identity) : null;
    let context=identity ? turnConversations.get(identity) : null;
    const pending=context?.pendingSubmission || saved?.pendingSubmission;
    const reject=message=>sendJson(response,409,{error:{message:'CHATGPT_PENDING_SUBMISSION_LOCKED: '+message}});
    if(!pending || !saved || body?.created_at!==pending.createdAt || !isExactChatConversationUrl(saved.chatUrl)){
      reject('The exact accepted image receipt changed; it remains protected');return;
    }
    if(context?.busy || turnControllers.has(identity) || existingImageReaders.has(identity)){
      reject('The image turn is still owned by an active reader');return;
    }
    if([...retiredImageReceipts.values()].filter(item=>item.thread===identity).length>=3){
      reject('The bounded image retry budget is exhausted');return;
    }
    if(!context){context=await createConversation(saved.chatUrl);context.pendingSubmission=pending;turnConversations.set(identity,context);}
    context.busy=true;
    try{
      await ensureConversationWindow(context);
      const options={chatUrl:saved.chatUrl,promptSha256:body.prompt_sha256};
      const probe=async()=>idleImageRetryProof(await chatTurnSnapshot(context.window),await pageSnapshot(context.window),pending,options);
      const first=await probe();await new Promise(resolve=>setTimeout(resolve,1500));
      if(!first || await probe()!==first){reject('The exact old image turn is not stably idle with no image');return;}
      retiredImageReceipts.set(identity+':'+pending.createdAt,{thread:identity,chatUrl:saved.chatUrl,
        pendingSubmission:{...pending},promptSha256:body.prompt_sha256,retiredAt:Date.now()});
      context.pendingSubmission=null;saveTurnContext(identity,context);persistState();
      sendJson(response,200,{status:'retired-for-one-image-retry',receipt_preserved:true,chat_url:saved.chatUrl,prompts_submitted:0});
    }finally{context.busy=false;}
    return;
  }
  if (request.method === "POST" && url.pathname === "/v1/session/recover-home-conversation") {
    const body=await readJson(request);
    const identity=normalizeTurnIdentity(body?.thread_id);
    const saved=identity ? savedTurns.get(identity) : null;
    let context=identity ? turnConversations.get(identity) : null;
    const pending=context?.pendingSubmission || saved?.pendingSubmission;
    const candidateUrl=String(body?.url||'');
    if(!saved || isExactChatConversationUrl(saved.chatUrl) || !pending?.submissionAccepted
      || Number(pending.userCount)!==0 || Number(pending.assistantCount)!==0
      || !isExactChatConversationUrl(candidateUrl) || !/^[a-f0-9]{64}$/.test(body?.prompt_sha256||'')){
      sendJson(response,409,{error:{message:'Only an accepted first turn missing its saved URL can be rebound'}});return;
    }
    if(context?.busy || turnControllers.has(identity) || existingImageReaders.has(identity)){
      sendJson(response,409,{error:{message:'The exact conversation is busy'}});return;
    }
    if([...savedTurns].some(([key,value])=>key!==identity && value.chatUrl===candidateUrl)){
      sendJson(response,409,{error:{message:'Candidate URL already belongs to another workspace'}});return;
    }
    if(!context){
      context=await createConversation(saved.chatUrl);context.pendingSubmission=pending;
      turnConversations.set(identity,context);
    }
    await ensureConversationWindow(context);
    const inventory=await chatTurnSnapshot(context.window);
    if(!(inventory.recentConversationLinks||[]).some(item=>item.url===candidateUrl)){
      sendJson(response,404,{error:{message:'Candidate URL is not present in this account history'}});return;
    }
    context.busy=true;
    let reader=null;
    try{
      reader=await createConversation(candidateUrl);
      await waitForComposer(reader.window,{timeoutMs:15_000});
      const options={url:candidateUrl,promptSha256:body.prompt_sha256};
      const probe=async()=>{
        const snapshot=await chatTurnSnapshot(reader.window),page=await pageSnapshot(reader.window);
        return {snapshot,page,proof:homeConversationProof(snapshot,page,options)};
      };
      const first=await probe();
      await new Promise(resolve=>setTimeout(resolve,1500));
      const second=await probe();
      if(!first.proof || second.proof!==first.proof){
        sendJson(response,409,{error:{message:'Candidate does not prove the exact first prompt; original receipt kept'},
          candidate_diagnostic:{url:second.snapshot.url,userCount:second.snapshot.userCount,
            assistantCount:second.snapshot.assistantCount,latestUserTextSha256:second.snapshot.latestUserTextSha256,
            latestUserContentSha256:second.snapshot.latestUserContentSha256,
            latestUserText:second.snapshot.users?.at(-1)?.text||'',
            pageReady:second.page.ready,conversationLoadFailed:second.page.conversationLoadFailed}});return;
      }
      reader.pendingSubmission=pending;reader.pendingAttachment=context.pendingAttachment;
      reader.touchedAt=Date.now();turnConversations.set(identity,reader);
      saveTurnContext(identity,reader);persistState();
      suspendConversationWindow(context);allContexts.delete(context);reader=null;
      sendJson(response,200,{status:'rebound',url:candidateUrl,receipt_preserved:true,prompts_submitted:0});
    }finally{
      context.busy=false;
      if(reader){suspendConversationWindow(reader);allContexts.delete(reader);}
    }
    return;
  }
  if (request.method === "POST" && url.pathname === "/v1/session/open-conversation") {
    const openRequest = await readJson(request);
    const threadId = String(openRequest?.thread_id || "").trim();
    const identity = threadId ? normalizeTurnIdentity(threadId) : "";
    const saved = identity ? savedTurns.get(identity) || null : null;
    const requestedUrl = String(openRequest?.url || saved?.chatUrl || "").trim();
    if (!isExactChatConversationUrl(requestedUrl)) {
      sendJson(response, 400, {
        error: { message: "Exact ChatGPT conversation URL /c/... is required" },
      });
      return;
    }
    await waitForAuthenticatedSession();
    if (!mainWindow || mainWindow.isDestroyed()) mainWindow = createMainWindow();
    await mainWindow.loadURL(requestedUrl, {
      extraHeaders: "Cache-Control: no-cache\r\nPragma: no-cache\r\n",
    });
    mainWindow.show();
    mainWindow.focus();
    sendJson(response, 200, {
      status: "opened",
      thread_id: identity ? identity.slice("thread:".length) : "",
      url: requestedUrl,
    });
    return;
  }
  if (request.method === "POST" && url.pathname === "/v1/session/repair") {
    await readJson(request);
    const repaired = await repairBrowserSession();
    sendJson(response, 200, {
      status: repaired.authenticated && repaired.ready ? "repaired" : "login-required",
      authenticated: repaired.authenticated,
      ready: repaired.ready,
      login_preserved: repaired.authenticated,
      url: repaired.url,
    });
    return;
  }
  if (request.method === "POST" && url.pathname === "/v1/session/shutdown") {
    await readJson(request);
    if (busyCount > 0) {
      sendJson(response, 409, {
        error: { message: "ChatGPT Web Bridge is busy" },
      });
      return;
    }
    sendJson(response, 202, { status: "shutting-down" });
    setImmediate(() => { void shutdown(); });
    return;
  }
  if (request.method === "GET" && url.pathname === "/v1/models") {
    sendJson(response, 200, codexModelCatalog());
    return;
  }
  if (request.method === "POST" && url.pathname === "/v1/turns/recover") {
    const body = await readJson(request);
    const threadId = String(body?.thread_id || "");
    if (!/^[A-Za-z0-9_-]{8,160}$/.test(threadId)) {
      sendJson(response, 400, { error: { message: "Invalid Codex thread id" } });
      return;
    }
    const identity = `thread:${threadId}`;
    if (existingImageReaders.has(identity)) {
      sendJson(response,409,{error:{message:"This conversation is reading its completed image"}});
      return;
    }
    let context = turnConversations.get(identity) || null;
    const saved = savedTurns.get(identity) || null;
    if (!context && saved) {
      context = await createConversation(saved.chatUrl);
      context.pendingSubmission = saved.pendingSubmission || null;
      context.pendingAttachment = saved.pendingAttachment || null;
      turnConversations.set(identity, context);
    }
    if (!context) {
      sendJson(response, 410, {
        error: { message: "The saved ChatGPT conversation for this workspace is no longer available" },
      });
      return;
    }
    const earlyFailure = normalizeEarlyMasterFailure(Object.hasOwn(context, "earlyMasterFailure")
      ? context.earlyMasterFailure : saved?.earlyMasterFailure);
    if (earlyFailure && !context.pendingSubmission && !saved?.pendingSubmission
        && !context.pendingAttachment && !context.busy && !turnControllers.has(identity)) {
      context.busy = true;
      try {
        await ensureConversationWindow(context);
        const probe = async () => earlyMasterProof(earlyFailure, body.pending_attachment,
          body.pending_prompt_sha256, await chatTurnSnapshot(context.window), await pageSnapshot(context.window));
        const first = await probe();
        if (first) {
          await new Promise(resolve => setTimeout(resolve, 1500));
          const second = await probe();
          if (first === second && require('./pre-submit-restage.cjs').reserveEmptyAttachmentRestage(
              path.join(path.dirname(STATE_PATH), 'pre-submit-restage.json'), identity,
              earlyFailure.attachmentSha256, first, atomicJson)) {
            sendJson(response, 200, {status: 'not-submitted', safe_to_resend: true,
              proof: 'failed-before-master-input', attachment_sha256: earlyFailure.attachmentSha256,
              prompt_sha256: earlyFailure.promptSha256, output_text: ''});
            return;
          }
        }
      } finally { context.busy = false; }
    }
    if(body?.pending_attachment && !context.busy && !turnControllers.has(identity)
        && !isExactChatConversationUrl(context.chatUrl)){
      const pending=context.pendingSubmission || saved?.pendingSubmission;
      const attachment=context.pendingAttachment || saved?.pendingAttachment;
      const found=await recoverFirstMasterHome(context,{
        pending,attachment,expected:body.pending_attachment,stagingRoot:ATTACHMENT_STAGING_ROOT,
        usedUrls:new Set([...savedTurns].filter(([key])=>key!==identity).map(([,value])=>value.chatUrl)),
        inventory:async()=>{await ensureConversationWindow(context);return chatTurnSnapshot(context.window);},
        open:async url=>{const reader=await createConversation(url);try{
          await waitForComposer(reader.window,{timeoutMs:10000,maxPageReloads:0});return reader;
        }catch(error){suspendConversationWindow(reader);allContexts.delete(reader);throw error;}},
        probe:async reader=>({snapshot:await chatTurnSnapshot(reader.window),page:await pageSnapshot(reader.window)}),
        discard:reader=>{suspendConversationWindow(reader);allContexts.delete(reader);},
        onStatus:(stage,message)=>turnActivity.set(identity,{...(turnActivity.get(identity)||{}),active:true,
          stage,message,updatedAt:Date.now()}),
      });
      if(found){
        found.pendingSubmission=pending;found.pendingAttachment=attachment;
        turnConversations.set(identity,found);saveTurnContext(identity,found);persistState();
        suspendConversationWindow(context);allContexts.delete(context);context=found;
      }
    }
    let submissionEvidence = context.pendingSubmission || saved?.pendingSubmission || null;
    if (!submissionEvidence && /^[a-f0-9]{64}$/.test(body?.pending_prompt_sha256 || "")
        && !context.busy && !turnControllers.has(identity)) {
      context.busy = true;
      try {
        await ensureConversationWindow(context);
        const options = {chatUrl: context.chatUrl, promptSha256: body.pending_prompt_sha256};
        const isCompletedBaseline = snapshot => body.completed_turn_baseline
          && baselineUserHash(snapshot,{userSha256:body.completed_turn_baseline.user_sha256,
            userAttachment:body.completed_turn_baseline.user_attachment})
          && snapshot.latestAssistantTextSha256===body.completed_turn_baseline.assistant_sha256;
        const readProofSnapshot=async()=>{
          const snapshot=await chatTurnSnapshot(context.window),page=await pageSnapshot(context.window);
          if(snapshot.conversationLoadFailed||page.conversationLoadFailed)
            throw new Error('CHATGPT_CONVERSATION_LOAD_FAILED: exact queued-turn history has not loaded');
          return {snapshot,page};
        };
        // An unloaded reader cannot prove that an accepted or queued prompt
        // is absent. Reopen only that reader with the existing bounded recovery;
        // this callback neither submits nor clears an attachment/receipt.
        const firstRead=await recoverConversationLoad(context,readProofSnapshot,{recoverAccepted:readProofSnapshot});
        const firstSnapshot=firstRead.snapshot;
        const first = isCompletedBaseline(firstSnapshot) ? null
          : checkpointReceiptProof(firstSnapshot, firstRead.page, options);
        if (first) {
          await new Promise(resolve => setTimeout(resolve, 1500));
          const secondRead=await readProofSnapshot(),secondSnapshot=secondRead.snapshot;
          const second = isCompletedBaseline(secondSnapshot) ? null
            : checkpointReceiptProof(secondSnapshot, secondRead.page, options);
          if (first === second) {
            // Recover only an already completed reply to the exact local user
            // turn. A missing receipt never grants Send/Retry authorization.
            submissionEvidence = receiptFromCheckpoint(secondSnapshot, {outputMode: body.output_mode || "text"});
            context.pendingSubmission = submissionEvidence;
            saveTurnContext(identity, context);
            persistState();
          }
        }
      } finally { context.busy = false; }
    }
    if (!submissionEvidence) {
      // A staged native file was recorded before any trusted Send. Authorize
      // only reuse of those exact bytes, with two stable idle UI proofs. No
      // receipt alone never authorizes a text resend or a new attachment.
      if (body?.completed_turn_baseline && !context.busy && !turnControllers.has(identity)) {
        context.busy=true;
        try {
          await ensureConversationWindow(context);
          const options={chatUrl:context.chatUrl,pendingPromptSha256:body.pending_prompt_sha256,
            userSha256:body.completed_turn_baseline.user_sha256,assistantSha256:body.completed_turn_baseline.assistant_sha256,
            userAttachment:body.completed_turn_baseline.user_attachment};
          const probe=async()=>queuedTurnProof(await chatTurnSnapshot(context.window),await pageSnapshot(context.window),options);
          const first=await probe();
          if(first){
            await new Promise(resolve=>setTimeout(resolve,1500));
            if(first===await probe()){
              sendJson(response,200,{status:"not-submitted",safe_to_resend:true,proof:"queued-after-completed-turn",output_text:""});
              return;
            }
          }
        } finally {context.busy=false;}
      }
      if (body.allow_missing_content_repair === true && body.completed_turn_baseline
          && !context.pendingAttachment && !saved?.pendingAttachment
          && !context.busy && !turnControllers.has(identity)) {
        context.busy=true;
        try {
          await ensureConversationWindow(context);
          const options={chatUrl:context.chatUrl,...{
            userSha256:body.completed_turn_baseline.user_sha256,
            assistantSha256:body.completed_turn_baseline.assistant_sha256,
            userAttachment:body.completed_turn_baseline.user_attachment}};
          const probe=async()=>completedBaselineProof(await chatTurnSnapshot(context.window),await pageSnapshot(context.window),options);
          const first=await probe();
          if(first){
            await new Promise(resolve=>setTimeout(resolve,1500));
            if(first===await probe()){
              sendJson(response,200,{status:'recovery-baseline',proof:'stable-completed-baseline',
                safe_to_resend:false,safe_to_request_missing:true,
                user_sha256:options.userSha256,assistant_sha256:options.assistantSha256,
                pending_prompt_sha256:body.pending_prompt_sha256,
                evidence_sha256:crypto.createHash('sha256').update(first).digest('hex'),output_text:''});
              return;
            }
          }
        }finally{context.busy=false;}
      }
      const record = context.pendingAttachment;
      const expected = body?.pending_attachment;
      if (record && expected && !context.busy && !turnControllers.has(identity)) {
        context.busy = true;
        try {
          await ensureConversationWindow(context);
          const probe = async () => {
            if (!pendingAttachmentInCurrentChat(record, context.window)) return null;
            let bytes;
            try { bytes = fs.readFileSync(record.files[0].path); } catch { return null; }
            if (bytes.length !== expected.size
                || crypto.createHash("sha256").update(bytes).digest("hex") !== expected.sha256) return null;
            return preSubmitAttachmentProof(record, expected,
              await chatTurnSnapshot(context.window), await pageSnapshot(context.window),
              await pendingAttachmentUiState(context.window, record.files));
          };
          const first = await probe();
          await new Promise(resolve => setTimeout(resolve, 300));
          const second = first ? await probe() : null;
          if (first && first === second) {
            sendJson(response, 200, {status: "not-submitted", safe_to_resend: true,
              attachment_sha256: expected.sha256, output_text: ""});
            return;
          }
          // No trusted Send marker exists. A first Master selection can lose
          // its file card during renderer recovery. Prove the exact home is
          // empty twice, then reserve ONE restage before returning permission.
          const {emptyPreSubmitAttachmentProof,reserveEmptyAttachmentRestage}=require('./pre-submit-restage.cjs');
          const emptyProbe=async()=>{
            if(context.pendingSubmission||!pendingAttachmentInCurrentChat(record,context.window))return null;
            return emptyPreSubmitAttachmentProof(record,expected,await chatTurnSnapshot(context.window),
              await pageSnapshot(context.window),await pendingAttachmentUiState(context.window,record.files));
          };
          const emptyFirst=await emptyProbe();
          if(emptyFirst){
            await new Promise(resolve=>setTimeout(resolve,1500));
            if(emptyFirst===await emptyProbe()&&reserveEmptyAttachmentRestage(
                path.join(path.dirname(STATE_PATH),'pre-submit-restage.json'),identity,expected.sha256,emptyFirst,atomicJson)){
              sendJson(response,200,{status:'not-submitted',safe_to_resend:true,
                proof:'empty-pre-submit-attachment',attachment_sha256:expected.sha256,output_text:''});return;
            }
          }
        } finally { context.busy = false; }
      }
      sendJson(response, 200, { status: "not-submitted", output_text: "" });
      return;
    }
    // A previous bridge process can exit after the response socket is closed
    // while the durable pending checkpoint remains. The new process restores
    // that context conservatively, but there is no live controller left to
    // own it. Do not reject exact-turn recovery behind a stale in-memory busy
    // bit; only a live controller or a positive bridge turn count is evidence
    // that the conversation is currently generating.
    if (context.busy && !turnControllers.has(identity) && busyCount === 0) {
      context.busy = false;
      context.touchedAt = Date.now();
      turnConversations.set(identity, context);
      saveTurnContext(identity, context);
      persistState();
    }
    if (context.busy) {
      sendJson(response, 409, {
        error: { message: "This ChatGPT conversation is already running" },
      });
      return;
    }
    // A read of a completed text reply does not depend on the SPA loading.
    // Preserve the receipt on missing/unavailable source; never resend here.
    if(body?.output_mode!=='generated_image'&&submissionEvidence.outputMode!=='generated_image'){
      const recorded=submissionEvidence.promptSha256,requested=body?.pending_prompt_sha256;
      if((recorded||requested)&&!(recorded&&requested&&recorded!==requested&&submissionEvidence.requestPromptSha256!==requested)){
        context.busy=true;
        try{
          const source=await require('./conversation-image-source.cjs').readConversationTextSource({
            chatUrl:context.chatUrl,promptSha256:recorded||requested,receipt:submissionEvidence,
            fetch:(address,options)=>session.fromPartition(PARTITION).fetch(address,options),headers:conversationReadHeaders});
          if(source&&!response.destroyed){
            context.pendingSubmission=null;discardPendingAttachment(context);saveTurnContext(identity,context);persistState();
            turnActivity.set(identity,{active:false,stage:'source-response-recovered',updatedAt:Date.now(),
              message:'Đã đọc đúng phản hồi hoàn tất từ nguồn hội thoại, không phụ thuộc trang tải lại.'});
            sendJson(response,200,{status:'recovered',output_text:source.text});return;
          }
        }catch(error){if(error?.statusCode===429)throw error;}finally{context.busy=false;}
      }
    }
    await ensureConversationWindow(context);
    if((await pageSnapshot(context.window)).conversationFull)
      throw require('./conversation-capacity.cjs').conversationCapacityError();
    context.busy = true;
    context.touchedAt = Date.now();
    busyCount += 1;
    persistState();
    const controller = new AbortController();
    let completed = false;
    response.once("close", () => { if (!completed) controller.abort(); });
    const finishRecoveryRead = beginRecoveryReadDeadline(controller);
      const updateRecoveryActivity = (value, { progress = false } = {}) => {
        const current = turnActivity.get(identity) || {};
        const now = Date.now();
        turnActivity.set(identity, {
          ...current,
          ...value,
          updatedAt: now,
          ...(progress ? { progressUpdatedAt: now } : {}),
        });
      };
    try {
      updateRecoveryActivity({
        active: true,
        stage: "recovering-dom",
        message: "Đang nối lại DOM của đúng phiên ChatGPT…",
      });
      const sourceText = async () => {
        if(body?.output_mode==='generated_image'||submissionEvidence.outputMode==='generated_image')return null;
        const recorded=submissionEvidence.promptSha256,requested=body?.pending_prompt_sha256;
        if(recorded&&requested&&recorded!==requested&&submissionEvidence.requestPromptSha256!==requested)return null;
        try{return await require('./conversation-image-source.cjs').readConversationTextSource({
          chatUrl:context.chatUrl,promptSha256:recorded||requested,receipt:submissionEvidence,
          fetch:(url,options)=>session.fromPartition(PARTITION).fetch(url,options),
          headers:conversationReadHeaders,signal:controller.signal});}catch(error){if(error?.statusCode===429)throw error;return null;}
      };
      const recoverDom = () => recoverInterruptedTurn(context.window, {
        submissionEvidence,
        connectionInterruptionStaleMs: RECOVERY_CONNECTION_STALE_MS,
        noResponseEvidenceTimeoutMs: RECOVERY_NO_RESPONSE_STALE_MS,
        persistedConversationUrl: context.chatUrl,
        captureGeneratedImage: body?.output_mode === "generated_image"
          || submissionEvidence.captureGeneratedImage === true
          || submissionEvidence.outputMode === "generated_image",
        signal: controller.signal,
        onStatus: (stage, message) => {
          updateRecoveryActivity({ active: true, stage, message });
          if (stage === 'delivery-retry-reserved') {
            saveTurnContext(identity, context);
            persistState();
          }
        },
        onProgress: progress => updateRecoveryActivity({
          active: true,
          noResponseEvidence: progress?.noResponseEvidence === true,
        }, { progress: true }),
        onPartial: partial => updateRecoveryActivity({ active: true, text: partial }, { progress: true }),
      });
      const recoverExisting = async () => {
        const source=await sourceText();
        if(source){updateRecoveryActivity({active:true,stage:'source-response-recovered',
          message:'Đã đọc đúng phản hồi hoàn tất từ nguồn hội thoại đã lưu; không gửi lại prompt.'},{progress:true});return source;}
        try{return await recoverDom();}catch(error){const late=await sourceText();if(late)return late;throw error;}
      };
      const recovered = await recoverConversationLoad(context, recoverExisting, {
        signal: controller.signal,
        recoverAccepted: recoverExisting,
        onStatus: (stage, message) => updateRecoveryActivity({ active: true, stage, message }),
      });
      if (recovered && recovered.status === "not-submitted") {
        // The checkpoint was written before the trusted click, but the exact
        // conversation has no newer user/assistant turn, no generation marker
        // and no Retry/error state. Clear only this stale lock so the client
        // may submit the same prompt once; never clear an accepted turn here.
        context.pendingSubmission = null;
        saveTurnContext(identity, context);
        updateRecoveryActivity({
          active: false,
          stage: "not-submitted",
          message: "Đã xác minh lượt cũ chưa được gửi; đã mở khóa để gửi lại đúng một lần.",
        }, { progress: true });
        completed = true;
        sendJson(response, 200, {
          status: "not-submitted",
          safe_to_resend: recovered.safeToResend === true,
          diagnostic: recovered.diagnostic || null,
        });
        return;
      }
      const text = typeof recovered === "string"
        ? recovered
        : String(recovered?.text || recovered?.output_text || "");
      const generatedImage = recovered
        && recovered.generatedImage
        && typeof recovered.generatedImage === "object"
        ? recovered.generatedImage
        : recovered?.generated_image && typeof recovered.generated_image === "object"
          ? recovered.generated_image
          : null;
      const terminalError = String(recovered?.terminalError || "").trim();
      if (!text && !generatedImage && !terminalError) {
        throw new Error("DOM ChatGPT đã phục hồi nhưng không có output hợp lệ.");
      }
      context.pendingSubmission = null;
      discardPendingAttachment(context);
      saveTurnContext(identity, context);
      updateRecoveryActivity({
        active: false,
        stage: terminalError ? "recovered-terminal-error" : "recovered",
        message: terminalError
          || "Đã thu hồi đầy đủ phản hồi ChatGPT đang dở từ DOM.",
        text,
      }, { progress: true });
      completed = true;
      sendJson(response, 200, {
        status: "recovered",
        output_text: text,
        ...(terminalError ? { recovery_error: terminalError } : {}),
        ...(generatedImage ? { generated_image: generatedImage } : {}),
      });
    } catch (caught) {
      const error = recoveryReadError(caught, controller.signal);
      updateRecoveryActivity({
        active: false,
        stage: "recovery-error",
        message: error instanceof Error ? error.message : String(error),
      }, { progress: true });
      if (retireFailedRenderer(context, error)) saveTurnContext(identity, context);
      completed = true;
      throw error;
    } finally {
      finishRecoveryRead();
      context.busy = false;
      context.touchedAt = Date.now();
      busyCount = Math.max(0, busyCount - 1);
      persistState();
    }
    return;
  }
  if (request.method === "POST" && url.pathname === "/v1/turns/cancel") {
    const body = await readJson(request);
    const identity = normalizeTurnIdentity(body?.thread_id);
    const requestedTurnId = validTurnId(body?.turn_id) ? String(body.turn_id) : "";
    if (!identity || !requestedTurnId) {
      sendJson(response, 400, {
        error: { message: "Exact ChatGPT thread_id and turn_id are required" },
      });
      return;
    }
    const active = turnControllers.get(identity);
    if (!active) {
      sendJson(response, 404, { error: { message: "No active ChatGPT turn for this thread" } });
      return;
    }
    if (active.turnId !== requestedTurnId) {
      sendJson(response, 409, { error: { message: "ChatGPT turn identity does not match the active request" } });
      return;
    }
    const reason = String(body?.reason || "ChatGPT Bridge heartbeat đã dừng").slice(0, 240);
    active.controller.abort();
    const current = turnActivity.get(identity) || {};
    turnActivity.set(identity, {
      ...current,
      active: false,
      stage: "cancel-requested",
      message: reason,
      turn_id: active.turnId,
      updatedAt: Date.now(),
    });
    sendJson(response, 202, {
      status: "cancelling",
      thread_id: identity,
      turn_id: active.turnId,
    });
    return;
  }
  if (request.method === "GET" && url.pathname === "/v1/turns/status") {
    const threadId = url.searchParams.get("thread_id") || "";
    if (!/^[A-Za-z0-9_-]{8,160}$/.test(threadId)) {
      sendJson(response, 400, { error: { message: "Invalid Codex thread id" } });
      return;
    }
    const activity = turnActivity.get(`thread:${threadId}`);
    if (!activity) {
      sendJson(response, 404, { error: { message: "Codex turn activity is not available yet" } });
      return;
    }
    sendJson(response, 200, {
      active: activity.active === true,
      stage: String(activity.stage || ""),
      message: String(activity.message || ""),
      text: String(activity.text || ""),
      turn_id: String(activity.turn_id || ""),
      updated_at: Number(activity.updatedAt || 0),
      activity_updated_at: Number(activity.progressUpdatedAt || 0),
      no_response_evidence: activity.noResponseEvidence === true,
    });
    return;
  }
  if (request.method === "GET" && url.pathname === "/v1/turns/result") {
    const threadId = url.searchParams.get("thread_id") || "";
    const turnId = url.searchParams.get("turn_id") || "";
    if (!/^[A-Za-z0-9_-]{8,160}$/.test(threadId) || !validTurnId(turnId)) {
      sendJson(response, 400, { error: { message: "Invalid ChatGPT turn identity" } });
      return;
    }
    const item = turnResults.get(turnResultKey(`thread:${threadId}`, turnId));
    if (item && item.value && typeof item.value === "object") {
      sendJson(response, 200, {
        status: "completed",
        response: item.value,
        updated_at: Number(item.updatedAt || 0),
      });
      return;
    }
    const activity = turnActivity.get(`thread:${threadId}`);
    sendJson(response, 200, {
      status: activity?.active === true ? "pending" : "not-ready",
      updated_at: Number(activity?.updatedAt || 0),
    });
    return;
  }
  if (request.method === "GET" && url.pathname === "/v1/responses") {
    sendJson(response, 426, { error: { message: "Use HTTP Responses streaming" } });
    return;
  }
  if (request.method === "POST" && url.pathname === "/v1/responses") {
    const body = await readJson(request);
    await handleResponseRequest(request, response, body);
    return;
  }
  sendJson(response, 404, { error: { message: "Unknown local bridge endpoint" } });
}

function listenOnAvailablePort(start = DEFAULT_PORT) {
  return new Promise((resolve, reject) => {
    const attempt = port => {
      if (port > start + 20) return reject(new Error("No local port is available for DocuRandom Chat Bridge"));
      const server = http.createServer((request, response) => {
        routeRequest(request, response).catch(error => {
          if (response.headersSent || response.destroyed) return;
          const status = Number.isInteger(error?.statusCode) ? error.statusCode : 500;
          sendJson(response, status, {
            error: {
              ...(error?.code ? { code: String(error.code).slice(0, 120) } : {}),
              message: error instanceof Error ? error.message : String(error),
            },
          });
        });
      });
      server.on("clientError", (_error, socket) => socket.destroy());
      server.once("error", error => {
        if (error?.code === "EADDRINUSE") return attempt(port + 1);
        reject(error);
      });
      server.listen(port, LOOPBACK_HOST, () => resolve({ server, port }));
    };
    attempt(start);
  });
}

function configureSessionSecurity() {
  const browserSession = session.fromPartition(PARTITION);
  browserSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  browserSession.setPermissionCheckHandler(() => false);
  browserSession.webRequest.onBeforeSendHeaders((details, callback) => {
    const headers = { ...details.requestHeaders };
    delete headers["X-DocuRandom-Token"];
    if(/^https:\/\/chatgpt\.com\/backend-api\//.test(details.url) && details.webContentsId>0){
      const captured={};
      for(const [key,value] of Object.entries(headers))if(['authorization','oai-device-id','oai-language'].includes(key.toLowerCase()))captured[key]=value;
      if(Object.keys(captured).some(key=>key.toLowerCase()==='authorization'))conversationReadHeaders=captured;
    }
    callback({ requestHeaders: headers });
  });
  const filter = { urls: ["https://chatgpt.com/*", "https://chat.openai.com/*"] };
  const recordProviderEvent = details => {
    conversationHealth.record(details);
    for(const [identity,context] of turnConversations){
      if(context.window?.webContents?.id!==details.webContentsId || !context.pendingSubmission)continue;
      if(noteProviderDispatch(context.pendingSubmission,details)){
        saveTurnContext(identity,context);persistState();
      }
    }
  };
  browserSession.webRequest.onBeforeRequest(filter,(details,callback)=>{recordProviderEvent(details);callback({});});
  browserSession.webRequest.onHeadersReceived(filter, (details, callback) => {
    recordProviderEvent(details);
    callback({});
  });
  browserSession.webRequest.onCompleted(filter, recordProviderEvent);
  browserSession.webRequest.onErrorOccurred(filter, recordProviderEvent);
}

async function shutdown() {
  quitting = true;
  for (const { controller } of turnControllers.values()) controller.abort();
  turnControllers.clear();
  for (const context of conversationContexts()) {
    suspendConversationWindow(context);
  }
  conversations.clear();
  turnConversations.clear();
  allContexts.clear();
  turnActivity.clear();
  turnResults.clear();
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.destroy();
  await new Promise(resolve => apiServer ? apiServer.close(resolve) : resolve());
  try { fs.rmSync(DESCRIPTOR_PATH, { force: true }); } catch {}
  app.quit();
}

async function start() {
  if (!app.requestSingleInstanceLock()) {
    app.quit();
    return;
  }
  app.on("second-instance", () => {
    if (!mainWindow || mainWindow.isDestroyed()) mainWindow = createMainWindow();
    mainWindow.show();
    mainWindow.focus();
  });
  await app.whenReady();
  configureSessionSecurity();
  const listening = await listenOnAvailablePort();
  apiServer = listening.server;
  bridgePort = listening.port;
  writeRuntimeFiles();
  if (SMOKE_MODE) {
    const marker = process.env.DOCURANDOM_CHAT_BRIDGE_SMOKE_FILE;
    if (!marker || !path.isAbsolute(marker)) {
      throw new Error("Bridge smoke test requires DOCURANDOM_CHAT_BRIDGE_SMOKE_FILE");
    }
    atomicJson(marker, {
      ok: true,
      version: VERSION,
      host: LOOPBACK_HOST,
      port: bridgePort,
      kind: "docurandom-chat-bridge",
    });
    await shutdown();
    return;
  }
  mainWindow = createMainWindow();
  setInterval(() => {
    const cutoff = Date.now() - 2 * 60 * 60_000;
    const contexts = conversationContexts();
    for (const context of contexts) {
      if (!context.busy && context.touchedAt < cutoff) {
        for (const [id, candidate] of conversations) {
          if (candidate === context) conversations.delete(id);
        }
        for (const [id, candidate] of turnConversations) {
          if (candidate === context) turnConversations.delete(id);
        }
        suspendConversationWindow(context);
        allContexts.delete(context);
      }
    }
    for (const [id, activity] of turnActivity) {
      if (Number(activity.updatedAt || 0) < cutoff) turnActivity.delete(id);
    }
  }, 60_000).unref();
  app.on("activate", () => {
    if (!mainWindow || mainWindow.isDestroyed()) mainWindow = createMainWindow();
    mainWindow.show();
  });
  app.on("before-quit", event => {
    if (quitting) return;
    event.preventDefault();
    void shutdown();
  });
  process.once("SIGINT", () => { void shutdown(); });
  process.once("SIGTERM", () => { void shutdown(); });
}

void start().catch(error => {
  try { dialog.showErrorBox("DocuRandom Chat Bridge", error instanceof Error ? error.message : String(error)); } catch {}
  app.exit(1);
});
