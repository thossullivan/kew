<script lang="ts">
  // The demo's Tours entry (#3145), in the header's chrome slot at the top of
  // the More menu. From `lg` up it lists the tours; below, the tours do not
  // run, and one line says where they do instead of a list that does nothing.
  //
  // It is also where the tours meet the router: it is mounted on every
  // factory page for as long as the header is, so its `afterNavigate` reads a
  // deep link on the first load and on every navigation after (`page.state`
  // first, then the address bar, `navigated` in `controller.ts`), and it ends
  // an open tour when the window narrows below 1024.
  import { afterNavigate } from '$app/navigation';
  import { TOURS_DESKTOP_ONLY as DESKTOP_ONLY } from '../banner.js';
  import { endBelowDesktop, endTour, navigated, startTour, TOURS } from './controller';

  let entry: HTMLElement | undefined = $state();

  /**
   * The `More` press this entry sits under. A tour a deep link opened has no
   * press that opened it, and ending it would drop focus on `<body>`; it goes
   * here instead, where a tour started from the menu returns: a control that
   * takes focus as it is, and the place the tours are started from again.
   */
  function morePress(): HTMLElement | null {
    return entry?.closest('details')?.querySelector('summary') ?? null;
  }

  afterNavigate(() => navigated(morePress()));

  $effect(() => {
    const stop = endBelowDesktop();
    return () => {
      stop();
      endTour();
    };
  });

  /**
   * Start from the menu: close it, and give focus back to its `More` press
   * when the tour ends — the press that opened it, now that the menu is shut.
   */
  function open(id: string, event: MouseEvent): void {
    const details = (event.currentTarget as HTMLElement).closest('details');
    if (details) details.open = false;
    startTour(id, 1, details?.querySelector('summary') ?? null);
  }
</script>

<div class="hidden flex-col pt-1 lg:flex" data-tour-entry bind:this={entry}>
  <span class="px-3 py-1 text-11 tracking-[0.14em] text-text-3 uppercase">Tours</span>
  {#each TOURS as tour (tour.id)}
    <button
      type="button"
      data-tour-start={tour.id}
      onclick={(event) => open(tour.id, event)}
      class="flex min-h-8 w-full items-center px-3 py-1.5 text-left font-sans text-text-2 transition-colors hover:bg-ink-selected hover:text-text-1"
      >{tour.title}</button
    >
  {:else}
    <span class="px-3 py-1.5 font-sans text-text-3">No guided tours are in this build.</span>
  {/each}
</div>
<p class="px-3 pt-2 font-sans text-text-3 lg:hidden" data-tour-note>{DESKTOP_ONLY}</p>
<span class="my-1 h-px bg-hair" aria-hidden="true"></span>
