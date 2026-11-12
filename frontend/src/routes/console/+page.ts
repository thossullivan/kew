import { redirect } from '@sveltejs/kit';

export const ssr = false;

/**
 * `/console` was the legacy judgment console: the whole web UI at `/` until
 * FUI-26 moved it here, then a fallback while the factory screens grew a home
 * for each of its actions. Every one has a home now — holds on Monitor, caps on
 * Budget, the orphan acknowledge and the issue queue on Work (#3453) — so the
 * page is gone (#3454) and its URL forwards to the view it was for: what needs
 * you, on Work.
 *
 * A loader redirect, like `/dashboard` and `/recorder`: the query is dropped on
 * purpose (the console's `?filter=` values name filters Work does not have),
 * and outside the `(factory)` group so the forward draws no shell first.
 */
export function load(): never {
  throw redirect(308, '/work?filter=needs-you');
}
