#!/usr/bin/env node
import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  copyFileSync,
  cpSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  readlinkSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
process.env.TAURI_CONFIG = JSON.stringify({
  ...JSON.parse(process.env.TAURI_CONFIG || "{}"),
  productName: "Donut Local",
  identifier: "com.donutbrowser.localfeatures",
});
// Local builds retain stable version numbering while official updates notify only.
process.env.STABLE_RELEASE ??= "1";
const cargoTargetDirectory = process.env.CARGO_TARGET_DIR
  ? resolve(root, process.env.CARGO_TARGET_DIR)
  : join(root, "src-tauri", "target");
// aws-lc-sys ships its own Windows assembly objects when NASM is unavailable.
if (process.platform === "win32") {
  process.env.AWS_LC_SYS_PREBUILT_NASM ??= "1";
}
function run(command, args, cwd = root) {
  const windowsPnpm = process.platform === "win32" && command === "pnpm";
  const options = {
    cwd,
    stdio: "inherit",
    shell: windowsPnpm,
  };
  // Arguments here are fixed script names, never user-provided shell text.
  const result = windowsPnpm
    ? spawnSync(["pnpm.cmd", ...args].join(" "), options)
    : spawnSync(command, args, options);
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}
function frontendBuildRoot() {
  if (process.platform !== "win32") return root;
  try {
    readlinkSync(join(root, "src", "app", "page.tsx"));
  } catch (error) {
    if (error.code !== "EISDIR") return root;
    // Node reports EISDIR for readlink on regular exFAT files. Webpack expects
    // EINVAL, so build the exported UI on NTFS while keeping source/data intact.
    const localAppData = process.env.LOCALAPPDATA;
    if (!localAppData)
      throw new Error("LOCALAPPDATA is required for the NTFS frontend build");
    const version = JSON.parse(
      readFileSync(join(root, "package.json"), "utf8"),
    ).version;
    const key = createHash("sha256")
      .update(`${root}:${version}`)
      .digest("hex")
      .slice(0, 12);
    const staging = join(localAppData, "DonutLocalBuild", key);
    mkdirSync(staging, { recursive: true });
    const cacheRoot = realpathSync(join(localAppData, "DonutLocalBuild"));
    const stagingRoot = realpathSync(staging);
    if (dirname(stagingRoot) !== cacheRoot)
      throw new Error("Unsafe frontend staging directory");
    for (const name of ["src", "public"]) {
      const target = join(staging, name);
      if (existsSync(target)) {
        if (dirname(realpathSync(target)) !== stagingRoot)
          throw new Error("Unsafe staged source directory");
        rmSync(target, { recursive: true, force: true });
      }
      cpSync(join(root, name), target, { recursive: true });
    }
    for (const entry of readdirSync(root, { withFileTypes: true })) {
      if (entry.isFile())
        copyFileSync(join(root, entry.name), join(staging, entry.name));
    }
    const digest = createHash("sha256")
      .update(readFileSync(join(root, "pnpm-lock.yaml")))
      .digest("hex");
    const marker = join(staging, ".dependency-lock-sha256");
    if (!existsSync(marker) || readFileSync(marker, "utf8") !== digest) {
      cpSync(join(root, "node_modules"), join(staging, "node_modules"), {
        recursive: true,
      });
      writeFileSync(marker, digest);
    }
    console.log(`Frontend build on NTFS: ${staging}`);
    return staging;
  }
  return root;
}
if (!process.argv.includes("--package-only")) {
  run("pnpm", ["licenses:generate"]);
  const frontendRoot = frontendBuildRoot();
  run(
    process.execPath,
    [
      join(frontendRoot, "node_modules", "next", "dist", "bin", "next"),
      "build",
      "--webpack",
    ],
    frontendRoot,
  );
  if (frontendRoot !== root) {
    const output = resolve(root, "dist");
    if (dirname(output) !== root)
      throw new Error("Unsafe frontend output directory");
    rmSync(output, { recursive: true, force: true });
    cpSync(join(frontendRoot, "dist"), output, { recursive: true });
  }
  run("pnpm", ["copy-proxy-binary"]);
  run(
    "cargo",
    [
      "build",
      "--locked",
      "--features",
      "local-features",
      "--bin",
      "donutbrowser",
    ],
    join(root, "src-tauri"),
  );
}

if (process.platform === "win32") {
  const output = join(root, "local-release", "Donut Local");
  mkdirSync(output, { recursive: true });
  const target = execFileSync("rustc", ["-vV"], { encoding: "utf8" }).match(
    /host:\s*(\S+)/,
  )?.[1];
  if (!target) throw new Error("Cannot determine Rust build target");
  for (const name of ["donutbrowser.exe", "donut-proxy.exe", "xray.exe"]) {
    const source =
      name === "xray.exe"
        ? join(root, "src-tauri", "binaries", `xray-${target}.exe`)
        : join(cargoTargetDirectory, "debug", name);
    if (!existsSync(source)) throw new Error(`Missing build output: ${source}`);
    copyFileSync(source, join(output, name));
  }
  const xrayLicense = join(root, "src-tauri", "binaries", "xray-LICENSE.txt");
  if (existsSync(xrayLicense))
    copyFileSync(xrayLicense, join(output, "Xray-core-LICENSE.txt"));
  copyFileSync(join(root, "LICENSE"), join(output, "LICENSE"));
  copyFileSync(
    join(root, "LOCAL_FEATURES.md"),
    join(output, "LOCAL_FEATURES.md"),
  );
  writeFileSync(join(output, ".portable"), "");
  writeFileSync(
    join(output, "START.cmd"),
    '@echo off\r\ncd /d "%~dp0"\r\nstart "" "%~dp0donutbrowser.exe"\r\n',
  );
  writeFileSync(
    join(output, "START_SYNC.cmd"),
    `@echo off\r\ncd /d "${root}"\r\nnode scripts\\local-sync.mjs\r\n`,
  );
  const version = JSON.parse(
    readFileSync(join(root, "package.json"), "utf8"),
  ).version;
  const files = Object.fromEntries(
    readdirSync(output, { withFileTypes: true })
      .filter((entry) => entry.isFile() && entry.name !== "BUILD_MANIFEST.json")
      .map((entry) => [
        entry.name,
        createHash("sha256")
          .update(readFileSync(join(output, entry.name)))
          .digest("hex"),
      ]),
  );
  // Every package gets fresh provenance; never inherit an older runtime PASS.
  writeFileSync(
    join(output, "BUILD_MANIFEST.json"),
    `${JSON.stringify(
      {
        version,
        builtAt: new Date().toISOString(),
        source: root,
        release: `https://github.com/donutbrowser/donutbrowser/releases/tag/v${version}`,
        buildProfile: "dev",
        cargoFeatures: ["local-features"],
        files,
        runtimeVerified: false,
      },
      null,
      2,
    )}\n`,
  );
  console.log(`Local feature build: ${output}`);
}
