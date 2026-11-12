/**
 * Design C1 — the description panel's markdown subset.
 *
 * Two claims are checked here, and they pull in opposite directions:
 *
 * 1. **The subset renders.** Every form `markdown.ts` claims — headings,
 *    paragraphs, both list kinds with one nesting level, fences, quotes, GFM
 *    tables, rules, and the five inline forms — turns into tokens, so a body
 *    written on a tracker reads as the person wrote it.
 * 2. **Nothing outside the subset is a renderer.** A form this tokenizer does
 *    not know stays literal text in a paragraph — never dropped, never guessed
 *    at — and a URL whose scheme is not http(s) never becomes an `href`. The
 *    panel emits text nodes only (`no {@html` below), so the scheme gate is the
 *    one place an issue body could still reach the browser as behaviour.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { ItemDescription } from '$lib/api';
import Description from './Description.svelte';
import { parseInline, parseMarkdown, safeHref, type Block, type Inline } from './markdown';

/** The one block a body of `text` produces. */
function only(text: string): Block {
  const blocks = parseMarkdown(text);
  expect(blocks).toHaveLength(1);
  return blocks[0];
}

/** Every text-bearing token in a block tree, flattened, in written order. */
function words(parts: Inline[]): string {
  return parts.map((part) => part.text).join('');
}

/** The panel's markup for a body a read landed, comments and tag gaps gone. */
function draw(body: string): string {
  const description: ItemDescription = {
    item_id: '1177',
    availability: 'available',
    provenance: 'observed',
    observed_at: '2026-09-24T18:11:58+00:00',
    title: 'a body with markup in it',
    url: 'https://github.com/thossullivan/kew/issues/1177',
    description: body,
    expected: '',
    actual: '',
    reason: '',
    composed_at: '2026-09-24T18:12:04+00:00'
  };
  return render(Description, { props: { description, notice: '' } })
    .body.replace(/<!--[\s\S]*?-->/g, '')
    .replace(/>\s+</g, '><');
}

