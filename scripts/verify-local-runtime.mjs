import assert from "node:assert/strict";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { CdpClient } from "../e2e/lib/cdp.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const reportPath = join(root, ".local-build", "runtime-verification.json");
const afterRestart = process.argv.includes("--after-restart");
const cdp = await CdpClient.connect(19471);
await cdp.waitFor("Boolean(window.__TAURI_INTERNALS__?.invoke)", {
  description: "Tauri IPC ready",
});
if (afterRestart) {
  await cdp.waitFor(
    "window.__TAURI_INTERNALS__.invoke('get_mcp_server_status')",
    { description: "Persisted MCP startup" },
  );
}
const invoke = (command, args = {}) =>
  cdp.evaluate(
    `window.__TAURI_INTERNALS__.invoke(${JSON.stringify(command)}, ${JSON.stringify(args)})`,
  );
try {
  const features = await invoke("get_local_features");
  assert.equal(features.enabled, true);
  assert.equal(features.browserAutomation, true);
  assert.equal(features.fingerprintConfiguration, true);
  assert.equal(await invoke("cloud_get_user"), null);
  let port = (await invoke("get_mcp_config"))?.port;
  if (afterRestart) assert.ok(port, "MCP must start from persisted settings");
  else if (!port) port = await invoke("start_mcp_server");
  const settings = await invoke("get_app_settings");
  assert.ok(settings.mcp_token);
  const endpoint = `http://127.0.0.1:${port}/mcp`;
  const unauthorized = await fetch(endpoint, { method: "POST", body: "{}" });
  assert.equal(unauthorized.status, 401);
  let session;
  let requestId = 1;
  async function rpc(method, params = {}) {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${settings.mcp_token}`,
        ...(session ? { "mcp-session-id": session } : {}),
      },
      body: JSON.stringify({ jsonrpc: "2.0", id: requestId++, method, params }),
      signal: AbortSignal.timeout(90_000),
    });
    assert.equal(response.status, 200);
    session = response.headers.get("mcp-session-id") || session;
    const value = await response.json();
    if (value.error) throw new Error(JSON.stringify(value.error));
    return value.result;
  }
  const initialized = await rpc("initialize", {
    protocolVersion: "2025-03-26",
    capabilities: {},
    clientInfo: { name: "local-verification", version: "1" },
  });
  assert.ok(initialized.serverInfo);
  await fetch(endpoint, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${settings.mcp_token}`,
      "mcp-session-id": session,
    },
    body: JSON.stringify({
      jsonrpc: "2.0",
      method: "notifications/initialized",
    }),
  });
  const toolList = await rpc("tools/list");
  assert.ok(toolList.tools.some((tool) => tool.name === "add_extension"));
  async function tool(name, args = {}) {
    const result = await rpc("tools/call", { name, arguments: args });
    if (result.isError) throw new Error(JSON.stringify(result.content));
    if (!result.content) return result;
    const text = result.content.find((item) => item.type === "text")?.text;
    if (text === undefined) return result;
    try {
      return JSON.parse(text);
    } catch {
      return result;
    }
  }
  const sync = JSON.parse(
    await readFile(join(root, ".local-sync", "config.json"), "utf8"),
  );
  const syncEndpoint = `http://127.0.0.1:${sync.syncPort}`;
  const readiness = await (await fetch(`${syncEndpoint}/readyz`)).json();
  assert.equal(readiness.s3, true);
  const objectKey = "local-verification/roundtrip.txt";
  const payload = "Donut local sync durable roundtrip";
  async function syncCall(route, body) {
    const response = await fetch(`${syncEndpoint}/v1/objects/${route}`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${sync.syncToken}`,
      },
      body: JSON.stringify(body),
    });
    assert.equal(response.status, 200);
    return response.json();
  }
  let report;
  if (afterRestart) {
    report = JSON.parse(await readFile(reportPath, "utf8"));
    const extensions = await tool("list_extensions");
    assert.ok(
      extensions.some((extension) => extension.id === report.extensionId),
      "Extension metadata must persist",
    );
    const groups = await tool("list_extension_groups");
    assert.ok(
      groups.some((group) => group.id === report.extensionGroupId),
      "Extension group must persist",
    );
    const savedSync = await invoke("get_sync_settings");
    assert.equal(savedSync.sync_server_url, syncEndpoint);
    assert.equal(savedSync.sync_token, sync.syncToken);
    report.appRestartVerified = true;
  } else {
    const fixture = join(root, ".local-build", "fixtures", "extension");
    await mkdir(fixture, { recursive: true });
    await writeFile(
      join(fixture, "manifest.json"),
      JSON.stringify({
        manifest_version: 3,
        name: "Local feature verification",
        version: "1.0.0",
      }),
    );
    const extensions = await tool("list_extensions");
    const extension =
      extensions.find((item) => item.name === "Local feature verification") ??
      (await tool("add_extension", {
        name: "Local feature verification",
        path: fixture,
      }));
    const existingGroups = await tool("list_extension_groups");
    const group =
      existingGroups.find(
        (item) => item.name === "Local feature verification",
      ) ??
      (await tool("create_extension_group", {
        name: "Local feature verification",
        extension_ids: [extension.id],
      }));
    await tool("add_extension_to_group", {
      group_id: group.id,
      extension_id: extension.id,
    });
    await invoke("save_sync_settings", {
      syncServerUrl: syncEndpoint,
      syncToken: sync.syncToken,
    });
    const upload = await syncCall("presign-upload", {
      key: objectKey,
      contentType: "text/plain",
    });
    const put = await fetch(upload.url, {
      method: "PUT",
      headers: { "content-type": "text/plain" },
      body: payload,
    });
    assert.ok(put.ok);
    report = {
      features,
      signedOut: true,
      mcpAuthorizationChecked: true,
      realMcpTools: toolList.tools.length,
      extensionId: extension.id,
      extensionGroupId: group.id,
      syncReadiness: readiness,
      syncRoundtrip: true,
      appRestartVerified: false,
    };
    try {
      const before = await tool("list_profiles");
      const existing = (before.profiles ?? before).find(
        (item) => item.name === "Local runtime verification",
      );
      if (!existing)
        await tool("create_profile", {
          name: "Local runtime verification",
          browser: "wayfern",
          version: "152.0.7977.64",
        });
      const profiles = await tool("list_profiles");
      report.profileId =
        profiles.profiles?.find(
          (item) => item.name === "Local runtime verification",
        )?.id ??
        profiles.find?.((item) => item.name === "Local runtime verification")
          ?.id;
      report.profileCreation = "passed";
    } catch (error) {
      report.profileCreation = "engine-or-runtime-refusal";
      report.profileCreationError = error.message;
    }
  }
  const download = await syncCall("presign-download", { key: objectKey });
  assert.equal(await (await fetch(download.url)).text(), payload);
  const stat = await syncCall("stat", { key: objectKey });
  assert.equal(stat.exists, true);
  if (process.argv.includes("--rest-actions")) {
    const current = await invoke("get_app_settings");
    current.api_enabled = true;
    current.api_port = 19108;
    current.language = "vi";
    await invoke("save_app_settings", { settings: current });
    const apiPort = await invoke("start_api_server", { port: 19108 });
    const saved = await invoke("get_app_settings");
    const base = `http://127.0.0.1:${apiPort}/v1/profiles`;
    assert.equal((await fetch(base)).status, 401);
    const headers = { authorization: `Bearer ${saved.api_token}` };
    assert.equal((await fetch(base, { headers })).status, 200);
    assert.equal(
      (
        await fetch(`${base}/${report.profileId}/kill`, {
          method: "POST",
          headers,
        })
      ).status,
      204,
    );
    report.restAuthorizationChecked = true;
    report.restAutomationHandler = "passed";
    report.apiPort = apiPort;
  }
  if (process.argv.includes("--engine-actions") && report.profileId) {
    const fixtureServer = createServer((_request, response) => {
      response.setHeader("content-type", "text/html");
      response.end(
        "<!doctype html><title>Donut Local Automation</title><h1 id='proof'>Local automation fixture</h1>",
      );
    });
    await new Promise((resolve) =>
      fixtureServer.listen(0, "127.0.0.1", resolve),
    );
    try {
      const url = `http://127.0.0.1:${fixtureServer.address().port}/`;
      await tool("run_profile", { profile_id: report.profileId, url });
      report.browserLaunch = "passed";
      report.pageInfo = await tool("get_page_info", {
        profile_id: report.profileId,
      });
      report.javascriptResult = await tool("evaluate_javascript", {
        profile_id: report.profileId,
        expression: "document.getElementById('proof').textContent",
      });
      report.browserAutomation = "passed";
    } catch (error) {
      report.browserAutomation = "engine-or-runtime-refusal";
      report.browserAutomationError = error.message;
    } finally {
      try {
        await tool("kill_profile", { profile_id: report.profileId });
      } catch (error) {
        report.browserStopError = error.message;
      }
      await new Promise((resolve) => fixtureServer.close(resolve));
    }
  }
  report.checkedAt = new Date().toISOString();
  await writeFile(reportPath, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
} finally {
  cdp.close();
}
