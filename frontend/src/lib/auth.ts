import { login, fetchSession } from '$lib/api';

/** Pull `token=…` out of a URL fragment (never query string). Pure. */
export function parseTokenFromHash(hash: string): string | null {
	const m = /[#&]token=([^&]+)/.exec(hash);
	return m ? decodeURIComponent(m[1]) : null;
}

/**
 * If the URL fragment carries `#token=…`, exchange it via login() for a
 * session cookie then strip the fragment from history. Otherwise report
 * whether an existing session is live.
 */
export async function pickUpSession(f: typeof fetch = fetch): Promise<boolean> {
	const token = parseTokenFromHash(window.location.hash);
	if (token) {
		try {
			await login(token, f);
			return true;
		} catch {
			return false;
		} finally {
			// Strip only the fragment — the query string must survive so
			// redirect targets like /work?filter=needs-you keep their filter
			// (#714 review finding).
			//
			// The state written back is the state that was there (#2278 C2).
			// `replaceState` replaces the current history entry wholesale, and
			// what lives on that entry is SvelteKit's own bookkeeping: the
			// history index and page state its client router navigates by.
			// Passing `null` wiped it, and the back button stopped navigating —
			// on the one page every signed-in visitor lands on, because this is
			// where `#token=…` is spent. The raw history API is deliberate here
			// rather than `$app/navigation`'s `replaceState`: this runs on
			// arrival, before the router is necessarily ready to be told
			// anything, and the only thing being changed is the URL's fragment.
			window.history.replaceState(
				window.history.state,
				'',
				window.location.pathname + window.location.search
			);
		}
	}
	try {
		return (await fetchSession(f)).authenticated;
	} catch {
		return false;
	}
}

/** The factory's home tab: where `/` and `/dashboard` send a visitor (FUI-26). */
const FACTORY_HOME = '/work';

/**
 * Where `/` should land now that the factory is the UI (FUI-26): the Work tab,
 * carrying the query string and the fragment across untouched.
 *
 * Both matter. The fragment is the `#token=…` the `kew serve` banner prints at
 * the root, which the root layout exchanges for a session on arrival, and a
 * redirect that dropped it would break sign-in. The query is what an old
 * `/?filter=…` bookmark says, and the one filter the console and Work share is
 * renamed: the console's approvals view is Work's needs-you filter. Query before
 * fragment, per URL grammar (`path?query#fragment`). Pure.
 */
export function factoryHomeHref(
	loc: { search: string; hash: string } = window.location
): string {
	const params = new URLSearchParams(loc.search);
	if (params.get('filter') === 'approvals') params.set('filter', 'needs-you');
	const query = params.toString();
	return FACTORY_HOME + (query ? '?' + query : '') + (loc.hash || '');
}

/**
 * Where a legacy `/inbox` link should land. The inbox is no longer a page, and
 * after the cutover (FUI-26) it is no longer the console's approvals view
 * either: the judgment surface is the Work tab filtered to what needs a human.
 * Preserves any `#token=…` fragment so a stale `/inbox#token=…` link still signs
 * the visitor in, and the root layout picks the token up. Query before fragment,
 * per URL grammar (`path?query#fragment`). Pure.
 */
export function legacyInboxHref(loc: { hash: string } = window.location): string {
	return FACTORY_HOME + '?filter=needs-you' + (loc.hash || '');
}

/**
 * What `/inbox` does for the visitor standing on it: the href to replace the
 * document with, or `null` to stay there and teach sign-in.
 *
 * Two different arrivals reach that path. One is a bookmark of the original
 * approval inbox, which redirects. The other is every 401 in the app: the
 * factory screens all send a lapsed session to `/inbox` (the shell's shared
 * poll, the epic and run loaders, the budget and config guards), and after the
 * cutover `legacyInboxHref` points at a URL inside the `(factory)` group — whose
 * poll signs out on its own 401. Redirecting a signed-out visitor would bounce
 * straight back here and reload the document forever, with no way to sign in
 * between bounces. So a signed-out arrival rests, and the page draws the
 * sign-in card: this is the one screen in the app that being signed out has a
 * reason to sit on.
 *
 * A `#token=…` outranks the session reading rather than waiting on it, because
 * it IS a sign-in in flight: the session it will create does not exist yet, and
 * the fragment has to travel to the root layout's `pickUpSession` on the page
 * that keeps it. Pure.
 */
export function inboxLanding(loc: { hash: string }, authenticated: boolean): string | null {
	if (parseTokenFromHash(loc.hash) !== null || authenticated) return legacyInboxHref(loc);
	return null;
}
