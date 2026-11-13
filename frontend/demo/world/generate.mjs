#!/usr/bin/env node
/**
 * The demo world (#3141): a synthetic factory the demo build bundles instead
 * of `fixtures/factory/`.
 *
 *     node frontend/demo/world/generate.mjs
 *
 * One invented project (`acme/ledger`) with, all at once: an epic in flight
 * (`ledger-v2`, 13 nodes: done, running, one blocked, two waiting on a
 * person's merge decision, one failed and waiting on a retry), its tick
 * history for the epic page's replay, five needs-you items (an approval
 * hold, a READY PR waiting for a merge, the refused PR, the failed node, an
 * orphan PR no item links to), two open issues kew has not touched, and
 * one refused PR: sent back by review (NEEDS_CHANGES, then READY after a fix)
 * and then refused by the merge verify on held-out tests. As in kew's drive,
 * the failed verify holds the merge and the READY stands, so the refused PR
 * waits on a person's merge decision too. Beside the epic, one goal loop that
 * reached its gate weeks before it (`loopOf`), so `/loops` has a surface.
 *
 * Nothing here is a real epic, item, run, path or name. The SHAPE of the runs
 * is real: every duration, cost and token count is drawn around the medians
 * and p90s in `timings.json`, which `kew/scripts/demo_world_timings.py`
 * aggregates from the run-report corpus. The draw uses a fixed seed and the
 * world its own fixed epoch, so a rerun over the same `timings.json` writes
 * byte-identical files; `scripts/demo-world.test.ts` holds the committed set
 * to that, and `src/lib/demo/world.test.ts` types every file through `api.ts`.
 *
 * What reaches a world file from real data: ONLY numbers, and only through
 * `drawer()` — a duration, a cost or a token count drawn around one of the
 * `timings.json` spreads, whose keys, types and coarseness
 * `demo_world_timings.py`'s `problems()` holds to an allowlist (and
 * `test_demo_world_timings.py` holds the committed file to it). No string, key
 * or count in `timings.json` is copied into any file. A run kind the timings
 * do not name — a kind under the floor of 20 runs publishes nothing — draws
 * the fixed, invented `SYNTHETIC` spread below instead, never another kind's.
 *
 * Spend reconciles exactly: amounts are integer cents until they are written,
 * each node's spend is the sum of its runs, the epic's the sum of its nodes,
 * and every run in the world belongs to the epic, so the budget page's weekly
 * spend IS the epic's spend. A run's tokens are its fresh input, output and
 * cache reads, each drawn from its own spread; their sum is its total.
 *
 * Copied, not generated: the prompt previews (`prompts.json` and
 * `prompts-<role>.json`, the packaged templates rendered over the fixture
 * set's fixed sample item) and the model catalog (`models.json`) come from the
 * factory fixture set as they are; they carry no run, epic or spend figure.
 * Each role's outcomes and versions are generated from this world's own runs.
 * The leak scan reads every file, copied or not.
 */
import { copyFileSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const WORLD = new URL('./', import.meta.url);
const FACTORY = new URL('../../fixtures/factory/', import.meta.url);

/** The files in this folder that are not `/api` fixtures; `demo/fixture-bundle.ts` skips the same two. */
export const NOT_FIXTURES = ['manifest.json', 'timings.json'];

/* ------------------------------------------------------------------------ */
/* The fixed inputs: the epoch, the seed, the names                          */
/* ------------------------------------------------------------------------ */

const EPOCH_MS = Date.parse('2026-10-21T15:00:00Z');
const SEED = 3141;
const REPO = 'acme/ledger';
const GH = `https://github.com/${REPO}`;
const EPIC = 'ledger-v2';
const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const MODEL = 'claude-opus-5';
const RUNNER = 'claude-code';
/**
 * The governed reviewer's model. kew binds a verdict only to a claude-code
 * reviewer (`STREAM_VERDICT_RUNNERS`, kew/src/kew/review_stream.py), so it is
 * a Claude model kew's catalog prices. It is the authoring model: every run
 * row here, the reviewer's own included, names `MODEL`, and the scorecard sums
 * them under it, so another id would contradict the review run's own route.
 */
const REVIEWER = MODEL;

/** Every instant the way the server writes one: second precision, `+00:00`. */
const iso = (ms) => new Date(Math.round(ms / 1000) * 1000).toISOString().replace('.000Z', '+00:00');
const NOW = iso(EPOCH_MS);
const dollars = (cents) => Math.round(cents) / 100;

/** mulberry32: a tiny seeded generator, so the draw is the same on every machine. */
function prng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/* ------------------------------------------------------------------------ */
/* The epic: its nodes, edges and the story each node tells                  */
/* ------------------------------------------------------------------------ */

/**
 * Each node's story is a list of steps; `at` is hours before the epoch at
 * which its first run starts. A step is a run (`dispatch`, `review`,
 * `remediation`) or a terminal fact. Runs chain: each starts a few minutes
 * after the one before it finishes.
 */
const NODES = [
  { id: '101', title: 'Parse ledger entries from a CSV export', deps: [], at: 44, story: ['dispatch', 'review:READY', 'merge'] },
  { id: '102', title: 'Exact decimal amounts in every currency', deps: [], at: 40, story: ['dispatch', 'review:READY', 'merge'] },
  { id: '103', title: 'Double-entry posting rule', deps: ['101', '102'], at: 30, story: ['dispatch', 'review:READY', 'merge'] },
  { id: '104', title: 'Account balance query', deps: ['103'], at: 26, story: ['dispatch:failure', 'dispatch', 'review:READY', 'merge'] },
  { id: '105', title: 'Monthly statement export', deps: ['104'], at: 20, story: ['dispatch', 'review:READY_WITH_RESIDUALS', 'merge'] },
  { id: '106', title: 'Reconcile against a bank statement', deps: ['103'], at: 0.33, story: ['dispatch:pending'] },
  {
    id: '107',
    title: 'Idempotent import of a re-sent file',
    deps: ['101'],
    at: 6,
    story: ['dispatch', 'review:NEEDS_CHANGES', 'remediation', 'review:READY', 'verify:failed']
  },
  { id: '108', title: 'Multi-currency trial balance', deps: ['102', '104'], at: 12, story: ['dispatch', 'review:READY', 'verify:passed', 'await-merge'] },
  { id: '109', title: 'Close a period and lock its entries', deps: ['104'], at: 8, story: ['dispatch:host-restart', 'dispatch:host-restart', 'failed'] },
  { id: '110', title: 'Audit trail for every posting', deps: ['103'], blocked: 'needs a product decision: how long a posting is kept' },
  { id: '111', title: 'Year-end rollover', deps: ['109'] },
  { id: '112', title: 'Ledger CLI help and examples', deps: ['106', '107'] },
  { id: '113', title: 'Import from an OFX bank file', deps: ['102'] }
];

/** The approval hold: an item outside the epic whose estimate is at the per-issue cap. */
const HELD = { id: '114', title: 'Rebuild the balances cache from the journal', estimate_usd: 25.0 };
const HOLD_BASIS = 'no history — assuming per-issue cap of $25.00';

/** The refused PR and the PR waiting on a merge decision. */
const REFUSED = '107';
const AWAITING = '108';
const FAILED = '109';
const BLOCKED = '110';
const RUNNING = '106';
const READY_ITEM = '113';

/** Pull-request numbers: the item's own number plus a hundred. */
const prNumber = (id) => Number(id) + 100;
const prUrl = (id) => `${GH}/pull/${prNumber(id)}`;
const issueUrl = (id) => `${GH}/issues/${id}`;

/**
 * #3453 — an open pull request on a branch no run in this world recorded: an
 * orphan with the `no-record` reason, which no item links to, so Work's own
 * Acknowledge is the one way to clear it. Its number is clear of every item's
 * `prNumber`.
 */
const ORPHAN_PR = 231;
const ORPHAN_BRANCH = 'fix/rounding-typo';

/** #3453 — open issues kew has not touched, which Work's "Not started" picker queues. */
const CANDIDATES = [
  { id: '115', title: 'Export a trial balance as CSV' },
  { id: '116', title: 'Warn on a posting dated in a closed period' }
];

/** Held-out tests the merge verify of the refused PR found failing; the last was declared. */
const HELD_OUT_FAILED = [
  'tests/test_import.py::test_resent_file_posts_once',
  'tests/test_balances.py::test_balance_unchanged_after_reimport',
  'tests/test_import.py::test_duplicate_row_warning_text'
];
const HELD_OUT_DECLARED = ['tests/test_import.py::test_duplicate_row_warning_text'];

const HOST_RESTART_REASON = 'failed-terminal (exit 137, host_restart_repeated)';
const HOST_RESTART_SENTENCE = 'the host restarted under this node twice';
const HOST_RESTART_ACTION = 'check why the host keeps going down, then kew epic retry the node';

/* ------------------------------------------------------------------------ */
/* Runs, drawn from the timings                                             */
/* ------------------------------------------------------------------------ */

const STORY_KIND = { dispatch: 'dispatch', review: 'adversarial_review', remediation: 'review_remediation' };

/** The merge verify's duration is drawn as a CI-fix run's: the nearest kind of work. */
const VERIFY_KIND = 'ci_fix';

/**
 * The spread a field draws from when `timings.json` publishes none for its
 * kind (a kind under the floor publishes nothing). Invented round figures,
 * not derived from any data; never another kind's published spread.
 */
export const SYNTHETIC = {
  duration_minutes: { median: 15, p90: 30 },
  cost_usd: { median: 1.0, p90: 2.0 },
  input_tokens: { median: 200, p90: 400 },
  output_tokens: { median: 20_000, p90: 40_000 },
  cache_read_tokens: { median: 2_000_000, p90: 4_000_000 }
};

/** The token splits a run carries; each is drawn from its own spread. */
export const TOKEN_FIELDS = ['input_tokens', 'output_tokens', 'cache_read_tokens'];

function drawer(timings, random) {
  const normal = () => {
    const u = Math.max(random(), 1e-9);
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * random());
  };
  /** One value around a kind's median, never past 1.1 × its p90 nor under half its median. */
  return (kind, field) => {
    const spread = timings.by_run_kind[kind]?.[field] ?? SYNTHETIC[field];
    const value = spread.median * Math.exp(0.3 * normal());
    return Math.min(Math.max(value, spread.median * 0.5), spread.p90 * 1.1);
  };
}

function hex(random, length) {
  let out = '';
  for (let i = 0; i < length; i += 1) out += Math.floor(random() * 16).toString(16);
  return out;
}

