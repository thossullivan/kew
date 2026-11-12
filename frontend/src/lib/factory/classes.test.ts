/**
 * FUI-05 — the hairline layout primitives' class builders.
 *
 * The factory surfaces are built from rules and rows, not boxed cards, so the
 * geometry lives in pure builders that these tests can measure against the
 * artboards in `frontend/design/factory-ui/`. Two things are pinned here that
 * a component test could not see:
 *
 *  1. the legacy `Button` variants still emit byte-identical class strings —
 *     the expected values below are a verbatim copy of `Button.svelte` as it
 *     stood before the factory variants were added;
 *  2. `cn()` keeps a factory type class next to a colour class. tailwind-merge
 *     reads an unknown `text-*` as a *colour*, so before the `font-size`
 *     extension in `$lib/utils`, `cn('text-44', 'text-accent')` silently threw
 *     the 44px away.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { cn } from '$lib/utils';
import {
  buttonClasses,
  type ButtonVariant,
  CLAIM_TONE_TEXT,
  dataRowClasses,
  readoutClasses,
  ruleClasses,
  sectionHeadClasses,
  chipClasses
} from './classes';

/** `Button.svelte` at ab277759, before FUI-05 touched it. */
const LEGACY_BASE =
  'inline-flex items-center justify-center gap-2 rounded-[var(--radius-sm)] font-medium ' +
  'transition-[background,color,border,transform,box-shadow] duration-150 active:translate-y-px ' +
  'disabled:pointer-events-none disabled:opacity-45 select-none whitespace-nowrap';
const LEGACY_VARIANTS = {
  primary:
    'bg-accent text-accent-fg hover:bg-accent-bright shadow-[0_1px_0_rgba(255,255,255,0.12)_inset,0_6px_16px_-8px_var(--accent)]',
  outline: 'border border-border-strong bg-surface-2 text-fg-dim hover:text-fg hover:border-faint',
  ghost: 'text-muted hover:text-fg hover:bg-surface-2',
  danger:
    'border border-[color-mix(in_oklab,var(--danger)_45%,transparent)] text-danger hover:bg-[color-mix(in_oklab,var(--danger)_12%,transparent)]'
} as const;
/** Except `sm`'s type and marker: 13px, not `text-xs`'s 12px, since #3094. */
const LEGACY_SIZES = {
  sm: 'button-sm h-8 px-3 text-13',
  md: 'h-10 px-4 text-sm',
  lg: 'h-12 px-6 text-base'
} as const;

type LegacyVariant = keyof typeof LEGACY_VARIANTS;
type LegacySize = keyof typeof LEGACY_SIZES;

const LEGACY_PAIRS = (Object.keys(LEGACY_VARIANTS) as LegacyVariant[]).flatMap((variant) =>
  (Object.keys(LEGACY_SIZES) as LegacySize[]).map((size) => [variant, size] as const)
);

describe('buttonClasses — the pre-factory variants', () => {
  it.each(LEGACY_PAIRS)('%s / %s is unchanged', (variant, size) => {
    expect(buttonClasses({ variant, size })).toBe(
      `${LEGACY_BASE} ${LEGACY_VARIANTS[variant]} ${LEGACY_SIZES[size]}`
    );
  });

  it('still defaults to md', () => {
    expect(buttonClasses({ variant: 'outline' })).toBe(
      buttonClasses({ variant: 'outline', size: 'md' })
    );
  });

  it('still defaults to the primary variant', () => {
    expect(buttonClasses()).toBe(buttonClasses({ variant: 'primary', size: 'md' }));
  });

  it('still appends a caller class last', () => {
    expect(buttonClasses({ variant: 'primary', class: 'w-full' })).toBe(
      `${buttonClasses({ variant: 'primary' })} w-full`
    );
  });
});

describe('buttonClasses — the small size', () => {
  const VARIANTS: ButtonVariant[] = ['primary', 'outline', 'ghost', 'danger', 'factory', 'needs-you'];

  it.each(VARIANTS)('sets %s / sm at 13px, on the type scale, never 12px text-xs', (variant) => {
    const cls = buttonClasses({ variant, size: 'sm' }).split(' ');
    expect(cls).toContain('text-13');
    expect(cls).not.toContain('text-xs');
    // The marker the browser check finds every small button by.
    expect(cls).toContain('button-sm');
  });

  it('keeps 13px and the marker beside a caller colour', () => {
    const cls = buttonClasses({ variant: 'factory', size: 'sm', class: 'text-text-2' }).split(' ');
    expect(cls).toContain('text-13');
    expect(cls).toContain('button-sm');
  });
});

