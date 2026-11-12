import { fetchDashboard, fetchWorkProjection, type Dashboard, type WorkProjection } from '$lib/api';

export const ssr = false;

export type WorkReading = {
  /** `null` means the read failed — the page names it, never renders it empty. */
  projection: WorkProjection | null;
  /**
   * Seed only. The factory shell polls `/api/dashboard` for the whole group
   * (shell/pulse.svelte.ts), and the page prefers the pulse's copy the moment
   * it arrives; this read exists so the first paint of `/work` already has the
   * ready queue in it rather than a list that grows an instant later.
   */
  dashboard: Dashboard | null;
};

export type WorkPageData = {
  /**
   * #2545 D9 — the reading, NOT awaited. A loader that awaited it held the whole
   * client-rendered app off the screen until both reads were back: no header,
   * no tabs, nothing, for one to three seconds on a live server. Handing the
   * promise to the page lets SvelteKit draw the shell at once, and the page
   * says "Reading work items…" where the list will be.
   */
  reading: Promise<WorkReading>;
  /**
   * #3055 — settles when the list read alone has, answered or not. The page
   * re-reads the list on the shell's pulse and keeps one list read open at a
   * time, this one included; waiting on `reading` instead would hold the
   * pulse's re-reads off for as long as the dashboard read is slow.
   */
  listRead: Promise<unknown>;
};

export function load({ fetch }: { fetch: typeof globalThis.fetch }): WorkPageData {
  const projection = fetchWorkProjection(fetch);
  return {
    reading: readWork(projection, fetchDashboard(fetch)),
    listRead: projection.then(
      () => undefined,
      () => undefined
    )
  };
}

/**
 * FUI-18 — the Work screen reads two payloads (see `$lib/work/list`), and
 * neither failing is an error: a page that threw would replace the whole screen
 * with an error boundary, where the list can honestly show what it did read and
 * name what it did not. Both reads are taken in parallel, and settled
 * separately so one failure cannot take the other's result down with it.
 */
async function readWork(
  projectionRead: Promise<WorkProjection>,
  dashboardRead: Promise<Dashboard>
): Promise<WorkReading> {
  const [projection, dashboard] = await Promise.allSettled([projectionRead, dashboardRead]);
  return {
    projection: projection.status === 'fulfilled' ? projection.value : null,
    dashboard: dashboard.status === 'fulfilled' ? dashboard.value : null
  };
}