/** Every node's runs, rounds, merge and bands, in one pass over the stories. */
function schedule(timings) {
  const random = prng(SEED);
  const draw = drawer(timings, random);
  // Ids and shas come from a stream of their own, so regenerating the timings
  // moves durations and costs but never renames a run a check or a tour reads.
  const names = prng(SEED + 1);
  const usedIds = new Set();
  const runId = () => {
    let id;
    do id = hex(names, 8);
    while (usedIds.has(id) || /^\d+$/.test(id));
    usedIds.add(id);
    return id;
  };
  const merged = new Map();
  const nodes = new Map();
  for (const node of NODES) {
    const plan = { ...node, runs: [], rounds: [], bands: [], merged_at: null, verify: null, status: 'todo' };
    nodes.set(node.id, plan);
    if (!node.story) {
      // A blocked node was blocked as soon as it could have started: when its last dependency merged.
      if (node.blocked) {
        plan.status = 'blocked';
        plan.bands.push({ status: 'blocked', from: Math.max(...node.deps.map((d) => merged.get(d))) + 5 * MINUTE });
      }
      continue;
    }
    let t = EPOCH_MS - node.at * HOUR;
    for (const dep of node.deps) {
      if (!merged.has(dep) || merged.get(dep) > t) throw new Error(`${node.id} starts before ${dep} merged`);
    }
    let attempt = 0;
    let round = 0;
    for (const step of node.story) {
      const [what, detail] = step.split(':');
      if (what === 'merge') {
        plan.merged_at = t;
        merged.set(node.id, t);
        plan.bands.push({ status: 'done', from: t });
        plan.status = 'done';
        continue;
      }
      if (what === 'verify') {
        const at = t + Math.round(draw(VERIFY_KIND, 'duration_minutes')) * MINUTE;
        plan.verify = { outcome: detail, at, head: hex(names, 40) };
        t = at;
        continue;
      }
      if (what === 'await-merge') {
        plan.status = 'in_review';
        continue;
      }
      if (what === 'failed') {
        plan.status = 'failed';
        plan.bands.push({ status: 'failed', from: t });
        continue;
      }
      const kind = STORY_KIND[what];
      const pending = detail === 'pending';
      const failure = detail === 'failure' || detail === 'host-restart';
      if (kind === 'dispatch') attempt += 1;
      const minutes = Math.round(draw(kind, 'duration_minutes'));
      const duration = (failure ? Math.round(minutes * 0.6) : minutes) * MINUTE;
      const finish = pending ? null : t + duration;
      const cents = pending ? 0 : Math.round(draw(kind, 'cost_usd') * (failure ? 60 : 100));
      // A pending run has used nothing yet; a failed one stopped early.
      const tokens = Object.fromEntries(
        TOKEN_FIELDS.map((field) => [field, pending ? 0 : Math.round(draw(kind, field) * (failure ? 0.6 : 1))])
      );
      const run = {
        run_id: runId(),
        kind,
        status: pending ? 'pending' : failure ? 'failure' : 'success',
        started: t,
        finished: finish,
        cents,
        tokens,
        attempt: kind === 'dispatch' ? attempt : 1,
        error_category: detail === 'host-restart' ? 'host_restart' : detail === 'failure' ? 'tests_failed' : '',
        verdict: null
      };
      plan.runs.push(run);
      plan.bands.push({ status: kind === 'adversarial_review' ? 'in_review' : 'in_progress', from: t });
      if (pending) {
        plan.status = 'in_progress';
        break;
      }
      if (kind === 'adversarial_review') {
        round += 1;
        run.verdict = { round, verdict: detail, head_sha: hex(names, 40), ts: finish };
        plan.rounds.push({ round, verdict: detail, recorded_at: finish });
        if (detail === 'NEEDS_CHANGES') plan.status = 'in_progress';
        else plan.status = 'in_review';
      }
      if (failure && node.story.at(-1) !== 'failed') plan.bands.push({ status: 'failed', from: finish });
      if (kind === 'dispatch' && !failure) plan.bands.push({ status: 'in_review', from: finish });
      t = finish + (2 + Math.floor(random() * 5)) * MINUTE;
    }
    // A merge verify runs on the head the last review passed: for the refused
    // PR, the one its remediation pushed, which round 2 read and round 1 did
    // not. The sha is still drawn above, so no later id or sha moves.
    if (plan.verify) plan.runs.filter((r) => r.verdict).at(-1).verdict.head_sha = plan.verify.head;
    if (node.id === REFUSED) plan.status = 'in_review';
    if (t > EPOCH_MS) throw new Error(`${node.id}'s story runs past the epoch`);
  }
  // Close each band at the next one's start; the last runs to now.
  for (const plan of nodes.values()) {
    plan.bands = plan.bands
      .filter((band, i, all) => i === all.length - 1 || all[i + 1].from > band.from)
      .filter((band, i, all) => i === 0 || all[i - 1].status !== band.status)
      .map((band, i, all) => ({ ...band, to: all[i + 1]?.from ?? null }));
  }
  return nodes;
}

/* ------------------------------------------------------------------------ */
/* Readings shared by several payloads                                      */
/* ------------------------------------------------------------------------ */

const observed = (dimension, at = NOW) => ({
  dimension,
  availability: 'available',
  provenance: 'observed',
  observed_at: at,
  detail: null
});
const unprobed = {
  dimension: 'route_boundary',
  availability: 'unavailable',
  provenance: 'observed',
  observed_at: null,
  detail: 'nothing has probed this boundary'
};

function pressure(used) {
  return {
    delivery_repo: REPO,
    engaged: true,
    countable: true,
    used,
    total: 4,
    held: 0,
    oldest_hold_age_seconds: null,
    diff_burden_lines: 318,
    saturated: false,
    reasons: [],
    enforcement: 'inactive',
    enforcement_detail: 'the human-review policy is configured and reported, but not active',
    policy_generation: 1,
    observation: observed('review_capacity')
  };
}

const lastRun = (plan) => plan.runs.at(-1) ?? null;
const lastDispatch = (plan) => plan.runs.filter((r) => r.kind === 'dispatch').at(-1) ?? null;
const nodeCents = (plan) => plan.runs.reduce((sum, r) => sum + r.cents, 0);

/** A PR waiting on a person's merge decision: the READY one, and the refused one whose verify failed. */
const awaitsMerge = (plan) => plan.id === AWAITING || plan.id === REFUSED;

/**
 * A merge wait's sentence and links, as `kew.service.attention.say_wait` writes them: over a merge
 * verify that failed at the PR's head, the sentence leads with the failure and its count, and the
 * item's MERGE VERIFY section is the first link, the PR the second (#3460).
 */
function mergeWait(plan) {
  const pr = prNumber(plan.id);
  const link = { label: `Open PR #${pr} to merge`, href: prUrl(plan.id) };
  if (plan.verify?.outcome !== 'failed') return { sentence: `PR #${pr} is open — the merge decision is yours`, link };
  const count = HELD_OUT_FAILED.filter((id) => !HELD_OUT_DECLARED.includes(id)).length;
  const tests = count === 1 ? "1 of main's tests fails" : `${count} of main's tests fail`;
  return {
    sentence: `Merge verify failed at ${plan.verify.head.slice(0, 8)}: ${tests} on this PR — the merge decision is yours`,
    link,
    verify_link: { label: 'See the failed merge verify', href: `/work?item=${plan.id}#merge-verify` }
  };
}

/** A node's attention: who it is waiting on, as the epic snapshot serves it. */
function attentionOf(plan) {
  if (awaitsMerge(plan) && plan.status === 'in_review') {
    return {
      kind: 'merge_decision',
      reason: `PR open for #${plan.id} — awaiting review`,
      url: prUrl(plan.id),
      action: '',
      command: '',
      ...mergeWait(plan)
    };
  }
  if (plan.id === FAILED && plan.status === 'failed') {
    return {
      kind: 'host_restart_repeated',
      reason: HOST_RESTART_REASON,
      url: null,
      action: HOST_RESTART_ACTION,
      command: `kew epic retry ${EPIC} ${plan.id}`,
      sentence: HOST_RESTART_SENTENCE,
      link: null
    };
  }
  return null;
}

const MOTION = {
  done: 'settled',
  todo: 'waiting_kew',
  in_progress: 'executing',
  in_review: 'waiting_kew',
  blocked: 'waiting_human',
  failed: 'waiting_human'
};

/** One node as a snapshot taken at `t` serves it. */
function nodeAt(plan, t) {
  const band = plan.bands.findLast((b) => b.from <= t) ?? null;
  const status = band?.status ?? 'todo';
  const current = t >= EPOCH_MS;
  const view = { ...plan, status: current ? plan.status : status };
  // A wait is served once the node has reached it: its status is the current one and its last run has
  // returned, or, after a failed merge verify, once the verify has.
  const reached = plan.verify?.outcome === 'failed' ? plan.verify.at : (lastRun(plan)?.finished ?? 0);
  const settled = reached <= t;
  const attention = current || (status === plan.status && settled) ? attentionOf(view) : null;
  const hasPr = plan.runs.some((r) => r.kind === 'dispatch' && r.status === 'success' && r.finished <= t);
  const motion =
    attention !== null ? 'waiting_human' : view.status === 'in_review' && plan.id !== REFUSED ? 'waiting_kew' : MOTION[view.status];
  return {
    id: plan.id,
    title: plan.title,
    status: view.status,
    status_since: band ? iso(band.from) : null,
    url: issueUrl(plan.id),
    pr_url: hasPr ? prUrl(plan.id) : null,
    external: false,
    stuck: view.status === 'failed' ? HOST_RESTART_REASON : view.status === 'blocked' ? plan.blocked : null,
    attention,
    blocked_reason: view.status === 'blocked' ? plan.blocked : null,
    recovery_command: view.status === 'failed' ? `kew epic retry ${EPIC} ${plan.id}` : '',
    motion,
    alive: view.status !== 'done'
  };
}

const STATUSES = ['todo', 'queued', 'in_progress', 'in_review', 'done', 'failed', 'held', 'blocked', 'cancelled'];

function snapshotAt(nodes, t, tickId) {
  const list = [...nodes.values()].map((plan) => nodeAt(plan, t));
  const counts = Object.fromEntries(STATUSES.map((s) => [s, list.filter((n) => n.status === s).length]));
  const done = new Set(list.filter((n) => n.status === 'done').map((n) => n.id));
  const frontier = list
    .filter((n) => n.status === 'todo' && NODES.find((p) => p.id === n.id).deps.every((d) => done.has(d)))
    .map((n) => n.id);
  return {
    epic_ref: EPIC,
    tick_id: tickId,
    tick_ts: iso(t),
    signal: 'progressing',
    counts,
    frontier,
    nodes: list,
    edges: NODES.flatMap((n) => n.deps.map((d) => [d, n.id])),
    unavailable: []
  };
}

/** Node spend as of `t`: the runs that had finished by then. */
function drainAt(nodes, t) {
  const drains = {};
  let total = 0;
  for (const plan of nodes.values()) {
    const cents = plan.runs.filter((r) => r.finished !== null && r.finished <= t).reduce((s, r) => s + r.cents, 0);
    if (cents > 0) drains[plan.id] = dollars(cents);
    total += cents;
  }
  return { window_start: null, drain_total_usd: dollars(total), node_drain_usd: drains, label: 'since last tick' };
}

const EPIC_START = EPOCH_MS - 44 * HOUR;
const tickIdAt = (t) => 1 + Math.floor((t - EPIC_START) / MINUTE);
/** The driver's last tick: thirty seconds before the epoch, on its minute cadence. */
const LAST_TICK = EPOCH_MS - 30_000;

