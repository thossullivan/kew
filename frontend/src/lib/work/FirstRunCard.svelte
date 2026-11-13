<script lang="ts">
  // #2515 — the first-run card: the readiness checks that need attention, each
  // with its own fix command, then the next command. The one first-run surface
  // in the web UI (the legacy console's welcome card went with that console).
  //
  // A renderer. `firstRunCard` ($lib/work/first-run) decides every word and
  // which ONE command is the next move; this file names no command. The magenta
  // is spent once — on that command's Copy button, which `c` also presses —
  // because DESIGN.md rations it to "a person is the next move". Every other
  // command gets the quiet button.
  //
  // Each command is a real `<code>` block, as in `NeedsYouBlock.svelte`: the
  // button is a convenience over text that is already there, so a clipboard
  // the page cannot reach leaves the command selectable rather than lost. It is
  // the shared `Command` (#3077), so it wraps only at its own spaces and never
  // scrolls sideways inside its block.
  import type { FirstRunCardModel } from '$lib/work/first-run';
  import { Button, Command } from '$lib/ui';

  let {
    card,
    copied = '',
    oncopy = undefined
  }: {
    card: FirstRunCardModel;
    /** The command a copy just landed for, so its button can say so. */
    copied?: string;
    oncopy?: (command: string) => void;
  } = $props();

  let deferredOpen = $state(false);
  const deferredLine = $derived(
    card.deferred.length === 1
      ? '1 more check reads built-in defaults; it re-runs after'
      : `${card.deferred.length} more checks read built-in defaults; they re-run after`
  );
</script>

{#snippet command(text: string)}
  <div
    class="flex min-w-0 items-center gap-2.5 rounded-[7px] border border-hair-box bg-ink px-3 py-2"
  >
    <span aria-hidden="true" class="shrink-0 font-mono text-13 text-text-5">$</span>
    <Command command={text} class="min-w-0 flex-1 font-mono text-13 text-text-1" />
    <Button
      variant={text === card.primary ? 'needs-you' : 'factory'}
      size="sm"
      aria-label={text === card.primary ? 'Copy command (c)' : `Copy ${text}`}
      onclick={() => oncopy?.(text)}
      class="shrink-0">{copied === text ? 'Copied' : 'Copy'}</Button
    >
  </div>
{/snippet}

<section aria-label="First run" class="flex min-w-0 flex-col gap-5">
  <div class="flex flex-col gap-1.5">
    <h2 class="text-26 text-text-1">Nothing has run yet</h2>
    <p class="m-0 max-w-[640px] text-15 text-text-3">
      {#if card.checks.length > 0}
        Fix what the readiness checks found, then run the next command.
      {:else if card.reading === 'pending'}
        Reading the readiness checks…
      {:else if card.reading === 'failed'}
        The readiness checks could not be read; <code class="font-mono text-13">kew doctor</code> runs
        them here.
      {:else}
        Every readiness check passes. Run the next command.
      {/if}
    </p>
  </div>

  {#snippet checkList(checks: FirstRunCardModel['checks'], label: string)}
    <ul aria-label={label} class="m-0 flex list-none flex-col border-t border-hair p-0">
      {#each checks as check (check.id)}
        <li class="flex min-w-0 flex-col gap-2 border-b border-hair py-3.5">
          <div class="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1">
            <!-- `data-status-word`: the hook the 390 sweep reads (#2951). -->
            <span
              data-status-word
              class="text-11 whitespace-nowrap uppercase"
              class:text-danger={check.status === 'fail'}
              class:text-warn={check.status === 'warn'}>{check.status}</span
            >
            <span class="font-mono text-13 text-text-2">{check.id}</span>
            <span class="min-w-0 text-15 text-text-1">{check.summary}</span>
          </div>
          {#if check.command !== ''}
            {@render command(check.command)}
          {:else if check.hint !== ''}
            <p class="m-0 text-13 text-text-3">{check.hint}</p>
          {/if}
        </li>
      {/each}
    </ul>
  {/snippet}

  {#if card.checks.length > 0}
    {@render checkList(card.checks, 'Readiness checks to fix')}
  {/if}

  <dl class="m-0 grid grid-cols-[72px_minmax(0,1fr)] items-baseline gap-x-0 gap-y-2.5">
    <dt class="text-11 text-text-3">Next</dt>
    <dd class="m-0 min-w-0">{@render command(card.next)}</dd>
    <dt class="text-11 text-text-3">Then</dt>
    <!-- #3467 D28 — a command for a real issue the dashboard serves, with the
         quiet Copy, or a sentence: never `kew dispatch <issue>`, a template
         a terminal refuses. -->
    {#if card.then !== ''}
      <dd class="m-0 min-w-0">{@render command(card.then)}</dd>
    {:else}
      <dd data-first-run-then class="m-0 min-w-0 text-13 text-text-3">
        dispatch a first issue by its number.
      </dd>
    {/if}
  </dl>

  <!-- #2776 — with no config in force, the other checks read built-in
       defaults, so their fixes wait behind one quiet line AFTER the next
       command: a person reading top-down meets `kew init` first. Closed by
       default; their commands keep the quiet Copy. A button, not a
       `<details>`: that element is the app's menu primitive (menus.test.ts),
       and this is not a menu. -->
  {#if card.deferred.length > 0}
    <div class="flex min-w-0 flex-col gap-3">
      <button
        type="button"
        aria-expanded={deferredOpen}
        aria-controls="first-run-deferred"
        onclick={() => (deferredOpen = !deferredOpen)}
        class="cursor-pointer self-start border-0 bg-transparent p-0 text-left text-13 text-text-3 hover:text-text-1"
      >
        {deferredLine} <span class="font-mono">kew init</span>
        <span aria-hidden="true" class="inline-block transition-transform" class:rotate-90={deferredOpen}
          >▸</span
        >
      </button>
      <div id="first-run-deferred" hidden={!deferredOpen}>
        {@render checkList(card.deferred, 'Checks that re-run after kew init')}
      </div>
    </div>
  {/if}
</section>
