<script lang="ts">
  /**
   * FUI-05 — one row of a hairline table (DMonitor live runs, y ≈320). The
   * caller owns the columns, because every table has its own; this owns the
   * height, the gutter and the rule underneath.
   */
  import type { Snippet } from 'svelte';
  import { dataRowClasses, type Hairline } from '$lib/factory/classes';

  let {
    columns,
    head = false,
    height = 56,
    rule = undefined,
    class: klass = '',
    children,
    ...rest
  }: {
    /** A `grid-template-columns` value, e.g. `96px minmax(0, 1fr) 180px`. */
    columns: string;
    head?: boolean;
    height?: 56 | 50;
    /** Defaults to `quiet`, or `hair` under a head row. `none` ends a table. */
    rule?: Hairline | 'none';
    class?: string;
    children: Snippet;
    [key: string]: unknown;
  } = $props();

  const cls = $derived(dataRowClasses({ head, height, rule, class: klass }));
</script>

<div class={cls} style:grid-template-columns={columns} {...rest}>{@render children()}</div>
