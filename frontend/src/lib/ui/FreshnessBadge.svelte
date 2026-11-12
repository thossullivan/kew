<script lang="ts">
  // The staleness badge (#2303): a NEW shape, quiet in `--text-3`, that carries
  // its cause rather than making the reader go and find out what moved.
  //
  // Three DESIGN.md rules decide everything here:
  //
  // * status is shape PLUS colour, and a new state gets a NEW shape — hence the
  //   chevron in `factory/markGeometry.ts`, which no status mark uses;
  // * `--text-3` is the labels-and-ids colour, so a badge about a fact sits at
  //   that weight and competes with nothing;
  // * magenta is rationed to "a person is the next move", so it is spent on one
  //   case only — `needsAPerson`, which `freshness.ts` sets for a conflict the
  //   factory cannot resolve and for nothing else.
  //
  // `dot` is the zoomed-out DAG card's variant: the same shape at the 7px size
  // the card's own marks use, with no word (there is no room for one) and the
  // label carried as the accessible name instead.
  //
  // There is no component-render test infra in this repo (see epic/render.ts),
  // so the mapping this draws is unit-tested in `lib/freshness.test.ts` and the
  // geometry in `factory/markGeometry.test.ts`.
  import { cn } from '$lib/utils';
  import { chevronPath, strokeWidth } from '$lib/factory/markGeometry';
  import type { MarkSize } from '$lib/factory/status';
  import type { StalenessBadge } from '$lib/freshness';

  let {
    badge,
    dot = false,
    size = 12,
    class: klass = ''
  }: {
    badge: StalenessBadge;
    /** The DAG card's variant: the mark alone, no word. */
    dot?: boolean;
    size?: MarkSize;
    class?: string;
  } = $props();

  const tone = $derived(badge.needsAPerson ? 'var(--accent)' : 'var(--text-3)');
  const px = $derived(dot ? 7 : size);
  // A chevron's stroke is the cross weight's sibling: thin enough to stay quiet
  // at `--text-3`, heavy enough that the shape survives a 7px dot.
  const stroke = $derived(strokeWidth('thin', px));
  // The path itself stays inside the viewBox at every size
  // (markGeometry.test.ts pins that), but a round stroke cap extends half a
  // stroke past each end of it — so nothing here clips.
  const svgStyle = 'display: block; overflow: visible';
</script>

<span
  class={cn('inline-flex items-center gap-1', dot && 'fb-dot', klass)}
  title={badge.causes.map((c) => c.text).join(' · ')}
>
  <svg
    width={px}
    height={px}
    viewBox={`0 0 ${px} ${px}`}
    role="img"
    aria-label={dot ? badge.label : undefined}
    aria-hidden={dot ? undefined : true}
    style={svgStyle}
  >
    <path
      d={chevronPath(px)}
      fill="none"
      stroke={tone}
      stroke-width={stroke}
      stroke-linecap="round"
      stroke-linejoin="round"
    />
  </svg>
  {#if !dot}
    <span class="fb-word" style:color={tone}>{badge.label}</span>
  {/if}
</span>

<style>
  /* The label-11 size the status rows and ids use, so the badge adds no sixth
     type size (DESIGN.md Don'ts). */
  .fb-word {
    font-size: 11px;
    line-height: 1;
    letter-spacing: 0.01em;
    white-space: nowrap;
  }

  /* On a zoomed-out card the mark is the whole badge, so it takes no gap. */
  .fb-dot {
    gap: 0;
  }
</style>
