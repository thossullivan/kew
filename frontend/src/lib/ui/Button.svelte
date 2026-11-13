<script lang="ts">
  import type { Snippet } from 'svelte';
  import { buttonClasses, type ButtonSize, type ButtonVariant } from '$lib/factory/classes';

  let {
    variant = 'primary',
    size = undefined,
    href = undefined,
    type = 'button',
    disabled = false,
    class: klass = '',
    onclick = undefined,
    children,
    ...rest
  }: {
    variant?: ButtonVariant;
    /** Defaults to `md`, or to the 44px factory size for a factory variant. */
    size?: ButtonSize;
    href?: string;
    type?: 'button' | 'submit';
    disabled?: boolean;
    class?: string;
    onclick?: (e: MouseEvent) => void;
    children: Snippet;
    [key: string]: unknown;
  } = $props();

  const cls = $derived(buttonClasses({ variant, size, class: klass }));
</script>

{#if href}
  <a {href} {onclick} class={cls} {...rest}>{@render children()}</a>
{:else}
  <button {type} {disabled} {onclick} class={cls} {...rest}>{@render children()}</button>
{/if}
