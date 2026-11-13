/**
 * FUI-23 — the Monitor's live-runs table (DMonitor y ≈294–440).
 *
 * Six things are pinned here.
 *
 * 1. **Only runs that are actually working are listed.** The filter is the
 *    factory header's own `isWorkingRun`, so the table can never disagree with
 *    the "2 working" figure above it — a queued `pending` run is not drawn, and
 *    neither is a finished one.
 * 2. **Elapsed is `epic/format.elapsed` over `started_at`, and "start not
 *    recorded" otherwise.** A run with no start time has no duration; inventing one from
 *    `duration_seconds` (0 while a run is live) would draw "0s" on a run that
 *    has been going for an hour.
 * 3. **The stage word is the view's, the rest of the line is the payload's.**
 *    `review` is captioned "governed review" because that is what kew's review
 *    stage IS; `unknown` — a real answer from FUI-13, not a missing field —
 *    draws no stage word, and keeps the detail line that names the recorded
 *    kind, worded, so an operator still has something to look up. No cell is
 *    a bare dash (#2847 P11).
 * 4. **Posture is worded, not reworded.** "sandboxed" and "host worktree" are
 *    the two DMonitor draws; a token this table has no phrase for is printed as
 *    recorded rather than mapped to a friendlier guess.
 * 5. **A read that did not happen is not an empty table.** "nothing running"
 *    is a claim about the fleet, so it is only made when the runs payload was
 *    actually read.
 * 6. **The Item cell names an item or says it cannot.** A run DTO carries
 *    `issue_num` alone, so a run on a non-GitHub item arrives as the `0`
 *    sentinel; the row is still drawn, but "#0" and `/work?item=0` — a
 *    reference to no item and a selection the Work screen cannot key — are
 *    not.
 *
 * The component itself is rendered (SSR) for its markup. Svelte effects need a
 * client runtime this suite does not have, so the 1s clock is pinned by reading
 * the source, with everything it feeds tested above.
 */
import { readFileSync } from 'node:fs';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { Run } from '$lib/api';
import { elapsed } from '$lib/epic/format';
import { RUNS_UNREAD } from '$lib/monitor/readouts';
import {
  liveRunRows,
  liveRunsView,
  LIVE_RUNS_COLUMNS,
  LIVE_RUNS_EMPTY,
  LIVE_RUN_STATE,
  NO_ITEM,
  postureWord,
  RUNNER_NOT_RECORDED,
  runElapsed,
  runItemKey,
  STAGE_NOT_RECORDED,
  START_NOT_RECORDED,
  stageLine,
  stageWord,
  workHref
} from './live-runs';
import LiveRuns from './LiveRuns.svelte';

import { FIXTURE_EPOCH } from '../../../fixtures/plugin';
import runsJson from '../../../fixtures/factory/runs.json';

const FIXTURE = runsJson as unknown as Run[];

/**
 * The clock the fixture surface is really read against, as a fixed `nowMs`.
 *
 * NOT a date picked to suit the rows. Fixture mode shifts every instant it
 * serves by one delta — `now - FIXTURE_EPOCH` — so a run served at wall clock
 * `t` is `t - (FIXTURE_EPOCH - started_at)` old, which is the age the COMMITTED
 * instant has at `FIXTURE_EPOCH`. Reading the committed payload at the epoch is
 * therefore the same arithmetic the browser does, on every day it is opened.
 *
 * A hand-chosen `nowMs` is not: the previous one sat nine days behind the
 * epoch, a clock the serving path can never produce, and under it two runs the
 * dev server draws as "8d23h" — past the host's own
 * `PENDING_RUN_MAX_AGE_S`, so rows kew reads as abandoned rather than working —
 * asserted as "14m" and "30m" (reviewer finding, PR #2441).
 *
 * Every other use of it below is relative (`NOW - ms`), so the value itself
 * only matters to the fixture rows.
 */
const NOW = Date.parse(FIXTURE_EPOCH);

function run(over: Partial<Run> = {}): Run {
  return {
    ...FIXTURE[0],
    run_id: 'r1',
    issue_num: 2055,
    status: 'running',
    started_at: null,
    finished_at: null,
    ...over
  } as Run;
}

