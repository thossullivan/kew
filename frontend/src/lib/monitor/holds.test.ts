/**
 * FUI-24 — the Monitor's "Holds and blocks" column, decided in one pure place.
 *
 * Four things are pinned here.
 *
 * 1. **The artboard's order and its rows.** blocked, then budget, then
 *    approval — and no row kew has no fact for. DMonitor draws a third row,
 *    "self-hosted CI paused until #2033 merges"; nothing in kew records it, so
 *    the last block asserts the committed fixtures produce the two real rows
 *    and never a runners row.
 * 2. **The override sentence is built from the fields**, not from the
 *    operator's free-text `reason` — `file_value`, `value` and `set_by`, with
 *    the direction read off the money rather than assumed.
 * 3. **A source that did not answer is not an empty list.** Each of the three
 *    fails on its own; the count hedges and the column says what it is missing.
 * 4. **A decided row leaves the way it leaves the console**, keyed by
 *    `dashboard.ts`'s own `rowKey`, on the write response — and its dismissal
 *    ends when the read it was bridging lands, so the next hold raised on the
 *    same item is drawn instead of being swallowed by the last decision.
 * 5. **The head counts the rows that were drawn.** Not the composed list: "1
 *    active" over "nothing held or blocked" is the surface contradicting itself.
 * 6. **Both polled readings are re-taken together, and fail apart.** An
 *    override lifted by another operator leaves the column on the next poll;
 *    a budget read that failed costs the overrides and not the blocks.
 * 7. **Which reading stands for which source is `holdSources`**, not a chain
 *    of `??` in the component: the caps come from the poll the moment it
 *    answers, the page's `load()` map is only the seed, and a source nobody
 *    has read is named rather than defaulted to an empty list.
 * 8. **One row per blocked node.** A dependency several epics wait on is drawn
 *    once and counted once — and a boundary node no epic owns is still drawn,
 *    because "somebody else lists it" is not a fact this column has.
 * 9. **A hold row names its epic, its item, its reason in words, how long it
 *    has stood and what clears it** — by its REASON, never by the `budget_hold`
 *    row type every `epic_holds` row wears (#2208). An unknown reason is
 *    `other` with the reason as written, and the queue's approve button is
 *    offered on no hold at all: it releases a GitHub label, not an epic hold.
 *
 * The component itself is rendered (SSR) for the props and the markup. Svelte
 * effects need a client runtime this suite does not have, so the three lines
 * that cannot live in a pure module — the first read, the timer over it and
 * the prune it feeds — are pinned by reading the source, with everything they
 * decide tested above.
 */
import { readFileSync } from 'node:fs';
import type { ComponentProps } from 'svelte';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import { plainCommands } from '$lib/ui/command-markup';
import type {
  BudgetMap,
  Dashboard,
  DashboardRow,
  EpicDetail,
  EpicSnapshot,
  EpicSummary
} from '$lib/api';
import { rowKey } from '$lib/dashboard';
import { ATTENTION_STATE_WORD } from '$lib/work/list';
import { decideRemovesRow, type DecideState, type VerbResult } from '$lib/verbs';
import { captionText } from '$lib/monitor/readouts';
import { HOLD_SPECS } from '$lib/monitor/hold-reasons';
import {
  capWindow,
  composeHolds,
  countedHolds,
  holdKindOf,
  drawnHolds,
  EPICS_UNREAD,
  HELD_MARK,
  HOLD_MARK,
  HOLD_ORDER,
  holdsMeta,
  HOLDS_EMPTY,
  holdSources,
  holdWord,
  overrideCopy,
  pruneRowState,
  readHoldSources,
  visibleHolds,
  type HoldsFetchers,
  type HoldsSources
} from './holds';
import Holds from './Holds.svelte';
import { factoryPulse } from '$lib/shell/pulse.svelte';
import { JUDGMENT_ROW_SOURCES } from '$lib/shell/header-state';

import budgetJson from '../../../fixtures/factory/config-budget.json';
import dashboardJson from '../../../fixtures/factory/dashboard.json';
import epicJson from '../../../fixtures/factory/epics-launch-fixes.json';
import epicsJson from '../../../fixtures/factory/epics.json';

const BUDGET = budgetJson as unknown as BudgetMap;

/**
 * The committed caps map's weekly override is a RAISE — the operator already
 * answered the hold its own sentence names — so #2280 D14 reads it as a
 * CLEARED hold and draws no row for it. This is the same map with that cap
 * LOWERED instead: a restriction still in force, which is the only shape of
 * override this column still lists.
 */
const CAPPED: BudgetMap = {
  ...BUDGET,
  'budget.weekly_usd': {
    effective: 400,
    file_value: 600,
    override: { value: 400, set_by: 'operator', set_at: '2026-09-24T15:23:40+00:00', reason: '' }
  }
};

/** What `CAPPED`'s one row says. */
const CAPPED_SENTENCE = 'weekly cap lowered from $600 to $400 (operator)';
const DASHBOARD = dashboardJson as unknown as Dashboard;
const SNAPSHOT = (epicJson as unknown as EpicDetail).snapshot as EpicSnapshot;

const EMPTY_DASHBOARD = { ...DASHBOARD, rows: [], unavailable: [] } as Dashboard;

function sources(over: Partial<HoldsSources> = {}): HoldsSources {
  return {
    epics: [],
    budget: {},
    dashboard: EMPTY_DASHBOARD,
    ...over
  };
}

function node(over: Record<string, unknown> = {}) {
  return {
    id: '2053',
    title: 'a node',
    status: 'blocked',
    status_since: null,
    url: null,
    pr_url: null,
    external: false,
    stuck: null,
    attention: null,
    blocked_reason: 'needs a design decision',
    ...over
  } as EpicSnapshot['nodes'][number];
}

function snapshot(nodes: EpicSnapshot['nodes'], ref = 'launch-fixes'): EpicSnapshot {
  return {
    epic_ref: ref,
    tick_id: 1,
    tick_ts: '2026-09-24T15:00:00+00:00',
    signal: 'progressing',
    counts: {},
    frontier: [],
    nodes,
    edges: [],
    unavailable: []
  };
}

function row(over: Partial<DashboardRow> = {}): DashboardRow {
  return {
    type: 'approval_hold',
    consequence: 'money',
    age: '2026-09-24T12:00:00+00:00',
    reason: 'approval needed on #77',
    primary_action: 'approve',
    issue_num: 77,
    item_id: '77',
    estimate_usd: 12,
    epic_ref: null,
    url: 'https://example.invalid/77',
    pr_number: null,
    ...over
  };
}

/** An approval hold's sentence, as `kew.service.attention.say_wait` writes it. */
const APPROVAL_SENTENCE =
  'approve a run estimated at $25.00, the per-issue cap; no cost history to estimate from';

/** #114's approval hold: no epic, and a sentence that names no item. */
const APPROVAL_114: DashboardRow = row({
  reason: 'awaiting approval — no history — assuming per-issue cap of $25.00',
  sentence: APPROVAL_SENTENCE,
  issue_num: 114,
  item_id: '114',
  url: null,
  attention_kind: 'approval'
});

/**
 * One `epic_holds` row as the server serves it: type `budget_hold` whatever
 * held the node (`needs_you._budget_rows`), the raw hold reason, and the epic
 * and item it stands on.
 */
function hold(over: Partial<DashboardRow> = {}): DashboardRow {
  return row({
    type: 'budget_hold',
    consequence: 'money',
    reason: 'review_rounds_exhausted',
    // What the server writes for that reason (`attention.say_wait`, #2539).
    sentence: 'the review round cap was reached',
    age: '2026-09-24T12:00:00+00:00',
    issue_num: 2201,
    item_id: '2201',
    epic_ref: 'launch-fixes',
    url: null,
    estimate_usd: null,
    ...over
  });
}

/**
 * One `epic_wait` entry as the server serves it: the item's own reference
 * written onto the front of the reason (`_epic_wait_row` in
 * kew/src/kew/service/needs_you.py), the bucket's label, and the kind of wait
 * it is.
 */
function wait(over: Partial<DashboardRow> = {}): DashboardRow {
  return row({
    type: 'epic_wait',
    consequence: 'flow',
    reason: '#2055 — in_review awaiting merge',
    label: 'in_review awaiting merge',
    attention_kind: 'merge_ready',
    primary_action: 'review',
    age: '2026-09-24T12:00:00+00:00',
    issue_num: 2055,
    item_id: '2055',
    epic_ref: 'launch-fixes',
    url: null,
    estimate_usd: null,
    ...over
  });
}

function withRows(rows: DashboardRow[], unavailable: string[] = []): Dashboard {
  return { ...EMPTY_DASHBOARD, rows, unavailable };
}

/** The one row a dashboard carrying only `row` composes to. */
function setRowOf(row: DashboardRow, nowMs = Date.parse('2026-09-24T14:13:00+00:00')) {
  return composeHolds(sources({ dashboard: withRows([row]) }), nowMs).rows[0];
}

