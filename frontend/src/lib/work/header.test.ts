/**
 * FUI-19 — the Work item header: what it may offer, and what it may claim.
 *
 * DWork draws three buttons — Claim, Hold, Flags — that kew has never offered.
 * The convergence rule (I26) is that every surface presses only what the
 * projection stood behind, so this suite reads the SAME committed golden matrix
 * `surface-convergence.test.ts` and the Python side read, and holds the header
 * to it case by case. A fixed button set could not survive that: seventeen
 * cases offer eight different answers, several of them nothing at all.
 *
 * There is no browser in this suite, but there is no need for one: the
 * component is SERVER-rendered (`svelte/server`), so the assertion is made
 * against the markup the header actually produces rather than against a model
 * that only claims to describe it. The source is read as well, for the two
 * things markup cannot show — that a button has no way into this file except
 * through `header.actions`, and that the write path is the dashboard's
 * `performWorkAction` / `applyWorkActionResult` / re-read-on-409 one.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { DashboardRow, Run, WorkProjection, WorkView } from '$lib/api';
import {
  CANONICAL_WORK_ACTIONS,
  exceptionLabel,
  exceptionTone,
  workActionLabel
} from '$lib/dashboard';
import { needsYouBlock } from '$lib/needs-you/block';
import ItemHeader from './ItemHeader.svelte';
import { headerCrumbs, headerMeta, itemException, itemHeader, itemSpend } from './header';
import { mergeWorkList, stateWord, type WorkRow } from './list';
import { epicHref } from './refs';

const FIXTURE = fileURLToPath(
  new URL('../../../../kew/tests/fixtures/product_coherence/work_views.json', import.meta.url)
);
const CASES = (
  JSON.parse(readFileSync(FIXTURE, 'utf8')) as { cases: Array<{ name: string; view: WorkView }> }
).cases;

const source = readFileSync(new URL('./ItemHeader.svelte', import.meta.url), 'utf8');

/** The row the list would select for this view — the header's real input. */
function rowFor(view: WorkView): WorkRow {
  const projection = { views: [view] } as unknown as WorkProjection;
  const rows = mergeWorkList({ projection, dashboard: null, runs: null });
  expect(rows).toHaveLength(1);
  return rows[0];
}

function run(issueNum: number, cost: number, runId = `r${issueNum}`): Run {
  return { run_id: runId, issue_num: issueNum, cost_usd: cost } as unknown as Run;
}

/** The header as it actually renders — server-rendered, so there is markup. */
function html(row: WorkRow, runs: Run[] | null = null): string {
  return render(ItemHeader, { props: { row, runs } }).body;
}

/** The exception paragraph — the line that says why the item is off-normal. */
function exceptionLine(drawn: string): string {
  // Anchored on the line's own offset, so its tone classes stay assertable:
  // both `text-danger` and `text-warn` appear only when the projection's
  // verdict puts them there.
  const line = [...drawn.matchAll(/<p\b([^>]*)>([\s\S]*?)<\/p>/g)].find((m) =>
    m[1].includes('mt-2.5')
  );
  expect(line, 'the header drew no exception line').toBeDefined();
  // The detail is folded behind `why ▸` (#2536): in the document, hidden,
  // and named by the toggle that opens it.
  const fold = drawn.match(/<p id="item-exception-why" hidden=""[^>]*>([\s\S]*?)<\/p>/);
  if (fold) expect(line![2]).toContain('aria-controls="item-exception-why"');
  return (line![1] + ' ' + line![2] + ' ' + (fold?.[1] ?? ''))
    .replace(/<[^>]+>/g, '')
    .replace(/\s+/g, ' ');
}

/**
 * The meta row's text, tags stripped — state, containment, spend, PR, run.
 *
 * Anchored on the row's own offset (`mt-3.5`), the way `exceptionLine` is
 * anchored on its own: the state cell is a component, so what it PRINTS is only
 * visible in the rendered markup.
 */
