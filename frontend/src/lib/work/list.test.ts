/**
 * FUI-18 — the Work list model, against the golden fixtures the visual check
 * photographs.
 *
 * The fixtures in `frontend/fixtures/factory/` are the payloads DWork was drawn
 * from, so the list this model builds from them and the ten rows on the
 * artboard are the same ten rows. That makes them the right anchor for the
 * state word: `itemState` is asserted over every `execution.state` ×
 * `exception.state` pair the golden projection contains, with the word the
 * artboard prints beside it.
 */
import { describe, expect, it } from 'vitest';
import dashboardJson from '../../../fixtures/factory/dashboard.json';
import projectionJson from '../../../fixtures/factory/dashboard-work.json';
import goLive from '../../../fixtures/factory/epics-go-live-2026-09.json';
import runsJson from '../../../fixtures/factory/runs.json';
import workViewJson from '../../../fixtures/factory/verbs-work-2055.json';
import heldRoundDueJson from '../../../fixtures/scenarios/dashboard-work-held-round-due.json';
import type { Dashboard, Run, WorkProjection, WorkView } from '$lib/api';
import { KNOWN_EXCEPTION_STATES, rowKey } from '$lib/dashboard';
import { statusWord } from '$lib/epic/render';
import { statusShape } from '$lib/factory/status';
import { rowSentence } from '$lib/needs-you/summary';
import {
  ATTENTION_STATE_WORD,
  attentionState,
  EPIC_MARK,
  EXCEPTION_STATE_WORD,
  EXECUTION_STATE_WORD,
  filterWorkRows,
  itemState,
  judgmentItemIds,
  judgmentRowsNotListed,
  listNotices,
  mergeWorkList,
  needsYouDiamond,
  parseWorkFilter,
  READ_FAILURE_FALLBACK,
  rowDispatchIsNext,
  rowReason,
  searchWorkRows,
  selectedRow,
  stateWord,
  WORK_FILTERS,
  type WorkRow
} from './list';

const dashboard: Dashboard = dashboardJson;
const projection: WorkProjection = projectionJson;
const runs: Run[] = runsJson;
/** The one-item golden view `/api/verbs/work/2055` answers with. */
const golden: WorkView = workViewJson;

const input = { projection, dashboard, runs };

/** The state word DWork prints for each item the golden projection covers.
 *
 * #2053 read `manual` until #2279/D7, which was the third of three treatments
 * one node was drawn with at once — magenta "manual" here, violet BLOCKED on
 * Monitor, truncated on the DAG — none of them counted. A terminal failure now
 * takes the failure word everywhere, and the magenta goes back to meaning a
 * person was asked. */
const ARTBOARD_STATE: Record<string, string> = {
  '2055': 'in review',
  '2053': 'flawed',
  '2047': 'success',
  '2033': 'working',
  '1969': 'success',
  '1803': 'success'
};

function view(overrides: {
  itemId?: string | null;
  execution?: string;
  exception?: string;
}): WorkView {
  const base = projection.views[0];
  return {
    ...base,
    identity: {
      ...base.identity,
      item_id: 'itemId' in overrides ? overrides.itemId! : base.identity.item_id
    },
    execution: { ...base.execution, state: overrides.execution ?? base.execution.state },
    exception: { ...base.exception, state: overrides.exception ?? base.exception.state }
  };
}

function row(rows: WorkRow[], itemId: string): WorkRow {
  const found = rows.find((r) => r.itemId === itemId);
  expect(found, `no row for ${itemId}`).toBeDefined();
  return found!;
}

