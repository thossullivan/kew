/**
 * #2250 — the popover beside a selected DAG node, and the Esc rule over it.
 *
 * The popover is what P6 traded the node's own GitHub link for, so the three
 * ways it could be worse than that link are the three things asserted here.
 *
 * 1. **It opens with no fetch.** Every word comes off the snapshot the page
 *    already has — title, status, `status_since`, `attention`, `url`, `pr_url`
 *    — plus the drain the economics payload already carried. A popover that
 *    had to ask the server would open empty on the one click a reader makes
 *    most.
 * 2. **It never invents a URL, and it admits when a link leaves kew.**
 *    "Open in Work" is an in-app route; the external links are the two URLs
 *    the server served and nothing composed out of a repo slug; every
 *    `target="_blank"` anchor ends its own text in `↗`.
 * 3. **It says the wait in the server's own words.** The reason is
 *    `attention.reason` and the command is `attention.command` — a sentence
 *    re-derived some other way would be the two-readings defect #2207 exists
 *    to prevent, one screen along.
 *
 *    And the command is the SERVED one, never one read back out of
 *    `attention.action`. `action` is `kew.waits`' prose about the move, so the
 *    leading words of every shape it has are argument-less: the first round of
 *    this popover put `kew epic unhold`, `kew epic retry the` and `kew epic
 *    continue <name> <node>` under a Copy button, and all three exit 2.
 *    `attention.command` is `fill_command`'s output, which is served only when
 *    it is runnable as printed (#2244).
 *
 * Plus the Esc reducer, which is two steps over two booleans and so is exactly
 * the kind of rule that ends up implemented twice and then disagrees: once in
 * the canvas and once in the page.
 *
 * The component is SERVER-rendered, as `EntryDetail` and `Pipeline` are in
 * their own suites: the pixels are read off the markup it really produces.
 */
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import { plainCommands } from '$lib/ui/command-markup';
import type { EpicNode, EpicNodeAttention, EpicNodeRun, PrFreshnessRow } from '$lib/api';
import NodePopover from './NodePopover.svelte';
import { actionNote, escape, linkOpensPopover, NO_FOCUS, nodePopover, runHistory, select } from './node-popover';

/** A fixed clock: "in review · 12m" has to be the same string every run. */
const NOW_MS = Date.parse('2026-10-02T15:00:00+00:00');

/**
 * Every `action` hint `kew.waits` really writes that begins with an
 * invocation, verbatim from `STALL_BUCKETS` — plus the one `kew epic status`
 * prints for a failed-terminal node. NONE of them is a line anybody can run,
 * which is why none of them may reach the Copy button.
 */
const REAL_HINTS = [
  'kew epic continue <name> <node>',
  'kew epic unhold, which also restarts the review cycle',
  'kew epic retry the node(s)'
];

/** The round cap's real hint, and the command the server fills for it. */
const UNHOLD_HINT = 'kew epic unhold, which also restarts the review cycle';
const UNHOLD_COMMAND = 'kew epic unhold ui-ux 2219';

const node = (over: Partial<EpicNode> = {}): EpicNode => ({
  id: '2219',
  title: 'one needs-you source for the header, Monitor, epic legend and Work',
  status: 'in_review',
  status_since: '2026-10-02T14:48:00+00:00',
  url: 'https://github.com/thossullivan/kew/issues/2219',
  pr_url: 'https://github.com/thossullivan/kew/pull/2231',
  external: false,
  stuck: null,
  attention: null,
  blocked_reason: null,
  ...over
});

const attention = (over: Partial<EpicNodeAttention> = {}): EpicNodeAttention => ({
  kind: 'merge_decision',
  reason: 'PR #2231 is open and green; nobody has merged it',
  url: 'https://github.com/thossullivan/kew/pull/2231',
  action: UNHOLD_HINT,
  command: UNHOLD_COMMAND,
  ...over
});

const draw = (over: Partial<EpicNode> = {}, drain: number | null = 12.34): string =>
  plainCommands(render(NodePopover, { props: { model: nodePopover(node(over), drain, NOW_MS) } }).body);

