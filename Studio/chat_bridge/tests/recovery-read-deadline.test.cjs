"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { beginRecoveryReadDeadline, recoveryReadError, RECOVERY_READ_TIMEOUT_MS,
  createAcceptedResponseLease, ACCEPTED_RESPONSE_IDLE_LEASE_MS,
  RECOVERY_CONNECTION_STALE_MS, RECOVERY_NO_RESPONSE_STALE_MS } = require("../src/recovery-read-deadline.cjs");

test('exact-reader repair has time to run inside its two-minute recovery lease',()=>{
  assert.ok(RECOVERY_CONNECTION_STALE_MS>0 && RECOVERY_CONNECTION_STALE_MS<RECOVERY_READ_TIMEOUT_MS/2);
  assert.ok(RECOVERY_NO_RESPONSE_STALE_MS>0 && RECOVERY_NO_RESPONSE_STALE_MS<RECOVERY_READ_TIMEOUT_MS/2);
});

function fakeTimers(){
  const callbacks=[],cleared=[];
  return {callbacks,cleared,setTimer(fn){callbacks.push(fn);return callbacks.length;},clearTimer(id){cleared.push(id);}};
}

test('fresh accepted wait yields a slot while pre-submit attachment and receipt remain protected',()=>{
  const controller=new AbortController(),timers=fakeTimers();
  const lease=createAcceptedResponseLease(controller,timers);
  const receipt={sendAttempted:true,submissionAccepted:false};
  lease.accept(receipt);assert.equal(timers.callbacks.length,0);
  receipt.submissionAccepted=true;lease.accept(receipt);lease.accept(receipt);
  assert.equal(timers.callbacks.length,1);
  lease.progress('');lease.progress(undefined); // Stop/status heartbeats cannot renew.
  assert.equal(timers.callbacks.length,1);timers.callbacks[0]();
  assert.equal(controller.signal.reason.code,'CHATGPT_RECOVERY_PENDING');
  assert.equal(receipt.submissionAccepted,true);assert.equal(ACCEPTED_RESPONSE_IDLE_LEASE_MS,300000);
  lease.finish();
});

test('new output renews the local wait without limiting provider thinking or accepting a stale timer',()=>{
  const controller=new AbortController(),timers=fakeTimers(),lease=createAcceptedResponseLease(controller,timers);
  lease.accept({submissionAccepted:true});lease.progress('first output');
  lease.progress(' first output ');assert.equal(timers.callbacks.length,2);
  timers.callbacks[0]();assert.equal(controller.signal.aborted,false);
  lease.progress('new output');timers.callbacks[1]();assert.equal(controller.signal.aborted,false);
  lease.finish();timers.callbacks[2]();assert.equal(controller.signal.aborted,false);
});

test('a confirmed terminal image retry starts a new wait and user cancellation keeps its reason',()=>{
  const controller=new AbortController(),timers=fakeTimers(),lease=createAcceptedResponseLease(controller,timers);
  lease.accept({submissionAccepted:true});lease.pause();timers.callbacks[0]();
  assert.equal(controller.signal.aborted,false);lease.accept({submissionAccepted:true});
  const reason=new Error('user-cancelled');controller.abort(reason);timers.callbacks[1]();
  assert.equal(controller.signal.reason,reason);lease.finish();
});

test("an old read releases its caller without changing the accepted receipt", async () => {
  const receipt = { turnId: "accepted", sendAttempted: true, submissionAccepted: true };
  const context = { pendingSubmission: receipt, busy: true };
  const controller = new AbortController();
  const finish = beginRecoveryReadDeadline(controller, { timeoutMs: 10 });
  try {
    await assert.rejects(new Promise((_, reject) => {
      controller.signal.addEventListener("abort", () => reject(recoveryReadError(new Error("Request cancelled"), controller.signal)));
      setTimeout(() => reject(new Error("read failed to release")), 250);
    }), error => error.code === "CHATGPT_RECOVERY_PENDING" && error.statusCode === 409);
  } finally { finish(); context.busy = false; }
  assert.equal(context.pendingSubmission, receipt);
  assert.equal(receipt.submissionAccepted, true);
  assert.equal(context.busy, false);
  assert.equal(RECOVERY_READ_TIMEOUT_MS, 120_000);
});

test("completed read clears the timer; a late callback cannot abort a new caller", async () => {
  const controller = new AbortController();
  const finish = beginRecoveryReadDeadline(controller, { timeoutMs: 5 });
  finish();
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(controller.signal.aborted, false);
});

test("client cancellation keeps its original reason instead of becoming a timeout", () => {
  const controller = new AbortController();
  const original = new Error("client closed");
  controller.abort(original);
  let callback;
  const finish = beginRecoveryReadDeadline(controller, { setTimer: value => { callback = value; return 1; }, clearTimer: () => {} });
  callback(); finish();
  assert.equal(controller.signal.reason, original);
  assert.equal(recoveryReadError(original, controller.signal), original);
});
