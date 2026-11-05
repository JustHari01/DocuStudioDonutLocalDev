/**
 * Flow Agent — Chrome Extension Background Service Worker
 *
 * Connects to local Python agent via WebSocket (agent runs WS server).
 * Captures bearer token, solves reCAPTCHA, proxies API calls through browser.
 */

importScripts('config.js');

let callbackUrl = 'http://127.0.0.1:3001/api/ext/callback';

let ws = null;
let flowKey = null;
let callbackSecret = null;  // Auth secret for HTTP callback, received from server on WS connect
let state = 'off'; // off | idle | running
let manualDisconnect = false;
let extensionClientId = '';
let connectedServerHost = CONFIG.DEFAULT_SERVER_HOST;
const UI_CAPABILITIES = ['ui_text_to_image', 'flow_project_history'];
const REQUIRED_IMAGE_MODEL = 'harbor_seal';
const REQUIRED_IMAGE_COUNT = 1;
const UI_INSPECT_CONFIGURATION_TIMEOUT_MS = 180000;
// Snapshot may need to inject the current content script and wake a
// minimized renderer. It is read-only, so give that bounded recovery path
// enough time to finish before returning a timeout to the desktop queue.
const UI_PROJECT_SNAPSHOT_TIMEOUT_MS = 180000;
// A single prompt normally completes well below four minutes.  Keep the
// transport deadline slightly above the content-script watchdog so a slow
// Flow generation can return its authoritative result instead of leaving the
// background worker locked while the desktop retries.
const UI_GENERATE_MESSAGE_TIMEOUT_MS = 300000;
const UI_JOB_STALE_IDLE_TIMEOUT_MS = 8 * 60 * 1000;
const UI_WAKE_PULSE_INTERVAL_MS = 750;
// A minimized/background Chromium renderer can take a few seconds to wake.
// Keep this transport deadline bounded, but do not fail the first semantic
// command while the tab is merely resuming its renderer.
const UI_COMMAND_TIMEOUT_MS = 30000;
// Closing/reopening the last Edge window can produce several asynchronous
// windows.onRemoved/onCreated notifications while Chromium is restoring the
// profile.  Do not destroy the bridge socket on the first transient probe.
// The alarm survives MV3 worker suspension; the socket is closed only after
// the grace period confirms that the browser really stayed closed.
const BROWSER_WINDOW_GRACE_ALARM = 'browserWindowGrace';
const BROWSER_WINDOW_CLOSE_GRACE_MINUTES = 0.25;

async function withUiDeadline(promise, timeoutMs = UI_COMMAND_TIMEOUT_MS, label = 'browser-command') {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`FLOW_UI_COMMAND_TIMEOUT:${label}`)), timeoutMs);
    })]);
  } finally { clearTimeout(timer); }
}
let activeUiJob = null;
let staleUiResetPromise = null;
let uiWakePulseGeneration = 0;
// One Flow tab/project is shared by all workspaces attached to this client.
// Queue UI handlers at the worker boundary as well as in the Python bridge;
// this also covers direct extension messages and prevents a late handler from
// racing the next workspace after the desktop request has timed out.
let uiOperationQueueDepth = 0;
let uiOperationTail = Promise.resolve();
const uiDebuggerTabs = new Set();

function enqueueUiOperation(_label, operation) {
  uiOperationQueueDepth += 1;
  const run = uiOperationTail.then(
    () => operation(),
    () => operation(),
  );
  uiOperationTail = run.catch(() => {});
  return run.finally(() => {
    uiOperationQueueDepth = Math.max(0, uiOperationQueueDepth - 1);
  });
}

chrome.debugger.onDetach.addListener((source) => {
  if (source?.tabId) uiDebuggerTabs.delete(source.tabId);
});

async function getBrowserPresence() {
  try {
    if (typeof chrome.windows?.getAll === 'function') {
      const windows = await chrome.windows.getAll({ populate: true, windowTypes: ['normal'] });
      let flowTabCount = 0;
      for (const browserWindow of windows) {
        for (const tab of (browserWindow.tabs || [])) {
          if (isFlowUrl(tab.url || tab.pendingUrl)) flowTabCount++;
        }
      }
      return {
        browserOpen: windows.length > 0,
        normalWindowCount: windows.length,
        flowTabCount,
      };
    }
    // Some Chromium-compatible profile builds expose tabs but omit part of
    // chrome.windows. Presence reporting must not abort UI generation there.
    const tabs = await chrome.tabs.query({});
    const windowIds = new Set(
      tabs.map((tab) => Number(tab.windowId)).filter((value) => value >= 0),
    );
    return {
      browserOpen: tabs.length > 0,
      normalWindowCount: windowIds.size,
      flowTabCount: tabs.filter((tab) => isFlowUrl(tab.url || tab.pendingUrl)).length,
    };
  } catch {
    return { browserOpen: false, normalWindowCount: 0, flowTabCount: 0 };
  }
}

async function sendBrowserPresence() {
  const presence = await getBrowserPresence();
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'browser_state', ...presence }));
  }
  return presence;
}

function normalizeCallbackUrl(value) {
  try {
    const raw = String(value || '').trim();
    const parsed = new URL(/^https?:\/\//i.test(raw) ? raw : `https://${raw}`);
    const local = /^(localhost|127\.0\.0\.1|192\.168\.|10\.)/.test(parsed.hostname);
    parsed.protocol = local ? 'http:' : 'https:';
    parsed.pathname = '/api/ext/callback';
    parsed.search = '';
    parsed.hash = '';
    return parsed.toString().replace(/\/$/, '');
  } catch {
    return 'http://127.0.0.1:8001/api/ext/callback';
  }
}
let metrics = {
  tokenCapturedAt: null,
  requestCount: 0,   // captcha-consuming requests only (gen image/video/upscale)
  successCount: 0,
  failedCount: 0,
  lastError: null,
};

// ─── URL → Log Type Classifier ─────────────────────────────

// Visible log types — only these appear in the request log
const _VISIBLE_TYPES = new Set(['GEN_IMG', 'GEN_VID', 'GEN_VID_REF', 'UPSCALE', 'TRACKING', 'URL_REFRESH']);

function _classifyApiUrl(url) {
  if (url.includes('uploadImage')) return 'UPLOAD';
  if (url.includes('batchGenerateImages')) return 'GEN_IMG';
  if (url.includes('UpsampleVideo')) return 'UPSCALE';
  if (url.includes('ReferenceImages')) return 'GEN_VID_REF';
  if (url.includes('batchAsyncGenerateVideo')) return 'GEN_VID';
  if (url.includes('batchCheckAsync')) return 'POLL';
  if (url.includes('upsampleImage')) return 'UPS_IMG';
  if (url.includes('/media/')) return 'MEDIA';
  if (url.includes('/credits')) return 'CREDITS';
  return 'API';
}

// ─── Request Log ────────────────────────────────────────────

let requestLog = [];

function addRequestLog(entry) {
  requestLog.unshift(entry);
  if (requestLog.length > 100) requestLog.pop();
  chrome.storage.local.set({ requestLog }).catch(() => {});
  broadcastRequestLog();
}

function updateRequestLog(id, updates) {
  const entry = requestLog.find((e) => e.id === id);
  if (entry) Object.assign(entry, updates);
  chrome.storage.local.set({ requestLog }).catch(() => {});
  broadcastRequestLog();
}

function broadcastRequestLog() {
  chrome.runtime.sendMessage({ type: 'REQUEST_LOG_UPDATE', log: requestLog }).catch(() => { });
}

// ─── Startup ────────────────────────────────────────────────

let initialization;
function ensureInitialized() {
  if (!initialization) initialization = init().catch((error) => {
    initialization = null;
    console.error('[Flow Agent] Initialization failed:', error);
  });
  return initialization;
}

chrome.runtime.onInstalled.addListener(ensureInitialized);
chrome.runtime.onStartup.addListener(ensureInitialized);
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === 'reconnect') connectToAgent();
  if (alarm.name === 'keepAlive') keepAlive();
  if (alarm.name === 'flushOutbox') flushOutbox();
  if (alarm.name === 'closeIdleFlowTab') await closeIdleFlowTab();
  if (alarm.name === BROWSER_WINDOW_GRACE_ALARM) await finalizeBrowserWindowClose();
});

function scheduleBrowserWindowCloseGrace() {
  chrome.alarms.create(BROWSER_WINDOW_GRACE_ALARM, {
    delayInMinutes: BROWSER_WINDOW_CLOSE_GRACE_MINUTES,
  });
}

async function finalizeBrowserWindowClose() {
  const presence = await getBrowserPresence();
  if (presence.browserOpen) {
    manualDisconnect = false;
    await connectToAgent();
    await sendBrowserPresence();
    return;
  }
  chrome.alarms.clear('reconnect');
  const activeSocket = ws;
  ws = null;
  try { activeSocket?.close(1000, 'EDGE_WINDOWS_CLOSED'); } catch { }
  setState('off');
}

// Edge can keep the extension worker alive after its last visible window is
// closed. Keep the bridge in a recoverable state for a short interval instead
// of immediately tearing it down; reopening Edge then reuses the same client
// without producing FLOW_UI_EXTENSION_NOT_AVAILABLE.
if (chrome.windows?.onRemoved) {
  chrome.windows.onRemoved.addListener((windowId) => {
    void clearAutomationWindow(windowId);
    setTimeout(async () => {
      const presence = await sendBrowserPresence();
      if (!presence.browserOpen) {
        setState('off');
        scheduleBrowserWindowCloseGrace();
      }
    }, 0);
  });
}

if (chrome.windows?.onCreated) {
  chrome.windows.onCreated.addListener(async (browserWindow) => {
    if (browserWindow?.type !== 'normal') return;
    chrome.alarms.clear(BROWSER_WINDOW_GRACE_ALARM);
    manualDisconnect = false;
    await connectToAgent();
    await sendBrowserPresence();
  });
}

async function init() {
  if (chrome.sidePanel?.setPanelBehavior) {
    try {
      await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
    } catch (error) {
      console.warn('[Flow Agent] Side Panel click behavior unavailable:', error.message);
    }
  }
  await chrome.storage.local.remove('customServerIp');
  const data = await chrome.storage.local.get(['flowKey', 'metrics', 'callbackSecret', 'callbackUrl', 'requestLog']);
  if (data.flowKey) flowKey = data.flowKey;
  if (data.metrics) Object.assign(metrics, data.metrics);
  if (data.callbackSecret) callbackSecret = data.callbackSecret;
  if (data.callbackUrl) callbackUrl = normalizeCallbackUrl(data.callbackUrl);
  if (Array.isArray(data.requestLog)) requestLog = data.requestLog.slice(0, 100);
  await loadOutbox();
  connectToAgent();
  // 0.5 min is Chrome's minimum alarm period — anything lower is silently clamped.
  chrome.alarms.create('keepAlive', { periodInMinutes: 0.5 });
  // Retry any responses left undelivered by a previous worker lifetime.
  chrome.alarms.create('flushOutbox', { periodInMinutes: 0.5 });
  flushOutbox();
}

ensureInitialized();

// ─── Token Capture ──────────────────────────────────────────

chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    if (!details?.requestHeaders?.length) return;
    const authHeader = details.requestHeaders.find(
      (h) => h.name?.toLowerCase() === 'authorization',
    );
    const value = authHeader?.value || '';
    if (!value.startsWith('Bearer ya29.')) return;

    const token = value.replace(/^Bearer\s+/i, '').trim();
    if (!token) return;

    // Always update — even if same token string, refresh the timestamp
    flowKey = token;
    metrics.tokenCapturedAt = Date.now();
    chrome.storage.local.set({ flowKey, metrics });
    console.log('[Flow Agent] Bearer token captured');

    // Notify agent
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'token_captured', flowKey }));
    }
  },
  { urls: ['https://aisandbox-pa.googleapis.com/*', 'https://flow.google.com/*', 'https://labs.google/*'] },
  ['requestHeaders', 'extraHeaders'],
);

let _openingFlowTab = false;

// ─── On-demand tab lifecycle ────────────────────────────────
// Open the Flow tab only when real work needs it (token capture or captcha).
// Keep it available in the background so user tabs are never redirected.
const FLOW_TAB_URLS = [
  'https://flow.google.com/*',
  'https://labs.google/fx/tools/flow*',
  'https://labs.google/fx/*/tools/flow*',
];
const FLOW_URL = 'https://flow.google.com/';
// A workspace checkpoint carries the Flow project id, not a durable tab id.
// These are the supported project route shapes seen across Flow revisions.
// Reopening one of these URLs is safe because it can only restore the known
// project; it must never create a replacement project.
const FLOW_PROJECT_URL_BUILDERS = [
  (projectId) => 'https://flow.google.com/u/0/project/' + encodeURIComponent(projectId),
  (projectId) => 'https://flow.google.com/u/1/project/' + encodeURIComponent(projectId),
  (projectId) => 'https://flow.google.com/project/' + encodeURIComponent(projectId),
  (projectId) => 'https://flow.google.com/projects/' + encodeURIComponent(projectId),
  (projectId) => 'https://labs.google/fx/tools/flow/project/' + encodeURIComponent(projectId),
];
// Try each known route for a bounded interval. There may be one remembered
// URL plus all route builders, so the caller's workspace deadline must be
// longer than the sum of every route attempt; otherwise it reports an empty
// tab id while this recovery operation is still running in the background.
const FLOW_PROJECT_REOPEN_TIMEOUT_MS = 12000;
const FLOW_PROJECT_REOPEN_MARGIN_MS = 30000;
const FLOW_WORKSPACE_TAB_OPEN_TIMEOUT_MS = 120000;
const FLOW_PROJECT_PROBE_TIMEOUT_MS = 5000;
const FLOW_WORK_TAB_SESSION_KEY = 'docurandomFlowWorkTab';
// Keep one extension-owned, minimized Edge window alive across workspace
// batches. Closing the last Flow tab tears down the browser window and makes
// the extension bridge look disconnected to the desktop while the next
// workspace is starting. The window is a private parking slot; user-owned
// Edge windows are never minimized or closed by this lifecycle.
const FLOW_AUTOMATION_WINDOW_SESSION_KEY = 'docurandomFlowAutomationWindow';
const FLOW_WORKSPACE_TABS_SESSION_KEY = 'docurandomFlowWorkspaceTabs';
let workTabId = null;
let flowTabOpening = null;
// Workspace-specific open paths can enter the extension concurrently. Keep
// actual tab/window creation serialized so two callers cannot both observe
// "no window" and create two minimized Edge windows before either result is
// remembered.
let flowTabCreationLock = Promise.resolve();
// Serialize project switches as well as physical tab creation. Two
// workspaces can otherwise race on the same parked Flow tab and cause the
// recovery path to open another tab for every monitor cycle.
let flowSharedTabLock = Promise.resolve();
let workTabCreatedByExtension = false;
const workspaceFlowTabs = new Map();
const workspaceFlowOpenings = new Map();
const workspaceFlowReopenings = new Map();
let workspaceFlowTabsLoaded = false;
let workspaceFlowTabsLoading = null;

function cleanWorkspaceIdentity(value) {
  return String(value || '').trim().slice(0, 200);
}

