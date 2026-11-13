/**
 * #2248 — what the event stream actually asks the API for.
 *
 * Everything else about this surface is a pure reading a server-side render can
 * check. These cannot be: they are about a timer, a cursor and the order two
 * reads come back in. So the component is mounted in jsdom — allowed dev-only
 * for exactly this (the operator's decision 4 of 2026-10-02) — with `$lib/api`
 * mocked, and the assertions are the calls it made. The `.client.test.ts` suffix is what puts
 * this file in the jsdom project (`vite.config.ts`); without the `browser`
 * resolve condition it carries, `svelte` resolves to its server build and
 * `mount()` does not exist.
 *
 * The one that matters most is the timer. The stream used to poll whatever it
 * was showing; a range in the past cannot change, so polling it is a request
 * every five seconds for an answer that is already on the screen. "Paused" has
 * to be true and not just printed.
 */
import { flushSync, mount, unmount } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuditEvent, AuditList } from '$lib/api';
// Types only, so this import is erased and cannot run before the mocks below.
import type { StreamQuery } from './stream-query';

const fetchAudit = vi.fn();
const fetchAuditHistogram = vi.fn();
const fetchEpics = vi.fn();

vi.mock('$app/environment', () => ({ browser: true }));
vi.mock('$lib/api', () => ({ fetchAudit, fetchAuditHistogram, fetchEpics }));

const { default: EventStream } = await import('./EventStream.svelte');
const { applyPreset, LIVE_QUERY, withWindow } = await import('./stream-query');
const { STREAM_LIMIT, STREAM_POLL_MS, STREAM_SETTLE_MS } = await import('./stream');

const NOW = Date.parse('2026-10-02T10:04:00.000Z');

const emptyPage: AuditList = { events: [], next_after: null };

const histogram = {
  since: '2026-10-02T09:34:00.000Z',
  until: '2026-10-02T10:04:00.000Z',
  bucket_s: 30,
  buckets: [],
  total: 0
};

let target: HTMLElement;
let component: Record<string, unknown> | null = null;
/** The query the component is mounted on, where a test moves the window. */
let queries: SvelteMap<string, StreamQuery>;

function show(props: Record<string, unknown>): void {
  target = document.createElement('div');
  document.body.appendChild(target);
  component = mount(EventStream, { target, props }) as Record<string, unknown>;
  flushSync();
}

/** Mounted on a window a test can move — the operator leaving one for another. */
function showMoving(query: StreamQuery): void {
  queries = new SvelteMap([['query', query]]);
  show({
    get query() {
      return queries.get('query')!;
    }
  });
}

/** One audit event, `minutes` before NOW. */
function at(minutes: number): AuditEvent {
  return {
    id: 1000 - minutes,
    timestamp: new Date(NOW - minutes * 60_000).toISOString(),
    event: 'dispatch_completed',
    issue_num: 2219,
    item_id: null,
    run_id: null,
    actor_type: 'host',
    actor_id: 'kew',
    summary: null,
    epic_ref: 'ui-refine'
  };
}

/** A promise a test resolves itself, which is how two reads are put in order. */
function defer<T>(): {
  promise: Promise<T>;
  settle: (value: T) => void;
  fail: (err: unknown) => void;
} {
  let settle!: (value: T) => void;
  let fail!: (err: unknown) => void;
  const promise = new Promise<T>((resolve, reject) => {
    settle = resolve;
    fail = reject;
  });
  return { promise, settle, fail };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  fetchAudit.mockReset().mockResolvedValue(emptyPage);
  fetchAuditHistogram.mockReset().mockResolvedValue(histogram);
  fetchEpics.mockReset().mockResolvedValue([]);
});

afterEach(() => {
  if (component) unmount(component);
  component = null;
  target?.remove();
  vi.useRealTimers();
});

