#!/usr/bin/env node
/**
 * Fold the docs into the site build so one directory can be deployed.
 *
 * Locally, Caddy serves two separate trees behind one origin: `www/build` at
 * `/` and the mkdocs output at `/docs/` (see Caddyfile, which strips the
 * prefix with handle_path). Static hosts do not mount volumes — they take one
 * directory — so the deployable artifact has to be a single tree with the docs
 * physically at `build/docs/`.
 *
 * That is strictly closer to the truth than the mount was: the docs now live
 * at the path they are served from, so mkdocs' relative asset references
 * resolve the same way locally and in production.
 *
 * Run via `npm run dist`. `npm run serve` is unchanged and still uses the
 * two-mount Caddy setup, which stays the local mirror of production.
 */

import { cp, rm, stat, readdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');

const BUILD = resolve(root, 'build');
const DOCS_SRC = resolve(root, '../site');
const DOCS_DEST = resolve(BUILD, 'docs');

/** Directory exists and is non-empty. */
async function ready(path) {
  try {
    const s = await stat(path);
    if (!s.isDirectory()) return false;
    return (await readdir(path)).length > 0;
  } catch {
    return false;
  }
}

if (!(await ready(BUILD))) {
  console.error('dist: www/build is missing or empty — run `npm run build` first.');
  process.exit(1);
}

if (!(await ready(DOCS_SRC))) {
  console.error(
    '\ndist: the docs build is missing at ../site.\n\n' +
      '    kew/.venv/bin/mkdocs build      (from the repo root)\n\n' +
      'Deploying the marketing site without the docs would ship a page whose\n' +
      '"Read a real evidence pack" door 404s, so this is a hard stop.\n'
  );
  process.exit(1);
}

// Replace rather than merge: a stale page deleted from the docs should not
// survive in the deployed tree just because it was there last time.
await rm(DOCS_DEST, { recursive: true, force: true });
await cp(DOCS_SRC, DOCS_DEST, { recursive: true });

if (!(await ready(resolve(DOCS_DEST, 'search')))) {
  console.warn('dist: warning — docs copied but search/ is absent; docs search will not work.');
}

const count = async (dir) => {
  let n = 0;
  for (const e of await readdir(dir, { withFileTypes: true, recursive: true })) {
    if (e.isFile()) n += 1;
  }
  return n;
};

console.log(`dist: build/ ready — ${await count(BUILD)} files, docs at build/docs/`);
