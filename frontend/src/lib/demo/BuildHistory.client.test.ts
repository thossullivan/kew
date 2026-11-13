/**
 * The demo's page foot draws its loading and error lines only on a surface a
 * feature names (#3144): `/health` names none, so it shows nothing at all,
 * while the build history loads and after a failed load alike. The routes are
 * the ones the demo build serves beside the file (`demo/provenance-bundle.ts`),
 * known before the file arrives.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import sample from '../../../demo/provenance/provenance.sample.json';

const route = { id: '/(factory)/health' as string | null };

vi.mock('$app/state', () => ({
  page: {
    get route() {
      return route;
    }
  }
}));
/** Which file the bundle shipped (`demo/provenance-bundle.ts` decides); a test sets it. */
const shipped = vi.hoisted(() => ({ source: 'sample' as 'sample' | 'real' }));
vi.mock('./provenance-meta', () => ({
  PROVENANCE_ROUTES: ['/epics/[ref]', '/work'],
  get PROVENANCE_SOURCE() {
    return shipped.source;
  }
}));

let target: HTMLElement;
/** Unmounts the foot mounted last, with the runtime that mounted it. */
let teardown: (() => void) | null = null;

beforeEach(() => {
  target = document.createElement('div');
  document.body.append(target);
});
afterEach(() => {
  shipped.source = 'sample';
  teardown?.();
  teardown = null;
  target.remove();
  vi.unstubAllGlobals();
});

/**
 * The foot on `routeId`, its one read answered by `answer`. Each foot is a
 * fresh module graph (the file is read once per visit, at module scope), so
 * Svelte's runtime is imported beside it, never from before the reset.
 */
async function foot(routeId: string, answer: () => Promise<Response>) {
  teardown?.();
  vi.resetModules();
  route.id = routeId;
  vi.stubGlobal('fetch', vi.fn(answer));
  const { flushSync, mount, unmount } = await import('svelte');
  const { default: BuildHistory } = await import('./BuildHistory.svelte');
  const component = mount(BuildHistory, { target });
  teardown = () => unmount(component);
  flushSync();
  // Let the read settle: a refused fetch, or an answer's body read, is done
  // within a turn of the event loop.
  await new Promise((resolve) => setTimeout(resolve, 20));
  flushSync();
  return state();
}

const state = () => target.querySelector('[data-build-history]')?.getAttribute('data-build-history') ?? null;

const pending = () => new Promise<Response>(() => {});
const refused = () => Promise.reject(new Error('offline'));
const served = () => Promise.resolve(new Response(JSON.stringify(sample), { status: 200 }));

describe('the page foot', () => {
  it('draws nothing on a surface no feature names, while the file loads or after it failed', async () => {
    expect(await foot('/(factory)/health', pending)).toBeNull();
    expect(await foot('/(factory)/health', refused)).toBeNull();
    expect(target.textContent?.trim()).toBe('');
  });

  it('says it is reading, or that the read failed, on a surface a feature names', async () => {
    expect(await foot('/(factory)/epics/[ref]', pending)).toBe('loading');
    expect(target.textContent).toContain('Reading how this page was built.');
    expect(await foot('/(factory)/epics/[ref]', refused)).toBe('error');
    expect(target.textContent).toContain('The build history did not load');
  });

  it('draws the ledger once the file arrives', async () => {
    await foot('/(factory)/epics/[ref]', served);
    await vi.waitFor(() => expect(state()).toBe('ready'), { timeout: 2_000 });
  });
});

