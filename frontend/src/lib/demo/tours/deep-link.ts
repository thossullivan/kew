/**
 * A tour's deep link, `?tour=<id>&step=<n>` with `n` from 1 (#3145).
 *
 * Read the way every shallow-routed page here reads its own query: from
 * `page.state` first, because `replaceState` never moves `page.url`, and from
 * `page.url` only when the state holds no word on it. Written by MERGING the
 * two keys into whatever the address bar and the state already hold, never by
 * replacing them: the page under the tour keeps its own `?item=` and its own
 * state key, and the tour keeps its two.
 *
 * The link lives in the address bar and in `page.state`, never in `page.url`:
 * a page that builds its next write from `page.url` would otherwise write an
 * ended tour back. The controller navigates to a step's route without the
 * link and writes it in after, and turns a deep link's arrival into a
 * navigation to the same address without it.
 */

export type TourLink = { tour: string; step: number };

/** The tour link in force: `null` when there is none, or the state ended it. */
export function readTourLink(state: App.PageState, url: URL): TourLink | null {
  if (state.tour !== undefined) {
    return state.tour === null ? null : { tour: state.tour, step: stepOf(String(state.tourStep ?? 1)) };
  }
  const tour = url.searchParams.get('tour');
  return tour ? { tour, step: stepOf(url.searchParams.get('step')) } : null;
}

/**
 * A 1-based step from the query, step 1 when the link names none, and 0 for
 * anything that is not a whole number from 1 (`step=0`, `step=abc`): a step no
 * tour has, so the link opens nothing and is taken out of the address, as a
 * step past a tour's end is (design review c9, X6).
 */
function stepOf(raw: string | null): number {
  if (raw === null) return 1;
  const step = /^[1-9]\d*$/.test(raw) ? Number(raw) : 0;
  return Number.isSafeInteger(step) ? step : 0;
}

/**
 * The state keys in which a page keeps the query string it last wrote
 * (`app.d.ts`): the Work page's, the Monitor stream's and the run page's. A page that holds its key builds its next address from it, so a
 * tour link left inside one comes back with the page's next write, after the
 * tour has ended.
 */
export const PAGE_SEARCH_KEYS = ['workSearch', 'streamSearch', 'runSearch'] as const;

/** `search` (`?a=b`, or '') with `link` set in it, or with any tour taken out for `null`. */
function withLink(search: string, link: TourLink | null): string {
  const params = new URLSearchParams(search);
  if (link) {
    params.set('tour', link.tour);
    params.set('step', String(link.step));
  } else {
    params.delete('tour');
    params.delete('step');
  }
  const query = params.toString();
  return query === '' ? '' : `?${query}`;
}

/**
 * The address and state that put `link` in force — or take any tour out of
 * them, for `null` — keeping every other query key and state key as it is.
 * `here` is the address bar as it stands (`location.href`), not `page.url`,
 * which a page's own shallow writes leave behind.
 *
 * The link is set, or taken out, inside every key of `PAGE_SEARCH_KEYS` as
 * well, and a key the state does not hold yet is seeded from `here`: the page
 * under the tour builds its next write from that key, so the write carries the
 * step in force while the tour is open and no tour once it has ended.
 */
export function writeTourLink(
  state: App.PageState,
  here: URL,
  link: TourLink | null
): { url: URL; state: App.PageState } {
  const url = new URL(here);
  url.search = withLink(here.search, link);
  const next: App.PageState = { ...state, tour: link?.tour ?? null, tourStep: link?.step ?? null };
  for (const key of PAGE_SEARCH_KEYS) next[key] = withLink(state[key] ?? here.search, link);
  return { url, state: next };
}

/** True when `url` carries a tour link, in either of its two keys. */
export function carriesTourLink(url: URL): boolean {
  return url.searchParams.has('tour') || url.searchParams.has('step');
}