describe('blocks', () => {
  it('reads the three heading levels, and nothing deeper', () => {
    expect(only('# one')).toEqual({ kind: 'heading', level: 1, inline: [{ kind: 'text', text: 'one' }] });
    expect(only('## two')).toMatchObject({ kind: 'heading', level: 2 });
    expect(only('### three')).toMatchObject({ kind: 'heading', level: 3 });
    // Four hashes is not a heading in this subset, so it is the text it is.
    expect(only('#### four')).toEqual({
      kind: 'paragraph',
      inline: [{ kind: 'text', text: '#### four' }]
    });
    // A hash with no space after it is a tag or an issue reference, not a head.
    expect(only('#1177 is the one')).toMatchObject({ kind: 'paragraph' });
  });

  it('splits paragraphs on blank lines and keeps the newlines inside one', () => {
    const blocks = parseMarkdown('one\ntwo\n\n\nthree');
    expect(blocks).toHaveLength(2);
    expect(blocks[0]).toMatchObject({ kind: 'paragraph', inline: [{ text: 'one\ntwo' }] });
    expect(blocks[1]).toMatchObject({ kind: 'paragraph', inline: [{ text: 'three' }] });
    expect(parseMarkdown('   \n\n  ')).toEqual([]);
  });

  it('reads bullet and numbered lists, with one level of nesting', () => {
    expect(only('- one\n* two\n+ three')).toEqual({
      kind: 'list',
      ordered: false,
      start: 1,
      items: [
        { inline: [{ kind: 'text', text: 'one' }], sub: null },
        { inline: [{ kind: 'text', text: 'two' }], sub: null },
        { inline: [{ kind: 'text', text: 'three' }], sub: null }
      ]
    });
    expect(only('1. first\n2) second')).toMatchObject({
      kind: 'list',
      ordered: true,
      items: [{ inline: [{ text: 'first' }] }, { inline: [{ text: 'second' }] }]
    });
    // A nested line hangs off the item above it; a THIRD level folds into the
    // same sub-list rather than disappearing.
    const nested = only('- top\n  - under\n      - deeper\n- next');
    expect(nested).toMatchObject({
      kind: 'list',
      ordered: false,
      items: [
        { inline: [{ text: 'top' }], sub: { ordered: false, items: [{ inline: [{ text: 'under' }] }, { inline: [{ text: 'deeper' }] }] } },
        { inline: [{ text: 'next' }], sub: null }
      ]
    });
    // A change of kind at the top level starts a new list, as GFM does.
    const two = parseMarkdown('- bullet\n1. number');
    expect(two.map((block) => block.kind)).toEqual(['list', 'list']);
  });

  it('keeps the ordinal a numbered list started at, so no step is renumbered', () => {
    // The first item's number is the list's own start, as GFM reads it; the
    // rest count up from there whatever they were written as.
    expect(only('3. third\n4. fourth')).toMatchObject({ ordered: true, start: 3 });
    expect(only('7) seven\n9) eight')).toMatchObject({ ordered: true, start: 7 });
    expect(only('1. one\n2. two')).toMatchObject({ ordered: true, start: 1 });
    // A bullet list has no ordinal, so its start is the 1 it renders at.
    expect(only('- one\n- two')).toMatchObject({ ordered: false, start: 1 });
    // The shape the fence test blesses: a procedure a fenced block splits in
    // two. The second half is its own list, and it begins at step 2 — read as
    // a start of 1 it would show the reader a step number nobody wrote.
    const split = parseMarkdown('1. run it:\n   ```\n   kew tui\n   ```\n2. read it');
    expect(split.map((block) => block.kind)).toEqual(['list', 'code', 'list']);
    expect(split[0]).toMatchObject({ kind: 'list', start: 1 });
    expect(split[2]).toMatchObject({ kind: 'list', start: 2 });
    // A nested numbered list keeps its own first ordinal too.
    expect(only('- top\n  4. four\n  5. five')).toMatchObject({
      ordered: false,
      start: 1,
      items: [{ sub: { ordered: true, start: 4 } }]
    });
  });

  it('keeps a list written at an indent flat, nesting only past its own first item', () => {
    // Nesting is relative to the list's first item, so a body that indented the
    // whole list is three items — not item one with two children under it.
    expect(only('  - one\n  - two\n  - three')).toMatchObject({
      ordered: false,
      items: [{ sub: null }, { sub: null }, { sub: null }]
    });
    // The case that reads worst when it folds: a numbered procedure, whose
    // sub-list would restart the count at 1 under step one.
    const steps = only('   1. clone the repo\n   2. install deps\n   3. run the tests');
    expect(steps).toMatchObject({ kind: 'list', ordered: true });
    const { items } = steps as Extract<Block, { kind: 'list' }>;
    expect(items.map((item) => words(item.inline))).toEqual([
      'clone the repo',
      'install deps',
      'run the tests'
    ]);
    expect(items.every((item) => item.sub === null)).toBe(true);
    // An item indented past that first one is still the nested one it was.
    expect(only('  - top\n    - under')).toMatchObject({
      items: [{ inline: [{ text: 'top' }], sub: { items: [{ inline: [{ text: 'under' }] }] } }]
    });
  });

  it('reads a fenced code block with its info string, verbatim', () => {
    expect(only('```python\nif x:\n    pass\n\n    # still code\n```')).toEqual({
      kind: 'code',
      lang: 'python',
      text: 'if x:\n    pass\n\n    # still code'
    });
    expect(only('~~~\nplain\n~~~')).toEqual({ kind: 'code', lang: '', text: 'plain' });
    // Inline markup inside a fence is text: a fence is not parsed at all.
    expect(only('```\n**not bold** `not code`\n```')).toMatchObject({
      text: '**not bold** `not code`'
    });
  });

  it('runs an unclosed fence to the end of the body', () => {
    expect(only('```\nopen\n## not a heading\nstill code')).toEqual({
      kind: 'code',
      lang: '',
      text: 'open\n## not a heading\nstill code'
    });
  });

  it('reads a fence written at an indent, less the indent it was written at', () => {
    // A fence under a list item is indented. Read as prose it would lose its
    // own backtick markers to a code span and carry raw newlines into a
    // paragraph, so it stays the code block it is.
    expect(only('  ```sh\n  kew doctor\n  ```')).toEqual({
      kind: 'code',
      lang: 'sh',
      text: 'kew doctor'
    });
    // Only the opener's own indent comes off: the body keeps its own shape.
    expect(only('  ```\n  if x:\n      pass\n  ```')).toMatchObject({
      text: 'if x:\n    pass'
    });
    const under = parseMarkdown('1. run it:\n   ```\n   kew tui\n   ```\n2. read it');
    expect(under.map((block) => block.kind)).toEqual(['list', 'code', 'list']);
    expect(under[1]).toMatchObject({ text: 'kew tui' });
  });

  it('reads a block quote as its own paragraphs', () => {
    expect(only('> first line\n> same paragraph\n>\n> second')).toEqual({
      kind: 'quote',
      paragraphs: [[{ kind: 'text', text: 'first line\nsame paragraph' }], [{ kind: 'text', text: 'second' }]]
    });
  });

  it('reads a GFM table, and pads a ragged row instead of dropping it', () => {
    const table = only(
      ['| gate | state |', '| --- | :---: |', '| tests | green |', '| lint |', '| build | red | extra |'].join('\n')
    );
    expect(table).toMatchObject({ kind: 'table' });
    const { head, rows } = table as Extract<Block, { kind: 'table' }>;
    expect(head.map(words)).toEqual(['gate', 'state']);
    expect(rows.map((row) => row.map(words))).toEqual([
      ['tests', 'green'],
      // The short row keeps its column, with an empty cell beside it.
      ['lint', ''],
      // The long one keeps every cell it was written with.
      ['build', 'red', 'extra']
    ]);
  });

  it('keeps a pipe table with no delimiter row as the text it is', () => {
    expect(only('| gate | state |\n| tests | green |')).toMatchObject({ kind: 'paragraph' });
  });

  it('reads a horizontal rule, and not a list or a table', () => {
    for (const rule of ['---', '***', '___', '--------', '---  ']) {
      expect(only(rule), rule).toEqual({ kind: 'rule' });
    }
  });

  it('never drops a line it does not understand', () => {
    const odd = '  <details><summary>log</summary>\n\n| ragged\n\n    indented block';
    const text = parseMarkdown(odd)
      .map((block) => ('inline' in block ? words(block.inline) : ''))
      .join('\n');
    expect(text).toContain('<details><summary>log</summary>');
    expect(text).toContain('| ragged');
    expect(text).toContain('indented block');
  });
});

