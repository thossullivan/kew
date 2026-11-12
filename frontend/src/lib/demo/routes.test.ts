/**
 * Every read the console makes has an answer in the demo world (#3141).
 *
 * The demo has no backend, so a read with no world file answers 404 and the
 * page draws its error state — `/loops` and `/scorecard` did, and 13 of 20
 * runs had no detail page. This test reads every `/api` path the console's
 * source names (string and template literals in `src/`, comments stripped),
 * and holds each one to exactly one of:
 *
 * - **answered**: every concrete URL the world can be asked for — every run,
 *   epic, node, item and prompt role it names — gets a 200 from the demo shim
 *   (`demoApi`, the code the demo build runs), or the quiet stream;
 * - **deliberately empty**: the world names nothing the read could be asked
 *   for, or the page reads the 404 as a sentence; each entry says which, and
 *   the claim is checked;
 * - **written, never read**: a write path, which the shim answers with the
 *   demo's sentence (`writes.test.ts`);
 * - **never read**: an `api.ts` client no page calls, checked by a search.
 *
 * A path the console starts reading lands in none of them and fails here
 * until the world answers it or it is listed with its reason.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { readFixtureSet } from '../../../demo/fixture-bundle';
import { DEMO_FIXTURE_SET } from '../../../demo/fixture-set';
import type { EpicDetail, FlightTimeline, Hold, PromptRole, Run, WorkProjection } from '../api';
import { demoApi } from './respond';

const SRC = fileURLToPath(new URL('../../', import.meta.url));
const set = readFixtureSet(DEMO_FIXTURE_SET);
const api = demoApi(set);
const file = <T>(name: string): T => set.files[name] as T;

/** Every `.ts` / `.svelte` source under `src/`, tests and the demo shim itself excluded. */
function sources(dir = SRC): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return path.endsWith(join('lib', 'demo')) ? [] : sources(path);
    return /\.(ts|svelte)$/.test(name) && !/\.test\.ts$/.test(name) ? [path] : [];
  });
}

/** Comments out, so a path a comment names is not a read. */
const code = (text: string) => text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:\\])\/\/[^\n]*/g, '$1');

/**
 * The `/api` path templates a source's literals name: each `${…}` becomes
 * `{}`, and one that trails a segment (`/flight${query}`) is its query.
 */
