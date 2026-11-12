/**
 * The one gate a served URL passes to become an `href` (#3470 C10).
 *
 * Moved here from `$lib/work/markdown`, where it gated a tracker body's links,
 * because a URL the server serves — an issue's, a pull request's — is the same
 * kind of input: whatever the forge or the ledger recorded. A `javascript:`
 * URL in a `pr_url` is a script on a click, and a relative one resolves
 * against kew's own origin, a claim nobody made.
 *
 * Whitespace and control characters are stripped before the test — `java\0script:`
 * and a tab-prefixed scheme are both the same attack — and what is left has to
 * be an absolute http(s) URL with something after the slashes. Every other
 * target, including a relative path or a `#` anchor, is refused (null); a
 * caller prints a refused URL as text, never as a link.
 */
export function safeHref(url: string | null | undefined): string | null {
  const cleaned = (url ?? '').trim().replace(/[\u0000-\u001f\u007f]/g, '');
  return /^https?:\/\/\S+$/i.test(cleaned) ? cleaned : null;
}

/**
 * `safeHref`, or a path inside kew's own app.
 *
 * A link the server names into the console — a verify section, say — is a
 * root-relative path, which `safeHref` refuses by design. This lets that one
 * shape through as well: a single leading `/` followed by something other than
 * a second slash or a backslash (both of which browsers read as another host).
 */
export function safeAppHref(url: string | null | undefined): string | null {
  const cleaned = (url ?? '').trim().replace(/[\u0000-\u001f\u007f]/g, '');
  return /^\/(?![/\\])\S*$/.test(cleaned) ? cleaned : safeHref(cleaned);
}
