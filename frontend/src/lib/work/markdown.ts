/**
 * Design C1 — the markdown subset the description panel renders, as tokens.
 *
 * An issue body is the one string on this console a stranger wrote. So this is
 * a tokenizer, not a renderer: it produces plain data, and `Description.svelte`
 * turns that data into Svelte elements, every piece of text a text node. There
 * is no HTML anywhere in this pipeline — no `{@html}`, no sanitiser to get
 * wrong, nothing that could parse `<img onerror=…>` into a node — which is why
 * the subset can be small without being unsafe.
 *
 * Two rules decide everything below:
 *
 * 1. **The subset is honest about its size.** Headings `#`–`###`, paragraphs,
 *    bullet and numbered lists with ONE level of nesting, fenced code, block
 *    quotes, GFM tables and horizontal rules; inline code spans, `**bold**`,
 *    `*em*`, `[text](url)` and bare http(s) URLs. That is all of it.
 * 2. **Anything else is the text it is.** A form this file does not know stays
 *    literal in a paragraph — a `<details>` wrapper, an `![image](url)`, an
 *    unfenced indented block, a four-hash heading, a pipe row with no delimiter
 *    under it. Nothing is dropped, because a body that silently loses a line is
 *    worse than one that shows a marker the reader can see for themselves.
 *
 * The one place a body could still become behaviour rather than text is a link
 * target, so `safeHref` is the gate: only `http:` and `https:` survive it, and a
 * link it refuses keeps the words it was written with.
 */
import { safeHref } from '$lib/safe-href';

/** A run of inline content. Flat by design: inline markup does not nest here. */
export type Inline =
  | { kind: 'text'; text: string }
  | { kind: 'code'; text: string }
  | { kind: 'strong'; text: string }
  | { kind: 'em'; text: string }
  | { kind: 'link'; text: string; href: string };

/**
 * A list and its items. `sub` is the one nesting level the subset allows.
 *
 * `start` is the ordinal the list's FIRST item was written with — 1 for a
 * bullet list, and for a numbered one that began at 1. It is kept because a
 * procedure does not always start at one: a body that numbered its steps from
 * 3, or whose steps were split in two by a fenced block, is renumbered from 1
 * without it, and a step number nobody wrote is worse than no markup at all.
 * Only the first item's ordinal is read, as GFM does — the rest count up.
 */
export type MarkdownList = { ordered: boolean; start: number; items: ListItem[] };
export type ListItem = { inline: Inline[]; sub: MarkdownList | null };

export type Block =
  | { kind: 'heading'; level: number; inline: Inline[] }
  | { kind: 'paragraph'; inline: Inline[] }
  | { kind: 'code'; lang: string; text: string }
  | ({ kind: 'list' } & MarkdownList)
  | { kind: 'quote'; paragraphs: Inline[][] }
  | { kind: 'table'; head: Inline[][]; rows: Inline[][][] }
  | { kind: 'rule' };

/** A block and the line the scanner resumes on. */
type Taken = { block: Block; next: number };

/**
 * A fence opener, and the indent it was written at. An indented fence is still
 * a fence: one under a list item is how a tracker body shows code inside a
 * step, and reading it as prose would consume its own backtick markers.
 */
