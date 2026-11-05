/**
 * DocuRandom Flow UI automation.
 *
 * This module deliberately drives the visible Google Flow interface. It does
 * not read cookies, capture credentials, call private generation endpoints, or
 * attempt to bypass CAPTCHA. Generated items therefore remain attached to the
 * Flow project visible in the signed-in account.
 */

// Keep the UI driver and background worker on one source of truth. A literal
// here previously drifted behind manifest.json, making every prompt fail with
// FLOW_UI_VERSION_MISMATCH even after the Flow tab reloaded.
(() => {
const DOCURANDOM_FLOW_UI_VERSION = chrome.runtime.getManifest().version;
const FLOW_SELECTORS = globalThis.__DOCURANDOM_FLOW_SELECTORS__ || {
  schemaVersion: 1,
  revision: 'fallback',
  promptEditors: [
    'div[role="textbox"][contenteditable="true"]',
    'div[role="textbox"][contenteditable="plaintext-only"]',
    'div[role="textbox"]',
    'textarea',
  ],
  outputTiles: '[data-tile-id]',
  openLayers: '[data-state="open"],[role="dialog"],[role="menu"]',
  modalLayers: '[role="dialog"],[aria-modal="true"]',
  menus: '[role="menu"]',
  horizontalMenuRows: ':scope > div[data-orientation="horizontal"]',
};

if (globalThis.__DOCURANDOM_FLOW_UI_VERSION__ !== DOCURANDOM_FLOW_UI_VERSION) {
  globalThis.__DOCURANDOM_FLOW_UI_VERSION__ = DOCURANDOM_FLOW_UI_VERSION;

  const jobs = new Map();
  const nativeSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const sleep = async (ms) => {
    // Hidden/minimized pages can have setTimeout clamped to many seconds. Ask
    // the extension worker to own the delay; the response wakes this isolated
    // world while leaving the user's active tab untouched.
    const response = await chrome.runtime.sendMessage({
      type: 'UI_BACKGROUND_SLEEP',
      delayMs: Math.max(0, Math.min(10000, Number(ms) || 0)),
    }).catch(() => null);
    if (response?.ok) return;
    await nativeSleep(ms);
  };
  const IMAGE_MODEL_LABELS = {
    gem_pix_2: ['Nano Banana Pro', 'Gemini 3 Pro Image'],
    harbor_seal: ['Nano Banana 2 Lite'],
  };
  const IMAGE_MODEL_NAMES = {
    gem_pix_2: 'Nano Banana Pro',
    harbor_seal: 'Nano Banana 2 Lite',
  };
  const REQUIRED_IMAGE_MODEL = 'harbor_seal';
  const REQUIRED_IMAGE_COUNT = 1;
  const PROJECT_UI_READY_TIMEOUT_MS = 45000;
  const REQUIRED_IMAGE_MODEL_NAME = IMAGE_MODEL_NAMES[REQUIRED_IMAGE_MODEL];
  const stabilizedUiProjects = new Set();

  function visible(element) {
    if (!(element instanceof Element)) return false;
    const style = getComputedStyle(element);
    const box = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && box.width > 0 && box.height > 0;
  }

  function enabled(element) {
    return Boolean(
      visible(element)
      && !element.matches?.(':disabled')
      && element.getAttribute?.('aria-disabled') !== 'true'
      && element.getAttribute?.('disabled') === null
    );
  }

  function all(selector, root = document) {
    try {
      return [...root.querySelectorAll(selector)].filter(visible);
    } catch {
      return [];
    }
  }

  function includingRoot(selector, root = document) {
    const matches = all(selector, root);
    if (root instanceof Element && root.matches(selector) && visible(root)) {
      matches.unshift(root);
    }
    return [...new Set(matches)];
  }

  function textOf(element) {
    return String(element?.innerText || element?.textContent || '').replace(/\s+/g, ' ').trim();
  }

  function buttonWithIcon(iconNames, root = document) {
    const wanted = new Set(iconNames.map((value) => String(value).trim().toLocaleLowerCase()));
    for (const button of all('button,[role="button"]', root).filter(enabled)) {
      const icons = [...button.querySelectorAll(
        'i,svg,use,[data-icon],span.material-symbols-outlined,span.material-icons',
      )];
      const values = [
        textOf(button),
        button.getAttribute('aria-label') || '',
        button.getAttribute('title') || '',
        button.getAttribute('data-tooltip') || '',
        ...icons.flatMap((icon) => [
          textOf(icon),
          icon.getAttribute('data-icon') || '',
          icon.getAttribute('aria-label') || '',
          icon.getAttribute('href') || '',
        ]),
      ].map((value) => String(value).trim().toLocaleLowerCase());
      if (values.some((value) => [...wanted].some((name) => value === name || value.includes(name)))) {
        return button;
      }
    }
    return null;
  }

  function buttonWithText(labels, root = document) {
    const wanted = labels.map((value) => String(value).toLocaleLowerCase());
    return all('button,[role="button"],[role="menuitem"]', root).filter(enabled).find((button) => {
      const value = textOf(button).toLocaleLowerCase();
      return wanted.some((label) => value === label || value.includes(label));
    }) || null;
  }

  function promptEditorValue(editor) {
    if (!editor) return '';
    if ('value' in editor) return String(editor.value || '').replace(/\s+/g, ' ').trim();
    return textOf(editor);
  }

  function normalizePrompt(value) {
    return String(value || '').replace(/\s+/g, ' ').trim();
  }

  function promptMatchesEditor(editor, prompt) {
    const actual = promptEditorValue(editor);
    const expected = normalizePrompt(prompt);
    if (!actual || !expected) return false;
    if (actual.includes(expected)) return true;
    // Very long prompts can be represented by several rich-text nodes. Verify
    // both ends after whitespace normalization rather than comparing a raw
    // prefix that may contain newlines from ChatGPT's exported prompt block.
    const head = expected.slice(0, Math.min(80, expected.length));
    const tail = expected.slice(Math.max(0, expected.length - 80));
    return actual.includes(head) && actual.includes(tail);
  }

  function findPromptEditor() {
    const topModal = all(FLOW_SELECTORS.modalLayers).at(-1) || null;
    const candidates = all(FLOW_SELECTORS.promptEditors.join(','))
      .filter(enabled)
      .filter((element) => !element.closest('[aria-hidden="true"]'))
      .filter((element) => !topModal || topModal.contains(element))
      .filter((element) => {
        if (element.getAttribute('aria-readonly') === 'true') return false;
        if (element instanceof HTMLTextAreaElement) return !element.readOnly;
        const editable = element.getAttribute('contenteditable');
        return element.isContentEditable
          || editable === 'true'
          || editable === 'plaintext-only';
      });
    return candidates.sort((left, right) => {
      const leftBox = left.getBoundingClientRect();
      const rightBox = right.getBoundingClientRect();
      const score = (element, box) => {
        let value = box.bottom * 1000 + box.width * box.height;
        if (element.getAttribute('role') === 'textbox') value += 100000;
        if (element.getAttribute('aria-multiline') === 'true') value += 50000;
        if (element.closest('form')) value += 25000;
        // Semantic proximity to a real control is stable across languages,
        // window sizes and browser zoom levels.
        let parent = element.parentElement;
        for (let depth = 0; parent && depth < 5; depth += 1) {
          if (all('button[type="submit"],button', parent).filter(enabled).length) {
            value += 20000 - depth * 2000;
            break;
          }
          parent = parent.parentElement;
        }
        return value;
      };
      const leftScore = score(left, leftBox);
      const rightScore = score(right, rightBox);
      return rightScore - leftScore;
    })[0] || null;
  }

  function findSubmitButton(editor) {
    const labels = [
      'send', 'submit', 'generate', 'create', 'run',
      'gửi', 'tạo', '生成', '送信',
    ];
    const roots = [];
    let current = editor;
    for (let depth = 0; current && depth < 5; depth += 1) {
      current = current.parentElement;
      if (current) roots.push(current);
    }
    for (const root of roots) {
      const iconButton = buttonWithIcon(
        ['arrow_forward', 'arrow_upward', 'send', 'north', 'play_arrow'],
        root,
      );
      if (iconButton) return iconButton;
      const submit = all('button[type="submit"],[role="button"]', root)
        .filter(enabled)
        .find((button) => {
          const value = [
            textOf(button),
            button.getAttribute('aria-label') || '',
            button.getAttribute('title') || '',
            button.getAttribute('data-tooltip') || '',
          ].join(' ').toLocaleLowerCase();
          return labels.some((label) => value.includes(label));
        });
      if (submit) return submit;
    }
    return null;
  }

  function findComposerControl(editor, iconNames) {
    let current = editor;
    for (let depth = 0; current && depth < 5; depth += 1) {
      current = current.parentElement;
      if (!current) break;
      const control = buttonWithIcon(iconNames, current);
      if (control) return control;
    }
    return null;
  }

  function submissionStarted(editor, prompt, baseline, submitBefore = null) {
    const expected = normalizePrompt(prompt);
    const prefix = expected.slice(0, Math.min(80, expected.length));
    const editorText = promptEditorValue(editor);
    if (buttonWithIcon(['stop'])) return true;
    if (all(FLOW_SELECTORS.outputTiles).some((tile) => {
      const id = tileIdentity(tile);
      return id && !baseline.has(id);
    })) return true;
    // Clearing the composer alone is not proof of submission: React can replace
    // or reset a controlled editor after a failed synthetic input. Only accept
    // it together with a real submit-control state transition.
    const promptCleared = Boolean(prefix && !editorText.includes(prefix));
    const submitTransitioned = Boolean(
      submitBefore
      && (!submitBefore.isConnected || !enabled(submitBefore)),
    );
    return promptCleared && submitTransitioned;
  }

  async function waitForSubmissionStart(
    editor,
    prompt,
    baseline,
    submitBefore,
    job,
    timeoutMs,
  ) {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      if (job.cancelled) throw new Error('CANCELLED');
      const activeEditor = findPromptEditor() || editor;
      if (submissionStarted(activeEditor, prompt, baseline, submitBefore)) return true;
      await sleep(150);
    }
    return false;
  }

  async function waitFor(getter, timeoutMs, job, description) {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      if (job.cancelled) throw new Error('CANCELLED');
      while (job.paused && !job.cancelled) await sleep(250);
      const value = getter();
      if (value) return value;
      await sleep(250);
    }
    throw new Error(`FLOW_UI_TIMEOUT:${description}`);
  }

  function emit(job, progress, message, phase = 'running') {
    chrome.runtime.sendMessage({
      type: 'UI_AUTOMATION_PROGRESS',
      jobId: job.id,
      progress: Math.max(0, Math.min(1, Number(progress) || 0)),
      phase,
      message,
      projectId: projectIdFromLocation(),
      projectUrl: String(location.href || '').slice(0, 1000),
    }).catch(() => {});
  }

  async function click(element, job, label) {
    if (!element) throw new Error(`FLOW_UI_ELEMENT_NOT_FOUND:${label}`);
    if (job.cancelled) throw new Error('CANCELLED');
    element.scrollIntoView({ block: 'center', inline: 'center' });
    await sleep(120);
    if (job.cancelled) throw new Error('CANCELLED');
    if (!element.isConnected || !enabled(element)) {
      throw new Error(`FLOW_UI_ELEMENT_CHANGED:${label}`);
    }
    try { element.focus({ preventScroll: true }); } catch { element.focus?.(); }
    if (element.matches('.mat-mdc-button-base,.mat-button-toggle-button')) {
      // Angular's trigger already handles click. The legacy Radix pointer
      // sequence can toggle its overlay twice (open then immediately close).
      element.click();
      await sleep(350);
      return;
    }
    element.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
    element.dispatchEvent(new MouseEvent('mousemove', { bubbles: true }));
    // Current Flow uses a Radix-style composite settings trigger whose open
    // handler may listen on pointerdown rather than click. Dispatch the full
    // semantic activation sequence on the located element itself; no viewport
    // or screen coordinate is involved.
    element.dispatchEvent(new PointerEvent('pointerdown', {
      bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true,
    }));
    element.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
    element.dispatchEvent(new PointerEvent('pointerup', {
      bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true,
    }));
    element.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
    // Invoke the semantically discovered DOM control itself. No screen or
    // viewport coordinates are used, so minimized/resized windows, zoom and
    // antidetect profile dimensions cannot move the target.
    element.click();
    await sleep(350);
  }

  function editorOwnsFocus(editor) {
    const active = document.activeElement;
    return Boolean(active && (active === editor || editor.contains(active)));
  }

  async function focusPromptEditor(editor, job) {
    let current = editor;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      if (job.cancelled) throw new Error('CANCELLED');
      if (!current?.isConnected || !enabled(current)) current = findPromptEditor();
      if (!current) break;

      current.scrollIntoView({ block: 'center', inline: 'nearest' });
      await sleep(180);
      // Never use a screen coordinate to focus the composer. If Flow shifts a
      // thumbnail into the old position during a React reflow, a coordinate
      // click would open the image viewer and the prompt would be lost.
      try { current.focus({ preventScroll: true }); } catch { current.focus(); }
      await sleep(220);
      if (current.isConnected && editorOwnsFocus(current)) return current;

      // Flow can replace the contenteditable node while settling its layout.
      // Re-acquire the live node before the next attempt.
      current = findPromptEditor();
    }
    throw new Error('FLOW_UI_PROMPT_FOCUS_FAILED');
  }

  async function recoverPromptEditor(job) {
    let editor = findPromptEditor();
    if (editor || !projectIdFromLocation()) return editor;

    emit(job, 0.165, 'Flow đang che ô nhập · tự đóng màn hình xem ảnh và khôi phục…', 'recover-ui');
    for (let attempt = 0; attempt < 2; attempt += 1) {
      await chrome.runtime.sendMessage({ type: 'UI_TRUSTED_PRESS_ESCAPE' }).catch(() => null);
      await sleep(650);
      editor = findPromptEditor();
      if (editor) return editor;

      // Some viewer revisions consume Escape. Close only a visible modal's own
      // close button, never a page-level button or media tile.
      const overlays = all(FLOW_SELECTORS.modalLayers);
      const overlay = overlays.at(-1);
      const close = overlay ? buttonWithIcon(['close'], overlay) : null;
      if (close) {
        close.click();
        await sleep(650);
        editor = findPromptEditor();
        if (editor) return editor;
      }
    }
    return null;
  }

  function hasVerifiedEmptyProjectHistory(editor, tileCount) {
    if (!editor || tileCount !== 0 || !projectIdFromLocation()
      || document.readyState !== 'complete' || buttonWithIcon(['stop'])) return false;
    const project = all('flow-project-page')[0];
    if (!project || !all('button[aria-label="Settings trigger"]', project).length
      || !all('button[aria-label="Start generation"]', project).length) return false;
    // A mounted composer alone can also be a loading shell or a filtered grid.
    // Require Flow's explicit empty-canvas message and no loading/filter state.
    const emptyMessage = all('h1,h2,h3,p,span,div', project).some(element =>
      textOf(element) === 'Start creating or drop media');
    if (!emptyMessage) return false;
    if (all('input[type="search"],flow-search-bar input', project)
      .some(input => String(input.value || '').trim())) return false;
    if (all('[aria-pressed="true"],.mat-mdc-chip-selected,[role="alert"],'
      + '[role="progressbar"],mat-progress-spinner,mat-spinner,[aria-busy="true"]', project).length) return false;
    return true;
  }

  function projectSurfaceState() {
    const editor = findPromptEditor();
    const tiles = all(FLOW_SELECTORS.outputTiles);
    return {
      projectId: projectIdFromLocation(),
      url: String(location.href || ''),
      path: String(location.pathname || ''),
      readyState: String(document.readyState || ''),
      visibility: String(document.visibilityState || ''),
      hasPromptEditor: Boolean(editor),
      tileCount: tiles.length,
      hasSettingsControl: Boolean(buttonWithIcon(['settings_2'])),
      emptyHistoryVerified: hasVerifiedEmptyProjectHistory(editor, tiles.length),
    };
  }

  async function waitForProjectSurface(
    job,
    { requireHistory = false, timeoutMs = PROJECT_UI_READY_TIMEOUT_MS } = {},
  ) {
    const started = Date.now();
    let emptySince = null;
    let lastState = projectSurfaceState();
    while (Date.now() - started < timeoutMs) {
      if (job.cancelled) throw new Error('CANCELLED');
      lastState = projectSurfaceState();
      const hasUsableSurface = lastState.hasPromptEditor || lastState.tileCount > 0;
      emptySince = lastState.emptyHistoryVerified ? (emptySince ?? Date.now()) : null;
      const stableEmptyHistory = emptySince !== null && Date.now() - emptySince >= 1000;
      const hasRequiredHistory = !requireHistory || lastState.tileCount > 0 || stableEmptyHistory;
      if (hasUsableSurface && hasRequiredHistory) return lastState;
      await sleep(250);
    }
    throw new Error(`FLOW_UI_PROJECT_NOT_READY:${JSON.stringify(lastState)}`);
  }

  function menuRowWithIcon(iconName, menu) {
    const wanted = String(iconName || '').toLocaleLowerCase();
    return all(':scope > div', menu).find((row) => {
      const values = [...row.querySelectorAll('i,span.material-symbols-outlined,span.material-icons')]
        .map((icon) => textOf(icon).toLocaleLowerCase());
      return values.some((value) => value === wanted || value.includes(wanted));
    }) || null;
  }

  async function stabilizeFlowWorkspaceUi(job) {
    const projectId = projectIdFromLocation();
    if (!projectId || stabilizedUiProjects.has(projectId)) return;

    const settings = buttonWithIcon(['settings_2']);
    if (!settings) return;
    emit(job, 0.085, 'Đang ổn định bố cục Flow cho hàng đợi ảnh…', 'configure-ui');
    try {
      await click(settings, job, 'flow-ui-settings');
      const menu = await waitFor(
        () => all(FLOW_SELECTORS.menus).at(-1) || null,
        2500,
        job,
        'flow-ui-menu',
      ).catch(() => null);
      if (!menu) return;

      // Equivalent to Flow Automation's one-time UI preconfiguration: grid
      // layout, stable tile size, visible prompt text and clear-after-submit.
      for (const [index, label] of [['0', 'grid-layout'], ['1', 'grid-size']]) {
        const liveMenu = all(FLOW_SELECTORS.menus).at(-1);
        const horizontalRows = liveMenu
          ? all(FLOW_SELECTORS.horizontalMenuRows, liveMenu)
          : [];
        const row = horizontalRows[Number(index)];
        const option = row ? all('button', row)[0] : null;
        if (option) await click(option, job, label);
      }
      for (const [icon, label] of [['visibility', 'show-prompt-text'], ['ink_eraser', 'clear-old-prompt']]) {
        const liveMenu = all(FLOW_SELECTORS.menus).at(-1);
        const row = liveMenu ? menuRowWithIcon(icon, liveMenu) : null;
        const option = row ? all('button', row)[1] : null;
        if (option) await click(option, job, label);
      }

      const close = buttonWithIcon(['settings_2']);
      if (close) await click(close, job, 'close-flow-ui-settings');
      await sleep(500);
      stabilizedUiProjects.add(projectId);
    } catch {
      // This is a layout hardening step, not a generation requirement. Keep the
      // user's current Flow settings when a future UI revision hides an option.
      const close = buttonWithIcon(['settings_2']);
      if (close?.getAttribute('aria-expanded') === 'true') close.click();
    }
  }

  async function randomizedComposerSettle(job, baseMs = 1000) {
    if (job.cancelled) throw new Error('CANCELLED');
    const delay = baseMs * (0.6 + Math.random() * 0.8);
    await sleep(delay);
    if (job.cancelled) throw new Error('CANCELLED');
  }

  function projectIdFromLocation() {
    const decode = (value) => {
      try { return decodeURIComponent(String(value || '').trim()); } catch { return String(value || '').trim(); }
    };
    const fromPath = (value) => {
      const match = String(value || '').match(/\/(?:projects?)\/([^/?#]+)/i);
      return match ? decode(match[1]) : '';
    };
    const fromQuery = (value) => {
      const parts = String(value || '').replace(/^#\?/, '').replace(/^#/, '').split('&');
      for (const key of ['project', 'projectId', 'project_id']) {
        const found = parts
          .map((part) => part.split('='))
          .find((part) => part[0] === key);
        if (found?.[1]) return decode(found[1]);
      }
      return '';
    };
    try {
      const parsed = new URL(String(location.href || ''));
      const pathId = fromPath(parsed.pathname);
      if (pathId) return pathId;
      for (const key of ['project', 'projectId', 'project_id']) {
        const queryId = decode(parsed.searchParams.get(key));
        if (queryId) return queryId;
      }
      const hashId = fromPath(parsed.hash) || fromQuery(parsed.hash);
      if (hashId) return hashId;
    } catch { /* the test harness may expose only pathname */ }
    const pathnameId = fromPath(location.pathname);
    if (pathnameId) return pathnameId;
    const attrRoot = [document.documentElement, document.body];
    for (const root of attrRoot) {
      for (const name of ['data-project-id', 'data-projectid', 'data-project']) {
        const value = root?.getAttribute?.(name);
        if (value) return decode(value);
      }
    }
    return '';
  }

  async function ensureProject(job, request = {}) {
    if (projectIdFromLocation() && request.newProject !== true) {
      // A completed navigation only proves that the document shell loaded. A
      // project route can still be hydrating for several seconds, especially
      // in a minimized Edge window. Do not enter configure/submit while the
      // composer and history surface are still absent.
      await waitForProjectSurface(job);
      return;
    }
    if (projectIdFromLocation() && request.newProject === true) {
      // A tabs.update navigation destroys this isolated world and its pending
      // generate response port. The worker must prepare the new project before
      // issuing any generation command; never navigate from inside that port.
      throw new Error('FLOW_UI_HOME_REQUIRED_UNSENT');
    }
    emit(job, 0.04, 'Đang tạo dự án mới trên Google Flow…', 'project');
    const create = await waitFor(findCreateProjectButton, 45000, job, 'create-project');
    await click(create, job, 'create-project');
    await waitFor(() => projectIdFromLocation(), 60000, job, 'project-page');
    emit(job, 0.08, 'Đã mở dự án Flow.', 'project');
  }

  function findCreateProjectButton() {
    // Flow's Angular home (flow.google.com) uses mat-icon "add", not
    // React's old "add_2". Match the project action, never a generic add tile.
    return all('button.new-project-button,button[data-testid="new-project"]')
      .find(enabled)
      || buttonWithText(['Dự án mới', 'New project', 'Create project', '新しいプロジェクト'])
      || buttonWithIcon(['add_2']);
  }

  function openConfigurationRoot() {
    return all(FLOW_SELECTORS.openLayers).at(-1) || document;
  }

  function selectedStateOnNode(element) {
    if (!(element instanceof Element)) return false;
    if (element instanceof HTMLInputElement && element.checked) return true;
    if (element.matches?.(':checked')) return true;
    if (['aria-checked', 'aria-pressed', 'aria-selected'].some(
      (name) => element.getAttribute(name) === 'true',
    )) return true;
    const ariaCurrent = String(element.getAttribute('aria-current') || '').toLocaleLowerCase();
    if (ariaCurrent && ariaCurrent !== 'false') return true;
    if (['on', 'checked', 'active', 'selected'].includes(
      String(element.getAttribute('data-state') || '').toLocaleLowerCase(),
    )) return true;
    if (element.getAttribute('data-selected') === 'true') return true;
    if ([...element.classList].some(
      (name) => /(^|[-_])(active|checked|selected)($|[-_])/i.test(name),
    )) return true;
    return false;
  }

  function controlIsSelected(element) {
    if (!element) return false;
    if (selectedStateOnNode(element)) return true;
    const semanticDescendants = element.querySelectorAll?.(
      'input[type="radio"],input[type="checkbox"],[aria-checked],[aria-pressed],'
      + '[aria-selected],[aria-current],[data-state],[data-selected]',
    ) || [];
    if ([...semanticDescendants].some(selectedStateOnNode)) return true;

    // Flow has used both a checked native radio and a check/done glyph across
    // UI revisions. Read either semantic representation; never infer selection
    // from color or screen coordinates.
    const selectedIcon = [...(element.querySelectorAll?.(
      'i,svg,use,[data-icon],span.material-symbols-outlined,span.material-icons',
    ) || [])].some((icon) => {
      const values = [
        textOf(icon),
        icon.getAttribute('data-icon') || '',
        icon.getAttribute('aria-label') || '',
        icon.getAttribute('href') || '',
      ].map((value) => String(value).trim().toLocaleLowerCase());
      return values.some((value) => ['check', 'done', 'check_circle'].includes(value));
    });
    if (selectedIcon) return true;

    // Radix-style controls can put state on the label/wrapper rather than the
    // clickable child. Limit this lookup to two actionable ancestors so an
    // unrelated open dialog cannot be mistaken for a selected option.
    let parent = element.parentElement;
    for (let depth = 0; parent && depth < 2; depth += 1, parent = parent.parentElement) {
      if (parent.matches('label,button,[role="radio"],[role="tab"],[role="option"],[data-state]')
          && selectedStateOnNode(parent)) return true;
    }
    return false;
  }

  function imageCountValue(element) {
    if (!element) return 0;
    const exactValues = [
      element.getAttribute?.('data-value'),
      element.getAttribute?.('value'),
      textOf(element),
    ];
    for (const raw of exactValues) {
      const value = String(raw || '').trim();
      if (/^[1-4]$/.test(value)) return Number(value);
      const compact = value.match(/^(?:x\s*([1-4])|([1-4])\s*x)$/i);
      if (compact) return Number(compact[1] || compact[2]);
    }
    const descriptions = [
      element.getAttribute?.('aria-label'),
      element.getAttribute?.('title'),
      element.getAttribute?.('data-tooltip'),
    ];
    for (const raw of descriptions) {
      const value = String(raw || '').trim().toLocaleLowerCase();
      const match = value.match(/(?:^|\D)([1-4])\s*(?:image|images|output|outputs|generation|generations|ảnh)(?:\D|$)/i);
      if (match) return Number(match[1]);
    }
    return 0;
  }

  function imageCountControls(root) {
    return includingRoot(
      'button,[role="tab"],[role="radio"],label,input[type="radio"]',
      root,
    ).filter((element) => imageCountValue(element) > 0);
  }

  function rovingControlIsSelected(element, controls) {
    if (element.getAttribute?.('tabindex') !== '0') return false;
    const siblings = controls.filter((candidate) => candidate !== element
      && candidate.parentElement === element.parentElement);
    return siblings.length > 0
      && siblings.some((candidate) => candidate.getAttribute?.('tabindex') === '-1');
  }

  function selectedImageCount(root, preferred = null) {
    const controls = imageCountControls(root);
    if (preferred && preferred.isConnected && !controls.includes(preferred)) {
      controls.unshift(preferred);
    }
    const selected = controls.find((control) => (
      controlIsSelected(control) || rovingControlIsSelected(control, controls)
    ));
    return selected ? imageCountValue(selected) : 0;
  }

  function imageCountDiagnostics(root) {
    const candidates = includingRoot(
      'button,[role="button"],[role="tab"],[role="radio"],label,input[type="radio"]',
      root,
    ).map((element) => ({
      tag: element.tagName.toLocaleLowerCase(),
      role: element.getAttribute('role') || '',
      text: textOf(element).slice(0, 48),
      ariaLabel: String(element.getAttribute('aria-label') || '').slice(0, 48),
      ariaChecked: element.getAttribute('aria-checked') || '',
      ariaPressed: element.getAttribute('aria-pressed') || '',
      ariaSelected: element.getAttribute('aria-selected') || '',
      dataState: element.getAttribute('data-state') || '',
      dataValue: element.getAttribute('data-value') || '',
      value: String(element.getAttribute('value') || '').slice(0, 24),
      tabIndex: element.getAttribute('tabindex') || '',
      checked: element instanceof HTMLInputElement ? element.checked : false,
    })).filter((item) => [
      item.text, item.ariaLabel, item.dataValue, item.value,
    ].some((value) => /[1-4]/.test(value))).slice(-20);
    return JSON.stringify({
      rootTag: root instanceof Element ? root.tagName.toLocaleLowerCase() : 'document',
      rootRole: root instanceof Element ? root.getAttribute('role') || '' : '',
      candidateCount: candidates.length,
      candidates,
    });
  }

  function findDisplayedModelControl(_root = document) {
    // Model options render in a portal outside the configuration trigger. Read
    // the real dropdown from the whole document so an open menu option can
    // never be mistaken for the currently selected model.
    const candidates = all(
      'button[aria-haspopup="menu"],[role="button"][aria-haspopup="menu"],[role="combobox"]',
      document,
    )
      .filter((element) => !['menuitem', 'option'].includes(element.getAttribute('role') || ''))
      .filter((element) => {
        const value = textOf(element).toLocaleLowerCase();
        // "Nano Banana 2", "Nano Banana 2 Lite" and "Nano Banana Pro"
        // are three distinct current Flow options. The generic text identifies
        // the dropdown control only; modelControlIsRequired performs the exact
        // Lite verification below.
        return value.includes('nano banana') || value.includes('gemini 3 pro image');
      });
    const score = (element) => {
      const value = textOf(element).toLocaleLowerCase();
      const popup = String(element.getAttribute('aria-haspopup') || '').toLocaleLowerCase();
      let total = 0;
      if (element.getAttribute('role') === 'combobox') total += 100;
      if (['listbox', 'menu'].includes(popup)) total += 90;
      if (value.includes('arrow_drop_down') || value.includes('expand_more')) total += 80;
      if (element.querySelector(
        '[data-icon="arrow_drop_down"],[data-icon="expand_more"],'
        + 'span.material-symbols-outlined,span.material-icons',
      )) {
        const icons = [...element.querySelectorAll(
          '[data-icon],span.material-symbols-outlined,span.material-icons',
        )].map((icon) => textOf(icon).toLocaleLowerCase());
        if (icons.some((icon) => ['arrow_drop_down', 'expand_more'].includes(icon))) total += 80;
      }
      // The outer composer trigger includes aspect and output-count summaries;
      // it is not the nested model dropdown even though it repeats the model.
      if (/crop_[0-9]|(?:^|\s)x[1-4](?:\s|$)/i.test(value)) total -= 150;
      return total;
    };
    return candidates.sort((left, right) => score(right) - score(left))[0] || null;
  }

  function modelControlIsRequired(element) {
    const value = textOf(element).toLocaleLowerCase();
    return value.includes('nano banana 2 lite')
      && !/(^|\s)pro($|\s)/i.test(value);
  }

  function imageModelDiagnostics(root) {
    const candidates = includingRoot(
      'button,[role="button"],[role="combobox"],[role="menuitem"],[role="option"]',
      root,
    ).map((element) => ({
      tag: element.tagName.toLocaleLowerCase(),
      role: element.getAttribute('role') || '',
      text: textOf(element).slice(0, 72),
      ariaLabel: String(element.getAttribute('aria-label') || '').slice(0, 48),
      ariaExpanded: element.getAttribute('aria-expanded') || '',
      ariaSelected: element.getAttribute('aria-selected') || '',
      dataState: element.getAttribute('data-state') || '',
    })).filter((item) => /nano banana|gemini 3 pro image/i.test(
      `${item.text} ${item.ariaLabel}`,
    )).slice(-12);
    return JSON.stringify({
      rootTag: root instanceof Element ? root.tagName.toLocaleLowerCase() : 'document',
      candidateCount: candidates.length,
      candidates,
    });
  }

  function findRequiredModelOption() {
    return all('button,[role="menuitem"],[role="option"]', document)
      .filter((element) => !element.hasAttribute('aria-haspopup'))
      .find((element) => textOf(element).toLocaleLowerCase().includes(
        'nano banana 2 lite',
      )) || null;
  }

  async function activateWithTrustedEnter(element, job, label) {
    if (!element?.isConnected || !enabled(element)) {
      throw new Error(`FLOW_UI_ELEMENT_CHANGED:${label}`);
    }
    element.scrollIntoView({ block: 'center', inline: 'center' });
    try { element.focus({ preventScroll: true }); } catch { element.focus?.(); }
    await sleep(150);
    await chrome.runtime.sendMessage({ type: 'UI_TRUSTED_PRESS_ENTER' }).catch(() => null);
    await sleep(350);
    if (job.cancelled) throw new Error('CANCELLED');
  }

  async function openRequiredModelOption(job, label = 'required-model-option') {
    let modelControl = findDisplayedModelControl(document);
    if (!modelControl) return null;

    // Prefer activating the semantically discovered DOM control itself. This
    // does not depend on the browser window being foreground/visible and keeps
    // working when Edge is minimized or the user is working in another tab.
    // Trusted keyboard input remains a fallback for Flow builds whose Radix
    // trigger ignores a programmatic pointer/click sequence.
    if (modelControl.getAttribute('aria-expanded') !== 'true') {
      await click(modelControl, job, `${label}-dom-open`);
    }
    let modelOption = await waitFor(
      () => findRequiredModelOption(),
      3500,
      job,
      `${label}-dom-visible`,
    ).catch(() => null);
    if (modelOption) return modelOption;

    modelControl = findDisplayedModelControl(document);
    if (!modelControl) return null;
    // If a stale/partial portal says it is open but exposes no model options,
    // close it before the trusted retry so Enter cannot accidentally toggle it
    // shut or select a different focused item.
    if (modelControl.getAttribute('aria-expanded') === 'true') {
      await chrome.runtime.sendMessage({ type: 'UI_TRUSTED_PRESS_ESCAPE' }).catch(() => null);
      await sleep(500);
      modelControl = findDisplayedModelControl(document);
    }
    if (!modelControl) return null;
    await activateWithTrustedEnter(modelControl, job, `${label}-trusted-open`);
    modelOption = await waitFor(
      () => findRequiredModelOption(),
      5000,
      job,
      `${label}-trusted-visible`,
    ).catch(() => null);
    return modelOption;
  }

  async function selectRequiredModelOption(modelOption, job) {
    // DOM activation is stable for background/minimized tabs. Verify the live
    // dropdown text before considering it selected; the keyboard fallback is
    // used only if the first activation was ignored by Flow.
    await click(modelOption, job, 'required-model-option-dom');
    let confirmed = await waitFor(() => {
      const liveControl = findDisplayedModelControl(document);
      return modelControlIsRequired(liveControl) ? liveControl : null;
    }, 3500, job, 'verify-image-model-dom').catch(() => null);
    if (confirmed) return confirmed;

    const liveOption = await openRequiredModelOption(job, 'required-model-option-retry');
    if (!liveOption) return null;
    await activateWithTrustedEnter(liveOption, job, 'required-model-option-trusted');
    confirmed = await waitFor(() => {
      const liveControl = findDisplayedModelControl(document);
      return modelControlIsRequired(liveControl) ? liveControl : null;
    }, 5000, job, 'verify-image-model-trusted').catch(() => null);
    return confirmed;
  }

  async function configureImage(job, options) {
    emit(job, 0.1, 'Đang khóa Flow ở Nano Banana 2 Lite · 1 ảnh…', 'configure');
    // A project URL can arrive before Angular has mounted its composer.
    // Wait for the actual controls, not merely the completed navigation.
    let promptEditor = await recoverPromptEditor(job);
    const configButton = await waitFor(() => {
      promptEditor = findPromptEditor();
      return promptEditor ? findComposerControl(promptEditor, ['crop', 'tune']) : null;
    }, 20000, job, 'image-settings-ready').catch(() => null);
    if (!configButton) {
      const controls = all('button,[role="button"]')
        .map((element) => ({text: textOf(element).slice(0, 90),
          label: String(element.getAttribute('aria-label') || '').slice(0, 60)})).slice(-25);
      throw new Error(`FLOW_UI_REQUIRED_IMAGE_CONFIG_NOT_CONFIRMED:${JSON.stringify({
        hasEditor: Boolean(promptEditor), path: location.pathname, controls,
      })}`);
    }

    await click(configButton, job, 'image-settings');
    let root = await waitFor(() => {
      const candidate = openConfigurationRoot();
      if (candidate !== document) return candidate;
      return imageCountControls(document).length >= 2 ? document : null;
    }, 1500, job, 'image-settings-open').catch(() => null);
    if (!root) {
      // React currently ignores HTMLElement.click() on the composite Flow
      // settings trigger in some Edge profiles. Focus the semantically located
      // button and activate it with a trusted Enter key; this remains stable
      // across window position/zoom and never relies on screen coordinates.
      try { configButton.focus({ preventScroll: true }); } catch { configButton.focus?.(); }
      await chrome.runtime.sendMessage({ type: 'UI_TRUSTED_PRESS_ENTER' }).catch(() => null);
      root = await waitFor(() => {
        const candidate = openConfigurationRoot();
        if (candidate !== document) return candidate;
        return imageCountControls(document).length >= 2 ? document : null;
      }, 3000, job, 'image-settings-open-trusted').catch(() => null);
    }
    if (!root) {
      throw new Error(`FLOW_UI_REQUIRED_IMAGE_CONFIG_NOT_CONFIRMED:${imageCountDiagnostics(document)}`);
    }
    const configurationRoot = root;

    const angularImageMode = all('button[role="radio"]', root).find(
      (element) => textOf(element.querySelector('mat-icon')) === 'image',
    );
    if (angularImageMode && angularImageMode.getAttribute('aria-checked') !== 'true') {
      await click(angularImageMode, job, 'text-to-image-mode');
      await waitFor(() => angularImageMode.getAttribute('aria-checked') === 'true',
        5000, job, 'verify-image-mode');
      root = configurationRoot.isConnected ? configurationRoot : openConfigurationRoot();
    }

    // The first tab in Flow's first tab list is the image mode. This mirrors
    // the visible UI rather than relying on translated labels.
    const tabLists = all('[role="tablist"]', root);
    if (tabLists.length) {
      const imageTab = all('button,[role="tab"]', tabLists[0])[0];
      if (imageTab && imageTab.getAttribute('aria-selected') !== 'true') {
        await click(imageTab, job, 'text-to-image-mode');
        root = configurationRoot.isConnected ? configurationRoot : openConfigurationRoot();
      }
    }

    const aspectIcons = {
      landscape: ['16_9'],
      portrait: ['9_16'],
      square: ['1_1'],
      '4x3': ['4_3'],
      '3x4': ['3_4'],
    };
    const aspectButton = buttonWithIcon(aspectIcons[options.aspect] || aspectIcons.landscape, root);
    if (aspectButton) await click(aspectButton, job, `aspect-${options.aspect}`);

    const countButton = imageCountControls(root)
      .filter((button) => imageCountValue(button) === REQUIRED_IMAGE_COUNT)
      .at(-1) || null;
    if (!countButton) {
      throw new Error(`FLOW_UI_IMAGE_COUNT_NOT_CONFIRMED:${imageCountDiagnostics(root)}`);
    }
    await click(countButton, job, `count-${REQUIRED_IMAGE_COUNT}`);
    const countConfirmed = await waitFor(() => {
      const liveRoot = configurationRoot.isConnected ? configurationRoot : openConfigurationRoot();
      return selectedImageCount(liveRoot, countButton) === REQUIRED_IMAGE_COUNT ? true : null;
    }, 5000, job, 'verify-image-count').catch(() => null);
    if (!countConfirmed) {
      const liveRoot = configurationRoot.isConnected ? configurationRoot : openConfigurationRoot();
      throw new Error(`FLOW_UI_IMAGE_COUNT_NOT_CONFIRMED:${imageCountDiagnostics(liveRoot)}`);
    }

    root = configurationRoot.isConnected ? configurationRoot : openConfigurationRoot();
    let modelControl = findDisplayedModelControl(document);
    if (!modelControl) {
      throw new Error(`FLOW_UI_IMAGE_MODEL_NOT_CONFIRMED:${imageModelDiagnostics(document)}`);
    }
    let modelConfirmed = modelControlIsRequired(modelControl) ? modelControl : null;
    if (!modelControlIsRequired(modelControl)) {
      const modelOption = await openRequiredModelOption(job);
      if (!modelOption) {
        throw new Error(`FLOW_UI_IMAGE_MODEL_NOT_CONFIRMED:${imageModelDiagnostics(document)}`);
      }
      modelConfirmed = await selectRequiredModelOption(modelOption, job);
    }
    if (!modelConfirmed) {
      throw new Error(`FLOW_UI_IMAGE_MODEL_NOT_CONFIRMED:${imageModelDiagnostics(document)}`);
    }

    const livePromptEditor = findPromptEditor() || promptEditor;
    const closeButton = configButton.isConnected
      ? configButton
      : findComposerControl(livePromptEditor, ['crop', 'tune']);
    if (closeButton) await click(closeButton, job, 'close-image-settings');
    emit(job, 0.16, `Đã xác nhận ${REQUIRED_IMAGE_MODEL_NAME} · đúng 1 ảnh.`, 'configure');
    return {
      model: REQUIRED_IMAGE_MODEL,
      modelName: REQUIRED_IMAGE_MODEL_NAME,
      observedModelText: textOf(modelConfirmed),
      imageCount: REQUIRED_IMAGE_COUNT,
      verified: true,
    };
  }

  function tileIds() {
    return new Set(all(FLOW_SELECTORS.outputTiles).map(tileIdentity).filter(Boolean));
  }

  function tileIdentity(tile) {
    return tile.getAttribute('data-tile-id') || tile.getAttribute('data-media-id') || '';
  }

  function chooseGeneratedImage(tile) {
    const candidates = (tile.matches('img') ? [tile] : [...tile.querySelectorAll('img')])
      .filter((image) => {
        const source = image.currentSrc || image.src || '';
        return source.startsWith('http') && (image.naturalWidth >= 256 || image.width >= 256);
      })
      .sort((left, right) => (right.naturalWidth * right.naturalHeight) - (left.naturalWidth * left.naturalHeight));
    const image = candidates[0];
    if (!image) return null;
    return {
      tileId: tileIdentity(tile),
      url: image.currentSrc || image.src,
      width: image.naturalWidth || image.width || 0,
      height: image.naturalHeight || image.height || 0,
    };
  }

  const PROJECT_SNAPSHOT_MAX_TILES = 120;
  const PROJECT_SNAPSHOT_MAX_TEXT = 2000;

  function boundedSnapshotText(value, limit = PROJECT_SNAPSHOT_MAX_TEXT) {
    return String(value || '').replace(/\s+/g, ' ').trim().slice(0, limit);
  }

  function tileSearchText(tile) {
    const values = [
      textOf(tile),
      tile.getAttribute?.('aria-label') || '',
      tile.getAttribute?.('title') || '',
      tile.getAttribute?.('data-prompt') || '',
      tile.getAttribute?.('data-query') || '',
      tile.getAttribute?.('data-description') || '',
    ];
    const descendants = tile.querySelectorAll?.(
      '[aria-label],[title],[alt],[data-prompt],[data-query],[data-description]',
    ) || [];
    for (const element of descendants) {
      values.push(
        element.getAttribute?.('aria-label') || '',
        element.getAttribute?.('title') || '',
        element.getAttribute?.('alt') || '',
        element.getAttribute?.('data-prompt') || '',
        element.getAttribute?.('data-query') || '',
        element.getAttribute?.('data-description') || '',
      );
    }
    return normalizePrompt(values.filter(Boolean).join(' '));
  }

  function snapshotDebugTile(tile) {
    const ancestors = [];
    let current = tile;
    for (let depth = 0; current && depth < 5; depth += 1, current = current.parentElement) {
      const attributes = {};
      for (const name of [
        'id', 'class', 'role', 'aria-label', 'title', 'data-tile-id', 'data-media-id',
        'data-prompt', 'data-query', 'data-description', 'data-testid',
      ]) {
        const value = current.getAttribute?.(name);
        if (value) attributes[name] = boundedSnapshotText(value, 300);
      }
      ancestors.push({
        depth,
        tag: String(current.tagName || '').toLowerCase(),
        text: boundedSnapshotText(textOf(current), 1200),
        attributes,
        html: boundedSnapshotText(current.outerHTML || '', 4000),
      });
    }
    return {
      tileId: tileIdentity(tile),
      tag: String(tile.tagName || '').toLowerCase(),
      ownText: boundedSnapshotText(textOf(tile), 1200),
      searchText: boundedSnapshotText(tileSearchText(tile), 2000),
      ancestors,
    };
  }

  function tileComponentRoot(tile) {
    return tile?.closest?.('flow-image-tile')
      || (tile?.matches?.('flow-image-tile') ? tile : null)
      || tile?.parentElement
      || tile;
  }

  function findReusePromptButton(tile) {
    const root = tileComponentRoot(tile);
    if (!root) return null;
    return [...root.querySelectorAll('button,[role="button"]')]
      .find((button) => {
        const labels = [
          textOf(button),
          button.getAttribute('aria-label') || '',
          button.getAttribute('title') || '',
          button.getAttribute('data-tooltip') || '',
          ...[...button.querySelectorAll('i,mat-icon,span.material-symbols-outlined,span.material-icons')]
            .map(textOf),
        ].join(' ').toLocaleLowerCase();
        return labels.includes('redo')
          || labels.includes('reuse prompt')
          || labels.includes('use prompt again')
          || labels.includes('sử dụng lại câu lệnh')
          || labels.includes('dùng lại câu lệnh')
          || labels.includes('再利用');
      }) || null;
  }

  function replacePromptEditorText(editor, value) {
    if (!editor) return false;
    const text = String(value || '');
    try { editor.focus({ preventScroll: true }); } catch { editor.focus?.(); }
    if ('value' in editor) {
      const prototype = editor instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
      if (setter) setter.call(editor, text);
      else editor.value = text;
    } else {
      editor.textContent = text;
    }
    editor.dispatchEvent(new InputEvent('input', {
      bubbles: true,
      inputType: text ? 'insertText' : 'deleteContentBackward',
      data: text || null,
    }));
    editor.dispatchEvent(new Event('change', { bubbles: true }));
    return promptEditorValue(editor) === normalizePrompt(text);
  }

  async function readPromptFromReuseButton(tile, job, originalText) {
    // Flow renders the hotbar in the DOM but keeps it non-visible until the
    // tile receives hover semantics. Reveal that tile by DOM events first;
    // this remains independent of viewport coordinates and works in a
    // minimized/background tab.
    const hoverTargets = [];
    let hoverTarget = tile;
    for (let depth = 0; hoverTarget && depth < 4; depth += 1) {
      hoverTargets.push(hoverTarget);
      hoverTarget = hoverTarget.parentElement;
    }
    for (const target of hoverTargets) {
      for (const type of ['pointerover', 'pointerenter', 'mouseover', 'mouseenter', 'mousemove']) {
        try {
          const event = type.startsWith('pointer')
            ? new PointerEvent(type, {
              bubbles: type !== 'pointerenter', cancelable: true,
              pointerId: 1, pointerType: 'mouse', isPrimary: true,
            })
            : new MouseEvent(type, { bubbles: type !== 'mouseenter', cancelable: true });
          target.dispatchEvent(event);
        } catch {}
      }
    }
    await sleep(220);
    const button = findReusePromptButton(tile);
    if (!button) return { promptText: '', reason: 'reuse-button-not-found' };
    try {
      if (!button.isConnected || button.matches(':disabled')
        || button.getAttribute('aria-disabled') === 'true') {
        throw new Error('FLOW_UI_REUSE_BUTTON_DISABLED');
      }
      // The hotbar is deliberately rendered as an opacity/size-zero overlay
      // until a real pointer enters the tile. The button is already proven to
      // be inside this exact tile, so invoking its DOM control directly is
      // safe and avoids coordinates or a focus/visibility dependency.
      button.click();
      await sleep(350);
    } catch (error) {
      return {
        promptText: '',
        reason: String(error?.message || error || 'reuse-button-click-failed'),
      };
    }
    const deadline = Date.now() + 4500;
    let editor = null;
    let promptText = '';
    try {
      while (Date.now() < deadline) {
        if (job.cancelled) throw new Error('CANCELLED');
        editor = findPromptEditor();
        promptText = promptEditorValue(editor);
        if (promptText && promptText !== originalText) break;
        await sleep(180);
      }
      const result = {
        promptText: boundedSnapshotText(promptText, PROJECT_SNAPSHOT_MAX_TEXT),
        reason: promptText ? 'reuse-prompt-read' : 'reuse-prompt-empty',
      };
      // Reusing a prompt only opens it in the composer. Restore the previous
      // composer text immediately so history reconciliation never submits or
      // leaves a prompt staged for a later workspace.
      return result;
    } finally {
      const liveEditor = findPromptEditor() || editor;
      if (liveEditor && promptEditorValue(liveEditor) !== originalText) {
        replacePromptEditorText(liveEditor, originalText);
      }
      await chrome.runtime.sendMessage({ type: 'UI_TRUSTED_PRESS_ESCAPE' }).catch(() => null);
    }
  }

  function snapshotPromptMatches(searchText, prompt) {
    const actual = normalizePrompt(searchText);
    const expected = normalizePrompt(prompt);
    if (!actual || !expected) return false;
    if (actual.includes(expected)) return true;
    const head = expected.slice(0, Math.min(80, expected.length));
    const tail = expected.slice(Math.max(0, expected.length - 80));
    return actual.includes(head) && actual.includes(tail);
  }

  function snapshotProjectTileEntry(element) {
    const image = chooseGeneratedImage(element);
    return {
      element,
      tile: {
        tileId: tileIdentity(element),
        text: boundedSnapshotText(textOf(element)),
        promptText: boundedSnapshotText(tileSearchText(element)),
        image: image ? {
          tileId: image.tileId,
          url: image.url,
          width: image.width,
          height: image.height,
        } : null,
        ready: Boolean(image),
      },
    };
  }

  function visibleProjectTileEntries() {
    return all(FLOW_SELECTORS.outputTiles)
      .slice(-PROJECT_SNAPSHOT_MAX_TILES)
      .map(snapshotProjectTileEntry);
  }

  function projectScrollContainer(tile) {
    let current = tile;
    for (let depth = 0; current && depth < 16; depth += 1, current = current.parentElement) {
      if (!(current instanceof Element)) continue;
      const style = getComputedStyle(current);
      const overflowY = String(style.overflowY || style.overflow || '').toLocaleLowerCase();
      const scrollHeight = Number(current.scrollHeight || 0);
      const clientHeight = Number(current.clientHeight || 0);
      if (clientHeight > 0 && scrollHeight > clientHeight + 8
        && /(?:auto|scroll|overlay)/.test(overflowY)) {
        return current;
      }
    }
    const documentScroller = document.scrollingElement;
    if (documentScroller instanceof Element) {
      const scrollHeight = Number(documentScroller.scrollHeight || 0);
      const clientHeight = Number(documentScroller.clientHeight || 0);
      if (clientHeight > 0 && scrollHeight > clientHeight + 8) return documentScroller;
    }
    return null;
  }

  function projectScrollPositions(container) {
    if (!container) return [null];
    const scrollHeight = Number(container.scrollHeight || 0);
    const clientHeight = Number(container.clientHeight || 0);
    const maximum = Math.max(0, scrollHeight - clientHeight);
    const current = Math.max(0, Number(container.scrollTop || 0));
    if (maximum <= 8) return [current];
    // Flow virtualizes its history grid. Walk by less than one viewport so a
    // tile on a row boundary is rendered in at least one sample. Keep the
    // number of samples bounded even if a project contains many generations.
    const step = Math.max(240, Math.floor((clientHeight || 600) * 0.75));
    const positions = [current, 0];
    for (let position = step; position < maximum && positions.length < 24; position += step) {
      positions.push(position);
    }
    positions.push(maximum);
    return [...new Set(positions.map((position) => Math.max(0, Math.min(maximum, position))))];
  }

  function mergeProjectTileEntry(tileEntriesByKey, entry) {
    const key = entry.tile.tileId || entry.element;
    const existing = tileEntriesByKey.get(key);
    if (!existing) {
      tileEntriesByKey.set(key, entry);
      return entry;
    }
    existing.element = entry.element;
    if (!existing.tile.text && entry.tile.text) existing.tile.text = entry.tile.text;
    if (!existing.tile.promptText && entry.tile.promptText) existing.tile.promptText = entry.tile.promptText;
    if (!existing.tile.image && entry.tile.image) {
      existing.tile.image = entry.tile.image;
      existing.tile.ready = entry.tile.ready;
    }
    return existing;
  }

  async function inspectVisibleProjectTiles(job, tileEntriesByKey, originalText, reusedPromptReads, discoveryPrompts = []) {
    const initialEntries = [...tileEntriesByKey.values()];
    const firstElement = initialEntries[0]?.element || null;
    const container = projectScrollContainer(firstElement);
    const originalScrollTop = container ? Number(container.scrollTop || 0) : 0;
    const positions = projectScrollPositions(container);
    const inspectedKeys = new Set();
    const matched = () => discoveryPrompts.some(prompt =>
      [...tileEntriesByKey.values()].some(entry => snapshotPromptMatches(entry.tile.promptText, prompt)));
    const discoveryDeadline = discoveryPrompts.length ? Date.now() + 6000 : Infinity;
    try {
      for (const position of positions) {
        if (job.cancelled) throw new Error('CANCELLED');
        if (matched() || Date.now() >= discoveryDeadline) return;
        if (container && position !== null) {
          container.scrollTop = position;
          await sleep(450);
        }
        const entries = position === positions[0] && initialEntries.length
          ? initialEntries
          : visibleProjectTileEntries();
        for (const entry of entries) {
          const merged = mergeProjectTileEntry(tileEntriesByKey, entry);
          if (matched() || Date.now() >= discoveryDeadline) return;
          const key = merged.tile.tileId || merged.element;
          if (inspectedKeys.has(key)) continue;
          inspectedKeys.add(key);
          const read = await readPromptFromReuseButton(merged.element, job, originalText);
          reusedPromptReads.push({ tileId: merged.tile.tileId, ...read });
          if (read.promptText) {
            merged.tile.promptText = boundedSnapshotText(`${merged.tile.promptText} ${read.promptText}`);
          }
        }
      }
    } finally {
      if (container) {
        container.scrollTop = originalScrollTop;
        await sleep(180);
      }
    }
  }

  async function inspectProjectSnapshot(job, request = {}) {
    const prompts = Array.isArray(request.prompts)
      ? request.prompts.map((value) => String(value || '').trim()).filter(Boolean).slice(0, 8)
      : [];
    // Reconciliation is only authoritative after the project UI has mounted.
    // Reading an empty shell would turn a real submitted prompt into a false
    // no-match and leave every Resume permanently blocked.
    await waitForProjectSurface(job, {
      requireHistory: prompts.length > 0 && request.inspectTilePrompts === true
        && request.discoveryOnly !== true,
      ...(request.discoveryOnly === true ? { timeoutMs: 5000 } : {}),
    });
    let tileEntries = visibleProjectTileEntries();
    const tileEntriesByKey = new Map();
    for (const entry of tileEntries) mergeProjectTileEntry(tileEntriesByKey, entry);
    const tilesBeforePromptRead = () => [...tileEntriesByKey.values()]
      .map((entry) => entry.tile)
      .filter((tile) => tile.tileId || tile.text || tile.promptText);
    const reusedPromptReads = [];
    if (request.inspectTilePrompts === true) {
      const originalText = promptEditorValue(findPromptEditor());
      await inspectVisibleProjectTiles(job, tileEntriesByKey, originalText, reusedPromptReads,
        request.discoveryOnly === true ? prompts : []);
      tileEntries = [...tileEntriesByKey.values()].slice(-PROJECT_SNAPSHOT_MAX_TILES);
    }
    const tiles = tilesBeforePromptRead();
    const matches = prompts.map((prompt, index) => {
      const matched = tiles.find((tile) => snapshotPromptMatches(tile.promptText, prompt));
      return {
        promptIndex: index + 1,
        matched: Boolean(matched),
        tileId: matched?.tileId || '',
        ready: Boolean(matched?.ready),
        image: matched?.image || null,
      };
    });
    const editor = findPromptEditor();
    const emptyStateVerified = hasVerifiedEmptyProjectHistory(editor, tiles.length);
    const result = {
      projectId: projectIdFromLocation(),
      projectUrl: location.href,
      uiVersion: DOCURANDOM_FLOW_UI_VERSION,
      selectorRevision: FLOW_SELECTORS.revision,
      tileCount: tiles.length,
      tiles,
      matches,
      composer: {
        hasPromptEditor: Boolean(editor),
        text: boundedSnapshotText(promptEditorValue(editor), 500),
        submitVisible: Boolean(editor && findSubmitButton(editor)),
        stopVisible: Boolean(buttonWithIcon(['stop'])),
      },
      history: {
        scanRequested: request.inspectTilePrompts === true,
        scanComplete: request.inspectTilePrompts !== true
          || (tiles.length > 0 ? reusedPromptReads.length >= tiles.length : emptyStateVerified),
        scannedTileCount: reusedPromptReads.length,
        emptyStateVerified,
      },
      surface: projectSurfaceState(),
    };
    if (request.debug === true) {
      result.debugTiles = all(FLOW_SELECTORS.outputTiles)
        .slice(-8)
        .map(snapshotDebugTile);
    }
    if (request.inspectTilePrompts === true) {
      result.reusedPromptReads = reusedPromptReads;
    }
    return result;
  }

  function flowFailureSnapshot() {
    const regions = all('[role="alert"],[role="dialog"],[data-sonner-toast]');
    const text = regions.map(textOf).join(' ').toLocaleLowerCase();
    const modelLimitMarkers = [
      'model limit',
      'limit for this model',
      'daily limit for',
      'daily generation limit',
      'try another model',
      'switch model',
      'nano banana',
      'giới hạn của mô hình',
      'giới hạn cho mô hình',
      'giới hạn tạo ảnh hằng ngày',
      'giới hạn tạo ảnh hàng ngày',
      'đổi mô hình',
    ];
    if (modelLimitMarkers.some((marker) => text.includes(marker))) {
      return { code: 'MODEL_DAILY_LIMIT', signature: text };
    }
    const checks = [
      ['captcha', 'CAPTCHA_REQUIRED'],
      ['unusual activity', 'UNUSUAL_ACTIVITY'],
      ['hoạt động bất thường', 'UNUSUAL_ACTIVITY'],
      ['too many requests', 'RATE_LIMITED'],
      ['rate limit', 'RATE_LIMITED'],
      ['daily limit', 'MODEL_DAILY_LIMIT'],
      ['giới hạn hằng ngày', 'MODEL_DAILY_LIMIT'],
      ['giới hạn hàng ngày', 'MODEL_DAILY_LIMIT'],
      ['giới hạn', 'LIMITED'],
      ['policy', 'POLICY_REJECTED'],
      ['chính sách', 'POLICY_REJECTED'],
    ];
    const found = checks.find(([needle]) => text.includes(needle));
    return { code: found ? found[1] : '', signature: found ? text : '' };
  }

  function detectFlowFailure(baselineSignature = '') {
    const failure = flowFailureSnapshot();
    if (!failure.code || (baselineSignature && failure.signature === baselineSignature)) return '';
    return failure.code;
  }

  async function fillAndSubmit(
    job,
    prompt,
    expectedCount,
    promptNumber,
    promptTotal,
    ignoreStaleModelLimitUntil = 0,
  ) {
    const baseline = tileIds();
    const baselineFailure = flowFailureSnapshot();
    let textbox = await waitFor(
      () => findPromptEditor(),
      5000,
      job,
      'prompt-editor-initial',
    ).catch(() => null);
    if (!textbox) textbox = await recoverPromptEditor(job);
    if (!textbox) {
      textbox = await waitFor(
        () => findPromptEditor(),
        40000,
        job,
        'prompt-editor',
      );
    }
    textbox = await focusPromptEditor(textbox, job);
    await randomizedComposerSettle(job);
    const trustedInsert = await chrome.runtime.sendMessage({
      type: 'UI_TRUSTED_REPLACE_TEXT',
      text: prompt,
    }).catch(() => null);
    let inserted = Boolean(trustedInsert?.ok);
    textbox = findPromptEditor() || textbox;
    if (!editorOwnsFocus(textbox)) textbox = await focusPromptEditor(textbox, job);
    if (!inserted) {
      document.execCommand('selectAll', false);
      try {
        inserted = document.execCommand('insertText', false, prompt);
      } catch {
        inserted = false;
      }
    }
    if (!inserted || !promptMatchesEditor(textbox, prompt)) {
      if ('value' in textbox) {
        const prototype = textbox instanceof HTMLTextAreaElement
          ? HTMLTextAreaElement.prototype
          : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
        if (setter) setter.call(textbox, prompt);
        else textbox.value = prompt;
      }
      else textbox.textContent = prompt;
      textbox.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        inputType: 'insertText',
        data: prompt,
      }));
      textbox.dispatchEvent(new Event('change', { bubbles: true }));
    }
    await randomizedComposerSettle(job);
    textbox = findPromptEditor() || textbox;
    const verified = promptMatchesEditor(textbox, prompt);
    if (!verified) throw new Error('FLOW_UI_PROMPT_NOT_INSERTED');
    emit(
      job,
      0.17 + ((promptNumber - 1) / promptTotal) * 0.72,
      `Đã nhập prompt ${promptNumber}/${promptTotal} vào Flow · đang gửi…`,
      'submit',
    );

    // A real Enter key is the most stable submit action across Flow UI
    // revisions. If the current composer does not submit on Enter, fall back
    // to a broad visible-button lookup and verify that generation actually
    // started before reporting success.
    const submitBeforeEnter = findSubmitButton(textbox);
    if (job.cancelled) throw new Error('CANCELLED');
    job.submissionAttempted = true;
    const enter = await chrome.runtime.sendMessage({
      type: 'UI_TRUSTED_PRESS_ENTER',
    }).catch(() => null);
    let submitted = Boolean(enter?.ok)
      && await waitForSubmissionStart(
        textbox,
        prompt,
        baseline,
        submitBeforeEnter,
        job,
        5000,
      );
    if (!submitted) {
      if (job.cancelled) throw new Error('CANCELLED');
      const sendButton = findSubmitButton(textbox);
      if (!sendButton) throw new Error('FLOW_UI_SUBMIT_NOT_FOUND');
      await click(sendButton, job, 'submit');
      submitted = await waitForSubmissionStart(
        textbox,
        prompt,
        baseline,
        sendButton,
        job,
        5000,
      );
      if (!submitted) {
        if (job.cancelled) throw new Error('CANCELLED');
        // Some Flow builds ignore a debugger-coordinate click at non-100%
        // browser zoom. Invoke the already verified visible button directly as
        // a final UI fallback, then re-check the page state.
        sendButton.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
        sendButton.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
        sendButton.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
        sendButton.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
        sendButton.click();
        submitted = await waitForSubmissionStart(
          textbox,
          prompt,
          baseline,
          sendButton,
          job,
          5000,
        );
      }
    }
    if (!submitted) throw new Error('FLOW_UI_SUBMIT_NOT_CONFIRMED');
    job.submittedPromptNumbers.add(promptNumber);
    emit(
      job,
      0.18 + ((promptNumber - 1) / promptTotal) * 0.72,
      `Đã gửi prompt ${promptNumber}/${promptTotal} · Flow đang tạo ảnh…`,
      'submitted',
    );

    const started = Date.now();
    // A stuck Flow page must finish before the bridge and desktop HTTP
    // deadlines. Otherwise the next prompt sees FLOW_UI_BUSY and the rest of
    // the queue appears to be skipped.
    const timeoutMs = 4 * 60 * 1000;
    let partial = [];
    while (Date.now() - started < timeoutMs) {
      if (job.cancelled) throw new Error('CANCELLED');
      while (job.paused && !job.cancelled) await sleep(250);
      const failure = detectFlowFailure(baselineFailure.signature);
      const staleModelLimit = failure === 'MODEL_DAILY_LIMIT' && Date.now() < ignoreStaleModelLimitUntil;
      if (failure && !staleModelLimit) throw new Error(failure);

      const newTiles = all(FLOW_SELECTORS.outputTiles).filter((tile) => {
        const id = tileIdentity(tile);
        return id && !baseline.has(id);
      });
      partial = newTiles.map(chooseGeneratedImage).filter(Boolean);
      if (partial.length >= expectedCount) return partial.slice(0, expectedCount);

      const stopVisible = Boolean(buttonWithIcon(['stop']));
      if (!stopVisible && partial.length) {
        await sleep(1500);
        const completed = newTiles.map(chooseGeneratedImage).filter(Boolean);
        return completed.slice(0, expectedCount);
      }
      const elapsedRatio = Math.min(0.9, (Date.now() - started) / timeoutMs);
      emit(
        job,
        0.18 + ((promptNumber - 1 + elapsedRatio) / promptTotal) * 0.72,
        `Đang chờ Flow · ảnh ${promptNumber}/${promptTotal} · đã có ${partial.length}/${expectedCount}`,
        'generate',
      );
      await sleep(1000);
    }
    if (partial.length) return partial;
    throw new Error('FLOW_GENERATION_TIMEOUT');
  }

  async function run(job, request) {
    const prompts = (request.prompts || [request.prompt])
      .map((value) => String(value || '').trim())
      .filter(Boolean);
    if (!prompts.length) throw new Error('NO_PROMPTS');

    await ensureProject(job, request);
    await stabilizeFlowWorkspaceUi(job);
    const activeModel = REQUIRED_IMAGE_MODEL;
    const images = [];
    const promptOutcomes = [];
    for (let index = 0; index < prompts.length; index += 1) {
      if (index) {
        const minDelay = Math.max(0, Number(request.delayMinSeconds) || 3);
        const maxDelay = Math.max(minDelay, Number(request.delayMaxSeconds) || 5);
        const delay = minDelay + Math.random() * (maxDelay - minDelay);
        emit(job, 0.18 + (index / prompts.length) * 0.72, `Chờ ${delay.toFixed(1)} giây trước prompt tiếp theo…`, 'delay');
        await sleep(delay * 1000);
      }
      let generated = null;
      let generationFailure = '';
      await configureImage(job, {
        ...request,
        count: REQUIRED_IMAGE_COUNT,
        model: REQUIRED_IMAGE_MODEL,
      });
      try {
        generated = await fillAndSubmit(
          job,
          prompts[index],
          REQUIRED_IMAGE_COUNT,
          index + 1,
          prompts.length,
        );
      } catch (error) {
        const code = String(error?.message || error).trim();
        if (code === 'MODEL_DAILY_LIMIT') {
          throw new Error('REQUIRED_IMAGE_MODEL_LIMITED');
        }
        if (!job.submittedPromptNumbers.has(index + 1)) throw error;
        // The submit action was positively observed before this failure.
        // Never resend it and never fall back to a different model.
        generationFailure = code || 'FLOW_SUBMITTED_PROMPT_NO_IMAGE';
        generated = [];
        emit(
          job,
          0.18 + ((index + 1) / prompts.length) * 0.72,
          `Prompt ${index + 1}/${prompts.length} đã gửi nhưng Flow không trả ảnh · đang chuyển tiếp…`,
          'submitted-no-image',
        );
      }
      generated.forEach((image) => images.push({
        ...image,
        promptIndex: index + 1,
        model: activeModel,
        modelName: IMAGE_MODEL_NAMES[activeModel] || activeModel,
      }));
      promptOutcomes.push({
        promptIndex: index + 1,
        submitted: job.submittedPromptNumbers.has(index + 1),
        status: generated.length ? 'image-ready' : 'submitted-no-image',
        imageCount: generated.length,
        error: generationFailure,
      });
    }
    const submittedCount = promptOutcomes.filter((item) => item.submitted).length;
    emit(
      job,
      0.94,
      `Đã gửi ${submittedCount}/${prompts.length} prompt · Flow tạo ${images.length} ảnh.`,
      'download',
    );
    return {
      images,
      promptOutcomes,
      projectId: projectIdFromLocation(),
      projectUrl: location.href,
      modelUsed: activeModel,
      exhaustedModels: [],
    };
  }

  async function inspectConfiguration(job, request) {
    await ensureProject(job);
    const configuration = await configureImage(job, {
      ...request,
      aspect: request.aspect || 'landscape',
      count: REQUIRED_IMAGE_COUNT,
      model: REQUIRED_IMAGE_MODEL,
    });
    return {
      ...configuration,
      projectId: projectIdFromLocation(),
      projectUrl: location.href,
      selectorRevision: FLOW_SELECTORS.revision,
      uiVersion: DOCURANDOM_FLOW_UI_VERSION,
      promptTouched: false,
      promptSubmitted: false,
    };
  }

  chrome.runtime.onMessage.addListener((message, _sender, respond) => {
    if (globalThis.__DOCURANDOM_FLOW_UI_VERSION__ !== DOCURANDOM_FLOW_UI_VERSION) return false;
    if (message?.type === 'DOCURANDOM_UI_PULSE') {
      respond({
        ok: true,
        now: Date.now(),
        visibility: document.visibilityState,
        hasFocus: document.hasFocus(),
      });
      return false;
    }
    if (message?.type === 'DOCURANDOM_UI_GENERATE') {
      const job = {
        id: String(message.jobId || Date.now()),
        cancelled: false,
        paused: false,
        submittedPromptNumbers: new Set(),
      };
      jobs.set(job.id, job);
      run(job, message.request || {})
        .then((result) => respond({ ok: true, ...result }))
        .catch((error) => respond({
          ok: false,
          error: String(error?.message || error),
          submitted: job.submittedPromptNumbers.size > 0,
          submissionAttempted: Boolean(job.submissionAttempted),
          projectId: projectIdFromLocation(),
          projectUrl: String(location.href || '').slice(0, 1000),
        }))
        .finally(() => jobs.delete(job.id));
      return true;
    }
    if (message?.type === 'DOCURANDOM_UI_PREPARE_PROJECT') {
      // This command cannot insert or submit an image prompt. Its response can
      // disappear during project creation; the worker validates the new route
      // before starting the separate generation command.
      const job = {
        id: String(message.jobId || Date.now()),
        cancelled: false,
        paused: false,
        submittedPromptNumbers: new Set(),
      };
      jobs.set(job.id, job);
      ensureProject(job, { newProject: false })
        .then(() => respond({ ok: true, projectId: projectIdFromLocation() }))
        .catch((error) => respond({ ok: false, error: String(error?.message || error) }))
        .finally(() => jobs.delete(job.id));
      return true;
    }
    if (message?.type === 'DOCURANDOM_UI_PROJECT_SNAPSHOT') {
      const job = {
        id: String(message.jobId || Date.now()),
        cancelled: false,
        paused: false,
        submittedPromptNumbers: new Set(),
      };
      jobs.set(job.id, job);
      inspectProjectSnapshot(job, message.request || {})
        .then((result) => respond({ ok: true, ...result }))
        .catch((error) => respond({ ok: false, error: String(error?.message || error) }))
        .finally(() => jobs.delete(job.id));
      return true;
    }
    if (message?.type === 'DOCURANDOM_UI_INSPECT_CONFIGURATION') {
      const job = {
        id: String(message.jobId || `inspect-${Date.now()}`),
        cancelled: false,
        paused: false,
        submittedPromptNumbers: new Set(),
      };
      jobs.set(job.id, job);
      inspectConfiguration(job, message.request || {})
        .then((result) => respond({ ok: true, ...result }))
        .catch((error) => respond({ ok: false, error: String(error?.message || error) }))
        .finally(() => jobs.delete(job.id));
      return true;
    }
    if (message?.type === 'DOCURANDOM_UI_CANCEL') {
      const job = jobs.get(String(message.jobId || ''));
      if (job) job.cancelled = true;
      respond({ ok: Boolean(job), submitted: Boolean(job?.submittedPromptNumbers.size),
        submissionAttempted: Boolean(job?.submissionAttempted) });
      return false;
    }
    if (message?.type === 'DOCURANDOM_UI_RECOVER') {
      const requestedJobId = String(message.jobId || '');
      let cancelledJobs = 0;
      let submitted = false;
      let submissionAttempted = false;
      for (const [jobId, job] of jobs.entries()) {
        if (!requestedJobId || requestedJobId === jobId) {
          job.cancelled = true;
          submitted ||= job.submittedPromptNumbers.size > 0;
          submissionAttempted ||= Boolean(job.submissionAttempted);
          cancelledJobs += 1;
        }
      }
      respond({ ok: true, cancelledJobs, submitted, submissionAttempted });
      return false;
    }
    if (message?.type === 'DOCURANDOM_UI_PAUSE') {
      const job = jobs.get(String(message.jobId || ''));
      if (job) job.paused = Boolean(message.paused);
      respond({ ok: Boolean(job), paused: Boolean(job?.paused) });
      return false;
    }
    if (message?.type === 'DOCURANDOM_UI_CHECK') {
      respond({
        ok: true,
        version: DOCURANDOM_FLOW_UI_VERSION,
        isFlow: (
          location.hostname === 'flow.google.com'
          || (location.hostname === 'labs.google' && location.pathname.includes('/flow'))
        ),
        projectId: projectIdFromLocation(),
        url: location.href,
        selectorRevision: FLOW_SELECTORS.revision,
      });
      return false;
    }
    return false;
  });

  if (
    location.protocol === 'file:'
    && document.documentElement?.dataset.docurandomFlowTest === '1'
  ) {
    globalThis.__DOCURANDOM_FLOW_TEST_HOOKS__ = Object.freeze({
      findPromptEditor,
      findSubmitButton,
      findComposerControl,
      findCreateProjectButton,
      tileIdentity,
      chooseGeneratedImage,
      click,
      normalizePrompt,
      promptMatchesEditor,
      projectIdFromLocation,
      inspectProjectSnapshot,
      waitForProjectSurface,
      hasVerifiedEmptyProjectHistory,
      snapshotPromptMatches,
      readPromptFromReuseButton,
    });
  }
}
})();
