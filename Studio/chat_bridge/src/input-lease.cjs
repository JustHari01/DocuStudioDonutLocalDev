"use strict";

// Bound local IPC only. This lease never limits a submitted model response.
function awaitInputOperation(operation, {
  signal, timeoutMs = 30_000,
  code = "CHATGPT_NATIVE_INPUT_TIMEOUT",
  message = "Lệnh nhập ChatGPT không phản hồi; prompt chưa được gửi.",
} = {}) {
  return new Promise((resolve, reject) => {
    let finished = false;
    let timer;
    const finish = (error, value) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      if (error) reject(error); else resolve(value);
    };
    const abort = () => finish(new Error("Request cancelled"));
    if (signal?.aborted) return abort();
    signal?.addEventListener("abort", abort, { once: true });
    timer = setTimeout(() => finish(Object.assign(new Error(`${code}: ${message}`), { code })),
      Math.max(1, Number(timeoutMs) || 30_000));
    Promise.resolve().then(() => {
      if (finished) return;
      return operation();
    }).then(value => finish(null, value), error => finish(error));
  });
}

module.exports = { awaitInputOperation };