describe('nodePopover — the snapshot in hand, and nothing else', () => {
  it('reads the status and how long off `status` and `status_since`', () => {
    expect(nodePopover(node(), null, NOW_MS).status).toBe('in review · 12m');
  });

  it('prints no duration at all when `status_since` is unreadable', () => {
    // A fabricated duration is worse than none: `format.ts`'s rule.
    for (const since of [null, '', 'not a timestamp']) {
      expect(nodePopover(node({ status_since: since }), null, NOW_MS).status).toBe('in review');
    }
  });

  it('keeps the status word apart from its age, so the word is one element', () => {
    expect(nodePopover(node(), null, NOW_MS)).toMatchObject({ word: 'in review', age: '12m' });
    expect(nodePopover(node({ status_since: null }), null, NOW_MS)).toMatchObject({
      word: 'in review',
      age: ''
    });
  });

  it('draws the status word in its own element under the 390 sweep\'s hook', () => {
    // `data-status-word` is what `status-words.mjs` reads (#2898); the age
    // beside it is not part of the word, so it is not inside the hook.
    expect(draw()).toMatch(/<span[^>]*data-status-word[^>]*>in review<\/span>/);
  });

  it('says the reason the driver recorded, which the card no longer prints (#2543)', () => {
    const blocked = node({ status: 'blocked', blocked_reason: '  needs a design decision ' });
    expect(nodePopover(blocked, null, NOW_MS).recorded).toBe('needs a design decision');
    expect(draw({ status: 'blocked', blocked_reason: 'needs a design decision' })).toContain(
      '<p class="rec'
    );
    expect(nodePopover(node(), null, NOW_MS).recorded).toBe('');
    expect(draw()).not.toContain('class="rec');
  });

  it('takes the reason and the command from `attention`, and nowhere else', () => {
    const m = nodePopover(node({ attention: attention() }), 1.5, NOW_MS);
    expect(m.reason).toBe('PR #2231 is open and green; nobody has merged it');
    expect(m.command).toBe(UNHOLD_COMMAND);
    expect(m.note).toBe('which also restarts the review cycle');
    expect(m.mark).toBe('needs_you');
    expect(m.drain).toBe('$1.50');
  });

  it('prints the SERVED command, never one read out of the action prose', () => {
    // The defect this replaced: `kew epic unhold` / `kew epic retry the` /
    // `kew epic continue <name> <node>` under a Copy button, each exiting 2 on
    // a missing argument. An attention with no `command` has nothing runnable
    // to offer, and the honest reading of that is no command line at all.
    for (const action of REAL_HINTS) {
      const m = nodePopover(node({ attention: attention({ action, command: '' }) }), null, NOW_MS);
      expect(m.command).toBe('');
      expect(m.command).not.toContain('kew');
    }
  });

  it('prints the served command whatever the action prose says', () => {
    // Shape 3: prose that only MENTIONS an unhold. The command is still the
    // server's, and the sentence stays whole as the note.
    const action = 're-check the worktree its reason names, then kew epic unhold';
    const m = nodePopover(
      node({ attention: attention({ action, command: 'kew epic unhold ui-ux 2219' }) }),
      null,
      NOW_MS
    );
    expect(m.command).toBe('kew epic unhold ui-ux 2219');
    expect(m.note).toBe(action);
  });

  it('reads a serve that predates `attention.command` as no command', () => {
    const stale = { ...attention() } as Record<string, unknown>;
    delete stale.command;
    const m = nodePopover(
      node({ attention: stale as unknown as EpicNodeAttention }),
      null,
      NOW_MS
    );
    expect(m.command).toBe('');
  });

  it('says nothing about a wait on a node nobody is waiting on', () => {
    const m = nodePopover(node(), null, NOW_MS);
    expect([m.reason, m.command, m.note, m.drain]).toEqual(['', '', '', '']);
    // The mark is then the node's real status, not the magenta diamond.
    expect(m.mark).toBe('in_review');
  });

  it('is pure: same node, same clock, same reading', () => {
    // No fetch, no `Date.now()` inside — which is what lets the canvas open it
    // on a click rather than on a response.
    const once = nodePopover(node({ attention: attention() }), 12.34, NOW_MS);
    const twice = nodePopover(node({ attention: attention() }), 12.34, NOW_MS);
    expect(once).toEqual(twice);
  });

  it('links to Work in-app, and to the two URLs the server served', () => {
    const m = nodePopover(node(), null, NOW_MS);
    expect(m.links).toEqual([
      { key: 'work', label: 'Open in Work', href: '/work?item=2219', external: false }
    ]);
    expect(m.external.map((l) => [l.label, l.href])).toEqual([
      ['Issue', 'https://github.com/thossullivan/kew/issues/2219'],
      ['PR #2231', 'https://github.com/thossullivan/kew/pull/2231']
    ]);
  });

  it('names a PR only off its own URL, never off a composed slug', () => {
    expect(nodePopover(node({ pr_url: 'https://example.invalid/x' }), null, NOW_MS).external[1])
      .toMatchObject({ label: 'PR' });
  });

  // C10 (#3470): a served URL is a link only through safeHref.
  it.each([
    ['a javascript: URL', 'javascript:alert(1)'],
    ['a control-split scheme', 'java\u0000script:alert(1)'],
    ['a relative path', '/work?item=2219'],
    ['an anchor', '#changes'],
    ['a protocol-relative URL', '//evil.example/pull/1']
  ])('links no issue or PR from %s', (_, url) => {
    const m = nodePopover(node({ url, pr_url: url }), null, NOW_MS);
    expect(m.external).toEqual([]);
  });

  it('grows no external link for a node with no URL', () => {
    const m = nodePopover(node({ url: null, pr_url: null }), null, NOW_MS);
    expect(m.external).toEqual([]);
    // "Open in Work" stays: the Work route exists for every node id.
    expect(m.links).toHaveLength(1);
  });

  it('treats whitespace as nothing written', () => {
    const m = nodePopover(
      node({
        url: '   ',
        pr_url: '',
        attention: attention({ reason: '  ', action: ' ', command: '  ' })
      }),
      null,
      NOW_MS
    );
    expect([m.external, m.reason, m.command, m.note]).toEqual([[], '', '', '']);
  });
});

