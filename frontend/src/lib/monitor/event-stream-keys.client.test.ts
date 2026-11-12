/**
 * #2248 — the brush's keys, its reset and its drags, as the operator actually
 * performs them.
 *
 * The arithmetic of a pan, a zoom and a draw is `brush.ts`, held by
 * `overview-track.test.ts` without a browser. What cannot be checked there is
 * the step between: which query the component writes when the track reports a
 * gesture, and what that query costs. Those decisions are the whole of this
 * file, and the first three of them were wrong in the first cut.
 *
 * A reset cleared `from`/`to`, which under `Custom…` — whose range IS its
 * window — threw the operator's range away and dropped the view back to
 * Live 30m.
 *
 * A pan or a zoom out with the brush already spanning the whole range (which is
 * every live reading) returns the window unchanged, and pinning that window
 * stopped the 5s poll with nothing on the screen to show for the key.
 *
 * The component is mounted in jsdom for the same reason as
 * `event-stream-fetch.client.test.ts`: a keydown needs a DOM to be dispatched
 * into, and `$effect` — the poll these keys can silently stop — does not run in
 * an SSR render.
 */
import { flushSync, mount, unmount } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import type { AuditList } from '$lib/api';
// Types only, so this import is erased and cannot run before the mocks below.
import type { StreamQuery } from './stream-query';

const fetchAudit = vi.fn();
const fetchAuditHistogram = vi.fn();
const fetchEpics = vi.fn();

vi.mock('$app/environment', () => ({ browser: true }));
vi.mock('$lib/api', () => ({ fetchAudit, fetchAuditHistogram, fetchEpics }));

const { default: EventStream } = await import('./EventStream.svelte');
const { applyPreset, isLive, LIVE_QUERY, resolveStreamScope, withWindow } = await import(
  './stream-query'
);
const { STREAM_POLL_MS, STREAM_SETTLE_MS, streamWindow } = await import('./stream');

const NOW = Date.parse('2026-10-02T10:04:00.000Z');

const emptyPage: AuditList = { events: [], next_after: null };

let target: HTMLElement;
let component: Record<string, unknown> | null = null;
let onquery: Mock<(next: StreamQuery) => void>;
/** The query the component is mounted on, held so a write can come back as one. */
let queries: SvelteMap<string, StreamQuery>;

/** The component, mounted on one clock reading, with its queries recorded. */
function show(query: StreamQuery): void {
  target = document.createElement('div');
  document.body.appendChild(target);
  onquery = vi.fn<(next: StreamQuery) => void>();
  component = mount(EventStream, {
    target,
    props: { query, nowMs: NOW, onquery }
  }) as Record<string, unknown>;
  flushSync();
}

/**
 * The component mounted on a query it writes back to ITSELF — which is what the
 * Monitor page does with `replaceState` and `page.state`, and the loop every
 * gesture here actually runs in. A query held still is the one thing that
 * cannot show what a gesture costs, or what it does to a range that follows it.
 */
function follow(query: StreamQuery): void {
  target = document.createElement('div');
  document.body.appendChild(target);
  onquery = vi.fn<(next: StreamQuery) => void>((next) => queries.set('query', next));
  queries = new SvelteMap([['query', query]]);
  component = mount(EventStream, {
    target,
    props: {
      get query() {
        return queries.get('query')!;
      },
      nowMs: NOW,
      onquery
    }
  }) as Record<string, unknown>;
  flushSync();
}

/** The overview track: the brush's own seat for the keyboard. */
const track = () => target.querySelector('[role="slider"]') as HTMLElement;

/** The track's width in pixels, as the stub below reports it — jsdom lays out nothing. */
const WIDTH = 800;

/** The track, measured, with the brush box over it that takes a press first. */
function grips(): HTMLElement {
  const el = track();
  el.getBoundingClientRect = () => ({ left: 0, width: WIDTH, right: WIDTH }) as DOMRect;
  return el.querySelector('[data-grip]') as HTMLElement;
}

/** The pointer goes down — `press` below is the keyboard's. */
const hold = (el: Element, clientX: number) =>
  el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0, clientX }));

const moveTo = (el: Element, clientX: number) => {
  el.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, clientX }));
  flushSync();
};

const release = (el: Element) => {
  el.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
  flushSync();
};

function press(key: string): void {
  track().dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
  flushSync();
}

function doubleClick(): void {
  track().dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
  flushSync();
}

/** The one query the component wrote, or `undefined` — it writes at most one. */
const written = (): StreamQuery | undefined => onquery.mock.calls.at(-1)?.[0];

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  fetchAudit.mockReset().mockResolvedValue(emptyPage);
  fetchAuditHistogram.mockReset().mockResolvedValue(null);
  fetchEpics.mockReset().mockResolvedValue([]);
});

