/**
 * #2248 — the Monitor page's half of the event stream's URL state: what the
 * stream writes has to come back to it as its query.
 *
 * It did not, in a real browser, and every unit test was green. The page read
 * the query off `page.url` and wrote it with `replaceState` — and shallow
 * routing does not move `page.url`: SvelteKit keeps it at the last real
 * navigation and updates `page.state` alone. So a preset click rewrote the
 * address bar and the stream snapped straight back to Live 30m, and so did
 * every brush drag, pan, zoom and filter. The component tests could not see it:
 * they hand the component a query and record what it writes, and never ran the
 * write through the page that is meant to give it back.
 *
 * This runs it through the page, with `$app/state` and `$app/navigation`
 * standing in for SvelteKit and doing exactly what SvelteKit does on a
 * `replaceState`: the state moves, the URL the page reads does not.
 */
import { flushSync, mount, unmount } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
// Types only, so this import is erased and cannot run before the mocks below.
import type { StreamQuery } from '$lib/monitor/stream-query';

/** `page.url` and `page.state`, reactive, as `$app/state` serves them. */
const kit = new SvelteMap<string, unknown>();

const replaceState = vi.fn((url: URL | string, state: App.PageState) => {
  // SvelteKit's `replaceState`: history and `page.state` move; `page.url` stays
  // at the last real navigation.
  void url;
  kit.set('state', state);
});

/** The props the page last handed the stream. */
let stream: { query: StreamQuery; onquery: (next: StreamQuery) => void } | null = null;

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
// The page's three columns are stubbed: what is under test is the wiring
// between the page and the stream, not anything they fetch.
vi.mock('$lib/monitor/EventStream.svelte', () => ({
  default: (_anchor: unknown, props: typeof stream) => {
    stream = props;
  }
}));
vi.mock('$lib/monitor/Holds.svelte', () => ({ default: () => {} }));
vi.mock('$lib/monitor/LiveRuns.svelte', () => ({ default: () => {} }));

const { default: MonitorPage } = await import('./+page.svelte');
const { applyPreset, jumpToNow, toggleKind, withWindow } = await import(
  '$lib/monitor/stream-query'
);

const NOW = Date.parse('2026-10-02T10:04:00.000Z');
const unread = { available: false as const, reason: 'not read' };

let target: HTMLElement;
let component: Record<string, unknown> | null = null;

/** A real navigation: a new URL, and the state SvelteKit resets with it. */
function navigate(href: string): void {
  kit.set('url', new URL(href, 'http://kew.test'));
  kit.set('state', {});
  flushSync();
}

/** The stream's write, the way the component makes it. */
function write(next: StreamQuery): void {
  stream!.onquery(next);
  flushSync();
}

/** The query the page is handing the stream now. */
const query = (): StreamQuery => stream!.query;

/** The search the page last wrote to the address bar. */
const written = (): string => new URL(replaceState.mock.calls.at(-1)![0]).search;

function open(href: string): void {
  navigate(href);
  target = document.createElement('div');
  document.body.appendChild(target);
  component = mount(MonitorPage, {
    target,
    props: { data: { budget: unread } }
  }) as Record<string, unknown>;
  flushSync();
}

beforeEach(() => {
  replaceState.mockClear();
  stream = null;
});

afterEach(() => {
  if (component) unmount(component);
  component = null;
  target?.remove();
  kit.clear();
});

describe('a write from the stream', () => {
  it('comes back as its query, although page.url does not move', () => {
    open('/monitor');
    expect(query().preset).toBe('live-30m');

    write(applyPreset(query(), '24h', NOW));

    expect(written()).toBe('?range=24h');
    // The regression: `page.url` is still `/monitor` here, as it is in a real
    // browser, and the stream read Live 30m off it.
    expect((kit.get('url') as URL).search).toBe('');
    expect(query().preset).toBe('24h');
  });

  it('holds a brushed window, and the next write builds on it', () => {
    open('/monitor?range=24h');
    const window = { startMs: NOW - 3_600_000, endMs: NOW - 1_800_000 };

    write(withWindow(query(), window));
    expect(query()).toMatchObject({ preset: '24h', fromMs: window.startMs, toMs: window.endMs });

    // A filter after a brush keeps the brush: each write starts from the query
    // the last one left, not from the URL the page was opened on.
    write(toggleKind(query(), 'review'));
    expect(query()).toMatchObject({ fromMs: window.startMs, kinds: ['review'] });
    expect(written()).toContain('range=24h');
    expect(written()).toContain('from=');
    expect(written()).toContain('kind=review');
  });

  it('goes back to live on L', () => {
    open('/monitor?range=7d');
    write(jumpToNow(query()));
    expect(query().preset).toBe('live-30m');
    expect(written()).toBe('');
  });

  it('keeps a param the stream does not own', () => {
    open('/monitor?panel=holds');
    write(applyPreset(query(), '6h', NOW));
    expect(written()).toBe('?panel=holds&range=6h');
  });
});

describe('a real navigation', () => {
  it('opens on what the URL says', () => {
    open('/monitor?range=7d&epic=ui-refine&kind=money');
    expect(query()).toMatchObject({ preset: '7d', epic: 'ui-refine', kinds: ['money'] });
  });

  it('replaces what the stream last wrote', () => {
    open('/monitor');
    write(applyPreset(query(), '24h', NOW));
    expect(query().preset).toBe('24h');

    // A link to the Monitor — the nav tab, or "Filter to #2219" from elsewhere.
    navigate('/monitor?item=2219');
    expect(query()).toMatchObject({ preset: 'live-30m', item: '2219' });
  });
});