describe('itemState — the interim state word (FUI-29 deletes it)', () => {
  it('names every execution × exception pair the golden projection holds', () => {
    const pairs = new Set<string>();
    for (const v of projection.views) {
      pairs.add(`${v.execution.state}×${v.exception.state}`);
      const id = v.identity.item_id!;
      expect(itemState(v), `${id} (${v.execution.state} × ${v.exception.state})`).toBe(
        ARTBOARD_STATE[id]
      );
    }
    // The fixture is only worth anchoring to if it spans the states; four
    // distinct pairs over the six views is what DWork was drawn from.
    expect([...pairs].sort()).toEqual([
      'completed×awaiting_human_decision',
      'completed×none',
      'in_flight×in_flight',
      'terminal×terminal_failure'
    ]);
  });

  it('agrees with the single-item view of the same item', () => {
    // `/api/verbs/work/2055` and the projection's row for 2055 are the same
    // composition; one word for both, or the list and the detail disagree.
    expect(golden.identity.item_id).toBe('2055');
    expect(itemState(golden)).toBe(ARTBOARD_STATE['2055']);
  });

  it('gives a word the mark table can draw, for every state in either table', () => {
    const words = [
      ...Object.values(EXECUTION_STATE_WORD),
      ...Object.values(EXCEPTION_STATE_WORD)
    ].filter((w) => w !== '');
    for (const word of words) {
      expect(statusShape(word, 'run').known, word).toBe(true);
    }
  });

  it('decides a word for every exception the projection can raise', () => {
    for (const state of KNOWN_EXCEPTION_STATES) {
      expect(Object.hasOwn(EXCEPTION_STATE_WORD, state), state).toBe(true);
    }
    // And invents none the projection cannot: no third vocabulary in here.
    for (const state of Object.keys(EXCEPTION_STATE_WORD)) {
      expect(KNOWN_EXCEPTION_STATES, state).toContain(state);
    }
  });

  it('lets the exception outrank the raw execution state', () => {
    // A completed run is not "success" while a human's merge decision is out.
    expect(itemState(view({ execution: 'completed', exception: 'awaiting_human_decision' }))).toBe(
      'in review'
    );
    expect(itemState(view({ execution: 'completed', exception: 'none' }))).toBe('success');
    expect(itemState(view({ execution: 'completed', exception: 'fleet_stopped' }))).toBe('held');
  });

  it('reads an item between kew review rounds as kew working, not as a decision', () => {
    // #2328: the last round returned NEEDS_CHANGES or NEEDS_FULL, so the next
    // move is kew's own remediation or its next full round. Cyan, never the
    // magenta rationed for "a person is the next move".
    expect(itemState(view({ execution: 'completed', exception: 'review_remediation_due' }))).toBe(
      'working'
    );
    expect(itemState(view({ execution: 'completed', exception: 'review_round_due' }))).toBe(
      'working'
    );
  });

  it('never paints a declined artifact as a success', () => {
    // #2131 review: `declined` (`_after_decline` in kew/service/work.py) was in
    // neither client table, so a completed run whose PR a human closed without
    // merging fell through to the execution word and read as "success" — the one
    // reading that says "nothing more to do" about abandoned work.
    expect(KNOWN_EXCEPTION_STATES).toContain('declined');
    expect(itemState(view({ execution: 'completed', exception: 'declined' }))).toBe('manual');
  });

  it('falls back honestly when a state is one it has never heard of', () => {
    // An unknown exception degrades to the execution reading...
    expect(itemState(view({ execution: 'terminal', exception: 'sky_fell' }))).toBe('flawed');
    // ...and an unknown execution state is echoed, never guessed as idle.
    expect(itemState(view({ execution: 'hibernating', exception: 'none' }))).toBe('hibernating');
    // A state word that names a prototype member must not find one.
    expect(itemState(view({ execution: 'constructor', exception: 'constructor' }))).toBe(
      'constructor'
    );
  });
});