describe('actionNote — what the hint says beyond the command', () => {
  it('keeps the clause `kew.waits` writes after the invocation', () => {
    // The round cap's real hint (`waits.py`, ROUNDS_EXHAUSTED_HOLD_REASON).
    // The invocation in it is argument-less, so the popover prints the served
    // command and this clause under it — not the clause's own `kew epic
    // unhold`, which exits 2 on "Missing argument 'NAME'".
    expect(actionNote('kew epic unhold, which also restarts the review cycle')).toBe(
      'which also restarts the review cycle'
    );
    expect(actionNote('kew epic unhold ui-ux 2219 — it restarts the review cycle')).toBe(
      'it restarts the review cycle'
    );
  });

  it('says nothing more for a hint that is only an invocation', () => {
    for (const hint of ['kew epic unhold ui-ux 2219', 'kew epic continue <name> <node>']) {
      expect(actionNote(hint)).toBe('');
    }
  });

  it('keeps prose whole, however much of a command it mentions', () => {
    // A hint that does not START with an invocation is an instruction, and the
    // command it names is the last thing it asks for.
    for (const hint of [
      'map [sandbox] images.<runner>, then kew epic unhold',
      're-check the worktree its reason names, then kew epic unhold',
      'fix the cause, then kew epic unhold',
      'merge to resume'
    ]) {
      expect(actionNote(hint)).toBe(hint);
    }
  });

  it('answers for nothing written', () => {
    for (const raw of [null, undefined, '', '   ']) {
      expect(actionNote(raw)).toBe('');
    }
  });

  it('never leaves a bare `kew` invocation in the note either', () => {
    // The note is prose under the command, not a second command: no reading of
    // a real hint may put an argument-less invocation at the START of it.
    for (const hint of REAL_HINTS) {
      expect(actionNote(hint).startsWith('kew ')).toBe(false);
    }
  });
});

describe('the Esc reducer', () => {
  it('closes the popover first and KEEPS the selection', () => {
    // The rail is the panel the operator is actually reading, so the box over
    // the graph goes before the node does.
    expect(escape({ selected: '2219', popover: true })).toEqual({
      selected: '2219',
      popover: false
    });
  });

  it('clears the selection on the second press', () => {
    expect(escape({ selected: '2219', popover: false })).toEqual(NO_FOCUS);
  });

  it('is idempotent once there is nothing left to drop', () => {
    expect(escape(NO_FOCUS)).toEqual(NO_FOCUS);
  });

  it('selecting a node does both of the brief`s two things at once', () => {
    expect(select('2219')).toEqual({ selected: '2219', popover: true });
    // And selecting nothing is never a popover with no node under it.
    expect(select(null)).toEqual(NO_FOCUS);
  });
});

