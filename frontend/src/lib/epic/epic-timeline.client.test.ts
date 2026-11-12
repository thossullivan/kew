/**
 * The Gantt in a DOM (#2271) — the half `timeline.test.ts` cannot reach.
 *
 * The model is pure arithmetic and is tested as such. What this file is for is
 * the wiring between it and the canvas, which has exactly one way to fail
 * silently and has failed it before (the DAG canvas rendered at 1360x0): a
 * chart that measures itself draws NOTHING until it has been measured, so a
 * structure that only appears once there are rows can never get a width, and
 * the view stays empty for ever.
 *
 * jsdom has no layout and no `ResizeObserver`, so both are stood up here: the
 * observer answers once, synchronously, with the sizes below, which is what a
 * browser does on the first frame.
 */
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { EpicTimeline } from '$lib/api';

const PLOT_W = 900;
const BODY_H = 400;

/** `clientWidth` / `clientHeight`, as a browser would report them for this
 * layout: the ruler and every lane span the plot, the scrolling body is
 * shorter than its rows. */
beforeEach(() => {
  for (const [prop, value] of [
    ['clientWidth', PLOT_W],
    ['clientHeight', BODY_H]
  ] as const) {
    Object.defineProperty(HTMLElement.prototype, prop, {
      configurable: true,
      get() {
        return value;
      }
    });
  }
  Object.defineProperty(Element.prototype, 'getBoundingClientRect', {
    configurable: true,
    value: () => ({ left: 0, top: 0, width: PLOT_W, height: BODY_H, right: PLOT_W, bottom: BODY_H })
  });
  // One synchronous answer per `observe`, which is what the first frame of a
  // real browser delivers (Svelte's size bindings read the element itself, so
  // the entry only has to name its target).
  class ResizeObserverShim {
    #cb: (entries: { target: Element }[]) => void;
    constructor(cb: (entries: { target: Element }[]) => void) {
      this.#cb = cb;
    }
    observe(target: Element): void {
      this.#cb([{ target }]);
    }
    unobserve(): void {}
    disconnect(): void {}
  }
  vi.stubGlobal('ResizeObserver', ResizeObserverShim);
});

const { default: EpicTimelineView } = await import('./EpicTimeline.svelte');

const timeline: EpicTimeline = {
  epic_ref: 'launch-fixes',
  now: '2026-09-28T12:00:00+00:00',
  span: { start: '2026-09-28T08:00:00+00:00', end: '2026-09-28T12:00:00+00:00' },
  nodes: [
    {
      id: '2055',
      title: 'the first node',
      bands: [
        { status: 'in_progress', from: '2026-09-28T08:00:00+00:00', to: '2026-09-28T09:00:00+00:00' },
        { status: 'awaiting_publication', from: '2026-09-28T09:00:00+00:00', to: null }
      ],
      runs: [
        {
          run_id: 'r1',
          kind: 'dispatch',
          status: 'success',
          started_at: '2026-09-28T08:00:00+00:00',
          finished_at: '2026-09-28T08:58:00+00:00',
          cost_usd: 2.5,
          attempt_number: 1
        }
      ],
      rounds: [{ round: 1, verdict: 'READY', recorded_at: '2026-09-28T08:59:00+00:00' }],
      merged_at: '2026-09-28T09:00:00+00:00'
    },
    {
      id: '2056',
      title: 'the second node',
      bands: [{ status: 'in_review', from: '2026-09-28T10:00:00+00:00', to: null }],
      runs: [
        {
          run_id: 'r2',
          kind: 'adversarial_review',
          status: 'pending',
          started_at: '2026-09-28T10:00:00+00:00',
          finished_at: null,
          cost_usd: 0.4,
          attempt_number: 2
        }
      ],
      rounds: [],
      merged_at: null
    }
  ]
};

let target: HTMLElement;
let component: Record<string, unknown> | null = null;
let picked: (string | null)[] = [];

function open(selected: string | null = null): HTMLElement {
  target = document.createElement('div');
  document.body.appendChild(target);
  picked = [];
  component = mount(EpicTimelineView, {
    target,
    props: {
      timeline,
      selected,
      onselect: (id: string | null) => picked.push(id)
    }
  }) as Record<string, unknown>;
  flushSync();
  return target;
}

/** The same mount for a timeline other than the fixture above. */
function mountWith(tl: EpicTimeline): HTMLElement {
  target = document.createElement('div');
  document.body.appendChild(target);
  component = mount(EpicTimelineView, {
    target,
    props: { timeline: tl, selected: null, onselect: () => {} }
  }) as Record<string, unknown>;
  flushSync();
  return target;
}

const canvas = (): HTMLElement => target.querySelector('.tl') as HTMLElement;
const rows = (): HTMLElement[] => [...target.querySelectorAll('.row')] as HTMLElement[];
/** Every ruler label, which is the one reading of the time scale. */
const rulerLabels = (): string[] =>
  [...target.querySelectorAll('.ruler text')].map((t) => t.textContent ?? '');