function metaRow(drawn: string): string {
  const at = drawn.indexOf('mt-3.5');
  expect(at, 'the header drew no meta row').toBeGreaterThan(-1);
  // `/<p\b/`, not `indexOf('<p')`: the failed X is an SVG `<path>`, and a plain
  // substring search for the exception paragraph stops inside the mark itself.
  const rest = drawn.slice(at);
  const end = rest.search(/<p\b/);
  return rest
    .slice(0, end > 0 ? end : undefined)
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ');
}

/** The same row with a needs-you entry carrying `reason` (#2280 D9). */
function withReason(row: WorkRow, reason: string): WorkRow {
  const entry = {
    type: 'work_exception',
    consequence: 'review',
    age: '2026-09-24T12:00:00+00:00',
    reason,
    primary_action: 'inspect',
    issue_num: null,
    item_id: row.itemId,
    estimate_usd: null,
    epic_ref: null,
    url: null,
    pr_number: null,
    attention_kind: 'work_exception'
  } satisfies DashboardRow;
  return { ...row, needsYou: true, attention: entry };
}

/**
 * The row with the set entry `work_reason` serves for an ORDINARY exception —
 * the item reference, the state in words, then the exception's own detail
 * (kew/service/attention.py). The block below reads that reason, so it is
 * carrying the sentence and the header must not print it a second time.
 */
function withEntry(row: WorkRow): WorkRow {
  const state = (row.view?.exception.state ?? '').replace(/_/g, ' ');
  return withReason(row, `${row.ref} — ${state}: ${row.view?.exception.detail ?? ''}`);
}

/**
 * The row with the set entry `work_reason` serves for a MERGE decision, which
 * is the branch that words itself and drops the detail:
 * `PR open for #2055 — awaiting review`. Nothing in the block below is that
 * sentence, so this is the shape that must keep the header's line.
 */
function withMergeEntry(row: WorkRow): WorkRow {
  return withReason(row, `PR open for ${row.ref} — awaiting review`);
}

/** The opening `<a ...>` tag the header drew for an href. `''` when it drew none. */
function openTagFor(drawn: string, href: string): string {
  for (const chunk of drawn.split('<a ')) {
    if (!chunk.startsWith('href="' + href + '"')) continue;
    return chunk.slice(0, chunk.indexOf('>'));
  }
  return '';
}

/** The text of every button the header drew, in the order it drew them. */
function renderedButtons(row: WorkRow): string[] {
  // A disclosure toggle (`aria-expanded`, the `why ▸` of #2536) opens text on
  // this screen; it is not a write, so it is not one of the offered actions.
  const actions = [...html(row).matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)].filter(
    (m) => !m[1].includes('aria-expanded')
  );
  return actions.map((m) =>
    m[2]
      .replace(/<!--[\s\S]*?-->/g, '')
      .replace(/<[^>]+>/g, '')
      .replace(/\s+/g, ' ')
      .trim()
  );
}

describe.each(CASES.map((c) => [c.name, c] as const))('%s — offered actions only', (_name, c) => {
  it('models the projection’s own offer, in its order, and nothing beside it', () => {
    expect(itemHeader({ row: rowFor(c.view), runs: null }).actions).toEqual(c.view.actions);
  });

  it('renders exactly that set — never a fixed Claim / Hold / Flags row', () => {
    expect(renderedButtons(rowFor(c.view))).toEqual(
      c.view.actions.map((a) => workActionLabel(a.action))
    );
  });

  it('never reaches for a verb this view withheld', () => {
    const offered = itemHeader({ row: rowFor(c.view), runs: null }).actions.map((a) => a.action);
    for (const verb of CANONICAL_WORK_ACTIONS) {
      if (c.view.actions.some((a) => a.action === verb)) continue;
      expect(offered).not.toContain(verb);
    }
  });

  it('asks for a reason exactly when one of the offers requires one', () => {
    expect(itemHeader({ row: rowFor(c.view), runs: null }).needsReason).toBe(
      c.view.actions.some((a) => a.requires_reason)
    );
  });
});

