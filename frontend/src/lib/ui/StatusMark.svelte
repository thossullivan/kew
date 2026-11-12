<script lang="ts">
  // The status mark: a shape that carries the meaning, optionally with its word
  // (FUI-04). Shape and colour come from `statusShape`, and every number comes
  // from `factory/markGeometry` — this file only decides which SVG element
  // carries which of them.
  //
  // There is no component-render test infra in this repo (see epic/render.ts),
  // so the mapping is unit-tested in factory/status.test.ts and the geometry in
  // factory/markGeometry.test.ts — extents asserted against the SVG
  // frontend/design/factory-ui/ emits at 10, 12, 14 and 18, and against the
  // 7px CSS circles of the DWork rows at 7.
  import { cn } from '$lib/utils';
  import {
    QUIET_MEANINGS,
    statusShape,
    type MarkSize,
    type StatusVocabulary
  } from '$lib/factory/status';
  import {
    barWidth,
    barX,
    barsWidth,
    crossPath,
    dashArray,
    DIAMOND_HALO_HALF,
    DOT_R,
    glyphSize,
    halfDiscPath,
    HALO_DOT_R,
    HALO_R,
    hatchLines,
    RING_R,
    RING_X_HALF,
    SQUARE_HALF,
    strokeWidth,
    X_HALF,
    type StrokeWeight
  } from '$lib/factory/markGeometry';

  let {
    kind,
    vocabulary = 'epic',
    size = 12,
    word = false,
    bg = 'var(--ink)',
    label,
    class: klass = ''
  }: {
    /** A status word from any of the app's vocabularies. Unknown reads as todo. */
    kind: string | null | undefined;
    /**
     * Which vocabulary `kind` came from. A run-list row showing kpi.ts's
     * `displayStatus` must say `'run'`, or its transient `queued` reads as the
     * epic snapshot's unarmed node.
     */
    vocabulary?: StatusVocabulary;
    size?: MarkSize;
    /** Show the status word after the mark — D's "dot plus word" pairing. */
    word?: boolean;
    /** What a hollow mark is filled with, so it hides the edge it sits on. */
    bg?: string;
    /** Accessible name; defaults to the status word. */
    label?: string;
    class?: string;
  } = $props();

  const mark = $derived(statusShape(kind, vocabulary));
  const wordColor = $derived(QUIET_MEANINGS.has(mark.meaning) ? 'var(--text-3)' : mark.color);

  function stroke(weight: StrokeWeight): number {
    return strokeWidth(weight, size);
  }

  const c = $derived(size / 2);
  const glyph = $derived(glyphSize(size));

  const svgWidth = $derived(mark.shape === 'bars' ? barsWidth(size) : size);
  // A halo and the diamond's corners sit outside the box; never clip them.
  const svgStyle = 'display: block; overflow: visible';
</script>

<span class={cn('inline-flex max-w-full min-w-0 items-center gap-1.5', klass)}>
  <svg
    class="shrink-0"
    width={svgWidth}
    height={size}
    viewBox={`0 0 ${svgWidth} ${size}`}
    role={word ? undefined : 'img'}
    aria-label={word ? undefined : (label ?? mark.word)}
    aria-hidden={word ? true : undefined}
    style={svgStyle}
  >
    {#if mark.shape === 'ring' || mark.shape === 'ring-thick' || mark.shape === 'ring-dashed'}
      <circle
        cx={c}
        cy={c}
        r={glyph * RING_R}
        fill={bg}
        stroke={mark.color}
        stroke-width={stroke(mark.shape === 'ring-thick' ? 'thick' : 'thin')}
        stroke-dasharray={mark.shape === 'ring-dashed' ? dashArray(size) : undefined}
      />
    {:else if mark.shape === 'ring-half'}
      <circle
        cx={c}
        cy={c}
        r={glyph * RING_R}
        fill={bg}
        stroke={mark.color}
        stroke-width={stroke('thin')}
      />
      <path d={halfDiscPath(size)} fill={mark.color} />
    {:else if mark.shape === 'dot'}
      <circle cx={c} cy={c} r={glyph * DOT_R} fill={mark.color} />
    {:else if mark.shape === 'dot-halo'}
      <circle cx={c} cy={c} r={glyph * HALO_R} fill={mark.color} fill-opacity="0.18" />
      <circle cx={c} cy={c} r={glyph * HALO_DOT_R} fill={mark.color} />
    {:else if mark.shape === 'x'}
      <path
        d={crossPath(size, X_HALF)}
        stroke={mark.color}
        stroke-width={stroke('cross')}
        stroke-linecap="round"
      />
    {:else if mark.shape === 'ring-x'}
      <circle
        cx={c}
        cy={c}
        r={glyph * RING_R}
        fill={bg}
        stroke={mark.color}
        stroke-width={stroke('faint')}
      />
      <path d={crossPath(size, RING_X_HALF)} stroke={mark.color} stroke-width={stroke('faint')} />
    {:else if mark.shape === 'hatch-square'}
      <rect
        x={c - glyph * SQUARE_HALF}
        y={c - glyph * SQUARE_HALF}
        width={glyph * SQUARE_HALF * 2}
        height={glyph * SQUARE_HALF * 2}
        fill={bg}
        stroke={mark.color}
        stroke-width={stroke('thin')}
      />
      {#each hatchLines(size) as line, i (i)}
        <line
          x1={line.x1}
          y1={line.y1}
          x2={line.x2}
          y2={line.y2}
          stroke={mark.color}
          stroke-width="1"
        />
      {/each}
    {:else if mark.shape === 'diamond-halo'}
      <rect
        x={c - glyph * DIAMOND_HALO_HALF}
        y={c - glyph * DIAMOND_HALO_HALF}
        width={glyph * DIAMOND_HALO_HALF * 2}
        height={glyph * DIAMOND_HALO_HALF * 2}
        fill={mark.color}
        fill-opacity="0.16"
        transform={`rotate(45 ${c} ${c})`}
      />
      <rect
        x={c - glyph * SQUARE_HALF}
        y={c - glyph * SQUARE_HALF}
        width={glyph * SQUARE_HALF * 2}
        height={glyph * SQUARE_HALF * 2}
        fill={mark.color}
        transform={`rotate(45 ${c} ${c})`}
      />
    {:else if mark.shape === 'bars'}
      {#each [0, 1, 2] as i (i)}
        <rect x={barX(size, i)} y="0" width={barWidth(size)} height={size} fill={mark.color} />
      {/each}
    {/if}
  </svg>
  {#if word}
    <!-- #2848 P19 — a status word is one label: `needs you` never breaks
         into NEEDS / YOU in a narrow cell. A word too long for its cell (an
         unknown state a newer server echoes) ends in an ellipsis instead of
         running into its neighbour; the text stays whole in the DOM, so the
         accessible name is the full word, and so is the hover text.
         `data-status-word` is the hook the 390 sweep reads (#2898). -->
    <span
      data-status-word
      class="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap"
      style:color={wordColor}
      title={label ?? mark.word}>{label ?? mark.word}</span
    >
  {/if}
</span>
