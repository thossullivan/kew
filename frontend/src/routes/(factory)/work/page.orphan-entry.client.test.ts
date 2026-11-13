/**
 * #3453 — Work with an orphan PR entry selected: the row names no work item,
 * so the pane takes no per-item read for it, and acknowledging it takes the row
 * out of the list and the selection off it.
 *
 * The page is mounted in jsdom with `fetch` as the boundary, and SvelteKit's
 * shallow routing stood in for by a `page.state` that `replaceState` writes, as
 * the real one does — so a selection the page writes is the selection it reads.
 */
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Dashboard, DashboardRow } from '$lib/api';
import dashboardJson from '../../../../fixtures/factory/dashboard.json';
import workJson from '../../../../fixtures/factory/dashboard-work.json';

const nav = vi.hoisted(() => ({
  /** The query string the page is opened at. */
  opened: '',
  /** Every URL the page wrote through `replaceState`, in order. */
  writes: [] as string[],
  reset: () => {}
}));

vi.mock('$app/environment', () => ({ browser: true }));
vi.mock('$app/state', async () => {
  const { SvelteMap } = await import('svelte/reactivity');
  const cell = new SvelteMap<'state', App.PageState>();
  nav.reset = () => cell.clear();
  return {
    page: {
      get url() {
        return new URL(`http://localhost/work${nav.opened}`);
      },
      get state() {
        return cell.get('state') ?? {};
      },
      set state(next: App.PageState) {
        cell.set('state', next);
      }
    }
  };
});
vi.mock('$app/navigation', async () => {
  const { page } = await import('$app/state');
  return {
    replaceState: (url: URL | string, state: App.PageState) => {
      nav.writes.push(String(url));
      page.state = state;
    }
  };
});

const { factoryPulse } = await import('$lib/shell/pulse.svelte');
const { load } = await import('./+page');
const { default: WorkPage } = await import('./+page.svelte');

const PR = 2608;
const ORPHAN_KEY = `orphan_pr:${PR}`;
const base = dashboardJson as unknown as Dashboard;

/** The committed set plus one unlinked orphan PR, the shape the server serves. */
const withOrphan: Dashboard = {
  ...base,
  rows: [
    ...base.rows,
    {
      ...base.rows[0],
      type: 'orphan_pr',
      consequence: 'hygiene',
      reason: 'orphan PR — no-record',
      sentence: '',
      primary_action: 'ack',
      issue_num: null,
      item_id: '',
      epic_ref: null,
      pr_number: PR,
      url: `https://github.com/o/r/pull/${PR}`,
      attention_kind: '',
      label: '',
      command: '',
      title: ''
    } as DashboardRow
  ],
  needs_you_no_epic: 1
};

/**
 * A server that answers the two list reads and the orphan acknowledge, and
 * records the path of every request it is sent. A per-item read answers 404,
 * which is what kew serve says for an id it does not know.
 */
function server() {
  const paths: string[] = [];
  const fetch = vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input), 'http://localhost').pathname;
    paths.push(path);
    if (path === '/api/dashboard/work') return new Response(JSON.stringify(workJson));
    if (path === '/api/dashboard') return new Response(JSON.stringify(withOrphan));
    if (path === `/api/verbs/orphans/${PR}/ack`) {
      return new Response(JSON.stringify({ pr_number: PR, acked: true, acked_at: null }));
    }
    if (path.startsWith('/api/work/')) {
      return new Response(JSON.stringify({ detail: 'not found' }), { status: 404 });
    }
    return new Promise<Response>(() => {});
  }) as unknown as typeof globalThis.fetch;
  return { fetch, itemReads: () => paths.filter((p) => p.startsWith('/api/work/')) };
}

/** Let settled promises run their continuations, and the page draw them. */
async function drain(): Promise<void> {
  for (let i = 0; i < 4; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
  flushSync();
}

/** One shell pulse: the page re-reads the selected item's readings on it. */
async function pulse(): Promise<void> {
  factoryPulse.dashboard = structuredClone(withOrphan);
  flushSync();
  await drain();
}

let component: Record<string, unknown> | null = null;

/** Open Work at `search`, against `fetch`, and let both list reads land. */
async function open(search: string, fetch: typeof globalThis.fetch): Promise<void> {
  nav.opened = search;
  vi.stubGlobal('fetch', fetch);
  const target = document.body.appendChild(document.createElement('div'));
  component = mount(WorkPage, { target, props: { data: load({ fetch }) } });
  flushSync();
  await drain();
}

const row = (item: string) =>
  document.querySelector(`aside[aria-label="Issues"] a[data-item="${CSS.escape(item)}"]`);

afterEach(() => {
  if (component) unmount(component);
  component = null;
  factoryPulse.dashboard = null;
  nav.opened = '';
  nav.writes = [];
  nav.reset();
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

describe('Work with an entry row that names no item selected', () => {
  it('sends no per-item read for it, on selection or on the tick', async () => {
    const api = server();
    await open(`?item=${encodeURIComponent(ORPHAN_KEY)}`, api.fetch);
    // Selected: its acknowledge is on screen.
    expect(document.querySelector(`[data-orphan-ack="${PR}"]`)).not.toBeNull();
    await pulse();
    await pulse();
    expect(api.itemReads()).toEqual([]);
    // Nor does it draw panels for readings it will never have.
    expect(document.querySelector('section[aria-label="Evidence ledger"]')).toBeNull();
  });

  it('reads an item row’s pipeline, evidence and description as before', async () => {
    // The same server and the same clock: an item row's readings still go out,
    // so the empty list above is the page declining, not the stub missing them.
    const api = server();
    await open('?item=2055', api.fetch);
    expect(new Set(api.itemReads())).toEqual(
      new Set(['/api/work/2055/pipeline', '/api/work/2055/evidence', '/api/work/2055/description'])
    );
    expect(document.querySelector('section[aria-label="Evidence ledger"]')).not.toBeNull();
  });
});

describe('acknowledging the selected orphan on Work', () => {
  it('takes its row out of the list and the selection off it', async () => {
    const api = server();
    await open(`?item=${encodeURIComponent(ORPHAN_KEY)}`, api.fetch);
    expect(row(ORPHAN_KEY)).not.toBeNull();
    const ack = [...document.querySelectorAll(`[data-orphan-ack="${PR}"] button`)].find(
      (b) => b.textContent?.trim() === 'Acknowledge'
    ) as HTMLButtonElement | undefined;
    expect(ack).toBeDefined();
    ack!.click();
    await drain();

    // The row left on the write's answer, with the dashboard read unchanged.
    expect(row(ORPHAN_KEY)).toBeNull();
    // The address the reader is left at — the page's last write, else the one
    // it was opened at — names no item, and the pane is back on the queue.
    const at = new URL(nav.writes.at(-1) ?? `/work${nav.opened}`, 'http://localhost');
    expect(at.searchParams.has('item')).toBe(false);
    expect(document.querySelector('section[aria-label="Needs you"]')).not.toBeNull();
    expect(document.querySelector(`[data-orphan-ack="${PR}"]`)).toBeNull();
  });
});