/* ------------------------------------------------------------------------ */
/* Runs as the ledger serves them                                           */
/* ------------------------------------------------------------------------ */

/** A run's total: its fresh input, output and cache reads. */
export const totalTokens = (run) => TOKEN_FIELDS.reduce((sum, field) => sum + run.tokens[field], 0);
/** Cache writes: an invented fixed share of the cache reads (the timings publish none). */
const cacheWrites = (run) => Math.round(run.tokens.cache_read_tokens * 0.04);

/**
 * The run kinds that seal an artifact and publish it: kew seals a receipt only
 * on dispatch_core's path, which the implementation and the review's
 * remediation both run. A reviewer (`adversarial_review`) seals and publishes
 * nothing (kew's `publication_credentials._NON_PUBLICATION_RUN_KINDS`).
 */
const SEALING_KINDS = new Set(['dispatch', 'review_remediation']);

/**
 * The verdict a run's detail carries, as kew's `_latest_verdict`
 * (kew/src/kew/service/runs.py) reads it: the run's own round, attested by
 * that run (`scope: 'run'`); else the newest verdict standing over its work
 * item (`scope: 'item'`), which is what an implementation run's page shows
 * (design review c9, D7: #104's merged run read "no verdict recorded" beside
 * the READY its epic rail showed); else none.
 */
function verdictOf(plan, run) {
  const shown = (v, scope) => ({ round: v.round, verdict: v.verdict, head_sha: v.head_sha, reviewer_model: REVIEWER, ts: iso(v.ts), scope });
  if (run.verdict) return shown(run.verdict, 'run');
  const standing = plan.runs.filter((r) => r.verdict).at(-1)?.verdict;
  return standing ? shown(standing, 'item') : null;
}

function runRow(plan, run, { detail = false } = {}) {
  const published = SEALING_KINDS.has(run.kind) && run.status === 'success';
  const verdict = verdictOf(plan, run);
  const stage = { dispatch: 'implement', adversarial_review: 'review', review_remediation: 'remediate' }[run.kind];
  const row = {
    run_id: run.run_id,
    issue_num: Number(plan.id),
    title: plan.title,
    runner: RUNNER,
    model: MODEL,
    status: run.status,
    cost_usd: dollars(run.cents),
    channel: 'hosted',
    started_at: iso(run.started),
    finished_at: run.finished === null ? null : iso(run.finished),
    duration_seconds: run.finished === null ? 0 : Math.round((run.finished - run.started) / 1000),
    input_tokens: run.tokens.input_tokens,
    output_tokens: run.tokens.output_tokens,
    cache_read_tokens: run.tokens.cache_read_tokens,
    cache_creation_tokens: cacheWrites(run),
    diff_additions: published ? 80 + (Number(plan.id) % 7) * 23 : null,
    diff_deletions: published ? 9 + (Number(plan.id) % 5) * 4 : null,
    diff_files: published ? 3 + (Number(plan.id) % 4) : null,
    review_cycles: plan.rounds.length,
    pr_url: published || run.kind !== 'dispatch' ? (plan.runs.some((r) => r.kind === 'dispatch' && r.status === 'success') ? prUrl(plan.id) : null) : null,
    pr_state: plan.status === 'done' ? 'merged' : published || run.kind !== 'dispatch' ? 'open' : null,
    attempt_number: run.attempt,
    error_category: run.error_category,
    complexity: 'medium',
    branch: `agent/issue-${plan.id}`,
    stage,
    stage_detail: run.verdict ? `round ${run.verdict.round} · PR #${prNumber(plan.id)}` : published ? `PR #${prNumber(plan.id)}` : '',
    posture: 'sandboxed',
    filesystem_boundary: 'container',
    network_boundary: 'proxied',
    command_guard: 'allowlist',
    admission: 'admitted',
    credential_names: [],
    egress_verification_basis: null,
    reason: run.status === 'failure' ? (run.error_category === 'host_restart' ? 'the host restarted under this run' : 'the run ended with failing tests') : null,
    run_kind: run.kind,
    item_id: plan.id,
    reported_model: MODEL,
    provider: 'anthropic',
    transport_mode: 'direct',
    planned_cost_usd: null,
    accounting_status: run.finished === null ? 'pending' : 'settled',
    verdict: detail ? verdict : null
  };
  if (!detail) {
    row.motion = run.status === 'pending' ? 'executing' : 'settled';
    row.alive = run.status === 'pending';
  }
  if (row.pr_url === null) row.pr_state = null;
  return row;
}

const PRODUCER = { producer_kind: 'host', producer_name: 'kew.dispatch', producer_version: '0.9.0', attested_by_host: true };

/** The base a sealing run's change set is built on: one invented sha per run. */
const baseShaOf = (run) => `${run.run_id}`.repeat(5);

function flightOf(plan, run) {
  const sealed = run.status === 'success' && SEALING_KINDS.has(run.kind);
  const events = [
    { kind: 'evidence.dispatch', at: run.started, facts: { route: `${RUNNER}/${MODEL}` } }
  ];
  // The trusted base the host attested at launch (kew's dispatch_sandbox
  // records it as a receipt fact), which the sealed change set's base must
  // match: without it the run page read "3/4 checks match" on a merged run
  // (design review c9, D7).
  if (sealed) {
    events.push({
      kind: 'evidence.receipt_fact',
      at: run.started,
      facts: { fact: 'trusted_base_sha', value: baseShaOf(run), source: 'origin/main' }
    });
  }
  if (run.finished !== null && run.status === 'success') {
    if (sealed) events.push({ kind: 'evidence.artifact', at: run.finished - 3 * MINUTE, facts: { files: 3 + (Number(plan.id) % 4) } });
    if (run.verdict) events.push({ kind: 'evidence.verdict', at: run.finished, facts: { verdict: run.verdict.verdict } });
  }
  const through = events.length;
  return {
    run_id: run.run_id,
    attempt_number: run.attempt,
    item_id: plan.id,
    issue_num: Number(plan.id),
    source_tracker: 'github',
    source_id: plan.id,
    sealed,
    through_seq: through,
    chain_head: `${run.run_id}`.repeat(8),
    chain_verified: true,
    artifact_node_id: sealed ? `${run.run_id}:${through}` : null,
    runner_telemetry: 'structured',
    events: events.map((e, i) => ({
      seq: i + 1,
      timestamp: iso(e.at),
      kind: e.kind,
      origin: 'evidence',
      ...PRODUCER,
      sealed,
      terminal: false,
      facts: e.facts
    })),
    gaps: [],
    availability: [
      { dimension: 'host_sequence', availability: 'available', provenance: 'observed', detail: '' },
      { dimension: 'artifact_seal', availability: sealed ? 'available' : 'unavailable', provenance: 'observed', detail: sealed ? '' : 'the run sealed no artifact' },
      { dimension: 'artifact_diff', availability: sealed ? 'available' : 'unavailable', provenance: 'observed', detail: sealed ? '' : 'the run sealed no artifact' }
    ]
  };
}

/** The payload a run's owner record carries. */
const ownerPayload = (plan) => ({ owner_pid: 4100 + Number(plan.id) });

/**
 * The payload a finished run's last event carries. A failed run's completion
 * carries its error_category, as the backend's dispatch_completed does
 * (dispatch_core.py), so the run page draws its row as a failure (#3458).
 */
function finishPayload(run) {
  if (run.verdict) return { verdict: run.verdict.verdict, round: run.verdict.round };
  return run.status === 'failure' ? { status: run.status, error_category: run.error_category } : { status: run.status };
}

/**
 * An event's `summary` on `/api/audit`, as the server writes it (`_summary` in
 * kew/src/kew/service/audit.py): the payload as compact ASCII JSON, cut at 200
 * characters with a trailing `…` (#3470 C7). Never prose the server never sends.
 */