describe('buttonClasses — the factory variants', () => {
  it('draws a 44px hairline outline at 4px, from DWork x 1180–1392', () => {
    const cls = buttonClasses({ variant: 'factory' });
    expect(cls).toContain('h-11'); // 44px
    expect(cls).toContain('rounded-[4px]');
    expect(cls).toContain('border-hair-strong');
    expect(cls).toContain('bg-transparent');
    expect(cls).toContain('text-text-1');
    expect(cls).toContain('text-13');
    expect(cls).not.toContain('rounded-[var(--radius-sm)]');
  });

  it('reserves the solid magenta for a needs-you call to action', () => {
    const cls = buttonClasses({ variant: 'needs-you' });
    expect(cls).toContain('bg-accent');
    expect(cls).toContain('text-ink');
    expect(cls).toContain('h-11');
    expect(cls).toContain('rounded-[4px]');
    // Solid, so no outline — the hairline is the quiet variant's job.
    expect(cls).not.toContain('border-hair-strong');
    // No other variant may claim the magenta ground.
    for (const variant of ['factory', 'outline', 'ghost', 'danger'] as const) {
      expect(buttonClasses({ variant }), variant).not.toContain('bg-accent');
    }
  });

  it('takes the 44px size without being asked, and yields to an explicit one', () => {
    expect(buttonClasses({ variant: 'factory' })).toContain('h-11');
    expect(buttonClasses({ variant: 'factory', size: 'sm' })).toContain('h-8');
    expect(buttonClasses({ variant: 'factory', size: 'sm' })).not.toContain('h-11');
  });

  it('keeps the two factory variants at the same height and radius', () => {
    const factory = buttonClasses({ variant: 'factory' });
    const needsYou = buttonClasses({ variant: 'needs-you' });
    for (const shared of ['h-11', 'rounded-[4px]', 'font-mono', 'text-13']) {
      expect(factory, shared).toContain(shared);
      expect(needsYou, shared).toContain(shared);
    }
  });
});

describe('cn keeps the factory type scale', () => {
  it('does not let a colour class eat the size', () => {
    expect(cn('text-44', 'text-accent')).toBe('text-44 text-accent');
    expect(cn('text-11', 'text-text-3')).toBe('text-11 text-text-3');
  });

  it('still lets one factory size override another', () => {
    expect(cn('text-13', 'text-15')).toBe('text-15');
    expect(cn('text-sm', 'text-13')).toBe('text-13');
  });
});

describe('ruleClasses', () => {
  it('is a one-pixel hairline in each tone', () => {
    expect(ruleClasses('quiet')).toBe('h-px w-full border-0 bg-hair-quiet');
    expect(ruleClasses()).toBe('h-px w-full border-0 bg-hair');
    expect(ruleClasses('strong')).toBe('h-px w-full border-0 bg-hair-strong');
  });
});

describe('dataRowClasses — DMonitor live runs, y ≈320', () => {
  it('is a 56px grid row under a quiet hairline', () => {
    const cls = dataRowClasses();
    expect(cls).toContain('grid');
    expect(cls).toContain('items-center');
    expect(cls).toContain('h-14'); // 56px
    expect(cls).toContain('border-b');
    expect(cls).toContain('border-hair-quiet');
  });

  it('offers the compact 50px row', () => {
    expect(dataRowClasses({ height: 50 })).toContain('h-[50px]');
    expect(dataRowClasses({ height: 50 })).not.toContain('h-14');
  });

  it('drops the hairline off the last row', () => {
    expect(dataRowClasses({ rule: 'none' })).not.toContain('border-b');
  });

  it('heads a table with 11px uppercase over the stronger hairline', () => {
    const head = dataRowClasses({ head: true });
    expect(head).toContain('text-11');
    expect(head).toContain('text-text-3');
    expect(head).toContain('border-hair');
    expect(head).not.toContain('border-hair-quiet');
    // A head row is sized by its own padding, not by a row height.
    expect(head).not.toContain('h-14');
    expect(head).toContain('pb-2.5'); // 10px
  });
});

