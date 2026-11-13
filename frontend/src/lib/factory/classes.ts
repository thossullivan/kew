/**
 * FUI-05 — class builders for the factory UI's hairline parts.
 *
 * Direction D draws an instrument panel, not a deck of cards: a rule separates
 * where a border used to enclose, and every measurement in here comes off the
 * artboards in `frontend/design/factory-ui/` (DMonitor readouts y ≈88–258, the
 * live-runs header row y ≈320, the DWork action buttons x ≈1180–1392).
 *
 * The geometry lives here rather than in the components so it can be measured
 * by a plain unit test — in particular the promise that the pre-factory
 * `Button` variants still emit exactly the classes they did before.
 */
import type { ClaimTone } from '$lib/run-links';
import { cn } from '$lib/utils';

export type ButtonVariant = 'primary' | 'outline' | 'ghost' | 'danger' | 'factory' | 'needs-you';
export type ButtonSize = 'sm' | 'md' | 'lg' | 'factory';

/** The three hairline weights of the token set. */
export type Hairline = 'quiet' | 'hair' | 'strong';

/**
 * What a readout's figure means, which is what colours it. `money` is the
 * instrument yellow the gauges already use, so a spend figure and the track
 * under it read as one reading (DMonitor Today and This week, y ≈88–258).
 */
export type ReadoutTone = 'neutral' | 'working' | 'attention' | 'money';

export const BUTTON_BASE =
  'inline-flex items-center justify-center gap-2 rounded-[var(--radius-sm)] font-medium ' +
  'transition-[background,color,border,transform,box-shadow] duration-150 active:translate-y-px ' +
  'disabled:pointer-events-none disabled:opacity-45 select-none whitespace-nowrap';

export const BUTTON_VARIANTS: Record<ButtonVariant, string> = {
  primary:
    'bg-accent text-accent-fg hover:bg-accent-bright shadow-[0_1px_0_rgba(255,255,255,0.12)_inset,0_6px_16px_-8px_var(--accent)]',
  outline: 'border border-border-strong bg-surface-2 text-fg-dim hover:text-fg hover:border-faint',
  ghost: 'text-muted hover:text-fg hover:bg-surface-2',
  danger:
    'border border-[color-mix(in_oklab,var(--danger)_45%,transparent)] text-danger hover:bg-[color-mix(in_oklab,var(--danger)_12%,transparent)]',
  // The quiet instrument button: a hairline box on the ink ground, no fill.
  factory:
    'rounded-[4px] border border-hair-strong bg-transparent px-[18px] font-mono font-normal ' +
    'text-text-1 hover:border-text-5 hover:bg-ink-selected',
  // Rationed: solid magenta is the colour of "a human has to decide", so it is
  // only ever the one call to action on a needs-you band (DWork y ≈522–598).
  'needs-you':
    'rounded-[4px] bg-accent px-5 gap-2.5 font-mono font-semibold text-ink hover:bg-accent-bright'
};

/**
 * Sizes. The factory size carries height and type only — its horizontal
 * padding sits on the variant, because the artboard's magenta call to action
 * is wider (20px) than the outline buttons beside it (18px).
 *
 * `sm` is 13px, on DESIGN.md's scale, not Tailwind's 12px `text-xs` (#3094),
 * so no small button has to put itself back on the scale one at a time.
 * `button-sm` draws nothing: it is how a browser check finds every small
 * button on a page (`scripts/ui-checks/button-sm-type.mjs`).
 */
export const BUTTON_SIZES: Record<ButtonSize, string> = {
  sm: 'button-sm h-8 px-3 text-13',
  md: 'h-10 px-4 text-sm',
  lg: 'h-12 px-6 text-base',
  factory: 'h-11 text-13'
};

const FACTORY_VARIANTS: ReadonlySet<ButtonVariant> = new Set<ButtonVariant>([
  'factory',
  'needs-you'
]);

