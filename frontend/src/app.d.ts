// See https://svelte.dev/docs/kit/types#app.d.ts
// for information about these interfaces
declare global {
	/** `true` only in the demo build (`demo/vite.demo.config.ts`, #3139). */
	const __KEW_DEMO__: boolean;
	namespace App {
		// interface Error {}
		// interface Locals {}
		// interface PageData {}
		interface PageState {
			/** The epic page's selected node, as its last `replaceState` wrote
			 * it: shallow routing does not update `page.url`. */
			node?: string | null;
			/**
			 * The epic page's canvas view — `graph` or `timeline` (#2271) — as its
			 * last `replaceState` wrote it. Written together with `node` rather
			 * than on its own: `replaceState` REPLACES the page state, so a write
			 * naming one of the two would drop the other.
			 */
			epicView?: 'graph' | 'timeline';
			/**
			 * The epic graph's lineage selection (`+#2469+`, #2544) as its last
			 * `replaceState` wrote it. Written with `node` and `epicView` for
			 * the same reason: a write naming one would drop the others.
			 */
			select?: string | null;
			/**
			 * The epic page's replay as its last `replaceState` wrote it (#3142):
			 * the mode, the moment (`?t=`, whole seconds since the first frame,
			 * null in live) and the playback speed. Written with the three above,
			 * so a node click never drops the moment.
			 */
			epicMode?: 'live' | 'replay';
			replayT?: number | null;
			replaySpeed?: 1 | 10 | 60;
			/**
			 * The Monitor event stream's search string, as it last wrote it with
			 * `replaceState`, which does not move `page.url` (#2248).
			 */
			streamSearch?: string;
			/** The Work page's search string (`?item=`, `?filter=`), as it last
			 * wrote it: shallow routing does not update `page.url`. */
			workSearch?: string;
			/**
			 * The run page's search string (`?q=`, `?focusedTime=`,
			 * `?attempt=`), as its last `replaceState` wrote it (#2300). Same
			 * reason as the two above: shallow routing never moves `page.url`.
			 */
			runSearch?: string;
			/** The Prompts page's selected role (`?role=`), as its last
			 * `replaceState` wrote it (#3019). */
			promptRole?: string | null;
			/**
			 * The demo build's open tour and its 1-based step (`?tour=`,
			 * `?step=`, #3145), as its last `replaceState` wrote them; `null`
			 * once it ended. Merged into the state the page already holds,
			 * never written in place of it.
			 */
			tour?: string | null;
			tourStep?: number | null;
		}
		// interface Platform {}
	}
}

export {};
