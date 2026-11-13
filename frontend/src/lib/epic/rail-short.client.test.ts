/**
 * #2333 — the rail on a window too short for it.
 *
 * The rail is a fixed band under the page's own scroller, so its height comes
 * straight off the canvas: at 1440x600 it took the bottom ~220px and left a
 * slot of graph. Under 700px tall it keeps one line — which node is selected —
 * and a disclosure that opens the four sections again.
 *
 * jsdom, because the whole behaviour is `innerHeight`, a `resize` and a click:
 * none of them exists in an SSR render, which is what `rail.test.ts` does.
 * jsdom has no layout, so nothing here reads a size — only which parts of the
 * rail are in the document, and what the disclosure says about itself.
 */
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import type { EpicNode, EpicNodeDetail, ItemEvidence, ItemPipeline, WorkView } from '$lib/api';
import { SHORT_VIEWPORT, type RailInput } from './rail';
import SelectedRail from './SelectedRail.svelte';

const node = (over: Partial<EpicNode> = {}): EpicNode => ({
  id: '2055',
  title: 'review gate lows',
  status: 'in_review',
  status_since: '2026-09-24T15:00:00Z',
  url: 'https://github.com/o/r/issues/2055',
  pr_url: 'https://github.com/o/r/pull/2059',
  external: false,
  stuck: null,
  attention: { kind: 'merge_decision', reason: 'awaiting review', url: 'https://x/pull/9' },
  blocked_reason: null,
  ...over
});

const detail: EpicNodeDetail = { id: '2055', runs: [], lifetime_usd: 3.1, drain_usd: 0.4 };

const view = {
  route: { runner: 'claude', model: 'opus', containment: 'contained', provenance: 'declared' }
} as WorkView;

const pipeline: ItemPipeline = {
  item_id: '2055',
  lanes: [
    {
      name: 'main',
      stages: [
        { stage: 'implement', state: 'done', label: 'f5dcb6a5', detail: 'success', evidence: null }
      ]
    }
  ],
  unavailable: [],
  composed_at: '2026-09-24T15:47:00Z'
};

const evidence: ItemEvidence = {
  item_id: '2055',
  rows: [
    {
      n: 1,
      label: 'sandboxed',
      event: 'sandbox_resolved',
      summary: 'contained',
      ts: '2026-09-24T15:24:15Z',
      state: 'attested'
    }
  ],
  attested_count: 1,
  total: 1,
  chain_verified: true,
  reason: '',
  unavailable: [],
  composed_at: '2026-09-24T15:47:00Z'
};

const input: RailInput = {
  epicRef: 'factory-ui',
  node: node(),
  edges: [['2047', '2055']],
  unavailable: [],
  detail,
  view,
  pipeline,
  evidence
};

let open: ReturnType<typeof mount> | null = null;

/** A window of the given height, mounted and settled. */
function rail(height: number): HTMLElement {
  window.innerHeight = height;
  const target = document.createElement('div');
  document.body.append(target);
  open = mount(SelectedRail, { target, props: { input } });
  flushSync();
  return target;
}

const disclosure = (target: HTMLElement): HTMLButtonElement | null =>
  target.querySelector('button[aria-controls="selected-rail-detail"]');

const sections = (target: HTMLElement): HTMLElement | null =>
  target.querySelector('#selected-rail-detail');

afterEach(() => {
  if (open) unmount(open);
  open = null;
  document.body.innerHTML = '';
});

describe('the rail on a short window', () => {
  it('keeps the whole rail on a window tall enough for it', () => {
    const target = rail(SHORT_VIEWPORT);
    expect(disclosure(target)).toBeNull();
    expect(sections(target)).not.toBeNull();
  });

  it('collapses to the selected line and a disclosure under 700px', () => {
    const target = rail(600);
    const button = disclosure(target);
    expect(button).not.toBeNull();
    expect(button?.getAttribute('aria-expanded')).toBe('false');
    // The line still says WHICH node, so the collapse costs no identity.
    expect(target.textContent).toContain('#2055');
    expect(target.textContent).toContain('review gate lows');
    // ...and the sections the canvas wanted the height back from are gone.
    expect(sections(target)).toBeNull();
    expect(target.textContent).not.toContain('Evidence');
  });

  it('opens all four sections on the disclosure, and closes them again', () => {
    const target = rail(600);
    const button = disclosure(target) as HTMLButtonElement;
    button.click();
    flushSync();
    expect(button.getAttribute('aria-expanded')).toBe('true');
    expect(sections(target)).not.toBeNull();
    for (const heading of ['Selected', 'Pipeline', 'Evidence', 'Needs you']) {
      expect(target.textContent).toContain(heading);
    }
    // The control that opened them stays above them, reachable without a
    // scroll through the thing it would close.
    expect(button.compareDocumentPosition(sections(target) as Node)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING
    );
    button.click();
    flushSync();
    expect(button.getAttribute('aria-expanded')).toBe('false');
    expect(sections(target)).toBeNull();
  });

  it('follows the window when it is resized past the threshold', () => {
    const target = rail(600);
    expect(sections(target)).toBeNull();
    window.innerHeight = 960;
    window.dispatchEvent(new Event('resize'));
    flushSync();
    expect(disclosure(target)).toBeNull();
    expect(sections(target)).not.toBeNull();
  });
});
