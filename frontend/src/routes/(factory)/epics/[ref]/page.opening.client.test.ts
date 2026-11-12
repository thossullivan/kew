/**
 * #2284 r8 — the opening selection (D20) is a decision, not a reading.
 *
 * Until the reader picks a node the epic page opens on the one somebody is
 * waiting on. That default was derived over the LIVE snapshot, so the page
 * re-aimed itself whenever the driver changed which node came first in the
 * needs-you order: a tick (or a between-ticks `attention` frame, which
 * rewrites the whole join) moved the selection, and with it panned the
 * reader's camera through DagView's reveal, swapped the rail's contents and
 * refired the four per-node reads — with `picked` false and the reader having
 * done nothing.
 *
 * So the claims here are about what the SSE tail may and may not move:
 *
 * 1. the page opens on the first waiting node, as it always did;
 * 2. a frame that changes which node is first-needs-you does NOT move the
 *    selection — neither by reordering the waiting nodes, nor by resolving
 *    the one the page opened on;
 * 3. a frame like that takes no reads for another node;
 * 4. the reader still moves it, and a new epic still decides its own;
 * 5. an epic whose driver had not ticked at load gets its opening from the
 *    first snapshot that arrives, since "nobody is waiting" is not knowable
 *    from a graph that is not there yet;
 * 6. and the latch holds an id, not a node — a frame whose graph no longer
 *    carries the node the page opened on clears the selection rather than
 *    handing back an id the snapshot on screen does not have.
 *
 * Mounted rather than read as source (`page.node-url.test.ts` pins the
 * shape): the bug was in WHEN the decision was taken, which only a running
 * component and a frame pushed through its stream can show.
 */
import { flushSync, mount, unmount } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  EpicDetail,
  EpicFrame,
  EpicNode,
  EpicNodeAttention,
  EpicSnapshot,
  EpicTick
} from '$lib/api';

/** `page.url` and `page.state`, reactive, as `$app/state` serves them. */
const kit = new SvelteMap<string, unknown>();

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
vi.mock('$app/navigation', () => ({
  replaceState: vi.fn((url: URL | string, state: App.PageState) => {
    void url;
    kit.set('state', state);
  })
}));

/** The live stream's frame sink, as the page hands it to `streamEpic`. */
let onFrame: ((f: EpicFrame) => void) | null = null;

/** One of the rail's four reads: counted, and failing — what each column
 * says about a failed read is `rail.test.ts`'s business, not this file's. */
const read = (): ReturnType<typeof vi.fn> =>
  vi.fn(async (id: string) => {
    void id;
    throw new Error('not read in this test');
  });

const fetchEpicNode = vi.fn(async (ref: string, id: string) => {
  void ref;
  void id;
  throw new Error('not read in this test');
});
const fetchWorkView = read();
const fetchPipeline = read();
const fetchEvidence = read();

vi.mock('$lib/api', () => ({
  fetchEpic: async () => {
    throw new Error('not read in this test');
  },
  fetchEpicNode,
  fetchEpicTicks: async () => [] as EpicTick[],
  fetchEpicTickStrip: async () => [],
  fetchEpicTimeline: async () => {
    throw new Error('not read in this test');
  },
  fetchEvidence,
  fetchPipeline,
  fetchWorkView,
  streamEpic: (ref: string, sink: (f: EpicFrame) => void) => {
    void ref;
    onFrame = sink;
    return { close: () => {} };
  }
}));

/** What the canvas was last handed. */
type CanvasProps = { selected: string | null; onselect: (id: string | null) => void };
let graph: { props: CanvasProps } = { props: null! };

vi.mock('$lib/epic/DagView.svelte', () => ({
  default: (_anchor: unknown, props: CanvasProps) => {
    graph = { props };
  }
}));
vi.mock('$lib/epic/EpicTimeline.svelte', () => ({ default: () => {} }));
vi.mock('$lib/epic/SelectedRail.svelte', () => ({ default: () => {} }));

const { default: EpicPage } = await import('./+page.svelte');

const waiting: EpicNodeAttention = {
  kind: 'review_decision',
  reason: 'a PR is waiting on a merge decision',
  url: null
};

function node(id: string, attention: EpicNodeAttention | null = null): EpicNode {
  return {
    id,
    title: `node ${id}`,
    status: 'in_progress',
    status_since: '2026-09-28T08:05:00+00:00',
    url: null,
    pr_url: null,
    external: false,
    stuck: null,
    attention,
    blocked_reason: null
  };
}