describe('only the runs that are working', () => {
  it('draws a pending run that has begun, and never a queued one', () => {
    // `displayStatus`'s own distinction: a `pending` run with a start time is
    // running, one without it is queued for the dispatcher.
    const rows = liveRunRows(
      [
        run({ run_id: 'begun', status: 'pending', started_at: '2026-09-24T15:46:15+00:00' }),
        run({ run_id: 'queued', status: 'pending', started_at: null })
      ],
      NOW
    );
    expect(rows.map((r) => r.key)).toEqual(['begun']);
  });

  it('draws no run that has finished, whatever it finished as', () => {
    const over = ['success', 'failure', 'refused', 'killed', 'budget_exceeded', 'budget_stopped'];
    const rows = liveRunRows(
      over.map((status, i) => run({ run_id: `r${i}`, status })),
      NOW
    );
    expect(rows).toEqual([]);
  });

  it('keeps the payload order, which is the newest run first', () => {
    const rows = liveRunRows(
      [run({ run_id: 'a', issue_num: 2055 }), run({ run_id: 'b', issue_num: 2033 })],
      NOW
    );
    expect(rows.map((r) => r.ref)).toEqual(['#2055', '#2033']);
  });

  it('gives every drawn row the state word its own filter asserted', () => {
    // A row is in this table because `isWorkingRun` said it is working, so the
    // State cell is that same fact rather than a second derivation of it. #2050
    // serves the word and FUI-29 reads it from there.
    const rows = liveRunRows([run({ status: 'running' })], NOW);
    expect(rows.map((r) => r.state)).toEqual([LIVE_RUN_STATE]);
    expect(LIVE_RUN_STATE).toBe('working');
  });
});

describe('elapsed', () => {
  it('formats the time since started_at with epic/format.elapsed', () => {
    for (const ms of [45_000, 47 * 60_000, (2 * 60 + 13) * 60_000, 76 * 3600_000]) {
      const startedAt = new Date(NOW - ms).toISOString();
      expect(runElapsed(startedAt, NOW), String(ms)).toBe(elapsed(ms));
    }
    expect(runElapsed(new Date(NOW - 45_000).toISOString(), NOW)).toBe('45s');
    expect(runElapsed(new Date(NOW - 47 * 60_000).toISOString(), NOW)).toBe('47m');
    expect(runElapsed(new Date(NOW - (2 * 60 + 13) * 60_000).toISOString(), NOW)).toBe('2h13m');
  });

  it('says the start is not recorded when there is none, never a zero duration', () => {
    // `duration_seconds` is 0 on a live run, so a fallback to it would draw
    // "0s" on a run that has been going for an hour.
    expect(runElapsed(null, NOW)).toBe(START_NOT_RECORDED);
    expect(runElapsed('', NOW)).toBe(START_NOT_RECORDED);
    expect(runElapsed(undefined, NOW)).toBe(START_NOT_RECORDED);
    expect(runElapsed('not a timestamp', NOW)).toBe(START_NOT_RECORDED);
  });

  it('never renders a negative duration when the clock skews', () => {
    expect(runElapsed(new Date(NOW + 30_000).toISOString(), NOW)).toBe('0s');
  });

  it('ticks on the clock it is given, not on the runs poll', () => {
    const startedAt = '2026-09-24T15:46:00+00:00';
    const at = (nowMs: number) => liveRunRows([run({ started_at: startedAt })], nowMs)[0].elapsed;
    expect(at(Date.parse(startedAt) + 5_000)).toBe('5s');
    expect(at(Date.parse(startedAt) + 6_000)).toBe('6s');
  });
});

