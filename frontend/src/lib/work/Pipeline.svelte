<script lang="ts">
  // FUI-20 — the Work screen's Pipeline section (DWork y ≈276–432): the head
  // and its legend, the hairline column grid with its 6px ticks, and one lane
  // per payload lane.
  //
  // A renderer, like `ItemHeader`. Every decision — which columns exist and in
  // which order, which caption line carries the status colour, whether a lane is
  // quiet, whether there is anything to draw at all — lives in `./pipeline`, and
  // every mark and line below the head is `Swimlane`'s (FUI-07) over
  // `statusShape` (FUI-04). There is no stage table in this file: nothing here
  // knows that kew serves four stages, so the day the payload serves a
  // fifth it appears without an edit.
  //
  // The read is the page's, not this component's, for the reason `ItemHeader`'s
  // write path is shaped the way it is: the page owns the polling clock, and a
  // component that fetched inside an effect could not be rendered server-side —
  // which is how `pipeline.test.ts` checks these pixels until FUI-02's
  // screenshot harness can shoot DWork.
  import type { ItemPipeline } from '$lib/api';
  import { SectionHead, StatusMark, Swimlane } from '$lib/ui';
  import { NO_RUN_YET, PIPELINE_LEGEND, PIPELINE_VOCABULARY, pipelineModel } from './pipeline';

  let {
    pipeline,
    notice = ''
  }: {
    /** The payload, or `null` — an id the server does not know has none. */
    pipeline: ItemPipeline | null;
    /** A read that FAILED, said out loud by `pipelineReadFailure`. */
    notice?: string;
  } = $props();

  const model = $derived(pipelineModel(pipeline));
  // Two different gaps, never both: `notice` comes with no payload to draw, and
  // the model's comes from the payload that was drawn.
  const said = $derived(notice || model.notice);
</script>

<!-- Absent, rather than an empty grid: a dashed placeholder pipeline would draw
     stages this item has not got and a lane nobody named. A named read failure
     is the one thing worth a section with nothing under it. -->
{#if !model.empty || said !== ''}
  <!-- 18px over the page's own 12px gap is the artboard's 30px. -->
  <section aria-label="Pipeline" class="mt-[18px] flex flex-col">
    <!-- The legend is a key to the marks below it, not a count, so it takes the
         head's quiet 11px meta slot and stays sentence case. -->
    <SectionHead label="Pipeline" class="mb-[18px]">
      <span class="flex flex-wrap items-center gap-5">
        {#each PIPELINE_LEGEND as entry (entry.key)}
          <span class="flex items-center gap-1.5">
            <StatusMark
              kind={entry.status}
              vocabulary={PIPELINE_VOCABULARY}
              size={7}
              label={entry.label}
            />
            {entry.label}
          </span>
        {/each}
      </span>
    </SectionHead>

    {#if !model.empty}
      <Swimlane
        stages={model.stages}
        lanes={model.lanes}
        size={14}
        vocabulary={PIPELINE_VOCABULARY}
        laneHeading="Lane"
        label="Pipeline stages"
      />
    {/if}

    {#if model.noRun}
      <!-- Not a warning: an idle item is the most ordinary row on the screen. -->
      <p class="mt-3 text-13 text-text-2">{NO_RUN_YET}</p>
    {/if}

    {#if said !== ''}
      <p role="status" class="mt-3 flex items-start gap-2 text-13 text-text-3">
        <StatusMark kind="unverified" size={7} label="unreadable" class="mt-1 shrink-0" />
        <span>{said}</span>
      </p>
    {/if}
  </section>
{/if}
