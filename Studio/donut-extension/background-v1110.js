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
        "youtube-click",
        "youtube-query",
        "youtube-attached-video",
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
      "ISOLATED",
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
    return await executeInTab(
      tab.id,
      (source) => globalThis.eval(source),
      [String(params.expression || "")],
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
        const output = [];
        const visit = (root) => {
          for (const element of root.querySelectorAll(selector)) output.push(element);
          for (const element of root.querySelectorAll("*")) {
            if (element.shadowRoot) visit(element.shadowRoot);
          }
        };
        visit(document);
        const target = output[output.length - 1] || null;
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
          text: String(target.innerText || target.textContent || "").trim(),
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
        let target = selector ? deepAll(selector).at(-1) : null;
        const wanted = (Array.isArray(names) ? names : [])
          .map((value) => String(value || "").trim().toLocaleLowerCase())
          .filter(Boolean);
        if (!target && wanted.length) {
          target = deepAll("button,ytcp-button,tp-yt-paper-radio-button,tp-yt-paper-item")
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
