import { fetchEpics, type EpicSummary } from '$lib/api';
import { isUnauthorized } from '$lib/epic/live';

export const ssr = false;

/**
 * Fetch the epic list from `kew serve`. If the API is unreachable, return
 * null so the page renders an "offline / no data" empty state rather than
 * erroring (mirrors routes/loops/+page.ts exactly). A 401 is NOT softened
 * into that generic offline state: it means we have no valid session, so we
 * send the browser to /inbox to pick one up (never a token in the URL)
 * rather than lying that the server is unreachable — mirrors
 * routes/(factory)/epics/[ref]/+page.ts exactly.
 */
export async function load({
  fetch
}: {
  fetch: typeof globalThis.fetch;
}): Promise<{ epics: EpicSummary[] | null }> {
  try {
    return { epics: await fetchEpics(fetch) };
  } catch (e) {
    if (isUnauthorized(e)) window.location.href = '/inbox';
    return { epics: null };
  }
}