describe('the artboard’s fixed button set', () => {
  it('is a deviation: some views offer nothing, so the header draws nothing', () => {
    const empty = CASES.filter((c) => c.view.actions.length === 0);
    expect(empty.length).toBeGreaterThan(0);
    for (const c of empty) {
      const drawn = html(rowFor(c.view));
      expect(renderedButtons(rowFor(c.view))).toEqual([]);
      for (const invented of ['Claim', 'Hold', 'Flags']) expect(drawn).not.toContain(invented);
    }
  });

  it('is never a floor: the offers across the matrix disagree with each other', () => {
    const shapes = new Set(
      CASES.map((c) =>
        itemHeader({ row: rowFor(c.view), runs: null })
          .actions.map((a) => a.action)
          .join(',')
      )
    );
    expect(shapes.size).toBeGreaterThan(1);
  });
});

describe('the component can only draw what the model offered', () => {
  it('has no button of its own — every one comes from `header.actions`', () => {
    expect(source).toContain('{#each header.actions as action (action.action)}');
    expect(source).toContain('workActionLabel(action.action)');
    // One `<Button`, and it is the one inside that each-block.
    expect(source.match(/<Button/g) ?? []).toHaveLength(1);
    const each = source.indexOf('{#each header.actions');
    expect(source.indexOf('<Button')).toBeGreaterThan(each);
  });

  it('draws the breadcrumb, title and meta row it was given', () => {
    const drawn = html(rowFor(CASES[0].view), [run(4242, 2.5)]);
    expect(drawn).toContain('#4242');
    expect(drawn).toContain('/epics/epic-product-coherence');
    expect(drawn).toContain('$2.50');
    expect(drawn).toContain('max-w-[840px]');
  });

  it('never names D’s invented verbs', () => {
    for (const invented of ['Claim', 'Hold', 'Flags']) expect(source).not.toContain(`>${invented}<`);
    expect(source).not.toMatch(/'(claim|hold|flags)'/i);
  });

  it('writes through the one action path, and re-reads on a stale revision', () => {
    expect(source).toContain('performWorkAction(item, action, revision, reason)');
    expect(source).toContain('applyWorkActionResult(');
    // The 409 contract: transition from the server's view, then re-read.
    expect(source).toContain('if (out.view) onapplied?.(out.view);');
    expect(source).toContain('if (out.reread) await onstale?.();');
  });

  it('decides against the item and revision read BEFORE the await', () => {
    // A write takes a moment, and in that moment the reader can select another
    // row. Both the target and the revision are captured while the press is
    // still this row's, so nothing after the await re-reads them off a header
    // that has since become a different item's.
    const act = source.slice(source.indexOf('async function act('));
    const captureItem = act.indexOf('const item = header.itemId;');
    const captureRevision = act.indexOf('const revision = header.revision;');
    const awaited = act.indexOf('await performWorkAction(');
    expect(captureItem).toBeGreaterThan(-1);
    expect(captureRevision).toBeGreaterThan(captureItem);
    expect(awaited).toBeGreaterThan(captureRevision);
    const after = act.slice(awaited);
    expect(after).not.toContain('header.revision');
    // The one remaining read of the live item id is the guard below.
    expect(after.match(/header\.itemId/g) ?? []).toHaveLength(1);
  });

  it('paints the outcome only while the row it was fired for is still on screen', () => {
    // The regression: busy / chip / error written after the await with no check
    // put one item's outcome onto whatever item the reader had switched to.
    const act = source.slice(source.indexOf('async function act('));
    const awaited = act.indexOf('await performWorkAction(');
    const guard = act.indexOf('if (header.itemId === item) {');
    const handOver = act.indexOf('if (out.view) onapplied?.(out.view);');
    expect(guard).toBeGreaterThan(awaited);
    for (const write of ['busy = null;', 'chip = out.chip;', 'error = out.error;']) {
      const at = act.indexOf(write, guard);
      expect(at).toBeGreaterThan(guard);
      // Inside the guarded block, which closes before the callbacks.
      expect(at).toBeLessThan(handOver);
    }
    // Those three are the ONLY writes to this header's readout after the await.
    const after = act.slice(awaited);
    for (const field of ['busy', 'chip', 'error']) {
      expect(after.match(new RegExp(field + ' = ', 'g')) ?? []).toHaveLength(1);
    }
  });

  it('blocks a send that needs a reason nobody has written', () => {
    expect(source).toContain("action.requires_reason && reason.trim() === ''");
  });

  it('gives the reason field a real name, not just a placeholder', () => {
    const view = CASES.find((c) => c.view.actions.some((a) => a.requires_reason))!.view;
    const drawn = html(rowFor(view));
    // A placeholder is not an accessible name — it goes away on the first
    // keystroke — and the list's own search input already carries an sr-only
    // label, so the field that sends a refusal gets the same treatment.
    expect(drawn).toMatch(/<label class="sr-only" for="work-action-reason">[^<]+<\/label>/);
    expect(drawn).toContain('id="work-action-reason"');
    // No field, no label: a view that needs no reason draws neither.
    const quiet = CASES.find((c) => !c.view.actions.some((a) => a.requires_reason))!.view;
    expect(html(rowFor(quiet))).not.toContain('work-action-reason');
  });
});