function flowProjectIdFromUrl(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  const decode = (candidate) => {
    try { return decodeURIComponent(String(candidate || '').trim()); } catch { return String(candidate || '').trim(); }
  };
  const fromPath = (candidate) => {
    const match = String(candidate || '').match(/\/(?:projects?)\/([^/?#]+)/i);
    return match ? decode(match[1]) : '';
  };
  try {
    const parsed = new URL(raw);
    const pathId = fromPath(parsed.pathname);
    if (pathId) return pathId;
    for (const key of ['project', 'projectId', 'project_id']) {
      const queryId = decode(parsed.searchParams.get(key));
      if (queryId) return queryId;
    }
    const hashId = fromPath(parsed.hash);
    if (hashId) return hashId;
    for (const key of ['project', 'projectId', 'project_id']) {
      const queryId = decode(parsed.hash.replace(/^#\?/, '').replace(/^#/, '').split('&')
        .map((part) => part.split('='))
        .find((part) => part[0] === key)?.[1] || '');
      if (queryId) return queryId;
    }
  } catch { /* fall through to a pathname/hash-shaped value */ }
  return fromPath(raw);
}

function flowProjectIdFromTab(tab) {
  return flowProjectIdFromUrl(tab?.url) || flowProjectIdFromUrl(tab?.pendingUrl);
}

async function probeFlowTabProjectId(tab, expectedProjectId) {
  const expected = cleanWorkspaceIdentity(expectedProjectId);
  if (!tab?.id || !expected || !isFlowUrl(tab.url || tab.pendingUrl)) return '';
  const results = await withUiDeadline(
    chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => {
        const values = [location.href];
        const roots = [
          document.documentElement,
          document.body,
          ...document.querySelectorAll(
            '[data-project-id],[data-projectid],[data-project]',
          ),
        ];
        for (const root of roots.slice(0, 80)) {
          for (const name of ['data-project-id', 'data-projectid', 'data-project']) {
            const value = root?.getAttribute?.(name);
            if (value) values.push(value);
          }
        }
        return values;
      },
    }),
    FLOW_PROJECT_PROBE_TIMEOUT_MS,
    'project-probe',
  ).catch(() => []);
  const values = results.flatMap((entry) => (
    Array.isArray(entry?.result) ? entry.result : []
  ));
  return values
    .map((value) => flowProjectIdFromUrl(value) || cleanWorkspaceIdentity(value))
    .find((value) => value === expected) || '';
}

function workspaceTabRecord(
  tab,
  projectId = '',
  createdByExtension = false,
  projectUrl = '',
) {
  const tabUrl = String(tab?.url || tab?.pendingUrl || '').trim();
  return {
    tabId: Number(tab?.id || 0),
    windowId: Number(tab?.windowId || 0) || null,
    projectId: cleanWorkspaceIdentity(projectId) || flowProjectIdFromTab(tab),
    projectUrl: String(projectUrl || tabUrl).trim().slice(0, 1000),
    createdByExtension: Boolean(createdByExtension),
  };
}

async function persistWorkspaceFlowTabs() {
  const value = Object.fromEntries(workspaceFlowTabs.entries());
  await chrome.storage.session.set({ [FLOW_WORKSPACE_TABS_SESSION_KEY]: value }).catch(() => {});
}

async function restoreWorkspaceFlowTabs() {
  if (workspaceFlowTabsLoaded) return;
  if (workspaceFlowTabsLoading) return workspaceFlowTabsLoading;
  workspaceFlowTabsLoading = (async () => {
    const stored = await chrome.storage.session.get(FLOW_WORKSPACE_TABS_SESSION_KEY).catch(() => ({}));
    const raw = stored?.[FLOW_WORKSPACE_TABS_SESSION_KEY];
    for (const [workspaceId, value] of Object.entries(raw && typeof raw === 'object' ? raw : {})) {
      const savedProjectId = cleanWorkspaceIdentity(value?.projectId)
        || flowProjectIdFromUrl(value?.projectUrl || value?.url);
      const savedProjectUrl = String(value?.projectUrl || value?.url || '').trim();
      const tabId = Number(value?.tabId || 0);
      // Keep the project identity even when the old tab was closed. The next
      // workspace request can then reopen the exact project instead of
      // treating a stale tab id as a missing project.
      if (!tabId) {
        if (savedProjectId) {
          workspaceFlowTabs.set(workspaceId, workspaceTabRecord(
            { id: 0 },
            savedProjectId,
            value?.createdByExtension === true,
            savedProjectUrl,
          ));
        }
        continue;
      }
      const tab = await chrome.tabs.get(tabId).catch(() => null);
      if (!tab) {
        if (savedProjectId) {
          workspaceFlowTabs.set(workspaceId, workspaceTabRecord(
            { id: 0 },
            savedProjectId,
            value?.createdByExtension === true,
            savedProjectUrl,
          ));
        }
        continue;
      }
      const tabUrl = tab.url || tab.pendingUrl;
      if (!isFlowUrl(tabUrl) && !isGoogleAccountsUrl(tabUrl)) continue;
      workspaceFlowTabs.set(workspaceId, workspaceTabRecord(
        tab,
        savedProjectId,
        value?.createdByExtension === true,
        savedProjectUrl,
      ));
    }
    workspaceFlowTabsLoaded = true;
    workspaceFlowTabsLoading = null;
  })();
  return workspaceFlowTabsLoading;
}

async function rememberWorkspaceFlowTab(
  workspaceId,
  tab,
  projectId = '',
  createdByExtension = false,
  projectUrl = '',
) {
  const cleanId = cleanWorkspaceIdentity(workspaceId);
  if (!cleanId || !tab?.id) return;
  await restoreWorkspaceFlowTabs();
  workspaceFlowTabs.set(
    cleanId,
    workspaceTabRecord(tab, projectId, createdByExtension, projectUrl),
  );
  await persistWorkspaceFlowTabs();
}

function flowProjectUrlCandidates(projectId, remembered = {}) {
  const expected = cleanWorkspaceIdentity(projectId);
  if (!expected) return [];
  const candidates = [];
  const add = (value) => {
    const candidate = String(value || '').trim();
    if (!candidate || !isFlowUrl(candidate)
      || flowProjectIdFromUrl(candidate) !== expected
      || candidates.includes(candidate)) return;
    candidates.push(candidate);
  };
  add(remembered?.projectUrl);
  for (const build of FLOW_PROJECT_URL_BUILDERS) {
    try { add(build(expected)); } catch { /* invalid builder cannot block recovery */ }
  }
  return candidates;
}

function workspaceFlowTabOpenTimeoutMs(projectId, remembered = {}) {
  const candidateCount = Math.max(
    1,
    flowProjectUrlCandidates(projectId, remembered).length,
  );
  return Math.max(
    FLOW_WORKSPACE_TAB_OPEN_TIMEOUT_MS,
    candidateCount * FLOW_PROJECT_REOPEN_TIMEOUT_MS + FLOW_PROJECT_REOPEN_MARGIN_MS,
  );
}

async function isExactFlowProjectTab(tab, expectedProjectId) {
  const expected = cleanWorkspaceIdentity(expectedProjectId);
  if (!tab?.id || !expected || !isFlowUrl(tab.url || tab.pendingUrl)) return false;
  if (flowProjectIdFromTab(tab) === expected) return true;
  // The shell route can render before Flow exposes the project identity in
  // the URL. Probe a few times without clicking or changing page state.
  for (let attempt = 0; attempt < 3; attempt += 1) {
    if (await probeFlowTabProjectId(tab, expected) === expected) return true;
    if (attempt < 2) await sleep(350);
  }
  return false;
}

async function findWorkspaceFlowTab(workspaceId, projectId = '') {
  const cleanId = cleanWorkspaceIdentity(workspaceId);
  const cleanProjectId = cleanWorkspaceIdentity(projectId);
  if (!cleanId) return null;
  await restoreWorkspaceFlowTabs();
  const remembered = workspaceFlowTabs.get(cleanId);
  if (remembered?.tabId) {
    const tab = await chrome.tabs.get(remembered.tabId).catch(() => null);
    const tabUrl = tab?.url || tab?.pendingUrl;
    // The URL is the authority for the project currently loaded in the tab.
    // A stale storage record must never make a tab with another project look
    // like the requested workspace project.
    const exactFlowTab = tab && isFlowUrl(tabUrl)
      && (!cleanProjectId || await isExactFlowProjectTab(tab, cleanProjectId));
    const rememberedLoginTab = tab && cleanProjectId
      && remembered.projectId === cleanProjectId
      && isGoogleAccountsUrl(tabUrl);
    if (exactFlowTab || rememberedLoginTab || (tab && !cleanProjectId && isFlowUrl(tabUrl))) {
      if (cleanProjectId && remembered.projectId !== cleanProjectId) {
        await rememberWorkspaceFlowTab(
          cleanId,
          tab,
          cleanProjectId,
          remembered.createdByExtension,
          tabUrl,
        );
      }
      return { tab, record: workspaceFlowTabs.get(cleanId) || remembered };
    }
    if (remembered.projectId) {
      // Keep the known project id but clear only the dead/mismatched tab id.
      // This record is the recovery key for the next request.
      workspaceFlowTabs.set(
        cleanId,
        workspaceTabRecord(
          { id: 0 },
          remembered.projectId,
          remembered.createdByExtension,
          remembered.projectUrl,
        ),
      );
      await persistWorkspaceFlowTabs();
    } else {
      workspaceFlowTabs.delete(cleanId);
      await persistWorkspaceFlowTabs();
    }
  }
  if (!cleanProjectId) return null;
  // URL-filtered queries are not reliable across Chromium forks when a tab is
  // still in a shell/pending navigation state. Query the tab inventory and
  // apply the same strict Flow/project checks locally before opening anything.
  const tabs = (await chrome.tabs.query({}).catch(() => []))
    .filter((tab) => isFlowUrl(tab?.url || tab?.pendingUrl));
  const { browserWindow: automationWindow, tabs: automationTabs } =
    await getAutomationFlowTabs();
  const automationTabIds = new Set(automationTabs.map((tab) => Number(tab.id)));
  const candidate = tabs.find(
    (tab) => automationTabIds.has(Number(tab.id))
      && flowProjectIdFromTab(tab) === cleanProjectId,
  ) || tabs.find((tab) => flowProjectIdFromTab(tab) === cleanProjectId);
  if (candidate?.id) {
    await rememberWorkspaceFlowTab(
      cleanId,
      candidate,
      cleanProjectId,
      automationTabIds.has(Number(candidate.id)),
      candidate.url || candidate.pendingUrl,
    );
    if (automationWindow?.id && automationTabIds.has(Number(candidate.id))) {
      const recordsByTabId = new Map();
      for (const record of workspaceFlowTabs.values()) {
        const tabId = Number(record?.tabId || 0);
        if (tabId) recordsByTabId.set(tabId, record);
      }
      await collapseOwnedAutomationFlowTabs(
        candidate.id,
        automationWindow,
        recordsByTabId,
      );
    }
    return { tab: candidate, record: workspaceFlowTabs.get(cleanId) };
  }
  // Some Flow revisions keep the project identity in the page state while
  // the address bar contains only a shell route. Probe that state read-only;
  // never click a project link or adopt a tab without an exact match.
  for (const tab of tabs) {
    if (!(await isExactFlowProjectTab(tab, cleanProjectId))) continue;
    await rememberWorkspaceFlowTab(
      cleanId,
      tab,
      cleanProjectId,
      false,
      tab.url || tab.pendingUrl,
    );
    return { tab, record: workspaceFlowTabs.get(cleanId) };
  }
  return null;
}

function isSafeBackgroundSharedFlowTab(tab, browserWindow) {
  if (!tab?.id || !isFlowUrl(tab.url || tab.pendingUrl)) return false;
  // Reuse the tool's current tab across workspace projects only when doing so
  // cannot replace the page the user is actively looking at. A minimized
  // automation window, an inactive tab, or an unfocused window is safe.
  return browserWindow?.state === 'minimized'
    || tab.active === false
    || browserWindow?.focused === false;
}

async function findReusableFlowTab(expectedProjectId = '') {
  const expected = cleanWorkspaceIdentity(expectedProjectId);
  await restoreWorkspaceFlowTabs();
  // MV3 service workers are restarted after idle periods. Restore the single
  // shared tab identity from session storage before considering a new tab.
  await restoreRememberedWorkTab();
  const rememberedByTabId = new Map();
  for (const record of workspaceFlowTabs.values()) {
    const tabId = Number(record?.tabId || 0);
    if (tabId) rememberedByTabId.set(tabId, record);
  }

  // Do not use the URL-filtered tabs API here. Chromium forks can omit a tab
  // while it is still in a pending navigation state, which was the reason the
  // old code incorrectly concluded that it needed another Edge window.
  const tabs = (await chrome.tabs.query({}).catch(() => []))
    .filter((tab) => isFlowUrl(tab?.url || tab?.pendingUrl))
    .filter((tab) => tab?.discarded !== true);
  const { browserWindow: automationWindow, tabs: automationTabs } =
    await getAutomationFlowTabs();
  const automationTabIds = new Set(automationTabs.map((tab) => Number(tab.id)));
  const candidates = [];
  const exactProjectTabIds = new Set(
    expected
      ? tabs
        .filter((tab) => {
          const tabId = Number(tab?.id || 0);
          const rememberedProjectId = cleanWorkspaceIdentity(
            rememberedByTabId.get(tabId)?.projectId,
          );
          return flowProjectIdFromTab(tab) === expected
            || rememberedProjectId === expected;
        })
        .map((tab) => Number(tab.id))
      : [],
  );

  // The extension may have adopted a Flow tab opened by the user's explicit
  // "Cập nhật/Mở Flow" action. If its project changes, keep using that same
  // background tab instead of accumulating one new tab per workspace. Exact
  // project matches below still win, and a focused active tab is never taken
  // over.
  let sharedWorkTabId = 0;
  const currentWorkTabId = Number(workTabId || 0);
  if (currentWorkTabId && exactProjectTabIds.size === 0) {
    const currentWorkTab = tabs.find((tab) => Number(tab?.id || 0) === currentWorkTabId);
    if (currentWorkTab) {
      const currentWindow = typeof chrome.windows?.get === 'function'
        ? await chrome.windows.get(currentWorkTab.windowId).catch(() => null)
        : null;
      if (isSafeBackgroundSharedFlowTab(currentWorkTab, currentWindow)) {
        sharedWorkTabId = currentWorkTabId;
        candidates.push({
          tab: currentWorkTab,
          record: rememberedByTabId.get(currentWorkTabId),
          score: 2600,
          dedicated: automationTabIds.has(currentWorkTabId),
          createdByExtension: workTabCreatedByExtension
            || automationTabIds.has(currentWorkTabId),
          sharedWorkTab: true,
        });
      }
    }
  }

  for (const tab of tabs) {
    const tabId = Number(tab?.id || 0);
    if (!tabId) continue;
    if (tabId === sharedWorkTabId) continue;
    const record = rememberedByTabId.get(tabId);
    const urlProjectId = flowProjectIdFromTab(tab);
    const rememberedProjectId = cleanWorkspaceIdentity(record?.projectId);

    // The dedicated minimized window is the single physical Flow slot. Its
    // current URL may belong to the previous workspace; the caller navigates
    // this same tab to the requested project after taking the shared lock.
    // This check must happen before per-workspace mismatch filtering or every
    // project switch/recovery will create another tab.
    if (automationTabIds.has(tabId)) {
      let score = 2400;
      if (expected && urlProjectId === expected) score += 500;
      if (tabId === Number(workTabId || 0)) score += 100;
      if (!urlProjectId) score += 25;
      candidates.push({
        tab,
        record,
        score,
        dedicated: true,
        createdByExtension: true,
      });
      continue;
    }

    // Do not fall back to the same user-visible managed tab if it was rejected
    // above. The caller can create/reuse the dedicated background slot instead.
    if (tabId === currentWorkTabId && !sharedWorkTabId) continue;

    // A tab remembered for another workspace is not a safe host. An exact
    // project remains reusable, while a project-less Flow home tab is the
    // intended target opened by the user's "Cập nhật/Mở Flow" action.
    if (rememberedProjectId && (!expected || rememberedProjectId !== expected)) {
      continue;
    }
    if (expected && urlProjectId && urlProjectId !== expected) continue;
    if (expected && urlProjectId === expected) {
      candidates.push({ tab, record, score: 1000 });
      continue;
    }
    if (urlProjectId) continue;
    let score = 100;
    if (record && !rememberedProjectId) score += 500;
    if (isLegacyFlowMarketingLanding(tab.url || tab.pendingUrl)) score += 100;
    if (tab.status === 'complete') score += 20;
    candidates.push({
      tab,
      record,
      score,
      dedicated: false,
      createdByExtension: record?.createdByExtension === true,
    });
  }
  candidates.sort((left, right) => right.score - left.score);
  const selected = candidates[0] || null;
  if (selected?.dedicated && automationWindow?.id) {
    await collapseOwnedAutomationFlowTabs(
      selected.tab.id,
      automationWindow,
      rememberedByTabId,
    );
  }
  return selected;
}

async function reopenWorkspaceFlowTab(workspaceId, projectId, remembered = {}) {
  const cleanId = cleanWorkspaceIdentity(workspaceId);
  const cleanProjectId = cleanWorkspaceIdentity(projectId);
  if (!cleanId || !cleanProjectId) {
    throw new Error('FLOW_WORKSPACE_PROJECT_REOPEN_FAILED');
  }
  const key = [cleanId, cleanProjectId].join('\u0000');
  const pending = workspaceFlowReopenings.get(key);
  if (pending) return pending;

  const operation = (async () => {
    const candidates = flowProjectUrlCandidates(cleanProjectId, remembered);
    if (!candidates.length) {
      throw new Error('FLOW_WORKSPACE_PROJECT_REOPEN_FAILED');
    }
    let created = null;
    let reusedExisting = false;
    let previousUrl = '';
    let loginCandidate = '';
    try {
      // Prefer the Flow tab opened by the user or by "Cập nhật/Mở Flow".
      // Only create a tab when no safe existing Flow home/project tab exists.
      const reusable = await findReusableFlowTab(cleanProjectId);
      created = reusable?.tab || await createFlowTabWithWindowFallback(candidates[0]);
      reusedExisting = Boolean(reusable?.tab?.id);
      previousUrl = String(created.url || created.pendingUrl || '').trim();
      const createdByExtension = reusedExisting
        ? reusable?.createdByExtension === true
        : true;
      await rememberWorkspaceFlowTab(
        cleanId,
        created,
        cleanProjectId,
        createdByExtension,
        candidates[0],
      );
      await rememberWorkTab(created, createdByExtension);
      try { await chrome.tabs.update(created.id, { autoDiscardable: false }); } catch { }

      for (let index = 0; index < candidates.length; index += 1) {
        const candidateUrl = candidates[index];
        if (index > 0 || (reusedExisting && previousUrl !== candidateUrl)) {
          await chrome.tabs.update(created.id, { url: candidateUrl });
        }
        const tab = await waitForTabComplete(
          created.id,
          FLOW_PROJECT_REOPEN_TIMEOUT_MS,
        ).catch(() => null);
        if (!tab) continue;
        const tabUrl = tab.url || tab.pendingUrl;
        // A direct project route can legitimately land on Google Accounts.
        // Keep that exact tab so the user can sign in and press Continue;
        // try the other account-index route shapes first, because the saved
        // project may belong to a non-default Google account.
        if (isGoogleAccountsUrl(tabUrl)) {
          loginCandidate = candidateUrl;
          continue;
        }
        if (!(await isExactFlowProjectTab(tab, cleanProjectId))) continue;
        await rememberWorkspaceFlowTab(
          cleanId,
          tab,
          cleanProjectId,
          createdByExtension,
          tabUrl || candidateUrl,
        );
        await rememberWorkTab(tab, createdByExtension);
        return tab;
      }
      if (loginCandidate) {
        await chrome.tabs.update(created.id, { url: loginCandidate });
        const loginTab = await waitForTabComplete(
          created.id,
          FLOW_PROJECT_REOPEN_TIMEOUT_MS,
        ).catch(() => created);
        await rememberWorkspaceFlowTab(
          cleanId,
          loginTab,
          cleanProjectId,
          createdByExtension,
          loginCandidate,
        );
        await rememberWorkTab(loginTab, createdByExtension);
        return loginTab;
      }
      throw new Error('FLOW_WORKSPACE_PROJECT_REOPEN_FAILED');
    } catch (error) {
      if (created?.id && workTabId === created.id) await forgetWorkTab();
      if (created?.id && !reusedExisting) {
        await chrome.tabs.remove(created.id).catch(() => {});
      } else if (created?.id && previousUrl && isFlowUrl(previousUrl)) {
        // A user-owned Flow tab must survive a failed project probe. Restore
        // its original route instead of deleting or leaving it on a broken
        // project URL.
        await chrome.tabs.update(created.id, { url: previousUrl }).catch(() => {});
      }
      // Preserve the project key for the next Continue/retry. A failed
      // reopen is never converted into a project-creation request.
      workspaceFlowTabs.set(
        cleanId,
        workspaceTabRecord(
          { id: 0 },
          cleanProjectId,
          reusedExisting
            ? reusable?.createdByExtension === true
            : true,
          candidates[0],
        ),
      );
      await persistWorkspaceFlowTabs();
      if (String(error?.message || '') === 'FLOW_WORKSPACE_PROJECT_REOPEN_FAILED') {
        throw error;
      }
      throw new Error('FLOW_WORKSPACE_PROJECT_REOPEN_FAILED');
    }
  })();
  const tracked = operation.finally(() => {
    if (workspaceFlowReopenings.get(key) === tracked) {
      workspaceFlowReopenings.delete(key);
    }
  });
  workspaceFlowReopenings.set(key, tracked);
  return tracked;
}

async function getOrOpenWorkspaceFlowTab({ workspaceId, projectId = '', newProject = false }) {
  const cleanId = cleanWorkspaceIdentity(workspaceId);
  const cleanProjectId = cleanWorkspaceIdentity(projectId);
  if (!cleanId) return null;
  const key = [cleanId, cleanProjectId, newProject ? 'new' : 'existing'].join('\u0000');
  const pending = workspaceFlowOpenings.get(key);
  if (pending) return pending;
  const operation = withSharedFlowTabLock(async () => {
    const existing = await findWorkspaceFlowTab(cleanId, cleanProjectId);
    if (existing && !newProject) {
      await rememberWorkTab(existing.tab, existing.record?.createdByExtension === true);
      return existing.tab;
    }
    // A pending workspace tab is still on the Flow home page. Reuse it for the
    // first project-creation request, but never reuse a tab already bound to a
    // different project's workspace.
    if (existing && newProject && !existing.record?.projectId) {
      await rememberWorkTab(existing.tab, existing.record?.createdByExtension === true);
      return existing.tab;
    }
    if (!newProject && cleanProjectId) {
      // Tab IDs are ephemeral. Once a checkpoint has a project ID, a closed or
      // mismatched tab is recoverable: reopen the exact project and validate it
      // before the UI driver is allowed to continue.
      return reopenWorkspaceFlowTab(
        cleanId,
        cleanProjectId,
        workspaceFlowTabs.get(cleanId) || {},
      );
    }
    if (!newProject) throw new Error('FLOW_WORKSPACE_PROJECT_TAB_NOT_FOUND');
    const reusable = await findReusableFlowTab();
    const created = reusable?.tab || await createFlowTabWithWindowFallback();
    const createdByExtension = reusable
      ? reusable.createdByExtension === true
      : true;
    await rememberWorkspaceFlowTab(cleanId, created, '', createdByExtension);
    await rememberWorkTab(created, createdByExtension);
    await sleep(3000);
    const ready = await chrome.tabs.get(created.id).catch(() => created);
    try { await chrome.tabs.update(ready.id, { autoDiscardable: false }); } catch { }
    return ready;
  });
  const tracked = operation.finally(() => {
    if (workspaceFlowOpenings.get(key) === tracked) workspaceFlowOpenings.delete(key);
  });
  workspaceFlowOpenings.set(key, tracked);
  return tracked;
}

async function rememberWorkTab(tab, createdByExtension) {
  workTabId = tab?.id || null;
  workTabCreatedByExtension = Boolean(workTabId && createdByExtension);
  if (!workTabId) {
    await chrome.storage.session.remove(FLOW_WORK_TAB_SESSION_KEY).catch(() => {});
    return;
  }
  await chrome.storage.session.set({
    [FLOW_WORK_TAB_SESSION_KEY]: {
      tabId: workTabId,
      windowId: tab.windowId || null,
      createdByExtension: workTabCreatedByExtension,
    },
  }).catch(() => {});
}

async function forgetWorkTab() {
  workTabId = null;
  workTabCreatedByExtension = false;
  await chrome.storage.session.remove(FLOW_WORK_TAB_SESSION_KEY).catch(() => {});
}

async function rememberAutomationWindow(windowId) {
  const normalized = Number(windowId || 0);
  if (!normalized) return;
  await chrome.storage.session.set({
    [FLOW_AUTOMATION_WINDOW_SESSION_KEY]: { windowId: normalized },
  }).catch(() => {});
}

async function clearAutomationWindow(windowId = null) {
  const stored = await chrome.storage.session.get(
    FLOW_AUTOMATION_WINDOW_SESSION_KEY,
  ).catch(() => ({}));
  const rememberedId = Number(
    stored?.[FLOW_AUTOMATION_WINDOW_SESSION_KEY]?.windowId || 0,
  );
  if (!windowId || rememberedId === Number(windowId)) {
    await chrome.storage.session.remove(FLOW_AUTOMATION_WINDOW_SESSION_KEY)
      .catch(() => {});
  }
}

async function getAutomationWindow() {
  const stored = await chrome.storage.session.get(
    FLOW_AUTOMATION_WINDOW_SESSION_KEY,
  ).catch(() => ({}));
  const windowId = Number(
    stored?.[FLOW_AUTOMATION_WINDOW_SESSION_KEY]?.windowId || 0,
  );
  if (!windowId || typeof chrome.windows?.get !== 'function') return null;
  const browserWindow = await chrome.windows.get(windowId).catch(() => null);
  if (!browserWindow || browserWindow.type !== 'normal') {
    await clearAutomationWindow(windowId);
    return null;
  }
  return browserWindow;
}

async function getAutomationFlowTabs() {
  const browserWindow = await getAutomationWindow();
  if (!browserWindow?.id || typeof chrome.windows?.get !== 'function') {
    return { browserWindow: null, tabs: [] };
  }
  const detailed = await chrome.windows.get(browserWindow.id, { populate: true })
    .catch(() => browserWindow);
  const tabs = Array.isArray(detailed?.tabs)
    ? detailed.tabs.filter((tab) => isFlowUrl(tab?.url || tab?.pendingUrl))
    : [];
  return { browserWindow: detailed || browserWindow, tabs };
}

async function collapseOwnedAutomationFlowTabs(keepTabId, browserWindow, recordsByTabId) {
  const keep = Number(keepTabId || 0);
  if (!keep || !browserWindow?.id || !Array.isArray(browserWindow.tabs)) return;
  let recordsChanged = false;
  for (const tab of browserWindow.tabs) {
    const tabId = Number(tab?.id || 0);
    if (!tabId || tabId === keep || !isFlowUrl(tab?.url || tab?.pendingUrl)) continue;
    const record = recordsByTabId.get(tabId);
    // Only retire tabs explicitly created by this extension. A user-owned
    // Flow tab must survive even when it is in the same Edge window.
    if (record?.createdByExtension !== true || activeUiJob?.tabId === tabId) continue;
    const projectId = cleanWorkspaceIdentity(
      record.projectId || flowProjectIdFromTab(tab),
    );
    for (const [workspaceId, workspaceRecord] of workspaceFlowTabs.entries()) {
      if (Number(workspaceRecord?.tabId || 0) !== tabId) continue;
      workspaceFlowTabs.set(
        workspaceId,
        workspaceTabRecord(
          { id: 0 },
          projectId || workspaceRecord.projectId,
          true,
          workspaceRecord.projectUrl || tab.url || tab.pendingUrl,
        ),
      );
      recordsChanged = true;
    }
    if (workTabId === tabId) {
      workTabId = keep;
      workTabCreatedByExtension = true;
    }
    await chrome.tabs.remove(tabId).catch(() => {});
  }
  if (recordsChanged) await persistWorkspaceFlowTabs();
}

async function parkAutomationWindow(windowId) {
  const normalized = Number(windowId || 0);
  if (!normalized || typeof chrome.windows?.update !== 'function') return;
  // This is deliberately limited to the recorded extension-owned window.
  // Never change the state/focus of a user's normal Edge window.
  await chrome.windows.update(normalized, {
    state: 'minimized',
    focused: false,
  }).catch(() => {});
}

async function closeCompletedWorkspaceFlowTab(workspaceId, tab) {
  const cleanId = cleanWorkspaceIdentity(workspaceId);
  const tabId = Number(tab?.id || 0);
  if (!cleanId || !tabId) {
    return { closed: false, reason: 'missing-workspace-or-tab' };
  }

  await restoreWorkspaceFlowTabs();
  const remembered = workspaceFlowTabs.get(cleanId);
  // Never close a tab the user opened. The extension-owned marker is also
  // what lets us close the dedicated minimized Edge window without touching
  // any normal Edge window used for other work.
  if (!remembered?.createdByExtension || Number(remembered.tabId || 0) !== tabId) {
    return { closed: false, reason: 'tab-not-extension-owned' };
  }

  const current = await chrome.tabs.get(tabId).catch(() => null);
  const currentUrl = current?.url || current?.pendingUrl || '';
  if (!current || (!isFlowUrl(currentUrl) && !isGoogleAccountsUrl(currentUrl))) {
    return { closed: false, reason: 'tab-changed-or-closed' };
  }

  const projectId = cleanWorkspaceIdentity(
    remembered.projectId || flowProjectIdFromTab(current),
  );
  const projectUrl = String(
    remembered.projectUrl || currentUrl,
  ).trim().slice(0, 1000);
  const windowId = Number(remembered.windowId || current.windowId || 0) || null;

  // Persist the project identity before removing the ephemeral tab id. A
  // later Continue/resume can reopen the exact project instead of treating a
  // completed render as an unbound workspace.
  workspaceFlowTabs.set(
    cleanId,
    workspaceTabRecord(
      { id: 0 },
      projectId,
      true,
      projectUrl,
    ),
  );
  await persistWorkspaceFlowTabs();
  const automationWindow = await getAutomationWindow();
  if (
    windowId !== null
    && automationWindow
    && Number(automationWindow.id) === windowId
  ) {
    try {
      // Keep one resident tab in the dedicated minimized window. Returning it
      // to Flow home makes it reusable by the next workspace while the
      // completed project's durable id remains in workspaceFlowTabs above.
      const parkedTab = await chrome.tabs.update(tabId, {
        url: FLOW_URL,
        active: false,
        autoDiscardable: false,
      });
      await rememberWorkTab(parkedTab, true);
      await parkAutomationWindow(windowId);
      console.log(
        `[Flow Agent] Parked completed workspace Flow tab workspace=${cleanId} tab=${tabId} window=${windowId}`,
      );
      return { closed: false, parked: true, tabId, windowId };
    } catch (error) {
      console.warn(
        `[Flow Agent] Completed workspace Flow tab park failed workspace=${cleanId} tab=${tabId}:`,
        error?.message || error,
      );
      return { closed: false, reason: String(error?.message || error) };
    }
  }

  // A legacy extension-owned tab may live in a user's normal Edge window. Do
  // not close or minimize that window during migration; the next job will use
  // the dedicated background window created by the new path.
  console.log(
    `[Flow Agent] Kept completed user-window Flow tab workspace=${cleanId} tab=${tabId}`,
  );
  return { closed: false, reason: 'tab-not-dedicated-automation-window', tabId, windowId };
}

if (chrome.tabs?.onRemoved) {
  chrome.tabs.onRemoved.addListener((tabId) => {
    let changed = false;
    for (const [workspaceId, record] of workspaceFlowTabs.entries()) {
      if (Number(record?.tabId || 0) !== Number(tabId)) continue;
      if (record?.projectId) {
        workspaceFlowTabs.set(
          workspaceId,
          workspaceTabRecord(
            { id: 0 },
            record.projectId,
            record.createdByExtension === true,
            record.projectUrl,
          ),
        );
      } else {
        workspaceFlowTabs.delete(workspaceId);
      }
      changed = true;
    }
    if (changed) void persistWorkspaceFlowTabs();
    if (workTabId === tabId) void forgetWorkTab();
  });
}

async function restoreRememberedWorkTab() {
  if (workTabId) return;
  const stored = await chrome.storage.session.get(FLOW_WORK_TAB_SESSION_KEY).catch(() => ({}));
  const remembered = stored?.[FLOW_WORK_TAB_SESSION_KEY];
  const rememberedId = Number(remembered?.tabId || 0);
  if (!rememberedId) return;
  const tab = await chrome.tabs.get(rememberedId).catch(() => null);
  // storage.session survives service-worker suspension, not a browser restart;
  // still validate the URL/window so an unrelated tab can never be adopted.
  const tabUrl = tab?.url || tab?.pendingUrl;
  if (!tab || (!isFlowUrl(tabUrl) && !isGoogleAccountsUrl(tabUrl))) {
    await chrome.storage.session.remove(FLOW_WORK_TAB_SESSION_KEY).catch(() => {});
    return;
  }
  if (remembered?.windowId && tab.windowId !== remembered.windowId) return;
  workTabId = tab.id;
  workTabCreatedByExtension = remembered?.createdByExtension === true;
}

async function findExistingFlowTab() {
  // URL-filtered queries can omit a tab during pending navigation on Chromium
  // forks. Inspect the inventory and apply the same semantic URL check used
  // by the workspace-aware path before deciding to create anything.
  const tabs = (await chrome.tabs.query({}).catch(() => []))
    .filter((tab) => isFlowUrl(tab?.url || tab?.pendingUrl));
  if (!tabs.length) return null;
  // Prefer the extension's already-minimized automation window. If an older
  // extension worker lost its in-memory id, this recovers that tab instead of
  // creating a new Edge window on every retry.
  const ranked = await Promise.all(tabs.map(async (tab) => {
    const browserWindow = typeof chrome.windows?.get === 'function'
      ? await chrome.windows.get(tab.windowId).catch(() => null)
      : null;
    let score = 0;
    if (browserWindow?.state === 'minimized') score += 100;
    if (tab.discarded !== true) score += 20;
    if (tab.status === 'complete') score += 10;
    score += Math.min(9, Math.floor(Number(tab.lastAccessed || 0) / 1e12));
    return { tab, score };
  }));
  ranked.sort((left, right) => right.score - left.score);
  return ranked[0]?.tab || null;
}

function scheduleFlowTabClose() {
  // The Flow browser is a resident background worker, like a Donut profile.
  // Do not close/recreate its only tab between workspace batches: that was the
  // source of repeated Edge flashes and bridge reconnect storms.
}

async function closeIdleFlowTab() {
  // Kept as an alarm-compatible no-op for profiles that still have an older
  // scheduled alarm after the extension updates. The resident window must
  // remain available so the WebSocket client does not repeatedly disconnect.
}

function isGoogleAccountsUrl(url) {
  return /^https:\/\/accounts\.google\.com\//i.test(String(url || ''));
}

function isFlowUrl(url) {
  return !!url && FLOW_TAB_URLS.some((p) => new RegExp(p.replace(/\./g, '\\.').replace(/\*/g, '.*')).test(url));
}

function isLegacyFlowMarketingLanding(url) {
  try {
    const parsed = new URL(String(url || ''));
    return parsed.hostname === 'labs.google'
      && /^\/fx(?:\/[^/]+)?\/tools\/flow\/?$/i.test(parsed.pathname);
  } catch {
    return false;
  }
}

async function withFlowTabCreationLock(operation) {
  const previous = flowTabCreationLock;
  let release;
  flowTabCreationLock = new Promise((resolve) => { release = resolve; });
  await previous;
  try {
    return await operation();
  } finally {
    release();
  }
}

async function withSharedFlowTabLock(operation) {
  const previous = flowSharedTabLock;
  let release;
  flowSharedTabLock = new Promise((resolve) => { release = resolve; });
  await previous.catch(() => {});
  try {
    return await operation();
  } finally {
    release();
  }
}

async function _createFlowTabWithWindowFallback(url = FLOW_URL) {
  // Routine Flow work belongs to one dedicated minimized Edge window. The old
  // implementation inserted a tab into whichever normal window was focused;
  // Edge then exposed the tab and sometimes moved the pointer to it. Reusing a
  // private parking window keeps Flow isolated from the user's work desktop.
  const targetUrl = String(url || FLOW_URL);
  const rememberedWindow = await getAutomationWindow();
  if (rememberedWindow?.id && typeof chrome.tabs?.create === 'function') {
    const createdTab = await chrome.tabs.create({
      windowId: rememberedWindow.id,
      url: targetUrl,
      active: false,
    }).catch(() => null);
    if (createdTab?.id) {
      await rememberAutomationWindow(rememberedWindow.id);
      await parkAutomationWindow(rememberedWindow.id);
      return createdTab;
    }
    await clearAutomationWindow(rememberedWindow.id);
  }

  // Always create the resident Flow window minimized, even when the user has
  // other Edge windows open. `focused:false` alone is not sufficient on every
  // Chromium/Windows build; the explicit minimized state is the important
  // part that prevents a new Flow tab from appearing in front of the user.
  if (typeof chrome.windows?.create === 'function') {
    const createdWindow = await chrome.windows.create({
      url: targetUrl,
      focused: false,
      state: 'minimized',
      type: 'normal',
    }).catch(() => null);
    const createdTab = createdWindow?.tabs?.[0] || null;
    if (createdTab?.id) {
      await rememberAutomationWindow(createdWindow.id || createdTab.windowId);
      await parkAutomationWindow(createdWindow.id || createdTab.windowId);
      return createdTab;
    }
  }

  // Compatibility fallback for Chromium forks that do not expose windows API.
  // It still avoids activation where the tabs API supports `active:false`.
  const createdTab = await chrome.tabs.create({
    url: targetUrl,
    active: false,
  }).catch(() => null);
  if (!createdTab?.id) throw new Error('FLOW_WINDOW_CREATE_FAILED');
  return createdTab;
}

async function createFlowTabWithWindowFallback(url = FLOW_URL) {
  return withFlowTabCreationLock(
    () => _createFlowTabWithWindowFallback(url),
  );
}

async function prepareFlowTab({ existingOnly = false } = {}) {
  // Health/precheck requests arrive once per workspace and are not part of
  // the UI generation queue. Serialize them here too, otherwise several
  // monitors can all observe a missing resident tab and create one each.
  return withSharedFlowTabLock(async () => {
    await restoreRememberedWorkTab();
    let managedTab = workTabId
      ? await chrome.tabs.get(workTabId).catch(() => null)
      : null;
    if (!managedTab?.id) {
      // Prefer an unbound Flow home tab, especially the one opened by the
      // user's "Cập nhật/Mở Flow" action, before adopting an older project tab.
      const reusable = await findReusableFlowTab();
      managedTab = reusable?.tab || await findExistingFlowTab();
      if (managedTab?.id && reusable?.tab?.id === managedTab.id) {
        workTabCreatedByExtension = reusable.createdByExtension === true;
      }
    }
    if (!managedTab?.id && !existingOnly) managedTab = await getOrOpenFlowTab();
    if (!managedTab?.id) return null;
    await rememberWorkTab(managedTab, workTabCreatedByExtension);
    try { await chrome.tabs.update(managedTab.id, { autoDiscardable: false }); } catch { }
    return managedTab;
  });
}

// Finds/wakes/creates the Flow tab. Returns
// the tab, or null if it couldn't be opened.
async function _getOrOpenFlowTab() {
  await restoreRememberedWorkTab();
  if (workTabId) {
    try {
      let tab = await chrome.tabs.get(workTabId);
      if (tab && !isFlowUrl(tab.url || tab.pendingUrl)) {
        // Flow can temporarily redirect this exact tab to Google Accounts.
        // Preserve that navigation so reconnect/precheck cannot interrupt the
        // user by replacing the sign-in page or spawning another Flow tab.
        console.log('[Flow Agent] Managed Flow tab is outside Flow; preserving possible sign-in navigation');
        return tab;
      }
      scheduleFlowTabClose();
      return tab;
    } catch (e) {
      await forgetWorkTab(); // closed by the user — find/reopen exactly one
    }
  }

  const existingTab = await findExistingFlowTab();
  if (existingTab?.id) {
    await rememberWorkTab(existingTab, false);
    return existingTab;
  }

  const createdTab = await createFlowTabWithWindowFallback();
  await rememberWorkTab(createdTab, true);
  await sleep(3000);
  scheduleFlowTabClose();
  return await chrome.tabs.get(workTabId);
}

async function getOrOpenFlowTab(options = {}) {
  const workspaceId = cleanWorkspaceIdentity(options.workspaceId);
  if (workspaceId) {
    return getOrOpenWorkspaceFlowTab({
      workspaceId,
      projectId: options.projectId,
      newProject: options.newProject === true,
    });
  }
  if (flowTabOpening) return flowTabOpening;
  flowTabOpening = _getOrOpenFlowTab();
  try {
    let tab = await flowTabOpening;
    if (tab?.id) {
      // labs.google/fx/tools/flow is now only a public marketing page. It has
      // no project composer, so waiting for the old add_2 control always times
      // out. Migrating this exact empty landing tab is safe: no project or
      // submitted generation exists to lose, and we still reuse one tab.
      if (isLegacyFlowMarketingLanding(tab.url || tab.pendingUrl)) {
        await chrome.tabs.update(tab.id, { url: FLOW_URL });
        tab = await waitForTabComplete(tab.id);
      }
      try {
        // Keep the workspace tab alive even when Edge is minimized or the user
        // works in another tab. This does not bring Flow to the foreground.
        await chrome.tabs.update(tab.id, { autoDiscardable: false });
      } catch { }
    }
    return tab;
  } finally {
    flowTabOpening = null;
  }
}

async function waitForTabComplete(tabId, timeoutMs = 45000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const tab = await chrome.tabs.get(tabId);
      if (tab?.status === 'complete') return tab;
    } catch { }
    await sleep(250);
  }
  throw new Error('FLOW_TAB_LOAD_TIMEOUT');
}

