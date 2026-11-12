/**
 * The demo tours, bundled (#3145).
 *
 * A tour is one JSON file under `demo/tours/<id>.json`: its id, its title, the
 * route it starts on and its steps. A step names its route, the
 * `data-tour="..."` attribute it highlights, the docs page and anchor it links
 * to, and its passage id — and nothing it says: every word a step shows is a
 * passage in `docs/`, written as normal docs text between two comments:
 *
 *     <!-- demo-step: <tour>/<n> -->
 *     **<step title>**
 *
 *     <body paragraph(s)>
 *     <!-- /demo-step -->
 *
 * mkdocs drops the comments and shows the rest as it shows any paragraph, so
 * the docs and the tour cannot say two different things. This plugin reads
 * both at build time and serves `virtual:kew-demo-tours`: every tour with each
 * step's title, body and docs link filled in from its passage.
 *
 * A wrong tour fails the build here rather than a visitor's tour later: a step
 * with no passage, a passage marked twice, a passage no step reads, a marker
 * that does not parse, a step whose passage is on another page than the one it
 * links to, or an anchor that page does not have.
 *
 * Only the demo config registers this plugin, so the normal build never
 * resolves the virtual module.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Plugin } from 'vite';
import type { BundledTour, Segment, TourSpec, TourStepSpec } from '../src/lib/demo/tours/types';

export type { BundledTour, Segment, TourSpec, TourStepSpec };

export const DEMO_TOURS_ID = 'virtual:kew-demo-tours';
const RESOLVED = `\0${DEMO_TOURS_ID}`;

/** Where the published docs live; a step's link is this plus its page. */
export const DOCS_SITE = 'https://kew.dev/docs/';

/** A passage as the docs hold it, and where. */
export type Passage = { id: string; file: string; line: number; title: string; body: Segment[][] };

const OPEN = /^<!--\s*demo-step:\s*(\S+)\s*-->$/;
const CLOSE = /^<!--\s*\/demo-step\s*-->$/;
const PASSAGE_ID = /^([a-z0-9][a-z0-9-]*)\/([1-9]\d*)$/;
const TITLE = /^\*\*(.+)\*\*$/;
const SLUG = /^[a-z0-9][a-z0-9-]*$/;
const FENCE = /^\s*(```|~~~)/;

/**
 * Markdown the popover cannot draw. A step's words are text nodes, `<code>`
 * and `<strong>`, never HTML, so anything else would reach the visitor as its
 * own source text: refused at build time instead.
 */
const UNRENDERED: ReadonlyArray<[RegExp, string]> = [
  [/<\/?[A-Za-z][^>]*>/, 'an HTML tag'],
  [/&(?:#\d+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);/, 'an HTML entity'],
  [/(?:^|[^\w*])([*_]{1,2})(?=\S)(?:.*?\S)?\1(?![\w*])/, 'emphasis other than **bold**']
];

/** Refuse `text` if it holds markup the popover would show as literal text. */
function refuseUnrendered(text: string): void {
  for (const [pattern, what] of UNRENDERED) {
    const found = pattern.exec(text);
    if (found) {
      throw new Error(
        `${what} ("${found[0].trim()}"), which the tour popover would show as literal text; ` +
          'use plain words, a `code span` or **bold**'
      );
    }
  }
}

/**
 * A passage's paragraph as runs: `code` spans and **bold**, a link's text
 * only. Throws on markup no run can carry (`UNRENDERED`); a code span's
 * contents are literal, as mkdocs shows them.
 */
export function segments(paragraph: string): Segment[] {
  const out: Segment[] = [];
  const pattern = /`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\([^)]*\)/g;
  let at = 0;
  for (const match of paragraph.matchAll(pattern)) {
    if (match.index > at) out.push({ text: paragraph.slice(at, match.index) });
    if (match[1] !== undefined) out.push({ text: match[1], code: true });
    else if (match[2] !== undefined) out.push({ text: match[2], strong: true });
    else out.push({ text: match[3] });
    at = match.index + match[0].length;
  }
  if (at < paragraph.length) out.push({ text: paragraph.slice(at) });
  for (const run of out) if (!run.code) refuseUnrendered(run.text);
  return out;
}

/** A step title: plain words, as the popover's title shows it. */
function plainTitle(title: string): string {
  const runs = segments(title);
  if (runs.length !== 1 || runs[0].code || runs[0].strong || runs[0].text !== title) {
    throw new Error('its title holds markup; a step title is plain words');
  }
  return title;
}

