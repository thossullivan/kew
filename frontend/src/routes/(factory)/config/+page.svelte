<script lang="ts">
  /**
   * FUI-28 — `/config` on the factory's hairline primitives.
   *
   * A restyle only: the same overridable fields, the same human-review policy,
   * the same approver-gated writes and the same compare-and-swap on the review
   * generation. What changed is the grammar — the per-field Cards became one
   * hairline list under an 11px section head, the source pill became the word
   * `SOURCE_TONE_CLASS` colours, and each dialog is a hairline box on the raised
   * ink rather than a card floating over the page.
   *
   * `legacy-restyle-b.test.ts` holds the frame to the artboard; every word and
   * every write in here is still `config-page.test.ts`'s.
   */
  import { onMount } from 'svelte';
  import { fmtDateTime, NO_TIME } from '$lib/time';
  import { Button, Input, SectionHead } from '$lib/ui';
  import { contained, topBox } from '$lib/ui/contained';
  import { suspendShortcuts } from '$lib/keys/registry';
  import ModelPicker from '$lib/ModelPicker.svelte';
  import { pickModel, routeDraft, runnerModels } from '$lib/model-picker';
  import type { CatalogModel, OverridableMap } from '$lib/api';
  import {
    REVIEW_SECTION,
    buildReviewRows,
    fetchReviewPolicy,
    setReviewOverride,
    revertReviewOverride,
    reviewSourceBadge,
    generationNote,
    capacityNote,
    reversionNote,
    approverNote,
    type ReviewPolicy,
    type ReviewRow,
    buildSections,
    sourceBadge,
    SOURCE_TONE_CLASS,
    formatValue,
    isMoneyField,
    isUnset,
    parseNumeric,
    isRouteValue,
    setOverride,
    revertOverride,
    applyOverride,
    applyRevert,
    type ConfigSection,
    type FieldRow
  } from '$lib/config-page';

  let {
    data
  }: { data: { overridable: OverridableMap | null; models: CatalogModel[] | null } } = $props();

  // The catalog the model controls pick from (#2542); null when it was not read.
  const models = $derived(data.models);

  // Deep-reactive so a row replaced on a write response re-renders in place.
  let sections = $state<ConfigSection[]>(data.overridable ? buildSections(data.overridable) : []);

  // Per-row inline error (revert failures, 403s) — never a page-top message.
  let rowError = $state<Record<string, string | null>>({});

  type Dialog = {
    row: FieldRow;
    text: string; // number / text single field
    runner: string; // route: runner half
    model: string; // route: model half
    reason: string;
    saving: boolean;
    error: string | null;
  };
  let dialog = $state<Dialog | null>(null);

  function replaceRow(field: string, updated: FieldRow) {
    for (const section of sections) {
      const i = section.rows.findIndex((r) => r.field === field);
      if (i !== -1) {
        section.rows[i] = updated;
        return;
      }
    }
  }

  function openDialog(row: FieldRow) {
    rowError[row.field] = null;
    const eff = row.resolution.effective;
    dialog = {
      row,
      text: row.kind === 'route' || eff == null ? '' : String(eff),
      runner: row.kind === 'route' && isRouteValue(eff) ? eff.runner : '',
      model: row.kind === 'route' && isRouteValue(eff) ? eff.model : '',
      reason: '',
      saving: false,
      error: null
    };
  }

  function closeDialog() {
    // Never dismiss mid-write: the pending request's error must stay visible
    // in the dialog that issued it.
    if (dialog?.saving) return;
    dialog = null;
  }

  // Turn the dialog's raw inputs into the JSON value the endpoint expects,
  // enforcing the routing {runner, model} shape (never a bare string).
  // A model is written only if the catalog lists it: the picker cannot emit
  // anything else, and this refuses a prefilled value it no longer lists. A
  // route's runner is the picked model's catalog runner (`routeDraft`).
  function draftValue(d: Dialog): unknown {
    if (d.row.kind === 'route') return routeDraft(models ?? [], d.runner, d.model);
    if (d.row.kind === 'model') {
      const offered = d.row.runner ? runnerModels(models ?? [], d.row.runner) : (models ?? []);
      const id = pickModel(offered, d.text);
      if (id === null) throw new Error('Pick a model from the catalog.');
      return id;
    }
    if (d.row.kind === 'number') return parseNumeric(d.text);
    const s = d.text.trim();
    if (s === '') throw new Error('Enter a value.');
    return s;
  }

  async function confirmOverride() {
    if (!dialog) return;
    const d = dialog;
    let value: unknown;
    try {
      value = draftValue(d);
    } catch (e) {
      d.error = e instanceof Error ? e.message : 'Invalid value';
      return;
    }
    d.saving = true;
    d.error = null;
    const res = await setOverride(d.row.field, value, d.reason);
    d.saving = false;
    if (res.ok) {
      // Transition from the write response — never a lagging read-back.
      replaceRow(
        d.row.field,
        applyOverride(d.row, res.data.effective, { value, reason: d.reason })
      );
      dialog = null;
    } else {
      d.error = res.message;
    }
  }

  const revertPending = $state<Record<string, boolean>>({});

  async function revert(row: FieldRow) {
    if (revertPending[row.field]) return;
    revertPending[row.field] = true;
    rowError[row.field] = null;
    try {
      const res = await revertOverride(row.field);
      if (res.ok) {
        replaceRow(row.field, applyRevert(row, res.data.effective));
      } else {
        rowError[row.field] = res.message;
      }
    } finally {
      revertPending[row.field] = false;
    }
  }

  // --- Human-review policy (I13W) -------------------------------------------
  // Loaded from GET /api/config/human_review and re-seated from every write
  // response. The whole state is the server's; this component computes no part
  // of it — not the expiry, not the over-cap bit, not which layer is in force.
  let policy = $state<ReviewPolicy | null>(null);
  let policyError = $state<string | null>(null);
  const reviewRows = $derived(policy ? buildReviewRows(policy) : []);

  onMount(async () => {
    const res = await fetchReviewPolicy();
    if (res.ok) policy = res.data;
    else policyError = res.message;
  });

  type ReviewDialog = {
    row: ReviewRow;
    text: string;
    expires: string;
    reason: string;
    saving: boolean;
    error: string | null;
  };
  let reviewDialog = $state<ReviewDialog | null>(null);

  // No page shortcut answers behind an open dialog (#2781).
  $effect(() => {
    if (dialog !== null || reviewDialog !== null) return suspendShortcuts();
  });
  const reviewPending = $state<Record<string, boolean>>({});
  let reviewRowError = $state<Record<string, string | null>>({});

  function openReviewDialog(row: ReviewRow) {
    reviewRowError[row.field] = null;
    reviewDialog = {
      row,
      text: row.value === null ? '' : String(row.value),
      expires: '',
      reason: '',
      saving: false,
      error: null
    };
  }

  function closeReviewDialog() {
    if (reviewDialog?.saving) return;
    reviewDialog = null;
  }

  async function confirmReviewOverride() {
    const d = reviewDialog;
    if (!d || !policy) return;
    let value: number;
    try {
      value = parseNumeric(d.text);
    } catch (e) {
      d.error = e instanceof Error ? e.message : 'Invalid value';
      return;
    }
    d.saving = true;
    d.error = null;
    // if_generation is the compare-and-swap: the generation this page was
    // SHOWING. A concurrent edit comes back 409 instead of being overwritten.
    const res = await setReviewOverride(d.row.field, value, d.reason, {
      expiresAt: d.expires,
      ifGeneration: policy.generation
    });
    d.saving = false;
    if (res.ok) {
      policy = res.data;
      reviewDialog = null;
    } else {
      d.error = res.message;
    }
  }

  async function revertReview(row: ReviewRow) {
    if (reviewPending[row.field]) return;
    reviewPending[row.field] = true;
    reviewRowError[row.field] = null;
    try {
      const res = await revertReviewOverride(row.field);
      if (res.ok) policy = res.data;
      else reviewRowError[row.field] = res.message;
    } finally {
      reviewPending[row.field] = false;
    }
  }

  /** The dialogs' one input: a 44px hairline field on the ink, as DWork's. */
  const FIELD_INPUT =
    'h-11 rounded-[4px] border-hair-strong bg-transparent text-13 text-text-1 placeholder:text-text-3';

  /**
   * A config row: one column below 640, the buttons on their own line under
   * the row's text, and the two-column row from `sm` up (#3468 D14). At 390 a
   * two-column row gave Override… and Revert about 200px and left the label,
   * value, hint and override note an 80px column, twelve lines tall.
   */
  const ROW =
    'grid grid-cols-1 items-start gap-3 border-b border-hair-quiet py-4 last:border-b-0 ' +
    'sm:grid-cols-[minmax(0,1fr)_auto] sm:gap-6';
  const ROW_BUTTONS = 'flex shrink-0 flex-wrap gap-2.5';

  // Three different facts, three different readings. No timestamp at all means
  // the value was just written and the reload has not come back yet. A stamp
  // fmtDateTime cannot read is printed raw — on a settings page the stored
  // text is the useful thing to show, louder than an em dash that hides it.
  function fmtDate(iso: string) {
    if (!iso) return 'just now';
    const when = fmtDateTime(iso);
    return when === NO_TIME ? iso : when;
  }
