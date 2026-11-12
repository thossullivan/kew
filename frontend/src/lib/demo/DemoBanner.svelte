<script lang="ts">
  // The demo build's banner (#3139), in the factory shell's banner slot. It is
  // a row of the shell's column, not an overlay: the header and the page sit
  // below it and lose its height, so it covers nothing at any width, and its
  // sentence wraps rather than scrolling sideways at 390px.
  //
  // It says what kew is and offers the tours (design review c9, D17): on a
  // phone a visitor never opens the header's More menu, so the banner is the
  // one way in. "Take a tour" opens the tour list in the banner's own row,
  // below the sentence; from 1024px up each tour starts from it, and below,
  // where tours do not run, the list says where they do.
  import { DEMO_BANNER, TAKE_A_TOUR, TOURS_DESKTOP_ONLY } from './banner.js';
  import { startTour, TOURS } from './tours/controller';

  /** The list the toggle controls; one banner per page, so one id. */
  const LIST = 'demo-tour-list';
  let open = $state(false);
  let toggle: HTMLButtonElement | undefined = $state();

  /** Start a tour from the list: shut the list, and give focus back to the toggle when the tour ends. */
  function start(id: string): void {
    open = false;
    startTour(id, 1, toggle ?? null);
  }
</script>

<div role="note" data-demo-banner class="shrink-0 border-b border-hair bg-ink-raised px-4 py-1 text-13 text-text-1 sm:px-8">
  <p class="flex flex-wrap items-center gap-x-3">
    <span data-demo-banner-text class="py-1">{DEMO_BANNER}</span>
    <button
      type="button"
      bind:this={toggle}
      data-demo-tours-toggle
      aria-expanded={open}
      aria-controls={LIST}
      onclick={() => (open = !open)}
      class="inline-flex min-h-8 items-center gap-1.5 font-sans text-13 text-text-1 underline underline-offset-2 hover:text-text-1"
    >
      {TAKE_A_TOUR}<span aria-hidden="true" class="inline-block w-2 no-underline">{open ? '▾' : '▸'}</span>
    </button>
  </p>
  <div id={LIST} hidden={!open} data-demo-tour-list class="pb-2">
    <ul class="flex flex-col">
      {#each TOURS as tour (tour.id)}
        <li>
          <button
            type="button"
            data-banner-tour-start={tour.id}
            onclick={() => start(tour.id)}
            class="hidden min-h-8 items-center text-left font-sans text-13 text-text-2 hover:text-text-1 lg:flex"
            >{tour.title}</button
          >
          <span class="flex min-h-8 items-center font-sans text-13 text-text-2 lg:hidden">{tour.title}</span>
        </li>
      {/each}
    </ul>
    <p data-demo-tour-note class="font-sans text-13 text-text-3 lg:hidden">{TOURS_DESKTOP_ONLY}</p>
  </div>
</div>
