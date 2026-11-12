/**
 * #2248 — where a row's expand points.
 *
 * The expand under an event carries the two places an operator goes next: the
 * item's own page and the run that did it. Both ids come off the wire — an
 * `item_id` may be a tracker key and a `run_id` is whatever the host minted —
 * so both are OPAQUE: encoded into the link, never parsed, which is the rule
 * every other `?item=` and `/runs/` link on this console already follows
 * (`live-runs.workHref`, `flight-recorder.runHref`, both reused here rather
 * than rebuilt). Interpolated raw, an id holding a `+`, an `&` or a `#`
 * selected a different item, or a truncated one.
 *
 * jsdom, because the links only exist while a row is expanded and a row is
 * expanded by a click.
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

const NOW = Date.parse('2026-10-02T10:04:00.000Z');

/** An id a URL cannot carry as it stands: a plus, an ampersand and a hash. */
const ITEM = 'KEW-7&a+b#c';
const RUN = 'loop/step 1';

function page(over: Partial<AuditEvent>): AuditList {
  return {
    events: [
      {
        id: 7,
        timestamp: new Date(NOW - 60_000).toISOString(),
        event: 'approval_hold',
        issue_num: null,
        item_id: null,
        run_id: null,
        actor_type: 'host',
        actor_id: 'kew',
        // Two facts, one line: the row keeps an expand, which is what holds the
        // links.
        summary: '{"reason":"budget_exceeded","scope":"weekly"}',
        epic_ref: null,
        ...over
      }
    ],
    next_after: null
  };
}

let target: HTMLElement;
let component: Record<string, unknown> | null = null;

/** The stream, rendered on a page the caller supplies, with the row expanded. */
function expand(source: AuditList): HTMLAnchorElement[] {
  target = document.createElement('div');
  document.body.appendChild(target);
  component = mount(EventStream, {
    target,
    props: { source, nowMs: NOW }
  }) as Record<string, unknown>;
  flushSync();
  const row = target.querySelector('[aria-expanded]') as HTMLButtonElement;
  expect(row, 'the row that opens the expand').not.toBeNull();
  row.click();
  flushSync();
  return [...target.querySelectorAll('a')] as HTMLAnchorElement[];
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

describe("the expand's in-app links", () => {
  it('encodes the item id rather than interpolating it', () => {
    const links = expand(page({ item_id: ITEM }));
    const work = links.find((el) => el.textContent?.includes('Open in Work'));
    expect(work).toBeDefined();
    expect(work!.getAttribute('href')).toBe(`/work?item=${encodeURIComponent(ITEM)}`);
    // Raw, the `&` started a second param and the `#` threw the rest away.
    expect(work!.getAttribute('href')).not.toContain('&a+b#c');
  });

  it('encodes the run id rather than interpolating it', () => {
    const links = expand(page({ run_id: RUN }));
    const run = links.find((el) => el.textContent?.includes('Open run'));
    expect(run).toBeDefined();
    expect(run!.getAttribute('href')).toBe(`/runs/${encodeURIComponent(RUN)}`);
    expect(run!.getAttribute('href')).not.toContain('loop/step 1');
  });

  it('offers neither where the event names neither', () => {
    const links = expand(page({}));
    expect(links.map((el) => el.textContent)).not.toContain('Open in Work');
    expect(links.map((el) => el.textContent)).not.toContain('Open run');
  });
});