describe('spend', () => {
  const runs = [run(4242, 1.5), run(4242, 1.6, 'r2'), run(99, 40)];

  it('sums the runs in hand for this item, and no other item’s', () => {
    expect(itemSpend(runs, '4242')).toEqual({ total: 3.1, runs: 2, current: null });
  });

  it('is absent, not $0.00, when no run in hand belongs to the item', () => {
    expect(itemSpend(runs, '2055')).toBeNull();
    expect(itemSpend(null, '4242')).toBeNull();
    // A Linear key never matches a run's numeric `issue_num`; an invented
    // $0.00 would read as "this cost nothing", which nothing said.
    expect(itemSpend(runs, 'SFD-12')).toBeNull();
  });

  it('is the yellow cell, and says what it was summed over', () => {
    const view = CASES[0].view;
    const cells = headerMeta({ row: rowFor(view), runs });
    const spend = cells.find((c) => c.key === 'spend');
    expect(spend).toMatchObject({ value: '$3.10', suffix: 'item total', tone: 'money' });
    expect(spend?.title).toContain('2 runs');
  });

  it('labels the run the item is on apart from the item total (#2786)', () => {
    // #2055 on the fixture: the implementing run cost $3.10 and a review run
    // $0.42 more. The epic card and rail print the $3.10; Work printed an
    // unlabelled "$3.52 spent" beside it.
    const view = structuredClone(CASES[0].view);
    view.identity.item_id = '2055';
    view.identity.run_id = 'f5dcb6a5';
    const both = [run(2055, 3.1, 'f5dcb6a5'), run(2055, 0.42, '9c41e2b7')];
    const cells = headerMeta({ row: rowFor(view), runs: both });
    const money = cells.filter((c) => c.tone === 'money').map((c) => `${c.value} ${c.suffix}`);
    expect(money).toEqual(['$3.10 this run', '$3.52 item total']);
    expect(metaRow(html(rowFor(view), both))).toContain('$3.10 this run');
  });

  it('draws one labelled number when the run is the whole item total', () => {
    const view = structuredClone(CASES[0].view);
    view.identity.item_id = '2055';
    view.identity.run_id = 'f5dcb6a5';
    const cells = headerMeta({ row: rowFor(view), runs: [run(2055, 3.1, 'f5dcb6a5')] });
    const money = cells.filter((c) => c.tone === 'money').map((c) => `${c.value} ${c.suffix}`);
    expect(money).toEqual(['$3.10 item total']);
  });

  it('leaves no cell behind when the shell has not polled the runs yet', () => {
    const keys = headerMeta({ row: rowFor(CASES[0].view), runs: null }).map((c) => c.key);
    expect(keys).not.toContain('spend');
  });
});

