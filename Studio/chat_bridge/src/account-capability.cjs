"use strict";

// All contexts share the account, but a missing model is not an account-wide
// submission failure. Only an explicit hard limit can be classified as 429.
async function accountCapabilityFailure(error, contexts, readSnapshot, makeError) {
  if (!String(error?.message || error).includes("CHATGPT_MODEL_UNAVAILABLE")) return error;
  for (const context of [...contexts].slice(0, 8)) {
    if (context.busy || !context.window || context.window.isDestroyed()) continue;
    try {
      const snapshot = await readSnapshot(context.window);
      const notice = String(snapshot?.rateLimitText || "");
      if (snapshot?.rateLimited === true && notice
          && !/^Capabilities reduced until /i.test(notice)) {
        return makeError(notice);
      }
    } catch { /* A missing diagnostic cannot establish an account limit. */ }
  }
  return error;
}

async function readAccountCapabilities(contexts, mainWindow, readSnapshot) {
  const windows = new Set();
  if (mainWindow && !mainWindow.isDestroyed()) windows.add(mainWindow);
  let skippedActive = false;
  for (const context of contexts) {
    if (context.busy) { skippedActive = true; continue; }
    if (context.window && !context.window.isDestroyed()) windows.add(context.window);
  }
  const results = await Promise.allSettled([...windows].map(readSnapshot));
  const snapshots = results.filter(result => result.status === "fulfilled")
    .map(result => result.value);
  const hard = snapshots.find(snapshot => snapshot?.rateLimited === true);
  const reduced = snapshots.find(snapshot => snapshot?.capabilitiesReduced === true);
  return {
    schema_version: 1,
    complete: windows.size > 0 && !skippedActive && results.every(result =>
      result.status === "fulfilled" && typeof result.value?.rateLimited === "boolean"),
    authenticated: snapshots.some(snapshot => snapshot?.authenticated === true),
    ready: !hard && snapshots.some(snapshot => snapshot?.authenticated === true && snapshot.ready === true),
    hard_rate_limited: Boolean(hard),
    rate_limit_text: String(hard?.rateLimitText || ""),
    capabilities_reduced: Boolean(reduced),
    capability_notice: String(reduced?.capabilityNotice || ""),
    inspected_pages: snapshots.length,
  };
}

module.exports = { accountCapabilityFailure, readAccountCapabilities };
