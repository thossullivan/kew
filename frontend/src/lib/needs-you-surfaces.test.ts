/**
 * One needs-you set, four surfaces that read it (#2207).
 *
 * The owner's screenshots of 2026-09-29 caught the four disagreeing: the header
 * said 16 needs you, Monitor's holds list said 4 active, the go-live epic's
 * legend said `needs you 0` over six nodes each waiting on a human, and Work
 * showed one idle issue under a banner saying the counted items "have no row
 * here". Four readings of one English phrase.
 *
 * The server now composes the set once (`needs_you_set` in
 * kew/src/kew/service/needs_you.py; `kew/tests/test_needs_you_parity.py` holds
 * the payload each surface reads to it). This module is the client half: the
 * four pure models are given ONE payload and held to one number.
 *
 * There is no jsdom in this suite and no way to mount a component (see
 * `urls.test.ts`), so the Work assertion is over `mergeWorkList` — the model the
 * `{#each shown as row}` in `(factory)/work/+page.svelte` renders one `<a>` per
 * — plus a read of that markup for the two attributes an entry row needs.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import type { Dashboard, DashboardRow, EpicNode } from '$lib/api';
import dashboardJson from '../../fixtures/factory/dashboard.json';
import { epicNeedsYou, needsYou, needsYouReading } from '$lib/epic/attention';
import { rowKey } from '$lib/dashboard';
import { composeHolds, countedHolds, drawnHolds, holdsMeta } from '$lib/monitor/holds';
import { captionText, needsYouReadout } from '$lib/monitor/readouts';
import { rowSentence } from '$lib/needs-you/summary';
import { headerCounts } from '$lib/shell/header-state';
import { entryDetail } from '$lib/work/entry-detail';
import { EPIC_MARK, filterWorkRows, mergeWorkList, rowReason } from '$lib/work/list';

const EPIC = 'go-live';

/** A fixed clock, so the "held …" line in a detail block is reproducible. */
const NOW_MS = Date.parse('2026-09-30T09:00:00+00:00');

/** One entry per kind of wait the set can name, as the server serves them. */
const ENTRIES: DashboardRow[] = [
  {
    type: 'approval_hold',
    consequence: 'money',
    age: '',
    reason: 'awaiting approval — over the per-issue cap',
    primary_action: 'approve',
    issue_num: 2020,
    item_id: '2020',
    estimate_usd: 12.5,
    epic_ref: null,
    url: null,
    pr_number: null
  },
  {
    type: 'budget_hold',
    consequence: 'money',
    age: '2026-09-28T09:00:00+00:00',
    reason: 'budget_preflight: epic cap reached at $40.00',
    primary_action: 'approve',
    issue_num: 2005,
    item_id: '2005',
    estimate_usd: null,
    epic_ref: EPIC,
    url: null,
    pr_number: null
  },
  {
    type: 'stalled_epic',
    consequence: 'epic',
    age: '2026-09-28T09:00:00+00:00',
    reason: 'driver gone — launch-fixes — no tick in 6d 8h',
    primary_action: 'resume',
    issue_num: null,
    item_id: null,
    estimate_usd: null,
    epic_ref: 'launch-fixes',
    url: null,
    pr_number: null
  },
  {
    type: 'pr_review',
    consequence: 'review',
    age: '2026-09-28T09:00:00+00:00',
    reason: 'PR open for #2001 — awaiting review',
    primary_action: 'review',
    issue_num: 2001,
    item_id: '2001',
    estimate_usd: null,
    epic_ref: EPIC,
    url: 'https://example.invalid/pr/1',
    pr_number: null
  },
  {
    type: 'epic_wait',
    consequence: 'wait',
    age: '2026-09-28T09:00:00+00:00',
    reason: '#2002 — awaiting preserved-work continuation',
    primary_action: 'kew epic continue <name> <node>',
    issue_num: 2002,
    item_id: '2002',
    estimate_usd: null,
    epic_ref: EPIC,
    url: null,
    pr_number: null
  },
  {
    type: 'epic_wait',
    consequence: 'wait',
    age: '2026-09-28T09:00:00+00:00',
    reason: '#2003 — awaiting host publication',
    primary_action: 'restore GitHub access or open the preserved branch PR',
    issue_num: 2003,
    item_id: '2003',
    estimate_usd: null,
    epic_ref: EPIC,
    url: null,
    pr_number: null
  },
  {
    type: 'orphan_pr',
    consequence: 'hygiene',
    age: '2026-09-28T09:00:00+00:00',
    reason: 'orphan PR — branch has no run',
    primary_action: 'ack',
    issue_num: null,
    item_id: null,
    estimate_usd: null,
    epic_ref: null,
    url: 'https://example.invalid/pr/77',
    pr_number: 77
  }
];

