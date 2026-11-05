"use strict";

const CHAT_URL = "https://chatgpt.com/";
const NAVIGATION_LABELS = Object.freeze({
  back: "←  QUAY LẠI",
  home: "⌂  CHATGPT",
  reload: "↻  TẢI LẠI",
});

function isExactChatConversationIdentity(value) {
  const match = String(value || "").match(/^\/c\/([^/?#]+)$/);
  if (!match) return false;
  try {
    // ChatGPT assigns a renderer-local route before the server creates the
    // saved conversation. Reloading this provisional route loses the turn.
    const id = decodeURIComponent(match[1]);
    return Boolean(id) && !/^local-chatgpt:/i.test(id) && !/[\/?#]/.test(id);
  } catch { return false; }
}

function isExactChatConversationUrl(value) {
  try {
    const parsed = new URL(String(value || ""));
    return parsed.protocol === "https:" && parsed.hostname === "chatgpt.com"
      && !parsed.username && !parsed.password
      && isExactChatConversationIdentity(parsed.pathname);
  } catch { return false; }
}

function navigateBackOrHome(window) {
  if (!window || window.isDestroyed()) return false;
  const history = window.webContents.navigationHistory;
  if (history?.canGoBack()) {
    history.goBack();
  } else {
    void window.loadURL(CHAT_URL);
  }
  return true;
}

function installNavigationMenu(window, Menu) {
  if (!window || window.isDestroyed() || !Menu?.buildFromTemplate) return null;
  const menu = Menu.buildFromTemplate([
    {
      id: "docurandom-back",
      label: NAVIGATION_LABELS.back,
      accelerator: "Alt+Left",
      click: () => navigateBackOrHome(window),
    },
    {
      id: "docurandom-home",
      label: NAVIGATION_LABELS.home,
      accelerator: "Alt+Home",
      click: () => {
        if (!window.isDestroyed()) void window.loadURL(CHAT_URL);
      },
    },
    {
      id: "docurandom-reload",
      label: NAVIGATION_LABELS.reload,
      accelerator: "Ctrl+R",
      click: () => {
        if (!window.isDestroyed()) window.webContents.reload();
      },
    },
  ]);
  window.setMenu(menu);
  window.setAutoHideMenuBar(false);
  window.setMenuBarVisibility(true);
  return menu;
}

module.exports = {
  CHAT_URL,
  NAVIGATION_LABELS,
  isExactChatConversationIdentity,
  isExactChatConversationUrl,
  installNavigationMenu,
  navigateBackOrHome,
};
