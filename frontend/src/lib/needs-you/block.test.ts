/**
 * #2280 D9/D11 — the ONE needs-you block: one model, one renderer, two halves.
 *
 * The defect this closes is a wait reading three ways on one screen. The band
 * over a projection row said the safest action's sentence and nothing else —
 * no Why, no age, no command, no magenta border — while the block an ENTRY row
 * got said all four; and the header above both printed a third sentence about
 * the same wait. The rows an operator actually merges are projection rows, so
 * the thinnest reading was the one on the work that matters.
 *
 * So this suite holds the merged model to the three claims that make it one
 * reading:
 *
 * 1. **Both halves fill the same fields.** Whatever a row came from, the block
 *    has a label, a summary, a Why, a Since, a command and its links — or the
 *    honest '' where that half has no fact to fill it with.
 * 2. **The entry's facts win.** The set row is what the header counts and
 *    Monitor lists, and it alone carries the stamp and the computed command.
 *    The projection contributes the title and, where the reason adds nothing,
 *    the sentence behind the decision.
 * 3. **The renderer prints every field it is given, once.** Including the
 *    command, in a real `<code>` block with a magenta Copy beside it.
 *
 * The committed fixtures are the fourth assertion, and the one that has to be
 * read against the SERVER and not against what a screenshot wanted. Two rows,
 * because one row could not say both things: the merge decision is a wait
 * `kew.service.needs_you` computes no command for, so the block over it offers
 * no Copy — and the go-live round-cap hold beside it is a wait `kew.waits`
 * really does compute `kew epic unhold <epic> <item>` for, which is what makes
 * the command block D27 asks the gallery for reachable from committed fixture
 * state rather than only from a row written here.
 */
import { readFileSync } from 'node:fs';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import { plainCommands } from '$lib/ui/command-markup';
import type { Dashboard, DashboardRow, WorkProjection, WorkView } from '$lib/api';
import dashboardJson from '../../../fixtures/factory/dashboard.json';
import projectionJson from '../../../fixtures/factory/dashboard-work.json';
import workViewJson from '../../../fixtures/factory/verbs-work-2055.json';
import NeedsYouBlockView from './NeedsYouBlock.svelte';
import { needsYouBlock, type NeedsYouBlock } from './block';
import { mergeWorkList, selectedRow, type WorkRow } from '$lib/work/list';
import { rowSummary } from './summary';

/** A fixed clock: the Since line has to be the same string every run. */
const NOW_MS = Date.parse('2026-09-24T18:00:00+00:00');

/**
 * The same, for the committed go-live row — whose wait began on the 29th, five
 * days after the launch-fixes world the clock above is set in. A duration is
 * only reproducible against a clock that is actually after the stamp.
 */
const HELD_NOW_MS = Date.parse('2026-10-04T18:00:00+00:00');

const DASHBOARD = dashboardJson as unknown as Dashboard;
const PROJECTION = projectionJson as unknown as WorkProjection;
const VIEW: WorkView = workViewJson;

/** The committed set row: #2055, a merge decision on PR #2059. */
const ENTRY: DashboardRow = DASHBOARD.rows.find(
  (row) => row.attention_kind === 'merge_decision'
)!;

/**
 * The committed set row that carries a COMMAND: the go-live round-cap hold,
 * served as `_epic_wait_row`'s `epic_wait` with `kew.waits`'s unhold template
 * filled (kew/tests/test_needs_you_commands.py derives the whole row from the
 * server, so nothing on it is a value chosen for a screenshot).
 */
const HELD: DashboardRow = DASHBOARD.rows.find((row) => row.command !== '')!;

/**
 * A set row that really does carry a command, as `_epic_wait_row` serves one
 * (kew/src/kew/service/needs_you.py): `kew.waits`'s `awaiting_continuation`
 * bucket, its template filled with this epic and this item.
 *
 * `ENTRY` is not that row and cannot be made into one by writing a command
 * onto it: `SAFEST_COMMAND` has no entry for `await_human_decision`, because
 * the merge is made on the host. So the two readings are kept apart — the
 * committed row for what the fixture-mode screen says, this one for the Do
 * this row and its Copy.
 */
const COMMANDED: DashboardRow = {
  ...ENTRY,
  type: 'epic_wait',
  consequence: 'wait',
  reason: '#2055 — awaiting preserved-work continuation',
  primary_action: 'kew epic continue <name> <node>',
  attention_kind: 'held',
  label: 'awaiting preserved-work continuation',
  pr_number: null,
  command: 'kew epic continue launch-fixes 2055'
};

