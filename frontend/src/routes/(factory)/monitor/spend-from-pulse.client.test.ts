/**
 * #2847 (Q2) — the spend gauges move with the shell's pulse, and the page
 * makes no spend read of its own.
 *
 * The page used to poll `/api/report/spend` on the 7s heartbeat beside the
 * pulse's `/api/dashboard` read, whose `glance.caps` is the same two windows:
 * two reads of one figure per beat, per tab. What #2282 (C9) asked of that poll
 * still holds — a Monitor left open prints the figures the fleet is spending
 * now, not the ones it was opened with — so it is what is pinned here, off the
 * pulse's reading, with every request the page makes watched at the network.
 *
 * jsdom, because what is under test is a reactive read and the render after it.
 */
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Dashboard, SpendWindows } from '$lib/api';

vi.mock('$app/environment', () => ({ browser: true }));
vi.mock('$app/state', () => ({ page: { url: new URL('http://kew.test/monitor'), state: {} } }));
vi.mock('$app/navigation', () => ({ replaceState: vi.fn() }));
// The three columns are stubbed: what is under test is the readout row.
vi.mock('$lib/monitor/EventStream.svelte', () => ({ default: () => {} }));
vi.mock('$lib/monitor/Holds.svelte', () => ({ default: () => {} }));
vi.mock('$lib/monitor/LiveRuns.svelte', () => ({ default: () => {} }));

const { default: MonitorPage } = await import('./+page.svelte');
const { factoryPulse, PULSE_INTERVAL_MS } = await import('$lib/shell/pulse.svelte');
const { SPEND_UNREAD } = await import('$lib/monitor/readouts');

/** A spend payload with `agentic` — the enforced figure the gauge reads (N5). */
function spend(daily: number, weekly: number): SpendWindows {
  return {
    daily: { window_days: 1, spent: daily, cap: 250, agentic: daily, interactive: 0 },
    weekly: { window_days: 7, spent: weekly, cap: 1500, agentic: weekly, interactive: 0 }
  };
}

/** A dashboard as the pulse holds it, carrying `caps` (or `null`, fail-soft). */
function dashboard(caps: SpendWindows | null): Dashboard {
  return {
    banner: { fleet_stopped: false, no_live_driver: false },
    rows: [],
    glance: {
      fleet: null,
      caps,
      routing: [],
      ready_queue: [],
      queue_candidates: [],
      recent_completions: []
    },
    as_of: {},
    unavailable: caps === null ? ['caps'] : []
  } as unknown as Dashboard;
}

const budget = { available: false as const, reason: 'not read' };

let target: HTMLElement;
let component: Record<string, unknown> | null = null;
let requested: string[];

function open(): void {
  target = document.createElement('div');
  document.body.appendChild(target);
  component = mount(MonitorPage, { target, props: { data: { budget } } }) as Record<
    string,
    unknown
  >;
  flushSync();
}

beforeEach(() => {
  vi.useFakeTimers();
  requested = [];
  // The network boundary: every path the page asks for, answered with nothing.
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: RequestInfo | URL) => {
      requested.push(new URL(String(url), 'http://kew.test').pathname);
      return new Response('{}');
    })
  );
  factoryPulse.dashboard = null;
});

afterEach(() => {
  if (component) unmount(component);
  component = null;
  target?.remove();
  factoryPulse.dashboard = null;
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('the spend gauges', () => {
  it("print the pulse's caps, and follow its next reading", () => {
    factoryPulse.dashboard = dashboard(spend(178.05, 915.05));
    open();
    expect(target.textContent).toContain('$178.05');

    factoryPulse.dashboard = dashboard(spend(181.4, 920.0));
    flushSync();

    expect(target.textContent).toContain('$181.40');
    expect(target.textContent).toContain('$920'); // gaugeMoney drops the .00
    expect(target.textContent).not.toContain('$178.05');
  });

  it('say the spend is not read before the pulse has landed, then answer', () => {
    open();
    expect(target.textContent).toContain(SPEND_UNREAD);

    factoryPulse.dashboard = dashboard(spend(12.5, 40.0));
    flushSync();

    expect(target.textContent).toContain('$12.50');
    expect(target.textContent).not.toContain(SPEND_UNREAD);
  });

  it('say the spend is unavailable when the dashboard lost its caps', () => {
    factoryPulse.dashboard = dashboard(null);
    open();
    expect(target.textContent).toContain('spend unavailable');
  });

  it('make no spend request of their own over several beats', async () => {
    factoryPulse.dashboard = dashboard(spend(1, 2));
    open();
    await vi.advanceTimersByTimeAsync(PULSE_INTERVAL_MS * 3);
    flushSync();

    expect(requested.filter((path) => path === '/api/report/spend')).toEqual([]);
  });
});