/** A factory variant is 44px unless the caller insists otherwise. */
export function defaultButtonSize(variant: ButtonVariant): ButtonSize {
  return FACTORY_VARIANTS.has(variant) ? 'factory' : 'md';
}

export function buttonClasses(
  opts: { variant?: ButtonVariant; size?: ButtonSize; class?: string } = {}
): string {
  const { variant = 'primary', size, class: extra } = opts;
  return cn(
    BUTTON_BASE,
    BUTTON_VARIANTS[variant],
    BUTTON_SIZES[size ?? defaultButtonSize(variant)],
    extra
  );
}

/**
 * D25 — "every control is at least 32px tall" at 390px (DESIGN.md, Responsive
 * Behavior). Two forms, because the rule reaches two kinds of control and only
 * one of them is a box:
 *
 * `TAP_PILL` is for a control that draws itself — a chip, a pill, a select.
 * It grows to 32px below `sm` and hands the artboard its own height back above
 * it, where a mouse is doing the pointing.
 *
 * `TAP_INLINE` is for a control that is a word in a sentence: an issue ref in
 * a holds line, a stream row's own text. Vertical padding on an inline box is
 * part of what the box is tapped by but not of the line height it sits in, so
 * this buys the press target without moving a single word — and with no
 * background or border to grow, it is invisible. 10px either side of a 13px
 * line clears 32px; above `sm` it comes off so a mouse row stays tight.
 *
 * Both are breakpoint-gated rather than flat because the factory artboards
 * draw a 22px pill at desktop widths. The shell header, whose controls are
 * 32px on the artboard too, carries a plain `h-8`/`min-h-8` instead.
 */
export const TAP_PILL = 'min-h-8 sm:min-h-0';
export const TAP_INLINE = 'py-2.5 sm:py-0';

const RULE_BG: Record<Hairline, string> = {
  quiet: 'bg-hair-quiet',
  hair: 'bg-hair',
  strong: 'bg-hair-strong'
};

const RULE_BORDER: Record<Hairline, string> = {
  quiet: 'border-hair-quiet',
  hair: 'border-hair',
  strong: 'border-hair-strong'
};

/**
 * DESIGN.md `chip` / `chip-active`: a toggle or filter is a pill, not a label.
 * "SHOW PROBLEMS ONLY" and "79 RETIRED · SHOW" were uppercase 11px words a
 * reader took for headings (#2787 D34, N17). data-13-mono in a hair-strong
 * outline; on, the cyan outline and wash. 32px of press below `sm`.
 */
export function chipClasses(active: boolean, extra?: string): string {
  return cn(
    'inline-flex cursor-pointer items-center gap-1.5 rounded-full border px-3 py-0.5 font-mono text-13',
    active
      ? 'border-accent-2 bg-accent-2-wash text-accent-2'
      : 'border-hair-strong text-text-2 hover:border-text-5 hover:text-text-1',
    TAP_PILL,
    extra
  );
}

/** A standalone hairline. */
export function ruleClasses(tone: Hairline = 'hair', extra?: string): string {
  return cn('h-px w-full border-0', RULE_BG[tone], extra);
}

/**
 * One row of a hairline table. Body rows are 56px (or 50px where the artboard
 * packs them tighter) over a quiet rule; a head row is sized by its own
 * padding and sits over the ordinary hairline, as in DMonitor's live runs.
 */
export function dataRowClasses(
  opts: {
    head?: boolean;
    height?: 56 | 50;
    rule?: Hairline | 'none';
    class?: string;
  } = {}
): string {
  const { head = false, height = 56, rule = head ? 'hair' : 'quiet', class: extra } = opts;
  return cn(
    'grid gap-5',
    head
      ? 'items-baseline pb-2.5 text-11 text-text-3'
      : cn('items-center', height === 50 ? 'h-[50px]' : 'h-14'),
    rule !== 'none' && `border-b ${RULE_BORDER[rule]}`,
    extra
  );
}

