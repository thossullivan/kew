import { redirect } from '@sveltejs/kit';

export const ssr = false;

/**
 * `/dashboard` was the console's home before Group 6 promoted the console to
 * `/`; FUI-26 then moved the console to `/console` (removed since, #3454) and
 * made the factory's Work tab the way in. So this path forwards to `/work`, which is where `/` sends a
 * visitor too — an old bookmark lands on the factory, not on a page that
 * forwards again.
 *
 * A loader redirect rather than the fragment-preserving replace `/` and `/inbox`
 * use: no printed link ever carried a token on `/dashboard`, and those two are
 * the entry points sign-in depends on.
 */
export function load(): never {
  throw redirect(308, '/work');
}
