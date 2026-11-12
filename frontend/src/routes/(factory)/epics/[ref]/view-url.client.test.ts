/**
 * #2271 — the epic page's half of the Graph / Timeline switch: what the
 * switch writes has to come back to it as its query, and the selection the
 * two views share has to survive the crossing.
 *
 * The Monitor shipped exactly this bug once (#2248, and
 * `monitor/stream-url.client.test.ts` is its regression): the page read its
 * query off `page.url` and wrote it with `replaceState` — and shallow routing
 * does not move `page.url`. SvelteKit keeps it at the last real navigation and
 * updates `page.state` alone, so every write snapped straight back. A second
 * way to ship it is open here, because `replaceState` REPLACES the page state:
 * a view switch that wrote only `{ epicView }` would clear `page.state.node`,
 * and the selection the two views are meant to hand each other would vanish
 * on the way across.
 *
 * So this runs the writes through the page, with `$app/state` and
 * `$app/navigation` standing in for SvelteKit and doing exactly what it does
 * on a `replaceState`: the state moves, the URL the page reads does not.
 */
import { flushSync, mount, unmount } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { EpicDetail, EpicNode, EpicTimeline } from '$lib/api';

/** `page.url` and `page.state`, reactive, as `$app/state` serves them. */
const kit = new SvelteMap<string, unknown>();

const replaceState = vi.fn((url: URL | string, state: App.PageState) => {
  // SvelteKit's `replaceState`: history and `page.state` move; `page.url`
  // stays at the last real navigation.
  void url;
  kit.set('state', state);
});

vi.mock('$app/environment', () => ({ browser: true }));
vi.mock('$app/state', () => ({
  page: {
    get url() {
      return kit.get('url') as URL;
    },
    get state() {
      return kit.get('state') as App.PageState;
    }
  }
}));
vi.mock('$app/navigation', () => ({ replaceState }));

const TIMELINE: EpicTimeline = {
  epic_ref: 'launch-fixes',
  now: '2026-09-29T15:40:00+00:00',
  span: { start: '2026-09-28T08:05:00+00:00', end: '2026-09-29T15:40:00+00:00' },
  nodes: [
    {
      id: '2055',
      title: 'first',
      bands: [{ status: 'in_progress', from: '2026-09-28T08:05:00+00:00', to: null }],
      runs: [],
      rounds: [],
      merged_at: null
    },
    { id: '2056', title: 'second', bands: [], runs: [], rounds: [], merged_at: null }
  ]
};

const unread = async () => {
  throw new Error('not read in this test');
};

vi.mock('$lib/api', () => ({
  fetchEpic: unread,
  fetchEpicNode: unread,
  fetchEpicTicks: unread,
  // #2304's strip is read on every live epic page; it is not this test's
  // subject, so it answers an empty strip rather than refusing.
  fetchEpicTickStrip: vi.fn(async () => []),
  fetchEpicTimeline: vi.fn(async () => TIMELINE),
  fetchEvidence: unread,
  fetchPipeline: unread,
  fetchWorkView: unread,
  streamEpic: () => ({ close: () => {} })
}));

/** What each canvas was last handed, and how many times it was mounted. */
type CanvasProps = { selected: string | null; onselect: (id: string | null) => void };
let graph: { props: CanvasProps; mounts: number } = { props: null!, mounts: 0 };
let timeline: { props: CanvasProps; mounts: number } = { props: null!, mounts: 0 };

vi.mock('$lib/epic/DagView.svelte', () => ({
  default: (_anchor: unknown, props: CanvasProps) => {
    graph = { props, mounts: graph.mounts + 1 };
  }
}));
vi.mock('$lib/epic/EpicTimeline.svelte', () => ({
  default: (_anchor: unknown, props: CanvasProps) => {
    timeline = { props, mounts: timeline.mounts + 1 };
  }
}));
vi.mock('$lib/epic/SelectedRail.svelte', () => ({ default: () => {} }));

const { default: EpicPage } = await import('./+page.svelte');

function node(id: string, title: string): EpicNode {
  return {
    id,
    title,
    status: 'in_progress',
    status_since: '2026-09-28T08:05:00+00:00',
    url: null,
    pr_url: null,
    external: false,
    stuck: null,
    attention: null,
    blocked_reason: null
  };
}

const detail = (signal = 'progressing'): EpicDetail => ({
  epic_ref: 'launch-fixes',
  snapshot: {
    epic_ref: 'launch-fixes',
    tick_id: 9,
    tick_ts: '2026-09-29T15:40:00+00:00',
    signal,
    counts: { in_progress: 2 },
    frontier: ['2055'],
    nodes: [node('2055', 'first'), node('2056', 'second')],
    edges: [['2055', '2056']],
    unavailable: []
  },
  heartbeat: {
    last_tick_started_at: '2026-09-29T15:40:00+00:00',
    last_tick_ts: '2026-09-29T15:40:00+00:00',
    last_tick_duration_s: 1,
    tick_count: 9,
    interval_s: 60,
    last_signal: signal,
    change_count: 0
  },
  economics: {
    window_start: null,
    drain_total_usd: 0,
    node_drain_usd: {},
    label: 'spend'
  }
});

