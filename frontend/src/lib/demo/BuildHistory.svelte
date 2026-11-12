<script lang="ts" module>
  import type { Provenance } from './build-history';
  import { PROVENANCE_URL } from './build-history';

  // One read for the whole visit: the file is static, the same on every page,
  // and a foot mounted after it arrived starts from it rather than loading.
  let read: Promise<Provenance> | null = null;
  let loaded: Provenance | null = null;
  function provenance(): Promise<Provenance> {
    read ??= fetch(PROVENANCE_URL).then(async (res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      loaded = (await res.json()) as Provenance;
      return loaded;
    });
    return read;
  }
</script>

<script lang="ts">
  // The demo build's page foot (#3144), in the factory shell's foot slot: how
  // the surface above it was built, from the exported build history. A row of
  // the shell's column, so it takes its height from `main` and never covers
  // the page; expanded, the ledger scrolls inside its own capped box. A
  // surface no feature names draws nothing at all — not while the file loads,
  // not when it fails: the routes the file names are bundled beside it, so
  // the foot knows before the read whether this surface has a history.
  import { page } from '$app/state';
  import { TAP_PILL } from '$lib/factory/classes';
  import { PROVENANCE_ROUTES, PROVENANCE_SOURCE } from './provenance-meta';
  import {
    BUILD_HISTORY_LABEL,
    LIST_PRICE_NOTE,
    buildSummary,
    featuresFor,
    heldOutLine,
    integrityLine,
    namesRoute,
    prLabel,
    tallied,
    usd
  } from './build-history';

  let doc: Provenance | null = $state(loaded);
  let failed = $state(false);
  /** Whether the ledger is open; it starts shut. */
  let open = $state(false);
  /** The ledger region the toggle controls; one foot per page, so one id. */
  const REGION = 'build-history-ledger';

  const named = $derived(namesRoute(PROVENANCE_ROUTES, page.route.id));

  $effect(() => {
    if (!named) return;
    provenance().then(
      (value) => (doc = value),
      () => (failed = true)
    );
  });

  const features = $derived(doc === null ? [] : featuresFor(doc, page.route.id));
  const TH = 'py-1 pr-4 text-left align-top font-normal text-11 text-text-3';
  const TD = 'py-1 align-top text-13 text-text-2';
</script>

{#if !named}
  <!-- No feature names this surface: no foot, in any state. -->
{:else if failed}
  <p data-build-history="error" class="shrink-0 border-t border-hair bg-ink px-4 py-2 text-13 text-text-3 sm:px-8">
    The build history did not load, so this page cannot say how it was built.
  </p>
{:else if doc === null}
  <p data-build-history="loading" class="shrink-0 border-t border-hair bg-ink px-4 py-2 text-13 text-text-3 sm:px-8">
    Reading how this page was built.
  </p>
{:else if features.length > 0}
  <!-- A disclosure, not a menu: the epic page's ticker toggle (▸/▾, a button
       that says what opens, `aria-expanded` and `aria-controls` on the region
       below). It starts shut, and only its own button shuts it again. -->
  <!-- `data-tour` holds the line AND the ledger it opens: a tour step on Build
       history keeps the opened ledger inside its highlight, not under the
       overlay, and the tour re-places the step as the foot grows (#3148). No
       tour points here while the build ships the samples (design review c9,
       V3); the step returns with the real export. -->
  <div data-build-history="ready" data-tour="build-history" class="shrink-0 border-t border-hair bg-ink">
    <!-- One line on a phone (design review c9, D17): the foot is sticky, and
         wrapped to three lines it took 100px of an 844px screen. The summary
         is cut with an ellipsis there, whole in its title, and opening the
         ledger reads every figure; from 640 up the line wraps as it did. -->
    <div class="flex items-center gap-x-3 px-4 py-1 text-13 whitespace-nowrap text-text-2 sm:flex-wrap sm:px-8 sm:whitespace-normal">
      <button
        type="button"
        data-build-toggle
        class={`flex items-center gap-1.5 font-mono text-13 text-text-3 hover:text-text-1 ${TAP_PILL}`}
        aria-expanded={open}
        aria-controls={REGION}
        onclick={() => (open = !open)}
      >
        <span aria-hidden="true" class="inline-block w-2">{open ? '▾' : '▸'}</span>
        {BUILD_HISTORY_LABEL}
      </button>
      <span
        data-build-summary
        title={buildSummary(features)}
        class="min-w-0 truncate font-mono sm:overflow-visible sm:whitespace-normal">{buildSummary(features)}</span
      >
      {#if PROVENANCE_SOURCE === 'sample'}
        <!-- A build of the committed samples says so on the line (#3146): the
             figures are invented, so nothing may read them as kew's own build
             history. -->
        <span data-build-sample class="text-13 text-text-3">sample data</span>
      {/if}
    </div>
    <div id={REGION} hidden={!open} class="max-h-[40dvh] overflow-y-auto px-4 pb-3 sm:px-8">
      {#each features as feature (feature.id)}
        <table data-build-feature={feature.id} class="mt-2 w-full max-w-3xl border-collapse">
          <caption class="py-1 text-left text-13 text-text-1">{feature.title}</caption>
          <tbody>
            <tr>
              <th scope="row" class={TH}>Epics</th>
              <td class={`${TD} font-mono break-all`}>
                {feature.epics.length === 0 ? 'None named.' : feature.epics.join(', ')}
              </td>
            </tr>
            <tr>
              <th scope="row" class={TH}>PRs</th>
              <td class={TD}>
                {#if feature.prs.length === 0}
                  No pull request.
                {:else}
                  <span class="flex flex-wrap gap-x-3">
                    {#each feature.prs as pr (pr.url)}
                      <!-- The number is text, never a link: the real PRs are in
                           kew's private development repository, and an invented
                           owner's repository in the samples is someone else's
                           URL space. -->
                      <span data-build-pr class="font-mono text-text-2">{prLabel(pr.url)}</span>
                    {/each}
                  </span>
                {/if}
              </td>
            </tr>
            <tr>
              <th scope="row" class={TH}>Runs</th>
              <td class={`${TD} font-mono`}>
                {tallied(feature.runs)
                  .map(({ word, count }) => `${word} ${count}`)
                  .join(', ') || 'None.'}
              </td>
            </tr>
            <tr>
              <th scope="row" class={TH}>List cost</th>
              <td class={`${TD} font-mono`}>{usd(feature.list_cost_usd)}</td>
            </tr>
            <tr>
              <th scope="row" class={TH}>Review rounds</th>
              <td class={`${TD} font-mono`}>
                {tallied(feature.review_rounds)
                  .map(({ word, count }) => `${word} ${count}`)
                  .join(', ') || 'None.'}
              </td>
            </tr>
            <tr>
              <th scope="row" class={TH}>Held-out</th>
              <td class={TD}>{heldOutLine(feature.held_out)}</td>
            </tr>
            <tr>
              <th scope="row" class={TH}>Integrity</th>
              <td class={TD}>{integrityLine(feature.integrity, doc.integrity_source)}</td>
            </tr>
          </tbody>
        </table>
      {/each}
      <p data-build-note class="mt-2 max-w-3xl text-13 text-text-3">{LIST_PRICE_NOTE}</p>
    </div>
  </div>
{/if}
