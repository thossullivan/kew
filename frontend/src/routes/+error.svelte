<script lang="ts">
  // The root error page (design review c9 X7). An address no route matches —
  // and any error no nearer page catches — renders here, inside the root
  // layout only: the `(factory)` group's layout, with its header, is not
  // above this file. So it draws the same column that layout draws — the
  // chrome slots (the demo build's banner and foot) and the factory header —
  // and says in one sentence what happened and where to go, where the
  // framework's bare default said "404 Not Found" on a blank page.
  import { page } from '$app/state';
  import FactoryHeader from '$lib/shell/FactoryHeader.svelte';
  import { chromeSlots } from '$lib/shell/chrome-slots.svelte';
  import { factoryPulse } from '$lib/shell/pulse.svelte';
  import { TAP_INLINE } from '$lib/factory/classes';

  // The header's readings come from the one shared poll, as on every page.
  $effect(() => factoryPulse.subscribe());

  const missing = $derived(page.status === 404);
  /** The error is Work's own, where a link to Work would loop. */
  const onWork = $derived(page.url.pathname === '/work');
  /** The error's own words, as text: never HTML. */
  const detail = $derived((page.error?.message ?? '').trim());
</script>

<svelte:head><title>{missing ? 'Not found' : 'Error'} · kew</title></svelte:head>

<div class="flex h-dvh flex-col bg-ink text-text-1 tabular-nums">
  {#if chromeSlots.banner}
    <chromeSlots.banner />
  {/if}
  <FactoryHeader />
  <main class="flex min-h-0 flex-1 flex-col overflow-y-auto px-4 pt-10 pb-9 sm:px-10" data-error-page>
    <p data-status-word class="m-0 text-11 whitespace-nowrap text-text-3">{page.status}</p>
    <!-- A sentence, so Plex Sans: the base `h1` rule sets the mono title face. -->
    <h1 class="m-0 mt-3 max-w-[60ch] font-sans text-15 font-medium tracking-normal text-text-1">
      {#if missing}
        That page does not exist.
      {:else}
        This page could not be drawn.
      {/if}
      {#if onWork}
        <!-- The error came from Work itself, so "Go to Work" would land here
             again: the way out is to read the page once more. -->
        <button
          type="button"
          data-error-retry
          onclick={() => location.reload()}
          class={`cursor-pointer text-text-1 underline underline-offset-4 hover:text-text-2 ${TAP_INLINE}`}
          >Try again.</button
        >
      {:else}
        <a href="/work" class={`text-text-1 underline underline-offset-4 hover:text-text-2 ${TAP_INLINE}`}
          >Go to Work.</a
        >
      {/if}
    </h1>
    {#if !missing && detail !== ''}
      <p class="m-0 mt-2 max-w-[80ch] font-mono text-13 text-text-3 [overflow-wrap:anywhere]">{detail}</p>
    {/if}
  </main>
  {#if chromeSlots.foot}
    <chromeSlots.foot />
  {/if}
</div>