describe('stage', () => {
  it('captions the review stage "governed review"', () => {
    const rows = liveRunRows([run({ stage: 'review', stage_detail: 'round 1 · PR #2059' })], NOW);
    expect(rows[0].stage).toBe('governed review');
    expect(rows[0].stageDetail).toBe('round 1 · PR #2059');
    expect(stageLine(rows[0])).toBe('governed review · round 1 · PR #2059');
  });

  it('names implement, publish and the CI fix as kew names them', () => {
    expect(stageWord('implement')).toBe('implement');
    expect(stageWord('publish')).toBe('publish');
    expect(stageWord('ci_fix')).toBe('CI fix');
  });

  it('drops an unknown stage word, keeping the kind it names in words', () => {
    // FUI-13's `unknown` is an answer — a run kind kew has no stage word
    // for — so the cell says nothing rather than naming the wrong stage. The
    // serve puts the recorded kind in the detail line precisely so the row is
    // still traceable; that is not dropped with the stage word, and it is
    // worded (#2847 P11: the live table printed "— · review_stage").
    const rows = liveRunRows([run({ stage: 'unknown', stage_detail: 'resolve' })], NOW);
    expect(rows[0].stage).toBe('');
    expect(rows[0].stageDetail).toBe('resolve');
    expect(stageLine(rows[0])).toBe('resolve');
    const [review] = liveRunRows(
      [run({ stage: 'unknown', stage_detail: 'review_stage · PR #2059' })],
      NOW
    );
    expect(stageLine(review)).toBe('review stage · PR #2059');
  });

  it('says a stage nobody reported is not recorded', () => {
    // An older serve omits the field altogether; a blank is the same "not
    // reported" the served `unknown` is.
    const rows = liveRunRows(
      [
        run({ stage: '', stage_detail: '' }),
        run({ run_id: 'r2', stage: undefined as unknown as string, stage_detail: '' })
      ],
      NOW
    );
    expect(rows.map((r) => r.stage)).toEqual(['', '']);
    // Nothing qualifying the stage either: the cell says so, not a dash.
    expect(rows.map((r) => stageLine(r))).toEqual([STAGE_NOT_RECORDED, STAGE_NOT_RECORDED]);
  });

  it('names a stage from a newer serve rather than blanking it', () => {
    expect(stageWord('backport')).toBe('backport');
    expect(stageWord('risk_review')).toBe('risk review');
  });

  it('reads neither table through the prototype', () => {
    // Both words come off the wire, and `constructor` survives normalizing:
    // an object-literal lookup answers it with a function, which would be
    // printed into the cell (factory/status.ts's `own` documents the trap).
    expect(stageWord('constructor')).toBe('constructor');
    expect(postureWord('constructor')).toBe('constructor');
    expect(typeof stageWord('valueOf')).toBe('string');
    expect(typeof postureWord('valueOf')).toBe('string');
  });
});

describe('posture', () => {
  it('words the two postures DMonitor draws', () => {
    expect(postureWord('sandboxed')).toBe('sandboxed');
    expect(postureWord('host')).toBe('host worktree');
    // The ledger token for a run the operator took outside containment
    // (`sandbox.POSTURE_UNCONTAINED`). Same boundary, same words.
    expect(postureWord('uncontained')).toBe('host worktree');
  });

  it('says not recorded when no posture was attested', () => {
    // `SandboxPostureDTO`'s own default, and the "pending" it serves for a
    // latest attempt with no posture event. Neither is "unsandboxed".
    expect(postureWord('not recorded')).toBe('not recorded');
    expect(postureWord('pending')).toBe('not recorded');
    expect(postureWord('')).toBe('not recorded');
    expect(postureWord(undefined)).toBe('not recorded');
  });

  it('prints a posture it has no phrase for as recorded', () => {
    // `native` (contained by the runner itself) and `refused` are real tokens
    // with no artboard wording. Mapping them onto one of the two above would
    // claim a boundary kew did not record.
    expect(postureWord('native')).toBe('native');
    expect(postureWord('refused')).toBe('refused');
    expect(postureWord('uncontained_flow')).toBe('uncontained flow');
  });
});

describe('the item link', () => {
  it('links the row to the item on the Work screen', () => {
    const rows = liveRunRows([run({ issue_num: 2055 })], NOW);
    expect(rows[0].ref).toBe('#2055');
    expect(rows[0].itemId).toBe('2055');
    expect(rows[0].href).toBe('/work?item=2055');
  });

  it('escapes the item it names, which comes off the wire', () => {
    expect(workHref('SFD-12')).toBe('/work?item=SFD-12');
    expect(workHref('a b&c')).toBe('/work?item=a%20b%26c');
  });

  it('names no item for the 0 sentinel rather than fabricating #0', () => {
    // A run DTO carries `issue_num` alone, so a live run on a non-GitHub
    // (Linear) item is served as 0. "#0" is a reference to an item no tracker
    // has, and `/work?item=0` is a row the Work screen does not key: the cell
    // says there is no item, in words rather than a bare dash (#2847 P11).
    const [row] = liveRunRows([run({ issue_num: 0 })], NOW);
    expect(row.ref).toBe(NO_ITEM);
    expect(row.itemId).toBe('');
    expect(row.href).toBe('');
  });

  it('still lists the run it cannot name, with every other cell read', () => {
    // The row is what the table is for — something IS executing. Only the one
    // cell the payload cannot fill goes quiet.
    const [row] = liveRunRows(
      [run({ issue_num: 0, stage: 'implement', model: 'claude-opus-5', posture: 'sandboxed' })],
      NOW
    );
    expect(row.stage).toBe('implement');
    expect(row.runner).toBe('Opus 5');
    expect(row.runnerId).toBe('claude-opus-5');
    expect(row.posture).toBe('sandboxed');
    expect(row.state).toBe(LIVE_RUN_STATE);
  });

  it('keys a positive issue number and nothing else', () => {
    expect(runItemKey(2055)).toBe('2055');
    expect(runItemKey(0)).toBe('');
    expect(runItemKey(-1)).toBe('');
    expect(runItemKey(null)).toBe('');
    expect(runItemKey(undefined)).toBe('');
    expect(runItemKey(Number.NaN)).toBe('');
  });
});

