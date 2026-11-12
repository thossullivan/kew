<script module lang="ts">
  import { edgeStroke, type EdgeStyle } from './metro';

  /** What one edge draws, handed over as the Flow edge's `data`. */
  export type DagEdgeData = {
    /** The rounded orthogonal path through dagre's bend points, in flow
     * coordinates — which are the layout's own, so no re-projection. */
    d: string;
    /** The stroke `edgeStyle` gave this edge (pending/blocked/quiet/live). */
    style: EdgeStyle;
    /** Dimmed because some OTHER node's lineage is being hovered. */
    dim: boolean;
    /** Lit because the hovered node's lineage runs through it. */
    lit: boolean;
    /** On the SELECTED node's lineage, which DESIGN.md's dag-edge draws in
     * cyan for as long as the selection lasts. */
    sel: boolean;
    /** Whether anything is selected on the canvas at all. Cyan is the
     * selected lineage, so with nothing selected no edge may borrow it
     * (D21) — not even a live one. */
    anySelected: boolean;
  };
</script>

<script lang="ts">
  // One edge on the Svelte Flow canvas (#2250).
  //
  // The path is the layout's own orthogonal route, rounded
  // (`dagre-layout.roundedPath`) and carried on the edge's `data` — NOT
  // recomputed from the handle positions Svelte Flow hands every custom edge.
  // That route runs only along lanes and through the channels between layers,
  // so it never crosses a card; a bezier between two handles would cut
  // straight across the cards in between.
  //
  // The strokes are `metro.ts`'s `edgeStyle` and `edgeStroke` — the pieces of
  // that module the layout half's deletion leaves standing. Which of the two
  // colours an edge ends up in (its status's, or the selected lineage's cyan)
  // is `edgeStroke`'s decision, not this file's.
  // `data` is OPTIONAL because Svelte Flow's `EdgeTypes` says it is: every
  // custom edge has to accept the shape the library hands any edge, and an
  // edge with no data is one whose route nobody computed — so it draws
  // nothing rather than a line from the origin.
  let { data }: { data?: DagEdgeData } = $props();
</script>

{#if data}
  <path
    class="wire"
    class:dim={data.dim}
    class:lit={data.lit}
    d={data.d}
    fill="none"
    stroke={edgeStroke(data.style, {
      sel: data.sel,
      lit: data.lit,
      anySelected: data.anySelected
    })}
    stroke-width={data.lit || data.sel ? data.style.width + 0.75 : data.style.width}
    stroke-dasharray={data.style.dash ?? undefined}
    stroke-opacity={data.style.opacity ?? undefined}
  />
{/if}

<style>
  /* Hover lineage: the hovered node's cone brightens and the rest steps back
     (Dagster's reading, the brief's §2.3). `stroke-opacity` is the attribute
     each style may already set, so the dimming rides a CSS `opacity` on the
     element instead of fighting it. */
  .wire {
    transition: opacity 120ms ease-out;
  }

  .dim {
    opacity: 0.3;
  }
</style>
