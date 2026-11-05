import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { CdpClient } from "../e2e/lib/cdp.mjs";

const cdp = await CdpClient.connect(19471);
const invoke = (command, args = {}) =>
  cdp.evaluate(
    `window.__TAURI_INTERNALS__.invoke(${JSON.stringify(command)},${JSON.stringify(args)})`,
  );
try {
  const officialUpdate = await invoke("check_for_app_updates_manual");
  if (officialUpdate) {
    assert.equal(officialUpdate.notification_only, true);
    assert.equal(officialUpdate.manual_update_required, true);
  }
  const browserUpdates = await invoke("check_for_browser_updates");
  assert.ok(Array.isArray(browserUpdates));
  for (const update of browserUpdates) {
    assert.equal(typeof update.browser, "string");
    assert.equal(typeof update.new_version, "string");
    assert.ok(Array.isArray(update.affected_profiles));
  }
  const fixture = {
    current_version: "0.34.0",
    new_version: "v99.0.0",
    release_notes: "Synthetic update policy fixture",
    download_url: "http://127.0.0.1:1/must-not-download",
    is_nightly: false,
    published_at: "2026-10-01T00:00:00Z",
    manual_update_required: false,
    release_page_url: "https://github.com/zhom/donutbrowser/releases",
    repo_update: false,
    notification_only: false,
  };
  let blocked = 0;
  for (const command of [
    "download_and_prepare_app_update",
    "restart_application",
  ]) {
    try {
      await invoke(command, { updateInfo: fixture });
      assert.fail(`${command} must be refused`);
    } catch (error) {
      assert.match(String(error), /only notifies about official updates/);
      blocked++;
    }
  }
  const report = {
    checkedAt: new Date().toISOString(),
    officialUpdate,
    officialStableCheckReached: true,
    browserUpdates,
    engineAutomaticUpdatesDisabled: false,
    blockedDownloadAndInstallCommands: blocked,
    syntheticFixture: true,
  };
  await mkdir(".local-build", { recursive: true });
  await writeFile(
    ".local-build/update-policy-runtime.json",
    JSON.stringify(report, null, 2),
  );
  console.log(JSON.stringify(report, null, 2));
} finally {
  cdp.close();
}