const SUMMARY_MAX = 200;
function serverSummary(payload) {
  const text = JSON.stringify(payload).replace(/[\u007f-\uffff]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`);
  return text.length <= SUMMARY_MAX ? text : `${text.slice(0, SUMMARY_MAX - 1)}…`;
}

function auditOf(plan, run, firstSeq) {
  const steps = [
    ['dispatch_owner_recorded', 'policy', run.started + 1000, ownerPayload(plan)],
    ['routing_decision', 'agent', run.started + 2000, { recommended_runner: RUNNER, applied: false }],
    ['sandbox_resolved', 'host', run.started + 23_000, { posture: 'sandboxed' }]
  ];
  if (run.finished !== null) {
    steps.push([
      run.kind === 'adversarial_review' ? 'adversarial_review_verdict' : 'dispatch_completed',
      'host',
      run.finished,
      finishPayload(run)
    ]);
  }
  return {
    events: steps.map(([event, actor_type, at, payload], i) => ({
      seq: firstSeq + i,
      ts: iso(at),
      event,
      actor_type,
      item_id: plan.id,
      payload
    })),
    run_id: run.run_id,
    next_seq: firstSeq + steps.length
  };
}

const DIFF_FILES = [
  ['src/ledger/importer.py', 'def import_file(path, journal):\n    digest = file_digest(path)\n    if journal.has_import(digest):\n        return journal.import_for(digest)\n    return journal.record_import(digest, parse_rows(path))\n'],
  ['src/ledger/journal.py', 'class Journal:\n    def has_import(self, digest):\n        return digest in self._imports\n'],
  ['tests/test_import.py', 'def test_resent_file_posts_once(tmp_path):\n    journal = Journal()\n    import_file(sample(tmp_path), journal)\n    import_file(sample(tmp_path), journal)\n    assert len(journal.postings) == 2\n'],
  ['docs/importing.md', '## Importing a file twice\n\nA file kew has already imported is recognised by its digest and posts nothing new.\n']
];

/** The files a node's sealed change set holds: the refused PR's own story, or one module and its test. */
function diffFiles(plan) {
  if (plan.id === REFUSED) return DIFF_FILES;
  const slug = plan.title.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '').slice(0, 32);
  const [description, expected] = DESCRIPTIONS[plan.id];
  return [
    [`src/ledger/${slug}.py`, `"""${description}"""\n`],
    [`tests/test_${slug}.py`, `def test_${slug}():\n    """${expected}"""\n`]
  ];
}

function diffOf(plan, run, flight) {
  const digest = (seed) => `sha256:${(seed + run.run_id).padEnd(64, '0').slice(0, 64)}`;
  const files = diffFiles(plan).map(([path, text], i) => ({
    path,
    change: plan.id === REFUSED && i === 3 ? 'added' : 'changed',
    mode: '100644',
    digest: digest(`${i}f`),
    byte_length: text.length,
    text,
    text_availability: 'available',
    detail: ''
  }));
  const base = baseShaOf(run);
  const tree = `${run.run_id}`.split('').reverse().join('').repeat(5);
  return {
    node_id: flight.artifact_node_id,
    text: [
      `artifact ${digest('a')}`,
      `base ${base}`,
      `tree ${tree}`,
      'verification exact_tree',
      `sealed through audit sequence ${flight.through_seq}`,
      ...files.map((f) => `${f.change} ${f.mode} ${f.digest} ${f.byte_length} ${f.path}`),
      ''
    ].join('\n'),
    run_id: run.run_id,
    attempt_number: run.attempt,
    base_sha: base,
    tree_sha: tree,
    package_digest: digest('a'),
    receipt_digest: digest('r'),
    verification: 'exact_tree',
    files,
    availability: [
      { dimension: 'artifact_diff', availability: 'available', provenance: 'observed', detail: '' },
      {
        dimension: 'diff_line_counts',
        availability: 'unavailable',
        provenance: 'unobserved',
        detail: 'the sealed package carries final content only; the base content the counts would be measured against is not part of this route'
      }
    ]
  };
}

/* ------------------------------------------------------------------------ */
/* Work items                                                               */
/* ------------------------------------------------------------------------ */

const NO_CHANGE = { state: 'not_applicable', judgment_state: '', policy: '', reasons: [], sync_outstanding: 0, sync_failed: 0, effects_outstanding: 0 };

/** The facts that differ between items; everything else in a view is the same reading. */
function viewFacts(plan) {
  const head = lastDispatch(plan);
  const shared = { execution: 'completed', run_status: 'success', artifact: 'published', judgment: 'accepted', outcome: ['delivered', 'merged'] };
  if (plan.status === 'done') {
    return { ...shared, exception: ['none', '', false, 'none'], action: ['wait', 'nothing is outstanding'], checks: 'passing', mergeability: 'merged', decision: 'approved' };
  }
  if (awaitsMerge(plan)) {
    const why = 'kew opens the pull request; you decide whether it lands.';
    return { ...shared, judgment: 'awaiting_human_decision', outcome: ['in_progress', 'artifact published, decision pending'], exception: ['awaiting_human_decision', why, true, 'judgment'], action: ['wait', why], checks: 'passing', mergeability: 'clean', decision: 'review_required' };
  }
  if (plan.id === RUNNING) {
    return { execution: 'in_flight', run_status: 'pending', artifact: 'absent', judgment: 'not_applicable', outcome: ['in_progress', 'implement is running'], exception: ['in_flight', 'run pending', false, 'execution'], action: ['wait', 'run pending — a second dispatch would duplicate it'], checks: 'unknown', mergeability: 'not_applicable', decision: 'unknown' };
  }
  if (plan.id === FAILED) {
    return { execution: 'terminal', run_status: 'failure', artifact: 'absent', judgment: 'not_applicable', outcome: ['abandoned', 'handed to a human: the host restarted under it twice'], exception: ['terminal_failure', HOST_RESTART_SENTENCE, true, 'execution'], action: ['wait', `retry it with kew epic retry ${EPIC} ${plan.id}`], checks: 'unknown', mergeability: 'not_applicable', decision: 'unknown', head };
  }
  throw new Error(`no work view for ${plan.id}`);
}

function workView(plan) {
  const f = viewFacts(plan);
  const head = lastDispatch(plan);
  const latest = lastRun(plan);
  const published = f.artifact === 'published';
  return {
    identity: {
      source_tracker: 'github',
      item_id: plan.id,
      item_ref: `#${plan.id}`,
      delivery_repo: REPO,
      epic_ref: EPIC,
      node_id: plan.id,
      run_id: latest.run_id,
      attempt_number: head.attempt
    },
    source: { tracker: 'github', item_state: plan.status === 'done' ? 'closed' : 'open', synchronization: 'synchronized', observation: observed('tracker_item') },
    route: { runner: RUNNER, model: MODEL, reported_model: MODEL, containment: 'contained', provenance: 'declared', claim: 'honored' },
    execution: {
      state: f.execution,
      run_status: f.run_status,
      attempts: head.attempt,
      started_at: iso(head.started),
      finished_at: head.finished === null ? null : iso(head.finished),
      error_category: f.run_status === 'failure' ? head.error_category : ''
    },
    artifact: {
      state: f.artifact,
      publication: published ? 'published' : 'not_required',
      branch: `agent/issue-${plan.id}`,
      pr_url: published ? prUrl(plan.id) : null,
      orphan: false
    },
    judgment: { state: f.judgment, observation: observed('pull_request'), decided_at: plan.merged_at === null ? null : iso(plan.merged_at) },
    outcome: { state: f.outcome[0], detail: f.outcome[1] },
    integration: { checks: f.checks, mergeability: f.mergeability, review_decision: f.decision, observation: observed('pull_request') },
    readiness: { state: 'unproven', fingerprint: null, evidence_ref: null, detail: 'nothing has probed this boundary', observation: unprobed },
    no_change: NO_CHANGE,
    review_pressure: pressure(2),
    exception: { state: f.exception[0], detail: f.exception[1], blocking: f.exception[2], dimension: f.exception[3] },
    safest_action: { action: f.action[0], reason: f.action[1], blocked_by: null },
    actions: [],
    view_revision: `wv1:${plan.id}${head.run_id}${'0'.repeat(21)}`,
    composed_at: NOW,
    observations: [observed('pull_request'), observed('review_capacity'), unprobed, observed('tracker_item')]
  };
}

const stage = (name, state, label, detail = '', evidence = null) => ({ stage: name, state, label, detail, evidence });

/**
 * The head a plan's pull request stands at: the one its merge verify ran on,
 * else the one its last review read. Never the sealed change set's base sha
 * (`baseShaOf`), which a head that fell back to it read as (design review c9:
 * #104's verified head was its base). The dispatch's id stands in only for a
 * plan no review has read, whose pipeline names no head.
 */
const prHead = (plan) =>
  plan.verify?.head ?? plan.runs.filter((r) => r.verdict).at(-1)?.verdict.head_sha ?? lastDispatch(plan)?.run_id.repeat(5);

function pipelineOf(plan) {
  const head = lastDispatch(plan);
  const verify = plan.verify;
  const headSha = prHead(plan);
  const short = headSha?.slice(0, 8);
  const lastRound = plan.rounds.at(-1);
  let stages;
  if (!head) {
    stages = ['implement', 'publish', 'review', 'merge'].map((s) => stage(s, 'pending', 'not started'));
  } else if (head.status === 'pending') {
    stages = [
      stage('implement', 'working', `run ${head.run_id}`, 'running', { kind: 'run', ref: head.run_id }),
      stage('publish', 'pending', 'not started'),
      stage('review', 'pending', 'not started'),
      stage('merge', 'pending', 'not started')
    ];
  } else if (head.status === 'failure') {
    stages = [
      stage('implement', 'failed', `run ${head.run_id}`, 'failure', { kind: 'run', ref: head.run_id }),
      stage('publish', 'pending', 'not started'),
      stage('review', 'pending', 'not started'),
      stage('merge', 'needs_you', 'needs you', 'retry the node')
    ];
  } else {
    const verdict = (lastRound?.verdict ?? 'READY').toLowerCase().replaceAll('_', ' ');
    const merge =
      plan.status === 'done'
        ? stage('merge', 'done', 'merged', '', { kind: 'pull_request', ref: prUrl(plan.id) })
        : awaitsMerge(plan)
          ? stage('merge', 'needs_you', 'needs you', 'merge decision', { kind: 'pull_request', ref: prUrl(plan.id) })
          : stage('merge', 'pending', 'after review', 'the review verdict decides', { kind: 'pull_request', ref: prUrl(plan.id) });
    stages = [
      stage('implement', 'done', `run ${head.run_id}`, 'success', { kind: 'run', ref: head.run_id }),
      stage('publish', 'done', 'scan clean', short, { kind: 'output_scan', ref: short }),
      stage('review', 'done', `round ${lastRound.round}`, verdict, { kind: 'review_verdict', ref: short }),
      merge
    ];
  }
  const declared = HELD_OUT_DECLARED;
  const undeclared = HELD_OUT_FAILED.filter((id) => !declared.includes(id));
  const failed = verify?.outcome === 'failed';
  const mergeVerify = !head || head.status !== 'success'
    ? null
    : {
        outcome: failed ? 'failed' : 'passed',
        head: headSha,
        is_current_head: true,
        error_category: null,
        held_out: {
          outcome: failed ? 'failed' : 'passed',
          failed: failed ? HELD_OUT_FAILED : [],
          declared: failed ? declared : [],
          undeclared: failed ? undeclared : [],
          failed_more: 0,
          declared_more: 0,
          undeclared_more: 0
        },
        recorded_at: iso(verify?.at ?? plan.merged_at ?? head.finished)
      };
  return { item_id: plan.id, lanes: [{ name: 'main', stages }], unavailable: [], composed_at: NOW, merge_verify: mergeVerify };
}

function evidenceOf(plan) {
  const head = lastDispatch(plan);
  const review = plan.runs.filter((r) => r.kind === 'adversarial_review').at(-1);
  const labels = [
    ['Sandboxed', 'sandbox_resolved'],
    ['Canonical publication', 'publication_posture'],
    ['Scan clean', 'publication_output_scan'],
    ['Verdict', 'adversarial_review_verdict']
  ];
  const short = prHead(plan)?.slice(0, 8);
  const facts = !head
    ? []
    : [
        ['sandboxed', head.started + 23_000],
        ...(head.status === 'success'
          ? [
              ['canonical', head.finished - 8000],
              [`clean · ${short}`, head.finished - 4000],
              ...(review ? [[review.verdict.verdict.toLowerCase().replaceAll('_', ' '), review.finished]] : [])
            ]
          : [])
      ];
  const rows = labels.map(([label, event], i) => ({
    n: i + 1,
    label,
    event,
    summary: facts[i]?.[0] ?? '',
    ts: facts[i] ? iso(facts[i][1]) : '',
    state: facts[i] ? 'attested' : 'pending'
  }));
  return {
    item_id: plan.id,
    rows,
    attested_count: facts.length,
    total: 4,
    chain_verified: true,
    reason: head ? '' : 'no run has been dispatched for this item',
    unavailable: [],
    composed_at: NOW
  };
}

const DESCRIPTIONS = {
  101: ['Read the bank-exported CSV into ledger entries, one per row, keeping the source row number.', 'every row of the sample export becomes one entry.'],
  102: ['Store amounts as integer minor units with an ISO currency code, so no sum ever rounds.', 'adding 0.10 three times gives exactly 0.30.'],
  103: ['Refuse a posting whose debits and credits do not balance, and say by how much.', 'an unbalanced posting is refused with the difference.'],
  104: ['Answer the balance of an account as of any date from the journal.', 'the balance matches the sample statement on every day.'],
  105: ['Write a month of postings per account as a statement file.', 'the statement for the sample month matches the bank one.'],
  106: ['Match ledger entries to the lines of a bank statement and list what is left over.', 'the sample statement reconciles with nothing left.'],
  107: ['Importing the same file twice must post nothing the second time.', 'a re-sent file adds no posting and changes no balance.'],
  108: ['A trial balance across currencies, converted at the rate of the closing day.', 'the trial balance of the sample books sums to zero.'],
  109: ['Closing a period locks its entries; a later posting into it is refused.', 'a posting dated inside a closed period is refused.'],
  110: ['Keep an audit trail of who posted what, and when.', 'every posting names its author and time.'],
  111: ['Carry closing balances into the new year as opening entries.', 'opening balances equal last year’s closing ones.'],
  112: ['Help text and worked examples for every ledger command.', 'each command prints an example that runs.'],
  113: ['Import an OFX bank file the same way a CSV export is imported.', 'the sample OFX file imports the same entries as its CSV twin.'],
  114: ['Rebuild the cached balances from the journal, so a corrupted cache can be recovered.', 'a rebuilt cache equals the one it replaces.']
};