function apiTemplates(text: string): string[] {
  const src = code(text);
  const out = new Set<string>();
  for (const m of src.matchAll(/["'`](?=\/api[/"'`?$])/g)) {
    let i = (m.index ?? 0) + 1;
    let path = '';
    while (i < src.length) {
      if (src[i] === '$' && src[i + 1] === '{') {
        let depth = 0;
        let j = i + 1;
        for (; j < src.length; j += 1) {
          if (src[j] === '{') depth += 1;
          else if (src[j] === '}' && --depth === 0) break;
        }
        path += '{}';
        i = j + 1;
      } else if (/[\w/.-]/.test(src[i])) {
        path += src[i];
        i += 1;
      } else break;
    }
    out.add(path.replace(/([^/])\{\}$/, '$1'));
  }
  return [...out];
}

const named = new Set(sources().flatMap((path) => apiTemplates(readFileSync(path, 'utf8'))));

// --- what the world can be asked for --------------------------------------

const runs = file<Run[]>('runs.json');
const runIds = runs.map((r) => r.run_id);
const loopIds = file<{ loop_id: string }[]>('loops.json').map((l) => l.loop_id);
const epicRefs = file<{ epic_ref: string }[]>('epics.json').map((e) => e.epic_ref);
const nodesOf = (ref: string) => (file<EpicDetail>(`epics-${ref}.json`).snapshot?.nodes ?? []).map((n) => n.id);
const holds = file<Hold[]>('approvals.json').map((h) => h.item_id ?? String(h.issue_num));
const items = [...new Set([...epicRefs.flatMap(nodesOf), ...holds])];
const viewed = new Set(
  file<WorkProjection>('dashboard-work.json').views.flatMap((v) => (v.identity.item_id ? [v.identity.item_id] : []))
);
const profileRoles = file<PromptRole[]>('prompts.json')
  .filter((r) => r.profile_applies)
  .map((r) => r.role);
const flights = runIds.map((id) => file<FlightTimeline>(`runs-${id}-flight.json`));
const e = encodeURIComponent;
const each = <T>(values: T[], url: (value: T) => string) => values.map(url);

/** Every answered template, and every concrete URL the world can be asked for under it. */
const ANSWERED: Record<string, string[]> = {
  '/api/meta': ['/api/meta'],
  '/api/bundle': ['/api/bundle'],
  '/api/health': ['/api/health'],
  '/api/health/env': ['/api/health/env'],
  '/api/readiness': ['/api/readiness'],
  '/api/models': ['/api/models'],
  '/api/auth/session': ['/api/auth/session'],
  '/api/fleet/status': ['/api/fleet/status'],
  '/api/approvals': ['/api/approvals'],
  '/api/dashboard': ['/api/dashboard'],
  '/api/dashboard/work': ['/api/dashboard/work'],
  '/api/config/budget': ['/api/config/budget'],
  '/api/config/effective': ['/api/config/effective'],
  '/api/config/overridable': ['/api/config/overridable'],
  '/api/config/human_review': ['/api/config/human_review'],
  '/api/report/spend': ['/api/report/spend'],
  '/api/report/models': ['/api/report/models?days=30', '/api/report/models?days=7'],
  '/api/report/outcomes': ['/api/report/outcomes?weeks=8'],
  '/api/audit': ['/api/audit', '/api/audit?limit=50'],
  '/api/audit/histogram': ['/api/audit/histogram?since=2026-10-20T00:00:00Z&until=2026-10-21T00:00:00Z&bucket_s=900'],
  '/api/audit/status': ['/api/audit/status'],
  '/api/prs/freshness': ['/api/prs/freshness?limit=200'],
  '/api/loops': ['/api/loops', '/api/loops?status=pursuing'],
  '/api/loops/{}': each(loopIds, (id) => `/api/loops/${e(id)}`),
  '/api/loops/{}/audit': each(loopIds, (id) => `/api/loops/${e(id)}/audit`),
  '/api/loops/{}/iterations': each(loopIds, (id) => `/api/loops/${e(id)}/iterations?limit=200`),
  '/api/run-requests': ['/api/run-requests?limit=20'],
  '/api/dispatch/resolve': ['/api/dispatch/resolve', ...each(holds, (id) => `/api/dispatch/resolve?item=${e(id)}`)],
  '/api/runs': ['/api/runs'],
  '/api/runs/{}': each(runIds, (id) => `/api/runs/${e(id)}`),
  '/api/runs/{}/flight': each(runIds, (id) => `/api/runs/${e(id)}/flight?attempt=1`),
  '/api/runs/{}/audit': each(runIds, (id) => `/api/runs/${e(id)}/audit?after_seq=0&limit=200`),
  '/api/runs/{}/events': each(runIds, (id) => `/api/runs/${e(id)}/events`),
  '/api/runs/{}/nodes/{}/diff': flights
    .filter((f) => f.artifact_node_id !== null)
    .map((f) => `/api/runs/${e(f.run_id)}/nodes/${e(f.artifact_node_id ?? '')}/diff`),
  '/api/epics': ['/api/epics'],
  '/api/epics/{}': each(epicRefs, (ref) => `/api/epics/${e(ref)}`),
  '/api/epics/{}/ticks': each(epicRefs, (ref) => `/api/epics/${e(ref)}/ticks?after=0`),
  '/api/epics/{}/tick-strip': each(epicRefs, (ref) => `/api/epics/${e(ref)}/tick-strip?limit=30`),
  '/api/epics/{}/timeline': each(epicRefs, (ref) => `/api/epics/${e(ref)}/timeline`),
  '/api/epics/{}/stream': each(epicRefs, (ref) => `/api/epics/${e(ref)}/stream`),
  '/api/epics/{}/nodes/{}': epicRefs.flatMap((ref) => each(nodesOf(ref), (id) => `/api/epics/${e(ref)}/nodes/${e(id)}`)),
  '/api/verbs/work/{}': each([...viewed], (id) => `/api/verbs/work/${e(id)}`),
  '/api/work/{}/pipeline': each(items, (id) => `/api/work/${e(id)}/pipeline`),
  '/api/work/{}/evidence': each(items, (id) => `/api/work/${e(id)}/evidence`),
  '/api/work/{}/description': each(items, (id) => `/api/work/${e(id)}/description`),
  '/api/prompts': ['/api/prompts'],
  '/api/prompts/{}': each(
    file<PromptRole[]>('prompts.json').map((r) => r.role),
    (role) => `/api/prompts/${e(role)}`
  ),
  '/api/prompts/{}/versions': each(profileRoles, (role) => `/api/prompts/${e(role)}/versions`),
  '/api/prompts/{}/outcomes': each(profileRoles, (role) => `/api/prompts/${e(role)}/outcomes`)
};

/**
 * Reads the world answers with a deliberate 404, each with the reason the
 * page draws a sentence (or nothing can ask), and the URLs that prove it.
 */
const EMPTY: Record<string, { why: string; urls: string[]; holds: () => boolean }> = {
  // The run page reveals a prompt only when the run's flight recorded one;
  // no world flight does, so the panel says "this run retained no prompt"
  // and offers no reveal — and kew answers this read 404 for such a run.
  '/api/runs/{}/prompt': {
    why: 'no world run retained a compiled prompt',
    urls: each(runIds, (id) => `/api/runs/${e(id)}/prompt`),
    holds: () => flights.every((f) => f.events.every((ev) => ev.facts.artifact_kind !== 'compiled_prompt'))
  },
  // A node no run has touched has no work view: the epic page and the
  // palette say "no work item recorded for this node".
  '/api/verbs/work/{}': {
    why: 'an item nothing has run on has no work view',
    urls: each(
      items.filter((id) => !viewed.has(id)),
      (id) => `/api/verbs/work/${e(id)}`
    ),
    holds: () => items.filter((id) => !viewed.has(id)).every((id) => runs.every((r) => r.item_id !== id))
  },
  // No run request is queued, so no request id exists to ask for.
  '/api/run-requests/{}': {
    why: 'the world holds no run request',
    urls: [],
    holds: () => file<{ requests: unknown[] }>('run-requests.json').requests.length === 0
  }
};

/** Write paths: the shim answers each with the demo's sentence, never a world file. */
const WRITES = new Set([
  '/api/auth/login',
  '/api/fleet/stop',
  '/api/approvals/{}',
  '/api/config/{}',
  '/api/palette/ask',
  '/api/verbs/resume',
  '/api/verbs/queue/{}',
  '/api/verbs/orphans/{}/ack',
  '/api/verbs/work/{}/action'
]);

/** `api.ts` clients no page calls: the run step timeline (`fetchTimeline`) predates the flight recorder. */
const NEVER_READ: Record<string, string> = { '/api/runs/{}/timeline': 'fetchTimeline' };

describe('every /api read the console names', () => {
  it('is answered, deliberately empty, a write, or never read', () => {
    const classified = new Set([...Object.keys(ANSWERED), ...Object.keys(EMPTY), ...WRITES, ...Object.keys(NEVER_READ)]);
    expect([...named].filter((t) => !classified.has(t)).sort()).toEqual([]);
    // And no listed path is stale: each one is still in the source.
    expect([...classified].filter((t) => !named.has(t)).sort()).toEqual([]);
    expect(named.size).toBeGreaterThan(50);
  });

  it.each(Object.entries(ANSWERED))('%s answers from the world', (template, urls) => {
    expect(urls.length, `${template} has no URL to ask`).toBeGreaterThan(0);
    for (const url of urls) {
      const answer = api.answer('GET', url);
      expect(answer.status, `${url}: ${JSON.stringify('body' in answer ? answer.body : '')}`).toBe(200);
      if (!('stream' in answer)) expect(answer.body, url).not.toBeNull();
    }
  });

  it.each(Object.entries(EMPTY))('%s is deliberately empty', (_template, { why, urls, holds }) => {
    expect(holds(), why).toBe(true);
    for (const url of urls) expect(api.answer('GET', url).status, url).toBe(404);
  });

  it.each(Object.entries(NEVER_READ))('%s is never read: no page calls %s', (_template, client) => {
    const callers = sources().filter((path) => !path.endsWith(join('lib', 'api.ts')) && code(readFileSync(path, 'utf8')).includes(client));
    expect(callers).toEqual([]);
  });

  it('gives every run row its detail, flight and ledger page', () => {
    expect(runIds.length).toBeGreaterThanOrEqual(20);
    for (const id of runIds) {
      for (const read of ['', '-flight', '-audit']) expect(Object.hasOwn(set.files, `runs-${id}${read}.json`), `${id}${read}`).toBe(true);
    }
  });
});
