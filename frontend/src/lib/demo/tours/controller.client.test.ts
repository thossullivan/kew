/**
 * The tours (#3145) against a stand-in router: where the tour link ends up —
 * the address bar, `page.url`, `page.state` — and whether the tour is open,
 * when a tour opens from a deep link, moves across routes, ends, is ended while
 * its own navigation is in flight, or is come back to through history.
 *
 * The real Tours entry (`TourEntry.svelte`) is mounted, so its `afterNavigate`
 * is what reads each navigation. `$app/state` and `$app/navigation` stand in
 * for SvelteKit and do what it does:
 *
 * - `goto` loads the route, then moves the address, `page.url` and a fresh
 *   `page.state` together, then runs every `afterNavigate` callback, then
 *   resolves. A newer `goto` started while one is loading takes over: the older
 *   one is dropped and its promise resolves at once, as `navigate()` in
 *   SvelteKit's client does with its navigation token.
 * - `replaceState` is shallow: the address and `page.state` move, `page.url`
 *   stays.
 * - `traverse` is Back or Forward: the entry's address and its saved state come
 *   back, and `page.url` comes back as the URL the last real navigation to it
 *   set (SvelteKit keeps it in the history entry), then `afterNavigate` runs.
 * - `afterNavigate` also runs once when the component mounts.
 *
 * driver.js is the real one, drawing into jsdom.
 */
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { BundledTour } from './types';

const kit = vi.hoisted(() => ({
  url: new URL('http://demo.test/work'),
  state: {} as App.PageState,
  /** Each `goto` takes the next; a `goto` that finds a newer one was dropped. */
  token: 0,
  /** Set to hold every `goto` at its load until the test lets it land. */
  hold: null as null | { land: () => void; landed: Promise<void> },
  /** The mounted `afterNavigate` callbacks. */
  after: new Set<() => void>()
}));

vi.mock('$app/state', () => ({
  page: {
    get url() {
      return kit.url;
    },
    get state() {
      return kit.state;
    }
  }
}));

vi.mock('$app/navigation', async () => {
  const { onDestroy, onMount } = await import('svelte');
  const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 0));
  return {
    afterNavigate: (callback: () => void) => {
      onMount(() => {
        kit.after.add(callback);
        callback();
      });
      onDestroy(() => kit.after.delete(callback));
    },
    replaceState: (url: URL | string, state: App.PageState) => {
      window.history.replaceState({}, '', String(url));
      kit.state = state;
    },
    goto: async (to: URL | string, opts: { replaceState?: boolean; state?: App.PageState } = {}): Promise<void> => {
      const mine = ++kit.token;
      await (kit.hold ? kit.hold.landed : tick());
      if (mine !== kit.token) return;
      const url = new URL(String(to), window.location.href);
      if (opts.replaceState) window.history.replaceState({}, '', url);
      else window.history.pushState({}, '', url);
      kit.url = url;
      kit.state = opts.state ?? {};
      await tick();
      for (const callback of kit.after) callback();
    }
  };
});

function step(tour: string, n: number, route: string, target: string) {
  return {
    route,
    target,
    docs: 'web/index.md',
    anchor: 'a',
    passage: `${tour}/${n}`,
    title: `Step ${n}`,
    body: [[{ text: `Step ${n}.` }]],
    href: 'https://kew.dev/docs/web/#a'
  };
}

const TOUR_SET: BundledTour[] = [
  { id: 't', title: 'A tour', start: '/work', steps: [step('t', 1, '/work', 'here'), step('t', 2, '/other', 'there')] },
  {
    id: 'three',
    title: 'Three routes',
    start: '/work',
    steps: [step('three', 1, '/work', 'here'), step('three', 2, '/other', 'there'), step('three', 3, '/third', 'yonder')]
  },
  { id: 'gone', title: 'A tour whose target is missing', start: '/work', steps: [step('gone', 1, '/work', 'nowhere')] },
  {
    id: 'moments',
    title: 'Two replay moments',
    start: '/epic',
    steps: [
      { ...step('moments', 1, '/epic', 'graph'), query: { mode: 'replay', t: '0' } },
      { ...step('moments', 2, '/epic', 'graph'), query: { mode: 'replay', t: '60' } }
    ]
  },
  { id: 'plain', title: 'A step that names no query', start: '/epic', steps: [step('plain', 1, '/epic', 'graph')] },
  {
    id: 'item',
    title: 'A tour that selects a Work item',
    start: '/other',
    steps: [
      step('item', 1, '/other', 'there'),
      { ...step('item', 2, '/work', 'here'), query: { item: '107' } },
      { ...step('item', 3, '/work', 'yonder'), query: { item: '107' } },
      step('item', 4, '/work', 'here')
    ]
  }
];
vi.mock('./bundle', () => ({ TOURS: TOUR_SET }));

