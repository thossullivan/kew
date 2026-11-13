/**
 * The public demo's two addresses, written once.
 *
 * Every demo link ships DEMO_URL, the start page, in its HTML. Once the page's
 * script runs in a desktop window, `tourOnDesktop` points the link at
 * DEMO_TOUR_URL, the "needs you" tour the docs link. A tour never opens below
 * 1024px (frontend/src/lib/demo/tours/controller.ts), so a phone, or a visitor
 * without script, lands on the start page. Plain JavaScript so the browser
 * check imports the same values the pages render.
 */
export const DEMO_URL = 'https://demo.kew.dev/';

export const DEMO_TOUR_URL = 'https://demo.kew.dev/work?tour=needs-you&step=1';

/** The tour's own floor, the query its controller opens a tour under. */
export const TOUR_DESKTOP = '(min-width: 64rem)';

/**
 * A Svelte action for a link whose HTML says DEMO_URL: on mount, a window at
 * the tour's floor or wider gets the tour instead.
 *
 * @param {HTMLAnchorElement} link
 */
export function tourOnDesktop(link) {
  if (window.matchMedia(TOUR_DESKTOP).matches) link.href = DEMO_TOUR_URL;
}
