"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  CHAT_URL,
  NAVIGATION_LABELS,
  installNavigationMenu,
  navigateBackOrHome,
  isExactChatConversationIdentity,
  isExactChatConversationUrl,
} = require("../src/navigation.cjs");

test("provisional ChatGPT routes cannot be used as durable recovery links", () => {
  for (const id of ["local-chatgpt:abc", "local-chatgpt%3Aabc", "LOCAL-CHATGPT%3aabc", "%ZZ", "abc/extra"]) {
    assert.equal(isExactChatConversationIdentity(`/c/${id}`), false);
    assert.equal(isExactChatConversationUrl(`https://chatgpt.com/c/${id}`), false);
  }
  assert.equal(isExactChatConversationUrl("https://chatgpt.com/c/6ac5b07e-63b8-83ec-9a3e-41b61a8836c9"), true);
  assert.equal(isExactChatConversationUrl("https://user@chatgpt.com/c/abc"), false);
});

function fakeWindow({ canGoBack = false } = {}) {
  const calls = [];
  const window = {
    calls,
    destroyed: false,
    isDestroyed() { return this.destroyed; },
    loadURL(url) { calls.push(["load", url]); return Promise.resolve(); },
    setMenu(menu) { calls.push(["menu", menu]); },
    setAutoHideMenuBar(value) { calls.push(["auto-hide", value]); },
    setMenuBarVisibility(value) { calls.push(["visible", value]); },
    webContents: {
      navigationHistory: {
        canGoBack: () => canGoBack,
        goBack: () => calls.push(["back"]),
      },
      reload: () => calls.push(["reload"]),
    },
  };
  return window;
}

test("native Electron navigation remains outside ChatGPT and identity-provider pages", () => {
  const window = fakeWindow();
  const Menu = {
    buildFromTemplate(template) { return { items: template }; },
  };

  const menu = installNavigationMenu(window, Menu);

  assert.deepEqual(menu.items.map(item => item.label), [
    NAVIGATION_LABELS.back,
    NAVIGATION_LABELS.home,
    NAVIGATION_LABELS.reload,
  ]);
  assert.deepEqual(window.calls.slice(-2), [
    ["auto-hide", false],
    ["visible", true],
  ]);
});

test("back uses browser history and safely falls home when no history exists", () => {
  const withHistory = fakeWindow({ canGoBack: true });
  const withoutHistory = fakeWindow({ canGoBack: false });

  assert.equal(navigateBackOrHome(withHistory), true);
  assert.deepEqual(withHistory.calls, [["back"]]);
  assert.equal(navigateBackOrHome(withoutHistory), true);
  assert.deepEqual(withoutHistory.calls, [["load", CHAT_URL]]);
});
