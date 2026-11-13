// What the demo console says on screen (#3139). Plain JS, like `sentinel.js`,
// so the Node-side checks import the same strings the app draws: the demo
// browser check reads the banner against it, and `npm run ui-check` fails when
// either is found under `build/` — demo words in the normal build mean demo
// code reached it, whatever module they came in through.

/**
 * What the demo banner says, on every page of the demo build: that the data
 * is invented, and what kew is, for a visitor who arrives on a phone with no
 * tour to read it from (design review c9, D17).
 */
export const DEMO_BANNER = 'Demo: synthetic data. kew runs coding agents headless; you make the merge calls.';

/** The banner's way into the tours, at every width. */
export const TAKE_A_TOUR = 'Take a tour';

/** What the tours' entries say where the tours do not run, below 1024px. */
export const TOURS_DESKTOP_ONLY = 'Guided tours run on a desktop screen.';

/** How every refused write's sentence starts (`writes.ts`). */
export const DEMO_REFUSAL = 'Demo: this would';
