const BRIDGE_URL = "ws://127.0.0.1:17842";
const EXTENSION_VERSION = chrome.runtime.getManifest().version;
importScripts('google-workspace.js');

let socket = null;
let connectInFlight = null;
let reconnectTimer = null;
let keepAliveTimer = null;
let controlledTabId = null;
let pairingInProgress = false;
let bridgeState = {
  connected: false,
  paired: false,
  profileId: "",
  error: "",
};

function randomClientId() {
  return crypto.randomUUID().replaceAll("-", "");
}

async function identity() {
  const stored = await chrome.storage.local.get([
    "clientId",
    "profileId",
    "clientSecret",
  ]);
  const clientId = stored.clientId || randomClientId();
  if (!stored.clientId) {
    await chrome.storage.local.set({ clientId });
  }
  return {
    clientId,
    profileId: stored.profileId || "",
    clientSecret: stored.clientSecret || "",
  };
}

async function updateState(patch) {
  bridgeState = { ...bridgeState, ...patch };
  await chrome.storage.local.set({ bridgeState });
}

function send(payload) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    throw new Error("Bridge local chưa kết nối.");
  }
  socket.send(JSON.stringify(payload));
}

function scheduleReconnect() {
  if (pairingInProgress) return;
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    void connect();
  }, 1500);
}

function ensureKeepAlive() {
  if (keepAliveTimer !== null) return;
  // Chrome MV3 may suspend a service worker after roughly 30 seconds.
  // WebSocket traffic inside that window resets the idle timer, keeping long
  // Studio uploads connected without opening or focusing an extension page.
  keepAliveTimer = setInterval(() => {
    if (socket?.readyState === WebSocket.OPEN) {
      try {
        socket.send(JSON.stringify({ type: "client_ping", at: Date.now() }));
      } catch {
        socket?.close();
      }
    } else {
      connect();
    }
  }, 20000);
}

async function connect() {
  if (connectInFlight) return connectInFlight;
  const pending = connectImpl();
  connectInFlight = pending;
  try {
    return await pending;
  } finally {
    if (connectInFlight === pending) connectInFlight = null;
  }
}

async function connectImpl() {
  ensureKeepAlive();
  // The service worker starts a background connect on load.  When DocuRandom
  // opens the pairing page at the same time, that old connect must not race
  // pairNow() and overwrite its diagnostic state with the generic
  // "not paired" state.
  if (pairingInProgress) return;
  const id = await identity();
  if (pairingInProgress) return;
  if (!id.profileId || !id.clientSecret) {
    await updateState({
      connected: false,
      paired: false,
      profileId: "",
      error: "Chưa ghép với profile DocuRandom.",
    });
    return;
  }
  if (
    socket &&
    (socket.readyState === WebSocket.OPEN ||
      socket.readyState === WebSocket.CONNECTING)
  ) {
    return;
  }
  const candidate = new WebSocket(BRIDGE_URL);
  socket = candidate;
  candidate.onopen = () => {
    if (socket !== candidate) return;
    send({
      type: "hello",
      client_id: id.clientId,
      profile_id: id.profileId,
      secret: id.clientSecret,
      extension_version: EXTENSION_VERSION,
    });
  };
  candidate.onmessage = async (event) => {
    if (socket !== candidate) return;
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (message.type === "hello_ok") {
      await updateState({
        connected: true,
        paired: true,
        profileId: message.profile_id || id.profileId,
        error: "",
      });
      return;
    }
    if (message.type === "command") {
      await runCommand(message);
      return;
    }
    if (message.type === "error") {
      await updateState({ connected: false, error: message.error || "Lỗi bridge." });
    }
  };
  candidate.onclose = async () => {
    if (socket !== candidate) return;
    socket = null;
    await updateState({ connected: false });
    scheduleReconnect();
  };
  candidate.onerror = () => {
    if (socket === candidate) candidate.close();
  };
}

async function activeTab(params = {}) {
  const requestedTabId = Number(params.tab_id || params.tabId || 0);
  if (Number.isInteger(requestedTabId) && requestedTabId > 0) {
    return await chrome.tabs.get(requestedTabId);
  }
  if (controlledTabId !== null) {
    try {
      return await chrome.tabs.get(controlledTabId);
    } catch {
      controlledTabId = null;
    }
  }
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tabs[0]?.id) {
    throw new Error("Profile Donut chưa có tab hoạt động.");
  }
  return tabs[0];
}

async function executeInTab(tabId, func, args = [], world = "MAIN") {
  // A checkview "next" call clicks the pagination control inside the page.
  // An empty response does not prove the click failed, so never execute it a
  // second time here. The host can reconcile using a read-only snapshot.
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world,
    func,
    args,
  });
  if (!Array.isArray(results) || !results.length) {
    throw new Error("SCRIPT_EXECUTION_RETURNED_NO_FRAME");
  }
  if (results[0]?.error) {
    throw new Error(String(results[0].error));
  }
  if (results[0].result === undefined ||
      (func === checkviewContentPageInTab && results[0].result === null)) {
    throw new Error("SCRIPT_EXECUTION_RETURNED_NO_RESULT");
  }
  return results[0].result;
}

