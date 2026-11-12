/**
 * A tour step is re-placed when its target's size changes under it (#3148):
 * Build history opened from inside its highlight grows the target, and
 * driver.js placed the popover once, for the shut line.
 *
 * The boundaries are faked, nothing in the controller is: driver.js is a fake
 * that counts `refresh()` (a re-place), `ResizeObserver` is a stub the test
 * reports sizes through, and `requestAnimationFrame` queues frames the test
 * runs one at a time. What is pinned: a steady target, or one that moves a
 * pixel or less, is never re-placed; a change of 2px or more is re-placed
 * exactly once per frame however many times it is reported; a refresh that
 * reports the size it placed at does not loop; ending the tour drops a frame
 * still pending; a resize while driver.js is still moving to the step waits
 * for the move to end; and a step that is leaving (its target unmounted,
 * reporting 0x0) or a target no longer in the page is never re-placed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { BundledTour } from './types';

const fake = vi.hoisted(() => ({
  refreshes: 0,
  driven: [] as number[],
  config: null as null | { onHighlighted?: () => void; onNextClick?: () => void },
  /** Set to hold `onHighlighted` back, as driver.js does while it animates the move. */
  holdHighlight: false,
  release: null as null | (() => void),
  /** Set to hold every `goto` until the test lets it land. */
  holdGoto: null as null | (() => void)
}));

vi.mock('driver.js', () => ({
  driver: (config: { onHighlighted?: () => void }) => {
    fake.config = config;
    return {
      // No animation: driver.js calls `onHighlighted` once the step is placed;
      // held, it calls it when the test releases the move.
      drive: (index: number) => {
        fake.driven.push(index);
        if (!fake.holdHighlight) fake.config?.onHighlighted?.();
        else fake.release = () => fake.config?.onHighlighted?.();
      },
      refresh: () => {
        fake.refreshes += 1;
      },
      destroy: () => {},
      isActive: () => true
    };
  }
}));

vi.mock('$app/state', () => ({
  page: {
    get url() {
      return new URL(window.location.href);
    },
    state: {}
  }
}));

vi.mock('$app/navigation', () => ({
  goto: (to: URL | string) =>
    new Promise<void>((resolve) => {
      const land = () => {
        window.history.pushState({}, '', String(to));
        resolve();
      };
      if (fake.holdGoto === null && !held.on) land();
      else fake.holdGoto = land;
    }),
  replaceState: (url: URL | string) => window.history.replaceState({}, '', String(url))
}));

const TOUR: BundledTour = {
  id: 'grow',
  title: 'A step whose target grows',
  start: '/work',
  steps: [
    {
      route: '/work',
      target: 'foot',
      docs: 'web/index.md',
      anchor: 'a',
      passage: 'grow/1',
      title: 'Step 1',
      body: [[{ text: 'Step 1.' }]],
      href: 'https://kew.dev/docs/web/#a'
    }
  ]
};
/** Two steps on two routes: a Next that leaves the first step's page. */
const CROSS: BundledTour = {
  id: 'cross',
  title: 'Two steps on two routes',
  start: '/work',
  steps: [
    { ...TOUR.steps[0], passage: 'cross/1' },
    { ...TOUR.steps[0], route: '/other', target: 'next', passage: 'cross/2', title: 'Step 2' }
  ]
};
vi.mock('./bundle', () => ({ TOURS: [TOUR, CROSS] }));

/** Whether the next `goto` is held (`fake.holdGoto` then lands it). */
const held = vi.hoisted(() => ({ on: false }));

/** Animation frames, run by the test one at a time. */
const frames = new Map<number, FrameRequestCallback>();
let nextFrame = 1;
function runFrame(): void {
  const due = [...frames.entries()];
  frames.clear();
  for (const [, callback] of due) callback(performance.now());
}

/** The one ResizeObserver the controller holds, and the element it watches. */
let observer: { callback: ResizeObserverCallback; target: Element | null } | null = null;
class StubObserver {
  constructor(callback: ResizeObserverCallback) {
    observer = { callback, target: null };
  }
  observe(target: Element) {
    if (observer) observer.target = target;
  }
  disconnect() {
    observer = null;
  }
  unobserve() {}
}

/**
 * The observer reports the watched element at `width` x `height` (its border
 * box). A step that stopped watching gets no report: there is no observer.
 */
function report(width: number, height: number): void {
  if (!observer?.target) return;
  const entry = {
    target: observer.target,
    borderBoxSize: [{ inlineSize: width, blockSize: height }],
    contentBoxSize: [{ inlineSize: width, blockSize: height }],
    devicePixelContentBoxSize: [],
    contentRect: { width, height }
  } as unknown as ResizeObserverEntry;
  observer.callback([entry], observer as unknown as ResizeObserver);
}