afterEach(() => {
  if (component) unmount(component);
  component = null;
  target?.remove();
  vi.useRealTimers();
});

describe('reset (a double-click on the track)', () => {
  it('keeps the range a Custom… window IS, rather than discarding it', async () => {
    const custom = applyPreset(LIVE_QUERY, 'custom', NOW);
    show(custom);
    await vi.advanceTimersByTimeAsync(0);
    doubleClick();

    const next = written();
    expect(next).toBeDefined();
    // The range survives: clearing the pins left `range=custom` with no window,
    // which is no range at all — the reading falls back to Live 30m.
    expect(next!.preset).toBe('custom');
    expect(next!.fromMs).toBe(custom.fromMs);
    expect(next!.toMs).toBe(custom.toMs);
    expect(resolveStreamScope(next!, NOW).range).toEqual(resolveStreamScope(custom, NOW).range);
    expect(isLive(next!)).toBe(false);
  });

  it('puts the brush back around the whole range of a preset that has one', async () => {
    const day = applyPreset(LIVE_QUERY, '24h', NOW);
    show(withWindow(day, { startMs: NOW - 3_600_000, endMs: NOW - 1_800_000 }));
    await vi.advanceTimersByTimeAsync(0);
    doubleClick();

    const next = written();
    expect(next).toMatchObject({ preset: '24h', fromMs: null, toMs: null });
    const scope = resolveStreamScope(next!, NOW);
    expect(scope.window).toEqual(scope.range);
  });
});

describe('a key that cannot move the window', () => {
  /** Live is the reading where the window already spans the whole range. */
  it('writes nothing, and so leaves the poll running', async () => {
    show(LIVE_QUERY);
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchAudit).toHaveBeenCalledTimes(1);

    for (const key of ['ArrowLeft', 'ArrowRight', '-']) {
      press(key);
      expect(onquery, key).not.toHaveBeenCalled();
    }
    // Still live: the pin that would have paused it was never written.
    expect(target.textContent).toContain('Live');
    await vi.advanceTimersByTimeAsync(STREAM_POLL_MS);
    expect(fetchAudit).toHaveBeenCalledTimes(2);
  });

  it('still pins the window for a key that does move it', async () => {
    show(LIVE_QUERY);
    await vi.advanceTimersByTimeAsync(0);
    press('+');

    const next = written();
    const live = streamWindow(NOW);
    expect(next).toBeDefined();
    expect(isLive(next!)).toBe(false);
    expect(next!.toMs! - next!.fromMs!).toBeLessThan(live.endMs - live.startMs);
  });

  it('pans once there is a window narrower than the range to pan', async () => {
    const day = applyPreset(LIVE_QUERY, '24h', NOW);
    const window = { startMs: NOW - 3_600_000, endMs: NOW - 1_800_000 };
    show(withWindow(day, window));
    await vi.advanceTimersByTimeAsync(0);
    press('ArrowLeft');

    const next = written();
    expect(next).toBeDefined();
    expect(next!.fromMs).toBeLessThan(window.startMs);
    expect(next!.toMs! - next!.fromMs!).toBe(window.endMs - window.startMs);
  });
});

/**
 * `Custom…` is the one range with no span of its own: its range IS its window,
 * so it moves with every gesture. A move clamped to it was therefore clamped
 * to the window being moved — the first zoom-in was final, and the zoom-out,
 * the two pans and the reset that should have undone it each wrote nothing.
 * The operator had no way back to the range they pinned.
 */
