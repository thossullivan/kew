<script lang="ts">
  import type { Snippet } from 'svelte';

  let {
    label = '',
    hint = '',
    children
  }: {
    label?: string;
    hint?: string;
    children: Snippet;
  } = $props();
</script>

<label class="flex flex-col gap-1.5">
  {#if label}<span class="eyebrow">{label}</span>{/if}
  {@render children()}
  {#if hint}<span class="text-xs text-faint">{hint}</span>{/if}
</label>
