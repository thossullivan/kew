/**
 * #2300 — the run page's split, driven through the component.
 *
 * Three behaviours here were invisible to every pure reading and to every
 * payload test, and each has a history on another surface:
 *
 *   * **the filter is URL state.** `replaceState` is shallow routing: SvelteKit
 *     moves the address bar and `page.state` and leaves `page.url` at the last
 *     real navigation. A page that reads `page.url` writes the query and snaps
 *     straight back — the trap #2248 (Monitor), #2250 (Work), #2271 (Epics) and
 *     #2285 (Console) each found separately. The mocks below do exactly what
 *     SvelteKit does, so a filter that cannot survive a reload fails here.
 *   * **hovering a row marks its time on the timeline**, debounced 100 ms.
 *   * **the live tail follows only while the reader is at the bottom.**
 */
import { flushSync, mount, unmount } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Run, RunAuditEvent } from '$lib/api';
import fixtureAudit from '../../../fixtures/factory/runs-f5dcb6a5-audit.json';

/** `page.url` and `page.state`, reactive, as `$app/state` serves them. */
const kit = new SvelteMap<string, unknown>();

const replaceState = vi.fn((url: URL | string, state: App.PageState) => {
  // SvelteKit's `replaceState`: history and `page.state` move; `page.url` stays
  // at the last real navigation.
  void url;
  kit.set('state', state);
});

vi.mock('$app/environment', () => ({ browser: true }));
vi.mock('$app/state', () => ({
  page: {
    get url() {
      return kit.get('url') as URL;
    },
    get state() {
      return kit.get('state') as App.PageState;
    }
  }
}));
vi.mock('$app/navigation', () => ({ replaceState }));

const fetchRunAudit = vi.fn();
/** The run's own row, which the page re-reads so its tail can stop (#2300). */
const fetchRun = vi.fn();
vi.mock('$lib/api', async (original) => ({
  ...(await original<Record<string, unknown>>()),
  fetchRun,
  fetchRunAudit
}));

const { default: RunEvents } = await import('./RunEvents.svelte');
/** Read through the mocked module, after the doubles above are initialised:
 * a value imported at the top would run the factory before they exist. */
const { RUN_AUDIT_PAGE } = await import('$lib/api');

const T0 = Date.parse('2026-10-05T12:00:00Z');

function ev(seq: number, event: string, offsetS: number): RunAuditEvent {
  return {
    seq,
    ts: new Date(T0 + offsetS * 1000).toISOString(),
    event,
    actor_type: 'policy',
    item_id: '2300',
    payload: { step: seq }
  };
}

const EVENTS = [
  ev(1, 'run_started', 0),
  ev(2, 'routing_decision', 30),
  ev(3, 'artifact_sealed', 90),
  ev(4, 'run_finished', 120)
];

const RUN: Run = {
  run_id: 'r1',
  issue_num: 2300,
  title: 'the run page',
  runner: 'claude-code',
  model: 'claude-opus-5',
  status: 'success',
  cost_usd: 1.25,
  channel: 'hosted',
  started_at: new Date(T0).toISOString(),
  finished_at: new Date(T0 + 120_000).toISOString(),
  duration_seconds: 120,
  input_tokens: 0,
  output_tokens: 0,
  cache_read_tokens: 0,
  cache_creation_tokens: 0,
  diff_additions: null,
  diff_deletions: null,
  diff_files: null,
  review_cycles: null,
  pr_url: null,
  pr_state: null,
  attempt_number: 1,
  error_category: '',
  complexity: 'medium',
  branch: 'agent/issue-2300',
  stage: 'implement',
  stage_detail: '',
  posture: 'sandboxed',
  filesystem_boundary: 'container',
  network_boundary: 'proxied',
  egress_verification_basis: null,
  credential_names: [],
  command_guard: 'allowlist',
  admission: 'admitted',
  reason: null,
  run_kind: 'dispatch',
  item_id: '2300',
  reported_model: null,
  provider: null,
  transport_mode: 'direct',
  planned_cost_usd: null,
  accounting_status: 'settled',
  verdict: null
};

let host: HTMLDivElement;
let app: Record<string, unknown> | null = null;

function render(search = '', over: Partial<Run> = {}) {
  kit.set('url', new URL(`http://127.0.0.1:8765/runs/r1${search}`));
  kit.set('state', {});
  host = document.createElement('div');
  // The list measures itself; jsdom reports 0 for every box, so the geometry
  // the tail rule reads is stubbed per test where it matters.
  document.body.appendChild(host);
  app = mount(RunEvents, {
    target: host,
    props: {
      runId: 'r1',
      run: { ...RUN, ...over },
      first: { events: EVENTS, next_seq: 4 }
    }
  }) as Record<string, unknown>;
  flushSync();
}

