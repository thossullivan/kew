import { fetchLoops, type Loop } from '$lib/api';

export const ssr = false;

/**
 * Fetch the loop list from `kew serve`. If the API is unreachable, return null
 * so the page renders an "offline / no loops" empty state rather than erroring.
 */
export async function load({
  fetch
}: {
  fetch: typeof globalThis.fetch;
}): Promise<{ loops: Loop[] | null }> {
  try {
    return { loops: await fetchLoops(undefined, fetch) };
  } catch {
    return { loops: null };
  }
}