describe('readoutClasses — DMonitor readouts, y ≈88–258', () => {
  it('stacks an 11px label, a 44px figure and a 15px caption', () => {
    const parts = readoutClasses();
    expect(parts.label).toContain('text-11');
    expect(parts.figure).toContain('text-44');
    expect(parts.caption).toContain('text-15');
    expect(parts.root).toContain('flex-col');
  });

  it('reads its meta one step louder than a section head reads its own', () => {
    const readout = readoutClasses().meta;
    const head = sectionHeadClasses().meta;
    // "71.2% of cap" is a measurement, so it is 13px in --text-2, where the
    // head's "2 working" is 11px in --text-3. The two must not converge.
    expect(readout).toContain('text-13');
    expect(readout).toContain('text-text-2');
    expect(readout).not.toContain('text-11');
    expect(readout).not.toContain('text-text-3');
    expect(readout).not.toBe(head);
    // 13px carries no face of its own, and the artboard draws the meta in mono.
    expect(readout).toContain('font-mono');
    // Still the right-aligned slot on the label's baseline.
    expect(readout).toContain('ml-auto');
    // …and 13px has no case or tracking to undo, unlike 11px.
    expect(readout).not.toContain('normal-case');
    expect(head).toContain('normal-case');
  });

  it('colours the figure by what the number means', () => {
    expect(readoutClasses({ tone: 'working' }).figure).toContain('text-accent-2');
    expect(readoutClasses({ tone: 'attention' }).figure).toContain('text-accent');
    expect(readoutClasses().figure).toContain('text-text-1');
    // The size survives the colour (see the cn tests above).
    expect(readoutClasses({ tone: 'working' }).figure).toContain('text-44');
  });

  it('divides neighbouring readouts with a hairline and goes flush at the edges', () => {
    expect(readoutClasses({ divided: true }).root).toContain('border-l');
    expect(readoutClasses().root).not.toContain('border-l');
    expect(readoutClasses({ edge: 'start' }).root).toContain('pl-0');
    expect(readoutClasses({ edge: 'end' }).root).toContain('pr-0');
  });
});

describe('sectionHeadClasses — "LIVE RUNS … 2 working"', () => {
  it('sets an 11px uppercase label against right-aligned meta', () => {
    const parts = sectionHeadClasses();
    expect(parts.label).toContain('text-11');
    expect(parts.label).toContain('text-text-2');
    expect(parts.meta).toContain('ml-auto');
    expect(parts.meta).toContain('text-text-3');
    // The meta reads "2 working", not "2 WORKING".
    expect(parts.meta).toContain('normal-case');
    expect(parts.root).toContain('items-baseline');
    expect(parts.root).toContain('mb-3'); // 12px, and the head owns it
  });
});

describe('sectionHeadClasses — a caption that is a sentence', () => {
  it('sets it in data-13, never in label-11 (#2787 D4)', () => {
    const { note, meta } = sectionHeadClasses();
    expect(note).toContain('text-13');
    expect(note).not.toContain('text-11');
    // The same right-aligned slot and colour the count meta takes.
    expect(note).toContain('ml-auto');
    expect(note).toContain('text-text-3');
    expect(meta).toContain('text-11');
  });
});

describe('sectionHeadClasses — the label holds, the note gives way', () => {
  it('never wraps or shrinks the label', () => {
    const { label } = sectionHeadClasses();
    expect(label).toContain('shrink-0');
    expect(label).toContain('whitespace-nowrap');
  });

  it('puts the note on its own line below sm, where it wraps and is never cut', () => {
    const { root, note } = sectionHeadClasses();
    // The row may break below sm, and keeps one line from sm up.
    expect(root).toContain('flex-wrap');
    expect(root).toContain('sm:flex-nowrap');
    // Below sm the note takes a full line of its own, left-aligned under the label.
    expect(note).toContain('max-sm:basis-full');
    expect(note).toContain('max-sm:ml-0');
    // It may shrink and wrap, and nothing clips it.
    expect(note).toContain('min-w-0');
    expect(note).toContain('wrap-anywhere');
    expect(note).not.toContain('truncate');
    // The type role, colour and right-aligned slot from sm up stay as they were.
    expect(note).toContain('text-13');
    expect(note).toContain('text-text-3');
    expect(note).toContain('ml-auto');
  });
});