/** The title line and body paragraphs between one passage's markers. */
function passageText(lines: string[], where: string, id: string): { title: string; body: Segment[][] } {
  const content = [...lines];
  while (content.length > 0 && content[0].trim() === '') content.shift();
  const title = TITLE.exec((content.shift() ?? '').trim());
  if (!title) throw new Error(`${where}: a demo-step passage starts with one **<step title>** line`);
  const paragraphs: string[][] = [[]];
  for (const line of content) {
    if (line.trim() === '') paragraphs.push([]);
    else paragraphs[paragraphs.length - 1].push(line.trim());
  }
  try {
    const body = paragraphs.filter((p) => p.length > 0).map((p) => segments(p.join(' ')));
    if (body.length === 0) throw new Error('a demo-step passage has no body paragraph');
    return { title: plainTitle(title[1].trim()), body };
  } catch (error) {
    throw new Error(`${where}: demo-step ${id}: ${(error as Error).message}`);
  }
}

/** The fence a line opens (its run of three or more ` or ~), or null. */
function fenceOpened(line: string): string | null {
  return /^ {0,3}(`{3,}|~{3,})/.exec(line)?.[1] ?? null;
}

/** True when `line` closes the code block `fence` opened. */
function fenceClosed(line: string, fence: string): boolean {
  const run = line.trim();
  return run.length >= fence.length && [...run].every((c) => c === fence[0]);
}

/**
 * Every passage in one docs page. `file` is its path under `docs/`. A marker
 * inside a fenced code block is code — a page may show the syntax — and a
 * passage cannot hold a code block, which a step cannot show.
 */
export function extractPassages(markdown: string, file: string): Passage[] {
  const passages: Passage[] = [];
  let open: { id: string; line: number; lines: string[] } | null = null;
  let fence: string | null = null;
  for (const [index, raw] of markdown.split('\n').entries()) {
    const line = raw.trim();
    const where = `docs/${file}:${index + 1}`;
    if (fence !== null) {
      if (fenceClosed(raw, fence)) fence = null;
      continue;
    }
    fence = fenceOpened(raw);
    if (fence !== null) {
      if (open) throw new Error(`${where}: demo-step ${open.id} holds a fenced code block, which a tour step cannot show`);
      continue;
    }
    const opening = OPEN.exec(line);
    if (opening) {
      if (open) throw new Error(`${where}: demo-step ${opening[1]} opens inside ${open.id}`);
      if (!PASSAGE_ID.test(opening[1])) {
        throw new Error(`${where}: demo-step id "${opening[1]}" is not <tour>/<n>, numbered from 1`);
      }
      open = { id: opening[1], line: index + 1, lines: [] };
    } else if (CLOSE.test(line)) {
      if (!open) throw new Error(`${where}: <!-- /demo-step --> closes no passage`);
      passages.push({ id: open.id, file, line: open.line, ...passageText(open.lines, where, open.id) });
      open = null;
    } else if (line.startsWith('<!--') && line.includes('demo-step')) {
      throw new Error(`${where}: a demo-step marker that does not parse: ${line}`);
    } else if (open) {
      open.lines.push(raw);
    }
  }
  if (open) throw new Error(`docs/${file}:${open.line}: demo-step ${open.id} is never closed`);
  return passages;
}

/** Python-Markdown's default heading slug, which is what mkdocs anchors on. */
export function slugify(heading: string): string {
  return heading
    .normalize('NFKD')
    .replace(/[^\x00-\x7f]/g, '')
    .replace(/[^\w\s-]/g, '')
    .trim()
    .toLowerCase()
    .replace(/[-\s]+/g, '-');
}

/** Every anchor a page's headings give it: the slug, or an explicit `{#id}`. */
export function headingAnchors(markdown: string): Set<string> {
  const anchors = new Set<string>();
  let fenced = false;
  for (const line of markdown.split('\n')) {
    if (FENCE.test(line)) fenced = !fenced;
    const heading = fenced ? null : /^#{1,6}\s+(.*?)\s*#*\s*$/.exec(line);
    if (!heading) continue;
    const explicit = /\{#([\w-]+)\}\s*$/.exec(heading[1]);
    anchors.add(explicit ? explicit[1] : slugify(heading[1].replace(/<[^>]+>/g, '')));
  }
  return anchors;
}

/** A docs page's published URL: mkdocs serves `a/b.md` at `a/b/`, `a/index.md` at `a/`. */
export function docsHref(page: string, anchor: string): string {
  const path = page.replace(/\.md$/, '').replace(/(^|\/)index$/, '$1');
  const dir = path === '' || path.endsWith('/') ? path : `${path}/`;
  return `${DOCS_SITE}${dir}#${anchor}`;
}

function isString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

/** The query keys a step may not set: the tour's own deep link. */
const TOUR_KEYS = new Set(['tour', 'step']);

/**
 * A step's `query`: an object of query keys to plain values, at least one,
 * none of them the tour's own `tour` or `step`. `fail` throws.
 */
function checkQuery(query: unknown, fail: (why: string) => never): void {
  if (query === null || typeof query !== 'object' || Array.isArray(query)) fail('is not an object of query keys');
  const entries = Object.entries(query as Record<string, unknown>);
  if (entries.length === 0) fail('names no key; leave it out instead');
  for (const [key, value] of entries) {
    if (!/^[a-z][a-zA-Z]*$/.test(key)) fail(`key "${key}" is not a query key`);
    if (TOUR_KEYS.has(key)) fail(`sets "${key}", which is the tour's own link`);
    if (typeof value !== 'string' || !/^[\w.-]+$/.test(value)) fail(`"${key}" is not a plain value`);
  }
  // A replay with no `t` plays from its first frame, and a step must open on
  // one frame: told the same address again, the page would pause a replay
  // the step meant to play (#3167).
  const keys = query as Record<string, string>;
  if (keys.mode === 'replay' && keys.t === undefined) {
    fail('sets "mode": "replay" with no "t"; name the moment the step opens on');
  }
}

/** A tour file's contents, checked field by field. `file` names it in errors. */
export function parseTour(value: unknown, file: string): TourSpec {
  const fail = (why: string): never => {
    throw new Error(`demo/tours/${file}: ${why}`);
  };
  if (value === null || typeof value !== 'object') return fail('is not a JSON object');
  const tour = value as Record<string, unknown>;
  if (!isString(tour.id) || !SLUG.test(tour.id)) return fail('needs an "id" of lowercase letters, digits and -');
  if (`${tour.id}.json` !== file) fail(`its id "${tour.id}" is not its file name`);
  if (!isString(tour.title)) fail('needs a "title"');
  if (!isString(tour.start) || !tour.start.startsWith('/')) fail('needs a "start" route');
  if (!Array.isArray(tour.steps) || tour.steps.length === 0) return fail('needs at least one step');
  const steps = tour.steps.map((raw: unknown, index: number): TourStepSpec => {
    const n = index + 1;
    const step = (raw ?? {}) as Record<string, unknown>;
    const { route, target, docs, anchor, passage, query, card, fit, clear } = step;
    if (!isString(route) || !/^\/[^?#]*$/.test(route)) fail(`step ${n} needs a "route": a path, no query`);
    if (query !== undefined) checkQuery(query, (why) => fail(`step ${n}'s "query" ${why}`));
    if (!isString(target) || !SLUG.test(target)) {
      fail(`step ${n} needs a "target": the value of a data-tour attribute, never a CSS selector or text`);
    }
    // A graph card inside the target, by its node id (`data-id` on the card
    // the epic graph draws), so a step about one node highlights that card.
    if (card !== undefined && !(isString(card) && /^[\w-]+$/.test(card))) {
      fail(`step ${n}'s "card" must be a node id: letters, digits, _ and -`);
    }
    if (fit !== undefined && fit !== true) fail(`step ${n}'s "fit" is true or left out`);
    if (clear !== undefined && !(isString(clear) && SLUG.test(clear))) {
      fail(`step ${n}'s "clear" must be the value of a data-tour attribute`);
    }
    // A step that names a card asks the visitor to read it, and Fit all draws
    // a 13-node graph too small to read on a laptop (c9 review).
    if (card !== undefined && fit === true) fail(`step ${n} names a card and fits the whole graph; a named card must stay readable`);
    // ...and selects it, so the page frames it and its rail reads that node.
    if (card !== undefined && (query as Record<string, string> | undefined)?.node !== card) {
      fail(`step ${n} names card "${card}" but its "query" does not select it ("node": "${card}")`);
    }
    if (!isString(docs) || !docs.endsWith('.md')) fail(`step ${n} needs "docs": its page under docs/`);
    if (!isString(anchor) || !/^[\w-]+$/.test(anchor)) fail(`step ${n} needs an "anchor" on its docs page`);
    if (passage !== `${tour.id}/${n}`) fail(`step ${n}'s "passage" must be "${tour.id}/${n}"`);
    const spec = { route, target, docs, anchor, passage } as TourStepSpec;
    if (query !== undefined) spec.query = { ...(query as Record<string, string>) };
    if (card !== undefined) spec.card = card as string;
    if (fit === true) spec.fit = true;
    if (clear !== undefined) spec.clear = clear as string;
    return spec;
  });
  if (steps[0].route !== tour.start) fail(`"start" is ${tour.start} but step 1 is on ${steps[0].route}`);
  return { id: tour.id, title: tour.title as string, start: tour.start as string, steps };
}

/**
 * The most words a step's popover may say, title and body together: a popover
 * is read in a glance beside what it points at, not studied.
 */
export const MAX_STEP_WORDS = 40;

/** A passage's words as its popover shows them: the title, then every paragraph. */
export function passageWords(passage: Pick<Passage, 'title' | 'body'>): number {
  const text = [passage.title, ...passage.body.map((runs) => runs.map((run) => run.text).join(''))].join(' ');
  return text.split(/\s+/).filter((word) => word !== '').length;
}

/**
 * Every tour with its passages filled in. `pages` is every docs page's text by
 * its path under `docs/`. Throws on a missing, duplicate or unread passage, a
 * passage on the wrong page, and an anchor its page does not have.
 */
export function bundleTours(tours: TourSpec[], pages: Record<string, string>): BundledTour[] {
  const passages = new Map<string, Passage>();
  for (const [file, markdown] of Object.entries(pages).sort(([a], [b]) => a.localeCompare(b))) {
    for (const passage of extractPassages(markdown, file)) {
      const seen = passages.get(passage.id);
      if (seen) {
        throw new Error(
          `demo-step ${passage.id} is marked twice: docs/${seen.file}:${seen.line} and docs/${file}:${passage.line}`
        );
      }
      passages.set(passage.id, passage);
    }
  }
  const unread = new Set(passages.keys());
  const bundled = tours.map((tour) => ({
    ...tour,
    steps: tour.steps.map((step) => {
      const passage = passages.get(step.passage);
      if (!passage) throw new Error(`demo/tours/${tour.id}.json: no <!-- demo-step: ${step.passage} --> in docs/`);
      if (passage.file !== step.docs) {
        throw new Error(`demo-step ${step.passage} is in docs/${passage.file}, but its step links to docs/${step.docs}`);
      }
      if (!headingAnchors(pages[step.docs]).has(step.anchor)) {
        throw new Error(`demo-step ${step.passage}: docs/${step.docs} has no #${step.anchor}`);
      }
      const words = passageWords(passage);
      if (words > MAX_STEP_WORDS) {
        throw new Error(
          `demo-step ${step.passage} (docs/${passage.file}:${passage.line}) is ${words} words with its title; a tour popover holds at most ${MAX_STEP_WORDS}`
        );
      }
      unread.delete(step.passage);
      return { ...step, title: passage.title, body: passage.body, href: docsHref(step.docs, step.anchor) };
    })
  }));
  if (unread.size > 0) {
    const where = [...unread].map((id) => `${id} (docs/${passages.get(id)?.file})`);
    throw new Error(`demo-step passages no tour step reads: ${where.join(', ')}`);
  }
  return bundled.sort((a, b) => a.id.localeCompare(b.id));
}

/** What mkdocs leaves out of every build, whatever `exclude_docs` says. */
const MKDOCS_DEFAULT_EXCLUDES = ['.*', '/templates/'];

/**
 * The `exclude_docs` block of `mkdocs.yml`, one gitignore-style pattern per
 * line, comments and blank lines dropped; [] when the file sets none. Read by
 * hand — mkdocs.yml carries `!!python` tags a YAML parser refuses — and only
 * as the `|` block it is written as, so a reshaped key fails the build rather
 * than letting an excluded page's passage into a tour.
 */
export function excludeDocs(mkdocsYml: string): string[] {
  const lines = mkdocsYml.split('\n');
  const at = lines.findIndex((line) => /^exclude_docs\s*:/.test(line));
  if (at === -1) return [];
  if (!/^exclude_docs\s*:\s*\|[-+]?\s*$/.test(lines[at])) {
    throw new Error('mkdocs.yml: the demo build reads exclude_docs only as a `|` block');
  }
  const patterns: string[] = [];
  for (const line of lines.slice(at + 1)) {
    if (line.trim() !== '' && !/^\s/.test(line)) break;
    const pattern = line.trim();
    if (pattern !== '' && !pattern.startsWith('#')) patterns.push(pattern);
  }
  return patterns;
}

/** One gitignore-style pattern as a test of a `/`-separated path under `docs/`. */
function excludes(pattern: string): (path: string) => boolean {
  const dirOnly = pattern.endsWith('/');
  const bare = pattern.replace(/\/$/, '');
  // As in .gitignore: a leading or inner slash anchors the pattern at the root.
  const anchored = bare.includes('/');
  const glob = bare.replace(/^\//, '');
  let body = '';
  for (let i = 0; i < glob.length; i++) {
    if (glob.startsWith('**', i)) {
      body += '.*';
      i++;
    } else if (glob[i] === '*') body += '[^/]*';
    else if (glob[i] === '?') body += '[^/]';
    else body += glob[i].replace(/[.+^${}()|[\]\\]/g, '\\$&');
  }
  const test = new RegExp(`${anchored ? '^' : '(?:^|/)'}${body}${dirOnly ? '/.*' : '(?:/.*)?'}$`);
  return (path) => test.test(path);
}

/** True when mkdocs would leave `path` out, under `patterns` (last match wins, `!` re-includes). */
function excluded(path: string, patterns: string[]): boolean {
  let out = false;
  for (const pattern of [...MKDOCS_DEFAULT_EXCLUDES, ...patterns]) {
    const negated = pattern.startsWith('!');
    if (excludes(negated ? pattern.slice(1) : pattern)(path)) out = !negated;
  }
  return out;
}

/**
 * Every `*.md` under `dir` that mkdocs publishes, by its path relative to it
 * with `/` separators: a page `exclude_docs` (or mkdocs's own defaults, dot
 * files and `/templates/`) leaves out is not read, so an audit pack or an
 * untracked draft can neither lend a tour its words nor break the build.
 */
export function publishedPages(dir: string, exclude: string[], root: string = dir): Record<string, string> {
  const pages: Record<string, string> = {};
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    const rel = relative(root, path).split(sep).join('/');
    if (excluded(entry.isDirectory() ? `${rel}/` : rel, exclude)) continue;
    if (entry.isDirectory()) Object.assign(pages, publishedPages(path, exclude, root));
    else if (entry.isFile() && entry.name.endsWith('.md')) pages[rel] = readFileSync(path, 'utf8');
  }
  return pages;
}

/**
 * The tours under `toursDir`, bundled against the published docs under
 * `docsDir`, with `mkdocs` the config that says which pages those are.
 */
export function readTours(toursDir: URL, docsDir: URL, mkdocs: URL = new URL('../mkdocs.yml', docsDir)): BundledTour[] {
  const dir = fileURLToPath(toursDir);
  const tours = readdirSync(dir)
    .filter((name) => name.endsWith('.json'))
    .sort()
    .map((file) => {
      let value: unknown;
      try {
        value = JSON.parse(readFileSync(join(dir, file), 'utf8'));
      } catch (error) {
        throw new Error(`demo/tours/${file} is not JSON: ${(error as Error).message}`);
      }
      return parseTour(value, file);
    });
  const exclude = excludeDocs(readFileSync(fileURLToPath(mkdocs), 'utf8'));
  return bundleTours(tours, publishedPages(fileURLToPath(docsDir), exclude));
}

/** The Vite plugin that bundles the tours as `virtual:kew-demo-tours`. */
export function demoTours(toursDir: URL, docsDir: URL): Plugin {
  return {
    name: 'kew-demo-tours',
    resolveId(id) {
      return id === DEMO_TOURS_ID ? RESOLVED : null;
    },
    load(id) {
      if (id !== RESOLVED) return null;
      // Rebuild when a tour or a docs page changes under `vite dev`.
      const mkdocs = new URL('../mkdocs.yml', docsDir);
      for (const file of [toursDir, docsDir, mkdocs]) this.addWatchFile(fileURLToPath(file));
      return `export default ${JSON.stringify(readTours(toursDir, docsDir))};`;
    }
  };
}
