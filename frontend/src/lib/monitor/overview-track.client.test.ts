/**
 * #2248 — the brush's drag gestures, as a pointer performs them.
 *
 * The arithmetic of a draw, a pan and a resize is `brush.ts`, held by
 * `overview-track.test.ts` without a browser. What cannot be checked there is
 * which of the three a pointerdown starts — the brush box sits over the track
 * and takes the press first, so that choice is the component's and nothing
 * else's — and when the gesture reports.
 *
 * It was wrong in the first cut, and wrong in the state every range opens in.
 * The window starts out equal to its range (`stream-query.ts`), so the box
 * covers the track from edge to edge: its pointerdown stopped propagation and
 * started a pan of a window with nowhere to pan to, which the brush maths
 * rightly answer with the window unchanged. The drag did nothing, and
 * drag-to-draw — the documented gesture, and the only way to get a narrower
 * window with a mouse — was unreachable underneath it.
 *
 * It then reported every frame, which is the second thing held here. A gesture
 * is ONE report, measured in the geometry the press landed in: the range moves
 * under a drag (it follows the window under `Custom…` and the clock while
 * live), and a drag re-measured mid-gesture collapses.
 *
 * jsdom, because a pointerdown needs a DOM to be dispatched into. It lays
 * nothing out, so the one measurement the component takes off the DOM is
 * stubbed; everything else here is the real component.
 */
import { flushSync, mount, unmount } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { registeredShortcuts } from '$lib/keys/registry';
import OverviewTrack from './OverviewTrack.svelte';
import { timeAtPx, type Span } from './brush';
import { HOUR_MS, MINUTE_MS } from './stream-query';

const START = Date.parse('2026-10-02T09:00:00.000Z');
const RANGE: Span = { startMs: START, endMs: START + HOUR_MS };
/** The track's width in pixels, as the stub below reports it. */
const WIDTH = 800;
const BUCKET = 15 * MINUTE_MS;

let target: HTMLElement;
let component: Record<string, unknown> | null = null;
let onwindow: Mock<(next: Span) => void>;
let onkey: Mock<(key: string) => void>;
/**
 * The two spans the track is mounted on, held reactively so a test can move
 * them under a gesture in progress — which is what the page itself does.
 */
let spans: SvelteMap<string, Span>;

/** The track, mounted around one window, measured as `WIDTH` pixels wide. */
function show(window: Span): HTMLElement {
  target = document.createElement('div');
  document.body.appendChild(target);
  onwindow = vi.fn<(next: Span) => void>();
  onkey = vi.fn<(key: string) => void>();
  spans = new SvelteMap([
    ['range', RANGE],
    ['window', window]
  ]);
  component = mount(OverviewTrack, {
    target,
    props: {
      reading: null,
      get range() {
        return spans.get('range')!;
      },
      get window() {
        return spans.get('window')!;
      },
      bucketMs: BUCKET,
      onwindow,
      onkey
    }
  }) as Record<string, unknown>;
  flushSync();
  const track = target.querySelector('[role="slider"]') as HTMLElement;
  track.getBoundingClientRect = () => ({ left: 0, width: WIDTH, right: WIDTH }) as DOMRect;
  return track;
}

const press = (el: Element, clientX: number) =>
  el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0, clientX }));

const moveTo = (el: Element, clientX: number) => {
  el.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, clientX }));
  flushSync();
};

/** The hand comes up, which is when a gesture reports. */
const release = (el: Element) => {
  el.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
  flushSync();
};

/** The last window the gesture reported, or `undefined` if it reported none. */
const reported = (): Span | undefined => onwindow.mock.calls.at(-1)?.[0];

/** Where the brush box sits, in percentages of the track, as the DOM has it. */
const box = (track: HTMLElement) => {
  const el = track.querySelector('[data-grip]') as HTMLElement;
  return { left: el.style.left, width: el.style.width };
};

afterEach(() => {
  if (component) unmount(component);
  component = null;
  target?.remove();
});

