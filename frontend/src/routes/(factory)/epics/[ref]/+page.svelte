<script lang="ts">
  import {
    fetchEpic,
    fetchEpicNode,
    fetchEpicTickStrip,
    fetchEpicTicks,
    fetchEpicTimeline,
    fetchEvidence,
    fetchPrFreshness,
    fetchPipeline,
    fetchWorkView,
    streamEpic,
    type EpicDetail,
    type EpicNode,
    type EpicNodeDetail,
    type EpicTick,
    type EpicTickStrip,
    type EpicTimeline,
    type ItemEvidence,
    type ItemPipeline,
    type PrFreshnessRow,
    type WorkView
  } from '$lib/api';
  import { replaceState } from '$app/navigation';
  import { page } from '$app/state';
  import { untrack } from 'svelte';
  import { pageVisibility } from '$lib/shell/visibility.svelte';
  import LineageBox from '$lib/epic/LineageBox.svelte';
  import DagView from '$lib/epic/DagView.svelte';
  import {
    failedReading,
    FRESHNESS_PAGE,
    freshnessNotice,
    readFreshness,
    type FreshnessReading
  } from '$lib/freshness';
  import EpicTimelineView from '$lib/epic/EpicTimeline.svelte';
  import TickTimeline from '$lib/factory/TickTimeline.svelte';
  import {
    FULL_STRIP_LIMIT,
    fitStripWidth,
    heartbeatFromTickLog,
    lastTickTs as tickLogLastTs,
    stripLimit
  } from '$lib/factory/tick-strip';
  import SelectedRail from '$lib/epic/SelectedRail.svelte';
  import {
    CANVAS_VIEWS,
    CANVAS_VIEW_LABEL,
    NOW_POLL_MS,
    canvasView,
    otherView,
    EPIC_KEYS,
    togglesView,
    type CanvasView
  } from '$lib/epic/timeline';
  import { bindShortcuts, isTyping, registerShortcuts } from '$lib/keys/registry';
  import {
    nodeMoment,
    railSource,
    EVIDENCE_ABSENT,
    PIPELINE_ABSENT,
    SHORT_VIEWPORT,
    type RailInput
  } from '$lib/epic/rail';
  import type { Source } from '$lib/monitor/readouts';
  import {
    accumulatedTicker,
    applyFrame,
    initialLiveState,
    isUnauthorized,
    seededTicker,
    type LiveState
  } from '$lib/epic/live';
  import { staleness, type Staleness } from '$lib/epic/staleness';
  import { heartbeatWord } from '$lib/epic/roster';
  import {
    frameAtT,
    frameOffsets,
    parseSpeed,
    parseT,
    playbackDwellMs,
    REPLAY_SPEEDS,
    scrubberStops,
    wallClock,
    type ReplaySpeed
  } from '$lib/epic/replay';
  import type { TickerLine } from '$lib/epic/ticker';
  import { fmtTime, utcTitle } from '$lib/time';
  import { cellStyle, epicProgress, heartbeatTone, signalTone } from '$lib/epic/progress';
  import {
    attentionNotice,
    epicNeedsYou,
    needsYou,
    needsYouReading,
    needsYouSetNotice,
    NEEDS_YOU_WORK_HREF,
    offGraphNeedsYou,
    offGraphNotice,
    stalledWaitsLink
  } from '$lib/epic/attention';
  import {
    escape as escapeFocus,
    linkOpensPopover,
    type Focus,
    type NodeAddress
  } from '$lib/epic/node-popover';
  import { statusShape } from '$lib/factory/status';
  import { headerCounts } from '$lib/shell/header-state';
  import { factoryPulse, PULSE_INTERVAL_MS } from '$lib/shell/pulse.svelte';
  import { Button } from '$lib/ui';
  import { TAP_PILL } from '$lib/factory/classes';
  import StatusMark from '$lib/ui/StatusMark.svelte';
  import ReadyRing from '$lib/epic/ReadyRing.svelte';

  // Server default page size for GET /api/epics/{ref}/ticks (server caps the
  // `limit` param at 1000; we never pass one, so this is the default page
  // it actually returns — a page shorter than this means "last page").
  const TICKS_PAGE_SIZE = 500;

  // Hard cap on enterReplay's pagination loop — 40 pages x 500 = 20k frames,
  // far beyond any real journal drain. Without a cap, a server bug that kept
  // returning full (never-short) pages would spin the loop forever; on
  // hitting the cap we stop fetching and proceed with what's loaded rather
  // than hang (honest degradation over an infinite fetch).
  const MAX_TICK_PAGES = 40;

  // The Graph / Timeline switch's chips. Same shape and tokens as the
  // Monitor's range presets (`EventStream.svelte`), so the console has one
  // segmented control and not two that nearly agree.
  const CHIP =
    'inline-flex h-[22px] cursor-pointer items-center rounded-full border px-2.5 font-mono text-[11px]';
  const CHIP_OFF = 'border-hair-strong text-text-3 hover:text-text-1';
  const CHIP_ON = 'border-accent-2 bg-accent-2-wash text-accent-2';
  // The replay speed chips are buttons, and a small button is 13px mono
  // (`{typography.data-13-mono}`, #3130); 11px is the uppercase label's size.
  const SPEED_CHIP =
    'inline-flex h-[22px] cursor-pointer items-center rounded-full border px-2.5 font-mono text-13';

  let { data }: { data: { detail: EpicDetail | null; ref: string } } = $props();

  // Derived (not `const x = data.y`) so a ref -> ref navigation — SvelteKit's
  // client router reuses this component instance across /epics/<ref> URLs —
  // actually reacts to the new `data`, instead of freezing the FIRST epic's
  // ref/connected-ness for the lifetime of the page (svelte-check's
  // state_referenced_locally warning was flagging exactly this).
  const ref = $derived(data.ref);
  /**
   * The epic as the server last gave it to us, when `load()` itself failed
   * (C17). The page used to be gated on `data.detail` alone, which is read
   * exactly once per navigation: a page opened while `kew serve` was down said
   * "start kew serve to see this epic" until somebody pressed reload, even
   * after the server came back. The retry poll below fills this in, and
   * everything downstream — the live seed, the SSE channel, the rail — comes up
   * off it as if the first read had worked. `null` once `data.detail` answers.
   */
  let reconnected = $state<{ ref: string; detail: EpicDetail } | null>(null);
  /**
   * The first paint, from `load()` or from the retry that replaced it. The
   * retry's reading carries the ref it was taken for, so a ref -> ref
   * navigation discards it by itself — this component instance is reused
   * across /epics/<ref> URLs, and epic A's recovered snapshot under epic B's
   * URL is the exact bug the reset effect below exists to prevent.
   */
  const detail = $derived<EpicDetail | null>(
    data.detail ?? (reconnected?.ref === ref ? reconnected.detail : null)
  );
  // Whether kew serve answered at all on first paint. A pre-journal epic
  // (driver running, nothing ticked yet) is still "connected" — it has a
  // heartbeat but snapshot is null; that is a distinct honest empty state
  // from "server unreachable", handled below.
  const connected = $derived(detail != null);
  /**
   * Whether the live SSE channel is still carrying this epic's frames.
   *
   * A DISTINCT fact from `connected`, which is the first read's answer and
   * latches true for the life of the page — it says the server answered once,
   * which is all the offline retry above needs to know. The motion rule is
   * about the tail, not the first read ("the clock … stops when nothing
   * executes or the stream drops", DESIGN.md): gated on `connected` alone, a
   * severed stream kept the halos running and the bars growing on a canvas
   * that had stopped hearing anything, drawing seconds nothing confirmed.
   *
   * Armed optimistically when the channel opens — an `EventSource` that is
   * still CONNECTING has not failed, and kew proves a thing stopped rather
   * than inferring it — then false on `error` and true again on `open` or on
   * any frame, so this follows EventSource's own auto-retry instead of
   * guessing at it. Reset by the channel's teardown, which is what takes it
   * false on entering replay.
   */
  let streamLive = $state(false);

  let live = $state<LiveState>(initialLiveState({}));

  let now = $state(Date.now());


  /**
   * Whether the selected node's popover is open.
   *
   * Not in the URL: a link lands on a NODE, and which transient box is open
   * over the graph is not part of where the reader is. It lives here rather
   * than in `DagView` because Esc is one rule over both halves of the focus
   * (`escape` in $lib/epic/node-popover) — close the popover and keep the
   * selection, then clear the selection — and a rule split across two
   * components is a rule that disagrees with itself.
   */
  let popover = $state(false);
  // The four reads the rail makes for ONE selection (FUI-17). `undefined` is
  // "not asked yet" — a source is only ever set to a payload or to the named
  // reason there isn't one, so a column can say which of the two it is.
  let nodeDetail = $state<Source<EpicNodeDetail> | undefined>(undefined);
  let nodeView = $state<Source<WorkView> | undefined>(undefined);
  let nodePipeline = $state<Source<ItemPipeline> | undefined>(undefined);
  let nodeEvidence = $state<Source<ItemEvidence> | undefined>(undefined);

  // --- mode: 'live' (SSE tail) | 'replay' (time-lapse over the full journal)
  let mode = $state<'live' | 'replay'>('live');
  // The ticker is a disclosure now (DEpic draws no ticker at all, and the plan
  // keeps it rather than dropping it): collapsed by default so the graph gets
  // the strip's height, open for as long as the operator is reading it.
  let tickerOpen = $state(false);
  // The phone's fold over the legend and the tick strip (D16). Shut is a
  // class, not an `{#if}`: from 640 up both are always drawn, whatever this
  // says, and the strip keeps the box it is measured against.
  let foldOpen = $state(false);
  const foldShut = $derived(foldOpen ? '' : 'max-sm:hidden');
  let replayFrames = $state<EpicTick[]>([]);
  let replayIndex = $state(0);
  let replayPlaying = $state(true);
  let replayLoading = $state(false);
  let replayError = $state(false);
  /**
   * Whether the journal-tail read behind the ticker failed.
   *
   * It used to be swallowed (`catch(() => {})`), so an epic whose `/ticks`
   * read 404s drew an empty ticker under "last tick —" and looked like a
   * driver that had never run (design review C2, D22). A failed read is a
   * sentence on the strip now.
   */
  let tickerError = $state(false);
  /**
   * Whether the READER decided the selection — a `?node=` link they
   * followed, a node they clicked, a walk with the arrows, or an Esc that
   * cleared it. Until they have, the page opens on the node that needs a
   * person (D20): the rail showed one sentence beside an empty 195px column
   * while the graph already knew which node was waiting.
   *
   * Component state, not a reading of the URL: `writeUrl` writes `node: null`
   * on a view switch too, and inferring "the reader cleared it" from that
   * null dropped the opening selection on the way to the timeline.
   *
   * Seeded here as well as in the per-ref reset below, so the FIRST render of
   * a `?node=` link already knows the selection is the reader's — an effect
   * that corrected it afterwards would have opened the link's popover one
   * frame late, or not at all.
   */
  let picked = $state(page.url.searchParams.get('node') !== null);
  /**
   * The opening selection itself (D20), as a decision taken ONCE per epic:
   * the first node somebody was waiting on in the first snapshot this page
   * saw. Null when nobody was.
   *
   * It used to be derived live over the snapshot on screen, which let the
   * page re-aim itself with nobody touching it: a driver tick — or a
   * between-ticks `attention` frame, which rewrites the whole needs-you join
   * — that resolved the waiting node and left a different one waiting moved
   * the selection, and with it panned the reader's camera (DagView's reveal
   * effect), swapped the rail's contents and refired the four per-node reads,
   * all while `picked` was false (review r8).
   */
  let openingPick = $state<string | null>(null);
  /**
   * Whether that decision has been taken for this epic yet.
   *
   * Plain `let`: nothing renders it, and the latch below must not re-run on
   * its own write. It waits for a snapshot that HAS nodes rather than
   * settling at mount — an epic whose driver has not ticked yet loads with
   * `snapshot: null`, and "nobody is waiting" is not knowable from a graph
   * that is not there yet.
   */
  let openingDecided = false;

  // --- the run timeline (#2271) ---------------------------------------------
  // Its own read, not an SSE frame: the Gantt is a ledger projection (runs,
  // review rounds, the merge), not a tick, so the view polls it while it is
  // open. `null` is "not read yet"; `timelineError` is the named reason there
  // is nothing to draw, which the empty state says out loud.
  let timeline = $state<EpicTimeline | null>(null);
  let timelineError = $state(false);

  // --- artifact freshness (#2303) -------------------------------------------
  // Its own read, like the timeline: what a review verdict was about and what
  // moved since is a ledger projection, not a tick, and it reaches every node
  // in ONE request rather than per card. `null` is "not read yet", which draws
  // NO badge — a page that could not read it must not paint "current".
  //
  // THIS epic's rows, across every page they take: the endpoint is bounded and
  // cursored, so a global first page can fill before reaching the epic on
  // screen — and no badge anywhere then reads exactly like nothing being stale.
  // `readFreshness` asks for this ref, follows the cursor, and reports what it
  // could not reach for `freshNotice` to say on the strip.
  let freshness = $state<FreshnessReading | null>(null);
  /** Bumped by the retry button, so one failed read is not the end of it. */
  let timelineAttempt = $state(0);

  // --- the full tick strip (#2304) ------------------------------------------
  // Every ATTEMPT this driver has made lately, not only the ticks that changed
  // something. Declared up here with the other per-epic reads so the reset
  // below drops them with the rest of the previous epic's readings: carried
  // across a ref -> ref navigation, these two drew epic A's attempts under
  // epic B's aria-label and repeated epic A's read failure beside epic B
  // (reviewer finding, round 4). The effect that fills them — and the signal
  // it re-reads on — is further down, beside the other reads it belongs with.
  //
  // `tickStripError` is said out loud (D22) rather than swallowed: an empty
  // strip beside a live driver is a claim, and only a read that SUCCEEDED may
  // make it.
  let tickStrip = $state<EpicTickStrip[]>([]);
  // A short window has no height to spare for the strip: the canvas keeps a
  // graph's worth of height under the folded rail (#2333's ui-check reads
  // 300px at 1440x600), so the strip folds away below the rail's own
  // `SHORT_VIEWPORT`, as the rail does.
  let viewportH = $state(0);
  const stripFits = $derived(viewportH === 0 || viewportH >= SHORT_VIEWPORT);
  // How WIDE the strip may be (#2473). The design's 760px is wider than this
  // section's box on a phone, and the column above clips the x axis rather than
  // scrolling it — so a 760px strip at 390px lost its right-hand ~450px, which
  // is where its newest attempts are. Measured off the box this strip is in
  // rather than folded at a breakpoint: the box is the window less this
  // section's gutters and whatever the scroller takes, so the breakpoint where
  // 760 stops fitting is not a number this file can know. The rule itself is
  // `fitStripWidth`, where it is unit-tested.
  let stripBox = $state(0);
  const stripWidth = $derived(fitStripWidth(stripBox));
  // And how MANY attempts that width can draw apart (#2473, review round 1).
  // Fitting the width alone handed a 310px lane the 189 attempts the 760px one
  // was sized for, and the batcher's 4px floor is a cliff, not a squeeze: past
  // what the lane can resolve the merge cascades and the whole strip is one
  // bar. So the newest `stripLimit` of them are drawn, while the read itself
  // still asks for the design width's worth — widening the window re-adds the
  // older attempts from rows already in hand rather than from the server.
  const stripShown = $derived(tickStrip.slice(-stripLimit(stripWidth)));
  let tickStripError = $state(false);

  // Re-initialize ALL per-epic state whenever `ref` actually changes,
  // including the FIRST run (component mount) — a plain `let x = $state(...)`
  // initializer only captures data.detail's value once and would show epic
  // A's snapshot under epic B's URL on a ref -> ref navigation; this effect
  // is the one place that reads `data.detail` reactively, so every ref gets
  // its own fresh live/replay/selection state (see the {#key data.ref} below,
  // which remounts the markup/DagView layout alongside this reset).
  // Request epoch for async tick fetches: bumped on every per-ref reset so a
  // fetch started under epic A discards its result if it lands after the user
  // navigated to epic B (see enterReplay). Plain let — nothing renders it.
  let replayGen = 0;
  /**
   * The navigation whose replay link this page has answered (see the effect
   * beside `enterReplay`): the `page.url` it arrived on. Plain let, for
   * `replayGen`'s reason.
   */
  let linkAnswered: URL | null = null;
  /**
   * The `?t=` this page last wrote (null for none), so `answerLink` can tell
   * a navigation that carries the page's own state back from a fresh link.
   * Plain let, for `replayGen`'s reason.
   */
  let writtenT: number | null = null;
  /** Request epoch for the freshness poll, for `replayGen`'s reason. */
  let freshnessGen = 0;

  $effect(() => {
    void ref; // dependency: rerun whenever the ref (and therefore `data`) changes
    replayGen += 1;
    linkAnswered = null;
    writtenT = null;
    // ...and the freshness read's own epoch with it. The poll effect does bump
    // this when it re-arms, but it only re-arms for `connected`/`view`/`mode`
    // and its own ref capture; orphaning the in-flight read HERE is what makes
    // "a reading for epic A can never land under epic B" true of the reset
    // itself, which is the one thing that runs on every ref change.
    freshnessGen += 1;
    live = initialLiveState({
      snapshot: detail?.snapshot ?? null,
      economics: detail?.economics ?? null,
      heartbeat: detail?.heartbeat ?? null
    });
    now = Date.now();
    popover = false;
    clearNodeReads();
    mode = 'live';
    tickerOpen = false;
    foldOpen = false;
    replayFrames = [];
    replayIndex = 0;
    replayPlaying = true;
    replayLoading = false;
    replayError = false;
    tickerError = false;
    // A link that names a node is the reader's own selection; nothing else
    // is. Untracked, because this effect must rerun for a new `ref` and for
    // nothing else — `page.url` moves on every selection.
    picked = untrack(() => page.url.searchParams.get('node') !== null);
    // ...and the opening selection is decided again, from the NEW epic's own
    // first snapshot rather than the one the latch below already spent.
    openingPick = null;
    openingDecided = false;
    timeline = null;
    timelineError = false;
    timelineAttempt = 0;
    freshness = null;
    // The strip and its read failure belong to the epic they were read for.
    tickStrip = [];
    tickStripError = false;
  });

  // Replay is only offered once the journal actually has recorded change —
  // change_count is the heartbeat's own count of ticks that moved the
  // signal, so 0 means "nothing to time-lapse" even if tick_count > 0.
  const canReplay = $derived((live.heartbeat?.change_count ?? 0) > 0);

  // The heartbeat as the tick log has it (#2783, N18): the strip below and the
  // Epics list both read that log, so the staleness line and "last tick" read
  // it too rather than saying "no heartbeat yet" above a strip of attempts.
  const beat = $derived(heartbeatFromTickLog(live.heartbeat, tickStrip));
  const st = $derived<Staleness>(staleness(beat, now));

  // The snapshot/economics/ticker actually fed to DagView + the header,
  // switched by mode. Replay frames come from a fully-paginated tick fetch
  // (see enterReplay below), not from live's SSE-fed state.
  const replayFrame = $derived<EpicTick | null>(replayFrames[replayIndex] ?? null);
  const currentSnapshot = $derived(mode === 'live' ? live.snapshot : (replayFrame?.snapshot ?? null));

  /**
   * The selection is URL state (`?node=2219`), not component state.
   *
   * It was `let selected = $state(null)`, which made a node's panel
   * unreachable by link and left Back unable to undo a selection — P7 of the
   * UX brief. The URL is the single reading now: `onselect` writes it with
   * `replaceState` (a selection is not a navigation, and walking the graph
   * with the arrow keys must not flood history, which is `work/+page.svelte`'s
   * rule for the same move), and this reads it back.
   *
   * `?node=` is matched against the SNAPSHOT, so an unknown id selects
   * nothing: a stale or hand-typed link must not put the rail on a node this
   * epic does not have, and must not take the four per-selection reads for one
   * either.
   */
  //
  // Read through `page.state` first: `replaceState` is shallow routing, which
  // rewrites the address bar and `page.state` but NOT `page.url` — reading the
  // URL alone left every click on a node writing `?node=` while the rail, the
  // popover and the selection ring stayed on the old value. `page.url` is the
  // reading only until the first selection (a link that lands on `?node=`).
  const wanted = $derived<string | null>(
    page.state.node !== undefined ? page.state.node : page.url.searchParams.get('node')
  );
  /**
   * The node the page opens on when the reader has not picked one: the node
   * `openingPick` latched, which is the one the canvas already frames
   * (`firstFrame`'s focus) and the one the rail has Why / Do this for (D20).
   * Null once the reader has picked anything, including an Esc that cleared
   * the selection — the default is not handed straight back.
   */
  const opening = $derived<string | null>(picked ? null : openingPick);
  /**
   * The one place the opening selection is decided: the first snapshot that
   * has nodes in it, whether that is the one `load` brought or the first SSE
   * frame of an epic that had not ticked yet. Every snapshot after it is
   * ignored, so only the reader moves the selection from here on.
   */
  $effect(() => {
    const nodes = currentSnapshot?.nodes ?? [];
    if (openingDecided || nodes.length === 0) return;
    openingDecided = true;
    openingPick = nodes.find((n) => needsYou(n))?.id ?? null;
  });
  /** `id` when the snapshot on screen carries it, else null. */
  function onScreen(id: string | null): string | null {
    if (id == null) return null;
    return (currentSnapshot?.nodes ?? []).some((n) => n.id === id) ? id : null;
  }
  /**
   * The selection every consumer is handed: the reader's `?node=` when the
   * snapshot on screen carries it, else the opening pick on the same terms.
   *
   * BOTH candidates are matched against the snapshot, not just `wanted`
   * (review r9). The opening pick is LATCHED off the first snapshot that had
   * nodes, and the graph on screen can stop carrying it — a replay seek to a
   * frame from before the node existed, or a tick that dropped it. Handed on
   * unvalidated it rang a node the canvas cannot draw, left the rail's
   * `selectedNode` null beside a `selected` that was not, and took the four
   * per-node reads for a node this epic is not showing.
   */
  const selected = $derived<string | null>(onScreen(wanted) ?? onScreen(opening));

  /**
   * The graph's lineage selection (`?select=+%232469+`, #2544), the text the
   * box above the canvas holds. Read through `page.state` first, as `wanted`
   * and `view` are, so a selection typed after load is the one drawn.
   */
  const lineageText = $derived<string | null>(
    page.state.select !== undefined ? page.state.select : page.url.searchParams.get('select')
  );

  /**
   * Which canvas the reader is on (`?view=timeline`), the Graph / Timeline
   * switch's half of the URL state (#2271).
   *
   * Read through `page.state` first for exactly the reason the selection is:
   * `replaceState` is shallow routing, which moves `page.state` and the
   * address bar but NOT `page.url`. The two readings are written together by
   * `writeUrl` — `replaceState` REPLACES the whole page state, so a write
   * that mentioned one of them would silently drop the other.
   */
  const view = $derived<CanvasView>(
    canvasView(
      page.state.epicView !== undefined
        ? page.state.epicView
        : page.url.searchParams.get('view')
    )
  );

  /**
   * The replay a link names (#3142): `?mode=replay`, the moment `?t=` (whole
   * seconds since the first frame) and the playback `?speed=`. Read through
   * `page.state` first for the selection's reason — shallow routing never
   * moves `page.url` — and written by `writeUrl` with every other key, because
   * `replaceState` replaces the whole state: a node click that wrote only
   * `node` would drop `t`.
   *
   * `mode` itself stays component state: the URL asks for replay once per
   * navigation (`linkAnswered`), and from then on mirrors the toggle.
   */
  const urlMode = $derived<'live' | 'replay'>(
    (page.state.epicMode !== undefined
      ? page.state.epicMode
      : page.url.searchParams.get('mode')) === 'replay'
      ? 'replay'
      : 'live'
  );
  const urlT = $derived<number | null>(
    page.state.replayT !== undefined ? page.state.replayT : parseT(page.url.searchParams.get('t'))
  );
  const replaySpeed = $derived<ReplaySpeed>(
    page.state.replaySpeed !== undefined
      ? page.state.replaySpeed
      : parseSpeed(page.url.searchParams.get('speed'))
  );
  /**
   * True once the graph on screen is the one the mode asks for: in replay,
   * a frame out of the journal; in live, the live snapshot, unless a replay
   * link is still to be answered. The demo tours' targets (`data-tour`,
   * #3147) are drawn only then, so a step that opens a replay moment shows
   * on that moment and never on the live graph it replaces.
   */
  const drawnAsAsked = $derived(mode === 'replay' ? replayFrame !== null : urlMode !== 'replay');

  const currentEconomics = $derived(
    mode === 'live' ? live.economics : (replayFrame?.economics ?? null)
  );
  // Replay's ticker ACCUMULATES lines from frames 0..replayIndex (newest
  // first, capped like live) — the demo narrative needs a building log, and
  // seeking backward honestly shows only what the journal had recorded by
  // that frame.
  const currentTicker = $derived<TickerLine[]>(
    mode === 'live' ? live.ticker : accumulatedTicker(replayFrames, replayIndex)
  );
  const wallClockStr = $derived(
    mode === 'replay' && replayFrame && replayFrames[0]
      ? wallClock(replayFrame.tick_ts, replayFrames[0].tick_ts)
      : null
  );
  const scrubberPositions = $derived(scrubberStops(replayFrames));

  const selectedNode = $derived<EpicNode | null>(
    (currentSnapshot?.nodes ?? []).find((n) => n.id === selected) ?? null
  );

  /**
   * The offline retry (C17). Armed only while the server has never answered, so
   * a healthy page pays nothing for it; it disarms itself the moment a reading
   * lands, and the SSE channel below — which is gated on the same
   * `connected` — takes over. Skipped while the tab is hidden and while a read
   * is already out, like every other poll in the factory (C8).
   */
  $effect(() => {
    if (connected) return;
    const epicRef = ref;
    let cancelled = false;
    let out = false;
    const timer = setInterval(() => {
      if (document.hidden || out) return;
      out = true;
      fetchEpic(epicRef)
        .then((d) => {
          if (!cancelled) reconnected = { ref: epicRef, detail: d };
        })
        .catch((e) => {
          // The session, not the server: every read on this page would fail the
          // same way, so there is nothing here to retry into.
          if (isUnauthorized(e)) window.location.href = '/inbox';
        })
        .finally(() => {
          out = false;
        });
    }, PULSE_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  });

  // Live channel: one EventSource + one 1s clock tick, both torn down
  // together on unmount or on switching to replay (mirrors loops/+page.svelte's
  // interval-lifecycle idiom). Only armed when connected and in live mode —
  // entering replay closes this EventSource (spec: replay is a full re-fetch,
  // not an SSE tail).
  //
  // Closed while the tab is hidden and opened again when it is shown (#2789,
  // K7): an open stream is a server worker and a heartbeat a second for a
  // reader who cannot see the canvas. The reopen is a fresh subscribe, so the
  // first frames it carries are the current state, not a backlog.
  $effect(() => {
    if (!connected || mode !== 'live' || !pageVisibility.visible) return;
    streamLive = true;
    const es = streamEpic(ref, (frame) => {
      // A frame IS the channel: proof it carries data, whatever a proxy that
      // swallowed the `open` event did.
      streamLive = true;
      live = applyFrame(live, frame);
    });
    es.onopen = () => {
      streamLive = true;
    };
    const timer = setInterval(() => {
      now = Date.now();
    }, 1000);

    // EventSource can't expose HTTP status codes, so a 401 (expired session)
    // and a network blip look identical from onerror alone. On any stream
    // error, fire a lightweight authenticated probe to tell them apart: if
    // the probe itself comes back unauthorized, no amount of EventSource
    // auto-retry will fix that — send the user to /inbox to pick up a new
    // session. Any other outcome just lets EventSource's built-in auto-retry
    // keep going as today. Debounced to at most one probe per 5s so a
    // flapping connection doesn't spam requests.
    let lastProbe = 0;
    es.onerror = () => {
      // Before the probe debounce: the canvas must stop on the FIRST error,
      // not on whichever one happens to win the 5s window.
      streamLive = false;
      const t = Date.now();
      if (t - lastProbe < 5000) return;
      lastProbe = t;
      fetchEpic(ref).catch((e) => {
        if (isUnauthorized(e)) window.location.href = '/inbox';
      });
    };

    return () => {
      streamLive = false;
      es.close();
      clearInterval(timer);
    };
  });

  /** Whether this epic has finished — the one terminal signal the driver
   * writes (`TERMINAL_SIGNALS` in `$lib/epic/roster`). */
  const epicComplete = $derived(currentSnapshot?.signal === 'complete');

  /**
   * The timeline's read, armed only while the Timeline view is open.
   *
   * Re-read every `NOW_POLL_MS` so the now line, the open bands and the bars
   * of runs still in flight move; a COMPLETE epic has nothing open left to
   * move, so it is read once and left alone. The epoch guard is the tick
   * fetches' (`replayGen`): a response that lands after the reader has
   * switched epics, or switched back to the graph, is dropped rather than
   * drawn under the wrong ref.
   */
  $effect(() => {
    // Live only: the timeline is a reading of now, and in replay it would draw
    // runs, merges and a now line the replayed tick had not seen (review r4).
    if (!connected || view !== 'timeline' || mode !== 'live') return;
    const epicRef = ref;
    const settled = epicComplete;
    void timelineAttempt;
    let cancelled = false;
    const read = async (): Promise<void> => {
      try {
        const next = await fetchEpicTimeline(epicRef);
        if (cancelled) return;
        timeline = next;
        timelineError = false;
      } catch (e) {
        // An expired session goes to sign-in like every other read here, not
        // to a retry button that can never succeed (review r5).
        if (isUnauthorized(e)) {
          window.location.href = '/inbox';
          return;
        }
        if (!cancelled) timelineError = true;
      }
    };
    void read();
    // A hidden tab reads nothing (C8): the now line is only worth moving for
    // somebody who can see it, and the next visible tick redraws it.
    const tick = (): void => {
      if (!document.hidden) void read();
    };
    const timer = settled ? null : setInterval(tick, NOW_POLL_MS);
    return () => {
      cancelled = true;
      if (timer !== null) clearInterval(timer);
    };
  });

  /**
   * The freshness poll (#2303).
   *
   * Live only, and only on the graph: in replay the badge would claim a head
   * that moved AFTER the tick being replayed, which is the same mistake the
   * timeline's own live-only gate exists to stop. One request fills every card,
   * re-read on `NOW_POLL_MS` because a head moves between ticks (a push, a
   * remediation) and a base moves without this host doing anything at all.
   *
   * The factory's three poll guards (C8): a hidden tab reads nothing, a tick on
   * top of a read still out is skipped rather than queued, and only the newest
   * reading writes the state.
   */
  $effect(() => {
    if (!connected || view !== 'graph' || mode !== 'live') return;
    // Captured, not read through the closure: `ref` is only reached inside an
    // async callback that a hidden tab never runs, so the effect would not
    // depend on it and a ref -> ref navigation would leave this poll aimed at
    // the epic the reader has left (the timeline poll captures its ref for the
    // same reason). The epoch below is the second half of that: the reset
    // effect bumps it too, so a read already in flight is orphaned either way.
    const epicRef = ref;
    const generation = ++freshnessGen;
    let out = false;
    const read = async (): Promise<void> => {
      if (document.hidden || out) return;
      out = true;
      try {
        const next = await readFreshness((cursor) =>
          fetchPrFreshness(FRESHNESS_PAGE, cursor, epicRef)
        );
        if (generation === freshnessGen) freshness = next;
      } catch (e) {
        // The session, not the server: every read on this page fails the same
        // way, so there is nothing here to retry into.
        if (isUnauthorized(e)) {
          window.location.href = '/inbox';
          return;
        }
        // Any other failure leaves the rows already read standing — an unread
        // refresh is not evidence that nothing moved — but it SAYS it failed.
        // Swallowing it left `freshness` null, and a null reading draws no
        // badge and no notice: a persistently failing endpoint rendered as an
        // epic where nothing is stale (reviewer finding, #2303).
        if (generation === freshnessGen) freshness = failedReading(freshness, e);
      } finally {
        out = false;
      }
    };
    void read();
    const timer = setInterval(() => void read(), NOW_POLL_MS);
    return () => clearInterval(timer);
  });

  /** Rows keyed by node id — the map `DagView` joins onto its cards. An
   * unread epic joins onto an EMPTY map, which draws no badge at all. */
  const NO_FRESHNESS: Map<string, PrFreshnessRow> = new Map();
  const freshnessByNode = $derived(freshness?.rows ?? NO_FRESHNESS);

  /**
   * The line that says a missing badge proves nothing — null in the ordinary
   * case. A card with no badge reads as "nothing moved here", which is only
   * true of a row that was actually read, so a failed sub-read, a page this
   * client never reached, or the read itself throwing (`failedReading`) is
   * confessed on the strip beside the other two
   * (`tickerError`, `attnNotice`) rather than left to look like a clean epic.
   */
  const freshNotice = $derived(freshnessNotice(freshness));

  // Replay playback: a setTimeout chain, one hop per frame, spaced by
  // dwellMs(frameCount). Re-armed every time replayIndex/replayPlaying/mode
  // changes and torn down via the effect's own cleanup — so pausing,
  // scrubbing, or switching back to live can never leave a stray timer
  // running in the background.
  $effect(() => {
    if (mode !== 'replay' || !replayPlaying) return;
    if (replayFrames.length === 0) return;
    if (replayIndex >= replayFrames.length - 1) {
      // End of playback: flip to paused so the control reads "▶ play"
      // (and togglePlay() restarts from frame 0 on the next press), and the
      // link now names the moment playback stopped on.
      replayPlaying = false;
      untrack(() => writeUrl({}));
      return;
    }
    const timer = setTimeout(() => {
      replayIndex += 1;
    }, playbackDwellMs(replayFrames.length, replaySpeed));
    return () => clearTimeout(timer);
  });

  function clearNodeReads(): void {
    nodeDetail = undefined;
    nodeView = undefined;
    nodePipeline = undefined;
    nodeEvidence = undefined;
  }

  // The node's own facts as the snapshot on screen has them — the fingerprint
  // the reads below are rekeyed on. A `$derived` so it compares by value: the
  // snapshot object is new on every SSE frame, but this string only moves when
  // the node reaches a state one of those reads could contradict. Notably NOT
  // when the server merely rewords the node's attention prose, which it does on
  // a wall clock (see `nodeMoment`) and which would refire this effect forever.
  const selectionMoment = $derived(nodeMoment(selectedNode));

  // The selected node's four reads, taken in parallel and landed
  // independently: the rail degrades per column, so one endpoint being down
  // must not hold up (or blank) the three that answered. `cancelled` guards
  // against a stale response landing after the user selected something else.
  //
  // These are reads of NOW, while the Selected and Needs-you columns beside
  // them read the node off the snapshot on screen — two clocks, which the two
  // dependencies here keep together (rail.ts rule 4):
  //
  //   `selectionMoment` — a driver tick that moves the node retakes all four,
  //   so the rail can never hide its Needs-you column (the node merged) beside
  //   a pipeline read from before the merge. Keyed on the fingerprint, not on
  //   the snapshot, so a tick that moved nothing the rail keys on costs
  //   nothing — and neither does the passage of time on its own.
  //
  //   `mode` — replay takes no reads at all. There is no "pipeline as of tick
  //   41" endpoint, so a read of now has no business beside a frame from the
  //   journal's past; the rail is told `replay` and dashes those columns.
  $effect(() => {
    void selectionMoment;
    const id = selected;
    if (!id || mode === 'replay') {
      clearNodeReads();
      return;
    }
    let cancelled = false;
    clearNodeReads();

    // One read: the payload on success, and on failure either the "no such
    // record" sentence (404) or the failure named with its status. A 401 is
    // neither — the session expired, and every other read will fail the same
    // way, so the page leaves for a new one.
    function take<T>(
      promise: Promise<T>,
      subject: string,
      absent: string,
      set: (source: Source<T>) => void
    ): void {
      promise
        .then((data) => {
          if (!cancelled) set(data);
        })
        .catch((e) => {
          if (isUnauthorized(e)) {
            window.location.href = '/inbox';
            return;
          }
          if (!cancelled) set(railSource<T>(subject, absent, e));
        });
    }

    take(fetchEpicNode(ref, id), 'node spend', 'no runs recorded for this node', (s) => {
      nodeDetail = s;
    });
    take(fetchWorkView(id), 'route', 'no work item recorded for this node', (s) => {
      nodeView = s;
    });
    take(fetchPipeline(id), 'pipeline', PIPELINE_ABSENT, (s) => {
      nodePipeline = s;
    });
    take(fetchEvidence(id), 'evidence', EVIDENCE_ABSENT, (s) => {
      nodeEvidence = s;
    });

    return () => {
      cancelled = true;
    };
  });

  /** One write's changes; an absent key keeps what the URL holds. */
  type UrlWrite = {
    node?: string | null;
    view?: CanvasView;
    select?: string | null;
    speed?: ReplaySpeed;
  };

  /**
   * The moment `?t=` names for the frame on screen, or null in live. While the
   * frames are still loading it is whatever the URL already held, so a link's
   * `t` survives a click made before its replay has drawn.
   */
  function replayMoment(): number | null {
    if (mode !== 'replay') return null;
    if (replayFrames.length === 0) return urlT;
    return frameOffsets(replayFrames)[replayIndex] ?? 0;
  }

  /**
   * Write the selection, the canvas view and the lineage selection to the
   * URL, shallowly:
   * `replaceState` keeps `load()` from re-running and keeps the Back button
   * pointing at the page the reader arrived from rather than at every node
   * they walked past.
   *
   * Both readings go out on every write, because `replaceState` replaces the
   * page state rather than merging into it: a view switch that wrote only
   * `{ epicView }` would clear `page.state.node`, and since that is the
   * reading that wins, the selection the two views are meant to SHARE would
   * disappear on the way across.
   *
   * The default of each is written as the absence of its param: `?view=graph`
   * and `?node=` with nothing after it are not states, they are noise in a
   * link somebody is going to paste.
   */
  function writeUrl(next: UrlWrite): void {
    // Keep what the URL holds (`wanted`), not the snapshot-validated `selected`:
    // while no snapshot has loaded (replay's loading window, or a frame older
    // than the node) `selected` is null, and writing it erased ?node= and the
    // shared selection on a view switch (review r5).
    const id = next.node !== undefined ? next.node : wanted;
    const to = next.view !== undefined ? next.view : view;
    const url = addressBar();
    if (id == null) url.searchParams.delete('node');
    else url.searchParams.set('node', id);
    if (to === 'graph') url.searchParams.delete('view');
    else url.searchParams.set('view', to);
    const sel = next.select !== undefined ? next.select : lineageText;
    if (sel == null || sel === '') url.searchParams.delete('select');
    else url.searchParams.set('select', sel);
    // The replay's three keys, from the page's own state: `mode` and the
    // frame on screen are the toggle's and the scrubber's, never a caller's.
    // A link's replay the page has not answered yet (the epic has no journal
    // yet, or the server has not answered) keeps the three as the link has
    // them, so a click made meanwhile does not spend it.
    const pending = mode === 'live' && urlMode === 'replay' && linkAnswered !== page.url;
    const replaying = mode === 'replay' || pending;
    const t = pending ? urlT : replayMoment();
    const speed = replaying ? (next.speed ?? replaySpeed) : 1;
    if (replaying) url.searchParams.set('mode', 'replay');
    else url.searchParams.delete('mode');
    if (t === null) url.searchParams.delete('t');
    else url.searchParams.set('t', String(t));
    writtenT = t;
    if (speed === 1) url.searchParams.delete('speed');
    else url.searchParams.set('speed', String(speed));
    // Every state key this page does not own (a demo tour's `tour` and
    // `tourStep`, #3145) rides along: `replaceState` replaces the state.
    replaceState(url, {
      ...page.state,
      node: id,
      epicView: to,
      select: sel || null,
      epicMode: replaying ? 'replay' : 'live',
      replayT: t,
      replaySpeed: speed
    });
    // The address now names this selection: a navigation back onto it is not
    // a link to a new node (`linkOpensPopover`).
    addressed = { path: url.pathname, node: id ?? null };
  }

  /**
   * The address this page's next write starts from: the address bar as it
   * stands, so a query key the page does not own (a demo tour's `?tour=` and
   * `?step=`) is kept as the writer that owns it last left it, and one it
   * took out is not written back from `page.url`, which shallow writes leave
   * behind. Every key the page does own is set or deleted by `writeUrl`.
   * `page.url` is the fallback for an address bar on another path (the
   * moment a navigation away is under way).
   */
  function addressBar(): URL {
    const here = new URL(window.location.href);
    return here.pathname === page.url.pathname ? here : new URL(page.url);
  }

  /** The selection box's write: canonical text, or null to clear it. */
  function onlineage(text: string | null): void {
    writeUrl({ select: text });
  }

  function onselect(id: string | null): void {
    // Whatever they chose, including nothing: an Esc that clears the
    // selection must not hand it straight back as the opening default.
    picked = true;
    writeUrl({ node: id });
  }

  /** The Graph / Timeline switch. The selection rides along untouched, so a
   * node picked on one canvas is still picked on the other. */
  function onview(to: CanvasView): void {
    writeUrl({ view: to });
  }

  function onpopover(open: boolean): void {
    popover = open;
  }

  /**
   * A navigation that lands on a `?node=` link to a node the address did not
   * name before, or from another page, opens that node's popover, as a first
   * load of one does (`DagView`'s first frame). DagView opens it only on the
   * canvas's first frame, so a link followed on this page, with the canvas
   * already framed, selected the node and left its popover shut (a demo
   * tour's deep link to a node step, #3149). The rule is `linkOpensPopover`;
   * a replay link keeps DagView's own. A selection made on the page is a
   * `replaceState`, not a navigation, so a click or an arrow walk is not
   * affected: `replaceState` never moves `page.url`, and every navigation
   * (a link, a `goto`, Back and Forward) does. Plain let, for `replayGen`'s
   * reason.
   */
  let nodeLinkAnswered: URL | null = null;
  /**
   * The address as it last stood, by navigation or by this page's own
   * `writeUrl`: `?node=` names the selection, which a close keeps, so a
   * navigation to the node already selected leaves a shut popover shut
   * (`linkOpensPopover`). Plain let, for `replayGen`'s reason. This effect is
   * declared before `answerLink`'s, so it reads the address from before the
   * navigation, not one a replay link's answer writes after it.
   */
  let addressed: NodeAddress | null = null;
  $effect(() => {
    const url = page.url;
    untrack(() => {
      if (url === nodeLinkAnswered) return;
      nodeLinkAnswered = url;
      const before = addressed;
      addressed = { path: url.pathname, node: url.searchParams.get('node') };
      if (!linkOpensPopover(before, url)) return;
      if (onScreen(url.searchParams.get('node')) === null) return;
      picked = true;
      popover = true;
    });
  });

  /** Esc anywhere on the page, including over the rail: the popover closes
   * first and the selection survives it, then the selection goes. `T` is here
   * too, because the switch it throws is the page's state and not either
   * canvas's. Both reach it through the console's key layer (#2512), which
   * leaves a press in a field to the field — all but Esc, which `EPIC_KEYS`
   * asks for from a field too, so it still closes from the replay scrubber. */
  function onPageKey(e: KeyboardEvent): void {
    // Spelled out rather than spread: a `KeyboardEvent`'s fields live on its
    // prototype, so `{ ...e }` copies none of them.
    const press = {
      key: e.key,
      metaKey: e.metaKey,
      ctrlKey: e.ctrlKey,
      altKey: e.altKey,
      typing: isTyping(e.target)
    };
    if (togglesView(press)) {
      e.preventDefault();
      onview(otherView(view));
      return;
    }
    if (e.key !== 'Escape' || selected === null) return;
    const next: Focus = escapeFocus({ selected, popover });
    popover = next.popover;
    if (next.selected !== selected) onselect(next.selected);
  }

  $effect(() => registerShortcuts(bindShortcuts('epic', EPIC_KEYS, onPageKey)));

  // Full-journal fetch, paginating on `after` until a short page (bounded by
  // MAX_TICK_PAGES). Shared by replay and the live ticker seed below. Takes
  // the epic ref as a CAPTURED argument — re-reading the reactive `ref`
  // between pages would mix two epics' pages into one result when the user
  // navigates mid-pagination.
  async function fetchAllTicks(epicRef: string): Promise<EpicTick[]> {
    const all: EpicTick[] = [];
    let after = 0;
    for (let pageNum = 0; pageNum < MAX_TICK_PAGES; pageNum++) {
      const page = await fetchEpicTicks(epicRef, after);
      all.push(...page);
      if (page.length < TICKS_PAGE_SIZE) break;
      after = page[page.length - 1].id;
    }
    return all;
  }

  /** The epic this seed has already been paid for. Plain let — nothing renders
   * it, and making it reactive would re-run the effect that writes it. */
  let tickerSeededFor: string | null = null;

  // Seed the live ticker from the journal tail WHEN THE TICKER IS OPENED: a
  // completed (or between-ticks) epic otherwise shows "no events yet" forever,
  // which is dishonest emptiness — the journal HAS events.
  //
  // It used to run on first paint, and the seed is a full journal drain —
  // `fetchAllTicks` pages on `after` up to MAX_TICK_PAGES, so every open of
  // every Epic page spent up to 40 requests filling a disclosure that starts
  // COLLAPSED and that most readers never expand (C10). The ticker's own lines
  // are the only thing the seed feeds — the count beside the control reads them,
  // so it is rendered only while they are on screen (see the disclosure button)
  // rather than claiming 0 for a journal nobody has asked for yet. So this is
  // the ticker's own cost, paid once per epic on the first expand.
  //
  // The seed MERGES under whatever SSE has already shown (`seededTicker` drops
  // the overlap by line identity) instead of standing down whenever the ticker
  // is non-empty: a live epic that ticked once between paint and expand used to
  // cost the reader the entire journal tail, permanently, because the seed is
  // one-shot and nothing came back for it.
  //
  // Display-only and one-shot: it never touches lastTickId. The id watermark
  // therefore says nothing about the seeded lines — the SSE hub polls the
  // journal on its own 1s clock while this drain reads the store directly, so a
  // tick committed in between is seeded here and then arrives as a frame whose
  // id is genuinely new. What keeps it from printing twice is applyFrame's
  // line-identity check against the ticker on screen (`unshown` in
  // $lib/epic/live), not the id gate.
  //
  // Tracked deps are ref/connected/tickerOpen only (the .then reads mode/live
  // untracked), so it runs at most once per epic — `tickerSeededFor` keeps a
  // close-and-reopen from draining the journal again.
  $effect(() => {
    const epicRef = ref;
    if (!connected || !tickerOpen || tickerSeededFor === epicRef) return;
    tickerSeededFor = epicRef;
    let cancelled = false;
    fetchAllTicks(epicRef)
      .then((all) => {
        if (cancelled) return;
        tickerError = false;
        if (mode !== 'live' || all.length === 0) return;
        live = { ...live, ticker: seededTicker(live.ticker, all) };
      })
      .catch((e) => {
        // An expired session goes to sign-in like every other read on this
        // page; anything else is said out loud (D22) rather than swallowed —
        // live SSE may still populate on the next tick, but until it does
        // the page must not imply the journal is empty. The one-shot latch is
        // also released, so reopening the ticker after the server comes back
        // retries the seed.
        if (isUnauthorized(e)) {
          window.location.href = '/inbox';
          return;
        }
        if (tickerSeededFor === epicRef) tickerSeededFor = null;
        if (!cancelled) tickerError = true;
      });
    return () => {
      cancelled = true;
    };
  });

  /**
   * The full tick strip (#2304): every ATTEMPT this driver has made lately,
   * not only the ticks that changed something.
   *
   * The "tick N · change M" line above it is the heartbeat's own pair, and the
   * gap between those two numbers is the thing no surface could show: a driver
   * that ticked 476 times and journaled 18 reads as 18 events in the ticker
   * below, and the 458 that held, failed or were locked out reached nobody.
   *
   * Read once per epic and then on the heartbeat's tick-ATTEMPT stamp — a
   * strip can only change when the driver attempts a tick, and that stamp is
   * the cheapest signal that it did. NOT `tick_count`, which was the first
   * version of this and the wrong reading: it is written by `begin_epic_tick`,
   * past the single-driver lock, the tool preflight and the non-empty-graph
   * guard, so a driver that is locked out, failing preflight or finding an
   * empty graph moves it never — and moves none of the stream's other frame
   * hashes either, so the live channel went silent and this strip sat frozen
   * on its last reading through precisely the attempts it was added to show
   * (review round 3). `tick_attempt_stamp` moves whenever the driver opens or
   * closes an attempt row, which is the same fact the strip is drawn from, and
   * `_heartbeat_tuple` server-side emits a heartbeat frame when it does.
   *
   * The two readings it writes are declared with the other per-epic reads
   * above, so the per-ref reset clears them on a ref -> ref navigation.
   */
  // Named so the effect re-runs when the driver ATTEMPTS a tick, and not
  // otherwise. `tick_count` stays in the key behind it so a `kew serve`
  // predating the attempt stamp still redraws on the ticks it can see. A
  // derived string, not two reads inside the effect: every frame replaces
  // `live`, so reading through it re-ran the fetch on every heartbeat (#2789,
  // K7), while a derived only wakes its readers when the key itself moves.
  const tickStripKey = $derived(
    `${live.heartbeat?.tick_attempt_stamp ?? ''}|${live.heartbeat?.tick_count ?? ''}`
  );
  $effect(() => {
    const epicRef = ref;
    // A hidden tab reads nothing; the strip catches up when it is shown again,
    // because visibility is part of what this effect depends on.
    if (!connected || !pageVisibility.visible) return;
    void tickStripKey;
    let cancelled = false;
    fetchEpicTickStrip(epicRef, FULL_STRIP_LIMIT)
      .then((rows) => {
        if (cancelled) return;
        tickStripError = false;
        tickStrip = rows;
      })
      .catch((e) => {
        if (isUnauthorized(e)) {
          window.location.href = '/inbox';
          return;
        }
        if (!cancelled) tickStripError = true;
      });
    return () => {
      cancelled = true;
    };
  });

  // Entering replay closes the live EventSource (the effect above tears it
  // down as soon as `mode` flips) and fetches the ENTIRE journal — paginating
  // on `after` until a short page — so the frame-indexed playback below has
  // the full history to scrub, not just what SSE happened to deliver.
  async function enterReplay(): Promise<void> {
    if (!canReplay || replayLoading) return;
    // Capture this request's epoch + ref: the per-ref reset effect bumps
    // replayGen on navigation, so a fetch started under epic A silently
    // drops its result if it resolves after the user moved to epic B — the
    // reset already re-initialized replay state, and a stale assign here
    // would clobber the new epic's frames with the old epic's journal.
    const gen = replayGen;
    const epicRef = ref;
    // The moment the URL names (`?t=`), taken before the toggle rewrites it.
    // Null in live once anything has written: `writeUrl` writes `replayT: null`.
    const startT = urlT;
    mode = 'replay';
    replayError = false;
    replayLoading = true;
    replayIndex = 0;
    replayPlaying = true;
    try {
      const all = await fetchAllTicks(epicRef);
      if (gen !== replayGen) return;
      replayFrames = all;
      showMoment(startT);
      writeUrl({});
    } catch (e) {
      if (isUnauthorized(e)) {
        window.location.href = '/inbox';
        return;
      }
      if (gen !== replayGen) return;
      // Non-401 failure (network blip, server restart mid-fetch): stay in
      // replay mode but surface a retry affordance rather than showing
      // "loading replay frames…" forever — the user's intent (they asked to
      // see the replay) is preserved, they just get an honest failure state
      // instead of a silent hang.
      replayFrames = [];
      replayError = true;
    } finally {
      if (gen === replayGen) replayLoading = false;
    }
  }

  // Leaving replay abandons the journal fetch that entering it started: the
  // epoch bump makes an in-flight `fetchAllTicks` drop its pages (it would
  // otherwise land minutes later and refill `replayFrames` for a mode the
  // operator already left), and because that same bump stops enterReplay's
  // `finally` from clearing the flag, the exit clears it here — a stuck
  // `replayLoading` would refuse every later press of `replay`.
  function exitReplay(): void {
    replayGen += 1;
    mode = 'live';
    replayFrames = [];
    replayIndex = 0;
    replayLoading = false;
    replayError = false;
    writeUrl({});
  }

  /**
   * A link's moment over the frames in hand: paused on the frame `t` names,
   * so the same `t` always draws the same graph, or playing from frame 0 when
   * the link names none.
   */
  function showMoment(t: number | null): void {
    if (t === null) {
      replayIndex = 0;
      replayPlaying = true;
    } else {
      replayIndex = frameAtT(replayFrames, t);
      replayPlaying = false;
    }
  }

  /**
   * True when a replay is playing and the navigation carries back the moment
   * this page wrote, in its state: a writer that kept the page's state (a demo
   * tour taking its link out) moved nothing. A fresh link carries no such
   * state, so it pauses on its `t` even when that is the frame on screen.
   */
  function keepsPlaying(): boolean {
    if (!replayPlaying) return false;
    return page.state.replayT !== undefined && page.state.replayT === writtenT;
  }

  /**
   * The link a navigation arrived on, answered: `?mode=replay` enters the
   * replay at its `t` (or moves one already running there), and a link that
   * names no replay leaves it.
   */
  function answerLink(): void {
    if (urlMode !== 'replay') {
      if (mode === 'replay') exitReplay();
      return;
    }
    if (mode === 'replay' && replayFrames.length > 0) {
      // A navigation that carries the page's own moment back leaves a playing
      // replay playing (#3167): a demo tour taking its own link out of an
      // address keeps the state the page wrote, and pausing on it stops a
      // replay nobody asked to stop. Nothing changed, so nothing is written.
      if (keepsPlaying()) return;
      showMoment(urlT);
      writeUrl({});
      return;
    }
    // A journal still loading was asked for another moment: start again.
    if (replayLoading) {
      replayGen += 1;
      replayLoading = false;
    }
    void enterReplay();
  }

  /**
   * Every navigation's link is answered once, as soon as the epic can replay
   * at all; between navigations the toggle owns `mode` and the URL only
   * mirrors it. A navigation is a new `page.url` (shallow writes leave it
   * where it is), so a link to the epic already on screen — the same ref,
   * new `?mode=replay&t=` — is answered too, not only the one it opened on.
   */
  $effect(() => {
    const arrived = page.url;
    void ref;
    if (arrived === linkAnswered || !connected || !canReplay) return;
    linkAnswered = arrived;
    untrack(answerLink);
  });

  function togglePlay(): void {
    if (!replayPlaying && replayIndex >= replayFrames.length - 1) replayIndex = 0;
    replayPlaying = !replayPlaying;
    // Pausing names the moment it stopped on; playing leaves the last one.
    if (!replayPlaying) writeUrl({});
  }

  /**
   * The scrubber's `input`: the frame moves, the address bar does not. A
   * drag fires one `input` per frame it passes, and browsers refuse a flood
   * of history writes (Safari and Firefox throw after a few hundred in ten
   * seconds, Chrome drops them), so the link is written on `change` — the
   * release, and every keyboard step — naming the frame let go on.
   */
  function seek(index: number): void {
    replayIndex = Math.max(0, Math.min(replayFrames.length - 1, index));
  }

  function onspeed(speed: ReplaySpeed): void {
    writeUrl({ speed });
  }

  // Everything the bottom rail draws, from the selection and its four reads.
  // The node, the edges and the attention come off the snapshot on screen, so
  // scrubbing replay moves the rail's identity with the graph — and `replay`
  // tells the rail to dash the columns whose reads are of now, which a frame
  // from the journal's past cannot borrow.
  const railInput = $derived<RailInput>({
    epicRef: ref,
    node: selectedNode,
    replay: mode === 'replay',
    edges: currentSnapshot?.edges ?? [],
    unavailable: currentSnapshot?.unavailable ?? [],
    detail: nodeDetail,
    view: nodeView,
    pipeline: nodePipeline,
    evidence: nodeEvidence
  });

  // --- the header strip (DEpic y 56-171) ---

  // The strip's numbers, bar and legend, all from the snapshot's counts — and
  // so mode-aware for free: scrubbing replay moves the bar with the graph.
  const progress = $derived(epicProgress(currentSnapshot?.counts ?? {}));

  // The needs-you count, and the confession when the reading behind it failed.
  //
  // LIVE, it is the server's one set filtered by this epic (#2279/D6): the
  // header's counter, this legend and the Epics list all read
  // `needs_you_by_epic`, so the three cannot disagree about one English
  // phrase the way they did — 1 in the header over legends saying 1 and 2.
  //
  // In REPLAY it is the frame's own diamonds (`needsYouReading`), because the
  // set is always NOW and a scrubbed frame is a snapshot from the past: the
  // honest number over a frame is what that frame drew. The count is over
  // EVERY node in it, which holds because every node with attention is drawn
  // with the needs-you treatment — DagView's `keepAttentionInGraph` keeps a
  // waited-on node out of the plain done strip, so a number here always has a
  // plated row under it to point at.
  const attention = $derived(
    mode === 'live'
      ? epicNeedsYou(factoryPulse.dashboard, ref)
      : needsYouReading(currentSnapshot?.nodes ?? [], currentSnapshot?.unavailable ?? [])
  );
  // Two confessions, one line. The node join's own (the DAG's diamonds) comes
  // first; in live mode a count that is not a count because the SERVED set
  // confessed a failed source says so too, rather than printing a bare "?".
  const attnNotice = $derived(
    attentionNotice(currentSnapshot?.unavailable ?? []) ??
      (mode === 'live' && !attention.known ? needsYouSetNotice(factoryPulse.dashboard) : null)
  );
  // A stall is magenta when a person is next and amber otherwise — never the
  // red DESIGN.md keeps for failed (#2539, D24) — and grey with no halo unless
  // a fresh heartbeat backs the live claim (#2540, D32).
  const tone = $derived(
    heartbeatTone(
      currentSnapshot?.signal,
      mode === 'live' && st.kind === 'fresh',
      attention.count > 0
    )
  );
  /**
   * How many nodes the driver could dispatch now (the snapshot's ready
   * frontier). The graph rings them, and a ring with no legend entry is a new
   * state nobody can look up (DESIGN.md: "New states get a new shape, and a
   * legend entry" — D23). Counted over the nodes the snapshot actually
   * carries, so the legend cannot promise a mark the graph has not drawn.
   */
  const nextUp = $derived(
    (currentSnapshot?.frontier ?? []).filter((id) =>
      (currentSnapshot?.nodes ?? []).some((n) => n.id === id)
    ).length
  );
  /** The fold's own id for a legend entry: a status a newer server counts is
   * not promised to be one token, and an IDREF with a space in it is two. */
  const foldId = (status: string): string => `epic-fold-legend-${status.replace(/[^\w-]/g, '_')}`;
  /** What the phone's disclosure folds, by id, as far as it is drawn: an
   * IDREF to an element the page did not render dangles for a screen reader,
   * the way a fixed id with nothing behind it did. */
  const foldControls = $derived(
    [
      ...progress.legend.filter((entry) => entry.count > 0).map((entry) => foldId(entry.status)),
      ...(nextUp > 0 ? ['epic-fold-next-up'] : []),
      ...(connected && mode === 'live' && tickStrip.length > 0 && stripFits ? ['epic-fold-ticks'] : [])
    ].join(' ') || undefined
  );
  // Loud only for a count that IS somebody waiting: see the legend entry below.
  const waiting = $derived(attention.known && attention.count > 0);

  // What the served count holds that this graph cannot draw.
  //
  // The legend's number is the epic's BUCKET of the one set, and the set holds
  // waits that are about the epic rather than about one of its nodes — a
  // stalled driver has no node to put a diamond on. Sitting in a legend of
  // node statuses, that number could exceed the diamonds below it with nothing
  // on the page accounting for the difference, which is the same defect
  // `keepAttentionInGraph` fixed for the entries that ARE nodes: a count is
  // only honest when every unit of it has something to point at.
  //
  // Live only. Replay counts the frame's own diamonds, so there is nothing
  // left over by construction.
  const offGraph = $derived(
    mode === 'live'
      ? offGraphNeedsYou(factoryPulse.dashboard?.rows, ref, currentSnapshot?.nodes ?? [])
      : []
  );
  const offGraphText = $derived(offGraphNotice(offGraph));
  // The stall banner's link, off the epic's bucket of the served set — the
  // legend's own reading (#2538, #2784). The banner is drawn live only.
  const stalledWaits = $derived(stalledWaitsLink(epicNeedsYou(factoryPulse.dashboard, ref)));
  // The banner's word in the list's stall colour (#2783, D24): magenta when a
  // person is next, amber otherwise, never the red DESIGN.md keeps for failed.
  const stallColor = $derived(signalTone('stalled', attention.count > 0).color);

  // The repo, from the same reading the factory header prints it from — the
  // strip must not derive a second answer to "which repo is this". The group's
  // layout already drives `factoryPulse`, so this only reads it.
  const repo = $derived(headerCounts(factoryPulse.dashboard, factoryPulse.runs).repo);

  // "last tick HH:MM:SS" from the heartbeat, in the viewer's zone and through
  // the console's one formatter (#2205) so the header cannot disagree with the
  // ticker rows below it. It is the newest of the journal's stamp and the tick
  // log's — the Epics list's own `lastTickTs` — so a driver whose ticks
  // changed nothing still has a last tick here, as it does there (#2783, N18).
  // Absent from both is said in words, never a fabricated time and never a
  // bare dash; the null is checked here so the title is dropped with it rather
  // than left as a tooltip on a reading that was never taken.
  const lastTickTs = $derived(tickLogLastTs(live.heartbeat?.last_tick_ts ?? null, tickStrip));
  const lastTick = $derived(lastTickTs ? fmtTime(lastTickTs) : 'no tick recorded');
</script>

<!-- Esc is the page's, not the canvas's: the popover and the selection are
     both state up here, and Esc pressed with focus in the rail (or anywhere
     else on the page) means the same two steps it means over the graph. -->
<svelte:window bind:innerHeight={viewportH} />

{#key data.ref}
<!-- The column DESIGN.md draws (Grid): header strip, canvas that grows, bottom
     rail at a fixed height. The rail is why this column owns a scroller of its
     own (#2278 C4 review): it is a band at the foot, outside the canvas, so on
     a window too short for the strip and the rail together the canvas has
     nothing left to give and the two fixed bands are taller than the page is.
     The shell clips at `main`, so with no scroller here the bottom of the rail
     could not be reached at all. It engages only in that case — above it the
     canvas absorbs the difference and nothing scrolls. `overflow-x-hidden`
     leaves the x axis as the shell has it: the rail's fixed columns are
     clipped on a narrow window, never scrolled sideways.
     `relative` makes the column the containing block of every absolute box
     inside it with no positioned ancestor of its own (the strip's `sr-only`
     labels, say). Without it such a box is placed against the window at its
     spot far down this column, outside every clip, and the DOCUMENT grew to
     reach it: 1130px in an 844px window at 390, with the node sheet open, and
     the nav scrolled away (#3053 N2). -->
<div class="relative flex min-h-0 flex-1 flex-col overflow-x-hidden overflow-y-auto">
  <!-- The header strip (DEpic y 56-171, x 40-1400): an instrument panel, not a
       card. It states the epic's shape three times over — the fraction, the
       segmented bar, the legend — because each reading answers a different
       question at a different distance. Every number comes from
       `epic/progress.ts`; this markup only decides which span carries which.

       It renders whatever it knows even when there is no snapshot: the ref and
       the repo are facts either way, and a page that blanks its own title while
       the server is down tells the operator less than one that still says which
       epic they were looking at. What it will not do is fill the rest of the
       strip in anyway — every reading below that comes off the snapshot or the
       heartbeat (last tick, the signal, the bar, the staleness notice) is
       guarded, because an unreachable `kew serve` gave this page nothing to
       read and "last tick —" next to "no heartbeat yet" states otherwise. -->
  <section
    aria-label={`Epic ${ref}`}
    class="flex shrink-0 flex-col gap-4 border-b border-hair px-4 pt-5 pb-[18px] sm:px-10"
  >
    <!-- The demo tour's target (#3146): the heartbeat rows, from the title row
         to the tick strip, which draw at any window height (the strip itself
         folds away below 700px). A wrapper with the section's own column and
         gap, so the rows sit exactly where they did; it is inside the section
         because driver.js stops the scrolling of a target's PARENT, and the
         section's parent is the page's scroller. The rows inside keep their
         indentation, so the change is the wrapper alone. -->
    <div data-tour="epic-heartbeat" class="flex flex-col gap-4">
    <!-- `data-sep-row`: a row whose separators ride the item after them and
         are clipped where that item starts a line (`.sep-row`, below). -->
    <div data-sep-row class="sep-row flex flex-wrap items-baseline gap-4">
      <span class="text-11 text-text-3">Epic</span>
      <h1 class="text-26 text-text-1">{ref}</h1>
      {#if repo}
        <span class="font-mono text-13 text-text-3">{repo}</span>
      {/if}

      <div class="grow"></div>

      <!-- Replay and the ticker. D draws neither, and the plan keeps both, so
           they are the quietest controls on the strip: text, no chrome. Their
           behaviour is unchanged — `enterReplay`/`exitReplay` are the same two
           functions the old segmented control called. -->
      {#if connected}
        <div class="flex items-center gap-4 self-center font-mono text-[11px]">
          {#if canReplay}
            <!-- One word, always pressable: it reads the mode the press would
                 leave for, never the fetch's progress. `enterReplay` flips the
                 mode before it starts paginating, so a "loading…" word here
                 could only ever be dead text — and disabling the control for
                 the whole pagination would trap the operator in replay for as
                 long as the journal takes to drain. The load says so where it
                 is happening: the graph reads "loading replay frames…". -->
            <button
              type="button"
              class="text-text-3 underline-offset-4 hover:text-text-1 hover:underline"
              onclick={mode === 'replay' ? exitReplay : enterReplay}
            >
              {mode === 'replay' ? 'live' : 'replay'}
            </button>
          {:else if urlMode === 'replay'}
            <!-- A replay link to an epic whose journal has recorded no change
                 yet (#3142): said where the control would be, rather than a
                 live graph under a link that asked for something else. The
                 link stays in the address bar (`writeUrl`'s `pending`), and
                 the replay opens on it once the journal has a change. -->
            <span class="font-sans text-13 text-text-3">This epic has no journal to replay yet.</span>
          {/if}
          <button
            type="button"
            class="flex items-center gap-1.5 text-text-3 hover:text-text-1"
            aria-expanded={tickerOpen}
            aria-controls={tickerOpen ? 'epic-ticker' : undefined}
            onclick={() => (tickerOpen = !tickerOpen)}
          >
            <span aria-hidden="true" class="inline-block w-2">{tickerOpen ? '▾' : '▸'}</span>
            ticker
            <!-- The count only while the lines it counts are on screen. The
                 journal drain that fills the ticker is paid on the first expand
                 (C10), so a collapsed ticker has read nothing: a number here
                 said 0 for a completed epic whose journal is full of events,
                 which is the dishonest emptiness the seed exists to fix, moved
                 into the one place that is always visible. A shut disclosure
                 makes no claim about what is inside it. -->
            {#if tickerOpen}<span class="text-text-2">{currentTicker.length}</span>{/if}
          </button>
        </div>
        <!-- Inside the guard with the controls: "last tick" is a reading of
             the heartbeat, and an unreachable `kew serve` handed us no
             heartbeat to read. "no tick recorded" is for a driver with no tick
             in the journal or the log, which is a different fact from nobody
             having asked.
             The separator is an empty box with no space after it, so the
             item's text is "last tick <time>" from its first character: a
             line break there is a space in the text, and the header then read
             " last tick …" (#3053 N17). -->
        <span
          data-last-tick={lastTickTs ?? ''}
          class="relative font-mono text-13 text-text-3"
          title={utcTitle(lastTickTs)}
          ><span data-sep aria-hidden="true" class="sep h-4"></span>last tick <span class="text-text-2">{lastTick}</span></span
        >
      {/if}

      {#if currentSnapshot}
        <!-- Signal as dot and word. The halo belongs to the progressing state
             alone: it is the only one that means "this is moving right now". -->
        <!-- `data-heartbeat` is the same word the Epics row carries (#2844 N18),
             so the two readings of one driver can be compared. -->
        <span
          class="relative flex items-center gap-2 self-center font-mono text-15"
          style:color={tone.color}
          data-heartbeat={heartbeatWord(currentSnapshot.signal, mode === 'live' && st.kind === 'fresh') ??
            'none'}
        >
          <span data-sep aria-hidden="true" class="sep h-4"></span>
          <span
            class="size-2 shrink-0 rounded-full"
            style:background={tone.color}
            style:box-shadow={tone.halo ? '0 0 0 4px var(--halo-cyan)' : undefined}
          ></span>
          {currentSnapshot.signal}
        </span>
      {/if}
    </div>

    {#if currentSnapshot}
      <div class="flex flex-wrap items-center gap-6" data-tour={drawnAsAsked ? 'epic-progress' : undefined}>
        <span class="text-26 leading-none text-text-1">
          {progress.done}<span class="font-normal text-text-3"> / {progress.total}</span>
        </span>
        <span class="-ml-4 font-mono text-13 text-text-2">done · {progress.pct}%</span>

        <!-- One cell per node while they all fit, one proportional segment per
             status once they do not. Segments share the 558px the 40 cells
             would have taken (12px over a 2px gap), so the switch does not
             move the legend. Either way the bar never outgrows its row: on a
             phone the cells shrink to share the width rather than run their
             last statuses off the screen (#2788 N2). -->
        <span
          role="img"
          aria-label={`Progress: ${progress.barLabel}`}
          class="flex h-2.5 max-w-full min-w-0 gap-0.5"
          class:w-[558px]={progress.bar.kind === 'segments'}
        >
          {#if progress.bar.kind === 'cells'}
            {#each progress.bar.cells as status, i (i)}
              <span class="h-2.5 w-3 min-w-0.5" style={cellStyle(status)}></span>
            {/each}
          {:else}
            {#each progress.bar.segments as seg (seg.status)}
              <!-- Grown, not `width: pct%` — percentages plus the 2px gaps
                   would overflow the row by the sum of the gaps. -->
              <span class="h-2.5 basis-0" style={cellStyle(seg.status)} style:flex-grow={seg.pct}
              ></span>
            {/each}
          {/if}
        </span>

        <div class="grow"></div>

        <!-- The legend names every status the payload counted with a count
             above zero (DESIGN.md: show only non-zero counts; a zero is noise
             in a legend). Order is the payload's own, so a status this UI has
             never heard of still lands here rather than vanishing. -->
        <!-- Each status word is its own element under `data-status-word`, the
             hook the 390 sweep reads (#2898). A word too long for the row (an
             unknown status a newer server counts) ends in an ellipsis, as
             StatusMark's does (#2848), and never pushes the page sideways:
             the legend and each entry are held to the row's width. -->
        <!-- Below 640 the legend and the tick strip fold behind one line
             (D16): together they were ~120px of a phone's header, and the
             canvas under them showed ~300px of graph. The needs-you count is
             not folded — it is the one reading a phone opens this page for —
             so it stays on the line beside the disclosure. -->
        <div data-legend data-sep-row class="sep-row flex max-w-full min-w-0 flex-wrap items-center gap-3">
          <button
            type="button"
            data-fold-toggle
            class={`flex items-center gap-1.5 font-mono text-[11px] text-text-3 hover:text-text-1 sm:hidden ${TAP_PILL}`}
            aria-expanded={foldOpen}
            aria-controls={foldControls}
            onclick={() => (foldOpen = !foldOpen)}
          >
            legend and ticks
            <span aria-hidden="true" class="inline-block w-2">{foldOpen ? '▾' : '▸'}</span>
          </button>
          {#each progress.legend.filter((entry) => entry.count > 0) as entry (entry.status)}
            {@const word = statusShape(entry.status).word}
            <span
              data-fold
              id={foldId(entry.status)}
              class={`flex max-w-full min-w-0 items-center gap-1.5 font-mono text-[11px] text-text-2 ${foldShut}`}
            >
              <StatusMark kind={entry.status} size={12} class="shrink-0" />
              <span data-status-word class="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap" title={word}
                >{word}</span
              >
              <span class="shrink-0 text-text-1">{entry.count}</span>
            </span>
          {/each}

          <!-- Next up: the driver's ready frontier, which the graph rings on
               every member. A count, so it is only here when it is a reading
               (DESIGN.md: show only non-zero counts). -->
          {#if nextUp > 0}
            <span
              data-fold
              data-next-up
              id="epic-fold-next-up"
              class={`relative flex items-center gap-1.5 font-mono text-[11px] text-text-2 ${foldShut}`}
              title="the driver can dispatch these now"
            >
              <span data-sep aria-hidden="true" class="sep h-3.5"></span>
              <ReadyRing size={12} />
              next up
              <span class="text-text-1">{nextUp}</span>
            </span>
          {/if}

          <!-- Needs you, after a divider: not a status but a join over them
               (FUI-10), which is why it is counted separately. Only drawn
               when somebody really is waiting (magenta), or when the join
               could not compose a count ("?" in warn, because "we could not
               tell" is a different claim from "a person is needed") — a zero
               is not drawn at all (DESIGN.md: show only non-zero counts). -->
          {#if waiting || !attention.known}
            <span
              class="relative flex items-center gap-1.5 font-mono text-[11px]"
              style:color={waiting ? 'var(--accent)' : 'var(--text-3)'}
              title={attnNotice ?? undefined}
            >
              <span data-sep aria-hidden="true" class="sep h-3.5"></span>
              <StatusMark kind="needs_you" size={12} />
              needs you
              <span
                data-epic-needs-you
                class:text-text-1={waiting}
                style:color={attention.known ? undefined : 'var(--text-3)'}>{attention.label}</span
              >
            </span>
          {/if}
        </div>
      </div>
    {/if}

    <!-- Replay controls when replaying, the staleness notice otherwise — and
         neither when the server never answered: `staleness(null, now)` is
         'unknown', which words itself "no heartbeat yet", a claim about the
         driver this page is in no position to make. The body below says the
         one true thing instead ("start kew serve to see this epic"). -->
    {#if connected}
      {#if mode === 'replay'}
        <!-- Replay UI: wall clock (mandatory — time jumps are DISPLAYED, not
             hidden), scrubber positioned by REAL tick_ts spacing so a
             multi-hour HELD plateau reads as a plateau, play/pause. -->
        <div class="flex flex-wrap items-center gap-4">
          <button
            type="button"
            class="font-mono text-[11px] text-text-3 hover:text-text-1 disabled:opacity-60"
            onclick={togglePlay}
            disabled={replayFrames.length === 0}
          >
            {replayPlaying ? '⏸ pause' : '▶ play'}
          </button>
          <!-- The frame's own instant goes in the title, not a label beside
               the reading: the clock left of the "·" renders in the viewer's
               zone like every other time on this page (#2205), so a hard-coded
               zone name here states a wrong instant to everyone outside it. -->
          <span class="font-mono text-13 text-text-2" title={utcTitle(replayFrame?.tick_ts)}
            >{wallClockStr ?? '—'}</span
          >
          <div class="min-w-[240px] flex-1">
            <input
              type="range"
              min="0"
              max={Math.max(0, replayFrames.length - 1)}
              value={replayIndex}
              aria-label="Replay frame"
              oninput={(e) => seek(Number((e.currentTarget as HTMLInputElement).value))}
              onchange={() => writeUrl({})}
              class="w-full"
            />
            <!-- Tick marks at REAL time spacing (scrubberStops), not evenly
                 by frame index — a multi-hour HELD plateau reads as a
                 plateau (long gap between marks), not evenly-spaced ticks. -->
            <div class="relative mt-0.5 h-1.5 w-full">
              {#each scrubberPositions as pos, i (i)}
                <span class="absolute top-0 h-1.5 w-px bg-hair-strong" style:left={`${pos * 100}%`}
                ></span>
              {/each}
            </div>
          </div>
          <span class="font-mono text-[11px] text-text-3">
            frame {replayFrames.length ? replayIndex + 1 : 0} / {replayFrames.length}
          </span>
          <!-- Playback speed (#3142), the canvas switch's chips at a button's
               13px: one tab stop per speed and none trapped, so Tab always
               leaves. -->
          <div class="flex items-center gap-1" role="group" aria-label="Replay speed">
            {#each REPLAY_SPEEDS as option (option)}
              <button
                type="button"
                class={`${SPEED_CHIP} ${TAP_PILL} ${replaySpeed === option ? CHIP_ON : CHIP_OFF}`}
                aria-pressed={replaySpeed === option}
                onclick={() => onspeed(option)}>{option}x</button
              >
            {/each}
          </div>
        </div>
      {:else}
        <!-- Staleness as a hairline notice (failure-modes table — the kind
             determines the notice, never a false "stale" claim on a complete
             epic). The filled panels are gone: a rule in the tone's colour
             carries it, so a warning still reads as one without becoming the
             loudest block on a screen whose job is the graph. -->
        {#if st.kind === 'fresh'}
          <span class="font-mono text-[11px] text-text-3">
            tick {live.heartbeat?.tick_count ?? 0} · change {live.heartbeat?.change_count ?? 0} · {Math.round(
              st.ageS
            )}s ago
          </span>
        {:else if st.kind === 'stale'}
          <div class="border-l pl-3 font-mono text-13 text-text-2" style:border-color="var(--warn)">
            <span style:color="var(--warn)" title={utcTitle(st.lastSeen)}
              >driver last seen {fmtTime(st.lastSeen)}</span
            >
            ({Math.round(st.ageS)}s ago) — the graph below is the last recorded state.
          </div>
        {:else if st.kind === 'complete'}
          <div
            class="border-l pl-3 font-mono text-13 text-text-2"
            style:border-color="var(--success)"
          >
            <span style:color="var(--success)">complete</span>
            · <span title={utcTitle(st.finishedAt)}
              >finished {st.finishedAt ? fmtTime(st.finishedAt, false) : '—'}</span
            >
          </div>
        {:else if st.kind === 'stalled'}
          <!-- One sentence and a link (#2784). The journal's per-node lines
               were a second reading of "who is waiting" (go-live printed six
               over a legend reading 0, #2538), and its fallback printed raw
               codes and commands lifted out of reason text in ~11px mono.
               Each wait is a row on Work, carrying the command the set
               computed for it, so the banner counts the epic's bucket — the
               legend's number — and sends the reader there. -->
          <div
            data-stall-banner
            class="border-l pl-3 font-mono text-13 text-text-2"
            style:border-color={stallColor}
          >
            <span style:color={stallColor}>stalled</span>
            — the driver stopped making progress.
            {#if stalledWaits}
              <a data-stalled-waits class="underline underline-offset-2" href={NEEDS_YOU_WORK_HREF}
                >{stalledWaits}</a
              >
            {/if}
          </div>
        {:else if st.kind === 'oneshot'}
          <span class="font-mono text-[11px] text-text-3">
            last driven {Math.round(st.ageS)}s ago (one-shot driver)
          </span>
        {:else}
          <span class="font-mono text-[11px] text-text-3">no heartbeat yet</span>
        {/if}
      {/if}
    {/if}

    <!-- The full tick strip (#2304), on the console's one shared time axis and
         batched where attempts overlap. It sits with the heartbeat because it
         is the same reading at more resolution: the line above counts the
         ticks, this says what each of them DID — including the ~89% the change
         journal drops and the failures that used to reach only stderr. -->
    {#if connected && mode === 'live' && tickStrip.length > 0 && stripFits}
      <!-- The measured box the strip is fitted to (#2473): `w-full` so it is
           this section's content width, never the strip's own. -->
      <div data-fold id="epic-fold-ticks" class={`w-full ${foldShut}`} bind:clientWidth={stripBox}>
        <TickTimeline
          ticks={stripShown}
          width={stripWidth}
          label={`${ref} driver ticks`}
          driverStatus={live.heartbeat?.driver_status ?? null}
        />
      </div>
    {:else if connected && mode === 'live' && tickStripError}
      <!-- An unread value is text-3 and a sentence; amber is money and held
           (#2787 N10). -->
      <div class="border-l border-hair-strong pl-3 font-mono text-13 text-text-3">
        <span>tick strip could not be read</span>
      </div>
    {/if}
    </div>

    <!-- The ticker's own read, when it failed. On the strip and not only
         inside the collapsed disclosure: the reading it contradicts ("last
         tick —", an empty ticker) is up here (D22). -->
    {#if connected && tickerError}
      <div class="border-l border-hair-strong pl-3 font-mono text-13 text-text-3">
        <span>tick history could not be read</span>
      </div>
    {/if}

    <!-- Silence on failure is not silence: a snapshot that could not compose the
         needs-you join has no diamonds on it, which reads exactly like an epic
         nobody is waiting on. Say which read failed instead. -->
    {#if attnNotice}
      <div class="border-l border-hair-strong pl-3 font-mono text-13 text-text-3">
        <span>{attnNotice}</span>
      </div>
    {/if}

    <!-- The freshness read's own confession, for the same reason one line up: a
         node with no staleness badge reads as "nothing moved", and that is a
         claim only a row that was READ can make (#2303). -->
    {#if connected && view === 'graph' && mode === 'live' && freshNotice}
      <div class="border-l border-hair-strong pl-3 font-mono text-13 text-text-3">
        <span>{freshNotice}</span>
      </div>
    {/if}

    <!-- The part of the count the graph cannot show: a wait that is about the
         epic itself (a stopped driver) has no node to carry a diamond, so
         saying nothing would leave the legend reading higher than the
         diamonds under it for no visible reason. Each entry is printed in the
         set's own words — magenta because these ARE people being waited on,
         which is the one thing the colour is rationed for. -->
    {#if offGraphText}
      <div class="border-l pl-3 font-mono text-13 text-text-2" style:border-color="var(--accent)">
        <span style:color="var(--accent)">needs you · {offGraphText}</span>
        {#each offGraph as row, i (`${row.item_id ?? ''}-${i}`)}
          <div class="text-[11px] text-text-3">{row.reason}</div>
        {/each}
      </div>
    {/if}

    {#if tickerOpen}
      <div
        id="epic-ticker"
        class="flex max-h-[26vh] flex-col gap-1 overflow-y-auto border-t border-hair-quiet pt-3"
      >
        {#each currentTicker as line, i (`${line.ts}-${i}`)}
          <div class="flex items-center gap-2 font-mono text-13">
            <span class="w-4 shrink-0 text-center text-text-3">{line.icon}</span>
            <span class="min-w-0 flex-1 truncate text-text-2">{line.text}</span>
            <span class="shrink-0 text-[11px] text-text-3" title={utcTitle(line.ts)}
              >{fmtTime(line.ts)}</span
            >
          </div>
        {:else}
          <div class="py-2 text-center font-mono text-[11px] text-text-3">
            {tickerError ? 'tick history could not be read' : 'no events yet.'}
          </div>
        {/each}
      </div>
    {/if}
  </section>

  {#if !connected}
    <p class="px-10 py-16 text-center text-13 text-text-3">start kew serve to see this epic.</p>
  {:else if !live.snapshot}
    <p class="px-10 py-16 text-center text-13 text-text-3">
      No journal yet — drive this epic to record it.
    </p>
  {:else}
    <!-- The graph takes the height the rail does not: the rail is a fixed
         band at the foot of the page, and the canvas runs edge to edge in
         what is left (the UXEpic artboard), panning and zooming inside it.

         Below `xl` it stops giving (#2333). That is where the rail folds — to
         two rows of sections at `lg`, to all four stacked below it — and a
         folded rail is up to three times the band the artboard draws. A pane
         that kept shrinking under one ended at zero: the epic page on a phone
         showed no graph at all. The floor is the canvas's own content height
         (`.dag`'s 360px, plus the view switch above it), so the difference
         goes to the page's scroller, which is what it is for, and the graph is
         still a graph when the reader scrolls back up to it. The floor follows
         the rail's own widest fold (`RAIL_TRACKS.fourUp`), because a rail that
         folds is a rail that grows. `data-canvas-pane` names the box for the
         browser check that reads its height (`scripts/ui-check.mjs`).

         The graph view floors the pane at its OWN content instead
         (`min-h-min`): the canvas's 360px floor, the "No dependencies" strip
         under it, or the edge-less card grid. A pane that clipped at the flex
         share cut the strip in half under the rail at 1440x900 (#2543, D29);
         now the strip keeps its row and the page scrolls the difference. The
         canvas floor does not give for it: at 300px "Fit all" no longer fit
         go-live's graph above the minimap's corner. -->
    <div
      data-canvas-pane
      data-tour={drawnAsAsked ? 'epic-graph' : undefined}
      class={`flex flex-1 flex-col overflow-hidden ${
        view === 'graph' ? 'min-h-min' : 'min-h-0 max-xl:min-h-[400px]'
      }`}
    >
      {#if currentSnapshot}
        <!-- Graph / Timeline, top-left of the canvas area (T throws it). Both
             views share `?node=` and the rail below, so a node picked on one
             is still picked on the other. -->
        <div
          class="flex shrink-0 items-center gap-1 px-10 pt-2.5 pb-1"
          role="group"
          aria-label="Canvas view"
        >
          {#each CANVAS_VIEWS as option (option)}
            <button
              type="button"
              class={`${CHIP} ${view === option ? CHIP_ON : CHIP_OFF}`}
              aria-pressed={view === option}
              onclick={() => onview(option)}>{CANVAS_VIEW_LABEL[option]}</button
            >
          {/each}
          <!-- The key that throws it, drawn as a cap the way the canvas's own
               legend draws its keys — and, like that legend, not below 1024,
               where there is no keyboard to hint at (#3053 N6). -->
          <span
            data-key-hint
            class="ml-1 hidden h-[18px] min-w-[18px] items-center justify-center rounded-[3px]
                   border border-b-2 border-hair-strong px-[5px] font-mono text-[11px] text-text-3 lg:inline-flex"
            title="Switch views">T</span
          >
          {#if view === 'timeline' && mode === 'live'}
            <span class="ml-3 hidden font-mono text-[11px] text-text-4 lg:inline"
              >wheel zoom · drag pan · F fit · ↑↓ rows</span
            >
          {/if}
          <!-- An edge-less epic (#2543) is a grid with no lineage to select.
               The field is a hint in a box ("+#N · #N+ · L on a node"), and a
               key hint is not drawn below 1024 (#3053 N6): at 390 it was cut
               at its edge. `contents` leaves the form a flex item of this row
               where it is drawn. -->
          {#if view === 'graph' && currentSnapshot.edges.length > 0}
            <div data-hint-field class="hidden lg:contents">
              <LineageBox
                text={lineageText}
                ids={currentSnapshot.nodes.map((n) => n.id)}
                onchange={onlineage}
              />
            </div>
          {/if}
        </div>
      {/if}

      {#if currentSnapshot && view === 'timeline'}
        {#if mode === 'replay'}
          <div class="flex flex-col items-center gap-2 px-3 py-10 text-center text-13 text-text-3">
            <span>The timeline shows the epic as it is now. Leave replay to see it, or switch to Graph to replay.</span>
          </div>
        {:else if timeline}
          <EpicTimelineView {timeline} {selected} {onselect} />
        {:else if timelineError}
          <div class="flex flex-col items-center gap-2 px-3 py-10 text-center text-13 text-text-3">
            <span>couldn't read this epic's timeline.</span>
            <Button variant="factory" onclick={() => (timelineAttempt += 1)}>retry</Button>
          </div>
        {:else}
          <div class="px-3 py-10 text-center text-13 text-text-3">reading the timeline…</div>
        {/if}
      {:else if currentSnapshot}
        <DagView
          snapshot={currentSnapshot}
          economics={currentEconomics}
          {selected}
          {popover}
          linked={picked}
          lineage={lineageText}
          {onlineage}
          history={nodeDetail}
          {onselect}
          {onpopover}
          streaming={connected && mode === 'live' && streamLive}
          replay={mode === 'replay'}
          freshness={freshnessByNode}
        />
      {:else if mode === 'replay' && replayError}
        <div class="flex flex-col items-center gap-2 px-3 py-10 text-center text-13 text-text-3">
          <span>couldn't load the journal.</span>
          <Button variant="factory" onclick={enterReplay}>retry</Button>
        </div>
      {:else}
        <div class="px-3 py-10 text-center text-13 text-text-3">loading replay frames…</div>
      {/if}
    </div>

    <!-- The selected-node rail (DEpic y 742-960). It replaces the node detail
         Card: the node's runs are reached through "Open #id in Work", which is
         the surface that owns them, rather than being listed twice. -->
    <SelectedRail input={railInput} />
  {/if}
</div>
{/key}

<style>
  /* A separator between two readings of a wrapping row belongs to the reading
     after it: drawn in the middle of the gap before that item, out of the
     flow, so it can never end a line and showing it moves nothing. Where its
     item starts a line the separator falls left of the row's own edge, and
     the row clips there — so no line starts with one either (#3053 N17: "▸
     ticker |" and "| ◆ needs you 10" at 390). The clip gives 3px of room
     left of the row for a focus ring and none to a separator, which sits half
     a gap (8px, or 6px in the legend) out. Above and right of the row it
     clips nothing anyone draws. */
  .sep-row {
    clip-path: inset(-48px -48px -48px -3px);
  }

  .sep {
    position: absolute;
    top: 50%;
    left: -8.5px;
    width: 1px;
    background: var(--hair-strong);
    transform: translateY(-50%);
    pointer-events: none;
  }

  [data-legend] .sep {
    left: -6.5px;
  }
</style>
