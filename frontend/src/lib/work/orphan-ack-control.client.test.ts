/**
 * #3453 — Work's orphan-PR acknowledge, in a DOM.
 *
 * The press goes to `/api/verbs/orphans/{pr}/ack` (the network is the stubbed
 * boundary): a 2xx hands the PR to `onacked`, which is how the page takes the
 * row away, and a refusal stays under the button in the server's words. A
 * linked orphan draws no button of its own.
 */
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { OrphanAck } from './orphan-ack';

const { default: OrphanAckControl } = await import('./OrphanAckControl.svelte');

let mounted: Array<Record<string, unknown>> = [];

afterEach(() => {
  for (const app of mounted) unmount(app);
  mounted = [];
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

function render(ack: OrphanAck | null, onacked: (pr: number) => void = () => {}) {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(OrphanAckControl, {
      target,
      props: { ack, href: (id: string) => `/work?item=${id}`, onacked }
    })
  );
  flushSync();
  return target;
}

/** Every request the page sent, with the answer `reply` gives it. */
function network(reply: () => Response) {
  const sent: Array<{ url: string; method: string }> = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: RequestInit) => {
      sent.push({ url, method: String(init.method) });
      return reply();
    })
  );
  return sent;
}

const buttons = (el: HTMLElement) => [...el.querySelectorAll('button')].map((b) => b.textContent?.trim());
const press = (el: HTMLElement, label: string) =>
  [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === label)!.click();

/** Wait until the write has been answered and the button that sent it is free again. */
async function settle(): Promise<void> {
  await vi.waitFor(() => {
    flushSync();
    const pending = [...document.querySelectorAll('button')].some((b) => b.textContent?.trim().endsWith('…'));
    if (pending || vi.mocked(fetch).mock.results.length === 0) throw new Error('the write is still out');
  });
}

const UNLINKED: OrphanAck = { kind: 'ack', pr: 2608, url: 'https://github.com/o/r/pull/2608' };

describe('an unlinked orphan PR', () => {
  it('shows Acknowledge and a link to the PR', () => {
    const el = render(UNLINKED);
    expect(buttons(el)).toEqual(['Acknowledge']);
    const open = el.querySelector<HTMLAnchorElement>('a[href="https://github.com/o/r/pull/2608"]');
    expect(open?.textContent).toContain('Open PR #2608');
  });

  it('acknowledges through the ack verb and hands the PR over so its row leaves', async () => {
    const sent = network(
      () => new Response(JSON.stringify({ pr_number: 2608, acked: true, acked_at: null }), { status: 200 })
    );
    const acked: number[] = [];
    const el = render(UNLINKED, (pr) => acked.push(pr));
    press(el, 'Acknowledge');
    await settle();
    expect(sent).toEqual([{ url: '/api/verbs/orphans/2608/ack', method: 'POST' }]);
    expect(acked).toEqual([2608]);
    expect(el.querySelector('[data-orphan-ack-error]')).toBeNull();
  });

  it('shows the refusal in the server’s words and keeps the row', async () => {
    network(() => new Response(JSON.stringify({ detail: 'PR #2608 is not an orphan any more' }), { status: 404 }));
    const acked: number[] = [];
    const el = render(UNLINKED, (pr) => acked.push(pr));
    press(el, 'Acknowledge');
    await settle();
    expect(acked).toEqual([]);
    expect(el.querySelector('[data-orphan-ack-error]')?.textContent).toBe('PR #2608 is not an orphan any more');
    // Still there to press again.
    expect(buttons(el)).toEqual(['Acknowledge']);
  });
});

describe('a linked orphan PR', () => {
  it('draws no acknowledge of its own and points at the item that offers one', () => {
    const el = render({ kind: 'linked', pr: 2608, itemId: '2055', ref: '#2055' });
    expect(buttons(el)).toEqual([]);
    const link = el.querySelector<HTMLAnchorElement>('[data-orphan-ack-linked] a');
    expect(link?.getAttribute('href')).toBe('/work?item=2055');
    expect(link?.textContent).toBe('#2055');
  });
});

it('draws nothing for a row with no orphan', () => {
  const el = render(null);
  expect(el.innerHTML.replace(/<!--.*?-->/g, '').trim()).toBe('');
});
