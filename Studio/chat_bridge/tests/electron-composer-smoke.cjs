"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { app, BrowserWindow, Menu } = require("electron");
const {
  chatTurnSnapshot,
  chooseModel,
  confirmPreSubmitAttachmentCleared,
  pageSnapshot,
  normalizeComposerTextForComparison,
  PROMPT_INSERT_CHUNK_CHARS,
  prepareWindowForBackground,
  submitPrompt,
  waitForSubmissionAccepted,
  withRenderedWindowStage,
  withTrustedWindowStage,
} = require("../src/automation.cjs");
const {
  NAVIGATION_LABELS,
  installNavigationMenu,
} = require("../src/navigation.cjs");

const smokeProfile = fs.mkdtempSync(path.join(os.tmpdir(), "DocuRandomComposerSmoke-"));
app.setPath("userData", smokeProfile);
app.commandLine.appendSwitch("disable-gpu-shader-disk-cache");

async function main() {
  await app.whenReady();
  const window = new BrowserWindow({
    show: false,
    width: 900,
    height: 640,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false,
      paintWhenInitiallyHidden: true,
    },
  });
  // DOM diagnostics hash exact prompts with Web Crypto, as on ChatGPT's HTTPS
  // origin. data: pages do not provide crypto.subtle on current Electron.
  let fixtureHtml = '';
  await window.webContents.session.protocol.handle('https', () => new Response(fixtureHtml,
    {headers: {'Content-Type': 'text/html; charset=utf-8'}}));
  const loadFixture = window.loadURL.bind(window);
  window.loadURL = async url => {
    assert.ok(url.startsWith('data:text/html;charset=utf-8,'));
    fixtureHtml = decodeURIComponent(url.slice(url.indexOf(',') + 1));
    return loadFixture('https://chatgpt.com/c/6ad1dfd2-e594-83ec-9e92-d807176f1231');
  };
  try {
    await window.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(
      '<button data-testid="model-switcher-dropdown-button" onclick="window.__modelClicks=(window.__modelClicks||0)+1">GPT-6 Astra · Extra high</button>',
    ));
    await chooseModel(window, "chatgpt-web/auto");
    assert.deepEqual(await window.webContents.executeJavaScript(
      '({label:document.querySelector("button").textContent,clicks:window.__modelClicks||0})',
    ), {label:"GPT-6 Astra · Extra high", clicks:0});
    const readinessWindow = {
      isDestroyed: () => window.isDestroyed(),
      webContents: {
        getURL: () => 'https://chatgpt.com/c/readiness-fixture',
        isDestroyed: () => window.webContents.isDestroyed(),
        executeJavaScript: (...args) => window.webContents.executeJavaScript(...args),
      },
    };
    await window.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(
      '<meta charset="utf-8"><h2>We couldn’t load your account</h2><button>Reload</button>',
    )}`);
    const accountFailureSnapshot = await pageSnapshot(readinessWindow);
    assert.equal(accountFailureSnapshot.accountLoadFailed, true, JSON.stringify(accountFailureSnapshot));
    const accountTurnFailure = await chatTurnSnapshot(readinessWindow);
    assert.equal(accountTurnFailure.accountLoadFailed, true, JSON.stringify(accountTurnFailure));
    await window.webContents.executeJavaScript(`(() => {
      const quote = document.createElement('div');
      quote.setAttribute('data-message-author-role', 'user');
      quote.textContent = 'We couldn’t load your account';
      document.body.replaceChildren(quote);
    })()`);
    assert.equal((await pageSnapshot(readinessWindow)).accountLoadFailed, false);
    assert.equal((await chatTurnSnapshot(readinessWindow)).accountLoadFailed, false);
    await window.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(`<!doctype html>
      <textarea id="pending-conversation-input" aria-label="Ask ChatGPT">loading saved conversation</textarea>
    `)}`);
    const loadingConversation = await pageSnapshot(readinessWindow);
    assert.equal(loadingConversation.ready, false);
    assert.equal(loadingConversation.visibleComposers, 0);
    assert.equal(loadingConversation.composerCandidates[0].id, 'pending-conversation-input');
    await window.webContents.executeJavaScript("document.querySelector('textarea').id='pending-home-input'");
    const loadingHome = await pageSnapshot(readinessWindow);
    assert.equal(loadingHome.ready, false);
    assert.equal(loadingHome.authenticated, false);
    assert.equal(loadingHome.visibleComposers, 0);
    assert.equal(loadingHome.composerCandidates[0].id, 'pending-home-input');
    await window.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(`<!doctype html>
      <meta charset="utf-8">
      <style>
        body { margin: 20px; }
        #prompt-textarea { display: block; width: 700px; min-height: 120px; }
      </style>
      <textarea placeholder="stale hidden editor" style="display:none"></textarea>
      <form>
        <div role="textbox" data-testid="prompt-textarea">
          <div id="prompt-textarea" data-testid="prompt-textarea"
            data-lexical-editor="true" role="textbox" contenteditable="true">stale interrupted draft</div>
        </div>
        <button type="button" data-testid="send-button"
          onclick="const editor = document.querySelector('#prompt-textarea');
            globalThis.__sent = [...editor.childNodes].map(node => node.textContent || '').join('\\n');
            const turn = document.createElement('div');
            turn.setAttribute('data-message-author-role', 'user');
            turn.textContent = globalThis.__sent;
            document.body.append(turn)">Send</button>
      </form>
    `)}`);
    assert.equal((await pageSnapshot(readinessWindow)).ready, true);
    assert.equal(await confirmPreSubmitAttachmentCleared(readinessWindow,[{target:"master.txt"}]),true);
    await window.webContents.executeJavaScript(`(() => {
      const card=document.createElement('div');card.dataset.testid='attachment-card';
      card.textContent='master.txt';document.querySelector('form').append(card);
    })()`);
    assert.equal(await confirmPreSubmitAttachmentCleared(readinessWindow,[{target:"master.txt"}]),false,
      "a surviving file card must retain the import checkpoint");
    await window.webContents.executeJavaScript("document.querySelector('[data-testid=attachment-card]').remove()");
    const wrongRoute = {...readinessWindow,webContents:{...readinessWindow.webContents,
      getURL:()=> 'https://chatgpt.com/auth/login'}};
    assert.equal(await confirmPreSubmitAttachmentCleared(wrongRoute,[{target:"master.txt"}]),false);
    const initialBounds = window.getBounds();
    const initialVisibility = await window.webContents.executeJavaScript(
      "({ visibilityState: document.visibilityState, hidden: document.hidden })",
    );
    assert.equal(window.isVisible(), false);
    assert.equal(initialVisibility.visibilityState, "visible");
    assert.equal(initialVisibility.hidden, false);
    const stagedVisibility = await withRenderedWindowStage(
      window,
      undefined,
      () => window.webContents.executeJavaScript(
        "({ visibilityState: document.visibilityState, hidden: document.hidden })",
      ),
    );
    assert.deepEqual(stagedVisibility, { visibilityState: "visible", hidden: false });
    assert.equal(window.isVisible(), false);
    assert.deepEqual(window.getBounds(), initialBounds);

    const navigationMenu = installNavigationMenu(window, Menu);
    assert.deepEqual(navigationMenu.items.map(item => item.label), [
      NAVIGATION_LABELS.back,
      NAVIGATION_LABELS.home,
      NAVIGATION_LABELS.reload,
    ]);
    assert.equal(window.isMenuBarVisible(), true);
    await prepareWindowForBackground(window);
    assert.equal(window.isVisible(), true);
    assert.equal(window.getOpacity(), 0);
    const backgroundVisibility = await window.webContents.executeJavaScript(
      "({ visibilityState: document.visibilityState, hidden: document.hidden })",
    );
    assert.deepEqual(backgroundVisibility, { visibilityState: "visible", hidden: false });

    await window.webContents.executeJavaScript(`(() => {
      document.querySelector('#prompt-textarea').textContent = 'z'.repeat(192000);
    })()`);

    const targetChars = 307_891;
    const promptHead = "Nhật Bản ↔ một quốc gia cụ thể. Có thể là:\n   - hệ thống/sản phẩm Nhật đối đầu\n";
    const firstChunk = `${promptHead}${"x".repeat(
      PROMPT_INSERT_CHUNK_CHARS - promptHead.length - 1,
    )}\n`;
    const promptTail = "- dòng ngay sau biên chunk\n\nImage 1. 日本の精密機械\n   - suffix line";
    const prompt = `${firstChunk}${promptTail}${"y".repeat(
      targetChars - firstChunk.length - promptTail.length,
    )}`;
    assert.equal(firstChunk.length, PROMPT_INSERT_CHUNK_CHARS);
    assert.equal(prompt.length, targetChars);
    const before = await withTrustedWindowStage(
      window,
      undefined,
      () => submitPrompt(window, prompt),
    );
    assert.equal(before, 0);
    assert.equal(window.isVisible(), true);
    assert.equal(window.getOpacity(), 0);
    const state = await window.webContents.executeJavaScript(`(() => ({
      sent: globalThis.__sent,
      visible: [...document.querySelector('#prompt-textarea').childNodes]
        .map(node => node.textContent || '').join('\\n'),
      hidden: document.querySelector('textarea').value
    }))()`);
    assert.equal(normalizeComposerTextForComparison(state.sent), prompt);
    assert.equal(normalizeComposerTextForComparison(state.visible), prompt);
    assert.equal(state.hidden, "");

    // Current ChatGPT uses semantic message attributes and MarkdownRoot
    // instead of the older data-message-author-role/.markdown wrappers.
    await window.webContents.executeJavaScript(`(() => {
      const main = document.createElement('main');
      const userTurn = document.createElement('div');
      userTurn.setAttribute('data-chatgpt-search-message-ids', 'user-current');
      const userBubble = document.createElement('div');
      userBubble.className = 'group/user-message';
      userBubble.textContent = 'Current user prompt';
      userTurn.append(userBubble);
      const assistant = document.createElement('div');
      assistant.setAttribute('data-chatgpt-selection-message-id', 'assistant-current');
      const markdown = document.createElement('div');
      markdown.className = 'MarkdownRoot-fixture';
      markdown.setAttribute('data-markdown-text-style', 'true');
      markdown.textContent = 'Current assistant response';
      assistant.append(markdown);
      main.append(userTurn, assistant);
      document.body.append(main);
    })()`);
    const snapshot = await chatTurnSnapshot(window);
    assert.equal(snapshot.assistantCount, 1);
    assert.equal(snapshot.assistants.at(-1).text, "Current assistant response");
    assert.ok(snapshot.assistants.at(-1).markdownCount > 0);
    assert.ok(snapshot.userCount >= 1);

    const quotaNotice = "Capabilities reduced until 3:59 PM. Responses may have lower quality.";
    const quotaWindow = {
      isDestroyed: () => window.isDestroyed(),
      webContents: {
        getURL: () => 'https://chatgpt.com/c/quota-fixture',
        isDestroyed: () => window.webContents.isDestroyed(),
        executeJavaScript: (...args) => window.webContents.executeJavaScript(...args),
      },
    };
    await window.webContents.executeJavaScript(`(() => {
      document.querySelector('[data-chatgpt-search-message-ids]').textContent = ${JSON.stringify(quotaNotice)};
    })()`);
    assert.equal((await pageSnapshot(quotaWindow)).rateLimited, false,
      'quoting a capability notice in a modern user message must not pause the account');
    await window.webContents.executeJavaScript(`(() => {
      const notice = document.createElement('div');
      notice.id = 'service-quota-fixture';
      notice.innerHTML = '<span>Capabilities reduced until 3:59 PM.</span> '
        + '<span>Responses may have lower quality.</span>';
      document.body.append(notice);
    })()`);
    const quotaSnapshot = await pageSnapshot(quotaWindow);
    assert.equal(quotaSnapshot.rateLimited, false,
      'a reduced-quality advisory must not manufacture a hard 429');
    assert.equal(quotaSnapshot.capabilitiesReduced, true);
    assert.match(quotaSnapshot.capabilityNotice, /3:59 PM/);
    assert.equal(quotaSnapshot.ready, true);
    await window.webContents.executeJavaScript(`(() => {
      const hard = document.createElement('div');
      hard.id = 'hard-quota-fixture';
      hard.setAttribute('role', 'alert');
      hard.textContent = 'Too many requests. Try again later.';
      document.body.append(hard);
    })()`);
    const hardSnapshot = await pageSnapshot(quotaWindow);
    assert.equal(hardSnapshot.rateLimited, true,
      'a real hard-limit notice still blocks even beside the advisory');
    assert.equal(hardSnapshot.ready, false);
    await window.webContents.executeJavaScript("document.querySelector('#hard-quota-fixture').remove()");
    await window.webContents.executeJavaScript("document.querySelector('#service-quota-fixture').remove()");

    const beforeAssistant = snapshot.assistantCount;
    const beforeUser = snapshot.userCount;
    await window.webContents.executeJavaScript(`(() => {
      const stop = document.createElement('button');
      stop.setAttribute('data-testid', 'stop-button');
      stop.textContent = 'Stop';
      document.body.append(stop);
    })()`);
    await assert.rejects(
      waitForSubmissionAccepted(window, {
        beforeAssistant,
        beforeUser,
        timeoutMs: 300,
      }),
      /timed out|chưa xác nhận/i,
    );
  } finally {
    window.destroy();
    app.quit();
  }
}

main().then(() => {
  if (process.env.DOCURANDOM_COMPOSER_SMOKE_RESULT) {
    fs.writeFileSync(process.env.DOCURANDOM_COMPOSER_SMOKE_RESULT, 'passed\n');
  }
}).catch(error => {
  console.error(error);
  app.exit(1);
});
