<script lang="ts">
  // An item's title with its markdown code spans drawn as inline mono (#2848,
  // P13), never as backticks. Inline, so it inherits the size, colour and line
  // clamp of whatever holds it; the caller's `title` attribute, where it has
  // one, takes `plainTitle` from the same module.
  import { titleSpans } from '$lib/factory/names';

  let { text }: { text: string } = $props();

  const spans = $derived(titleSpans(text));
</script>

{#each spans as span, i (i)}{#if span.mono}<code class="font-mono">{span.text}</code
    >{:else}{span.text}{/if}{/each}