describe('mergeWorkList', () => {
  it('builds the artboard’s ten rows, the needs-you set first, then newest first', () => {
    // The artboard's ten are the projection's and the ready queue's. `SFD-229`
    // is the eleventh and is an ENTRY row: the committed set's go-live
    // round-cap hold, which neither payload composes, so `entryRows` draws it
    // rather than letting the header count a row nobody can click (#2207). It
    // is in the fixture because it is the one committed state that carries a
    // command — a merge decision serves none — so the Do this block and its
    // Copy are reachable in fixture mode at all.
    // #2539 D15: the set's members (#2055 and SFD-229, the two the header
    // counts) are pinned to the top, then the rest newest first.
    const rows = mergeWorkList(input);
    expect(rows.map((r) => r.number)).toEqual([
      '2055',
      'SFD-229',
      '2060',
      '2058',
      '2054',
      '2053',
      '2050',
      '2047',
      '2033',
      '1969',
      '1803'
    ]);
    expect(row(rows, 'SFD-229').entry?.command).toBe('kew epic unhold go-live-2026-09 SFD-229');
    expect(row(rows, '2055').title).toBe('Review gate lows: comment and hold-back');
    expect(row(rows, '2060').state).toBe('idle');
    expect(row(rows, '2055').state).toBe('in review');
  });

  it('drops duplicates by item_id, keeping the row that has facts', () => {
    const queued = dashboard.glance.ready_queue.map((i) => ({ ...i, item_id: '2055' }));
    const rows = mergeWorkList({
      ...input,
      dashboard: { ...dashboard, glance: { ...dashboard.glance, ready_queue: queued } }
    });
    expect(rows.filter((r) => r.itemId === '2055')).toHaveLength(1);
    expect(row(rows, '2055').view).not.toBeNull();
    expect(row(rows, '2055').state).toBe('in review');
    // Two projection views for one id collapse the same way.
    const twice = { ...projection, views: [projection.views[0], projection.views[0]] };
    expect(mergeWorkList({ ...input, projection: twice }).filter((r) => r.itemId === '2055'))
      .toHaveLength(1);
  });

  it('keeps a Linear key as itself, on both sides of the merge', () => {
    const rows = mergeWorkList({
      projection: { ...projection, views: [view({ itemId: 'SFD-12' })] },
      dashboard: {
        ...dashboard,
        glance: {
          ...dashboard.glance,
          // The 0 sentinel: `item_id` is the only real key for a Linear issue.
          ready_queue: [
            {
              issue_num: 0,
              item_id: 'SFD-40',
              title: 'Rate-limit the tracker reads',
              complexity: 'small',
              route: 'claude-code/claude-opus-5',
              estimate_usd: 2
            }
          ]
        }
      },
      runs: []
    });
    expect(rows.map((r) => r.itemId)).toEqual(['2055', 'SFD-229', 'SFD-12', 'SFD-40']);
    // Bare in the No. column and in prose — `#SFD-12` is not a reference.
    // '2055' and 'SFD-229' too: each is a golden-dashboard needs-you entry the
    // projection here does not compose, so `entryRows` draws the entry's own
    // row rather than letting the header count something with no row (#2207).
    // Both keys ride through as themselves on that path as well.
    expect(rows.map((r) => r.number)).toEqual(['2055', 'SFD-229', 'SFD-12', 'SFD-40']);
    expect(rows.map((r) => r.ref)).toEqual(['#2055', 'SFD-229', 'SFD-12', 'SFD-40']);
    expect(row(rows, 'SFD-40').title).toBe('Rate-limit the tracker reads');
  });

  it('sorts numbered items before keys that cannot be counted', () => {
    const rows = mergeWorkList({
      projection: {
        ...projection,
        views: [view({ itemId: 'SFD-9' }), view({ itemId: '7' }), view({ itemId: 'ABC-1' })]
      },
      dashboard: null,
      runs: null
    });
    expect(rows.map((r) => r.itemId)).toEqual(['7', 'ABC-1', 'SFD-9']);
  });

  it('leaves a title empty rather than inventing one', () => {
    const rows = mergeWorkList({ projection, dashboard: null, runs: null });
    expect(row(rows, '2055').title).toBe('');
  });

  it('drops a view no URL could address', () => {
    const rows = mergeWorkList({
      projection: { ...projection, views: [view({ itemId: null })] },
      dashboard: null,
      runs: null
    });
    expect(rows).toEqual([]);
  });
});

describe('search', () => {
  const rows = mergeWorkList(input);

  it('matches the number and the title, case-insensitively', () => {
    expect(searchWorkRows(rows, '2055').map((r) => r.number)).toEqual(['2055']);
    expect(searchWorkRows(rows, 'REVIEW GATE').map((r) => r.number)).toEqual(['2055', '2047']);
    expect(searchWorkRows(rows, 'review gate').map((r) => r.number)).toEqual(['2055', '2047']);
  });

  it('matches a tracker key whatever case it is typed in', () => {
    const linear = mergeWorkList({
      projection: { ...projection, views: [view({ itemId: 'SFD-12' })] },
      dashboard: null,
      runs: null
    });
    expect(searchWorkRows(linear, 'sfd-12')).toHaveLength(1);
    expect(searchWorkRows(linear, 'SFD')).toHaveLength(1);
  });

  it('treats blank and whitespace as no search at all', () => {
    expect(searchWorkRows(rows, '')).toHaveLength(rows.length);
    expect(searchWorkRows(rows, '   ')).toHaveLength(rows.length);
  });
});

