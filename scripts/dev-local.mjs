#!/usr/bin/env node

import { spawn } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const localDataRoot = resolve(projectRoot, ".local-dev-data");
const localConfigPath = resolve(localDataRoot, "tauri.local-dev.conf.json");
const env = {
  ...process.env,
  DONUT_LOCAL_DEV: "1",
  NEXT_PUBLIC_DONUT_LOCAL_DEV: "1",
  DONUTBROWSER_DATA_ROOT: process.env.DONUTBROWSER_DATA_ROOT || localDataRoot,
  DONUTBROWSER_DATA_DIR:
    process.env.DONUTBROWSER_DATA_DIR || resolve(localDataRoot, "data"),
  DONUTBROWSER_CACHE_DIR:
    process.env.DONUTBROWSER_CACHE_DIR || resolve(localDataRoot, "cache"),
  DONUTBROWSER_EPHEMERAL_ROOT:
    process.env.DONUTBROWSER_EPHEMERAL_ROOT || resolve(localDataRoot, "ephemeral"),
};
const isWindows = process.platform === "win32";
const child = spawn(
  isWindows ? "pnpm.cmd" : "pnpm",
  [
    "tauri",
    "dev",
    "--features",
    "local-dev",
    "--config",
    localConfigPath,
  ],
  { cwd: projectRoot, env, stdio: "inherit", shell: isWindows },
);

child.on("error", (error) => {
  console.error(`Failed to start local dev mode: ${error.message}`);
  process.exit(1);
});
child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
  } else {
    process.exit(code ?? 1);
  }
});
