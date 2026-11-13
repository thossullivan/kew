/**
 * #2305 — the motion clock follows the STREAM, not the first read.
 *
 * DESIGN.md's motion rule: "The clock ticks once a second, stops when nothing
 * executes or the stream drops". The canvas implements the stopping half —
 * `DagView`'s `streaming` prop takes the halo and the running bar off every
 * card and freezes its clock — but the page fed that prop `connected`, which
 * is `data.detail != null`: the FIRST read's answer, latched true for the life
 * of the page. A severed SSE channel therefore kept animating halos and
 * growing bars indefinitely, drawing seconds nothing had confirmed.
 *
 * So the claims here are about the prop the canvas is handed:
 *
 * 1. an open stream animates, as it always did;
 * 2. an `error` on the EventSource stops it, on the FIRST error rather than
 *    whichever one wins the auth-probe's 5s debounce;
 * 3. EventSource's own auto-retry starts it again, via `open`;
 * 4. a frame is proof of the channel too, for a proxy that swallowed `open`;
 * 5. a drop stops the MOTION only — the canvas's other prop, `replay`, is what
 *    says a frame is history, and a dropped live stream is not history (review
 *    r5: fused into one flag, a drop moved every card's age onto the last
 *    tick's `tick_ts`).
 *
 * Mounted rather than read as source: the bug was in a latch, and only a
 * running component can show a value that never comes back down.
 */
import { flushSync, mount, unmount } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { EpicDetail, EpicFrame, EpicNode, EpicSnapshot, EpicTick } from '$lib/api';

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
/**
 * The EventSource stand-in the page was handed, with the handlers it set.
 *
 * Handlers typed WITHOUT EventSource's `this`: the page only ever assigns
 * them, and a stub that claimed the real `this` could not then be called with
 * itself.
 */
type StreamStub = {
  close: () => void;
  onopen?: ((ev: Event) => unknown) | null;
  onerror?: ((ev: Event) => unknown) | null;
};
let source: StreamStub = { close: () => {} };

const fetchEpic = vi.fn(async (ref: string) => {
  void ref;
  throw new Error('not read in this test');
});

const read = (): ReturnType<typeof vi.fn> =>
  vi.fn(async (id: string) => {
    void id;
    throw new Error('not read in this test');
  });

vi.mock('$lib/api', () => ({
  fetchEpic: (ref: string) => fetchEpic(ref),
  fetchEpicNode: async () => {
    throw new Error('not read in this test');
  },
  fetchEpicTicks: async () => [] as EpicTick[],
  fetchEpicTickStrip: async () => [],
  fetchEpicTimeline: async () => {
    throw new Error('not read in this test');
  },
  fetchEvidence: read(),
  fetchPipeline: read(),
  fetchWorkView: read(),
  streamEpic: (ref: string, sink: (f: EpicFrame) => void) => {
    void ref;
    onFrame = sink;
    source = { close: () => {} };
    return source;
  }
}));

/** What the canvas was last handed. `props` is Svelte's live accessor object,
 * so re-reading it after a flush shows what the page says NOW. */
type CanvasProps = { streaming: boolean; replay: boolean };
let graph: { props: CanvasProps } = { props: null! };

vi.mock('$lib/epic/DagView.svelte', () => ({
  default: (_anchor: unknown, props: CanvasProps) => {
    graph = { props };
  }
}));
vi.mock('$lib/epic/EpicTimeline.svelte', () => ({ default: () => {} }));
vi.mock('$lib/epic/SelectedRail.svelte', () => ({ default: () => {} }));

const { default: EpicPage } = await import('./+page.svelte');

function node(id: string): EpicNode {
  return {
    id,
    title: `node ${id}`,
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

const detail = (nodes: EpicNode[]): EpicDetail => ({
  epic_ref: 'launch-fixes',
  snapshot: snapshot(9, nodes),
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
const props = new SvelteMap<string, { detail: EpicDetail | null; ref: string }>();

async function settle(): Promise<void> {
  for (let i = 0; i < 3; i++) await Promise.resolve();
  flushSync();
}

async function open(detailFor: EpicDetail | null): Promise<void> {
  kit.set('url', new URL('/epics/launch-fixes', 'http://kew.test'));
  kit.set('state', {});
  props.set('data', { detail: detailFor, ref: 'launch-fixes' });
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

/** Fire the stream's `error`, the way EventSource does on a dropped channel. */
async function drop(): Promise<void> {
  if (!source.onerror) throw new Error('the page watched no stream errors');
  source.onerror(new Event('error'));
  flushSync();
  await settle();
}

/** Fire the stream's `open`, the way EventSource's auto-retry does. */
async function reopen(): Promise<void> {
  if (!source.onopen) throw new Error('the page watched no stream opens');
  source.onopen(new Event('open'));
  flushSync();
  await settle();
}

beforeEach(() => {
  onFrame = null;
  source = { close: () => {} };
  graph = { props: null! };
  fetchEpic.mockClear();
});

afterEach(() => {
  if (component) unmount(component);
  component = null;
  target?.remove();
  kit.clear();
  props.clear();
});

describe('the canvas animates only while the stream is up', () => {
  it('animates an epic whose stream is open', async () => {
    await open(detail([node('2055'), node('2056')]));
    expect(graph.props.streaming).toBe(true);
  });

  it('stops on the first stream error, not on the one that wins the probe', async () => {
    await open(detail([node('2055')]));
    expect(graph.props.streaming).toBe(true);

    await drop();

    expect(graph.props.streaming).toBe(false);
    // The auth probe still fires — telling a 401 from a blip is its job, and
    // stopping the clock must not have displaced it.
    expect(fetchEpic).toHaveBeenCalledWith('launch-fixes');
  });

  it('stays stopped through a second error inside the probe debounce', async () => {
    await open(detail([node('2055')]));
    await drop();
    await drop();
    expect(graph.props.streaming).toBe(false);
  });

  it('starts again when EventSource reconnects', async () => {
    await open(detail([node('2055')]));
    await drop();
    expect(graph.props.streaming).toBe(false);

    await reopen();

    expect(graph.props.streaming).toBe(true);
  });

  it('does not call a live frame history when the stream drops', async () => {
    // The two facts are separate props (review r5): `streaming` is about the
    // CHANNEL and stops the motion; `replay` is about the FRAME and chooses
    // the clock its ages are read against. Fused, a dropped live stream read
    // every card against the last tick's `tick_ts`, so the ages jumped
    // backwards at the drop and froze there.
    await open(detail([node('2055')]));
    expect(graph.props.replay).toBe(false);

    await drop();

    expect(graph.props.streaming).toBe(false);
    expect(graph.props.replay).toBe(false);
  });

  it('counts a frame as proof of the channel', async () => {
    // A proxy can swallow `open` and still deliver data; a frame arriving is
    // the channel, whatever the open event did.
    await open(detail([node('2055')]));
    await drop();
    expect(graph.props.streaming).toBe(false);

    if (!onFrame) throw new Error('the page opened no stream');
    onFrame(tick(10, [node('2055')]));
    flushSync();
    await settle();

    expect(graph.props.streaming).toBe(true);
  });
});
