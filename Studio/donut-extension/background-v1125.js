const BRIDGE_URL = "ws://127.0.0.1:17842";
const EXTENSION_VERSION = chrome.runtime.getManifest().version;

let socket = null;
let reconnectTimer = null;
let keepAliveTimer = null;
let controlledTabId = null;
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
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, 1500);
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
  ensureKeepAlive();
  const id = await identity();
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
  socket = new WebSocket(BRIDGE_URL);
  socket.onopen = () => {
    send({
      type: "hello",
      client_id: id.clientId,
      profile_id: id.profileId,
      secret: id.clientSecret,
      extension_version: EXTENSION_VERSION,
    });
  };
  socket.onmessage = async (event) => {
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
  socket.onclose = async () => {
    await updateState({ connected: false });
    scheduleReconnect();
  };
  socket.onerror = () => {
    socket?.close();
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
  return results[0].result;
}

// Return stable, ID-scoped rows from Studio's Content page.  Studio's current
// draft rows do not always expose an /video/<id>/edit anchor: a draft can have
// only a thumbnail URL (/vi/<id>/...) and an anchor whose accessible name is
// the title.  Keep this reader in the extension transport so recovery does not
// depend on the native Runtime.evaluate route.
function contentStateInTab(expectedVideoId, action, currentTabId) {
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
      const thumbnailMatch = text.match(/\/vi\/([A-Za-z0-9_-]{6,20})\//i);
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
        row,
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
}

async function execute(command, params) {
  if (command === "ping") {
    return {
      ok: true,
      extensionVersion: EXTENSION_VERSION,
      capabilities: [
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
      () => 42,
      [],
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
  const id = await identity();
  return await new Promise((resolve) => {
    const pairSocket = new WebSocket(BRIDGE_URL);
    const timer = setTimeout(() => {
      pairSocket.close();
      resolve({ ok: false, error: "Không thấy chế độ ghép đang mở trong DocuRandom." });
    }, 8000);
    pairSocket.onopen = () => {
      pairSocket.send(
        JSON.stringify({
          type: "pair",
          client_id: id.clientId,
          extension_version: EXTENSION_VERSION,
        }),
      );
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
        resolve({ ok: true, profileId: message.profile_id });
      } else if (message.type === "error") {
        clearTimeout(timer);
        pairSocket.close();
        resolve({ ok: false, error: message.error || "Không ghép được profile." });
      }
    };
    pairSocket.onerror = () => {
      clearTimeout(timer);
      resolve({ ok: false, error: "DocuRandom Browser Bridge chưa chạy." });
    };
  });
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
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