describe('the build-history disclosure', () => {
  it('is a button that names what opens and the region it controls, shut until pressed', async () => {
    await foot('/(factory)/epics/[ref]', served);
    await vi.waitFor(() => expect(state()).toBe('ready'), { timeout: 2_000 });
    expect(target.querySelector('details')).toBeNull();
    const button = target.querySelector<HTMLButtonElement>('button[data-build-toggle]')!;
    expect(button.type).toBe('button');
    expect(button.textContent?.trim()).toBe('▸ Build history');
    const id = button.getAttribute('aria-controls')!;
    const region = target.querySelector<HTMLElement>(`#${id}`);
    expect(region).not.toBeNull();
    expect(region!.querySelector('[data-build-feature="epic-graph"]')).not.toBeNull();

    expect(button.getAttribute('aria-expanded')).toBe('false');
    expect(region!.hidden).toBe(true);

    button.click();
    const { flushSync } = await import('svelte');
    flushSync();
    expect(button.getAttribute('aria-expanded')).toBe('true');
    expect(region!.hidden).toBe(false);
    expect(button.textContent?.trim()).toBe('▾ Build history');

    // Escape and a press outside leave it open: only its own button shuts it.
    region!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }));
    flushSync();
    expect(button.getAttribute('aria-expanded')).toBe('true');

    button.click();
    flushSync();
    expect(button.getAttribute('aria-expanded')).toBe('false');
    expect(region!.hidden).toBe(true);
  });
});

describe('the PRs row', () => {
  /** The epic graph's PRs cell, once the file served by `answer` has arrived. */
  async function prsCell(answer: () => Promise<Response>) {
    await foot('/(factory)/epics/[ref]', answer);
    await vi.waitFor(() => expect(state()).toBe('ready'), { timeout: 2_000 });
    const table = target.querySelector('[data-build-feature="epic-graph"]')!;
    const row = [...table.querySelectorAll('tr')].find((tr) => tr.querySelector('th')?.textContent?.trim() === 'PRs')!;
    return row.querySelector('td')!;
  }

  it('draws the sample PRs as plain numbers: the invented repository is someone else’s URL space', async () => {
    shipped.source = 'sample';
    const cell = await prsCell(served);
    expect(cell.querySelectorAll('a')).toHaveLength(0);
    expect([...cell.querySelectorAll('[data-build-pr]')].map((n) => n.textContent?.trim())).toEqual(['#43', '#44', '#45']);
    expect(cell.textContent).not.toContain('↗');
  });

  it('draws the real PRs as plain numbers too: they are in kew’s private development repository', async () => {
    shipped.source = 'real';
    const real = structuredClone(sample);
    const graph = real.features.find((f) => f.id === 'epic-graph')!;
    graph.prs = graph.prs.map((pr) => ({ ...pr, url: pr.url.replace('/acme/kew/', '/thossullivan/kew/') }));
    const cell = await prsCell(() => Promise.resolve(new Response(JSON.stringify(real), { status: 200 })));
    expect(cell.querySelectorAll('a[data-build-pr]')).toHaveLength(0);
    expect(cell.querySelectorAll('a')).toHaveLength(0);
    expect([...cell.querySelectorAll('[data-build-pr]')].map((n) => n.textContent?.trim())).toEqual(['#43', '#44', '#45']);
    expect(cell.textContent).not.toContain('↗');
  });
});

describe('the build-history line says which data it reads', () => {
  /** The line under the toggle, once the epic graph's file has arrived. */
  async function line(source: 'sample' | 'real') {
    shipped.source = source;
    await foot('/(factory)/epics/[ref]', served);
    await vi.waitFor(() => expect(state()).toBe('ready'), { timeout: 2_000 });
    return target.querySelector('[data-tour="build-history"]')!;
  }

  it('ends a sample build line with "sample data", plain 13px text-3 text after the summary', async () => {
    const row = await line('sample');
    const label = row.querySelector('[data-build-sample]');
    expect(label?.textContent?.trim()).toBe('sample data');
    expect(label?.tagName).toBe('SPAN');
    expect(label?.className.split(/\s+/).sort()).toEqual(['text-13', 'text-text-3']);
    const summary = row.querySelector('[data-build-summary]')!;
    expect(summary.compareDocumentPosition(label!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('says nothing about its data on a build of the real export', async () => {
    const row = await line('real');
    expect(row.querySelector('[data-build-sample]')).toBeNull();
    expect(row.textContent).not.toContain('sample data');
  });
});
