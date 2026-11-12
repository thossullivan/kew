/**
 * #3056 — two node popovers in one document, each naming its own sentence.
 *
 * The sentence's id was a fixed `pop-why`, so a second popover's
 * `aria-describedby` and "more" control pointed at the first one's. Each
 * instance now draws its own.
 */
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import type { EpicNode } from '$lib/api';
import { nodePopover } from './node-popover';

const { default: NodePopover } = await import('./NodePopover.svelte');

const NOW_MS = Date.parse('2026-10-02T15:00:00+00:00');

let mounted: Array<Record<string, unknown>> = [];

afterEach(() => {
  for (const app of mounted) unmount(app);
  mounted = [];
  document.body.innerHTML = '';
});

const node = (id: string, reason: string): EpicNode => ({
  id,
  title: `node ${id}`,
  status: 'in_review',
  status_since: '2026-10-02T14:48:00+00:00',
  url: null,
  pr_url: null,
  external: false,
  stuck: null,
  attention: { kind: 'hold', reason, url: null, action: '', command: '' },
  blocked_reason: null
});

function open(id: string, reason: string): void {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(NodePopover, { target, props: { model: nodePopover(node(id, reason), null, NOW_MS) } }));
}

describe('two popovers in one document (#3056)', () => {
  it('each describes itself by its own sentence, and neither by `pop-why`', () => {
    open('2219', 'held: the first sentence');
    open('2220', 'held: the second sentence');
    flushSync();

    const dialogs = [...document.querySelectorAll<HTMLElement>('[role="dialog"]')];
    expect(dialogs).toHaveLength(2);
    const targets = dialogs.map((d) => d.getAttribute('aria-describedby') ?? '');
    expect(targets.every((t) => t !== '' && t !== 'pop-why')).toBe(true);
    expect(new Set(targets).size).toBe(2);
    expect(document.getElementById('pop-why')).toBeNull();
    // Each target is in its own popover and is that popover's sentence.
    dialogs.forEach((d, i) => {
      const described = document.getElementById(targets[i]);
      expect(d.contains(described)).toBe(true);
    });
    expect(document.getElementById(targets[0])?.textContent).toBe('held: the first sentence');
    expect(document.getElementById(targets[1])?.textContent).toBe('held: the second sentence');
  });
});
