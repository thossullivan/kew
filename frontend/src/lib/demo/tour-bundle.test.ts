/**
 * The tour extractor (`demo/tour-bundle.ts`, #3145): a step's words come out
 * of a docs passage, and a tour that cannot find its words fails the build.
 */
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterAll, describe, expect, it } from 'vitest';
import {
  bundleTours,
  docsHref,
  excludeDocs,
  extractPassages,
  headingAnchors,
  MAX_STEP_WORDS,
  parseTour,
  passageWords,
  publishedPages,
  readTours,
  type TourSpec
} from '../../../demo/tour-bundle';

const TOURS = new URL('../../../demo/tours/', import.meta.url);
const DOCS = new URL('../../../../docs/', import.meta.url);
const MKDOCS = fileURLToPath(new URL('../../../../mkdocs.yml', import.meta.url));

function passage(id: string, title = 'A step', body = 'What it shows.'): string {
  return [`<!-- demo-step: ${id} -->`, `**${title}**`, '', body, `<!-- /demo-step -->`].join('\n');
}

function tour(id: string, steps: number, docs = 'web/index.md'): TourSpec {
  return {
    id,
    title: `The ${id} tour`,
    start: '/work',
    steps: Array.from({ length: steps }, (_, i) => ({
      route: '/work',
      target: 'work-list',
      docs,
      anchor: 'web-ui',
      passage: `${id}/${i + 1}`
    }))
  };
}

describe('extractPassages', () => {
  it('reads the title line and each body paragraph, with code spans and bold kept apart', () => {
    const markdown = [
      '# Page',
      '<!-- demo-step: t/1 -->',
      '**The list**',
      '',
      'Every issue, with `kew dispatch` one',
      'press away.',
      '',
      'A **second** paragraph, [linked](../x.md).',
      '<!-- /demo-step -->'
    ].join('\n');
    expect(extractPassages(markdown, 'web/index.md')).toEqual([
      {
        id: 't/1',
        file: 'web/index.md',
        line: 2,
        title: 'The list',
        body: [
          [{ text: 'Every issue, with ' }, { text: 'kew dispatch', code: true }, { text: ' one press away.' }],
          [{ text: 'A ' }, { text: 'second', strong: true }, { text: ' paragraph, ' }, { text: 'linked' }, { text: '.' }]
        ]
      }
    ]);
  });

  it.each([
    ['an unclosed passage', '<!-- demo-step: t/1 -->\n**T**\n\nBody.', /never closed/],
    ['a stray close', 'Body.\n<!-- /demo-step -->', /closes no passage/],
    ['a nested open', '<!-- demo-step: t/1 -->\n<!-- demo-step: t/2 -->', /opens inside t\/1/],
    ['a step numbered from 0', passage('t/0'), /numbered from 1/],
    ['a marker that does not parse', '<!-- demo-step t/1 -->', /does not parse/],
    ['no title line', '<!-- demo-step: t/1 -->\nBody.\n<!-- /demo-step -->', /\*\*<step title>\*\*/],
    ['no body', '<!-- demo-step: t/1 -->\n**T**\n<!-- /demo-step -->', /no body paragraph/]
  ])('refuses %s', (_, markdown, error) => {
    expect(() => extractPassages(markdown, 'p.md')).toThrow(error);
  });

  it('reads no marker inside a fenced code block: a page may show the syntax', () => {
    const markdown = [
      'Mark a step like this:',
      '```markdown',
      '<!-- demo-step: t/1 -->',
      '**A title**',
      '<!-- /demo-step -->',
      '```',
      '~~~',
      '<!-- demo-step t/2 -->',
      '~~~',
      passage('t/3')
    ].join('\n');
    expect(extractPassages(markdown, 'p.md').map((p) => p.id)).toEqual(['t/3']);
  });

  it('refuses a fenced code block inside a passage', () => {
    const markdown = ['<!-- demo-step: t/1 -->', '**T**', '', '```', 'kew dispatch', '```', '<!-- /demo-step -->'].join('\n');
    expect(() => extractPassages(markdown, 'p.md')).toThrow(/demo-step t\/1.*fenced code block/);
  });

  it.each([
    ['an HTML tag', 'Press <kbd>Esc</kbd> to end it.', /demo-step t\/1.*HTML/],
    ['a line break tag', 'One line<br>and another.', /demo-step t\/1.*HTML/],
    ['an HTML entity', 'Plans &amp; runs.', /demo-step t\/1.*entity/],
    ['a numeric entity', 'A &#8212; dash.', /demo-step t\/1.*entity/],
    ['*emphasis*', 'It is *only* a demo.', /demo-step t\/1.*emphasis/],
    ['_emphasis_', 'It is _only_ a demo.', /demo-step t\/1.*emphasis/],
    ['__strong__', 'It is __only__ a demo.', /demo-step t\/1.*emphasis/]
  ])('refuses %s in a body, which the popover would show as literal text', (_, body, error) => {
    expect(() => extractPassages(passage('t/1', 'A step', body), 'p.md')).toThrow(error);
  });

  it('refuses markup in a title, which the popover shows as plain text', () => {
    expect(() => extractPassages(passage('t/1', 'The `kew` list'), 'p.md')).toThrow(/demo-step t\/1.*title/);
  });

  it('keeps what reads as text: a snake_case name, a lone asterisk, markup inside a code span', () => {
    const [read] = extractPassages(passage('t/1', 'A step', 'Read run_id, 2 * 3, and `<kbd>&amp;*x*</kbd>`.'), 'p.md');
    expect(read.body).toEqual([
      [{ text: 'Read run_id, 2 * 3, and ' }, { text: '<kbd>&amp;*x*</kbd>', code: true }, { text: '.' }]
    ]);
  });
});