describe('chipClasses — a toggle is a chip, not a label (#2787 D34)', () => {
  it('is a data-13-mono pill in a hairline, never label-11', () => {
    const off = chipClasses(false);
    expect(off).toContain('rounded-full');
    expect(off).toContain('border-hair-strong');
    expect(off).toContain('font-mono');
    expect(off).toContain('text-13');
    expect(off).not.toContain('text-11');
    expect(off).not.toContain('accent-2');
  });

  it('takes the cyan outline and wash when on, and keeps its press height', () => {
    const on = chipClasses(true);
    expect(on).toContain('border-accent-2');
    expect(on).toContain('bg-accent-2-wash');
    expect(on).not.toContain('border-hair-strong');
    expect(on).toContain('min-h-8');
  });
});

describe('every factory colour the builders name is a real token', () => {
  const CSS = readFileSync(new URL('../../app.css', import.meta.url), 'utf8');
  const NON_COLOUR = new Set(['bg-transparent', 'border-0', 'border-b', 'border-l', 'border']);

  const parts = readoutClasses({ tone: 'working', divided: true });
  const every = [
    buttonClasses({ variant: 'factory' }),
    buttonClasses({ variant: 'needs-you' }),
    dataRowClasses(),
    dataRowClasses({ head: true }),
    ruleClasses('quiet'),
    ruleClasses('strong'),
    parts.root,
    parts.label,
    parts.meta,
    parts.figure,
    parts.caption,
    ...Object.values(sectionHeadClasses()),
    chipClasses(false),
    chipClasses(true)
  ].join(' ');

  it.each([...new Set(every.split(' '))].filter((c) => /^(hover:)?(bg|text|border)-[a-z]/.test(c)))(
    '%s resolves to a --color-* token',
    (klass) => {
      const bare = klass.replace(/^hover:/, '');
      if (NON_COLOUR.has(bare)) return;
      const name = bare.replace(/^(bg|text|border)-/, '');
      expect(CSS, `--color-${name}`).toContain(`--color-${name}: var(`);
    }
  );
});

/**
 * The visual check FUI-05 asks for is an eyeball against the artboards, which
 * no CI can run. This is the part a machine *can* check: the builders and the
 * artboards have to agree on the actual numbers and the actual hairline
 * colours, so a drift in either one fails here rather than in a screenshot
 * somebody has to look at.
 */