async function waitForFlowDocument(tabId) {
  const tab = await waitForTabComplete(tabId);
  const tabUrl = tab?.url || tab?.pendingUrl;
  if (/^https:\/\/accounts\.google\.com\//i.test(String(tabUrl || ''))) {
    throw new Error('FLOW_UI_LOGIN_REQUIRED');
  }
  if (!isFlowUrl(tabUrl)) throw new Error('FLOW_UI_NOT_ON_FLOW');
  return tab;
}

async function sendUiMessage(tabId, message) {
  try {
    return await chrome.tabs.sendMessage(tabId, message);
  } catch (firstError) {
    if (!/receiving end does not exist|could not establish connection/i.test(String(firstError))) {
      throw firstError;
    }
    // A previously-open tab may predate the extension update. Inject our own
    // local automation module once, then retry without refreshing the page.
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ['flow-selectors.js', 'flow-ui.js'],
    });
    await sleep(250);
    return chrome.tabs.sendMessage(tabId, message);
  }
}

function isUiMessageChannelClosedError(error) {
  const detail = String(error?.message || error || '');
  return /A listener indicated an asynchronous response/i.test(detail)
    || /message (?:channel|port) closed before a response/i.test(detail)
    || /the message port closed/i.test(detail);
}

async function wakeUiTabRenderer(tabId) {
  try {
    await chrome.tabs.update(tabId, { autoDiscardable: false });
  } catch { }
  if (uiDebuggerTabs.has(tabId)) {
    try {
      await chrome.debugger.sendCommand(
        { tabId },
        'Page.setWebLifecycleState',
        { state: 'active' },
      );
    } catch { }
    try {
      await chrome.debugger.sendCommand(
        { tabId },
        'Runtime.evaluate',
        { expression: 'void document.documentElement?.offsetHeight' },
      );
    } catch { }
  }
  try {
    await chrome.tabs.sendMessage(tabId, { type: 'DOCURANDOM_UI_PULSE' });
  } catch { }
}

