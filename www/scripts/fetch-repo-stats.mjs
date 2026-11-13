#!/usr/bin/env node
/**
 * Refreshes the "built with itself" figures, by hand, from a repository you
 * name.
 *
 * The committed snapshot, src/lib/data/repo-stats.json, is the only source the
 * build reads: `npm run build` never runs this script. The figures describe
 * kew's development repository, where its pull request history lives, and the
 * public repository the site links to is a snapshot with no such history, so
 * a build that fetched from the site's own slug would overwrite the page with
 * near-zero counts. That is why the repository to read is an explicit input
 * here and never src/lib/data/install.js.
 *
 *   npm run stats -- --repo <owner/name>       refresh the snapshot from that
 *   KEW_STATS_REPO=<owner/name> npm run stats  repository; a failed refresh
 *                                              exits 1 and leaves the snapshot
 *                                              as it was
 *
 * Without a repository it refuses to run (exit 2) and touches nothing. It
 * reads every merged pull request through `gh`, page by page, so the counts
 * never stop at a listing limit. Commit the refreshed snapshot through a PR.
 *
 * The snapshot carries `capturedAt`; the page prints it, because a number
 * with no date is a claim rather than evidence. It never records the
 * repository it was read from: the page bundles the whole snapshot, so a slug
 * written here would be published.
 */
import { execFileSync } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const OUT = resolve(dirname(fileURLToPath(import.meta.url)), '../src/lib/data/repo-stats.json');
const PER_PAGE = 100;
/** 20,000 pull requests. A listing longer than this is a fault, not a repository. */
const MAX_PAGES = 200;
/** gh's output, once projected, is a few kilobytes a page; this is headroom, not the plan. */
const MAX_BUFFER = 64 * 1024 * 1024;
/** Only the fields counted. A full page of pull request objects weighs about 2 MB. */
const PROJECTION = '[.[] | {number, ref: .head.ref, merged_at}]';
/** owner/name, as GitHub spells them. Anything else is refused before gh runs. */
const SLUG = /^[A-Za-z0-9-]+\/[A-Za-z0-9._-]+$/;

/** The repository named by --repo (or --repo=), else KEW_STATS_REPO, else null. */
function requestedRepo(argv, env) {
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--repo') return argv[i + 1] ?? '';
    if (argv[i].startsWith('--repo=')) return argv[i].slice('--repo='.length);
  }
  return env.KEW_STATS_REPO ?? null;
}

function gh(args) {
  return execFileSync('gh', args, {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
    maxBuffer: MAX_BUFFER
  });
}

/**
 * Every merged pull request, as { headRefName, mergedAt }, read 100 to a page
 * and keyed by number: a pull request closing mid-read shifts the listing, and
 * the same one then appears on two pages.
 */
function mergedPullRequests(repo) {
  const merged = new Map();
  for (let page = 1; page <= MAX_PAGES; page++) {
    const batch = JSON.parse(
      gh([
        'api',
        `repos/${repo}/pulls?state=closed&per_page=${PER_PAGE}&page=${page}`,
        '--jq',
        PROJECTION
      ])
    );
    for (const pr of batch) {
      if (pr.merged_at) merged.set(pr.number, { headRefName: pr.ref, mergedAt: pr.merged_at });
    }
    if (batch.length < PER_PAGE) return [...merged.values()];
  }
  throw new Error(
    `the pull request listing ran to more than ${MAX_PAGES} pages of ${PER_PAGE}; refusing to count a listing that does not end`
  );
}

function refresh(repo) {
  const closedIssues = Number(
    gh(['api', `search/issues?q=repo:${repo}+is:issue+is:closed&per_page=1`, '--jq', '.total_count']).trim()
  );

  // Agent-authored PRs are identified by kew's branch convention, agent/issue-N.
  const prs = mergedPullRequests(repo);
  if (!prs.length) throw new Error(`the listing for ${repo} holds no merged pull requests`);
  const agentPrs = prs.filter((p) => p.headRefName.startsWith('agent/')).length;
  const dates = prs.map((p) => p.mergedAt).filter(Boolean).sort();

  return {
    capturedAt: new Date().toISOString().slice(0, 10),
    mergedPrs: prs.length,
    closedIssues,
    agentPrs,
    agentShare: Number(((agentPrs / prs.length) * 100).toFixed(1)),
    firstMerge: dates[0]?.slice(0, 10) ?? null,
    lastMerge: dates.at(-1)?.slice(0, 10) ?? null
  };
}

const repo = requestedRepo(process.argv.slice(2), process.env);
if (!repo) {
  console.error(
    'no repository to read: pass --repo <owner/name> or set KEW_STATS_REPO. ' +
      'The snapshot describes kew’s development repository, never the slug the site links to.'
  );
  process.exit(2);
}
if (!SLUG.test(repo)) {
  console.error(`"${repo}" is not an owner/name repository`);
  process.exit(2);
}

try {
  const stats = refresh(repo);
  writeFileSync(OUT, JSON.stringify(stats, null, 2) + '\n');
  console.log(`repo stats -> ${stats.agentPrs}/${stats.mergedPrs} agent-authored (${stats.agentShare}%)`);
} catch (err) {
  const ghAbsent = err.code === 'ENOENT' && String(err.syscall ?? '').startsWith('spawn');
  console.error(
    ghAbsent ? 'gh is not installed' : `could not refresh repo stats: ${String(err.message).trim()}`
  );
  console.error('the snapshot was left as it was');
  process.exit(1);
}