const { clearSpot, endTour, startTour } = await import('./controller');
const { readTourLink } = await import('./deep-link');
const { goto, replaceState } = await import('$app/navigation');
const { default: TourEntry } = await import('./TourEntry.svelte');

/** Land on `path` as a fresh load or a pasted link does: nothing in the state. */
function land(path: string): void {
  window.history.replaceState({}, '', path);
  kit.url = new URL(window.location.href);
  kit.state = {};
}

/** Back or Forward to an entry: its address, its state, and the `page.url` it was navigated to with. */
function traverse(address: string, pageUrl: string, state: App.PageState): void {
  window.history.replaceState({}, '', address);
  kit.url = new URL(pageUrl, window.location.href);
  kit.state = state;
  for (const callback of kit.after) callback();
}

/** A row click on the Work page: its own write, built from its state key, replacing the state. */
function workRowClick(item: string): void {
  const url = new URL(kit.url);
  url.search = kit.state.workSearch ?? kit.url.search;
  url.searchParams.set('item', item);
  replaceState(url, { workSearch: url.search });
}

const popover = () => document.querySelector('.driver-popover.kew-tour');
const shownStep = () => popover()?.getAttribute('data-tour-step') ?? null;
const waitForStep = (passage: string) => vi.waitFor(() => expect(shownStep()).toBe(passage));
const pressEscape = () => window.dispatchEvent(new KeyboardEvent('keyup', { key: 'Escape' }));
const pressNext = () => (document.querySelector('.driver-popover-next-btn') as HTMLElement).click();
const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

let summary: HTMLElement;
let entry: ReturnType<typeof mount> | null = null;

/** Mount the Tours entry, as the header does on every factory page. */
function mountEntry(): void {
  const target = document.querySelector('[data-slot]') as HTMLElement;
  entry = mount(TourEntry, { target });
  flushSync();
}

/** Start `id` from the Tours entry, as a press on it does. */
function startFromMenu(id: string): void {
  (document.querySelector(`[data-tour-start="${id}"]`) as HTMLElement).click();
}

beforeEach(() => {
  // A desktop window, and no motion: driver.js then draws without animating.
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: true,
    media: query,
    addEventListener() {},
    removeEventListener() {}
  }));
  // Every step's target is drawn, whatever the route: what is under test is
  // the tour's route and link, not the pages.
  document.body.innerHTML =
    '<details><summary aria-label="More destinations">More</summary><div data-slot></div></details>' +
    '<main><aside data-tour="here">rows</aside><i data-tour="there">b</i><i data-tour="yonder">c</i><i data-tour="graph">d</i></main>';
  summary = document.querySelector('summary') as HTMLElement;
  kit.token = 0;
  kit.hold = null;
});

