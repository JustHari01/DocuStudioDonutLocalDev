import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { copyFile, readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const deadline = Date.now() + 15000;
let health;
let api;
while (Date.now() < deadline) {
  try {
    health = await fetch("http://127.0.0.1:51080/health", {
      signal: AbortSignal.timeout(1000),
    });
    api = await fetch("http://127.0.0.1:19108/v1/profiles", {
      signal: AbortSignal.timeout(1000),
    });
    if (health.ok && api.status === 401) break;
  } catch {}
  await new Promise((done) => setTimeout(done, 200));
}
assert.ok(health?.ok, "Persisted local MCP listener must be ready");
assert.equal(api?.status, 401, "REST must still require its local token");
let debugClosed = false;
try {
  await fetch("http://127.0.0.1:19471/json", {
    signal: AbortSignal.timeout(1000),
  });
} catch {
  debugClosed = true;
}
assert.ok(debugClosed, "Test debugging port must be closed");
const output = join(root, "local-release", "Donut Local");
const executable = join(output, "donutbrowser.exe");
const hash = async (path) =>
  createHash("sha256")
    .update(await readFile(path))
    .digest("hex");
const digest = await hash(executable);
assert.equal(
  digest,
  await hash(join(root, "src-tauri", "target", "debug", "donutbrowser.exe")),
);
const reportPath = join(root, ".local-build", "runtime-verification.json");
const report = JSON.parse(await readFile(reportPath, "utf8"));
report.normalStartupVerified = true;
report.testDebugPortClosed = true;
report.deliveredExecutableSha256 = digest;
await writeFile(reportPath, JSON.stringify(report, null, 2));
await copyFile(reportPath, join(output, "RUNTIME_VERIFICATION.json"));
const processInfo = JSON.parse(
  await readFile(join(root, ".local-build", "runtime-process.json"), "utf8"),
);
const manifest = {
  version: JSON.parse(await readFile(join(root, "package.json"), "utf8"))
    .version,
  source: root,
  executable,
  sha256: digest,
  buildProfile: "debug",
  cargoFeature: "local-features",
  pid: processInfo.pid,
  checkedAt: new Date().toISOString(),
  tests: [
    "local feature auth separation",
    "authenticated real MCP router",
    "page target startup race",
    "locale parity",
    "runtime API/MCP authorization",
    "profile creation and browser launch",
    "app and self-host storage restart persistence",
  ],
  limitation:
    "Wayfern page automation requires a real paid entitlement; vendor Cloud authorization preserved",
};
await writeFile(
  join(root, ".local-build", "build-manifest.json"),
  JSON.stringify(manifest, null, 2),
);
await writeFile(
  join(output, "BUILD_MANIFEST.json"),
  JSON.stringify(manifest, null, 2),
);
console.log(
  JSON.stringify(
    {
      normalStartupVerified: true,
      localMcpReady: true,
      restUnauthorizedStatus: api.status,
      testDebugPortClosed: debugClosed,
      sha256: digest,
      pid: processInfo.pid,
    },
    null,
    2,
  ),
);
