<script lang="ts">
  // FUI-18 — the Work screen: a 420px issue list and the detail area beside it
  // (DWork x 0–420, y 56–720 for the list).
  //
  // The page is a renderer. Every decision about what a row says lives in
  // `$lib/work/list`, which is where the two payloads are merged, the interim
  // state word is derived and an unreadable payload is turned into a named
  // notice. Nothing here re-derives any of it.
  //
  // Selection is URL state (`?item=`), so a row is linkable and survives a
  // reload — and each row is a real `<a href>` rather than a click handler, so
  // the browser's own affordances (middle-click, copy link, keyboard) work. The
  // link is shallow: `replaceState` in `select` keeps `load()` from re-running,
  // and the rows re-render from state already in hand.
  //
  // The detail side carries the FUI-19 item header and the FUI-20 pipeline,
  // both of them renderers over `$lib/work/header` and `$lib/work/pipeline`;
  // FUI-21 brings the needs-you band, description and evidence ledger.
  import { onDestroy, untrack } from 'svelte';
  import { browser } from '$app/environment';
  import { page } from '$app/state';
  import { replaceState } from '$app/navigation';
  import {
    fetchDescription,
    fetchEvidence,
    fetchPipeline,
    fetchReadiness,
    fetchWorkProjection,
    type Dashboard,
    type ItemDescription,
    type ItemEvidence,
    type ItemPipeline,
    type Readiness,
    type WorkProjection,
    type WorkView
  } from '$lib/api';
  import { dataRowClasses, TAP_INLINE } from '$lib/factory/classes';
  import { factoryPulse, PULSE_INTERVAL_MS } from '$lib/shell/pulse.svelte';
  import { StatusMark, Title } from '$lib/ui';
  import { dismissable } from '$lib/ui/dismissable';
  import Description from '$lib/work/Description.svelte';
  import DispatchLaunchpad from '$lib/work/DispatchLaunchpad.svelte';
  import NeedsYouBlock from '$lib/needs-you/NeedsYouBlock.svelte';
  import { isRunnable, pairClear, pairLedgers, watchPair } from '$lib/run-request';
  import EvidenceLedger from '$lib/work/EvidenceLedger.svelte';
  import FirstRunCard from '$lib/work/FirstRunCard.svelte';
  import { firstRunCard, isFirstRun } from '$lib/work/first-run';
  import ItemHeader from '$lib/work/ItemHeader.svelte';
  import OrphanAckControl from '$lib/work/OrphanAckControl.svelte';
  import { orphanAck, withoutAckedOrphans } from '$lib/work/orphan-ack';
  import QueuePicker from '$lib/work/QueuePicker.svelte';
  import Pipeline from '$lib/work/Pipeline.svelte';
  import MergeVerify from '$lib/work/MergeVerify.svelte';
  import { needsYouBlock } from '$lib/needs-you/block';
  import { readFailure } from '$lib/work/evidence';
  import { registerShortcuts } from '$lib/keys/registry';
  import {
    KEY_LEGEND,
    keyGlyph,
    needsListColumn,
    nextRow,
    workIntent,
    workShortcuts,
    type WorkIntent
  } from '$lib/work/keys';
  import { pipelineReadFailure } from '$lib/work/pipeline';
  import { needsYouSummary } from '$lib/work/queue';
  import { DescriptionReads, NewestReads, SingleRead } from '$lib/work/reads';
  import {
    filterWorkRows,
    listNotices,
    mergeWorkList,
    parseWorkFilter,
    searchWorkRows,
    selectedRow,
    stateWord,
    WORK_FILTERS,
    WORK_FILTER_LABEL,
    needsYouDiamond,
    rowDispatchIsNext,
    rowNamesItem,
    rowReason,
    type WorkFilter
  } from '$lib/work/list';
  import type { WorkPageData } from './+page';

  let { data }: { data: WorkPageData } = $props();

  // Seeded inside an effect rather than a `$state` initializer: reading the
  // prop in an initializer captures it once and never sees a fresh `load()`
  // (the same trap routes/+page.svelte documents).
  //
  // #2545 D9 — `load()` hands over the reading unawaited so the shell draws at
  // once; until it lands the list says "Reading work items…" rather than
  // naming an unread projection it has not finished reading. A re-run of
  // `load()` keeps the rows on screen while it reads again.
  let projection = $state<WorkProjection | null>(null);
  let seed = $state<Dashboard | null>(null);
  let reading = $state(true);
  /**
   * #2933 — the generation of each list read, the loader's and each pulse's: a
   * slow answer from pulse N that lands after pulse N+1's is older than the
   * list on screen, and is not drawn. Plain, as `lastPulse` is.
   */
  const listReads = new NewestReads();
  // #2933 — the loader's list read takes a generation from the same ledger as
  // the pulse's re-reads: an initial answer that lands after a pulse's has been
  // drawn is the older reading, and does not revert the list.
  $effect(() => {
    const pending = data.reading;
    const generation = listReads.begin();
    // A new load supersedes a pulse's read still out: its answer would be
    // dropped on arrival, so it is not left holding a request open (#3055).
    // The load's own read then holds the slot, so a pulse that fires while it
    // is still out skips rather than open a second list read beside it.
    rereads.abort();
    const loadOut = rereads.begin();
    let current = true;
    void data.listRead.then(() => {
      if (loadOut !== null) rereads.end(loadOut);
    });
    void pending.then((read) => {
      if (!current) return;
      if (listReads.take(generation)) projection = read.projection;
      seed = read.dashboard;
      reading = false;
    });
    return () => {
      current = false;
    };
  });

  // The shell polls `/api/dashboard` once for the whole group, so the live copy
  // is the pulse's; `seed` is only the loader's copy that keeps the first paint
  // from missing the ready queue.
  //
  // #3453 — less the orphan PRs acknowledged from this page: an acked orphan
  // leaves on the write's answer, as it does on the console, rather than
  // standing until the next read stops serving it.
  let ackedOrphans = $state<ReadonlySet<number>>(new Set());
  const dashboard = $derived<Dashboard | null>(
    withoutAckedOrphans(factoryPulse.dashboard ?? seed, ackedOrphans)
  );

  // `/api/dashboard/work` is not in the group's poll, and a list whose state
  // words froze at navigation time would quietly go stale. It is re-read on the
  // shell's tick rather than on a second timer of its own. `lastPulse` is a
  // plain `let` on purpose: an effect that wrote reactive state it also reads
  // would re-run itself.
  let lastPulse: Dashboard | null = null;
  $effect(() => {
    const tick = factoryPulse.dashboard;
    if (tick === null || tick === lastPulse) return;
    const firstTick = lastPulse === null;
    lastPulse = tick;
    // The first tick is the reading `load()` already took a moment ago.
    if (!firstTick) {
      void reread();
      // The pipeline and the evidence chain are the same kind of reading: a
      // review round that closed between two ticks must not sit on screen as
      // "reviewing", and a verdict that was attested must not stay pending. A
      // description already IN HAND is not re-read — it is a remote tracker read
      // behind a TTL cache, and an issue body somebody edits is not what this
      // clock watches — but a refusal FUI-31 never attempted is taken again, a
      // bounded number of times: it turns a read away while the same item's read
      // is out, while the process-wide bound is full, or while the minute's
      // budget is spent, and none of those is an answer about this item. Without
      // this the panel would hold that refusal, with no body and no link to
      // leave by, for as long as the row stays selected. A reading that reached
      // the tracker and FAILED is not re-read at all: it already spent one of
      // the reads a minute the whole process shares, and `rereadsDescription`
      // is where that line is drawn.
      if (detailItem !== null) {
        void readPipeline(detailItem);
        void readEvidence(detailItem);
        if (descriptionReads.claimReread(description)) void readDescription(detailItem);
      }
    }
  });

  // #3055 (C7-3): a pulse that finds a list read still out — its own last one
  // or the loader's — skips its own.
  const rereads = new SingleRead();
  onDestroy(() => rereads.abort());

  async function reread(): Promise<void> {
    const signal = rereads.begin();
    if (signal === null) return;
    const generation = listReads.begin();
    try {
      const next = await fetchWorkProjection(fetch, signal);
      if (!listReads.take(generation)) return;
      projection = next;
      // A pulse's reading that lands before the loader's is the list in hand.
      reading = false;
    } catch {
      // Keep the last good projection; a dropped poll is not a fact about work.
    } finally {
      rereads.end(signal);
    }
  }

  /**
   * FUI-19 — fold the server's post-write view back into the projection in
   * hand, so the header the reader is looking at transitions from the write
   * RESPONSE rather than from any belief of this page's about what the press
   * did. The next poll overwrites it with the same server's next reading.
   */
  function applyActedView(next: WorkView): void {
    const current = projection;
    if (current === null) return;
    const id = next.identity.item_id;
    if (id == null || id === '') return;
    projection = {
      ...current,
      views: current.views.map((v) => (v.identity.item_id === id ? next : v))
    };
  }

  const input = $derived({ projection, dashboard, runs: factoryPulse.runs });
  const rows = $derived(mergeWorkList(input));
  // The notices are decided against the rows the list is about to draw: one of
  // them is about judgment rows that have no row in `rows` at all.
  const notices = $derived(listNotices(input, rows));
  /**
   * The clock the "held 68d6h" / "waiting 1h00m" line is read against, ticking
   * at the shell's own poll rate — a wait that has stood for hours does not
   * need a second hand, and a duration frozen at the moment a row was selected
   * is the one reading on this pane that silently goes stale (`Holds.svelte`
   * keeps the same clock for the same reason).
   */
  let clock = $state(Date.now());
  $effect(() => {
    if (!browser) return;
    const timer = setInterval(() => {
      clock = Date.now();
    }, PULSE_INTERVAL_MS);
    return () => clearInterval(timer);
  });

  // What the detail pane shows before a row is chosen (#2210): the needs-you
  // queue, counted the way the header counts it. Over `rows`, so the pane and
  // the list beside it are two views of one merged reading.
  const queue = $derived(needsYouSummary(rows, dashboard, clock));
  /** The queue's headline, which is not "not read" while it is still being read. */
  const queueHeadline = $derived(
    reading && !queue.read ? 'Reading the needs-you queue…' : queue.headline
  );

  /**
   * #2515 — no work and no run history: the pane is the first-run card. The
   * readiness checks are read only then, and again on the shell's tick while
   * it stays up (the server caches them for a minute, so a re-read is one
   * dictionary copy). `undefined` is "not read yet", `null` a failed read.
   */
  const firstRun = $derived(
    isFirstRun({ rows, projection, dashboard, runs: factoryPulse.runs })
  );
  let readiness = $state<Readiness | null | undefined>(undefined);
  let readinessTick: Dashboard | null | undefined = undefined;
  $effect(() => {
    if (!firstRun) return;
    const tick = factoryPulse.dashboard;
    if (tick === readinessTick) return;
    readinessTick = tick;
    void readReadiness();
  });

  async function readReadiness(): Promise<void> {
    try {
      readiness = await fetchReadiness();
    } catch {
      // A dropped re-read keeps the last reading; only a first read that
      // failed says the checks could not be read.
      if (readiness === undefined) readiness = null;
    }
  }

  let query = $state('');
  let filterOpen = $state(false);

  // `replaceState` is shallow routing: it moves the address bar and
  // `page.state`, never `page.url`. Reading `page.url` left every click writing
  // `?item=` while the pane stayed on "Select an issue" (the trap #2248 and
  // #2250 fixed on their own pages). The query string this page last wrote is
  // kept in `page.state.workSearch`; every read and every next write starts
  // from it, and `page.url` is the reading only until the first write.
  const search = $derived(new URLSearchParams(page.state.workSearch ?? page.url.search));
  const filter = $derived<WorkFilter>(parseWorkFilter(search.get('filter')));
  const shown = $derived(searchWorkRows(filterWorkRows(rows, filter), query));
  const selected = $derived(selectedRow(rows, search.get('item')));

  /** The page's URL as this page last wrote it (see `search`). */
  function here(): URL {
    const url = new URL(page.url);
    url.search = page.state.workSearch ?? page.url.search;
    return url;
  }

  /** The one write: the address bar and `page.state` move together. */
  function write(url: URL): void {
    replaceState(url, { workSearch: url.search });
  }

  // FUI-20 — the selected item's pipeline. A per-item read, so it is taken here
  // rather than inside the section: the page already owns the polling clock, and
  // a component that fetched in an effect could not be rendered server-side,
  // which is how `pipeline.test.ts` checks its pixels.
  let pipeline = $state<ItemPipeline | null>(null);
  /** A read that FAILED, worded by `pipelineReadFailure`. '' otherwise. */
  let pipelineGap = $state('');
  // FUI-21 — the evidence chain and the description, the other two per-item
  // reads, taken here for the same reason and dropped with the same selection.
  let evidence = $state<ItemEvidence | null>(null);
  let evidenceGap = $state('');
  let description = $state<ItemDescription | null>(null);
  let descriptionGap = $state('');
  /** Which item the readings in hand belong to; a plain `let`, as `lastPulse` is. */
  let detailItem: string | null = null;
  /**
   * Whose description read is out and how many retries this selection has spent
   * (`$lib/work/reads`). Not keyed on the item id: a selection of A -> B -> A
   * has two reads of A in flight at once, and the earlier one must not hand the
   * in-flight flag back while the later one is still out. Plain and
   * non-reactive, as `detailItem` is — nothing renders from it.
   */
  const descriptionReads = new DescriptionReads();
  /**
   * #2933 — the generation of each pipeline and evidence read, so an answer
   * older than the one on screen — pulse N's, landing after pulse N+1's — is
   * not drawn over it. A failure is an answer too: it names a gap, and an old
   * one must not replace a newer reading either. Each selection is an epoch,
   * so a read taken for an earlier selection of the same item (A -> B -> A)
   * never draws into the pane cleared for this one.
   */
  const pipelineReads = new NewestReads();
  const evidenceReads = new NewestReads();

  $effect(() => {
    // #3453 — an entry row that names no item (an orphan PR, a stalled epic) is
    // selected by its entry's key, which no per-item endpoint knows: it holds
    // no readings, so nothing is read for it here or on the tick.
    const id = selected !== null && rowNamesItem(selected) ? selected.itemId : null;
    if (id === detailItem) return;
    // Another item's lanes, guarantees or issue body under this item's header
    // would be a lie about this item, so all three are dropped the moment the
    // selection moves — never left on screen to be overwritten when a read
    // happens to land.
    detailItem = id;
    pipeline = null;
    pipelineGap = '';
    evidence = null;
    evidenceGap = '';
    description = null;
    descriptionGap = '';
    // The retry budget is the new selection's, not what the last one spent —
    // and every read already out is superseded here, so an answer that lands
    // after this moment is not written to the screen.
    descriptionReads.select();
    pipelineReads.select();
    evidenceReads.select();
    if (id !== null) {
      void readPipeline(id);
      void readEvidence(id);
      void readDescription(id);
    }
  });

  /**
   * Read one item's pipeline. The outcome belongs to `itemId` and to nothing
   * else — the reader can select another row while this is in flight — and a
   * 404 is the answer rather than a failure: `api_work.py` 404s an item with no
   * runs, which every idle ready-queue issue is.
   */
  async function readPipeline(itemId: string): Promise<void> {
    const generation = pipelineReads.begin();
    const owns = () => detailItem === itemId && pipelineReads.take(generation);
    try {
      const next = await fetchPipeline(itemId);
      if (!owns()) return;
      pipeline = next;
      pipelineGap = '';
    } catch (err) {
      if (!owns()) return;
      // A 404 is the ANSWER — this item has no pipeline — so the section draws
      // nothing at all; any other failure keeps its head and names the gap.
      const failure = pipelineReadFailure(err);
      pipeline = null;
      pipelineGap = failure.absent ? '' : failure.notice;
    }
  }

  /**
   * Read one item's evidence chain. Unlike the pipeline there is no 404 that
   * means "absent": the guarantees exist before the work does, and an item with
   * no run answers with all four pending. So every failure is a gap.
   */
  async function readEvidence(itemId: string): Promise<void> {
    const generation = evidenceReads.begin();
    const owns = () => detailItem === itemId && evidenceReads.take(generation);
    try {
      const next = await fetchEvidence(itemId);
      if (!owns()) return;
      evidence = next;
      evidenceGap = '';
    } catch (err) {
      if (!owns()) return;
      evidence = null;
      evidenceGap = readFailure(err, 'the evidence chain');
    }
  }

  /**
   * Read one item's description. The endpoint never rejects for a tracker that
   * is down — it answers `unavailable` with a reason, which the panel renders as
   * its fallback — so a throw here is the console's own read failing. Either
   * way the panel is left with no body, which is what the tick re-reads.
   */
  async function readDescription(itemId: string): Promise<void> {
    // Ticketed before the first await: the endpoint refuses a second read of an
    // item whose read is already out, so a tick that fired one would spend a
    // retry on a refusal of its own making — and the ticket is what tells this
    // read apart from an earlier read of the SAME item, which a selection of
    // A -> B -> A has in flight beside it.
    const ticket = descriptionReads.begin();
    try {
      const next = await fetchDescription(itemId);
      if (!descriptionReads.owns(ticket)) return;
      description = next;
      descriptionGap = '';
    } catch (err) {
      if (!descriptionReads.owns(ticket)) return;
      description = null;
      descriptionGap = readFailure(err, 'the description');
    } finally {
      // Retired whatever it answered, and only if it is the read that is out: a
      // superseded one leaves the flag to the read that took its place.
      descriptionReads.end(ticket);
    }
  }

  /** The cursor rule (FUI-09): magenta when the item on screen needs a human. */
  $effect(() => {
    factoryPulse.viewedItemNeedsYou = selected?.needsYou ?? false;
    return () => {
      factoryPulse.viewedItemNeedsYou = false;
    };
  });

  /** The row's own href, so the link is real before any handler runs. */
  function itemHref(itemId: string): string {
    const url = here();
    url.searchParams.set('item', itemId);
    return `${url.pathname}${url.search}`;
  }

  /**
   * #2281 D2 — the list's own URL: this page's address with no `?item=`, which
   * below 1024 is the way back from a detail that fills the screen. Built from
   * `here()`, so the search and the filter the reader is on come back with it.
   */
  function backHref(): string {
    const url = here();
    url.searchParams.delete('item');
    return `${url.pathname}${url.search}`;
  }

  /** The back control's press: the same selection write every row makes. */
  function back(event: MouseEvent): void {
    // A modified click belongs to the browser, exactly as in `select`.
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0)
      return;
    event.preventDefault();
    clearSelection();
  }

  function select(event: MouseEvent, itemId: string): void {
    // Leave the modified clicks to the browser: a new tab must get a real load.
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0)
      return;
    event.preventDefault();
    const url = here();
    url.searchParams.set('item', itemId);
    write(url);
  }

  /**
   * `alsoClearItem` is the one-column case (#2281 D2): below 1024 a narrowed
   * list is behind the open item, so `f` has to bring the list back as well.
   * In ONE write — two `replaceState` calls in a tick would have the second
   * build its URL from `page.state` the first had not settled into yet, and
   * whichever landed second would drop the other's change.
   */
  function setFilter(next: WorkFilter, alsoClearItem = false): void {
    const url = here();
    if (alsoClearItem) url.searchParams.delete('item');
    if (next === 'all') url.searchParams.delete('filter');
    else url.searchParams.set('filter', next);
    write(url);
    filterOpen = false;
  }

  /**
   * #2280 — the ONE needs-you block, for the selected row. `null` is the
   * ordinary case: neither the needs-you set nor the projection says a person
   * is the next move on this item.
   */
  const block = $derived(needsYouBlock(selected, clock));

  /**
   * #2280 D11 — with exactly one thing waiting on a person, the first screen
   * expands its block under the heading rather than printing a list of one
   * over "Select an issue to see what it is waiting for."
   */
  const soleBlock = $derived(queue.sole === null ? null : needsYouBlock(queue.sole, clock));
  const selectedOrphan = $derived(orphanAck(selected, rows));
  const soleOrphan = $derived(orphanAck(queue.sole, rows));

  /** The orphan's row leaves with its acknowledge, and the pane goes back to the queue. */
  function orphanAcked(pr: number): void {
    // Read before the set changes: once it has, the row is gone from `selected`.
    const wasSelected = selected?.entry?.type === 'orphan_pr' && selected.entry.pr_number === pr;
    ackedOrphans = new Set([...ackedOrphans, pr]);
    if (wasSelected) clearSelection();
  }

  /** The first-run card, which takes the empty pane's place (#2515). */
  const firstRunModel = $derived(firstRun && !selected ? firstRunCard(readiness, dashboard) : null);

  /**
   * Whichever block is on screen owns the Copy button and `c`: the first-run
   * card's one next move, or a needs-you block's command.
   */
  const copyCommand = $derived(
    firstRunModel !== null ? firstRunModel.primary : ((selected ? block : soleBlock)?.command ?? '')
  );

  /**
   * `c` hands over the command a needs-you block's Run sends, so it asks the
   * block's gate (#2975): a runnable command copies only once its ledger
   * answered with nothing of it unsettled. The pair is watched here as well,
   * so the key's gate does not depend on which block is drawn.
   */
  const copyClear = $derived(
    !isRunnable(copyCommand) || pairClear($pairLedgers, copyCommand, null)
  );
  $effect(() => {
    const watched = copyCommand;
    if (!isRunnable(watched)) return;
    return untrack(() => watchPair(watched, null));
  });

  /** The command a copy just landed for, for a moment, so its button can say "Copied". */
  let copiedCommand = $state('');
  const copied = $derived(copiedCommand !== '' && copiedCommand === copyCommand);
  let copyFlash: ReturnType<typeof setTimeout> | null = null;

  /**
   * Copy the selected row's command. The mono block above the button holds the
   * same text, so a clipboard this page cannot reach — an insecure context, a
   * denied permission — leaves the command selectable rather than lost, and the
   * button simply does not flash.
   */
  async function copySelected(): Promise<void> {
    if (!copyClear) return;
    await copyCommandText(copyCommand);
  }

  /** Copy one command — the first-run card's buttons each carry their own. */
  async function copyCommandText(command: string): Promise<void> {
    if (command === '') return;
    try {
      await navigator.clipboard.writeText(command);
    } catch {
      // Clipboard unreachable. The `<code>` block is still the command.
      return;
    }
    copiedCommand = command;
    if (copyFlash !== null) clearTimeout(copyFlash);
    copyFlash = setTimeout(() => {
      copiedCommand = '';
      copyFlash = null;
    }, 1500);
  }

  /** The search box and the detail pane, for the two keys that move focus. */
  let searchBox = $state<HTMLInputElement | null>(null);
  let detailPane = $state<HTMLElement | null>(null);
  /** The list, which is the scroller a row has to be brought into view of. */
  let listPane = $state<HTMLElement | null>(null);
  /**
   * The viewport width, for the one keyboard decision that reads the
   * breakpoint: below 1024 the list is not in the column (`needsListColumn`).
   * `0` until the window measures it, which reads as the desktop the page is
   * designed at — the markup's own `lg:` default.
   */
  let viewportW = $state(0);
  /**
   * Set by `/` when the press had to bring the list back first, and spent by
   * the effect below.
   *
   * Not a `tick()`: the field is in flow only once the selection write has
   * RENDERED, and how many ticks that write takes to settle through
   * `replaceState` into `page.state` is SvelteKit's business, not this page's.
   * The effect waits for the thing that actually matters — the list being back
   * in the column — so the focus cannot land on a `display:none` input.
   */
  let searchWanted = $state(false);

  /** `/`'s own move, wherever the press came from. */
  function focusSearch(): void {
    searchBox?.focus();
    searchBox?.select();
  }

  $effect(() => {
    if (!searchWanted || selected !== null) return;
    searchWanted = false;
    focusSearch();
  });

  /**
   * #2281 C5 — bring the row the keyboard just selected into view.
   *
   * j and k moved the selection and nothing else, so holding either walked the
   * highlight straight out of the 420px column: the pane scrolls, and the
   * browser scrolls a scroller for a FOCUS move, which a `replaceState`
   * selection is not. `block: 'nearest'` is the whole behaviour — it scrolls by
   * the least that makes the row visible and does nothing at all while the row
   * already is, so the list walks instead of jumping, and a row in the middle
   * of the pane is left where it is.
   *
   * The row is found by the `data-item` attribute each anchor carries, inside
   * the list element rather than the document: the needs-you queue in the pane
   * beside it links to the same items, and a scroll must not be aimed at one of
   * those. A selection whose row the filter hid finds nothing and scrolls
   * nothing; below 1024 the row is found but the list it is in is
   * `display:none`, where `scrollIntoView` is a no-op — which is right, because
   * there is nothing on screen to bring it into view OF. The column there holds
   * the item j/k just moved to, named in its own header: one column, so moving
   * the selection and showing it are the same act, exactly as a tap is.
   */
  function revealRow(itemId: string): void {
    const row = listPane?.querySelector(`a[data-item="${CSS.escape(itemId)}"]`);
    row?.scrollIntoView({ block: 'nearest' });
  }

  /**
   * Select a row by id without flooding history: arrow movement is `replaceState`
   * for the same reason a click is — the back button must not have to unwind
   * every row the reader walked past.
   */
  function selectId(itemId: string): void {
    const url = here();
    url.searchParams.set('item', itemId);
    write(url);
  }

  function clearSelection(): void {
    const url = here();
    url.searchParams.delete('item');
    write(url);
  }

  /**
   * #2246 — the keyboard (P9 of the UX brief: no Work, Monitor or Epic page
   * handled an arrow key).
   *
   * `workIntent` owns WHICH press means what, and since #2512 the console's
   * key layer owns the two refusals that matter — a platform modifier belongs
   * to the browser, and a press while the reader is typing belongs to the
   * field (Escape in the search box is the field's own, `onSearchKey`). This
   * function only carries out the intent, and `preventDefault` is called only
   * for a press that was claimed, so an unclaimed key is left to the page
   * exactly as before.
   */
  function onIntent(intent: WorkIntent, event: KeyboardEvent): void {
    const target = event.target;
    if (intent === 'up' || intent === 'down') {
      const to = nextRow(shown, selected?.itemId, intent);
      if (to === null || to === selected?.itemId) {
        event.preventDefault();
        return;
      }
      selectId(to);
      revealRow(to);
    } else if (intent === 'open') {
      // Enter on a focused link, button or disclosure is that control's own
      // press — a reader who tabbed to a row and pressed Enter means "select
      // this row", not "move focus to whatever is selected".
      if (target instanceof HTMLElement && target.closest('a,button,summary') !== null) return;
      if (selected === null) return;
      detailPane?.focus();
    } else if (intent === 'clear') {
      clearSelection();
    } else if (intent === 'copy') {
      void copySelected();
    } else {
      // `/` and `f`, the two keys whose control is inside the list column. Below
      // 1024 that column holds the open item instead (#2281 D2), so the press
      // brings the list back first — otherwise `/` focused an input that was
      // not in flow and `f` narrowed a list nobody could see. Above 1024 both
      // panes are drawn and `reveal` is false, so neither key touches the
      // selection.
      const reveal = needsListColumn(intent, viewportW, selected !== null);
      if (intent === 'filter') {
        setFilter(filter === 'needs-you' ? 'all' : 'needs-you', reveal);
      } else if (reveal) {
        clearSelection();
        searchWanted = true;
      } else {
        focusSearch();
      }
    }
    event.preventDefault();
  }

  // The page's shortcuts, on the key layer for as long as the page is mounted.
  $effect(() => registerShortcuts(workShortcuts(onIntent)));

  /**
   * Escape in the search box: the one press `workIntent` still gives a field.
   * It leaves the field, and leaves it at that: the selection is still there
   * for a second press. Not prevented either, so a `type="search"` box still
   * clears its own value the way the browser clears one everywhere else.
   */
  function onSearchKey(event: KeyboardEvent): void {
    const intent = workIntent({
      key: event.key,
      metaKey: event.metaKey,
      ctrlKey: event.ctrlKey,
      altKey: event.altKey,
      typing: true
    });
    if (intent === 'clear') searchBox?.blur();
  }

  // #2281 D12 — 92px for the state cell, where the artboard draws 96: its 7px
  // mark, the 8px the artboard spaces the word by, and `needs you` — the
  // longest word the vocabulary can serve — at label-11. The four pixels go
  // to the title, which now clamps to two lines instead of being cut at one.
  //
  // 92 and not 88: label-11 tracks +0.14em, and CSS letter-spacing lands after
  // EVERY glyph including the last, so the inline box of `NEEDS YOU` is
  // 73.26px, not the 71.72px its glyphs and inter-glyph gaps come to. 88 left
  // 73 — a quarter of a pixel short, on a shrinkable flex item with a space in
  // it, which is all it takes to break `NEEDS YOU` over two lines.
  // `page.design-c2.test.ts` budgets the trailing step now.
  //
  // 104 and not 92 (#2848, P19): a merge decision's cell is the needs-you
  // diamond AND its `in review` mark — 7 + 8 + 7 + 8 + 73.26 = 103.26px — and
  // at 92 the word broke into IN / REVIEW beside it. The word no longer wraps
  // at all (StatusMark), so the column is what has to hold it.
  //
  // 108 and not 104 (#2898): Chromium on Linux snaps each Plex Mono advance to
  // a whole 7px, so `IN REVIEW` is 76.86px, not 73.26 — 106.86px of content —
  // and at 104 the sweep in `ui-checks/status-words.mjs` read it cut.
  const COLUMNS = '56px minmax(0, 1fr) 108px';

  /**
   * A row with a reason line (#2786 D14) is two grid rows: the title and the
   * State cell over the reason, and the No. cell (the first span) spanning
   * both. #2848 P14 — the title keeps its two lines over the reason's two
   * (#2947), so the row grows past 56px to hold them rather than cutting
   * either to the three words a phone fits on one line. The reason takes the
   * State column's width as well as the title's (#3467 D18).
   */
  const TWO_LINE = 'h-auto min-h-14 py-2 content-center gap-y-0.5 [&>span:first-child]:row-span-2';

  /** Each shown row's quiet second line, read once per list (`rowReason`). */
  const reasons = $derived(new Map(rows.map((r) => [r.itemId, rowReason(r)])));
  const reasonOf = (row: { itemId: string }): string => reasons.get(row.itemId) ?? '';
