<script lang="ts">
  /**
   * FUI-27 — the audit log, restyled onto the hairline primitives.
   *
   * One table in DMonitor's grammar, packed to the 50px row the artboard uses
   * for a dense list, with the filter chips above it as hairline boxes rather
   * than pills. No reading changed: `/api/audit` is paged exactly as before,
   * and the chain-verification status still comes from `/api/audit/status` on
   * the page load.
   *
   * The verdict over the table is a StatusMark, not a colour this page picked:
   * a verified chain is `attested` (the evidence vocabulary's green dot) and a
   * failed verification is the failure X. Its own word is supplied, because
   * "attested" is what the chain is, not what the reader needs to be told.
   *
   * `?event=<id>` (#3025) is a deep link to one event: it is drawn above the
   * table under a cyan rule (selected) and takes focus, read on its own so an
   * event older than the first page still shows, and its row is marked
   * selected when the loaded page carries it. A failed read of that one event
   * says so in its own sentence; the table and the chain status still render.
   *
   * C7 (#3470) — a row reads the way the Monitor reads the same event
   * (`$lib/audit-row`, over `eventLine`): a worded lead and the subject and fact
   * in words. `summary` is the compact JSON `_summary` writes, so it is only
   * ever printed behind the row's `payload ▸` disclosure.
   */
  import { fmtDateTime, utcTitle } from '$lib/time';
  import { StatusMark, Why } from '$lib/ui';
  import { auditRow } from '$lib/audit-row';
  import { dataRowClasses, TAP_INLINE } from '$lib/factory/classes';
  import { leadOf } from '$lib/monitor/event-line';
  import {
    fetchAudit,
    type AuditEvent,
    type AuditList,
    type AuditStatus
  } from '$lib/api';
  import type { AuditFocus } from './+page';

  let {
    data
  }: { data: { audit: AuditList | null; status: AuditStatus | null; focus: AuditFocus | null } } = $props();

  const focus = $derived(data.focus);
  let focusBox = $state<HTMLElement | null>(null);
  // The linked event takes focus once it is drawn, so a keyboard or screen
  // reader lands on it, and it is scrolled into view.
  $effect(() => {
    if (!focusBox) return;
    focusBox.focus({ preventScroll: true });
    focusBox.scrollIntoView({ block: 'nearest' });
  });

  const liveMode = $derived(data.audit != null);
  const status = $derived(data.status);

  // Chip options are derived from the (unfiltered) page `load()` returned — a
  // stable, data-driven set rather than a hardcoded taxonomy. Derived rather
  // than read once at initialiser time, so a fresh load() re-offers them.
  const initial = $derived(data.audit?.events ?? []);
  // Each chip reads the words its rows lead with, the code in its title (#3470).
  const eventOptions = $derived(
    [...new Set(initial.map((e) => e.event))].sort((a, b) => leadOf(a).localeCompare(leadOf(b)))
  );
  const actorOptions = $derived(
    [...new Set(initial.map((e) => e.actor_type).filter((a): a is string => !!a))].sort()
  );

  let events = $state<AuditEvent[]>([]);
  let nextAfter = $state<number | null>(null);
  let eventFilter = $state<string | null>(null);
  let actorFilter = $state<string | null>(null);
  let loading = $state(false);
  /** Which rows' `payload ▸` disclosures are open, by event id. */
  let payloadOpen = $state<Record<number, boolean>>({});

  // Seed, and re-seed on a fresh load(), from the page data — the same pattern
  // the epics list uses, so a reload is not left showing the previous page's
  // events.
  $effect(() => {
    events = data.audit?.events ?? [];
    nextAfter = data.audit?.next_after ?? null;
  });

  async function reload(): Promise<void> {
    if (!liveMode) return;
    loading = true;
    try {
      const res = await fetchAudit({
        event: eventFilter ?? undefined,
        actor_type: actorFilter ?? undefined,
        limit: 50
      });
      events = res.events;
      nextAfter = res.next_after;
    } catch {
      // keep the current page on transient failure
    } finally {
      loading = false;
    }
  }

  async function loadMore(): Promise<void> {
    if (!liveMode || nextAfter == null) return;
    loading = true;
    try {
      const res = await fetchAudit({
        event: eventFilter ?? undefined,
        actor_type: actorFilter ?? undefined,
        after: nextAfter,
        limit: 50
      });
      events = [...events, ...res.events];
      nextAfter = res.next_after;
    } catch {
      // ignore — user can retry
    } finally {
      loading = false;
    }
  }

  function toggleEvent(e: string): void {
    eventFilter = eventFilter === e ? null : e;
    void reload();
  }

  function toggleActor(a: string): void {
    actorFilter = actorFilter === a ? null : a;
    void reload();
  }

  /** A filter chip: a hairline box, marked in --text-1 when it is switched on. */
  function chip(active: boolean): string {
    return active
      ? 'rounded-[4px] border border-text-5 px-2.5 py-1 font-mono text-[11px] text-text-1'
      : 'rounded-[4px] border border-hair px-2.5 py-1 font-mono text-[11px] text-text-3 ' +
        'transition-colors hover:border-text-5 hover:text-text-1';
  }

  // Five columns from `lg`. Narrower, the fixed columns left the summary no
  // width at all, so a row becomes two lines: the lead and its time, then the
  // words and their disclosure across the full width (#3470).
  const COLUMNS =
    'grid-cols-[minmax(0,1fr)_auto] gap-y-1 lg:grid-cols-[220px_minmax(0,1fr)_72px_132px_168px]';
