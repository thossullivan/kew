import { fetchRuns, type Run } from '$lib/api';

export const ssr = false;

/**
 * Fetch run list from `kew serve`. If API is unreachable, return null
 * so the page renders an "offline / no runs" empty state rather than erroring.
 */
export async function load({
  fetch
}: {
  fetch: typeof globalThis.fetch;
}): Promise<{ runs: Run[] | null }> {
  try {
    return { runs: await fetchRuns(fetch) };
  } catch {
    return { runs: null };
  }
}