/** The plot's width and the list's box, as a browser would report them.
 *
 * jsdom has no layout and no `ResizeObserver`, so both are stood up here: the
 * observer answers once, synchronously, which is what a real browser's first
 * frame delivers (the same shim `epic-timeline.client.test.ts` uses). Without
 * a measured width the ruler has no pixels and the marker has nowhere to go.
 */
const PLOT_W = 900;
const LIST_H = 300;

beforeEach(() => {
  for (const [prop, value] of [
    ['clientWidth', PLOT_W],
    ['clientHeight', LIST_H]
  ] as const) {
    Object.defineProperty(HTMLElement.prototype, prop, {
      configurable: true,
      get() {
        return value;
      }
    });
  }
  class ResizeObserverShim {
    #cb: (entries: { target: Element }[]) => void;
    constructor(cb: (entries: { target: Element }[]) => void) {
      this.#cb = cb;
    }
    observe(target: Element): void {
      this.#cb([{ target }]);
    }
    unobserve(): void {}
    disconnect(): void {}
  }
  vi.stubGlobal('ResizeObserver', ResizeObserverShim);
  replaceState.mockClear();
  fetchRunAudit.mockReset();
  fetchRunAudit.mockResolvedValue({ run_id: 'r1', events: [], next_seq: 4 });
  fetchRun.mockReset();
  // By default the row still says what the loader read: a status read that
  // changes nothing must not be what stops the tail.
  fetchRun.mockResolvedValue({ ...RUN, status: 'pending', finished_at: null });
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  host?.remove();
  vi.useRealTimers();
});

const rows = () => [...host.querySelectorAll<HTMLElement>('[data-seq]')];
/** The timeline control that holds a recorded phase: the lane draws named
 * stages, each holding the phases it covers (#3458). */
const phase = (name: string) =>
  host.querySelector<SVGGElement>(`[role="button"][data-phases~="${name}"]`);

describe('the event list', () => {
  it('draws one row per recorded event, oldest first', () => {
    render();
    expect(rows().map((r) => r.dataset.seq)).toEqual(['1', '2', '3', '4']);
  });

  it('reads its filter from the URL on first paint', () => {
    render('?q=type%3Aartifact_sealed');
    expect(rows().map((r) => r.dataset.seq)).toEqual(['3']);
  });

  it('words each kind chip, with the recorded code as its title (#2541)', () => {
    render();
    const chip = [...host.querySelectorAll('button')].find((b) => b.title === 'routing_decision');
    expect(chip?.textContent).toBe('Route chosen 1');
    // A type the table has not met still prints, as itself.
    const unknown = [...host.querySelectorAll('button')].find((b) => b.title === 'artifact_sealed');
    expect(unknown?.textContent).toBe('artifact_sealed 1');
  });
});

describe('clicking a phase on the timeline', () => {
  it('filters the list and writes the token to the URL', () => {
    render();

    phase('routing_decision')?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    expect(replaceState).toHaveBeenCalled();
    const [url, state] = replaceState.mock.calls.at(-1)!;
    expect(String(url)).toContain('q=phase%3Arouting_decision');
    expect(state.runSearch).toContain('phase%3Arouting_decision');
    // The reading moved with the write — this is the trap the mocks model.
    expect(rows().map((r) => r.dataset.seq)).toEqual(['2']);
  });

  it('adds a second phase on shift-click and keeps both', () => {
    render();

    phase('routing_decision')?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
    phase('artifact_sealed')?.dispatchEvent(
      new MouseEvent('click', { bubbles: true, shiftKey: true })
    );
    flushSync();

    expect(rows().map((r) => r.dataset.seq)).toEqual(['2', '3']);
  });

  it('clears the filter without dropping the selection', () => {
    render('?q=phase%3Arouting_decision+routing');

    const clear = [...host.querySelectorAll('button')].find((b) => b.textContent === 'clear filter');
    clear?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    const [, state] = replaceState.mock.calls.at(-1)!;
    expect(state.runSearch).toContain('phase%3Arouting_decision');
    expect(state.runSearch).not.toContain('routing+');
    expect(rows().map((r) => r.dataset.seq)).toEqual(['2']);
  });
});

