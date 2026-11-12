/**
 * #3056 — a needs-you block's "Copied" flash, in a DOM.
 *
 * "Copy ids" says "Copied" for a moment and a timer puts the word back. A
 * block taken down inside that moment must take its timer with it, not leave
 * one set to write to a component that is gone.
 */
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { NeedsYouBlock as Block } from '$lib/needs-you/block';

const { default: NeedsYouBlock } = await import('./NeedsYouBlock.svelte');

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

const block: Block = {
  label: 'Declaration hold',
  summary: 'the PR body declares no line for 2 changed tests',
  why: '',
  since: '',
  command: '',
  note: '',
  links: [],
  external: [],
  nodeIds: ['tests/test_a.py::test_one', 'tests/test_b.py::test_two']
};

describe('a needs-you block taken down mid-flash (#3056)', () => {
  it('leaves no flash timer set', async () => {
    vi.useFakeTimers();
    const written: string[] = [];
    vi.stubGlobal('navigator', {
      clipboard: { writeText: async (text: string) => void written.push(text) }
    });
    const target = document.createElement('div');
    document.body.append(target);
    const app = mount(NeedsYouBlock, { target, props: { block } });
    flushSync();
    const before = vi.getTimerCount();

    const copy = target.querySelector('button[aria-label="Copy 2 node ids"]') as HTMLButtonElement;
    copy.click();
    // Not vacuous: the press set the flash and its timer.
    await vi.waitFor(() => {
      flushSync();
      expect(copy.textContent).toBe('Copied');
    });
    expect(written).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(before + 1);

    unmount(app);
    expect(vi.getTimerCount()).toBe(0);
  });
});
