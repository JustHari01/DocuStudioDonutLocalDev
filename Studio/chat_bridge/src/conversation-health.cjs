"use strict";

const UUID = "[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}";
const CONVERSATION_PATH = new RegExp(`^/backend-api/(?:f/)?conversation/(${UUID})/?$`, "i");

function requestIdentity(details) {
  if (!["GET", "POST"].includes(details?.method)) return null;
  let url;
  try { url = new URL(details.url); } catch { return null; }
  if (!["https://chatgpt.com", "https://chat.openai.com"].includes(url.origin)) return null;
  const match = CONVERSATION_PATH.exec(url.pathname);
  if (details.method === 'POST' && /^\/backend-api\/(?:f\/)?conversation\/?$/.test(url.pathname))
    return {kind:'submission'};
  if (match && details.method === "GET") return { kind: "conversation", conversationId: match[1].toLowerCase() };
  if (["/backend-api/me", "/api/auth/session"].includes(url.pathname)) return { kind: "session" };
  const pageId = conversationId("https://chatgpt.com" + url.pathname);
  if (pageId && details.method === "GET") return { kind: "conversation-page", conversationId: pageId };
  if (url.pathname.startsWith("/backend-api/")) {
    const publicNames = new Set(["f", "conversation", "conversations", "accounts", "check", "me",
      "models", "settings", "prepare", "sentinel", "requirements", "user", "files", "gizmos"]);
    // A small family inventory locates changed provider routes without
    // retaining arbitrary path segments that might contain private IDs.
    const family = url.pathname.slice(13).split("/").slice(0, 2)
      .map(part => publicNames.has(part) ? part : "other").join("/");
    return { kind: "backend", family };
  }
  return null;
}

function conversationId(url) {
  const match = new RegExp(`^https://chatgpt\\.com/c/(${UUID})/?(?:[?#].*)?$`, "i").exec(String(url || ""));
  return match ? match[1].toLowerCase() : "";
}

function classification(event) {
  if (!event) return "unknown";
  if (event.error) return "network-error";
  const status = event.statusCode;
  if (status === 401) return "authentication-required";
  if (status === 403) return "access-rejected";
  if ([404, 410].includes(status)) return "conversation-unavailable";
  if (status === 429) return "rate-limited";
  if (status >= 500) return "server-error";
  if (status >= 200 && status < 300) return "loaded";
  return "unknown";
}

function retryAfterSeconds(details, now) {
  const header = Object.entries(details?.responseHeaders || {})
    .find(([name]) => name.toLowerCase() === "retry-after")?.[1];
  const value = String(Array.isArray(header) ? header[0] : header || "").trim();
  const seconds = /^\d+$/.test(value) ? Number(value) : Math.ceil((Date.parse(value) - now) / 1000);
  return Number.isFinite(seconds) && seconds > 0 && seconds <= 2 * 86400 ? seconds : 0;
}

class ConversationHealth {
  constructor({ now = Date.now, maxEvents = 2048, maxAgeMs = 15 * 60_000 } = {}) {
    this.now = now; this.maxEvents = maxEvents; this.maxAgeMs = maxAgeMs; this.events = [];
  }
  record(details) {
    const identity = requestIdentity(details);
    if (!identity) return;
    // Deliberately omit URLs, query strings, bodies, headers and account data.
    this.events.push({ ...identity, at: this.now(),
      method: details.method,
      webContentsId: Number(details.webContentsId) || 0,
      statusCode: Number(details.statusCode) || 0,
      ...(Number(details.statusCode) === 429 && retryAfterSeconds(details, this.now())
        ? { retryAfterSeconds: retryAfterSeconds(details, this.now()) } : {}),
      error: /^net::ERR_[A-Z0-9_]+$/.test(details.error || "") ? details.error : "",
      fromCache: details.fromCache === true });
    // A burst of sidebar/background requests from one page must not evict
    // another workspace's only conversation error before it can be read.
    const counts = new Map();
    this.events = this.events.filter(event => this.now() - event.at <= this.maxAgeMs)
      .reverse().filter(event => {
        const key = event.webContentsId + ":" + event.kind;
        const count = (counts.get(key) || 0) + 1; counts.set(key, count);
        return count <= (event.kind === "backend" ? 24 : 8);
      }).reverse().slice(-this.maxEvents);
  }
  snapshot(url, webContentsId) {
    const id = conversationId(url);
    const recent = this.events.filter(event => this.now() - event.at <= this.maxAgeMs
      && event.webContentsId === webContentsId);
    const exact = recent.filter(event => event.kind === "conversation" && event.conversationId === id);
    // The sidebar index has its own quota. Preserve its throttle for failed
    // history recovery, without turning it into a generation/account quota
    // when the exact conversation or an accepted response remains usable.
    const historyThrottle = recent.filter(event => event.kind === 'backend'
      && /^(?:f\/)?conversations(?:\/|$)/.test(event.family)
      && event.statusCode === 429 && this.now() - event.at <= 120_000).at(-1);
    const throttled = recent.filter(event => (event.kind === "submission" || (event.kind === "backend"
      && /^(?:f\/)?conversation(?:\/|$)/.test(event.family)))
      && event.statusCode === 429 && this.now() - event.at <= 120_000).at(-1);
    return { classification: throttled ? "rate-limited" : classification(exact.at(-1)),
      ...(throttled ? { blocking: throttled } : {}), conversation: exact.slice(-4),
      ...(historyThrottle ? { history_throttle: historyThrottle } : {}),
      session: recent.filter(event => event.kind === "session").slice(-2),
      navigation: recent.filter(event => event.kind === "conversation-page" && event.conversationId === id).slice(-3),
      submission: recent.filter(event => event.kind === "submission").slice(-4),
      backend: recent.filter(event => event.kind === "backend").slice(-12) };
  }
}

module.exports = { ConversationHealth, conversationId, classification };