/** The fixture dashboard, serving the set above and an empty ready queue. */
const dashboard: Dashboard = {
  ...(dashboardJson as unknown as Dashboard),
  rows: ENTRIES,
  unavailable: [],
  glance: {
    ...(dashboardJson as unknown as Dashboard).glance,
    ready_queue: [],
    recent_completions: [],
    queue_candidates: []
  }
};

/** The epic's nodes, joined as `compose_node_attention` joins them. */
function node(id: string, attention: EpicNode['attention']): EpicNode {
  return {
    id,
    title: `node ${id}`,
    status: 'held',
    status_since: '2026-09-28T09:00:00+00:00',
    url: null,
    pr_url: null,
    external: false,
    stuck: null,
    attention,
    blocked_reason: null
  } as EpicNode;
}

/** The go-live epic's four node-addressable entries, as its snapshot carries them. */
const NODES: EpicNode[] = [
  node('2005', { kind: 'budget_exhausted', reason: 'held on budget', url: null, action: 'raise' }),
  node('2001', { kind: 'merge_decision', reason: 'awaiting review', url: null, action: '' }),
  node('2002', { kind: 'held', reason: 'awaiting continuation', url: null, action: 'continue' }),
  node('2003', { kind: 'publication_held', reason: 'awaiting publication', url: null, action: 'x' }),
  node('2010', null),
  node('2011', null)
];

