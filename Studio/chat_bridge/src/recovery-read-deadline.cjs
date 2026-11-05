"use strict";

// Only the local read of a previously submitted turn is bounded. The provider
// can keep generating; its receipt and conversation remain available next time.
const RECOVERY_READ_TIMEOUT_MS = 120_000;
// Recovery must run before its local read lease expires, leaving time to
// hydrate the exact conversation and observe a completed response.
const RECOVERY_CONNECTION_STALE_MS = 30_000;
const RECOVERY_NO_RESPONSE_STALE_MS = 45_000;
const ACCEPTED_RESPONSE_IDLE_LEASE_MS = 5 * 60_000;

function createAcceptedResponseLease(controller, {
  timeoutMs = ACCEPTED_RESPONSE_IDLE_LEASE_MS, setTimer = setTimeout, clearTimer = clearTimeout,
} = {}) {
  let timer=null, generation=0, accepted=false, lastText='', closed=false;
  const pause=()=>{generation+=1;if(timer!==null)clearTimer(timer);timer=null;accepted=false;lastText='';};
  const arm=()=>{
    if(timer!==null)clearTimer(timer);
    const own=++generation;
    timer=setTimer(()=>{
      if(closed || own!==generation || controller.signal.aborted)return;
      controller.abort(Object.assign(new Error(
        'CHATGPT_RECOVERY_PENDING: Lượt đã được ChatGPT nhận nhưng chưa có nội dung/ảnh mới '
        + 'trong 5 phút; giữ biên nhận và hội thoại, nhường phiên để dò tiếp; không gửi lại prompt.',
      ),{code:'CHATGPT_RECOVERY_PENDING',statusCode:409}));
    },timeoutMs);
    timer?.unref?.();
  };
  return {
    accept(receipt){
      if(!closed && !accepted && !controller.signal.aborted && receipt?.submissionAccepted===true){accepted=true;arm();}
    },
    progress(text){
      const next=typeof text==='string'?text.trim():'';
      // A Stop button or status heartbeat does not prove provider progress.
      if(accepted && !closed && !controller.signal.aborted && next && next!==lastText){lastText=next;arm();}
    },
    pause,
    finish(){closed=true;pause();},
  };
}

function beginRecoveryReadDeadline(controller, {
  timeoutMs = RECOVERY_READ_TIMEOUT_MS,
  setTimer = setTimeout,
  clearTimer = clearTimeout,
} = {}) {
  const timer = setTimer(() => {
    if (!controller.signal.aborted) {
      controller.abort(Object.assign(new Error(
        "CHATGPT_RECOVERY_PENDING: Chưa đọc được phản hồi của lượt đã gửi trong 2 phút; "
        + "giữ biên nhận và hội thoại, nhường phiên để dò tiếp; không gửi lại prompt.",
      ), { code: "CHATGPT_RECOVERY_PENDING", statusCode: 409 }));
    }
  }, timeoutMs);
  timer?.unref?.();
  return () => clearTimer(timer);
}

function recoveryReadError(error, signal) {
  return signal?.reason?.code === "CHATGPT_RECOVERY_PENDING" ? signal.reason : error;
}

module.exports = { beginRecoveryReadDeadline, recoveryReadError, RECOVERY_READ_TIMEOUT_MS,
  createAcceptedResponseLease, ACCEPTED_RESPONSE_IDLE_LEASE_MS,
  RECOVERY_CONNECTION_STALE_MS, RECOVERY_NO_RESPONSE_STALE_MS };
