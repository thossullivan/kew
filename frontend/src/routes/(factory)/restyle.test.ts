/**
 * FUI-27 — the legacy pages, drawn in the factory's own grammar.
 *
 * Runs, Run detail, Audit, Loops and the Epics list moved into the `(factory)`
 * group with the nav cutover (FUI-26) but kept the pre-factory look: boxed
 * `Card`s, pill `Badge`s and the Tailwind default type scale. Direction D draws
 * an instrument panel — hairline rules where a border used to enclose, the five
 * factory sizes, and one status mark per status word.
 *
 * There is no component renderer in this suite (see epic/render.ts), and no CI
 * run can take a screenshot, so the restyle is pinned where it is decided: the
 * page source. These assertions are deliberately about the *vocabulary* a page
 * paints in, not about its layout — a page is free to arrange its rows however
 * its artboard does, but it may not reach back for a card, a pill or a
 * `text-sm`, and a status must be a StatusMark rather than a colour this page
 * picked for itself.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

/** The five pages this issue restyles, by the name their tab shows. */
const PAGES: Array<[string, string]> = [
  ['runs', 'runs/+page.svelte'],
  ['run detail', 'runs/[id]/+page.svelte'],
  ['audit', 'audit/+page.svelte'],
  ['loops', 'loops/+page.svelte'],
  ['epics list', 'epics/+page.svelte']
].map(([name, path]) => [
  name,
  readFileSync(new URL(`./${path}`, import.meta.url), 'utf8')
]);

/**
 * The pre-factory components. `Card` is the boxed surface D replaces with a
 * rule, `Badge` the pill it replaces with a mark, `Stat` the figure `Readout`
 * supersedes, and `Input`/`CapBar` carry the old surface tokens in their own
 * markup — a factory page that mounts one paints a legacy control no matter
 * what the page around it says.
 */
const LEGACY_COMPONENTS = ['Card', 'Badge', 'Stat', 'Input', 'CapBar'];

/**
 * The pre-factory colour names. The factory ground is `--ink`, its ramp is
 * `--text-1..5` and its rules are `--hair*`; `fg`, `faint`, `muted`, `surface`
 * and `border` are the old palette the Rail pages were built on.
 */
const LEGACY_TOKENS = [
  'text-fg',
  'text-faint',
  'text-muted',
  'bg-surface',
  'border-border',
  'bg-accent-wash',
  'eyebrow'
];

/**
 * A font size that is not one of the five. Tailwind's `text-xs`…`text-xl` and
 * any hand-rolled rem size are the two ways the old pages sized type; the
 * arbitrary `text-[11px]` stays legal, because 11px without the uppercase
 * tracking of `.text-11` is what the artboards set a gauge axis and a ticker in
 * (see Gauge.svelte).
 */
const OFF_SCALE_TYPE = /text-(?:xs|sm|base|lg|xl|2xl|\[[0-9.]+rem\]|\[[0-9.]+em\])\b/;

describe.each(PAGES)('the %s page', (_name, source) => {
  it('mounts no boxed card, pill badge or legacy control', () => {
    for (const component of LEGACY_COMPONENTS) {
      // `\b` so that `<StatusMark` is not read as a `<Stat` with a suffix.
      expect(source, `<${component}> is a pre-factory component`).not.toMatch(
        new RegExp(`<${component}\\b`)
      );
    }
    const imports = source.match(/import \{[^}]*\} from '\$lib\/ui'/)?.[0] ?? '';
    for (const component of LEGACY_COMPONENTS) {
      expect(imports, `${component} imported from $lib/ui`).not.toMatch(
        new RegExp(`\\b${component}\\b`)
      );
    }
  });

  it('paints in the factory palette, never the pre-factory one', () => {
    for (const token of LEGACY_TOKENS) {
      expect(source, `${token} is a pre-factory token`).not.toContain(token);
    }
  });

  it('sets type on the five-size factory scale', () => {
    const offScale = source.match(new RegExp(OFF_SCALE_TYPE, 'g'));
    expect(offScale, `off-scale type: ${offScale?.join(', ')}`).toBeNull();
  });

  it('separates with hairlines, the factory divider', () => {
    expect(source).toMatch(/border-hair|dataRowClasses|<Rule\b/);
  });

  it('draws its statuses with the one status mark', () => {
    expect(source).toContain('StatusMark');
  });
});

