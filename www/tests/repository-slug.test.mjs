import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { copyFile, mkdir, mkdtemp, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, relative, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { INSTALL, INSTALL_PARTS, REPO_SLUG, REPO_URL } from '../src/lib/data/install.js';

const www = resolve(dirname(fileURLToPath(import.meta.url)), '..');

// Dated captures keep the repository they were recorded from: the evidence
// pack. The repo-stats snapshot is not one of them: it names no repository
// at all.
const CAPTURES = new Set(['src/lib/data/install.js', 'src/lib/data/pack-figures.js']);

async function files(directory) {
  const found = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) found.push(...(await files(path)));
    else if (/\.(svelte|ts|js|mjs|json)$/.test(entry.name)) found.push(path);
  }
  return found;
}

test('the install command and links are built from the one repository slug', () => {
  assert.equal(REPO_SLUG, 'thossullivan/kew');
  assert.equal(REPO_URL, `https://github.com/${REPO_SLUG}`);
  assert.equal(
    INSTALL,
    `uv tool install "kew-cli[serve] @ git+https://github.com/${REPO_SLUG}.git@main#subdirectory=kew"`
  );
});

test('the install command breaks after each @ and before #subdirectory and joins back exactly', () => {
  assert.equal(INSTALL_PARTS.join(''), INSTALL);
  assert.deepEqual(INSTALL_PARTS, [
    'uv tool install "kew-cli[serve] @',
    ` git+https://github.com/${REPO_SLUG}.git@`,
    'main',
    '#subdirectory=kew"'
  ]);
});

test('the install gate checks the command the site renders', async () => {
  const gate = await readFile(join(www, 'scripts/check-install.mjs'), 'utf8');
  assert.match(gate, /import \{ INSTALL \} from '\.\.\/src\/lib\/data\/install\.js';/);
  assert.doesNotMatch(gate, /thossullivan/);
});

test('no page or script names the repository outside the slug constant', async () => {
  const offenders = [];
  for (const path of [...(await files(join(www, 'src'))), ...(await files(join(www, 'scripts')))]) {
    const name = relative(www, path);
    if (CAPTURES.has(name)) continue;
    if ((await readFile(path, 'utf8')).includes(REPO_SLUG)) offenders.push(name);
  }
  assert.deepEqual(offenders, []);
});

// The development repository the statistics are read from. Deliberately not
// REPO_SLUG: the fetcher reads the repository it is given, never the slug the
// site links to.
const DEV_SLUG = 'example-owner/example-dev';

// A fake `gh` serving the REST pulls listing 100 to a page, for the slug in
// FAKE_SLUG only: 1,250 merged pull requests (1,000 on agent/ branches) and
// 50 closed without merging. Like the real gh, it applies `--jq` to the page;
// it knows only the fetcher's projection and refuses any other filter.
//   FAKE_BODY_BYTES  pad every pull request with a body this long, as the real
//                    listing carries full objects;
//   FAKE_OVERLAP     start every page after the first this many pull requests
//                    early, as a listing does when one closes mid-read;
//   FAKE_FAIL        fail the pulls listing as an API error does.
const FAKE_GH = `#!/usr/bin/env node
const args = process.argv.slice(2);
const path = args[1] ?? '';
if (args[0] !== 'api') process.exit(2);
if (path.startsWith('search/issues')) {
  if (!path.includes('repo:' + process.env.FAKE_SLUG + '+')) process.exit(3);
  console.log('77');
  process.exit(0);
}
const url = new URL(path, 'https://api.github.test/');
if (url.pathname !== '/repos/' + process.env.FAKE_SLUG + '/pulls') process.exit(4);
if (process.env.FAKE_FAIL) {
  process.stderr.write('HTTP 502: Bad Gateway (https://api.github.com/' + path + ')\\n');
  process.exit(1);
}
const perPage = Number(url.searchParams.get('per_page') ?? 30);
const page = Number(url.searchParams.get('page') ?? 1);
const body = 'x'.repeat(Number(process.env.FAKE_BODY_BYTES ?? 0));
const all = Array.from({ length: 1300 }, (_, i) => ({
  number: i + 1,
  head: { ref: i < 1000 ? 'agent/issue-' + i : 'feature-' + i },
  merged_at: i < 1250 ? new Date(Date.UTC(2026, 0, 1) + i * 3600e3).toISOString() : null,
  body
}));
const overlap = page > 1 ? Number(process.env.FAKE_OVERLAP ?? 0) : 0;
const start = Math.max(0, (page - 1) * perPage - overlap);
let out = all.slice(start, start + perPage);
const jq = args.indexOf('--jq');
if (jq !== -1) {
  if (args[jq + 1] !== '[.[] | {number, ref: .head.ref, merged_at}]') process.exit(5);
  out = out.map((pr) => ({ number: pr.number, ref: pr.head.ref, merged_at: pr.merged_at }));
}
process.stdout.write(JSON.stringify(out) + '\\n');
`;

