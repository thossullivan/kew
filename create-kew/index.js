#!/usr/bin/env node
"use strict";

// create-kew is a launcher, not a wizard: it installs the kew CLI with uv and
// hands every flag to `kew init`, which owns detection, questions and config.

const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

// The published kew-cli from PyPI, held to this launcher's own major version:
// create-kew X.Y.Z installs kew-cli >=X,<X+1, so an old launcher never pulls
// a kew whose `kew init` flags it does not know. The [serve] extra carries the
// web console; without it `kew serve` has no UI. The release workflow
// publishes this package to npm only after kew-cli reaches PyPI. The spec is
// one argument to uv (no shell), so its `<` and `>` are never redirections.
const LAUNCHER_VERSION = require("./package.json").version;
const MAJOR = /^(\d+)\./.exec(LAUNCHER_VERSION);
if (!MAJOR) {
  process.stderr.write(`create-kew: cannot read a major version from ${LAUNCHER_VERSION}\n`);
  process.exit(1);
}
const KEW_SPEC = `kew-cli[serve]>=${MAJOR[1]},<${Number(MAJOR[1]) + 1}`;

const UV_INSTALL = "curl -LsSf https://astral.sh/uv/install.sh | sh";
const KEW_INSTALL = `uv tool install "${KEW_SPEC}"`;

const USAGE = `
create-kew — install kew and run \`kew init\`

Usage: npx create-kew [kew init flags]

  1. Checks that uv is installed
  2. Installs the kew CLI: ${KEW_INSTALL}
  3. Runs \`kew init\` with every flag you passed, for example:
       npx create-kew --runner codex --yes

See \`kew init --help\` for the flags. Docs: https://kew.dev/docs
`;

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

function exitCode(result) {
  return typeof result.status === "number" ? result.status : 1;
}

function kewExecutable() {
  const name = process.platform === "win32" ? "kew.exe" : "kew";
  const dir = spawnSync("uv", ["tool", "dir", "--bin"], { encoding: "utf-8" });
  const binDir = exitCode(dir) === 0 ? dir.stdout.trim() : "";
  const installed = binDir ? path.join(binDir, name) : "";
  return installed && fs.existsSync(installed) ? installed : "kew";
}

function main(args) {
  if (args.includes("--help") || args.includes("-h")) {
    console.log(USAGE);
    return 0;
  }

  const uv = spawnSync("uv", ["--version"], { stdio: "ignore" });
  if (uv.error || exitCode(uv) !== 0) {
    fail(
      "create-kew needs uv, which was not found on PATH.\n" +
        `Install uv:   ${UV_INSTALL}\n` +
        `Then install kew:   ${KEW_INSTALL}`
    );
  }

  const install = spawnSync("uv", ["tool", "install", KEW_SPEC], { stdio: "inherit" });
  if (install.error || exitCode(install) !== 0) {
    fail(`Installing kew failed. Install it manually:   ${KEW_INSTALL}`);
  }

  const init = spawnSync(kewExecutable(), ["init", ...args], { stdio: "inherit" });
  if (init.error) {
    fail(
      `Could not run kew init: ${init.error.message}\n` +
        "Put uv's tool directory on PATH with `uv tool update-shell`, then run `kew init`."
    );
  }
  return exitCode(init);
}

process.exitCode = main(process.argv.slice(2));
