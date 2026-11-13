/**
 * FUI-09 — the factory header against its pixel reference.
 *
 * The epic's visual check is a headless-Chrome screenshot compared by eye
 * (`npm run shoot -- /work DWork`), which no CI run performs. The numbers that
 * check would catch are all in the DWork header markup, so they are asserted
 * here instead: the artboard is read as the source of truth, the geometry is
 * matched against the utilities `FactoryHeader.svelte` actually carries, and
 * every colour is matched against the app.css token it must come from — a
 * screenshot cannot tell `#ff2a6d` from `var(--accent)`, and only one of the
 * two switches theme.
 *
 * This is deliberately narrow: the five spacings, the two mark colours, the
 * cursor box and the hairlines. It is not a rendering test (there is no
 * component-render infra here — see StatusMark.svelte's note).
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { HEADER_FOLD } from './header-state';
import { FACTORY_TABS } from './nav';

const artboard = readFileSync(new URL('../../../design/factory-ui/DWork.html', import.meta.url), 'utf8');
const header = artboard.match(/<header[\s\S]*?<\/header>/)![0];
const component = readFileSync(new URL('./FactoryHeader.svelte', import.meta.url), 'utf8');
const css = readFileSync(new URL('../../app.css', import.meta.url), 'utf8');

/** The dark-theme value of one `--token`, as app.css declares it. */
function token(name: string): string {
  return css.match(new RegExp(`--${name}:\\s*([^;]+);`))![1].trim();
}

// ─────────────────────────────────────────────────────────────────────────────
// Reading the component instead of transcribing it.
//
// Every assertion below about WHAT the row carries and HOW WIDE it is used to
// be a string the test spelled out by hand, and two of them were wrong in the
// same way (#2418 review): one entry in a fold table was a PREFIX of another
// element's class, so it was satisfied by the wrong element and the reading it
// named went unchecked; and the width budget was a constant table that never
// looked at the markup, so putting a reading back on the phone row left every
// assertion green. Both are the same mistake — the test restating the component
// instead of reading it — so the row is parsed once here and the assertions ask
// questions of the parse.
// ─────────────────────────────────────────────────────────────────────────────

type Tag = {
  /** `span`, `a`, `details`, `StatusMark`… */
  name: string;
  /** Everything between the element name and the closing `>`. */
  attrs: string;
  /** Its `class` attribute, or '' when it carries none. */
  class: string;
  /** Nesting depth inside the parsed slice; 0 for a top-level element. */
  depth: number;
  /** The markup between this tag and its closing tag; '' when self-closing. */
  inner: string;
  /** This element's whole source, open tag through close tag. */
  outer: string;
};

/** Elements that never have a closing tag. */
const VOID = new Set(['br', 'hr', 'img', 'input']);

/**
 * Every element in `source`, in document order, with its nesting depth.
 *
 * Hand-rolled rather than regexed because a Svelte attribute value is not
 * always quoted: `onclick={() => (confirming = false)}` carries a `>` that ends
 * no tag, so `/<[^>]*>/` stops short of the class that follows it. Quote state
 * and `{}` depth are both tracked. HTML comments go first — the comments in
 * this component quote class names, and a quoted class name is not markup.
 */
function elements(source: string): Tag[] {
  const text = source.replace(/<!--[\s\S]*?-->/g, '');
  const out: Tag[] = [];
  const open: { tag: Tag; contentAt: number; startAt: number }[] = [];
  let i = 0;
  while (i < text.length) {
    const lt = text.indexOf('<', i);
    if (lt === -1) break;
    const next = text[lt + 1] ?? '';
    if (next === '/') {
      const gt = text.indexOf('>', lt);
      const top = open.pop();
      if (top) {
        top.tag.inner = text.slice(top.contentAt, lt);
        top.tag.outer = text.slice(top.startAt, (gt === -1 ? text.length : gt) + 1);
      }
      i = gt === -1 ? text.length : gt + 1;
      continue;
    }
    if (!/[A-Za-z]/.test(next)) {
      i = lt + 1;
      continue;
    }
    let j = lt + 1;
    let quote = '';
    let braces = 0;
    for (; j < text.length; j += 1) {
      const ch = text[j];
      if (quote !== '') {
        if (ch === quote) quote = '';
      } else if (ch === '"' || ch === "'") quote = ch;
      else if (ch === '{') braces += 1;
      else if (ch === '}') braces -= 1;
      else if (ch === '>' && braces === 0) break;
    }
    const whole = text.slice(lt, j + 1);
    const name = /^<([A-Za-z][\w.-]*)/.exec(whole)![1];
    const attrs = whole.slice(1 + name.length, whole.length - 1);
    const tag: Tag = {
      name,
      attrs,
      class: /class="([^"]*)"/.exec(attrs)?.[1] ?? '',
      depth: open.length,
      inner: '',
      outer: whole
    };
    out.push(tag);
    // A self-closing tag has no content, and its `outer` is the tag itself.
    if (!attrs.trimEnd().endsWith('/') && !VOID.has(name)) {
      open.push({ tag, contentAt: j + 1, startAt: lt });
    }
    i = j + 1;
  }
  for (const top of open) top.tag.inner = text.slice(top.contentAt);
  return out;
}

/** The immediate children of `tag`, as elements. */
const childrenOf = (tag: Tag): Tag[] => elements(tag.inner).filter((t) => t.depth === 0);

/**
 * A name for one item on the row, read off its own markup rather than its
 * classes: the accessible name of the mark it leads with, the words it prints,
 * or the expression it prints. Different for every item on this row, and it
 * survives an edit to that item's utilities — which a class string does not,
 * and which is why the fold table used to be able to match the wrong element.
 */
