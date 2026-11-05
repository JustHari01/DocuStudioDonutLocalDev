"use strict";

const INTERRUPTION_STALE_MS = 120_000;

// Inspect the provider's live status badge, never quoted response text or an
// older user turn. A stale Stop button alone must not masquerade as progress.
const INTERRUPTION_PAGE_HELPERS = `
  const chatConnectionInterrupted = visible => {
    const user = chatTurnsForRole('user').at(-1);
    if (!user) return false;
    const root = document.querySelector('main') || document.body;
    return [...root.querySelectorAll('span, p, div')].slice(-600).some(node => {
      if (node.closest('.markdown, [data-markdown-text-style], [class*="MarkdownRoot-"], form, nav, aside')) return false;
      const text = String(node.textContent || '').trim().replace(/\\s+/g, ' ');
      return /^Connection interrupted\\. Waiting for the complete answer\\.?$/i.test(text)
        && !user.contains(node)
        && Boolean(user.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING)
        && visible(node);
    });
  };
`;

class ConnectionInterruptionTracker {
  constructor({ staleMs = INTERRUPTION_STALE_MS } = {}) {
    this.staleMs = staleMs;
    this.reset();
  }
  reset() { this.since = undefined; this.signature = undefined; }
  update({ interrupted, signature }, now = Date.now()) {
    if (!interrupted) { this.reset(); return false; }
    if (this.since === undefined || signature !== this.signature) {
      this.since = now; this.signature = signature;
    }
    return now - this.since >= this.staleMs;
  }
}

module.exports = { INTERRUPTION_STALE_MS, INTERRUPTION_PAGE_HELPERS, ConnectionInterruptionTracker };