</script>

<div class="flex min-h-0 flex-1 flex-col gap-9 overflow-y-auto px-10 pt-8 pb-9">
  <header class="flex flex-col">
    <p class="text-11 text-text-3">Settings</p>
    <h1 class="mt-2 text-26 text-text-1">Config</h1>
    <p class="mt-3 max-w-[840px] text-15 text-text-2">
      Live overrides beat committed <span class="font-mono text-text-1">kew.toml</span> everywhere.
      Writes are approver-gated and audited.
    </p>
  </header>

  {#if !data.overridable}
    <p class="font-mono text-[11px] text-text-3">config not read — is kew serve running?</p>
  {:else}
    {#each sections as section (section.title)}
      <section aria-label={section.title} class="flex flex-col">
        <SectionHead
          label={section.title}
          meta={`${section.rows.length} ${section.rows.length === 1 ? 'field' : 'fields'}`}
        />
        <ul class="m-0 list-none p-0">
          {#each section.rows as row (row.field)}
            {@const badge = sourceBadge(row.source)}
            {@const ov = row.resolution.override}
            <li
              class={ROW}
            >
              <div class="min-w-0">
                <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <span class="text-15 text-text-1">{row.label}</span>
                  <!-- D42: a cap is money (amber mono), and an unset value
                       says what applies in words, never a bare dash. -->
                  <span
                    class={isUnset(row.kind, row.resolution.effective)
                      ? 'text-13 text-text-3'
                      : `font-mono text-13 ${isMoneyField(row.field) ? 'text-warn' : 'text-text-2'}`}
                    data-config-value>{formatValue(row.kind, row.resolution.effective, row.field)}</span
                  >
                  <!-- The layer in force, as a word: the pill was card grammar,
                       and `SOURCE_TONE_CLASS` keeps the four layers as distinct
                       as the badge tones made them. -->
                  <span class={`text-11 ${SOURCE_TONE_CLASS[badge.tone]}`}>{badge.label}</span>
                </div>
                <p class="mt-1.5 text-13 text-text-3">{row.hint}</p>
                {#if ov}
                  <!-- D42: the reason is upright and set off by a real space
                       on each side of its `·`. -->
                  <p class="mt-1.5 text-13 text-text-2" data-override-note>
                    overridden to <span class="font-mono text-text-1"
                      >{formatValue(row.kind, ov.value, row.field)}</span
                    >
                    by {ov.set_by}{' · '}{fmtDate(ov.set_at)}{#if ov.reason}{' · '}<span
                        >{ov.reason}</span
                      >{/if}
                  </p>
                {/if}
                {#if rowError[row.field]}
                  <p class="mt-2 text-13 text-danger">{rowError[row.field]}</p>
                {/if}
              </div>
              <div class={ROW_BUTTONS}>
                <Button variant="factory" onclick={() => openDialog(row)}>Override…</Button>
                {#if ov}
                  <Button
                    variant="factory"
                    disabled={revertPending[row.field]}
                    onclick={() => revert(row)}
                  >
                    {revertPending[row.field] ? 'Reverting…' : 'Revert'}
                  </Button>
                {/if}
              </div>
            </li>
          {/each}
        </ul>
      </section>
    {/each}
  {/if}

  <!-- Human-review policy (I13W). Every fact below is rendered from the
       service's contract: the generation, the expiry, the pending reversion
       and over_cap all arrive on the payload. Nothing here is recomputed. -->
  <section aria-label={REVIEW_SECTION} class="flex flex-col">
    <SectionHead
      label={REVIEW_SECTION}
      meta={policy ? `${policy.pending_reviews} awaiting a person` : ''}
    />

    {#if policyError}
      <p class="text-15 text-text-2">Could not load the human-review policy — {policyError}</p>
    {:else if !policy}
      <p class="font-mono text-[11px] text-text-3">loading the policy in force…</p>
    {:else}
      {@const p = policy}
      {@const badge = reviewSourceBadge(p)}
      {@const overCap = capacityNote(p)}
      {@const reversion = reversionNote(p)}
      <div class="flex flex-col gap-1.5 border-b border-hair-quiet pb-4">
        <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span class="text-15 text-text-1">Policy in force</span>
          <span class="font-mono text-13 text-text-2">{p.delivery_repo}</span>
          <span class={`text-11 ${SOURCE_TONE_CLASS[badge.tone]}`}>{badge.label}</span>
        </div>
        <p class="text-13 text-text-3">{generationNote(p)}</p>
        {#if overCap}
          <p class="text-13 text-warn">{overCap}</p>
        {/if}
        {#if reversion}
          <p class="text-13 text-text-2">{reversion}</p>
        {/if}
        <p class="text-13 text-text-3">{approverNote(p)}</p>
      </div>

      <ul class="m-0 list-none p-0">
        {#each reviewRows as row (row.field)}
          <li
            class={ROW}
          >
            <div class="min-w-0">
              <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span class="text-15 text-text-1">{row.label}</span>
                <span class="font-mono text-13 text-text-2">{row.display}</span>
              </div>
              <p class="mt-1.5 text-13 text-text-3">{row.hint}</p>
              {#if reviewRowError[row.field]}
                <p class="mt-2 text-13 text-danger">{reviewRowError[row.field]}</p>
              {/if}
            </div>
            <div class={ROW_BUTTONS}>
              <Button variant="factory" onclick={() => openReviewDialog(row)}>Override…</Button>
              {#if p.source === 'override'}
                <Button
                  variant="factory"
                  disabled={reviewPending[row.field]}
                  onclick={() => revertReview(row)}
                >
                  {reviewPending[row.field] ? 'Reverting…' : 'Revert'}
                </Button>
              {/if}
            </div>
          </li>
        {/each}
      </ul>
    {/if}
  </section>
</div>

<!-- Escape is handled HERE AND NOWHERE ELSE (C14). It was bound twice — at the
     window and again on each dialog's backdrop — so one press ran two
     dismissals, and a reader whose focus sat on the page body got only the
     window's. The window is the right single home: `use:contained` keeps focus
     in the open dialog, but a press on the dialog's own padding still leaves
     `document.activeElement` on `body`, where a backdrop-bound handler would
     never see the key. (`closeDialog` itself refuses to dismiss mid-save, and
     is a no-op when no dialog is open.) -->
<svelte:window
  onkeydown={(e) => {
    if (e.key !== 'Escape') return;
    // Only when one of this page's dialogs is the top box: a press left on
    // `body` under the palette is the palette's, not this one's (#2841).
    if (!topBox()?.hasAttribute('data-config-dialog')) return;
    if (dialog) closeDialog();
    if (reviewDialog) closeReviewDialog();
  }}
/>

<!-- The model control both dialog shapes share (#2542). -->
{#snippet picker(
  d: { row: FieldRow; saving: boolean },
  value: string,
  onpick: (id: string, model: CatalogModel) => void
)}
  {@const eff = d.row.resolution.effective}
  {#if models}
    <ModelPicker
      models={d.row.runner ? runnerModels(models, d.row.runner, value) : models}
      {value}
      current={isRouteValue(eff) ? eff.model : typeof eff === 'string' ? eff : ''}
      label={`${d.row.label} model`}
      disabled={d.saving}
      {onpick}
    />
  {:else}
    <p class="text-13 text-text-2">Model catalog not read — reload to pick a model.</p>
  {/if}
{/snippet}

{#if dialog}
  {@const d = dialog}
  <!-- The backdrop is a surface, not a control: it has no role and takes no
       focus, and `svelte-ignore` is correct rather than convenient here — its
       keyboard route is Escape at the window above, the ONE handler (C14), and
       a second one bound here is the bug that finding names. It was
       `role="button"` precisely to buy that handler, which also made every
       screen reader announce the page's dimming as a button. -->
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
  <div
    class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
    onclick={(e) => e.target === e.currentTarget && closeDialog()}
  >
    <!-- Dismissal fires only on true backdrop clicks (target check above),
         so the dialog needs no propagation handlers and keydown bubbles to
         the window-level Escape. `use:contained` moves focus into the dialog
         on mount — so keyboard users aren't left on the opener — and keeps Tab
         inside it, which is what `aria-modal` below has always claimed and did
         not do (C14). The box is a hairline on the raised ink — the one place
         the factory draws a border, because a modal has to sit off the page it
         covers. -->
    <div
      class="w-full max-w-md rounded-[11px] border border-hair-strong bg-ink-raised p-6 shadow-[var(--shadow)]"
      role="dialog"
      aria-modal="true"
      aria-label={`Override ${d.row.label}`}
      tabindex="-1"
      data-config-dialog
      use:contained
    >
      <p class="text-11 text-text-3">Override</p>
      <h2 class="mt-2 text-26 text-text-1">{d.row.label}</h2>

      <dl class="mt-4 mb-5">
        <dt class="text-11 text-text-3">Current</dt>
        <dd class="mt-1 font-mono text-13 text-text-2">
          {formatValue(d.row.kind, d.row.resolution.effective, d.row.field)}
        </dd>
      </dl>

      <div class="flex flex-col gap-3">
        {#if d.row.kind === 'route'}
          <!-- Routing edits the {runner, model} pair via distinct fields —
               never a bare string (the runner-name footgun). -->
          <!-- A picked catalog model fixes the runner; only a runner-only
               route (no model) types one, for runners the catalog omits. -->
          {@const pickedRunner = models?.find((m) => m.id === d.model)?.runner}
          {#if pickedRunner}
            <div class="flex flex-col gap-1.5">
              <span class="text-11 text-text-3">Runner</span>
              <span class="font-mono text-13 text-text-2" data-route-runner
                >{pickedRunner} <span class="font-sans text-11 text-text-3">from the model</span></span
              >
            </div>
          {:else}
            <label class="flex flex-col gap-1.5">
              <span class="text-11 text-text-3">Runner</span>
              <Input
                type="text"
                placeholder="claude-code"
                bind:value={d.runner}
                disabled={d.saving}
                class={FIELD_INPUT}
              />
            </label>
          {/if}
          <div class="flex flex-col gap-1.5">
            <div class="flex items-center justify-between gap-2">
              <span class="text-11 text-text-3">Model</span>
              {#if d.model !== ''}
                <!-- Back to a runner-only route, for a runner the catalog omits. -->
                <Button variant="factory" disabled={d.saving} onclick={() => (d.model = '')}
                  >Clear model</Button
                >
              {/if}
            </div>
            {@render picker(d, d.model, (id, model) => {
              d.model = id;
              d.runner = model.runner;
            })}
          </div>
        {:else if d.row.kind === 'model'}
          <div class="flex flex-col gap-1.5">
            <span class="text-11 text-text-3">New model</span>
            {@render picker(d, d.text, (id) => (d.text = id))}
          </div>
        {:else}
          <label class="flex flex-col gap-1.5">
            <span class="text-11 text-text-3">New value</span>
            <Input
              type={d.row.kind === 'number' ? 'number' : 'text'}
              bind:value={d.text}
              disabled={d.saving}
              class={FIELD_INPUT}
            />
          </label>
        {/if}

        <label class="flex flex-col gap-1.5">
          <span class="text-11 text-text-3">Reason</span>
          <Input
            type="text"
            placeholder="optional reason"
            bind:value={d.reason}
            disabled={d.saving}
            class={FIELD_INPUT}
          />
        </label>

        {#if d.error}
          <p class="text-13 text-danger">{d.error}</p>
        {/if}

        <div class="flex justify-end gap-2.5 pt-1">
          <Button variant="factory" disabled={d.saving} onclick={closeDialog}>Cancel</Button>
          <Button variant="factory" disabled={d.saving} onclick={confirmOverride}>
            {d.saving ? 'Saving…' : 'Confirm override'}
          </Button>
        </div>
      </div>
    </div>
  </div>
{/if}

{#if reviewDialog}
  {@const d = reviewDialog}
  <!-- The backdrop is a surface, not a control: it has no role and takes no
       focus, and `svelte-ignore` is correct rather than convenient here — its
       keyboard route is Escape at the window above, the ONE handler (C14), and
       a second one bound here is the bug that finding names. It was
       `role="button"` precisely to buy that handler, which also made every
       screen reader announce the page's dimming as a button. -->
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
  <div
    class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
    onclick={(e) => e.target === e.currentTarget && closeReviewDialog()}
  >
    <div
      class="w-full max-w-md rounded-[11px] border border-hair-strong bg-ink-raised p-6 shadow-[var(--shadow)]"
      role="dialog"
      aria-modal="true"
      aria-label={`Override ${d.row.label}`}
      tabindex="-1"
      data-config-dialog
      use:contained
    >
      <p class="text-11 text-text-3">Human-review policy</p>
      <h2 class="mt-2 text-26 text-text-1">{d.row.label}</h2>

      <dl class="mt-4 mb-5">
        <dt class="text-11 text-text-3">Current</dt>
        <dd class="mt-1 font-mono text-13 text-text-2">{d.row.display}</dd>
      </dl>

      <div class="flex flex-col gap-3">
        <label class="flex flex-col gap-1.5">
          <span class="text-11 text-text-3">New value</span>
          <Input type="number" bind:value={d.text} disabled={d.saving} class={FIELD_INPUT} />
        </label>

        <!-- Optional; the service refuses an expiry already past, and it is
             the service that performs the audited reversion when it fires. -->
        <label class="flex flex-col gap-1.5">
          <span class="text-11 text-text-3">Expires (UTC, optional)</span>
          <Input
            type="text"
            placeholder="2030-01-01T00:00:00+00:00"
            bind:value={d.expires}
            disabled={d.saving}
            class={FIELD_INPUT}
          />
        </label>

        <label class="flex flex-col gap-1.5">
          <span class="text-11 text-text-3">Reason</span>
          <Input
            type="text"
            placeholder="optional reason"
            bind:value={d.reason}
            disabled={d.saving}
            class={FIELD_INPUT}
          />
        </label>

        {#if d.error}
          <p class="text-13 text-danger">{d.error}</p>
        {/if}

        <div class="flex justify-end gap-2.5 pt-1">
          <Button variant="factory" disabled={d.saving} onclick={closeReviewDialog}>Cancel</Button>
          <Button variant="factory" disabled={d.saving} onclick={confirmReviewOverride}>
            {d.saving ? 'Saving…' : 'Confirm override'}
          </Button>
        </div>
      </div>
    </div>
  </div>
{/if}