describe('the four surfaces read one needs-you set', () => {
  it('counts the same number on the header, Monitor and Work', () => {
    const header = headerCounts(dashboard, []).needsYou;
    // What `Holds.svelte` actually draws and actually prints in its head:
    // `drawnHolds(composed, dismissed)`, whose `meta` is `holdsMeta` over the
    // DRAWN list. Asserting `countedHolds(...).length` instead would hold the
    // surface to a number nothing on it renders — `HoldRow.counted` has no
    // caller outside this suite, by design (see its doc comment).
    // `Source<T>` is `T | Unavailable`, so a successful reading IS the value.
    const monitor = drawnHolds(composeHolds({ epics: [], budget: {}, dashboard }), {});
    const work = filterWorkRows(
      mergeWorkList({ projection: null, dashboard, runs: [] }),
      'needs-you'
    ).length;

    expect(header).toBe(ENTRIES.length);
    expect(monitor.meta).toBe(holdsMeta(ENTRIES.length, true));
    expect(work).toBe(ENTRIES.length);
    // And the rows under that head are the set's own entries, one for one —
    // not a filtered subset of them, which is what had the head saying 4 under
    // a header saying 16. The column draws them in its own `HOLD_ORDER`
    // (decidable kinds first), so the comparison is of the sets, not the order.
    expect(monitor.rows.map((row) => row.key).sort()).toEqual(ENTRIES.map(rowKey).sort());
    // This fixture gives the column none of the two readings it takes for
    // itself (no blocked node, no cap override), which is what makes its head
    // and the header's count the same number here; `counted` is the column's
    // own marker for which of its rows the header counted.
    expect(countedHolds(monitor.rows)).toHaveLength(ENTRIES.length);
  });

  it('says one row the same way in both cells of Monitor', () => {
    // #2245. The Needs you caption and the Holds column print the same top row
    // two cells apart, and a budget hold is the row where two vocabularies
    // disagreed: `kew.waits` says "held on budget", the console's own table
    // "held on the dispatch budget". Since #2539 neither cell words it: both
    // print the sentence the server wrote, so there is one vocabulary left.
    const held = ENTRIES.find((entry) => entry.type === 'budget_hold')!;
    const sentence = 'held on budget: epic cap reached at $40.00';
    const served: Dashboard = { ...dashboard, rows: [{ ...held, label: 'held on budget', sentence }] };
    const column = drawnHolds(composeHolds({ epics: [], budget: {}, dashboard: served }), {}).rows;

    expect(captionText(needsYouReadout(served).caption)).toBe(sentence);
    expect(column).toHaveLength(1);
    expect(captionText(column[0].detail)).toContain(sentence);
  });

  it('sums the per-epic buckets to the number the header counts', () => {
    // #2279/D6. The header said 1 over legends saying 1 and 2 because each
    // surface filtered for itself; now the SERVER filters once
    // (`needs_you_by_epic`) and the legends and the Epics list read a bucket
    // of it. So the three readings are arithmetic on one set: the buckets sum
    // to every entry that names an epic, and the header's count is that plus
    // the fleet-wide ones (an approval on a loose issue, an orphan PR) —
    // which belong to no drain and so to no bucket.
    const byEpic: Record<string, number> = {};
    for (const entry of ENTRIES) {
      if (entry.epic_ref) byEpic[entry.epic_ref] = (byEpic[entry.epic_ref] ?? 0) + 1;
    }
    const served: Dashboard = { ...dashboard, needs_you_by_epic: byEpic };
    const fleet = ENTRIES.filter((e) => !e.epic_ref).length;
    const buckets = Object.keys(byEpic).map((ref) => epicNeedsYou(served, ref).count);

    expect(buckets.reduce((a, b) => a + b, 0) + fleet).toBe(
      headerCounts(served, []).needsYou
    );
    // And each bucket IS that epic's slice of the rows the header counted.
    for (const ref of Object.keys(byEpic)) {
      expect(epicNeedsYou(served, ref).count).toBe(
        ENTRIES.filter((e) => e.epic_ref === ref).length
      );
    }
  });

  it('counts the epic`s own slice on its legend', () => {
    // The legend is the set narrowed to one epic's nodes, and the join behind it
    // reads the same `kew.waits` table the set does — which is what took it from
    // `needs you 0` over six waiting nodes to the number those nodes earn.
    const reading = needsYouReading(NODES, dashboard.unavailable ?? []);
    expect(reading).toEqual({ count: 4, known: true, label: '4' });
    expect(reading.count).toBe(ENTRIES.filter((e) => e.epic_ref === EPIC).length);
    expect(NODES.filter(needsYou)).toHaveLength(4);
  });
});

