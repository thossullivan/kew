/**
 * #2814 — the merge queue's attention is a request for a person.
 *
 * `merge_queue_attention` is raised when main is red after a train stopped
 * part-way, or a merge landed a tree no verify read; the queue merges nothing
 * until someone looks at main. Drawn as neutral history it would scroll past
 * with the queue's routine verdicts, so the row carries the magenta needs-you
 * mark (DESIGN.md: magenta means a person is the next move) and its lead says
 * what to do. The routine verdict beside it stays neutral.
 *
 * jsdom, because the mark is the rendered dot's colour.
 */
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuditEvent, AuditList } from '$lib/api';

const fetchAudit = vi.fn();
const fetchAuditHistogram = vi.fn();
const fetchEpics = vi.fn();

vi.mock('$app/environment', () => ({ browser: true }));
vi.mock('$lib/api', () => ({ fetchAudit, fetchAuditHistogram, fetchEpics }));

const { default: EventStream } = await import('./EventStream.svelte');

const NOW = Date.parse('2026-10-14T10:04:00.000Z');

function event(id: number, name: string, summary: string): AuditEvent {
  return {
    id,
    timestamp: new Date(NOW - id * 60_000).toISOString(),
    event: name,
    issue_num: null,
    item_id: null,
    run_id: null,
    actor_type: 'policy',
    actor_id: 'merge-queue',
    summary,
    epic_ref: null
  };
}

const SOURCE: AuditList = {
  events: [
    event(
      1,
      'merge_queue_attention',
      '{"pr":null,"sha":"' + 'a'.repeat(40) + '","message":"main is red after a train stopped"}'
    ),
    event(2, 'merge_queue_settled', '{"pr":11,"state":"merged","reason":"kew verify passed"}')
  ],
  next_after: null
};

let target: HTMLElement;
let component: Record<string, unknown> | null = null;

/** Each rendered row's words, and the colour of the dot on its rail. */
function rows(): { words: string; mark: string }[] {
  target = document.createElement('div');
  document.body.appendChild(target);
  component = mount(EventStream, {
    target,
    props: { source: SOURCE, nowMs: NOW }
  }) as Record<string, unknown>;
  flushSync();
  return [...target.querySelectorAll<HTMLButtonElement>('button[aria-expanded]')].map((row) => {
    const dot = row.parentElement!.querySelector<HTMLElement>('span.rounded-full');
    return {
      words: row.textContent ?? '',
      mark: dot?.style.getPropertyValue('background-color') ?? ''
    };
  });
}

beforeEach(() => {
  fetchAudit.mockReset();
  fetchAuditHistogram.mockReset();
  fetchEpics.mockReset().mockResolvedValue([]);
});

afterEach(() => {
  if (component) unmount(component);
  component = null;
  target?.remove();
});

describe("the merge queue's attention in the event stream", () => {
  it('carries the needs-you mark and says what to do', () => {
    const attention = rows().find((row) => row.words.includes('Merge queue stopped'));
    expect(attention, 'the attention row').toBeDefined();
    expect(attention!.words).toContain('Merge queue stopped: check main');
    expect(attention!.mark).toBe('var(--accent)');
  });

  it("leaves the queue's routine verdict neutral", () => {
    const verdict = rows().find((row) => row.words.includes('Merge queue verdict'));
    expect(verdict, 'the verdict row').toBeDefined();
    expect(verdict!.mark).toBe('var(--text-2)');
  });
});
