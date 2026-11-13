/**
 * #2246 — what the Work detail pane is allowed to say about a needs-you entry.
 *
 * Five of the seven rows on the live Work screen are entries with no projection
 * view, and until this issue the pane had nothing at all for them:
 * `needsYouBand` returns `null` without a view. So this block is the one thing
 * standing between an operator and the epic page they used to be thrown out to,
 * and the three ways such a block lies are the three things asserted here.
 *
 * 1. **It says the row's wait in words nobody else contradicts.** The what and
 *    the why are `rowWords`' — the derivation `rowSummary` joins into the
 *    sentence Monitor's caption and the list row print (#2245). A pane headlined
 *    "the review round cap was reached" over a Why composed some other way would
 *    be that two-readings defect one screen along.
 * 2. **It never invents a URL.** The in-app links are routes this app has; the
 *    external ones are the single `url` the server served, and nothing is
 *    composed out of a repo slug.
 * 3. **It admits when a link leaves kew.** Every `target="_blank"` anchor the
 *    component renders ends its own text in `↗`.
 * 4. **It puts nobody else's move on the row.** The hold vocabulary answers for
 *    every string it is handed, so asking it about an entry that is not a hold
 *    printed `kew epic unhold` under a merge decision, an orphan PR and a
 *    stopped driver — beside the command the server actually served. Same rule
 *    for the duration: `held` is said only where the row's kind says it is held.
 *
 * The component is SERVER-rendered, as `Pipeline` and `NeedsYou` are in this
 * suite: the pixels are read off the markup the section really produces.
 */
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import { plainCommands } from '$lib/ui/command-markup';
import type { DashboardRow } from '$lib/api';
import { rowSummary } from '$lib/needs-you/summary';
import NeedsYouBlock from '$lib/needs-you/NeedsYouBlock.svelte';
import { commandNote, entryDetail, type EntryDetailModel } from './entry-detail';
import type { WorkRow } from './list';

/** A fixed clock: `held 68d6h` has to be the same string every run. */
const NOW_MS = Date.parse('2026-10-02T15:00:00+00:00');

/**
 * The round-cap hold from the UX brief's screenshot: node 1116 of
 * `governance-hardening`, held since 68d6h before `NOW_MS`.
 *
 * Shaped as `/api/dashboard` serves it. The `type` says only which table the
 * row came out of — every `epic_holds` entry is served as `budget_hold` whatever
 * held the node — so the WORDS are read off `reason` and `label` and the state
 * off `attention_kind`; `type` is read for one question only, and it is the one
 * the hold vocabulary turns on: did this row come out of `epic_holds` at all?
 */
const roundCap: DashboardRow = {
  type: 'budget_hold',
  consequence: 'epic',
  age: '2026-07-26T09:00:00+00:00',
  reason: 'review_rounds_exhausted: review pool $10.00 exhausted (spent $22.18)',
  primary_action: 'unhold',
  issue_num: 1116,
  item_id: '1116',
  estimate_usd: null,
  epic_ref: 'governance-hardening',
  url: null,
  pr_number: null,
  attention_kind: 'held',
  label: 'the review round cap was reached',
  command: 'kew epic unhold governance-hardening 1116',
  // What `kew.service.attention.say_wait` writes for this reason (#2539).
  sentence: 'the review round cap was reached: review pool $10.00 exhausted (spent $22.18)'
};

/** The Work row such an entry becomes (`entryRows` in list.ts). */
function rowFor(entry: DashboardRow, itemId = '1116'): WorkRow {
  return {
    itemId,
    number: itemId,
    ref: `#${itemId}`,
    title: rowSummary(entry),
    state: 'held',
    needsYou: true,
    view: null,
    tier: null,
    entry,
    attention: entry,
    href: `/work?item=${itemId}`,
    external: false
  };
}

function detailFor(over: Partial<DashboardRow> = {}): EntryDetailModel {
  const model = entryDetail(rowFor({ ...roundCap, ...over }), NOW_MS);
  expect(model).not.toBeNull();
  return model!;
}

/** The rendered section, comments stripped and whitespace between tags gone. */
function draw(detail: EntryDetailModel | null, copied = false): string {
  return plainCommands(render(NeedsYouBlock, { props: { block: detail, copied } }).body)
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/>\s+</g, '><');
}