let target: HTMLElement;
let component: Record<string, unknown> | null = null;

/** A real navigation: a new URL, and the state SvelteKit resets with it. */
function navigate(href: string): void {
  kit.set('url', new URL(href, 'http://kew.test'));
  kit.set('state', {});
  flushSync();
}

/**
 * Let the timeline's own read land. It is a fetch, not an SSE frame, so the
 * chart appears a microtask after the view does — every helper below settles,
 * so no test has to know which of the two it is looking at.
 */
async function settle(): Promise<void> {
  for (let i = 0; i < 3; i++) await Promise.resolve();
  flushSync();
}

async function open(href: string, signal = 'progressing'): Promise<void> {
  navigate(href);
  target = document.createElement('div');
  document.body.appendChild(target);
  component = mount(EpicPage, {
    target,
    props: { data: { detail: detail(signal), ref: 'launch-fixes' } }
  }) as Record<string, unknown>;
  flushSync();
  await settle();
}

/** The search the page last wrote to the address bar. */
const written = (): string => new URL(replaceState.mock.calls.at(-1)![0]).search;

/** Click the switch's "Graph" or "Timeline" chip, the way a reader does. */
async function click(label: string): Promise<void> {
  const button = [...target.querySelectorAll('button')].find(
    (b) => b.textContent?.trim() === label
  );
  if (!button) throw new Error(`no ${label} chip on the page`);
  button.click();
  flushSync();
  await settle();
}

/** A key press on the page, which is where `T` is bound. */
async function press(key: string): Promise<void> {
  window.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
  flushSync();
  await settle();
}

beforeEach(() => {
  replaceState.mockClear();
  graph = { props: null!, mounts: 0 };
  timeline = { props: null!, mounts: 0 };
});

afterEach(() => {
  if (component) unmount(component);
  component = null;
  target?.remove();
  kit.clear();
});

describe('a switch from the chips', () => {
  it('comes back as the view, although page.url does not move', async () => {
    await open('/epics/launch-fixes');
    expect(graph.mounts).toBe(1);
    expect(timeline.mounts).toBe(0);

    await click('Timeline');

    expect(written()).toBe('?view=timeline');
    // The regression: `page.url` is still `/epics/launch-fixes` here, as it is
    // in a real browser, and a page reading the view off it would have stayed
    // on the graph.
    expect((kit.get('url') as URL).search).toBe('');
    expect(timeline.mounts).toBe(1);
  });

  it('and back again, writing the default as the absence of the param', async () => {
    await open('/epics/launch-fixes?view=timeline');
    expect(timeline.mounts).toBe(1);
    expect(graph.mounts).toBe(0);

    await click('Graph');

    // `?view=graph` is not a state; it is noise in a link somebody will paste.
    expect(written()).toBe('');
    expect(graph.mounts).toBe(1);
  });

  it('is T as well as the chips', async () => {
    await open('/epics/launch-fixes');
    await press('t');
    expect(written()).toBe('?view=timeline');
    expect(timeline.mounts).toBe(1);
    await press('T');
    expect(written()).toBe('');
    expect(graph.mounts).toBe(2);
  });
});

describe('the selection the two views share', () => {
  it('survives the crossing, in both directions', async () => {
    await open('/epics/launch-fixes?view=timeline');
    expect(timeline.props.selected).toBeNull();

    // A click on a row, as the chart makes it.
    timeline.props.onselect('2055');
    await settle();
    expect(written()).toContain('node=2055');
    expect(written()).toContain('view=timeline');
    expect(timeline.props.selected).toBe('2055');

    // ...and the switch does not drop it. `replaceState` replaces the page
    // state, so a write naming only the view would have cleared `node`.
    await click('Graph');
    expect(written()).toBe('?node=2055');
    expect(graph.props.selected).toBe('2055');

    await click('Timeline');
    // Order only: `writeUrl` builds on `page.url`, which is still the URL the
    // page was navigated to, so a param that was already there keeps its place.
    expect(written()).toContain('node=2055');
    expect(written()).toContain('view=timeline');
    expect(timeline.props.selected).toBe('2055');
  });

  it('is still the snapshot`s to validate, on either canvas', async () => {
    // A stale or hand-typed `?node=` must not put the rail on a node this
    // epic does not have — the same guard the graph has always had.
    await open('/epics/launch-fixes?view=timeline&node=9999');
    expect(timeline.props.selected).toBeNull();
  });
});

describe('a real navigation', () => {
  it('opens on what the URL says', async () => {
    await open('/epics/launch-fixes?view=timeline&node=2056');
    expect(timeline.mounts).toBe(1);
    expect(timeline.props.selected).toBe('2056');
  });

  it('replaces what the switch last wrote', async () => {
    await open('/epics/launch-fixes');
    await click('Timeline');
    expect(timeline.mounts).toBe(1);
    const canvases = [graph.mounts, timeline.mounts];

    // A link to another epic — the nav tab, or a row on /epics. SvelteKit
    // resets the page state with it, so the new epic opens on the graph
    // rather than inheriting the last one's view.
    navigate('/epics/go-live-2026-09');
    expect(graph.mounts).toBe(canvases[0] + 1);
    expect(timeline.mounts).toBe(canvases[1]);
  });
});
