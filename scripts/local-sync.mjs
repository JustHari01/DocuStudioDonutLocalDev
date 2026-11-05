#!/usr/bin/env node
// Run the repository's real self-hosted sync backend with durable local S3.
import { spawn, spawnSync } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dataRoot = join(root, ".local-sync");
const tools = join(root, ".local-build", "tools");
mkdirSync(dataRoot, { recursive: true });
mkdirSync(join(dataRoot, "storage"), { recursive: true });
mkdirSync(tools, { recursive: true });
const configPath = join(dataRoot, "config.json");
const config = existsSync(configPath)
  ? JSON.parse(readFileSync(configPath, "utf8"))
  : {
      syncToken: randomBytes(32).toString("hex"),
      accessKey: randomBytes(16).toString("hex"),
      secretKey: randomBytes(32).toString("hex"),
      syncPort: 3929,
      storagePort: 3930,
    };
if (!existsSync(configPath))
  writeFileSync(configPath, JSON.stringify(config, null, 2), { mode: 0o600 });

const binary = join(tools, "rclone.exe");
if (!existsSync(binary)) {
  if (process.platform !== "win32" || process.arch !== "x64")
    throw new Error("This local setup currently supports Windows x64");
  const response = await fetch(
    "https://downloads.rclone.org/v1.75.1/rclone-v1.75.1-windows-amd64.zip",
  );
  if (!response.ok)
    throw new Error(`Rclone download failed: HTTP ${response.status}`);
  const archive = Buffer.from(await response.arrayBuffer());
  const hash = createHash("sha256").update(archive).digest("hex");
  if (
    hash !== "200eb602c126d82aa38b51e0f6b9ae837473ff99b51278d3f6f837574c494d6e"
  )
    throw new Error("Rclone archive checksum mismatch");
  const archivePath = join(tools, "rclone.zip");
  const extractPath = join(tools, "rclone");
  writeFileSync(archivePath, archive);
  const result = spawnSync(
    "powershell.exe",
    [
      "-NoProfile",
      "-Command",
      "Expand-Archive -LiteralPath $env:DONUT_SYNC_ARCHIVE -DestinationPath $env:DONUT_SYNC_EXTRACT -Force",
    ],
    {
      env: {
        ...process.env,
        DONUT_SYNC_ARCHIVE: archivePath,
        DONUT_SYNC_EXTRACT: extractPath,
      },
      stdio: "inherit",
      windowsHide: true,
    },
  );
  if (result.status !== 0) throw new Error("Cannot extract rclone");
  writeFileSync(
    binary,
    readFileSync(
      join(extractPath, "rclone-v1.75.1-windows-amd64", "rclone.exe"),
    ),
  );
}

const syncRoot = join(root, "donut-sync");
const cargoTarget = process.env.CARGO_TARGET_DIR
  ? resolve(root, process.env.CARGO_TARGET_DIR)
  : join(root, ".local-build", "local-sync-target");
const built = spawnSync(
  "cargo",
  ["build", "--locked", "--bin", "donut-sync"],
  {
    cwd: syncRoot,
    env: { ...process.env, CARGO_TARGET_DIR: cargoTarget },
    stdio: "inherit",
    windowsHide: true,
  },
);
if (built.status !== 0) process.exit(built.status ?? 1);

const children = [];
function stop() {
  for (const child of children) child.kill();
}
process.on("SIGINT", () => {
  stop();
  process.exit(0);
});
process.on("SIGTERM", () => {
  stop();
  process.exit(0);
});
process.on("exit", stop);
function start(command, args, env = process.env, cwd = root) {
  const child = spawn(command, args, {
    env,
    cwd,
    stdio: "inherit",
    windowsHide: true,
  });
  children.push(child);
  child.on("error", (error) => {
    console.error(error.message);
    stop();
    process.exit(1);
  });
  child.on("exit", (code) => {
    stop();
    process.exit(code ?? 1);
  });
  return child;
}
start(binary, [
  "serve",
  "s3",
  join(dataRoot, "storage"),
  "--addr",
  `127.0.0.1:${config.storagePort}`,
  "--auth-key",
  `${config.accessKey},${config.secretKey}`,
  "--force-path-style",
  "--vfs-cache-mode",
  "writes",
]);
start(
  join(cargoTarget, "debug", process.platform === "win32" ? "donut-sync.exe" : "donut-sync"),
  [],
  {
    ...process.env,
    HOST: "127.0.0.1",
    PORT: String(config.syncPort),
    SYNC_TOKEN: config.syncToken,
    S3_ENDPOINT: `http://127.0.0.1:${config.storagePort}`,
    S3_PUBLIC_ENDPOINT: `http://127.0.0.1:${config.storagePort}`,
    S3_ACCESS_KEY_ID: config.accessKey,
    S3_SECRET_ACCESS_KEY: config.secretKey,
    S3_BUCKET: "donut-local",
    S3_FORCE_PATH_STYLE: "true",
    // This server uses its own local credentials rather than vendor JWTs.
    SYNC_JWT_PUBLIC_KEY: "",
    BACKEND_INTERNAL_URL: "",
    BACKEND_INTERNAL_KEY: "",
  },
  syncRoot,
);
console.log(
  `Local sync: http://127.0.0.1:${config.syncPort}. Connection token is saved in ${configPath}.`,
);