describe('publishedPages', () => {
  const root = mkdtempSync(join(tmpdir(), 'kew-tour-docs-'));
  const write = (path: string, text: string) => {
    mkdirSync(dirname(join(root, path)), { recursive: true });
    writeFileSync(join(root, path), text);
  };
  write('docs/web/index.md', '# Web UI\n');
  write('docs/audit/pack.md', passage('t/1'));
  write('docs/vhs/tape.md', passage('t/1'));
  write('docs/brand-guidelines.md', passage('t/1'));
  write('docs/web/brand-guidelines.md', '# Kept\n');
  write('docs/.drafts/x.md', passage('t/1'));
  write('docs/templates/x.md', passage('t/1'));
  afterAll(() => rmSync(root, { recursive: true, force: true }));

  it("reads only the pages mkdocs builds: exclude_docs and mkdocs's own defaults are skipped", () => {
    const pages = publishedPages(join(root, 'docs'), ['/audit/**', '/vhs/**', '/brand-guidelines.md']);
    expect(Object.keys(pages).sort()).toEqual(['web/brand-guidelines.md', 'web/index.md']);
  });

  it("reads exclude_docs out of mkdocs.yml, and the committed one excludes the audit packs", () => {
    const yml = ['site_name: x', 'exclude_docs: |', '  # internal', '  /audit/**', '', '  /vhs/**', 'nav:', '  - a.md'].join('\n');
    expect(excludeDocs(yml)).toEqual(['/audit/**', '/vhs/**']);
    expect(excludeDocs(readFileSync(MKDOCS, 'utf8'))).toContain('/audit/**');
  });
});

describe('headingAnchors and docsHref', () => {
  it('anchors on the mkdocs slug or an explicit id, and skips fenced code', () => {
    const anchors = headingAnchors(
      ['# Web UI', '## Why the list agrees with `kew epic status`', '### [sandbox] {#sandbox}', '```', '# not a heading', '```'].join('\n')
    );
    expect([...anchors]).toEqual(['web-ui', 'why-the-list-agrees-with-kew-epic-status', 'sandbox']);
  });

  it('links a page the way mkdocs publishes it', () => {
    expect(docsHref('web/index.md', 'cockpit-shell')).toBe('https://kew.dev/docs/web/#cockpit-shell');
    expect(docsHref('cli/serve.md', 'flags')).toBe('https://kew.dev/docs/cli/serve/#flags');
    expect(docsHref('index.md', 'top')).toBe('https://kew.dev/docs/#top');
  });
});

