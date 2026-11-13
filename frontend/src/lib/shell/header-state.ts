// What the factory header shows, decided in one pure place (FUI-09).
//
// The two counters and the state cursor are the only numbers on every factory
// screen, so their definitions are pinned here rather than re-derived per page
// (Factory UI plan §1, global header): "needs you" is the dashboard's needs-you set —
// the same rows the Console lists, Monitor holds and Work filters on — and
// "working" is the runs that read as `running`. Nothing here counts twice, and
// neither word is redefined here: `running` comes from `displayStatus`, the
// app's one owner of that vocabulary.
//
// The distinction the whole file turns on: a count of 0 and a reading that
// could not be taken are different facts. `/api/dashboard` being unreadable is
// not "nothing needs you", so an unread count is `null` and renders as an em
// dash. A zero is only ever printed when zero is what was read — and because
// the dashboard is fail-soft *per source*, "what was read" includes checking
// that every source the judgment queue is built from actually answered.
//
// Unreadable is not the same as nothing, either. A partial payload still hands
// over the rows its healthy sources produced, so an unknown total keeps a known
// floor beside it: the counter prints the count, and the state cursor reads the
// floor — a blocking row that did arrive lights the cursor while the number
// above it stays an honest em dash.
import type { Dashboard, Run } from '$lib/api';
import { bannerNotices } from '$lib/dashboard';
import { displayStatus } from '$lib/kpi';

/** What an unreadable count renders as. Never `0` — see the note above. */
export const UNREADABLE = '—';

/**
 * The width the header may show its desktop row at, and why (#2333, #2419).
 *
 * The row is one 56px line of parts that mostly cannot shrink: the wordmark,
 * the nav and its three whole labels, and a cluster of readings and presses
 * that are `shrink-0` on purpose, because a press squeezed to a few pixels is a
 * lost press (`FactoryHeader.svelte`). A row like that cannot absorb a deficit
 * — it can only carry fewer things — so the breakpoint it folds on is not taste
 * but the width of the row itself.
 *
 * Both numbers below were read off the desktop row in a real browser, with IBM
 * Plex loaded and every always-on reading of the day present: the working
 * count, the needs-you count, the health badge, the repo and its hairline, and
 * More. They are what chose `fold`, and the two floors failed differently:
 *
 * * under `page` the PAGE scrolled sideways, because the nav had already been
 *   clipped to nothing and there was nothing left to give. That is #2419 — the
 *   first fix for #2278 folded at `sm`, a hundred pixels under this floor, so
 *   from 641px up the epic page had a horizontal scrollbar nothing on the epic
 *   page had put there;
 * * under `readable` the page fitted but the NAV did not: the three tabs
 *   started ellipsising, and a header that cannot say which view you are on has
 *   stopped doing its job even though no pixel is off screen.
 *
 * So `md` was not the answer even though it clears `page`: it would have traded
 * the scrollbar for a nav reading `Wo… Mon… Ep…`. `fold` is the first Tailwind
 * breakpoint that clears BOTH, and it is the number the rest of the console
 * already folds on — the Work list and detail, the epic minimap and key hints,
 * and the rail's own stack (DESIGN.md, Responsive Behavior) — so the whole
 * surface folds once.
 *
 * #2418 then rebuilt the row these were measured on, and neither floor is the
 * live number any more: the nav is `shrink-0` and cannot ellipsise at any
 * width, the full artboard cluster waits until `xl`, and the row is priced item
 * by item off the markup at every width in `factory-header.test.ts` rather than
 * measured once. What survives — and is what this constant is for — is the
 * breakpoint and the reasoning that chose it, which that file holds to `fold`
 * and `fold` to the markup. The PAGE scrolling is still what must never happen
 * again, and `ui-check` reads it at 641×900.
 */
export const HEADER_FOLD = {
  /**
   * Gutter, both header gaps, the wordmark and the whole right cluster, with
   * the nav clipped to nothing — the row as #2333 found it. Below this the page
   * scrolled sideways.
   */
  page: 741,
  /**
   * The same, plus the three tab labels at their full width and the gaps
   * between them. Below this that row ellipsised its nav; the row #2418 left
   * never does, at any width.
   */
  readable: 929,
  /** Tailwind's `lg`: the failure readings from here up, the folded row below. */
  fold: 1024
} as const;

