const dot = document.getElementById("dot");
const statusText = document.getElementById("statusText");
const profile = document.getElementById("profile");
const pairButton = document.getElementById("pairButton");
const forgetButton = document.getElementById("forgetButton");
const repairPanel = document.getElementById("repairPanel");
const repairSecret = document.getElementById("repairSecret");
const repairButton = document.getElementById("repairButton");
const message = document.getElementById("message");
const query = new URLSearchParams(location.search);

async function refresh() {
  try {
    const response = await chrome.runtime.sendMessage({ type: "get_status" });
    const state = response?.state || {};
    dot.classList.toggle("ok", Boolean(state.connected));
    statusText.textContent = state.connected
      ? "Đã kết nối DocuRandom"
      : state.paired
        ? "Đã ghép · đang chờ DocuRandom"
        : "Chưa ghép profile";
    profile.textContent = response?.profileId
      ? `Profile ID: ${response.profileId}`
      : "";
    if (state.error && !state.connected) message.textContent = state.error;
  } catch (error) {
    dot.classList.remove("ok");
    statusText.textContent = "Extension cần được tải lại";
    message.textContent = String(error?.message || error);
  }
}

pairButton.addEventListener("click", async () => {
  pairButton.disabled = true;
  message.textContent = "Đang ghép…";
  const result = await chrome.runtime.sendMessage({ type: "pair" });
  message.textContent = result?.ok
    ? "Ghép thành công. Bạn có thể đóng popup."
    : result?.error || "Không ghép được profile.";
  pairButton.disabled = false;
  await refresh();
});

forgetButton.addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "forget" });
  message.textContent = "Đã xóa liên kết trên profile này.";
  await refresh();
});

refresh();
setInterval(refresh, 1000);

// Local recovery for a profile whose bridge secret still exists in the
// profile's old extension state but whose current chrome.storage entry was
// cleared.  The secret is entered in this extension page, never placed in a
// URL, and is cleared from the input immediately after the message is sent.
if (query.get("docurandom_repair") === "1") {
  repairPanel.hidden = false;
  const restore = async (providedSecret = null) => {
    repairButton.disabled = true;
    const secret = providedSecret ?? repairSecret.value;
    repairSecret.value = "";
    message.textContent = "Đang khôi phục liên kết cũ…";
    try {
      const result = await chrome.runtime.sendMessage({
        type: "restore_pair",
        profile_id: query.get("profile_id") || "",
        secret,
      });
      message.textContent = result?.ok
        ? "Đã khôi phục. Đang nối lại DocuRandom…"
        : result?.error || "Không khôi phục được liên kết cũ.";
    } catch (error) {
      message.textContent = `Lỗi khôi phục: ${String(error?.message || error)}`;
    } finally {
      repairButton.disabled = false;
      await refresh();
    }
  };
  repairButton.addEventListener("click", restore);

}

// DocuRandom opens this internal URL inside the exact Donut profile selected
// for automation. The bridge server has already opened a short-lived pairing
// slot for that profile, so no extension popup click is needed and another
// profile cannot be paired accidentally.
if (new URLSearchParams(location.search).get("docurandom_pair") === "1") {
  pairButton.disabled = true;
  message.textContent = "Đang tự ghép đúng profile…";
  chrome.runtime
    .sendMessage({ type: "pair" })
    .then(async (result) => {
      message.textContent = result?.ok
        ? "Ghép tự động thành công."
        : result?.error || "Không ghép tự động được profile.";
      pairButton.disabled = false;
      await refresh();
    })
    .catch(async (error) => {
      message.textContent = `Lỗi ghép profile: ${String(error?.message || error)}`;
      pairButton.disabled = false;
      await refresh();
    });
}

// The bundled tool opens this private extension URL after replacing an
// unpacked Bridge build. Loading popup.js comes from disk even while the old
// service worker is resident; runtime.reload() then atomically picks up the
// new manifest/background without opening chrome://extensions or stealing the
// user's current tab.
if (new URLSearchParams(location.search).get("docurandom_reload") === "1") {
  setTimeout(() => chrome.runtime.reload(), 100);
}