/**
 * The readings the restyle dropped on its way off the pill `Badge` and the
 * `CapBar` (#2157 review). A pill carried a tone and a caption for free, and a
 * cap bar printed its own figures; a hairline page has to spell all of that out,
 * and the first draft of these four spots spelled out none of it.
 *
 * Pinned in the page source for the same reason as everything above: this suite
 * has no component renderer, so what a page paints is decided in its markup.
 */
describe('the readings a pill used to carry', () => {
  const sourceOf = (path: string) => readFileSync(new URL(`./${path}`, import.meta.url), 'utf8');

  /**
   * Every line a page spends on `safestAction.blockedBy`, comments dropped.
   * Scoped, because the tokens this block is pinned for are not rare on the
   * page as a whole — `text-warn` also paints the cost figure — and a guard
   * that reads the whole file passes on exactly the regression it is here to
   * catch. When the render sits in a conditional the whole conditional counts,
   * so the tone may live on the wrapper rather than the printed span.
   */
  const blockedByBlock = (source: string) => {
    const lines = source.match(/[^\n]*safestAction\.blockedBy[^\n]*\n?/g)?.join('') ?? '';
    const conditional = source.match(
      /\{#if[^}]*safestAction\.blockedBy\}[\s\S]*?\{\/if\}/
    )?.[0];
    return (conditional ?? lines).replace(/<!--[\s\S]*?-->/g, '');
  };

  it('colours each route claim by the tone the resolver gave it', () => {
    // A tone-blind drawer paints `substituted` and `refuted` in the same class
    // as `certified`, a reading the resolver's tones exist to keep apart.
    const source = sourceOf('runs/+page.svelte');
    expect(source, 'the claims drawer renders claim.label').toContain('claim.label');
    expect(source, 'claim.label is painted without claim.tone').toMatch(
      /CLAIM_TONE_TEXT\[claim\.tone\]/
    );
  });

  it('says "blocked by" and warns, when the safest action is blocked', () => {
    // `blockedBy` is a raw dimension id (`host_sequence`); on its own it names
    // no relation and reads as ordinary quiet meta rather than an obstruction.
    const block = blockedByBlock(sourceOf('runs/[id]/+page.svelte'));
    expect(block, 'blockedBy is not rendered at all').not.toBe('');
    expect(block, 'blockedBy prints without its "blocked by" label').toContain('blocked by ·');
    expect(block, 'a blocked action is not drawn in the warn tone').toContain('text-warn');
  });

  it('prints what the loop has spent, which its gauge does not', () => {
    // The CapBar this block replaced printed `$spent / $cap` in words. `Gauge`
    // captions itself only when over cap and derives its axis from the cap
    // alone, so a page that mounts one and prints nothing beside it shows an
    // under-cap loop no spend figure anywhere (factory/loops.test.ts renders the
    // gauge and pins that). `loopBudget` is the reading; this is that it is used.
    const source = sourceOf('loops/+page.svelte');
    expect(source, 'the budget block has no numeric reading').toContain('budget.figure');
    expect(source, 'the spend figure prints without its cap denominator').toContain(
      'budget.capLabel'
    );
  });

  it('derives the gate reading from the table that knows the terminal states', () => {
    // The first draft matched `achieved` and `needs_human` inline and fell
    // through to "gate pending" for everything else — including `unmet`, which
    // stops the poll, so the reading stayed pending for good. Both readings now
    // come from $lib/factory/loops, where one test can hold them to each other.
    const source = sourceOf('loops/+page.svelte');
    expect(source, 'the gate is derived inline again').toMatch(/loopGate\(/);
    expect(source, 'the poll keeps its own terminal set').toContain('LOOP_TERMINAL');
    expect(source, "a page-local terminal set can drift from the gate table").not.toMatch(
      /const TERMINAL\s*=/
    );
  });

  it('reads only the block, so a page-wide warn tone cannot stand in for it', () => {
    // The regression the assertion above is for: the tone is gone from the
    // block, and `text-warn` still appears further down the page on the cost
    // figure. A guard that matched the whole source would see nothing wrong.
    const tonelessPage = [
      '{#if view.safestAction.blockedBy}',
      '  <span>blocked by · {view.safestAction.blockedBy}</span>',
      '{/if}',
      '<div class="text-44 text-warn">{view.cost.amount}</div>'
    ].join('\n');
    expect(tonelessPage, 'the page-wide read is what made the guard vacuous').toContain(
      'text-warn'
    );
    expect(blockedByBlock(tonelessPage)).not.toContain('text-warn');
  });
});