function nameOf(tag: Tag): string {
  if (tag.name === 'details') return 'More';
  const mark = /<StatusMark[^>]*label=(?:"([^"]*)"|\{([^}]*)\})/.exec(tag.inner);
  if (mark) return mark[1] ?? mark[2];
  const words = wordsUnder(tag);
  if (words !== '') return words;
  const { exprs } = printed(tag);
  if (exprs.length > 0) return exprs[0];
  if (/aria-hidden/.test(tag.attrs)) return 'hairline';
  return tag.class;
}

/** Every literal word under `tag` — its own and its children's. */
function wordsUnder(tag: Tag): string {
  return [printed(tag).text, ...childrenOf(tag).map(wordsUnder)].filter((t) => t !== '').join(' ');
}

/**
 * What this element prints ITSELF: its literal words, and the expressions it
 * interpolates. Its children's markup is removed, and so is Svelte's own
 * control flow — `{#if}` prints nothing, `{badge.label}` prints a reading.
 */
function printed(tag: Tag): { text: string; exprs: string[] } {
  let rest = tag.inner;
  for (const child of childrenOf(tag)) rest = rest.split(child.outer).join(' ');
  rest = rest.replace(/\{[#:/@][^}]*\}/g, ' ');
  const exprs: string[] = [];
  rest = rest.replace(/\{([^{}]*)\}/g, (_match, expr: string) => {
    exprs.push(expr.trim());
    return ' ';
  });
  if (/[{}]/.test(rest)) throw new Error(`unparsed Svelte block in ${nameOf(tag)}: ${rest}`);
  return { text: rest.replace(/\s+/g, ' ').trim(), exprs };
}

/** Tailwind's breakpoints, in the order its variants cascade. */
const BREAKPOINTS: [string, number][] = [
  ['', 0],
  ['sm:', 640],
  ['lg:', 1024],
  ['xl:', 1280]
];

const DISPLAYED = new Set(['flex', 'block', 'inline', 'inline-flex', 'inline-block', 'table']);

/** Every utility in `cls` that applies at `px`, in cascade order. */
function utilities(cls: string, px: number): string[] {
  const applied: string[] = [];
  for (const [prefix, min] of BREAKPOINTS) {
    if (px < min) continue;
    for (const token of cls.split(/\s+/)) {
      if (prefix === '' ? token.includes(':') : !token.startsWith(prefix)) continue;
      applied.push(token.slice(prefix.length));
    }
  }
  return applied;
}

/** Is this element laid out at `px` wide? The last display utility wins. */
function onRow(cls: string, px: number): boolean {
  let shown = true;
  for (const util of utilities(cls, px)) {
    if (util === 'hidden') shown = false;
    else if (DISPLAYED.has(util)) shown = true;
  }
  return shown;
}

/** The `gap-` or `px-` this element lays out with at `px` wide, in pixels. */
function spacing(cls: string, prop: 'gap' | 'px', px: number): number {
  let value = 0;
  for (const util of utilities(cls, px)) {
    const m = new RegExp(`^${prop}-(?:\\[(\\d+)px\\]|(\\d+))$`).exec(util);
    if (m) value = m[1] !== undefined ? Number(m[1]) : Number(m[2]) * 4;
  }
  return value;
}

/** The first matching numeric utility at `px` wide, or null. */
function unit(cls: string, prop: string, px: number): number | null {
  let value: number | null = null;
  for (const util of utilities(cls, px)) {
    const m = new RegExp(`^${prop}-(px|\\d+)$`).exec(util);
    if (m) value = m[1] === 'px' ? 1 : Number(m[1]) * 4;
  }
  return value;
}

/**
 * IBM Plex Mono is a monospace bundled by @fontsource, so a reading is its
 * character count times one advance and there is no fallback metric to allow
 * for. The advance is 0.6em by the font's own metrics, which Chromium rounds UP
 * to a whole pixel before adding tracking — 8px at the 13px row size, measured
 * in headless Chromium against this component. Rounding up is also the safe
 * direction for a budget that has to hold.
 */
const ink = (text: string, px: number, trackingEm: number): number =>
  text.length * (Math.ceil(px * 0.6) + px * trackingEm);

/** The type size this element sets, or null when it inherits one. */
function fontOf(cls: string): { px: number | null; tracking: number | null } {
  const sized = /(?:^|\s)text-(?:\[(\d+)px\]|(11|13|15|26|44))(?:\s|$)/.exec(cls);
  const tracked = /(?:^|\s)tracking-\[(-?[\d.]+)em\](?:\s|$)/.exec(cls);
  return {
    px: sized ? Number(sized[1] ?? sized[2]) : null,
    tracking: tracked ? Number(tracked[1]) : null
  };
}

/**
 * What a `{…}` on the row can print at its widest. A reading that is not in
 * here and not inside a capped `truncate` span has no bound this file knows of,
 * and `priceOf` refuses it — which is the whole guard: the repo slug sat on the
 * row uncapped and unpriced, and the check that claimed to bound every reading
 * only looked at the ones already carrying `truncate` (#2418 review).
 *
 * Counts are bounded by their digits: `countLabel` prints digits or the em
 * dash, and a health badge prints how many checks are down.
 */
const COUNTS = new Set([
  'countLabel(counts.working)',
  'countLabel(counts.needsYou)',
  'health.count'
]);

/**
 * The widest this element can paint at `px` wide, in pixels.
 *
 * `give` is whether the row may squeeze this box, which is CSS and not taste:
 * a box with `shrink-0` is never squeezed; a box with `min-w-0` may be squeezed
 * all the way to its unshrinkable children; a `truncate` box ellipsises to
 * nothing; and a box with none of those keeps its automatic minimum, which is
 * its own whole content. Pricing the floor is the same walk with `give` on.
 */
function priceOf(
  tag: Tag,
  opts: { px: number; digits: number; give: boolean; font: number; tracking: number }
): number {
  const size = /\bsize=\{(\d+)\}/.exec(tag.attrs);
  if (size) return Number(size[1]); // a StatusMark draws its own box
  const cap = /(?:^|\s)max-w-(\d+)(?:\s|$)/.exec(tag.class);
  if (cap) {
    if (!/(?:^|\s)truncate(?:\s|$)/.test(tag.class)) {
      throw new Error(`${nameOf(tag)} has a max-w- cap but no truncate to ellipsise under it`);
    }
    return opts.give ? 0 : Number(cap[1]) * 4;
  }
  if (/(?:^|\s)truncate(?:\s|$)/.test(tag.class)) {
    throw new Error(`${nameOf(tag)} truncates with no max-w- cap, so the row cannot budget it`);
  }
  const width = unit(tag.class, 'w', opts.px);
  if (width !== null) return width;

  const font = fontOf(tag.class);
  const inner = {
    ...opts,
    font: font.px ?? opts.font,
    tracking: font.tracking ?? opts.tracking,
    // A box is only squeezed down to what its own children refuse to give.
    give: /(?:^|\s)shrink-0(?:\s|$)/.test(tag.class)
      ? false
      : /(?:^|\s)min-w-0(?:\s|$)/.test(tag.class)
        ? opts.give
        : false
  };
  const kids = childrenOf(tag).filter(
    (child) =>
      onRow(child.class, opts.px) &&
      // Out of flow, so it costs the row nothing: the More menu is one.
      !utilities(child.class, opts.px).some((u) => u === 'absolute' || u === 'fixed')
  );
  const { text, exprs } = printed(tag);
  let total = kids.reduce((sum, child) => sum + priceOf(child, inner), 0);
  let boxes = kids.length;
  if (text !== '') {
    total += ink(text, inner.font, inner.tracking);
    boxes += 1;
  }
  for (const expr of exprs) {
    if (!COUNTS.has(expr)) {
      throw new Error(
        `${nameOf(tag)} prints {${expr}}, which has no width bound: cap it with a ` +
          'truncate span, or add it to COUNTS with the reason it is bounded'
      );
    }
    total += ink('9'.repeat(opts.digits), inner.font, inner.tracking);
    boxes += 1;
  }
  return total + Math.max(0, boxes - 1) * spacing(tag.class, 'gap', opts.px);
}

const headerTag = elements(component.slice(component.indexOf('<header')))[0];
/** The header's own three items: the wordmark, the nav, the reading cluster. */
const headerItems = childrenOf(headerTag);
const cluster = headerItems.find((t) => t.class.startsWith('ml-auto'))!;
/** The readings, as separate items on the row. */
const rowItems = childrenOf(cluster);

/** The phone DESIGN.md verifies at, and the width the row's budget is held to. */
const PHONE = 390;

/**
 * The widths the row is priced at. 390 and 1280 are DESIGN.md's own two
 * (Responsive Behavior); 1024 is the fold, where the row first carries the
 * three failure readings and the stop's two-press confirm together; 640 and
 * 768 are the widths between, and 1440 is the artboard's.
 *
 * Read twice: `rowCost` adds the row up at each of them, and the fold describe
 * at the end of this file refuses a horizontal spacing tier that is not one of
 * them. A tier the budget never visits is a tier nothing has measured, which
 * is the whole of #2419.
 */
const PRICED = [PHONE, 640, 768, 1024, 1280, 1440];

describe('DWork header (y 0–56)', () => {
  it('is 56px tall with 32px side padding, a 40px gap and a bottom hairline', () => {
    expect(header).toContain('height: 56px');
    expect(header).toContain('padding: 0 32px');
    expect(header).toContain('gap: 40px');
    expect(header).toContain('border-bottom: 1px solid #221c33');
    // h-14 = 56px, sm:px-8 = 32px, sm:gap-10 = 40px. The artboard's numbers
    // are the `sm:` ones now: below 640px the row is tighter (#2278 D1, and
    // 12px rather than 16px since #2418 took the nav's give away), and 390px
    // is a width no artboard was drawn at.
    expect(component).toContain('h-14 shrink-0 items-center gap-3 border-b border-hair bg-ink px-4 sm:gap-10 sm:px-8');
    expect(token('hair')).toBe('#221c33');
  });

  it('draws the state cursor as a 12×24 block beside a 26px/600 wordmark', () => {
    expect(header).toContain('width: 12px; height: 24px');
    expect(header).toContain('font-size: 26px; letter-spacing: -0.03em');
    // h-6 = 24px, w-3 = 12px.
    expect(component).toContain('inline-block h-6 w-3');
    expect(component).toContain('text-[26px] leading-none font-semibold tracking-[-0.03em]');
  });

  it('spaces the tabs 28px apart at 13px with a 2px underline on the active one', () => {
    expect(header).toContain('gap: 28px');
    expect(header).toContain('font-size: 13px');
    expect(header).toContain('border-bottom: 2px solid #ebe6f2');
    expect(header).toContain('aria-current="page"');
    // sm:gap-7 = 28px; the underline is the active tab's own border-b-2.
    expect(component).toContain('font-mono text-13 tracking-[0.02em] sm:gap-7');
    expect(component).toContain('border-b-2');
    expect(component).toContain('class:border-text-1={active}');
    expect(token('text-1')).toBe('#ebe6f2');
  });

  it('spaces the right cluster 24px apart, with 8px between a mark and its count', () => {
    expect(header).toContain('gap: 24px');
    expect(header).toContain('gap: 8px');
    // lg:gap-6 = 24px, gap-2 = 8px. The artboard spacing is the FULL cluster's,
    // and the full cluster only fits from lg up (#2418, the describe below).
    expect(component).toContain('items-center gap-3 font-mono text-13 text-text-2 lg:gap-6');
    expect(component).toContain('<span class="hidden items-center gap-2 xl:flex">');
  });

  it('counts working in cyan and needs-you in magenta, at the artboard 7px', () => {
    // A 7px circle and a 7px square rotated 45°, which is StatusMark's `dot`
    // and `diamond` at its smallest size.
    expect(header).toContain('width: 7px; height: 7px; border-radius: 50%; background: #05d9e8');
    expect(header).toContain('width: 7px; height: 7px; background: #ff2a6d; transform: rotate(45deg)');
    expect(component).toContain('<StatusMark kind="working" size={7} label="working" />');
    expect(component).toContain('<StatusMark kind="needs_you" size={7} label="needs you" />');
    // The rationing that makes the pair readable: cyan is work, magenta is you.
    expect(token('accent-2')).toBe('#05d9e8');
    expect(token('accent')).toBe('#ff2a6d');
  });

  it('separates the repo name with a 1×20 hairline and quietens it', () => {
    expect(header).toContain('width: 1px; height: 20px; background: #221c33');
    expect(header).toContain('color: #8d84a3;">thossullivan/kew');
    // h-5 = 20px, w-px = 1px.
    expect(component).toContain('h-5 w-px shrink-0 bg-hair');
    expect(component).toContain('text-text-3">{counts.repo');
    expect(token('text-3')).toBe('#8d84a3');
  });

  it('keeps the Evals tab out of kew, where the artboard has no link either', () => {
    expect(header).toContain('title="Evals is not part of kew"');
    expect(component).not.toContain('Evals');
  });
});

/**
 * FUI-26 — the one write the cutover moved into the header.
 *
 * The console's fleet-stopped banner carried the resume, and the console is no
 * longer a page anyone passes through. The artboards have no stop in them (no
 * frame was drawn with a stopped fleet), so there are no pixels to match here:
 * what is pinned is that the control exists, that it confirms before it writes,
 * and that it uses the same `verbs.ts` call the console used, so the audit row
 * for a resume is the same row whichever surface pressed it.
 */
describe('the fleet stop in the header (the cutover parity row)', () => {
  it('announces a stop only when one was read, in the danger token', () => {
    expect(component).toContain("const stop = $derived(fleetStop(factoryPulse.dashboard))");
    expect(component).toContain('{#if stop}');
    expect(component).toContain('text-danger');
    expect(component).toContain('<StatusMark kind="failed" size={7} label={FLEET_STOPPED} />');
  });

  it('confirms before it resumes, and writes through verbs.ts', () => {
    expect(component).toContain("import { driverStaleNotice, resumeChip, resumeFleet } from '$lib/verbs'");
    expect(component).toContain('onclick={() => (confirming = true)}');
    expect(component).toContain('onclick={confirmResume}');
    expect(component).toContain('await resumeFleet(true)');
  });

  it('drops the last resume answer once the stop clears, so the next one is pressable', () => {
    expect(component).toContain('if (stop !== null) return;');
  });

  it('says what came back, refusal included, rather than going quiet', () => {
    expect(component).toContain('chip = resumeChip(res.data)');
    expect(component).toContain('notice = driverStaleNotice(res.data.driver_stale)');
    expect(component).toContain('else resumeError = res.message');
    expect(component).toContain("{#if resumeError !== ''}");
  });
});


/**
 * #2278 (D1), then #2418 — the header at 390px, and which side gives way.
 *
 * D1: every page scrolled sideways on a phone, and what went off the right edge
 * was the whole right cluster — the needs-you counter, the repo and More. The
 * header is a single 56px row by design (DESIGN.md, Grid), so the fix is not a
 * second line but a smaller set of things on it. D1 made the nav the one
 * compressible item to buy that room: `min-w-0`, a `truncate` label and
 * `overflow-clip`.
 *
 * #2418 is the bill for it. At 390px the tabs read `Wo… Monit… Epi…`: the nav
 * had paid for the readings beside it. Every reading in the cluster has a
 * second way to be had — its `title`, the More menu, the page it links to — and
 * a truncated nav label has none, so the order is inverted. The nav and its
 * labels are `shrink-0` at every width, and the readings fold into More
 * instead: below `lg` (1024px, the console's own fold) the row carries the
 * wordmark, the three whole labels, the needs-you mark with its count, a mark
 * for anything red now inside the menu, and More.
 *
 * `lg` rather than `sm` because the row's own width said so (#2333 round 3).
 * D1's fold was `sm`, and the row it folded to carried every reading at once:
 * ~741px of unshrinkable width, so from 641px up the header was wider than the
 * window — the nav ellipsised to nothing and the page STILL scrolled sideways
 * (#2419, caught by the 641×900 reading in `scripts/ui-check.mjs`).
 * `HEADER_FOLD` is that measurement and the breakpoint it chose; the last
 * describe in this file holds the two to each other.
 *
 * Asserted as markup for the same reason the geometry above is: no CI run can
 * resize a window. `npm run ui-check` reads the rendered text at 390px.
 */
describe('the header below 1024px', () => {
  it('tightens its own gutter and gaps rather than overflowing them', () => {
    expect(component).toContain('gap-3 border-b border-hair bg-ink px-4 sm:gap-10 sm:px-8');
  });

  it('keeps the wordmark whole, and the nav with it', () => {
    expect(component).toContain('flex shrink-0 items-center gap-[5px]');
    // No `min-w-0` and no clip on the nav any more: the row may not take width
    // off the navigation at any width (#2418).
    expect(component).toContain(
      'class="flex h-14 shrink-0 gap-4 font-mono text-13 tracking-[0.02em] sm:gap-7"'
    );
    expect(component).toContain('class="flex h-14 shrink-0 items-center border-b-2');
  });

  it('never ellipsises a nav label — the one reading with nowhere else to be read', () => {
    const nav = component.slice(component.indexOf('<nav'), component.indexOf('</nav>'));
    expect(nav).toContain('<span class="whitespace-nowrap">{tab.label}</span>');
    // The three D1 mechanisms that made a label ellipsisable, each by name.
    expect(nav).not.toContain('truncate');
    expect(nav).not.toContain('min-w-0');
    expect(nav).not.toContain('overflow-clip');
  });

  it('draws no needs-you counter until the count is read (design review c4, D35)', () => {
    // A magenta diamond over a bare `—` claimed "a person is needed" about a
    // number nobody had read; the whole counter waits for the reading.
    const counter = component.indexOf('<StatusMark kind="needs_you" size={7} label="needs you" />');
    const guard = component.lastIndexOf('{#if counts.needsYou !== null}', counter);
    expect(guard).toBeGreaterThan(-1);
    expect(component.slice(guard, counter)).not.toContain('{/if}');
  });

  it('keeps the needs-you count on the row, its word only where there is room', () => {
    // "What needs me?" is the phone's first question (DESIGN.md), so this one
    // reading holds the row at every width while the rest fold.
    // The wrapper also carries the demo tour's inert `data-tour` (#3146).
    expect(component).toMatch(/<span class="flex shrink-0 items-center gap-2"(?: data-[a-z-]+="[a-z-]+")*>/);
    expect(component).toContain('<span class="hidden lg:inline">needs you</span>');
  });

  /**
   * What the row carries, at each width, read off the component.
   *
   * This was a loop over class strings, and one entry — the fleet stop's — was
   * a PREFIX of the stale build's class, so it matched the stale build's
   * element and the stop's own fold went unasserted: the stop could be put
   * back on the 390px row with every test still green (#2418 review). There is
   * no substring matching left here. Each item is named by what it PRINTS, the
   * list is exact, and `onRow` applies the component's own display utilities in
   * Tailwind's cascade order, so adding a reading to any stage of the fold —
   * or dropping one — fails this and nothing else has to be remembered.
   */
  const carriedAt = (px: number): string[] =>
    rowItems.filter((item) => onRow(item.class, px)).map(nameOf);

  /** The one item on this row that is a press rather than a reading. */
  const STOP_PRESSES = 'confirm resume cancel resume';

  it('carries the needs-you count and nothing else below lg', () => {
    expect(carriedAt(390)).toEqual(['needs you', 'something is down, in More', 'More']);
    expect(carriedAt(390)).toEqual(carriedAt(1023));
  });

  it('takes the failures back at lg and the two quiet readings at xl', () => {
    expect(carriedAt(1024)).toEqual([
      'needs you',
      'stale build',
      'factory health',
      'FLEET_STOPPED',
      STOP_PRESSES,
      'More'
    ]);
    // The full artboard cluster, and the working count and repo are the two
    // readings it waited until 1280 for.
    expect(carriedAt(1280)).toEqual([
      'working',
      'needs you',
      'stale build',
      'factory health',
      'FLEET_STOPPED',
      STOP_PRESSES,
      'hairline',
      'counts.repo ?? UNREADABLE',
      'More'
    ]);
  });

  /** The More menu's two folded blocks: the failures, and the quiet pair. */
  const foldedBlocks = (() => {
    const menu = elements(component.slice(component.indexOf('<details class=')))[0];
    const panel = childrenOf(menu).find((t) => t.class.includes('absolute'))!;
    return childrenOf(panel).filter((t) => t.name === 'div' && /(?:lg|xl):hidden/.test(t.class));
  })();

  it('reads every reading it drops inside the More menu instead', () => {
    // Derived on BOTH sides, so a reading taken off the row and not put in the
    // menu fails here — which no assertion caught before: the row's fold and
    // the menu's contents were two hand-written lists that could disagree.
    const inMenu = foldedBlocks.flatMap((block) => childrenOf(block).map(nameOf));
    const dropped = rowItems
      .filter((item) => !onRow(item.class, PHONE))
      .map(nameOf)
      // The hairline is a 1px rule, not a reading, and the stop's presses are
      // not one either: in the menu they sit inside the stop's own block, with
      // their whole phrase (just below).
      .filter((name) => name !== 'hairline' && name !== STOP_PRESSES);
    expect([...inMenu].sort()).toEqual([...dropped].sort());
  });

  it('reads all of them WHOLE there, which is what the row could not afford', () => {
    const folded = foldedBlocks.map((block) => block.inner).join('\n');
    expect(foldedBlocks.map((b) => b.class)).toEqual([
      'flex flex-col gap-2 px-3 pt-2 lg:hidden',
      'flex flex-col gap-2 px-3 py-2 xl:hidden'
    ]);
    // The working count and the repo, as D1 left them…
    expect(folded).toContain('{countLabel(counts.working)}');
    expect(folded).toContain('{counts.repo ?? UNREADABLE}');
    // …and what #2418 moved in beside them. Each with its WORDS: the row could
    // only afford a mark and a `title`, and a title is not reachable by touch.
    expect(folded).toContain('{badge.label}');
    expect(folded).toContain('{health.count}');
    expect(folded).toContain('{FLEET_STOPPED}');
    expect(folded).toContain('{notice === \'\' ? stop.message : `${stop.message} ${notice}`}');
  });

  it('marks the row when something red is folded in, rather than going quiet', () => {
    // No silent failures: a failure behind a closed menu with nothing on the
    // row is a silent failure. The mark is a sibling of the summary, not inside
    // it — the summary's own aria-label would swallow its accessible name.
    expect(component).toContain(
      "const folded = $derived(stop !== null || health !== null || badge?.stale === true)"
    );
    expect(component).toContain('{#if folded}');
    expect(component).toContain(
      '<StatusMark kind="failed" size={7} label="something is down, in More" />'
    );
    expect(component.indexOf('{#if folded}')).toBeLessThan(
      component.indexOf('<details class=')
    );
  });

  it('gives every folded control a 32px press target (D25)', () => {
    expect(component).toContain('flex h-8 shrink-0 cursor-pointer list-none items-center');
    expect(component).toContain('flex min-h-8 items-center px-3');
    const menu = component.slice(component.indexOf('<details class='));
    const folded = menu.slice(menu.indexOf('lg:hidden'), menu.indexOf('{#each MORE_DESTINATIONS'));
    // The health link and all three resume presses live in here now.
    for (const tag of [...folded.matchAll(/<(?:button|a)\b[\s\S]*?class="([^"]*)"/g)]) {
      expect(tag[1], `a folded control without a press target: ${tag[1]}`).toMatch(/min-h-8/);
    }
  });

  it('closes on an outside press and on Escape, like every menu (C15)', () => {
    expect(component).toContain("import { dismissable } from '$lib/ui/dismissable'");
    expect(component).toContain('use:dismissable');
  });
});

/**
 * #2278 (D1 review) — which side of the row gives way, and #2418's answer: not
 * the nav, and not the cluster either. NOTHING on this row shrinks.
 *
 * `min-w-0` on the nav *and* on the right cluster had the browser split the
 * row's deficit between them, and a cluster narrower than its own content does
 * not compress it: every control in it is `shrink-0`, so what did not fit was
 * painted past the cluster's own right edge — `cancel` and the whole `More`
 * summary off a 390px screen, with the sideways scroll D1 removed back with
 * them. D1's fix was one-sided: the cluster kept the automatic minimum that is
 * the sum of its children, and the nav ellipsised. #2418 takes the other side
 * away too, so the row can only fit by CARRYING LESS — which is what the `lg:`
 * switches above are.
 *
 * Two things keep that honest, and both are the half a later edit is likeliest
 * to undo: every reading left on the row at `lg` is bounded (a `truncate` span
 * is `white-space: nowrap`, so it raises the cluster's minimum by its whole
 * sentence however it paints), and the phone row's width still adds up.
 */
describe('the row gives way by carrying less', () => {
  /** The cluster as it sits on the row — the More menu's own markup is not on it. */
  const row = component.slice(
    component.indexOf('<div class="ml-auto'),
    component.indexOf('<details class="relative shrink-0"')
  );

  it('puts the row\'s one release valve on the cluster, and nowhere inside it', () => {
    // `min-w-0` HERE is what lets the row ellipsise a sentence instead of
    // scrolling the page sideways. `min-w-0` on anything holding a press is the
    // #2278 D1 bug: the box is squeezed as a whole, so the press goes with it
    // and paints over the reading next door.
    expect(cluster.class).toContain('min-w-0');
    for (const item of rowItems) {
      const presses = childrenOf(item).filter((kid) => kid.name === 'button');
      if (presses.length === 0) continue;
      expect(item.class, `${nameOf(item)} holds a press in a box the row can squeeze`).toContain(
        'shrink-0'
      );
      expect(item.class).not.toContain('min-w-0');
      for (const press of presses) expect(press.class).toContain('shrink-0');
    }
  });

  it('gives the nav the same floor, so neither side pays for the other', () => {
    expect(component).toContain('class="flex h-14 shrink-0 gap-4 font-mono');
    expect(component).toContain('class="flex h-14 shrink-0 items-center border-b-2');
  });

  it('bounds every reading on the row — not only the ones already truncating', () => {
    // The old version of this read `class="…truncate…"` and checked those, so
    // the one reading on the row with NEITHER a cap nor a truncate — the repo
    // slug, whose `delivery_repo` has no length bound — was outside the check
    // that claimed to bound every one of them (#2418 review). `priceOf` asks
    // the other way round: it walks every element and refuses any reading it
    // cannot bound, so a new unbounded one fails here with its own name in the
    // message. The `toThrow`-free form is the assertion: it prices.
    for (const item of rowItems) {
      expect(() =>
        priceOf(item, { px: 1440, digits: 3, give: false, font: 13, tracking: 0 })
      ).not.toThrow();
    }
    // And the caps are real caps: a `truncate` with no `max-w-` is unbounded
    // too, which `priceOf` refuses from the same walk.
    const capped = [...row.matchAll(/class="([^"]*truncate[^"]*)"/g)].map((m) => m[1]);
    expect(capped.length).toBeGreaterThan(0);
    for (const klass of capped) expect(klass).toMatch(/max-w-/);
  });

  it('keeps the resume presses whole on the row and pressable in the menu', () => {
    // A button squeezed to a few pixels of its own word is not a button, and
    // nothing on the row may shrink it. The stop cluster alone: it ends at the
    // hairline before the repo name, so the menu's own buttons are not in the
    // slice.
    const stop = row.slice(row.indexOf('{#if stop}'), row.indexOf('<span class="hidden h-5 w-px'));
    // Split rather than matched to the tag's `>`: an inline `() =>` handler
    // holds one, so a regex for the opening tag stops short of the class.
    const buttons = stop.split('<button').slice(1);
    expect(buttons).toHaveLength(3);
    for (const button of buttons) {
      expect(/class="([^"]*)"/.exec(button)![1]).toContain('shrink-0');
    }
    // And below lg they are a stacked menu block with room for both phrases —
    // where D1 had to spend the second step down to one word.
    const menu = component.slice(component.indexOf('<details class='));
    expect(menu).toContain('>confirm resume</button');
    expect(menu).toContain('>cancel</button');
    expect(component).not.toContain('aria-label="confirm resume"');
    expect(component).toContain('onclick={confirmResume}');
  });

  it('says the sha and the refusal under a cap, with the sentence as the title', () => {
    expect(component).toContain('<span class="max-w-44 truncate">{badge.label}</span>');
    expect(component).toContain('<span class="max-w-32 truncate">{FLEET_STOPPED}</span>');
    expect(component).toContain('<span class="max-w-72 truncate text-text-2" title={resumeError}');
    expect(component).toContain('<span class="max-w-40 truncate text-text-2" title={chip}>');
  });

  /**
   * The row's width, added up — from the component, not from a table.
   *
   * This was a hand-maintained constant table that never read the markup, so
   * no addition to the row could turn it red: the fleet stop and the repo were
   * each put back on the 390px row and both width assertions stayed green
   * (#2418 review). Now every item is priced by walking its own markup —
   * `priceOf` — and the only numbers written here are the viewport widths and
   * the font's advance. Add a reading to the row and it is priced; add one
   * whose width has no bound and it refuses to price at all.
   *
   * What is priced is an UPPER bound, deliberately: every branch of a `{#if}`
   * is counted together, so the fleet stop is charged for `confirm resume`,
   * `cancel` AND `resume` at once, and a `max-w-` cap is charged in full even
   * where the reading under it is shorter. The row has more margin than this
   * says, never less.
   *
   * `give: true` prices the FLOOR — what is left when the valve has spent every
   * sentence it can — which is the number that decides whether the page scrolls
   * sideways. At phone width nothing on the row can give, so the floor and the
   * cost are the same number there.
   */
  function rowCost(px: number, digits: number): Record<string, number> {
    const opts = { px, digits, give: true, font: 13, tracking: 0 };
    const items = headerItems.filter((item) => onRow(item.class, px));
    const parts: Record<string, number> = {
      'side padding': 2 * spacing(headerTag.class, 'px', px),
      'row gaps': (items.length - 1) * spacing(headerTag.class, 'gap', px)
    };
    for (const item of items) {
      if (item.name === 'nav') {
        // The nav prices its own, from `nav.ts`: three whole labels and the
        // gaps between them. `{#each}` is the one Svelte block that repeats, so
        // it is counted here rather than inside `priceOf`.
        const tab = childrenOf(item)[0];
        expect(printed(childrenOf(tab)[0]).exprs).toEqual(['tab.label']);
        const labels = FACTORY_TABS.map((t) => t.label);
        const font = fontOf(item.class);
        parts['nav labels'] = ink(labels.join(''), font.px ?? 13, font.tracking ?? 0);
        parts['nav gaps'] = (labels.length - 1) * spacing(item.class, 'gap', px);
        continue;
      }
      if (item !== cluster) {
        parts[nameOf(item)] = priceOf(item, opts);
        continue;
      }
      const readings = rowItems.filter((reading) => onRow(reading.class, px));
      parts['cluster gaps'] = (readings.length - 1) * spacing(item.class, 'gap', px);
      for (const reading of readings) parts[nameOf(reading)] = priceOf(reading, opts);
    }
    return parts;
  }

  const total = (parts: Record<string, number>) =>
    Object.values(parts).reduce((a, b) => a + b, 0);
  const itemised = (parts: Record<string, number>) =>
    Object.entries(parts)
      .map(([k, v]) => `${k} ${v.toFixed(1)}`)
      .join(', ');

  it('fits at every width the design verifies, with the worst count it can print', () => {
    // 390 and 1280 are DESIGN.md's own two (Responsive Behavior); 1024 is the
    // fold, where the row first carries the three failure readings and the
    // stop's two-press confirm together, and 640/768 are the widths between.
    for (const px of PRICED) {
      for (const digits of [1, 2, 3]) {
        const parts = rowCost(px, digits);
        expect(
          total(parts),
          `the row floor is ${total(parts).toFixed(1)}px in a ${px}px screen with a ` +
            `${digits}-digit count: ${itemised(parts)}`
        ).toBeLessThan(px);
      }
    }
  });

  it('cannot shrink at all below lg, so that arithmetic is the whole guarantee', () => {
    // Above the fold a sentence ellipsising is the row's release valve. Below
    // it there is no valve and none is wanted: every reading left on the phone
    // row is a mark, a count or a whole word, and the row fits by arithmetic or
    // it scrolls the page sideways.
    for (const item of rowItems.filter((reading) => onRow(reading.class, PHONE))) {
      for (const tag of [item, ...childrenOf(item)]) {
        expect(tag.class, `${nameOf(item)} can be squeezed on the phone row`).not.toMatch(
          /(?:^|\s)(?:min-w-0|truncate)(?:\s|$)/
        );
      }
    }
  });

  it('fits the phone row with a margin rather than to the pixel', () => {
    // The phone row is the one width where this arithmetic is EXACT: nothing on
    // it can give, every reading is a mark or a whole word, and headless
    // Chromium measures the same 368.9px this prices. So the margin here is a
    // real margin — what an addition to the phone row has to spend before it
    // reaches the viewport edge. (The three-digit case is tighter and is the
    // test above's job; 999 runs needing a human is not the common reading.)
    expect(PHONE - total(rowCost(PHONE, 1))).toBeGreaterThan(16);
    // Above the fold no margin is claimed, because the floor priced there is a
    // deliberate over-count — every `{#if}` branch at once — and a margin on an
    // upper bound would mean nothing. Chromium measures that floor at ~903px
    // against the ~1015px priced. What is asserted is only what matters: it
    // fits, at every width and every count.
  });
});

/**
 * #2333 (round 3), #2419 — the header folds where its own row stops fitting.
 *
 * The page scrolled sideways at 641px, one pixel above the `sm` fold the first
 * #2278 fix chose. Nothing on the epic page was at fault — the header was. The
 * row it folded to carried every reading at once (working, needs you, the
 * health badge, the repo, its hairline and More), every one of them `shrink-0`,
 * and a real browser measured ~741px before the page stopped scrolling. So the
 * nav ellipsised its three tabs to nothing, the gaps stayed, and the page still
 * had to scroll to reach `More`.
 *
 * `lg` rather than `md`, which clears that floor too: the row had a second one
 * above it. The three tab labels and their gaps put a READABLE row at ~929px,
 * so `md` would have traded the scrollbar for a nav reading `Wo… Mon… Ep…` — a
 * header that fits and no longer says which view you are on. `HEADER_FOLD` is
 * those two measurements and the breakpoint they chose.
 *
 * #2418 then rebuilt the row above: the nav cannot ellipsise at any width, the
 * full cluster waits until `xl`, and the row's width is priced off the markup
 * rather than measured once in a browser (`rowCost`). So neither floor is the
 * live number any more — what survives is the breakpoint and the rule that
 * chose it, which is the same rule DESIGN.md states for the rail: a fold is
 * arithmetic, not a feeling. Two things are asserted from that. `lg` still
 * clears both floors that were measured, and the header changes no horizontal
 * spacing at a width nothing prices — which is what `sm` was.
 */
describe('the header folds where its row stops fitting (#2333, #2419)', () => {
  /** Tailwind's own breakpoints — the only widths the header may fold on. */
  const TAILWIND = { sm: 640, md: 768, lg: 1024, xl: 1280 } as const;

  it('clears the floor under which the PAGE scrolled, which `sm` was 100px under', () => {
    expect(HEADER_FOLD.page).toBeGreaterThan(TAILWIND.sm);
    expect(HEADER_FOLD.fold).toBeGreaterThanOrEqual(HEADER_FOLD.page);
  });

  it('clears the floor under which the NAV ellipsised, which is why not `md`', () => {
    // `md` buys back the scrollbar and spends the tabs: a header that fits and
    // no longer says which view you are on. A fold has to clear both floors.
    expect(HEADER_FOLD.readable).toBeGreaterThan(HEADER_FOLD.page);
    expect(TAILWIND.md).toBeGreaterThan(HEADER_FOLD.page);
    expect(TAILWIND.md).toBeLessThan(HEADER_FOLD.readable);
    expect(HEADER_FOLD.fold).toBeGreaterThanOrEqual(HEADER_FOLD.readable);
  });

  it('takes the first Tailwind breakpoint that clears both', () => {
    expect(HEADER_FOLD.fold).toBe(TAILWIND.lg);
  });

  it('changes no horizontal spacing at a width nothing prices', () => {
    // #2333 asked for every spacing variant to be `lg:`, because `sm:` was the
    // tier nothing had measured — the whole of #2419. #2418 measures it: the
    // tier below the fold is spacing only now, and `PRICED` adds the row up at
    // both of its ends. So what is kept is the general rule that stood behind
    // the `lg`-only one, and a new tier fails here until the budget visits it.
    //
    // Scoped to horizontal spacing on purpose. A later reading may well arrive
    // on the row carrying `sm:` from whatever surface it was written for, and a
    // blanket "no `sm:` anywhere" here would fail the whole suite over a
    // vertical `sm:py-0`. The browser checks are what catch a reading that
    // actually costs width; this catches the geometry this file owns.
    const code = component.replace(/<!--[\s\S]*?-->|^\s*\/\/.*$/gm, '');
    const tiers = [...code.matchAll(/(?:max-)?(sm|md|lg|xl|2xl):(?:gap|px|space)-/g)].map(
      (m) => m[1] as keyof typeof TAILWIND
    );
    // The four the row is laid out by: the header's gutter and the gap after
    // the wordmark, the nav's gap between tabs, and the cluster's between
    // readings.
    expect(tiers.length).toBeGreaterThanOrEqual(4);
    for (const tier of new Set(tiers)) {
      expect(
        PRICED,
        `the row changes its spacing at ${tier}, a width nothing prices`
      ).toContain(TAILWIND[tier]);
    }
  });

  it('keeps the artboard numbers for the gutter and the gaps above the fold', () => {
    // The artboards are all drawn at 1440, so the artboard's 32px gutter and
    // its 40px/28px/24px gaps are the widest tier each of the three carries —
    // `sm:` for the header's own and the nav's, `lg:` for the cluster's, which
    // is where the cluster first has more than one reading to space apart.
    // Below those the row is tighter: `px-4`, `gap-3` and `gap-4` are 390px.
    expect(component).toContain('bg-ink px-4 sm:gap-10 sm:px-8');
    expect(component).toContain('tracking-[0.02em] sm:gap-7');
    expect(component).toContain('text-text-2 lg:gap-6');
  });
});