function rowFor(over: Partial<WorkRow> = {}): WorkRow {
  return {
    itemId: '2055',
    number: '2055',
    ref: '#2055',
    title: 'Epic driver loses a tick',
    state: 'in review',
    needsYou: false,
    view: null,
    tier: null,
    entry: null,
    attention: null,
    href: '/work?item=2055',
    external: false,
    ...over
  };
}

/** The rendered block, comments stripped and whitespace between tags gone. */
function draw(block: NeedsYouBlock | null, copied = false): string {
  return plainCommands(render(NeedsYouBlockView, { props: { block, copied } }).body)
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/>\s+</g, '><');
}

describe('one model for both halves', () => {
  it('is null when neither the set nor the projection says a person is next', () => {
    expect(needsYouBlock(null, NOW_MS)).toBeNull();
    expect(needsYouBlock(rowFor(), NOW_MS)).toBeNull();
    // A view whose safest action is kew's own is not a needs-you either.
    const quiet: WorkView = {
      ...VIEW,
      judgment: { ...VIEW.judgment, state: 'accepted' },
      safest_action: { action: 'dispatch', reason: 'kew will dispatch it', blocked_by: null }
    };
    expect(needsYouBlock(rowFor({ view: quiet }), NOW_MS)).toBeNull();
  });

  it('fills every field from the entry when only the set names the row', () => {
    const block = needsYouBlock(rowFor({ attention: ENTRY, entry: ENTRY }), NOW_MS)!;
    expect(block.summary).toBe(rowSummary(ENTRY));
    expect(block.since).toBe('waiting 2h13m');
    expect(block.command).toBe(ENTRY.command);
    // The links are the ones the row can be addressed BY, and nothing else.
    // With no command, the server's links are Do this (#3467 D24). The merge
    // verify failed at the PR's head (#3460), so reading that failure is the
    // move and the PR follows it, quiet (design review c9 D10); the epic
    // stays in the quiet row.
    expect(block.moves?.map((l) => [l.href, l.move])).toEqual([
      ['/work?item=2055#merge-verify', true],
      [ENTRY.link!.href, false]
    ]);
    expect(block.links.map((l) => l.href)).toEqual(['/epics/launch-fixes?node=2055']);
    expect(block.external).toEqual([]);
  });

  it('fills what it can from the projection when the set does not name the row', () => {
    const block = needsYouBlock(rowFor({ view: VIEW }), NOW_MS)!;
    expect(block.label).toBe('Merge decision');
    expect(block.summary).toBe(VIEW.safest_action.reason);
    // No entry, so no stamp and no computed command exist to print. '' is the
    // honest reading — never a duration derived from a timestamp in reach.
    expect(block.since).toBe('');
    expect(block.command).toBe('');
    // With no command, the projection's link is the move (#3467 D24).
    expect(block.moves?.map((l) => [l.href, l.move])).toEqual([[VIEW.artifact.pr_url, true]]);
    expect(block.external).toEqual([]);
  });

  it('lets the entry`s facts win and takes the title from the projection', () => {
    const block = needsYouBlock(rowFor({ view: VIEW, attention: ENTRY }), NOW_MS)!;
    // The kind of decision is the projection's word; everything the set row
    // alone knows is the set row's.
    expect(block.label).toBe('Merge decision');
    expect(block.summary).toBe(rowSummary(ENTRY));
    expect(block.command).toBe(ENTRY.command);
    expect(block.since).toBe('waiting 2h13m');
    // The reason adds nothing of its own here, so the Why is the sentence
    // behind the decision rather than a blank row.
    expect(block.why).toBe(VIEW.safest_action.reason);
  });

  it('never prints the band`s sentence twice when it IS the summary', () => {
    const entry: DashboardRow = {
      ...ENTRY,
      reason: VIEW.safest_action.reason,
      sentence: VIEW.safest_action.reason,
      label: ''
    };
    const block = needsYouBlock(rowFor({ view: VIEW, attention: entry }), NOW_MS)!;
    expect(block.summary).toBe(VIEW.safest_action.reason);
    expect(block.why).toBe('');
  });
});