describe('the needs-you filter', () => {
  /** A judgment row of `type` that names no work item at all. */
  const itemless = (type: string): Dashboard['rows'][number] =>
    ({ type, issue_num: null, item_id: null }) as Dashboard['rows'][number];

  /** A judgment row of `type` naming `itemId` — a real, durable id. */
  const naming = (type: string, itemId: string): Dashboard['rows'][number] =>
    ({ type, issue_num: null, item_id: itemId }) as Dashboard['rows'][number];

  /** The id a judgment row names, the way `rowItemId` reads it. */
  const named = (r: Dashboard['rows'][number]): string => {
    const id = r.item_id != null && r.item_id !== '' ? r.item_id : r.issue_num;
    return id == null || id === '' || id === 0 ? '' : String(id);
  };

  /**
   * The invariant the header and the list stand or fall on: EVERY judgment row
   * the header counts is either drawn as a row of the needs-you list or named
   * in the notice. Asserted over the rows themselves rather than as a sum of
   * two lengths — a count identity holds even when a row is in neither.
   */
  /**
   * Every needs-you entry has a row of its own — the invariant #2207 added, and
   * the reason the "no row here" banner is gone. The count is the header's, so
   * this is the two surfaces being held to one number.
   */
  function everyJudgmentRowIsDrawn(dashboard: Dashboard): void {
    const rows = mergeWorkList({ ...input, dashboard });
    const drawn = filterWorkRows(rows, 'needs-you');
    expect(drawn).toHaveLength(dashboard.rows.length);
    expect(judgmentRowsNotListed(dashboard, rows)).toEqual([]);
  }

  it('shows exactly the items the dashboard’s judgment rows name', () => {
    expect([...judgmentItemIds(dashboard)]).toEqual(['2055', 'SFD-229']);
    const rows = mergeWorkList(input);
    expect(filterWorkRows(rows, 'needs-you').map((r) => r.number)).toEqual(['2055', 'SFD-229']);
    expect(filterWorkRows(rows, 'all')).toHaveLength(rows.length);
  });

  it('ignores the 0 sentinel and reads the durable id instead', () => {
    const rows = [
      { type: 'pr_review', issue_num: 0, item_id: 'SFD-12' },
      { type: 'budget_hold', issue_num: 0, item_id: null },
      { type: 'orphan_pr', issue_num: 41 }
    ] as Dashboard['rows'];
    expect([...judgmentItemIds({ ...dashboard, rows })]).toEqual(['SFD-12', '41']);
  });

  it('draws a row for an entry that names no work item at all', () => {
    // #2131 found it, #2207 fixed it: a `stalled_epic` entry is built with
    // `issue_num` and `item_id` at their DTO defaults, so it names no item and a
    // list keyed on item ids had no row for it. The header counted it anyway, so
    // `?filter=needs-you` rendered nothing under "3 needs you" and all the
    // screen could do was print a banner about it. Now the entry gets a row.
    const rows = [itemless('stalled_epic'), itemless('orphan_pr'), itemless('orphan_pr')];
    const partial = { ...dashboard, rows };
    expect([...judgmentItemIds(partial)]).toEqual([]);
    const merged = mergeWorkList({ ...input, dashboard: partial });
    everyJudgmentRowIsDrawn(partial);
    expect(filterWorkRows(merged, 'needs-you')).toHaveLength(3);
    expect(listNotices({ ...input, dashboard: partial }, merged)).toEqual([]);
    // Its identity is the entry's own key, which is what keeps two entries of
    // one type apart and matches how the console and Monitor track them.
    const stalled = merged.find((r) => r.entry?.type === 'stalled_epic');
    expect(stalled?.itemId).toBe(rowKey(rows[0]));
  });

  it('draws a row for an entry whose item no payload listed', () => {
    // The entry DOES name an item — a `budget_hold` on an epic node with no
    // delivery PR and no `ready` label — and the id is perfectly good. Neither
    // payload holds a row keyed on it, so the row is the entry's, keyed on the
    // id it named, which is what a later projection poll merges onto.
    const rows = [naming('budget_hold', '9042')];
    const partial = { ...dashboard, rows };
    expect([...judgmentItemIds(partial)]).toEqual(['9042']);
    const merged = mergeWorkList({ ...input, dashboard: partial });
    const drawn = merged.find((r) => r.itemId === '9042');
    expect(drawn?.needsYou).toBe(true);
    // No `attention_kind` on this entry, so nothing more specific is known —
    // which is exactly what `manual` says (`ATTENTION_STATE_WORD`).
    expect(drawn?.state).toBe('manual');
    expect(drawn?.title).toBe(rowSentence(rows[0]));
    everyJudgmentRowIsDrawn(partial);
    expect(listNotices({ ...input, dashboard: partial }, merged)).toEqual([]);
  });

  it('prints the entry`s own sentence where a title would go, and selects itself', () => {
    const rows = [itemless('stalled_epic')];
    const merged = mergeWorkList({ ...input, dashboard: { ...dashboard, rows } });
    const row = merged.find((r) => r.entry !== null);
    expect(row?.title).toBe(rowSentence(rows[0]));
    // #2246: an entry row's identity is its `rowKey`, which `selectedRow`
    // matches — so `?item=` selects it in place rather than selecting nothing,
    // and the row no longer sends the reader off the Work screen.
    expect(row?.href).toBe(`/work?item=${encodeURIComponent(rowKey(rows[0]))}`);
    expect(selectedRow(merged, row!.itemId)).toBe(row);
  });

  it('gives every row an in-app href, over the live-shaped fixture', () => {
    // The acceptance test for #2246's P1: five of the seven rows on the live
    // Work screen were needs-you entries, and clicking any of them left the
    // app — for a GitHub PR, a GitHub issue, or Linear. Now nothing does.
    const live = mergeWorkList(input);
    expect(live.length).toBeGreaterThan(0);
    for (const r of live) {
      expect(r.external, r.itemId).toBe(false);
      expect(r.href.startsWith('/work?'), r.href).toBe(true);
      expect(r.href).not.toMatch(/github\.com|linear\.app|^https?:/);
    }
  });

  it('reads an entry row`s state word off its attention kind', () => {
    // Not `manual` for everything: a round-cap hold, a merge decision and a
    // kind nobody has heard of are three different answers to "what is this
    // item waiting for?", and before #2246 the State column said `manual`
    // about all three.
    const kinds: [string, string][] = [
      ['held', 'held'],
      ['budget_exhausted', 'held'],
      ['publication_held', 'held'],
      ['sandbox_refused', 'held'],
      ['approval', 'held'],
      ['merge_ready', 'in review'],
      ['merge_decision', 'in review'],
      ['work_exception', 'manual'],
      ['epic_complete', 'manual'],
      // Open vocabulary (`AttentionKind`'s docstring) and a `kew serve` older
      // than #2244, which serves no kind at all.
      ['invented_tomorrow', 'manual'],
      ['', 'manual'],
      ['constructor', 'manual']
    ];
    for (const [kind, word] of kinds) {
      expect(attentionState(kind), kind).toBe(word);
      const entry = naming('budget_hold', '9042');
      const merged = mergeWorkList({
        ...input,
        dashboard: { ...dashboard, rows: [{ ...entry, attention_kind: kind }] }
      });
      expect(merged.find((r) => r.itemId === '9042')?.state, kind).toBe(word);
    }
    expect(attentionState(null)).toBe('manual');
    expect(attentionState(undefined)).toBe('manual');
    // Every word the table can answer with is a word the shared status
    // vocabulary draws a mark for — an unknown one would render as a muted
    // ring beside its own name, which is not what any of these means.
    for (const word of Object.values(ATTENTION_STATE_WORD)) {
      expect(statusShape(word, 'run').known, word).toBe(true);
    }
  });

  it('says nothing at all when every entry already has a row', () => {
    // The golden dashboard's one entry names its item AND the list holds it.
    expect(judgmentRowsNotListed(dashboard, mergeWorkList(input))).toEqual([]);
    expect(listNotices(input)).toEqual([]);
    everyJudgmentRowIsDrawn(dashboard);
  });

  it('reads an unknown ?filter= as the unfiltered list', () => {
    for (const f of WORK_FILTERS) expect(parseWorkFilter(f)).toBe(f);
    expect(parseWorkFilter('needs-me')).toBe('all');
    expect(parseWorkFilter(null)).toBe('all');
  });
});