describe('entryDetail', () => {
  it('answers the round-cap hold with the command, the words and the age', () => {
    const detail = detailFor();
    expect(detail.command).toBe('kew epic unhold governance-hardening 1116');
    // The head names the KIND of wait; the reason is the sentence's alone, so
    // it is said once (#3051 N8).
    expect(detail.label).toBe('Held');
    // The server's sentence already says what the reason adds, so the Why
    // line does not say it a second time (#2539).
    expect(detail.why).toBe('');
    expect(detail.since).toBe('held 68d6h');
    // And the headline is the row's one sentence, not a second composition.
    expect(detail.summary).toBe(rowSummary(roundCap));
    expect(detail.summary).toContain('the review round cap was reached');
    expect(detail.summary).toContain('review pool $10.00 exhausted (spent $22.18)');
  });

  it('keeps a Why the server sentence does not already carry', () => {
    const detail = detailFor({ sentence: 'the review round cap was reached' });
    expect(detail.why).toBe('review pool $10.00 exhausted (spent $22.18)');
  });

  it('says what running the command also does, without repeating it', () => {
    // `HOLD_SPECS` gives the round cap `{unhold} — it restarts the review
    // cycle`, and the command is already printed in its own block above the
    // note: the clause is what the note is for.
    expect(detailFor().note).toBe('it restarts the review cycle');
  });

  it('keeps a step that has to happen BEFORE the command whole', () => {
    // A containment hold's action is "map [sandbox] images.<runner>, then
    // {unhold}" — it does not begin with the command, so cutting a prefix off
    // it would drop the only part that matters.
    const note = detailFor({
      reason: 'sandbox_containment: no image mapped for runner claude-code',
      label: 'held on containment'
    }).note;
    expect(note).toContain('map [sandbox] images.<runner>');
    expect(note).toContain('kew epic unhold governance-hardening 1116');
  });

  it('cuts nothing when the action IS the command, and nothing from nothing', () => {
    expect(commandNote('kew epic unhold a 1', 'kew epic unhold a 1')).toBe('');
    expect(commandNote('', 'kew epic unhold a 1')).toBe('');
    expect(commandNote('raise review.pool', '')).toBe('raise review.pool');
  });

  it('shows the node in the epic, in the app, and links out only externally', () => {
    const detail = detailFor();
    expect(detail.links).toEqual([
      {
        key: 'epic',
        label: 'Show in epic',
        href: '/epics/governance-hardening?node=1116',
        external: false
      }
    ]);
    // Nothing external at all here: the server served no URL for this row, and
    // a forge URL composed from a repo slug would be an invention.
    expect(detail.external).toEqual([]);
    expect(detail.links.every((l) => l.external === false)).toBe(true);
  });

  it('puts the one URL the server served in `external`, and only there', () => {
    const detail = detailFor({
      url: 'https://github.com/thossullivan/kew/pull/2231',
      pr_number: 2231
    });
    expect(detail.external).toEqual([
      {
        key: 'artifact',
        label: 'PR #2231',
        href: 'https://github.com/thossullivan/kew/pull/2231',
        external: true
      }
    ]);
    expect(detail.links.some((l) => l.href.startsWith('http'))).toBe(false);
  });

  it.each(['javascript:alert(1)', '/runs/r1', '//evil.example/x'])(
    'makes no link of a served URL safeHref refuses (%j)',
    (url) => {
      expect(detailFor({ url, pr_number: 2231 }).external).toEqual([]);
      const link = { label: 'Open PR #2231 to merge', href: url };
      expect(detailFor({ link }).external).toEqual([]);
      // Nor a Do this move where no command clears the row (#3467 D24).
      expect(detailFor({ command: '', link }).moves).toEqual([]);
    }
  );

  it('keeps a verify link only when it is an app path or an http(s) URL', () => {
    const verify = (href: string) =>
      detailFor({ verify_link: { label: 'Merge verify failed', href } }).links.filter(
        (l) => l.key === 'verify'
      );
    expect(verify('/work?item=2231#verify')).toHaveLength(1);
    expect(verify('javascript:alert(1)')).toEqual([]);
    expect(verify('//evil.example/x')).toEqual([]);
  });

  it('names the served URL by what it opens, never a bare "Open" (#3467 D30)', () => {
    const label = (over: Partial<DashboardRow>) => detailFor(over).external.map((l) => l.label);
    // SFD-229's one URL is its Linear issue.
    expect(
      label({ item_id: 'SFD-229', issue_num: null, url: 'https://linear.app/kew/issue/SFD-229' })
    ).toEqual(['Issue SFD-229']);
    expect(label({ url: 'https://github.com/thossullivan/kew/issues/1116' })).toEqual(['Issue #1116']);
    // A PR served as a URL with no pr_number (#2893) is still the PR.
    expect(label({ url: 'https://github.com/thossullivan/kew/pull/2231' })).toEqual(['PR #2231']);
  });

  it('makes a served link the Do this move only where no command clears the row (#3467 D24)', () => {
    const link = { label: 'Open PR #2231 to merge', href: 'https://github.com/thossullivan/kew/pull/2231' };
    const merge = detailFor({ command: '', link, attention_kind: 'merge_decision', type: 'pr_review' });
    expect(merge.moves).toEqual([{ key: 'artifact', ...link, external: true, move: true }]);
    expect(merge.external).toEqual([]);
    // With a command, the command is the move and the link stays quiet.
    const held = detailFor({ link });
    expect(held.moves).toEqual([]);
    expect(held.external.map((l) => l.label)).toEqual([link.label]);
  });

  it('sends a row with no epic to Monitor, which is where it can be decided', () => {
    // The destination `entryHref` used to give an approval hold: a row this
    // screen can select is still a row somebody has to act on somewhere.
    const detail = detailFor({ epic_ref: null, item_id: null, issue_num: null });
    expect(detail.links).toEqual([
      { key: 'monitor', label: 'Show in Monitor', href: '/monitor', external: false }
    ]);
  });

  it('names the epic without a node when the entry names no item', () => {
    const detail = detailFor({ item_id: null, issue_num: null });
    expect(detail.links[0].href).toBe('/epics/governance-hardening');
  });

  it('encodes an opaque epic ref and item id rather than parsing them', () => {
    const detail = detailFor({ epic_ref: 'go live/2026', item_id: 'SFD-232', issue_num: 0 });
    expect(detail.links[0].href).toBe('/epics/go%20live%2F2026?node=SFD-232');
  });

  it('says nothing at all about a row that is not an entry`s', () => {
    // An item the projection composed has a view, and `NeedsYou.svelte`'s band
    // is what reads it. Two needs-you blocks over one row would be two answers.
    expect(entryDetail({ ...rowFor(roundCap), entry: null }, NOW_MS)).toBeNull();
  });

  it('prints no duration for a stamp it could not read', () => {
    expect(detailFor({ age: '' }).since).toBe('');
    expect(detailFor({ age: 'not a date' }).since).toBe('');
  });

  it('carries an unbucketed reason`s own words rather than guessing', () => {
    const over = {
      reason: 'merge decision on PR #2059',
      label: 'in_review awaiting merge',
      command: ''
    };
    const detail = detailFor(over);
    // The head is the row's kind; the reason's words are the summary's.
    expect(detail.label).toBe('Held');
    expect(detail.command).toBe('');
    expect(detail.summary).toBe(rowSummary({ ...roundCap, ...over }));
  });

  // The hold table answers for EVERY string: a reason no prefix claims falls to
  // the unbucketed hint, `fix the cause, then kew epic unhold <epic> <item>`.
  // Asked about every entry, it printed that unhold under a merge decision whose
  // served command was `kew epic merge`, under an orphan PR and under a driver
  // nobody is ticking — a move that is not the row's, contradicting the row's
  // own command. So the table is asked only about `epic_holds` rows.
  const notHolds: { said: string; over: Partial<DashboardRow> }[] = [
    {
      said: 'a node awaiting a merge',
      over: {
        type: 'epic_wait',
        attention_kind: 'merge_ready',
        reason: '#2219 — merge decision on PR #2231',
        label: 'in_review awaiting merge',
        command: 'kew epic merge ui-refine 2219'
      }
    },
    {
      said: 'a driver nobody is ticking',
      over: {
        type: 'stalled_epic',
        attention_kind: '',
        reason: 'driver gone — governance-hardening — no tick in 4h',
        label: '',
        command: 'kew epic run governance-hardening'
      }
    },
    {
      said: 'an orphan PR',
      over: {
        type: 'orphan_pr',
        attention_kind: '',
        reason: 'orphan PR — no issue on the branch',
        label: '',
        epic_ref: null,
        item_id: null,
        issue_num: null,
        command: ''
      }
    },
    {
      said: 'an approval',
      over: {
        type: 'approval_hold',
        attention_kind: 'approval',
        reason: 'needs-approval label on #1116',
        label: 'awaiting a spend approval',
        command: ''
      }
    }
  ];

  for (const { said, over } of notHolds) {
    it(`puts no unhold under ${said}, which no unhold clears`, () => {
      const detail = detailFor(over);
      expect(detail.note).toBe('');
      expect(detail.note).not.toContain('unhold');
      // And the row's own move, where the server filled one in, is untouched.
      expect(detail.command).toBe((over.command ?? '').trim());
    });
  }

  it('keeps the unbucketed hint for a HELD node the table has never heard of', () => {
    // The other half of the same rule: an `epic_holds` row IS a hold whatever
    // its reason says, an unhold IS what removes it, and `kew.waits`'
    // `UNBUCKETED_HELD_HINT` is what the CLI and the TUI print for exactly this.
    const detail = detailFor({ reason: 'something_new: a reason nobody has words for' });
    expect(detail.note).toBe('fix the cause, then kew epic unhold governance-hardening 1116');
  });

  it('says `held` only where the row`s own kind says it is held', () => {
    // `heldFor` words every age `held <age>`, which is true of every row Monitor
    // asks it about — all of them `epic_holds` rows. Most needs-you entries are
    // not holds, and `held 1h00m` over a merge decision asserts a hold the row
    // does not have. The word follows `attentionState`, the kind table the
    // list's own State column reads for these same rows.
    expect(detailFor().since).toBe('held 68d6h');
    expect(detailFor({ attention_kind: 'publication_held' }).since).toBe('held 68d6h');
    expect(detailFor({ attention_kind: 'approval' }).since).toBe('held 68d6h');
    for (const kind of ['merge_ready', 'merge_decision', 'work_exception', 'epic_complete', '']) {
      expect(detailFor({ type: 'epic_wait', attention_kind: kind }).since).toBe('waiting 68d6h');
    }
    // An unknown kind reads `manual` in that table — a person is next and
    // nothing more is known, which is not a hold either.
    expect(detailFor({ type: 'epic_wait', attention_kind: 'a_kind_from_2027' }).since).toBe(
      'waiting 68d6h'
    );
  });
});