describe('parseTour', () => {
  it('accepts a well-formed tour', () => {
    expect(parseTour(tour('t', 2), 't.json')).toEqual(tour('t', 2));
  });

  it("keeps a step's query, the moment a replay step opens on", () => {
    const spec = tour('t', 1);
    const withQuery = { ...spec, steps: [{ ...spec.steps[0], query: { mode: 'replay', t: '3900' } }] };
    expect(parseTour(withQuery, 't.json').steps[0].query).toEqual({ mode: 'replay', t: '3900' });
  });

  it('keeps the graph card a step names and its fit, and leaves both out when the step names neither', () => {
    const spec = tour('t', 2);
    const named = {
      ...spec,
      steps: [{ ...spec.steps[0], card: '107', query: { node: '107' }, clear: 'epic-graph' }, { ...spec.steps[1], fit: true }]
    };
    const [first, second] = parseTour(named, 't.json').steps;
    expect([first.card, first.clear, second.fit]).toEqual(['107', 'epic-graph', true]);
    expect('card' in second || 'fit' in first || 'clear' in second).toBe(false);
  });

  it('refuses a step that opens the replay with no moment, naming the step', () => {
    // A replay with no `t` plays, and a step must open on one frame: a page
    // told the same address again would otherwise pause a replay mid-play.
    const spec = tour('t', 2);
    const playing = { ...spec, steps: [spec.steps[0], { ...spec.steps[1], query: { mode: 'replay' } }] };
    expect(() => parseTour(playing, 't.json')).toThrow(/step 2's "query" sets "mode": "replay" with no "t"/);
  });

  it.each([
    ['an id that is not its file name', { ...tour('t', 1), id: 'u' }, /not its file name/],
    ['no steps', { ...tour('t', 1), steps: [] }, /at least one step/],
    [
      'a CSS selector for a target',
      { ...tour('t', 1), steps: [{ ...tour('t', 1).steps[0], target: '.work-list' }] },
      /data-tour attribute/
    ],
    [
      'a passage out of order',
      { ...tour('t', 2), steps: [tour('t', 2).steps[1], tour('t', 2).steps[0]] },
      /step 1's "passage" must be "t\/1"/
    ],
    ['a start that is not step 1', { ...tour('t', 1), start: '/monitor' }, /start/],
    [
      "a query that sets the tour's own link",
      { ...tour('t', 1), steps: [{ ...tour('t', 1).steps[0], query: { mode: 'replay', step: '2' } }] },
      /"step", which is the tour's own link/
    ],
    [
      'a query value that is not plain',
      { ...tour('t', 1), steps: [{ ...tour('t', 1).steps[0], query: { t: '0&tour=x' } }] },
      /"t" is not a plain value/
    ],
    ['an empty query', { ...tour('t', 1), steps: [{ ...tour('t', 1).steps[0], query: {} }] }, /names no key/],
    [
      'a card that is a selector, not a node id',
      { ...tour('t', 1), steps: [{ ...tour('t', 1).steps[0], card: '.card' }] },
      /step 1's "card" must be a node id/
    ],
    ['a fit that is not true', { ...tour('t', 1), steps: [{ ...tour('t', 1).steps[0], fit: 'yes' }] }, /"fit" is true or left out/],
    [
      'a card step that fits the whole graph, which draws the card too small to read',
      { ...tour('t', 1), steps: [{ ...tour('t', 1).steps[0], card: '107', query: { node: '107' }, fit: true }] },
      /names a card and fits the whole graph/
    ],
    [
      'a card step that does not select its card',
      { ...tour('t', 1), steps: [{ ...tour('t', 1).steps[0], card: '107', query: { node: '101' } }] },
      /does not select it/
    ],
    ['a clear region that is a selector', { ...tour('t', 1), steps: [{ ...tour('t', 1).steps[0], clear: '.dag' }] }, /"clear" must be/]
  ])('refuses %s', (_, value, error) => {
    expect(() => parseTour(value, 't.json')).toThrow(error);
  });
});

describe('bundleTours', () => {
  const page = `# Web UI\n\n${passage('t/1', 'One', 'First.')}\n\n${passage('t/2', 'Two', 'Second.')}\n`;

  it("fills each step's title, body and link from its passage", () => {
    const [bundled] = bundleTours([tour('t', 2)], { 'web/index.md': page });
    expect(bundled.steps.map((s) => [s.title, s.body, s.href])).toEqual([
      ['One', [[{ text: 'First.' }]], 'https://kew.dev/docs/web/#web-ui'],
      ['Two', [[{ text: 'Second.' }]], 'https://kew.dev/docs/web/#web-ui']
    ]);
  });

  it('fails on a step with no passage', () => {
    expect(() => bundleTours([tour('t', 3)], { 'web/index.md': page })).toThrow(/no <!-- demo-step: t\/3 -->/);
  });

  it('fails on a passage marked twice, naming both places', () => {
    expect(() =>
      bundleTours([tour('t', 2)], { 'web/index.md': page, 'web/epics.md': passage('t/1') })
    ).toThrow(/t\/1 is marked twice: docs\/web\/epics.md:1 and docs\/web\/index.md:3/);
  });

  it('fails on a passage no step reads', () => {
    expect(() => bundleTours([tour('t', 1)], { 'web/index.md': page })).toThrow(/no tour step reads: t\/2/);
  });

  it('fails on a passage on another page than its link, and on a missing anchor', () => {
    expect(() => bundleTours([tour('t', 2, 'web/epics.md')], { 'web/index.md': page, 'web/epics.md': '# Epics' })).toThrow(
      /is in docs\/web\/index.md, but its step links to docs\/web\/epics.md/
    );
    const noAnchor = page.replace('# Web UI', '# Elsewhere');
    expect(() => bundleTours([tour('t', 2)], { 'web/index.md': noAnchor })).toThrow(/has no #web-ui/);
  });

  it(`holds a popover to ${MAX_STEP_WORDS} words, title and body together, a code span's words counted`, () => {
    const words = (n: number) => Array.from({ length: n }, (_, i) => `w${i}`).join(' ');
    // Two title words and a code span of two: 36 + 2 + 2 is the cap exactly.
    const atCap = `# Web UI\n\n${passage('t/1', 'Two words', `${words(36)} \`kew verify\`.`)}\n`;
    expect(() => bundleTours([tour('t', 1)], { 'web/index.md': atCap })).not.toThrow();
    const over = `# Web UI\n\n${passage('t/1', 'Two words', `${words(37)} \`kew verify\`.`)}\n`;
    expect(() => bundleTours([tour('t', 1)], { 'web/index.md': over })).toThrow(
      new RegExp(`demo-step t/1 \\(docs/web/index.md:3\\) is 41 words with its title; a tour popover holds at most ${MAX_STEP_WORDS}`)
    );
  });

  it('counts the words a popover shows: a code span and the punctuation after it are one word', () => {
    expect(passageWords({ title: 'Sent back', body: [[{ text: 'returned ' }, { text: 'NEEDS_CHANGES', code: true }, { text: ': findings.' }]] })).toBe(
      5
    );
  });
});

describe('the committed tours', () => {
  it('every tour under demo/tours/ finds its words in docs/', () => {
    const tours = readTours(TOURS, DOCS);
    expect(tours.length).toBeGreaterThan(0);
    for (const t of tours) {
      for (const step of t.steps) {
        expect(step.title, `${step.passage} has a title`).not.toBe('');
        expect(step.href.startsWith('https://kew.dev/docs/'), step.href).toBe(true);
      }
    }
  });
});
