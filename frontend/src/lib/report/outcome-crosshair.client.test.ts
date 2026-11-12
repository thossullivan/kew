/**
 * AC2 of C3/#2301 — the crosshair syncs across the four charts, in a DOM.
 *
 * `outcomes.test.ts` pins the arithmetic: which band a ratio falls in. What
 * cannot be checked that way is the thing the acceptance criterion actually
 * names — that pointing at ONE chart lights the SAME week on all four, and on
 * the shared axis. A per-chart hover state would pass every pure test and
 * fail this one.
 *
 * jsdom has no layout, so `getBoundingClientRect` is stood up with the width a
 * browser would report for the plot cells; without it the handler correctly
 * refuses to map a pointer across a zero-width box.
 */
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import type { OutcomeReport } from '$lib/api';

const PLOT_W = 800;

beforeEach(() => {
  Object.defineProperty(Element.prototype, 'getBoundingClientRect', {
    configurable: true,
    value: () => ({
      left: 0,
      top: 0,
      width: PLOT_W,
      height: 60,
      right: PLOT_W,
      bottom: 60,
      x: 0,
      y: 0,
      toJSON: () => ({})
    })
  });
});

const { default: OutcomeCharts } = await import('./OutcomeCharts.svelte');

/** Four weeks, each distinguishable, with a reading on every chart. */
const REPORT: OutcomeReport = {
  weeks: [0, 1, 2, 3].map((i) => ({
    week_start: `2026-09-${(7 + i * 7).toString().padStart(2, '0')}`,
    merged_main: 40 + i * 10,
    merged_kew_touched: 20 + i,
    merged_kew_authored: 18 + i,
    ready_items: 10 + i,
    ready_hours: { p50: 2 + i, p90: 6, max: 9 },
    rounds_to_ready: { p50: 1 + i * 0.5, max: 4 },
    cost_per_merged: { mean: 26, p50: 20 + i },
    spend_total: 700,
    spend_unattributed: 30,
    nodes_done: 20,
    pr_sync_observed_at: '2026-09-27T18:00:00+00:00',
    partial: i === 3
  })),
  min_sample: 3,
  merged_main_head: 'abc1234',
  merged_main_unavailable: null
};

let host: HTMLElement;
let app: Record<string, unknown> | null = null;

function render(report: OutcomeReport = REPORT): HTMLElement {
  host = document.createElement('div');
  document.body.appendChild(host);
  app = mount(OutcomeCharts, { target: host, props: { report } });
  flushSync();
  return host;
}

afterEach(() => {
  if (app) unmount(app);
  app = null;
  host?.remove();
});

/** The four plot boxes, in the order the page draws them. */
function plots(): HTMLElement[] {
  return [...host.querySelectorAll<HTMLElement>('[role="img"]')];
}

/** Point at *week* by clicking the middle of its band on *plot*. */
function hover(plot: HTMLElement, weekIndex: number): void {
  const x = ((weekIndex + 0.5) / REPORT.weeks.length) * PLOT_W;
  plot.dispatchEvent(new PointerEvent('pointermove', { clientX: x, bubbles: true }));
  flushSync();
}

/** The chart keys currently showing an active mark. */
function lit(): string[] {
  return [...host.querySelectorAll('[data-active-mark]')].map(
    (node) => node.getAttribute('data-active-mark') ?? ''
  );
}

describe('the synced crosshair', () => {
  it('draws all four charts over one axis', () => {
    render();
    expect(plots()).toHaveLength(4);
    expect(host.querySelectorAll('[data-week]')).toHaveLength(REPORT.weeks.length);
  });

  it('lights the same week on all four when one chart is hovered', () => {
    render();
    hover(plots()[1], 2);
    expect(lit().sort()).toEqual(['cost', 'merged', 'ready', 'rounds']);
    expect(host.querySelector('[aria-current="true"]')?.getAttribute('data-week')).toBe(
      REPORT.weeks[2].week_start
    );
  });

  it('moves every chart together, from whichever chart is pointed at', () => {
    render();
    hover(plots()[3], 0);
    expect(host.querySelector('[aria-current="true"]')?.getAttribute('data-week')).toBe(
      REPORT.weeks[0].week_start
    );
    hover(plots()[0], 3);
    expect(host.querySelector('[aria-current="true"]')?.getAttribute('data-week')).toBe(
      REPORT.weeks[3].week_start
    );
    // Still exactly one week lit, on exactly four charts.
    expect(host.querySelectorAll('[aria-current="true"]')).toHaveLength(1);
    expect(lit()).toHaveLength(4);
  });

  it('prints the hovered week on every row, not a tooltip', () => {
    render();
    hover(plots()[0], 1);
    const readings = [...host.querySelectorAll('.font-mono.text-13')].map(
      (node) => node.textContent ?? ''
    );
    expect(readings.filter((text) => text.startsWith('Sep 14'))).toHaveLength(4);
    expect(readings.some((text) => text.includes('n='))).toBe(true);
  });

  it('drops the crosshair when the pointer leaves the stack', () => {
    render();
    hover(plots()[0], 1);
    host.firstElementChild!.dispatchEvent(new PointerEvent('pointerleave', { bubbles: false }));
    flushSync();
    expect(lit()).toEqual([]);
    expect(host.querySelector('[aria-current="true"]')).toBeNull();
  });

  it('steps the crosshair from the keyboard, newest week first', () => {
    render();
    const group = host.querySelector<HTMLElement>('[role="application"]')!;
    const press = (key: string) => {
      group.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
      flushSync();
    };
    press('ArrowLeft');
    expect(host.querySelector('[aria-current="true"]')?.getAttribute('data-week')).toBe(
      REPORT.weeks[2].week_start
    );
    press('Home');
    expect(host.querySelector('[aria-current="true"]')?.getAttribute('data-week')).toBe(
      REPORT.weeks[0].week_start
    );
    press('Escape');
    expect(lit()).toEqual([]);
  });

  it('leaves a modifier press to the browser: ⌘← is Back, not the crosshair', () => {
    render();
    const group = host.querySelector<HTMLElement>('[role="application"]')!;
    for (const init of [{ metaKey: true }, { ctrlKey: true }, { altKey: true }]) {
      const event = new KeyboardEvent('keydown', {
        key: 'ArrowLeft',
        bubbles: true,
        cancelable: true,
        ...init
      });
      group.dispatchEvent(event);
      flushSync();
      expect(event.defaultPrevented, JSON.stringify(init)).toBe(false);
    }
    expect(lit()).toEqual([]);
  });

  it('says why a chart is empty instead of drawing an empty box', () => {
    render({
      ...REPORT,
      merged_main_unavailable: 'origin/main could not be read from the local clone',
      weeks: REPORT.weeks.map((w) => ({ ...w, merged_main: null }))
    });
    // Three plots left, and a sentence where the fourth was.
    expect(plots()).toHaveLength(3);
    expect(host.textContent).toContain('could not be read from the local clone');
  });
});
