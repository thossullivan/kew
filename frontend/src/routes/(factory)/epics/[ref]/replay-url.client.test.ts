/**
 * #3142 — a link opens the epic's replay at a moment and a speed:
 * `?mode=replay&t=<seconds>&speed=<1|10|60>`.
 *
 * The page writes those three keys with `replaceState`, as it writes `node`,
 * `view` and `select`, and `replaceState` REPLACES the whole page state — so
 * every write has to carry every key, or a node click drops the moment the
 * reader was looking at. Shallow routing does not move `page.url` either
 * (#2248, #2250, #2267), so the page has to read what it wrote back off
 * `page.state`. This drives the page with `$app/state` and `$app/navigation`
 * standing in for SvelteKit and doing exactly what it does.
 */
import { flushSync, mount, unmount } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { EpicDetail, EpicNode, EpicSnapshot, EpicTick, EpicTimeline } from '$lib/api';

/** `page.url` and `page.state`, reactive, as `$app/state` serves them. */
const kit = new SvelteMap<string, unknown>();

const replaceState = vi.fn((url: URL | string, state: App.PageState) => {
  // SvelteKit's `replaceState`: history (the address bar) and `page.state`
  // move; `page.url` stays at the last real navigation.
  history.replaceState(null, '', String(url));
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
  span: {
    start: '2026-09-28T08:05:00+00:00',
    end: '2026-09-29T15:40:00+00:00'
  },
  nodes: [
    {
      id: '2055',
      title: 'first',
      bands: [{ status: 'in_progress', from: '2026-09-28T08:05:00+00:00', to: null }],
      runs: [],
      rounds: [],
      merged_at: null
    },
    {
      id: '2056',
      title: 'second',
      bands: [],
      runs: [],
      rounds: [],
      merged_at: null
    }
  ]
};

const unread = async () => {
  throw new Error('not read in this test');
};

vi.mock('$lib/api', () => ({
  fetchEpic: unread,
  fetchEpicNode: unread,
  fetchEpicTicks: vi.fn(async (_ref: string, after: number) => (after === 0 ? TICKS : [])),
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
type CanvasProps = {
  snapshot: EpicSnapshot;
  selected: string | null;
  onselect: (id: string | null) => void;
};
let graph: { props: CanvasProps; mounts: number } = { props: null!, mounts: 0 };
let timeline: { props: CanvasProps; mounts: number } = {
  props: null!,
  mounts: 0
};

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
const { fetchEpicTicks } = await import('$lib/api');
const { writeTourLink } = await import('$lib/demo/tours/deep-link');
const { playbackDwellMs } = await import('$lib/epic/replay');

function node(id: string, status: string): EpicNode {
  return {
    id,
    title: `node ${id}`,
    status,
    status_since: '2026-09-28T08:05:00+00:00',
    url: null,
    pr_url: null,
    external: false,
    stuck: null,
    attention: null,
    blocked_reason: null
  };
}

function snapshot(tickId: number, tickTs: string, first: string, second: string): EpicSnapshot {
  return {
    epic_ref: 'launch-fixes',
    tick_id: tickId,
    tick_ts: tickTs,
    signal: 'progressing',
    counts: {},
    frontier: [],
    nodes: [node('2055', first), node('2056', second)],
    edges: [['2055', '2056']],
    unavailable: []
  };
}

/** Three frames, at +0s, +600s and +3600s: nothing done, one done, both done. */
const TICKS: EpicTick[] = [
  ['2026-09-29T14:40:00+00:00', 'in_progress', 'todo'],
  ['2026-09-29T14:50:00+00:00', 'done', 'in_progress'],
  ['2026-09-29T15:40:00+00:00', 'done', 'done']
].map(([ts, first, second], i) => ({
  id: i + 1,
  tick_ts: ts,
  snapshot: snapshot(i + 1, ts, first, second),
  actions: {},
  economics: null
}));

const detail = (changeCount = 3): EpicDetail => ({
  epic_ref: 'launch-fixes',
  snapshot: TICKS[2].snapshot,
  heartbeat: {
    last_tick_started_at: '2026-09-29T15:40:00+00:00',
    last_tick_ts: '2026-09-29T15:40:00+00:00',
    last_tick_duration_s: 1,
    tick_count: 3,
    interval_s: 60,
    last_signal: 'progressing',
    change_count: changeCount
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

async function settle(): Promise<void> {
  for (let i = 0; i < 6; i++) await Promise.resolve();
  flushSync();
}

async function open(href: string, epic: EpicDetail = detail()): Promise<void> {
  kit.set('url', new URL(href, location.origin));
  kit.set('state', {});
  history.replaceState(null, '', href);
  flushSync();
  target = document.createElement('div');
  document.body.appendChild(target);
  component = mount(EpicPage, {
    target,
    props: { data: { detail: epic, ref: 'launch-fixes' } }
  }) as Record<string, unknown>;
  flushSync();
  await settle();
}

/**
 * A real navigation within the page, as `goto` makes one: `page.url` moves,
 * `page.state` starts empty, and the component stays mounted for the same ref.
 */
async function navigate(href: string): Promise<void> {
  history.pushState(null, '', href);
  kit.set('url', new URL(href, location.origin));
  kit.set('state', {});
  flushSync();
  await settle();
}

/** The query the page last wrote to the address bar, as a map. */
const written = (): Record<string, string> =>
  Object.fromEntries(new URL(replaceState.mock.calls.at(-1)![0]).searchParams);

/** Which nodes the frame on screen has done. */
const doneOnScreen = (): string[] =>
  graph.props.snapshot.nodes.filter((n) => n.status === 'done').map((n) => n.id);

function button(label: string): HTMLButtonElement {
  const found = [...target.querySelectorAll('button')].find((b) => b.textContent?.trim() === label);
  if (!found) throw new Error(`no ${label} button on the page`);
  return found;
}

const scrubber = (): HTMLInputElement =>
  target.querySelector<HTMLInputElement>('input[aria-label="Replay frame"]')!;

/** Drag the scrubber across frames without letting go: one `input` per frame passed. */
function dragThrough(frames: number[]): void {
  for (const frame of frames) {
    scrubber().value = String(frame);
    scrubber().dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
  }
}

/** Let go of the scrubber: the browser's `change`. */
function release(): void {
  scrubber().dispatchEvent(new Event('change', { bubbles: true }));
  flushSync();
}

/** Drag the scrubber to a frame and let go, the way a reader does. */
function seekTo(frame: number): void {
  dragThrough([frame]);
  release();
}

async function click(label: string): Promise<void> {
  button(label).click();
  flushSync();
  await settle();
}

beforeEach(() => {
  replaceState.mockClear();
  vi.mocked(fetchEpicTicks).mockClear();
  graph = { props: null!, mounts: 0 };
  timeline = { props: null!, mounts: 0 };
});

afterEach(() => {
  if (component) unmount(component);
  component = null;
  target?.remove();
  kit.clear();
  history.replaceState(null, '', '/');
});

describe('a replay link', () => {
  it('opens paused on the frame its t names', async () => {
    await open('/epics/launch-fixes?mode=replay&t=0');
    expect(doneOnScreen()).toEqual([]);
    expect(button('▶ play')).toBeTruthy();
    expect(written()).toEqual({ mode: 'replay', t: '0' });
  });

  it('draws the same graph for the same t, and the last t is the epic as it stands', async () => {
    await open('/epics/launch-fixes?mode=replay&t=900');
    expect(doneOnScreen()).toEqual(['2055']);
    unmount(component!);
    target.remove();
    await open('/epics/launch-fixes?mode=replay&t=900');
    expect(doneOnScreen()).toEqual(['2055']);
    unmount(component!);
    target.remove();
    await open('/epics/launch-fixes?mode=replay&t=3600');
    expect(graph.props.snapshot).toEqual(detail().snapshot);
  });

  it('round-trips mode, t and speed', async () => {
    await open('/epics/launch-fixes?mode=replay&t=600&speed=10');
    expect(written()).toEqual({ mode: 'replay', t: '600', speed: '10' });
    expect(button('10x').getAttribute('aria-pressed')).toBe('true');

    await click('60x');
    expect(written()).toEqual({ mode: 'replay', t: '600', speed: '60' });
    // Read back off `page.state`: `page.url` still says 10x, as in a browser.
    expect((kit.get('url') as URL).searchParams.get('speed')).toBe('10');
    expect(button('60x').getAttribute('aria-pressed')).toBe('true');
    expect(button('10x').getAttribute('aria-pressed')).toBe('false');

    seekTo(2);
    expect(written()).toEqual({ mode: 'replay', t: '3600', speed: '60' });
    expect(doneOnScreen()).toEqual(['2055', '2056']);
  });

  it('keeps t and speed through a node click and a view switch', async () => {
    await open('/epics/launch-fixes?mode=replay&t=600&speed=10');
    // Seek first, so the moment is one only `page.state` holds: `page.url`
    // still says t=600, and a writer building on it alone would put that back.
    seekTo(2);
    graph.props.onselect('2056');
    await settle();
    expect(written()).toEqual({
      node: '2056',
      mode: 'replay',
      t: '3600',
      speed: '10'
    });
    expect(graph.props.selected).toBe('2056');

    await click('Timeline');
    expect(written()).toEqual({
      node: '2056',
      view: 'timeline',
      mode: 'replay',
      t: '3600',
      speed: '10'
    });
  });

  it('drops all three on the way back to live', async () => {
    await open('/epics/launch-fixes?mode=replay&t=600&speed=10&node=2055');
    await click('live');
    expect(written()).toEqual({ node: '2055' });
  });

  it('is not demo-only: the replay toggle writes the mode for any epic with ticks', async () => {
    await open('/epics/launch-fixes');
    expect(replaceState).not.toHaveBeenCalled();
    await click('replay');
    expect(written()).toEqual({ mode: 'replay', t: '0' });
  });

  it('plays from the first frame when replay is pressed again after live, not from the moment the link named', async () => {
    // `page.url` still says t=600 after the press of live, and only
    // `page.state` says that moment is gone: a page reading `?t=` off
    // `page.url` alone reopened the old link's moment, paused.
    await open('/epics/launch-fixes?mode=replay&t=600');
    expect(doneOnScreen()).toEqual(['2055']);
    await click('live');
    await click('replay');
    expect(button('⏸ pause')).toBeTruthy();
    expect(doneOnScreen()).toEqual([]);
    expect(written()).toEqual({ mode: 'replay', t: '0' });
  });

  it('writes the address once per drag, on release, naming the frame it let go on', async () => {
    await open('/epics/launch-fixes?mode=replay&t=0');
    replaceState.mockClear();
    // Browsers refuse a flood of history writes (Safari and Firefox throw
    // after a few hundred in ten seconds), so the frames a drag passes over
    // move the graph and not the address bar.
    dragThrough([1, 2, 1, 2, 1, 2]);
    expect(doneOnScreen()).toEqual(['2055', '2056']);
    expect(replaceState).not.toHaveBeenCalled();
    release();
    expect(replaceState).toHaveBeenCalledTimes(1);
    expect(written()).toEqual({ mode: 'replay', t: '3600' });
  });

  it('keeps the query keys and state keys it does not own, such as an open tour', async () => {
    await open('/epics/launch-fixes?mode=replay&t=0');
    // The demo's tour writes its link in after the navigation, shallowly, as
    // every writer here does: the address bar and `page.state` carry it,
    // `page.url` never does.
    history.replaceState(null, '', '/epics/launch-fixes?mode=replay&t=0&tour=replay&step=2');
    kit.set('state', {
      ...(kit.get('state') as App.PageState),
      tour: 'replay',
      tourStep: 2
    } as App.PageState);
    flushSync();
    graph.props.onselect('2056');
    await settle();
    expect(written()).toEqual({
      node: '2056',
      mode: 'replay',
      t: '0',
      tour: 'replay',
      step: '2'
    });
    expect(replaceState.mock.calls.at(-1)![1]).toMatchObject({ tour: 'replay', tourStep: 2 });
  });

  it('does not write back a key another writer took out of the address bar', async () => {
    await open('/epics/launch-fixes?mode=replay&t=0&tour=replay&step=1');
    history.replaceState(null, '', '/epics/launch-fixes?mode=replay&t=0');
    graph.props.onselect('2056');
    await settle();
    expect(written()).toEqual({ node: '2056', mode: 'replay', t: '0' });
  });
});

describe('a replay link to the epic already on screen', () => {
  it('enters replay at its t from live', async () => {
    await open('/epics/launch-fixes');
    expect(doneOnScreen()).toEqual(['2055', '2056']);
    await navigate('/epics/launch-fixes?mode=replay&t=600');
    expect(doneOnScreen()).toEqual(['2055']);
    expect(button('▶ play')).toBeTruthy();
    expect(written()).toEqual({ mode: 'replay', t: '600' });
    // ...and the next write keeps it, rather than the live frame's nothing.
    graph.props.onselect('2056');
    await settle();
    expect(written()).toEqual({ node: '2056', mode: 'replay', t: '600' });
  });

  it('moves a running replay to its t and speed', async () => {
    await open('/epics/launch-fixes?mode=replay&t=3600');
    expect(doneOnScreen()).toEqual(['2055', '2056']);
    await navigate('/epics/launch-fixes?mode=replay&t=0&speed=60');
    expect(doneOnScreen()).toEqual([]);
    expect(button('60x').getAttribute('aria-pressed')).toBe('true');
    expect(written()).toEqual({ mode: 'replay', t: '0', speed: '60' });
  });

  it('goes back to live for a link that names no replay', async () => {
    await open('/epics/launch-fixes?mode=replay&t=0');
    await navigate('/epics/launch-fixes');
    expect(doneOnScreen()).toEqual(['2055', '2056']);
    expect(button('replay')).toBeTruthy();
    expect(written()).toEqual({});
  });
});

describe('a replay link to an epic with nothing to replay', () => {
  const NOTHING = 'This epic has no journal to replay yet.';

  it('says so, and keeps the link through a click', async () => {
    await open('/epics/launch-fixes?mode=replay&t=600&speed=10', detail(0));
    expect(target.textContent).toContain(NOTHING);
    graph.props.onselect('2056');
    await settle();
    expect(written()).toEqual({ node: '2056', mode: 'replay', t: '600', speed: '10' });
    expect(target.textContent).toContain(NOTHING);
  });

  it('says nothing for an epic opened live', async () => {
    await open('/epics/launch-fixes', detail(0));
    expect(target.textContent).not.toContain(NOTHING);
  });
});

describe('the live view', () => {
  it('never reads the journal', async () => {
    await open('/epics/launch-fixes');
    graph.props.onselect('2056');
    await settle();
    await click('Timeline');
    expect(fetchEpicTicks).not.toHaveBeenCalled();
  });
});

describe('a navigation that does not move the moment', () => {
  /**
   * A demo tour's deep link landing on a playing replay (#3167): the page
   * enters the replay while the journal is still on its way, the journal
   * lands and plays from its first frame (the page writes `t=0`), and then
   * the tour navigates to the same address without its `?tour=&step=`,
   * carrying the state the page left. That navigation names the moment the
   * address already held, so it must leave the replay playing.
   */
  async function landWhileTheJournalLoads(href: string): Promise<() => Promise<void>> {
    let land!: () => void;
    const loading = new Promise<void>((resolve) => (land = resolve));
    vi.mocked(fetchEpicTicks).mockImplementationOnce(async () => {
      await loading;
      return TICKS;
    });
    await open(href);
    expect(target.textContent).toContain('loading replay frames');
    return async () => {
      land();
      await settle();
    };
  }

  /** The tour's own navigation: the address bar as it stands, its link out, the page's state kept. */
  async function tourTakesItsLinkOut(): Promise<void> {
    const clean = writeTourLink(kit.get('state') as App.PageState, new URL(location.href), null);
    history.replaceState(null, '', String(clean.url));
    kit.set('url', clean.url);
    kit.set('state', clean.state);
    flushSync();
    await settle();
  }

  it('leaves a playing replay playing', async () => {
    const journalLands = await landWhileTheJournalLoads('/epics/launch-fixes?mode=replay&tour=x&step=3');
    await journalLands();
    expect(button('⏸ pause')).toBeTruthy();
    expect(written()).toEqual({ mode: 'replay', t: '0', tour: 'x', step: '3' });
    await tourTakesItsLinkOut();
    expect(button('⏸ pause')).toBeTruthy();
    expect(doneOnScreen()).toEqual([]);
  });

  it('leaves it playing on a later frame too, since the navigation carries the moment the page wrote', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    try {
      const journalLands = await landWhileTheJournalLoads('/epics/launch-fixes?mode=replay&tour=x&step=3');
      await journalLands();
      vi.advanceTimersByTime(playbackDwellMs(TICKS.length, 1));
      flushSync();
      expect(doneOnScreen()).toEqual(['2055']);
      await tourTakesItsLinkOut();
      expect(button('⏸ pause')).toBeTruthy();
      expect(doneOnScreen()).toEqual(['2055']);
    } finally {
      vi.useRealTimers();
    }
  });

  it('still pauses a playing replay on the moment a link moves it to', async () => {
    const journalLands = await landWhileTheJournalLoads('/epics/launch-fixes?mode=replay');
    await journalLands();
    expect(button('⏸ pause')).toBeTruthy();
    await navigate('/epics/launch-fixes?mode=replay&t=600');
    expect(button('▶ play')).toBeTruthy();
    expect(doneOnScreen()).toEqual(['2055']);
  });

  /**
   * A fresh link (a pasted address, a tour's Next) carries no state the page
   * wrote, so it pauses on its moment even when that moment is the frame on
   * screen or the `t` the page last wrote: only a navigation that carries the
   * page's own written moment back leaves the replay playing.
   */
  async function playOneFrameThenFollow(href: string): Promise<void> {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    try {
      await open('/epics/launch-fixes?mode=replay');
      expect(written()).toEqual({ mode: 'replay', t: '0' });
      vi.advanceTimersByTime(playbackDwellMs(TICKS.length, 1));
      flushSync();
      expect(doneOnScreen()).toEqual(['2055']);
      expect(button('⏸ pause')).toBeTruthy();
      await navigate(href);
    } finally {
      vi.useRealTimers();
    }
  }

  it('pauses on a fresh link to the frame on screen', async () => {
    await playOneFrameThenFollow('/epics/launch-fixes?mode=replay&t=600');
    expect(button('▶ play')).toBeTruthy();
    expect(doneOnScreen()).toEqual(['2055']);
  });

  it('pauses on a fresh link to the t the page last wrote', async () => {
    await playOneFrameThenFollow('/epics/launch-fixes?mode=replay&t=0');
    expect(button('▶ play')).toBeTruthy();
    expect(doneOnScreen()).toEqual([]);
  });
});