describe('notices', () => {
  it('says nothing when every payload was read', () => {
    expect(listNotices(input)).toEqual([]);
  });

  it('names an unreadable projection, and still lists the idle issues', () => {
    const broken = { ...input, projection: null };
    const notices = listNotices(broken);
    // The golden dashboard's needs-you entries name 2055, which only the
    // projection knows about, and SFD-229, which neither payload composes.
    // With the projection gone both still get their own rows, so the filter is
    // not empty under a header counting 2 — the projection notice is the only
    // fact left owed.
    expect(notices.map((n) => n.key)).toEqual(['projection']);
    expect(notices[0].text).toContain('work projection could not be read');
    // Never an empty list: the ready queue is still a real reading, and the
    // entries' rows ride with it.
    expect(mergeWorkList(broken).map((r) => r.number)).toEqual([
      '2055',
      'SFD-229',
      '2060',
      '2058',
      '2054',
      '2050'
    ]);
  });

  it('names the mark beside each notice for what that notice is about', () => {
    // A notice that is not about a failed read must not be announced as an
    // unreadable payload — the mark is the only thing assistive tech hears
    // before the text. Every notice left IS about a failed read: the one that
    // was not ("no row here") is gone, because the rows are drawn now (#2207).
    const read = listNotices({ ...input, projection: null, dashboard: null });
    expect(read.map((n) => n.label)).toEqual(['unreadable', 'unreadable']);
    // A needs-you entry with no item raises no notice at all, having a row.
    expect(
      listNotices({
        ...input,
        dashboard: { ...dashboard, rows: [{ type: 'stalled_epic' } as Dashboard['rows'][number]] }
      })
    ).toEqual([]);
    // And every notice names one, so the renderer never needs a fallback.
    for (const notice of read) expect(notice.label).not.toBe('');
  });

  it('names the items the server itself could not compose', () => {
    const notices = listNotices({
      ...input,
      projection: { ...projection, unreadable: ['2053', 'SFD-12'] }
    });
    expect(notices.map((n) => n.key)).toEqual(['unreadable']);
    expect(notices[0].text).toBe('2 items could not be read: #2053, SFD-12.');
  });

  it('names an unreadable ready queue and an incomplete judgment queue', () => {
    const notices = listNotices({
      ...input,
      dashboard: { ...dashboard, unavailable: ['ready_queue', 'pr_reviews'] }
    });
    expect(notices.map((n) => n.key)).toEqual(['ready_queue', 'judgment']);
    expect(notices[1].text).toContain('pr_reviews');
  });

  it('names a dashboard that could not be read at all, once', () => {
    const notices = listNotices({ ...input, dashboard: null });
    expect(notices.map((n) => n.key)).toEqual(['dashboard']);
  });
});