export type HeaderCounts = {
  /** Runs currently executing; `null` when `/api/runs` could not be read. */
  working: number | null;
  /**
   * The needs-you set; `null` when `/api/dashboard` could not be read, or when
   * any source those rows come from failed inside a fail-soft payload.
   *
   * It is `dashboard.rows.length` and nothing else. Monitor's holds column, the
   * Work screen's list and an epic's needs-you legend read that same set rather
   * than each deciding what the words mean, which is what let this counter say
   * 16 over a holds list saying 4 and an epic legend saying 0 (#2207).
   */
  needsYou: number | null;
  /**
   * The judgment rows that *were* read — equal to `needsYou` when the whole
   * queue answered, and the floor under it when only part of it did. Never
   * `null`: a dashboard that never arrived has a floor of 0.
   *
   * No counter prints this, because a floor is not a count. The state cursor
   * reads it: a row that did arrive is blocking work whether or not the total
   * above it is knowable, and a muted cursor over a read row would be the
   * null-vs-zero mistake again, one layer up.
   */
  needsYouFloor: number;
  /** The delivery repo, or `null` when no review-pressure reading was served. */
  repo: string | null;
};

/**
 * The `/api/dashboard` sources the judgment rows are assembled from
 * (`needs_you_set` in kew/src/kew/service/needs_you.py, carried through
 * `assemble` verbatim). The payload is fail-soft *per source*: a source that
 * raised is named in `unavailable` and contributes no rows, so `rows.length`
 * alone is a floor, not a count — and when every row source is out it is a
 * confident `0` for a queue nobody managed to read.
 *
 * `pr_reviews` belongs here even though it is not itself a row source: it is
 * the work projection `work_rows` renders, and when it fails `work_rows`
 * succeeds over `None` and stays silent, so `pr_reviews` is the only name that
 * appears. The glance-only sources (`fleet`, `caps`, `routing`, `ready_queue`,
 * `queue_candidates`, `recent_completions`, `banner`) are deliberately absent:
 * none of them can produce a judgment row, and an unreadable ready queue must
 * not blank a count that was taken correctly.
 *
 * `node_epics` is absent for that same reason, and it is the one worth spelling
 * out (#2279). It is the epic walk that fills in which epic each entry belongs
 * to, so a failed read loses the PER-EPIC buckets (`needs_you_by_epic`, which
 * an epic legend and the Epics list read through `epicNeedsYou`) and loses no
 * row: every entry is still served, and THIS count is still exact. Blanking the
 * header over it would be the null-vs-zero mistake pointing the other way —
 * a reading that was taken correctly, reported as unknown. The surfaces that
 * DO rest on it watch it where they read it: `EPIC_BUCKET_SOURCES` in
 * `$lib/epic/attention` is this array plus that one name, and nothing else in
 * `unavailable` clouds a per-epic count either.
 *
 * `executing_runs` (#2784) is absent on the same terms: it removes the entries
 * on items a run is executing on and produces none, so a failed read serves
 * every row it was given — a reading the header still counts, with the running
 * item drawn WORKING on Work beside it — and `EPIC_BUCKET_SOURCES` watches it.
 *
 * This list is the backend's vocabulary written out in a second language, so it
 * is held to the first: `kew/tests/test_factory_header_parity.py` reads this
 * array and fails if `assemble` grows, renames or drops a row source. Exported
 * for that anchor and for `header-state.test.ts`, which walks it rather than
 * transcribing it a third time.
 */
export const JUDGMENT_ROW_SOURCES: readonly string[] = [
  'pr_reviews',
  'approval_holds',
  'budget_holds',
  'stalled_epics',
  'work_rows',
  'epic_waits',
  'orphan_prs',
  'triage_proposals',
  'merge_queue'
];

/** True when every source the judgment queue is built from answered. */
function judgmentQueueRead(dashboard: Dashboard): boolean {
  const out = new Set(dashboard.unavailable ?? []);
  return !JUDGMENT_ROW_SOURCES.some((name) => out.has(name));
}

/**
 * Whether a run counts as working.
 *
 * `/api/runs` carries the backend `RunStatus` verbatim (kew/src/kew/run_store.py:
 * pending | awaiting_publication | success | failure | refused |
 * budget_exceeded | budget_stopped | budget_stop | killed). There is no
 * `running` in it: `running` is the display-only refinement `displayStatus`
 * derives from a `pending` run that has actually begun, and that function is
 * the app's single owner of the word. Matching the literal `'running'` against
 * a raw status would count nothing forever, so the counter asks `displayStatus`
 * instead — which also keeps a merely queued run out of "working".
 *
 * Exported because Monitor's 44px Working readout (FUI-22) is the same number
 * one type size larger: it filters with this predicate rather than restating
 * the word, so the two cannot disagree on the same screen.
 */
export function isWorkingRun(run: { status: string; started_at: string | null }): boolean {
  return displayStatus({ status: run.status, startedAt: run.started_at }) === 'running';
}