const FENCE = /^([ \t]*)(`{3,}|~{3,})[ \t]*(.*)$/;
const HEADING = /^(#{1,3})[ \t]+(.+?)[ \t]*$/;
const RULE = /^(?:-{3,}|\*{3,}|_{3,})[ \t]*$/;
const BULLET = /^([ \t]*)[-*+][ \t]+(.*)$/;
const ORDERED = /^([ \t]*)(\d{1,9})[.)][ \t]+(.*)$/;
const QUOTE = /^[ \t]*>[ \t]?(.*)$/;
/** One cell of a GFM delimiter row: dashes, with either alignment colon. */
const DELIMITER = /^:?-+:?$/;
/**
 * An indent this far past the list's OWN first item makes an item a nested one.
 * Measured against the first item rather than column zero, so a list a body
 * wrote at an indent — a numbered procedure inside a quote block, say — stays
 * the flat list it is instead of folding every sibling under item one.
 */
const NESTED_AT = 2;

/**
 * The inline forms, in the order they win. Code spans come first, so markup
 * inside backticks stays literal; the image form comes before the link form, so
 * `![alt](url)` is claimed whole and kept literal rather than read as a `!` in
 * front of a live link; links come before bare URLs, so a target is read once.
 * The groups are positional — see `inlineToken`.
 *
 * The image form is wider than the link form it shadows in the one direction
 * that costs nothing: its target takes the spaces and quotes a title is written
 * with, because a link's target has to survive `safeHref` to become an `href`
 * and an image's never does — so `![a](u "title")` is claimed here before the
 * bare-URL rule can turn the asset's address into a live anchor on it. Parens
 * inside it balance one `(…)` group deep, which is what an asset address off a
 * wiki page looks like (`![a](https://z.example/Foo_(bar).png)`).
 *
 * Its alt text is NOT open either. Brackets inside it have to balance, one `[…]`
 * group deep, which is what a body writes (`![a [b] c](u)`). An alt that ran to
 * the next `](` anywhere on the line would make this rule claim somebody else's
 * link instead: `![a] and [b](https://kew.dev)` is a stray `]` in prose
 * followed by a real link, and reading it as one image swallows that anchor.
 *
 * Nothing in here is open-ended, and that is a time bound, not a taste. A
 * target that ran to the next `)` anywhere on the line — or a link text to the
 * next `]` — re-read the rest of the body from every `![` and every `[` in it,
 * so a 64KB body of unterminated markers held the rendering thread for about a
 * second, quadratic in the body's length. Each run now stops at the character
 * that opens the same form again, which costs a failed match the distance to
 * the next marker instead of the whole body, and reads an unbalanced `[` the
 * way GFM does: `[a[b](u)` is the text `[a` and a link on `b`.
 *
 * `**` closes at the NEAREST `**` for the same reason: its content cannot cross
 * a `**` of its own, so `**x** and **y**` is two bold words instead of one bold
 * run from the first marker to the last with the inner ones printed as
 * characters. A single `*` inside is still content (`**a*b**`), and a `**` that
 * never closes is the characters it was written with.
 *
 *  1/2 code span ticks and body · 3 whole image · 4/5 link text and target ·
 *  6 bold · 7 em · 8 bare URL
 */
const INLINE =
  /(`+)([\s\S]*?)\1|(!\[(?:[^[\]\n]|\[[^[\]\n]*\])*\]\((?:[^()\n]|\([^()\n]*\))*\))|\[([^[\]\n]*)\]\(([^()\s]*)\)|\*\*([^\s*]|[^\s*](?:[^*]|\*(?!\*))*?[^\s*])\*\*|\*([^\s*][^*\n]*?[^\s*]|[^\s*])\*|(https?:\/\/[^\s<>()[\]]+)/g;

/** Trailing characters a sentence ends with, never part of the URL. */
const URL_TAIL = /[.,;:!?'")\]}]+$/;

/**
 * The only link targets that become an `href`: an absolute http(s) URL. The
 * gate is shared with every served URL the console links (`$lib/safe-href`);
 * this panel renders a tracker's words, and a link that resolves against
 * kew's own origin would be a claim nobody made.
 */
export { safeHref };

/** One inline token from a match of `INLINE`, or the literal text it was. */
function inlineToken(match: RegExpMatchArray): Inline[] {
  const [whole, ticks, code, image, linkText, target, strong, em, bare] = match;
  // A code span's own line endings are spaces, as GFM reads them: a newline
  // inside a span would draw a break in a `pre-line` paragraph nobody wrote.
  if (ticks !== undefined) return [{ kind: 'code', text: code.replace(/\n/g, ' ') }];
  // An image is outside the subset, so it is the text it was written as.
  if (image !== undefined) return [{ kind: 'text', text: image }];
  if (linkText !== undefined) {
    const href = safeHref(target);
    if (href === null) return [{ kind: 'text', text: whole }];
    return [{ kind: 'link', text: linkText === '' ? href : linkText, href }];
  }
  if (strong !== undefined) return [{ kind: 'strong', text: strong }];
  if (em !== undefined) return [{ kind: 'em', text: em }];
  const url = bare.replace(URL_TAIL, '');
  const tail = bare.slice(url.length);
  const href = safeHref(url);
  if (href === null) return [{ kind: 'text', text: bare }];
  const link: Inline = { kind: 'link', text: url, href };
  return tail === '' ? [link] : [link, { kind: 'text', text: tail }];
}

/** Inline content, in the order it was written. Everything else is text. */
export function parseInline(text: string): Inline[] {
  const parts: Inline[] = [];
  let at = 0;
  for (const match of text.matchAll(INLINE)) {
    const index = match.index ?? 0;
    if (index > at) parts.push({ kind: 'text', text: text.slice(at, index) });
    parts.push(...inlineToken(match));
    at = index + match[0].length;
  }
  if (at < text.length) parts.push({ kind: 'text', text: text.slice(at) });
  return parts;
}

/** True once a fence of the same character is at least as long as the opener. */
function closesFence(line: string, bars: string): boolean {
  const trimmed = line.trim();
  return trimmed.length >= bars.length && [...trimmed].every((char) => char === bars[0]);
}

/** A fence body line, less the opening fence's own indent. */
function undent(line: string, width: number): string {
  let at = 0;
  while (at < width && (line[at] === ' ' || line[at] === '\t')) at += 1;
  return line.slice(at);
}

function takeFence(lines: string[], start: number): Taken | null {
  const opener = FENCE.exec(lines[start]);
  if (opener === null) return null;
  const [, indent, bars, info] = opener;
  const body: string[] = [];
  let at = start + 1;
  while (at < lines.length && !closesFence(lines[at], bars)) {
    body.push(undent(lines[at], indent.length));
    at += 1;
  }
  // An unclosed fence runs to the end of the body: the alternative is to decide
  // it was never a fence, which re-reads a whole code listing as prose.
  return {
    block: { kind: 'code', lang: info.trim(), text: body.join('\n') },
    next: Math.min(at + 1, lines.length)
  };
}

function takeHeading(lines: string[], start: number): Taken | null {
  const head = HEADING.exec(lines[start]);
  if (head === null) return null;
  return {
    block: { kind: 'heading', level: head[1].length, inline: parseInline(head[2]) },
    next: start + 1
  };
}

function takeRule(lines: string[], start: number): Taken | null {
  if (!RULE.test(lines[start])) return null;
  return { block: { kind: 'rule' }, next: start + 1 };
}

function takeQuote(lines: string[], start: number): Taken | null {
  if (!QUOTE.test(lines[start])) return null;
  const inner: string[] = [];
  let at = start;
  while (at < lines.length) {
    const quoted = QUOTE.exec(lines[at]);
    if (quoted === null) break;
    inner.push(quoted[1]);
    at += 1;
  }
  const paragraphs = inner
    .join('\n')
    .split(/\n[ \t]*\n/)
    .map((part) => part.trim())
    .filter((part) => part !== '')
    .map(parseInline);
  return { block: { kind: 'quote', paragraphs }, next: at };
}

/** A row's cells, outer pipes dropped. `'| a | b |'` is `['a', 'b']`. */
function cells(line: string): string[] {
  let text = line.trim();
  if (text.startsWith('|')) text = text.slice(1);
  if (text.endsWith('|')) text = text.slice(0, -1);
  return text.split('|').map((cell) => cell.trim());
}

/** A body row, padded to the head's width. Extra cells are kept, never cut. */
function tableRow(line: string, width: number): Inline[][] {
  const row = cells(line);
  while (row.length < width) row.push('');
  return row.map(parseInline);
}

function takeTable(lines: string[], start: number): Taken | null {
  if (start + 1 >= lines.length || !lines[start].includes('|')) return null;
  const head = cells(lines[start]);
  const delimiter = cells(lines[start + 1]);
  // GFM's rule: the delimiter row has to match the head's width exactly. A
  // single-column table also needs a pipe in the delimiter, or `---` under any
  // sentence containing a pipe would stop being the rule it is.
  if (delimiter.length !== head.length || !delimiter.every((cell) => DELIMITER.test(cell)))
    return null;
  if (head.length < 2 && !lines[start + 1].includes('|')) return null;
  const rows: Inline[][][] = [];
  let at = start + 2;
  while (at < lines.length && lines[at].trim() !== '' && lines[at].includes('|')) {
    rows.push(tableRow(lines[at], head.length));
    at += 1;
  }
  return { block: { kind: 'table', head: head.map(parseInline), rows }, next: at };
}

/**
 * A list line: its indent in columns, its kind, the ordinal it was numbered
 * with (1 for a bullet, which has none) and the text after the marker.
 */
function listLine(
  line: string
): { indent: number; ordered: boolean; ordinal: number; text: string } | null {
  if (RULE.test(line)) return null;
  const bullet = BULLET.exec(line);
  if (bullet !== null)
    return {
      indent: bullet[1].replace(/\t/g, '    ').length,
      ordered: false,
      ordinal: 1,
      text: bullet[2]
    };
  const ordered = ORDERED.exec(line);
  if (ordered === null) return null;
  return {
    indent: ordered[1].replace(/\t/g, '    ').length,
    ordered: true,
    ordinal: Number(ordered[2]),
    text: ordered[3]
  };
}

function takeList(lines: string[], start: number): Taken | null {
  const first = listLine(lines[start]);
  if (first === null) return null;
  const items: ListItem[] = [];
  let at = start;
  while (at < lines.length) {
    const line = listLine(lines[at]);
    if (line === null) break;
    const nested = line.indent >= first.indent + NESTED_AT && items.length > 0;
    // A change of kind at the top level is a new list, as GFM reads it.
    if (!nested && line.ordered !== first.ordered) break;
    if (nested) {
      // Deeper indents fold into this one sub-list rather than vanishing: the
      // subset is one level, and the text still has to be readable.
      const parent = items[items.length - 1];
      const sub = parent.sub ?? { ordered: line.ordered, start: line.ordinal, items: [] };
      sub.items.push({ inline: parseInline(line.text), sub: null });
      parent.sub = sub;
    } else {
      items.push({ inline: parseInline(line.text), sub: null });
    }
    at += 1;
  }
  return { block: { kind: 'list', ordered: first.ordered, start: first.ordinal, items }, next: at };
}

/** Every block form but the paragraph, in the order a line is tried against. */
const TAKERS = [takeFence, takeHeading, takeRule, takeQuote, takeTable, takeList];

function startsBlock(lines: string[], at: number): boolean {
  return TAKERS.some((take) => take(lines, at) !== null);
}

function takeParagraph(lines: string[], start: number): Taken {
  const text: string[] = [lines[start]];
  let at = start + 1;
  while (at < lines.length && lines[at].trim() !== '' && !startsBlock(lines, at)) {
    text.push(lines[at]);
    at += 1;
  }
  return { block: { kind: 'paragraph', inline: parseInline(text.join('\n')) }, next: at };
}

/**
 * The body as blocks. Blank lines separate them and are themselves nothing; a
 * line no other form claims joins the paragraph being read.
 */
export function parseMarkdown(body: string): Block[] {
  const lines = body.replace(/\r\n?/g, '\n').split('\n');
  const blocks: Block[] = [];
  let at = 0;
  while (at < lines.length) {
    if (lines[at].trim() === '') {
      at += 1;
      continue;
    }
    let taken: Taken | null = null;
    for (const take of TAKERS) {
      taken = take(lines, at);
      if (taken !== null) break;
    }
    const { block, next } = taken ?? takeParagraph(lines, at);
    blocks.push(block);
    // Every taker consumes at least the line it was handed, so this terminates.
    at = Math.max(next, at + 1);
  }
  return blocks;
}