async function sendUiMessageWithRetry(
  tabId,
  message,
  timeoutMs = UI_COMMAND_TIMEOUT_MS,
  label = 'browser-command',
  attempts = 2,
) {
  let lastError;
  const count = Math.max(1, Number(attempts) || 1);
  for (let index = 0; index < count; index += 1) {
    try {
      return await withUiDeadline(
        sendUiMessage(tabId, message),
        timeoutMs,
        label,
      );
    } catch (error) {
      lastError = error;
      if (index + 1 >= count) throw error;
      await wakeUiTabRenderer(tabId);
      await sleep(350);
    }
  }
  throw lastError || new Error(`FLOW_UI_COMMAND_TIMEOUT:${label}`);
}

async function sendUiMessageWithTimeout(
  tabId,
  message,
  timeoutMs = UI_GENERATE_MESSAGE_TIMEOUT_MS,
) {
  let timer = null;
  try {
    return await Promise.race([
      sendUiMessage(tabId, message),
      new Promise((_, reject) => {
        timer = setTimeout(
          () => reject(new Error('FLOW_UI_CONTENT_TIMEOUT')),
          timeoutMs,
        );
      }),
    ]);
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}

async function reloadFlowTabInPlace(tabId) {
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab?.id) throw new Error('FLOW_TAB_NOT_FOUND');
  // This helper is only used after a positive pre-submit failure. It keeps the
  // same tab/profile/project URL; it never creates a window or adopts another
  // Flow project. The token-refresh path also uses this single reload primitive
  // so the browser mutation remains easy to audit.
  await chrome.tabs.reload(tab.id);
}

function uiJobIdleAgeMs(job) {
  const lastActivity = Number(job?.lastProgressAt || job?.startedAt || 0);
  return lastActivity > 0 ? Math.max(0, Date.now() - lastActivity) : Infinity;
}

async function resetStaleUiJob(job) {
  if (!job || activeUiJob !== job) return;
  if (!staleUiResetPromise) {
    staleUiResetPromise = (async () => {
      job.cancelRequested = true;
      const tab = job.tabId ? { id: job.tabId } : null;
      if (tab?.id) {
        // Cancel the stale content-script job in place. Never reload the Flow
        // document here: that can abort a generation still finishing in the
        // page and makes another workspace repeat already-paid work.
        const recovered = await withUiDeadline(recoverFlowUiInPlace(tab.id, job.id), 15000, 'recover-stale').catch(() => null);
        if (!recovered?.cancelAck?.ok) return;
      }
      if (activeUiJob === job) {
        activeUiJob = null;
        setState('idle');
      }
    })().finally(() => {
      staleUiResetPromise = null;
    });
  }
  await staleUiResetPromise;
}

async function rejectIfUiBusy(id) {
  const job = activeUiJob;
  if (!job) return false;
  if (
    staleUiResetPromise
    || uiJobIdleAgeMs(job) < UI_JOB_STALE_IDLE_TIMEOUT_MS
  ) {
    sendToAgent({ id, status: 409, error: 'FLOW_UI_BUSY' });
    return true;
  }
  await resetStaleUiJob(job);
  if (activeUiJob) {
    sendToAgent({ id, status: 409, error: 'FLOW_UI_BUSY' });
    return true;
  }
  return false;
}

async function ensureUiAutomationVersion(tabId, commandTimeoutMs = UI_COMMAND_TIMEOUT_MS) {
  const expected = chrome.runtime.getManifest().version;
  let check = await sendUiMessageWithRetry(
    tabId,
    { type: 'DOCURANDOM_UI_CHECK' },
    commandTimeoutMs,
    'browser-command',
    2,
  ).catch(() => null);
  if (check?.version === expected) return check;

  // Upgrade the isolated automation world in place. Reloading the document
  // would discard Flow's visible generation state and can waste image quota.
  await withUiDeadline(chrome.scripting.executeScript({
    target: { tabId },
    files: ['flow-selectors.js', 'flow-ui.js'],
  }), commandTimeoutMs, 'inject-driver');
  await sleep(350);
  check = await sendUiMessageWithRetry(
    tabId,
    { type: 'DOCURANDOM_UI_CHECK' },
    commandTimeoutMs,
    'browser-command',
    2,
  ).catch(() => null);
  if (check?.version !== expected) {
    throw new Error(`FLOW_UI_VERSION_MISMATCH:${check?.version || 'missing'}:${expected}`);
  }
  return check;
}

async function attachUiDebugger(tabId) {
  if (uiDebuggerTabs.has(tabId)) return;
  const target = { tabId };
  try {
    await withUiDeadline(chrome.debugger.attach(target, '1.3'), 10000, 'debugger-attach');
  } catch (error) {
    if (!String(error?.message || error).toLowerCase().includes('already attached')) throw error;
  }
  uiDebuggerTabs.add(tabId);
  // A minimized/background Flow tab can be marked hidden or frozen by
  // Chromium. Keep its renderer in the active lifecycle without focusing the
  // window or stealing the user's current tab.
  await withUiDeadline(Promise.allSettled([
    chrome.debugger.sendCommand(
      target,
      'Page.setWebLifecycleState',
      { state: 'active' },
    ),
    chrome.debugger.sendCommand(
      target,
      'Emulation.setFocusEmulationEnabled',
      { enabled: true },
    ),
  ]), 5000, 'renderer-wake');
}

async function detachUiDebugger(tabId) {
  if (!uiDebuggerTabs.has(tabId)) return;
  try {
    await withUiDeadline(chrome.debugger.sendCommand(
      { tabId },
      'Emulation.setFocusEmulationEnabled',
      { enabled: false },
    ), 2000, 'debugger-focus-cleanup');
  } catch { }
  uiDebuggerTabs.delete(tabId);
  try { await withUiDeadline(chrome.debugger.detach({ tabId }), 2000, 'debugger-detach'); } catch { }
}

async function pulseUiRenderer(tabId, jobId) {
  if (!uiDebuggerTabs.has(tabId)) return;
  const target = { tabId };
  const results = await Promise.allSettled([
    chrome.debugger.sendCommand(
      target,
      'Page.setWebLifecycleState',
      { state: 'active' },
    ),
    chrome.debugger.sendCommand(
      target,
      'Emulation.setFocusEmulationEnabled',
      { enabled: true },
    ),
    chrome.debugger.sendCommand(target, 'Runtime.evaluate', {
      expression: 'void document.documentElement',
      returnByValue: true,
    }),
    // An extension message is an external renderer task. It wakes overdue
    // page work even when Chromium has heavily throttled hidden-page timers.
    chrome.tabs.sendMessage(tabId, {
      type: 'DOCURANDOM_UI_PULSE',
      jobId,
    }),
  ]);
  const pulseAck = results[3];
  if (
    pulseAck?.status === 'fulfilled'
    && pulseAck.value?.ok
    && activeUiJob?.id === jobId
  ) {
    activeUiJob.lastPulseAckAt = Date.now();
  }
}

function startUiWakePulse(tabId, jobId) {
  const generation = ++uiWakePulseGeneration;
  const tick = async () => {
    if (generation !== uiWakePulseGeneration || activeUiJob?.id !== jobId) return;
    await withUiDeadline(pulseUiRenderer(tabId, jobId), 4000, 'renderer-pulse').catch(() => {});
    if (generation !== uiWakePulseGeneration || activeUiJob?.id !== jobId) return;
    setTimeout(tick, UI_WAKE_PULSE_INTERVAL_MS);
  };
  tick().catch(() => {});
  return generation;
}

function stopUiWakePulse(generation) {
  if (generation === uiWakePulseGeneration) uiWakePulseGeneration += 1;
}

async function trustedUiReplaceText(tabId, value) {
  await attachUiDebugger(tabId);
  const target = { tabId };
  const base = {
    key: 'a', code: 'KeyA', windowsVirtualKeyCode: 65, nativeVirtualKeyCode: 65,
  };
  await chrome.debugger.sendCommand(target, 'Input.dispatchKeyEvent', {
    type: 'keyDown', ...base, modifiers: 2,
  });
  await chrome.debugger.sendCommand(target, 'Input.dispatchKeyEvent', {
    type: 'keyUp', ...base, modifiers: 2,
  });
  await chrome.debugger.sendCommand(target, 'Input.insertText', { text: String(value || '') });
}

async function trustedUiPressEnter(tabId) {
  await attachUiDebugger(tabId);
  const target = { tabId };
  const base = {
    key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13,
  };
  await chrome.debugger.sendCommand(target, 'Input.dispatchKeyEvent', {
    type: 'keyDown', ...base, modifiers: 0,
  });
  await chrome.debugger.sendCommand(target, 'Input.dispatchKeyEvent', {
    type: 'keyUp', ...base, modifiers: 0,
  });
}

async function trustedUiPressEscape(tabId) {
  await attachUiDebugger(tabId);
  const target = { tabId };
  const base = {
    key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27,
  };
  await chrome.debugger.sendCommand(target, 'Input.dispatchKeyEvent', {
    type: 'keyDown', ...base, modifiers: 0,
  });
  await chrome.debugger.sendCommand(target, 'Input.dispatchKeyEvent', {
    type: 'keyUp', ...base, modifiers: 0,
  });
}

async function recoverFlowUiInPlace(tabId, jobId = '') {
  // Recovery is intentionally non-destructive: keep the document, project,
  // cookies, local storage and any image generation already running in Flow.
  // Only cancel our own stale driver, close a transient menu/viewer and inject
  // the current selector/driver code into the same tab.
  const cancelAck = await withUiDeadline(sendUiMessage(tabId, {
    type: 'DOCURANDOM_UI_RECOVER',
    jobId: String(jobId || ''),
  }), 3000, 'cancel-driver').catch(() => null);
  await withUiDeadline(trustedUiPressEscape(tabId), 3000, 'escape').catch(() => null);
  await withUiDeadline(chrome.scripting.executeScript({
    target: { tabId },
    files: ['flow-selectors.js', 'flow-ui.js'],
  }), 3000, 'inject-driver').catch(() => null);
  await sleep(500);
  const check = await withUiDeadline(sendUiMessage(tabId, { type: 'DOCURANDOM_UI_CHECK' }), 3000, 'check-driver').catch(() => null);
  return { ...check, cancelAck };
}

function shouldRecoverFlowUiInPlace(error) {
  const value = String(error || '');
  return [
    'FLOW_UI_TIMEOUT:prompt-editor',
    'FLOW_UI_PROMPT_FOCUS_FAILED',
    'FLOW_UI_PROMPT_NOT_INSERTED',
    'FLOW_UI_SUBMIT_NOT_FOUND',
    'FLOW_UI_SUBMIT_NOT_CONFIRMED',
    'FLOW_UI_CONTENT_TIMEOUT',
    'FLOW_UI_TIMEOUT_UNSENT',
    'FLOW_UI_IMAGE_COUNT_NOT_CONFIRMED',
    'FLOW_UI_IMAGE_MODEL_NOT_CONFIRMED',
    'FLOW_UI_REQUIRED_IMAGE_CONFIG_NOT_CONFIRMED',
    'FLOW_UI_ELEMENT_CHANGED:',
    'FLOW_UI_ELEMENT_UNSTABLE:',
  ].some((marker) => value.includes(marker))
    || isUiMessageChannelClosedError(value);
}

function safeDownloadSegment(value, fallback) {
  const cleaned = String(value || '')
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, '-')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/[. ]+$/g, '')
    .slice(0, 80);
  return cleaned || fallback;
}