function snapshot(tickId: number, nodes: EpicNode[]): EpicSnapshot {
  return {
    epic_ref: 'launch-fixes',
    tick_id: tickId,
    tick_ts: '2026-09-29T15:40:00+00:00',
    signal: 'progressing',
    counts: { in_progress: nodes.length },
    frontier: [nodes[0]?.id ?? ''],
    nodes,
    edges: nodes.slice(1).map((n) => [nodes[0].id, n.id] as [string, string]),
    unavailable: []
  };
}

const detail = (nodes: EpicNode[] | null, epicRef = 'launch-fixes'): EpicDetail => ({
  epic_ref: epicRef,
  snapshot: nodes === null ? null : { ...snapshot(9, nodes), epic_ref: epicRef },
  heartbeat: {
    last_tick_started_at: '2026-09-29T15:40:00+00:00',
    last_tick_ts: '2026-09-29T15:40:00+00:00',
    last_tick_duration_s: 1,
    tick_count: 9,
    interval_s: 60,
    last_signal: 'progressing',
    change_count: 0
  },
  economics: { window_start: null, drain_total_usd: 0, node_drain_usd: {}, label: 'spend' }
});

/** One SSE snapshot frame, which is how the driver's next tick arrives. */
function tick(id: number, nodes: EpicNode[]): EpicFrame {
  return {
    type: 'snapshot',
    data: {
      id,
      tick_ts: '2026-09-29T15:41:00+00:00',
      snapshot: snapshot(id, nodes),
      actions: {},
      economics: null
    }
  };
}

let target: HTMLElement;
let component: Record<string, unknown> | null = null;
/** The `data` prop, through a getter so a ref -> ref navigation moves it the
 * way SvelteKit's client router does: one component instance, new `data`. */
const props = new SvelteMap<string, { detail: EpicDetail | null; ref: string }>();

/** Let the page's own reads land — every helper settles, so no test has to
 * know whether it is looking at a frame or at a promise. */
async function settle(): Promise<void> {
  for (let i = 0; i < 3; i++) await Promise.resolve();
  flushSync();
}

async function open(href: string, detailFor: EpicDetail | null, ref = 'launch-fixes') {
  kit.set('url', new URL(href, 'http://kew.test'));
  kit.set('state', {});
  props.set('data', { detail: detailFor, ref });
  target = document.createElement('div');
  document.body.appendChild(target);
  component = mount(EpicPage, {
    target,
    props: {
      get data() {
        return props.get('data')!;
      }
    }
  }) as Record<string, unknown>;
  flushSync();
  await settle();
}

/** Push one frame down the live stream the page opened. */
async function push(frame: EpicFrame): Promise<void> {
  if (!onFrame) throw new Error('the page opened no stream');
  onFrame(frame);
  flushSync();
  await settle();
}

beforeEach(() => {
  onFrame = null;
  graph = { props: null! };
  fetchEpicNode.mockClear();
  fetchWorkView.mockClear();
  fetchPipeline.mockClear();
  fetchEvidence.mockClear();
});

afterEach(() => {
  if (component) unmount(component);
  component = null;
  target?.remove();
  kit.clear();
  props.clear();
});

describe('the page opens on the node that needs a person', () => {
  it('selects the first waiting node, without the reader touching it', async () => {
    await open('/epics/launch-fixes', detail([node('2055'), node('2056', waiting)]));
    expect(graph.props.selected).toBe('2056');
    // The four reads are taken for it, once each.
    expect(fetchPipeline.mock.calls.map((c) => c[0])).toEqual(['2056']);
  });

  it('selects nothing on an epic nobody is holding up', async () => {
    await open('/epics/launch-fixes', detail([node('2055'), node('2056')]));
    expect(graph.props.selected).toBeNull();
    expect(fetchPipeline).not.toHaveBeenCalled();
  });

  it('leaves a `?node=` link its own node', async () => {
    await open('/epics/launch-fixes?node=2055', detail([node('2055'), node('2056', waiting)]));
    expect(graph.props.selected).toBe('2055');
  });
});