describe('the runner cell', () => {
  it('names the model, which is what identifies the agent at work', () => {
    // By the catalog's display name (#2541), with the id kept for the `title`.
    const [row] = liveRunRows([run({ model: 'claude-opus-5' })], NOW);
    expect(row.runner).toBe('Opus 5');
    expect(row.runnerId).toBe('claude-opus-5');
    // An id the catalog does not carry prints as itself.
    expect(liveRunRows([run({ model: 'acme-llm-9' })], NOW)[0].runner).toBe('acme-llm-9');
  });

  it('falls back to the route name, then says the runner is not recorded', () => {
    expect(liveRunRows([run({ model: '', runner: 'claude-code' })], NOW)[0].runner).toBe(
      'claude-code'
    );
    expect(liveRunRows([run({ model: '', runner: '' })], NOW)[0].runner).toBe(RUNNER_NOT_RECORDED);
    expect(liveRunRows([run({ model: '', runner: '' })], NOW)[0].runnerId).toBe('');
  });
});

describe('the view', () => {
  it('counts the drawn rows in the section head', () => {
    const view = liveRunsView([run({ run_id: 'a' }), run({ run_id: 'b' })], NOW);
    expect(view.meta).toBe('2 working');
    expect(view.notice).toBe('');
  });

  it('says nothing running only when the payload was read', () => {
    const read = liveRunsView([], NOW);
    expect(read.rows).toEqual([]);
    expect(read.meta).toBe('');
    expect(read.notice).toBe(LIVE_RUNS_EMPTY);
  });

  it('names the unread source instead of claiming a quiet fleet', () => {
    const unread = liveRunsView({ available: false, reason: RUNS_UNREAD }, NOW);
    expect(unread.rows).toEqual([]);
    expect(unread.meta).toBe('');
    expect(unread.notice).toBe(RUNS_UNREAD);
    expect(unread.notice).not.toBe(LIVE_RUNS_EMPTY);
  });
});

describe('DMonitor, from the committed fixtures', () => {
  const rows = liveRunRows(FIXTURE, NOW);

  it('draws the one live run and not #2055\'s returned review round', () => {
    // DMonitor drew #2055's governed review as a second live row while the
    // same fixture served #2055 as the merge needs-you — a pairing the server
    // never serves, since an item with a run executing on it leaves the set
    // (#2842). The fixture's round 1 has returned, which is WHY the merge is
    // the operator's, so its run is finished and only #2033 is working.
    expect(rows.map((r) => r.ref)).toEqual(['#2033']);
    expect(rows.map((r) => r.href)).toEqual(['/work?item=2033']);
    expect(FIXTURE.find((r) => r.run_id === '9c41e2b7')?.status).toBe('success');
  });

  it('draws the artboard\'s stage, runner and posture cells', () => {
    expect(rows.map((r) => [r.stage, r.stageDetail])).toEqual([['implement', '']]);
    expect(rows.map((r) => r.runner)).toEqual(['Opus 5']);
    expect(rows.map((r) => r.runnerId)).toEqual(['claude-opus-5']);
    expect(rows.map((r) => r.posture)).toEqual(['host worktree']);
  });

  it('draws each elapsed cell from the start time the fixture serves', () => {
    // DMonitor prints "[elapsed]" because a static artboard has no clock, not
    // because the payload has no start time. It used to have none — the fixture
    // served `running` runs with a null `started_at`, which is a row the backend
    // cannot store (`RunRecord.started_at` is required) and which left both
    // cells on the em dash. #2377 gave them the start times the ledger holds,
    // so the cells now draw what a real reading would.
    expect(rows.map((r) => r.elapsed)).toEqual(['30m']);
    for (const row of rows) expect(row.elapsed).not.toBe(START_NOT_RECORDED);
  });

  it('draws a working run, never one the host would call abandoned', () => {
    // The cell above is a MINUTES reading because `NOW` is the serving clock,
    // and that is the only reason it is worth asserting. A start time committed
    // further behind `FIXTURE_EPOCH` than kew's pending bound still passes a
    // hand-picked clock while the dev server draws days — and a pending row
    // that old is one `service/work.py`'s `_abandoned_pending_run` reads as a
    // host that died, not as work in progress. So the bound is asserted on the
    // reading itself: no day or hour component in any live cell.
    // `kew/tests/test_factory_status_parity.py` holds the same rows to
    // `PENDING_RUN_MAX_AGE_S` itself, which is the number this mirrors.
    for (const row of rows) {
      expect(row.elapsed, row.ref).toMatch(/^\d+[ms]$/);
      const started = FIXTURE.find((r) => r.run_id === row.key)!.started_at;
      expect(NOW - Date.parse(started!), row.ref).toBeLessThan(4 * 60 * 60 * 1000);
    }
  });
});

