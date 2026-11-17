#!/usr/bin/env node
/**
 * Fail a deployment unless the displayed install commands resolve.
 *
 * Source installs must name a public GitHub repository, an explicit ref, and
 * an existing subdirectory. PyPI installs (`pip install`, `uv tool install`,
 * `pipx install`) must name a published project that declares every extra
 * the command asks for.
 * The npm launcher (`npx create-kew`) must be published, and the major
 * version of its latest release must equal that of the PyPI project's latest:
 * the launcher installs kew-cli of its own major version only.
 * Network and authentication failures block deployment.
 */

import { execFile } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';

// The same module the pages render from, so the gate checks the commands the
// site shows and copies (INSTALL and CREATE_KEW), never a copy of them.
import { CREATE_KEW, INSTALL } from '../src/lib/data/install.js';
import { pypiTarget, verifyNpmLauncher, verifyPyPIInstall, verifySourceInstall } from './install-gate.mjs';

const execFileAsync = promisify(execFile);
const here = dirname(fileURLToPath(import.meta.url));
const README = resolve(here, '../../README.md');

async function runGh(args) {
  return execFileAsync('gh', args, { timeout: 30_000 });
}

async function fetchProject(project) {
  const response = await fetch(`https://pypi.org/pypi/${project}/json`, {
    headers: { accept: 'application/json' },
    signal: AbortSignal.timeout(30_000)
  });
  return { status: response.status, json: response.status === 200 ? await response.json() : null };
}

async function fetchPackage(name) {
  // The abbreviated document: the dist-tags without every version's manifest.
  const response = await fetch(`https://registry.npmjs.org/${name}`, {
    headers: { accept: 'application/vnd.npm.install-v1+json' },
    signal: AbortSignal.timeout(30_000)
  });
  return { status: response.status, json: response.status === 200 ? await response.json() : null };
}

function report(result, passed) {
  if (result.ok) {
    console.log(passed(result.reason));
  } else {
    console.error(`install gate: ${result.reason}`);
    process.exitCode = 1;
  }
}

const command = INSTALL.trim();
const pypi = pypiTarget(command);

if (pypi === null) {
  const readme = await readFile(README, 'utf8');
  const result = await verifySourceInstall(command, readme, runGh);
  report(result, (reason) => `install: source command resolves: ${reason}`);
} else {
  const result = await verifyPyPIInstall(command, fetchProject);
  report(result, (reason) => `install: "${command}" resolves: ${reason}`);
}

// The launcher installs kew-cli from PyPI, so it is held to the project the
// PyPI command names; beside a source command there is nothing to hold it to.
const launcher = CREATE_KEW.trim();
if (pypi === null) {
  report(
    { ok: false, reason: `"${launcher}" installs from PyPI, but the install command is not a PyPI command` },
    () => ''
  );
} else {
  const result = await verifyNpmLauncher(launcher, pypi.project, fetchPackage, fetchProject);
  report(result, (reason) => `install: "${launcher}" resolves: ${reason}`);
}