function downloadAndWait(url, filename, timeoutMs = 180000) {
  return new Promise((resolve, reject) => {
    let downloadId = null;
    let timer = null;

    const cleanup = () => {
      if (timer) clearTimeout(timer);
      chrome.downloads.onChanged.removeListener(onChanged);
    };
    const finish = async () => {
      cleanup();
      try {
        const items = await chrome.downloads.search({ id: downloadId });
        const item = items?.[0];
        if (!item?.filename) throw new Error('DOWNLOAD_PATH_MISSING');
        resolve({
          downloadId,
          localPath: item.filename,
          finalUrl: item.finalUrl || item.url || url,
          bytesReceived: item.bytesReceived || 0,
        });
      } catch (error) {
        reject(error);
      }
    };
    const onChanged = (delta) => {
      if (delta.id !== downloadId) return;
      if (delta.error?.current) {
        cleanup();
        reject(new Error(`DOWNLOAD_FAILED:${delta.error.current}`));
      } else if (delta.state?.current === 'complete') {
        finish();
      }
    };

    chrome.downloads.onChanged.addListener(onChanged);
    timer = setTimeout(() => {
      cleanup();
      reject(new Error('DOWNLOAD_TIMEOUT'));
    }, timeoutMs);
    chrome.downloads.download(
      { url, filename, saveAs: false, conflictAction: 'uniquify' },
      (id) => {
        const error = chrome.runtime.lastError;
        if (error || !id) {
          cleanup();
          reject(new Error(error?.message || 'DOWNLOAD_START_FAILED'));
          return;
        }
        downloadId = id;
      },
    );
  });
}

function snapshotContainsPromptMatch(response) {
  return Array.isArray(response?.matches)
    && response.matches.some((item) => item && item.matched === true);
}

async function inspectExistingFlowTab(tab, request, jobId, timeoutMs = 8000) {
  if (!tab?.id || !isFlowUrl(tab.url) || tab.discarded === true) return null;
  activeUiJob.tabId = tab.id;
  activeUiJob.phase = 'history-tab-attach';
  await attachUiDebugger(tab.id);
  let commandStarted = false;
  let completed = false;
  try {
    activeUiJob.phase = 'history-tab-ensure-driver';
    await ensureUiAutomationVersion(tab.id, timeoutMs);
    activeUiJob.phase = 'history-tab-snapshot';
    commandStarted = true;
    const response = await sendUiMessageWithRetry(
      tab.id,
      { type: 'DOCURANDOM_UI_PROJECT_SNAPSHOT', jobId, request },
      timeoutMs,
      'project-snapshot',
      1,
    );
    if (!response?.ok) {
      throw new Error(response?.error || 'FLOW_UI_PROJECT_SNAPSHOT_FAILED');
    }
    completed = true;
    return response;
  } finally {
    if (commandStarted && !completed) {
      // A transport timeout does not stop the page job. Cancel it before
      // inspecting another project so a late Reuse read cannot keep changing
      // that project's composer in the background.
      await withUiDeadline(
        sendUiMessage(tab.id, { type: 'DOCURANDOM_UI_CANCEL', jobId }),
        2000,
        'cancel-project-snapshot',
      ).catch(() => null);
    }
    await detachUiDebugger(tab.id);
  }
}

async function findPromptInExistingFlowTabs(primaryTabId, request, jobId, primaryResponse) {
  const tabs = await chrome.tabs.query({ url: FLOW_TAB_URLS }).catch(() => []);
  const candidates = tabs
    .filter((tab) => tab?.id && tab.id !== primaryTabId && tab.status === 'complete')
    .sort((left, right) => Number(right.lastAccessed || 0) - Number(left.lastAccessed || 0));
  let scannedTabs = 1;
  const matchedProjects = new Map();
  const recordMatch = (tab, response) => {
    if (!snapshotContainsPromptMatch(response)) return;
    const projectId = cleanWorkspaceIdentity(response?.projectId);
    const tabProjectId = flowProjectIdFromTab(tab);
    if (!projectId || (tabProjectId && tabProjectId !== projectId)) return;
    matchedProjects.set(projectId, { tab, response });
  };
  const primaryTab = await chrome.tabs.get(primaryTabId).catch(() => null);
  recordMatch(primaryTab, primaryResponse);
  const deadline = Date.now() + 120000;
  for (const candidate of candidates) {
    if (activeUiJob?.cancelRequested) throw new Error('CANCELLED');
    if (Date.now() >= deadline) break;
    try {
      const response = await inspectExistingFlowTab(candidate, request, jobId);
      // inspectExistingFlowTab temporarily reports the candidate as the
      // active job tab while it is being inspected. Restore the primary tab
      // before the next candidate so a later timeout/error cannot be reported
      // as if the wrong project were the live recovery target.
      activeUiJob.tabId = primaryTabId;
      activeUiJob.phase = 'snapshot-search';
      scannedTabs += 1;
      recordMatch(candidate, response);
      if (matchedProjects.size > 1) {
        throw new Error('FLOW_LEGACY_PROJECT_MATCH_AMBIGUOUS');
      }
    } catch (error) {
      if (String(error?.message || error) === 'FLOW_LEGACY_PROJECT_MATCH_AMBIGUOUS') throw error;
      activeUiJob.tabId = primaryTabId;
      activeUiJob.phase = 'snapshot-search';
      scannedTabs += 1;
      console.info(
        `[Flow Agent] Flow project history scan skipped tab=${candidate.id}:`,
        error?.message || error,
      );
    }
  }
  if (matchedProjects.size === 1) {
    const { tab, response } = [...matchedProjects.values()][0];
    // Bind only after the search has rejected conflicting exact matches.
    await rememberWorkTab(tab, false);
    return { ...response, matchedTabId: tab.id, scannedTabs };
  }
  return { ...primaryResponse, matchedTabId: primaryTabId, scannedTabs };
}