describe('NodePopover — what the markup owes the reader', () => {
  it('offers "Open in Work" with an in-app href', () => {
    const html = draw();
    expect(html).toContain('Open in Work');
    expect(html).toMatch(/<a[^>]*href="\/work\?item=2219"/);
  });

  it('ends every new-tab anchor`s own text in ↗, and says `rel`', () => {
    const html = draw();
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
    expect(blank).toBe(2);
    // And the in-app link does NOT claim to leave: an arrow there would be a
    // promise about a route that never opens a tab.
    const inApp = anchors.filter((a) => a.includes('/work?item=2219'));
    expect(inApp).toHaveLength(1);
    // The artboard's in-app arrow is → (stay in kew), never ↗.
    expect(inApp[0].replace(/<[^>]*>/g, '').trim()).toBe('Open in Work →');
  });

  it('shows no external link for a node with no URL', () => {
    const html = draw({ url: null, pr_url: null });
    expect(html).not.toContain('target="_blank"');
    expect(html).not.toContain('↗');
    expect(html).toContain('Open in Work');
  });

  it('prints the command as selectable text, not only as a Copy button', () => {
    // EntryDetail's rule: the button is a convenience over text that is
    // already there, never the only copy of the command.
    const html = draw({ attention: attention() });
    expect(html).toMatch(/<code[^>]*>kew epic unhold ui-ux 2219<\/code>/);
    expect(html).toContain('Copy');
    expect(html).toContain('which also restarts the review cycle');
    expect(html).toContain('PR #2231 is open and green; nobody has merged it');
  });

  it('tags the command `data-command` and draws it in --text-1, not cyan (#2540 D5)', () => {
    // The browser check reads every `code[data-command]` as the browser
    // resolved it, so an untagged command is one it cannot see. The colour
    // itself is this component's scoped `.cmd code` rule.
    const html = draw({ attention: attention() });
    expect(html).toMatch(/<code[^>]*\bdata-command\b[^>]*>kew epic unhold ui-ux 2219<\/code>/);
    const source = readFileSync(fileURLToPath(new URL('./NodePopover.svelte', import.meta.url)), 'utf8');
    // The `code` is the shared `Command`'s (#3052 N4), so the rule reaches it as `:global`.
    const rule = source.match(/\.cmd :global\(code\)\s*\{([^}]*)\}/);
    expect(rule).not.toBeNull();
    expect(rule![1]).toMatch(/(^|[\s;])color:\s*var\(--text-1\)/);
    expect(rule![1]).not.toContain('--accent-2');
  });

  it('leaves no command `<code>` in the console coloured cyan (#2540 D5)', () => {
    // The popover was the surface the D5 pass missed. Any `<code>` carrying an
    // `accent-2` class, or a scoped `code` rule painting `--accent-2`, is a
    // command drawn cyan on a surface the browser check may never open.
    const root = fileURLToPath(new URL('../..', import.meta.url));
    const cyan: string[] = [];
    const walk = (dir: string) => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const path = join(dir, entry.name);
        if (entry.isDirectory()) walk(path);
        else if (entry.name.endsWith('.svelte')) {
          const text = readFileSync(path, 'utf8');
          for (const tag of text.match(/<code\b[^>]*>/g) ?? []) {
            if (tag.includes('accent-2')) cyan.push(`${path}: ${tag}`);
          }
          for (const [, selector, body] of text.matchAll(/([^{}]*\bcode\b[^{}]*)\{([^}]*)\}/g)) {
            if (/(^|[\s;])color:\s*var\(--accent-2\)/.test(body)) cyan.push(`${path}: ${selector.trim()}`);
          }
        }
      }
    };
    walk(root);
    expect(cyan).toEqual([]);
  });

  it('draws no `$` prompt and no Copy button when no command was served', () => {
    // A Copy button over a line that exits 2 is worse than no button at all.
    const html = draw({ attention: attention({ command: '' }) });
    expect(html).not.toContain('Copy');
    expect(html).not.toContain('<code');
    // The prose the hint adds is still there — it is what the operator reads.
    expect(html).toContain('which also restarts the review cycle');
  });

  it('says "no PR" when the node has none, as the artboard does', () => {
    expect(draw({ pr_url: null })).toContain('no PR');
    expect(draw()).not.toContain('no PR');
  });

  it('names the node to a screen reader', () => {
    expect(draw()).toContain(
      'aria-label="#2219 one needs-you source for the header, Monitor, epic legend and Work"'
    );
  });

  it('describes itself by its own sentence, under an id of its own (#3056)', () => {
    const html = draw({ attention: attention() });
    const target = html.match(/aria-describedby="([^"]+)"/)?.[1] ?? '';
    expect(target).not.toBe('');
    expect(target).not.toBe('pop-why');
    expect(html).toMatch(new RegExp(`id="${target}"[^>]*>PR #2231 is open and green; nobody has merged it<`));
    expect(html).not.toContain('pop-why');
  });

  it('describes itself by nothing when there is no sentence (#3056)', () => {
    expect(draw()).not.toContain('aria-describedby');
  });
});