describe('kinds and order', () => {
  it('draws the artboard order: blocked, then budget, then approval', () => {
    const view = composeHolds(
      sources({
        epics: [snapshot([node()])],
        budget: CAPPED,
        dashboard: withRows([
          row(),
          hold({ reason: 'budget_preflight: over the cap', issue_num: 88, item_id: '88' })
        ])
      })
    );
    expect(view.rows.map((r) => r.kind)).toEqual(['blocked', 'budget', 'budget', 'approval']);
  });

  it('files a hold by its reason, so a round cap lands under review, not budget', () => {
    const view = composeHolds(
      sources({
        budget: CAPPED,
        dashboard: withRows([hold(), hold({ item_id: '2202', reason: 'ci_pending' })])
      })
    );
    // The override is the only `budget` row here: the two holds are what their
    // reasons say they are (#2208).
    expect(view.rows.map((r) => r.kind)).toEqual(['budget', 'review', 'ci']);
  });

  it('files a SET row by its reason too, so one row says one thing', () => {
    // #2280. The kind column and the sentence beside it are two cells of one
    // row, and `rowWords` composes that sentence from the hold vocabulary
    // wherever it claims the reason (#2245). `holdKindOf` read only
    // `attention_kind`, whose answer for the generic `held` kind is `other` —
    // the word for "no better word" — so the committed round-cap hold, served
    // as an `epic_wait`, read `OTHER` beside a sentence the `review` bucket
    // wrote, while the identical reason out of `epic_holds` read `REVIEW`.
    const reason = 'review_rounds_exhausted: review pool $10.00 exhausted';
    const served = wait({
      reason: `#2055 — ${reason}`,
      label: 'the review round cap was reached',
      attention_kind: 'held'
    });
    expect(holdKindOf(served)).toBe('review');
    // The same reason out of the table it is a transcription of: one word.
    expect(holdKindOf(hold({ reason }))).toBe('review');
  });

  it('files a set row under the word its own sentence is built from, for every prefix', () => {
    // The invariant behind that, over the whole vocabulary: the category and
    // the sentence come from one reading of one string, so no prefix can grow
    // a second answer. The reference the set writes onto an `epic_wait` reason
    // is on the front, which is what `reasonBody` is for — `HOLD_SPECS` matches
    // a prefix, so leaving it on would match nothing at all.
    for (const spec of HOLD_SPECS) {
      const reason = `${spec.prefix}: whatever tail it carries`;
      const sentence = `${spec.label}: whatever tail it carries`;
      const row = wait({ reason: `#2055 — ${reason}`, attention_kind: 'held', sentence });
      expect(holdKindOf(row), spec.prefix).toBe(spec.category);
      // The sentence beside the word is the server's, verbatim (#2539).
      expect(captionText(setRowOf(row).detail), spec.prefix).toContain(sentence);
    }
  });

  it('keeps the kind the server stamped where the table claims nothing', () => {
    // The fallback is unchanged, and it is the answer for most of the set: a
    // merge decision, an orphan PR, a work exception and a driver nobody is
    // ticking carry reasons no hold prefix claims, and the kind of wait the
    // server named on the row is the only reading there is.
    expect(holdKindOf(wait())).toBe('merge');
    expect(holdKindOf(wait({ attention_kind: 'work_exception' }))).toBe('needs');
    expect(holdKindOf(wait({ attention_kind: 'held' }))).toBe('other');
    expect(holdKindOf(wait({ attention_kind: 'moon_phase_wrong' }))).toBe('needs');
  });

  it('orders the kinds exactly as HOLD_ORDER declares them', () => {
    // The hold categories sit where the single `budget` group used to, so the
    // column still reads blocks, then money, then the rest, then people.
    expect([...HOLD_ORDER]).toEqual([
      'blocked',
      'budget',
      'review',
      'sandbox',
      'dispatch',
      'merge',
      'ci',
      'other',
      'approval',
      'needs'
    ]);
  });

  it('draws each kind with the mark factory/status already gives that meaning', () => {
    // Lavender square, yellow ring, magenta diamond — never a local palette.
    // `needs` takes the same diamond as `approval`: both are a person's move,
    // and the colour the UI rations says exactly that (#2207). A hold the
    // MACHINERY is holding takes the yellow ring a cap already wears; a
    // needs-you `merge` entry is not one of those — nothing is holding the
    // node, a person simply has not merged it — so it takes the diamond too
    // (#2280 D14). This table is that reading, which is why the mark a row
    // DRAWS is the row's own field: an `epic_holds` row of the same category
    // takes `HELD_MARK` (see "the mark a row draws" below).
    expect(HOLD_MARK).toEqual({
      blocked: 'blocked',
      budget: 'held',
      review: 'held',
      sandbox: 'held',
      dispatch: 'held',
      merge: 'needs_you',
      ci: 'held',
      other: 'held',
      approval: 'needs_you',
      needs: 'needs_you'
    });
  });

  it('gives every row a key of its own, so two kinds cannot collide', () => {
    const view = composeHolds(
      sources({
        epics: [snapshot([node(), node({ id: '2054' })])],
        budget: CAPPED,
        dashboard: withRows([row(), row({ type: 'budget_hold' })])
      })
    );
    expect(new Set(view.rows.map((r) => r.key)).size).toBe(view.rows.length);
  });
});

describe('blocked rows', () => {
  it('names the node and prints the reason FUI-10 recorded', () => {
    const view = composeHolds(sources({ epics: [snapshot([node()])] }));
    expect(captionText(view.rows[0].detail)).toBe('#2053 needs a design decision');
  });

  it('links the node reference to its epic, not out of the app', () => {
    const view = composeHolds(sources({ epics: [snapshot([node()], 'launch fixes')] }));
    expect(view.rows[0].detail[0]).toEqual({ text: '#2053', href: '/epics/launch%20fixes' });
  });

  it('keeps a blocked node with no recorded reason, and says so', () => {
    const view = composeHolds(sources({ epics: [snapshot([node({ blocked_reason: null })])] }));
    // Dropping it would hide a real block behind a missing annotation.
    expect(captionText(view.rows[0].detail)).toBe('#2053 blocked, with no reason recorded');
  });

  it('lists only blocked nodes — a done or failed node is not a block', () => {
    const view = composeHolds(
      sources({
        epics: [snapshot([node({ id: '1', status: 'done' }), node({ id: '2', status: 'failed' })])]
      })
    );
    expect(view.rows).toEqual([]);
  });

  it('offers no action: nothing on this screen can unblock a node', () => {
    const view = composeHolds(sources({ epics: [snapshot([node()])] }));
    expect(view.rows[0].actions).toEqual([]);
    expect(view.rows[0].itemId).toBeNull();
  });
});

describe('the override sentence', () => {
  it('is built from file_value, value and set_by', () => {
    expect(overrideCopy('budget.weekly_usd', 600, 1500, 'operator')).toBe(
      'weekly cap reached at $600; raised to $1,500 (operator)'
    );
  });

  it('says lowered when the money went down', () => {
    expect(overrideCopy('budget.daily_usd', 250, 100, 'operator')).toBe(
      'daily cap lowered from $250 to $100 (operator)'
    );
  });

  it('claims no direction when the override restates the file value', () => {
    expect(overrideCopy('budget.daily_usd', 250, 250, 'ops')).toBe(
      'daily cap overridden at $250 (ops)'
    );
  });

  it('drops the parenthetical rather than printing an empty author', () => {
    expect(overrideCopy('budget.weekly_usd', 600, 1500, '')).toBe(
      'weekly cap reached at $600; raised to $1,500'
    );
  });

  it('names the window from the field, including one it has never seen', () => {
    expect(capWindow('budget.weekly_usd')).toBe('weekly');
    expect(capWindow('budget.per_issue_usd')).toBe('per-issue');
    expect(capWindow('budget.per_epic_hour_usd')).toBe('per-epic-hour');
  });

  it('ignores the override reason — the row stands on the fields', () => {
    const budget: BudgetMap = {
      'budget.weekly_usd': {
        effective: 400,
        file_value: 600,
        override: { value: 400, set_by: 'operator', set_at: '', reason: 'because I said so' }
      }
    };
    expect(captionText(composeHolds(sources({ budget })).rows[0].detail)).not.toContain(
      'because I said so'
    );
  });

  it('raises no row for a cap the config file alone decides', () => {
    const budget: BudgetMap = {
      'budget.daily_usd': { effective: 250, file_value: 250, override: null }
    };
    expect(composeHolds(sources({ budget })).rows).toEqual([]);
  });
});

describe('budget-hold and approval rows', () => {
  it('prints the server sentence with the row reference linked', () => {
    const view = composeHolds(sources({ dashboard: withRows([row()]) }));
    expect(view.rows[0].detail).toEqual([
      { text: 'approval needed on ', href: null },
      { text: '#77', href: 'https://example.invalid/77' }
    ]);
  });

  it('names the item first where the sentence does not (design review c9 D12)', () => {
    // The server says an approval as the action it waits on, which names no
    // item: the row leads with it, linked into the app as the epic rows' are.
    const view = composeHolds(sources({ dashboard: withRows([APPROVAL_114]) }), Date.parse('2026-09-24T15:00:00Z'));
    expect(view.rows[0].detail).toEqual([
      { text: '#114', href: '/work?item=114' },
      { text: ` — ${APPROVAL_SENTENCE} · held 3h00m`, href: null }
    ]);
  });

  it('reads a reference as named only when the sentence says it whole', () => {
    // `#11` is not named by a sentence about `#114`: the row still leads with it.
    const eleven = { ...APPROVAL_114, issue_num: 11, item_id: '11', sentence: `${APPROVAL_SENTENCE} (#114 shares it)` };
    const view = composeHolds(sources({ dashboard: withRows([eleven]) }), Date.parse('2026-09-24T15:00:00Z'));
    expect(view.rows[0].detail[0]).toEqual({ text: '#11', href: '/work?item=11' });
    // A sentence that does name it whole keeps the caption it had.
    const named = { ...APPROVAL_114, sentence: `${APPROVAL_SENTENCE} for #114` };
    const kept = composeHolds(sources({ dashboard: withRows([named]) }), Date.parse('2026-09-24T15:00:00Z'));
    expect(captionText(kept.rows[0].detail)).toBe(`${APPROVAL_SENTENCE} for #114`);
  });

  it('leaves every other row with no epic as it was: no item head', () => {
    // A merge decision with no epic says its PR, and its caption links out
    // as before (design review c9 D12 is about approval holds only).
    const merge = row({
      type: 'pr_review',
      reason: 'merge decision on PR #2059',
      sentence: 'PR #2059 is open — the merge decision is yours',
      issue_num: 2055,
      item_id: '2055',
      url: 'https://example.invalid/pull/2059',
      attention_kind: 'merge_decision'
    });
    const view = composeHolds(sources({ dashboard: withRows([merge]) }));
    expect(captionText(view.rows[0].detail)).toBe('PR #2059 is open — the merge decision is yours');
    expect(view.rows[0].detail.some((s) => s.text === '#2055')).toBe(false);
  });

  it('offers approve and deny on an approval hold, worded as the console words them', () => {
    const view = composeHolds(sources({ dashboard: withRows([row()]) }));
    expect(view.rows[0].actions).toEqual([
      { label: 'Deny', action: 'deny', primary: false },
      { label: 'Approve', action: 'approve', primary: true }
    ]);
  });

  it('offers no button on an epic hold — the queue`s approve cannot clear one', () => {
    // #2208: `decideHold` POSTs `/api/approvals/<item>`, which releases the
    // GitHub needs-approval LABEL. It has never read the `epic_holds` table,
    // so the only answer "Approve this hold" could ever get on a round-cap or
    // CI hold is `not_held`. The action that does clear it is named instead.
    const view = composeHolds(sources({ dashboard: withRows([hold()]) }));
    expect(view.rows[0].actions).toEqual([]);
    expect(view.rows[0].action).not.toBe('');
  });

  it('addresses a write to the durable item id, never a parsed one', () => {
    const view = composeHolds(
      sources({ dashboard: withRows([row({ item_id: 'SFD-12', issue_num: 0 })]) })
    );
    expect(view.rows[0].itemId).toBe('SFD-12');
  });

  it('falls back to the legacy numeric id when a payload predates item_id', () => {
    const view = composeHolds(sources({ dashboard: withRows([row({ item_id: null })]) }));
    expect(view.rows[0].itemId).toBe('77');
  });

  it('keys a row exactly as the console keys it, so a decided row cannot return', () => {
    const r = row();
    const view = composeHolds(sources({ dashboard: withRows([r]) }));
    expect(view.rows[0].key).toBe(rowKey(r));
  });

  it('draws every needs-you row, whatever its type — the set is the column', () => {
    // It used to draw the two it could DECIDE and leave the rest to the
    // console, which is why this head read "4 active" under a header counting
    // 16 of the same thing (#2207). Now the set IS the list.
    const view = composeHolds(
      sources({
        dashboard: withRows([
          row({ type: 'pr_review', item_id: '1' }),
          row({ type: 'orphan_pr', item_id: '2' }),
          row({ type: 'stalled_epic', item_id: '3' }),
          row({ type: 'work_exception', item_id: '4' }),
          row({ type: 'epic_wait', item_id: '5' })
        ])
      })
    );
    expect(view.rows.map((r) => r.kind)).toEqual(['needs', 'needs', 'needs', 'needs', 'needs']);
    expect(view.meta).toBe('5 active');
    expect(countedHolds(view.rows)).toHaveLength(5);
  });

  it('offers approve and deny only on the two kinds this screen can decide', () => {
    const view = composeHolds(
      sources({ dashboard: withRows([row({ type: 'epic_wait', item_id: '5' })]) })
    );
    expect(view.rows[0].actions).toEqual([]);
  });

  it('marks which rows are the needs-you set the header counts', () => {
    // A node blocked on an upstream node is nobody's move (`kew.waits`
    // classifies `blocked` to no attention kind) and a cap override in force is
    // a standing policy, not a wait — so neither is in the set. Both stay drawn
    // and both stay counted by THIS head, which counts what it drew; `counted`
    // is how a reader crossing from the header finds the row it counted.
    const view = composeHolds(
      sources({ epics: [snapshot([node()])], budget: CAPPED, dashboard: withRows([row()]) })
    );
    expect(view.rows.map((r) => r.counted)).toEqual([false, false, true]);
    expect(countedHolds(view.rows)).toHaveLength(1);
    expect(view.meta).toBe('3 active');
  });

  it('orders holds of one kind oldest-first, as the queue does', () => {
    const view = composeHolds(
      sources({
        dashboard: withRows([
          row({ item_id: 'new', age: '2026-09-24T18:00:00+00:00' }),
          row({ item_id: 'old', age: '2026-09-24T06:00:00+00:00' })
        ])
      })
    );
    expect(view.rows.map((r) => r.itemId)).toEqual(['old', 'new']);
  });
});