describe('a filter survives a reload', () => {
  it('re-reads the same rows from the URL the write left behind', () => {
    render();
    phase('artifact_sealed')?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
    const [written] = replaceState.mock.calls.at(-1)!;

    // A reload is a real navigation: `page.url` becomes the address bar and
    // `page.state` is empty again.
    if (app) unmount(app);
    app = null;
    host.remove();
    render(new URL(String(written)).search);

    expect(rows().map((r) => r.dataset.seq)).toEqual(['3']);
  });
});

describe('hovering a row', () => {
  it('marks its time on the timeline, after the debounce and not before', () => {
    vi.useFakeTimers();
    render();

    expect(host.querySelector('[data-testid="hover-mark"]')).toBeNull();
    rows()[2].dispatchEvent(new PointerEvent('pointerenter', { bubbles: true }));
    flushSync();
    expect(host.querySelector('[data-testid="hover-mark"]')).toBeNull();

    vi.advanceTimersByTime(100);
    flushSync();
    expect(host.querySelector('[data-testid="hover-mark"]')).not.toBeNull();
  });

  it('draws one marker for a pointer that crossed every row', () => {
    vi.useFakeTimers();
    render();

    for (const row of rows()) {
      row.dispatchEvent(new PointerEvent('pointerenter', { bubbles: true }));
      vi.advanceTimersByTime(20);
    }
    flushSync();
    expect(host.querySelectorAll('[data-testid="hover-mark"]')).toHaveLength(0);

    vi.advanceTimersByTime(100);
    flushSync();
    expect(host.querySelectorAll('[data-testid="hover-mark"]')).toHaveLength(1);
  });
});

describe('the timestamp permalink', () => {
  it('links each row to this page focused on its instant', () => {
    render();
    const link = rows()[1].querySelector('a');

    expect(link?.getAttribute('href')).toBe(
      `/runs/r1?focusedTime=${encodeURIComponent(EVENTS[1].ts)}`
    );
  });

  it('highlights the row a focusedTime points at', () => {
    render(`?focusedTime=${encodeURIComponent(EVENTS[2].ts)}`);

    const highlighted = rows().filter((r) => r.className.includes('bg-ink-selected'));
    expect(highlighted.map((r) => r.dataset.seq)).toEqual(['3']);
  });
});

describe('the live tail', () => {
  it('polls forward from the cursor only while the run is live', async () => {
    vi.useFakeTimers();
    render('', { status: 'pending', finished_at: null });

    await vi.advanceTimersByTimeAsync(1000);
    expect(fetchRunAudit).toHaveBeenCalledWith('r1', 4);
  });

  it('reads once a second, not once per batching beat', async () => {
    vi.useFakeTimers();
    render('', { status: 'pending', finished_at: null });

    await vi.advanceTimersByTimeAsync(3000);
    // Three beats, three reads — not thirty. A 100ms READ cadence would be ten
    // requests a second for a run that may last an hour.
    expect(fetchRunAudit.mock.calls.length).toBe(3);
  });

  it('reads nothing while the tab is in the background', async () => {
    vi.useFakeTimers();
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
    try {
      render('', { status: 'pending', finished_at: null });
      await vi.advanceTimersByTimeAsync(3000);
      expect(fetchRunAudit).not.toHaveBeenCalled();
    } finally {
      hidden.mockRestore();
    }
  });

  it('asks for nothing at all once the run has finished', async () => {
    vi.useFakeTimers();
    render();

    await vi.advanceTimersByTimeAsync(1000);
    expect(fetchRunAudit).not.toHaveBeenCalled();
  });

  it('folds a new page in without repeating what it already holds', async () => {
    vi.useFakeTimers();
    fetchRunAudit.mockResolvedValue({
      run_id: 'r1',
      events: [EVENTS[3], ev(5, 'publication_sealed', 150)],
      next_seq: 5
    });
    render('', { status: 'pending', finished_at: null });

    // One read, then the batching beat that applies what it returned.
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(100);
    flushSync();

    expect(rows().map((r) => r.dataset.seq)).toEqual(['1', '2', '3', '4', '5']);
  });

  it('applies a whole page as ONE update, on the batching beat', async () => {
    vi.useFakeTimers();
    fetchRunAudit.mockResolvedValue({
      run_id: 'r1',
      events: [ev(5, 'agent_turn', 150), ev(6, 'agent_turn', 160), ev(7, 'run_finished', 170)],
      next_seq: 7
    });
    render('', { status: 'pending', finished_at: null });

    await vi.advanceTimersByTimeAsync(1000);
    flushSync();
    // Read, but not yet applied: the batch window has not closed.
    expect(rows()).toHaveLength(4);

    await vi.advanceTimersByTimeAsync(100);
    flushSync();
    expect(rows()).toHaveLength(7);
  });

  it('keeps the rows it holds when a read fails, with no dialog', async () => {
    vi.useFakeTimers();
    fetchRunAudit.mockRejectedValue(new Error('offline'));
    render('', { status: 'pending', finished_at: null });

    await vi.advanceTimersByTimeAsync(3000);
    flushSync();

    expect(rows()).toHaveLength(4);
    expect(host.querySelector('[role="dialog"]')).toBeNull();
  });
});