export function headerCounts(dashboard: Dashboard | null, runs: Run[] | null): HeaderCounts {
  // The rows in hand. A partial payload cannot say how many there are in total,
  // but it can say how many it read, and that stays available to the cursor.
  const rowsRead = dashboard === null ? 0 : dashboard.rows.length;
  return {
    working: runs === null ? null : runs.filter(isWorkingRun).length,
    needsYou: dashboard === null || !judgmentQueueRead(dashboard) ? null : rowsRead,
    needsYouFloor: rowsRead,
    // `pressure` is optional (a serve that predates I19 omits it) and may be an
    // explicit null meaning "the reading itself could not be taken" — both are
    // "we don't know the repo", which renders as nothing rather than a guess.
    repo: dashboard?.glance.pressure?.delivery_repo ?? null
  };
}

/**
 * The fleet's safety stop, as the header reads it: its message, or `null` when
 * the fleet is not stopped.
 *
 * The header carries this because the cutover (FUI-26) took away the page every
 * reader used to pass through. A stop is the loudest fact the dashboard serves —
 * nothing dispatches until a human reverses it — and after the cutover the only
 * surface on every screen is the header, so this is where it says so and where
 * the resume lives.
 *
 * The message is the console's own (`bannerNotices`), not a second wording of
 * the same fact: the two surfaces render one sentence so they cannot come to
 * disagree about what a stop means.
 *
 * An unread dashboard reads as no stop. That is the one place this file bends
 * its null-versus-zero rule, and deliberately: an alarm invented out of a
 * dropped poll would offer a resume for a fleet nobody knows is stopped, and the
 * counters beside it already show em dashes for the reading that failed.
 */
export function fleetStop(dashboard: Dashboard | null): { message: string } | null {
  if (dashboard === null) return null;
  const stop = bannerNotices(dashboard.banner).find((n) => n.kind === 'fleet_stopped');
  return stop ? { message: stop.message } : null;
}

/** The word the header puts beside the stop, in the artboards' terse grammar. */
export const FLEET_STOPPED = 'fleet stopped';

/** A count as shown: its digits, or the em dash when it was never read. */
export function countLabel(n: number | null): string {
  return n === null ? UNREADABLE : String(n);
}

/** What a header reading says while its first read is still out (D35). */
export const READING = 'reading…';

/**
 * Whether a header reading is still waiting on its first read: unread, and no
 * read has settled yet. Then there is nothing to show, and a bare `—` — with
 * the cyan working mark beside it, "• — working" — claimed a reading that had
 * failed (design review c4, D35). The header says `reading…` instead, or
 * nothing. Once a read has settled, an unread value keeps its em dash.
 */
export function stillReading(value: unknown, hasRead: boolean): boolean {
  return value === null && !hasRead;
}

/**
 * The state cursor beside the wordmark.
 *
 * The owner's rule of 2026-09-24 (Factory UI plan, owner decisions): magenta when the
 * viewed item or the fleet needs a human, cyan when anything is working, muted
 * when idle. The artboards disagreed — DWork is magenta, DEpic and DMonitor are
 * cyan — because each shows a different state, which is what makes it a state
 * cursor rather than a logo colour.
 */
export type CursorTone = 'attention' | 'working' | 'idle';

export type CursorContext = {
  /** Fleet judgment queue, from `headerCounts`. */
  needsYou: number | null;
  /** Runs working, from `headerCounts`. */
  working: number | null;
  /**
   * Judgment rows known to have been read, from `headerCounts`. It carries the
   * signal when `needsYou` is `null` for a partial payload; a caller with no
   * reading to offer may leave it out, which reads as "none known".
   */
  needsYouFloor?: number;
  /**
   * True when the item the page is showing needs a human. Left undefined by
   * pages that show no single item (Monitor, Epics list).
   */
  itemNeedsYou?: boolean;
};

export function cursorTone(ctx: CursorContext): CursorTone {
  // An unreadable count is not a signal in either direction, so `null` falls
  // through to idle — a muted cursor claims nothing. The rows *under* an
  // unreadable count are a signal, though: a fail-soft payload that lost one
  // source still delivered the judgment rows of the sources that answered, and
  // those need a human now. So the cursor follows the floor while the counter
  // beside it honestly shows an em dash, and only a floor of 0 goes quiet.
  const blocking = Math.max(ctx.needsYou ?? 0, ctx.needsYouFloor ?? 0);
  if (ctx.itemNeedsYou || blocking > 0) return 'attention';
  if ((ctx.working ?? 0) > 0) return 'working';
  return 'idle';
}

/** Tone → token. Magenta is rationed to "a human is needed" (Factory UI plan finding 3). */
export const CURSOR_COLOR: Record<CursorTone, string> = {
  attention: 'var(--accent)',
  working: 'var(--accent-2)',
  idle: 'var(--text-5)'
};