describe('a hold row names its epic, its item and what clears it', () => {
  // #2208 — the owner's screenshots caught `BUDGET · review_rounds_exhausted ·
  // Approve this hold`: no epic, no issue, the wrong category, the raw reason
  // and a button that cannot clear the hold. One row per hold kind here, held
  // to the three readings that row got wrong.
  const NOW = Date.parse('2026-09-24T14:13:00+00:00');

  /** The one row a dashboard carrying a single hold composes to. */
  const only = (over: Partial<DashboardRow> = {}) =>
    composeHolds(sources({ dashboard: withRows([hold(over)]) }), NOW).rows[0];

  it('names the epic and the item, both linked into the app', () => {
    const r = only();
    expect(r.detail[0]).toEqual({ text: 'launch-fixes', href: '/epics/launch-fixes' });
    expect(r.detail[1]).toEqual({ text: ' / ', href: null });
    expect(r.detail[2]).toEqual({ text: '#2201', href: '/work?item=2201' });
  });

  it('prefers the url the payload carries for the item, when there is one', () => {
    expect(only({ url: 'https://example.invalid/2201' }).detail[2].href).toBe(
      'https://example.invalid/2201'
    );
  });

  it('links the item into the app when its served url is one safeHref refuses', () => {
    expect(only({ url: 'javascript:alert(1)' }).detail[2].href).toBe('/work?item=2201');
  });

  it('never links a served url safeHref refuses on a row with no epic', () => {
    const r = setRowOf(wait({ epic_ref: null, url: 'javascript:alert(document.domain)' }));
    expect(JSON.stringify(r.detail)).not.toContain('javascript:');
    expect(captionText(r.detail)).toContain('#2055');
  });

  it('still links the served url of a row with no epic when safeHref passes it', () => {
    const url = 'https://github.com/thossullivan/kew/pull/2059';
    const r = setRowOf(wait({ epic_ref: null, url }));
    expect(r.detail.some((segment) => segment.href === url)).toBe(true);
  });

  it('says how long the hold has stood, from the held_at the server stamped', () => {
    expect(captionText(only().detail)).toContain('held 2h13m');
  });

  it('prints no duration for a stamp it cannot read, never a fabricated one', () => {
    expect(captionText(only({ age: '' }).detail)).toBe(
      'launch-fixes / #2201 — the review round cap was reached'
    );
  });

  it.each([
    [
      'review_rounds_exhausted',
      'review',
      'the review round cap was reached',
      'kew epic unhold launch-fixes 2201 — it restarts the review cycle'
    ],
    [
      'budget_preflight: spent $12.00 > per_issue cap $10.00 — raise budget.per_issue_usd in kew.toml',
      'budget',
      'held on budget: spent $12.00 > per_issue cap $10.00 — raise budget.per_issue_usd in kew.toml',
      'raise budget.per_issue_usd'
    ],
    [
      'review_budget: review pool $5.00 exhausted (spent $6.00) — raise [review].max_review_usd',
      'budget',
      'held on the review budget: review pool $5.00 exhausted (spent $6.00) — raise [review].max_review_usd',
      'raise [review].max_review_usd'
    ],
    ['ci_pending', 'ci', 'CI checks are still running', ''],
    [
      'sandbox_containment: no image for claude',
      'sandbox',
      'held on containment: no image for claude',
      'map [sandbox] images.<runner>, then kew epic unhold launch-fixes 2201'
    ],
    [
      'preexisting_worktree_refused: /tmp/wt is dirty',
      'dispatch',
      'held on a pre-existing-worktree refusal: /tmp/wt is dirty',
      're-check the worktree its reason names, then kew epic unhold launch-fixes 2201'
    ],
    [
      'base_integration_conflict: src/kew/cli.py',
      'merge',
      'the base branch no longer merges into the branch: src/kew/cli.py',
      'resolve the conflict on the branch, then kew epic unhold launch-fixes 2201'
    ]
  ])('reads %s as its own category and action, beside the server sentence', (reason, kind, sentence, action) => {
    // The sentence is the one the server writes for this reason (#2539) and
    // is printed verbatim; the hold table decides the word and the action.
    const r = only({ reason, sentence });
    expect(r.kind).toBe(kind);
    expect(captionText(r.detail)).toBe(`launch-fixes / #2201 — ${sentence} · held 2h13m`);
    expect(r.action).toBe(action);
    // Not one of these is decidable here: `/api/approvals` releases the GitHub
    // needs-approval label and has never read the `epic_holds` table.
    expect(r.actions).toEqual([]);
  });

  it('reads a reason it has never heard of as other, with the reason as written', () => {
    const r = only({ reason: 'moon_phase_wrong: waxing gibbous', sentence: '' });
    expect(r.kind).toBe('other');
    expect(captionText(r.detail)).toContain('moon_phase_wrong: waxing gibbous');
    expect(r.action).toBe('fix the cause, then kew epic unhold launch-fixes 2201');
  });

  it('never files an unknown reason under budget, whatever the row type says', () => {
    // The row type is `budget_hold` for every `epic_holds` row, which is
    // exactly what made BUDGET the default. There is no default any more.
    for (const reason of ['moon_phase_wrong', 'review_rounds_exhausted', 'ci_pending', '']) {
      expect(only({ reason }).kind).not.toBe('budget');
    }
  });

  it('says a hold with no reason recorded rather than drawing an empty sentence', () => {
    // Nothing served at all: the row's type stands in, never a blank.
    const r = only({ reason: '', sentence: '' });
    expect(captionText(r.detail)).toBe('launch-fixes / #2201 — budget hold · held 2h13m');
  });

  it('stands the sentence alone when the payload names no epic or item', () => {
    const r = only({ epic_ref: null, item_id: '', issue_num: 0 });
    expect(captionText(r.detail)).toBe('the review round cap was reached · held 2h13m');
    // And the command drops the arguments rather than printing item zero.
    expect(r.action).toBe('kew epic unhold — it restarts the review cycle');
  });

  it.each([
    'base_integration_conflict: src/kew/cli.py',
    'sandbox_containment: no image for claude',
    'review_rate_limit_exhausted: 429 for 6h',
    'publication_held: /tmp/keep',
    'review_rounds_exhausted'
  ])('beside a command block, %s says no action line', (reason) => {
    // #2896 R7: one statement per hold. The command block is what clears the
    // row, so no action line is drawn under it.
    const command = 'kew epic unhold launch-fixes 2201';
    const r = only({ reason, command });
    expect(r.command).toBe(command);
    expect(r.action).toBe('');
  });

  it('keeps the action line on a hold that has no command block', () => {
    const r = only({ reason: 'base_integration_conflict: src/kew/cli.py', command: '' });
    expect(r.command).toBe('');
    expect(r.action).not.toBe('');
  });

  it('leaves the judgment queue`s own rows exactly as they were', () => {
    // Only the epic holds are recomposed: an approval hold's sentence is the
    // server's, with its own reference linked (`rowCaption`), and its two
    // buttons are the one write this column has.
    const r = composeHolds(sources({ dashboard: withRows([row()]) }), NOW).rows[0];
    expect(captionText(r.detail)).toBe('approval needed on #77');
    expect(r.action).toBe('');
    expect(r.actions).toHaveLength(2);
  });
});