describe('the breadcrumb', () => {
  it('is the item, then a link to the epic the projection named', () => {
    const view = CASES[0].view;
    expect(headerCrumbs(rowFor(view))).toEqual([
      { key: 'item', text: '#4242', href: null },
      { key: 'epic', text: 'epic epic-product-coherence', href: '/epics/epic-product-coherence' }
    ]);
  });

  it('prints one crumb for an item in no epic — there is no "no epic"', () => {
    const view = structuredClone(CASES[0].view);
    view.identity.epic_ref = null;
    expect(headerCrumbs(rowFor(view)).map((c) => c.key)).toEqual(['item']);
  });

  it('falls back to the needs-you entry’s epic when the row has no view', () => {
    // #2246. An entry row has no projection view at all, so `identity.epic_ref`
    // is not available to read — and the row it stands for DOES name an epic.
    // Without this the pane for five of the live screen's seven rows showed the
    // item crumb alone, under a block whose "Show in epic" link named the epic
    // the crumb would not print.
    const entry = {
      type: 'budget_hold',
      consequence: 'epic',
      age: '2026-07-26T09:00:00+00:00',
      reason: 'review_rounds_exhausted: review pool $10.00 exhausted',
      primary_action: 'unhold',
      issue_num: 1116,
      item_id: '1116',
      estimate_usd: null,
      epic_ref: 'governance-hardening',
      url: null,
      pr_number: null
    } as const;
    const row: WorkRow = {
      itemId: '1116',
      number: '1116',
      ref: '#1116',
      title: 'the review round cap was reached',
      state: 'held',
      needsYou: true,
      view: null,
      tier: null,
      entry,
      attention: entry,
      href: '/work?item=1116',
      external: false
    };
    expect(headerCrumbs(row)).toEqual([
      { key: 'item', text: '#1116', href: null },
      {
        key: 'epic',
        text: 'epic governance-hardening',
        href: '/epics/governance-hardening'
      }
    ]);
    // And still one crumb when nothing in hand names an epic.
    expect(headerCrumbs({ ...row, entry: { ...entry, epic_ref: null } }).map((c) => c.key)).toEqual(
      ['item']
    );
  });

  it('treats the epic ref as opaque', () => {
    expect(epicHref('org/repo#42')).toBe('/epics/org%2Frepo%2342');
  });
});