describe('inline', () => {
  it('reads code spans, bold, em and links', () => {
    expect(parseInline('a `b` c')).toEqual([
      { kind: 'text', text: 'a ' },
      { kind: 'code', text: 'b' },
      { kind: 'text', text: ' c' }
    ]);
    expect(parseInline('``a ` b``')).toEqual([{ kind: 'code', text: 'a ` b' }]);
    expect(parseInline('**bold**')).toEqual([{ kind: 'strong', text: 'bold' }]);
    expect(parseInline('*em*')).toEqual([{ kind: 'em', text: 'em' }]);
    expect(parseInline('[kew](https://kew.dev/docs)')).toEqual([
      { kind: 'link', text: 'kew', href: 'https://kew.dev/docs' }
    ]);
  });

  it('links a bare URL and leaves the sentence`s punctuation out of it', () => {
    expect(parseInline('see https://kew.dev/docs.')).toEqual([
      { kind: 'text', text: 'see ' },
      { kind: 'link', text: 'https://kew.dev/docs', href: 'https://kew.dev/docs' },
      { kind: 'text', text: '.' }
    ]);
  });

  it('keeps markup inside a code span literal', () => {
    expect(parseInline('`**x** [y](https://z.example)`')).toEqual([
      { kind: 'code', text: '**x** [y](https://z.example)' }
    ]);
  });

  it('leaves a lone star, a product and an identifier alone', () => {
    for (const text of ['2 * 3 * 4', 'a_b_c', 'half **of', 'a * b']) {
      expect(parseInline(text), text).toEqual([{ kind: 'text', text }]);
    }
  });

  it('closes bold at its own marker, never at the next one in the paragraph', () => {
    // A close tried after the longer form over-reached: `**x**` could not end
    // at its own `**`, so it ran to the next `**` anywhere in the paragraph and
    // bolded the words between, printing the inner markers as characters.
    expect(parseInline('**x** and **y**')).toEqual([
      { kind: 'strong', text: 'x' },
      { kind: 'text', text: ' and ' },
      { kind: 'strong', text: 'y' }
    ]);
    expect(parseInline('**a** b **c** d **e**')).toEqual([
      { kind: 'strong', text: 'a' },
      { kind: 'text', text: ' b ' },
      { kind: 'strong', text: 'c' },
      { kind: 'text', text: ' d ' },
      { kind: 'strong', text: 'e' }
    ]);
    // Longer bold, a single star inside it, and bold across the lines of its
    // own paragraph all still read as the body wrote them.
    expect(parseInline('**two words** and **more**')).toEqual([
      { kind: 'strong', text: 'two words' },
      { kind: 'text', text: ' and ' },
      { kind: 'strong', text: 'more' }
    ]);
    expect(parseInline('**a*b**')).toEqual([{ kind: 'strong', text: 'a*b' }]);
    expect(parseInline('**two\nlines**')).toEqual([{ kind: 'strong', text: 'two\nlines' }]);
    // A `**` with no close of its own is the characters it was written with.
    expect(parseInline('**a ** b')).toEqual([{ kind: 'text', text: '**a ** b' }]);
  });

  it('reads an unbalanced bracket before a link the way GFM does', () => {
    // A link text stops at a `[`, so the live anchor is on the inner brackets
    // and the stray `[a` in front of them is the text it is.
    expect(parseInline('[a[b](https://kew.dev)')).toEqual([
      { kind: 'text', text: '[a' },
      { kind: 'link', text: 'b', href: 'https://kew.dev' }
    ]);
    // A linked image is two forms the subset does not have, so all of it is
    // characters — except the destination, which is a URL in its own right.
    expect(parseInline('[![alt](https://z.example/x.png)](https://kew.dev)')).toEqual([
      { kind: 'text', text: '[' },
      { kind: 'text', text: '![alt](https://z.example/x.png)' },
      { kind: 'text', text: '](' },
      { kind: 'link', text: 'https://kew.dev', href: 'https://kew.dev' },
      { kind: 'text', text: ')' }
    ]);
  });

  it('reads a body of unterminated markers at the cap without stalling', () => {
    // Every run in `INLINE` stops at the character that opens the same form
    // again. Open-ended ones re-read the rest of the body from every `![`,
    // every `[` and every `**` in it: this body, at GitHub's 65,535-character
    // cap, took the rendering thread for about a second, and the cost grew with
    // the square of its length. It now parses in about a millisecond, so the
    // budget here is 250 times what the work costs — loose enough for a loaded
    // CI box, and still well under the second the open-ended reading took.
    const share = Math.floor(65535 / 4);
    const body = ['![a](', '![', '[a', '**a ']
      .map((seed) => seed.repeat(Math.floor(share / seed.length)))
      .join('');
    const started = performance.now();
    const parts = parseInline(body);
    expect(performance.now() - started).toBeLessThan(250);
    // Nothing in it is a form, so none of it is lost either.
    expect(parts.map((part) => ('text' in part ? part.text : '')).join('')).toBe(body);
  });

  it('keeps an image as the text it was written as, never as a link', () => {
    // `![alt](url)` is outside the subset. The `!` belongs to the form, so none
    // of it is left for the link form to turn into an anchor on the asset.
    expect(parseInline('![shot](https://kew.dev/shot.png)')).toEqual([
      { kind: 'text', text: '![shot](https://kew.dev/shot.png)' }
    ]);
    expect(parseInline('see ![a](https://z.example/x.png) here')).toEqual([
      { kind: 'text', text: 'see ' },
      { kind: 'text', text: '![a](https://z.example/x.png)' },
      { kind: 'text', text: ' here' }
    ]);
    // A real link beside an image is still the link it is.
    expect(parseInline('![a](https://z.example/x.png) [b](https://kew.dev)')).toEqual([
      { kind: 'text', text: '![a](https://z.example/x.png)' },
      { kind: 'text', text: ' ' },
      { kind: 'link', text: 'b', href: 'https://kew.dev' }
    ]);
  });

  it('keeps an image with a title, or brackets in its alt, as text too', () => {
    // The shapes a narrower image form missed, each of which fell through to
    // the bare-URL rule and put a live anchor on the asset's own address.
    for (const image of [
      '![shot](https://evil.example/x.png "a title")',
      "![shot](https://evil.example/x.png 'a title')",
      '![a [b] c](https://evil.example/x.png)',
      '![](https://evil.example/x.png "t")',
      '![alt text](https://evil.example/x.png?a=1&b=2 "t")',
      // A target balances one `(…)` group, which is what a wiki asset's own
      // address looks like.
      '![a](https://evil.example/Foo_(bar).png)',
      '![a](https://evil.example/Foo_(bar).png "t")'
    ]) {
      expect(parseInline(image), image).toEqual([{ kind: 'text', text: image }]);
    }
    // A titled image does not eat the link that follows it.
    expect(parseInline('![a](https://z.example/x.png "t") then [b](https://kew.dev)')).toEqual([
      { kind: 'text', text: '![a](https://z.example/x.png "t")' },
      { kind: 'text', text: ' then ' },
      { kind: 'link', text: 'b', href: 'https://kew.dev' }
    ]);
    // An exclamation that is not an image marker leaves the link alone.
    expect(parseInline('nice! [b](https://kew.dev)')).toEqual([
      { kind: 'text', text: 'nice! ' },
      { kind: 'link', text: 'b', href: 'https://kew.dev' }
    ]);
  });

  it('never lets an unfinished image claim the next link on the line', () => {
    // The alt text balances its brackets, so it cannot run to the `](` of
    // somebody else's link. An alt open to the next `](` anywhere on the line
    // read all of this as one image and rendered the whole thing as characters
    // — the anchor the body wrote, swallowed by a stray `]` in front of it.
    expect(parseInline('![a] and [b](https://kew.dev)')).toEqual([
      { kind: 'text', text: '![a] and ' },
      { kind: 'link', text: 'b', href: 'https://kew.dev' }
    ]);
    // The same shape without the `!`, which is the plainer way to write it.
    expect(parseInline('a] and [b](https://kew.dev)')).toEqual([
      { kind: 'text', text: 'a] and ' },
      { kind: 'link', text: 'b', href: 'https://kew.dev' }
    ]);
    // A bracket in prose on either side of a real link costs it nothing.
    expect(parseInline('[1] see [docs](https://kew.dev/docs) ]')).toEqual([
      { kind: 'text', text: '[1] see ' },
      { kind: 'link', text: 'docs', href: 'https://kew.dev/docs' },
      { kind: 'text', text: ' ]' }
    ]);
  });

  it('refuses every scheme but http and https', () => {
    for (const url of [
      'javascript:alert(1)',
      'JavaScript:alert(1)',
      ' javascript:alert(1)',
      'data:text/html,<script>x</script>',
      'vbscript:x',
      'file:///etc/passwd',
      '//evil.example/x',
      '/relative',
      '#anchor',
      'https://'
    ]) {
      expect(safeHref(url), url).toBeNull();
    }
    expect(safeHref('https://kew.dev')).toBe('https://kew.dev');
    expect(safeHref('  http://kew.dev/x  ')).toBe('http://kew.dev/x');
    // A control character cannot be smuggled through the gate as a scheme.
    expect(safeHref('java\u0000script:alert(1)')).toBeNull();
  });

  it('keeps a link whose scheme is refused as the text it was written as', () => {
    // Parentheses in the target put it outside the link form altogether, so
    // the whole thing is one run of text.
    expect(parseInline('[click](javascript:alert(1))')).toEqual([
      { kind: 'text', text: '[click](javascript:alert(1))' }
    ]);
    expect(parseInline('[boom](data:text/html,x)')).toEqual([
      { kind: 'text', text: '[boom](data:text/html,x)' }
    ]);
  });

  it('shows the URL for a link that named no text', () => {
    expect(parseInline('[](https://kew.dev)')).toEqual([
      { kind: 'link', text: 'https://kew.dev', href: 'https://kew.dev' }
    ]);
  });
});