describe('a drag on the brush with the whole range inside it', () => {
  it('draws the window it was dragged over, rather than doing nothing', () => {
    const track = show(RANGE);
    // The press lands on the box, because the box is the whole track.
    const body = track.querySelector('[data-grip]') as HTMLElement;

    press(body, 100);
    moveTo(body, 300);
    release(body);

    const drawn = reported();
    expect(drawn).toBeDefined();
    expect(drawn!.startMs).toBe(timeAtPx(RANGE, WIDTH, 100));
    expect(drawn!.endMs).toBe(timeAtPx(RANGE, WIDTH, 300));
    // And the box says which gesture it is holding, which is what the cursor
    // over it draws from.
    expect(body.dataset.grip).toBe('draw');
  });
});

describe('a drag on the brush with range outside it', () => {
  const window: Span = { startMs: START + 15 * MINUTE_MS, endMs: START + 30 * MINUTE_MS };

  it('draws rather than pans when the range outside it is a pixel of track', () => {
    // A window a millisecond inside an hour-wide range: there IS somewhere to
    // pan, arithmetically, and it is shorter than the box's own border.
    const track = show({ startMs: RANGE.startMs, endMs: RANGE.endMs - 1 });
    const body = track.querySelector('[data-grip]') as HTMLElement;
    expect(body.dataset.grip).toBe('draw');
  });

  it('pans the box, keeping its span', () => {
    const track = show(window);
    const body = track.querySelector('[data-grip="window"]') as HTMLElement;
    expect(body).not.toBeNull();

    press(body, 300);
    moveTo(body, 380);
    release(body);

    // 80px of an hour-wide, 800px track: six minutes older to newer.
    const panned = reported();
    expect(panned).toEqual({
      startMs: window.startMs + 6 * MINUTE_MS,
      endMs: window.endMs + 6 * MINUTE_MS
    });
  });

  it('resizes from a handle, leaving the other edge where it was', () => {
    const track = show(window);
    const grip = track.querySelector('[data-grip="start"]') as HTMLElement;

    press(grip, 200);
    moveTo(grip, 100);
    release(grip);

    const resized = reported();
    expect(resized).toBeDefined();
    expect(resized!.endMs).toBe(window.endMs);
    expect(resized!.startMs).toBe(timeAtPx(RANGE, WIDTH, 100));
  });
});

/**
 * One gesture, one report — and so one history entry and one pair of audit
 * reads for the whole drag, where reporting per frame cost a second of drag
 * sixty `replaceState` calls and a hundred and twenty requests against the
 * store the fleet is appending audit events to.
 */
describe('what a drag reports', () => {
  // The fixture's day is today, so the window it reports reads as clock times.
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(START);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('reports once, when the hand comes up, not once per frame', () => {
    const track = show(RANGE);
    const body = track.querySelector('[data-grip]') as HTMLElement;

    press(body, 100);
    for (const at of [140, 190, 250, 300, 340]) moveTo(body, at);
    expect(onwindow, 'nothing is reported while the pointer is down').not.toHaveBeenCalled();
    release(body);

    expect(onwindow).toHaveBeenCalledTimes(1);
    expect(reported()).toEqual({
      startMs: timeAtPx(RANGE, WIDTH, 100),
      endMs: timeAtPx(RANGE, WIDTH, 340)
    });
  });

  it('draws the box it is holding while it holds it', () => {
    const track = show(RANGE);
    const body = track.querySelector('[data-grip]') as HTMLElement;

    press(body, 200);
    moveTo(body, 600);
    // The drag has nothing to show for itself in the URL yet, so the box is the
    // whole of the feedback: half the track, starting a quarter along it.
    expect(box(track)).toEqual({ left: '25%', width: '50%' });
    expect(track.getAttribute('aria-valuetext')).toBe('09:15 to 09:45');
    release(body);
  });

  it('reports nothing for a press that never moved', () => {
    const track = show(RANGE);
    const body = track.querySelector('[data-grip]') as HTMLElement;

    press(body, 300);
    release(body);

    expect(onwindow).not.toHaveBeenCalled();
  });

  it('drops a gesture the browser cancelled rather than pinning it', () => {
    const track = show(RANGE);
    const body = track.querySelector('[data-grip]') as HTMLElement;

    press(body, 100);
    moveTo(body, 300);
    body.dispatchEvent(new PointerEvent('pointercancel', { bubbles: true }));
    flushSync();

    expect(onwindow).not.toHaveBeenCalled();
    // And the box is back around the committed window, which is the whole range.
    expect(box(track)).toEqual({ left: '0%', width: '100%' });
  });
});