describe('a reading that was not taken', () => {
  it('counts plainly when all three sources answered', () => {
    const view = composeHolds(
      sources({ epics: [snapshot([node()])], budget: CAPPED, dashboard: withRows([row()]) })
    );
    expect(view.complete).toBe(true);
    expect(view.notices).toEqual([]);
    expect(view.rows).toHaveLength(3);
    expect(view.meta).toBe('3 active');
  });

  it('says none active rather than going silent on a genuinely quiet fleet', () => {
    const view = composeHolds(sources());
    expect(view.meta).toBe('none active');
  });

  it('hedges the count and names the source when the epics were not read', () => {
    const view = composeHolds(
      sources({
        epics: { available: false, reason: EPICS_UNREAD },
        budget: CAPPED,
        dashboard: withRows([row()])
      })
    );
    expect(view.complete).toBe(false);
    expect(view.notices).toEqual([EPICS_UNREAD]);
    expect(view.meta).toBe('at least 2 active');
  });

  it('refuses to report zero holds over a world it could not read', () => {
    const view = composeHolds(
      sources({
        epics: { available: false, reason: EPICS_UNREAD },
        budget: { available: false, reason: 'budget unavailable (HTTP 503)' },
        dashboard: { available: false, reason: 'judgment queue not read' }
      })
    );
    expect(view.rows).toEqual([]);
    expect(view.meta).toBe('count unknown');
    expect(view.notices).toEqual([
      EPICS_UNREAD,
      'budget unavailable (HTTP 503)',
      'judgment queue not read'
    ]);
  });

  it('loses only its own rows when one source fails', () => {
    const view = composeHolds(
      sources({
        epics: [snapshot([node()])],
        budget: { available: false, reason: 'budget unavailable' },
        dashboard: withRows([row()])
      })
    );
    expect(view.rows.map((r) => r.kind)).toEqual(['blocked', 'approval']);
  });

  it('names a judgment-queue source the dashboard itself fail-softed', () => {
    const view = composeHolds(
      sources({ dashboard: withRows([], ['budget_holds', 'approval_holds']) })
    );
    expect(view.notices).toEqual(['budget holds unavailable', 'approval holds unavailable']);
    expect(view.meta).toBe('count unknown');
  });

  it('names every judgment-queue source, now that it draws every row type', () => {
    // There is no such thing as a row source this column does not read any
    // more: it draws the whole needs-you set, so a source the dashboard
    // fail-softed is a hole in THIS list and has to be said out loud.
    const view = composeHolds(sources({ dashboard: withRows([], ['orphan_prs']) }));
    expect(view.complete).toBe(false);
    expect(view.notices).toEqual(['orphan PRs unavailable']);
  });

  it('says every source the header counts from, when it is the one missing', () => {
    // The header blanks its count on any of JUDGMENT_ROW_SOURCES; this column
    // draws the same set, so it must not call that same reading complete. A
    // stopped merge queue whose ledger read failed is the case (#2883).
    for (const name of JUDGMENT_ROW_SOURCES) {
      const view = composeHolds(sources({ dashboard: withRows([], [name]) }));
      expect(view.complete, name).toBe(false);
      expect(view.meta, name).toBe('count unknown');
      expect(view.notices, name).toHaveLength(1);
    }
    const view = composeHolds(sources({ dashboard: withRows([], ['merge_queue']) }));
    expect(view.notices).toEqual(['the merge queue unavailable']);
  });

  it('holdsMeta never states a total it cannot stand behind', () => {
    expect(holdsMeta(3, true)).toBe('3 active');
    expect(holdsMeta(3, false)).toBe('at least 3 active');
    expect(holdsMeta(0, false)).toBe('count unknown');
  });
});

describe('a decided row leaves the way the console lets one leave', () => {
  const decided: VerbResult<DecideState> = { ok: true, data: { status: 'approved', issue: '77' } };
  const refused: VerbResult<DecideState> = {
    ok: false,
    kind: 'not_approver',
    status: 403,
    message: "You're not an approver."
  };

  it('removes the row on the write response, not on the read-back', () => {
    const view = composeHolds(sources({ dashboard: withRows([row()]) }));
    const key = view.rows[0].key;
    expect(decideRemovesRow(decided)).toBe(true);
    expect(visibleHolds(view.rows, { [key]: true })).toEqual([]);
  });

  it('keeps the row when the write was refused', () => {
    const view = composeHolds(sources({ dashboard: withRows([row()]) }));
    expect(decideRemovesRow(refused)).toBe(false);
    expect(visibleHolds(view.rows, {})).toEqual(view.rows);
  });

  it('keeps a decided row gone while the source still serves it', () => {
    // The poll lags the write (CS-6); re-deriving from the sources must not put
    // the row back under the operator's cursor.
    const r = row();
    const first = composeHolds(sources({ dashboard: withRows([r]) }));
    const dismissed = { [first.rows[0].key]: true };
    const next = composeHolds(sources({ dashboard: withRows([r]) }));
    expect(visibleHolds(next.rows, dismissed)).toEqual([]);
  });

  it('removes only the row that was decided', () => {
    const view = composeHolds(
      sources({
        epics: [snapshot([node()])],
        dashboard: withRows([row({ item_id: 'a' }), row({ item_id: 'b' })])
      })
    );
    const gone = view.rows.find((x) => x.itemId === 'a')!;
    expect(visibleHolds(view.rows, { [gone.key]: true }).map((x) => x.itemId)).toEqual([null, 'b']);
  });
});

describe('a blocked dependency outside the epic', () => {
  // `build_graph` records a resolved out-of-epic dependency as an external
  // boundary node inside EVERY epic that depends on it (and the driver never
  // advances one), so the block has to be drawn ONCE — but drawn.
  const external = node({ id: '2099', external: true, blocked_reason: 'waiting on a decision' });

  it('draws a shared blocked dependency once, not once per epic that depends on it', () => {
    const view = composeHolds(
      sources({
        epics: [snapshot([external], 'launch-fixes'), snapshot([external], 'serve-hardening')]
      })
    );
    expect(view.rows.map((r) => r.key)).toEqual(['blocked:2099']);
    expect(view.meta).toBe('1 active');
  });

  it('draws a blocked dependency no epic owns — nobody else is listing it', () => {
    // The regression: `build_graph` resolves any dependency the tracker can
    // answer for, including an item that is a member of no epic at all. Filter
    // every external node away and two epics report `counts.blocked: 1` each
    // while this column says "nothing held or blocked" — with `complete: true`
    // behind it, which is the one reading this surface must never give.
    const view = composeHolds(
      sources({
        epics: [snapshot([external], 'launch-fixes'), snapshot([external], 'serve-hardening')]
      })
    );
    expect(captionText(view.rows[0].detail)).toBe('#2099 waiting on a decision');
    expect(view.complete).toBe(true);
    expect(view.meta).not.toBe('none active');
  });

  it('keys it by the node, not by the epic it was first seen through', () => {
    // Which depending epic answers first is the server's order, not a fact
    // about the block: a key that moved with it would churn the row — and its
    // dismissal — on any poll that came back in the other order.
    const one = composeHolds(
      sources({
        epics: [snapshot([external], 'launch-fixes'), snapshot([external], 'serve-hardening')]
      })
    );
    const other = composeHolds(
      sources({
        epics: [snapshot([external], 'serve-hardening'), snapshot([external], 'launch-fixes')]
      })
    );
    expect(one.rows[0].key).toBe(other.rows[0].key);
  });

  it('prefers the epic that owns the node — the block is listed there', () => {
    const view = composeHolds(
      sources({
        epics: [
          snapshot([external], 'launch-fixes'),
          snapshot([node({ id: '2099' })], 'dependency-epic')
        ]
      })
    );
    expect(view.rows.map((r) => r.key)).toEqual(['blocked:dependency-epic:2099']);
    expect(view.rows[0].detail[0].href).toBe('/epics/dependency-epic');
    expect(view.meta).toBe('1 active');
  });

  it('reads the same however the owning epic is ordered in the walk', () => {
    const view = composeHolds(
      sources({
        epics: [
          snapshot([node({ id: '2099' })], 'dependency-epic'),
          snapshot([external], 'launch-fixes')
        ]
      })
    );
    expect(view.rows.map((r) => r.key)).toEqual(['blocked:dependency-epic:2099']);
  });

  it('yields to the owner only when the owner reads it as blocked too', () => {
    // The regression: every epic is its own snapshot read, so the member copy
    // and the boundary copy of one node can disagree — the member read a moment
    // earlier, or read while the tracker still called it ready. Suppressing the
    // blocked boundary node against a member copy that draws NO row is the
    // block disappearing from the column entirely, with `complete: true` behind
    // the count and nothing said about it.
    const view = composeHolds(
      sources({
        epics: [
          snapshot([external], 'launch-fixes'),
          snapshot([node({ id: '2099', status: 'ready', blocked_reason: null })], 'dependency-epic')
        ]
      })
    );
    expect(view.rows.map((r) => r.key)).toEqual(['blocked:2099']);
    expect(captionText(view.rows[0].detail)).toBe('#2099 waiting on a decision');
    expect(view.meta).toBe('1 active');
    expect(view.complete).toBe(true);
  });

  it('still draws it once when several dependents see a block the owner has not', () => {
    const view = composeHolds(
      sources({
        epics: [
          snapshot([external], 'launch-fixes'),
          snapshot([external], 'serve-hardening'),
          snapshot(
            [node({ id: '2099', status: 'running', blocked_reason: null })],
            'dependency-epic'
          )
        ]
      })
    );
    expect(view.rows.map((r) => r.key)).toEqual(['blocked:2099']);
    expect(view.meta).toBe('1 active');
  });

  it('never counts one blocked node twice, whatever the walk turned up', () => {
    const view = composeHolds(
      sources({
        epics: [
          snapshot([external, node({ id: '2053' })], 'launch-fixes'),
          snapshot([external], 'serve-hardening'),
          snapshot([node({ id: '2099' })], 'dependency-epic')
        ]
      })
    );
    expect(view.rows.map((r) => r.key)).toEqual([
      'blocked:launch-fixes:2053',
      'blocked:dependency-epic:2099'
    ]);
    expect(view.meta).toBe('2 active');
  });
});

describe('the head counts what was drawn', () => {
  const decided = composeHolds(sources({ dashboard: withRows([row()]) }));
  const key = decided.rows[0].key;

  it('never reads "1 active" over "nothing held or blocked"', () => {
    const view = drawnHolds(decided, { [key]: true });
    expect(view.rows).toEqual([]);
    expect(view.meta).toBe('none active');
  });

  it('keeps hedging when a source is missing behind the dismissal', () => {
    const partial = composeHolds(
      sources({
        epics: { available: false, reason: EPICS_UNREAD },
        dashboard: withRows([row()])
      })
    );
    const view = drawnHolds(partial, { [partial.rows[0].key]: true });
    expect(view.meta).toBe('count unknown');
    expect(view.notices).toEqual([EPICS_UNREAD]);
    expect(view.complete).toBe(false);
  });

  it('counts the rows left when only some were decided', () => {
    const both = composeHolds(
      sources({ dashboard: withRows([row({ item_id: 'a' }), row({ item_id: 'b' })]) })
    );
    const view = drawnHolds(both, { [both.rows[0].key]: true });
    expect(view.rows).toHaveLength(1);
    expect(view.meta).toBe('1 active');
  });
});

