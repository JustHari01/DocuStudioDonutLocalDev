import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// tsrs currently ships no Windows binary. Keep the same checked TypeScript
// source on Windows with the TypeScript compiler already pinned by this repo.
const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const windows = process.platform === "win32";
const result = spawnSync(
  windows ? process.execPath : "tsrs",
  windows
    ? [join(root, "node_modules/typescript/bin/tsc"), ...process.argv.slice(2)]
    : process.argv.slice(2),
  { stdio: "inherit", cwd: process.cwd() },
);
if (result.error) throw result.error;
process.exit(result.status ?? 1);
