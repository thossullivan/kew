import { ANCHORS, verifyAnchors } from '$lib/data/pack';

/**
 * The anchor hashes the evidence section draws, computed at build time (the
 * page is prerendered), so the HTML a reader gets, with or without script,
 * already holds the real rows and the real count. The section never prints
 * "chain intact" over hashes it has not computed and compared (design review
 * c9, C1). The browser recomputes them on mount.
 *
 * A server load, so it runs once, at build time: the prerendered HTML carries
 * the result and the browser never has to re-run it before hydrating (a
 * universal load would, and would fail the whole page where Web Crypto is
 * unavailable, as on plain http to a LAN address).
 */
export async function load() {
  return { anchors: await verifyAnchors(ANCHORS, null) };
}
