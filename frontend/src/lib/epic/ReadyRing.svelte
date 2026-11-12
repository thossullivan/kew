<script lang="ts">
  // The ready-frontier ring: the driver can dispatch this node now (D36). One
  // component for the card (DagNode, around its status mark) and the legend's
  // "next up" entry, so the two can never be drawn in two colours again — the
  // legend once drew it in cyan while every card drew it grey. Grey because a
  // node waiting its turn is not moving, and cyan is reserved for what is
  // (DESIGN.md).
  //
  // `centred` lays the ring over the middle of its positioned parent (the
  // card's mark); without it the ring is an inline box of its own `size` with
  // the dot inside it, for a legend to draw.
  let { size, centred = false }: { size: number; centred?: boolean } = $props();
</script>

<span
  data-ready-ring
  aria-hidden="true"
  class="ring"
  class:centred
  style:width={`${size}px`}
  style:height={`${size}px`}
  style:margin={centred ? `${-size / 2}px 0 0 ${-size / 2}px` : undefined}
>
  {#if !centred}<span data-ready-dot class="dot"></span>{/if}
</span>

<style>
  .ring {
    display: inline-flex;
    flex-shrink: 0;
    align-items: center;
    justify-content: center;
    box-sizing: border-box;
    border-radius: 999px;
    border: 1px solid var(--text-4);
  }

  .centred {
    position: absolute;
    top: 50%;
    left: 50%;
  }

  .dot {
    width: 4px;
    height: 4px;
    border-radius: 999px;
    background: var(--text-4);
  }
</style>