afterEach(() => {
  kit.hold = null;
  if (entry) unmount(entry);
  entry = null;
  endTour();
  kit.after.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('the focus a step opens with (design review c9 D16)', () => {
  it('lands on the forward press, not on the close press', async () => {
    land('/work?tour=three&step=1');
    mountEntry();
    await waitForStep('three/1');
    await settle();
    expect(document.activeElement).toBe(document.querySelector('.driver-popover-next-btn'));
    pressNext();
    await waitForStep('three/2');
    await settle();
    expect(document.activeElement).toBe(document.querySelector('.driver-popover-next-btn'));
  });
});

describe('a tour opened by a deep link', () => {
  it("leaves no link in page.url or the page's own state once it ends, and gives focus to More", async () => {
    land('/work?tour=t&step=1&item=7');
    mountEntry();
    await waitForStep('t/1');
    // Open: the address names the step; `page.url`, which a page builds its
    // next write from, does not.
    expect(window.location.search).toBe('?item=7&tour=t&step=1');
    expect(kit.url.search).toBe('?item=7');
    expect(kit.state.workSearch).toBe('?item=7&tour=t&step=1');

    pressEscape();
    expect(popover()).toBeNull();
    expect(window.location.search).toBe('?item=7');
    expect(kit.url.search).toBe('?item=7');
    expect(kit.state.workSearch).toBe('?item=7');
    expect(readTourLink(kit.state, kit.url)).toBeNull();
    expect(document.activeElement).toBe(summary);
  });
});

describe('moving across routes', () => {
  it('two quick Next presses across routes leave the tour open on the third step, on its route', async () => {
    land('/work');
    mountEntry();
    startFromMenu('three');
    await waitForStep('three/1');
    // The second press comes before /other has loaded: SvelteKit drops that
    // navigation for the one to /third.
    pressNext();
    pressNext();
    await waitForStep('three/3');
    await settle();
    expect(shownStep()).toBe('three/3');
    expect(window.location.pathname).toBe('/third');
    expect(window.location.search).toBe('?tour=three&step=3');
  });
});

describe('a step that names a query', () => {
  it("navigates to its route with the query, so page.url carries the moment and no tour link", async () => {
    land('/work');
    mountEntry();
    startFromMenu('moments');
    await waitForStep('moments/1');
    expect(kit.url.pathname).toBe('/epic');
    expect(kit.url.search).toBe('?mode=replay&t=0');
    expect(window.location.search).toBe('?mode=replay&t=0&tour=moments&step=1');
  });

  it("moves to the next step's moment on the same route, keeping the page's other keys", async () => {
    land('/epic?node=4&mode=replay&t=0');
    mountEntry();
    startFromMenu('moments');
    await waitForStep('moments/1');
    // The address already holds step 1's moment: nothing to navigate to.
    expect(kit.url.search).toBe('?node=4&mode=replay&t=0');
    // The epic page's own write, as it answers a moment: the state holds it.
    replaceState(new URL(window.location.href), { ...kit.state, epicMode: 'replay', replayT: 0 });
    pressNext();
    await waitForStep('moments/2');
    await vi.waitFor(() => expect(kit.url.searchParams.get('t')).toBe('60'));
    expect(kit.url.searchParams.get('node')).toBe('4');
    expect(kit.url.searchParams.has('tour')).toBe(false);
    // The new moment arrives without the state the page left: the page reads
    // its state before `page.url`, and that state holds the old moment.
    expect(kit.state.replayT).toBeUndefined();
    expect(kit.state.tour).toBe('moments');
    expect(window.location.search).toBe('?node=4&mode=replay&t=60&tour=moments&step=2');
    pressEscape();
    expect(window.location.search).toBe('?node=4&mode=replay&t=60');
  });

  it("a deep link to a later step opens on that step's moment, not the one in the address", async () => {
    land('/epic?mode=replay&t=0&tour=moments&step=2');
    mountEntry();
    await waitForStep('moments/2');
    expect(kit.url.search).toBe('?mode=replay&t=60');
    expect(window.location.search).toBe('?mode=replay&t=60&tour=moments&step=2');
  });
});

describe("the epic page's keys a step owns: mode, t, speed, view and select", () => {
  it("a replay step drops the visitor's timeline and selection, so the graph its passage reads is drawn", async () => {
    land('/epic?view=timeline&select=107&node=4&speed=10');
    mountEntry();
    startFromMenu('moments');
    await waitForStep('moments/1');
    expect(kit.url.search).toBe('?node=4&mode=replay&t=0');
    // A new moment arrives with no state, so the page reads the address.
    expect(kit.state.epicView).toBeUndefined();
    expect(kit.state.select).toBeUndefined();
    expect(window.location.search).toBe('?node=4&mode=replay&t=0&tour=moments&step=1');
  });

  it('a step that names no query leaves a replay address for the live page, keeping the other keys', async () => {
    land('/epic?node=4&mode=replay&t=60&speed=10&view=timeline&tour=plain&step=1');
    mountEntry();
    await waitForStep('plain/1');
    expect(kit.url.search).toBe('?node=4');
    expect(kit.state.epicMode).toBeUndefined();
    expect(kit.state.replayT).toBeUndefined();
    expect(window.location.search).toBe('?node=4&tour=plain&step=1');
  });

  it('a step whose keys the address already holds does not navigate', async () => {
    land('/epic?node=4');
    const landed = kit.url;
    mountEntry();
    startFromMenu('plain');
    await waitForStep('plain/1');
    // `page.url` is the one the visitor landed on: no navigation was made.
    expect(kit.url).toBe(landed);
    expect(window.location.search).toBe('?node=4&tour=plain&step=1');
  });
});

describe('a step whose query selects a Work item', () => {
  it('navigates to its ?item=; a later step with the same query, or with none, stays put and keeps the item', async () => {
    land('/other');
    mountEntry();
    startFromMenu('item');
    await waitForStep('item/1');
    pressNext();
    await waitForStep('item/2');
    expect(kit.url.search).toBe('?item=107');
    expect(window.location.pathname + window.location.search).toBe('/work?item=107&tour=item&step=2');
    // Every navigation the tour makes pushes an entry; staying put pushes none.
    const entries = window.history.length;
    const landed = kit.url;
    pressNext();
    await waitForStep('item/3');
    pressNext();
    await waitForStep('item/4');
    await settle();
    // `item` is not a key a step owns, so step 4, naming no query, keeps it.
    expect(kit.url).toBe(landed);
    expect(window.location.pathname + window.location.search).toBe('/work?item=107&tour=item&step=4');
    expect(window.history.length).toBe(entries);
  });

  it("a deep link to the item step on another item opens on the step's item", async () => {
    land('/work?item=108&tour=item&step=3');
    mountEntry();
    await waitForStep('item/3');
    expect(kit.url.search).toBe('?item=107');
    expect(window.location.search).toBe('?item=107&tour=item&step=3');
  });
});

describe('ending a tour while its own navigation is in flight', () => {
  it('leaves no tour link in the address the navigation lands on', async () => {
    land('/work');
    mountEntry();
    startTour('t', 1, summary);
    await waitForStep('t/1');
    let open = () => {};
    const landed = new Promise<void>((resolve) => (open = resolve));
    kit.hold = { land: open, landed };
    // Next: step 2 is on /other, so the tour navigates first.
    pressNext();
    expect(popover()).not.toBeNull();
    // The window narrows below 1024 before /other has drawn.
    endTour();
    open();
    await vi.waitFor(() => expect(window.location.pathname).toBe('/other'));
    await settle();
    expect(window.location.search).toBe('');
    expect(readTourLink(kit.state, kit.url)).toBeNull();
    expect(popover()).toBeNull();
  });

  it('ends a tour started on another route before its first popover shows', async () => {
    land('/other');
    mountEntry();
    let open = () => {};
    const landed = new Promise<void>((resolve) => (open = resolve));
    kit.hold = { land: open, landed };
    startTour('t', 1, summary);
    endTour();
    open();
    await vi.waitFor(() => expect(window.location.pathname).toBe('/work'));
    await settle();
    expect(window.location.search).toBe('');
    expect(readTourLink(kit.state, kit.url)).toBeNull();
    expect(popover()).toBeNull();
  });

  it("takes the link out of the address when a tour ends while waiting for a step's target", async () => {
    // Forward to an entry the tour was open on: the address holds the link,
    // `page.url` does not, and this step's target has not drawn yet.
    mountEntry();
    traverse('/work?tour=gone&step=1', '/work', {});
    expect(popover()).toBeNull();
    endTour();
    expect(window.location.search).toBe('');
  });
});

describe('coming back to an entry through history', () => {
  it('a Work row click mid-tour, Back, Forward: the address and the tour agree', async () => {
    land('/monitor');
    mountEntry();
    await goto('/work');
    startFromMenu('t');
    await waitForStep('t/1');
    workRowClick('Y');
    // The Work page's write kept the link in the address and dropped `tour`
    // from the state.
    expect(window.location.search).toBe('?tour=t&step=1&item=Y');
    expect(kit.state.tour).toBeUndefined();
    const workEntry = { address: `${window.location.pathname}${window.location.search}`, state: kit.state };

    traverse('/monitor', '/monitor', {});
    expect(popover()).toBeNull();
    traverse(workEntry.address, '/work', workEntry.state);
    // The address names step 1, so step 1 is what shows.
    await waitForStep('t/1');
    expect(window.location.search).toBe('?tour=t&step=1&item=Y');

    pressEscape();
    expect(window.location.search).toBe('?item=Y');
    workRowClick('Z');
    expect(window.location.search).toBe('?item=Z');
  });

  it('an address that still carries a link the state ended is cleaned', async () => {
    mountEntry();
    traverse('/work?item=7&tour=t&step=1', '/work', { workSearch: '?item=7', tour: null, tourStep: null });
    await settle();
    expect(popover()).toBeNull();
    expect(window.location.search).toBe('?item=7');
  });
});

describe('a link no tour can open', () => {
  it.each([
    ['a tour this build does not have', '/work?item=7&tour=nope&step=1'],
    ['a step past the end of the tour', '/work?item=7&tour=t&step=3'],
    ['step 0 of a tour that exists', '/work?item=7&tour=t&step=0'],
    ['a step that is not a number', '/work?item=7&tour=t&step=abc']
  ])('opens nothing and is taken out of the address: %s', async (_, address) => {
    land(address);
    mountEntry();
    await settle();
    expect(popover()).toBeNull();
    expect(window.location.search).toBe('?item=7');
    // The page's next write builds on its state key: no tour comes back with it.
    workRowClick('8');
    expect(window.location.search).toBe('?item=8');
  });

  it('opens nothing on a window narrower than the tours run at, and is taken out of the address', async () => {
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: false,
      media: query,
      addEventListener() {},
      removeEventListener() {}
    }));
    land('/work?item=7&tour=t&step=1');
    mountEntry();
    await settle();
    expect(popover()).toBeNull();
    expect(window.location.search).toBe('?item=7');
  });

  it('a link to a step past the end, followed mid-tour, ends the tour and leaves no link', async () => {
    land('/work');
    mountEntry();
    startFromMenu('t');
    await waitForStep('t/1');
    traverse('/work?tour=t&step=5', '/work', {});
    await settle();
    expect(popover()).toBeNull();
    expect(window.location.search).toBe('');
  });
});