describe('the meta row', () => {
  const view = CASES.find((c) => c.view.artifact.pr_url != null)!.view;

  it('is the artboard’s order, minus the cells kew has no fact for', () => {
    const cells = headerMeta({ row: rowFor(view), runs: [run(Number(view.identity.item_id), 2)] });
    expect(cells.map((c) => c.key)).toEqual(['state', 'containment', 'spend', 'pr', 'run']);
  });

  it('says the containment is declared, never that it was measured', () => {
    const containment = headerMeta({ row: rowFor(view), runs: null }).find(
      (c) => c.key === 'containment'
    );
    expect(containment?.value).toBe(view.route.containment);
    expect(containment?.title).toContain('declared');
    expect(containment?.title).toContain('not a measurement');
  });

  it('links the PR by its number, and the run by its short id', () => {
    const cells = headerMeta({ row: rowFor(view), runs: null });
    const pr = cells.find((c) => c.key === 'pr');
    expect(pr?.href).toBe(view.artifact.pr_url);
    expect(pr?.value).toBe(`#${view.artifact.pr_url!.split('/').pop()}`);
    const runCell = cells.find((c) => c.key === 'run');
    expect(runCell?.value).toBe(view.identity.run_id!.slice(0, 8));
    // The truncation is a display: the whole id is still in the hover text.
    expect(runCell?.title).toContain(view.identity.run_id!);
  });

  it('marks the PR as the one cell that leaves the console', () => {
    const cells = headerMeta({ row: rowFor(view), runs: null });
    expect(cells.find((c) => c.key === 'pr')?.external).toBe(true);
    // The run link is an in-app route, so it navigates in place.
    expect(cells.find((c) => c.key === 'run')?.external).toBe(false);
  });

  it('opens the forge link in a new tab, with rel="noreferrer"', () => {
    // Every other pr_url link in the app (runs, epics, the DAG) does this; a
    // same-tab forge link would drop the reader out of the console, and out of
    // the selection the URL is carrying.
    const cells = headerMeta({ row: rowFor(view), runs: null });
    const drawn = html(rowFor(view));
    const pr = openTagFor(drawn, cells.find((c) => c.key === 'pr')!.href!);
    expect(pr).toContain('target="_blank"');
    expect(pr).toContain('rel="noreferrer"');
    // The run link is an in-app route, so it navigates in place and gets neither.
    const runTag = openTagFor(drawn, cells.find((c) => c.key === 'run')!.href!);
    expect(runTag).toContain('href=');
    expect(runTag).not.toContain('_blank');
    expect(runTag).not.toContain('noreferrer');
  });

  it('falls back to a word when a PR url carries no number', () => {
    const odd = structuredClone(view);
    odd.artifact.pr_url = 'https://example.invalid/pulls/';
    expect(headerMeta({ row: rowFor(odd), runs: null }).find((c) => c.key === 'pr')?.value).toBe(
      'open'
    );
  });

  it('never links a served PR url that is not an absolute http(s) URL', () => {
    const refused = [
      'javascript:alert(document.domain)//pull/1',
      '/pull/1',
      ' java\tscript:x//pull/1'
    ];
    for (const bad of refused) {
      const odd = structuredClone(view);
      odd.artifact.pr_url = bad;
      const pr = headerMeta({ row: rowFor(odd), runs: null }).find((c) => c.key === 'pr');
      expect(pr?.href, bad).toBeNull();
      expect(pr?.external, bad).toBe(false);
      // The reference is still printed, as text.
      expect(pr?.value, bad).toBe('#1');
      const drawn = html(rowFor(odd));
      expect(drawn, bad).not.toContain('javascript:');
      expect(drawn, bad).not.toContain('href="/pull/1"');
    }
  });

  it('carries only the state word for an item the projection has never touched', () => {
    const idle: WorkRow = {
      itemId: '2055',
      number: '2055',
      ref: '#2055',
      title: 'Ready, never dispatched',
      state: 'idle',
      needsYou: false,
      view: null,
      tier: null,
      entry: null,
      attention: null,
      href: '/work?item=2055',
      external: false
    };
    expect(headerMeta({ row: idle, runs: null }).map((c) => c.key)).toEqual(['state']);
    const header = itemHeader({ row: idle, runs: null });
    expect(header.actions).toEqual([]);
    // No view, no revision: there is nothing an action could be decided against.
    expect(header.revision).toBe('');
    expect(header.title).toBe('Ready, never dispatched');
  });

  it('shows the state word the list row already showed', () => {
    const row = rowFor(view);
    expect(headerMeta({ row, runs: null })[0]).toMatchObject({
      kind: 'status',
      // The mark is still decided from the raw state…
      value: row.state,
      // …and the WORD is the one the list row prints.
      word: stateWord(row.state)
    });
  });

  /**
   * #2281 D13 — one status vocabulary, on one screen.
   *
   * Three run states spell their meaning differently from the word the mark
   * table gives it: `success`/done, `flawed`/failed, `manual`/needs you. When
   * the list row started printing `stateWord` and the header kept printing
   * `StatusMark`'s own echo of `kind`, those three read one way in the list and
   * another in the header of the SAME item, side by side above 1024 — the
   * invariant `headerMeta`'s own docstring claims.
   *
   * Rendered, not modelled: the drift was in the markup's defaulting, so a
   * model-level assertion would have passed throughout.
   */
  it('prints the row’s own word, for every state that spells its meaning differently', () => {
    const base = rowFor(view);
    for (const state of ['success', 'flawed', 'manual', 'in review', 'idle', 'working', 'held']) {
      const row: WorkRow = { ...base, state };
      const word = stateWord(state);
      expect(headerMeta({ row, runs: null })[0].word, state).toBe(word);
      // The header's meta row, with the tags stripped: the first word in it is
      // the state, and it is the list's word for the state.
      const meta = metaRow(html(row));
      expect(meta, state).toContain(word);
      // …and never the raw state where the two differ.
      if (state !== word) expect(meta, state).not.toContain(state);
    }
  });

  it('decides the mark from the raw state, so shape and word cannot disagree', () => {
    // `stateWord` resolves the meaning and `StatusMark` resolves the shape, and
    // both read `statusShape(state, 'run')`. The cell therefore hands the
    // component the state — not the word — or a `done`/`failed` word would be
    // re-resolved through the epic vocabulary on its way to a shape.
    expect(source).toContain('kind={meta.value}');
    expect(source).toContain('label={meta.word}');
    expect(source).toContain('vocabulary="run"');
  });
});

