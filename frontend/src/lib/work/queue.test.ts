/**
 * #2210 — what the Work detail pane shows before an issue is chosen.
 *
 * The pane was one sentence ("Select an issue…") over two thirds of the screen,
 * on the one tab an operator opens to find out what is waiting for them (owner
 * review, 2026-09-29). It now shows the needs-you queue: the count the header
 * counts, and the rows it is made of.
 *
 * The count is `headerCounts`' — the #2207/#2229 rule that one needs-you set
 * has one reading. An unread queue says so rather than reading as a clear one.
 */
import { describe, expect, it } from 'vitest';
import type { Dashboard, DashboardRow } from '$lib/api';
import dashboardJson from '../../../fixtures/factory/dashboard.json';
import { needsYouBlock } from '$lib/needs-you/block';
import { headerCounts } from '$lib/shell/header-state';
import { mergeWorkList, type WorkRow } from '$lib/work/list';
import { needsYouSummary, QUEUE_CAP, QUEUE_CLEAR, QUEUE_UNREAD } from '$lib/work/queue';

const dashboard = dashboardJson as unknown as Dashboard;

const row = (over: Partial<WorkRow> = {}): WorkRow => ({
  itemId: '2055',
  number: '2055',
  ref: '#2055',
  title: 'Epic driver loses a tick',
  state: 'working',
  needsYou: false,
  view: null,
  tier: null,
  entry: null,
  attention: null,
  href: '/work?item=2055',
  external: false,
  ...over
});

/** The needs-you row a stalled epic gets: no work item, so it links OUT. */
const stalledEpic: DashboardRow = {
  type: 'stalled_epic',
  consequence: 'attention',
  age: '2026-09-29T12:00:00+00:00',
  reason: 'epic stalled — no node advanced in 3 ticks',
  primary_action: 'kew epic status go-live',
  issue_num: null,
  estimate_usd: null,
  epic_ref: 'go-live',
  url: null,
  pr_number: null
};

/** A needs-you entry with no work item. Since #2246 its row selects in place. */
const entryRow = (over: Partial<WorkRow> = {}): WorkRow =>
  row({
    itemId: 'stalled_epic:go-live',
    needsYou: true,
    entry: stalledEpic,
    href: '/work?item=stalled_epic%3Ago-live',
    ...over
  });

/**
 * An `epic_wait` entry as the server serves it: the item's own reference is
 * written onto the front of the reason (`_epic_wait_row` in
 * kew/src/kew/service/needs_you.py), and the bucket's label is the wording.
 */
const epicWait: DashboardRow = {
  type: 'epic_wait',
  consequence: 'flow',
  age: '2026-09-29T12:00:00+00:00',
  reason: '#2055 — in_review awaiting merge',
  label: 'in_review awaiting merge',
  attention_kind: 'merge_ready',
  primary_action: 'review',
  issue_num: 2055,
  item_id: '2055',
  estimate_usd: null,
  epic_ref: 'launch-fixes',
  url: null,
  pr_number: null
};

describe('a listed wait says its item once', () => {
  // The row prints the reference in a column of its own, and the sentence
  // under the title led with the same reference — so every `epic_wait` entry
  // said `#2055` twice in one row. The `summary !== text` guard could not see
  // it: it compared the sentence against the TITLE, and the repeat was against
  // the ref column beside it.
  it('leaves the reference to the No. column it belongs to', () => {
    const entry = needsYouSummary(
      [row({ needsYou: true, attention: epicWait })],
      dashboard
    ).entries[0];
    expect(entry.ref).toBe('#2055');
    expect(entry.text).toBe('Epic driver loses a tick');
    expect(entry.summary).toBe('in_review awaiting merge');
    expect(entry.summary).not.toContain('#2055');
  });

  it('draws no second line where the title IS the sentence', () => {
    // An entry row the set composed carries that sentence as its title
    // (`entryRows`), so the line under it would have said it twice over.
    const set: Dashboard = { ...dashboard, rows: [epicWait], unavailable: [] };
    const rows = mergeWorkList({ projection: null, dashboard: set, runs: null });
    const entry = needsYouSummary(rows, set).entries[0];
    expect(entry.ref).toBe('#2055');
    expect(entry.text).toBe('in_review awaiting merge');
    expect(entry.summary).toBe('');
  });

  it('still prints a sentence that is not the title', () => {
    // The fixture's merge decision names no reference of its own, so there is
    // nothing to leave off and nothing in the title to repeat.
    const entry = needsYouSummary(
      [row({ needsYou: true, attention: dashboard.rows[0] })],
      dashboard
    ).entries[0];
    // Led by the merge verify that failed at its head (#3460).
    expect(entry.summary).toBe(
      "Merge verify failed at 3670e43f: 2 of main's tests fail on this PR — the merge decision is yours"
    );
    expect(entry.since).not.toBe('');
  });

  it('says nothing for a flagged row the needs-you set does not name', () => {
    const entry = needsYouSummary([row({ needsYou: true })], dashboard).entries[0];
    expect(entry.summary).toBe('');
    expect(entry.since).toBe('');
  });
});