describe('selection', () => {
  const rows = mergeWorkList(input);

  it('selects the named row', () => {
    expect(selectedRow(rows, '2055')?.number).toBe('2055');
  });

  it('selects nothing for an absent, blank or missing ?item=', () => {
    expect(selectedRow(rows, '9999')).toBeNull();
    expect(selectedRow(rows, '')).toBeNull();
    expect(selectedRow(rows, null)).toBeNull();
  });
});

describe('stateWord — the STATE column’s one vocabulary (#2281 D13)', () => {
  it('says what the DAG says about the same meaning', () => {
    // The screens drifted: the list said "success / idle / manual" where the
    // DAG said "done / todo" about nodes in the same state. The mark table is
    // the single source (DESIGN.md), so the WORD is now the meaning's word —
    // `statusWord`, which is what every DAG node prints.
    expect(stateWord('success')).toBe(statusWord('done'));
    expect(stateWord('flawed')).toBe(statusWord('failed'));
    expect(stateWord('manual')).toBe(statusWord('needs_you'));
    expect(stateWord('manual')).toBe('needs you');
    expect(stateWord('in review')).toBe('in review');
    expect(stateWord('idle')).toBe('idle');
    expect(stateWord('working')).toBe('working');
    expect(stateWord('held')).toBe('held');
  });

  it('invents nothing: every word the three tables can serve is its meaning’s', () => {
    const words = [
      ...Object.values(EXECUTION_STATE_WORD),
      ...Object.values(EXCEPTION_STATE_WORD),
      ...Object.values(ATTENTION_STATE_WORD)
    ].filter((w) => w !== '');
    expect(words.length).toBeGreaterThan(10);
    for (const word of words) {
      const mark = statusShape(word, 'run');
      expect(mark.known, word).toBe(true);
      expect(stateWord(word), word).toBe(statusWord(mark.meaning));
    }
  });

  it('keeps the mark and the word reading the same vocabulary', () => {
    // `queued` is the one word the two vocabularies spell the same and mean
    // differently, and a row of this list is a RUN reading: the dispatcher's
    // turn, never a person's. A word taken without the vocabulary would say
    // "queued" beside the epic's unarmed-node mark.
    expect(stateWord('queued')).toBe(statusWord('pending'));
    expect(stateWord('queued')).not.toBe(statusWord('queued'));
  });

  it('echoes a state it has never heard of rather than calling it todo', () => {
    // Forward compatibility, the same rule `itemState` and `statusShape` keep:
    // a server that grows a state renders honestly. Printing the fallback
    // meaning's word would claim a reading nobody served.
    expect(stateWord('hibernating')).toBe('hibernating');
    expect(stateWord('sky_fell')).toBe('sky fell');
    expect(stateWord('')).toBe('todo');
    // A state word that names a prototype member must not find one.
    expect(stateWord('constructor')).toBe('constructor');
  });
});

describe('the needs-you set on the list (#2539, D15)', () => {
  it('marks every row the header counts with the diamond, and pins them first', () => {
    const rows = mergeWorkList(input);
    const counted = new Set(dashboard.rows.map((r) => r.item_id));
    const members = rows.filter((r) => r.needsYou);
    // Every member the set names, and nothing else.
    expect(new Set(members.map((r) => r.itemId))).toEqual(counted);
    // First, as one block.
    expect(rows.slice(0, members.length).every((r) => r.needsYou)).toBe(true);
    // #2055 is a merge decision whose state is the cyan IN REVIEW ring: the
    // diamond is drawn beside it, because that ring alone says nobody is waited on.
    const merge = row(rows, '2055');
    expect(statusShape(merge.state, 'run').meaning).not.toBe('needs_you');
    expect(needsYouDiamond(merge)).toBe(true);
    // A non-member never wears it.
    for (const r of rows.filter((x) => !x.needsYou)) expect(needsYouDiamond(r)).toBe(false);
  });

  it('gives no second diamond to a state that already is one', () => {
    expect(needsYouDiamond({ needsYou: true, state: 'manual' })).toBe(false);
    expect(needsYouDiamond({ needsYou: true, state: 'held' })).toBe(true);
  });
});

