import {
  fetchDiff,
  fetchFlight,
  fetchRun,
  fetchRunAudit,
  type Diff,
  type FlightTimeline,
  type Run,
  type RunAuditPage
} from '$lib/api';
import { isUnauthorized } from '$lib/epic/live';

export const ssr = false;

export type RunDetailData = {
  runId: string;
  /** The attempt the link asked for, or null for "whatever is latest". */
  attempt: number | null;
  run: Run | null;
  flight: FlightTimeline | null;
  diff: Diff | null;
  /** The first forward page of this run's ledger events (#2300). */
  audit: RunAuditPage | null;
};

/**
 * Read one run's canonical evidence: the run row, its receipt-backed Flight
 * projection, and — only when the projection names a sealed artifact — the
 * change set bound to it. A diff is requested for `artifact_node_id` and
 * nothing else, because that is the only node the server will answer for.
 *
 * `?attempt=` is honoured because a run page given no attempt selects the
 * LATEST one, which is the wrong evidence for an earlier bar on an epic's
 * Gantt (#2300) — the link from a bar names the attempt it drew.
 *
 * The first page of the run's ledger events is read here too: the event list
 * is the page's main reading, and a loader read is what lets a reload land on
 * the same rows rather than flashing an empty list while an effect fetches.
 * Forward pages and the live tail are the page's own reads from `next_seq` —
 * one page is a page, never the run: a read that comes back at the server's
 * cap is shown as `N of 200+` with a control for the next one, because a
 * ledger silently cut off at 200 is the one thing an evidence surface may not
 * do (`mayHoldMoreRunEvents`).
 *
 * The exact compiled prompt is deliberately NOT loaded here. `app.html` sets
 * `data-sveltekit-preload-data="hover"`, so this loader runs when an operator
 * merely hovers a link to the page; the bytes are fetched from the page's own
 * reveal click and nowhere else.
 *
 * Each read fails soft into `null` so a missing run, a store with no evidence
 * surface, or an unreachable server renders an honest empty state rather than
 * an error page — except a 401, which means we hold no session and sends the
 * browser to /inbox to pick one up (never a token in the URL).
 */
export async function load({
  params,
  url,
  fetch
}: {
  params: { id: string };
  url: URL;
  fetch: typeof globalThis.fetch;
}): Promise<RunDetailData> {
  const read = async <T>(fn: () => Promise<T>): Promise<T | null> => {
    try {
      return await fn();
    } catch (e) {
      if (isUnauthorized(e) && typeof window !== 'undefined') window.location.href = '/inbox';
      return null;
    }
  };

  const asked = Number(url.searchParams.get('attempt'));
  const attempt = Number.isInteger(asked) && asked > 0 ? asked : null;
  const [run, flight, audit] = await Promise.all([
    read(() => fetchRun(params.id, fetch)),
    read(() => fetchFlight(params.id, attempt ?? undefined, fetch)),
    read(() => fetchRunAudit(params.id, 0, undefined, fetch))
  ]);
  const node = flight?.artifact_node_id ?? null;
  const diff = node === null ? null : await read(() => fetchDiff(params.id, node, fetch));
  return { runId: params.id, attempt, run, flight, diff, audit };
}