describe('Work renders a row per needs-you entry', () => {
  const rows = mergeWorkList({ projection: null, dashboard, runs: [] });

  it('draws one row for every entry, whatever the entry names', () => {
    // Including the two that name no work item at all — a stalled epic and an
    // orphan PR. Those are what the banner used to apologise for.
    expect(filterWorkRows(rows, 'needs-you')).toHaveLength(ENTRIES.length);
    const titles = filterWorkRows(rows, 'needs-you').map((r) => r.title);
    // The sentence is the shared reading — the ONE reading of a needs-you row
    // (#2245). Before #2246 the row printed `entry.reason` verbatim, so the
    // Issue column read `budget_preflight: epic cap reached at $40.00` beside a
    // Holds column saying "held on the dispatch budget" about the same row. It
    // is `rowSentence` and not `rowSummary` because the No. column beside it
    // already prints the reference the set writes onto an `epic_wait` reason.
    //
    // #2786 D14 — a row is titled by its ITEM, and the sentence is the quiet
    // line under that title: a stalled epic's title is its ref, and its wait
    // reads exactly the sentence. No entry's sentence is lost between the two.
    const readings = filterWorkRows(rows, 'needs-you').map((r) => [r.title, rowReason(r)]);
    for (const entry of ENTRIES)
      expect(readings.some((lines) => lines.includes(rowSentence(entry))), entry.type).toBe(true);
    const stalled = rows.find((r) => r.entry?.type === 'stalled_epic')!;
    expect([stalled.number, stalled.title, rowReason(stalled)]).toEqual([
      EPIC_MARK,
      'launch-fixes',
      rowSentence(ENTRIES.find((e) => e.type === 'stalled_epic')!)
    ]);
  });

  it('gives each entry row a key of its own, so two cannot collide', () => {
    const keys = rows.map((r) => r.itemId);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it('selects a row with no work item in place, and never links out', () => {
    // #2246. These two rows used to be the exceptions: a stalled epic went to
    // the epic page and an orphan PR to the forge, so clicking either left the
    // Work screen. Their identity is the entry's own `rowKey`, which
    // `selectedRow` matches, so `?item=` selects them like any other row — and
    // where the entry lives is a secondary link in the detail pane.
    const stalled = rows.find((r) => r.entry?.type === 'stalled_epic');
    expect(stalled?.href).toBe('/work?item=stalled_epic%3Alaunch-fixes');
    expect(stalled?.external).toBe(false);
    const orphan = rows.find((r) => r.entry?.type === 'orphan_pr');
    expect(orphan?.href).toBe('/work?item=orphan_pr%3A77');
    expect(orphan?.external).toBe(false);
    // The whole list, not just those two: no row anywhere leaves the app.
    expect(rows.every((r) => r.external === false)).toBe(true);
    expect(rows.every((r) => r.href.startsWith('/work?'))).toBe(true);
  });

  it('keeps the orphan PR reachable, as an external link in its detail block', () => {
    const orphan = rows.find((r) => r.entry?.type === 'orphan_pr')!;
    const detail = entryDetail(orphan, NOW_MS)!;
    expect(detail.external.map((l) => l.href)).toEqual(['https://example.invalid/pr/77']);
    expect(detail.links.every((l) => l.external === false)).toBe(true);
  });

  it('selects itself once the projection composes the item', () => {
    // 2001's entry has no row of its own HERE (the projection is unreadable in
    // this fixture, so the entry's own row stands in and links to the PR). Give
    // the projection the item and the row is the projection's: it selects
    // itself, still flagged needs-you, and the entry row is not drawn twice.
    const withItem = mergeWorkList({
      projection: {
        views: [
          {
            identity: { item_id: '2001', issue_num: 2001 },
            execution: { state: 'completed' },
            exception: { state: 'awaiting_human_decision', detail: '' },
            safest_action: { action: 'await_human_decision', reason: '' },
            artifact: { pr_url: 'https://example.invalid/pr/1' }
          }
        ],
        unreadable: []
      } as never,
      dashboard,
      runs: []
    });
    const row = withItem.find((r) => r.itemId === '2001');
    expect(row?.entry).toBeNull();
    expect(row?.needsYou).toBe(true);
    expect(row?.href).toBe('/work?item=2001');
    expect(withItem.filter((r) => r.itemId === '2001')).toHaveLength(1);
    // And the set's count is unchanged: one entry, one row, either way.
    expect(filterWorkRows(withItem, 'needs-you')).toHaveLength(ENTRIES.length);
  });

  it('renders every row as one anchor that selects in place', () => {
    // The `{#each}` the model above feeds. Read from the source because this
    // suite cannot mount a component: what must hold is that the markup draws
    // one `<a>` per row of `shown`, and that every one of them is the current
    // URL with `?item=` set — never the row's own href with a `target`, which
    // is how five of seven rows used to leave the screen (#2246).
    const page = readFileSync(
      new URL('../routes/(factory)/work/+page.svelte', import.meta.url),
      'utf8'
    );
    expect(page).toContain('{#each shown as row, i (row.itemId)}');
    expect(page).toContain('href={itemHref(row.itemId)}');
    expect(page).not.toContain('row.href');
    expect(page).not.toContain('row.external');
    // And no banner: the notice list no longer carries an "unlisted" entry.
    expect(page).not.toContain('no row here');
  });
});
