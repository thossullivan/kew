import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { PAGES, pageRoute, parseMkdocs, renderLlmsTxt } from '../scripts/llms-txt.mjs';

const www = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const mkdocsText = readFileSync(resolve(www, '../mkdocs.yml'), 'utf8');

/** A fake built site holding an index.html for each given source path. */
function fakeSite(paths) {
  const site = mkdtempSync(join(tmpdir(), 'llms-site-'));
  for (const path of paths) {
    const dir = join(site, pageRoute(path));
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, 'index.html'), '<html></html>');
  }
  return site;
}

/** The URLs of every markdown link in an llms.txt. */
function links(text) {
  return [...text.matchAll(/\]\(([^)]+)\)/g)].map((m) => m[1]);
}

test('every page llms.txt links is in the real mkdocs nav', () => {
  const navPaths = parseMkdocs(mkdocsText).nav.map(([, path]) => path);
  for (const path of PAGES) assert.ok(navPaths.includes(path), `${path} missing from nav`);
});

test('llms.txt has the llmstxt.org shape: title, summary, then linked pages', () => {
  const text = renderLlmsTxt(mkdocsText, fakeSite(PAGES));
  const lines = text.split('\n');
  assert.equal(lines[0], '# kew');
  assert.match(lines[2], /^> \S/);
  assert.ok(lines.includes('## Docs'));
  assert.deepEqual(links(text), [
    'https://kew.dev/docs/getting-started/',
    'https://kew.dev/docs/agents/',
    'https://kew.dev/docs/cli/',
    'https://kew.dev/docs/cli/exit-codes/',
    'https://kew.dev/docs/configuration/'
  ]);
  // Titles come from the nav, not from a hand-written copy.
  assert.match(text, /^- \[CLI reference: Exit codes\]\(https:\/\/kew\.dev\/docs\/cli\/exit-codes\/\)$/m);
});

test('a nav page the built site lacks is a hard stop, not a dead link', () => {
  const site = fakeSite(PAGES.filter((p) => p !== 'cli/exit-codes.md'));
  assert.throws(() => renderLlmsTxt(mkdocsText, site), /cli\/exit-codes\.md has no built page/);
});

test('a linked page dropped from the nav is a hard stop', () => {
  const text = mkdocsText.replace(/^\s*- Agents: agents\.md\n/m, '');
  assert.notEqual(text, mkdocsText);
  assert.throws(() => renderLlmsTxt(text, fakeSite(PAGES)), /agents\.md is not in the mkdocs nav/);
});

test('the CLI writes llms.txt and exits non-zero when a page is missing', () => {
  const out = join(mkdtempSync(join(tmpdir(), 'llms-out-')), 'llms.txt');
  const script = resolve(www, 'scripts/llms-txt.mjs');
  execFileSync('node', [script, '--site', fakeSite(PAGES), '--out', out]);
  assert.match(readFileSync(out, 'utf8'), /^# kew\n/);

  const bad = join(dirname(out), 'bad.txt');
  const run = spawnSync('node', [script, '--site', fakeSite([]), '--out', bad]);
  assert.notEqual(run.status, 0);
  assert.equal(existsSync(bad), false);
});

const builtSite = resolve(www, '../site');
test(
  'every llms.txt link resolves to a page in the built site/',
  { skip: existsSync(builtSite) ? false : 'no ../site; run mkdocs build from the repo root' },
  () => {
    const text = renderLlmsTxt(mkdocsText, builtSite);
    const base = parseMkdocs(mkdocsText).url;
    for (const url of links(text)) {
      assert.ok(url.startsWith(base), url);
      const page = join(builtSite, url.slice(base.length), 'index.html');
      assert.ok(existsSync(page), `${url} -> ${page}`);
    }
  }
);

/**
 * deploy.sh's guards, run against a scratch repo holding a copy of the script.
 * `npm` and `uvx` are stubbed to record that they ran and fail, so a guard
 * that passes is visible as a marker and nothing real is built or published.
 */
function deployRepo({ branch = 'main', dirty = false } = {}) {
  const root = mkdtempSync(join(tmpdir(), 'deploy-guard-'));
  const git = (...args) =>
    execFileSync('git', ['-c', 'user.name=t', '-c', 'user.email=t@t', ...args], { cwd: root });
  git('init', '-q', '-b', 'main');
  mkdirSync(join(root, 'www'));
  copyFileSync(resolve(www, 'deploy.sh'), join(root, 'www/deploy.sh'));
  git('add', '.');
  git('commit', '-q', '-m', 'init');
  // deploy.sh refuses a HEAD that is not the local origin/main ref.
  git('update-ref', 'refs/remotes/origin/main', 'HEAD');
  if (branch !== 'main') git('checkout', '-q', '-b', branch);
  if (dirty) writeFileSync(join(root, 'untracked.md'), 'never committed\n');

  const marker = join(root, '.built');
  // A dirty tree must be dirty for git, not for the marker: keep it out of it.
  writeFileSync(join(root, '.git/info/exclude'), '.built\n');

  // Exported bash functions shadow the real tools. Stub binaries on PATH would
  // not: tmp is often mounted noexec, and bash then silently runs the real npm.
  const stubs = {};
  for (const tool of ['npm', 'uvx']) {
    stubs[`BASH_FUNC_${tool}%%`] = `() { echo ${tool} >> '${marker}'; return 1; }`;
  }
  const run = spawnSync('bash', [join(root, 'www/deploy.sh'), '--dry-run'], {
    encoding: 'utf8',
    env: { ...process.env, ...stubs }
  });
  return { run, built: existsSync(marker) };
}

test('deploy.sh --dry-run refuses a dirty checkout before building anything', () => {
  const { run, built } = deployRepo({ dirty: true });
  assert.notEqual(run.status, 0);
  assert.match(run.stderr, /dirty checkout/);
  assert.equal(built, false);
});

test('deploy.sh --dry-run refuses a branch other than main before building anything', () => {
  const { run, built } = deployRepo({ branch: 'feature' });
  assert.notEqual(run.status, 0);
  assert.match(run.stderr, /refusing to deploy from 'feature'/);
  assert.equal(built, false);
});

test('deploy.sh --dry-run on a clean main gets past the guards', () => {
  const { run, built } = deployRepo();
  assert.equal(built, true, run.stderr);
  assert.doesNotMatch(run.stderr, /refusing/);
});