describe('the tail stops when the RUN stops', () => {
  it('drops the timer once the row says the run has finished', async () => {
    // The `run` prop is loader data and nothing on this page invalidates it —
    // no interval on the page or the layout, and the shared pulse never calls
    // `invalidate` — so a run that completed while the page was open went on
    // asking for its audit tail once a second for as long as the tab stayed
    // open, which is the poll nobody counted (#2285 C7).
    vi.useFakeTimers();
    render('', { status: 'pending', finished_at: null });

    await vi.advanceTimersByTimeAsync(9000);
    const whileLive = fetchRunAudit.mock.calls.length;
    expect(whileLive).toBe(9);

    // The row's own beat lands at 10s and reports the run done.
    fetchRun.mockResolvedValue({
      ...RUN,
      status: 'success',
      finished_at: new Date(T0 + 120_000).toISOString()
    });
    await vi.advanceTimersByTimeAsync(1000);
    flushSync();
    const atStop = fetchRunAudit.mock.calls.length;

    await vi.advanceTimersByTimeAsync(30_000);
    expect(fetchRunAudit.mock.calls.length).toBe(atStop);
    // And the header stops claiming the run is streaming.
    expect(host.textContent).not.toContain('streaming');
  });

  it('reads the row on its own slower beat, not with every tail read', async () => {
    vi.useFakeTimers();
    render('', { status: 'pending', finished_at: null });

    await vi.advanceTimersByTimeAsync(10_000);
    // Ten cursor reads cost one row read: a status changes once per run.
    expect(fetchRunAudit.mock.calls.length).toBe(10);
    expect(fetchRun.mock.calls.length).toBe(1);
  });

  it('reads no row at all for a run that was already finished', async () => {
    vi.useFakeTimers();
    render();

    await vi.advanceTimersByTimeAsync(30_000);
    expect(fetchRun).not.toHaveBeenCalled();
  });

  it('reads nothing while the tab is in the background', async () => {
    vi.useFakeTimers();
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
    try {
      render('', { status: 'pending', finished_at: null });
      await vi.advanceTimersByTimeAsync(30_000);
      expect(fetchRun).not.toHaveBeenCalled();
    } finally {
      hidden.mockRestore();
    }
  });

  it('keeps tailing when the row read fails', async () => {
    vi.useFakeTimers();
    fetchRun.mockRejectedValue(new Error('offline'));
    render('', { status: 'pending', finished_at: null });

    await vi.advanceTimersByTimeAsync(12_000);
    flushSync();
    // A status this page could not read is not a reason to stop following a
    // run that is still executing — and it is not a dialog either.
    expect(fetchRunAudit.mock.calls.length).toBe(12);
    expect(host.querySelector('[role="dialog"]')).toBeNull();
  });
});

