<script lang="ts">
  import '../app.css';
  import { onMount } from 'svelte';
  import type { Snippet } from 'svelte';

  let { children }: { children: Snippet } = $props();

  let grain = $state('');

  /* Procedural grain — cheaper than shipping a noise PNG, and it keeps the
     flat dark canvas reading as material rather than as an empty div. */
  onMount(() => {
    const size = 180;
    const c = document.createElement('canvas');
    c.width = c.height = size;
    const ctx = c.getContext('2d');
    if (!ctx) return;
    const img = ctx.createImageData(size, size);
    for (let i = 0; i < img.data.length; i += 4) {
      const v = (Math.random() * 255) | 0;
      img.data[i] = img.data[i + 1] = img.data[i + 2] = v;
      img.data[i + 3] = 10;
    }
    ctx.putImageData(img, 0, 0);
    grain = c.toDataURL();
  });
</script>

<a class="skip" href="#content">Skip to content</a>

{#if grain}
  <div class="grain" style:background-image="url({grain})"></div>
{/if}

{@render children()}

<style>
  .grain {
    position: fixed;
    inset: 0;
    z-index: 1;
    pointer-events: none;
    opacity: 0.4;
    background-size: 180px 180px;
  }
</style>
