/**
 * #2285 (C7, C8, C10, C11, C13, C17, C18) — the factory's reading habits.
 *
 * Design review cycle 1 found the same shape of defect on five surfaces: a poll
 * that nobody had counted. Two pages read `/api/dashboard` on every tick when
 * one shared pulse already had it; every Epic page open drained a whole journal
 * to fill a disclosure that starts collapsed; Loops fetched each selection
 * twice and then never looked at its list again; and every one of them kept
 * asking while the tab was in the background.
 *
 * None of that is visible from a payload or a pure reading, which is why all of
 * it survived a green suite. It IS visible in the source, so the source is
 * what this file reads — the same instrument `work/page.select-in-place.test.ts`
 * and `menus.test.ts` use for the rules they protect. The behaviour that can be
 * driven is driven elsewhere: `lib/shell/pulse.client.test.ts` for the pulse's
 * three guards, `lib/ui/contained.client.test.ts` for the modal's focus. The
 * legacy Console these rules first covered is gone (#3454).
 *
 * "A hidden tab makes no requests" is no longer read here (#2789, K4). It was a
 * search for `document.hidden` over a hand-kept list of files, and two polls
 * that were not on the list kept asking in the background while it stayed
 * green. The browser now counts the requests themselves:
 * `scripts/ui-checks/hidden-tab.mjs`, run by `npm run ui-check`.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const read = (path: string): string =>
  readFileSync(fileURLToPath(new URL(path, import.meta.url)), 'utf8');

/**
 * The source with its comments stripped. A comment at a removal names what it
 * removed — that is the point of writing one — so a count of `disabled`, of
 * `aria-modal` or of an Escape handler has to be taken over the code alone.
 */
function markup(source: string): string {
  return source
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|\n)[ \t]*\/\/.*/g, '$1');
}

const loops = read('./loops/+page.svelte');
const epic = read('./epics/[ref]/+page.svelte');
const config = read('./config/+page.svelte');
const runEvents = read('../../lib/runs/RunEvents.svelte');

describe('the Epic page (C10, C17)', () => {
  it('seeds the ticker when the ticker is opened, not on every page open', () => {
    // The seed is a full journal drain (`fetchAllTicks` pages up to
    // MAX_TICK_PAGES), and the ticker starts collapsed.
    expect(epic).toContain('if (!connected || !tickerOpen || tickerSeededFor === epicRef) return;');
    // Paid at most once per epic: a close-and-reopen must not drain it again.
    expect(epic).toContain('tickerSeededFor = epicRef;');
  });

  it('shows the ticker count only where the seed has been paid', () => {
    // The lazy seed is only honest if nothing OUTSIDE the disclosure reads the
    // lines it fills: the count sat on the always-visible control, so a
    // collapsed ticker said 0 for a completed epic with a full journal.
    const code = markup(epic);
    expect(code).toContain(
      '{#if tickerOpen}<span class="text-text-2">{currentTicker.length}</span>'
    );
    // One reader of the count, and it is inside a `tickerOpen` branch.
    expect(code.match(/currentTicker\.length/g)).toHaveLength(1);
  });

  it('merges the seed under the lines SSE already delivered', () => {
    // Standing down whenever the ticker was non-empty cost a live epic its
    // whole journal tail — the seed is one-shot, so nothing came back for it.
    // `seededTicker` drops the overlap instead (lib/epic/live.test.ts drives it).
    expect(epic).toContain('live = { ...live, ticker: seededTicker(live.ticker, all) };');
    expect(epic).not.toContain('live.ticker.length > 0');
  });

  it('keeps asking when the first load failed (C17)', () => {
    expect(epic).toContain('let reconnected = $state<{ ref: string; detail: EpicDetail } | null>');
    expect(epic).toContain('const connected = $derived(detail != null);');
    // The retry disarms itself the moment the server answers.
    expect(epic).toContain('if (connected) return;');
  });
});

