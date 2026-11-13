/**
 * What the page foot knows about the bundled build history before the file
 * arrives (#3144): `virtual:kew-demo-provenance-meta`, which only the demo
 * build's Vite plugin (`demo/provenance-bundle.ts`) can resolve. One module
 * reads it, so a test of the page foot can stand its own in for the bundle.
 *
 * - `PROVENANCE_ROUTES`: the routes the shipped file names.
 * - `PROVENANCE_SOURCE`: which file shipped, as the plugin chose it; a
 *   sample build says "sample data" on the foot's line. Either way the PR
 *   numbers are text, never links: the real PRs are in kew's private
 *   development repository, and the samples' are on an invented owner's
 *   repository, which is someone else's URL space.
 */
import meta from 'virtual:kew-demo-provenance-meta';

export const PROVENANCE_ROUTES: readonly string[] = meta.routes;
export const PROVENANCE_SOURCE: 'real' | 'sample' = meta.source;