function descriptionOf(id, title) {
  const [description, expected] = DESCRIPTIONS[id];
  return {
    item_id: id,
    availability: 'available',
    provenance: 'observed',
    observed_at: NOW,
    title,
    url: issueUrl(id),
    description,
    expected,
    actual: '',
    reason: '',
    composed_at: NOW
  };
}

function idlePipeline(id) {
  return pipelineOf({ id, runs: [], rounds: [], verify: null, status: 'todo', merged_at: null });
}

function dispatchPlan(id, spentCents, dailySpentCents) {
  return {
    checked_at: NOW,
    item: id,
    issue_num: Number(id),
    lane: { value: 'implement', source: 'the caller' },
    complexity: { value: 'medium', source: 'the ready queue’s `medium` label', known: true },
    runner: { value: RUNNER, source: 'kew.toml [routing].default' },
    model: { value: MODEL, source: 'kew.toml [routing].default' },
    transport: { value: 'direct', source: 'default (direct)' },
    containment: {
      posture: 'contained',
      refusal_reason: null,
      runtime: 'docker',
      image: 'kew-agent:0.9.0',
      image_present: true,
      credential_names_present: [{ name: 'CLAUDE_CODE_OAUTH_TOKEN', set: true }],
      source: 'resolve_effective_sandbox, as dispatch resolves it'
    },
    budget: {
      runtime_cap_usd: 25.0,
      runtime_cap_source: 'kew.toml [agent].max_budget_usd',
      per_issue_cap_usd: 25.0,
      per_issue_cap_source: 'kew.toml [budget].per_issue_usd',
      issue_spent_usd: dollars(spentCents),
      daily_cap_usd: BUDGET.daily,
      daily_cap_source: 'kew.toml [budget].daily_usd',
      daily_spent_usd: dollars(dailySpentCents),
      per_issue_remaining_usd: dollars(2500 - spentCents),
      daily_remaining_usd: dollars(BUDGET.daily * 100 - dailySpentCents),
      runtime_cap_exceeds_issue_headroom: false,
      source: 'budget.effective_budget_view over recorded spend'
    },
    governor: {
      would_swap: false,
      reason: 'no_warm_alternatives',
      recommended_runner: RUNNER,
      recommended_model: MODEL,
      est_savings_usd: 0.0,
      source: 'runners.governor.cache_governor (advice only, nothing written)'
    },
    route_fingerprint: { value: '4be10c7a93e2', source: 'probe_route.resolve_probe_route (spends nothing)' },
    live_status: { value: 'unverified', source: 'no stored readiness probe for this boundary', probed_at: null },
    admission: {
      fleet_stopped: false,
      fleet_stopped_reason: '',
      open_hold: id === HELD.id,
      headroom: { usd: dollars(2500 - spentCents), binds: 'per_issue', source: '[budget].per_issue_usd less recorded spend' },
      source: 'the fleet latch, the approval log and recorded spend'
    },
    command: `kew dispatch ${id}`
  };
}

/* ------------------------------------------------------------------------ */
/* The world                                                                */
/* ------------------------------------------------------------------------ */

const BUDGET = { per_issue: 25.0, daily: 80.0, weekly: 400.0 };