// A fake `gh` whose pulls listing never ends: every page is full.
const ENDLESS_GH = `#!/bin/sh
case "$2" in
  search/*) echo 77 ;;
  *) cat "$FAKE_PAGE" ;;
esac
`;

const SNAPSHOT = { capturedAt: '2026-09-02', mergedPrs: 942 };

/**
 * The keys a statistics snapshot may carry. The page bundles the whole
 * snapshot, so anything else (a `repo` slug above all) would be published.
 */
const SNAPSHOT_KEYS = [
  'agentPrs',
  'agentShare',
  'capturedAt',
  'closedIssues',
  'firstMerge',
  'lastMerge',
  'mergedPrs'
];

/** Keys a snapshot carries beyond SNAPSHOT_KEYS. */
function extraSnapshotKeys(stats) {
  return Object.keys(stats).filter((key) => !SNAPSHOT_KEYS.includes(key));
}

test('the committed statistics snapshot names no repository', async () => {
  const stats = JSON.parse(await readFile(join(www, 'src/lib/data/repo-stats.json'), 'utf8'));

  assert.deepEqual(extraSnapshotKeys(stats), []);
});

test('a statistics snapshot carrying a repo key is refused', () => {
  assert.deepEqual(extraSnapshotKeys({ repo: 'owner/name', capturedAt: '2026-11-02' }), ['repo']);
});

/**
 * Run the fetcher from a copy of itself, so the committed snapshot is never
 * touched; `snapshot` seeds the copy's snapshot. `args` default to naming
 * DEV_SLUG. Resolves to the exit code, the output, and the snapshot the run
 * left.
 */
async function fetchStats(
  t,
  { gh = FAKE_GH, env = {}, args = ['--repo', DEV_SLUG], snapshot = null } = {}
) {
  const temporary = await mkdtemp(join(tmpdir(), 'kew-repo-stats-'));
  t.after(() => rm(temporary, { recursive: true, force: true }));
  const bin = join(temporary, 'bin');
  await mkdir(bin);
  if (gh !== null) await writeFile(join(bin, 'gh'), gh, { mode: 0o755 });
  // node for the fake, and without a gh: a system gh must never answer.
  const nodeBin = join(temporary, 'node-bin');
  await mkdir(nodeBin);
  await symlink(process.execPath, join(nodeBin, 'node'));
  const PATH = gh === null ? `${bin}:${nodeBin}` : `${bin}:${nodeBin}:/usr/bin:/bin`;
  const fullPage = join(temporary, 'full-page.json');
  const merged = '2026-01-01T00:00:00Z';
  await writeFile(
    fullPage,
    JSON.stringify(Array.from({ length: 100 }, (_, i) => ({ number: i + 1, ref: 'agent/x', merged_at: merged })))
  );
  for (const file of ['scripts/fetch-repo-stats.mjs']) {
    await mkdir(dirname(join(temporary, file)), { recursive: true });
    await copyFile(join(www, file), join(temporary, file));
  }
  const out = join(temporary, 'src/lib/data/repo-stats.json');
  await mkdir(dirname(out), { recursive: true });
  if (snapshot) await writeFile(out, JSON.stringify(snapshot));

  const run = await new Promise((done) => {
    execFile(
      process.execPath,
      [join(temporary, 'scripts/fetch-repo-stats.mjs'), ...args],
      {
        env: { ...withoutStatsRepo(process.env), PATH, FAKE_SLUG: DEV_SLUG, FAKE_PAGE: fullPage, ...env }
      },
      (error, stdout, stderr) => done({ code: error ? (error.code ?? 1) : 0, stdout, stderr })
    );
  });
  const stats = await readFile(out, 'utf8').then(JSON.parse, () => null);
  return { ...run, stats };
}

/** The environment without an inherited KEW_STATS_REPO, so each test names its own. */
function withoutStatsRepo(env) {
  const { KEW_STATS_REPO: _, ...rest } = env;
  return rest;
}

