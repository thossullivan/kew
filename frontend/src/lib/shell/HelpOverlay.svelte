<script lang="ts">
  // #2513 — the `?` overlay: what this surface answers, and the keys it
  // answers to.
  //
  // It owns the reserved `?` press (`claimReserved('help')`), so no surface
  // can take it, and reads the page's shortcuts from the key layer at the
  // moment it opens: whatever the mounted surface registered is what it lists,
  // and nothing else. The sentences are `surface-help.ts`'s.
  //
  // A box over the page like the Config dialogs: `role="dialog"` plus
  // `use:contained`, which moves focus in and keeps Tab inside what
  // `aria-modal` promises. While it is open a window listener in the CAPTURE
  // phase takes every press but Tab before the key layer's own (bubble-phase)
  // listener can see it — so Esc closes the overlay rather than also clearing
  // the Work selection behind it, and a `j` does not walk a list nobody can
  // see. Closing puts the focus back on the element that had it.
  import { page } from '$app/state';
  import { claimReserved, registeredShortcuts, suspendShortcuts } from '$lib/keys/registry';
  import { contained } from '$lib/ui/contained';
  import { currentSurface, docsUrl, helpRows, type HelpRow } from './surface-help';

  let open = $state(false);
  let rows = $state<HelpRow[]>([]);
  let returnTo: HTMLElement | null = null;

  const surface = $derived(currentSurface(page.url.pathname));
  const docs = $derived(surface ? docsUrl(surface) : null);

  function show(): void {
    const active = document.activeElement;
    returnTo = active instanceof HTMLElement && active !== document.body ? active : null;
    rows = helpRows(registeredShortcuts());
    open = true;
  }

  function close(): void {
    if (!open) return;
    open = false;
    const el = returnTo;
    returnTo = null;
    // After the box has gone, so `contained` is not holding the focus inside it.
    queueMicrotask(() => {
      if (el?.isConnected) el.focus();
    });
  }

  $effect(() => claimReserved('help', () => (open ? close() : show())));

  // The key layer's own hold on the page (#2781), as every modal takes; the
  // capture listener below also keeps presses from listeners outside it.
  $effect(() => {
    if (open) return suspendShortcuts();
  });

  $effect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === 'Tab') return;
      event.stopPropagation();
      if (event.key === 'Escape' || event.key === '?') {
        event.preventDefault();
        close();
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  });

  // A navigation under it (the browser's Back) closes it, so it never goes on
  // describing the page it was opened on.
  $effect(() => {
    void page.url.pathname;
    return () => close();
  });
</script>

{#if open}
  <!-- Dismissal on a true backdrop click only (target check), as Config's. -->
  <div
    class="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 px-4 pt-[12dvh] pb-4"
    role="presentation"
    onclick={(e) => {
      if (e.target === e.currentTarget) close();
    }}
  >
    <div
      class="w-full max-w-[560px] min-w-0 border border-hair-box bg-ink-raised p-6 shadow-[var(--shadow)]"
      role="dialog"
      aria-modal="true"
      aria-labelledby="help-overlay-title"
      tabindex="-1"
      data-help-overlay
      use:contained
    >
      <div class="flex items-baseline gap-3">
        <h2 id="help-overlay-title" class="text-15 text-text-1">
          {surface ? surface.label : 'Help'}
        </h2>
        <div class="grow"></div>
        <span class="font-mono text-11 text-text-3">? or esc to close</span>
      </div>

      {#if surface}
        <p class="mt-3 text-13 text-text-2" data-help-answers>
          {surface.answers}
          {#if docs}
            <a
              class="whitespace-nowrap text-text-1 underline decoration-hair-strong underline-offset-2 hover:decoration-text-2"
              href={docs}
              target="_blank"
              rel="noopener noreferrer">Docs ↗</a
            >
          {/if}
        </p>
      {/if}

      <h3 class="mt-5 text-11 text-text-3">Shortcuts</h3>
      {#if rows.length > 0}
        <dl class="mt-2 grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-4 gap-y-1" data-help-shortcuts>
          {#each rows as row (row.label)}
            <dt class="flex flex-wrap gap-1 font-mono text-13 text-text-1">
              {#each row.keys as key (key)}
                <kbd class="border border-hair-strong px-1" data-help-key>{key}</kbd>
              {/each}
            </dt>
            <dd class="min-w-0 text-13 text-text-2">{row.label}</dd>
          {/each}
        </dl>
      {:else}
        <p class="mt-2 text-13 text-text-3">This page has no shortcuts of its own.</p>
      {/if}
    </div>
  </div>
{/if}