describe('the renderer', () => {
  it('draws nothing at all when there is no block', () => {
    expect(draw(null)).toBe('');
  });

  it('prints the command in a <code> block with a magenta Copy beside it', () => {
    const html = draw(needsYouBlock(rowFor({ view: VIEW, attention: COMMANDED }), NOW_MS));
    expect(html).toMatch(new RegExp(`<code[^>]*>${COMMANDED.command}</code>`));
    expect(html).toContain('aria-label="Copy command (c)"');
    expect(html).toContain('>Copy<');
    expect(
      draw(needsYouBlock(rowFor({ view: VIEW, attention: COMMANDED }), NOW_MS), true)
    ).toContain('>Copied<');
  });

  it('draws no command block for a wait no kew command clears', () => {
    // The committed merge decision is that wait: `''` is the server's own
    // answer for it, and an empty `<code>` block with a Copy button beside it
    // would be an affordance for nothing. Its Do this is the server's link
    // instead (#3467 D24).
    const html = draw(needsYouBlock(rowFor({ view: VIEW, attention: ENTRY }), NOW_MS));
    expect(html).toContain('>Do this<');
    expect(html).not.toContain('<code');
    expect(html).not.toContain('>Copy<');
    // And a wait with neither a command, a note nor a served link has no row.
    const bare: DashboardRow = { ...ENTRY, link: null, verify_link: null };
    expect(draw(needsYouBlock(rowFor({ attention: bare, entry: bare }), NOW_MS))).not.toContain('>Do this<');
  });

  it('makes the served link the magenta Do this on a merge decision (#3467 D24)', () => {
    // No failed verify at the head: the PR link is the move.
    const passed: DashboardRow = { ...ENTRY, verify_link: null };
    const html = draw(needsYouBlock(rowFor({ view: VIEW, attention: passed }), NOW_MS));
    const doThis = html.indexOf('>Do this<');
    const move = html.match(/<a [^>]*data-needs-you-move[^>]*>/)?.[0] ?? '';
    // The server's href and words, drawn as the one filled control.
    expect(move).toContain(`href="${ENTRY.link!.href}"`);
    expect(move).toMatch(/[\s"]bg-accent[\s"]/);
    expect(move).toContain('target="_blank"');
    expect(html).toContain(`>${ENTRY.link!.label} <span aria-hidden="true">↗</span></a>`);
    expect(doThis).toBeGreaterThan(-1);
    expect(html.indexOf(move)).toBeGreaterThan(doThis);
    expect(html.match(/[\s"]bg-accent[\s"]/g)).toHaveLength(1);
    // Not a second time in the quiet row under the block.
    expect(html.match(new RegExp(`href="${ENTRY.link!.href}"`, 'g'))).toHaveLength(1);
  });

  it('makes the failed merge verify the magenta Do this, the PR quiet after it (design review c9 D10)', () => {
    const html = draw(needsYouBlock(rowFor({ view: VIEW, attention: ENTRY }), NOW_MS));
    const doThis = html.indexOf('>Do this<');
    const move = html.match(/<a [^>]*data-needs-you-move[^>]*>/)?.[0] ?? '';
    // The safest next move is to read the failure, not to merge over it.
    expect(move).toContain('href="/work?item=2055#merge-verify"');
    expect(move).toMatch(/[\s"]bg-accent[\s"]/);
    expect(move).not.toContain('target="_blank"');
    const pr = html.match(new RegExp(`<a [^>]*href="${ENTRY.link!.href}"[^>]*>`))?.[0] ?? '';
    expect(pr).not.toMatch(/[\s"]bg-accent[\s"]/);
    expect(pr).toContain('target="_blank"');
    expect(doThis).toBeGreaterThan(-1);
    expect(html.indexOf(move)).toBeGreaterThan(doThis);
    expect(html.indexOf(pr)).toBeGreaterThan(html.indexOf(move));
    // One magenta control, and the PR drawn once.
    expect(html.match(/[\s"]bg-accent[\s"]/g)).toHaveLength(1);
    expect(html.match(new RegExp(`href="${ENTRY.link!.href}"`, 'g'))).toHaveLength(1);
  });

  it('makes the projection`s link the magenta Do this when the set does not name the row (#3467 D24)', () => {
    const html = draw(needsYouBlock(rowFor({ view: VIEW, attention: null, entry: null }), NOW_MS));
    const doThis = html.indexOf('>Do this<');
    const move = html.match(/<a [^>]*data-needs-you-move[^>]*>/)?.[0] ?? '';
    expect(doThis).toBeGreaterThan(-1);
    expect(move).toContain(`href="${VIEW.artifact.pr_url}"`);
    expect(move).toMatch(/[\s"]bg-accent[\s"]/);
    expect(move).toContain('target="_blank"');
    expect(html.indexOf(move)).toBeGreaterThan(doThis);
    expect(html.match(/[\s"]bg-accent[\s"]/g)).toHaveLength(1);
    // Not a second time in the quiet row under the block.
    expect(html.match(new RegExp(`href="${VIEW.artifact.pr_url}"`, 'g'))).toHaveLength(1);
  });

  it('leads with the summary, Since and Do this, and folds Why behind why ▸ (#2536, #3467)', () => {
    const html = draw(needsYouBlock(rowFor({ view: VIEW, attention: COMMANDED }), NOW_MS));
    const order = ['Needs you', 'Since', 'Do this', 'Why'].map((k) => html.indexOf(`>${k}<`));
    expect(order.every((i) => i > -1)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    expect(html).toContain('Merge decision');
    expect(html).toContain(rowSummary(COMMANDED));
    // `held`, not `waiting`: the word is the row's own attention kind's, and
    // this row is a held one. The same field reads `waiting 2h13m` over the
    // committed merge decision above.
    expect(html).toContain('held 2h13m');
    // The age is printed ONCE, under the label that says which reading it is.
    expect(html.match(/2h13m/g)).toHaveLength(1);
    // Folded, not dropped: the explanation is in the document, closed, and
    // the toggle names what it opens. Since is not in it: it is in the lead.
    const fold = html.match(/<dl id="(needs-you-why-[^"]+)" hidden=""/);
    expect(fold).not.toBeNull();
    expect(html).toContain(`aria-controls="${fold![1]}"`);
    expect(html).toContain('aria-expanded="false"');
    expect(html.indexOf(fold![0])).toBeLessThan(html.indexOf('>Why<'));
    expect(html.indexOf('>Since<')).toBeLessThan(html.indexOf(fold![0]));
  });

  it('spends the magenta on the card`s left edge and the Copy, and nowhere else', () => {
    const html = draw(needsYouBlock(rowFor({ view: VIEW, attention: ENTRY }), NOW_MS));
    expect(html).toContain('border-l-accent');
    expect(html).toContain('text-accent');
  });
});

