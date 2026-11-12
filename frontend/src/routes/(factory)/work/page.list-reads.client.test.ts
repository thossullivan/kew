/**
 * #3055 (C7-3) — Work keeps one `/api/dashboard/work` read open per tab,
 * counting the page load's own.
 *
 * The pulse's re-reads take a `SingleRead` slot, and so does the loader's read
 * of the list: a server slow from the very first read would otherwise see the
 * second pulse open a list read beside the load's, still out. The page is
 * mounted in jsdom with `fetch` as the boundary; every list read is held, and
 * the count is of reads sent and not yet settled or aborted.
 */
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Dashboard } from '$lib/api';
import dashboardJson from '../../../../fixtures/factory/dashboard.json';
import workJson from '../../../../fixtures/factory/dashboard-work.json';

vi.mock('$app/environment', () => ({ browser: true }));
vi.mock('$app/navigation', () => ({ replaceState: () => {} }));
vi.mock('$app/state', () => ({
  page: { url: new URL('http://localhost/work'), state: {} }
}));

const { factoryPulse } = await import('$lib/shell/pulse.svelte');
const { load } = await import('./+page');
const { default: WorkPage } = await import('./+page.svelte');

const LIST = '/api/dashboard/work';

/** A fetch that holds every list read until `release()`; any other read never answers. */
function heldServer() {
  let sent = 0;
  let open = 0;
  let most = 0;
  const held: Array<() => void> = [];
  const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), 'http://localhost').pathname;
    if (path !== LIST) return new Promise<Response>(() => {});
    sent += 1;
    open += 1;
    most = Math.max(most, open);
    return new Promise<Response>((resolve, reject) => {
      let settled = false;
      const close = () => {
        if (settled) return false;
        settled = true;
        open -= 1;
        return true;
      };
      init?.signal?.addEventListener('abort', () => {
        if (close()) reject(new DOMException('aborted', 'AbortError'));
      });
      held.push(() => {
        // A whole projection, as the server serves it: a bare `{ views: [] }`
        // has no `unreadable`, and the page's notices read its length.
        if (close()) resolve(new Response(JSON.stringify(workJson)));
      });
    });
  }) as unknown as typeof globalThis.fetch;
  return {
    fetch,
    sent: () => sent,
    most: () => most,
    release: () => held.splice(0).forEach((answer) => answer())
  };
}

/** One shell pulse: a new dashboard reading, which is what the page re-reads on. */
function pulse(): void {
  factoryPulse.dashboard = structuredClone(dashboardJson) as unknown as Dashboard;
  flushSync();
}

/** Let settled promises run their continuations. */
const drain = () => new Promise((resolve) => setTimeout(resolve, 0));

let component: Record<string, unknown> | null = null;

afterEach(() => {
  if (component) unmount(component);
  component = null;
  factoryPulse.dashboard = null;
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

describe('Work list reads (#3055)', () => {
  it('sends no pulse list read while the page load’s read is still out', async () => {
    const server = heldServer();
    vi.stubGlobal('fetch', server.fetch);
    const target = document.body.appendChild(document.createElement('div'));
    component = mount(WorkPage, { target, props: { data: load({ fetch: server.fetch }) } });
    flushSync();
    for (let tick = 0; tick < 4; tick += 1) pulse();
    await drain();
    expect(server.sent()).toBe(1);
    expect(server.most()).toBe(1);

    // Once the load's read answers, the next pulse reads the list again — and
    // a pulse after that, finding THAT read out, still sends nothing.
    server.release();
    await drain();
    await drain();
    pulse();
    pulse();
    await drain();
    expect(server.sent()).toBe(2);
    expect(server.most()).toBe(1);
    server.release();
  });
});

/**
 * A server whose list reads answer at once, the first with `first` and every
 * one after with `later`, and whose `/api/dashboard` read is held until
 * `releaseDashboard()`; any other read never answers.
 */
function slowDashboardServer(first: unknown, later: unknown) {
  let lists = 0;
  let dashboard: (() => void) | null = null;
  const fetch = vi.fn((input: RequestInfo | URL) => {
    const path = new URL(String(input), 'http://localhost').pathname;
    if (path === LIST) {
      lists += 1;
      return Promise.resolve(new Response(JSON.stringify(lists === 1 ? first : later)));
    }
    if (path === '/api/dashboard') {
      return new Promise<Response>((resolve) => {
        dashboard = () => resolve(new Response(JSON.stringify(dashboardJson)));
      });
    }
    return new Promise<Response>(() => {});
  }) as unknown as typeof globalThis.fetch;
  return {
    fetch,
    lists: () => lists,
    releaseDashboard: () => dashboard?.()
  };
}

/** The state word of `item`'s row, as drawn. */
const stateWord = (item: string) =>
  document
    .querySelector(`aside[aria-label="Issues"] a[data-item="${item}"] [data-status-word]`)
    ?.textContent?.trim()
    .toLowerCase() ?? null;

describe('Work list reads, the loader against a pulse (#2933)', () => {
  it('keeps a pulse’s newer list when the loader’s older one lands after it', async () => {
    // The loader's list answer is drawn only once `/api/dashboard` answers too,
    // but frees the one-read slot as soon as the list itself answers: a pulse
    // can read and draw a newer list while the loader's older one waits.
    const ITEM = '2055';
    const older = structuredClone(workJson) as { views: Array<Record<string, unknown>> };
    const newer = structuredClone(workJson) as { views: Array<Record<string, unknown>> };
    newer.views = newer.views.map((view) =>
      (view.identity as { item_id: string }).item_id === ITEM
        ? { ...view, exception: { state: 'none', detail: '', blocking: false, dimension: '' } }
        : view
    );
    const server = slowDashboardServer(older, newer);
    vi.stubGlobal('fetch', server.fetch);
    const target = document.body.appendChild(document.createElement('div'));
    component = mount(WorkPage, { target, props: { data: load({ fetch: server.fetch }) } });
    flushSync();
    await drain();
    // The first tick is the loader's own reading; the second re-reads.
    pulse();
    pulse();
    await drain();
    await drain();
    flushSync();
    expect(server.lists()).toBe(2);
    expect(stateWord(ITEM)).toBe('done');

    server.releaseDashboard();
    await drain();
    await drain();
    flushSync();
    expect(stateWord(ITEM)).toBe('done');
  });
});