async function handleUiProjectSnapshot(msg) {
  const { id, params = {} } = msg;
  if (await rejectIfUiBusy(id)) return;
  const jobId = String(params.jobId || id || `snapshot-${Date.now()}`);
  activeUiJob = {
    id: jobId,
    requestId: id,
    submitted: false,
    progressObserved: false,
    inspection: true,
    startedAt: Date.now(),
    lastProgressAt: Date.now(),
    phase: 'starting',
  };
  let attachedTabId = null;
  let wakePulse = 0;
  const workspaceId = cleanWorkspaceIdentity(params.workspaceId);
  const flowProjectId = cleanWorkspaceIdentity(params.flowProjectId);
  const activeTabOnly = params.activeTabOnly === true;
  const discoveryOnly = !workspaceId && !flowProjectId && !activeTabOnly
    && params.inspectTilePrompts === true
    && Array.isArray(params.prompts) && params.prompts.some(value => String(value || '').trim())
    && params.searchExistingTabs !== false;
  let tab = null;
  setState('running');
  try {
    if (activeTabOnly && (workspaceId || flowProjectId)) {
      throw new Error('FLOW_ACTIVE_TAB_SNAPSHOT_CANNOT_USE_WORKSPACE_IDENTITY');
    }
    // A project-bound reconciliation read may reopen the exact saved project,
    // but it must never create a new project or adopt an unrelated tab.
    if (workspaceId && !flowProjectId) {
      throw new Error('FLOW_WORKSPACE_PROJECT_ID_MISSING');
    }
    activeUiJob.phase = 'restore-tab';
    if (workspaceId) {
      tab = await withUiDeadline(
        getOrOpenFlowTab({ workspaceId, projectId: flowProjectId, newProject: false }),
        flowProjectId
          ? workspaceFlowTabOpenTimeoutMs(
            flowProjectId,
            workspaceFlowTabs.get(workspaceId) || {},
          )
          : 15000,
        'workspace-flow-tab',
      );
    } else if (activeTabOnly) {
      // A user-confirmed workspace binding may inspect only the currently
      // selected Flow project.  Do not restore a remembered tab, search other
      // tabs, switch projects, or create a browser tab in this mode.
      const activeTabs = await chrome.tabs.query({ active: true, currentWindow: true }).catch(() => []);
      tab = activeTabs.find((candidate) => isFlowUrl(candidate?.url || candidate?.pendingUrl)) || null;
      if (!tab?.id) throw new Error('FLOW_ACTIVE_TAB_NOT_A_FLOW_PROJECT');
    } else {
      await restoreRememberedWorkTab();
      tab = workTabId
        ? await chrome.tabs.get(workTabId).catch(() => null)
        : null;
      if (!tab?.id || (!isFlowUrl(tab.url) && !/^https:\/\/accounts\.google\.com\//i.test(String(tab.url || '')))) {
        tab = await findExistingFlowTab();
      }
    }
    if (!tab?.id) throw new Error('FLOW_TAB_NOT_FOUND');
    activeUiJob.tabId = tab.id;
    const request = {
      prompts: Array.isArray(params.prompts)
        ? params.prompts.map((value) => String(value || '').trim()).filter(Boolean).slice(0, 8)
        : [],
      debug: params.debug === true,
      inspectTilePrompts: params.inspectTilePrompts === true,
      workspaceId,
      flowProjectId,
      activeTabOnly,
      discoveryOnly,
    };
    let response;
    if (discoveryOnly) {
      // An empty or stale remembered project must not consume the whole
      // reconciliation deadline before the other existing projects are read.
      response = await inspectExistingFlowTab(tab, request, jobId).catch((error) => {
        if (activeUiJob?.cancelRequested) throw error;
        return { ok: true };
      });
      response = await findPromptInExistingFlowTabs(tab.id, request, jobId, response);
    } else {
      // The managed Flow window is minimized. Wake its renderer before waiting
      // for the navigation's complete event; otherwise Chromium can leave this
      // exact tab in `loading` while the lifecycle is frozen.
      activeUiJob.phase = 'wake-renderer';
      attachedTabId = tab.id;
      await attachUiDebugger(tab.id);
      activeUiJob.phase = 'wait-document';
      await waitForFlowDocument(tab.id);
      activeUiJob.phase = 'ensure-driver';
      await ensureUiAutomationVersion(tab.id);
      wakePulse = startUiWakePulse(tab.id, jobId);
      activeUiJob.phase = 'snapshot-command';
      response = await sendUiMessageWithRetry(
        tab.id,
        { type: 'DOCURANDOM_UI_PROJECT_SNAPSHOT', jobId, request },
        UI_PROJECT_SNAPSHOT_TIMEOUT_MS,
        'project-snapshot',
        2,
      );
      if (!response?.ok) throw new Error(response?.error || 'FLOW_UI_PROJECT_SNAPSHOT_FAILED');
      if (params.debug === true) {
        const diagnostic = await chrome.debugger.sendCommand({ tabId: tab.id }, 'Runtime.evaluate', {
          returnByValue: true,
          expression: `(() => {
            const isVisible = node => { const r = node.getBoundingClientRect(); const s = getComputedStyle(node); return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden'; };
            const compact = value => String(value || '').replace(/\\s+/g, ' ').trim();
            return {
              title: document.title,
              bodyText: compact(document.body?.innerText).slice(0, 5000),
              controls: [...document.querySelectorAll('button,[role="button"],textarea,[contenteditable="true"]')].filter(isVisible).slice(0, 100).map(node => ({
                tag: node.tagName, id: node.id, role: node.getAttribute('role'),
                ariaLabel: node.getAttribute('aria-label'), text: compact(node.innerText).slice(0, 160),
                disabled: node.disabled === true, classes: String(node.className || '').slice(0, 120),
              })),
              components: [...new Set([...document.querySelectorAll('*')].map(node => node.tagName.toLowerCase()).filter(name => name.startsWith('flow-')))].slice(0, 100),
            };
          })()`,
        });
        response.diagnostics = diagnostic?.result?.value || {};
      }
      if (activeTabOnly) {
        const tabProjectId = flowProjectIdFromTab(tab);
        const pageProjectId = cleanWorkspaceIdentity(response.projectId);
        if (!pageProjectId) throw new Error('FLOW_ACTIVE_TAB_PROJECT_ID_NOT_READY');
        if (tabProjectId && pageProjectId !== tabProjectId) {
          throw new Error('FLOW_ACTIVE_TAB_PROJECT_ID_MISMATCH');
        }
      }
      if (
        !activeTabOnly
        && !snapshotContainsPromptMatch(response)
        && !workspaceId
        && params.searchExistingTabs !== false
      ) {
        stopUiWakePulse(wakePulse);
        wakePulse = 0;
        await detachUiDebugger(tab.id);
        attachedTabId = null;
        response = await findPromptInExistingFlowTabs(tab.id, request, jobId, response);
      }
    }
    if (response?.matchedTabId && response.matchedTabId !== tab.id) {
      const matchedTab = await chrome.tabs.get(response.matchedTabId).catch(() => null);
      if (matchedTab?.id) {
        // The exact prompt match is the authority for a multi-tab recovery.
        // Continue persisting and reporting that tab, not the primary tab
        // that happened to be inspected first.
        tab = matchedTab;
        activeUiJob.tabId = matchedTab.id;
      }
    }
    if (workspaceId) {
      const known = workspaceFlowTabs.get(workspaceId);
      await rememberWorkspaceFlowTab(
        workspaceId,
        tab,
        response.projectId || flowProjectId,
        known?.createdByExtension === true,
        response.projectUrl || tab.url || tab.pendingUrl,
      );
    }
    sendToAgent({
      id,
      status: 200,
      result: {
        projectId: response.projectId || '',
        projectUrl: response.projectUrl || tab.url || '',
        matchedTabId: response.matchedTabId || tab.id,
        scannedTabs: Number(response.scannedTabs || 1),
        uiVersion: response.uiVersion || '',
        selectorRevision: response.selectorRevision || '',
        tileCount: Number(response.tileCount || 0),
        tiles: Array.isArray(response.tiles) ? response.tiles : [],
        matches: Array.isArray(response.matches) ? response.matches : [],
        composer: response.composer && typeof response.composer === 'object'
          ? response.composer
          : {},
        debugTiles: Array.isArray(response.debugTiles) ? response.debugTiles : [],
        reusedPromptReads: Array.isArray(response.reusedPromptReads)
          ? response.reusedPromptReads
          : [],
        history: response.history && typeof response.history === 'object'
          ? response.history
          : {},
        surface: response.surface && typeof response.surface === 'object'
          ? response.surface
          : {},
        ...(params.debug === true ? { diagnostics: response.diagnostics || {} } : {}),
      },
    });
  } catch (error) {
    const detail = String(error?.message || error);
    const phase = activeUiJob?.id === jobId ? activeUiJob.phase || 'unknown' : 'cleanup';
    const tab = activeUiJob?.id === jobId ? activeUiJob.tabId || '' : '';
    sendToAgent({ id, status: 400, error: `${detail}:phase=${phase}:tab=${tab}` });
  } finally {
    stopUiWakePulse(wakePulse);
    if (attachedTabId) await detachUiDebugger(attachedTabId);
    if (activeUiJob?.id === jobId) {
      activeUiJob = null;
      setState('idle');
    }
  }
}

async function handleUiInspectConfiguration(msg) {
  const { id, params = {} } = msg;
  if (await rejectIfUiBusy(id)) return;
  const jobId = String(params.jobId || id || `inspect-${Date.now()}`);
  activeUiJob = {
    id: jobId,
    requestId: id,
    submitted: false,
    progressObserved: false,
    inspection: true,
    startedAt: Date.now(),
    lastProgressAt: Date.now(),
    phase: 'starting',
  };
  let attachedTabId = null;
  let wakePulse = 0;
  setState('running');
  try {
    activeUiJob.phase = 'get-flow-tab';
    const tab = await withUiDeadline(getOrOpenFlowTab(), 15000, 'get-flow-tab');
    if (!tab?.id) throw new Error('FLOW_TAB_NOT_FOUND');
    activeUiJob.tabId = tab.id;
    // Wake the minimized Flow renderer before checking for document complete.
    activeUiJob.phase = 'wake-renderer';
    attachedTabId = tab.id;
    await attachUiDebugger(tab.id);
    activeUiJob.phase = 'wait-document';
    await waitForFlowDocument(tab.id);
    activeUiJob.phase = 'ensure-driver';
    await ensureUiAutomationVersion(tab.id);
    wakePulse = startUiWakePulse(tab.id, jobId);
    const uiMessage = {
      type: 'DOCURANDOM_UI_INSPECT_CONFIGURATION',
      jobId,
      request: { aspect: params.aspect || 'landscape' },
    };
    activeUiJob.phase = 'inspect-command';
    let response = await sendUiMessageWithTimeout(
      tab.id,
      uiMessage,
      UI_INSPECT_CONFIGURATION_TIMEOUT_MS,
    );
    if (!response?.ok && shouldRecoverFlowUiInPlace(response?.error)) {
      await recoverFlowUiInPlace(tab.id, jobId);
      await ensureUiAutomationVersion(tab.id);
      response = await sendUiMessageWithTimeout(
        tab.id,
        uiMessage,
        UI_INSPECT_CONFIGURATION_TIMEOUT_MS,
      );
    }
    if (!response?.ok) throw new Error(response?.error || 'FLOW_UI_CONFIGURATION_INSPECTION_FAILED');
    sendToAgent({
      id,
      status: 200,
      result: {
        verified: response.verified === true,
        model: response.model || '',
        modelName: response.modelName || '',
        observedModelText: response.observedModelText || '',
        imageCount: Number(response.imageCount || 0),
        projectId: response.projectId || '',
        projectUrl: response.projectUrl || tab.url || '',
        selectorRevision: response.selectorRevision || '',
        uiVersion: response.uiVersion || '',
        promptTouched: response.promptTouched === true,
        promptSubmitted: response.promptSubmitted === true,
      },
    });
  } catch (error) {
    const detail = String(error?.message || error);
    const phase = activeUiJob?.id === jobId ? activeUiJob.phase || 'unknown' : 'cleanup';
    const tab = activeUiJob?.id === jobId ? activeUiJob.tabId || '' : '';
    sendToAgent({ id, status: 400, error: `${detail}:phase=${phase}:tab=${tab}` });
  } finally {
    stopUiWakePulse(wakePulse);
    if (attachedTabId) await detachUiDebugger(attachedTabId);
    if (activeUiJob?.id === jobId) {
      activeUiJob = null;
      setState('idle');
    }
  }
}

async function prepareFlowProjectBeforeGenerate(tab, {
  workspaceId, flowProjectId, newProject, jobId,
}) {
  if (!newProject) return { tab, projectId: flowProjectId };
  let current = await chrome.tabs.get(tab.id);
  if (!isFlowUrl(current.url || current.pendingUrl || '')) {
    throw new Error('FLOW_UI_PROJECT_PREPARE_WRONG_PAGE');
  }
  // This navigation belongs to the worker, before any generate response port
  // exists. Reusing a previous project must not destroy a live generate job.
  if (flowProjectIdFromTab(current)) {
    current = await chrome.tabs.update(current.id, { url: FLOW_URL });
  }
  await waitForFlowDocument(current.id);
  await ensureUiAutomationVersion(current.id);
  let preparationError = '';
  try {
    const response = await sendUiMessageWithTimeout(current.id, {
      type: 'DOCURANDOM_UI_PREPARE_PROJECT',
      jobId,
    }, 60000);
    if (!response?.ok) throw new Error(response?.error || 'FLOW_UI_PROJECT_PREPARE_FAILED');
  } catch (error) {
    if (!isUiMessageChannelClosedError(error)) throw error;
    preparationError = String(error?.message || error);
  }
  current = await waitForTabComplete(current.id, 60000);
  const projectId = cleanWorkspaceIdentity(flowProjectIdFromTab(current));
  if (!projectId) {
    throw new Error(`FLOW_UI_PROJECT_PREPARE_NO_PROJECT:${preparationError}`);
  }
  await ensureUiAutomationVersion(current.id);
  if (workspaceId) {
    const owned = workspaceFlowTabs.get(workspaceId)?.createdByExtension === true;
    await rememberWorkspaceFlowTab(workspaceId, current, projectId, owned);
  }
  sendToAgent({
    type: 'ui_progress', requestId: activeUiJob?.requestId, jobId,
    progress: 0.08, phase: 'project-prepared',
    message: 'Đã chuẩn bị dự án Flow riêng · bắt đầu nối bộ tạo ảnh…',
    projectId, projectUrl: current.url || '',
  });
  return { tab: current, projectId };
}

async function handleUiGenerateImages(msg) {
  const { id, params = {} } = msg;
  if (await rejectIfUiBusy(id)) return;
  const jobId = String(params.jobId || id || `ui-${Date.now()}`);
  activeUiJob = {
    id: jobId,
    requestId: id,
    submitted: false,
    progressObserved: false,
    projectId: '',
    startedAt: Date.now(),
    lastProgressAt: Date.now(),
    phase: 'starting',
  };
  const job = activeUiJob;
  let attachedTabId = null;
  let wakePulse = 0;
  const workspaceId = cleanWorkspaceIdentity(params.workspaceId);
  const flowProjectId = cleanWorkspaceIdentity(params.flowProjectId);
  const newProject = params.newProject === true;
  let tab = null;
  let closeWorkspaceTabAfterSuccess = false;
  setState('running');
  try {
    job.phase = 'get-flow-tab';
      tab = await withUiDeadline(
        getOrOpenFlowTab({
          workspaceId,
          projectId: flowProjectId,
          newProject,
        }),
        workspaceId && flowProjectId
          ? workspaceFlowTabOpenTimeoutMs(
            flowProjectId,
            workspaceFlowTabs.get(workspaceId) || {},
          )
          : 15000,
        workspaceId ? 'workspace-flow-tab' : 'get-flow-tab',
      );
    if (!tab?.id) throw new Error('FLOW_TAB_NOT_FOUND');
    job.tabId = tab.id;
    // Wake the minimized Flow renderer before waiting for its load event.
    // This is still pre-submit, so a timeout cannot duplicate a prompt.
    job.phase = 'wake-renderer';
    attachedTabId = tab.id;
    await attachUiDebugger(tab.id);
    job.phase = 'wait-document';
    await waitForFlowDocument(tab.id);
    job.phase = 'ensure-driver';
    await ensureUiAutomationVersion(tab.id);
    job.phase = 'prepare-project';
    const prepared = await prepareFlowProjectBeforeGenerate(tab, {
      workspaceId, flowProjectId, newProject, jobId,
    });
    tab = prepared.tab;
    job.projectId = prepared.projectId || flowProjectId;
    wakePulse = startUiWakePulse(tab.id, jobId);
    if (activeUiJob?.id !== jobId || activeUiJob.cancelRequested) throw new Error('CANCELLED');
    const uiMessage = {
      type: 'DOCURANDOM_UI_GENERATE',
      jobId,
      request: {
        prompts: Array.isArray(params.prompts) ? params.prompts : [params.prompt],
        aspect: params.aspect || 'landscape',
        count: REQUIRED_IMAGE_COUNT,
        model: REQUIRED_IMAGE_MODEL,
        workspaceId,
        flowProjectId: prepared.projectId || flowProjectId,
        newProject: false,
        delayMinSeconds: params.delayMinSeconds ?? 3,
        delayMaxSeconds: params.delayMaxSeconds ?? 5,
      },
    };
    job.phase = 'generate-command';
    let response = await sendUiMessageWithTimeout(tab.id, uiMessage).catch((error) => {
      const detail = String(error?.message || error);
      if (job.submitted) {
        return {
          ok: true,
          images: [],
          promptOutcomes: [{
            promptIndex: 1,
            submitted: true,
            status: 'submitted-no-image',
            imageCount: 0,
            error: detail,
          }],
        };
      }
      // Every route to prompt insertion/submission emits a lifecycle event
      // first (project, configure-ui or configure). Therefore a full content-
      // message timeout with no progress at all means the minimized renderer
      // never reached the prompt; nothing could have been submitted to Flow.
      // Treat only that narrow state as safely unsent so the desktop can recover
      // the same tab and retry instead of permanently poisoning the checkpoint.
      const noProgress = !job.progressObserved && !job.submitted;
      const errorCode = detail.includes('FLOW_UI_CONTENT_TIMEOUT') && noProgress
        ? 'FLOW_UI_TIMEOUT_UNSENT:phase=no-progress'
        : detail.includes('FLOW_UI_CONTENT_TIMEOUT') ? detail
        : /receiving end does not exist|could not establish connection/i.test(detail)
          ? 'FLOW_UI_TIMEOUT_UNSENT'
      : `FLOW_UI_SUBMISSION_UNCERTAIN: ${detail}`;
      return { ok: false, error: errorCode };
    });
    job.projectId = cleanWorkspaceIdentity(response?.projectId) || job.projectId;
    if (
      !response?.ok
      && !job.submitted
      && String(response.error || '').includes('FLOW_UI_PROJECT_NOT_READY')
    ) {
      sendToAgent({
        type: 'ui_progress',
        requestId: id,
        jobId,
        progress: 0.08,
        phase: 'recover-project-surface',
        message: 'Flow chưa dựng xong giao diện project · đang tải lại đúng tab rồi thử lại prompt chưa gửi…',
      });
      try {
        // The content script returned a concrete pre-submit failure. Reloading
        // this same project tab is safe here; unlike a timeout after submit,
        // no prompt can be duplicated.
        await reloadFlowTabInPlace(tab.id);
        await waitForTabComplete(tab.id);
        await sleep(750);
        await ensureUiAutomationVersion(tab.id);
        response = await sendUiMessageWithTimeout(tab.id, uiMessage);
      } catch (error) {
        response = {
          ok: false,
          error: `FLOW_UI_PROJECT_RECOVERY_FAILED:${String(error?.message || error)}`,
        };
      }
      job.projectId = cleanWorkspaceIdentity(response?.projectId) || job.projectId;
    }
    if (!response?.ok && shouldRecoverFlowUiInPlace(response?.error)) {
      sendToAgent({
        type: 'ui_progress',
        requestId: id,
        jobId,
        progress: 0.08,
        phase: 'recover-ui',
        message: 'Đang kiểm tra và phục hồi driver trên đúng tab Flow hiện tại, không reload…',
      });
      // Do not resend inside the same bridge request. The content-script
      // watchdog may have timed out while its original promise is still
      // unwinding. Return the authoritative "unsent" failure so the desktop
      // checkpoint can retry this one prompt later without touching the page.
      const recovery = await recoverFlowUiInPlace(tab.id, jobId);
      const ack = recovery?.cancelAck;
      const originalError = String(response.error || 'unknown UI bridge failure');
      const channelClosed = isUiMessageChannelClosedError(originalError);
      if (originalError.includes('FLOW_UI_CONTENT_TIMEOUT') || channelClosed) {
        // A closed response port is not proof that Enter was pressed. Only
        // downgrade it when the same content driver confirms that it had a
        // live job and never attempted submission.
        const cancelAckSummary = ack
          ? 'ok=' + Boolean(ack.ok)
            + ',cancelledJobs=' + Number(ack.cancelledJobs || 0)
            + ',submissionAttempted=' + Boolean(ack.submissionAttempted)
            + ',submitted=' + Boolean(ack.submitted)
          : 'unavailable';
        const cancelProvesUnsent = Boolean(
          ack?.ok && ack.cancelledJobs > 0 && !ack.submissionAttempted && !ack.submitted,
        );
        response.error = cancelProvesUnsent
          ? 'FLOW_UI_TIMEOUT_UNSENT:phase='
            + (channelClosed ? 'message-channel' : 'no-progress')
            + ':cause=' + originalError
          : 'FLOW_UI_SUBMISSION_UNCERTAIN:phase=generate-command:cause='
            + originalError + ';cancelAck=' + cancelAckSummary;
      }
    }
    job.projectId = cleanWorkspaceIdentity(response?.projectId) || job.projectId;
    if (!response?.ok) {
      if (response.submitted) throw new Error(`FLOW_PROMPT_SUBMITTED_NO_IMAGE: ${response.error || 'CANCELLED_AFTER_SUBMIT'}`);
      if (response.submissionAttempted && !response.submitted) {
        // Preserve the content-script's concrete failure (for example
        // FLOW_UI_SUBMIT_NOT_CONFIRMED) so the desktop can distinguish a
        // missing confirmation from a transport timeout during reconciliation.
        throw new Error(`FLOW_UI_SUBMISSION_UNCERTAIN: ${response.error || 'FLOW_UI_SUBMIT_NOT_CONFIRMED'}`);
      }
      throw new Error(response?.error || 'FLOW_UI_AUTOMATION_FAILED');
    }
    const promptOutcomes = Array.isArray(response.promptOutcomes)
      ? response.promptOutcomes
      : [];
    const submittedFailures = promptOutcomes.filter(
      (item) => item?.submitted && item?.status === 'submitted-no-image',
    );

    const folder = safeDownloadSegment(
      params.downloadFolder || response.projectId,
      `project-${Date.now()}`,
    );
    const downloaded = [];
    const warnings = [];
    for (let index = 0; index < (response.images || []).length; index += 1) {
      const image = response.images[index];
      if (!/^https?:\/\//i.test(String(image?.url || ''))) {
        warnings.push(`Ảnh ${index + 1} không có URL tải hợp lệ.`);
        continue;
      }
      // Flow now exposes signed CDN URLs. The desktop downloader can fetch
      // these directly; routing them through the browser's Save/scan pipeline
      // adds minutes on some profiles and can outlive the job watchdog.
      const downloadUrl = new URL(image.url);
      if (downloadUrl.hostname === 'flow-content.google') {
        downloaded.push({ ...image, imageUrl: image.url, localPath: '' });
        continue;
      }
      const filename = `DocuRandom Flow/${folder}/Image_${String(index + 1).padStart(4, '0')}.png`;
      try {
        const remainingMs = Math.max(1000, 300000 - (Date.now() - job.startedAt));
        const result = await downloadAndWait(image.url, filename, Math.min(45000, remainingMs));
        downloaded.push({
          ...image,
          imageUrl: result.finalUrl,
          localPath: result.localPath,
          downloadId: result.downloadId,
        });
      } catch (error) {
        // Keep the signed URL as a recovery path. The local backend can still
        // download it directly and the Flow project retains the generated tile.
        warnings.push(`Ảnh ${index + 1}: ${error?.message || error}`);
        downloaded.push({ ...image, imageUrl: image.url, localPath: '' });
      }
    }
    if (!downloaded.length && !submittedFailures.length) {
      throw new Error('FLOW_UI_NO_VALID_IMAGES');
    }
    // Only close a workspace-owned Flow window after every requested prompt
    // has an image. An acknowledged prompt without an image must keep the
    // project open for history reconciliation; closing it here would make a
    // late result harder to inspect and recover safely.
    const requestedPromptCount = (
      Array.isArray(params.prompts)
        ? params.prompts
        : [params.prompt]
    ).map((value) => String(value || '').trim()).filter(Boolean).length;
    closeWorkspaceTabAfterSuccess = Boolean(
      workspaceId
      && tab?.id
      && requestedPromptCount > 0
      && downloaded.length >= requestedPromptCount
      && promptOutcomes.length >= requestedPromptCount
      && promptOutcomes
        .slice(0, requestedPromptCount)
        .every((item) => item?.status === 'image-ready' && Number(item?.imageCount || 0) > 0)
      && submittedFailures.length === 0,
    );
    if (submittedFailures.length) {
      sendToAgent({
        type: 'ui_progress',
        requestId: id,
        jobId,
        progress: 0.96,
        phase: 'recover-after-submission',
        message: 'Prompt đã gửi nhưng Flow chưa trả ảnh · giữ nguyên trang và chuyển sang checkpoint kế tiếp…',
      });
      try {
        // The failed prompt is already acknowledged as submitted. Leave the
        // Flow document intact so a late image is not destroyed and never
        // resend that prompt from this request.
        await recoverFlowUiInPlace(tab.id, jobId);
      } catch (error) {
        warnings.push(`Không thể phục hồi driver Flow tại chỗ: ${error?.message || error}`);
      }
    }
    const observedProjectId = String(
      response.projectId || flowProjectId || flowProjectIdFromTab(tab) || '',
    ).trim();
    if (workspaceId) {
      const known = workspaceFlowTabs.get(workspaceId);
      await rememberWorkspaceFlowTab(
        workspaceId,
        tab,
        observedProjectId,
        known?.createdByExtension === true,
        response.projectUrl || tab.url || tab.pendingUrl,
      );
    }
    sendToAgent({
      id,
      status: 200,
      result: {
        images: downloaded,
        projectId: observedProjectId,
        projectUrl: response.projectUrl || tab.url || '',
        modelUsed: REQUIRED_IMAGE_MODEL,
        exhaustedModels: Array.isArray(response.exhaustedModels) ? response.exhaustedModels : [],
        promptOutcomes,
        warnings,
      },
    });
  } catch (error) {
    const detail = String(error?.message || error);
    const phase = activeUiJob?.id === jobId ? activeUiJob.phase || 'unknown' : 'cleanup';
    const activeTabId = activeUiJob?.id === jobId ? activeUiJob.tabId || '' : '';
    let observedProjectId = flowProjectId || cleanWorkspaceIdentity(job.projectId) || '';
    if (workspaceId && (tab?.id || activeTabId)) {
      const current = await chrome.tabs.get(tab?.id || activeTabId).catch(() => null);
      observedProjectId = flowProjectIdFromTab(current)
        || cleanWorkspaceIdentity(job.projectId)
        || observedProjectId;
      if (current?.id && observedProjectId) {
        const known = workspaceFlowTabs.get(workspaceId);
        await rememberWorkspaceFlowTab(
          workspaceId,
          current,
          observedProjectId,
          known?.createdByExtension === true,
          current.url || current.pendingUrl || known?.projectUrl,
        );
      }
    }
    const projectSuffix = observedProjectId ? `:project=${observedProjectId}` : '';
    sendToAgent({ id, status: 400, error: `${detail}:phase=${phase}:tab=${activeTabId}${projectSuffix}` });
  } finally {
    stopUiWakePulse(wakePulse);
    if (attachedTabId) await detachUiDebugger(attachedTabId);
    if (closeWorkspaceTabAfterSuccess) {
      await closeCompletedWorkspaceFlowTab(workspaceId, tab);
    }
    if (activeUiJob?.id === jobId) {
      activeUiJob = null;
      setState('idle');
    }
  }
}

async function cancelUiJob(jobId) {
  const job = activeUiJob;
  if (!job || (jobId && job.id !== jobId)) return false;
  job.cancelRequested = true;
  const tab = job.tabId ? { id: job.tabId } : null;
  if (tab?.id) {
    await withUiDeadline(sendUiMessage(tab.id, { type: 'DOCURANDOM_UI_CANCEL', jobId: job.id }), 5000, 'cancel-job').catch(() => {});
  }
  return true;
}

// Token is considered fresh if it exists and was captured less than 50 minutes ago.
// Google OAuth tokens expire after ~60 min, so 50 min gives a safe buffer.
function isTokenFresh() {
  if (!flowKey || !metrics.tokenCapturedAt) return false;
  const ageMs = Date.now() - metrics.tokenCapturedAt;
  return ageMs < 50 * 60 * 1000; // 50 minutes
}

async function captureTokenFromFlowTab({ forceReload = false } = {}) {
  // Skip if token is still fresh — no need to open/refresh anything
  if (isTokenFresh() && !forceReload) {
    console.log('[Flow Agent] Token still fresh, skipping tab refresh');
    return;
  }

  if (_openingFlowTab) {
    console.log('[Flow Agent] Flow tab already opening, skipping');
    return;
  }
  _openingFlowTab = true;
  try {
    const tab = await getOrOpenFlowTab();
    if (!tab) {
      console.log('[Flow Agent] Flow tab not ready yet after open');
      return;
    }
    if (forceReload) {
      // Restart the one managed/existing Flow tab in place. getOrOpenFlowTab()
      // restores the remembered tab id or adopts an existing Flow tab before
      // it is ever allowed to create a window, so repeated remote restarts do
      // not multiply Edge windows.
      await reloadFlowTabInPlace(tab.id);
      await waitForTabComplete(tab.id);
      await sleep(750);
      await ensureUiAutomationVersion(tab.id);
    }
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: ['content.js'],
    });
    console.log('[Flow Agent] Token refresh triggered on Flow tab');
  } catch (e) {
    console.error('[Flow Agent] Token refresh failed:', e);
  } finally {
    _openingFlowTab = false;
  }
}


// ─── WebSocket to Agent ─────────────────────────────────────