describe('the fetch for a window', () => {
  it('passes since, until and the filters', async () => {
    show({
      query: {
        ...applyPreset(LIVE_QUERY, '24h', NOW),
        epic: 'ui-refine',
        item: '2219',
        kinds: ['money']
      }
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchAudit).toHaveBeenCalledTimes(1);
    expect(fetchAudit.mock.calls[0][0]).toMatchObject({
      since: '2026-10-01T10:04:00.000Z',
      until: '2026-10-02T10:04:00.001Z',
      epic: 'ui-refine',
      item: '2219',
      limit: STREAM_LIMIT
    });
    expect(fetchAudit.mock.calls[0][0].event_prefix).toContain('budget_');
    expect(fetchAudit.mock.calls[0][0].after).toBeUndefined();
  });

  it('asks the histogram for the same filters over the whole range', async () => {
    show({ query: { ...applyPreset(LIVE_QUERY, '24h', NOW), epic: 'ui-refine' } });
    await vi.advanceTimersByTimeAsync(0);
    const [window, filters] = fetchAuditHistogram.mock.calls[0];
    expect(window).toMatchObject({ since: '2026-10-01T10:04:00.000Z', bucket_s: 900 });
    // The overview cannot count what the list is hiding.
    expect(filters).toMatchObject({ epic: 'ui-refine' });
  });

  it('asks for the window it was given, brushed or whole', async () => {
    const day = applyPreset(LIVE_QUERY, '24h', NOW);
    show({ query: day });
    await vi.advanceTimersByTimeAsync(0);
    unmount(component!);
    component = null;
    show({
      query: withWindow(day, {
        startMs: Date.parse('2026-10-02T02:45:00.000Z'),
        endMs: Date.parse('2026-10-02T04:30:00.000Z')
      })
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchAudit.mock.calls.at(-1)![0]).toMatchObject({
      since: '2026-10-02T02:45:00.000Z',
      until: '2026-10-02T04:30:00.001Z'
    });
  });
});

describe('Load older', () => {
  const older = (button: HTMLButtonElement | undefined) => {
    expect(button, 'the Load older button').toBeDefined();
    button!.click();
    flushSync();
  };

  const button = () =>
    [...target.querySelectorAll('button')].find((el) =>
      el.textContent?.includes('Load older')
    ) as HTMLButtonElement | undefined;

  it('passes after=next_after and stops at the tail of the log', async () => {
    fetchAudit
      .mockResolvedValueOnce({ events: [at(10)], next_after: 42 })
      .mockResolvedValueOnce({ events: [at(400)], next_after: null });

    show({ query: applyPreset(LIVE_QUERY, '24h', NOW) });
    await vi.advanceTimersByTimeAsync(0);
    older(button());
    await vi.advanceTimersByTimeAsync(0);

    expect(fetchAudit).toHaveBeenCalledTimes(2);
    // The window's end and filters, and no `since`: the next page may lie past
    // the window's start, and the window widens to it (below).
    expect(fetchAudit.mock.calls[1][0]).toMatchObject({
      after: 42,
      until: '2026-10-02T10:04:00.001Z'
    });
    expect(fetchAudit.mock.calls[1][0].since).toBeUndefined();
    // The page it fetched is in the list, under the one that was already there.
    expect(target.textContent).toContain('09:54');
    // And the control is gone: `next_after` is null on a read with no floor, so
    // there is nothing older to ask for and a button that fetched nothing would
    // promise there was.
    expect(button()).toBeUndefined();
  });

  /**
   * Tom, 2026-10-02: "Load older" may widen the window past its start, moving
   * the brush's left edge to match — so it is useful even when one page held
   * the whole window.
   */
  describe('past the window', () => {
    const day = () => applyPreset(LIVE_QUERY, '24h', NOW);
    const DAY_MS = 86_400_000;

    /** Mounted on a query it writes back to itself, as the Monitor page does. */
    function follow(query: StreamQuery): ReturnType<typeof vi.fn> {
      queries = new SvelteMap([['query', query]]);
      const onquery = vi.fn((next: StreamQuery) => queries.set('query', next));
      show({
        get query() {
          return queries.get('query')!;
        },
        onquery
      });
      return onquery;
    }

    it('is offered when one page held the whole window, and pages from its oldest event', async () => {
      fetchAudit
        .mockResolvedValueOnce({ events: [at(10), at(70)], next_after: null })
        .mockResolvedValueOnce({ events: [at(2000)], next_after: 7 });
      follow(day());
      await vi.advanceTimersByTimeAsync(0);
      older(button());
      await vi.advanceTimersByTimeAsync(0);
      // 70 minutes ago is the oldest event in hand: id 1000 - 70.
      expect(fetchAudit.mock.calls[1][0]).toMatchObject({ after: 930 });
    });

    it('moves the brush’s left edge to the oldest event it reached', async () => {
      fetchAudit
        .mockResolvedValueOnce({ events: [at(10)], next_after: null })
        .mockResolvedValueOnce({ events: [at(2000)], next_after: 7 });
      const onquery = follow(day());
      await vi.advanceTimersByTimeAsync(0);
      older(button());
      await vi.advanceTimersByTimeAsync(0);

      expect(onquery).toHaveBeenCalledTimes(1);
      const next = onquery.mock.calls[0][0] as StreamQuery;
      expect(next.preset).toBe('24h');
      expect(next.fromMs).toBe(NOW - 2000 * 60_000);
      expect(next.toMs).toBe(NOW);
      expect(NOW - next.fromMs!).toBeGreaterThan(DAY_MS);
    });

    it('keeps the pages it loaded, and re-reads only the bars for the wider range', async () => {
      fetchAudit
        .mockResolvedValueOnce({ events: [at(10)], next_after: null })
        .mockResolvedValueOnce({ events: [at(2000)], next_after: 7 });
      follow(day());
      await vi.advanceTimersByTimeAsync(0);
      expect(fetchAuditHistogram).toHaveBeenCalledTimes(1);
      older(button());
      await vi.advanceTimersByTimeAsync(STREAM_SETTLE_MS);

      // The list was not read again: a head-page read would drop the page just
      // loaded and show the operator the list they had before they clicked.
      expect(fetchAudit).toHaveBeenCalledTimes(2);
      expect(target.textContent).toContain('09:54');
      expect(target.textContent).toContain('00:44:00');
      // The overview now spans the window it holds.
      expect(fetchAuditHistogram).toHaveBeenCalledTimes(2);
      expect(fetchAuditHistogram.mock.calls[1][0].since).toBe(
        new Date(NOW - 2000 * 60_000).toISOString()
      );
    });

    it('reads the newest page before an empty window', async () => {
      fetchAudit
        .mockResolvedValueOnce(emptyPage)
        .mockResolvedValueOnce({ events: [at(2000)], next_after: null });
      follow(day());
      await vi.advanceTimersByTimeAsync(0);
      expect(target.textContent).toContain('No events in this window');
      // The sentence carries the control, and the tail's copy of it stands
      // down: one "Load older" on the screen, where the reader is looking.
      expect(
        [...target.querySelectorAll('button')].filter((el) =>
          el.textContent?.includes('Load older')
        )
      ).toHaveLength(1);
      older(button());
      await vi.advanceTimersByTimeAsync(0);
      expect(fetchAudit.mock.calls[1][0]).toMatchObject({ until: '2026-10-01T10:04:00.000Z' });
      expect(fetchAudit.mock.calls[1][0].after).toBeUndefined();
      expect(fetchAudit.mock.calls[1][0].since).toBeUndefined();
    });
  });

  /**
   * The label names the instant the next page is older THAN, which is the
   * oldest event in hand. It printed the window's own start, which goes out as
   * the `since` of this very request — and `since` is inclusive, so every event
   * the button can return is at or after the instant the label said it loads
   * events before.
   */
  it('names the oldest event in hand, not the window floor it asks from', async () => {
    fetchAudit.mockResolvedValue({ events: [at(10), at(70)], next_after: 42 });
    show({ query: applyPreset(LIVE_QUERY, '24h', NOW) });
    await vi.advanceTimersByTimeAsync(0);

    const label = button()!.textContent ?? '';
    // 08:54 is the oldest of the two events the page holds.
    expect(label).toContain('before 08:54');
    // 10:04 is the window's start, which is where the request begins.
    expect(label).not.toContain('10:04');
    expect(fetchAudit.mock.calls[0][0].since).toBe('2026-10-01T10:04:00.000Z');
  });

  it('is not offered while the feed is live', async () => {
    fetchAudit.mockResolvedValue({ events: [], next_after: 42 });
    show({ query: LIVE_QUERY });
    await vi.advanceTimersByTimeAsync(0);
    expect(button()).toBeUndefined();
  });
});

/**
 * A paused range is where the brush works, and it has to hold still under it.
 * Every read re-took the clock, so each gesture slid the 24 hours the track
 * spans by however long the operator had been looking, the box drifted against
 * the bars, and every move asked for the same bars again.
 */
describe('a paused range', () => {
  const DAY_MS = 86_400_000;
  const track = () => target.querySelector('[role="slider"]') as HTMLElement;

  it('holds its clock and its bars while the window moves inside it', async () => {
    showMoving(applyPreset(LIVE_QUERY, '24h', NOW));
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchAuditHistogram).toHaveBeenCalledTimes(1);

    // Ten minutes of reading the overview, then a brush gesture.
    vi.setSystemTime(NOW + 600_000);
    const window = { startMs: NOW - 3_600_000, endMs: NOW - 1_800_000 };
    queries.set('query', withWindow(queries.get('query')!, window));
    flushSync();
    await vi.advanceTimersByTimeAsync(STREAM_SETTLE_MS);

    expect(fetchAudit).toHaveBeenCalledTimes(2);
    expect(fetchAudit.mock.calls[1][0].since).toBe(new Date(window.startMs).toISOString());
    // The range is the 24 hours the preset was chosen at, and its bars stand.
    expect(fetchAuditHistogram).toHaveBeenCalledTimes(1);
    expect(Number(track().getAttribute('aria-valuemin'))).toBe(NOW - DAY_MS);
    expect(Number(track().getAttribute('aria-valuemax'))).toBe(NOW);
  });

  it('reads the clock again when a preset is chosen', async () => {
    showMoving(applyPreset(LIVE_QUERY, '24h', NOW));
    await vi.advanceTimersByTimeAsync(0);
    vi.setSystemTime(NOW + 600_000);
    queries.set('query', applyPreset(queries.get('query')!, '6h', NOW + 600_000));
    flushSync();
    await vi.advanceTimersByTimeAsync(STREAM_SETTLE_MS);

    expect(fetchAuditHistogram).toHaveBeenCalledTimes(2);
    expect(fetchAuditHistogram.mock.calls[1][0].since).toBe(
      new Date(NOW + 600_000 + STREAM_SETTLE_MS - 6 * 3_600_000).toISOString()
    );
  });

  it('asks for the bars again when a filter changes what they count', async () => {
    showMoving(applyPreset(LIVE_QUERY, '24h', NOW));
    await vi.advanceTimersByTimeAsync(0);
    queries.set('query', { ...queries.get('query')!, epic: 'ui-refine' });
    flushSync();
    await vi.advanceTimersByTimeAsync(STREAM_SETTLE_MS);
    expect(fetchAuditHistogram).toHaveBeenCalledTimes(2);
    expect(fetchAuditHistogram.mock.calls[1][1]).toMatchObject({ epic: 'ui-refine' });
  });

  it('keeps the snapped live range a window was brushed out of', async () => {
    // 10:04 live is the strip 09:35–10:05; the brush was drawn over that.
    show({ query: withWindow(LIVE_QUERY, { startMs: NOW - 600_000, endMs: NOW - 300_000 }) });
    await vi.advanceTimersByTimeAsync(0);
    expect(track().getAttribute('aria-valuemin')).toBe(
      String(Date.parse('2026-10-02T09:35:00.000Z'))
    );
    expect(track().getAttribute('aria-valuemax')).toBe(
      String(Date.parse('2026-10-02T10:05:00.000Z'))
    );
  });
});

describe('the poll', () => {
  it('runs while live', async () => {
    show({ query: LIVE_QUERY });
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchAudit).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(STREAM_POLL_MS);
    expect(fetchAudit).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(STREAM_POLL_MS * 3);
    expect(fetchAudit).toHaveBeenCalledTimes(5);
  });

  it('does not run for any non-live range', async () => {
    for (const key of ['1h', '6h', '24h', '7d'] as const) {
      fetchAudit.mockClear();
      show({ query: applyPreset(LIVE_QUERY, key, NOW) });
      await vi.advanceTimersByTimeAsync(0);
      expect(fetchAudit, key).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(STREAM_POLL_MS * 10);
      expect(fetchAudit, key).toHaveBeenCalledTimes(1);
      unmount(component!);
      component = null;
    }
  });

  it('does not run once a window is brushed inside the live range', async () => {
    show({
      query: withWindow(LIVE_QUERY, { startMs: NOW - 600_000, endMs: NOW - 300_000 })
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchAudit).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(STREAM_POLL_MS * 4);
    expect(fetchAudit).toHaveBeenCalledTimes(1);
    // And it says so, rather than looking live and standing still.
    expect(target.textContent).toContain('Paused');
    expect(target.textContent).toContain('Jump to now');
  });
});

/**
 * A read for the window the operator has LEFT.
 *
 * `poll` starts again on every query change and awaits two requests; the awaits
 * give no order back, so the answer for the previous window can land after the
 * answer for the current one. It used to land on top of it — the page, the
 * cursor and the bars all assigned unconditionally — and under a non-live range
 * there is no timer to correct it, because "paused" means paused. A stale
 * failure was worse than a stale success: it printed "stream unavailable" over
 * a reading that was good.
 */
describe('a read for a window the operator has left', () => {
  const day = () => applyPreset(LIVE_QUERY, '24h', NOW);
  const brushed = { startMs: NOW - 3_600_000, endMs: NOW - 1_800_000 };

  /** The first read in flight, the window moved, the second read in flight. */
  async function twoWindows(): Promise<{ left: ReturnType<typeof defer<AuditList>> }> {
    const left = defer<AuditList>();
    const landed = defer<AuditList>();
    fetchAudit.mockReset().mockReturnValueOnce(left.promise).mockReturnValueOnce(landed.promise);
    showMoving(day());
    await vi.advanceTimersByTimeAsync(0);
    queries.set('query', withWindow(day(), brushed));
    flushSync();
    await vi.advanceTimersByTimeAsync(STREAM_SETTLE_MS);
    expect(fetchAudit).toHaveBeenCalledTimes(2);
    // The window they are on answers first, and is what the screen reads.
    landed.settle({ events: [at(44)], next_after: null });
    await vi.advanceTimersByTimeAsync(0);
    expect(target.textContent).toContain('09:20');
    return { left };
  }

  it('does not print "unavailable" over the reading they are looking at', async () => {
    const { left } = await twoWindows();

    left.fail(new Error('/api/audit -> 503'));
    await vi.advanceTimersByTimeAsync(0);

    expect(target.textContent).toContain('09:20');
    expect(target.textContent).not.toContain('unavailable');
  });

  it('does not put its own page, or its cursor, on the screen', async () => {
    const { left } = await twoWindows();

    left.settle({ events: [at(10)], next_after: 42 });
    await vi.advanceTimersByTimeAsync(0);

    expect(target.textContent).toContain('09:20');
    expect(target.textContent).not.toContain('09:54');
    // And the cursor it carried is not what the next page is asked from: the
    // window on the screen reached its own tail, so "Load older" continues from
    // the oldest event in hand (44 minutes ago: id 956), not from 42.
    fetchAudit.mockResolvedValue(emptyPage);
    const button = [...target.querySelectorAll('button')].find((el) =>
      el.textContent?.includes('Load older')
    ) as HTMLButtonElement;
    button.click();
    flushSync();
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchAudit.mock.calls.at(-1)![0]).toMatchObject({ after: 956 });
  });
});