describe('a dismissal ends when the read it bridges lands', () => {
  it('keeps the dismissal while the sources still serve the row', () => {
    const view = composeHolds(sources({ dashboard: withRows([row()]) }));
    const dismissed = { [view.rows[0].key]: true };
    expect(pruneRowState(view, dismissed)).toBe(dismissed);
  });

  it('drops it once the row is gone, so the SAME item can hold again', () => {
    // `rowKey` is `type:item_id`: a re-raised approval hold on #77 carries the
    // identical key, and a dismissal kept forever would hide it for good.
    const first = composeHolds(sources({ dashboard: withRows([row()]) }));
    const key = first.rows[0].key;
    const confirmed = composeHolds(sources({ dashboard: withRows([]) }));
    const pruned = pruneRowState(confirmed, { [key]: true });
    expect(pruned).toEqual({});

    const reraised = composeHolds(sources({ dashboard: withRows([row()]) }));
    expect(drawnHolds(reraised, pruned).rows.map((r) => r.key)).toEqual([key]);
    expect(drawnHolds(reraised, pruned).meta).toBe('1 active');
  });

  it('prunes a write error the same way, and only for rows that left', () => {
    const view = composeHolds(
      sources({ dashboard: withRows([row({ item_id: 'a' }), row({ item_id: 'b' })]) })
    );
    const [a, b] = view.rows.map((r) => r.key);
    const left = composeHolds(sources({ dashboard: withRows([row({ item_id: 'b' })]) }));
    expect(pruneRowState(left, { [a]: 'refused', [b]: 'refused' })).toEqual({
      [b]: 'refused'
    });
  });

  it('holds a decided row out for as long as the source serves it', () => {
    const r = row();
    const view = composeHolds(sources({ dashboard: withRows([r]) }));
    const dismissed = { [view.rows[0].key]: true };
    const lagging = composeHolds(sources({ dashboard: withRows([r]) }));
    expect(pruneRowState(lagging, dismissed)).toBe(dismissed);
    expect(drawnHolds(lagging, dismissed).rows).toEqual([]);
  });

  // The regression: a read that did not happen is not a read that came back
  // without the row. `/api/dashboard` fail-softs per row source — 200, no
  // approval rows, `unavailable: ['approval_holds']` — and pruning the
  // dismissal on that reading resurrects the decided row the moment the source
  // recovers while the write's own read-back still lags.
  it('keeps the dismissal when the row SOURCE did not answer', () => {
    const decided = composeHolds(sources({ dashboard: withRows([row()]) }));
    const key = decided.rows[0].key;
    const dismissed = { [key]: true };

    const softFailed = composeHolds(sources({ dashboard: withRows([], ['approval_holds']) }));
    expect(softFailed.rows).toEqual([]);
    expect(softFailed.notices).toEqual(['approval holds unavailable']);
    expect(pruneRowState(softFailed, dismissed)).toBe(dismissed);

    // …and the row is still hidden when the source comes back before the
    // read-back that confirms the write.
    const recovered = composeHolds(sources({ dashboard: withRows([row()]) }));
    expect(drawnHolds(recovered, dismissed).rows).toEqual([]);
    expect(drawnHolds(recovered, dismissed).meta).toBe('none active');
  });

  it('keeps it when the whole queue read failed, and drops it when that read lands', () => {
    const decided = composeHolds(sources({ dashboard: withRows([row()]) }));
    const key = decided.rows[0].key;
    const unread = composeHolds(
      sources({ dashboard: { available: false, reason: 'judgment queue not read' } })
    );
    expect(pruneRowState(unread, { [key]: true })).toEqual({ [key]: true });
    // The confirming read answered for that source and has no such row.
    const confirmed = composeHolds(sources({ dashboard: withRows([], ['budget_holds']) }));
    expect(pruneRowState(confirmed, { [key]: true })).toEqual({});
  });

  it('prunes the sources that DID answer while holding the one that did not', () => {
    const budgetHold = row({ type: 'budget_hold', item_id: 'b9', issue_num: 9 });
    const both = composeHolds(sources({ dashboard: withRows([row(), budgetHold]) }));
    const [budgetKey, approvalKey] = [rowKey(budgetHold), rowKey(row())];
    expect(both.rows.map((r) => r.key)).toEqual([budgetKey, approvalKey]);

    // Budget holds answered (and the hold is closed); approvals did not answer.
    const partial = composeHolds(sources({ dashboard: withRows([], ['approval_holds']) }));
    expect(pruneRowState(partial, { [budgetKey]: true, [approvalKey]: true })).toEqual({
      [approvalKey]: true
    });
  });

  it('keeps a blocked or override dismissal when its own read failed', () => {
    // Neither family is decidable from this column today, but the per-row maps
    // are keyed the same way for all three sources and must not drop a key on a
    // read that never happened.
    const walked = composeHolds(sources({ epics: [snapshot([node()])], budget: CAPPED }));
    const [blockedKey, overrideKey] = walked.rows.map((r) => r.key);
    const dark = composeHolds(
      sources({
        epics: { available: false, reason: EPICS_UNREAD },
        budget: { available: false, reason: 'budget unavailable' }
      })
    );
    const state = { [blockedKey]: 'x', [overrideKey]: 'x' };
    expect(pruneRowState(dark, state)).toBe(state);
    expect(pruneRowState(walked, state)).toBe(state);
  });
});