describe('against the committed fixtures', () => {
  /** The rows `/work` flags in fixture mode, by the path the page takes. */
  function flagged(): WorkRow[] {
    const projection = { views: [VIEW] } as unknown as WorkProjection;
    const rows = mergeWorkList({ projection, dashboard: DASHBOARD, runs: [] });
    const needsYou = rows.filter((row) => row.needsYou);
    // One per committed entry, which is what the header counts.
    expect(needsYou).toHaveLength(DASHBOARD.rows.length);
    return needsYou;
  }

  /** The row for one committed item, as `/work` selects it. */
  function committed(itemId = ENTRY.item_id!): WorkRow {
    return flagged().find((row) => row.itemId === itemId)!;
  }

  it('offers no Copy on the merge-decision row, because the server serves it none', () => {
    // D27 wanted a command block in the gallery and the first pass got one by
    // writing `kew epic merge launch-fixes 2055 --head <sha>` onto the
    // committed `/api/dashboard` row. No reachable server state serves that: a
    // merge-decision row is `_work_row`'s, whose command is
    // `SAFEST_COMMAND[safest_action]`, and `await_human_decision` has no entry
    // in that table — the merge happens on the host, so '' is the honest
    // answer, held in Python by `test_needs_you_commands.py`. A Copy button
    // over a line nobody can run is worse than no button, so the row keeps
    // everything it can say and says nothing about what to run.
    expect(ENTRY.command).toBe('');
    const block = needsYouBlock(committed(), NOW_MS)!;
    expect(block.command).toBe('');
    expect(block.summary).toBe(rowSummary(ENTRY));
    expect(block.label).toBe('Merge decision');
    const html = draw(block);
    expect(html).not.toContain('aria-label="Copy command (c)"');
    expect(html).not.toContain('<code');
    // And the wait is still a wait: the block says since when, and its move
    // is a link (#3467 D24) — the failed merge verify at the PR's head, which
    // the committed row carries, with the pull request after it, quiet
    // (design review c9 D10).
    expect(block.since).toBe('waiting 2h13m');
    expect(block.moves?.filter((l) => l.move).map((l) => l.href)).toEqual([ENTRY.verify_link!.href]);
    expect(block.moves?.filter((l) => !l.move).map((l) => l.href)).toEqual([ENTRY.url]);
  });

  it('reaches a command block from the committed payload, with a Copy', () => {
    // The other half of D27, and the thing emptying the invented command cost:
    // with only the merge decision in the set, fixture-mode `/work` had no
    // command anywhere, so the affordance the gallery is asked to photograph
    // was reachable from no committed state at all. The set now also carries
    // the go-live round-cap hold — a wait `kew.waits` really computes an
    // unhold for — and this is that row, reached by the page's own path.
    const block = needsYouBlock(committed(HELD.item_id!), HELD_NOW_MS)!;

    expect(block.command).toBe(HELD.command);
    expect(block.command).not.toBe('');
    // The kind, not the reason the summary already opens with (#3051 N8).
    expect(block.label).toBe('Held');
    expect(block.summary).toContain('the review round cap was reached');
    // Said `held`, not `waiting`: the word is the row's own attention kind's.
    expect(block.since).toBe('held 5d8h');

    const html = draw(block);
    // A real `<code>` block, so the line survives a clipboard this page cannot
    // reach, with the one magenta button on the pane beside it.
    expect(html).toMatch(new RegExp(`<code[^>]*>${HELD.command}</code>`));
    expect(html).toContain('aria-label="Copy command (c)"');
    expect(html).toContain('>Do this<');
    expect(html).toContain('>Copy<');
  });

  /**
   * The items the design-review gallery SELECTS on `/work`, read off
   * `scripts/gallery.mjs`. That file drives a browser the moment it is
   * imported, so the shot list is parsed out of its source.
   */
  function shotItems(): string[] {
    const source = readFileSync(
      new URL('../../../scripts/gallery.mjs', import.meta.url),
      'utf8'
    );
    const found = source.matchAll(/route:\s*'\/work\?item=([^'&]+)'/g);
    return [...found].map((m) => decodeURIComponent(m[1]));
  }

  /**
   * The row fixture-mode `/work?item=<id>` selects, over both committed
   * payloads — the projection the dev server answers `/api/dashboard/work`
   * with, and the needs-you set beside it — by the page's own path.
   */
  function shown(itemId: string): WorkRow | null {
    const rows = mergeWorkList({ projection: PROJECTION, dashboard: DASHBOARD, runs: [] });
    return selectedRow(rows, itemId);
  }

  it('says each committed row`s reason once: the head names the kind (#3051 N8)', () => {
    // SFD-229 read "NEEDS YOU · the review round cap was reached" over "SFD-229
    // — the review round cap was reached: review pool $10.00 exhausted…". The
    // head is the kind now, and no clause of the sentence is in it.
    const heads: Record<string, string> = {};
    for (const row of flagged()) {
      const block = needsYouBlock(row, HELD_NOW_MS)!;
      heads[row.itemId] = block.label;
      const clauses = block.summary
        .split(/: | — /)
        .map((c) => c.trim().toLowerCase())
        .filter((c) => c !== '');
      expect(clauses.filter((c) => block.label.toLowerCase().includes(c))).toEqual([]);
    }
    expect(heads[HELD.item_id!]).toBe('Held');
    expect(heads[ENTRY.item_id!]).toBe('Merge decision');
  });

  it('is photographed with that Copy by a committed gallery shot', () => {
    // Reachable is not photographed, and the two are not one claim. The test
    // above proves the committed payload HOLDS a wait with a command; a design
    // cycle only gets to check the affordance if a shot the gallery actually
    // takes lands on it. Neither `/work` shot did: the first screen lists two
    // waits rather than expanding one, and `?item=2055` selects the merge
    // decision, whose command is ''. So the Copy button was in no picture of
    // any cycle, which is what `work-needs-you` is the shot for.
    const items = shotItems();
    expect(items.length).toBeGreaterThan(0);

    const commanded = items
      .map((item) => needsYouBlock(shown(item), HELD_NOW_MS))
      .filter((block) => (block?.command ?? '') !== '');
    expect(
      commanded.map((block) => block!.command),
      `no /work gallery shot selects a wait with a command (shots: ${items.join(', ')})`
    ).toContain(HELD.command);

    // And the shot draws the affordance, not just the field behind it.
    const html = draw(commanded.find((block) => block!.command === HELD.command)!);
    expect(html).toContain('>Do this<');
    expect(html).toContain('aria-label="Copy command (c)"');
    expect(html).toContain('>Copy<');
  });
});