export function buildWorld(timings) {
  const nodes = schedule(timings);
  const plans = [...nodes.values()];
  const allRuns = plans.flatMap((plan) => plan.runs.map((run) => ({ plan, run })));
  const files = {};

  // --- the epic ---------------------------------------------------------
  const tickCount = tickIdAt(LAST_TICK);
  const current = snapshotAt(nodes, EPOCH_MS, tickCount);
  current.tick_ts = '';
  const economics = drainAt(nodes, EPOCH_MS);
  const epicCents = plans.reduce((sum, plan) => sum + nodeCents(plan), 0);
  if (dollars(epicCents) !== economics.drain_total_usd) throw new Error('epic spend does not reconcile');
  // The journal: one tick at every instant a node changed status, then the last tick.
  const changes = [...new Set(plans.flatMap((p) => p.bands.map((b) => b.from)))].filter((t) => t < LAST_TICK).sort((a, b) => a - b);
  const instants = [...changes, LAST_TICK];
  // The heartbeat counts the journal it carries (count_epic_ticks), which is
  // what offers the epic's replay at all (#3142).
  const heartbeat = {
    last_tick_started_at: iso(LAST_TICK),
    last_tick_ts: iso(LAST_TICK + 2000),
    last_tick_duration_s: 2.0,
    tick_count: tickCount,
    interval_s: 60,
    last_signal: 'progressing',
    change_count: instants.length,
    tracker: 'github',
    driver_status: 'live',
    cadence_source: 'recorded'
  };
  files[`epics-${EPIC}.json`] = { epic_ref: EPIC, snapshot: current, economics, heartbeat };
  files['epics.json'] = [
    {
      epic_ref: EPIC,
      signal: 'progressing',
      counts: current.counts,
      drain_total_usd: economics.drain_total_usd,
      last_tick_ts: heartbeat.last_tick_ts,
      interval_s: 60,
      last_tick_duration_s: 2.0,
      last_attempt_started_at: iso(LAST_TICK),
      last_attempt_finished_at: iso(LAST_TICK + 2000),
      last_attempt_outcome: 'nothing',
      last_attempt_reached_tick: true,
      change_count: instants.length,
      label: 'since last tick',
      archived: false,
      archived_at: null,
      archive_reason: ''
    }
  ];

  let before = new Map();
  files[`epics-${EPIC}-ticks.json`] = instants.map((t) => {
    const snapshot = t === LAST_TICK ? { ...current, tick_ts: iso(t) } : snapshotAt(nodes, t, tickIdAt(t));
    const now = new Map(snapshot.nodes.map((n) => [n.id, n.status]));
    const moved = (status) => snapshot.nodes.filter((n) => n.status === status && before.get(n.id) !== status).map((n) => n.id);
    const actions = {
      schema: 1,
      dispatched: moved('in_progress'),
      merged: moved('done').map((id) => ({ id, approved_by: null })),
      held: [],
      stuck: moved('failed').map((id) => ({ id, reason: HOST_RESTART_REASON }))
    };
    before = now;
    return { id: snapshot.tick_id, tick_ts: iso(t), snapshot, actions, economics: drainAt(nodes, t) };
  });

  // The strip: the last thirty tick attempts, one a minute, the one that dispatched #106 among them.
  const runningStart = nodes.get(RUNNING).runs[0].started;
  files[`epics-${EPIC}-tick-strip.json`] = Array.from({ length: 30 }, (_, i) => {
    const started = LAST_TICK - (29 - i) * MINUTE;
    const dispatched = Math.abs(started - runningStart) < MINUTE && started <= runningStart ? 1 : 0;
    return {
      id: tickIdAt(started),
      ts_started: iso(started),
      ts_finished: iso(started + 2000),
      outcome: dispatched ? 'dispatched' : 'nothing',
      signal: 'progressing',
      n_dispatched: dispatched,
      n_merged: 0,
      n_held: 0,
      n_stuck: 0,
      journal_tick_id: dispatched ? tickIdAt(started) : null,
      error_class: null
    };
  });

  // The change strip (`tick-strip?changes=true`): the attempts that journaled
  // a change, one per journal row, as the server's change read returns them
  // whatever no-op ticks sit between. The Epics list draws and counts its
  // marks; served the plain strip above, it found one change beside the
  // header's 28 (design review c9, D15).
  files[`epics-${EPIC}-tick-strip-changes.json`] = files[`epics-${EPIC}-ticks.json`].map((row) => {
    const { dispatched, merged, stuck } = row.actions;
    const outcome = dispatched.length ? 'dispatched' : merged.length ? 'merged' : 'nothing';
    const started = Date.parse(row.tick_ts);
    return {
      id: row.id,
      ts_started: iso(started),
      ts_finished: iso(started + 2000),
      outcome,
      signal: 'progressing',
      n_dispatched: dispatched.length,
      n_merged: merged.length,
      n_held: 0,
      n_stuck: stuck.length,
      journal_tick_id: row.id,
      error_class: null
    };
  });

  files[`epics-${EPIC}-timeline.json`] = {
    epic_ref: EPIC,
    now: NOW,
    span: { start: iso(EPIC_START), end: NOW },
    nodes: plans.map((plan) => ({
      id: plan.id,
      title: plan.title,
      bands: plan.bands.map((b) => ({ status: b.status, from: iso(b.from), to: b.to === null ? null : iso(b.to) })),
      runs: plan.runs.map((r) => ({
        run_id: r.run_id,
        kind: r.kind === 'dispatch' ? 'dispatch' : r.kind,
        status: r.status,
        started_at: iso(r.started),
        finished_at: r.finished === null ? null : iso(r.finished),
        cost_usd: dollars(r.cents),
        attempt_number: r.attempt
      })),
      rounds: plan.rounds.map((r) => ({ round: r.round, verdict: r.verdict, recorded_at: iso(r.recorded_at) })),
      merged_at: plan.merged_at === null ? null : iso(plan.merged_at)
    }))
  };

  for (const plan of plans) {
    const cents = nodeCents(plan);
    files[`epics-${EPIC}-nodes-${plan.id}.json`] = {
      id: plan.id,
      runs: plan.runs.map((r) => ({
        run_id: r.run_id,
        status: r.status,
        model: MODEL,
        runner: RUNNER,
        cost_usd: dollars(r.cents),
        total_tokens: totalTokens(r),
        cache_creation_tokens: cacheWrites(r),
        cache_read_tokens: r.tokens.cache_read_tokens,
        pr_url: r.kind === 'dispatch' && r.status === 'success' ? prUrl(plan.id) : null,
        started_at: iso(r.started),
        finished_at: r.finished === null ? null : iso(r.finished)
      })),
      lifetime_usd: dollars(cents),
      drain_usd: dollars(cents)
    };
  }

  // --- runs -------------------------------------------------------------
  const newestFirst = [...allRuns].sort((a, b) => b.run.started - a.run.started);
  files['runs.json'] = newestFirst.map(({ plan, run }) => runRow(plan, run));
  // Every run row has its detail, flight and ledger page, and every sealed run its diff.
  let seq = 2000;
  for (const { plan, run } of newestFirst) {
    files[`runs-${run.run_id}.json`] = runRow(plan, run, { detail: true });
    const flight = flightOf(plan, run);
    files[`runs-${run.run_id}-flight.json`] = flight;
    const audit = auditOf(plan, run, seq);
    seq = audit.next_seq + 10;
    files[`runs-${run.run_id}-audit.json`] = audit;
    if (flight.artifact_node_id !== null) {
      files[`runs-${run.run_id}-nodes-${flight.artifact_node_id.replace(':', '_')}-diff.json`] = diffOf(plan, run, flight);
    }
  }

  // --- spend ------------------------------------------------------------
  // kew's windows (`budget._spend_windows`, `service/report.spend_windows`):
  // the day is the UTC calendar day of now, the week that day and the six
  // before it, and a run counts in a window by when it STARTED.
  const epochDate = new Date(EPOCH_MS);
  const dayStart = Date.UTC(epochDate.getUTCFullYear(), epochDate.getUTCMonth(), epochDate.getUTCDate());
  const weekStart = dayStart - 6 * 24 * HOUR;
  const startedSince = (since) => allRuns.filter(({ run }) => run.started >= since).reduce((s, { run }) => s + run.cents, 0);
  const dailyCents = startedSince(dayStart);
  const weeklyCents = startedSince(weekStart);
  if (weeklyCents !== epicCents) throw new Error('every run belongs to the epic and started this week, so the week must equal it');
  const windows = {
    daily: { window_days: 1, spent: dollars(dailyCents), cap: BUDGET.daily, agentic: dollars(dailyCents), interactive: 0.0 },
    weekly: { window_days: 7, spent: dollars(weeklyCents), cap: BUDGET.weekly, agentic: dollars(weeklyCents), interactive: 0.0 }
  };
  files['report-spend.json'] = windows;
  const field = (value) => ({ effective: value, file_value: value, override: null });
  files['config-budget.json'] = {
    'budget.per_issue_usd': field(BUDGET.per_issue),
    'budget.daily_usd': field(BUDGET.daily),
    'budget.weekly_usd': field(BUDGET.weekly)
  };

  // --- the needs-you set and the work projection ------------------------
  const awaiting = nodes.get(AWAITING);
  const failed = nodes.get(FAILED);
  /** A merge decision row; it has stood since the READY landed, or since the verify that held the merge failed. */
  const mergeRow = (plan) => {
    const pr = prNumber(plan.id);
    const wait = mergeWait(plan);
    return {
      type: 'pr_review',
      consequence: 'review',
      age: iso(plan.verify?.outcome === 'failed' ? plan.verify.at : lastRun(plan).finished),
      reason: `merge decision on PR #${pr}`,
      primary_action: 'open',
      issue_num: Number(plan.id),
      item_id: plan.id,
      estimate_usd: null,
      epic_ref: EPIC,
      url: prUrl(plan.id),
      pr_number: pr,
      work_unread: false,
      attention_kind: 'merge_decision',
      label: '',
      command: '',
      sentence: wait.sentence,
      title: plan.title,
      link: wait.link,
      ...(wait.verify_link ? { verify_link: wait.verify_link } : {}),
      node_ids: [],
      repo: null
    };
  };
  const rows = [
    {
      type: 'approval_hold',
      consequence: 'money',
      age: iso(EPOCH_MS - 47 * MINUTE),
      reason: `awaiting approval — ${HOLD_BASIS}`,
      primary_action: 'approve',
      issue_num: Number(HELD.id),
      item_id: HELD.id,
      estimate_usd: HELD.estimate_usd,
      epic_ref: null,
      url: null,
      pr_number: null,
      work_unread: false,
      attention_kind: 'approval',
      label: '',
      command: '',
      // The hold's sentence as the server writes it now (design review c9,
      // D12; `kew.approval_basis.approval_sentence` for a basis with no
      // history): the action and its estimate, which is the per-issue cap and
      // never a ceiling on the run, not the reason's debug wording.
      sentence: `approve a run estimated at $${HELD.estimate_usd.toFixed(2)}, the per-issue cap; no cost history to estimate from`,
      title: HELD.title,
      link: null,
      node_ids: [],
      repo: null
    },
    mergeRow(awaiting),
    mergeRow(nodes.get(REFUSED)),
    {
      type: 'epic_wait',
      consequence: 'wait',
      age: iso(failed.bands.at(-1).from),
      reason: `#${FAILED} — ${HOST_RESTART_REASON}`,
      primary_action: HOST_RESTART_ACTION,
      issue_num: Number(FAILED),
      item_id: FAILED,
      estimate_usd: null,
      epic_ref: EPIC,
      url: issueUrl(FAILED),
      pr_number: null,
      work_unread: false,
      attention_kind: 'host_restart_repeated',
      label: HOST_RESTART_SENTENCE,
      command: `kew epic retry ${EPIC} ${FAILED}`,
      sentence: HOST_RESTART_SENTENCE,
      title: failed.title,
      link: null,
      node_ids: [],
      repo: null
    },
    {
      type: 'orphan_pr',
      consequence: 'hygiene',
      age: iso(EPOCH_MS - 3 * HOUR),
      reason: 'orphan PR — no-record',
      primary_action: 'ack',
      issue_num: null,
      item_id: '',
      estimate_usd: null,
      epic_ref: null,
      url: `${GH}/pull/${ORPHAN_PR}`,
      pr_number: ORPHAN_PR,
      work_unread: false,
      attention_kind: '',
      label: '',
      command: '',
      sentence: `PR #${ORPHAN_PR} is open on ${ORPHAN_BRANCH}, a branch no kew run recorded`,
      title: '',
      link: null,
      node_ids: [],
      repo: null
    }
  ];
  const ready = nodes.get(READY_ITEM);
  const lastFinished = newestFirst.find(({ run }) => run.finished !== null);
  files['dashboard.json'] = {
    banner: { fleet_stopped: false, no_live_driver: false },
    rows,
    glance: {
      fleet: { stopped: false, pending: 1 },
      caps: windows,
      routing: ['small', 'medium', 'large'].map((complexity) => ({ complexity, route: `${RUNNER}/${MODEL}`, provenance: 'file' })),
      ready_queue: [
        { issue_num: Number(READY_ITEM), item_id: READY_ITEM, title: ready.title, complexity: 'medium', route: `${RUNNER}/${MODEL}`, estimate_usd: 3.5 }
      ],
      queue_candidates: CANDIDATES.map(({ id, title }) => ({ issue_num: Number(id), item_id: id, title })),
      recent_completions: newestFirst
        .filter(({ run }) => run.status === 'success')
        .slice(0, 3)
        .map(({ plan, run }) => ({
          run_id: run.run_id,
          issue_num: Number(plan.id),
          item_id: plan.id,
          title: plan.title,
          status: run.status,
          cost_usd: dollars(run.cents),
          model: MODEL,
          finished_at: iso(run.finished)
        })),
      pressure: pressure(2)
    },
    as_of: { runs: NOW, tracker: NOW, forge: NOW },
    unavailable: [],
    needs_you_by_epic: { [EPIC]: rows.filter((r) => r.epic_ref === EPIC).length }
  };
  if (!lastFinished) throw new Error('the world has no finished run');

  const viewed = plans.filter((plan) => plan.runs.length > 0);
  files['dashboard-work.json'] = {
    pressure: pressure(2),
    projection_revision: 'wp1:ledger0000000000000000000000002',
    composed_at: NOW,
    unreadable: [],
    views: viewed.map(workView)
  };
  for (const plan of viewed) {
    files[`verbs-work-${plan.id}.json`] = workView(plan);
    files[`work-${plan.id}-pipeline.json`] = pipelineOf(plan);
    files[`work-${plan.id}-evidence.json`] = evidenceOf(plan);
    files[`work-${plan.id}-description.json`] = descriptionOf(plan.id, plan.title);
  }
  // Every other item the world names — the nodes nothing has run on, and the
  // held item — has its pipeline, evidence and description too, all idle.
  for (const [id, title] of [
    ...plans.filter((plan) => plan.runs.length === 0).map((plan) => [plan.id, plan.title]),
    [HELD.id, HELD.title]
  ]) {
    files[`work-${id}-pipeline.json`] = idlePipeline(id);
    files[`work-${id}-evidence.json`] = evidenceOf({ id, runs: [], rounds: [] });
    files[`work-${id}-description.json`] = descriptionOf(id, title);
  }
  files['dispatch-resolve.json'] = dispatchPlan(READY_ITEM, 0, dailyCents);
  files[`dispatch-resolve/${READY_ITEM}.json`] = dispatchPlan(READY_ITEM, 0, dailyCents);
  files[`dispatch-resolve/${HELD.id}.json`] = dispatchPlan(HELD.id, 0, dailyCents);
  files['approvals.json'] = [{ issue_num: Number(HELD.id), item_id: HELD.id, estimate_usd: HELD.estimate_usd, basis: HOLD_BASIS }];
  files['run-requests.json'] = { requests: [] };

  const refused = nodes.get(REFUSED);
  const verdictOf = (plan) => {
    const review = plan.runs.filter((r) => r.kind === 'adversarial_review').at(-1);
    return { round: review.verdict.round, verdict: review.verdict.verdict, head_sha: review.verdict.head_sha, recorded_at: iso(review.finished) };
  };
  const headOf = (plan) => ({ sha: plan.runs.filter((r) => r.kind === 'adversarial_review').at(-1).verdict.head_sha, source: 'snapshot', at: iso(lastRun(plan).started) });
  files['prs-freshness.json'] = {
    rows: [awaiting, refused].map((plan) => ({
      item_id: plan.id,
      epic_ref: EPIC,
      pr_number: prNumber(plan.id),
      pr_url: prUrl(plan.id),
      last_verdict: verdictOf(plan),
      latest_head: headOf(plan),
      review_state: 'current',
      base: { state: 'clean', reason: 'the head merges into its base', since: iso(lastRun(plan).finished), at: iso(EPOCH_MS - 5 * MINUTE), source: 'snapshot' },
      observed_at: iso(EPOCH_MS - 5 * MINUTE)
    })),
    next_cursor: null,
    unavailable: [],
    composed_at: iso(EPOCH_MS - 5 * MINUTE)
  };

  // --- the audit log ----------------------------------------------------
  const recent = [];
  for (const { plan, run } of newestFirst) {
    if (run.started > EPOCH_MS - 6 * HOUR) recent.push({ at: run.started + 1000, event: 'dispatch_owner_recorded', plan, run, summary: serverSummary(ownerPayload(plan)) });
    if (run.finished !== null && run.finished > EPOCH_MS - 6 * HOUR) {
      recent.push({
        at: run.finished,
        event: run.verdict ? 'adversarial_review_verdict' : 'dispatch_completed',
        plan,
        run,
        summary: serverSummary(finishPayload(run))
      });
    }
  }
  recent.sort((a, b) => b.at - a.at);
  files['audit.json'] = {
    // The loop's events are the ledger's oldest (`loopOf`), so these follow them.
    events: recent.map((e, i) => ({
      id: LOOP_EVENTS + recent.length - i,
      timestamp: iso(e.at),
      event: e.event,
      issue_num: Number(e.plan.id),
      item_id: null,
      run_id: e.run.run_id,
      actor_type: 'host',
      actor_id: 'kew',
      summary: e.summary,
      epic_ref: EPIC
    })),
    next_after: null
  };
  const bucketMs = 15 * MINUTE;
  const since = EPOCH_MS - 24 * HOUR - 6 * HOUR;
  const buckets = [];
  for (let start = since; start < EPOCH_MS - 6 * HOUR; start += bucketMs) {
    const counts = {};
    for (const { run } of allRuns) {
      if (run.started >= start && run.started < start + bucketMs) counts.dispatch_owner_recorded = (counts.dispatch_owner_recorded ?? 0) + 1;
      if (run.finished !== null && run.finished >= start && run.finished < start + bucketMs) {
        const name = run.verdict ? 'review_outcome' : 'dispatch_completed';
        counts[name] = (counts[name] ?? 0) + 1;
      }
    }
    if (Object.keys(counts).length > 0) buckets.push({ start: iso(start), counts });
  }
  files['audit-histogram.json'] = {
    since: iso(since),
    until: iso(EPOCH_MS - 6 * HOUR),
    bucket_s: bucketMs / 1000,
    total: buckets.reduce((s, b) => s + Object.values(b.counts).reduce((x, y) => x + y, 0), 0),
    buckets
  };
  files['audit-status.json'] = { events: recent.length, verified: true, anchors_matched: 1, anchors_total: 1 };

  // --- the host ---------------------------------------------------------
  files['fleet-status.json'] = { stopped: false, pending: 1 };
  files['meta.json'] = { billing: 'metered', plan_label: 'API (metered)', plan_monthly_usd: 0.0, version: '0.9.0+demo' };
  files['auth-session.json'] = { authenticated: true, csrf_token: 'demo-csrf' };
  files['bundle.json'] = { present: true, git_sha: '', built_at: 0, stale: false };
  files['health.json'] = healthOf();
  files['health-env.json'] = healthEnv();
  files['config-effective.json'] = effectiveConfig();
  files['config-human_review.json'] = {
    delivery_repo: REPO,
    values: { 'human_review.max_pending_reviews': 4, 'human_review.max_diff_lines': 2000, 'human_review.max_paths': 40 },
    generation: 1,
    expires_at: null,
    source: 'system',
    approvers: [],
    pending_reviews: 2,
    over_cap: false,
    pending_reversion: null
  };
  files['config-overridable.json'] = overridable();

  // --- reports, prompts, loops and the doctor ---------------------------
  files['report-models.json'] = scorecardOf(plans, weeklyCents);
  files['report-outcomes.json'] = outcomesOf(plans, weeklyCents);
  for (const [role, kind] of Object.entries(PROMPT_ROLES)) {
    files[`prompts-${role}-outcomes.json`] = promptOutcomesOf(plans, role, kind);
    files[`prompts-${role}-versions.json`] = {
      role,
      generation: 0,
      can_decide: true,
      decide_refusal: null,
      max_body_chars: 4000,
      active_version: null,
      versions: []
    };
  }
  // One goal loop, achieved before any window the spend readings count
  // (`LOOP_AT`), so the loops page has a surface and every spend figure
  // still reconciles to the epic's runs.
  const loop = loopOf();
  files['loops.json'] = [loop.row];
  files[`loops-${LOOP_ID}.json`] = { ...loop.row, iterations: loop.iterations };
  files[`loops-${LOOP_ID}-iterations.json`] = loop.iterations;
  files[`loops-${LOOP_ID}-audit.json`] = loop.audit;
  files['readiness.json'] = readinessOf();

  const manifest = {
    epoch: NOW,
    repo: REPO,
    epic_ref: EPIC,
    refused_item: REFUSED,
    awaiting_merge_item: AWAITING,
    failed_item: FAILED,
    blocked_item: BLOCKED,
    hold_id: HELD.id,
    run_id: lastDispatch(refused).run_id,
    needs_you: rows.length,
    orphan_pr: ORPHAN_PR,
    queue_candidates: CANDIDATES.map(({ id }) => id),
    held_out_failed: HELD_OUT_FAILED,
    held_out_declared: HELD_OUT_DECLARED,
    refused_head: refused.verify.head.slice(0, 8),
    spend: { epic_usd: economics.drain_total_usd, daily_usd: windows.daily.spent, weekly_usd: windows.weekly.spent }
  };
  return { files, manifest };
}

