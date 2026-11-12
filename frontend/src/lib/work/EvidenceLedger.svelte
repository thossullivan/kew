<script lang="ts">
  // FUI-21 — the evidence ledger (DWork x 853–1392, y ≈624–928): FUI-12's
  // ordered guarantees as 50px rows with a numbered index, a vertical rail, the
  // guarantee and the audit event behind it, the recorded time and the word.
  //
  // A renderer. `evidenceLedger` decides every reading — the count, the rail
  // segment above and below each row, and the one thing that earns a check —
  // and the `chain_verified` guard lives there so no markup can draw a green
  // check on a chain kew has not vouched for.
  //
  // Two deliberate readings of the artboard:
  //
  // * the rail dashes CYAN into a pending row, which is the one place this
  //   screen paints the working colour outside a live mark: the solid record
  //   stops exactly where the chain does, and what is still ahead is drawn as
  //   ahead (the spec for this node says so in as many words);
  // * the state's WORD and mark are `statusShape`'s (FUI-04), so a pending row's
  //   word is the quiet one the rest of the app gives a step nobody has reached
  //   — D draws that word cyan, and the single colour authority wins.
  import type { ItemEvidence } from '$lib/api';
  import { StatusMark } from '$lib/ui';
  import { evidenceLedger } from './evidence';
  import PanelDisclosure from './PanelDisclosure.svelte';

  let {
    evidence,
    notice = ''
  }: {
    /** The payload, or `null` when the read has not landed or failed. */
    evidence: ItemEvidence | null;
    /** A read that FAILED, worded by the page. '' otherwise. */
    notice?: string;
  } = $props();

  const model = $derived(evidenceLedger(evidence, notice));
</script>

<section
  aria-label="Evidence ledger"
  class="flex min-w-0 flex-col overflow-y-auto border-t border-hair pt-[22px] lg:border-t-0 lg:border-l lg:pt-0 lg:pl-9"
>
  <!-- Closed on load (#2786 D16); the summary is the attested count. -->
  <PanelDisclosure label="Evidence ledger">
    {#snippet head()}
      {#if model.empty}
        <span class="font-mono text-13 text-text-3">{model.count}</span>
      {:else}
        <!-- The numerator alone is green: it counts what kew VOUCHED for, not what
             it recorded — the same reading the DEpic evidence column prints. -->
        <span class="font-mono text-13 text-text-3"
          ><span class="text-success">{model.attested}</span> / {model.total} attested</span
        >
      {/if}
    {/snippet}

    <!-- The reason is empty while the read is still out, and then there is
         nothing here at all: no rows, and no claim about why there are none. -->
    {#if model.empty}
      {#if model.reason !== ''}
        <p class="text-13 text-text-3">{model.reason}</p>
      {/if}
    {:else}
      <ol class="m-0 list-none p-0">
        {#each model.rows as row (row.key)}
          <li
            data-event={row.event}
            class="grid h-[50px] grid-cols-[30px_24px_minmax(0,1fr)_auto] items-center gap-x-2.5"
          >
            <span class="font-mono text-13 text-text-3">{row.n}</span>
            <!-- The rail: two hairline halves around the mark, each drawn only
                 where there is a neighbour to connect to. `aria-hidden` — it
                 carries no fact the row's own word does not already say. -->
            <span aria-hidden="true" class="relative h-[50px]">
              {#if row.above}
                <span
                  class="absolute top-0 left-[11px] h-5 w-0 border-l-[1.5px]"
                  class:border-dashed={row.above === 'dashed'}
                  class:border-accent-2={row.above === 'dashed'}
                  class:border-hair-strong={row.above === 'solid'}
                ></span>
              {/if}
              <span class="absolute top-1/2 left-0 flex w-6 -translate-y-1/2 justify-center">
                <StatusMark kind={row.state} size={12} label={row.word} />
              </span>
              {#if row.below}
                <span
                  class="absolute top-[30px] bottom-0 left-[11px] w-0 border-l-[1.5px]"
                  class:border-dashed={row.below === 'dashed'}
                  class:border-accent-2={row.below === 'dashed'}
                  class:border-hair-strong={row.below === 'solid'}
                ></span>
              {/if}
            </span>
            <span class="flex min-w-0 flex-col gap-0.5">
              <span class="text-15 text-text-1">{row.label}</span>
              <span data-summary class="truncate font-mono text-13 text-text-3" title={row.summary}
                >{row.summary}</span
              >
            </span>
            <span class="flex items-center gap-3.5">
              <span
                class="font-mono text-13"
                class:text-text-2={row.timed}
                class:text-text-3={!row.timed}
                title={row.timeTitle}>{row.time}</span
              >
              <span class="flex items-center gap-1.5 text-11" style:color={row.wordColor}>
                {#if row.check}
                  <!-- The check is the attested row's alone. -->
                  <svg
                    width="11"
                    height="11"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    stroke-width="2.6"
                    stroke-linecap="round"
                    stroke-linejoin="round"
                    aria-hidden="true"
                  >
                    <path d="M5 12.5 10 17 19 7" />
                  </svg>
                {/if}
                {row.word}
              </span>
            </span>
          </li>
        {/each}
      </ol>
    {/if}

    {#if model.notice !== ''}
      <!-- The server's own sentence about why the rows read as they do. -->
      <p class="text-13 text-text-3">{model.notice}</p>
    {/if}
  </PanelDisclosure>
</section>
