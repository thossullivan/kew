/**
 * #2975 — a needs-you block's Copy, in a DOM.
 *
 * Copy hands over the same command Run sends, so it asks Run's gate: while the
 * pair's ledger lookup is out, or a POST of it is open, a pasted copy could run
 * beside a request already made, and Copy takes no press.
 */
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { NeedsYouBlock as Block } from '$lib/needs-you/block';

const { default: NeedsYouBlock } = await import('./NeedsYouBlock.svelte');

let mounted: Array<Record<string, unknown>> = [];

afterEach(() => {
  for (const app of mounted) unmount(app);
  mounted = [];
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

const block = (command: string): Block => ({
  label: 'Merge decision',
  summary: 'the review round cap was reached',
  why: '',
  since: '',
  command,
  note: '',
  links: [],
  external: []
});

const request = (command: string, state: string) => ({
  id: `r-${command.split(' ').pop()}`,
  command,
  operator: 'tom',
  requested_at: '2026-10-11T00:00:00+00:00',
  source: 'web',
  state,
  item_id: null,
  drive: null,
  host: null,
  pid: null,
  log: null,
  started_at: null,
  finished_at: null,
  exit_code: state === 'done' ? 0 : null,
  detail: null,
  model: null
});

function render(command: string, oncopy: () => void): HTMLElement {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(NeedsYouBlock, { target, props: { block: block(command), oncopy } }));
  flushSync();
  return target;
}

const copy = (target: HTMLElement) =>
  target.querySelector('button[aria-label="Copy command (c)"]') as HTMLButtonElement;
const run = (target: HTMLElement) =>
  target.querySelector('button[data-run-button]') as HTMLButtonElement;

describe("a needs-you block's Copy (#2975)", () => {
  it('takes no press while the ledger lookup is out, and one once it lands empty', async () => {
    const lookups: Array<(r: Response) => void> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url.startsWith('/api/run-requests?'))
          return new Promise<Response>((resolve) => lookups.push(resolve));
        throw new Error(`unexpected ${url}`);
      })
    );
    const copied: string[] = [];
    const target = render('kew dispatch 3101', () => copied.push('copy'));

    await vi.waitFor(() => expect(lookups).toHaveLength(1));
    expect(copy(target).disabled).toBe(true);
    copy(target).dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(copied).toEqual([]);

    lookups[0](Response.json({ requests: [] }));
    await vi.waitFor(() => expect(copy(target).disabled).toBe(false));
    copy(target).click();
    expect(copied).toEqual(['copy']);
  });

  it('takes no press while a Run of it is sending, nor while that request is queued', async () => {
    const command = 'kew dispatch 3102';
    let answer: (r: Response) => void = () => {};
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === 'POST') return new Promise<Response>((resolve) => (answer = resolve));
        if (url.startsWith('/api/run-requests?')) return Response.json({ requests: [] });
        return Response.json(request(command, 'done'));
      })
    );
    const copied: string[] = [];
    const target = render(command, () => copied.push('copy'));
    await vi.waitFor(() => expect(run(target).disabled).toBe(false));

    run(target).click();
    flushSync();
    expect(copy(target).disabled).toBe(true);
    copy(target).dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(copied).toEqual([]);

    // Queued is still unsettled: Copy stays shut until the drive settles it.
    answer(Response.json({ request: request(command, 'queued'), created: true }));
    await vi.waitFor(() => expect(run(target).textContent).toContain('Queued'));
    expect(copy(target).disabled).toBe(true);

    await vi.waitFor(() => expect(copy(target).disabled).toBe(false), { timeout: 5000 });
    copy(target).click();
    expect(copied).toEqual(['copy']);
  });

  it('copies a command the drive cannot run without waiting on any ledger', () => {
    const fetch = vi.fn();
    vi.stubGlobal('fetch', fetch);
    const copied: string[] = [];
    const target = render('kew epic run epic:a', () => copied.push('copy'));

    expect(copy(target).disabled).toBe(false);
    copy(target).click();
    expect(copied).toEqual(['copy']);
    expect(fetch).not.toHaveBeenCalled();
  });
});