function healthOf() {
  const at = iso(EPOCH_MS - 40_000);
  return {
    checked_at: NOW,
    observed_window_days: 7,
    runtimes: [
      { name: 'docker', configured: true, backed: true, present: true },
      { name: 'podman', configured: false, backed: true, present: false }
    ],
    images: [{ runner: RUNNER, ref: 'kew-agent:0.9.0', source: 'images.claude-code', routed: true, present: true }],
    posture: ['default', 'review'].map((lane) => ({
      lane,
      runner: RUNNER,
      runtime: 'docker',
      contained: true,
      egress: 'kew-allowlist',
      credential_mode: 'real',
      dangerously_skip_containment: false,
      posture: 'sandboxed',
      refusal: null
    })),
    observed_7d: { docker: 0 },
    runners: [
      {
        name: RUNNER,
        binary: 'claude',
        binary_present: true,
        version: '2.1.4',
        version_supported: true,
        credential_names: [
          { name: 'ANTHROPIC_API_KEY', set: false },
          { name: 'CLAUDE_CODE_OAUTH_TOKEN', set: true }
        ],
        routed_tiers: ['default'],
        review_lane: true
      }
    ],
    trackers: {
      github: {
        reachable: true,
        auth_ok: true,
        scopes: ['repo', 'workflow'],
        rate: { limit: 5000, remaining: 4870, reset_at: iso(EPOCH_MS + 0) },
        observed_at: at,
        observed_age_s: 40.0,
        reason: ''
      },
      linear: { configured: false, credential_set: false, reachable: null, team: '' }
    },
    sweep: {
      present: true,
      heartbeat_at: at,
      heartbeat_age_s: 40.0,
      interval_s: 60.0,
      last_attempt_at: at,
      last_ok_at: at,
      last_sweep_at: at,
      last_result: 'ok',
      query_failures: 0,
      last_ok_age_s: 40.0,
      last_attempt_age_s: 40.0
    },
    drivers: [
      {
        epic_ref: EPIC,
        liveness: 'live',
        tick_age_s: 30.0,
        interval_s: 60.0,
        last_tick_at: iso(LAST_TICK),
        lock: { pid_alive: true, heartbeat_age_s: 10.0, observed_age_s: 40.0 }
      }
    ],
    watchers: [
      { name: 'delegation', enabled: false, running: null, detail: 'kew delegate watch — assign-to-kew trigger', last_seen_at: null, last_seen_age_s: null },
      { name: 'epic_stream', enabled: true, running: true, detail: 'serve SSE fan-out, one poller per watched epic', pollers: 1 }
    ],
    loops: { pursuing: 0, stale: 0 },
    fleet: { stopped: false, pending: 1, reason: '' }
  };
}

/**
 * #3461 — the world's one goal loop: an issue kew pursued on its own before
 * the epic, through four assistant turns to an achieved gate. It finished ten
 * weeks before the epoch, outside the budget's week, the 30-day scorecard and
 * the 8-week outcomes, so those readings stay the epic's runs alone; and its
 * iterations name no run, as no run of the world's ledger is the loop's.
 */
const LOOP_ID = '5f3c9a1e-2b7d-4c80-9e14-a6d2f08b7c35';
const LOOP_AT = EPOCH_MS - 70 * 24 * HOUR;
const LOOP_STEPS = [
  { minutes: 6, cents: 38, tokens: 51_204 },
  { minutes: 9, cents: 27, tokens: 36_880 },
  { minutes: 7, cents: 31, tokens: 42_515 },
  { minutes: 5, cents: 19, tokens: 24_090 }
];
/** The loop's audit rows: started, checkout bound, one per step, finished. */
const LOOP_EVENTS = LOOP_STEPS.length + 3;

function loopOf() {
  const iterations = [];
  let t = LOOP_AT + MINUTE;
  for (const [idx, step] of LOOP_STEPS.entries()) {
    t += step.minutes * MINUTE;
    iterations.push({
      iteration_id: `${LOOP_ID.slice(0, 24)}${String(idx).padStart(12, '0')}`,
      idx,
      kind: 'assistant_turn',
      status: 'pursuing',
      cost_usd: dollars(step.cents),
      tokens: step.tokens,
      run_id: null,
      started_at: iso(t),
      finished_at: iso(t)
    });
  }
  const finished = t + 2 * MINUTE;
  const row = {
    motion: 'settled',
    alive: false,
    loop_id: LOOP_ID,
    source: 'issue',
    issue_num: 98,
    repo: REPO,
    goal_text:
      '#98: Round half to even when a posting splits a cent\n\nA posting split three ways leaves a cent over; ' +
      'round each share half to even so the shares still sum to the posting.',
    runner_name: RUNNER,
    model: MODEL,
    drive_mode: 'delegated',
    status: 'achieved',
    status_reason: 'the gate passed: the rounding tests pass on the loop branch',
    needs_human: false,
    stale: false,
    budget_usd: 5.0,
    spent_usd: dollars(LOOP_STEPS.reduce((sum, step) => sum + step.cents, 0)),
    wall_clock_secs: 3600,
    max_steps: 20,
    step_count: iterations.length,
    created_at: iso(LOOP_AT),
    updated_at: iso(finished),
    finished_at: iso(finished)
  };
  const event = (id, at, name, summary = '') => ({
    id,
    timestamp: iso(at),
    event: name,
    actor_type: 'host',
    actor_id: 'kew',
    loop_id: LOOP_ID,
    summary
  });
  // Newest first, as `/api/loops/{id}/audit` answers.
  const audit = [
    event(1, LOOP_AT, 'loop_started'),
    event(2, LOOP_AT + MINUTE, 'loop_checkout_bound'),
    ...iterations.map((it, i) => event(3 + i, Date.parse(it.started_at), 'iteration_recorded')),
    event(LOOP_EVENTS, finished, 'loop_finished', row.status_reason)
  ].reverse();
  return { row, iterations, audit };
}

function healthEnv() {
  const v = (name, purpose, required_by, set) => ({ name, purpose, required_by, set });
  return {
    as_seen_by: 'the serve process',
    vars: [
      v('GITHUB_TOKEN', 'GitHub API and git push', 'every repo read and publication', true),
      v('GH_TOKEN', 'GitHub API (gh CLI’s own name)', 'the gh CLI', false),
      v('LINEAR_API_KEY', 'Linear GraphQL API', 'a Linear-tracked project', false),
      v('ANTHROPIC_API_KEY', 'Anthropic API account', 'claude-code, API billing', false),
      v('CLAUDE_CODE_OAUTH_TOKEN', 'Claude subscription login, forwardable to a container', 'claude-code, subscription billing', true),
      v('KEW_PROJECT', 'which project directory kew resolves against', 'every command', false),
      v('KEW_REPO', 'the delivery repository override', 'an operator override', false),
      v('KEW_LOG_DIR', 'where the ledger lives', 'an operator override', false)
    ]
  };
}

