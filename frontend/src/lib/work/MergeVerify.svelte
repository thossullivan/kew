<script lang="ts">
  // #3140 — the merge verify reading under the Pipeline section: why the merge
  // verify refused this PR, or the head it passed at. A renderer over
  // `./merge-verify`, which owns the wording and the colour rule.
  import type { ItemPipeline } from '$lib/api';
  import { errorCategoryLabel } from '$lib/factory/names';
  import { SectionHead, StatusMark } from '$lib/ui';
  import { idBreaks, mergeVerifyReading } from './merge-verify';
  import { PIPELINE_VOCABULARY } from './pipeline';

  let { pipeline }: { pipeline: ItemPipeline | null } = $props();

  const reading = $derived(mergeVerifyReading(pipeline));

  // #3460 — a merge over a failed verify links here (`#merge-verify`). The
  // pipeline arrives after the page does, so the browser's own jump to the
  // fragment found nothing; scroll once the section exists.
  const ANCHOR = 'merge-verify';
  let section: HTMLElement | undefined = $state();
  let scrolled = false;
  $effect(() => {
    if (!section || scrolled || window.location.hash !== `#${ANCHOR}`) return;
    scrolled = true;
    section.scrollIntoView({ block: 'start' });
  });
  // The head is an id, so it is set in Plex Mono inside the sentence.
  const parts = $derived.by(() => {
    if (!reading || reading.head === '' || !reading.sentence.includes(reading.head)) {
      return null;
    }
    const at = reading.sentence.indexOf(reading.head);
    return {
      before: reading.sentence.slice(0, at),
      after: reading.sentence.slice(at + reading.head.length)
    };
  });
</script>

{#if reading}
  <section
    bind:this={section}
    id={ANCHOR}
    aria-label="Merge verify"
    class="mt-[18px] flex scroll-mt-4 flex-col"
  >
    <SectionHead label="Merge verify" class="mb-3" />
    <!-- `data-tour`: the demo's refused-PR tour points at the reading and at
         the held-out ids beneath it (#3148). Inert in the console. -->
    <p class="flex items-start gap-2 text-13 text-text-1" data-merge-verify={reading.status} data-tour="merge-verify">
      <StatusMark
        kind={reading.status}
        vocabulary={PIPELINE_VOCABULARY}
        size={7}
        label={reading.status.replace('_', ' ')}
        class="mt-1 shrink-0"
      />
      <span class="min-w-0">
        {#if parts && reading}
          {parts.before}<span class="font-mono" data-merge-verify-head>{reading.head}</span
          >{parts.after}
        {:else}
          {reading.sentence}
        {/if}
      </span>
    </p>
    {#if reading.tests.length > 0}
      <ul
        aria-label="Main's tests that fail on this PR"
        class="mt-2 ml-[15px] flex flex-col gap-1"
        data-tour="held-out-tests"
      >
        {#each reading.tests as test (test.id)}
          <li class="min-w-0 text-13">
            <!-- A node id breaks at its own seams (after `::` and `/`);
                 `overflow-wrap: anywhere` is the fallback for a seamless run
                 too long for a 390px line. -->
            <span class="font-mono [overflow-wrap:anywhere] text-text-2" data-held-out-test
              >{#each idBreaks(test.id) as piece, i (i)}{#if i > 0}<wbr />{/if}{piece}{/each}</span
            >{#if test.excused}<span class="text-13 text-text-3" data-held-out-excused
                >{' — excused by a declared behavior change'}</span
              >{/if}
          </li>
        {/each}
        {#if reading.more > 0}
          <!-- The server sends the first 50; the rest are counted, not lost. -->
          <li class="text-13 text-text-3" data-held-out-more>and {reading.more} more</li>
        {/if}
      </ul>
    {/if}
    {#if reading.category !== '' && reading.status !== 'done'}
      <!-- The label is label-11; the value is the category in words (#3466),
           a sentence, so it sits outside the label-11 run at 13 px, never
           uppercased or tracked, and the code kew recorded stays in its title
           for the operator who greps the ledger for it. -->
      <p class="mt-2 ml-[15px] flex flex-wrap items-baseline gap-x-2">
        <span class="text-11 text-text-3">error category</span>
        <span
          class="text-13 text-text-2 normal-case tracking-normal"
          title={reading.category}
          data-merge-verify-category>{errorCategoryLabel(reading.category)}</span
        >
      </p>
    {/if}
  </section>
{/if}
