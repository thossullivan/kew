/**
 * #2539 — one server sentence per wait, drawn the same on every surface.
 *
 * One merge decision read four ways: Work said "kew opens the pull request; you
 * decide whether it lands", the epic popover and the rail "PR open for #2055 —
 * awaiting review", and the pipeline "round 1 ready". Each surface was
 * composing its own sentence out of the reason, the label and its own tables.
 *
 * The server now writes the sentence (`kew.service.attention.say_wait`) onto
 * both the needs-you row and the node's attention, and every surface prints
 * that field. This is held without grepping anybody's source: each surface is
 * handed a wait whose `sentence` is a marker nothing could derive from its
 * reason, its label or a hold table, and each must print exactly the marker.
 * A surface that composes for itself prints something else, and fails here.
 */
import { describe, expect, it } from 'vitest';
import type { Dashboard, DashboardRow, EpicNode } from '$lib/api';
import dashboardJson from '../../../fixtures/factory/dashboard.json';
import { statusSegments, statusLineText } from '$lib/epic/attention';
import { nodePopover } from '$lib/epic/node-popover';
import { railAttention } from '$lib/epic/rail';
import { composeHolds, drawnHolds } from '$lib/monitor/holds';
import { captionText, needsYouReadout } from '$lib/monitor/readouts';
import { needsYouBlock } from '$lib/needs-you/block';
import { mergeWorkList } from '$lib/work/list';
import { needsYouSummary } from '$lib/work/queue';

/** Words no reason, label or hold table in the console could produce. */
const MARKER = 'the server alone wrote this, zq-7';
const NOW_MS = Date.parse('2026-09-30T09:00:00+00:00');
const dashboard = dashboardJson as unknown as Dashboard;

/** A round-cap hold: the reason every client table has its own words for. */
const HOLD: DashboardRow = {
  type: 'epic_wait',
  consequence: 'wait',
  age: '2026-09-29T09:41:00+00:00',
  reason: '#2201 — review_rounds_exhausted: review pool $10.00 exhausted',
  primary_action: 'kew epic unhold',
  issue_num: 2201,
  item_id: '2201',
  estimate_usd: null,
  epic_ref: 'launch-fixes',
  url: null,
  pr_number: null,
  attention_kind: 'held',
  label: 'the review round cap was reached',
  command: 'kew epic unhold launch-fixes 2201',
  sentence: MARKER
};

/** The same hold out of `epic_holds`, which Monitor draws through its own table. */
const EPIC_HOLD_ROW: DashboardRow = {
  ...HOLD,
  type: 'budget_hold',
  reason: 'review_rounds_exhausted: review pool $10.00 exhausted'
};

/** A merge decision: the wait the four readings disagreed about. */
const MERGE: DashboardRow = {
  ...HOLD,
  type: 'pr_review',
  reason: 'PR open for #2201 — awaiting review',
  attention_kind: 'merge_decision',
  label: '',
  command: '',
  url: 'https://github.com/o/r/pull/2207',
  pr_number: 2207,
  link: { label: 'Open PR #2207 to merge', href: 'https://github.com/o/r/pull/2207' }
};

const NODE_BASE: EpicNode = {
  id: '2201',
  title: 'review gate lows',
  status: 'in_review',
  status_since: null,
  url: null,
  pr_url: 'https://github.com/o/r/pull/2207',
  external: false,
  stuck: null,
  attention: null,
  blocked_reason: null
};

const asNode = (row: DashboardRow): EpicNode => ({
  ...NODE_BASE,
  attention: {
    kind: row.attention_kind ?? '',
    reason: row.reason,
    url: row.url,
    command: row.command,
    sentence: row.sentence,
    link: row.link ?? null
  }
});

const served = (row: DashboardRow): Dashboard => ({ ...dashboard, rows: [row], unavailable: [] });

describe.each([
  ['a journaled hold', HOLD],
  ['an epic_holds row', EPIC_HOLD_ROW],
  ['a merge decision', MERGE]
])('every surface prints the server sentence for %s, and nothing of its own', (_, row) => {
  it('Work: the list row, the queue and the needs-you block', () => {
    const rows = mergeWorkList({ projection: null, dashboard: served(row), runs: null });
    const listed = rows.find((r) => r.itemId === '2201')!;
    expect(listed.title).toBe(MARKER);
    const queue = needsYouSummary(rows, served(row), NOW_MS);
    expect(queue.entries[0].text).toBe(MARKER);
    // The block's headline adds only the reference the server wrote on the
    // reason (`#2201 — ` on an epic wait); the words after it are the marker.
    const ref = row.reason.startsWith('#2201 — ') ? '#2201 — ' : '';
    expect(needsYouBlock(listed, NOW_MS)!.summary).toBe(`${ref}${MARKER}`);
  });

  it('Monitor: the Needs you caption and the Holds column', () => {
    expect(captionText(needsYouReadout(served(row)).caption)).toContain(MARKER);
    const column = drawnHolds(composeHolds({ epics: [], budget: {}, dashboard: served(row) }), {});
    expect(captionText(column.rows[0].detail)).toContain(MARKER);
  });

  it('the epic: the popover, the rail and the DAG status line', () => {
    const node = asNode(row);
    expect(nodePopover(node, null, NOW_MS).reason).toBe(MARKER);
    expect(railAttention({ epicRef: 'launch-fixes', node })!.prose).toBe(MARKER);
    expect(statusLineText(statusSegments(node))).toContain(MARKER);
  });
});

describe('a merge decision moves through the server link, on every surface that offers one', () => {
  it('names the link the same way on Work, Monitor and the rail', () => {
    const rows = mergeWorkList({ projection: null, dashboard: served(MERGE), runs: null });
    const block = needsYouBlock(rows.find((r) => r.itemId === '2201')!, NOW_MS)!;
    // On Work it is the block's Do this move (#3467 D24), not a quiet link.
    expect(block.moves?.filter((l) => l.move).map((l) => l.label)).toEqual(['Open PR #2207 to merge']);
    const column = drawnHolds(composeHolds({ epics: [], budget: {}, dashboard: served(MERGE) }), {});
    expect(column.rows[0].link?.label).toBe('Open PR #2207 to merge');
    expect(railAttention({ epicRef: 'launch-fixes', node: asNode(MERGE) })!.cta?.label).toBe(
      'Open PR #2207 to merge'
    );
  });
});