describe('where a moved popover goes (clearSpot)', () => {
  const box = (left: number, top: number, right: number, bottom: number) => ({ left, top, right, bottom });

  it('beside what it must not cover, on the right first, inside the window', () => {
    const spot = clearSpot([box(100, 100, 300, 200)], 200, 100, 1440, 960);
    expect(spot).toEqual({ left: 312, top: 100, covered: 0 });
  });

  it('clear of a dialog the target sits in, not only of the target', () => {
    // The target is a block inside a node popover that runs to the right
    // edge: the right has no room, so the spot is to the left of both.
    const target = box(1100, 300, 1400, 400);
    const dialog = box(1080, 100, 1430, 640);
    const spot = clearSpot([target, dialog], 300, 200, 1440, 960);
    expect(spot.covered).toBe(0);
    expect(spot.left + 300).toBeLessThanOrEqual(1080);
  });

  it('covering least when nothing in the window is clear', () => {
    const spot = clearSpot([box(0, 0, 1366, 600)], 300, 200, 1366, 650);
    expect(spot.covered).toBeGreaterThan(0);
    expect(spot.top + 200).toBeLessThanOrEqual(650);
  });
});

describe('a step whose target has not drawn', () => {
  it('says so on the console after 5s, naming the tour, the step and the selector', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    land('/work');
    mountEntry();
    startTour('gone', 1, summary);
    await vi.advanceTimersByTimeAsync(5_000);
    expect(error).toHaveBeenCalledTimes(1);
    const said = String(error.mock.calls[0][0]);
    expect(said).toContain('gone');
    expect(said).toContain('step 1');
    expect(said).toContain('[data-tour="nowhere"]');
  });

  it('ends when the visitor navigates away while it waits, and draws nothing on the new page', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    land('/work');
    mountEntry();
    startTour('gone', 1, summary);
    void goto('/monitor');
    await vi.advanceTimersByTimeAsync(10);
    expect(window.location.pathname).toBe('/monitor');
    await vi.advanceTimersByTimeAsync(5_000);
    expect(popover()).toBeNull();
    expect(error).not.toHaveBeenCalled();
  });
});