describe('LiveRuns.svelte', () => {
  // SSR rendering is the component harness this suite has: it runs the props,
  // the derived view and the markup for real. Svelte's hydration markers sit
  // between every tag and its text; they are not what these assertions test.
  const html = (runs: Run[] | { available: false; reason: string }) =>
    render(LiveRuns, { props: { runs } }).body.replace(/<!--.*?-->/g, '');

  it('draws the six columns of the artboard\'s grid', () => {
    // Two rows, so the quiet hairline BETWEEN rows is drawn: the fixture has
    // one live run since #2842, and a lone row draws no rule under itself.
    const body = html([
      run({ run_id: 'a', status: 'pending', started_at: new Date(NOW - 60_000).toISOString() }),
      run({ run_id: 'b', status: 'pending', started_at: new Date(NOW - 60_000).toISOString() })
    ]);
    expect(body).toContain(LIVE_RUNS_COLUMNS);
    for (const head of ['Item', 'Stage', 'Runner', 'Posture', 'Elapsed', 'State']) {
      expect(body, head).toContain(`>${head}</span>`);
    }
    // 56px rows over the quiet hairline, under a head on the ordinary one.
    expect(body).toContain('h-14');
    expect(body).toContain('border-hair-quiet');
  });

  it('draws each live run as a row that links to its item', () => {
    const body = html(FIXTURE);
    expect(body).toContain('href="/work?item=2033"');
    expect(body).not.toContain('href="/work?item=2055"');
    expect(body).toContain('implement');
    expect(body).toContain('host worktree');
    expect(body).toContain('1 working');
    expect(body).toContain('>working</span>');
  });

  it('draws an unnamed item as a dead cell, not a link to item zero', () => {
    const body = html([run({ issue_num: 0 })]);
    expect(body).not.toContain('/work?item=0');
    expect(body).not.toContain('#0');
    expect(body).toContain(`>${NO_ITEM}</span>`);
    // The run is still listed — the absence is one cell, not the row.
    expect(body).toContain('1 working');
    expect(body).not.toContain(LIVE_RUNS_EMPTY);
  });

  it('says nothing running on an empty read, and names an unread one', () => {
    expect(html([])).toContain(LIVE_RUNS_EMPTY);
    const unread = html({ available: false, reason: RUNS_UNREAD });
    expect(unread).toContain(RUNS_UNREAD);
    expect(unread).not.toContain(LIVE_RUNS_EMPTY);
  });

  it('runs the elapsed column off a 1s clock of its own', () => {
    // The clock is an effect, which needs a client runtime this suite does not
    // have; what it feeds is `runElapsed`, tested above. The runs themselves
    // ride the shell's shared poll, so this component adds no second fetch.
    const component = readFileSync(new URL('./LiveRuns.svelte', import.meta.url), 'utf8');
    expect(component).toMatch(/setInterval\(\s*\(\) => \{\s*clock = Date\.now\(\);/);
    expect(component).toContain('ELAPSED_TICK_MS');
    expect(component).toContain('clearInterval');
    expect(component).toContain('factoryPulse.runs');
    expect(component).not.toMatch(/await fetch|fetchRuns/);
  });

  it('hangs off the Monitor page, under the readout row', () => {
    const page = readFileSync(
      new URL('../../routes/(factory)/monitor/+page.svelte', import.meta.url),
      'utf8'
    );
    expect(page).toContain('<LiveRuns />');
    expect(page.indexOf('aria-label="Readouts"')).toBeLessThan(page.indexOf('<LiveRuns />'));
    expect(page.indexOf('<LiveRuns />')).toBeLessThan(page.indexOf('<Holds'));
  });
});
