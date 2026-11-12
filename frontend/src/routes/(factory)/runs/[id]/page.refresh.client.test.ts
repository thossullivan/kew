/**
 * #3057 — a refresh re-reads the run, and the safest action follows it.
 *
 * The page draws its safest action off what the event list read for itself
 * (further pages, the live tail, the run's re-read status), because the list
 * reads past the loader. But a refresh is `invalidateAll`: the loader re-runs
 * and the list is NOT remounted. A run seen live that stopped at
 * `awaiting_publication` froze that self-read, so after `kew finalize` the
 * refreshed loader said `success` with `publication_finalized` and the page
 * still offered `kew finalize <run>`, which the server refuses.
 *
 * Mounted rather than read off `buildFlightView`: the bug was a reading that
 * outlived the loader data, which only a running page can show.
 */
import { flushSync, mount, unmount } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { FlightTimeline, Run, RunAuditEvent } from '$lib/api';
import type { RunDetailData } from './+page';

/** `page.url`, `page.state` and the page's `data`, reactive. */
const kit = new SvelteMap<string, unknown>();

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
vi.mock('$app/navigation', () => ({
  invalidateAll: vi.fn(async () => {}),
  replaceState: vi.fn((url: URL | string, state: App.PageState) => {
    void url;
    kit.set('state', state);
  })
}));

const fetchRun = vi.fn();
const fetchRunAudit = vi.fn();
vi.mock('$lib/api', async (original) => ({
  ...(await original<Record<string, unknown>>()),
  fetchRun,
  fetchRunAudit
}));

const { default: RunPage } = await import('./+page.svelte');

const RUN: Run = {
  run_id: 'r1',
  issue_num: 3057,
  title: 'publish the preserved artifact',
  runner: 'claude-code',
  model: 'claude-opus-5',
  status: 'running',
  cost_usd: 1.25,
  channel: 'hosted',
  started_at: '2026-10-19T10:00:00Z',
  finished_at: null,
  duration_seconds: 0,
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
  branch: 'agent/issue-3057',
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
  item_id: '3057',
  reported_model: null,
  provider: null,
  transport_mode: 'direct',
  planned_cost_usd: null,
  accounting_status: 'settled',
  verdict: null
};

const FLIGHT: FlightTimeline = {
  run_id: 'r1',
  attempt_number: 1,
  item_id: '3057',
  issue_num: 3057,
  source_tracker: 'github',
  source_id: '3057',
  sealed: true,
  through_seq: 1,
  chain_head: 'f'.repeat(64),
  chain_verified: true,
  artifact_node_id: null,
  runner_telemetry: 'structured',
  events: [],
  gaps: [],
  availability: [
    { dimension: 'host_sequence', availability: 'available', provenance: 'observed', detail: '' }
  ]
};

function ev(seq: number, event: string, payload: Record<string, unknown> = {}): RunAuditEvent {
  return {
    seq,
    ts: new Date(Date.parse('2026-10-19T10:00:00Z') + seq * 1000).toISOString(),
    event,
    actor_type: 'policy',
    item_id: '3057',
    payload
  };
}

const DEFERRED = ev(2, 'publication_deferred', { step: 'push', error: 'protected branch' });

function data(run: Run, events: RunAuditEvent[]): RunDetailData {
  return {
    runId: 'r1',
    attempt: null,
    run,
    flight: FLIGHT,
    diff: null,
    audit: { run_id: 'r1', events, next_seq: events.length }
  };
}

let host: HTMLDivElement;
let app: Record<string, unknown> | null = null;

function render(first: RunDetailData): void {
  kit.set('url', new URL('http://127.0.0.1:8765/runs/r1'));
  kit.set('state', {});
  kit.set('data', first);
  host = document.createElement('div');
  document.body.appendChild(host);
  app = mount(RunPage, {
    target: host,
    // A getter over reactive state: assigning `kit.set('data', …)` is what
    // SvelteKit does to `data` when `invalidateAll` re-runs the loader.
    props: {
      get data() {
        return kit.get('data') as RunDetailData;
      }
    }
  }) as Record<string, unknown>;
  flushSync();
}

const safest = () => host.querySelector('[aria-label="Safest action"]')?.textContent ?? '';

class ResizeObserverShim {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', ResizeObserverShim);
  fetchRun.mockReset();
  fetchRunAudit.mockReset();
  fetchRunAudit.mockResolvedValue({ run_id: 'r1', events: [], next_seq: 2 });
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  host?.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('refreshing a run the page watched stop at awaiting publication', () => {
  it('drops kew finalize once the refreshed loader says the run was finalized', async () => {
    vi.useFakeTimers();
    render(data(RUN, [ev(1, 'run_started'), DEFERRED]));

    // The row's own beat: the run stopped, awaiting publication.
    fetchRun.mockResolvedValue({ ...RUN, status: 'awaiting_publication', finished_at: '2026-10-19T10:05:00Z' });
    await vi.advanceTimersByTimeAsync(10_000);
    flushSync();
    expect(safest()).toContain('kew finalize r1');

    // The operator ran `kew finalize`, then pressed refresh: the loader reads
    // the run as success, with the settlement on its ledger.
    kit.set(
      'data',
      data({ ...RUN, status: 'success', finished_at: '2026-10-19T10:05:00Z', pr_url: 'https://github.com/o/r/pull/9' }, [
        ev(1, 'run_started'),
        DEFERRED,
        ev(3, 'publication_finalized')
      ])
    );
    flushSync();

    expect(safest()).not.toContain('kew finalize');
  });

  it('ignores a status read that was still out when the refresh landed', async () => {
    vi.useFakeTimers();
    render(data(RUN, [ev(1, 'run_started'), DEFERRED]));

    let answer: (row: Run) => void = () => {};
    fetchRun.mockReturnValue(new Promise<Run>((resolve) => (answer = resolve)));
    await vi.advanceTimersByTimeAsync(10_000);

    // The refreshed row alone says the deferral is closed: no settlement on
    // the ledger page, so a stale status read is the only thing that could
    // put the command back.
    kit.set('data', data({ ...RUN, status: 'success', finished_at: '2026-10-19T10:05:00Z' }, [
      ev(1, 'run_started'),
      DEFERRED
    ]));
    flushSync();
    // The read issued before the refresh answers with what it saw then.
    answer({ ...RUN, status: 'awaiting_publication', finished_at: '2026-10-19T10:05:00Z' });
    await vi.advanceTimersByTimeAsync(0);
    flushSync();

    expect(safest()).not.toContain('kew finalize');
  });
});
