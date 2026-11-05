"use strict";
const { conversationId } = require('./conversation-health.cjs');
const HASH = /^[a-f0-9]{64}$/;
const STALE_MS = 30 * 60_000;
const OBSERVE_MS = 90_000;

function imageReceiptProof(read, pending, { chatUrl, promptSha256, draftSha256, now = Date.now(), stopped = false } = {}) {
  const { snapshot: s, page: p, draft: d } = read || {};
  if(pending?.providerDispatchObservedAt && pending.submissionAccepted!==true)return null;
  if (!pending?.sendAttempted || pending.outputMode !== 'generated_image'
      || !Number.isFinite(pending.createdAt) || pending.createdAt <= 0 || now - pending.createdAt < STALE_MS
      || !HASH.test(promptSha256 || '') || !HASH.test(draftSha256 || '')
      || (pending.promptSha256 && pending.promptSha256 !== promptSha256)
      || !conversationId(chatUrl) || conversationId(s?.url) !== conversationId(chatUrl)
      || conversationId(p?.url) !== conversationId(chatUrl) || conversationId(d?.url) !== conversationId(chatUrl)
      || s?.readyState !== 'complete' || p?.authenticated !== true || p?.ready !== true
      || s?.conversationLoadFailed || s?.accountLoadFailed || s?.connectionInterrupted
      || p?.conversationLoadFailed || p?.accountLoadFailed || p?.rateLimited
      || d?.visibleComposers !== 1 || !Array.isArray(s?.imageNodes) || s.imageNodes.length
      || Number(s?.streamingMarkerCount) !== 0 || !Number.isInteger(s?.stopControlCount)
      || s.stopControlCount < 0 || s.stopControlCount > 1) return null;
  const user = s.users?.at(-1), assistant = s.assistants?.at(-1);
  if (!user?.key) return null;
  let kind;
  if (s.latestUserTextSha256 === promptSha256 || s.latestUserContentSha256 === promptSha256) {
    const identity=value=>String(value||'').replace(/^[^:]+:/,'');
    if ((pending.acceptedUserKey && identity(user.key)!==identity(pending.acceptedUserKey))
        || s.userCount > pending.userCount + 1) return null;
    if (d.actualLength > 1 || (!stopped && s.latestAssistantAfterUser !== false)) return null;
    kind = s.stopControlCount === 1 ? 'stalled-generating' : 'idle-no-image';
  } else {
    // Old builds accepted a remounted turn key as a send acknowledgement.
    // Only the intact unsent draft and unchanged pre-click history can repair
    // that legacy receipt. Never apply this to a fingerprint-verified echo.
    const intactDraft = d.sha256 === draftSha256 && d.actualLength >= 256;
    const lostLegacyDraft = d.actualLength <= 1 && now - pending.createdAt >= 60 * 60_000
      && ['user-turn', 'user-turn-key', 'assistant-turn', 'assistant-turn-key'].includes(pending.acceptanceSignal);
    if (['exact-user-prompt','exact-source-user-prompt'].includes(pending.acceptanceSignal) || s.stopControlCount !== 0
        || (!intactDraft && !lostLegacyDraft)
        || s.userCount !== pending.userCount || s.assistantCount !== pending.assistantCount
        || user.key !== pending.userKey || (assistant?.key || '') !== (pending.assistantKey || '')
        || (pending.beforeUserTextSha256 && s.latestUserTextSha256 !== pending.beforeUserTextSha256)) return null;
    kind = 'legacy-echo-mismatch';
  }
  return { kind, userKey: user.key, binding: JSON.stringify([conversationId(chatUrl), pending.createdAt,
    promptSha256, user.key, s.latestUserTextSha256]),
    signature: JSON.stringify([kind, user.key, assistant?.key || '', s.latestAssistantTextSha256 || '',
      s.userCount, s.assistantCount, d.sha256, s.stopControlCount]) };
}

function providerHold(health) {
  if (health?.classification === 'rate-limited') {
    const seconds = Math.max(60, Number(health.blocking?.retryAfterSeconds) || 1200);
    throw Object.assign(new Error(`CHATGPT_RATE_LIMITED: HTTP 429; retry-after-seconds=${seconds}; giữ biên nhận ảnh.`), { statusCode: 429 });
  }
  if (['authentication-required', 'access-rejected', 'conversation-unavailable'].includes(health?.classification))
    throw Object.assign(new Error(`CHATGPT_IMAGE_RECOVERY_BLOCKED: ${health.classification}; giữ biên nhận ảnh.`), { statusCode: 409 });
}

async function repairStalledImage({ pending, options, observations, key, probe, health, stop, persist,
  pause = ms => new Promise(resolve => setTimeout(resolve, ms)), now = Date.now, observeMs = OBSERVE_MS }) {
  const read = async () => { providerHold(health?.()); return probe(); };
  const prior = observations.get(key);
  const proof = value => imageReceiptProof(value, pending, { ...options, now: now(), stopped: !!prior?.stopAttemptedAt });
  const first = proof(await read());
  if (!first) return { status: 'not-proven', safe_to_retry: false };
  await pause(1500);
  const second = proof(await read());
  if (!second || first.binding !== second.binding || first.signature !== second.signature)
    return { status: 'changed', safe_to_retry: false };
  if (prior && prior.binding!==second.binding) return { status: 'changed', safe_to_retry: false };
  if (second.kind !== 'stalled-generating') {
    return { status: 'retry-authorized', safe_to_retry: true, reason: second.kind, binding: second.binding };
  }
  let observation = observations.get(key);
  if (observation && observation.binding !== second.binding)
    return { status: 'changed', safe_to_retry: false };
  if (observation?.stopAttemptedAt) return { status: 'stop-pending', safe_to_retry: false };
  if (!observation || observation.binding !== second.binding || observation.signature !== second.signature) {
    observation = { binding: second.binding, signature: second.signature, firstSeenAt: now(), lastSeenAt: now() };
    observations.set(key, observation); persist();
  }
  observation.lastSeenAt = now(); persist();
  if (now() - observation.firstSeenAt < observeMs)
    return { status: 'observing', safe_to_retry: false, retry_after_seconds: Math.ceil(observeMs / 1000) };
  providerHold(health?.());
  const marked = () => { observation.stopAttemptedAt = now(); persist(); };
  const dispatched = await stop(second, marked);
  if (!dispatched) return { status: 'changed', safe_to_retry: false };
  const deadline = now() + 8000;
  while (now() < deadline) {
    await pause(250);
    const after = imageReceiptProof(await read(), pending, { ...options, now: now(), stopped: true });
    if (!after || after.binding !== second.binding) return { status: 'changed-after-stop', safe_to_retry: false };
    if (after.kind === 'idle-no-image') {
      await pause(1500);
      const final = imageReceiptProof(await read(), pending, { ...options, now: now(), stopped: true });
      if (final?.kind === 'idle-no-image' && final.binding === after.binding && final.signature === after.signature)
        return { status: 'retry-authorized', safe_to_retry: true, reason: 'stopped-stalled-image', binding: final.binding };
      return { status: 'changed-after-stop', safe_to_retry: false };
    }
  }
  return { status: 'stop-pending', safe_to_retry: false };
}
module.exports = { imageReceiptProof, repairStalledImage, providerHold, STALE_MS, OBSERVE_MS };