/**
 * D5 — at 390px the popover covered the very node it was about: there is no
 * side of a card a 320px panel can sit beside on a phone canvas. Below 1024
 * it is a bottom sheet instead (DESIGN.md Responsive Behavior), and every
 * control in it is at the 32px a thumb needs (D25).
 */
describe('NodePopover as a bottom sheet (D5)', () => {
  const sheet = (): string =>
    render(NodePopover, {
      props: { model: nodePopover(node(), 12.34, NOW_MS), sheet: true }
    }).body;

  it('marks itself a sheet, so its width is the canvas`s and not 320px', () => {
    expect(sheet()).toMatch(/class="[^"]*\bsheet\b/);
    // And it is still the popover: a sheet is a placement, not a new panel.
    expect(sheet()).toContain('role="dialog"');
  });

  it('is a box beside its card by default — the desktop reading is unchanged', () => {
    expect(draw()).not.toMatch(/class="[^"]*\bsheet\b/);
  });

  it('grows every control in it to 32px (D25)', () => {
    const html = sheet();
    // Copy, "Open in Work" and the two ↗ links: h-7/h-[30px] on the desktop
    // box, h-8 in the sheet.
    expect(html).not.toContain('h-[30px]');
    expect(html).not.toMatch(/\bh-7\b/);
    expect((html.match(/\bh-8\b/g) ?? []).length).toBeGreaterThanOrEqual(2);
  });
});

describe('the popover lists each staleness cause, with its commit (#2303)', () => {
  const freshness = (over: Partial<PrFreshnessRow> = {}): PrFreshnessRow => ({
    item_id: '2219',
    epic_ref: 'ui-ux',
    pr_number: 2231,
    pr_url: 'https://github.com/thossullivan/kew/pull/2231',
    last_verdict: {
      round: 2,
      verdict: 'READY',
      head_sha: '9f8e7d1c4b2a0e6f3d5c8b1a7e9f2c4d6b8a0e3f',
      recorded_at: '2026-10-02T11:12:00+00:00'
    },
    latest_head: {
      sha: 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678',
      source: 'snapshot',
      at: '2026-10-02T14:02:00+00:00'
    },
    review_state: 'stale',
    base: {
      state: 'clean',
      reason: 'the head merges into its base',
      since: null,
      at: null,
      source: 'snapshot'
    },
    observed_at: '2026-10-02T14:48:00+00:00',
    ...over
  });

  const drawFresh = (row: PrFreshnessRow | null): string =>
    render(NodePopover, {
      props: { model: nodePopover(node(), 12.34, NOW_MS, row) }
    }).body;

  it('prints the badge, the cause sentence and the commit link', () => {
    const html = drawFresh(freshness());

    expect(html).toContain('review stale');
    expect(html).toContain('the head moved to a1b2c3d');
    expect(html).toContain('9f8e7d1');
    expect(html).toContain(
      'href="https://github.com/thossullivan/kew/pull/2231/commits/a1b2c3d"'
    );
  });

  it('marks the commit link as leaving kew, like every other anchor here', () => {
    const html = drawFresh(freshness());
    const anchor = html.slice(html.indexOf('/commits/a1b2c3d'));

    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noreferrer"');
    expect(anchor).toContain('↗');
  });

  it('says what an unreadable base means rather than drawing nothing', () => {
    const reason = 'the driver has not journaled a merge state for this pull request yet';
    const html = drawFresh(
      freshness({
        review_state: 'current',
        base: { state: 'unknown', reason, since: null, at: null, source: 'unobserved' }
      })
    );

    expect(html).toContain(reason);
  });

  it('draws no freshness block at all when there is nothing to say', () => {
    const clean = freshness({ review_state: 'current' });

    expect(drawFresh(clean)).not.toContain('review stale');
    expect(drawFresh(clean)).not.toContain('class="fresh"');
    // ...and a page that could not read the endpoint draws nothing either,
    // rather than claiming the artifact is current.
    expect(drawFresh(null)).not.toContain('class="fresh"');
  });

  it('is absent from the model when no freshness row was handed over', () => {
    const model = nodePopover(node(), null, NOW_MS);

    expect(model.badges).toEqual([]);
    expect(model.baseNote).toBe('');
  });
});

