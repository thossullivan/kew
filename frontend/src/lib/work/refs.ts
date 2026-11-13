/**
 * The two addresses a Work surface composes, and nothing else.
 *
 * They were written in `./header`, which is where the first caller needed
 * them. They were never header facts: five modules outside it compose a link
 * with one or the other, and two of those — `./entry-detail` and
 * `./evidence`, the two halves of the one needs-you block — were importing
 * them off the header while the header is the thing that reads the block
 * (#2280 D10). Keeping them there would have closed an import cycle around
 * that read, so they sit under the header rather than inside it and the
 * layering runs one way.
 *
 * Both are pure readings of a string the server served. Neither invents a URL:
 * the epic ref is encoded, never parsed, and the PR ref is the last path
 * segment of the URL the projection already carried.
 */

/** The epic page for a ref. The ref is opaque — encoded, never parsed. */
export function epicHref(ref: string): string {
  return `/epics/${encodeURIComponent(ref)}`;
}

/**
 * The PR's display ref, from the last path segment of its URL. Shared with
 * the epic rail's call to action, which names the same pull request.
 */
export function prRef(url: string): string {
  const tail = url.split(/[?#]/)[0].replace(/\/+$/, '').split('/').pop() ?? '';
  return /^[0-9]+$/.test(tail) ? `#${tail}` : 'open';
}
