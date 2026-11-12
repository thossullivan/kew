<script lang="ts">
  import type { Snippet } from 'svelte';

  let {
    label,
    value,
    unit = '',
    delta = null,
    deltaGood = 'up',
    hint = '',
    icon
  }: {
    label: string;
    value: string | number;
    unit?: string;
    delta?: number | null;
    deltaGood?: 'up' | 'down';
    hint?: string;
    icon?: Snippet;
  } = $props();

  const deltaTone = $derived(
    delta == null || delta === 0
      ? 'var(--muted)'
      : (delta > 0) === (deltaGood === 'up')
        ? 'var(--success)'
        : 'var(--danger)'
  );
</script>

<div class="flex flex-col gap-1">
  <div class="flex items-center justify-between">
    <span class="eyebrow">{label}</span>
    {#if icon}<span class="text-faint">{@render icon()}</span>{/if}
  </div>
  <div class="flex items-baseline gap-1.5">
    <span class="nums font-display text-3xl text-fg">{value}</span>
    {#if unit}<span class="text-sm text-muted">{unit}</span>{/if}
    {#if delta != null}
      <span class="nums ml-auto text-xs font-medium" style:color={deltaTone}>
        {delta > 0 ? '↑' : delta < 0 ? '↓' : '·'}{Math.abs(delta).toFixed(2)}
      </span>
    {/if}
  </div>
  {#if hint}<span class="text-xs text-faint">{hint}</span>{/if}
</div>