// Return stable, ID-scoped rows from Studio's Content page.  Studio's current
// draft rows do not always expose an /video/<id>/edit anchor: a draft can have
// only a thumbnail URL (/vi/<id>/...) and an anchor whose accessible name is
// the title.  Keep this reader in the extension transport so recovery does not
// depend on the native Runtime.evaluate route.
function contentStateInTab(expectedVideoId, action, currentTabId) {
  try {
  const normalize = (value) => String(value || "")
    .normalize("NFKC")
    .replace(/\s+/g, " ")
    .trim()
    .toLocaleLowerCase();
  const roots = [];
  const seenRoots = new Set();
  const visitRoots = (root) => {
    if (!root || !root.querySelectorAll || seenRoots.has(root)) return;
    seenRoots.add(root);
    roots.push(root);
    for (const element of root.querySelectorAll("*")) {
      if (element.shadowRoot) visitRoots(element.shadowRoot);
    }
  };
  const renderedText = (root) => {
    const chunks = [];
    const visitText = (node) => {
      if (!node) return;
      if (node.nodeType === Node.TEXT_NODE) {
        const value = String(node.nodeValue || "").trim();
        if (value) chunks.push(value);
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE &&
          node.nodeType !== Node.DOCUMENT_FRAGMENT_NODE) return;
      if (node.nodeType === Node.ELEMENT_NODE) {
        if (!node.isConnected) return;
        const style = getComputedStyle(node);
        if (style.display === "none" || style.visibility === "hidden" ||
            node.getAttribute("aria-hidden") === "true") return;
      }
      if (node.shadowRoot) visitText(node.shadowRoot);
      for (const child of node.childNodes || []) visitText(child);
    };
    visitText(root);
    return chunks.join(" ").replace(/\s+/g, " ").trim();
  };
  const visible = (element) => {
    if (!element?.isConnected || element.getClientRects().length === 0) return false;
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" &&
      element.getAttribute("aria-hidden") !== "true";
  };
  const rowFor = (element) => {
    let current = element;
    for (let depth = 0; depth < 32 && current; depth += 1) {
      const tag = String(current.tagName || "").toUpperCase();
      const role = String(current.getAttribute?.("role") || "").toLowerCase();
      const className = String(current.className || "").toLowerCase();
      if (tag === "YTCP-VIDEO-ROW" || tag.includes("VIDEO-ROW") ||
          (role === "row" && className.includes("video-row"))) return current;
      if (current.parentElement) current = current.parentElement;
      else {
        const root = current.getRootNode?.();
        current = root && root.host ? root.host : null;
      }
    }
    return null;
  };
  const idsFor = (element) => {
    const ids = new Set();
    const values = [
      element.getAttribute?.("href") || "",
      element.getAttribute?.("src") || "",
      element.getAttribute?.("data-video-id") || "",
      element.getAttribute?.("video-id") || "",
      element.getAttribute?.("data-videoid") || "",
      element.getAttribute?.("videoid") || "",
    ];
    for (const value of values) {
      const text = String(value || "");
      const hrefMatch = text.match(/\/video\/([A-Za-z0-9_-]{6,20})\/(?:edit|analytics)(?:[/?#]|$)/i);
      if (hrefMatch) ids.add(hrefMatch[1]);
      // YouTube may serve the same thumbnail through /vi/, /vi_webp/ or
      // another /vi_<format>/ path. The path still carries the canonical
      // video id, so keep the row-to-video mapping semantic instead of
      // depending on a rendered coordinate or a single image format.
      const thumbnailMatch = text.match(/\/vi(?:_[A-Za-z0-9]+)?\/([A-Za-z0-9_-]{6,20})\//i);
      if (thumbnailMatch) ids.add(thumbnailMatch[1]);
      if (/^[A-Za-z0-9_-]{6,20}$/.test(text)) ids.add(text);
    }
    return ids;
  };
  const isControl = (element) => {
    const tag = String(element.tagName || "").toUpperCase();
    return tag === "BUTTON" || tag === "YTCP-BUTTON" ||
      tag === "TP-YT-PAPER-BUTTON" || tag === "TP-YT-PAPER-ITEM" ||
      element.getAttribute?.("role") === "button";
  };
  const controlText = (element) => normalize([
    element.getAttribute?.("aria-label") || "",
    element.getAttribute?.("title") || "",
    element.innerText || "",
    element.textContent || "",
    renderedText(element),
  ].join(" "));
  const continueLabels = [
    "continue uploading",
    "continue upload",
    "resume upload",
    "resume uploading",
    "retry upload",
    "tiếp tục tải lên",
    "tiep tuc tai len",
    "tiếp tục tải tệp lên",
    "tiep tuc tai tep len",
    "tiếp tục tải video lên",
    "tiep tuc tai video len",
    "アップロードを続行",
    "继续上传",
    "업로드 계속",
  ];
  const isContinue = (label) => continueLabels.some((name) => label.includes(name));

  visitRoots(document);
  const elements = [];
  for (const root of roots) {
    for (const element of root.querySelectorAll("*")) elements.push(element);
  }
  const rows = new Map();
  let position = 0;
  for (const element of elements) {
    const row = rowFor(element);
    if (!row) continue;
    let item = rows.get(row);
    if (!item) {
      item = { row, elements: [], ids: new Set(), position: position++ };
      rows.set(row, item);
    }
    item.elements.push(element);
    for (const videoId of idsFor(element)) item.ids.add(videoId);
  }
  const candidates = [];
  for (const item of rows.values()) {
    for (const videoId of item.ids) {
      const titleElement = item.elements.find(
        (element) => element.getAttribute?.("id") === "video-title",
      );
      const title = titleElement
        ? String(titleElement.getAttribute?.("aria-label") ||
            titleElement.getAttribute?.("title") ||
            renderedText(titleElement) || "").trim()
        : "";
      const controls = item.elements.filter(
        (element) => isControl(element) && visible(element) &&
          isContinue(controlText(element)),
      );
      const control = controls.find(
        (element) => String(element.tagName || "").toUpperCase() === "BUTTON",
      ) || controls[0] || null;
      const rowText = renderedText(item.row).slice(0, 2400);
      candidates.push({
        videoId,
        title,
        rowText,
        position: item.position,
        hasContinue: Boolean(control),
        continueLabel: control ? controlText(control).slice(0, 300) : "",
        disabled: Boolean(control && (control.disabled ||
          control.getAttribute("aria-disabled") === "true")),
        row: item.row,
        control,
      });
    }
  }
  candidates.sort((left, right) => left.position - right.position);
  const summarize = (item) => ({
    videoId: item.videoId,
    title: item.title,
    rowText: item.rowText,
    position: item.position,
    hasContinue: item.hasContinue,
    continueLabel: item.continueLabel,
    disabled: item.disabled,
  });
  const expected = String(expectedVideoId || "").trim();
  if (!expected) return { tabId: currentTabId, candidates: candidates.map(summarize) };
  const matches = candidates.filter((item) => item.videoId === expected);
  const base = {
    tabId: currentTabId,
    found: matches.length === 1,
    ambiguous: matches.length > 1,
    matchCount: matches.length,
    interrupted: matches.some((item) => item.hasContinue),
    matches: matches.map(summarize),
  };
  if (matches.length !== 1) return base;
  const selected = matches[0];
  const result = { ...base, ...summarize(selected), clicked: false };
  if (!selected.hasContinue || String(action || "probe").toLowerCase() !== "resume") return result;
  if (selected.disabled) return { ...result, disabled: true };
  selected.control.click();
  return { ...result, clicked: true };
  } catch (error) {
    return {
      probeError: String(error?.message || error),
      stack: String(error?.stack || "").slice(0, 800),
    };
  }
}

// YouTube Studio renders the realtime range picker as a Polymer dropdown.
// Run this semantic action in the target's DOM so a parked/background window
// does not need OS focus or native screen coordinates.
async function checkviewSelect60InTab() {
  const normalize = (value) => String(value || "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLocaleLowerCase();
  const roots = [];
  const seenRoots = new Set();
  const visitRoots = (root) => {
    if (!root || !root.querySelectorAll || seenRoots.has(root)) return;
    seenRoots.add(root);
    roots.push(root);
    for (const element of root.querySelectorAll("*")) {
      if (element.shadowRoot) visitRoots(element.shadowRoot);
    }
  };
  const deepAll = (selector) => {
    visitRoots(document);
    const output = [];
    for (const root of roots) {
      for (const element of root.querySelectorAll(selector)) output.push(element);
    }
    return output;
  };
  const usable = (element) => {
    if (!element || !element.isConnected) return false;
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" &&
      element.getAttribute("aria-hidden") !== "true";
  };
  const textOf = (element) => normalize([
    element?.getAttribute?.("aria-label") || "",
    element?.getAttribute?.("title") || "",
    element?.innerText || "",
    element?.textContent || "",
  ].join(" "));
  const is60 = (value) => {
    const text = normalize(value);
    return /\b60\s*(?:minutes?|mins?|min|phut)\b/.test(text) ||
      /60\s*(?:分|분|分钟|分鐘)/.test(text);
  };
  const is48 = (value) => {
    const text = normalize(value);
    return /\b48\s*(?:hours?|hrs?|hr|gio|h)\b/.test(text) ||
      /48\s*(?:時間|시간|小时|小時)/.test(text);
  };
  const menuSelector =
    "tp-yt-paper-item,tp-yt-paper-radio-button,[role='option']," +
    "[role='menuitem'],[role='menuitemradio']";
  const controls = () => deepAll(
    "ytcp-dropdown-trigger,button,[role='button'],[role='tab']," +
    "[role='radio'],[role='combobox']",
  ).filter(usable).filter((element) => !element.matches(menuSelector));
  const rangeControl = (minutes) => {
    const wanted = minutes === 60 ? is60 : is48;
    return controls()
      .filter((element) => {
        const label = textOf(element);
        return wanted(label) && !(minutes === 60 && is48(label));
      })
      .sort((left, right) => {
        const score = (element) =>
          (element.matches("[role='button'],button") ? 4 : 0) +
          (element.getAttribute("tabindex") !== null ? 1 : 0) +
          (element.tagName !== "YTCP-DROPDOWN-TRIGGER" ? 2 : 0);
        return score(right) - score(left);
      });
  };
  const menuOption = (minutes) => {
    const wanted = minutes === 60 ? is60 : is48;
    return deepAll(menuSelector).filter(usable)
      .filter((element) => {
        const label = textOf(element);
        return wanted(label) && !(minutes === 60 && is48(label));
      })
      .sort((left, right) => textOf(left).length - textOf(right).length);
  };
  const snapshot = () => {
    const selected60 = rangeControl(60)[0] || null;
    const selected48 = rangeControl(48)[0] || null;
    const report = deepAll("*")
      .filter(usable)
      .map(textOf)
      .find((value) => value.includes("hoat dong gan day nhat") &&
        (is60(value) || is48(value))) || "";
    return {
      control60: Boolean(selected60),
      control48: Boolean(selected48),
      controlLabel: textOf(selected60 || selected48),
      reportLabel: report,
    };
  };
  const pause = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
  let state = snapshot();
  if (state.control60) return { ok: true, changed: false, ...state };

  let opened = false;
  for (const trigger of rangeControl(48)) {
    trigger.click();
    await pause(180);
    if (menuOption(60).length) {
      opened = true;
      break;
    }
  }
  if (!opened) {
    return { ok: false, error: "CHECKVIEW_RANGE_MENU_NOT_OPENED", ...snapshot() };
  }
  const option = menuOption(60)[0];
  option.click();
  const deadline = Date.now() + 6000;
  do {
    await pause(250);
    state = snapshot();
    if (state.control60) return { ok: true, changed: true, ...state };
  } while (Date.now() < deadline);
  return { ok: false, error: "CHECKVIEW_60_RANGE_NOT_CONFIRMED", ...state };
}

// Read the complete Content list without using the native CDP/Accessibility
// snapshot.  YouTube Studio virtualizes rows and frequently keeps only the
// first/sample row in the accessibility tree while the list is still loading.
// This reader runs in the paired extension context, walks every open shadow
// root, sweeps the list's own scroll container, and advances the semantic Next
// button only after two stable DOM observations.  It never uses OS focus or
// screen coordinates, so the checkview window can remain minimized.
async function checkviewContentPageInTab(action = "snapshot", beforeSignature = "") {
  // One deadline for the entire injected command. A Next call reads the old
  // page, clicks, then reads the new page; separate 24-second deadlines for
  // those phases could outlive the bridge RPC and lose the click's reply.
  const deadline = Date.now() + 30000;
  const pause = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
  const normalize = (value) => String(value || "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLocaleLowerCase();
  const isUsable = (element) => {
    if (!element?.isConnected) return false;
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" &&
      element.getAttribute("aria-hidden") !== "true";
  };
  const rootsForDocument = () => {
    const roots = [];
    const seen = new Set();
    const visit = (root) => {
      if (!root || !root.querySelectorAll || seen.has(root)) return;
      seen.add(root);
      roots.push(root);
      for (const element of root.querySelectorAll("*")) {
        if (element.shadowRoot) visit(element.shadowRoot);
      }
    };
    visit(document);
    return roots;
  };
  const deepAll = (selector) => {
    const output = [];
    for (const root of rootsForDocument()) {
      for (const element of root.querySelectorAll(selector)) {
        if (!output.includes(element)) output.push(element);
      }
    }
    return output;
  };
  const renderedText = (root) => {
    const chunks = [];
    const visit = (node) => {
      if (!node) return;
      if (node.nodeType === Node.TEXT_NODE) {
        const value = String(node.nodeValue || "").trim();
        if (value) chunks.push(value);
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE &&
          node.nodeType !== Node.DOCUMENT_FRAGMENT_NODE) return;
      if (node.nodeType === Node.ELEMENT_NODE) {
        if (!node.isConnected) return;
        const style = getComputedStyle(node);
        if (style.display === "none" || style.visibility === "hidden" ||
            node.getAttribute("aria-hidden") === "true") return;
      }
      if (node.shadowRoot) visit(node.shadowRoot);
      for (const child of node.childNodes || []) visit(child);
    };
    visit(root);
    return chunks.join(" ").replace(/\s+/g, " ").trim();
  };
  const rowFor = (element) => {
    let current = element;
    for (let depth = 0; depth < 40 && current; depth += 1) {
      const tag = String(current.tagName || "").toUpperCase();
      const role = String(current.getAttribute?.("role") || "").toLowerCase();
      const className = String(current.className || "").toLowerCase();
      if (tag === "YTCP-VIDEO-ROW" || tag.includes("VIDEO-ROW") ||
          (role === "row" && className.includes("video-row"))) return current;
      if (current.parentElement) current = current.parentElement;
      else {
        const root = current.getRootNode?.();
        current = root && root.host ? root.host : null;
      }
    }
    return null;
  };
  const addMatches = (ids, value) => {
    const text = String(value || "");
    const patterns = [
      /\/video\/([A-Za-z0-9_-]{6,20})(?=[/?#]|$)/gi,
      /\/vi(?:_[A-Za-z0-9]+)?\/([A-Za-z0-9_-]{6,20})(?=[/?#]|$)/gi,
      /[?&]v=([A-Za-z0-9_-]{6,20})(?=[&#]|$)/gi,
      /\/shorts\/([A-Za-z0-9_-]{6,20})(?=[/?#]|$)/gi,
    ];
    for (const pattern of patterns) {
      let match;
      while ((match = pattern.exec(text)) !== null) ids.add(match[1]);
    }
  };
  const idsFor = (element) => {
    const ids = new Set();
    if (!element) return ids;
    const values = [
      element.getAttribute?.("href") || "",
      element.href || "",
      element.getAttribute?.("src") || "",
      element.src || "",
      element.getAttribute?.("srcset") || "",
      element.getAttribute?.("data-src") || "",
      element.getAttribute?.("data-video-id") || "",
      element.getAttribute?.("video-id") || "",
      element.getAttribute?.("data-videoid") || "",
      element.getAttribute?.("videoid") || "",
    ];
    for (const value of values) {
      const text = String(value || "");
      addMatches(ids, text);
      if (/^[A-Za-z0-9_-]{6,20}$/.test(text)) ids.add(text);
    }
    try { addMatches(ids, element.outerHTML || ""); } catch { /* stale node */ }
    return ids;
  };
  const titleFor = (item) => {
    const titleNodes = item.elements.filter(
      (element) => String(element.getAttribute?.("id") || "") === "video-title",
    );
    const candidates = [
      ...titleNodes,
      ...item.elements.filter((element) => {
        const tag = String(element.tagName || "").toUpperCase();
        return tag === "A" && String(element.getAttribute?.("href") || "")
          .toLowerCase().includes("/video/");
      }),
    ];
    for (const element of candidates) {
      const value = String(
        element.getAttribute?.("aria-label") ||
        element.getAttribute?.("title") ||
        renderedText(element) ||
        element.textContent || "",
      ).replace(/\s+/g, " ").trim();
      if (value) return value.slice(0, 500);
    }
    return "";
  };
  const collectRows = () => {
    const rowMap = new Map();
    const elements = deepAll("*");
    let position = 0;
    for (const element of elements) {
      const row = rowFor(element);
      if (!row || !isUsable(row)) continue;
      let item = rowMap.get(row);
      if (!item) {
        item = { row, elements: [row], ids: new Set(), position: position++ };
        rowMap.set(row, item);
      }
      if (!item.elements.includes(element)) item.elements.push(element);
      for (const videoId of idsFor(element)) item.ids.add(videoId);
    }
    const output = [];
    for (const item of rowMap.values()) {
      // The row outerHTML catches lazy thumbnail attributes that are not yet
      // exposed on the image element itself.
      for (const videoId of idsFor(item.row)) item.ids.add(videoId);
      for (const videoId of item.ids) {
        output.push({
          videoId,
          title: titleFor(item),
          rowText: renderedText(item.row).slice(0, 2400),
          position: item.position,
          row: item.row,
        });
      }
    }
    // A few Studio builds omit the row host from the composed tree.  The
    // anchor fallback still groups each direct video link and is deduplicated
    // below; it is only used when no proper row was discovered.
    if (!output.length) {
      for (const anchor of deepAll("a[href]")) {
        if (!isUsable(anchor)) continue;
        const ids = idsFor(anchor);
        for (const videoId of ids) {
          output.push({
            videoId,
            title: String(anchor.getAttribute("aria-label") ||
              anchor.getAttribute("title") || renderedText(anchor) || "")
              .replace(/\s+/g, " ").trim().slice(0, 500),
            rowText: renderedText(anchor).slice(0, 2400),
            position: position++,
            row: anchor,
          });
        }
      }
    }
    const unique = new Map();
    for (const item of output) {
      const prior = unique.get(item.videoId);
      if (!prior || String(item.title || "").length > String(prior.title || "").length) {
        unique.set(item.videoId, item);
      }
    }
    return {
      items: [...unique.values()].sort((left, right) => left.position - right.position),
      firstRow: output[0]?.row || null,
    };
  };
  const nextMarkers = [
    "next page", "go to next page", "trang tiep theo", "trang ke tiep",
    "chuyen den trang tiep theo", "次のページ", "次へ", "다음 페이지", "下一页", "下一頁",
  ];
  const controlText = (element) => normalize([
    element.getAttribute?.("aria-label") || "",
    element.getAttribute?.("title") || "",
    element.innerText || "",
    element.textContent || "",
  ].join(" "));
  const nextControl = () => {
    const controls = deepAll(
      "button,[role='button'],ytcp-button,tp-yt-paper-button," +
      "tp-yt-paper-icon-button,[aria-label],a",
    ).filter(isUsable);
    const matches = controls.filter((element) => {
      const label = controlText(element);
      return nextMarkers.some((marker) => label.includes(marker));
    });
    matches.sort((left, right) => {
      const score = (element) => {
        const label = controlText(element);
        return (label.includes("go to next page") ? 5 : 0) +
          (String(element.tagName || "").toUpperCase() === "BUTTON" ? 3 : 0) +
          (element.getClientRects().length ? 1 : 0);
      };
      return score(right) - score(left);
    });
    const element = matches[0] || null;
    if (!element) return { found: false, enabled: false, label: "", element: null };
    const disabled = Boolean(element.disabled) ||
      element.getAttribute("aria-disabled") === "true" ||
      element.hasAttribute("disabled") ||
      String(element.getAttribute("class") || "").toLowerCase().includes("disabled");
    return {
      found: true,
      enabled: !disabled,
      label: controlText(element).slice(0, 300),
      element,
    };
  };
  const scrollableFor = (row) => {
    const candidates = [];
    let current = row;
    for (let depth = 0; depth < 60 && current; depth += 1) {
      if (current.nodeType === Node.ELEMENT_NODE) {
        const style = getComputedStyle(current);
        if (current.scrollHeight > current.clientHeight + 16 &&
            current.clientHeight > 0 && style.overflowY !== "hidden") {
          candidates.push(current);
        }
      }
      if (current.parentElement) current = current.parentElement;
      else {
        const root = current.getRootNode?.();
        current = root && root.host ? root.host : null;
      }
    }
    const documentScroller = document.scrollingElement;
    if (documentScroller) candidates.push(documentScroller);
    return candidates.sort(
      (left, right) => (right.scrollHeight - right.clientHeight) -
        (left.scrollHeight - left.clientHeight),
    )[0] || documentScroller || null;
  };
  const scrollTopOf = (scroller) => Number(scroller?.scrollTop || 0);
  const scrollMaxOf = (scroller) => Math.max(
    0,
    Number(scroller?.scrollHeight || 0) - Number(scroller?.clientHeight || 0),
  );
  const summary = (raw) => {
    const items = raw.items.map((item) => ({
      videoId: item.videoId,
      title: item.title,
      rowText: item.rowText,
      position: item.position,
    }));
    return {
      rows: items,
      videoCount: items.length,
      pageSignature: items.map((item) => item.videoId).join("\u001f"),
    };
  };
  const readStable = async () => {
    let previous = "";
    let stable = 0;
    let latest = { items: [], firstRow: null };
    let firstDataAt = 0;
    while (Date.now() < deadline) {
      latest = collectRows();
      const signature = latest.items.map((item) => item.videoId).join("\u001f");
      if (signature) {
        if (!firstDataAt) firstDataAt = Date.now();
        stable = signature === previous ? stable + 1 : 1;
        if (stable >= 2 && Date.now() - firstDataAt >= 900) return latest;
      } else {
        stable = 0;
      }
      previous = signature;
      await pause(220);
    }
    throw new Error("CHECKVIEW_CONTENT_NOT_READY");
  };
  const readPage = async () => {
    const merged = new Map();
    let stable = await readStable();
    let scroller = scrollableFor(stable.firstRow);
    let lastTop = -1;
    let stagnant = 0;
    while (Date.now() < deadline) {
      stable = collectRows();
      for (const item of stable.items) {
        const prior = merged.get(item.videoId);
        if (!prior || String(item.title || "").length > String(prior.title || "").length) {
          merged.set(item.videoId, item);
        }
      }
      if (!scroller) scroller = scrollableFor(stable.firstRow);
      const top = scrollTopOf(scroller);
      const maximum = scrollMaxOf(scroller);
      if (!scroller || maximum <= 16 || top >= maximum - 2) {
        if (top === lastTop) stagnant += 1; else stagnant = 0;
        lastTop = top;
        if (stagnant >= 2 || !scroller || maximum <= 16) break;
        await pause(220);
        continue;
      }
      const step = Math.max(240, Number(scroller.clientHeight || 600) * 0.8);
      const nextTop = Math.min(maximum, top + step);
      scroller.scrollTop = nextTop;
      if (nextTop === top) stagnant += 1; else stagnant = 0;
      if (stagnant >= 3) break;
      await pause(220);
    }
    const finalRows = [...merged.values()].sort((left, right) => left.position - right.position);
    if (!finalRows.length) throw new Error("CHECKVIEW_CONTENT_NOT_READY");
    const result = summary({ items: finalRows });
    const next = nextControl();
    return {
      ...result,
      nextFound: next.found,
      nextEnabled: next.enabled,
      nextLabel: next.label,
      scrolled: Boolean(scroller && scrollMaxOf(scroller) > 16),
    };
  };
  if (String(action || "snapshot").toLowerCase() === "next") {
    const current = await readPage();
    const expected = String(beforeSignature || current.pageSignature);
    if (!current.nextFound || !current.nextEnabled) return { ...current, complete: true };
    if (expected && current.pageSignature !== expected) {
      throw new Error("CHECKVIEW_PAGE_CHANGED_BEFORE_NEXT");
    }
    const control = nextControl().element;
    if (!control) throw new Error("CHECKVIEW_NEXT_PAGE_NOT_FOUND");
    control.click();
    let candidateSignature = "";
    let stable = 0;
    while (Date.now() < deadline) {
      const candidate = collectRows();
      const signature = candidate.items.map((item) => item.videoId).join("\u001f");
      if (signature && signature !== expected) {
        stable = signature === candidateSignature ? stable + 1 : 1;
        candidateSignature = signature;
        if (stable >= 2) {
          const scroller = scrollableFor(candidate.firstRow);
          if (scroller) scroller.scrollTop = 0;
          const page = await readPage();
          return { ...page, transitioned: true, complete: true };
        }
      } else {
        stable = 0;
        candidateSignature = "";
      }
      await pause(220);
    }
    throw new Error("CHECKVIEW_NEXT_PAGE_NOT_CONFIRMED");
  }
  return { ...(await readPage()), complete: true };
}

async function execute(command, params) {
  if (command.startsWith('google_')) return await GoogleWorkspace.execute(command, params);
  if (command === "ping") {
    return {
      ok: true,
      extensionVersion: EXTENSION_VERSION,
      capabilities: [
        "google-workspace-v1",
        "google-workspace-v2",
        "background-tab",
        "page-state",
        "youtube-set-text",
        "youtube-native-edit-v2",
        "youtube-tags-v1",
        "youtube-tags-commit-v2",
        "youtube-click",
        "youtube-query",
        "youtube-query-active-v2",
        "youtube-active-controls-v2",
      "youtube-upload-completion-v3",
      "youtube-attached-video",
      "youtube-resume-interrupted-upload-v1",
        "youtube-content-state-v1",
        "youtube-checkview-range-v1",
        "youtube-checkview-content-v1",
        "bridge-keepalive",
      ],
    };
  }
  if (command === "tabs_list") {
    const tabs = await chrome.tabs.query({});
    return tabs.map((tab) => ({
      id: tab.id,
      active: Boolean(tab.active),
      title: tab.title || "",
      url: tab.url || "",
      windowId: tab.windowId,
    }));
  }
  if (command === "navigate") {
    const current = await activeTab(params);
    const explicitlyActive = params.active === true;
    const target = params.new_tab
      ? await chrome.tabs.create({
          url: String(params.url || ""),
          active: explicitlyActive,
          windowId: Number.isInteger(Number(params.window_id))
            ? Number(params.window_id)
            : current.windowId,
        })
      : await chrome.tabs.update(current.id, {
          url: String(params.url || ""),
          ...(params.active === true ? { active: true } : {}),
        });
    controlledTabId = target.id || null;
    return { tabId: target.id, url: target.url || params.url || "" };
  }
  if (command === "select_tab") {
    const tab = await activeTab(params);
    controlledTabId = tab.id || null;
    return {
      tabId: tab.id,
      windowId: tab.windowId,
      active: Boolean(tab.active),
      url: tab.url || "",
    };
  }
  if (command === "page_state") {
    const tab = await activeTab(params);
    return await executeInTab(
      tab.id,
      () => ({
        url: location.href,
        title: document.title,
        readyState: document.readyState,
        hasFocus: document.hasFocus(),
        hidden: document.hidden,
        body: Boolean(document.body),
      }),
      [],
      "MAIN",
    );
  }
  if (command === "close_tab") {
    const tab = await activeTab(params);
    await chrome.tabs.remove(tab.id);
    if (controlledTabId === tab.id) controlledTabId = null;
    return { closed: true, tabId: tab.id };
  }
  if (command === "script_eval") {
    const tab = await activeTab(params);
    const world = String(params.world || "ISOLATED").toUpperCase();
    if (world !== "MAIN" && world !== "ISOLATED") {
      throw new Error("SCRIPT_WORLD_UNSUPPORTED");
    }
    return await executeInTab(
      tab.id,
      async (source) => await Promise.resolve(globalThis.eval(source)),
      [String(params.expression || "")],
      world,
    );
  }
  if (command === "youtube_content_state") {
    const tab = await activeTab(params);
    return await executeInTab(
      tab.id,
      contentStateInTab,
      [
        String(params.video_id || params.videoId || ""),
        String(params.action || "probe"),
        tab.id,
      ],
      "ISOLATED",
    );
  }
  if (command === "youtube_checkview_select_60") {
    const tab = await activeTab(params);
    return await executeInTab(
      tab.id,
      checkviewSelect60InTab,
      [],
      "ISOLATED",
    );
  }
  if (command === "youtube_checkview_content_page") {
    const tab = await activeTab(params);
    return await executeInTab(
      tab.id,
      checkviewContentPageInTab,
      [
        String(params.action || "snapshot"),
        String(params.before_signature || params.beforeSignature || ""),
      ],
      "ISOLATED",
    );
  }
  if (command === "insert_text") {
    const tab = await activeTab(params);
    return await executeInTab(
      tab.id,
      (text) => {
        let target = document.activeElement;
        while (target?.shadowRoot?.activeElement) target = target.shadowRoot.activeElement;
        if (!target) throw new Error("Trang chưa focus đúng ô nhập.");
        if (target.isContentEditable) {
          target.focus();
          document.execCommand("selectAll", false, null);
          document.execCommand("insertText", false, text);
        } else if ("value" in target) {
          const setter = Object.getOwnPropertyDescriptor(
            Object.getPrototypeOf(target),
            "value",
          )?.set;
          if (setter) setter.call(target, text);
          else target.value = text;
          target.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
          target.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
        } else {
          throw new Error("Phần tử đang focus không nhận văn bản.");
        }
        return true;
      },
      [String(params.text || "")],
      "MAIN",
    );
  }
  if (command === "set_file_url") {
    const tab = await activeTab(params);
    return await executeInTab(
      tab.id,
      async (fileUrl, fileName, mimeType, acceptHint) => {
        const deepAll = (selector) => {
          const output = [];
          const visit = (root) => {
            for (const element of root.querySelectorAll(selector)) output.push(element);
            for (const element of root.querySelectorAll("*")) {
              if (element.shadowRoot) visit(element.shadowRoot);
            }
          };
          visit(document);
          return output;
        };
        const inputs = deepAll('input[type="file"]');
        const hint = String(acceptHint || "").toLowerCase();
        const input = inputs.find((el) => String(el.accept || "").toLowerCase().includes(hint)) || inputs[0];
        if (!input) throw new Error(`Không tìm thấy ô chọn file ${hint || "media"}.`);
        const response = await fetch(fileUrl, { cache: "no-store" });
        if (!response.ok) throw new Error(`Không đọc được file local: HTTP ${response.status}.`);
        const blob = await response.blob();
        const file = new File([blob], fileName, {
          type: mimeType || blob.type || "application/octet-stream",
          lastModified: Date.now(),
        });
        const transfer = new DataTransfer();
        transfer.items.add(file);
        input.files = transfer.files;
        input.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
        input.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
        return { name: file.name, size: file.size, type: file.type, count: input.files.length };
      },
      [
        String(params.url || ""),
        String(params.name || "upload.bin"),
        String(params.mime || "application/octet-stream"),
        String(params.accept_hint || ""),
      ],
      "ISOLATED",
    );
  }
  if (command === "youtube_set_text") {
    const tab = await activeTab(params);
    const pageResult = await executeInTab(
      tab.id,
      (field, text, currentTabId) => {
        const deepAll = (selector) => {
          const output = [];
          const visit = (root) => {
            for (const element of root.querySelectorAll(selector)) output.push(element);
            for (const element of root.querySelectorAll("*")) {
              if (element.shadowRoot) visit(element.shadowRoot);
            }
          };
          visit(document);
          return output;
        };
        const selectors = {
          title: ["#title-textarea #textbox", "ytcp-social-suggestions-textbox#title-textarea #textbox"],
          description: ["#description-textarea #textbox", "ytcp-social-suggestions-textbox#description-textarea #textbox"],
          tags: ["#tags-container input"],
        }[String(field || "")] || [];
        let target = null;
        for (const selector of selectors) {
          const matches = deepAll(selector);
          // Studio keeps hidden template editors in the DOM while replacing
          // the live upload form. Never write to a stale/hidden textbox just
          // because it appears later in document order.
          const visible = matches.filter((element) => {
            if (!element.isConnected || element.getClientRects().length === 0) return false;
            const style = getComputedStyle(element);
            return style.display !== "none" && style.visibility !== "hidden";
          });
          target = visible[visible.length - 1] || target;
          if (target) break;
        }
        if (!target) throw new Error(`YOUTUBE_FIELD_NOT_FOUND:${field}`);
        const value = String(text || "");
        if (target.isContentEditable) {
          // Do not assign textContent here. YouTube's Polymer editor can show
          // that DOM mutation while its internal value remains unchanged, so
          // the final dialog looks filled but saves an empty draft. Drive the
          // focused editor through the browser editing command instead; this
          // follows the same input path as typing and produces the beforeinput /
          // input sequence that Studio observes.
          target.focus({ preventScroll: true });
          const selection = target.ownerDocument.getSelection();
          const range = target.ownerDocument.createRange();
          range.selectNodeContents(target);
          selection.removeAllRanges();
          selection.addRange(range);
          const inserted = document.execCommand("insertText", false, value);
          selection.removeAllRanges();
          if (!inserted || String(target.textContent || "").replace(/\r\n/g, "\n") !== value.replace(/\r\n/g, "\n")) {
            throw new Error(`YOUTUBE_FIELD_NATIVE_EDIT_FAILED:${field}`);
          }
        } else if ("value" in target) {
          target.focus({ preventScroll: true });
          const setter = Object.getOwnPropertyDescriptor(
            Object.getPrototypeOf(target),
            "value",
          )?.set;
          if (setter) setter.call(target, value);
          else target.value = value;
        } else {
          throw new Error(`YOUTUBE_FIELD_NOT_EDITABLE:${field}`);
        }
        // execCommand already emits the browser's editing event for
        // contenteditable. Plain inputs still need their framework events.
        if (!target.isContentEditable) {
          const inputEvent = typeof InputEvent === "function"
            ? new InputEvent("input", {
                bubbles: true,
                composed: true,
                inputType: "insertText",
                data: value,
              })
            : new Event("input", { bubbles: true, composed: true });
          target.dispatchEvent(inputEvent);
          target.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
        }
        return {
          field,
          value: target.isContentEditable ? target.textContent : target.value,
          laidOut: target.getClientRects().length > 0,
          tabId: currentTabId,
        };
      },
      [String(params.field || ""), String(params.text || ""), tab.id],
      "ISOLATED",
    );
    return { bridge: true, pageResult: pageResult ?? null, tabId: tab.id };
  }
  if (command === "youtube_set_tags") {
    const tab = await activeTab(params);
    return await executeInTab(
      tab.id,
      async (requestedTags) => {
        const deepAll = (selector) => {
          const output = [];
          const visit = (root) => {
            for (const element of root.querySelectorAll(selector)) output.push(element);
            for (const element of root.querySelectorAll("*")) {
              if (element.shadowRoot) visit(element.shadowRoot);
            }
          };
          visit(document);
          return output;
        };
        const visible = (element) => {
          if (!element?.isConnected || element.getClientRects().length === 0) return false;
          const style = getComputedStyle(element);
          return style.display !== "none" && style.visibility !== "hidden";
        };
        const inputs = deepAll("#tags-container #text-input, #tags-container input")
          .filter(visible);
        const input = inputs.at(-1) || null;
        if (!input) throw new Error("YOUTUBE_TAG_INPUT_NOT_FOUND");
        const container = deepAll("#tags-container").filter(visible).at(-1) || null;
        const tags = (Array.isArray(requestedTags) ? requestedTags : [])
          .map((value) => String(value || "").trim())
          .filter(Boolean);
        const setter = Object.getOwnPropertyDescriptor(
          Object.getPrototypeOf(input),
          "value",
        )?.set;
        const setValue = (value) => {
          if (setter) setter.call(input, value);
          else input.value = value;
          const event = typeof InputEvent === "function"
            ? new InputEvent("input", {
                bubbles: true,
                composed: true,
                inputType: "insertText",
                data: value,
              })
            : new Event("input", { bubbles: true, composed: true });
          input.dispatchEvent(event);
        };
        const key = (type) => {
          const event = new KeyboardEvent(type, {
            key: "Enter",
            code: "Enter",
            bubbles: true,
            composed: true,
            cancelable: true,
          });
          for (const property of ["keyCode", "which", "charCode"]) {
            try {
              Object.defineProperty(event, property, {
                configurable: true,
                get: () => (property === "charCode" && type !== "keypress" ? 0 : 13),
              });
            } catch (_error) {}
          }
          input.dispatchEvent(event);
        };
        for (const tag of tags) {
          input.focus({ preventScroll: true });
          setValue(tag);
          key("keydown");
          key("keypress");
          key("keyup");
          await new Promise((resolve) => setTimeout(resolve, 120));
        }
        const committed = container
          ? Array.from(container.querySelectorAll("ytcp-chip"))
              .filter(visible)
              .map((element) => String(element.innerText || element.textContent || "").trim())
              .filter(Boolean)
          : [];
        return {
          found: true,
          tags: committed,
          value: String(input.value || ""),
          laidOut: visible(input),
        };
      },
      [Array.isArray(params.tags) ? params.tags : []],
      "ISOLATED",
    );
  }
  if (command === "youtube_get_text") {
    const tab = await activeTab(params);
    return await executeInTab(
      tab.id,
      (field) => {
        const deepAll = (selector) => {
          const output = [];
          const visit = (root) => {
            for (const element of root.querySelectorAll(selector)) output.push(element);
            for (const element of root.querySelectorAll("*")) {
              if (element.shadowRoot) visit(element.shadowRoot);
            }
          };
          visit(document);
          return output;
        };
        const selectors = {
          title: ["#title-textarea #textbox", "ytcp-social-suggestions-textbox#title-textarea #textbox"],
          description: ["#description-textarea #textbox", "ytcp-social-suggestions-textbox#description-textarea #textbox"],
          tags: ["#tags-container input"],
        }[String(field || "")] || [];
        let target = null;
        for (const selector of selectors) {
          const matches = deepAll(selector);
          const visible = matches.filter((element) => {
            if (!element.isConnected || element.getClientRects().length === 0) return false;
            const style = getComputedStyle(element);
            return style.display !== "none" && style.visibility !== "hidden";
          });
          target = visible[visible.length - 1] || target;
          if (target) break;
        }
        if (!target) return { found: false, field };
        if (String(field || "") === "tags") {
          const containers = deepAll("#tags-container").filter((element) => {
            if (!element.isConnected || element.getClientRects().length === 0) return false;
            const style = getComputedStyle(element);
            return style.display !== "none" && style.visibility !== "hidden";
          });
          const container = containers[containers.length - 1] || null;
          const tags = container
            ? Array.from(container.querySelectorAll("ytcp-chip"))
                .filter((element) => element.isConnected && element.getClientRects().length > 0)
                .map((element) => String(element.innerText || element.textContent || "").trim())
                .filter(Boolean)
            : [];
          return {
            found: true,
            field,
            value: tags.length ? tags.join(", ") : String(target.value || ""),
            tags,
            laidOut: target.getClientRects().length > 0,
          };
        }
        return {
          found: true,
          field,
          value: target.isContentEditable ? target.textContent : target.value,
          laidOut: target.getClientRects().length > 0,
        };
      },
      [String(params.field || "")],
      "ISOLATED",
    );
  }
  if (command === "youtube_query") {
    const tab = await activeTab(params);
    return await executeInTab(
      tab.id,
      (selector) => {
        // Upload controls in YouTube Studio are Polymer elements whose live
        // progress text lives inside one or more nested shadow roots. The
        // shadow host's innerText/textContent is often empty, so reading only
        // the host makes a completed transfer look stuck at the last local
        // checkpoint. Walk rendered light/shadow DOM text explicitly.
        const renderedText = (root) => {
          const chunks = [];
          const visitText = (node) => {
            if (!node) return;
            if (node.nodeType === Node.TEXT_NODE) {
              const value = String(node.nodeValue || "").trim();
              if (value) chunks.push(value);
              return;
            }
            if (node.nodeType !== Node.ELEMENT_NODE && node.nodeType !== Node.DOCUMENT_FRAGMENT_NODE) {
              return;
            }
            if (node.nodeType === Node.ELEMENT_NODE) {
              if (!node.isConnected) return;
              const style = getComputedStyle(node);
              if (style.display === "none" || style.visibility === "hidden") return;
              if (node.getAttribute("aria-hidden") === "true") return;
            }
            if (node.shadowRoot) visitText(node.shadowRoot);
            for (const child of node.childNodes || []) visitText(child);
          };
          visitText(root);
          return chunks.join(" ").replace(/\s+/g, " ").trim();
        };
        const output = [];
        const visit = (root) => {
          for (const element of root.querySelectorAll(selector)) output.push(element);
          for (const element of root.querySelectorAll("*")) {
            if (element.shadowRoot) visit(element.shadowRoot);
          }
        };
        visit(document);
        // Studio commonly keeps hidden template/stale copies of one dialog.
        // Prefer the live laid-out copy so progress/completion is read from
        // the current upload, not from an invisible older element.
        const laidOut = output.filter((element) => {
          if (!element?.isConnected || element.getClientRects().length === 0) return false;
          const style = getComputedStyle(element);
          return style.display !== "none" && style.visibility !== "hidden";
        });
        const target = laidOut[laidOut.length - 1] || output[output.length - 1] || null;
        if (!target) return { found: false, count: 0 };
        return {
          found: true,
          count: output.length,
          tag: target.tagName || "",
          id: target.id || "",
          name: target.getAttribute("name") || "",
          ariaLabel: target.getAttribute("aria-label") || "",
          href: "href" in target ? String(target.href || "") : String(target.getAttribute("href") || ""),
          src: "src" in target ? String(target.src || "") : String(target.getAttribute("src") || ""),
          value: "value" in target ? String(target.value || "") : "",
          text: renderedText(target),
          checked: Boolean(target.checked),
          ariaChecked: target.getAttribute("aria-checked") || "",
          disabled: Boolean(target.disabled),
          ariaDisabled: target.getAttribute("aria-disabled") || "",
          laidOut: target.getClientRects().length > 0,
        };
      },
      [String(params.selector || "")],
      "ISOLATED",
    );
  }
  if (command === "youtube_attached_video") {
    const tab = await activeTab(params);
    return await executeInTab(
      tab.id,
      () => {
        const output = [];
        const visit = (root) => {
          for (const element of root.querySelectorAll('input[type="file"]')) output.push(element);
          for (const element of root.querySelectorAll("*")) {
            if (element.shadowRoot) visit(element.shadowRoot);
          }
        };
        visit(document);
        for (const input of output) {
          const accept = String(input.accept || "").toLowerCase();
          const isVideo = accept.includes("video") || input.name === "Filedata";
          if (!isVideo || !input.files || input.files.length !== 1) continue;
          const file = input.files[0];
          return {
            name: String(file.name || ""),
            size: Number(file.size || 0),
            type: String(file.type || ""),
            count: input.files.length,
          };
        }
        return {};
      },
      [],
      "ISOLATED",
    );
  }
  if (command === "youtube_resume_interrupted_upload") {
    const tab = await activeTab(params);
    return await executeInTab(
      tab.id,
      (expectedFileName, action, currentTabId) => {
        const normalize = (value) => String(value || "")
          .normalize("NFKC")
          .replace(/\s+/g, " ")
          .trim()
          .toLocaleLowerCase();
        const expected = normalize(expectedFileName);
        const roots = [];
        const visitRoots = (root) => {
          if (!root || !root.querySelectorAll || roots.includes(root)) return;
          roots.push(root);
          for (const element of root.querySelectorAll("*")) {
            if (element.shadowRoot) visitRoots(element.shadowRoot);
          }
        };
        const renderedText = (root) => {
          const chunks = [];
          const visitText = (node) => {
            if (!node) return;
            if (node.nodeType === Node.TEXT_NODE) {
              const value = String(node.nodeValue || "").trim();
              if (value) chunks.push(value);
              return;
            }
            if (node.nodeType !== Node.ELEMENT_NODE &&
                node.nodeType !== Node.DOCUMENT_FRAGMENT_NODE) return;
            if (node.nodeType === Node.ELEMENT_NODE) {
              if (!node.isConnected) return;
              const style = getComputedStyle(node);
              if (style.display === "none" || style.visibility === "hidden") return;
              if (node.getAttribute("aria-hidden") === "true") return;
            }
            if (node.shadowRoot) visitText(node.shadowRoot);
            for (const child of node.childNodes || []) visitText(child);
          };
          visitText(root);
          return chunks.join(" ").replace(/\s+/g, " ").trim();
        };
        const visible = (element) => {
          if (!element?.isConnected || element.getClientRects().length === 0) return false;
          const style = getComputedStyle(element);
          return style.display !== "none" && style.visibility !== "hidden" &&
            element.getAttribute("aria-hidden") !== "true";
        };
        const controlText = (element) => normalize([
          element.getAttribute?.("aria-label") || "",
          element.getAttribute?.("title") || "",
          element.innerText || "",
          element.textContent || "",
          renderedText(element),
        ].join(" "));
        const continueLabels = [
          "continue uploading",
          "continue upload",
          "resume upload",
          "resume uploading",
          "retry upload",
          "tiếp tục tải lên",
          "tiep tuc tai len",
          "tiếp tục tải tệp lên",
          "tiep tuc tai tep len",
          "tiếp tục tải video lên",
          "tiep tuc tai video len",
          "アップロードを続行",
          "继续上传",
          "업로드 계속",
        ];
        const isContinueControl = (label) => continueLabels.some((name) => label.includes(name));
        const controls = [];
        visitRoots(document);
        for (const root of roots) {
          for (const element of root.querySelectorAll(
            "button,[role='button'],ytcp-button,tp-yt-paper-button,tp-yt-paper-item",
          )) {
            if (!controls.includes(element)) controls.push(element);
          }
        }
        const matches = [];
        let continueControlCount = 0;
        for (const control of controls) {
          if (!visible(control)) continue;
          const label = controlText(control);
          if (!isContinueControl(label)) continue;
          continueControlCount += 1;
          let row = control;
          let rowText = "";
          for (let depth = 0; depth < 28 && row; depth += 1) {
            const text = normalize(renderedText(row));
            if (expected && text.includes(expected)) {
              rowText = text;
              break;
            }
            if (row.parentElement) {
              row = row.parentElement;
            } else {
              const root = row.getRootNode?.();
              row = root && root.host ? root.host : null;
            }
          }
          if (!rowText) continue;
          matches.push({ control, row, rowText, label });
        }
        // A Polymer button can expose both a host and an inner native button
        // through the deep-root walk. Treat those as one row; two separate
        // rows with the same filename remain ambiguous and are rejected.
        const uniqueMatches = [];
        for (const candidate of matches) {
          if (uniqueMatches.some((item) => item.row === candidate.row)) continue;
          uniqueMatches.push(candidate);
        }
        const summarized = uniqueMatches.map(({ rowText, label }) => ({
          rowText: rowText.slice(0, 2400),
          label: label.slice(0, 300),
        }));
        if (uniqueMatches.length !== 1) {
          return {
            interrupted: continueControlCount > 0,
            found: false,
            ambiguous: uniqueMatches.length > 1,
            matchCount: uniqueMatches.length,
            matches: summarized,
            tabId: currentTabId,
          };
        }
        const selected = uniqueMatches[0];
        const control = selected.control;
        const disabled = Boolean(control.disabled) ||
          control.getAttribute("aria-disabled") === "true";
        if (disabled) {
          return {
            interrupted: true,
            found: true,
            clicked: false,
            disabled: true,
            matchCount: 1,
            rowText: selected.rowText.slice(0, 2400),
            tabId: currentTabId,
          };
        }
        if (String(action || "probe").toLocaleLowerCase() === "resume") {
          control.click();
          return {
            interrupted: true,
            found: true,
            clicked: true,
            matchCount: 1,
            rowText: selected.rowText.slice(0, 2400),
            tabId: currentTabId,
          };
        }
        return {
          interrupted: true,
          found: true,
          clicked: false,
          matchCount: 1,
          rowText: selected.rowText.slice(0, 2400),
          tabId: currentTabId,
        };
      },
      [
        String(params.file_name || params.fileName || ""),
        String(params.action || "probe"),
        tab.id,
      ],
      "ISOLATED",
    );
  }
  if (command === "youtube_click") {
    const tab = await activeTab(params);
    const pageResult = await executeInTab(
      tab.id,
      (selector, names, currentTabId) => {
        const deepAll = (query) => {
          const output = [];
          const visit = (root) => {
            for (const element of root.querySelectorAll(query)) output.push(element);
            for (const element of root.querySelectorAll("*")) {
              if (element.shadowRoot) visit(element.shadowRoot);
            }
          };
          visit(document);
          return output;
        };
        const visible = (element) => {
          if (!element?.isConnected || element.getClientRects().length === 0) return false;
          const style = getComputedStyle(element);
          return style.display !== "none" && style.visibility !== "hidden";
        };
        const selectorMatches = selector ? deepAll(selector) : [];
        let target = selectorMatches.filter(visible).at(-1) || selectorMatches.at(-1) || null;
        const wanted = (Array.isArray(names) ? names : [])
          .map((value) => String(value || "").trim().toLocaleLowerCase())
          .filter(Boolean);
        if (!target && wanted.length) {
          const namedControls = deepAll("button,ytcp-button,tp-yt-paper-radio-button,tp-yt-paper-item");
          target = [...namedControls.filter(visible), ...namedControls]
            .find((element) => {
              const label = String(
                element.getAttribute("aria-label") || element.innerText || element.textContent || "",
              ).trim().toLocaleLowerCase();
              return wanted.some((name) => label === name || label.includes(name));
            });
        }
        if (!target) throw new Error("YOUTUBE_CONTROL_NOT_FOUND");
        target.click();
        return {
          clicked: true,
          tag: target.tagName || "",
          id: target.id || "",
          tabId: currentTabId,
        };
      },
      [
        String(params.selector || ""),
        Array.isArray(params.names) ? params.names : [],
        tab.id,
      ],
      "ISOLATED",
    );
    return { bridge: true, pageResult: pageResult ?? null, tabId: tab.id };
  }
  if (command === "focus") {
    const tab = await activeTab(params);
    await chrome.windows.update(tab.windowId, { focused: true });
    await chrome.tabs.update(tab.id, { active: true });
    return { focused: true };
  }
  throw new Error(`Lệnh không được hỗ trợ: ${command}`);
}

async function runCommand(message) {
  try {
    const result = await execute(message.command, message.params || {});
    send({ type: "result", id: message.id, ok: true, result });
  } catch (error) {
    send({
      type: "result",
      id: message.id,
      ok: false,
      error: String(error?.message || error),
    });
  }
}

async function pairNow() {
  if (pairingInProgress) {
    return { ok: false, error: "Đang có một phiên ghép profile khác." };
  }
  pairingInProgress = true;
  let id;
  try {
    id = await identity();
    await updateState({
      connected: false,
      paired: false,
      profileId: "",
      error: "Đang mở kết nối ghép profile…",
    });
  } catch (error) {
    const detail = String(error?.message || error);
    await updateState({
      connected: false,
      paired: false,
      profileId: "",
      error: `PAIRING_STORAGE_ERROR:${detail}`,
    });
    pairingInProgress = false;
    return { ok: false, error: `Không đọc được trạng thái ghép profile: ${detail}` };
  }
  try {
    return await new Promise((resolve) => {
    let pairSocket;
    let settled = false;
    const finish = async (result, stateError = "") => {
      if (settled) return;
      settled = true;
      if (stateError) {
        await updateState({
          connected: false,
          paired: false,
          profileId: "",
          error: stateError,
        });
      }
      resolve(result);
    };
    try {
      pairSocket = new WebSocket(BRIDGE_URL);
    } catch (error) {
      const detail = String(error?.message || error);
      void finish(
        { ok: false, error: `Không mở được kênh ghép profile: ${detail}` },
        `PAIRING_SOCKET_THROW:${detail}`,
      );
      return;
    }
    const timer = setTimeout(() => {
      try { pairSocket.close(); } catch {}
      void finish(
        { ok: false, error: "Không thấy chế độ ghép đang mở trong DocuRandom." },
        "PAIRING_TIMEOUT:127.0.0.1:17842",
      );
    }, 8000);
    pairSocket.onopen = () => {
      try {
        pairSocket.send(
          JSON.stringify({
            type: "pair",
            client_id: id.clientId,
            extension_version: EXTENSION_VERSION,
          }),
        );
      } catch (error) {
        const detail = String(error?.message || error);
        clearTimeout(timer);
        void finish(
          { ok: false, error: `Không gửi được yêu cầu ghép profile: ${detail}` },
          `PAIRING_SEND_ERROR:${detail}`,
        );
      }
    };
    pairSocket.onmessage = async (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      if (message.type === "paired") {
        clearTimeout(timer);
        try {
          await chrome.storage.local.set({
            profileId: message.profile_id,
            clientSecret: message.secret,
          });
          await updateState({
            connected: true,
            paired: true,
            profileId: message.profile_id,
            error: "",
          });
          pairSocket.close();
          socket?.close();
          socket = null;
          setTimeout(connect, 100);
          await finish({ ok: true, profileId: message.profile_id });
        } catch (error) {
          const detail = String(error?.message || error);
          await finish(
            { ok: false, error: `Không lưu được liên kết profile: ${detail}` },
            `PAIRING_STORAGE_SAVE_ERROR:${detail}`,
          );
        }
      } else if (message.type === "error") {
        clearTimeout(timer);
        pairSocket.close();
        void finish(
          { ok: false, error: message.error || "Không ghép được profile." },
          `PAIRING_BRIDGE_ERROR:${message.error || "unknown"}`,
        );
      }
    };
    pairSocket.onerror = () => {
      clearTimeout(timer);
      void finish(
        { ok: false, error: "DocuRandom Browser Bridge chưa chạy hoặc Wayfern đã từ chối kênh local." },
        "PAIRING_SOCKET_ERROR:127.0.0.1:17842",
      );
    };
    pairSocket.onclose = () => {
      clearTimeout(timer);
      void finish(
        { ok: false, error: "Kênh ghép profile đã đóng trước khi nhận phản hồi." },
        "PAIRING_SOCKET_CLOSED",
      );
    };
    });
  } finally {
    pairingInProgress = false;
  }
}

async function restorePair(profileId, clientSecret) {
  if (pairingInProgress) {
    return { ok: false, error: "Đang có một phiên ghép profile khác." };
  }
  const requestedProfileId = String(profileId || "").trim();
  const secret = String(clientSecret || "").trim();
  if (!/^[A-Za-z0-9_-]{8,128}$/.test(requestedProfileId)) {
    return { ok: false, error: "Profile ID khôi phục không hợp lệ." };
  }
  if (!/^[A-Za-z0-9_-]{20,128}$/.test(secret)) {
    return { ok: false, error: "Khóa liên kết cũ không hợp lệ." };
  }
  try {
    await chrome.storage.local.set({
      profileId: requestedProfileId,
      clientSecret: secret,
    });
    await updateState({
      connected: false,
      paired: true,
      profileId: requestedProfileId,
      error: "Đã khôi phục khóa; đang nối lại bridge…",
    });
    socket?.close();
    socket = null;
    setTimeout(connect, 100);
    return { ok: true, profileId: requestedProfileId };
  } catch (error) {
    const detail = String(error?.message || error);
    await updateState({
      connected: false,
      paired: false,
      profileId: "",
      error: `PAIRING_RESTORE_STORAGE_ERROR:${detail}`,
    });
    return { ok: false, error: `Không lưu được khóa liên kết cũ: ${detail}` };
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "get_status") {
    identity().then(async (id) => {
      const stored = await chrome.storage.local.get("bridgeState");
      sendResponse({
        ok: true,
        state: stored.bridgeState || bridgeState,
        profileId: id.profileId,
      });
    });
    return true;
  }
  if (message?.type === "pair") {
    pairNow().then(sendResponse);
    return true;
  }
  if (message?.type === "restore_pair") {
    const expectedPrefix = `chrome-extension://${chrome.runtime.id}/popup.html`;
    if (!String(sender?.url || "").startsWith(expectedPrefix)) {
      sendResponse({ ok: false, error: "Chỉ cho phép khôi phục từ trang Bridge nội bộ." });
      return false;
    }
    restorePair(message.profile_id, message.secret).then(sendResponse);
    return true;
  }
  if (message?.type === "forget") {
    chrome.storage.local
      .remove(["profileId", "clientSecret", "bridgeState"])
      .then(() => {
        socket?.close();
        socket = null;
        updateState({ connected: false, paired: false, profileId: "", error: "" });
        sendResponse({ ok: true });
      });
    return true;
  }
  return false;
});

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.action.onClicked.addListener(async () => {
  const setupUrl = chrome.runtime.getURL("popup.html");
  const tabs = await chrome.tabs.query({ url: setupUrl });
  if (tabs[0]?.id) {
    await chrome.tabs.update(tabs[0].id, { active: true });
    await chrome.windows.update(tabs[0].windowId, { focused: true });
    return;
  }
  await chrome.tabs.create({ url: setupUrl, active: true });
});
connect();