function effectiveConfig() {
  const f = (value, source = 'kew.toml') => ({ value, source });
  return {
    config_path: '/srv/acme/ledger/kew.toml',
    human_review_unavailable: '',
    fields: {
      'agent.max_budget_usd': f(25.0),
      'agent.model': f('', 'default'),
      'agent.runner': f(RUNNER),
      'agent.timeout_minutes': f(120, 'default'),
      'budget.daily_usd': f(BUDGET.daily),
      'budget.per_issue_usd': f(BUDGET.per_issue),
      'budget.weekly_usd': f(BUDGET.weekly),
      'epic.max_parallel': f(3),
      'human_review.max_diff_lines': { value: 2000, source: 'human_review policy (system values)', generation: 1, expires_at: null, over_cap: false },
      'human_review.max_paths': { value: 40, source: 'human_review policy (system values)', generation: 1, expires_at: null, over_cap: false },
      'human_review.max_pending_reviews': { value: 4, source: 'human_review policy (system values)', generation: 1, expires_at: null, over_cap: false },
      'logging.dir': f('/srv/acme/ledger/.kew'),
      'parallel.max_workers': f(3),
      'project.repo': f(REPO),
      'retry.max_attempts': f(3, 'default'),
      'review.max_remediation_usd': f(40.0, 'default'),
      'review.max_review_usd': f(10.0, 'default'),
      'review.max_rounds': f(2, 'default'),
      'routing.default': f({ runner: RUNNER, model: '' }),
      'sandbox.allow_uncontained_flows': f(false, 'default'),
      'sandbox.enabled': f(true)
    }
  };
}

function overridable() {
  const f = (value, default_value) => ({ effective: value, file_value: value, default_value, override: null });
  return {
    agent: { 'agent.max_budget_usd': f(25.0, 25.0), 'agent.model': f('', '') },
    breaker: { 'breaker.cost_anomaly_factor': f(3.0, 3.0) },
    budget: {
      'budget.daily_usd': f(BUDGET.daily, 50.0),
      'budget.per_issue_usd': f(BUDGET.per_issue, 15.0),
      'budget.weekly_usd': f(BUDGET.weekly, 200.0)
    },
    review: { 'review.model': f('', '') },
    routing: {}
  };
}

/* ------------------------------------------------------------------------ */
/* Reports over the world's own runs                                         */
/* ------------------------------------------------------------------------ */

/** Nearest-rank quantile of a sorted, non-empty list. */
const quantile = (sorted, q) => sorted[Math.max(0, Math.ceil(q * sorted.length) - 1)];
const median = (values) => {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 1 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
};
const isReady = (verdict) => verdict.startsWith('READY');
/** The round at which a node's review first said READY, or null. */
const roundToReady = (plan) => plan.rounds.find((r) => isReady(r.verdict))?.round ?? null;
const hasPr = (plan) => plan.runs.some((r) => r.kind === 'dispatch' && r.status === 'success');

/** `/api/report/models`: the one model this factory routes to, over every run. */
function scorecardOf(plans, weeklyCents) {
  const cell = {
    runs: plans.reduce((n, p) => n + p.runs.filter((r) => r.finished !== null).length, 0),
    opened: plans.filter(hasPr).length,
    merged: plans.filter((p) => p.status === 'done').length,
    review_total: plans.reduce((n, p) => n + p.rounds.length, 0),
    review_n: plans.filter((p) => p.rounds.length > 0).length,
    spend: dollars(weeklyCents),
    planned_spend: 0,
    paired_spend: 0,
    planned_n: 0
  };
  return {
    days: 30,
    rows: [{ model: MODEL, channel: 'hosted', lifecycle_status: 'ok', ...cell, by_complexity: [{ complexity: 'medium', ...cell }] }]
  };
}

/** `/api/report/outcomes`: eight weeks, the factory's whole history in the last. */
function outcomesOf(plans, weeklyCents) {
  const weekMs = 7 * 24 * HOUR;
  const epochDay = new Date(EPOCH_MS);
  const monday = Date.UTC(epochDay.getUTCFullYear(), epochDay.getUTCMonth(), epochDay.getUTCDate()) - ((epochDay.getUTCDay() + 6) % 7) * 24 * HOUR;
  if (EPIC_START < monday) throw new Error('the epic starts before the week the outcomes put it in');
  const ready = plans.filter((p) => roundToReady(p) !== null);
  const merged = plans.filter((p) => p.status === 'done');
  const hours = ready
    .map((p) => (p.rounds.find((r) => isReady(r.verdict)).recorded_at - p.runs[0].started) / HOUR)
    .sort((a, b) => a - b);
  const rounds = ready.map(roundToReady).sort((a, b) => a - b);
  const mergedCost = merged.map((p) => nodeCents(p) / 100).sort((a, b) => a - b);
  const unattributed = plans.flatMap((p) => p.runs).filter((r) => r.kind === 'dispatch' && r.status !== 'success');
  const round2 = (n) => Math.round(n * 100) / 100;
  const empty = { p50: null, p90: null, max: null };
  const weeks = Array.from({ length: 8 }, (_, i) => {
    const start = new Date(monday - (7 - i) * weekMs).toISOString().slice(0, 10);
    if (i < 7) {
      return {
        week_start: start,
        merged_main: 0,
        merged_kew_touched: 0,
        merged_kew_authored: 0,
        ready_items: 0,
        ready_hours: empty,
        rounds_to_ready: { p50: null, max: null },
        cost_per_merged: { mean: null, p50: null },
        spend_total: 0,
        spend_unattributed: 0,
        nodes_done: 0,
        pr_sync_observed_at: null,
        partial: false
      };
    }
    return {
      week_start: start,
      merged_main: merged.length,
      merged_kew_touched: merged.length,
      merged_kew_authored: merged.length,
      ready_items: ready.length,
      ready_hours: { p50: round2(quantile(hours, 0.5)), p90: round2(quantile(hours, 0.9)), max: round2(hours.at(-1)) },
      rounds_to_ready: { p50: quantile(rounds, 0.5), max: rounds.at(-1) },
      cost_per_merged: { mean: round2(mergedCost.reduce((s, c) => s + c, 0) / mergedCost.length), p50: round2(median(mergedCost)) },
      spend_total: dollars(weeklyCents),
      spend_unattributed: dollars(unattributed.reduce((s, r) => s + r.cents, 0)),
      nodes_done: merged.length,
      pr_sync_observed_at: NOW,
      partial: true
    };
  });
  return { weeks, min_sample: 3, merged_main_head: hex(prng(SEED + 2), 40), merged_main_unavailable: null };
}

/** The prompt roles the console reads outcomes for, and the run kind each one's prompt drives. */
const PROMPT_ROLES = {
  dispatch: 'dispatch',
  adversarial_review: 'adversarial_review',
  review_remediation: 'review_remediation',
  ci_fix: 'ci_fix',
  resolve: 'resolve'
};

/** `/api/prompts/{role}/outcomes`: how this world's items went under each role's built-in prompt. */
function promptOutcomesOf(plans, role, kind) {
  const ran = plans.filter((p) => p.runs.some((r) => r.kind === kind && r.finished !== null));
  const base = { version: null, label: 'built-in', state: null, n: ran.length, comparable: ran.length >= 10 };
  if (kind === 'adversarial_review') {
    const verdicts = {};
    for (const plan of plans) for (const r of plan.rounds) verdicts[r.verdict] = (verdicts[r.verdict] ?? 0) + 1;
    const n = plans.reduce((sum, p) => sum + p.rounds.length, 0);
    return { role, kind: 'reviewer', min_items: 10, versions: n === 0 ? [] : [{ ...base, n, comparable: n >= 10, verdicts }], generation: 0 };
  }
  if (ran.length === 0) return { role, kind: 'implementer', min_items: 10, versions: [], generation: 0 };
  const reviewed = ran.filter((p) => p.rounds.length > 0);
  const ready = ran.filter((p) => roundToReady(p) !== null);
  const costs = ran.map((p) => p.runs.filter((r) => r.kind === kind).reduce((s, r) => s + r.cents, 0) / 100);
  const row = {
    ...base,
    round_one_items: reviewed.length,
    first_pass_rate: reviewed.length === 0 ? null : reviewed.filter((p) => isReady(p.rounds[0].verdict)).length / reviewed.length,
    ready_items: ready.length,
    mean_rounds_to_ready: ready.length === 0 ? null : ready.reduce((s, p) => s + roundToReady(p), 0) / ready.length,
    merged_items: ran.filter((p) => p.status === 'done').length,
    median_cost_usd: Math.round(median(costs) * 100) / 100
  };
  return { role, kind: 'implementer', min_items: 10, versions: [row], generation: 0 };
}

/** `/api/readiness`: `kew doctor` passing — read only on a first run, which this world is not. */
function readinessOf() {
  const check = (id, summary) => ({ id, status: 'ok', summary, hint: null, fix: null, details: [], warnings: [] });
  return {
    schema: 1,
    checks: [
      check('project-config', 'kew.toml found'),
      check('required-tools', 'git, gh and docker are on PATH'),
      check('runner-credential', 'claude-code has a credential'),
      check('gh-auth', 'gh is signed in'),
      check('containment', 'agent runs are contained'),
      check('container-runtime', 'docker is running'),
      check('route-image', 'kew-agent:0.9.0 is present'),
      check('model-eol', 'no routed model is retiring')
    ],
    ok: true,
    checked_at: NOW
  };
}

/**
 * Copied from the factory fixture set as they are (see the header): the
 * prompt previews and the model catalog, never a role's outcomes or versions,
 * which are generated above.
 */
const PRODUCT_FILES = /^(prompts(-[a-z_]+)?|models)\.json$/;
const isCopied = (name) => PRODUCT_FILES.test(name) && !/-(outcomes|versions)\.json$/.test(name);

/** Serialise one payload the way every committed fixture is: two-space JSON and a newline. */
export const serialise = (payload) => `${JSON.stringify(payload, null, 2)}\n`;

export function writeWorld(dir = WORLD) {
  const timings = JSON.parse(readFileSync(new URL('timings.json', dir), 'utf8'));
  const { files, manifest } = buildWorld(timings);
  const root = fileURLToPath(dir);
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    if (entry.name.endsWith('.json') && !NOT_FIXTURES.includes(entry.name)) rmSync(`${root}${entry.name}`);
    if (entry.isDirectory()) rmSync(`${root}${entry.name}`, { recursive: true });
  }
  for (const [name, payload] of Object.entries(files)) {
    mkdirSync(fileURLToPath(new URL('./', new URL(name, dir))), { recursive: true });
    writeFileSync(new URL(name, dir), serialise(payload));
  }
  for (const name of readdirSync(fileURLToPath(FACTORY)).filter(isCopied).sort()) {
    copyFileSync(new URL(name, FACTORY), new URL(name, dir));
  }
  writeFileSync(new URL('manifest.json', dir), serialise(manifest));
  return Object.keys(files).length;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  console.log(`wrote ${writeWorld()} world files to ${fileURLToPath(WORLD)}`);
}
