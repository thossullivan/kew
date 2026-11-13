/**
 * Turn the console into the demo console (#3139).
 *
 * `src/hooks.client.ts` calls this from `init`, behind `__KEW_DEMO__`, before
 * the first `load` runs — and SvelteKit reads `window.fetch` at call time
 * inside `load`, so from then on every `/api` request the app makes is
 * answered by the bundled fixture set and nothing leaves the origin. It also
 * quiets the live streams, fills the shell's banner slot, its header slot
 * (the Tours entry, #3145) and its page foot (how each surface was built,
 * #3144), and stamps the document with `DEMO_SENTINEL`.
 *
 * This is the one module that imports the bundled set, and the only demo
 * module the app reaches: the normal build drops the import that leads here.
 */
import fixtureSet from 'virtual:kew-demo-fixtures';
import { chromeSlots } from '$lib/shell/chrome-slots.svelte';
import BuildHistory from './BuildHistory.svelte';
import DemoBanner from './DemoBanner.svelte';
import { quietEventSource } from './event-source';
import { demoApi, demoFetch, rebasedSet } from './respond';
import { DEMO_SENTINEL } from './sentinel.js';
import TourEntry from './tours/TourEntry.svelte';

export function installDemo(win: Window & typeof globalThis = window): void {
  const origin = win.location.origin;
  const api = demoApi(rebasedSet(fixtureSet, Date.now()));
  win.fetch = demoFetch(api, origin, win.fetch.bind(win));
  win.EventSource = quietEventSource(win.EventSource, origin);
  win.document.documentElement.dataset.kewDemo = DEMO_SENTINEL;
  chromeSlots.banner = DemoBanner;
  chromeSlots.header = TourEntry;
  chromeSlots.foot = BuildHistory;
}