describe('NeedsYouBlock.svelte, over an entry', () => {
  it('draws nothing at all for a row with no entry behind it', () => {
    expect(draw(null)).toBe('');
  });

  it('puts the command in a <code> block with a copy button beside it', () => {
    const html = draw(detailFor());
    // A real `<code>`: the Copy button is a convenience over text that is
    // already selectable, never the only copy of the command.
    expect(html).toMatch(/<code[^>]*>kew epic unhold governance-hardening 1116<\/code>/);
    expect(html).toContain('aria-label="Copy command (c)"');
    expect(html).toContain('>Copy<');
    expect(draw(detailFor(), true)).toContain('>Copied<');
  });

  it('prints the summary, Since and Do this, then Why behind why ▸ (#2536, #3467)', () => {
    // A sentence without the reason's tail, so the block has a Why to fold.
    const html = draw(detailFor({ sentence: 'the review round cap was reached' }));
    const order = ['Needs you', 'Since', 'Do this', 'Why'].map((k) => html.indexOf(`>${k}<`));
    expect(order.every((i) => i > -1)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    // The wait names itself in the head, beside "Needs you"; the one sentence
    // every surface prints is the lead under it (#2280 D9).
    expect(html).toContain('the review round cap was reached');
    expect(html).toContain('review pool $10.00 exhausted (spent $22.18)');
    expect(html).toContain('held 68d6h');
    expect(html).toContain('it restarts the review cycle');
  });

  it('prints the age once, under the label that says which reading it is', () => {
    // It was stamped in the section head AND set against `Since` two lines
    // below, so the age printed twice whenever it printed at all.
    const html = draw(detailFor());
    expect(html.match(/held 68d6h/g)).toHaveLength(1);
    // And it is the labelled one that survives: `held` and `waiting` are the
    // same field, and only the `Since` row says what the duration is OF.
    expect(html).toMatch(/>Since<\/dt><dd[^>]*>held 68d6h</);
  });

  it('names the wait to a screen reader rather than just saying "needs you"', () => {
    expect(draw(detailFor())).toContain(
      'aria-label="Needs you: held — the review round cap was reached'
    );
  });

  it('ends every new-tab anchor`s own text in ↗', () => {
    const html = draw(
      detailFor({ url: 'https://github.com/thossullivan/kew/pull/2231', pr_number: 2231 })
    );
    const anchors = html.match(/<a\b[^>]*>[\s\S]*?<\/a>/g) ?? [];
    expect(anchors.length).toBeGreaterThan(0);
    let blank = 0;
    for (const anchor of anchors) {
      if (!anchor.includes('target="_blank"')) continue;
      blank += 1;
      expect(anchor).toContain('rel="noreferrer"');
      // The text the reader sees, tags removed, must end in the arrow.
      expect(anchor.replace(/<[^>]*>/g, '').trim()).toMatch(/↗$/);
    }
    expect(blank).toBe(1);
    // And the in-app link does NOT claim to leave: an arrow there would be a
    // promise about a new tab that never opens.
    const inApp = anchors.find((a) => a.includes('/epics/governance-hardening'))!;
    expect(inApp).not.toContain('target=');
    expect(inApp).not.toContain('↗');
  });

  it('draws no `Do this` at all when the row has neither command nor action', () => {
    // An approval is decided on Monitor's own buttons: kew serves no command for
    // it, and the hold table has no action for a row that is not a hold. An
    // empty `Do this` label over nothing was the shape the unhold note hid.
    const html = draw(
      detailFor({ type: 'approval_hold', attention_kind: 'approval', command: '' })
    );
    expect(html).not.toContain('>Do this<');
    expect(html).not.toContain('<code');
    // The rest of the block still stands — the row still says what it is.
    expect(html).toContain('>Since<');
  });

  it('draws no command block for a row no kew command clears', () => {
    const html = draw(detailFor({ command: '', reason: 'budget_preflight: epic cap reached' }));
    expect(html).not.toContain('<code');
    expect(html).not.toContain('Copy command');
    // The action is still said — on a budget row it is the cap to raise, and
    // dropping it would leave the pane with no move at all.
    expect(html).toContain('Do this');
  });
});

/**
 * #2884 — a declaration hold's reason names one id and counts the rest, so the
 * block draws every id the server served, whole, under its own label.
 */
describe('a declaration hold', () => {
  const ids = [
    'tests/test_hosts_providers.py::TestGcloud::test_every_gcloud_call_runs_in_that_environment',
    'tests/test_hosts_providers.py::TestGcloud::test_subprocess_runs_ceiling_returns_but_leaves_the_child',
    'tests/test_serve_api_dashboard.py::TestDashboardReadsUnderLoad::test_a_slow_epic_reader_names_its_source'
  ];
  const declare = {
    reason: `test_declarations_missing: 3 undeclared: ${ids[0]} +2 more`,
    label: '',
    sentence: '',
    node_ids: ids
  };

  it('carries every served id into the block', () => {
    expect(detailFor(declare).nodeIds).toEqual(ids);
  });

  it('draws each id in full, with one copy action for all of them', () => {
    const html = draw(detailFor(declare));
    for (const id of ids) expect(html).toContain(`>${id}</code>`);
    expect(html.match(/data-node-id/g)?.length).toBe(ids.length);
    expect(html).toContain('aria-label="Copy 3 node ids"');
  });

  it('draws no Declare section for any other hold', () => {
    expect(detailFor().nodeIds).toEqual([]);
    expect(draw(detailFor())).not.toContain('data-declare-ids');
  });
});
