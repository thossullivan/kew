/**
 * FUI-22 — the Monitor readout row, decided in one pure place.
 *
 * Three things are pinned here.
 *
 * 1. **The money rule (N5).** The gauge and the figure read `agentic`, the
 *    portion the governor actually enforces, never `spent`. A fixture where
 *    the two are equal cannot tell them apart, so the cases below deliberately
 *    separate them.
 * 2. **A missing reading is not a zero.** Every source can fail on its own, and
 *    a failed one prints an em dash and says why — the same discipline the
 *    factory header applies to its two counters.
 * 3. **The artboard's own strings.** The last block reads DMonitor.html and the
 *    committed fixtures and asserts that this module reproduces the readouts
 *    the artboard draws. That is the visual check's arithmetic, run in CI,
 *    where `npm run shoot -- /monitor DMonitor` is a human comparison nothing
 *    in CI performs.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import type { BudgetMap, Dashboard, DashboardRow, SpendCell, SpendWindows } from '$lib/api';
import { HOLD_SPECS } from '$lib/monitor/hold-reasons';
import { UNREADABLE } from '$lib/shell/header-state';
import {
  captionText,
  DAILY_CAP_FIELD,
  isUnavailable,
  needsYouReadout,
  priorCap,
  QUEUE_UNREAD,
  RUNS_UNREAD,
  spendReadout,
  unreadable,
  WEEKLY_CAP_FIELD,
  workingReadout,
  type StageRun
} from './readouts';

import budgetJson from '../../../fixtures/factory/config-budget.json';
import dashboardJson from '../../../fixtures/factory/dashboard.json';
import spendJson from '../../../fixtures/factory/report-spend.json';

const cell = (over: Partial<SpendCell> = {}): SpendCell => ({
  window_days: 1,
  spent: 200,
  cap: 250,
  agentic: 100,
  interactive: 100,
  ...over
});

// The served shape (FUI-13): `/api/runs` puts a `stage` on every run, and
// sends "unknown" for a kind it has no stage word for. The helper defaults to
// that rather than omitting the field, so these tests see what serve sends.
const run = (over: Partial<StageRun> = {}): StageRun => ({
  status: 'running',
  started_at: '2026-09-24T15:00:00+00:00',
  stage: 'unknown',
  ...over
});

const row = (over: Partial<DashboardRow> = {}): DashboardRow => ({
  type: 'pr_review',
  consequence: 'review',
  age: '2026-09-24T15:46:58+00:00',
  reason: 'merge decision on PR #2059',
  primary_action: 'open',
  issue_num: 2055,
  item_id: '2055',
  estimate_usd: null,
  epic_ref: 'launch-fixes',
  url: 'https://github.com/thossullivan/kew/pull/2059',
  pr_number: 2059,
  ...over
});

const dash = (over: Partial<Dashboard> = {}): Dashboard => ({
  banner: { fleet_stopped: false, no_live_driver: false },
  rows: [row()],
  glance: {
    fleet: null,
    caps: null,
    routing: [],
    ready_queue: [],
    queue_candidates: [],
    recent_completions: []
  },
  as_of: {},
  unavailable: [],
  ...over
});

describe('the spend readouts', () => {
  it('gauges the enforced portion — agentic, never spent (N5)', () => {
    const r = spendReadout('Today', cell({ spent: 200, agentic: 100, cap: 250 }));
    expect(r.gauge?.spent).toBe(100);
    expect(r.figure).toBe('$100');
    // 100/250, not 200/250: reading `spent` would print "80.0% of cap".
    expect(r.meta).toBe('40.0% of cap');
  });

  it('says "no cap" rather than inventing a scale, and draws no track', () => {
    const r = spendReadout('Today', cell({ cap: null, agentic: 42.5 }));
    expect(r.figure).toBe('$42.50');
    expect(r.capLabel).toBe('no cap');
    expect(r.meta).toBe('no cap');
    expect(r.gauge).toEqual({ spent: 42.5, cap: null, marker: null });
  });

  it('keeps the true overrun in words when the fill clamps', () => {
    const r = spendReadout('Today', cell({ agentic: 500, cap: 250 }));
    expect(r.meta).toBe('×2.0 over cap');
  });

  it('shows an em dash and the reason when the source could not be read', () => {
    const r = spendReadout('This week', unreadable('spend', new Error('/api/report/spend -> 503')));
    expect(r.figure).toBe(UNREADABLE);
    expect(r.capLabel).toBe('');
    expect(r.meta).toBe('');
    expect(captionText(r.caption)).toBe('spend unavailable (HTTP 503)');
    expect(r.gauge).toBeNull();
  });

  it('names an unreachable source without a status code', () => {
    expect(unreadable('budget', new TypeError('Failed to fetch')).reason).toBe(
      'budget unavailable'
    );
  });
});

describe('the prior-cap marker', () => {
  const budget: BudgetMap = {
    [DAILY_CAP_FIELD]: { effective: 250, file_value: 250, override: null },
    [WEEKLY_CAP_FIELD]: {
      effective: 1500,
      file_value: 600,
      override: {
        value: 1500,
        set_by: 'operator',
        set_at: '2026-09-24T15:23:40+00:00',
        reason: 'weekly cap reached at $600'
      }
    }
  };

  it('marks the superseded file cap when an override is in force', () => {
    expect(priorCap(budget, WEEKLY_CAP_FIELD)).toEqual({ value: 600, label: 'prior cap' });
  });

  it('marks nothing when the cap is exactly what the file says', () => {
    expect(priorCap(budget, DAILY_CAP_FIELD)).toBeNull();
  });

  it('marks nothing when the budget map itself could not be read', () => {
    expect(priorCap(unreadable('budget', null), WEEKLY_CAP_FIELD)).toBeNull();
    expect(priorCap({}, WEEKLY_CAP_FIELD)).toBeNull();
  });

  it('hands the marker to the gauge, true value and all', () => {
    const r = spendReadout('This week', cell({ agentic: 915.05, cap: 1500 }), priorCap(budget, WEEKLY_CAP_FIELD));
    expect(r.gauge?.marker).toEqual({ value: 600, label: 'prior cap' });
  });
});

describe('the working readout', () => {
  it('counts what the header counts, and names the stages under it', () => {
    const runs = [run({ stage: 'implement' }), run({ stage: 'review round 2' })];
    const r = workingReadout(runs);
    expect(r.figure).toBe('2');
    expect(r.tone).toBe('working');
    expect(captionText(r.caption)).toBe('one reviewer, one implementer');
  });

  it('leaves a queued run out of both the figure and the caption', () => {
    const runs = [
      run({ stage: 'implement' }),
      run({ status: 'pending', started_at: null, stage: 'implement' })
    ];
    const r = workingReadout(runs);
    expect(r.figure).toBe('1');
    expect(captionText(r.caption)).toBe('one implementer');
  });

  it('says the stage is not reported rather than guessing one (FUI-13)', () => {
    // Serve's "unknown" is a real answer for a kind it has no word for, and
    // "unknown" is not a stage: "two in unknown" would name one kew lacks.
    const r = workingReadout([run(), run()]);
    expect(r.figure).toBe('2');
    expect(captionText(r.caption)).toBe('stage not reported');
  });

  it('reads an older serve that sends no stage at all the same way', () => {
    const r = workingReadout([run({ stage: undefined }), run({ stage: null })]);
    expect(r.figure).toBe('2');
    expect(captionText(r.caption)).toBe('stage not reported');
  });

  it('counts a served "unknown" beside the stages that do have a word', () => {
    const r = workingReadout([run({ stage: 'review' }), run(), run()]);
    expect(captionText(r.caption)).toBe('one reviewer, two not reported');
  });

  it('accounts for every working run when only some report a stage', () => {
    const r = workingReadout([run({ stage: 'review' }), run()]);
    expect(captionText(r.caption)).toBe('one reviewer, one not reported');
  });

  it('pluralises, and falls back to the stage word kew has not named', () => {
    const runs = [run({ stage: 'review' }), run({ stage: 'review' }), run({ stage: 'publish' })];
    expect(captionText(workingReadout(runs).caption)).toBe(
      'two reviewers, one in publish'
    );
  });

  it('says nothing at all when nothing is working', () => {
    const r = workingReadout([]);
    expect(r.figure).toBe('0');
    expect(r.caption).toEqual([]);
  });

  it('shows an em dash and the reason when /api/runs was never read', () => {
    const r = workingReadout(unreadable('runs', null));
    expect(r.figure).toBe(UNREADABLE);
    expect(captionText(r.caption)).toBe(RUNS_UNREAD);
  });
});

describe('the needs-you readout', () => {
  it('links the top judgment row inside the sentence, into the app', () => {
    const r = needsYouReadout(dash());
    expect(r.figure).toBe('1');
    expect(r.tone).toBe('attention');
    // The link is the Work row that can act on the item (#2245), not the pull
    // request on GitHub: a click out of the console decides nothing.
    expect(r.caption).toEqual([
      { text: 'merge decision on ', href: null },
      { text: 'PR #2059', href: '/work?item=2055' }
    ]);
  });

  it('says a hold in the server sentence, never the reason string the driver wrote', () => {
    const board = dash({
      rows: [
        row({
          type: 'budget_hold',
          consequence: 'money',
          reason: 'review_rounds_exhausted',
          label: 'the review round cap was reached',
          sentence: 'the review round cap was reached',
          url: null,
          pr_number: null,
          issue_num: 2101,
          item_id: '2101'
        })
      ]
    });
    const caption = captionText(needsYouReadout(board).caption);
    expect(caption).not.toContain('review_rounds_exhausted');
    expect(caption).toContain('review round cap');
  });

  it('prints the server sentence verbatim, over one row per prefix the table knows', () => {
    // #2539: the caption composes nothing. The guard that no hold prefix is
    // printed raw now lives where the sentence is written — the server
    // (`test_the_server_sentence_never_prints_a_hold_prefix_raw` in
    // kew/tests/test_hold_category_parity.py, over this same table).
    for (const spec of HOLD_SPECS) {
      const sentence = `${spec.label}: what it carries`;
      const board = dash({
        rows: [
          row({
            type: 'budget_hold',
            consequence: 'money',
            reason: `${spec.prefix}: what it carries`,
            sentence,
            url: null,
            pr_number: null
          })
        ]
      });
      const caption = captionText(needsYouReadout(board).caption);
      expect(caption, spec.prefix).toBe(sentence);
    }
  });

  it('says how many rows the top one is standing in front of', () => {
    const rows = [row(), row({ item_id: '2101', issue_num: 2101 }), row({ item_id: '2102' })];
    const r = needsYouReadout(dash({ rows }));
    expect(r.figure).toBe('3');
    expect(captionText(r.caption)).toBe('merge decision on PR #2059 and 2 more');
    // The count is the queue's, and the link still goes to the top row's item.
    expect(r.caption.map((s) => s.href)).toEqual([null, '/work?item=2055', null]);
  });

  it('counts "and N more" over the set the figure counts, not the rows it can order', () => {
    // A serve that grows a row type (the DTO is defined to): the figure counts
    // every row served (`headerCounts`, #2207) while `orderRows` drops the type
    // this client has no word for. Counting the rows in hand here would put two
    // queue sizes in one cell — "3" over "and 1 more".
    const rows = [row(), row({ item_id: '2101', issue_num: 2101 }), row({ type: 'moon_phase' })];
    const r = needsYouReadout(dash({ rows }));
    expect(r.figure).toBe('3');
    expect(captionText(r.caption)).toBe('merge decision on PR #2059 and 2 more');
  });

  it('keeps the reference an entry with no work item carries', () => {
    // An orphan PR names no item, so `/work?item=` can select nothing — and
    // dropping to no link would take away the one reference the row has. The
    // fallbacks are `entryHref`'s (work/list.ts): the artifact, then the epic.
    const orphan = row({
      type: 'orphan_pr',
      consequence: 'hygiene',
      reason: 'PR #77 has no issue',
      item_id: null,
      issue_num: 0,
      pr_number: 77,
      url: 'https://github.com/thossullivan/kew/pull/77'
    });
    expect(needsYouReadout(dash({ rows: [orphan] })).caption).toEqual([
      { text: 'PR #77', href: 'https://github.com/thossullivan/kew/pull/77' },
      { text: ' has no issue', href: null }
    ]);
  });

  it('never links a served url safeHref refuses, falling to the epic instead', () => {
    const orphan = row({
      type: 'orphan_pr',
      consequence: 'hygiene',
      reason: 'PR #77 has no issue',
      item_id: null,
      issue_num: 0,
      pr_number: 77,
      url: 'javascript:alert(document.domain)'
    });
    const caption = needsYouReadout(dash({ rows: [orphan] })).caption;
    expect(JSON.stringify(caption)).not.toContain('javascript:');
    expect(caption).toEqual([
      { text: 'PR #77', href: '/epics/launch-fixes' },
      { text: ' has no issue', href: null }
    ]);
  });

  it('sends an entry with neither an item nor an artifact to its epic', () => {
    const stalled = row({
      type: 'stalled_epic',
      consequence: 'epic',
      reason: 'driver gone — launch-fixes — no tick in 6d 8h',
      item_id: null,
      issue_num: null,
      pr_number: null,
      url: null,
      epic_ref: 'launch-fixes'
    });
    expect(needsYouReadout(dash({ rows: [stalled] })).caption).toEqual([
      { text: 'driver gone — launch-fixes — no tick in 6d 8h', href: '/epics/launch-fixes' }
    ]);
  });

  it('shows the oldest most-consequential row, not the first one served', () => {
    const board = dash({
      rows: [
        row(),
        row({
          type: 'budget_hold',
          consequence: 'money',
          reason: 'estimate over cap on #2101',
          issue_num: 2101,
          item_id: '2101',
          url: null,
          pr_number: null
        })
      ]
    });
    const r = needsYouReadout(board);
    expect(r.figure).toBe('2');
    expect(captionText(r.caption)).toBe('estimate over cap on #2101 and 1 more');
    expect(r.caption[0].href).toBeNull();
  });

  it('links the whole sentence when the ref is not written into it', () => {
    const board = dash({ rows: [row({ reason: 'a merge decision is waiting' })] });
    const r = needsYouReadout(board);
    expect(r.caption).toEqual([{ text: 'a merge decision is waiting', href: '/work?item=2055' }]);
  });

  it('falls back to the row type when the server wrote no reason', () => {
    const board = dash({ rows: [row({ reason: '', url: null })] });
    expect(captionText(needsYouReadout(board).caption)).toBe('PR review');
  });

  it('is a confident zero when the whole queue answered and was empty', () => {
    const board = dash({ rows: [] });
    const r = needsYouReadout(board);
    expect(r.figure).toBe('0');
    expect(r.caption).toEqual([]);
  });

  it('names the sources a fail-soft payload lost rather than printing a count', () => {
    const board = dash({ rows: [], unavailable: ['work_rows', 'caps'] });
    const r = needsYouReadout(board);
    expect(r.figure).toBe(UNREADABLE);
    // `caps` is a glance source; it cannot produce a judgment row, so it is
    // not why the count is unknown and must not be named as if it were.
    expect(captionText(r.caption)).toBe('work_rows unavailable');
  });

  it('shows an em dash and the reason when /api/dashboard was never read', () => {
    const r = needsYouReadout(unreadable('dashboard', null));
    expect(r.figure).toBe(UNREADABLE);
    expect(captionText(r.caption)).toBe(QUEUE_UNREAD);
  });
});

describe('a source', () => {
  it('is only unavailable when it says so', () => {
    expect(isUnavailable(unreadable('spend', null))).toBe(true);
    expect(isUnavailable<SpendWindows>(spendJson)).toBe(false);
    expect(isUnavailable<BudgetMap>({})).toBe(false);
  });
});

describe('against the DMonitor artboard (x 40–1400, y ≈88–258)', () => {
  const artboard = readFileSync(
    new URL('../../../design/factory-ui/DMonitor.html', import.meta.url),
    'utf8'
  );
  const readouts = artboard.match(/<section aria-label="Readouts"[\s\S]*?<\/section>/)![0];
  const page = readFileSync(
    new URL('../../routes/(factory)/monitor/+page.svelte', import.meta.url),
    'utf8'
  );
  const spend: SpendWindows = spendJson;
  const budget: BudgetMap = budgetJson;
  const board: Dashboard = dashboardJson;

  it('draws four cells in the ratio 1 / 1 / 1.35 / 1.35 between two hairlines', () => {
    expect(readouts).toContain('grid-template-columns: 1fr 1fr 1.35fr 1.35fr');
    expect(readouts).toContain('border-top: 1px solid #221c33');
    expect(readouts).toContain('border-bottom: 1px solid #221c33');
    // The same ratio and the same two hairlines, as the page carries them —
    // from 1024 up, which is the width the artboard was drawn at. Below it the
    // four cells fold to 2×2 (#2282 D3) and the ratio has nothing to say.
    expect(page).toContain(
      'grid grid-cols-2 border-y border-hair lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1.35fr)_minmax(0,1.35fr)]'
    );
  });

  it("gives a marked gauge the artboard's headroom, and an unmarked one 8px", () => {
    // A marker labels itself above the track (GAUGE.markerLabelTop is -24), so
    // This week is dropped 26px where Today needs only 8.
    expect(readouts).toContain('height: 40px; margin-top: 8px');
    expect(readouts).toContain('height: 40px; margin-top: 26px');
    expect(page).toContain("margin-top={cell.gauge.marker ? '26px' : '8px'}");
  });

  it('trails the figure with a 15px cap in the label colour, 10px clear', () => {
    expect(readouts).toContain('font-size: 15px; color: #8d84a3; font-weight: 400; margin-left: 10px;');
    // ml-2.5 = 10px; text-15 = 15px; text-text-3 is the #8d84a3 label colour.
    expect(page).toContain('ml-2.5 hidden text-15 font-normal text-text-3 lg:inline');
  });

  it('reproduces the Today figures the artboard prints', () => {
    const r = spendReadout('Today', spend.daily, priorCap(budget, DAILY_CAP_FIELD));
    expect(readouts).toContain(`>${r.figure}<`);
    expect(readouts).toContain(`/ ${r.capLabel}<`);
    expect(readouts).toContain(`>${r.meta}<`);
    expect(r.gauge?.marker).toBeNull();
  });

  it('reproduces the This week figures, prior cap and all', () => {
    const r = spendReadout('This week', spend.weekly, priorCap(budget, WEEKLY_CAP_FIELD));
    expect(readouts).toContain(`>${r.figure}<`);
    expect(readouts).toContain(`/ ${r.capLabel}<`);
    expect(readouts).toContain(`>${r.meta}<`);
    expect(readouts).toContain('prior cap $600');
    expect(r.gauge?.marker).toEqual({ value: 600, label: 'prior cap' });
  });

  it('reproduces the needs-you caption, with the artboard linking the same ref', () => {
    const r = needsYouReadout(board);
    // Two entries in the committed set: the merge decision the artboard draws,
    // and the go-live round-cap hold that carries the one command `/work` has
    // a Do this block to print. The caption describes the top row and counts
    // the rest, which is what "and N more" is for.
    expect(r.figure).toBe(String(board.rows.length));
    // The server's sentence for the merge (#2539), the same on every surface —
    // led by the merge verify that failed at its head (#3460).
    expect(captionText(r.caption)).toBe(
      "Merge verify failed at 3670e43f: 2 of main's tests fail on this PR — the merge decision is yours and 1 more"
    );
    // The artboard predates #2539 and words the caption its own way
    // ("merge decision on PR #2059"); the ref it links is still the same one.
    expect(readouts).toContain('>PR #2059</a>');
  });
});

describe('the needs-you summary by kind (#3052 N3)', () => {
  it('says the committed queue as its kinds, the first linked to the top row', () => {
    const r = needsYouReadout(dashboardJson as unknown as Dashboard);
    expect(r.summary).toEqual([
      { text: '1 merge decision', href: '/work?item=2055' },
      { text: ' · ', href: null },
      { text: '1 hold', href: null }
    ]);
  });

  it('counts each kind and names no more than two of them', () => {
    const rows = [
      row(),
      row({ item_id: '2056', issue_num: 2056 }),
      row({ type: 'epic_wait', consequence: 'review', item_id: '2057', issue_num: 2057 }),
      row({ type: 'orphan_pr', item_id: '', issue_num: null })
    ];
    const r = needsYouReadout(dash({ rows }));
    expect(captionText(r.summary ?? [])).toBe('2 merge decisions · 1 hold · 1 more');
  });

  it('adds up to the figure when the client has no word for a served type', () => {
    const r = needsYouReadout(dash({ rows: [row(), row({ type: 'from_a_newer_serve' })] }));
    expect(r.figure).toBe('2');
    expect(captionText(r.summary ?? [])).toBe('1 merge decision · 1 more');
  });

  it('says nothing for an empty queue or a source it could not read', () => {
    expect(needsYouReadout(dash({ rows: [] })).summary).toEqual([]);
    expect(needsYouReadout(unreadable('dashboard', null)).summary).toBeUndefined();
  });
});