const { endTour, startTour } = await import('./controller');

/** Run frames and tasks until `count` steps have been driven, as the page does. */
async function untilDriven(count: number): Promise<void> {
  for (let i = 0; i < 200 && fake.driven.length < count; i++) {
    runFrame();
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  expect(fake.driven.length).toBe(count);
}

/** Start tour `id` and run until its first step is driven. */
async function open(id = TOUR.id): Promise<void> {
  startTour(id);
  await untilDriven(1);
  expect(fake.driven).toEqual([0]);
  expect(observer?.target).toBe(document.querySelector('[data-tour="foot"]'));
}

beforeEach(() => {
  fake.refreshes = 0;
  fake.driven = [];
  fake.holdHighlight = false;
  fake.release = null;
  fake.holdGoto = null;
  held.on = false;
  frames.clear();
  observer = null;
  window.history.replaceState({}, '', '/work');
  document.body.innerHTML = '<main><div data-tour="foot">Build history</div><i data-tour="next">b</i></main>';
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: true,
    media: query,
    addEventListener() {},
    removeEventListener() {}
  }));
  vi.stubGlobal('ResizeObserver', StubObserver);
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    const id = nextFrame++;
    frames.set(id, callback);
    return id;
  });
  vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id));
});

afterEach(() => {
  endTour();
  vi.unstubAllGlobals();
});

describe('a tour step whose target changes size', () => {
  it('re-places nothing for a steady target reporting the same size again', async () => {
    await open();
    report(1366, 27);
    report(1366, 27);
    report(1366, 27);
    runFrame();
    runFrame();
    expect(fake.refreshes).toBe(0);
  });

  it('re-places nothing for a change of a pixel or less', async () => {
    await open();
    report(1366, 27);
    report(1367, 28);
    runFrame();
    report(1365, 26);
    runFrame();
    expect(fake.refreshes).toBe(0);
  });

  it('re-places once per frame for a change of 2px or more, however often it is reported', async () => {
    await open();
    report(1366, 27);
    report(1366, 29);
    for (const height of [120, 200, 260, 288]) report(1366, height);
    expect(fake.refreshes).toBe(0);
    runFrame();
    expect(fake.refreshes).toBe(1);
    // The refresh reports the size it placed at: no change, no loop.
    report(1366, 288);
    runFrame();
    runFrame();
    expect(fake.refreshes).toBe(1);
    // The ledger shut again: a change from the placed size, once more.
    report(1366, 27);
    report(1366, 27);
    runFrame();
    expect(fake.refreshes).toBe(2);
  });

  it('drops a pending re-place when the tour ends', async () => {
    await open();
    report(1366, 27);
    report(1366, 288);
    expect(frames.size).toBe(1);
    endTour();
    expect(frames.size).toBe(0);
    expect(observer).toBeNull();
    runFrame();
    expect(fake.refreshes).toBe(0);
  });

  it('waits for driver.js to finish moving to the step, then re-places once', async () => {
    fake.holdHighlight = true;
    await open();
    report(1366, 27);
    report(1366, 288);
    runFrame();
    runFrame();
    // Still moving: a re-place now would be lost when the move ends.
    expect(fake.refreshes).toBe(0);
    fake.release?.();
    expect(fake.refreshes).toBe(1);
    runFrame();
    expect(fake.refreshes).toBe(1);
  });

  it('re-places nothing as a cross-route Next leaves the step and its target unmounts to 0x0', async () => {
    await open(CROSS.id);
    report(1366, 27);
    held.on = true;
    fake.config?.onNextClick?.();
    // On its way to the next route: the old page lays out as it goes, its
    // target still in the page, then the target unmounts.
    report(1366, 300);
    runFrame();
    expect(fake.refreshes).toBe(0);
    document.querySelector('[data-tour="foot"]')?.remove();
    report(0, 0);
    runFrame();
    runFrame();
    expect(fake.refreshes).toBe(0);
    held.on = false;
    fake.holdGoto?.();
    await untilDriven(2);
    expect(fake.refreshes).toBe(0);
    expect(observer?.target).toBe(document.querySelector('[data-tour="next"]'));
  });

  it('re-places nothing for a target no longer in the page', async () => {
    await open();
    report(1366, 27);
    document.querySelector('[data-tour="foot"]')?.remove();
    report(0, 0);
    runFrame();
    runFrame();
    expect(fake.refreshes).toBe(0);
  });
});