describe('a capped page is never passed off as the whole run', () => {
  /** A full first page: the loader read the cap, so there is more behind it. */
  const full = Array.from({ length: RUN_AUDIT_PAGE }, (_, i) =>
    ev(i + 1, 'agent_turn', i)
  );

  function renderFull() {
    kit.set('url', new URL('http://127.0.0.1:8765/runs/r1'));
    kit.set('state', {});
    host = document.createElement('div');
    document.body.appendChild(host);
    app = mount(RunEvents, {
      target: host,
      props: { runId: 'r1', run: RUN, first: { events: full, next_seq: RUN_AUDIT_PAGE } }
    }) as Record<string, unknown>;
    flushSync();
  }

  const moreButton = () =>
    [...host.querySelectorAll('button')].find((b) => b.textContent?.includes('read the next'));

  it('says so in the count, and offers the next page', () => {
    renderFull();
    // "200 of 200" read as the whole run; the loader read page 0 only and
    // forward paging lived inside the live-only tail effect.
    expect(host.textContent).toContain(`${RUN_AUDIT_PAGE} of ${RUN_AUDIT_PAGE}+`);
    expect(moreButton()).toBeDefined();
  });

  it('reads the count in sentence case, like the live mark beside it', () => {
    // `SectionHead`'s meta wrapper neutralises `text-11`'s uppercase and
    // tracking on purpose — only the label shouts. Re-applying `text-11` on
    // the count itself put those declarations back on the child, where the
    // wrapper cannot reach them, and "30 OF 200+" is not a sentence.
    renderFull();
    const count = [...host.querySelectorAll('span')].find(
      (s) => s.textContent === `${RUN_AUDIT_PAGE} of ${RUN_AUDIT_PAGE}+`
    );

    expect(count).toBeDefined();
    expect(count?.classList.contains('text-11')).toBe(false);
    // And it is sized exactly like the sibling it shares the head with.
    expect(count?.className).toContain('font-mono text-[11px]');
  });

  it('reads the next page from the cursor and folds it in', async () => {
    renderFull();
    fetchRunAudit.mockResolvedValue({
      run_id: 'r1',
      events: [ev(RUN_AUDIT_PAGE + 1, 'dispatch_completed', 400)],
      next_seq: RUN_AUDIT_PAGE + 1
    });

    moreButton()?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await vi.waitFor(() => expect(rows()).toHaveLength(RUN_AUDIT_PAGE + 1));

    expect(fetchRunAudit).toHaveBeenCalledWith('r1', RUN_AUDIT_PAGE);
    // The short page is the end of the ledger: the count drops the `+` and
    // the control goes away rather than answering nothing.
    expect(host.textContent).toContain(`${RUN_AUDIT_PAGE + 1} of ${RUN_AUDIT_PAGE + 1}`);
    expect(host.textContent).not.toContain(`of ${RUN_AUDIT_PAGE + 1}+`);
    expect(moreButton()).toBeUndefined();
  });

  it('hands every page it reads back to the page, so the safest action reads it too', async () => {
    // The safest action was drawn off the loader's first page while this list
    // paged on: a settlement past page one showed here and not there (#3057).
    const reads: { events: RunAuditEvent[]; status: string | null }[] = [];
    kit.set('url', new URL('http://127.0.0.1:8765/runs/r1'));
    kit.set('state', {});
    host = document.createElement('div');
    document.body.appendChild(host);
    app = mount(RunEvents, {
      target: host,
      props: {
        runId: 'r1',
        run: RUN,
        first: { events: full, next_seq: RUN_AUDIT_PAGE },
        onread: (read: { events: RunAuditEvent[]; status: string | null }) => reads.push(read)
      }
    }) as Record<string, unknown>;
    flushSync();
    fetchRunAudit.mockResolvedValue({
      run_id: 'r1',
      events: [ev(RUN_AUDIT_PAGE + 1, 'publication_finalized', 400)],
      next_seq: RUN_AUDIT_PAGE + 1
    });

    moreButton()?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await vi.waitFor(() => expect(rows()).toHaveLength(RUN_AUDIT_PAGE + 1));
    flushSync();

    const last = reads[reads.length - 1];
    expect(last.events).toHaveLength(RUN_AUDIT_PAGE + 1);
    expect(last.events[last.events.length - 1].event).toBe('publication_finalized');
  });

  it('offers nothing to press on a run that fits in one page', () => {
    render();
    expect(host.textContent).toContain('4 of 4');
    expect(moreButton()).toBeUndefined();
  });
});

describe('the filter controls', () => {
  it('offers no clear for a phase-only selection, which it cannot change', () => {
    // `clearRunFilter` keeps what was CLICKED, so on a phase-only filter it
    // returns an identical filter — the button was offered and did nothing.
    render('?q=phase%3Arouting_decision');
    const clear = [...host.querySelectorAll('button')].find((b) => b.textContent === 'clear filter');

    expect(clear).toBeUndefined();
    // And the selection is still droppable the way it was made.
    phase('routing_decision')?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
    expect(rows()).toHaveLength(4);
  });

  it('offers the clear as soon as something typed or chipped is in it', () => {
    render('?q=phase%3Arouting_decision+routing');
    expect(
      [...host.querySelectorAll('button')].find((b) => b.textContent === 'clear filter')
    ).toBeDefined();
  });
});

