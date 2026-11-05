"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  assertLoopbackAddress,
  createAccessToken,
  isAllowedNavigation,
  tokenMatches,
} = require("../src/security.cjs");

test("access tokens are long, random, and require an exact bearer match", () => {
  const first = createAccessToken();
  const second = createAccessToken();
  assert.match(first, /^[A-Za-z0-9_-]{64}$/);
  assert.notEqual(first, second);
  assert.equal(tokenMatches(first, `Bearer ${first}`), true);
  assert.equal(tokenMatches(first, `Bearer ${second}`), false);
  assert.equal(tokenMatches(first, first), false);
});

test("navigation is restricted to HTTPS login and ChatGPT origins", () => {
  assert.equal(isAllowedNavigation("https://chatgpt.com/?temporary-chat=true"), true);
  assert.equal(isAllowedNavigation("https://auth.openai.com/authorize"), true);
  assert.equal(isAllowedNavigation("https://accounts.google.com/o/oauth2/auth"), true);
  assert.equal(isAllowedNavigation("http://chatgpt.com/"), false);
  assert.equal(isAllowedNavigation("https://chatgpt.com.evil.example/"), false);
  assert.equal(isAllowedNavigation("file:///C:/secret.txt"), false);
});

test("only IPv4 loopback clients pass the server boundary", () => {
  assert.doesNotThrow(() => assertLoopbackAddress("127.0.0.1"));
  assert.doesNotThrow(() => assertLoopbackAddress("::ffff:127.0.0.1"));
  assert.throws(() => assertLoopbackAddress("192.168.1.2"), /non-loopback/);
  assert.throws(() => assertLoopbackAddress("::1"), /non-loopback/);
});

test("update shutdown stays authenticated and refuses to interrupt active work", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "src", "main.cjs"), "utf8");
  const authorization = source.indexOf("tokenMatches(accessToken");
  const shutdownRoute = source.indexOf('url.pathname === "/v1/session/shutdown"');
  assert.ok(authorization >= 0 && shutdownRoute > authorization);
  assert.match(source.slice(shutdownRoute, shutdownRoute + 700), /busyCount > 0/);
  assert.match(source.slice(shutdownRoute, shutdownRoute + 700), /status: "shutting-down"/);
});

test("session repair is authenticated, idle-only and preserves browser storage", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "src", "main.cjs"), "utf8");
  const authorization = source.indexOf("tokenMatches(accessToken");
  const repairRoute = source.indexOf('url.pathname === "/v1/session/repair"');
  assert.ok(authorization >= 0 && repairRoute > authorization);
  assert.match(source.slice(repairRoute, repairRoute + 500), /repairBrowserSession/);
  const repairFunction = source.slice(
    source.indexOf("async function repairBrowserSession"),
    source.indexOf("function createChatWindow"),
  );
  assert.match(repairFunction, /busyCount > 0/);
  assert.match(repairFunction, /clearCache/);
  assert.doesNotMatch(repairFunction, /clearStorageData|removeSession|Cookies/);
});

test("a transient continuation failure retains its saved-chat binding", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "src", "main.cjs"), "utf8");
  assert.match(source, /if \(contextUsable\)[^]*turnConversations\.set\(turnIdentity, context\)/);
  assert.match(source, /saveTurnContext\(turnIdentity, context\)/);
  assert.match(source, /url\.pathname === "\/v1\/turns\/recover"/);
});

test("parallel activity is distinct from full bridge capacity", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "src", "main.cjs"), "utf8");
  const statusRoute = source.indexOf('url.pathname === "/v1/session/status"');
  const inspectRoute = source.indexOf('url.pathname === "/v1/session/inspect"');
  assert.ok(statusRoute >= 0 && inspectRoute > statusRoute);
  assert.match(source.slice(statusRoute, inspectRoute), /busy:\s*!canAcceptConversationTurn\(\)/);
  assert.match(source, /const MAX_CONVERSATIONS = 20/);
  assert.match(source, /chatUrl:[^]*evictLeastRecentlyUsedIdleWindow/);
});

test("a disconnected client cannot hold a pre-submission turn for the full recovery grace", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "src", "main.cjs"), "utf8");
  assert.match(source, /DISCONNECTED_BEFORE_SUBMISSION_GRACE_MS = 90 \* 1000/);
  assert.match(source, /const graceMs = context\.pendingSubmission/);
  assert.match(source, /if \(responseDisconnected\) scheduleDisconnectedAbort\(\)/);
});

test("session status exposes active bridge heartbeat time without response content", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "src", "main.cjs"), "utf8");
  assert.match(source, /function latestActiveTurnActivityUpdatedAt\(\)/);
  assert.match(source, /function latestActiveTurnUpdatedAt\(\)/);
  assert.match(source, /activity_updated_at: latestActiveTurnActivityUpdatedAt\(\)/);
  assert.match(source, /updated_at: latestActiveTurnUpdatedAt\(\)/);
  const statusStart = source.indexOf('url.pathname === "/v1/session/status"');
  const inspectStart = source.indexOf('url.pathname === "/v1/session/inspect"');
  assert.ok(statusStart >= 0 && inspectStart > statusStart);
  assert.doesNotMatch(source.slice(statusStart, inspectStart), /output_text/);
});

test("a renderer that stops emitting callbacks cannot hold a turn slot forever", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "src", "main.cjs"), "utf8");
  assert.match(source, /TURN_HEARTBEAT_STALE_MS = 3 \* 60 \* 1000/);
  assert.match(source, /TURN_HEARTBEAT_CHECK_MS = 5 \* 1000/);
  assert.match(source, /heartbeatTimer = setInterval/);
  assert.match(source, /controller\.abort\(\)/);
  assert.match(source, /clearInterval\(heartbeatTimer\)/);
});

test("authentication keeps confirmed sessions through transient renderer states", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "src", "main.cjs"), "utf8");
  const updateStart = source.indexOf("async function updateAuthentication");
  const responseStart = source.indexOf("async function handleResponseRequest");
  assert.ok(updateStart >= 0 && responseStart > updateStart);
  const authSection = source.slice(updateStart, responseStart);
  assert.match(authSection, /snapshot\?\.signedOut === true/);
  assert.match(authSection, /async function waitForAuthenticatedSession/);
  assert.match(source.slice(responseStart, responseStart + 700), /await waitForAuthenticatedSession\(\)/);
  assert.match(source, /CHATGPT_AUTH_RETRY_DELAY_MS = 250/);
});
