/**
 * #3453 — Work's "Not started" picker, in a DOM: it lists the open issues kew
 * has not touched (`glance.queue_candidates`), queues one only after an
 * explicit confirm, and shows what the queue write answered. The network is
 * the stubbed boundary.
 */
import { flushSync, mount, unmount } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { QueueCandidate } from '$lib/api';

const { default: QueuePicker } = await import('./QueuePicker.svelte');

let mounted: Array<Record<string, unknown>> = [];

afterEach(() => {
  for (const app of mounted) unmount(app);
  mounted = [];
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

const CANDIDATES: QueueCandidate[] = [
  { issue_num: 3101, item_id: '3101', title: 'Retry budget reads after a 502' },
  { issue_num: 3102, item_id: '3102', title: 'Name the stale lock holder' }
];

function render(candidates: QueueCandidate[]) {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(QueuePicker, { target, props: { candidates } }));
  flushSync();
  return target;
}

function network(reply: () => Response) {
  const sent: Array<{ url: string; method: string; body: unknown }> = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: RequestInit) => {
      sent.push({ url, method: String(init.method), body: JSON.parse(String(init.body)) });
      return reply();
    })
  );
  return sent;
}

const button = (el: HTMLElement, label: string) =>
  [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === label) ?? null;

function pick(el: HTMLElement, issue: number): void {
  const select = el.querySelector('select')!;
  const option = [...select.options].find((o) => o.textContent?.startsWith(`#${issue}`))!;
  select.value = option.value;
  select.selectedIndex = option.index;
  select.dispatchEvent(new Event('change'));
  flushSync();
}

/** Wait until the write has been answered and the button that sent it is free again. */
async function settle(): Promise<void> {
  await vi.waitFor(() => {
    flushSync();
    const pending = [...document.querySelectorAll('button')].some((b) => b.textContent?.trim().endsWith('…'));
    if (pending || vi.mocked(fetch).mock.results.length === 0) throw new Error('the write is still out');
  });
}

describe('the Not started picker', () => {
  it('lists every queue candidate by ref and title', () => {
    const el = render(CANDIDATES);
    const options = [...el.querySelectorAll('option')].map((o) => o.textContent?.trim());
    expect(options).toEqual([
      'Pick an issue…',
      '#3101 — Retry budget reads after a 502',
      '#3102 — Name the stale lock holder'
    ]);
    expect(button(el, 'Queue')?.disabled).toBe(true);
  });

  it('draws nothing at all with no candidate', () => {
    const el = render([]);
    expect(el.querySelector('[data-queue-picker]')).toBeNull();
    expect(el.querySelector('select, button')).toBeNull();
  });

  it('asks before it queues, and Cancel sends nothing', () => {
    const sent = network(() => new Response('{}'));
    const el = render(CANDIDATES);
    pick(el, 3102);
    button(el, 'Queue')!.click();
    flushSync();
    expect(el.querySelector('[data-queue-confirm]')?.textContent).toContain('Name the stale lock holder');
    expect(sent).toEqual([]);
    button(el, 'Cancel')!.click();
    flushSync();
    expect(el.querySelector('[data-queue-confirm]')).toBeNull();
    expect(sent).toEqual([]);
  });

  it('confirming queues through the queue verb and shows the result', async () => {
    const sent = network(
      () => new Response(JSON.stringify({ issue: 3102, ready: true, driver_stale: false }), { status: 200 })
    );
    const el = render(CANDIDATES);
    pick(el, 3102);
    button(el, 'Queue')!.click();
    flushSync();
    button(el, 'Confirm queue')!.click();
    await settle();
    expect(sent).toEqual([{ url: '/api/verbs/queue/3102', method: 'POST', body: { confirm: true } }]);
    expect(el.querySelector('[data-queue-result="3102"]')?.textContent).toContain('queued');
    // Queued, it is no longer offered; the other candidate still is.
    const options = [...el.querySelectorAll('option')].map((o) => o.textContent?.trim());
    expect(options).toEqual(['Pick an issue…', '#3101 — Retry budget reads after a 502']);
  });

  it('says when no loop is running to pick the queued issue up', async () => {
    network(() => new Response(JSON.stringify({ issue: 3101, ready: true, driver_stale: true }), { status: 200 }));
    const el = render(CANDIDATES);
    pick(el, 3101);
    button(el, 'Queue')!.click();
    flushSync();
    button(el, 'Confirm queue')!.click();
    await settle();
    expect(el.querySelector('[data-queue-result="3101"]')?.textContent).toContain('kew run');
  });

  it('drops the choice and its confirm when a poll stops offering the chosen issue', () => {
    // The candidates are the page's live reading: a reactive cell standing in
    // for the pulse, so the picker sees each poll as the page hands it over.
    const live = new SvelteMap([['candidates', CANDIDATES]]);
    const target = document.createElement('div');
    document.body.append(target);
    mounted.push(
      mount(QueuePicker, {
        target,
        props: {
          get candidates() {
            return live.get('candidates') ?? [];
          }
        }
      })
    );
    flushSync();
    pick(target, 3102);
    button(target, 'Queue')!.click();
    flushSync();
    expect(target.querySelector('[data-queue-confirm]')).not.toBeNull();

    // A poll without #3102: no confirm for an issue no longer on offer, and no
    // Queue to press over a choice that is gone.
    live.set('candidates', [CANDIDATES[0]]);
    flushSync();
    expect(target.querySelector('[data-queue-confirm]')).toBeNull();
    expect(target.querySelector('select')?.value).toBe('');
    expect(button(target, 'Queue')?.disabled).toBe(true);

    // #3102 offered again: the picker asks for a choice, it does not reopen
    // the confirm nobody asked for.
    live.set('candidates', CANDIDATES);
    flushSync();
    expect(target.querySelector('[data-queue-confirm]')).toBeNull();
    expect(target.querySelector('select')?.value).toBe('');
    expect(button(target, 'Queue')?.disabled).toBe(true);
  });

  it('shows a refusal under the picker and keeps the issue on offer', async () => {
    network(() => new Response(JSON.stringify({ detail: 'nope' }), { status: 403 }));
    const el = render(CANDIDATES);
    pick(el, 3101);
    button(el, 'Queue')!.click();
    flushSync();
    button(el, 'Confirm queue')!.click();
    await settle();
    expect(el.querySelector('[data-queue-error]')?.textContent).toContain("You're not an approver");
    expect(el.querySelector('[data-queue-result]')).toBeNull();
    expect(button(el, 'Confirm queue')).not.toBeNull();
  });
});
