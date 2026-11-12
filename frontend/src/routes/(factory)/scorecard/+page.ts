import {
  DEFAULT_OUTCOME_WEEKS,
  DEFAULT_SCORECARD_DAYS,
  fetchOutcomes,
  fetchScorecard,
  type OutcomeReport,
  type Scorecard
} from '$lib/api';

export const ssr = false;

/**
 * Fetch scorecard data from `kew serve`. If API is unreachable, return null
 * so the page renders an "offline / no data" empty state rather than erroring.
 *
 * The two reads settle independently (C3/#2301): the weekly outcome section is
 * a different endpoint, and a `kew serve` that does not serve it yet must
 * still render the per-model table rather than the offline state.
 */
export async function load({
  fetch
}: {
  fetch: typeof globalThis.fetch;
}): Promise<{ scorecard: Scorecard | null; outcomes: OutcomeReport | null }> {
  const [scorecard, outcomes] = await Promise.all([
    fetchScorecard(DEFAULT_SCORECARD_DAYS, fetch).catch(() => null),
    fetchOutcomes(DEFAULT_OUTCOME_WEEKS, fetch).catch(() => null)
  ]);
  return { scorecard, outcomes };
}