/**
 * The range can move under a gesture, and a drag measured against the range it
 * is changing is a drag that collapses.
 *
 * `Custom…` is where it bites: its range IS its window (`stream-query.ts`), so
 * a window reported mid-drag comes straight back as a narrower range, and the
 * next frame re-maps the track into it — nine minutes drawn over a 30-minute
 * range reached the bucket floor in four frames. Live has the same shape with
 * the clock as the mover. Either way the gesture is measured in the geometry
 * the press landed in.
 */
describe('a range that moves under the gesture', () => {
  it('keeps measuring the drag in the range the press landed in', () => {
    const track = show(RANGE);
    const body = track.querySelector('[data-grip]') as HTMLElement;

    press(body, 100);
    moveTo(body, 300);
    // The page re-renders with a narrower range — what `Custom…` does with any
    // window it is handed, and what the clock does to a live range.
    spans.set('range', { startMs: START + 10 * MINUTE_MS, endMs: START + 20 * MINUTE_MS });
    flushSync();
    moveTo(body, 340);
    release(body);

    expect(reported()).toEqual({
      startMs: timeAtPx(RANGE, WIDTH, 100),
      endMs: timeAtPx(RANGE, WIDTH, 340)
    });
  });

  it('keeps measuring a resize in it too', () => {
    const window: Span = { startMs: START + 15 * MINUTE_MS, endMs: START + 30 * MINUTE_MS };
    const track = show(window);
    const grip = track.querySelector('[data-grip="end"]') as HTMLElement;

    press(grip, 400);
    moveTo(grip, 600);
    spans.set('range', { startMs: window.startMs, endMs: timeAtPx(RANGE, WIDTH, 600) });
    flushSync();
    moveTo(grip, 700);
    release(grip);

    const resized = reported();
    expect(resized!.startMs).toBe(window.startMs);
    expect(resized!.endMs).toBe(timeAtPx(RANGE, WIDTH, 700));
  });
});

describe('the brush keys, on the console key layer (#2781)', () => {
  /** A press on `el`, bubbling to the window the way a real one does. */
  const key = (el: EventTarget, key: string, init: KeyboardEventInit = {}): KeyboardEvent => {
    const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init });
    el.dispatchEvent(event);
    return event;
  };

  it('registers its keys while mounted, so the help overlay can list them', () => {
    show(RANGE);
    expect(registeredShortcuts('brush').map((s) => s.key)).toEqual([
      'ArrowLeft',
      'ArrowRight',
      '+',
      '=',
      '-',
      '_',
      'l',
      'L'
    ]);
    if (component) unmount(component);
    component = null;
    expect(registeredShortcuts('brush')).toEqual([]);
  });

  it('answers a bare press on the track', () => {
    const track = show(RANGE);
    expect(key(track, '=').defaultPrevented).toBe(true);
    expect(key(track, 'ArrowLeft').defaultPrevented).toBe(true);
    expect(onkey.mock.calls).toEqual([['zoom-in'], ['pan-older']]);
  });

  it('leaves ⌘=, ^−, ⌥← and ⌘← to the browser', () => {
    const track = show(RANGE);
    for (const [k, init] of [
      ['=', { metaKey: true }],
      ['-', { ctrlKey: true }],
      ['ArrowLeft', { altKey: true }],
      ['ArrowLeft', { metaKey: true }]
    ] as const) {
      expect(key(track, k, init).defaultPrevented, `${k} ${JSON.stringify(init)}`).toBe(false);
    }
    expect(onkey).not.toHaveBeenCalled();
  });

  it('leaves a press anywhere else on the page alone', () => {
    show(RANGE);
    expect(key(document.body, '=').defaultPrevented).toBe(false);
    expect(onkey).not.toHaveBeenCalled();
  });
});