</script>

<div class="flex min-h-0 flex-1 flex-col">
  <header
    class="flex shrink-0 flex-wrap items-baseline gap-4 border-b border-hair px-4 pt-5 pb-[18px] sm:px-10"
  >
    <h1 class="text-26 text-text-1">Audit</h1>
    <span class="text-11 text-text-3">hash-chained governance log</span>
    <div class="grow"></div>
    {#if status}
      <span class="flex items-baseline gap-3 font-mono text-13">
        <StatusMark
          kind={status.verified ? 'attested' : 'failure'}
          size={10}
          word
          label={status.verified ? 'chain verified' : 'verification failed'}
          class="shrink-0 self-center"
        />
        <span class="text-text-3">
          {status.events} events · {status.anchors_matched}/{status.anchors_total} anchors
        </span>
      </span>
    {:else}
      <span class="font-mono text-13 text-text-3">offline · no data</span>
    {/if}
  </header>

  {#if !liveMode}
    <p class="px-10 py-16 text-center text-13 text-text-3">
      start kew serve to see the audit log.
    </p>
  {:else}
    <div class="min-h-0 flex-1 overflow-y-auto px-4 pt-7 pb-9 sm:px-10">
      {#if focus}
        <section
          bind:this={focusBox}
          tabindex="-1"
          aria-label="Linked audit event"
          class="mb-6 min-w-0 border-l-2 border-accent-2 py-1 pl-4 outline-none"
          data-audit-focus={focus.id}
          data-focus-state={focus.state}
        >
          {#if focus.state === 'found'}
            <p class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <span class="font-mono text-13 text-text-1">event #{focus.event.id}</span>
              <span class="font-mono text-13 text-text-1">{focus.event.event}</span>
              <span class="font-mono text-13 text-text-3"
                >{[focus.event.actor_type, focus.event.actor_id].filter(Boolean).join(' ')}</span
              >
              <span class="font-mono text-13 text-text-3" title={utcTitle(focus.event.timestamp)}
                >{fmtDateTime(focus.event.timestamp)}</span
              >
            </p>
            {@const read = auditRow(focus.event)}
            <p class="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <span class="text-15 text-text-1" title={read.code}>{read.lead}</span>
              {#if read.words}
                <span class="min-w-0 text-15 break-words text-text-2" title={read.recorded}
                  >{read.words}</span
                >
              {/if}
              {#if read.detail}
                <Why
                  label="payload"
                  controls="audit-focus-payload"
                  bind:open={() => payloadOpen[-1] ?? false, (v) => (payloadOpen[-1] = v)}
                  class={TAP_INLINE}
                />
              {/if}
            </p>
            {#if read.detail}
              <pre
                id="audit-focus-payload"
                hidden={!payloadOpen[-1]}
                class="mt-2 overflow-x-auto border-l border-hair-strong bg-ink-raised px-3.5 py-2.5 font-mono text-[11px] whitespace-pre text-text-2"
                data-audit-payload>{read.detail}</pre>
            {/if}
          {:else if focus.state === 'missing'}
            <p class="text-15 text-text-2">Audit event #{focus.id} is not in this log.</p>
          {:else}
            <p class="text-15 text-text-2">Audit event #{focus.id} could not be read.</p>
          {/if}
        </section>
      {/if}

      {#if eventOptions.length || actorOptions.length}
        <div class="mb-6 flex flex-wrap items-center gap-2">
          {#each eventOptions as e (e)}
            <button
              type="button"
              aria-pressed={eventFilter === e}
              onclick={() => toggleEvent(e)}
              title={e}
              data-audit-chip={e}
              class={chip(eventFilter === e)}>{leadOf(e)}</button
            >
          {/each}
          {#each actorOptions as a (a)}
            <button
              type="button"
              aria-pressed={actorFilter === a}
              onclick={() => toggleActor(a)}
              class={chip(actorFilter === a)}>{a}</button
            >
          {/each}
        </div>
      {/if}

      <div class={dataRowClasses({ head: true, class: `${COLUMNS} max-lg:hidden` })}>
        <span>Event</span><span>Summary</span><span>Item</span><span>Actor</span>
        <span class="text-right">Recorded{loading ? ' · loading' : ''}</span>
      </div>

      {#each events as ev, i (ev.id)}
        {@const read = auditRow(ev)}
        <div
          class={dataRowClasses({
            height: 50,
            rule: i === events.length - 1 ? 'none' : 'quiet',
            class: `${COLUMNS} max-lg:h-auto max-lg:py-2.5`
          })}
          class:bg-ink-selected={ev.id === focus?.id}
          data-audit-row={ev.id}
        >
          <span class="truncate text-13 text-text-1" title={read.code}>{read.lead}</span>
          <span class="flex min-w-0 items-baseline gap-3 max-lg:col-span-full max-lg:row-start-2">
            <span class="min-w-0 truncate text-15 text-text-2" title={read.recorded} data-audit-words
              >{read.words}</span
            >
            {#if read.detail}
              <Why
                label="payload"
                controls={`audit-payload-${ev.id}`}
                bind:open={() => payloadOpen[ev.id] ?? false, (v) => (payloadOpen[ev.id] = v)}
                class={TAP_INLINE}
              />
            {/if}
          </span>
          <span class="font-mono text-13 text-text-3 max-lg:hidden"
            >{ev.issue_num ? `#${ev.issue_num}` : ''}</span
          >
          <span class="truncate font-mono text-13 text-text-3 max-lg:hidden">{ev.actor_type ?? ''}</span>
          <!-- The viewer's zone, with the UTC instant in the title: the same
               event in the Monitor's stream reads the same clock (#2205). -->
          <span
            class="truncate text-right font-mono text-13 text-text-3 max-lg:col-start-2 max-lg:row-start-1"
            title={utcTitle(ev.timestamp)}>{fmtDateTime(ev.timestamp)}</span
          >
        </div>
        {#if read.detail}
          <!-- What the log recorded, verbatim, under the row that read it. -->
          <pre
            id={`audit-payload-${ev.id}`}
            hidden={!payloadOpen[ev.id]}
            class="mb-2.5 overflow-x-auto border-l border-hair-strong bg-ink-raised px-3.5 py-2.5 font-mono text-[11px] whitespace-pre text-text-2"
            data-audit-payload={ev.id}>{read.detail}</pre>
        {/if}
      {:else}
        <p class="py-10 text-center text-13 text-text-3">no audit events match this filter.</p>
      {/each}

      {#if nextAfter != null}
        <div class="mt-6 flex justify-center">
          <button
            type="button"
            onclick={loadMore}
            disabled={loading}
            class="rounded-[4px] border border-hair-strong px-[18px] py-2.5 font-mono text-13
                   text-text-2 transition-colors hover:border-text-5 hover:text-text-1
                   disabled:opacity-50"
          >
            {loading ? 'loading…' : 'load more'}
          </button>
        </div>
      {/if}
    </div>
  {/if}
</div>