describe('the one wait the pane expands', () => {
  /**
   * #2280 D11. `sole` is the whole condition behind `{#if soleBlock}` on the
   * empty pane, and the page's only guard was a source-text grep for that
   * line — which passes whether the predicate is right or ever true. These
   * four hold the predicate itself.
   */
  it('is the flagged row ITSELF when exactly one needs a person', () => {
    // Identity, not a copy: the page hands this row straight to
    // `needsYouBlock`, so a `sole` that reconstructed the row would hand it a
    // second reading of the same wait — the defect this issue is named for.
    const only = row({ needsYou: true, attention: epicWait });
    const summary = needsYouSummary(
      [row({ itemId: '2060', number: '2060', ref: '#2060' }), only, row()],
      dashboard
    );
    expect(summary.sole).toBe(only);
  });

  it('fills the block the pane draws, command and all', () => {
    // What the branch is FOR: with one wait the reader gets the sentence, the
    // age and the command without a click. So `sole` has to be a row
    // `needsYouBlock` can read — the committed commanded wait, reached by the
    // merged list's own path.
    const held = dashboard.rows.find((r) => r.command !== '')!;
    const one: Dashboard = { ...dashboard, rows: [held], unavailable: [] };
    const rows = mergeWorkList({ projection: null, dashboard: one, runs: null });
    const summary = needsYouSummary(rows, one);

    expect(summary.sole).not.toBeNull();
    expect(summary.sole!.itemId).toBe(held.item_id);
    const block = needsYouBlock(summary.sole, Date.parse('2026-10-04T18:00:00+00:00'))!;
    expect(block.command).toBe(held.command);
    expect(block.since).toBe('held 5d8h');
  });

  it('is null the moment a second thing needs a person', () => {
    // Two waits is a list of two, not one expanded block and one row hidden
    // behind it.
    const summary = needsYouSummary(
      [row({ needsYou: true }), entryRow({ title: 'epic stalled' })],
      dashboard
    );
    expect(summary.sole).toBeNull();
    expect(summary.entries).toHaveLength(2);
  });

  it('is null when nothing needs a person, however many rows there are', () => {
    const summary = needsYouSummary([row(), row({ itemId: '2060' })], dashboard);
    expect(summary.sole).toBeNull();
  });

  it('expands nothing on the committed fixture, which carries two waits', () => {
    // The payload fixture mode serves holds the merge decision (no command —
    // the merge is made on the host) AND the go-live round-cap hold that
    // carries one, because no single row says both things. So fixture-mode
    // `/work` lists two waits and expands neither, and the design-review
    // gallery photographs the Copy button through the shot that SELECTS the
    // commanded wait (`work-needs-you` in scripts/gallery.mjs) rather than
    // through this screen. Pinned so that reading is a decision and not a
    // surprise: a set that ever holds exactly ONE wait expands it here, and
    // this test and that shot move together.
    const rows = mergeWorkList({ projection: null, dashboard, runs: null });
    const summary = needsYouSummary(rows, dashboard);
    expect(rows.filter((r) => r.needsYou)).toHaveLength(dashboard.rows.length);
    expect(dashboard.rows.length).toBeGreaterThan(1);
    expect(summary.sole).toBeNull();
  });
});

