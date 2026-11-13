#!/usr/bin/env node
/**
 * Fail a deployment unless the displayed install command resolves.
 *
 * Source installs must name a public GitHub repository, an explicit ref, and
 * an existing subdirectory. PyPI installs (`pip install`, `uv tool install`,
 * `pipx install`) must name a published project that declares every extra
 * the command asks for.
 * Network and authentication failures block deployment.
 */

import { execFile } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';

// The same module the pages render from, so the gate checks the repository
// the site names (REPO_SLUG) and never a copy of it.
import { INSTALL } from '../src/lib/data/install.js';
import { pypiTarget, verifyPyPIInstall, verifySourceInstall } from './install-gate.mjs';

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

const command = INSTALL.trim();

if (pypiTarget(command) === null) {
  const readme = await readFile(README, 'utf8');
  const result = await verifySourceInstall(command, readme, runGh);
  if (result.ok) {
    console.log(`install: source command resolves: ${result.reason}`);
  } else {
    console.error(`install gate: ${result.reason}`);
    process.exitCode = 1;
  }
} else {
  const result = await verifyPyPIInstall(command, fetchProject);
  if (result.ok) {
    console.log(`install: "${command}" resolves: ${result.reason}`);
  } else {
    console.error(`install gate: ${result.reason}`);
    process.exitCode = 1;
  }
}
