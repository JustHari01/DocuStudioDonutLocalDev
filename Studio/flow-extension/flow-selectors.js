/**
 * Versioned, local Google Flow selector catalog.
 *
 * Keep this file data-only so a Flow UI revision can be serviced without
 * changing the queue/state-machine code. Unlike third-party automation, the
 * catalog is packaged with DocuRandom and is never downloaded at runtime.
 */
globalThis.__DOCURANDOM_FLOW_SELECTORS__ = Object.freeze({
  schemaVersion: 1,
  revision: '2026.09.03.1',
  promptEditors: Object.freeze([
    '.ProseMirror[contenteditable="true"]',
    'div[role="textbox"][contenteditable="true"]',
    'div[role="textbox"][contenteditable="plaintext-only"]',
    'div[role="textbox"]',
    '[contenteditable="true"][data-placeholder]',
    '[contenteditable="plaintext-only"][data-placeholder]',
    'textarea',
  ]),
  outputTiles: '[data-tile-id],flow-image-tile img[data-media-id]',
  openLayers: '[data-state="open"],[role="dialog"],[role="menu"],.cdk-overlay-pane',
  modalLayers: '[role="dialog"],[aria-modal="true"]',
  menus: '[role="menu"]',
  horizontalMenuRows: ':scope > div[data-orientation="horizontal"]',
});