describe('the two readings this column takes for itself', () => {
  const summary = (epic_ref: string, blocked: number) => ({
    epic_ref,
    signal: 'progressing',
    counts: { blocked },
    drain_total_usd: 0,
    last_tick_ts: null,
    interval_s: null,
    change_count: 0,
    label: epic_ref
  });

  const OVERRIDDEN: BudgetMap = {
    'budget.weekly_usd': {
      effective: 400,
      file_value: 600,
      override: { value: 400, set_by: 'operator', set_at: '', reason: 'operator' }
    }
  };
  const REVERTED: BudgetMap = {
    'budget.weekly_usd': { effective: 600, file_value: 600, override: null }
  };

  /** What the column shows after one poll, with the judgment queue quiet. */
  async function polled(f: Partial<HoldsFetchers>) {
    return composeHolds({ ...(await readHoldSources(f)), dashboard: EMPTY_DASHBOARD });
  }

  function fetchers(over: Partial<HoldsFetchers> = {}): Partial<HoldsFetchers> {
    return {
      epics: async () => [summary('launch-fixes', 1)],
      epic: async (ref: string) =>
        ({ epic_ref: ref, snapshot: snapshot([node()], ref), heartbeat: null }) as EpicDetail,
      budget: async () => OVERRIDDEN,
      ...over
    };
  }

  it('re-reads the overrides, so one another operator lifted leaves the column', async () => {
    // The bug this pins: a budget map read once by `load()` goes on drawing a
    // hold that no longer exists, with `complete: true` behind the count.
    let caps = OVERRIDDEN;
    const f = fetchers({ budget: async () => caps });

    const before = await polled(f);
    expect(before.rows.map((r) => r.kind)).toEqual(['blocked', 'budget']);
    expect(before.meta).toBe('2 active');

    caps = REVERTED;
    const after = await polled(f);
    expect(after.rows.map((r) => r.kind)).toEqual(['blocked']);
    expect(after.meta).toBe('1 active');
    expect(after.complete).toBe(true);
  });

  it('draws an override raised after the page loaded', async () => {
    let caps = REVERTED;
    const f = fetchers({ budget: async () => caps });
    expect((await polled(f)).rows).toHaveLength(1);
    caps = OVERRIDDEN;
    const after = await polled(f);
    expect(captionText(after.rows[1].detail)).toBe(
      'weekly cap lowered from $600 to $400 (operator)'
    );
  });

  it('loses the overrides and not the blocks when the budget read fails', async () => {
    const f = fetchers({
      budget: async () => {
        throw new Error('/api/config/budget -> 503');
      }
    });
    const view = await polled(f);
    expect(view.rows.map((r) => r.kind)).toEqual(['blocked']);
    expect(view.notices).toEqual(['budget unavailable (HTTP 503)']);
    expect(view.complete).toBe(false);
  });

  it('loses the blocks and not the overrides when an epic read fails', async () => {
    const f = fetchers({
      epic: async () => {
        throw new Error('/api/epics/launch-fixes -> 500');
      }
    });
    const view = await polled(f);
    expect(view.rows.map((r) => r.kind)).toEqual(['budget']);
    expect(view.notices).toEqual(['epics unavailable (HTTP 500)']);
  });

  it('opens only the epics that have a blocked node — the poll runs all day', async () => {
    const opened: string[] = [];
    const f = fetchers({
      epics: async () => [summary('launch-fixes', 1), summary('quiet-epic', 0)],
      epic: async (ref: string) => {
        opened.push(ref);
        return { epic_ref: ref, snapshot: snapshot([node()], ref), heartbeat: null } as EpicDetail;
      }
    });
    await readHoldSources(f);
    expect(opened).toEqual(['launch-fixes']);
  });

  it('does not open an archived epic, so its blocks reach no row (#2243)', async () => {
    // An operator who archived an epic was promised it leaves the needs-you
    // count — which the server now keeps for every entry the epic owns. This
    // column walks the epics itself, so the promise has to be kept here too or
    // Monitor draws blocks the header has already stopped counting.
    const opened: string[] = [];
    const f = fetchers({
      epics: async () => [
        summary('launch-fixes', 1),
        { ...summary('containment', 2), archived: true }
      ],
      epic: async (ref: string) => {
        opened.push(ref);
        return { epic_ref: ref, snapshot: snapshot([node()], ref), heartbeat: null } as EpicDetail;
      }
    });
    const view = await polled(f);
    expect(opened).toEqual(['launch-fixes']);
    expect(view.rows.filter((r) => r.kind === 'blocked').map((r) => r.key)).toEqual([
      'blocked:launch-fixes:2053'
    ]);
    // And the walk still completed, so the count is a count and not a floor.
    expect(view.complete).toBe(true);
  });

  it('skips the archived shelf fixture, which the harness serves no detail for', async () => {
    // The operator's visual check for #2243: `tui-rewrite` is the committed
    // archived epic and now carries a blocked node, so the fixture world only
    // renders this column if the skip is real. Without it the walk opens an
    // epic the harness has summary-only (#2206) and the whole column reads
    // "epics unavailable" — which is the regression, made visible.
    const opened: string[] = [];
    const f = fetchers({
      epics: async () => epicsJson as unknown as EpicSummary[],
      epic: async (ref: string) => {
        opened.push(ref);
        return { epic_ref: ref, snapshot: snapshot([node()], ref), heartbeat: null } as EpicDetail;
      }
    });
    const read = await readHoldSources(f);
    expect(opened).toEqual(['launch-fixes', 'go-live-2026-09']);
    expect(Array.isArray(read.epics)).toBe(true);
  });

  it('keeps an epic a `kew serve` predating the archive cannot speak about', async () => {
    // `archived` is optional on the wire (#2206). A missing field is "no
    // decision recorded", which must read as active — hiding a blocked epic
    // because an older server said nothing is the one unsafe default here.
    const opened: string[] = [];
    const f = fetchers({
      epics: async () => [summary('launch-fixes', 1)],
      epic: async (ref: string) => {
        opened.push(ref);
        return { epic_ref: ref, snapshot: snapshot([node()], ref), heartbeat: null } as EpicDetail;
      }
    });
    await readHoldSources(f);
    expect(opened).toEqual(['launch-fixes']);
  });

  it('is the component\'s own poll, not a one-shot read taken by the page', () => {
    // Svelte's effects need a client runtime this suite does not have, so the
    // three lines that cannot live in a pure module — the first read, the
    // timer over it, and the prune the poll feeds — are pinned by reading the
    // component. Everything those lines DECIDE lives in `holdSources` and
    // `pruneRowState` above, where it is tested rather than grepped for.
    const component = readFileSync(new URL('./Holds.svelte', import.meta.url), 'utf8');
    expect(component).toMatch(/function land\(next: PolledSources\): void \{\s*polled = next;/);
    // The poll declines a round while the tab is hidden (#2285, C8), and holds
    // one round out at a time (#3468 C9; `holds-poll.test.ts`).
    expect(component).toContain('const poll = holdsPoll(land, () => document.hidden);');
    expect(component).toMatch(/poll\.show\(\);/);
    expect(component).toContain('setInterval(() => poll.beat(), HOLDS_POLL_MS);');
    expect(component).toMatch(/holdSources\(\{\s*polled,\s*seedBudget: budget/);
  });

  it('hands every caps reading back to the page, so one map draws both', () => {
    // `priorCap` marks the prior cap off the same `override` these rows are
    // built from: a pin left standing over a lifted override would contradict
    // the list right below it (see `monitor/+page.svelte`).
    const component = readFileSync(new URL('./Holds.svelte', import.meta.url), 'utf8');
    expect(component).toMatch(/onbudget\?\.\(next\.budget\)/);
    const page = readFileSync(
      new URL('../../routes/(factory)/monitor/+page.svelte', import.meta.url),
      'utf8'
    );
    expect(page).toMatch(/onbudget=/);
    expect(page).toContain('priorCap(budget, DAILY_CAP_FIELD)');
    expect(page).toContain('priorCap(budget, WEEKLY_CAP_FIELD)');
  });

  it('prunes both per-row maps against the rows the sources still serve', () => {
    const component = readFileSync(new URL('./Holds.svelte', import.meta.url), 'utf8');
    expect(component).toMatch(/pruneRowState\(view, dismissed\)/);
    expect(component).toMatch(/pruneRowState\(view, errors\)/);
  });

  it('serves no blocks at all when one snapshot of the walk is missing', async () => {
    // A short list of blocks that looks complete is the failure this column
    // exists to avoid, so a partial walk is "epics unavailable", not a subset.
    const f = fetchers({
      epics: async () => [summary('launch-fixes', 1), summary('serve-hardening', 2)],
      epic: async (ref: string) => {
        if (ref === 'serve-hardening') throw new Error('/api/epics/serve-hardening -> 500');
        return { epic_ref: ref, snapshot: snapshot([node()], ref), heartbeat: null } as EpicDetail;
      }
    });
    const read = await readHoldSources(f);
    expect(read.epics).toEqual({ available: false, reason: 'epics unavailable (HTTP 500)' });
  });
});

describe('which reading stands for which source', () => {
  // The rule the component used to carry as a chain of `??` in markup no
  // vitest can mount. It is the poll's answer that counts: the page's `load()`
  // map is a seed, and a seed that outlives the poll is how this column draws
  // a hold nobody is holding.
  const OVERRIDDEN: BudgetMap = {
    'budget.weekly_usd': {
      effective: 400,
      file_value: 600,
      override: { value: 400, set_by: 'operator', set_at: '', reason: 'operator' }
    }
  };
  const REVERTED: BudgetMap = {
    'budget.weekly_usd': { effective: 600, file_value: 600, override: null }
  };
  const SNAPS = [snapshot([node()])];

  it('shows the page load map until the first poll answers', () => {
    const out = holdSources({ polled: null, seedBudget: OVERRIDDEN, dashboard: EMPTY_DASHBOARD });
    expect(out.budget).toBe(OVERRIDDEN);
  });

  it('takes the caps from the poll the moment it has answered', () => {
    const out = holdSources({
      polled: { epics: SNAPS, budget: REVERTED },
      seedBudget: OVERRIDDEN,
      dashboard: EMPTY_DASHBOARD
    });
    expect(out.budget).toBe(REVERTED);
  });

  it('drops an override the poll says is gone, though the load map still has it', () => {
    // The end of the trade the seed exists for: it fills the first frame and
    // nothing after it. Keeping it would leave a "1 active" cap hold standing
    // over a cap that is back to the config file's own number.
    const view = composeHolds(
      holdSources({
        polled: { epics: [], budget: REVERTED },
        seedBudget: OVERRIDDEN,
        dashboard: EMPTY_DASHBOARD
      })
    );
    expect(view.rows).toEqual([]);
    expect(view.meta).toBe('none active');
  });

  it('lets a failed caps read replace the seed rather than re-serving it', () => {
    const failed = { available: false, reason: 'budget unavailable (HTTP 503)' } as const;
    const view = composeHolds(
      holdSources({
        polled: { epics: [], budget: failed },
        seedBudget: OVERRIDDEN,
        dashboard: EMPTY_DASHBOARD
      })
    );
    expect(view.notices).toEqual(['budget unavailable (HTTP 503)']);
    expect(view.complete).toBe(false);
  });

  it('names the epics unread before the first poll, and never as an empty list', () => {
    const view = composeHolds(
      holdSources({ polled: null, seedBudget: {}, dashboard: EMPTY_DASHBOARD })
    );
    expect(view.notices).toEqual([EPICS_UNREAD]);
    expect(view.meta).toBe('count unknown');
  });

  it('takes the blocks from the poll once it has answered', () => {
    const view = composeHolds(
      holdSources({
        polled: { epics: SNAPS, budget: {} },
        seedBudget: {},
        dashboard: EMPTY_DASHBOARD
      })
    );
    expect(view.rows.map((r) => r.kind)).toEqual(['blocked']);
    expect(view.complete).toBe(true);
  });

  it('prefers a payload the caller supplied over anything it polled', () => {
    const out = holdSources({
      polled: { epics: SNAPS, budget: {} },
      seedBudget: {},
      epics: [],
      dashboard: EMPTY_DASHBOARD
    });
    expect(out.epics).toEqual([]);
  });

  it('names the queue unread until the shell pulse has ticked', () => {
    const view = composeHolds(holdSources({ polled: null, seedBudget: {}, dashboard: null }));
    expect(view.notices).toContain('judgment queue not read');
    expect(view.complete).toBe(false);
  });
});

describe('Holds.svelte', () => {
  // SSR rendering is the component harness this suite has: it runs the props,
  // the derived sources and the markup for real (effects, and so the poll,
  // need a client runtime — those lines are pinned by reading the source).
  // Svelte's hydration markers sit between every tag and its text; they are
  // not what these assertions are about.
  const html = (props: ComponentProps<typeof Holds>) =>
    render(Holds, { props }).body.replace(/<!--.*?-->/g, '');

  it('draws the caps the page seeded it with, before any poll of its own', () => {
    factoryPulse.dashboard = EMPTY_DASHBOARD;
    const body = html({ budget: CAPPED, epics: [] });
    expect(body).toContain('weekly cap lowered from ');
    expect(body).toContain('$400');
    expect(body).toContain('1 active');
  });

  it('draws a blocked row under the kind word its mark is taken from', () => {
    factoryPulse.dashboard = EMPTY_DASHBOARD;
    const body = html({ budget: {}, epics: [snapshot([node()])] });
    expect(body).toContain('needs a design decision');
    expect(body).toContain('href="/epics/launch-fixes"');
    expect(body).toContain('>blocked</span>');
  });

  it('offers the queue decisions on the row, and none on a block', () => {
    factoryPulse.dashboard = withRows([row()]);
    const decidable = html({ budget: {}, epics: [] });
    expect(decidable).toContain('>Approve</button>');
    expect(decidable).toContain('>Deny</button>');
    expect(decidable).toContain('href="https://example.invalid/77"');

    factoryPulse.dashboard = EMPTY_DASHBOARD;
    expect(html({ budget: {}, epics: [snapshot([node()])] })).not.toContain('</button>');
  });

  it('draws a hold under HELD, with the action that clears it', () => {
    // #2539 D19: the word follows the state. A round-cap hold read REVIEW in
    // the amber of a hold — the word of one thing in the colour of another.
    factoryPulse.dashboard = withRows([hold()]);
    const body = html({ budget: {}, epics: [] });
    expect(body).toContain('>held</span>');
    expect(body).not.toContain('>review</span>');
    expect(body).not.toContain('>budget</span>');
    expect(body).toContain('href="/epics/launch-fixes"');
    expect(body).toContain('href="/work?item=2201"');
    expect(body).toContain('the review round cap was reached');
    expect(body).toContain('kew epic unhold launch-fixes 2201');
    // The button that could only ever answer `not_held` is gone (#2208).
    expect(body).not.toContain('Approve this hold');
    expect(body).not.toContain('</button>');
  });

  it('marks the first magenta command row and the first merge decision for the demo tour, and no other row', () => {
    // The "What needs me?" tour points at one row of each: a wait that is
    // yours with the command the server computed, and a merge decision made
    // on the forge. An amber hold carries a command too and is not a person's
    // move, so it is never the one marked: one is drawn first here, ahead of
    // every magenta row, so a rule that skipped the colour would mark it.
    factoryPulse.dashboard = withRows([
      wait({
        reason: '#2061 — held',
        attention_kind: 'held',
        command: 'kew epic unhold launch-fixes 2061',
        issue_num: 2061,
        item_id: '2061'
      }),
      hold(),
      wait({
        reason: '#2056 — failed-terminal (exit 137, host_restart_repeated)',
        attention_kind: 'host_restart_repeated',
        command: 'kew epic retry launch-fixes 2056',
        issue_num: 2056,
        item_id: '2056'
      }),
      wait({
        reason: '#2057 — failed-terminal (exit 137, host_restart_repeated)',
        attention_kind: 'host_restart_repeated',
        command: 'kew epic retry launch-fixes 2057',
        issue_num: 2057,
        item_id: '2057'
      }),
      wait({
        attention_kind: 'merge_decision',
        reason: 'merge decision on PR #208',
        link: { label: 'Open PR #208 to merge', href: 'https://github.com/acme/ledger/pull/208' }
      })
    ]);
    const body = html({ budget: {}, epics: [] });
    const tagged = [...body.matchAll(/<li data-hold-item="(\d+)"(?: data-tour="([a-z-]+)")?/g)].map(
      ([, item, tour]) => [item, tour ?? null]
    );
    // The amber row with a command is drawn before the first magenta one.
    const order = tagged.map(([item]) => item);
    expect(order.indexOf('2061')).toBeLessThan(order.indexOf('2056'));
    expect(tagged).toEqual(
      expect.arrayContaining([
        ['2061', null],
        ['2201', null],
        ['2056', 'hold-command'],
        ['2057', null],
        ['2055', 'merge-decision']
      ])
    );
    expect(tagged).toHaveLength(5);
  });

  it('draws no action at all on a CI-pending hold', () => {
    factoryPulse.dashboard = withRows([
      hold({ reason: 'ci_pending', sentence: 'CI checks are still running' })
    ]);
    const body = html({ budget: {}, epics: [] });
    expect(body).toContain('>held</span>');
    expect(body).toContain('CI checks are still running');
    expect(body).not.toContain('kew epic unhold');
    expect(body).not.toContain('</button>');
  });

  it('draws an unknown reason as held, with the reason the driver wrote', () => {
    factoryPulse.dashboard = withRows([
      hold({ reason: 'moon_phase_wrong: waxing', sentence: 'moon_phase_wrong: waxing' })
    ]);
    const body = html({ budget: {}, epics: [] });
    expect(body).toContain('>held</span>');
    expect(body).toContain('moon_phase_wrong: waxing');
    expect(body).not.toContain('>budget</span>');
  });

  it('says nothing held or blocked only when every source answered', () => {
    factoryPulse.dashboard = EMPTY_DASHBOARD;
    expect(html({ budget: {}, epics: [] })).toContain(HOLDS_EMPTY);
  });

  it('names the source it is missing instead of drawing a quiet column', () => {
    factoryPulse.dashboard = null;
    const body = html({ budget: {}, epics: [] });
    expect(body).toContain('judgment queue not read');
    expect(body).not.toContain(HOLDS_EMPTY);
    expect(body).toContain('count unknown');
  });
});

describe('the mark a row draws', () => {
  // #2280 D14 gave `merge` the needs-you diamond on the premise that nothing
  // is holding a merge row. That is true of the needs-you ENTRY — a merge
  // decision is a person's next move — and false of three `epic_holds`
  // reasons that earn the same category: `base_integration_conflict`,
  // `base_integration_failed` and `merge_gate_touched` are nodes the merge
  // queue itself is holding, and a mark read off the kind alone dressed all
  // three as a human decision. So the mark is a field of the ROW, decided
  // where the row knows which table it came out of.
  const NOW = Date.parse('2026-09-24T14:13:00+00:00');

  /**
   * The `waiting 2h13m` stamp, anchored on the duration: a bare `waiting ` also
   * matches the `in_review awaiting merge` the reason itself carries.
   */
  const WAITING_STAMP = /\bwaiting \d/;

  /** The one row an `epic_holds` payload with this reason composes to. */
  const held = (reason: string) =>
    composeHolds(sources({ dashboard: withRows([hold({ reason })]) }), NOW).rows[0];

  it.each([
    'base_integration_conflict: src/kew/cli.py',
    'base_integration_failed: the integration step exited 1',
    'merge_gate_touched@a1b2c3: .github/workflows/ci.yml'
  ])('draws %s under the held ring: the merge queue IS holding that node', (reason) => {
    const r = held(reason);
    expect(r.kind).toBe('merge');
    expect(r.mark).toBe(HELD_MARK);
    expect(r.mark).not.toBe('needs_you');
  });

  it('draws every epic hold under the held ring, whatever category its reason earns', () => {
    for (const spec of HOLD_SPECS) {
      expect(held(`${spec.prefix}: whatever tail it carries`).mark, spec.prefix).toBe(HELD_MARK);
    }
    // Including the reasons the table has never heard of, and the absent one:
    // a row of that table is held by definition.
    for (const reason of ['moon_phase_wrong: waxing gibbous', '']) {
      expect(held(reason).mark, reason).toBe(HELD_MARK);
    }
  });

  it('draws a needs-you merge decision under the diamond: nothing is holding it', () => {
    const r = composeHolds(
      sources({ dashboard: withRows([wait({ attention_kind: 'merge_decision' })]) }),
      NOW
    ).rows[0];
    expect(r.kind).toBe('merge');
    expect(r.mark).toBe('needs_you');
  });

  it('draws a needs-you work exception under the diamond, not the hold ring', () => {
    // The same mistake in the other direction: `work_exception` was filed
    // under `review` — the category the review MACHINERY's holds earn, a
    // crashed reviewer or a spent round cap — so every non-merge needs-you
    // work row wore the amber hold ring over its own stamp reading `waiting`,
    // and the diamond the colour ration keeps for a human decision was spent
    // on nothing. The server words this wait `manual`, not `held`.
    const r = composeHolds(
      sources({ dashboard: withRows([wait({ attention_kind: 'work_exception' })]) }),
      NOW
    ).rows[0];
    expect(r.kind).toBe('needs');
    expect(r.mark).toBe('needs_you');
    expect(r.mark).not.toBe(HELD_MARK);
    expect(captionText(r.detail)).toMatch(WAITING_STAMP);
  });

  it('draws a node the host restarted under twice under the diamond, off its kind', () => {
    // Nothing holds that node — it failed, and a person decides the retry
    // (#2971). The server stamps its own kind for it, and the mark is read off
    // that kind: the same reason under the generic `held` stamp keeps the
    // ring, so the diamond is never drawn off the reason text.
    const reason = '#2058 — failed-terminal (exit 137, host_restart_repeated)';
    const r = composeHolds(
      sources({
        dashboard: withRows([wait({ reason, attention_kind: 'host_restart_repeated' })])
      }),
      NOW
    ).rows[0];
    expect(r.kind).toBe('needs');
    expect(r.mark).toBe('needs_you');
    expect(captionText(r.detail)).toMatch(WAITING_STAMP);
    expect(setRowOf(wait({ reason, attention_kind: 'held' })).mark).toBe(HELD_MARK);
  });

  it('never rings a set row as held when its own stamp says waiting', () => {
    // The invariant behind both of those, over the whole kind vocabulary this
    // frontend mirrors (`ATTENTION_STATE_WORD`) plus a kind it has never heard
    // of: one row may not say `waiting 2h13m` under the mark that means the
    // machinery is holding it. The two readings come from one field — the
    // server's `attention_kind` — so they cannot be allowed to disagree.
    for (const kind of [...Object.keys(ATTENTION_STATE_WORD), 'moon_phase_wrong']) {
      const r = composeHolds(
        sources({ dashboard: withRows([wait({ attention_kind: kind })]) }),
        NOW
      ).rows[0];
      if (!WAITING_STAMP.test(captionText(r.detail))) continue;
      expect(r.mark, kind).not.toBe(HELD_MARK);
    }
  });

  it('keeps the held ring on a set row whose reason earns `merge`', () => {
    // The other half of the reason-first category (#2280): three of the hold
    // table's reasons are `merge`, and they are nodes the merge QUEUE is
    // holding. A set row carrying one of those is stamped `held` by the server,
    // so the mark is read off that stamp and not off the category word beside
    // it — otherwise the diamond this UI rations to "a person is the next move"
    // would be spent on a node nobody can move yet.
    for (const reason of [
      'base_integration_conflict: src/kew/cli.py',
      'merge_gate_touched@a1b2c3: .github/workflows/ci.yml'
    ]) {
      const r = setRowOf(wait({ reason: `#2055 — ${reason}`, attention_kind: 'held' }));
      expect(r.kind, reason).toBe('merge');
      expect(r.mark, reason).toBe(HELD_MARK);
      expect(captionText(r.detail), reason).toMatch(/\bheld \d/);
    }
  });

  it('gives a blocked node and a standing cap the marks they always had', () => {
    const view = composeHolds(sources({ epics: [snapshot([node()])], budget: CAPPED }), NOW);
    expect(view.rows.map((r) => [r.kind, r.mark])).toEqual([
      ['blocked', 'blocked'],
      ['budget', 'held']
    ]);
  });

  it('draws the row`s own mark in the markup, not a lookup on its kind', () => {
    // The two shapes are `factory/status`'s: the held ring is `--warn`, the
    // needs-you diamond `--accent`. One merge row per render, so the colour in
    // the body is that row's.
    const body = (dashboard: Dashboard) => {
      factoryPulse.dashboard = dashboard;
      const html = render(Holds, { props: { budget: {}, epics: [] } }).body;
      factoryPulse.dashboard = EMPTY_DASHBOARD;
      return html;
    };
    // The word follows the mark (#2539 D19): the machine's merge hold is HELD
    // in amber, the person's merge decision MERGE in magenta.
    const machine = body(withRows([hold({ reason: 'base_integration_conflict: src/a.py' })]));
    expect(machine).toContain('>held</span>');
    expect(machine).not.toContain('>merge</span>');
    expect(machine).toContain('var(--warn)');
    expect(machine).not.toContain('var(--accent)');

    const person = body(withRows([wait({ attention_kind: 'merge_decision' })]));
    expect(person).toContain('>merge</span>');
    expect(person).toContain('var(--accent)');
    expect(person).not.toContain('var(--warn)');
  });
});

describe('a set row says its item once', () => {
  // The needs-you set writes the item's own reference onto every `epic_wait`
  // reason (`_epic_wait_row` in kew/src/kew/service/needs_you.py), and the
  // D14 prefix put the place in front of that sentence — so the row read
  // `launch-fixes / #2055 — #2055 — in_review awaiting merge`, naming the item
  // twice in one line. The head owns the reference where it links the item.
  const NOW = Date.parse('2026-09-24T14:13:00+00:00');
  const only = (over: Partial<DashboardRow> = {}) =>
    captionText(composeHolds(sources({ dashboard: withRows([wait(over)]) }), NOW).rows[0].detail);

  it('names the item once, in the head that links it', () => {
    const said = only();
    expect(said).toBe('launch-fixes / #2055 — in_review awaiting merge · waiting 2h13m');
    expect(said.match(/#2055/g)).toHaveLength(1);
  });

  it('says a tracker id once too, not only a GitHub number', () => {
    const said = only({
      item_id: 'SFD-12',
      issue_num: null,
      reason: 'SFD-12 — in_review awaiting merge'
    });
    expect(said).toBe('launch-fixes / SFD-12 — in_review awaiting merge · waiting 2h13m');
    expect(said.match(/SFD-12/g)).toHaveLength(1);
  });

  it('keeps the reference on a row whose head names only the epic', () => {
    // Nothing else on this row would say which item it is about: the head has
    // no item to link, so the sentence carries its own reference.
    expect(only({ item_id: '', issue_num: 0 })).toBe(
      'launch-fixes — #2055 — in_review awaiting merge · waiting 2h13m'
    );
  });

  it('keeps a reason nobody has words for, less the reference the head said', () => {
    const said = only({ label: '', reason: '#2055 — flux_capacitor_cold: at 1.21GW' });
    expect(said).toBe('launch-fixes / #2055 — flux_capacitor_cold: at 1.21GW · waiting 2h13m');
    expect(said.match(/#2055/g)).toHaveLength(1);
  });

  it('falls back to the row`s type when the entry says nothing at all', () => {
    expect(only({ label: '', reason: '' })).toBe(
      'launch-fixes / #2055 — epic wait · waiting 2h13m'
    );
  });

  it('draws it once in the markup too', () => {
    factoryPulse.dashboard = withRows([wait()]);
    const body = render(Holds, { props: { budget: {}, epics: [] } }).body.replace(
      /<!--.*?-->/g,
      ''
    );
    factoryPulse.dashboard = EMPTY_DASHBOARD;
    expect(body).toContain('in_review awaiting merge');
    expect(body.match(/#2055/g)).toHaveLength(1);
  });
});

describe('against the committed fixtures and the artboard', () => {
  const view = composeHolds(
    sources({ epics: [SNAPSHOT], budget: CAPPED, dashboard: DASHBOARD })
  );
  const html = readFileSync(
    new URL('../../../design/factory-ui/DMonitor.html', import.meta.url),
    'utf8'
  );

  it('reproduces the blocked row DMonitor draws', () => {
    expect(captionText(view.rows[0].detail)).toBe(
      '#2053 needs a design decision: a different user for reviewed code'
    );
    expect(html).toContain('needs a design decision: a different user for reviewed code');
  });

  it('draws no row for the RAISED cap DMonitor draws: that hold is cleared', () => {
    // The artboard's budget row is a cap the operator already raised — the
    // spend it was blocking is running again — and drawing it kept a cleared
    // hold in the "N active" head for as long as the override stood (#2280
    // D14). The committed fixture is exactly that shape, so the column that
    // used to draw it draws nothing for it now.
    for (const part of ['weekly cap reached at ', '$600', 'raised to ', '$1,500', '(operator)']) {
      expect(html, part).toContain(part);
    }
    expect(BUDGET['budget.weekly_usd'].override?.value).toBeGreaterThan(
      BUDGET['budget.weekly_usd'].file_value
    );
    const committed = composeHolds(sources({ epics: [SNAPSHOT], budget: BUDGET }));
    expect(captionText(committed.rows.flatMap((r) => r.detail))).not.toContain('raised to ');
    expect(committed.rows.filter((r) => r.key.startsWith('budget:'))).toEqual([]);
    // A cap LOWERED below the file value is a live restriction and still a row.
    expect(
      captionText(composeHolds(sources({ budget: CAPPED })).rows[0].detail)
    ).toBe(CAPPED_SENTENCE);
  });

  it('carries the server`s command on the row — and none where it serves none', () => {
    // #2280 D14 / AC2: the hold rows said what clears them in prose and gave
    // the reader nothing to run. The command is the server's own — never
    // parsed out of the reason — which cuts both ways: the committed row is a
    // merge decision, and `kew.service.needs_you` serves no command for one
    // (`SAFEST_COMMAND` has no `await_human_decision` entry; the merge is made
    // on the host). So the row carries '' and this column offers no Copy over
    // it, rather than a line an operator would paste and watch fail.
    const merge = view.rows.find((r) => r.key.startsWith('pr_review:'))!;
    expect(DASHBOARD.rows[0].command).toBe('');
    expect(merge.command).toBe('');
    // And the row says WHERE, which is what `rowCaption` alone could not:
    // `launch-fixes / #2055 — …` rather than the sentence on its own.
    expect(captionText(merge.detail).startsWith('launch-fixes / #2055 — ')).toBe(true);
    // A merge decision is a person's move, so it is MERGE under the magenta
    // diamond, not the NEEDS the category column used to fall back to. The
    // mark is read off the ROW, which is the only place that can answer it:
    // `merge` is also three `epic_holds` reasons, and those nodes ARE held.
    expect(merge.kind).toBe('merge');
    expect(merge.mark).toBe('needs_you');
  });

  it('draws a command block, with Copy, for a row that has one', () => {
    // The row shape is `_epic_wait_row`'s (kew/src/kew/service/needs_you.py):
    // `kew.waits`'s `awaiting_continuation` bucket with its template filled,
    // which is a command an operator really can paste. The `<code>` block
    // beside the button is the copy that survives a clipboard this page cannot
    // reach.
    const command = 'kew epic continue launch-fixes 2055';
    factoryPulse.dashboard = withRows([
      wait({
        reason: '#2055 — awaiting preserved-work continuation',
        label: 'awaiting preserved-work continuation',
        attention_kind: 'held',
        primary_action: 'kew epic continue <name> <node>',
        command
      })
    ]);
    const body = plainCommands(render(Holds, { props: { budget: {}, epics: [] } }).body).replace(
      /<!--.*?-->/g,
      ''
    );
    factoryPulse.dashboard = EMPTY_DASHBOARD;
    expect(body).toContain(command);
    expect(body).toContain('<code');
    expect(body).toContain('>Copy</');
  });

  it('draws no command block for the committed merge decision', () => {
    // Narrowed to that row on purpose: the committed set also carries a held
    // row the server DOES compute a command for (below), and the claim here is
    // about the merge decision — no Copy over a line nobody can run.
    factoryPulse.dashboard = {
      ...DASHBOARD,
      rows: DASHBOARD.rows.filter((r) => r.attention_kind === 'merge_decision')
    };
    const body = render(Holds, { props: { budget: {}, epics: [] } }).body.replace(
      /<!--.*?-->/g,
      ''
    );
    factoryPulse.dashboard = EMPTY_DASHBOARD;
    expect(body).toContain('#2055');
    expect(body).not.toContain('<code');
    expect(body).not.toContain('>Copy</');
  });

  it('draws the command block the committed set really carries', () => {
    // The other half of D14/D27, over the payload the browser is served in
    // fixture mode rather than a row written here: `/api/dashboard` carries
    // the go-live round-cap hold, whose `kew epic unhold <epic> <item>` is a
    // command `kew.waits` really computes — so this column's Copy affordance
    // is reachable from committed state, not only from a hand-made row.
    const commanded = DASHBOARD.rows.filter((r) => r.command);
    expect(commanded).toHaveLength(1);
    factoryPulse.dashboard = DASHBOARD;
    const body = plainCommands(render(Holds, { props: { budget: {}, epics: [] } }).body).replace(
      /<!--.*?-->/g,
      ''
    );
    factoryPulse.dashboard = EMPTY_DASHBOARD;
    expect(body).toContain(commanded[0].command!);
    expect(body).toContain('<code');
    expect(body).toContain('>Copy</');
  });

  it('draws no runners row: kew records no such fact', () => {
    // The artboard has one; it is the row this column deliberately omits.
    expect(html).toContain('self-hosted CI paused until');
    expect(captionText(view.rows.flatMap((r) => r.detail))).not.toContain('CI');
  });

  it('draws the artboard`s two rows, plus every entry of the fixture`s set', () => {
    // The artboard's blocked row, then the override and the fixture's own
    // needs-you entries. Nothing in the set is left to the console any more
    // (#2207), so a reader who crossed from the header finds its row here.
    expect(view.rows[0].kind).toBe('blocked');
    expect(view.rows.filter((r) => !r.counted)).toHaveLength(2);
    // Every entry of the set, under the word it earns, in the column's own
    // order — which is `HOLD_ORDER` and not the order the payload serves them
    // in: the committed hold is `review` and the merge decision `merge`, so
    // the hold is drawn above a row the payload lists after it.
    expect(countedHolds(view.rows).map((r) => r.kind)).toEqual(
      DASHBOARD.rows
        .map((r) => holdKindOf(r))
        .sort((a, b) => HOLD_ORDER.indexOf(a) - HOLD_ORDER.indexOf(b))
    );
  });

  it('counts what it drew, with nothing missing behind it', () => {
    expect(view.complete).toBe(true);
    expect(view.meta).toBe(`${view.rows.length} active`);
  });
});

describe('the word follows the state, and every magenta row has a line (#2539)', () => {
  const NOW = Date.parse('2026-09-24T14:13:00+00:00');
  const rowsOf = (...rows: DashboardRow[]) =>
    composeHolds(sources({ dashboard: withRows(rows) }), NOW).rows;

  it('says HELD over every hold, whatever held it (D19)', () => {
    // A round-cap hold read REVIEW in amber: a review word in a hold colour.
    for (const r of rowsOf(hold(), hold({ reason: 'base_integration_conflict: a.py' }))) {
      expect(r.mark).toBe(HELD_MARK);
      expect(holdWord(r)).toBe('held');
    }
    // A journaled hold the hold table files under `review` is still held.
    const journaled = setRowOf(
      wait({ reason: '#2055 — review_crash: boom', attention_kind: 'held' })
    );
    expect(journaled.kind).toBe('review');
    expect(holdWord(journaled)).toBe('held');
  });

  it('says MERGE in magenta over a person`s merge decision (D20)', () => {
    const merge = setRowOf(wait({ attention_kind: 'merge_decision' }));
    expect(merge.mark).toBe('needs_you');
    expect(holdWord(merge)).toBe('merge');
  });

  it('gives a merge row the server link as its action line (D20)', () => {
    const href = 'https://github.com/o/r/pull/2059';
    const merge = setRowOf(
      wait({ attention_kind: 'merge_decision', link: { label: 'Open PR #2059 to merge', href } })
    );
    expect(merge.command).toBe('');
    expect(merge.link).toEqual({ label: 'Open PR #2059 to merge', href, external: true });
  });

  it('makes no link out of a served link or verify link that is not http(s) (C10)', () => {
    const merge = setRowOf(
      wait({
        attention_kind: 'merge_decision',
        link: { label: 'Open PR', href: 'javascript:alert(1)' },
        verify_link: { label: 'Verify', href: 'javascript:alert(2)' }
      })
    );
    expect(merge.link?.href ?? '').not.toMatch(/javascript:/i);
    expect(merge.verifyLink).toBeNull();
  });

  it('never draws a magenta row with nothing under it', () => {
    const rows = rowsOf(
      wait({ attention_kind: 'merge_decision' }),
      wait({ item_id: '2060', issue_num: 2060, attention_kind: 'work_exception' }),
      wait({ item_id: '2061', issue_num: 2061, attention_kind: 'held', command: 'kew epic unhold a 2061' })
    );
    const magenta = rows.filter((r) => r.mark === 'needs_you');
    expect(magenta.length).toBeGreaterThan(0);
    for (const r of magenta) {
      const line = r.command !== '' || r.action !== '' || r.actions.length > 0 || r.link != null;
      expect(line, r.key).toBe(true);
    }
    // A row with a command is not given a second line beside it.
    expect(rows.find((r) => r.command !== '')?.link ?? null).toBeNull();
  });
});