describe('the SSE tail does not re-aim the page', () => {
  it('keeps the selection when a frame reorders the waiting nodes', async () => {
    const [a, b] = [node('2055', waiting), node('2056', waiting)];
    await open('/epics/launch-fixes', detail([a, b]));
    expect(graph.props.selected).toBe('2055');

    // The next tick lists the same two waiting nodes the other way round.
    // Derived live, the page would have jumped to 2056 — panning the camera
    // and swapping the rail under a reader who had done nothing.
    await push(tick(10, [b, a]));

    expect(graph.props.selected).toBe('2055');
    // And nothing about 2055 moved, so it did not pay for four more reads.
    expect(fetchPipeline.mock.calls.map((c) => c[0])).toEqual(['2055']);
    expect(fetchEvidence.mock.calls.map((c) => c[0])).toEqual(['2055']);
    expect(fetchWorkView.mock.calls.map((c) => c[0])).toEqual(['2055']);
    expect(fetchEpicNode.mock.calls.map((c) => c[1])).toEqual(['2055']);
  });

  it('keeps it when the frame resolves that node and leaves another waiting', async () => {
    await open('/epics/launch-fixes', detail([node('2055', waiting), node('2056')]));
    expect(graph.props.selected).toBe('2055');

    await push(tick(10, [node('2055'), node('2056', waiting)]));

    expect(graph.props.selected).toBe('2055');
    // 2055 itself changed, so its own reads may be retaken — but no read is
    // ever taken for the node the driver happened to leave waiting.
    expect(fetchPipeline.mock.calls.every((c) => c[0] === '2055')).toBe(true);
  });

  it('keeps it across an `attention` frame, which rewrites the whole join', async () => {
    await open('/epics/launch-fixes', detail([node('2055', waiting), node('2056')]));
    expect(graph.props.selected).toBe('2055');

    // Not tick-gated and not a journal row: serve pushes this between ticks.
    await push({ type: 'attention', data: { by_node: { '2056': waiting }, unavailable: [] } });

    expect(graph.props.selected).toBe('2055');
    expect(fetchPipeline.mock.calls.every((c) => c[0] === '2055')).toBe(true);
  });
});

describe('what does move the opening selection', () => {
  it('the reader, whose pick is final even when it clears the selection', async () => {
    await open('/epics/launch-fixes', detail([node('2055'), node('2056', waiting)]));
    expect(graph.props.selected).toBe('2056');

    graph.props.onselect('2055');
    await settle();
    expect(graph.props.selected).toBe('2055');

    // An Esc that clears it must not be handed the default straight back.
    graph.props.onselect(null);
    await settle();
    expect(graph.props.selected).toBeNull();

    // ...and a later frame still does not hand it back either.
    await push(tick(10, [node('2055'), node('2056', waiting)]));
    expect(graph.props.selected).toBeNull();
  });

  it('a new epic, which decides its own from its own first snapshot', async () => {
    await open('/epics/launch-fixes', detail([node('2055'), node('2056', waiting)]));
    expect(graph.props.selected).toBe('2056');

    // A ref -> ref navigation: SvelteKit reuses this component and hands it
    // new `data`, so the latch has to be re-armed per epic.
    kit.set('url', new URL('/epics/go-live', 'http://kew.test'));
    kit.set('state', {});
    props.set('data', {
      detail: detail([node('3001'), node('3002', waiting)], 'go-live'),
      ref: 'go-live'
    });
    flushSync();
    await settle();

    expect(graph.props.selected).toBe('3002');
  });

  it('a frame that drops the node it opened on, which clears it', async () => {
    await open('/epics/launch-fixes', detail([node('2055'), node('2056', waiting)]));
    expect(graph.props.selected).toBe('2056');

    // The opening pick is latched as an ID off the first snapshot that had
    // nodes, so the graph on screen can stop carrying it. Handed back
    // unvalidated it rang a node DagView cannot draw, left the rail's node
    // null beside a selection that was not, and took the four per-node reads
    // for a node this epic is not showing.
    await push(tick(10, [node('2055')]));

    expect(graph.props.selected).toBeNull();
    // Still just the one read from the opening paint — none for the dropped node.
    expect(fetchPipeline.mock.calls.map((c) => c[0])).toEqual(['2056']);
    expect(fetchEpicNode.mock.calls.map((c) => c[1])).toEqual(['2056']);
  });

  it('the first snapshot of an epic whose driver had not ticked at load', async () => {
    // `snapshot: null` with a heartbeat is a real state (driver running,
    // nothing ticked yet) — and one where nobody can say who is waiting.
    await open('/epics/launch-fixes', detail(null));
    expect(graph.props).toBe(null); // no snapshot, no canvas

    await push(tick(1, [node('2055'), node('2056', waiting)]));

    expect(graph.props.selected).toBe('2056');
  });
});