function wheel(deltaY: number, clientX = PLOT_W / 2): void {
  canvas().dispatchEvent(new WheelEvent('wheel', { deltaY, clientX, bubbles: true, cancelable: true }));
  flushSync();
}

function key(k: string): void {
  canvas().dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }));
  flushSync();
}

afterEach(() => {
  if (component) unmount(component);
  component = null;
  target?.remove();
  vi.unstubAllGlobals();
});

describe('the chart draws itself', () => {
  it('is one row per node, with bars and bands at a non-zero size', () => {
    open();
    expect(rows()).toHaveLength(timeline.nodes.length);
    expect(rows()[0].textContent).toContain('#2055');
    expect(rows()[0].textContent).toContain('the first node');

    // The regression this file exists for: a measured chart that has not been
    // measured draws nothing.
    const widths = [...target.querySelectorAll('.lane rect')].map((r) =>
      Number(r.getAttribute('width'))
    );
    expect(widths.length).toBeGreaterThan(0);
    for (const w of widths) expect(w).toBeGreaterThan(0);
    expect(Math.max(...widths)).toBeGreaterThan(50);
  });

  it('draws the ruler, the now line and the merge', () => {
    open();
    expect(rulerLabels().length).toBeGreaterThan(1);
    expect(rulerLabels()).toContain('now');
    expect(target.querySelector('.now')).not.toBeNull();
    expect(target.textContent).toContain('merged');
  });

  it('says so when there is nothing to draw, rather than going blank', () => {
    mountWith({ ...timeline, nodes: [] });
    expect(target.textContent).toContain('no runs recorded for this epic yet');
  });

  it('draws every review round of a retried node, duplicate numbers and all', () => {
    // `record_review_round` restarts the round count at 1 for each review
    // cycle and the API serves every cycle, so a retried node's lane arrives
    // as rounds [1, 2, 1, 2]. A keyed `#each` on the round number cannot hold
    // four marks under two keys — it drops half of what the chart is for.
    const at = (m: number) => `2026-09-28T10:${String(m).padStart(2, '0')}:00+00:00`;
    mountWith({
      ...timeline,
      nodes: [
        {
          ...timeline.nodes[1],
          rounds: [
            { round: 1, verdict: 'NEEDS_CHANGES', recorded_at: at(10) },
            { round: 2, verdict: 'READY_WITH_RESIDUALS', recorded_at: at(20) },
            { round: 1, verdict: 'NEEDS_CHANGES', recorded_at: at(30) },
            { round: 2, verdict: 'READY', recorded_at: at(40) }
          ]
        }
      ]
    });
    // One dot per round; the node has not merged, so no merge dot joins them.
    const dots = [...target.querySelectorAll('.lane circle')];
    expect(dots).toHaveLength(4);
    expect(new Set(dots.map((d) => d.getAttribute('cx'))).size).toBe(4);
  });
});

describe('the canvas grammar', () => {
  it('zooms the time axis on the wheel, and F puts it back', () => {
    open();
    const fitted = rulerLabels();

    wheel(-240);
    const zoomed = rulerLabels();
    expect(zoomed).not.toEqual(fitted);

    key('F');
    expect(rulerLabels()).toEqual(fitted);
  });

  it('reads a bar out on hover: kind, status, span, duration and cost', () => {
    open();
    // By its colour: `BAR_COLOR.dispatch`, which is the one rect in this
    // fixture drawn in `--text-2`.
    const bar = [...target.querySelectorAll('.lane rect')].find(
      (r) => r.getAttribute('fill') === 'var(--text-2)'
    ) as SVGRectElement;
    bar.dispatchEvent(new Event('pointerenter'));
    flushSync();
    const tip = target.querySelector('.tip') as HTMLElement;
    expect(tip.textContent).toContain('dispatch · success');
    expect(tip.textContent).toContain('2026-09-28 08:00:00');
    expect(tip.textContent).toContain('$2.50');

    bar.dispatchEvent(new Event('pointerleave'));
    flushSync();
    expect(target.querySelector('.tip')).toBeNull();
  });

  it('selects the node a row is clicked on', () => {
    open();
    rows()[1].click();
    flushSync();
    expect(picked).toEqual(['2056']);
  });

  it('walks the rows with the arrow keys, and stops at the ends', () => {
    open('2055');
    key('ArrowDown');
    expect(picked).toEqual(['2056']);
    // Nothing below the last row and nothing above the first: the ends of the
    // chart are a real edge of the picture.
    picked = [];
    key('ArrowUp');
    expect(picked).toEqual([]);
  });

  it('marks the selected row', () => {
    open('2056');
    expect(rows()[0].classList.contains('sel')).toBe(false);
    expect(rows()[1].classList.contains('sel')).toBe(true);
  });
});