</script>

<!-- The keyboard is the key layer's one window listener rather than the
     list's, so it answers with nothing focused as well as with the list
     focused — the ordinary state after a page load. The layer is what declines
     a press that belongs to the browser or to a field the reader is typing in. -->
<svelte:window bind:innerWidth={viewportW} />

<div class="flex min-h-0 flex-1">
  <!-- #2281 D2 — below 1024 the page is ONE column (DESIGN.md, Responsive
       Behavior), and the two panes take turns in it: the list while nothing is
       selected, the selected item's detail once there is one, with the `← Work`
       control below as the way back. Selecting used to highlight a row and
       leave the detail off-canvas to the right of a 390px screen, which is the
       one thing this screen is for. `?item=` is still the whole state, so a
       phone's URL opens the same item on a desktop beside its list.

       What a phone gives up is the detail pane's EMPTY state — the needs-you
       queue (#2210) — because the question at that width is "which row", and
       the rows are what is in front of the reader. The header's needs-you
       counter and this list's needs-you filter are both still there, and every
       row a person is the next move on wears the magenta diamond in the list
       itself. -->
  <aside
    aria-label="Issues"
    data-tour="work-list"
    bind:this={listPane}
    class={`min-w-0 flex-col overflow-y-auto lg:flex lg:w-[420px] lg:shrink-0 lg:border-r
            lg:border-hair ${selected || firstRunModel ? 'hidden' : 'flex w-full'}`}
  >
    <!-- #2545 D18 — below 1024 the detail pane's needs-you queue is not on
         screen, so the list carries its headline as one line above the search:
         the question this tab is opened for is answered before any row is.
         Pressing it narrows the list to those rows. -->
    {#if !reading}
      {#if queue.entries.length > 0}
        <button
          type="button"
          data-needs-you-strip
          onclick={() => setFilter('needs-you')}
          class="flex min-h-11 w-full items-center gap-2 border-b border-hair px-7 text-left
                 text-13 text-text-1 transition-colors hover:bg-ink-selected lg:hidden"
        >
          <StatusMark kind="needs_you" size={7} label="needs you" />
          <span class="min-w-0 truncate">{queue.headline}</span>
        </button>
      {:else}
        <p
          data-needs-you-strip
          class="flex min-h-11 items-center border-b border-hair px-7 text-13 text-text-3 lg:hidden"
        >
          {queue.headline}
        </p>
      {/if}
    {/if}

    <div class="flex items-center gap-2 px-7 pt-5 pb-4">
      <label class="sr-only" for="work-search">Search issues</label>
      <input
        id="work-search"
        type="search"
        bind:this={searchBox}
        bind:value={query}
        onkeydown={onSearchKey}
        placeholder="Search issues"
        class="h-11 min-w-0 flex-1 rounded-[4px] border border-hair-strong bg-transparent px-3.5
               font-mono text-13 text-text-1 placeholder:text-text-3
               hover:border-text-5 focus:border-text-5 focus:outline-none"
      />
      <!-- A narrowed list is marked in --text-1, the same way the header marks
           its active tab. Not magenta: the accent is rationed to the one call
           to action on a needs-you band (factory/classes.ts), and a control
           being switched on is not a human decision waiting to be made. -->
      <!-- `use:dismissable` (#2278 C15). The menu closed on neither an outside
           press nor Escape, and Escape was worse than nothing here: the window
           handler below read it as "clear the selection" and threw the reader's
           item away while the menu stayed open. The action spends the press on
           the menu, in the document's capture phase, so it reaches
           `onIntent` — through the key layer's window listener, and so the
           last of all — only when no menu is open. -->
      <details class="relative" bind:open={filterOpen} use:dismissable>
        <summary
          aria-label="Filter issues"
          title={`Filter: ${WORK_FILTER_LABEL[filter]}`}
          class="flex h-11 w-11 cursor-pointer list-none items-center justify-center rounded-[4px]
                 border bg-transparent transition-colors hover:border-text-5 hover:text-text-1"
          class:border-hair-strong={filter === 'all'}
          class:text-text-2={filter === 'all'}
          class:border-text-1={filter !== 'all'}
          class:text-text-1={filter !== 'all'}
        >
          <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.6"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <path d="M4 6h16M7 12h10M10 18h4" />
          </svg>
        </summary>
        <div
          class="absolute right-0 z-20 mt-2 flex w-40 flex-col border border-hair bg-ink-raised py-1
                 font-mono text-13"
        >
          {#each WORK_FILTERS as option (option)}
            <button
              type="button"
              aria-pressed={filter === option}
              onclick={() => setFilter(option)}
              class="flex min-h-8 w-full items-center px-3 py-1.5 text-left transition-colors
                     hover:bg-ink-selected hover:text-text-1"
              class:text-text-1={filter === option}
              class:text-text-2={filter !== option}>{WORK_FILTER_LABEL[option]}</button
            >
          {/each}
        </div>
      </details>
    </div>

    {#if reading}
      <p role="status" class="px-7 py-5 text-15 text-text-3">Reading work items…</p>
    {:else}
      {#if notices.length > 0}
        <div role="status" class="flex flex-col gap-2 px-7 pb-4 text-13 text-text-3">
          {#each notices as notice (notice.key)}
            <p class="flex items-start gap-2">
              <StatusMark kind="unverified" size={7} label={notice.label} class="mt-1.5 shrink-0" />
              <span>{notice.text}</span>
            </p>
          {/each}
        </div>
      {/if}

      <div class={dataRowClasses({ head: true, class: 'gap-3 px-7' })} style:grid-template-columns={COLUMNS}>
        <span>No.</span><span>Issue</span><span class="text-right">State</span>
      </div>

      {#each shown as row, i (row.itemId)}
        <!-- EVERY row selects in place (#2246). A row standing for a needs-you
             entry used to link OUT, to the epic or the artifact, and five of the
             seven rows on the live screen are such rows — so most of what an
             operator clicked here took them off Work. An entry row's identity is
             its `rowKey`, which `selectedRow` matches, so `?item=` selects it like
             any other; where the entry actually lives is a secondary link in the
             pane (`EntryDetail`). No row anchor carries `target` any more. -->
        <a
          href={itemHref(row.itemId)}
          data-item={row.itemId}
          data-needs-you={row.needsYou ? '' : undefined}
          onclick={(event) => select(event, row.itemId)}
          aria-current={selected?.itemId === row.itemId ? 'true' : undefined}
          class={dataRowClasses({
            rule: i === shown.length - 1 ? 'none' : 'quiet',
            class:
              'shrink-0 gap-3 px-7 transition-colors hover:bg-ink-selected ' +
              'focus-visible:outline-accent-2 focus-visible:-outline-offset-2' +
              (reasonOf(row) === '' ? '' : ` ${TWO_LINE}`)
          })}
          class:bg-ink-selected={selected?.itemId === row.itemId}
          style:grid-template-columns={COLUMNS}
        >
          <!-- #2786 N3 — the No. cell cuts a long id at its own edge and carries
               the whole id as its title: a live `supervised-daemon` ran out of
               the 56px column into the title beside it. -->
          <span
            class="overflow-hidden font-mono text-13 text-ellipsis whitespace-nowrap"
            class:text-text-1={selected?.itemId === row.itemId}
            class:text-text-3={selected?.itemId !== row.itemId}
            title={row.number}>{row.number}</span
          >
          <!-- #2786 D14 — the title is the ITEM's, and why it waits is the
               quiet line under it. #2848 — the title keeps two lines either
               way (the row grows to hold the reason), and its backtick spans
               draw as inline mono, not as backticks. -->
          <span
            class="line-clamp-2 text-15"
            class:text-text-1={selected?.itemId === row.itemId}
            class:font-medium={selected?.itemId === row.itemId}
            class:text-text-2={selected?.itemId !== row.itemId}
            ><Title text={row.title || row.ref} /></span
          >
          {#if reasonOf(row) !== ''}
            <!-- #2947 (#3467 D18) — two lines, then the ellipsis: at 390 one
                 line read "the review round cap w…", and the `title` holding
                 the rest is out of a finger's reach. Past two lines the row's
                 own needs-you block prints the whole sentence. It runs under
                 the State cell too: the title's column alone is 146px at 390,
                 and two lines of it cut a 60-character reason. -->
            <span
              data-row-reason
              class="col-span-2 col-start-2 row-start-2 line-clamp-2 text-13 [overflow-wrap:anywhere]
                     text-text-3"
              title={reasonOf(row)}>{reasonOf(row)}</span
            >
          {/if}
          <!-- #2281 D13 — one status vocabulary. The cell used to print the
               list's own word at the 13px data size, so an item the DAG called
               `done` was `success` here and `todo` was `idle`; the word is the
               MEANING's now (`stateWord`), at the label-11 DESIGN.md gives to
               status words — which is what uppercases it, so nothing here
               re-cases anything. -->
          <span class="flex justify-end text-11">
            <!-- #2539 D15 — every row the header counts wears the diamond, beside
                 whatever its state is: #2055 read a cyan IN REVIEW while counted
                 in "2 needs you". -->
            {#if needsYouDiamond(row)}
              <span data-needs-you-diamond class="mr-2 inline-flex shrink-0 self-center"
                ><StatusMark kind="needs_you" size={7} label="needs you" /></span
              >
            {/if}
            <!-- gap-2 (8px) over StatusMark's own 6px: the artboard's state cell
                 spaces its mark and word wider than the header's does. -->
            <StatusMark
              kind={row.state}
              vocabulary="run"
              size={7}
              word
              label={stateWord(row.state)}
              class="gap-2"
            />
          </span>
        </a>
      {/each}

      {#if shown.length === 0}
        <p class="px-7 py-5 text-15 text-text-3">
          {#if rows.length === 0}
            No work to show.
          {:else}
            No issue matches this search or filter.
          {/if}
        </p>
      {/if}
      <!-- #3453 — the open issues kew has not touched, under the rows a reader
           scans for what can start. -->
      <QueuePicker candidates={dashboard?.glance.queue_candidates ?? []} />
    {/if}

    <!-- The legend at the foot of the list (UXWork's `.keys` strip). `mt-auto`
         so it sits on the bottom edge of a short list rather than floating
         under the last row. The keys are `KEY_LEGEND`'s, which `keys.test.ts`
         holds to the intents `workIntent` actually answers — a legend that
         promises a key nothing handles is worse than no legend.
         #2545 D18 — and only from 1024 up, as DESIGN.md has it: a phone has no
         keyboard to read the hints for, and the strip took ~80px of a 390px
         list. #3467 D30 — and only over rows: with none, the keys move
         nothing and the strip is noise. -->
    {#if shown.length > 0}
    <div
      aria-label="Keyboard"
      class="keys mt-auto hidden flex-wrap items-center gap-x-4 gap-y-2 border-t border-hair px-7 py-3.5
             text-11 text-text-3 lg:flex"
    >
      {#each KEY_LEGEND as hint (hint.label)}
        <span class="flex items-center gap-1.5">
          {#each hint.keys as key (key)}
            <kbd
              class="rounded-[3px] border border-hair-strong px-1.5 py-0.5 font-mono text-11
                     text-text-2">{keyGlyph(key)}</kbd
            >
          {/each}
          {hint.label}
        </span>
      {/each}
    </div>
    {/if}
  </aside>

  <!-- A `<section>`, where DWork draws a `<main>`: the artboard is a whole page
       and owns its landmark, while in the app `(factory)/+layout.svelte` owns
       the one `<main>` every tab renders inside. A second one here would give
       the page two `main` landmarks — invalid HTML, and a screen reader's "skip
       to main" would have two destinations to choose between. -->
  <!-- `tabindex="-1"` so Enter can move focus here from the list without the
       pane itself joining the tab order: it is a region, not a control. -->
  <!-- `overflow-y-auto` is this pane's own scroller (#2278 C4 review). The
       shell clips at one viewport now, so a pane that cannot fit its content
       has to scroll it or lose it: on a short laptop the header, band and
       pipeline alone fill the 56px-shorter box, and what sits under them — the
       description and the whole evidence ledger — was squeezed to nothing with
       no way to reach it. Nothing scrolls here while the pane fits, which is
       every screen the artboard was drawn for; below that the pane scrolls as
       one column and the two lower panes keep their own scrollers inside it. -->
  <!-- `px-4` below `sm` (#2281 D2): the artboard's 48px gutters are a quarter
       of a 390px screen, and the pane they frame is the detail a phone came
       here for. -->
  <section
    aria-label="Item detail"
    data-tour="work-detail"
    bind:this={detailPane}
    tabindex="-1"
    class={`min-w-0 flex-1 flex-col gap-3 overflow-y-auto px-4 pt-7 pb-8
            focus-visible:outline-accent-2 sm:px-12 lg:flex ${selected || firstRunModel ? 'flex' : 'hidden'}`}
  >
    {#if selected}
      <!-- The way back to the one column the list is in. A real link, like
           every row: it is this page's URL without `?item=`, so middle-click
           and copy-link answer the way they do everywhere else on this page.
           `min-h-8` is D25's 32px press target. -->
      <a
        href={backHref()}
        onclick={back}
        class="-mt-1 flex min-h-8 w-fit items-center font-mono text-13 text-text-2
               transition-colors hover:text-text-1 lg:hidden">← Work</a
      >
      <ItemHeader
        row={selected}
        runs={factoryPulse.runs}
        onapplied={applyActedView}
        onstale={reread}
      />
      <!-- One block, whichever payload the row came from (#2280 D9). -->
      <NeedsYouBlock {block} {copied} oncopy={copySelected} itemId={selected.itemId} />
      <OrphanAckControl ack={selectedOrphan} href={itemHref} onacked={orphanAcked} />
      <!-- #3453 — the item's own readings, for a row that names an item: an
           entry row keyed on its entry (an orphan PR, a stalled epic) has no
           pipeline, issue body or evidence chain to draw, and reads none. -->
      {#if rowNamesItem(selected)}
        <Pipeline {pipeline} notice={pipelineGap} />
        <MergeVerify {pipeline} />
      {/if}
      <!-- "Plan a dispatch" (#2302, #2535): one line — the route this item
           would take, ready or refused, and the copy — with the rule behind
           each field in a drawer on demand. It reads once per item and per
           choice, never polls, and never dispatches: the command is how that
           happens.
           Only where dispatch is the item's next move (#2785): an item in
           review or held has another one, and a needs-you entry row is not an
           item. Its Run and Copy are quiet (#3467 D29): with nothing needing
           a person, Work shows no magenta. -->
      {#if rowDispatchIsNext(selected)}
        <DispatchLaunchpad itemId={selected.itemId} />
      {/if}
      <!-- The lower grid (DWork y ≈624–928). 5fr / 7fr, and `flex-1` so a long
           description scrolls inside its own column rather than pushing the
           ledger off the screen.
           `min-h-72` is the floor under that: `flex-1` alone resolves to
           whatever height is left, and on a short screen that is zero — two
           0px-tall scrollers hold their content where no scroll can reach it.
           288px is roughly a heading and six lines, and once the floor bites
           it is the pane above that scrolls. -->
      <!-- One column below 1024 (#2281 D2): 5fr of a 390px screen is a 130px
           description, which wraps an issue body one word to a line. -->
      {#if rowNamesItem(selected)}
        <div class="mt-[26px] grid min-h-72 grid-cols-1 flex-1 lg:grid-cols-[5fr_7fr]">
          <Description {description} notice={descriptionGap} />
          <EvidenceLedger {evidence} notice={evidenceGap} />
        </div>
      {/if}
    {:else if firstRunModel}
      <!-- #2515 — no work and no run history. Below 1024 this pane takes the
           one column the list would: an empty list has nothing to pick, and
           the card is what a new user came here for. -->
      <FirstRunCard card={firstRunModel} copied={copiedCommand} oncopy={copyCommandText} />
    {:else}
      <!-- Nothing selected. This is the tab an operator opens to find out what
           is waiting for them, so the pane answers that rather than asking
           them to pick something first (#2210). The count is the header's own
           (`$lib/work/queue`), and each wait is a link into the thing that is
           waiting — the item's own row, or the artifact an entry points at. -->
      <section aria-label="Needs you" class="flex min-w-0 flex-col gap-4">
        <div class="flex items-baseline gap-3">
          <!-- The magenta is rationed to "a person is the next move", so a
               clear queue — and one nobody could read — gets the headline and
               no mark: a diamond over "nothing needs you" spends the one
               colour this UI keeps for an outstanding decision. -->
          {#if queue.entries.length > 0}
            <StatusMark kind="needs_you" size={12} label="needs you" />
          {/if}
          <h2
            class="text-26"
            class:text-text-1={queue.entries.length > 0}
            class:text-text-3={queue.entries.length === 0}
          >
            {queueHeadline}
          </h2>
        </div>

        {#if soleBlock}
          <!-- One wait, expanded: the question the tab is for is already
               answered, so the reader gets the sentence, the age and the
               command without a click (#2280 D11). -->
          <NeedsYouBlock block={soleBlock} {copied} oncopy={copySelected} itemId={queue.sole?.itemId ?? null} />
          <!-- No "Open #…" line under it (#3467 D30): the block's own links
               already reach the item, and the row is one press away. -->
          <OrphanAckControl ack={soleOrphan} href={itemHref} onacked={orphanAcked} />
        {:else if queue.entries.length > 0}
          <ul class="flex min-w-0 list-none flex-col border-t border-hair">
            {#each queue.entries as entry (entry.key)}
              <li class="min-w-0">
                <!--
                  A wait that selects an item takes the list's own href, so
                  clicking it from `?filter=needs-you` keeps the filter the
                  reader is on. Every entry does since #2246 — `selects` is
                  still read rather than assumed, so a wait that one day links
                  out keeps the destination the model gave it and does not get
                  a `?item=` for an id the list cannot select. Same rule, same
                  handler, as the rows in the list.
                -->
                <!-- Every row here needs a person, so every row carries the
                     one mark that means it — the magenta diamond — plus what
                     it is waiting for and how long it has stood (#2280 D11).
                     The state word is gone: it said "in review" in cyan over a
                     merge decision, which is the one reading this list must
                     not give. -->
                <a
                  href={entry.selects ? itemHref(entry.key) : entry.href}
                  onclick={entry.selects ? (event) => select(event, entry.key) : undefined}
                  class="flex min-w-0 items-start gap-3 border-b border-hair py-3.5
                         transition-colors hover:bg-ink-selected"
                >
                  <span class="flex shrink-0 items-center pt-1">
                    <StatusMark kind="needs_you" size={10} label="needs you" />
                  </span>
                  <span class="w-[72px] shrink-0 pt-px font-mono text-13 text-text-3"
                    >{entry.ref}</span
                  >
                  <span class="flex min-w-0 flex-1 flex-col gap-1">
                    <span class="min-w-0 truncate text-15 text-text-1">{entry.text}</span>
                    <!-- One model, one renderer: whether this line repeats
                         what is above it is `entryOf`'s to decide, and it
                         serves '' when it would. The markup's own
                         `summary !== text` test could not see the repeat that
                         mattered — the No. column left of these two cells,
                         which the sentence used to name again (#2280 D11). -->
                    {#if entry.summary !== ''}
                      <span class="min-w-0 truncate text-13 text-text-3">{entry.summary}</span>
                    {/if}
                  </span>
                  {#if entry.since !== ''}
                    <span class="shrink-0 pt-px font-mono text-13 text-text-2">{entry.since}</span>
                  {/if}
                </a>
              </li>
            {/each}
          </ul>
        {/if}

        {#if queue.more > 0}
          <!-- Never a silent shortening: the rest are one filter away. -->
          <a
            href="/work?filter=needs-you"
            class={`text-13 text-text-3 hover:text-text-1 hover:underline ${TAP_INLINE}`}
            >{queue.more} more — show the whole queue</a
          >
        {/if}

        {#if soleBlock === null}
          <p class="text-15 text-text-3">Select an issue to see what it is waiting for.</p>
        {/if}
      </section>
    {/if}
  </section>
</div>
