/**
 * #3052 N4 — read a server render's command blocks as the text they hold.
 *
 * The shared `Command` draws each word of a command in its own span, so a line
 * can break only between words (never at an id's hyphen). The block's text is
 * still the command exactly, but its markup is no longer one run of text, so a
 * server-render test that looks for `<code …>kew epic unhold ui-ux 2219</code>`
 * reads the markup through this first: every `code[data-command]` keeps its
 * own tag and attributes and loses only the tags and hydration comments inside
 * it. Everything outside a command block is left as rendered.
 */
export function plainCommands(html: string): string {
  return html.replace(
    /(<code\b[^>]*\bdata-command(?=[\s=>])[^>]*>)([\s\S]*?)(<\/code>)/g,
    (_, open: string, inner: string, close: string) => open + inner.replace(/<[^>]*>/g, '') + close
  );
}