describe('the builders measure what the artboards draw', () => {
  const DESIGN = new URL('../../../design/factory-ui/', import.meta.url);
  const DMONITOR = readFileSync(new URL('DMonitor.html', DESIGN), 'utf8');
  const DWORK = readFileSync(new URL('DWork.html', DESIGN), 'utf8');
  const CSS = readFileSync(new URL('../../app.css', import.meta.url), 'utf8');

  /** The dark value of a token — the artboards are drawn on the ink ground. */
  function darkHex(token: string): string {
    const dark = CSS.slice(0, CSS.indexOf("[data-theme='light']"));
    const found = dark.match(new RegExp(`--${token}:\\s*(#[0-9a-f]{3,8});`, 'i'));
    expect(found, `--${token}`).not.toBeNull();
    return found![1].toLowerCase();
  }

  /**
   * The size a factory type class is drawn at, read out of app.css. The five
   * sizes are declared with `@utility` rather than as `.class` rules (#2282
   * D3: so `lg:text-44` can exist), so both spellings are read here.
   */
  function typePx(klass: string): number {
    const found = CSS.match(
      new RegExp(`(?:\\.|@utility\\s+)${klass}\\s*\\{[^}]*font-size:\\s*(\\d+)px`)
    );
    expect(found, `.${klass}`).not.toBeNull();
    return Number(found![1]);
  }

  /** Every spacing class the builders emit, in the pixels Tailwind gives it. */
  const TAILWIND_PX: Record<string, number> = {
    'h-14': 56,
    'h-[50px]': 50,
    'h-11': 44,
    'gap-5': 20,
    'gap-2.5': 10,
    'pb-2.5': 10,
    'px-7': 28,
    'pt-[22px]': 22,
    'pb-5': 20,
    'px-[18px]': 18,
    'px-5': 20,
    'mb-3': 12,
    'rounded-[4px]': 4
  };

  const MEASURES = [
    {
      what: 'a live-run row is 56px',
      artboard: DMONITOR,
      declaration: 'align-items: center; height: 56px;',
      klass: 'h-14',
      px: 56,
      built: dataRowClasses()
    },
    {
      what: 'a table gutter is 20px',
      artboard: DMONITOR,
      declaration: '180px 160px 120px 120px; gap: 20px;',
      klass: 'gap-5',
      px: 20,
      built: dataRowClasses()
    },
    {
      what: 'a head row clears its rule by 10px',
      artboard: DMONITOR,
      declaration: 'gap: 20px; padding-bottom: 10px;',
      klass: 'pb-2.5',
      px: 10,
      built: dataRowClasses({ head: true })
    },
    {
      what: 'a section head clears its table by 12px',
      artboard: DMONITOR,
      declaration: 'align-items: baseline; margin-bottom: 12px;',
      klass: 'mb-3',
      px: 12,
      built: sectionHeadClasses().root
    },
    {
      what: 'a readout stacks on a 10px gap',
      artboard: DMONITOR,
      declaration: 'flex-direction: column; gap: 10px;',
      klass: 'gap-2.5',
      px: 10,
      built: readoutClasses().root
    },
    {
      what: 'a readout is inset 28px',
      artboard: DMONITOR,
      declaration: 'padding: 22px 28px 20px;',
      klass: 'px-7',
      px: 28,
      built: readoutClasses().root
    },
    {
      what: 'a readout sits 22px below the rule above it',
      artboard: DMONITOR,
      declaration: 'padding: 22px 28px 20px;',
      klass: 'pt-[22px]',
      px: 22,
      built: readoutClasses().root
    },
    {
      what: 'a factory button is 44px tall',
      artboard: DWORK,
      declaration: 'height: 44px; padding: 0 18px; border-radius: 4px;',
      klass: 'h-11',
      px: 44,
      built: buttonClasses({ variant: 'factory' })
    },
    {
      what: 'a factory button is 18px wide of its label',
      artboard: DWORK,
      declaration: 'height: 44px; padding: 0 18px; border-radius: 4px;',
      klass: 'px-[18px]',
      px: 18,
      built: buttonClasses({ variant: 'factory' })
    },
    {
      what: 'a factory button rounds by 4px',
      artboard: DWORK,
      declaration: 'height: 44px; padding: 0 18px; border-radius: 4px;',
      klass: 'rounded-[4px]',
      px: 4,
      built: buttonClasses({ variant: 'factory' })
    },
    {
      what: 'the needs-you call to action is 20px wide of its label',
      artboard: DWORK,
      declaration: 'padding: 0 20px; border-radius: 4px; background: #ff2a6d;',
      klass: 'px-5',
      px: 20,
      built: buttonClasses({ variant: 'needs-you' })
    }
  ];

  it.each(MEASURES)('$what', ({ artboard, declaration, klass, px, built }) => {
    expect(artboard, declaration).toContain(declaration);
    expect(declaration, `${px}px`).toContain(`${px}px`);
    expect(TAILWIND_PX[klass], klass).toBe(px);
    expect(built.split(' '), klass).toContain(klass);
  });

  /**
   * The type sizes, which the px map above cannot hold because they come from
   * the `.text-*` rules in app.css rather than from Tailwind's scale. This is
   * where a part that borrows another part's type string fails: a readout's
   * meta is 13px and a section head's is 11px, so one string cannot serve both.
   */
  const TYPE = [
    {
      what: 'a readout label is 11px',
      artboard: DMONITOR,
      declaration: 'font-size: 11px; letter-spacing: 0.14em; text-transform: uppercase;',
      klass: 'text-11',
      px: 11,
      built: readoutClasses().label
    },
    {
      what: "a readout's meta is 13px, not the section head's 11px",
      artboard: DMONITOR,
      declaration: 'monospace; font-size: 13px; color: #b5acc8;">71.2% of cap<',
      klass: 'text-13',
      px: 13,
      built: readoutClasses().meta
    },
    {
      what: 'a readout figure is 44px',
      artboard: DMONITOR,
      declaration: 'font-size: 44px; font-weight: 500; line-height: 1;',
      klass: 'text-44',
      px: 44,
      built: readoutClasses().figure,
      // The artboard's 44px is the readout from 1024 up; below that the same
      // scale is taken one step down (#2282 D3), so the builder carries the
      // size at a breakpoint rather than bare.
      builtAs: 'lg:text-44'
    },
    {
      what: 'a readout caption is 15px',
      artboard: DMONITOR,
      declaration: 'font-size: 15px; color: #b5acc8;',
      klass: 'text-15',
      px: 15,
      built: readoutClasses().caption
    },
    {
      what: 'a section head is 11px',
      artboard: DMONITOR,
      declaration:
        'font-size: 11px; letter-spacing: 0.14em; text-transform: uppercase; font-weight: 500;',
      klass: 'text-11',
      px: 11,
      built: sectionHeadClasses().label
    },
    {
      what: "a section head's meta is 11px",
      artboard: DMONITOR,
      declaration: 'monospace; font-size: 11px; color: #8d84a3;">2 working<',
      klass: 'text-11',
      px: 11,
      built: sectionHeadClasses().meta
    },
    {
      what: 'a factory button labels at 13px',
      artboard: DWORK,
      declaration: 'font-size: 13px;',
      klass: 'text-13',
      px: 13,
      built: buttonClasses({ variant: 'factory' })
    }
  ];

  it.each(TYPE)('$what', ({ artboard, declaration, klass, px, built, builtAs }) => {
    expect(artboard, declaration).toContain(declaration);
    expect(declaration, `${px}px`).toContain(`font-size: ${px}px`);
    expect(typePx(klass), klass).toBe(px);
    expect(built.split(' '), klass).toContain(builtAs ?? klass);
  });

  /**
   * Every declaration of a factory type class, read out of app.css — the
   * properties, not just the `font-size` the measures above pick off.
   */
  function typeDecls(klass: string): Record<string, string> {
    const found = CSS.match(new RegExp(`@utility\\s+${klass}\\s*\\{([^}]*)\\}`));
    const decls: Record<string, string> = {};
    for (const line of (found?.[1] ?? '').replace(/\/\*[\s\S]*?\*\//g, '').split(';')) {
      const [prop, ...rest] = line.split(':');
      if (rest.length) decls[prop.trim()] = rest.join(':').trim();
    }
    return decls;
  }

  /**
   * The readout figure takes the scale one step down below lg (#2282 D3), which
   * means `text-26` and `lg:text-44` are BOTH on the element from 1024px up —
   * the breakpoint swaps nothing, it only adds. So the size taken at lg has to
   * re-declare every property the size below it sets, or that property leaks
   * across the breakpoint: `text-44` with no `letter-spacing` of its own left
   * 26's -0.015em tracking on a 44px figure the artboard draws untracked, which
   * neither the size measures above nor a screenshot-free component test sees.
   */
  describe('the size taken at a breakpoint overrides the one under it', () => {
    const figure = readoutClasses().figure.split(' ');
    const base = figure.find((c) => /^text-\d+$/.test(c)) ?? '';
    const atLg = (figure.find((c) => /^lg:text-\d+$/.test(c)) ?? '').replace('lg:', '');

    it('composes two sizes of the one scale', () => {
      expect(base, 'a bare size below lg').toBe('text-26');
      expect(atLg, 'the artboard size from lg up').toBe('text-44');
    });

    it('re-declares, at lg, every property the size below it sets', () => {
      const under = typeDecls(base);
      expect(Object.keys(under), `@utility ${base}`).not.toHaveLength(0);
      for (const prop of Object.keys(under)) {
        expect(Object.keys(typeDecls(atLg)), `${atLg} { ${prop} }`).toContain(prop);
      }
    });

    it('tracks the 44px figure as the artboard draws it', () => {
      const drawn = DMONITOR.match(/style="[^"]*font-size: 44px[^"]*"/);
      expect(drawn, 'a 44px readout on the artboard').not.toBeNull();
      expect(drawn![0], 'the artboard draws it untracked').not.toContain('letter-spacing');
      expect(typeDecls(atLg)['letter-spacing'], `${atLg} tracking`).toBe('normal');
    });
  });

  const COLOURS = [
    {
      what: 'a body row rule is --hair-quiet',
      artboard: DMONITOR,
      declaration: 'border-bottom: 1px solid #1a1528',
      token: 'hair-quiet',
      klass: 'border-hair-quiet',
      built: dataRowClasses()
    },
    {
      what: 'a head row rule is --hair',
      artboard: DMONITOR,
      declaration: 'color: #8d84a3; border-bottom: 1px solid #221c33',
      token: 'hair',
      klass: 'border-hair',
      built: dataRowClasses({ head: true })
    },
    {
      what: 'the rule between readouts is --hair',
      artboard: DMONITOR,
      declaration: 'border-left: 1px solid #221c33',
      token: 'hair',
      klass: 'border-hair',
      built: readoutClasses({ divided: true }).root
    },
    {
      what: 'a readout label is --text-3',
      artboard: DMONITOR,
      declaration: 'text-transform: uppercase; color: #8d84a3;',
      token: 'text-3',
      klass: 'text-text-3',
      built: readoutClasses().label
    },
    {
      what: "a readout's meta is --text-2, brighter than its label",
      artboard: DMONITOR,
      declaration: 'font-size: 13px; color: #b5acc8;',
      token: 'text-2',
      klass: 'text-text-2',
      built: readoutClasses().meta
    },
    {
      what: "a section head's meta is --text-3, quieter than a readout's",
      artboard: DMONITOR,
      declaration: 'monospace; font-size: 11px; color: #8d84a3;">2 working<',
      token: 'text-3',
      klass: 'text-text-3',
      built: sectionHeadClasses().meta
    },
    {
      what: 'a readout caption is --text-2',
      artboard: DMONITOR,
      declaration: 'font-size: 15px; color: #b5acc8;',
      token: 'text-2',
      klass: 'text-text-2',
      built: readoutClasses().caption
    },
    {
      what: 'a working figure is --accent-2',
      artboard: DMONITOR,
      declaration: 'font-size: 44px; font-weight: 500; line-height: 1; color: #05d9e8;',
      token: 'accent-2',
      klass: 'text-accent-2',
      built: readoutClasses({ tone: 'working' }).figure
    },
    {
      what: 'a needs-you figure is --accent',
      artboard: DMONITOR,
      declaration: 'font-size: 44px; font-weight: 500; line-height: 1; color: #ff2a6d;',
      token: 'accent',
      klass: 'text-accent',
      built: readoutClasses({ tone: 'attention' }).figure
    },
    {
      what: 'a factory button is outlined in --hair-strong',
      artboard: DWORK,
      declaration: 'border-radius: 4px; border: 1px solid #342a4a;',
      token: 'hair-strong',
      klass: 'border-hair-strong',
      built: buttonClasses({ variant: 'factory' })
    },
    {
      what: 'the needs-you call to action is filled with --accent',
      artboard: DWORK,
      declaration: 'border-radius: 4px; background: #ff2a6d;',
      token: 'accent',
      klass: 'bg-accent',
      built: buttonClasses({ variant: 'needs-you' })
    },
    {
      what: 'the needs-you label is --ink, on the magenta',
      artboard: DWORK,
      declaration: 'background: #ff2a6d; color: #0a0714;',
      token: 'ink',
      klass: 'text-ink',
      built: buttonClasses({ variant: 'needs-you' })
    }
  ];

  it.each(COLOURS)('$what', ({ artboard, declaration, token, klass, built }) => {
    expect(artboard, declaration).toContain(declaration);
    expect(declaration.toLowerCase(), `--${token}`).toContain(darkHex(token));
    expect(built.split(' '), klass).toContain(klass);
  });
});

/**
 * A route claim's tone survives the pill's removal.
 *
 * `routeClaims` returns a tone per claim, and the pre-factory surfaces render it
 * as a `Badge` colour. A factory page has no pill, so it needs a text colour for
 * the same five tones — and it needs them to be *different*, which is the part a
 * page that dropped the tone and hard-coded `text-text-1` still satisfied.
 */
describe('CLAIM_TONE_TEXT', () => {
  /** Every member of `ClaimTone` (run-links.ts), read from the union's source. */
  const TONES = readFileSync(new URL('../run-links.ts', import.meta.url), 'utf8')
    .match(/export type ClaimTone =([^;]+);/)![1]
    .match(/'([a-z]+)'/g)!
    .map((quoted) => quoted.slice(1, -1));

  it('covers every ClaimTone the claim resolver can return', () => {
    expect(TONES.length).toBeGreaterThan(1);
    for (const tone of TONES) {
      expect(CLAIM_TONE_TEXT, `no class for tone ${tone}`).toHaveProperty(tone);
    }
    expect(Object.keys(CLAIM_TONE_TEXT).sort()).toEqual([...TONES].sort());
  });

  it('gives danger, warn and success each their own colour', () => {
    const classes = Object.values(CLAIM_TONE_TEXT);
    expect(new Set(classes).size, `collapsed tones: ${classes.join(', ')}`).toBe(classes.length);
    expect(CLAIM_TONE_TEXT.danger).toBe('text-danger');
    expect(CLAIM_TONE_TEXT.warn).toBe('text-warn');
    expect(CLAIM_TONE_TEXT.success).toBe('text-success');
  });
});