const FIGURE_TONE: Record<ReadoutTone, string> = {
  neutral: 'text-text-1',
  working: 'text-accent-2',
  attention: 'text-accent',
  money: 'text-warn'
};

/**
 * A readout cell: label, meta, figure, caption. `divided` draws the hairline
 * that separates it from the readout on its left; `edge` pulls the outer
 * padding off so the first and last cells align with the page gutter.
 *
 * A readout's meta is not the section head's meta, so it does not borrow that
 * string: "71.2% of cap" is drawn at 13px in --text-2 (DMonitor y ≈166), where
 * a head's "2 working" is 11px in --text-3 (y ≈294). Same slot, one reading
 * louder, because a readout's meta is itself a measurement.
 */
export function readoutClasses(
  opts: {
    tone?: ReadoutTone;
    divided?: boolean;
    edge?: 'start' | 'end';
    class?: string;
  } = {}
): { root: string; label: string; meta: string; figure: string; caption: string } {
  const { tone = 'neutral', divided = false, edge, class: extra } = opts;
  return {
    root: cn(
      'flex flex-col gap-2.5 px-7 pt-[22px] pb-5',
      divided && 'border-l border-hair',
      edge === 'start' && 'pl-0',
      edge === 'end' && 'pr-0',
      extra
    ),
    // One line each, label and meta (#2896 R12): at 390 a cell is half the
    // screen, and "THIS WEEK" and "31.5% of cap" each broke over two lines.
    label: 'text-11 whitespace-nowrap text-text-3',
    // 13px carries no tracking or case of its own, but it is not mono either.
    meta: 'ml-auto font-mono text-13 whitespace-nowrap text-text-2',
    // 26 below lg, 44 from there up (#2282 D3): four 44px figures do not fit
    // across a phone, and the 2×2 grid the Monitor folds into at that width
    // would clip every one of them. One step down the same five-size scale,
    // never a sixth size.
    figure: cn('text-26 lg:text-44', FIGURE_TONE[tone]),
    caption: 'text-15 text-text-2'
  };
}

/**
 * A section head: "LIVE RUNS" against right-aligned meta ("2 working"). The
 * meta stays sentence case — only the label shouts. A caption that is a
 * sentence ("names only — no value is ever read") is a `note` instead: data-13,
 * never label-11 with its case undone (#2787 D4).
 *
 * The label never wraps and never shrinks; the right-hand side gives way. Below
 * `sm` the row may break, and a note takes a line of its own under the label
 * and wraps there, never cut (#2952). From `sm` up it keeps one line.
 */
export function sectionHeadClasses(extra?: string): {
  root: string;
  label: string;
  meta: string;
  note: string;
} {
  return {
    // The 12px below the head is part of the head, not of the table under it.
    root: cn('mb-3 flex flex-wrap items-baseline gap-x-4 gap-y-1 sm:flex-nowrap', extra),
    label: 'shrink-0 whitespace-nowrap text-11 font-medium text-text-2',
    meta: 'ml-auto text-11 normal-case tracking-normal text-text-3',
    note: 'ml-auto min-w-0 wrap-anywhere text-13 text-text-3 max-sm:ml-0 max-sm:basis-full'
  };
}

/**
 * A route claim's tone, as a factory text colour.
 *
 * `routeClaims` is the one resolver for the three claims (run-links.ts), and
 * the tone it returns is part of the claim, not decoration a surface may drop:
 * `substituted` and `refuted` are danger, `verified live` and `certified` are
 * success, an expired probe is warn. The pre-factory surfaces carry that tone in
 * a pill `Badge`; a factory page has no pill, so it colours the claim's label
 * here instead. Without this, every claim in the set paints the same and a
 * refuted boundary reads exactly like a certified one.
 */
export const CLAIM_TONE_TEXT: Record<ClaimTone, string> = {
  neutral: 'text-text-1',
  accent: 'text-accent',
  success: 'text-success',
  warn: 'text-warn',
  danger: 'text-danger'
};