async function connectToAgent() {
  if (manualDisconnect) return;
  const presence = await getBrowserPresence();
  if (!presence.browserOpen) {
    setState('off');
    scheduleBrowserWindowCloseGrace();
    return;
  }
  if (ws?.readyState === WebSocket.CONNECTING) return;
  if (ws?.readyState === WebSocket.OPEN) return;

  const data = await chrome.storage.local.get(['clientId']);
  const serverIp = CONFIG.DEFAULT_SERVER_HOST;
  connectedServerHost = serverIp;
  const isLocal = /^(127\.0\.0\.1|localhost|192\.168\.|10\.)/.test(serverIp);
  const wsScheme = isLocal ? 'ws' : 'wss';
  const httpScheme = isLocal ? 'http' : 'https';
  const wsUrl = `${wsScheme}://${serverIp}/ws`;

  // Dynamically resolve callbackUrl
  callbackUrl = `${httpScheme}://${serverIp}/api/ext/callback`;

  try {
    ws = new WebSocket(wsUrl);
  } catch (e) {
    console.error('[Flow Agent] WS connect error:', e);
    scheduleReconnect();
    return;
  }

  ws.onopen = async () => {
    console.log('[Flow Agent] Connected to agent: ' + wsUrl);
    chrome.alarms.clear('reconnect');
    setState('idle');

    const storage = await chrome.storage.local.get(['clientId']);
    let clientId = storage.clientId;
    if (!clientId) {
      const prefix = CONFIG.DEFAULT_CLIENT_ID_PREFIX || 'client';
      clientId = `${prefix}-${Math.random().toString(36).substring(2, 8)}`;
      await chrome.storage.local.set({ clientId });
    }
    extensionClientId = clientId;

    // Send current state + resend token if we have one, along with clientId
    ws.send(JSON.stringify({
      type: 'extension_ready',
      clientId: clientId,
      extensionVersion: chrome.runtime.getManifest().version,
      flowKeyPresent: !!flowKey,
      capabilities: UI_CAPABILITIES,
      tokenAge: flowKey && metrics.tokenCapturedAt ? Date.now() - metrics.tokenCapturedAt : null,
      ...presence,
    }));
    if (flowKey) {
      ws.send(JSON.stringify({
        type: 'token_captured',
        clientId: clientId,
        flowKey: flowKey
      }));
    }
    // Backend is reachable again — push any responses queued while it was down.
    flushOutbox();
  };

  ws.onmessage = async ({ data }) => {
    try {
      const msg = JSON.parse(data);

      if (msg.method === 'ui_generate_images') {
        await enqueueUiOperation('generate_images', () => handleUiGenerateImages(msg));
      } else if (msg.method === 'ui_project_snapshot') {
        await enqueueUiOperation('project_snapshot', () => handleUiProjectSnapshot(msg));
      } else if (msg.method === 'ui_inspect_configuration') {
        await enqueueUiOperation('inspect_configuration', () => handleUiInspectConfiguration(msg));
      } else if (msg.method === 'cancel_ui_job') {
        const cancelled = await cancelUiJob(msg.params?.jobId);
        sendToAgent({ id: msg.id, status: cancelled ? 200 : 404, result: { cancelled } });
      } else if (msg.method === 'api_request') {
        await handleApiRequest(msg);
      } else if (msg.method === 'trpc_request') {
        await handleTrpcRequest(msg);
      } else if (msg.method === 'upload_video') {
        await handleUploadVideo(msg);
      } else if (msg.method === 'solve_captcha') {
        await handleSolveCaptcha(msg);
      } else if (msg.method === 'get_status') {
        sendToAgent({
          id: msg.id,
          result: {
            state,
            uiQueueDepth: uiOperationQueueDepth,
            uiJobId: activeUiJob?.id || '',
            uiJobAgeMs: activeUiJob ? uiJobIdleAgeMs(activeUiJob) : 0,
            uiJobPhase: activeUiJob?.phase || '',
            uiJobTabId: activeUiJob?.tabId || null,
            flowKeyPresent: !!flowKey,
            manualDisconnect,
            tokenAge: metrics.tokenCapturedAt ? Date.now() - metrics.tokenCapturedAt : null,
            metrics,
          },
        });
      } else if (msg.method === 'prepare_flow_tab') {
        // Prepare/reuse a background Flow tab and return its identity. This is
        // the routine path used by the desktop button and must not activate a
        // tab or create a new window when an Edge window is already open.
        const existingOnly = msg.existingOnly === true;
        const managedTab = await prepareFlowTab({ existingOnly });
        if (!managedTab?.id) {
          sendToAgent({
            id: msg.id,
            status: 404,
            error: 'FLOW_TAB_NOT_FOUND',
          });
        } else {
          sendToAgent({
            id: msg.id,
            status: 200,
            result: {
              tabId: managedTab.id,
              windowId: managedTab.windowId || null,
              active: managedTab.active === true,
              createdByExtension: workTabCreatedByExtension,
            },
          });
        }
      } else if (msg.method === 'reload_extension') {
        // Reload the MV3 worker in place. Opening chrome-extension://popup.html
        // used to create a visible/focused Edge tab during every update.
        const response = {
          id: msg.id,
          status: 200,
          result: {
            reloaded: true,
            extensionVersion: chrome.runtime.getManifest().version,
          },
        };
        if (ws?.readyState === WebSocket.OPEN) {
          try { ws.send(JSON.stringify(response)); } catch { sendToAgent(response); }
        } else {
          sendToAgent(response);
        }
        setTimeout(() => {
          try { chrome.runtime.reload(); } catch (error) {
            console.error('[Flow Agent] In-place extension reload failed:', error);
          }
        }, 150);
      } else if (msg.method === 'open_flow_tab') {
        // Python bridge asks us to prepare a Flow tab. Passive extension
        // reconnects set existingOnly and must never create another tab.
        // Always prepare the tab before checking token freshness. A cached
        // token must never make the bridge believe a missing Flow tab exists.
        const existingOnly = msg.existingOnly === true;
        const managedTab = await prepareFlowTab({ existingOnly });
        if (!managedTab?.id) {
          console.log('[Flow Agent] No Flow tab available; passive reconnect did not open one');
          return;
        }
        if (!isTokenFresh()) {
          console.log('[Flow Agent] open_flow_tab: token missing/expired, recovering existing Flow tab');
          await recoverFlowUiInPlace(managedTab.id);
          await chrome.scripting.executeScript({
            target: { tabId: managedTab.id },
            files: ['content.js'],
          }).catch(() => null);
          await sleep(5000);
        } else {
          console.log('[Flow Agent] open_flow_tab: reused Flow tab and cached token');
        }
        if (flowKey && ws?.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'token_captured', flowKey }));
          console.log('[Flow Agent] Sent token for prepared Flow tab');
        } else {
          const data = await chrome.storage.local.get(['flowKey']);
          if (data.flowKey) {
            flowKey = data.flowKey;
            if (ws?.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ type: 'token_captured', flowKey }));
              console.log('[Flow Agent] Sent token from storage for prepared Flow tab');
            }
          }
        }
      } else if (msg.method === 'recover_flow_ui') {
        const tab = await getOrOpenFlowTab();
        if (tab?.id) {
          await recoverFlowUiInPlace(tab.id);
          await ensureUiAutomationVersion(tab.id);
          console.log('[Flow Agent] Recovered Flow UI in place without page reload');
        }
      } else if (msg.method === 'refresh_flow_tab' || msg.method === 'force_refresh') {
        // Python bridge asks us to refresh token.
        // force_refresh (or an explicit msg.force) bypasses the freshness check:
        // Google can invalidate a token via inactivity long before its 50-min
        // age limit, so a "fresh" token may still be dead (401). In that case we
        // must actually reload the tab and re-capture, not resend the cached one.
        const force = msg.force === true || msg.method === 'force_refresh';
        if (force && activeUiJob) {
          console.warn('[Flow Agent] Deferred hard page refresh while a UI image job is active');
          return;
        }
        if (isTokenFresh() && !force) {
          console.log('[Flow Agent] refresh_flow_tab: token fresh, sending cached token');
          if (ws?.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'token_captured', flowKey }));
          }
        } else {
          console.log('[Flow Agent] refresh_flow_tab: forcing tab reload + re-capture');
          // Drop the stale token so captureTokenFromFlowTab can't short-circuit.
          if (force) {
            flowKey = null;
            metrics.tokenCapturedAt = null;
          }
          await captureTokenFromFlowTab({ forceReload: force });
          await sleep(3000);
          if (flowKey && ws?.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'token_captured', flowKey }));
            console.log('[Flow Agent] Sent token after refresh');
          } else {
            const data = await chrome.storage.local.get(['flowKey']);
            if (data.flowKey) {
              flowKey = data.flowKey;
              if (ws?.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'token_captured', flowKey }));
                console.log('[Flow Agent] Sent token from storage after refresh');
              }
            }
          }
        }
      } else if (msg.type === 'callback_config') {
        callbackSecret = msg.secret;
        callbackUrl = normalizeCallbackUrl(msg.callback_url);
        chrome.storage.local.set({ callbackSecret: msg.secret, callbackUrl });
        console.log('[Flow Agent] Received callback config:', callbackUrl);
      } else if (msg.type === 'callback_secret') {
        callbackSecret = msg.secret;
        chrome.storage.local.set({ callbackSecret: msg.secret });
        console.log('[Flow Agent] Received callback secret');
      } else if (msg.type === 'pong') {
        // keepalive response
      }
    } catch (e) {
      console.error('[Flow Agent] Message error:', e);
    }
  };

  ws.onclose = () => {
    setState('off');
    if (!manualDisconnect) scheduleReconnect();
  };

  ws.onerror = (e) => {
    console.error('[Flow Agent] WS error:', e);
    metrics.lastError = 'WS_ERROR';
    chrome.storage.local.set({ metrics });
  };
}

function scheduleReconnect() {
  chrome.alarms.create('reconnect', { delayInMinutes: 0.5 });
}

async function keepAlive() {
  const presence = await sendBrowserPresence();
  if (!presence.browserOpen) {
    setState('off');
    scheduleBrowserWindowCloseGrace();
    return;
  }
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'ping', ...presence }));
  } else {
    connectToAgent();
  }
}

function sendToAgent(msg) {
  // API responses (with msg.id) go through a durable outbox so a generated
  // result is never lost — persisted and retried until the agent acks it.
  if (msg.id) {
    enqueueResponse(msg);
    return;
  }
  // Non-response messages (ping, status, token) — best-effort over WS.
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
  }
}

// ─── Durable Response Outbox ────────────────────────────────
// A generated image/video result must survive a momentary backend hiccup or a
// service-worker restart. Every id-bearing response is persisted and retried
// with backoff until the agent confirms receipt, then dropped.

const MAX_DELIVERY_ATTEMPTS = 8;
let outbox = {};              // id -> { msg, attempts, nextAt }
let _flushingOutbox = false;

async function loadOutbox() {
  try {
    const { responseOutbox } = await chrome.storage.local.get('responseOutbox');
    if (responseOutbox && typeof responseOutbox === 'object') outbox = responseOutbox;
  } catch { }
}

function persistOutbox() {
  chrome.storage.local.set({ responseOutbox: outbox }).catch(() => { });
}

function enqueueResponse(msg) {
  outbox[msg.id] = { msg, attempts: 0, nextAt: 0 };
  persistOutbox();
  flushOutbox();
}

async function deliverOnce(entry) {
  try {
    const serverIp = connectedServerHost || CONFIG.DEFAULT_SERVER_HOST;
    const targetCallbackUrl = normalizeCallbackUrl(serverIp);

    const resp = await fetch(targetCallbackUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(entry.msg),
    });
    // Any HTTP reply means the backend is reachable and has taken the response
    // (ok:true = matched a request, ok:false = unknown id / already handled).
    // Either way there is nothing to retry — only transport failures retry.
    if (resp.ok) return true;
    // 5xx / transient server error — retry.
    return false;
  } catch {
    // Network error: backend unreachable. Try WS as an immediate fallback but
    // keep the entry queued so a later flush can still deliver it.
    if (ws?.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify(entry.msg)); } catch { }
    }
    return false;
  }
}

async function flushOutbox() {
  if (_flushingOutbox) return;
  _flushingOutbox = true;
  try {
    const ids = Object.keys(outbox);
    if (!ids.length) return;
    const now = Date.now();
    for (const id of ids) {
      const entry = outbox[id];
      if (!entry) continue;
      if (entry.nextAt && entry.nextAt > now) continue;
      const delivered = await deliverOnce(entry);
      if (delivered) {
        delete outbox[id];
        persistOutbox();
        continue;
      }
      entry.attempts++;
      if (entry.attempts >= MAX_DELIVERY_ATTEMPTS) {
        console.error('[Flow Agent] Dropping response', id, 'after', entry.attempts, 'failed deliveries');
        delete outbox[id];
      } else {
        // Exponential backoff, capped at 30s.
        entry.nextAt = Date.now() + Math.min(30000, 1000 * 2 ** entry.attempts);
      }
      persistOutbox();
    }
  } finally {
    _flushingOutbox = false;
  }
}

// ─── reCAPTCHA Solving ──────────────────────────────────────

async function requestCaptchaFromTab(tabId, requestId, pageAction) {
  try {
    return await chrome.tabs.sendMessage(tabId, {
      type: 'GET_CAPTCHA',
      requestId,
      pageAction,
    });
  } catch (error) {
    const msg = error?.message || '';
    const shouldInject =
      msg.includes('Receiving end does not exist') ||
      msg.includes('Could not establish connection');
    if (!shouldInject) throw error;

    // Inject content script and retry
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ['content.js'],
    });
    await sleep(200);
    return await chrome.tabs.sendMessage(tabId, {
      type: 'GET_CAPTCHA',
      requestId,
      pageAction,
    });
  }
}

async function solveCaptcha(requestId, captchaAction) {
  const tab = await getOrOpenFlowTab();
  if (!tab) return { error: 'NO_FLOW_TAB' };

  try {
    const resp = await Promise.race([
      requestCaptchaFromTab(tab.id, requestId, captchaAction),
      new Promise((_, rej) => setTimeout(() => rej(new Error('CAPTCHA_TIMEOUT')), 30000)),
    ]);
    return resp;
  } catch (e) {
    return { error: e.message };
  }
}

async function handleSolveCaptcha(msg) {
  const { id, params } = msg;
  const result = await solveCaptcha(id, params?.captchaAction || 'VIDEO_GENERATION');

  // Standalone captcha solve counts as captcha-consuming
  metrics.requestCount++;
  if (result?.token) {
    metrics.successCount++;
  } else {
    metrics.failedCount++;
    metrics.lastError = result?.error || 'NO_TOKEN';
  }
  chrome.storage.local.set({ metrics });

  sendToAgent({ id, result });
}

// ─── API Request Proxy ──────────────────────────────────────

async function handleTrpcRequest(msg) {
  const { id, params } = msg;
  const { url, method = 'POST', headers = {}, body } = params;

  if (!url || !(
    url.startsWith('https://flow.google.com/')
    || url.startsWith('https://labs.google/')
  )) {
    sendToAgent({ id, error: 'INVALID_TRPC_URL' });
    return;
  }

  setState('running');
  // TRPC calls don't consume captcha and are silent — no metrics, no request log.

  const fetchHeaders = { 'Content-Type': 'application/json', ...headers };
  if (flowKey) {
    fetchHeaders['authorization'] = `Bearer ${flowKey}`;
  }

  try {
    const resp = await fetch(url, {
      method,
      headers: fetchHeaders,
      body: body ? JSON.stringify(body) : undefined,
      credentials: 'include',
    });
    const data = await resp.json();
    sendToAgent({ id, status: resp.status, data });
  } catch (e) {
    console.error('[Flow Agent] tRPC request failed:', e);
    sendToAgent({ id, error: e.message || 'TRPC_FETCH_FAILED' });
  } finally {
    setState('idle');
  }
}


async function handleUploadVideo(msg) {
  const { id, params } = msg;
  const { videoBase64, projectId, videoSize } = params;

  try {
    const tabs = await chrome.tabs.query({ url: FLOW_TAB_URLS });
    if (!tabs.length) {
      sendToAgent({ id, error: 'NO_FLOW_TAB' });
      return;
    }

    const size = videoSize || (videoBase64 ? Math.floor(videoBase64.length * 3 / 4) : 0);

    // Get session URL via page context XHR (needs session cookies)
    const startResults = await chrome.scripting.executeScript({
      target: { tabId: tabs[0].id },
      world: 'MAIN',
      func: (projId, sz) => {
        return new Promise((resolve) => {
          const xhr = new XMLHttpRequest();
          xhr.open('POST', '/fx/api/upload-video?action=start');
          xhr.setRequestHeader('X-Upload-Project-Id', projId);
          xhr.setRequestHeader('X-Upload-Content-Type', 'video/mp4');
          xhr.setRequestHeader('X-Upload-Content-Length', sz.toString());
          xhr.withCredentials = true;
          xhr.onload = () => {
            let data;
            try { data = JSON.parse(xhr.responseText); } catch { data = {}; }
            resolve({
              sessionUrl: data.sessionUrl || xhr.getResponseHeader('X-Upload-Session-Url') || '',
              status: xhr.status,
            });
          };
          xhr.onerror = () => resolve({ error: 'POST_FAILED' });
          xhr.send();
        });
      },
      args: [projectId, size],
    });

    const step1 = startResults?.[0]?.result;
    if (!step1 || step1.error || !step1.sessionUrl) {
      sendToAgent({ id, error: step1?.error || 'NO_SESSION_URL' });
      return;
    }

    // Return sessionUrl + token — caller handles PUT
    sendToAgent({
      id,
      result: {
        sessionUrl: step1.sessionUrl,
        token: flowKey || '',
      },
    });
  } catch (e) {
    sendToAgent({ id, error: `UPLOAD_ERROR: ${e.message}` });
  }
}