describe('the exception', () => {
  it('is the projection\u2019s own verdict, labelled and toned off `blocking`', () => {
    for (const c of CASES) {
      const e = c.view.exception;
      expect(itemException(e)).toEqual({
        state: e.state,
        label: exceptionLabel(e.state),
        detail: e.detail,
        tone: exceptionTone(e),
        blocking: e.blocking
      });
    }
  });

  it('is absent for an item that is not off-normal, and for one with no view', () => {
    const normal = structuredClone(CASES[0].view);
    normal.exception = { state: 'none', detail: '', blocking: false, dimension: 'execution' };
    expect(itemException(normal.exception)).toBeNull();
    expect(itemHeader({ row: rowFor(normal), runs: null }).exception).toBeNull();
    const idle: WorkRow = {
      itemId: '2055',
      number: '2055',
      ref: '#2055',
      title: 'Ready, never dispatched',
      state: 'idle',
      needsYou: false,
      view: null,
      tier: null,
      entry: null,
      attention: null,
      href: '/work?item=2055',
      external: false
    };
    expect(itemHeader({ row: idle, runs: null }).exception).toBeNull();
  });

  it('does not print the sentence the needs-you block below it carries', () => {
    // #2280 D10. The header used to stamp `exception.detail` under the title,
    // in mono and in a tone a reader takes for the accent, directly above a
    // block saying something else about the same wait. One wait, two
    // sentences, neither of them the other's summary. Where the block IS
    // saying this sentence, the block is the only surface that says it.
    for (const c of CASES) {
      const drawn = html(withEntry(rowFor(c.view)));
      if (c.view.exception.detail !== '') expect(drawn).not.toContain(c.view.exception.detail);
      // And no tone of it is left behind either: the accent-adjacent colours
      // went with the line they were on.
      expect(drawn).not.toContain('text-danger');
      expect(drawn).not.toContain('text-warn');
    }
  });

  it('is gated on the block`s own words, not on which payload the row came from', () => {
    // Both directions of the gate this replaced (`row.attention === null`),
    // over the whole committed matrix:
    //
    // * a row the SET names through a reason that omits the detail — the
    //   `awaiting_human_decision` branch of `work_reason`, which words itself
    //   `PR open for #2055 — awaiting review` — had the line suppressed, so
    //   the exception's own sentence was printed NOWHERE on /work;
    // * a row with a projection band and no set entry had it printed
    //   unconditionally, over a block already heading the same wait.
    //
    // One question answers both: does the block below contain this sentence?
    // So the line is drawn exactly when it does not — asserted over every
    // case in the matrix in both row shapes, and over the band-only row whose
    // `Why` is the projection's detail precisely so the header can stop
    // printing it.
    let said = 0;
    let carried = 0;
    for (const c of CASES) {
      const detail = c.view.exception.detail;
      for (const row of [rowFor(c.view), withMergeEntry(rowFor(c.view))]) {
        const block = needsYouBlock(row);
        const says = block !== null && (block.summary.includes(detail) || block.why.includes(detail));
        const drawn = itemHeader({ row, runs: null }).exceptionLine;
        expect(drawn === null).toBe(says);
        if (says) {
          carried += 1;
          // Nothing above the block repeats it, tone and all.
          expect(html(row)).not.toContain('text-danger');
        } else {
          said += 1;
          // And the sentence reaches the screen rather than nowhere.
          expect(html(row)).toContain(detail);
        }
      }
    }
    // Neither branch is vacuous: the matrix exercises both directions.
    expect(said).toBeGreaterThan(0);
    expect(carried).toBeGreaterThan(0);
  });

  it('says WHY the item is off-normal where no block below says it', () => {
    // The regression D10 reintroduced by deleting the line outright. A QUIET
    // exception (`QUIET_EXCEPTIONS` in kew/service/attention.py: `in_flight`,
    // `review_in_flight`, `terminal_failure`) is in no needs-you set and
    // raises no band, because kew is the next move and nobody is waited on \u2014
    // so `needsYouBlock` returns null and "the attempt ended killed" was
    // printed nowhere on /work at all. The same is true of every row while
    // `/api/dashboard` is unreadable. Those rows keep the line.
    const uncovered = CASES.filter((c) => needsYouBlock(rowFor(c.view)) === null);
    expect(uncovered.length).toBeGreaterThan(0);
    for (const c of uncovered) {
      const line = exceptionLine(html(rowFor(c.view)));
      expect(line).toContain(c.view.exception.detail);
      expect(line).toContain(exceptionLabel(c.view.exception.state));
      // Danger for a blocking exception, warn for one that is not \u2014 the same
      // pair `exceptionTone` hands every other surface.
      expect(line.includes('text-danger')).toBe(c.view.exception.blocking);
      expect(line.includes('text-warn')).toBe(!c.view.exception.blocking);
    }
  });

  it('draws no exception line when there is nothing off-normal to say', () => {
    const normal = structuredClone(CASES[0].view);
    normal.exception = { state: 'none', detail: '', blocking: false, dimension: 'execution' };
    const drawn = html(rowFor(normal));
    expect(drawn).not.toContain('text-danger');
    expect(drawn).not.toContain('none');
  });

  it('keeps the exception on the MODEL, which every other reader still has', () => {
    // The fact and the drawing are two fields: `itemHeader.exception` is what
    // the convergence fixture reads whichever surface prints the sentence, and
    // the state word the header does print is the one the exception decides.
    for (const c of CASES) {
      const row = rowFor(c.view);
      for (const header of [
        itemHeader({ row, runs: null }),
        itemHeader({ row: withEntry(row), runs: null })
      ]) {
        expect(header.exception?.detail).toBe(c.view.exception.detail);
        expect(header.exception?.label).toBe(exceptionLabel(c.view.exception.state));
      }
      // Whether the LINE is drawn is the block's question, not this one's:
      // `exception` is the fact and survives either answer.
      expect(itemHeader({ row: withEntry(row), runs: null }).exceptionLine).toBeNull();
    }
  });
});

describe('the title', () => {
  it('falls back to the item ref when no payload in hand named the item', () => {
    const row = rowFor(CASES[0].view);
    expect(row.title).toBe('');
    expect(itemHeader({ row, runs: null }).title).toBe(row.ref);
  });
});