describe('a held item reads HELD whatever its review exception says (#2842)', () => {
  // The scenario composes SFD-229 between two review rounds — `review_round_due`,
  // "kew re-reviews next", which alone reads WORKING — while the committed set
  // names the go-live hold on it. The re-review cannot happen until a person
  // lifts the hold, so the row is the hold's, and WORKING beside the diamond was
  // live #2814's reading.
  const heldRoundDue: WorkProjection = heldRoundDueJson;
  const rows = mergeWorkList({ projection: heldRoundDue, dashboard, runs });

  it('draws the composed view, not an entry row', () => {
    const held = row(rows, 'SFD-229');
    expect(held.view?.exception.state).toBe('review_round_due');
    expect(itemState(held.view!)).toBe('working');
    expect(held.entry).toBeNull();
  });

  it('reads held, with the diamond beside it, and never working', () => {
    const held = row(rows, 'SFD-229');
    expect(held.state).toBe('held');
    expect(stateWord(held.state)).not.toBe(stateWord('working'));
    expect(needsYouDiamond(held)).toBe(true);
  });

  it('leaves a merge decision on its own word', () => {
    // #2055's entry is a merge decision, not a hold: IN REVIEW plus the diamond.
    const rowsWithMerge = mergeWorkList(input);
    expect(row(rowsWithMerge, '2055').state).toBe('in review');
  });
});

describe('where dispatch is the next move (#2785)', () => {
  const rows = mergeWorkList(input);
  const byId = (id: string) => rows.find((r) => r.itemId === id)!;

  it('carries the ready queue tier on a queued row, and null on any other', () => {
    expect(byId('2054').tier).toBe('medium');
    expect(byId('2055').tier).toBeNull();
    expect(byId('SFD-229').tier).toBeNull();
  });

  it('is a queued item, and never an item in review or held', () => {
    expect(rowDispatchIsNext(byId('2060'))).toBe(true);
    expect(rowDispatchIsNext(byId('2055'))).toBe(false);
    expect(rowDispatchIsNext(byId('SFD-229'))).toBe(false);
  });

  it('is never a needs-you entry row keyed on its entry', () => {
    const stalled = {
      ...dashboard.rows[0],
      type: 'stalled_epic',
      item_id: null,
      issue_num: null,
      pr_number: null,
      epic_ref: 'go-live-2026-09'
    };
    const listed = mergeWorkList({ ...input, dashboard: { ...dashboard, rows: [stalled] } });
    const row = listed.find((r) => r.itemId.startsWith('stalled_epic:'))!;
    expect(row.entry).not.toBeNull();
    expect(rowDispatchIsNext(row)).toBe(false);
  });
});