async function handleApiRequest(msg) {
  const { id, params } = msg;
  const { url, method, headers, body, captchaAction } = params;

  if (!url) {
    sendToAgent({ id, error: 'MISSING_URL' });
    return;
  }

  if (!url.startsWith('https://aisandbox-pa.googleapis.com/')) {
    sendToAgent({ id, error: 'INVALID_URL' });
    return;
  }

  setState('running');
  const hasCaptcha = !!captchaAction;
  if (hasCaptcha) metrics.requestCount++;

  const logId = id;
  const logType = _classifyApiUrl(url);
  if (_VISIBLE_TYPES.has(logType)) {
    const payloadSummary = body ? JSON.stringify(body).slice(0, 200) : null;
    addRequestLog({ id: logId, type: logType, time: new Date().toISOString(), status: 'processing', error: null, outputUrl: null, url, payloadSummary });
  }

  try {
    // Step 1: Solve captcha if needed
    let captchaToken = null;
    if (captchaAction) {
      const captchaResult = await solveCaptcha(id, captchaAction);
      captchaToken = captchaResult?.token || null;
      if (!captchaToken) {
        // Cannot proceed without captcha — API will 403
        const err = captchaResult?.error || 'CAPTCHA_FAILED';
        console.error(`[Flow Agent] Captcha failed for ${captchaAction}: ${err}`);
        sendToAgent({ id, status: 403, error: `CAPTCHA_FAILED: ${err}` });
        if (hasCaptcha) { metrics.failedCount++; metrics.lastError = `CAPTCHA_FAILED: ${err}`; }
        chrome.storage.local.set({ metrics });
        updateRequestLog(logId, { status: 'failed', error: `CAPTCHA_FAILED: ${err}` });
        setState('idle');
        return;
      }
    }

    // Step 2: Inject captcha token into body
    let finalBody = body;
    if (captchaToken && finalBody) {
      finalBody = JSON.parse(JSON.stringify(finalBody)); // deep clone
      if (finalBody.clientContext?.recaptchaContext) {
        finalBody.clientContext.recaptchaContext.token = captchaToken;
      }
      if (finalBody.requests && Array.isArray(finalBody.requests)) {
        for (const req of finalBody.requests) {
          if (req.clientContext?.recaptchaContext) {
            req.clientContext.recaptchaContext.token = captchaToken;
          }
        }
      }
    }

    // Step 3: Use flowKey for auth
    const activeFlowKey = flowKey;
    if (!activeFlowKey) {
      sendToAgent({ id, status: 503, error: 'NO_FLOW_KEY' });
      if (hasCaptcha) { metrics.failedCount++; metrics.lastError = 'NO_FLOW_KEY'; }
      chrome.storage.local.set({ metrics });
      updateRequestLog(logId, { status: 'failed', error: 'NO_FLOW_KEY' });
      setState('idle');
      return;
    }

    const fetchHeaders = { ...(headers || {}) };
    fetchHeaders['authorization'] = `Bearer ${activeFlowKey}`;

    // Step 4: Make the API call from browser context
    const response = await fetch(url, {
      method: method || 'POST',
      headers: fetchHeaders,
      credentials: 'include',
      body: method === 'GET' ? undefined : JSON.stringify(finalBody),
    });

    let responseData;
    const responseText = await response.text();
    try {
      responseData = JSON.parse(responseText);
    } catch {
      responseData = responseText;
    }

    // Self-heal: a 401 means Google invalidated our cached token (usually via
    // inactivity, before our 50-min freshness window). Drop it so the very next
    // request / refresh forces a genuine tab reload + re-capture instead of
    // resending the same dead token.
    if (response.status === 401) {
      console.warn('[Flow Agent] 401 UNAUTHENTICATED — invalidating cached token to force refresh');
      flowKey = null;
      metrics.tokenCapturedAt = null;
      chrome.storage.local.set({ flowKey: null });
    }

    sendToAgent({
      id,
      status: response.status,
      data: responseData,
    });

    const responseSummary = responseText ? responseText.slice(0, 300) : null;
    if (response.ok) {
      if (hasCaptcha) { metrics.successCount++; metrics.lastError = null; }
      updateRequestLog(logId, { status: 'success', httpStatus: response.status, responseSummary });
    } else {
      if (hasCaptcha) { metrics.failedCount++; metrics.lastError = `API_${response.status}`; }
      updateRequestLog(logId, { status: 'failed', error: `API_${response.status}`, httpStatus: response.status, responseSummary });
    }
  } catch (e) {
    sendToAgent({
      id,
      status: 500,
      error: e.message || 'API_REQUEST_FAILED',
    });
    if (hasCaptcha) { metrics.failedCount++; metrics.lastError = e.message; }
    updateRequestLog(logId, { status: 'failed', error: e.message || 'API_REQUEST_FAILED' });
  }

  chrome.storage.local.set({ metrics });
  setState('idle');
}

// ─── State & Popup ──────────────────────────────────────────

function setState(newState) {
  state = newState;
  const badges = { idle: '●', running: '▶', off: '○' };
  const colors = { idle: '#22c55e', running: '#f59e0b', off: '#6b7280' };
  chrome.action.setBadgeText({ text: badges[state] || '' });
  chrome.action.setBadgeBackgroundColor({ color: colors[state] || '#000' });
  broadcastStatus();
}

function broadcastStatus() {
  chrome.runtime.sendMessage({ type: 'STATUS_PUSH' }).catch(() => { });
}

chrome.runtime.onMessage.addListener((msg, _, reply) => {
  if (msg.type === 'UI_BACKGROUND_SLEEP') {
    // Content-page timers are aggressively throttled when Edge is minimized.
    // Holding this extension response port moves the wait into the background
    // worker, so DOM polling keeps its real cadence without focusing Flow.
    const delayMs = Math.max(0, Math.min(10000, Number(msg.delayMs) || 0));
    setTimeout(() => reply({ ok: true, elapsed: delayMs }), delayMs);
    return true;
  }

  if (msg.type === 'UI_NAVIGATE_FLOW_HOME') {
    const tabId = _?.tab?.id;
    if (!tabId) {
      reply({ ok: false, error: 'FLOW_TAB_ID_MISSING' });
      return true;
    }
    chrome.tabs.update(tabId, { url: FLOW_URL })
      .then(() => waitForTabComplete(tabId))
      .then(() => reply({ ok: true, tabId }))
      .catch((error) => reply({
        ok: false,
        error: String(error?.message || error || 'FLOW_HOME_NAVIGATION_FAILED'),
      }));
    return true;
  }

  if (msg.type === 'UI_TRUSTED_REPLACE_TEXT') {
    const tabId = _?.tab?.id;
    if (!tabId) {
      reply({ ok: false, error: 'FLOW_TAB_ID_MISSING' });
      return true;
    }
    trustedUiReplaceText(tabId, msg.text)
      .then(() => reply({ ok: true }))
      .catch((error) => reply({ ok: false, error: String(error?.message || error) }));
    return true;
  }

  if (msg.type === 'UI_TRUSTED_PRESS_ENTER') {
    const tabId = _?.tab?.id;
    if (!tabId) {
      reply({ ok: false, error: 'FLOW_TAB_ID_MISSING' });
      return true;
    }
    trustedUiPressEnter(tabId)
      .then(() => reply({ ok: true }))
      .catch((error) => reply({ ok: false, error: String(error?.message || error) }));
    return true;
  }

  if (msg.type === 'UI_TRUSTED_PRESS_ESCAPE') {
    const tabId = _?.tab?.id;
    if (!tabId) {
      reply({ ok: false, error: 'FLOW_TAB_ID_MISSING' });
      return true;
    }
    trustedUiPressEscape(tabId)
      .then(() => reply({ ok: true }))
      .catch((error) => reply({ ok: false, error: String(error?.message || error) }));
    return true;
  }

  if (msg.type === 'UI_AUTOMATION_PROGRESS') {
    if (
      msg.phase === 'submitted'
      && activeUiJob
      && String(msg.jobId || '') === activeUiJob.id
    ) {
      activeUiJob.submitted = true;
    }
    if (activeUiJob && String(msg.jobId || '') === activeUiJob.id) {
      activeUiJob.progressObserved = true;
      activeUiJob.lastProgressAt = Date.now();
      activeUiJob.projectId = cleanWorkspaceIdentity(msg.projectId) || activeUiJob.projectId;
    }
    chrome.runtime.sendMessage({ type: 'UI_AUTOMATION_PROGRESS_PUSH', ...msg }).catch(() => { });
    sendToAgent({
      type: 'ui_progress',
      requestId: activeUiJob?.id === String(msg.jobId || '') ? activeUiJob.requestId : null,
      jobId: msg.jobId,
      progress: msg.progress,
      phase: msg.phase,
      message: msg.message,
      projectId: cleanWorkspaceIdentity(msg.projectId),
      projectUrl: String(msg.projectUrl || '').trim().slice(0, 1000),
    });
    reply({ ok: true });
    return true;
  }

  if (msg.type === 'SETTINGS_UPDATED') {
    if (ws) {
      try { ws.close(); } catch { }
    }
    connectToAgent();
    reply({ ok: true });
    return true;
  }

  if (msg.type === 'STATUS') {
    reply({
      connected: ws?.readyState === WebSocket.OPEN,
      agentConnected: ws?.readyState === WebSocket.OPEN,
      flowKeyPresent: !!flowKey,
      manualDisconnect,
      tokenAge: metrics.tokenCapturedAt ? Date.now() - metrics.tokenCapturedAt : null,
      metrics: {
        requestCount: metrics.requestCount,
        successCount: metrics.successCount,
        failedCount: metrics.failedCount,
        lastError: metrics.lastError,
      },
      state,
      clientId: extensionClientId,
    });
  }

  if (msg.type === 'DISCONNECT') {
    manualDisconnect = true;
    if (ws) ws.close();
    reply({ ok: true });
    return true;
  }

  if (msg.type === 'RECONNECT') {
    manualDisconnect = false;
    connectToAgent();
    reply({ ok: true });
    return true;
  }

  if (msg.type === 'REQUEST_LOG') {
    reply({ log: requestLog });
    return true;
  }

  if (msg.type === 'GET_CLIENT_CREDITS') {
    const host = String(connectedServerHost || CONFIG.DEFAULT_SERVER_HOST).trim().replace(/\/$/, '');
    const hostWithoutScheme = host.replace(/^https?:\/\//i, '');
    const local = /^(127\.0\.0\.1|localhost|192\.168\.|10\.)(:|$)/.test(hostWithoutScheme);
    const base = /^https?:\/\//i.test(host) ? host : `${local ? 'http' : 'https'}://${host}`;
    chrome.storage.local.get(['clientId']).then(({ clientId }) => fetch(`${base}/v1/credits`, {
      headers: (extensionClientId || clientId) ? { 'X-Client-Id': extensionClientId || clientId } : {},
    }))
      .then(async (response) => {
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
        reply(data);
      })
      .catch((error) => {
        console.error('[Flow Agent] Credit request failed:', error);
        reply({ error: error.message });
      });
    return true;
  }

  if (msg.type === 'CLEAR_REQUEST_LOG') {
    requestLog = [];
    chrome.storage.local.remove('requestLog').then(() => {
      broadcastRequestLog();
      reply({ ok: true });
    });
    return true;
  }

  if (msg.type === 'ADD_HISTORY') {
    addRequestLog({
      id: msg.entry?.id || `popup-${Date.now()}`,
      time: msg.entry?.time || new Date().toISOString(),
      type: msg.entry?.type || 'GEN_IMG',
      status: msg.entry?.status || 'success',
      url: msg.entry?.url || '',
      payloadSummary: msg.entry?.prompt || '',
      responseSummary: msg.entry?.url ? 'Generated result ready' : 'Generation completed',
    });
    reply({ ok: true });
    return true;
  }

  if (msg.type === 'OPEN_FLOW_TAB') {
    prepareFlowTab()
      .then((tab) => {
        if (!tab?.id) throw new Error('FLOW_TAB_NOT_FOUND');
        reply({
          ok: true,
          tabId: tab.id,
          windowId: tab.windowId || null,
          active: tab.active === true,
        });
      })
      .catch((e) => reply({ ok: false, error: e.message }));
    return true;
  }

  if (msg.type === 'REFRESH_TOKEN') {
    captureTokenFromFlowTab()
      .then(() => reply({ ok: true }))
      .catch((e) => reply({ error: e.message }));
    return true;
  }

  if (msg.type === 'TEST_CAPTCHA') {
    solveCaptcha(`test-${Date.now()}`, msg.pageAction || 'IMAGE_GENERATION')
      .then((r) => reply(r))
      .catch((e) => reply({ error: e.message }));
    return true;
  }

  if (msg.type === 'TRPC_MEDIA_URLS') {
    handleTrpcMediaUrls(msg.trpcUrl, msg.body);
    reply({ ok: true });
    return true;
  }

  return true;
});

// ─── TRPC Media URL Extractor ──────────────────────────────

function handleTrpcMediaUrls(trpcUrl, bodyText) {
  try {
    // Extract all fresh GCS signed URLs
    const urlRegex = /https:\/\/storage\.googleapis\.com\/ai-sandbox-videofx\/(?:image|video)\/[0-9a-f-]{36}\?[^"'\s]+/g;
    const matches = bodyText.match(urlRegex) || [];
    if (!matches.length) return;

    // Deduplicate and parse
    const urlMap = {};
    for (const rawUrl of matches) {
      // Unescape JSON-escaped URLs
      const url = rawUrl.replace(/\\u0026/g, '&').replace(/\\/g, '');
      const mediaMatch = url.match(/\/(image|video)\/([0-9a-f-]{36})\?/);
      if (mediaMatch) {
        const [, mediaType, mediaId] = mediaMatch;
        // Keep last occurrence (freshest)
        urlMap[mediaId] = { mediaType, url, mediaId };
      }
    }

    const entries = Object.values(urlMap);
    if (!entries.length) return;

    console.log(`[Flow Agent] Captured ${entries.length} fresh media URLs from TRPC`);
    // URL refresh is silent — don't show in request log

    // Forward to agent for DB update
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'media_urls_refresh',
        urls: entries,
      }));
    }
  } catch (e) {
    console.error('[Flow Agent] Failed to extract TRPC media URLs:', e);
  }
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// ─── Human-like Telemetry ──────────────────────────────────
// Periodically send tracking events to Google's analytics endpoints
// to mimic normal browser behavior.

const _UA = navigator.userAgent;
let _telemetrySessionId = `;${Date.now()}`;

function _rand(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }

function _buildBatchLogPayload() {
  const events = [];
  const types = ['FLOW_IMAGE_LATENCY', 'FLOW_VIDEO_LATENCY'];
  const count = _rand(1, 3);
  for (let i = 0; i < count; i++) {
    events.push({
      event: types[_rand(0, types.length - 1)],
      eventProperties: [
        { key: 'CURRENT_TIME_MS', doubleValue: Date.now() },
        { key: 'DURATION_MS', doubleValue: _rand(150, 800) },
        { key: 'USER_AGENT', stringValue: _UA },
        { key: 'IS_DESKTOP', booleanValue: true },
      ],
      eventMetadata: { sessionId: _telemetrySessionId },
      eventTime: new Date().toISOString(),
    });
  }
  return { appEvents: events };
}

function _buildFrontendEventsPayload() {
  const eventTypes = [
    'FLOW_IMAGE_LATENCY', 'FLOW_VIDEO_LATENCY', 'GRID_SCROLL_DEPTH',
    'FLOW_PROJECT_OPEN', 'FLOW_SCENE_VIEW',
  ];
  const count = _rand(1, 4);
  const events = [];
  for (let i = 0; i < count; i++) {
    const et = eventTypes[_rand(0, eventTypes.length - 1)];
    const params = {
      USER_AGENT: { '@type': 'type.googleapis.com/google.protobuf.StringValue', value: _UA },
      IS_DESKTOP: { '@type': 'type.googleapis.com/google.protobuf.StringValue', value: 'true' },
    };
    if (et.includes('LATENCY')) {
      params.CURRENT_TIME_MS = { '@type': 'type.googleapis.com/google.protobuf.StringValue', value: String(Date.now()) };
      params.DURATION_MS = { '@type': 'type.googleapis.com/google.protobuf.StringValue', value: String(_rand(100, 600)) };
    }
    if (et === 'GRID_SCROLL_DEPTH') {
      params.MEDIA_GENERATION_PAYGATE_TIER = { '@type': 'type.googleapis.com/google.protobuf.StringValue', value: 'PAYGATE_TIER_TWO' };
    }
    events.push({
      eventType: et,
      metadata: {
        sessionId: _telemetrySessionId,
        createTime: new Date().toISOString(),
        additionalParams: params,
      },
    });
  }
  return { events };
}

async function sendTelemetry() {
  if (!flowKey || state === 'off') return;

  const headers = {
    'Content-Type': 'text/plain;charset=UTF-8',
    'authorization': `Bearer ${flowKey}`,
  };

  // Telemetry is silent — don't show in request log
  try {
    if (Math.random() < 0.5) {
      await fetch(`https://aisandbox-pa.googleapis.com/v1:batchLog`, {
        method: 'POST', headers, credentials: 'include',
        body: JSON.stringify(_buildBatchLogPayload()),
      });
    } else {
      await fetch(`https://aisandbox-pa.googleapis.com/v1/flow:batchLogFrontendEvents`, {
        method: 'POST', headers, credentials: 'include',
        body: JSON.stringify(_buildFrontendEventsPayload()),
      });
    }
  } catch { }
}

// Send telemetry at random intervals (45-120s) to look organic
function scheduleTelemetry() {
  const delay = _rand(45, 120) * 1000;
  setTimeout(async () => {
    await sendTelemetry();
    scheduleTelemetry(); // reschedule with new random interval
  }, delay);
}

// Refresh session ID every ~30min like a real user
setInterval(() => { _telemetrySessionId = `;${Date.now()}`; }, _rand(25, 35) * 60 * 1000);

scheduleTelemetry();

console.log('[Flow Agent] Extension loaded');