describe('Loops (C11)', () => {
  it('loads a selection exactly once', () => {
    // Both `selectLoop` and the selection effect used to read, which cost four
    // requests per click (each load takes the detail and the audit).
    expect(loops.match(/void loadDetail\(/g)?.length).toBe(2); // the effect, and the poll
    const select = loops.slice(loops.indexOf('function selectLoop'));
    expect(select.slice(0, select.indexOf('}'))).not.toContain('loadDetail');
  });

  it('refreshes the list, not just the selected loop', () => {
    expect(loops).toContain('fetchLoops()');
    expect(loops).toContain('let polledLoops = $state<Loop[] | null>(null);');
    expect(loops).toContain('const loops = $derived<Loop[]>(polledLoops ?? data.loops ?? []);');
  });

  it('drops a reply the newest read has already replaced', () => {
    expect(loops).toContain('if (gen !== detailGen) return;');
    // The poll declines a tick while a read is out; a click never is.
    expect(loops).toContain('if (document.hidden || outstanding > 0) return;');
  });

  it('guards the list poll the way it guards the detail poll', () => {
    // The list read got neither guard, so against a server slower than the 7s
    // heartbeat the reads stacked and an older list could land last.
    expect(loops).toContain('if (document.hidden || listOut) return;');
    expect(loops).toContain('const gen = ++listGen;');
    expect(loops).toContain('if (gen === listGen) polledLoops = next;');
    // And the flag is released even when the read failed.
    expect(loops).toContain('listOut = false;');
  });
});

describe('the run page’s event tail (C3/#2300)', () => {
  it('stops when the RUN stops, not when the prop does', () => {
    // `live` derived from the `run` prop alone, and nothing on this page
    // invalidates loader data — no interval on the page or the layout, and the
    // shared pulse never calls `invalidate`. So a run that completed while the
    // page was open went on polling `/api/runs/{id}/audit` once a second for
    // as long as the tab stayed open, which is exactly the poll nobody counted.
    expect(runEvents).toContain('const live = $derived(stillLive(LIVE_STATUSES, run?.status, readStatus));');
    expect(runEvents).toContain('const row = await fetchRun(runId);');
    // Both timers are armed by the one `live` effect and dropped together.
    expect(runEvents).toContain('const statusTimer = setInterval(readRunStatus, STATUS_POLL_MS);');
    expect(runEvents).toContain('clearInterval(statusTimer);');
    // And both reads carry the pulse's other two guards: a beat landing on a
    // read still out is skipped, so two replies cannot land out of order.
    expect(runEvents).toContain('if (reading || (typeof document !== \'undefined\' && document.hidden)) return;');
    expect(runEvents).toContain(
      'if (readingStatus || (typeof document !== \'undefined\' && document.hidden)) return;'
    );
  });

  it('offers no control that can never be pressed', () => {
    // `clearRunFilter` keeps the clicked selection, so on a phase-only filter
    // it returns an identical filter: gated on `canClearRunFilter`, not on the
    // filter merely being non-empty.
    const code = markup(runEvents);
    expect(code).toContain('{#if canClearRunFilter(filter)}');
    expect(code).not.toContain('{#if !isRunFilterEmpty(filter)}');
    // Every `disabled` is a condition, never a bare attribute.
    expect(code.match(/\sdisabled(?=[\s>])/g)).toBeNull();
  });

  it('never passes a capped page off as the whole run', () => {
    const code = markup(runEvents);
    expect(code).toContain('runEventsCount(shown.length, events.length, more)');
    expect(code).toContain('{#if more && !live}');
    expect(code).not.toContain('`${shown.length} of ${events.length}`');
  });
});

describe('the Config modal (C14)', () => {
  it('handles Escape exactly once', () => {
    // It was bound at the window AND on each dialog's backdrop, so one press
    // ran two dismissals.
    expect(markup(config).match(/Escape/g)?.length).toBe(1);
    expect(config).toContain("if (e.key !== 'Escape') return;");
  });

  it('contains focus in both dialogs, which is what aria-modal claims', () => {
    const code = markup(config);
    expect(code.match(/aria-modal="true"/g)?.length).toBe(2);
    expect(code.match(/use:contained/g)?.length).toBe(2);
    // `contained` focuses the box itself, so the bare focus attachment is gone.
    expect(config).not.toContain('node.focus()');
  });

  it('does not announce the backdrop as a button', () => {
    expect(markup(config)).not.toContain('role="button"');
  });
});
