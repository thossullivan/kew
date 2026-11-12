<script lang="ts">
  // The factory shell (FUI-09). A route group changes no URL: `/(factory)/work`
  // is served at `/work`, so moving a page in here swaps the Rail for this
  // header and nothing else. The Rail keeps every route outside the group
  // (routes/+layout.svelte decides by route id).
  //
  // `tabular-nums` is set once here rather than on every factory type utility:
  // the counters, spend readouts and elapsed columns all need figures that do
  // not jitter as they tick.
  //
  // The group filled in tab by tab, each depending on this shell existing
  // first: `/monitor` landed with FUI-22, `/work` with FUI-18 and the move of
  // the epic page with FUI-14. `shell.test.ts` names any tab whose page has not
  // landed yet and fails the day one of them quietly does, so a gap stays
  // declared rather than discovered. The header's pixel reference (DWork y 0-56)
  // is checked in `factory-header.test.ts`, which reads the artboard directly,
  // because no CI run can take a screenshot.
  import Palette from '$lib/palette/Palette.svelte';
  import FactoryHeader from '$lib/shell/FactoryHeader.svelte';
  import HelpOverlay from '$lib/shell/HelpOverlay.svelte';
  import { chromeSlots } from '$lib/shell/chrome-slots.svelte';
  import { factoryPulse } from '$lib/shell/pulse.svelte';
  import type { Snippet } from 'svelte';

  let { children }: { children: Snippet } = $props();

  // One poll for the whole group: the store starts it for the first subscriber
  // and stops it when the last factory surface unmounts.
  $effect(() => factoryPulse.subscribe());
</script>

<!-- `h-dvh`, not a minimum height (#2278 C4). A floor is not a ceiling: a list
     longer than the window grew the document instead of its own pane, and the
     whole page scrolled — the 56px header and the Work detail slid away with
     the rows the reader was walking. Exactly one viewport tall, `main` taking
     what the header leaves and clipping the rest, puts the scrolling on
     whichever pane inside a page owns it (DESIGN.md, Grid: "pages fill the
     viewport height"). A page that is one long column carries its own
     `overflow-y-auto`; `shell.test.ts` reads every page in the group for it. -->
<div class="flex h-dvh flex-col bg-ink text-text-1 tabular-nums">
  <!-- The chrome slots (#3139): empty, and drawing nothing, unless something
       fills `chromeSlots` — the demo build's banner here, its page foot below.
       Rows of this column, so they take height from `main` and never cover it. -->
  {#if chromeSlots.banner}
    <chromeSlots.banner />
  {/if}
  <FactoryHeader />
  <main class="flex min-h-0 flex-1 flex-col overflow-hidden">
    {@render children()}
  </main>
  {#if chromeSlots.foot}
    <chromeSlots.foot />
  {/if}
</div>
<!-- Cmd-K (#2516): every factory surface, one palette. -->
<Palette />
<!-- `?` anywhere in the group (#2513): the surface's question and its keys. -->
<HelpOverlay />