describe('the needs-you queue summary', () => {
  it('counts what the header counts, from the one set', () => {
    const rows = mergeWorkList({ projection: null, dashboard, runs: null });
    const summary = needsYouSummary(rows, dashboard);
    const counted = headerCounts(dashboard, null).needsYou;
    expect(counted).not.toBeNull();
    expect(summary.headline).toBe(`${counted} needs you`);
  });

  it('lists the rows the set put in the list, newest first, capped', () => {
    const rows = mergeWorkList({ projection: null, dashboard, runs: null });
    const summary = needsYouSummary(rows, dashboard);
    const flagged = rows.filter((r) => r.needsYou);
    expect(summary.entries.length).toBe(Math.min(flagged.length, QUEUE_CAP));
    expect(summary.entries.map((e) => e.key)).toEqual(
      flagged.slice(0, QUEUE_CAP).map((r) => r.itemId)
    );
    // Every entry can be acted on: a row that selects itself, or the artifact
    // an entry with no work item points at.
    expect(summary.entries.every((e) => e.href !== '')).toBe(true);
  });

  it('says how many it did not list rather than shortening in silence', () => {
    const many = Array.from({ length: QUEUE_CAP + 3 }, (_, i) =>
      row({ itemId: `${2000 + i}`, number: `${2000 + i}`, needsYou: true })
    );
    const summary = needsYouSummary(many, dashboard);
    expect(summary.entries).toHaveLength(QUEUE_CAP);
    expect(summary.more).toBe(3);
  });

  it('says every entry selects an item, so the page can keep the query', () => {
    // The page writes a self-selecting entry's href from the URL it is on
    // (`?filter=needs-you` survives the click). Since #2246 that is EVERY
    // entry: an entry row's identity is its own `rowKey`, which `selectedRow`
    // matches, so the pane's list no longer sends a reader off the screen for
    // the stalled epic and the orphan PR.
    const summary = needsYouSummary(
      [row({ needsYou: true }), entryRow({ title: 'epic stalled' })],
      dashboard
    );
    expect(summary.entries.map((e) => e.key)).toEqual(['2055', 'stalled_epic:go-live']);
    expect(summary.entries.every((e) => e.selects)).toBe(true);
    expect(summary.entries.every((e) => e.external === false)).toBe(true);
  });

  it('leaves a row that DOES link out its own destination', () => {
    // `selects` is read off `href` rather than hard-coded true: the page
    // replaces a selecting entry's href with its own, and must not do that to
    // a destination the list could not select.
    const summary = needsYouSummary(
      [entryRow({ href: 'https://example.invalid/pr/77', external: true })],
      dashboard
    );
    expect(summary.entries[0].selects).toBe(false);
    expect(summary.entries[0].href).toBe('https://example.invalid/pr/77');
  });

  it('prints the entry’s own sentence where a title would go', () => {
    const entry = row({
      itemId: 'stalled_epic:go-live',
      number: 'go-live',
      ref: 'go-live',
      title: 'epic stalled — no node advanced in 3 ticks',
      needsYou: true,
      href: '/epics/go-live'
    });
    const summary = needsYouSummary([entry], dashboard);
    expect(summary.entries[0].text).toBe('epic stalled — no node advanced in 3 ticks');
    expect(summary.entries[0].ref).toBe('go-live');
  });

  it('falls back to the row’s ref when no payload named it', () => {
    const summary = needsYouSummary([row({ title: '', needsYou: true })], dashboard);
    expect(summary.entries[0].text).toBe('#2055');
  });

  it('says nothing needs you only when the whole queue was read', () => {
    const clear: Dashboard = { ...dashboard, rows: [], unavailable: [] };
    const summary = needsYouSummary([], clear);
    expect(summary.headline).toBe(QUEUE_CLEAR);
    expect(summary.read).toBe(true);
    expect(summary.entries).toEqual([]);
  });

  it('never reads an unread queue as a clear one', () => {
    const summary = needsYouSummary([], null);
    expect(summary.headline).toBe(QUEUE_UNREAD);
    expect(summary.read).toBe(false);
  });

  it('says "at least" where the payload named a reading it could not take', () => {
    // A partial payload can say how many rows it HELD, never how many there
    // are — the same distinction `headerCounts` draws for the header's count.
    const partial: Dashboard = {
      ...dashboard,
      rows: dashboard.rows.slice(0, 1),
      unavailable: ['approval_holds']
    };
    const summary = needsYouSummary([], partial);
    expect(summary.headline).toBe('at least 1 needs you');
    expect(summary.read).toBe(false);
  });
});
