const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../flow-extension/flow-ui.js'), 'utf8');
const background = fs.readFileSync(path.join(__dirname, '../flow-extension/background.js'), 'utf8');

function harness() {
  const listeners = [];
  const buttons = [];
  class Element {
    constructor(text, project = false) { this.innerText = text; this.project = project; }
    getBoundingClientRect() { return {width:150,height:40}; }
    getAttribute() { return null; }
    matches() { return false; }
    closest() { return null; }
    contains() { return false; }
    querySelectorAll() { return []; }
  }
  const context = vm.createContext({
    Element, setTimeout, clearTimeout,
    URL,
    getComputedStyle: () => ({display:'block',visibility:'visible'}),
    location: {protocol:'file:', pathname:'/project/test', href:'file:///project/test'},
    document: {documentElement:{dataset:{docurandomFlowTest:'1'}},
      querySelectorAll: selector => selector.startsWith('button.new-project')
        ? buttons.filter(b => b.project) : buttons},
    chrome: {runtime:{getManifest:()=>({version:'test'}), onMessage:{addListener:fn=>listeners.push(fn)}}},
  });
  vm.runInContext(source, context);
  return {context, buttons, Element, listeners};
}

function projectPreparationHarness({ channelClosed = false, createProject = true, wrongPage = false } = {}) {
  const helper = background.slice(
    background.indexOf('async function prepareFlowProjectBeforeGenerate('),
    background.indexOf('async function handleUiGenerateImages('),
  );
  const calls = [];
  let tab = { id: 7, url: wrongPage ? 'https://example.com/' : 'https://flow.google.com/u/0/project/previous' };
  const context = vm.createContext({
    FLOW_URL: 'https://flow.google.com/',
    activeUiJob: { requestId: 'request' },
    workspaceFlowTabs: new Map([['workspace', { createdByExtension: true }]]),
    cleanWorkspaceIdentity: value => String(value || ''),
    flowProjectIdFromTab: value => value.url.match(/\/project\/([^/?#]+)/)?.[1] || '',
    isFlowUrl: value => value.startsWith('https://flow.google.com/'),
    isUiMessageChannelClosedError: error => /channel closed/.test(error.message),
    chrome: { tabs: {
      get: async () => ({ ...tab }),
      update: async (id, update) => { calls.push('navigate-home'); tab = { id, ...update }; return tab; },
    } },
    waitForFlowDocument: async () => calls.push('document-ready'),
    ensureUiAutomationVersion: async () => calls.push('driver-ready'),
    sendUiMessageWithTimeout: async (_id, message) => {
      calls.push(message.type);
      if (createProject) tab.url = 'https://flow.google.com/u/0/project/fresh';
      if (channelClosed) throw new Error('message channel closed');
      return { ok: true };
    },
    waitForTabComplete: async () => ({ ...tab }),
    rememberWorkspaceFlowTab: async (_workspace, _tab, project, owned) => calls.push(`bind:${project}:${owned}`),
    sendToAgent: message => calls.push(`progress:${message.projectId}`),
  });
  vm.runInContext(helper, context);
  return { calls, prepare: () => context.prepareFlowProjectBeforeGenerate(tab, {
    workspaceId: 'workspace', flowProjectId: '', newProject: true, jobId: 'job',
  }), context, tab };
}

test('new project navigation finishes before any image generation command', async () => {
  const h = projectPreparationHarness();
  const result = await h.prepare();
  assert.equal(result.projectId, 'fresh');
  assert.deepEqual(h.calls, [
    'navigate-home', 'document-ready', 'driver-ready', 'DOCURANDOM_UI_PREPARE_PROJECT',
    'driver-ready', 'bind:fresh:true', 'progress:fresh',
  ]);
});

test('project creation may close its own response channel only with a verified new project route', async () => {
  const h = projectPreparationHarness({ channelClosed: true });
  assert.equal((await h.prepare()).projectId, 'fresh');
  const missing = projectPreparationHarness({ channelClosed: true, createProject: false });
  await assert.rejects(missing.prepare(), /FLOW_UI_PROJECT_PREPARE_NO_PROJECT/);
  assert.equal(missing.calls.some(call => call.startsWith('bind:')), false);
});

test('existing projects and unrelated pages cannot be navigated during preparation', async () => {
  const h = projectPreparationHarness();
  const result = await h.context.prepareFlowProjectBeforeGenerate(h.tab, {
    workspaceId: 'workspace', flowProjectId: 'previous', newProject: false,
  });
  assert.equal(result.projectId, 'previous');
  assert.deepEqual(h.calls, []);
  const wrong = projectPreparationHarness({ wrongPage: true });
  await assert.rejects(wrong.prepare(), /FLOW_UI_PROJECT_PREPARE_WRONG_PAGE/);
  assert.deepEqual(wrong.calls, []);
});

test('project identity parser accepts Flow path, query and hash routes', () => {
  const {context} = harness();
  const parser = context.__DOCURANDOM_FLOW_TEST_HOOKS__.projectIdFromLocation;
  context.location.pathname = '/projects/path-project';
  context.location.href = 'https://flow.google.com/projects/path-project';
  assert.equal(parser(), 'path-project');
  context.location.pathname = '/';
  context.location.href = 'https://flow.google.com/?projectId=query-project';
  assert.equal(parser(), 'query-project');
  context.location.href = 'https://flow.google.com/#/project/hash-project';
  assert.equal(parser(), 'hash-project');
});

test('reinjecting same driver never redeclares constants or duplicates listeners', () => {
  const {context, listeners} = harness();
  vm.runInContext(source, context);
  assert.equal(listeners.length, 1);
});
test('Angular new-project button is selected, unrelated add control is not', () => {
  const {context, buttons, Element} = harness();
  buttons.push(new Element('add Upload'));
  assert.equal(context.__DOCURANDOM_FLOW_TEST_HOOKS__.findCreateProjectButton(), null);
  const create = new Element('add Dự án mới', true);
  buttons.push(create);
  assert.equal(context.__DOCURANDOM_FLOW_TEST_HOOKS__.findCreateProjectButton(), create);
});
test('a stuck browser command cannot retain the queue forever', async () => {
  const helper = background.match(/async function withUiDeadline\([\s\S]*?\n\}/)[0];
  const context = vm.createContext({setTimeout,clearTimeout,UI_COMMAND_TIMEOUT_MS:5});
  vm.runInContext(helper, context);
  await assert.rejects(context.withUiDeadline(new Promise(()=>{})), /FLOW_UI_COMMAND_TIMEOUT/);
  assert.equal(await context.withUiDeadline(Promise.resolve('done')), 'done');
});

test('closed UI response channels require an explicit cancellation acknowledgement', () => {
  assert.match(background, /function isUiMessageChannelClosedError/);
  assert.match(background, /cancelledJobs > 0 && !ack\.submissionAttempted/);
  assert.match(background, /FLOW_UI_TIMEOUT_UNSENT:phase=/);
  assert.match(background, /FLOW_UI_SUBMISSION_UNCERTAIN:phase=generate-command:cause=/);
  assert.match(background, /;cancelAck=/);
});

test('workspace project recovery gets a deadline longer than all route attempts', () => {
  assert.match(background, /FLOW_WORKSPACE_TAB_OPEN_TIMEOUT_MS = 120000/);
  assert.match(background, /candidateCount \* FLOW_PROJECT_REOPEN_TIMEOUT_MS \+ FLOW_PROJECT_REOPEN_MARGIN_MS/);
  assert.match(background, /workspaceFlowTabOpenTimeoutMs\(/);
});

function reusableFlowHarness(tabs, windowState, windowFocused) {
  const helper = background.match(
    /function isSafeBackgroundSharedFlowTab\([\s\S]*?\n\}/,
  )[0] + '\n' + background.match(
    /async function restoreRememberedWorkTab\([\s\S]*?\n\}/,
  )[0] + '\n' + background.match(
    /async function findReusableFlowTab\([\s\S]*?\n\}/,
  )[0];
  const context = vm.createContext({
    FLOW_WORK_TAB_SESSION_KEY: 'docurandomFlowWorkTab',
    cleanWorkspaceIdentity: value => String(value || '').trim().slice(0, 200),
    flowProjectIdFromTab: tab => String(tab?.url || tab?.pendingUrl || '').match(/\/project\/([^/?#]+)/)?.[1] || '',
    isFlowUrl: value => String(value || '').startsWith('https://flow.google.com/'),
    isGoogleAccountsUrl: value => String(value || '').startsWith('https://accounts.google.com/'),
    isLegacyFlowMarketingLanding: () => false,
    restoreWorkspaceFlowTabs: async () => {},
    workspaceFlowTabs: new Map(),
    workTabId: 0,
    workTabCreatedByExtension: false,
    getAutomationFlowTabs: async () => ({browserWindow: null, tabs: []}),
    collapseOwnedAutomationFlowTabs: async () => {},
    chrome: {
      tabs: {
        query: async () => tabs,
        get: async id => tabs.find(tab => Number(tab.id) === Number(id)) || null,
      },
      windows: {
        get: async id => ({
          id,
          type: 'normal',
          state: windowState,
          focused: windowFocused,
        }),
      },
      storage: {
        session: {
          get: async key => ({
            [key]: {tabId: 7, windowId: 11, createdByExtension: false},
          }),
          remove: async () => {},
        },
      },
    },
  });
  vm.runInContext(helper, context);
  return context;
}

test('a background shared Flow tab is reusable across workspaces without stealing focus', async () => {
  const context = reusableFlowHarness([
    {
      id: 7,
      windowId: 11,
      active: false,
      status: 'complete',
      url: 'https://flow.google.com/u/0/project/previous-project',
    },
  ], 'normal', true);

  const selected = await context.findReusableFlowTab('next-project');
  assert.equal(selected.tab.id, 7);
  assert.equal(selected.sharedWorkTab, true);
  assert.equal(selected.createdByExtension, false);
});

test('an exact checkpoint project beats the shared Flow tab', async () => {
  const context = reusableFlowHarness([
    {id: 7, windowId: 11, active: false, status: 'complete', url: 'https://flow.google.com/u/0/project/previous-project'},
    {id: 8, windowId: 12, active: true, status: 'complete', url: 'https://flow.google.com/u/0/project/checkpoint-project'},
  ], 'minimized', false);

  const selected = await context.findReusableFlowTab('checkpoint-project');
  assert.equal(selected.tab.id, 8);
});

test('a focused active shared Flow tab is not taken over', async () => {
  const context = reusableFlowHarness([
    {id: 7, windowId: 11, active: true, status: 'complete', url: 'https://flow.google.com/u/0/project/previous-project'},
  ], 'normal', true);

  assert.equal(await context.findReusableFlowTab('next-project'), null);
});

test('completed workspace generation parks its dedicated Flow window', async () => {
  let updatedTab = null;
  let parkedWindow = null;
  const context = vm.createContext({
    console,
    cleanWorkspaceIdentity: value => String(value || '').trim().slice(0, 200),
    restoreWorkspaceFlowTabs: async () => {},
    persistWorkspaceFlowTabs: async () => { context.persisted = (context.persisted || 0) + 1; },
    workspaceTabRecord: (tab, projectId, createdByExtension, projectUrl) => ({
      tabId: Number(tab?.id || 0),
      projectId,
      createdByExtension,
      projectUrl,
    }),
    flowProjectIdFromTab: () => 'project-1',
    isFlowUrl: url => String(url || '').startsWith('https://flow.google.com/'),
    isGoogleAccountsUrl: () => false,
    workspaceFlowTabs: new Map([
      ['DaiTa15', {
        tabId: 7,
        windowId: 11,
        projectId: 'project-1',
        projectUrl: 'https://flow.google.com/u/0/project/project-1',
        createdByExtension: true,
      }],
    ]),
    workTabId: 7,
    getAutomationWindow: async () => ({id: 11, type: 'normal'}),
    rememberWorkTab: async tab => { context.parkedTab = tab; },
    parkAutomationWindow: async id => { parkedWindow = id; },
    FLOW_URL: 'https://flow.google.com/',
    chrome: {
      tabs: {
        get: async id => id === 7
          ? {id: 7, windowId: 11, url: 'https://flow.google.com/u/0/project/project-1'}
          : null,
        update: async (id, updates) => {
          updatedTab = {id, updates};
          return {id, windowId: 11, url: updates.url};
        },
      },
      windows: {},
    },
  });
  const helper = background.match(
    /async function closeCompletedWorkspaceFlowTab[\s\S]*?\n\}\n\nif \(chrome\.tabs\?\./,
  )[0].replace(/\n\nif \(chrome\.tabs\?\.[\s\S]*$/, '');
  vm.runInContext(helper, context);

  const result = await context.closeCompletedWorkspaceFlowTab(
    'DaiTa15',
    {id: 7, windowId: 11},
  );
  assert.equal(result.closed, false);
  assert.equal(result.parked, true);
  assert.equal(JSON.stringify(updatedTab), JSON.stringify({
    id: 7,
    updates: {
      url: 'https://flow.google.com/',
      active: false,
      autoDiscardable: false,
    },
  }));
  assert.equal(parkedWindow, 11);
  assert.equal(context.parkedTab.id, 7);
  assert.equal(context.workspaceFlowTabs.get('DaiTa15').tabId, 0);
  assert.equal(context.workspaceFlowTabs.get('DaiTa15').projectId, 'project-1');
  assert.equal(context.persisted, 1);
});

test('Google sign-in redirects stop before injecting the Flow driver', async () => {
  const helper = background.match(/async function waitForFlowDocument\([\s\S]*?\n\}/)[0];
  let url = 'https://accounts.google.com/v3/signin/confirmidentifier';
  const context = vm.createContext({
    waitForTabComplete: async id => ({id, url}),
    isFlowUrl: value => value.startsWith('https://flow.google.com/'),
  });
  vm.runInContext(helper, context);
  await assert.rejects(context.waitForFlowDocument(1), /FLOW_UI_LOGIN_REQUIRED/);
  url = 'https://example.com/';
  await assert.rejects(context.waitForFlowDocument(1), /FLOW_UI_NOT_ON_FLOW/);
  url = 'https://flow.google.com/u/1/project/test';
  assert.equal((await context.waitForFlowDocument(1)).url, url);
});

test('new Flow image nodes preserve their media ID and signed URL', () => {
  const {context} = harness();
  const tile = {
    getAttribute: name => name === 'data-media-id' ? 'new-media' : null,
    matches: value => value === 'img',
    src:'https://flow-content.google/image/new-media', naturalWidth:1376, naturalHeight:768,
  };
  const hooks = context.__DOCURANDOM_FLOW_TEST_HOOKS__;
  assert.equal(hooks.tileIdentity(tile), 'new-media');
  assert.equal(hooks.chooseGeneratedImage(tile).url, tile.src);
  assert.equal(hooks.chooseGeneratedImage(tile).tileId, 'new-media');
});

test('project snapshot matches an existing tile without submitting a prompt', async () => {
  const {context, buttons, Element} = harness();
  const prompt = 'Chronology board image proving vibration rose before defects.';
  const tile = new Element(prompt);
  tile.getAttribute = name => name === 'data-tile-id' ? 'existing-tile' : null;
  buttons.push(tile);
  const snapshot = await context.__DOCURANDOM_FLOW_TEST_HOOKS__.inspectProjectSnapshot(
    {cancelled:false},
    {prompts:[prompt]},
  );
  assert.equal(snapshot.tileCount, 1);
  assert.equal(snapshot.matches[0].matched, true);
  assert.equal(snapshot.matches[0].tileId, 'existing-tile');
});

test('active-only project snapshot reads just the selected Flow tab', async () => {
  const handler = background.match(
    /async function handleUiProjectSnapshot\(msg\) \{[\s\S]*?\n\}/,
  )[0];
  const activeTab = {
    id: 55,
    url: 'https://flow.google.com/u/0/project/selected-project',
    pendingUrl: '',
  };
  const calls = {query: [], requests: [], restore: 0, search: 0, create: 0, attach: 0};
  const results = [];
  const context = vm.createContext({
    activeUiJob: null,
    UI_PROJECT_SNAPSHOT_TIMEOUT_MS: 100,
    Date,
    cleanWorkspaceIdentity: value => String(value || '').trim().slice(0, 200),
    rejectIfUiBusy: async () => false,
    setState: () => {},
    isFlowUrl: value => /^https:\/\/(flow\.google\.com|labs\.google)\//.test(String(value || '')),
    restoreRememberedWorkTab: async () => { calls.restore += 1; throw Error('must not restore'); },
    findExistingFlowTab: async () => { throw Error('must not scan tabs'); },
    findPromptInExistingFlowTabs: async () => { calls.search += 1; throw Error('must not scan tabs'); },
    flowProjectIdFromTab: tab => String(tab?.url || '').match(/\/project\/([^/?#]+)/)?.[1] || '',
    withUiDeadline: promise => promise,
    waitForFlowDocument: async () => {},
    attachUiDebugger: async () => { calls.attach += 1; },
    detachUiDebugger: async () => {},
    ensureUiAutomationVersion: async () => {},
    startUiWakePulse: () => 1,
    stopUiWakePulse: () => {},
    sendUiMessageWithRetry: async (_tabId, message) => {
      calls.requests.push(message);
      return {
        ok: true,
        projectId: 'selected-project',
        projectUrl: activeTab.url,
        matchedTabId: activeTab.id,
      };
    },
    snapshotContainsPromptMatch: () => false,
    rememberWorkspaceFlowTab: async () => { throw Error('must not remember a workspace tab'); },
    workspaceFlowTabs: new Map(),
    sendToAgent: value => results.push(value),
    chrome: {
      tabs: {
        query: async query => { calls.query.push(query); return [activeTab]; },
        get: async id => id === activeTab.id ? activeTab : null,
        create: async () => { calls.create += 1; throw Error('must not create a tab'); },
      },
    },
  });
  vm.runInContext(handler, context);

  await context.handleUiProjectSnapshot({id: 'snapshot-1', params: {activeTabOnly: true}});

  assert.equal(calls.query.length, 1);
  assert.equal(calls.query[0].active, true);
  assert.equal(calls.query[0].currentWindow, true);
  assert.equal(calls.restore, 0);
  assert.equal(calls.search, 0);
  assert.equal(calls.create, 0);
  assert.equal(calls.requests[0].request.activeTabOnly, true);
  assert.equal(results[0].result.projectId, 'selected-project');

  activeTab.url = 'https://example.com/not-flow';
  await context.handleUiProjectSnapshot({id: 'snapshot-2', params: {activeTabOnly: true}});
  assert.equal(calls.attach, 1);
  assert.match(results[1].error, /FLOW_ACTIVE_TAB_NOT_A_FLOW_PROJECT/);
});

test('project surface readiness waits for a mounted history/composer surface', async () => {
  const {context, buttons, Element} = harness();
  const tile = new Element('mounted history tile');
  tile.getAttribute = name => name === 'data-tile-id' ? 'mounted-tile' : null;
  buttons.push(tile);
  const state = await context.__DOCURANDOM_FLOW_TEST_HOOKS__.waitForProjectSurface(
    {cancelled:false},
    {requireHistory:true, timeoutMs:20},
  );
  assert.equal(state.tileCount, 1);
  assert.equal(state.hasPromptEditor, false);
});

test('legacy discovery continues after its remembered project cannot be read', async () => {
  const handler = background.match(/async function handleUiProjectSnapshot\(msg\) \{[\s\S]*?\n\}/)[0];
  const primary = {id: 11, url: 'https://flow.google.com/project/empty'};
  const matched = {id: 12, url: 'https://flow.google.com/project/proven'};
  const results = [];
  let searched = false;
  const context = vm.createContext({
    Date, activeUiJob: null, workTabId: 11,
    cleanWorkspaceIdentity: value => String(value || ''),
    rejectIfUiBusy: async () => false, setState: () => {},
    restoreRememberedWorkTab: async () => {}, isFlowUrl: () => true,
    inspectExistingFlowTab: async (_tab, request) => {
      assert.equal(request.discoveryOnly, true);
      throw Error('FLOW_UI_PROJECT_NOT_READY');
    },
    findPromptInExistingFlowTabs: async (_tabId, _request, _jobId, initial) => {
      searched = true;
      assert.equal(initial.ok, true);
      return {ok: true, matchedTabId: 12, projectId: 'proven', matches: [{matched: true}]};
    },
    stopUiWakePulse: () => {}, detachUiDebugger: async () => {},
    chrome: {tabs: {get: async id => id === 11 ? primary : matched}},
    sendToAgent: value => results.push(value),
  });
  vm.runInContext(handler, context);
  await context.handleUiProjectSnapshot({id: 'legacy', params: {prompts: ['exact old prompt'], inspectTilePrompts: true}});
  assert.equal(searched, true);
  assert.equal(results[0].status, 200);
  assert.equal(results[0].result.projectId, 'proven');
  assert.equal(context.activeUiJob, null);
});

test('history transport timeout cancels the page inspection before detaching', async () => {
  const helper = background.match(/async function inspectExistingFlowTab\([\s\S]*?\n\}/)[0];
  const calls = [];
  const context = vm.createContext({
    activeUiJob: {}, isFlowUrl: () => true,
    attachUiDebugger: async () => calls.push('attach'),
    ensureUiAutomationVersion: async () => {},
    sendUiMessageWithRetry: async () => { throw Error('transport timeout'); },
    sendUiMessage: async (_tabId, message) => { calls.push(message.type); return {ok: true}; },
    withUiDeadline: promise => promise,
    detachUiDebugger: async () => calls.push('detach'),
  });
  vm.runInContext(helper, context);
  await assert.rejects(context.inspectExistingFlowTab({id: 1, url: 'flow'}, {}, 'job'), /transport timeout/);
  assert.deepEqual(calls, ['attach', 'DOCURANDOM_UI_CANCEL', 'detach']);
});

function historySearchHarness(responses) {
  const helper = background.match(/async function findPromptInExistingFlowTabs\([\s\S]*?\n\}/)[0];
  const tabs = [1, 2, 3].map(id => ({id, status: 'complete', url: `https://flow.google.com/project/${responses[id]?.projectId || 'empty'}`}));
  const remembered = [];
  const context = vm.createContext({
    Date, console, FLOW_TAB_URLS: [], activeUiJob: {},
    cleanWorkspaceIdentity: value => String(value || ''),
    flowProjectIdFromTab: tab => tab?.url?.split('/').at(-1) || '',
    snapshotContainsPromptMatch: response => response?.matches?.some(item => item.matched),
    inspectExistingFlowTab: async tab => responses[tab.id] || {ok: true, matches: []},
    rememberWorkTab: async tab => remembered.push(tab.id),
    chrome: {tabs: {query: async () => tabs, get: async id => tabs.find(tab => tab.id === id)}},
  });
  vm.runInContext(helper, context);
  return {context, remembered};
}

test('legacy history accepts one proven project and rejects two different matching projects', async () => {
  const match = projectId => ({ok: true, projectId, matches: [{matched: true}]});
  const single = historySearchHarness({2: match('proven')});
  const result = await single.context.findPromptInExistingFlowTabs(1, {}, 'job', {ok: true});
  assert.equal(result.projectId, 'proven');
  assert.equal(result.scannedTabs, 3);
  assert.deepEqual(single.remembered, [2]);
  const ambiguous = historySearchHarness({2: match('a'), 3: match('b')});
  await assert.rejects(ambiguous.context.findPromptInExistingFlowTabs(1, {}, 'job', {ok: true}), /FLOW_LEGACY_PROJECT_MATCH_AMBIGUOUS/);
  assert.deepEqual(ambiguous.remembered, []);
});

test('a cancelled Reuse inspection restores the original composer without submitting', async () => {
  const {context, Element} = harness();
  class TextArea extends Element { constructor() { super(''); this.value = 'original composer'; } }
  const editor = new TextArea();
  editor.focus = () => {};
  editor.dispatchEvent = () => {};
  context.HTMLTextAreaElement = TextArea;
  context.HTMLInputElement = class {};
  context.InputEvent = class {};
  context.Event = class {};
  const job = {cancelled: false};
  const reuse = new Element('reuse prompt');
  reuse.isConnected = true;
  reuse.click = () => { editor.value = 'prompt from another tile'; job.cancelled = true; };
  const tile = new Element('tile');
  tile.querySelectorAll = () => [reuse];
  tile.dispatchEvent = () => {};
  context.document.querySelectorAll = selector => selector.includes('textarea') ? [editor] : [];
  context.chrome.runtime.sendMessage = async () => ({ok: true});
  await assert.rejects(context.__DOCURANDOM_FLOW_TEST_HOOKS__.readPromptFromReuseButton(tile, job, editor.value), /CANCELLED/);
  assert.equal(editor.value, 'original composer');
});

test('legacy discovery can inspect a mounted empty composer without waiting for nonexistent history', async () => {
  const {context, Element} = harness();
  class TextArea extends Element { constructor() { super(''); this.value = ''; } }
  context.HTMLTextAreaElement = TextArea;
  const editor = new TextArea();
  context.document.querySelectorAll = selector => selector.includes('textarea') ? [editor] : [];
  const snapshot = await context.__DOCURANDOM_FLOW_TEST_HOOKS__.inspectProjectSnapshot(
    {cancelled: false},
    {prompts: ['old prompt'], inspectTilePrompts: true, discoveryOnly: true},
  );
  assert.equal(snapshot.surface.hasPromptEditor, true);
  assert.equal(snapshot.tileCount, 0);
  assert.equal(snapshot.matches[0].matched, false);
});

test('Angular click runs once without the legacy pointer-toggle sequence', async () => {
  const {context} = harness();
  // Exercise the production helper with only the worker-owned delay stubbed.
  context.chrome.runtime.sendMessage = async () => ({ok:true});
  let clicks = 0;
  const element = {
    isConnected: true, scrollIntoView() {}, focus() {},
    matches: s => s === '.mat-mdc-button-base,.mat-button-toggle-button',
    getAttribute: () => null, getBoundingClientRect:()=>({width:50,height:30}),
    click: () => { clicks++; },
    dispatchEvent: () => { throw new Error('Should not synthesize pointer events for Angular'); },
  };
  // Element instanceof is required by the shared visibility check.
  Object.setPrototypeOf(element, context.Element.prototype);
  await context.__DOCURANDOM_FLOW_TEST_HOOKS__.click(element, {cancelled:false}, 'settings');
  assert.equal(clicks, 1);
});

test('empty history needs a mounted project, explicit empty canvas, and no filtering or loading', () => {
  const {context, Element} = harness();
  context.document.readyState = 'complete';
  const project = new Element('project');
  const editor = new Element('');
  const settings = new Element('settings');
  const generate = new Element('arrow_forward');
  const message = new Element('Start creating or drop media');
  const search = new Element(''); search.value = '';
  let mounted = true, empty = true, busy = false;
  project.querySelectorAll = selector => {
    if (selector === 'button[aria-label="Settings trigger"]') return [settings];
    if (selector === 'button[aria-label="Start generation"]') return [generate];
    if (selector === 'h1,h2,h3,p,span,div') return empty ? [message] : [];
    if (selector.startsWith('input[type="search"]')) return [search];
    if (selector.startsWith('[aria-pressed=')) return busy ? [new Element('loading')] : [];
    return [];
  };
  context.document.querySelectorAll = selector => selector === 'flow-project-page' && mounted ? [project] : [];
  const verify = () => context.__DOCURANDOM_FLOW_TEST_HOOKS__.hasVerifiedEmptyProjectHistory(editor, 0);
  assert.equal(verify(), true);
  search.value = 'hidden results'; assert.equal(verify(), false); search.value = '';
  busy = true; assert.equal(verify(), false); busy = false;
  empty = false; assert.equal(verify(), false); empty = true;
  mounted = false; assert.equal(verify(), false); mounted = true;
  context.document.readyState = 'loading'; assert.equal(verify(), false);
});