describe('the empty states', () => {
  it('says the run is queued when it has recorded nothing', () => {
    kit.set('url', new URL('http://127.0.0.1:8765/runs/r1'));
    kit.set('state', {});
    host = document.createElement('div');
    document.body.appendChild(host);
    app = mount(RunEvents, {
      target: host,
      props: { runId: 'r1', run: RUN, first: { events: [], next_seq: 0 } }
    }) as Record<string, unknown>;
    flushSync();

    expect(host.textContent).toContain('Run queued');
  });

  it('says a kind selection admits nothing in this run', () => {
    render('?q=type%3Afrom_another_run');
    expect(host.textContent).toContain('No kinds selected');
  });
});

describe('a row reads as a sentence (#3458)', () => {
  it('shows no JSON and no seq until its disclosure is opened', () => {
    render();
    const row = rows()[1];
    expect(row.textContent).not.toContain('{');
    expect(row.querySelector('[data-event-detail]')).toBeNull();

    const toggle = row.querySelector<HTMLButtonElement>('button[aria-expanded]');
    expect(toggle?.getAttribute('aria-expanded')).toBe('false');
    toggle?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    const detail = row.querySelector('[data-event-detail]')?.textContent ?? '';
    expect(toggle?.getAttribute('aria-expanded')).toBe('true');
    expect(detail).toContain('seq 2');
    expect(detail).toContain('"step": 2');
  });

  it('names every stage on the timeline', () => {
    render();
    const names = [...host.querySelectorAll('[data-stage-label]')].map((t) => t.textContent);
    // A kind with no stage of its own is named in words, never by its code.
    expect(names).toEqual(['Run started', 'dispatch', 'seal', 'Run finished']);
  });

  it('takes Space on a stage, so it filters and does not scroll the page', () => {
    render();
    const space = new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true });
    phase('artifact_sealed')?.dispatchEvent(space);
    flushSync();

    expect(space.defaultPrevented).toBe(true);
    expect(rows().map((r) => r.dataset.seq)).toEqual(['3']);
  });
});

describe('a stage press is the stage pressed (#3458)', () => {
  /** The fixture run, whose dispatch, sandbox and work stages sit a second
   * apart: their names are nudged along under each other's bars. */
  const FIXTURE = fixtureAudit as { events: RunAuditEvent[] };

  function renderAt(width: number) {
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
      configurable: true,
      get() {
        return width;
      }
    });
    kit.set('url', new URL('http://127.0.0.1:8765/runs/r1'));
    kit.set('state', {});
    host = document.createElement('div');
    document.body.appendChild(host);
    const events = FIXTURE.events;
    app = mount(RunEvents, {
      target: host,
      props: {
        runId: 'r1',
        run: { ...RUN, started_at: events[0].ts, finished_at: events.at(-1)?.ts ?? null },
        first: { events, next_seq: events.length }
      }
    }) as Record<string, unknown>;
    flushSync();
  }

  const num = (el: Element, attr: string) => Number(el.getAttribute(attr));

  /** Every stage whose press covers the point, by name. */
  function pressedAt(x: number, y: number): string[] {
    return [...host.querySelectorAll('[data-stage-press]')]
      .filter((r) => {
        const [rx, ry, rw, rh] = ['x', 'y', 'width', 'height'].map((a) => num(r, a));
        return x >= rx && x < rx + rw && y >= ry && y < ry + rh;
      })
      .map((r) => r.closest('[data-stage]')?.getAttribute('data-stage') ?? '');
  }

  for (const width of [358, 736, 1300]) {
    it(`gives each bar and each name to its own stage alone at ${width}px`, () => {
      renderAt(width);
      const controls = [...host.querySelectorAll<SVGGElement>('[role="button"][data-stage]')];
      expect(controls.map((g) => g.dataset.stage)).toEqual(expect.arrayContaining(['dispatch', 'sandbox', 'work']));
      for (const g of controls) {
        const name = g.dataset.stage ?? '';
        const bar = g.querySelector('rect:not([data-stage-press])') as Element;
        const label = g.querySelector('[data-stage-label]') as Element;
        const barAt = [num(bar, 'x') + num(bar, 'width') / 2, num(bar, 'y') + num(bar, 'height') / 2];
        // A name's middle: half its width along, and above its baseline.
        const labelAt = [num(label, 'x') + (name.length * 7) / 2, num(label, 'y') - 4];
        expect(pressedAt(barAt[0], barAt[1]), `${name} bar`).toEqual([name]);
        expect(pressedAt(labelAt[0], labelAt[1]), `${name} name`).toEqual([name]);
      }
    });
  }
});
