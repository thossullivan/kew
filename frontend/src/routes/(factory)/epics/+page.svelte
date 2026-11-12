<script lang="ts">
  /**
   * FUI-27 — the epics list, restyled onto the hairline primitives.
   *
   * The page is one table in DMonitor's grammar (live runs, y ≈294–440): an
   * 11px head row over 56px rows separated by a quiet rule, with no card
   * enclosing them. Nothing it reads changed — `/api/epics` serves the same
   * summary it always did, and the poll below is the one this page has had
   * since #454.
   *
   * Two readings are borrowed rather than re-decided here: `signalTone` paints
   * the signal dot exactly as the epic strip above the DAG paints it, and
   * `epicProgress` orders and counts the statuses, so a row in this list and
   * the strip on `/epics/[ref]` cannot disagree about how far an epic is. That
   * replaced this page's own STATUS_ORDER and countTone tables — a second
   * ranking of the same statuses, and a second colour for each.
   *
   * #2206 adds a third borrowed reading and the shelf it decides. `epicRoster`
   * splits the roster into Active and Finished / Archived and orders each by
   * last tick, newest first; sixty-six epics sorted by ref put a drain that
   * finished in July above one that ticked a minute ago. The Finished shelf is
   * COLLAPSED by default — that is what "excluded from the default view" means
   * here: retired, not deleted, and one click away with its count always in
   * view, because a shelf whose size you cannot see is a shelf you forget.
   *
   * #2283 is design review cycle 1's verdict on this screen, and it changes
   * what the row IS. Three findings, all of them about leading with the truth:
   *
   *   - D17 — the list had no needs-you column at all, and nine unlabelled
   *     count marks of which five were zero. NEEDS YOU is now the first
   *     column and a link to the node that is waited on (`needsYouHref`), and
   *     only the statuses that happened get a mark (`nonZeroCounts`).
   *   - D16 — "progressing" carried a live cyan halo while the Last tick cell
   *     two columns over said "no ticks yet". A verdict is not a pulse:
   *     `signalReading` withdraws the live claim without a tick inside twice
   *     the interval and says "no heartbeat" instead.
   *   - D4 — at 390px the six columns and nine marks lost the epic names and
   *     stacked the marks over the rows beneath. The table now exists from
   *     1024 up; below it every epic is a two-line block.
   */
  import { SectionHead, StatusMark } from '$lib/ui';
  import TickStrip from '$lib/factory/TickStrip.svelte';
  import { chipClasses, dataRowClasses, TAP_INLINE } from '$lib/factory/classes';
  import { DRAIN_CAPTION, DRAIN_COLUMN, drainTitle } from '$lib/epic/drain';
  import { epicNeedsYou, needsYouSetNotice } from '$lib/epic/attention';
  import { NEEDS_YOU_WORK_HREF, noEpicNeedsYou } from '$lib/epic/attention';
  import { epicProgress } from '$lib/epic/progress';
  import {
    epicRoster,
    epicWithTickLog,
    heartbeatWord,
    heartbeatFresh,
    needsYouHref,
    nonZeroCounts,
    signalReading,
    stalledReason,
    summaryAttempt
  } from '$lib/epic/roster';
  import { signalLabel } from '$lib/factory/names';
  import { epicHref } from '$lib/work/refs';
  import { factoryPulse } from '$lib/shell/pulse.svelte';
  import {
    CHANGE_STRIP_LEGEND,
    CHANGE_WINDOW,
    COMPACT_SLOTS,
    changeSlots,
    changeWord,
    lastTickTs,
    tickCounts,
    windowChangeCount
  } from '$lib/factory/tick-strip';
  import { epicStripRows, readStrips } from '$lib/factory/strip-reads';
  import {
    fetchEpicChangeStrip,
    fetchEpics,
    type EpicSummary,
    type EpicTickStrip
  } from '$lib/api';
  import { isUnauthorized } from '$lib/epic/live';
  import { READ_HOLD_MS, RosterPoll } from '$lib/epic/roster-poll';
  import { fmtTime } from '$lib/time';

  let { data }: { data: { epics: EpicSummary[] | null } } = $props();

  const liveMode = $derived(data.epics != null);
  let epics = $state<EpicSummary[]>([]);

  /**
   * #3054 C7-5 — when the list was last read, and whether the read after it
   * was aborted at `READ_HOLD_MS`. The abort ends the fetch, not the server's
   * work, and the rows below are then the last answer that did land: the page
   * says so in a sentence until a read lands again.
   */
  let lastRead = $state<number | null>(null);
  let stale = $state(false);

  // Seed (and re-seed, on a fresh load()) from `data.epics` inside a
  // reactive effect rather than at $state-initializer time — reading a prop
  // directly in an initializer only captures its value once and never
  // updates on a later load(), which is exactly the bug this page had before
  // #454's final review (svelte-check's state_referenced_locally warning).
  $effect(() => {
    epics = heartbeatRead(data.epics ?? []);
    lastRead = Date.now();
  });

  /**
   * The roster as the rows read it: each epic's heartbeat taken the way its
   * epic page takes it (#2844 N18, `epicWithTickLog`) — off the newest tick
   * attempt the summary serves on EVERY row, so a row past the strip cap
   * cannot read "no heartbeat" beside a page that reads live. Read as each
   * payload lands, because it is a reading of that payload and nothing else.
   */
  function heartbeatRead(rows: EpicSummary[]): EpicSummary[] {
    return rows.map((epic) => epicWithTickLog(epic));
  }

  const roster = $derived(epicRoster(epics));

  /**
   * Each row's needs-you count — the SERVER's one set, filtered by that epic
   * (#2279/D6). This list showed none at all while the header said 1 and two
   * epic legends said 1 and 2, so the one place an operator compares drains
   * was the one place the number was missing. It is read off the same payload
   * the header counts (`factoryPulse.dashboard.needs_you_by_epic`, subscribed
   * by the (factory) layout) rather than from `/api/epics`: a count derived
   * here from anything else would be a fourth opinion about one phrase.
   */
  const needsYouNotice = $derived(needsYouSetNotice(factoryPulse.dashboard));

  /**
   * The set's entries that name no epic (#2784) — an orphan PR, a spend
   * approval on a loose issue. The header counts them and no epic row could,
   * so the header read 11 over a column totalling 10 with the eleventh nowhere
   * on this screen. They get one row of their own, last, linking to Work where
   * each is a row with its command: the column then sums to the header.
   */
  const noEpic = $derived(noEpicNeedsYou(factoryPulse.dashboard));

  /**
   * Each row's tick strip (#2304) — what its driver has actually been doing.
   *
   * Since #2844 D23 its marks are the epic's changes, its failed ticks and its
   * live tick, read off `epic_tick_log` with the no-ops filtered out by the
   * server — so the strip moves when `change_count` moves, and a failing
   * driver still shows its red ×s. It is a picture only: the row's heartbeat
   * comes off the summary (`epicWithTickLog`), never off this read.
   *
   * Read per row, bounded by `$lib/factory/strip-reads`: the first N rows of the
   * roster, and only the rows whose tick-ATTEMPT stamp has moved since their
   * strip was read — attempt, because the heartbeat instants move only for a
   * tick that got past the lock, the preflight and the non-empty-graph guard,
   * and the attempts they turn away are the strip's whole subject (see
   * `epicStripRows`). A quiet roster therefore asks for nothing on its second poll —
   * without that rule this page's 5s interval would be one request per epic
   * forever, which is the shape of the problem #2296 had to undo.
   */
  let strips = $state<Record<string, EpicTickStrip[]>>({});
  const stripRead = new Map<string, string>();

  function readEpicStrips(rows: EpicSummary[]): void {
    readStrips(
      epicStripRows(rows),
      stripRead,
      (ref) => fetchEpicChangeStrip(ref, CHANGE_WINDOW),
      (ref, ticks) => {
        strips = { ...strips, [ref]: ticks };
      }
    );
  }

  $effect(() => {
    if (!liveMode) return;
    const rows = roster.active;
    // A hidden tab reads nothing (#3054 C7-2). The rows are read above the
    // check, so the roster the show lands re-runs this effect.
    if (document.hidden) return;
    readEpicStrips(rows);
  });

  /**
   * Whether the Finished / Archived shelf is open. Operator state, not server
   * state: it survives a poll (the interval replaces `epics`, not this) and
   * resets on navigation, like every other disclosure in the factory.
   */
  let showFinished = $state(false);

  const fmtUSD = (n: number) => '$' + n.toFixed(2);

  // EpicSummaryDTO carries last_tick_ts + change_count but no tick counter
  // (that only exists on EpicHeartbeatDTO, used by the [ref] detail page) —
  // so the list row shows "change M" + a last-tick age, not "tick N".
  function tickAge(iso: string | null): string {
    if (!iso) return 'no ticks yet';
    const ms = Date.now() - Date.parse(iso);
    if (Number.isNaN(ms)) return '—';
    const s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return `${s}s ago`;
    const m = Math.round(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h}h ago`;
    const d = Math.round(h / 24);
    return `${d}d ago`;
  }

  /** Every non-zero count in words, for the Nodes cell's title: below 1024 the
   * cell draws only the marks one line holds (#2844 P22). */
  function countsTitle(legend: { status: string; count: number }[]): string | undefined {
    const counts = nonZeroCounts(legend).map((entry) => `${entry.status} ${entry.count}`);
    return counts.length > 0 ? counts.join(' · ') : undefined;
  }

  /**
   * What the Finished row says in place of a signal word. An archived epic's
   * reason is the operator's own sentence; a `complete` one has none, so the
   * signal stands. Never invented: an archive recorded without a reason reads
   * "archived" and nothing more.
   */
  function retirement(epic: EpicSummary): string {
    if (!epic.archived) return '';
    return epic.archive_reason ? `archived — ${epic.archive_reason}` : 'archived';
  }

  // Poll refresh: mirrors routes/loops/+page.svelte's setInterval-with-cleanup
  // pattern. There's no per-epic terminal state at the list level (unlike a
  // loop's TRUE-terminal statuses) — the set of epics and their signals can
  // always change — so this polls unconditionally while live, cleaning up the
  // interval on unmount. `RosterPoll` keeps one read out at a time, lets only
  // the newest answer land, and reads once when the tab is shown (#2900 C3).
  $effect(() => {
    if (!liveMode) return;
    const poll = new RosterPoll<EpicSummary[]>({
      read: (signal) => fetchEpics(fetch, signal),
      land: (next) => {
        epics = heartbeatRead(next);
        lastRead = Date.now();
        stale = false;
      },
      fail: (e) => {
        if (isUnauthorized(e)) {
          window.location.href = '/inbox';
          return;
        }
        /* keep showing the last good list on a transient poll failure */
      },
      // A hidden tab reads nothing (#2285, C8), and draws no answer that
      // lands after it hid (#3054 C7-2).
      hidden: () => document.hidden,
      stale: () => {
        stale = true;
      }
    });
    const timer = setInterval(() => poll.beat(), 5000);
    const onVisibility = () => {
      if (!document.hidden) poll.show();
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibility);
      poll.stop();
    };
  });

  /**
   * The table, from 1024 up (#2283 D4). Seven columns now: NEEDS YOU leads it,
   * because the one thing this screen is read for is which drain wants a
   * person (DESIGN.md: "Lead every surface with what needs a person").
   *
   * The fixed widths are what fits at 1024. Five fixed columns —
   * 104 + 200 + 88 + 72 + 96 = 560px — plus six 20px gaps is 680px; at 1024,
   * less the page's own 80px gutter (`sm:px-10`), that leaves 264px for the
   * two elastic cells, and both carry `min-w-0` so they shrink to an ellipsis
   * rather than pushing the row past the viewport. At 768 the same budget
   * leaves 8px for the pair, which is the defect this fix is about. Below 1024
   * there is no table at all; see the row. #2304's tick strip is an eighth
   * column that appears at 1280 instead of spending that 264px; the track
   * lists themselves moved into the scoped stylesheet at the foot of this
   * file, because an inline grid-template cannot carry a media query.
   */

  /** One slot of a row strip, in px. `.epic-row`'s xl column is
   * `COMPACT_SLOTS * STRIP_SLOT_PX` wide and the two must agree, or the strips
   * stop sharing an edge — which is the only reason to draw one per row. */
  const STRIP_SLOT_PX = 7;
</script>

{#snippet headRow()}
  <!--
    The heads name the seven columns, which only exist from 1024 up: below that
    a row is a two-line block and a heading strip over it would name cells that
    are not there (#2283 D4, as DMonitor's live runs already do at this width).
  -->
  <div class={dataRowClasses({ head: true, class: 'epic-row hidden lg:grid' })}>
    <span>Needs you</span><span>Epic</span><span>Signal</span><span>Nodes</span>
    <!-- One word, as every other head (#2787 N22): the server's caption
         ("drain spend") wrapped onto two lines in the 88px track. The caption
         stays the head's `title` — always "drain spend", whatever captions the
         rows carry (#2844 P9; a row's own caption is on its value). -->
    <span class="text-right" title={DRAIN_CAPTION}>{DRAIN_COLUMN}</span>
    <!-- What a mark in the strip below is (#2844 D23): a change is a grey dot,
         and only the failed tick and the live one keep a status colour. -->
    <span class="hidden xl:block" title={CHANGE_STRIP_LEGEND}>Ticks</span>
    <span class="text-right" title={`changes in the strip's last ${CHANGE_WINDOW} marks`}>Change</span
    ><span class="text-right">Last tick</span>
  </div>
{/snippet}

{#snippet noEpicRow()}
  <!-- The "no epic" bucket's row (#2784): the same needs-you cell as an epic
       row, the bucket's name where an epic's would be, and no drain readings —
       there is no drain. Grid cells are placed in order, so the empty ones keep
       the count under the Needs you head. The diamond is drawn for a known
       count only: an unknown one reads a bare "?" in text-3 (#2842). -->
  <div
    data-no-epic-row
    class={dataRowClasses({
      rule: 'none',
      class: 'epic-row flex h-auto flex-wrap gap-3 py-3 lg:grid lg:h-14 lg:gap-5 lg:py-0'
    })}
  >
    <a
      href={NEEDS_YOU_WORK_HREF}
      class={`order-1 flex min-w-0 items-center gap-1.5 truncate font-mono text-[11px]
              underline-offset-2 hover:underline lg:order-none ${TAP_INLINE}`}
      style:color={noEpic.known ? 'var(--accent)' : 'var(--text-3)'}
      aria-label={`${noEpic.label} needs you, in no epic`}
      title={noEpic.known ? `${noEpic.count} needs you` : (needsYouNotice ?? undefined)}
    >
      {#if noEpic.known && noEpic.count > 0}<StatusMark kind="needs_you" size={10} />{/if}
      <span data-no-epic-needs-you>{noEpic.label}</span>
    </a>
    <span class="order-2 truncate font-mono text-13 text-text-3 lg:order-none">no epic</span>
  </div>
{/snippet}

{#snippet shelf(rows: EpicSummary[])}
  {@render headRow()}
  {#each rows as epic, i (epic.epic_ref)}
    {@const progress = epicProgress(epic.counts)}
    {@const needs = epicNeedsYou(factoryPulse.dashboard, epic.epic_ref)}
    {@const signal = signalReading(epic, Date.now(), needs.count > 0)}
    {@const stall = stalledReason(epic, Date.now(), needs.count)}
    {@const changes = windowChangeCount(strips[epic.epic_ref], epic.driver_status)}
    {@const lastTs = lastTickTs(epic.last_tick_ts, [
      ...summaryAttempt(epic),
      ...(strips[epic.epic_ref] ?? [])
    ])}
    <!--
      Seven columns from 1024 up, a two-line block below it (#2283 D4): at
      390px the old row put six columns and nine count marks across the
      screen, which lost the epic names and stacked the marks over the rows
      under them. The cells are the same DOM nodes at both widths and in the
      order the heads name them — that is the order a screen reader reads at
      any width — so the phone layout is `order` over a wrapping flex row, as
      DMonitor's live runs are. The name and its signal on the first line, what
      the drain has done on the second.

      The row is a `div` and not one big `<a>`: the needs-you cell is a link of
      its own now, and an anchor inside an anchor is not a document. The two
      links carry the row's two destinations instead, each with its own hover —
      which is how the live-runs table beside it has always worked.
    -->
    <div
      data-epic-row={epic.epic_ref}
      class={dataRowClasses({
        rule: i === rows.length - 1 ? 'none' : 'quiet',
        class: 'epic-row flex h-auto flex-wrap gap-3 py-3 lg:grid lg:h-14 lg:gap-5 lg:py-0'
      })}
    >
      <!-- The needs-you reading, FIRST and linked (#2283 D17): magenta ONLY
           where somebody really is waiting, a bare "?" in text-3 where the set
           could not be composed — no diamond over an unknown set (#2842) —
           and an empty cell where nobody is — a badge saying
           0 on sixty-six rows is sixty-six marks for no information, and
           magenta is rationed (DESIGN.md). The words "needs you" are the
           column's head above 1024 and the link's own accessible name below
           it, where there is no head row to carry them. -->
      {#if !needs.known || needs.count > 0}
        <a
          href={needsYouHref(epic.epic_ref, factoryPulse.dashboard?.rows)}
          class={`order-1 flex min-w-0 items-center gap-1.5 truncate font-mono text-[11px]
                  underline-offset-2 hover:underline lg:order-none ${TAP_INLINE}`}
          style:color={needs.known ? 'var(--accent)' : 'var(--text-3)'}
          aria-label={`${needs.label} needs you`}
          title={needs.known ? `${needs.count} needs you` : (needsYouNotice ?? undefined)}
        >
          {#if needs.known}<StatusMark kind="needs_you" size={10} />{/if}
          <span data-epic-row-needs-you>{needs.label}</span>
        </a>
      {:else}
        <!-- An empty cell, and only where there are cells: the grid places
             children in order, so a row that skipped this one would slide every
             reading left into the wrong column. Below 1024 it is not drawn at
             all, where a blank flex item would be a gap in a sentence. -->
        <span class="order-1 hidden lg:order-none lg:block"></span>
      {/if}

      <!-- `basis-0 grow` below 1024 (#2844 P22): the name takes what the line
           has left and truncates, so a long ref or a "no heartbeat" note can
           never push the signal onto a line of its own. Inert in the grid. -->
      <span class="order-2 flex min-w-0 grow basis-0 items-baseline gap-2.5 lg:order-none">
        <a
          href={epicHref(epic.epic_ref)}
          class={`truncate font-mono text-13 text-text-1 underline-offset-2
                  hover:underline ${TAP_INLINE}`}>{epic.epic_ref}</a
        >
        <!-- #2206's retirement sentence, and nothing where it is absent: the
             only other caption an EpicSummary carries is the drain figure's,
             which now heads that column instead of repeating on every row. -->
        {#if retirement(epic)}
          <span class="truncate font-mono text-[11px] text-text-3">{retirement(epic)}</span>
        {/if}
      </span>

      <!-- The strip's dot and word, drawn from the same tone table, so the
           list and the epic's own header cannot disagree about its signal —
           with the LIVE claim withdrawn where no heartbeat backs it up
           (#2283 D16, decided in `signalReading`). -->
      <!-- A stalled epic carries its reason (#3054 N14): "10 need you" in the
           magenta `signalTone` already gives it where a person is next, or
           "no heartbeat" / "nothing ready" in amber — never a bare "stalled". -->
      <span
        data-signal
        data-stalled-reason={stall?.note}
        class="order-3 flex min-w-0 items-center gap-2 font-mono text-13 lg:order-none"
        style:color={signal.color}
        title={signal.note ? `${epic.signal ?? 'unknown'} · ${signal.note}` : undefined}
        data-heartbeat={heartbeatWord(epic.signal, heartbeatFresh(epic, Date.now())) ?? 'none'}
      >
        <span
          class="size-2 shrink-0 rounded-full"
          style:background={signal.color}
          style:box-shadow={signal.halo
            ? `0 0 0 4px color-mix(in oklab, ${signal.color} 22%, transparent)`
            : 'none'}
        ></span>
        <!-- The signal in words (#2844 P8): `awaiting_merges` printed as
             itself. The recorded code stays the word's title. -->
        <span class="truncate" title={epic.signal ?? undefined}>{signalLabel(epic.signal)}</span>
        {#if signal.note}
          <span class="truncate text-[11px] text-text-3">· {signal.note}</span>
        {:else if stall}
          <!-- What would move it is the reason's title. -->
          <span
            data-stalled-note
            class="truncate text-[11px]"
            style:color={stall.personNext ? 'var(--accent)' : 'var(--text-3)'}
            title={`stalled · ${stall.note} — ${stall.hint}`}>· {stall.note}</span
          >
        {/if}
      </span>

      <span
        class="order-6 flex min-w-0 grow basis-0 items-center gap-2 font-mono text-13 text-text-2 lg:order-none lg:gap-3.5"
        title={countsTitle(progress.legend)}
      >
        <span class="shrink-0 text-text-1"
          >{progress.done}<span class="text-text-3"> / {progress.total}</span></span
        >
        <!-- Only the statuses that happened (#2283 D17, and DESIGN.md: "Show
             only non-zero counts"). Nine marks a row, five of them zero, is
             what the review photographed; the done/total beside them is still
             counted over every status the payload serves, so the zeros are
             not information this row has lost. The word stays the mark's
             title, because a list row has room for the shape and the count
             but not for eight status words. -->
        <!-- One line below 1024 (#2844 P22): every epic is the same two-line
             block, so the marks that do not fit wrap out of the one-line box
             and are not drawn — whole, never a mark cut in half (DESIGN.md:
             the phone row fits by carrying less). The cell's title carries
             every count, and the epic's page draws them all. One line at
             every width since #3054 N18: go-live's nine marks wrapped onto a
             second line inside the table's 56px row. The column is wider
             too (the stylesheet), so at 1440 a row that long draws whole. -->
        <span
          data-nodes-marks
          class="flex h-[1lh] min-w-0 flex-wrap items-center gap-x-2 gap-y-1 overflow-hidden lg:gap-x-3"
        >
          {#each nonZeroCounts(progress.legend) as entry (entry.status)}
            <span class="flex items-center gap-1.5" title={`${entry.status} ${entry.count}`}>
              <StatusMark kind={entry.status} size={7} />
              {entry.count}
            </span>
          {/each}
        </span>
      </span>

      <!-- A row whose own caption is not the head's keeps it here, so a
           figure measured over a different window still says so. -->
      <!-- `data-tour`: the demo's spend tour reads the first row's drain, once
           per page (#3149). -->
      <span
        class="order-7 ml-auto text-right font-mono text-13 text-text-2 lg:order-none lg:ml-0"
        data-tour={i === 0 ? 'epic-drain' : undefined}
        title={drainTitle(DRAIN_CAPTION, epic.label)}>{fmtUSD(epic.drain_total_usd)}</span
      >
      <!-- The tick strip (#2304), from 1280 only — the width where it does not
           come out of the two elastic cells' 1024 budget (see the stylesheet at
           the foot of this file). A strip of fixed slots is the one cell that
           cannot shrink, and on a phone it would cost the block a third line.
           Its marks are the epic's CHANGES (#2844 D23, `changeSlots`): one grey
           dot per attempt that moved the snapshot, plus the failed ticks' red ×
           and the live tick — a strip of attempts was flat beside a rising
           CHANGE. The server reads the marks themselves
           (`fetchEpicChangeStrip`), so no run of no-ops can hide a change.
           `changeSlots` pads it to that fixed width, so the strips down this
           column share an edge and can be read against each other. The row's
           served `driver_status` goes with the ticks: an open row past the
           strip's own floor is only abandoned if the server cannot still see
           its driver beating (`isTickLive`). -->
      <span data-ticks-cell class="hidden xl:block">
        {#if strips[epic.epic_ref]}
          <TickStrip
            slots={changeSlots(strips[epic.epic_ref], COMPACT_SLOTS, epic.driver_status)}
            slotPx={STRIP_SLOT_PX}
            label={`${epic.epic_ref} changes and failed driver ticks`}
            popover={tickCounts}
            word={(t) => changeWord(t, epic.driver_status)}
          />
        {/if}
      </span>
      <!-- Change and Last tick are the table's own two columns and are drawn
           only where the table is. On a phone they would cost the block a
           third line, and a bare "0" under no head row is not a reading: the
           epic's own page states both in full, and what the list is read for
           on a phone — who needs me, how far is it — is on the two lines
           above. The heartbeat is not lost with them: the signal cell says
           "no heartbeat" in its own words at every width. -->
      <!-- The changes inside the strip's window (#3054 N1), counted off the
           strip's own read: the served `change_count` counts journal rows,
           and before #2879 the journal wrote one on every tick. A dash until
           the strip is read, and on rows past the strip cap, which read none.
           A quiet epic's cell is empty, never "0" (D34): DESIGN.md shows only
           non-zero counts. -->
      <span class="hidden text-right font-mono text-13 text-text-3 lg:block"
        data-change-count={changes ?? ''}
        title={changes === null ? 'change strip not read' : undefined}
        >{changes === null ? '—' : changes > 0 ? changes : ''}</span
      >
      <!-- #2545 D23 — off the tick log as well as the journal, so a row with
           ticks in its strip never reads "no ticks yet" beside them: the
           newest attempt the summary serves, and the strip's own marks for a
           `kew serve` that serves no attempt. -->
      <span
        data-last-tick={lastTs ?? ''}
        class="hidden truncate text-right font-mono text-13 text-text-3 lg:block"
        >{tickAge(lastTs)}</span
      >
      <!--
        The line break, below 1024 only: a flex row wraps where it runs out of
        width, and where the second line starts here is a decision, not an
        overflow. Removed from the grid above that, where there is one line.
      -->
      <span class="order-5 w-full lg:hidden" aria-hidden="true"></span>
    </div>
  {/each}
{/snippet}

<div class="flex min-h-0 flex-1 flex-col">
  <header
    class="flex shrink-0 flex-wrap items-baseline gap-4 border-b border-hair px-4 pt-5 pb-[18px] sm:px-10"
  >
    <!-- Mono, as the other factory h1s are (#2283, D18's first half). The
         "dependency graph" subtitle is gone with it: the page is called Epics,
         the graph is what an epic's own page draws, and the words were a
         second name for a screen that already had one. -->
    <h1 class="font-mono text-26 text-text-1">Epics</h1>
    <div class="grow"></div>
    <span class="font-mono text-13 text-text-3">
      {liveMode ? 'kew serve · connected' : 'offline · no data'}
    </span>
  </header>

  <div class="min-h-0 flex-1 overflow-y-auto px-4 pt-7 pb-9 sm:px-10">
    <!-- #3054 C7-5: a read held past READ_HOLD_MS is aborted, and the rows
         below are then the last answer that landed. Said once, in a
         sentence, until a read lands again. -->
    {#if liveMode && stale}
      <p data-epics-stale role="status" class="pb-5 text-13 text-text-2">
        This list is stale: the last read took longer than {READ_HOLD_MS / 1000} s and was
        stopped. It was last read at
        <span class="font-mono">{lastRead === null ? 'no time yet' : fmtTime(lastRead)}</span>; the
        next read that lands replaces it.
      </p>
    {/if}
    <SectionHead label="Active" meta={liveMode ? `${roster.active.length} driven` : 'not read'} />

    {#if !liveMode}
      <p class="py-10 text-center text-13 text-text-3">start kew serve to see epics.</p>
    {:else if epics.length === 0}
      <p class="py-10 text-center text-13 text-text-3">
        No epics driven yet — <span class="font-mono text-text-2">kew epic run &lt;name&gt;</span>
        records the first drain.
      </p>
    {:else}
      {#if roster.active.length === 0}
        <p class="py-10 text-center text-13 text-text-3">
          Every epic is finished or archived — nothing is being driven.
        </p>
      {:else}
        {@render shelf(roster.active)}
      {/if}

      {#if factoryPulse.dashboard && (!noEpic.known || noEpic.count > 0)}
        <div class="border-t border-hair">{@render noEpicRow()}</div>
      {/if}

      {#if roster.finished.length > 0}
        <div class="pt-9">
          <SectionHead label="Finished / Archived">
            <button
              type="button"
              class={chipClasses(showFinished)}
              aria-expanded={showFinished}
              onclick={() => (showFinished = !showFinished)}
            >
              {roster.finished.length} retired · {showFinished ? 'hide' : 'show'}
            </button>
          </SectionHead>
          {#if showFinished}
            {@render shelf(roster.finished)}
          {/if}
        </div>
      {/if}
    {/if}
  </div>
</div>

<style>
  /*
    The table's columns (#2283 D4's budget), and the one column #2304 adds.

    Seven at 1024: 104 + 200 + 88 + 72 + 96 = 560px of fixed cells plus six
    20px gaps is 680px; less the page's own 80px gutter a 1024 viewport leaves
    264px for the two elastic cells, which is what that review settled on.
    Adding the 112px tick strip there would cut those two to 132px and lose the
    epic names again — so the strip is an EIGHTH column that appears at 1280
    (812px of 1200, leaving 388px elastic) and nothing below it moves. Its cell
    is `hidden xl:block`, and a `display: none` cell is not a grid item, so the
    seven-column track list below is still exact at 1024.

    The Signal cell is 56px wider from 1280 (design review c4, D22): at 200px
    "progressing · no heartbeat" was cut to "progressi…" beside ~400px of
    empty width at 1440. 256px holds the word and its note whole; the eight
    columns are then 868px of 1200, which still leaves the two elastic cells
    332px — more than the 264px the 1024 table gives them.

    In CSS rather than an inline `style:` because an inline grid-template
    cannot carry a media query, and the alternative — two readings of the same
    row keyed off a JS width guess — is how a layout starts disagreeing with
    itself mid-resize.
  */
  /*
    Nodes takes twice the Epic cell's share since #3054 N18: at 1:1.1 go-live's
    nine marks needed a second line at 1440, while its ref used half of the
    Epic cell. 2fr gives Nodes ~328px at 1440, which draws that row whole. The
    Epic cell keeps a 120px floor so a "go-live-2026-09"-length ref is not cut
    at 1280; Nodes keeps one line at every width and drops whole marks below
    what it holds, its title carrying every count.
  */
  :global(.epic-row) {
    grid-template-columns: 104px minmax(120px, 1fr) 200px minmax(0, 2fr) 88px 72px 96px;
  }

  @media (min-width: 1280px) {
    :global(.epic-row) {
      grid-template-columns: 104px minmax(120px, 1fr) 256px minmax(0, 2fr) 88px 112px 72px 96px;
    }
  }
</style>