describe('runHistory (#2544): the popover leads with the node\'s runs', () => {
  const NOW = Date.parse('2026-10-08T12:00:00Z');
  const run = (run_id: string, started_at: string | null, status = 'success', cost_usd = 0.5): EpicNodeRun => ({
    run_id,
    status,
    model: 'm',
    runner: 'r',
    cost_usd,
    total_tokens: 0,
    cache_creation_tokens: 0,
    cache_read_tokens: 0,
    pr_url: null,
    started_at,
    finished_at: null
  });

  it('draws nothing while the read is in flight', () => {
    expect(runHistory(undefined, NOW)).toBeNull();
  });

  it('says why when the read failed, rather than "no runs"', () => {
    expect(runHistory({ available: false, reason: 'node spend unreadable (500)' }, NOW)).toEqual({
      runs: [],
      summary: 'node spend unreadable (500)'
    });
  });

  it('lists the last three runs newest first, each linked to its run page', () => {
    const h = runHistory(
      {
        id: '2469',
        runs: [
          run('r1', '2026-10-08T09:00:00Z'),
          run('r3', '2026-10-08T11:00:00Z', 'review_failed', 1.25),
          run('r0', null),
          run('r2', '2026-10-08T10:00:00Z')
        ],
        lifetime_usd: 2.75,
        drain_usd: 0
      },
      NOW
    )!;
    expect(h.summary).toBe('4 runs · $2.75');
    expect(h.runs.map((r) => r.id)).toEqual(['r3', 'r2', 'r1']);
    expect(h.runs[0]).toEqual({
      id: 'r3',
      href: '/runs/r3',
      verdict: 'review failed',
      cost: '$1.25',
      ago: '1h00m ago'
    });
  });

  it('renders the history before the status line, linking in-app', () => {
    const html = render(NodePopover, {
      props: {
        model: nodePopover(node(), null, NOW),
        history: runHistory(
          { id: '2219', runs: [run('r9', '2026-10-08T11:30:00Z')], lifetime_usd: 0.5, drain_usd: 0 },
          NOW
        )
      }
    }).body;
    expect(html).toContain('href="/runs/r9"');
    expect(html.indexOf('data-history')).toBeLessThan(html.indexOf('class="state'));
  });
});

describe('linkOpensPopover (#3149): `?node=` names the selection, so only a new node or page opens one', () => {
  const at = (path: string) => new URL(path, 'http://kew.test');
  const epic = '/epics/ledger-v2';

  it('opens on the first reading of a node link', () => {
    expect(linkOpensPopover(null, at(`${epic}?node=104`))).toBe(true);
  });

  it('opens on a link to a node the address did not name, on the same page', () => {
    expect(linkOpensPopover({ path: epic, node: null }, at(`${epic}?node=104`))).toBe(true);
    expect(linkOpensPopover({ path: epic, node: '108' }, at(`${epic}?node=104`))).toBe(true);
  });

  it('opens on a node link from another page, whatever that page named', () => {
    expect(linkOpensPopover({ path: '/epics', node: '104' }, at(`${epic}?node=104`))).toBe(true);
  });

  it('leaves a shut popover shut when the navigation lands on the node already selected, with any key added', () => {
    expect(linkOpensPopover({ path: epic, node: '104' }, at(`${epic}?node=104`))).toBe(false);
    expect(linkOpensPopover({ path: epic, node: '104' }, at(`${epic}?node=104&view=timeline&tour=x&step=2`))).toBe(
      false
    );
  });

  it('never opens without a node, or on a replay link, which keeps its own first-frame rule', () => {
    expect(linkOpensPopover(null, at(epic))).toBe(false);
    expect(linkOpensPopover({ path: epic, node: '104' }, at(epic))).toBe(false);
    expect(linkOpensPopover(null, at(`${epic}?mode=replay&t=0&node=104`))).toBe(false);
    expect(linkOpensPopover({ path: epic, node: null }, at(`${epic}?mode=replay&t=0&node=104`))).toBe(false);
  });
});