test('the repo stats fetcher counts merged pull requests past the first thousand', async (t) => {
  const { code, stderr, stats } = await fetchStats(t);

  assert.equal(code, 0, stderr);
  assert.deepEqual(extraSnapshotKeys(stats), []);
  assert.equal(stats.mergedPrs, 1250);
  assert.equal(stats.agentPrs, 1000);
  assert.equal(stats.agentShare, 80);
  assert.equal(stats.closedIssues, 77);
  assert.equal(stats.firstMerge, '2026-01-01');
  assert.equal(stats.lastMerge, '2026-02-22');
});

test('the repo stats fetcher reads a listing page larger than a mebibyte', async (t) => {
  // 100 pull requests of 12 kB each is 1.2 MB a page, short of the 1.9 MB a
  // real page of full pull request objects weighs.
  const { code, stderr, stats } = await fetchStats(t, { env: { FAKE_BODY_BYTES: '12000' } });

  assert.equal(code, 0, stderr);
  assert.equal(stats.mergedPrs, 1250);
  assert.equal(stats.agentPrs, 1000);
});

test('the repo stats fetcher counts a pull request listed on two pages once', async (t) => {
  const { code, stderr, stats } = await fetchStats(t, { env: { FAKE_OVERLAP: '10' } });

  assert.equal(code, 0, stderr);
  assert.equal(stats.mergedPrs, 1250);
  assert.equal(stats.agentPrs, 1000);
  assert.equal(stats.agentShare, 80);
});

test('the repo stats fetcher refuses a listing that runs past its page cap', async (t) => {
  const { code, stderr, stats } = await fetchStats(t, { gh: ENDLESS_GH });

  assert.notEqual(code, 0);
  assert.match(stderr, /more than 200 pages/);
  assert.doesNotMatch(stderr, /gh unavailable|not installed/);
  assert.equal(stats, null);
});

test('a failed refresh names the real error and leaves the snapshot', async (t) => {
  const { code, stderr, stats } = await fetchStats(t, { env: { FAKE_FAIL: '1' }, snapshot: SNAPSHOT });

  assert.notEqual(code, 0);
  assert.match(stderr, /HTTP 502: Bad Gateway/);
  assert.doesNotMatch(stderr, /gh unavailable|not installed/);
  assert.deepEqual(stats, SNAPSHOT);
});

test('the fetcher reads the repository named in KEW_STATS_REPO', async (t) => {
  // The fake gh answers DEV_SLUG only, so a full count proves the slug was read.
  const { code, stderr, stats } = await fetchStats(t, { args: [], env: { KEW_STATS_REPO: DEV_SLUG } });

  assert.equal(code, 0, stderr);
  assert.equal(stats.mergedPrs, 1250);
  assert.deepEqual(extraSnapshotKeys(stats), []);
});

test('the fetcher reads --repo=<owner/name> as well as --repo <owner/name>', async (t) => {
  const { code, stderr, stats } = await fetchStats(t, { args: [`--repo=${DEV_SLUG}`] });

  assert.equal(code, 0, stderr);
  assert.equal(stats.mergedPrs, 1250);
});

test('the fetcher refuses to run without a repository and leaves the snapshot', async (t) => {
  // A gh that would answer for the site's own slug: the fetcher must not fall back to it.
  const { code, stdout, stderr, stats } = await fetchStats(t, {
    args: [],
    env: { FAKE_SLUG: REPO_SLUG },
    snapshot: SNAPSHOT
  });

  assert.equal(code, 2);
  assert.match(stderr, /--repo <owner\/name>.*KEW_STATS_REPO/);
  assert.equal(stdout, '');
  assert.deepEqual(stats, SNAPSHOT);
});

test('the fetcher refuses a repository that is not owner/name', async (t) => {
  for (const repo of ['', 'kew', 'owner/name/extra', '../owner/name', 'owner/name?x=1']) {
    const { code, stderr, stats } = await fetchStats(t, { args: ['--repo', repo], snapshot: SNAPSHOT });

    assert.equal(code, 2, `${repo}: ${stderr}`);
    assert.deepEqual(stats, SNAPSHOT);
  }
});

test('a refresh without gh fails and leaves the snapshot', async (t) => {
  const { code, stderr, stats } = await fetchStats(t, { gh: null, snapshot: SNAPSHOT });

  assert.equal(code, 1);
  assert.match(stderr, /gh is not installed/);
  assert.match(stderr, /snapshot was left as it was/);
  assert.deepEqual(stats, SNAPSHOT);
});