describe('under Custom…', () => {
  const custom = () => applyPreset(LIVE_QUERY, 'custom', NOW);

  /** One key, pressed against `query`; the query it wrote, or `undefined`. */
  async function pressed(query: StreamQuery, key: string): Promise<StreamQuery | undefined> {
    show(query);
    await vi.advanceTimersByTimeAsync(0);
    press(key);
    const next = written();
    if (component) unmount(component);
    component = null;
    target.remove();
    return next;
  }

  it('comes back to the window a zoom-in left, on the key that reverses it', async () => {
    const pinned = custom();
    const inward = await pressed(pinned, '+');
    expect(inward).toBeDefined();
    expect(inward!.toMs! - inward!.fromMs!).toBeLessThan(pinned.toMs! - pinned.fromMs!);

    const outward = await pressed(inward!, '-');
    expect(outward).toBeDefined();
    expect(outward!.fromMs).toBe(pinned.fromMs);
    expect(outward!.toMs).toBe(pinned.toMs);
    // The range follows the window here, so the overview is back too.
    expect(resolveStreamScope(outward!, NOW).range).toEqual(
      resolveStreamScope(pinned, NOW).range
    );
  });

  it('pans, carrying its range with it', async () => {
    const pinned = custom();
    const span = pinned.toMs! - pinned.fromMs!;
    const older = await pressed(pinned, 'ArrowLeft');
    expect(older).toBeDefined();
    expect(older!.fromMs).toBe(pinned.fromMs! - span / 10);
    expect(older!.toMs! - older!.fromMs!).toBe(span);

    // And back: a pan is reversible by the arrow that reverses it.
    const back = await pressed(older!, 'ArrowRight');
    expect(back).toMatchObject({ fromMs: pinned.fromMs, toMs: pinned.toMs });
  });

  it('stays paused, and still offers the one way out', async () => {
    const inward = await pressed(custom(), '+');
    expect(isLive(inward!)).toBe(false);
    show(inward!);
    await vi.advanceTimersByTimeAsync(0);
    press('L');
    expect(isLive(written()!)).toBe(true);
  });
});

/**
 * A drag, with the query it writes coming back as the query it is measured
 * against — the loop the page actually closes through `replaceState`.
 *
 * `Custom…` is where that loop bit: its range IS its window, so a window
 * reported mid-drag came back as the range the NEXT frame was mapped into.
 * Nine minutes drawn over a 30-minute range collapsed through 2.7 minutes, 49
 * seconds and 15 seconds to the bucket floor in four frames, and the operator's
 * selection was gone before their hand came up. The same loop `moveBounds`
 * closed for the keys.
 */
describe('a drag under Custom…', () => {
  /** 30 minutes of range, the brush around the whole of it, nothing pinned since. */
  const thirtyMinutes = () => applyPreset(LIVE_QUERY, 'custom', NOW);

  it('selects the span it was dragged over, not a collapsing fraction of it', async () => {
    const custom = thirtyMinutes();
    follow(custom);
    await vi.advanceTimersByTimeAsync(0);
    const body = grips();

    // 100px to 340px of an 800px track: nine of the range's thirty minutes.
    hold(body, 100);
    for (const at of [140, 220, 300, 340]) moveTo(body, at);
    release(body);

    const next = written();
    expect(next).toBeDefined();
    expect(next!.toMs! - next!.fromMs!).toBe(9 * 60_000);
    expect(next!.fromMs).toBe(custom.fromMs! + (30 * 60_000 * 100) / WIDTH);
    expect(next!.toMs).toBe(custom.fromMs! + (30 * 60_000 * 340) / WIDTH);
  });

  it('costs the range one query and one read, not one per frame', async () => {
    follow(thirtyMinutes());
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchAudit).toHaveBeenCalledTimes(1);
    const body = grips();

    hold(body, 100);
    for (const at of [140, 180, 220, 260, 300, 340]) moveTo(body, at);
    // Six frames of drag, and nothing has been written or asked for: the box on
    // the track is the feedback, and the URL is not a frame buffer.
    expect(onquery).not.toHaveBeenCalled();
    expect(fetchAudit).toHaveBeenCalledTimes(1);

    release(body);
    expect(onquery).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(STREAM_SETTLE_MS);
    expect(fetchAudit).toHaveBeenCalledTimes(2);
    expect(fetchAuditHistogram).toHaveBeenCalledTimes(2);
  });
});

/**
 * A held arrow key is the other burst, and it must still move the window on
 * every repeat: the reading settles, the navigation does not wait for it.
 */
describe('a burst of keys', () => {
  it('moves on every repeat and reads once, when they stop', async () => {
    const day = applyPreset(LIVE_QUERY, '24h', NOW);
    const window = { startMs: NOW - 3_600_000, endMs: NOW - 1_800_000 };
    follow(withWindow(day, window));
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchAudit).toHaveBeenCalledTimes(1);

    for (let i = 0; i < 10; i += 1) press('ArrowLeft');

    // Every repeat panned the window a tenth of it, and the URL carries all ten.
    const next = written();
    expect(onquery).toHaveBeenCalledTimes(10);
    expect(next!.fromMs).toBe(window.startMs - 10 * 3 * 60_000);
    expect(next!.toMs! - next!.fromMs!).toBe(window.endMs - window.startMs);
    // And the store was asked nothing while they were coming.
    expect(fetchAudit).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(STREAM_SETTLE_MS);
    expect(fetchAudit).toHaveBeenCalledTimes(2);
    expect(fetchAudit.mock.calls[1][0].since).toBe(new Date(next!.fromMs!).toISOString());
  });
});