describe('a row is titled by its item, with its wait on the line under it (#2786)', () => {
  const rows = mergeWorkList(input);
  const byId = (id: string) => rows.find((r) => r.itemId === id)!;
  const epicNode = (id: string): string => {
    const nodes = goLive.snapshot.nodes as { id: string; title: string }[];
    return nodes.find((n) => n.id === id)!.title;
  };

  it('titles SFD-229 as its epic card does, and keeps the hold as its reason', () => {
    const row = byId('SFD-229');
    expect(row.title).toBe(epicNode('SFD-229'));
    expect(rowReason(row)).toBe(rowSentence(row.attention!));
    expect(rowReason(row)).toContain('the review round cap was reached');
  });

  /** 2055 as a merge with no failed verify at its head: today's sentence, no verify link. */
  const plainMerge = (row: WorkRow): WorkRow => ({
    ...row,
    attention: {
      ...row.attention!,
      sentence: 'PR #2059 is open — the merge decision is yours',
      verify_link: null
    }
  });

  it('drops the merge-decision reason its State cell already says, and a quiet row has none', () => {
    // #2848 — 2055's State cell is IN REVIEW beside the needs-you diamond, so
    // "PR #2059 is open — the merge decision is yours" only repeated it.
    const row = plainMerge(byId('2055'));
    expect(row.attention!.attention_kind).toBe('merge_decision');
    expect(stateWord(row.state)).toBe('in review');
    expect(needsYouDiamond(row)).toBe(true);
    expect(rowReason(row)).toBe('');
    expect(rowReason(byId('2060'))).toBe('');
  });

  it('drops it when the PR is served as a URL with no pr_number (#2893)', () => {
    // Live merge rows carry the PR as a URL and `pr_number: null`.
    const row = plainMerge(byId('2055'));
    const pr = 'https://github.com/o/r/pull/2059';
    const live = { ...row, attention: { ...row.attention!, pr_number: null, url: pr } };
    expect(rowReason(live)).toBe('');
    // An issue URL names no pull request, so the sentence stays.
    const issue = { ...live, attention: { ...live.attention, url: 'https://github.com/o/r/issues/2055' } };
    expect(rowReason(issue)).toBe('PR #2059 is open — the merge decision is yours');
  });

  it('reads a remote read failure as kew’s pending read, not a needs-you, and says what it could not read (#2893)', () => {
    // Live #2870: `remote_unavailable` with no needs-you entry (the server's
    // set no longer counts it) drew the magenta diamond and no reason at all.
    const base = view({ itemId: '2870', execution: 'completed', exception: 'remote_unavailable' });
    const failed: WorkView = {
      ...base,
      exception: { ...base.exception, detail: "the source item's state could not be read" }
    };
    const listed = mergeWorkList({ ...input, projection: { ...projection, views: [failed] } });
    const row = listed.find((r) => r.itemId === '2870')!;
    expect(row.needsYou).toBe(false);
    expect(row.attention).toBeNull();
    expect(statusShape(row.state, 'run').meaning).not.toBe('needs_you');
    expect(statusShape(row.state, 'run').meaning).toBe('pending');
    expect(needsYouDiamond(row)).toBe(false);
    expect(filterWorkRows(listed, 'needs-you').map((r) => r.itemId)).not.toContain('2870');
    expect(rowReason(row)).toBe("the source item's state could not be read");
    // A detail the server left empty still names the failure.
    const bare = { ...row, view: { ...failed, exception: { ...failed.exception, detail: ' ' } } };
    expect(rowReason(bare)).toBe(READ_FAILURE_FALLBACK);
    // Any other quiet exception keeps the line empty.
    expect(rowReason({ ...row, view: view({ exception: 'in_flight' }) })).toBe('');
  });

  it('keeps a merge-decision reason when the State cell does not say in review', () => {
    const row = plainMerge(byId('2055'));
    expect(rowReason({ ...row, state: 'held' })).toBe('PR #2059 is open — the merge decision is yours');
    expect(rowReason({ ...row, needsYou: false })).toBe('PR #2059 is open — the merge decision is yours');
  });

  it('leads with a merge verify that failed at the head, even beside IN REVIEW (#3460)', () => {
    // The State cell says a merge is yours; it does not say kew just saw this
    // head fail, so that sentence is never dropped as a repeat.
    const row = byId('2055');
    expect(stateWord(row.state)).toBe('in review');
    expect(needsYouDiamond(row)).toBe(true);
    expect(rowReason(row)).toBe(
      "Merge verify failed at 3670e43f: 2 of main's tests fail on this PR — the merge decision is yours"
    );
  });

  it('prints the sentence once, as the title, when nothing names the item', () => {
    const entry = { ...dashboard.rows[1], title: '' };
    const listed = mergeWorkList({ ...input, dashboard: { ...dashboard, rows: [entry] } });
    const row = listed.find((r) => r.itemId === 'SFD-229')!;
    expect(row.title).toBe(rowSentence(entry));
    expect(rowReason(row)).toBe('');
  });

  it('marks a stalled epic short in the No. column and titles it by its ref', () => {
    const stalled = {
      ...dashboard.rows[0],
      type: 'stalled_epic',
      item_id: null,
      issue_num: null,
      pr_number: null,
      // As `_stalled_rows` serves it: no attention kind and no PR URL,
      // not the merge row's it is spread from.
      attention_kind: '',
      url: null,
      epic_ref: 'supervised-daemon',
      sentence: 'the driver is gone — no tick in 6d 8h'
    };
    const listed = mergeWorkList({ ...input, dashboard: { ...dashboard, rows: [stalled] } });
    const row = listed.find((r) => r.itemId.startsWith('stalled_epic:'))!;
    expect([row.number, row.title, rowReason(row)]).toEqual([
      EPIC_MARK,
      'supervised-daemon',
      'the driver is gone — no tick in 6d 8h'
    ]);
    // The header crumb and accessible names still name the epic, not the mark.
    expect(row.ref).toBe('supervised-daemon');
  });
});
