import { fetchEpic, type EpicDetail } from '$lib/api';
import { isUnauthorized } from '$lib/epic/live';

export const ssr = false;

/**
 * Fetch the initial epic snapshot/heartbeat/economics for the first paint.
 * If the API is unreachable (or the ref 404s), return null so the page
 * renders an "offline / no data" empty state rather than erroring — mirrors
 * routes/loops/+page.ts and routes/epics/+page.ts exactly. A 401 is NOT
 * softened into that generic offline state: it means we have no valid
 * session, so we send the browser to /inbox to pick one up (never a token
 * in the URL) rather than lying that the server is unreachable. The live
 * $effect in +page.svelte takes over from here via SSE.
 */
export async function load({
  params,
  fetch
}: {
  params: { ref: string };
  fetch: typeof globalThis.fetch;
}): Promise<{ detail: EpicDetail | null; ref: string }> {
  try {
    return { detail: await fetchEpic(params.ref, fetch), ref: params.ref };
  } catch (e) {
    if (isUnauthorized(e)) window.location.href = '/inbox';
    return { detail: null, ref: params.ref };
  }
}