/**
 * A body shaped like #1177's: the one on `/work?item=1177` that sent this panel
 * to a renderer, with the forms that read as noise when they are printed raw.
 */
const BODY_1177 = [
  '## Problem',
  '',
  'The Work page prints `description` raw, so a body like this one reads as noise.',
  '',
  '| surface | renders markdown |',
  '| --- | --- |',
  '| TUI | yes |',
  '| Work | no |',
  '',
  '### Change',
  '',
  '- a tokenizer in `markdown.ts`',
  '- **never** `{@html}`',
  '',
  'See https://github.com/thossullivan/kew/issues/1177 for the original.'
].join('\n');

describe('the panel', () => {
  it('renders the #1177 body with no literal hash head or table pipe left', () => {
    const html = draw(BODY_1177);
    // The panel's own head is the h2, so a body `##` is an h4 under it.
    expect(html).toContain('>Problem</h4>');
    // A wide table scrolls in its own box, so the page never scrolls sideways.
    expect(html).toContain('overflow-x-auto"><table');
    expect(html).toContain('<code');
    expect(html).toContain('<strong');
    // The forms that used to print as themselves are gone from the TEXT: no
    // `##` heading marker, and no table pipe anywhere in the rendered body.
    const text = html.replace(/<[^>]*>/g, '');
    expect(text).not.toContain('##');
    expect(text).not.toContain('|');
    expect(text).not.toContain('](');
    // The link is an anchor that cannot reach back into this tab.
    expect(html).toContain('href="https://github.com/thossullivan/kew/issues/1177"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).toContain('target="_blank"');
  });

  it('renders HTML in a body as the characters it is, never as markup', () => {
    const html = draw('<img src=x onerror="alert(1)"> and <b>bold</b>\n\n<script>alert(2)</script>');
    expect(html).not.toContain('<img');
    expect(html).not.toContain('<b>');
    expect(html).not.toContain('<script>');
    // Svelte escapes the character that could OPEN a tag; a bare `>` in a text
    // node is itself, which is valid HTML and cannot start anything.
    expect(html).toContain('&lt;img src=x onerror=');
    expect(html).toContain('&lt;script>alert(2)&lt;/script>');
  });

  it('renders an image as the characters it is, with no anchor to the asset', () => {
    const html = draw('![boom](https://evil.example/shot.png)');
    expect(html).not.toContain('<a ');
    expect(html).not.toContain('href=');
    expect(html).toContain('![boom](https://evil.example/shot.png)');
  });

  it('renders a titled image as characters as well, with no anchor to the asset', () => {
    const html = draw('![boom](https://evil.example/shot.png "a title")');
    expect(html).not.toContain('<a ');
    expect(html).not.toContain('href=');
    expect(html).toContain('a title');
  });

  it('numbers a procedure from the step a body wrote, not from 1', () => {
    // The `<ol>` carries the body's own first ordinal, so the second half of a
    // procedure a fence split in two reads as steps 2 and 3.
    const html = draw('1. run it:\n   ```\n   kew tui\n   ```\n2. read it\n3. file it');
    expect(html).toContain('start="2"');
    // The half that does start at 1 says nothing, which is what `<ol>` means.
    expect(html).not.toContain('start="1"');
    // A bullet list is never numbered, so it never carries a start.
    expect(draw('- one\n- two')).not.toContain('start=');
  });

  it('refuses a javascript: link in a body, and keeps the words', () => {
    const html = draw('[click me](javascript:alert(1))');
    expect(html).not.toContain('javascript:alert(1)"');
    expect(html).not.toContain('href="javascript');
    expect(html).toContain('click me');
  });

  it('scrolls a long code line inside its own block', () => {
    const html = draw('```\n' + 'x'.repeat(400) + '\n```');
    expect(html).toContain('overflow-x-auto');
  });

  it('uses no {@html} anywhere in the Work surface', () => {
    // The whole safety claim of this panel: every piece of issue text is a text
    // node. One `{@html}` in this directory, in any component, would undo it.
    const dir = fileURLToPath(new URL('.', import.meta.url));
    const components = readdirSync(dir).filter((name) => name.endsWith('.svelte'));
    expect(components.length).toBeGreaterThan(0);
    for (const name of components) {
      expect(readFileSync(new URL(name, import.meta.url), 'utf8'), name).not.toContain('{@html');
    }
  });
});
