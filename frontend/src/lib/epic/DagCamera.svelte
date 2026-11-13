<script module lang="ts">
  import type { Viewport } from '@xyflow/svelte';

  /** One camera move. A NEW object per move: the effect below keys on its
   * identity, so asking for the same view twice still moves the camera back
   * to it after the reader has panned away. */
  export type CameraMove = { view: Viewport; duration: number };
</script>

<script lang="ts">
  // Moves the DAG canvas's camera (#2250).
  //
  // It has to be a child of `<SvelteFlow>`: writing the bound `viewport`
  // prop from outside repaints the graph but leaves the pan/zoom behaviour's
  // own transform where it was, so the next drag snapped back to the old
  // view. The store's `panZoom.setViewport` moves both, and the store is only
  // reachable from inside the flow's context.
  import { useStore } from '@xyflow/svelte';

  let { move = null }: { move?: CameraMove | null } = $props();

  const store = useStore();

  $effect(() => {
    const panZoom = store.panZoom;
    if (panZoom && move) void panZoom.setViewport(move.view, { duration: move.duration });
  });
</script>
