<script lang="ts">
  // The 56px factory header (FUI-09): wordmark and state cursor, three tabs,
  // the two live counters, the fleet's safety stop with its resume (FUI-26),
  // the repo, and everything legacy folded into "More".
  //
  // Every number it shows comes from `header-state.ts` and every reading from
  // the one shared `factoryPulse`, so this file only decides which span carries
  // which of them. Geometry is the header of each artboard in
  // frontend/design/factory-ui/, y 0–56: 32px side padding, a 40px gap after
  // the wordmark, 28px between tabs, 24px in the right cluster, and a 2px
  // underline on the active tab that sits on the header's own bottom hairline.
  //
  // Those are the desktop numbers, and they are the FULL cluster's numbers.
  // One 56px row is the design (DESIGN.md, Grid), so what changes with width
  // is not the row — it is how much is ON the row, and the row takes its
  // readings back in two steps:
  //
  //   every width  the wordmark, the three tabs, the needs-you mark with its
  //                count, a red mark for anything folded, and More.
  //   `lg` 1024    plus the failures: the stale build, the factory health,
  //                the fleet stop with its resume. This is the console's own
  //                fold (DESIGN.md, Responsive Behavior).
  //   `xl` 1280    plus the two quiet readings — the working count and the
  //                repo — which is the full artboard cluster.
  //
  // The second step is arithmetic, not taste (#2418 review): 1024px cannot
  // hold three failure readings AND a working count AND a repo AND the stop's
  // two-press confirm, and what it could not hold it scrolled sideways. The
  // quiet pair is what gives up its place, because "how many runs are working"
  // and "which repo" are the two readings here that nothing waits on.
  // Everything not on the row is in the More menu, which is why More itself
  // stays at every width — it is the only way to read them there.
  //
  // `lg` is where the first step lands because the row's own width put it
  // there, not taste (#2333 round 3): the first fix for #2278 folded at `sm`,
  // and the desktop row it folded to needed ~741px before the page stopped
  // scrolling and ~929px before the nav stopped ellipsising its tabs
  // (`HEADER_FOLD`). So from 641px up the header was wider than its window,
  // and the epic page carried a horizontal scrollbar nothing on the epic page
  // had put there (#2419). `lg` is the first Tailwind breakpoint above both
  // floors, and it is the number the rest of the console already folds on —
  // the Work list and detail, the epic minimap and key hints, and the rail's
  // own stack (DESIGN.md, Responsive Behavior).
  //
  // The one thing the row will NOT spend to keep a reading is a nav label
  // (#2418). At 390px the tabs read `Wo… Monit… Epi…`: the nav was the row's
  // one compressible item, so it paid for the counts beside it. Every other
  // reading here has a second way to be read — a title, the More menu, the
  // page it links to — and a truncated nav label has none: it is the way
  // around the console. So the nav is `shrink-0` with whole labels at every
  // width, and the readings opposite give way first.
  //
  // With the nav fixed, the row's own spacing is all that is left to tune, and
  // below `sm` it is tuned: 12px between the three row items against the
  // artboard's 40px, and 16px between tabs against its 28px. That is 16px of
  // margin bought back. The row then measures 369px of content in a 390px
  // screen, and the budget behind that number is priced item by item off this
  // markup in `factory-header.test.ts` — a phone row with nothing on it that
  // can shrink either fits by arithmetic or scrolls the page sideways.
  //
  // Above the fold it is the other way round: the row carries everything, so
  // it cannot fit by carrying less and something must give. The give is the
  // cluster's SENTENCES — each one a `truncate` span under a `max-w-` cap —
  // and nothing else: every mark, every count, both resume presses, `More` and
  // the nav hold their whole width. A sentence has a `title`, a menu line and
  // a page to be read from; a mark cut in half is a reading destroyed. See the
  // cluster's own note below.
  import { page } from '$app/state';
  import { theme } from '$lib/theme.svelte';
  import { dismissable } from '$lib/ui/dismissable';
  import StatusMark from '$lib/ui/StatusMark.svelte';
  import { driverStaleNotice, resumeChip, resumeFleet } from '$lib/verbs';
  import { bundleBadge } from './bundle-badge';
  import {
    countLabel,
    CURSOR_COLOR,
    cursorTone,
    FLEET_STOPPED,
    fleetStop,
    headerCounts,
    stillReading,
    UNREADABLE
  } from './header-state';
  import { healthBadge } from '$lib/health';
  import { FACTORY_TABS, isActive, MORE_GROUPS } from './nav';
  import { chromeSlots } from './chrome-slots.svelte';
  import { factoryPulse } from './pulse.svelte';

  const pathname = $derived(page.url.pathname);
  const counts = $derived(headerCounts(factoryPulse.dashboard, factoryPulse.runs));
  const tone = $derived(
    cursorTone({ ...counts, itemNeedsYou: factoryPulse.viewedItemNeedsYou })
  );
  const badge = $derived(bundleBadge(factoryPulse.bundle));
  /**
   * The factory-health link (#2298). `null` when the reading is healthy AND when
   * it could not be taken: a header badge is an interruption, so it appears only
   * when something REQUIRED is down — a missing image no lane routes to and a
   * tracker no epic uses are both deliberately not counted.
   */
  const health = $derived(healthBadge(factoryPulse.health));
  const cursorLabel = $derived(
    { attention: 'needs you', working: 'working', idle: 'idle' }[tone]
  );

  let moreOpen = $state(false);

  // The safety stop and its resume (FUI-26). The console's fleet-stopped banner
  // was on the page every reader passed through; after the cutover the header is,
  // so the stop and the one write that reverses it come with it. Same two-step
  // confirm as the console, because resuming a stopped fleet starts dispatching
  // work again, and the same `verbs.ts` call, so the audit row is identical.
  const stop = $derived(fleetStop(factoryPulse.dashboard));
  /**
   * Is anything red folded into the More menu? Below `lg` the stale build, the
   * factory health count and the fleet stop all read inside it (#2418), and a
   * failure behind a closed menu with nothing on the row is a silent failure.
   * The mark beside `More` is what keeps it loud at phone width.
   */
  const folded = $derived(stop !== null || health !== null || badge?.stale === true);
  let resuming = $state(false);
  let confirming = $state(false);
  /** The server's answer: its chip, and any notice that came with it. '' until then. */
  let chip = $state('');
  let notice = $state('');
  /** A refused or failed resume, said out loud rather than swallowed. */
  let resumeError = $state('');

  // A stop that has cleared takes its answer with it. Without this, the chip
  // from one resume would still be in hand the next time the fleet stops, and it
  // renders where that stop's resume belongs — so a second stop would announce
  // itself with no way to reverse it.
  $effect(() => {
    if (stop !== null) return;
    chip = '';
    notice = '';
    resumeError = '';
    confirming = false;
  });

  async function confirmResume(): Promise<void> {
    resuming = true;
    resumeError = '';
    const res = await resumeFleet(true); // confirm: true — the press below is it
    resuming = false;
    confirming = false;
    if (res.ok) {
      chip = resumeChip(res.data);
      notice = driverStaleNotice(res.data.driver_stale) ?? '';
    } else resumeError = res.message;
  }
</script>

<header
  class="flex h-14 shrink-0 items-center gap-3 border-b border-hair bg-ink px-4 sm:gap-10 sm:px-8"
>
  <!-- Wordmark plus the state cursor. 26px/600 with −0.03em tracking is the
       artboard's own wordmark, tighter than the shared `.text-26` title scale,
       so the sizes are spelled out here rather than fighting that utility. -->
  <div
    class="flex shrink-0 items-center gap-[5px] font-mono text-[26px] leading-none font-semibold tracking-[-0.03em] text-text-1"
  >
    <span>kew</span>
    <span
      class="inline-block h-6 w-3"
      style:background={CURSOR_COLOR[tone]}
      role="img"
      aria-label={`factory state: ${cursorLabel}`}
    ></span>
  </div>

  <!-- `shrink-0`, and a label with nothing on it that can ellipsise (#2418).
       The nav used to be the row's one compressible item: `min-w-0` on it and
       on every tab, a `truncate` span carrying the label, and `overflow-clip`
       so it could compress all the way to nothing. That kept the readings
       beside it whole and spent the navigation to do it — at 390px the tabs
       read `Wo… Monit… Epi…`. The trade is backwards: a clipped reading can
       still be had another way (its title, the More menu, the page it links
       to) and a clipped nav label cannot be had at all.
       `shrink-0` on the nav AND on every tab. The nav's own floor is what
       stops the row taking width off it; the floor on each tab is what stops
       the nav taking width off a label to hold its gaps, which is the same
       ellipsis one level down. The row still has a valve — the readings
       opposite ellipsise their sentences (the cluster's note below) — and it
       is on the far side of this floor.
       The gap is the phone's own, 16px against the artboard's 28px: with the
       labels fixed, the space between them is the only part of the nav left to
       give, and that is what the needs-you count beside it is paid for with. -->
  <nav
    aria-label="Factory views"
    class="flex h-14 shrink-0 gap-4 font-mono text-13 tracking-[0.02em] sm:gap-7"
  >
    {#each FACTORY_TABS as tab (tab.href)}
      {@const active = isActive(pathname, tab.href)}
      <a
        href={tab.href}
        aria-current={active ? 'page' : undefined}
        class="flex h-14 shrink-0 items-center border-b-2 pt-[2px] transition-colors"
        class:border-text-1={active}
        class:text-text-1={active}
        class:border-transparent={!active}
        class:text-text-2={!active}
        class:hover:text-text-1={!active}
        ><span class="whitespace-nowrap">{tab.label}</span></a
      >
    {/each}
  </nav>

  <!-- The readings, and the row's ONE release valve.
       Below `lg` the row fits by CARRYING LESS: the `lg:` switches below are
       that. The row keeps the needs-you mark with its count — the phone's
       first question from DESIGN.md, "what needs me?" — and every other
       reading reads inside the More menu instead, behind the one press that
       stays. Nothing on that row can shrink, and nothing needs to: it adds up
       to 369px of a 390px screen, priced off this markup in
       `factory-header.test.ts` and measured at 368.9px in Chromium.
       From `lg` up the row carries the FULL artboard cluster, and carrying
       less is no longer available to it — so this is where the valve is. The
       cluster alone is ~490px of readings against the ~690px the row has left
       at 1024 once the wordmark and three whole labels are on it, and that is
       the calm reading: a stale build, a failing health check and a stopped
       fleet are each an extra sentence the artboard was never drawn with, and
       three of them together want more row than 1280 has. Without a valve
       that is sideways scroll at a width DESIGN.md forbids it at.
       So `min-w-0` here, and the give comes out of the SENTENCES: every one
       of them is a `truncate` span under a `max-w-` cap, so a cluster narrower
       than its own content ellipsises those and nothing else. What may not
       give is anything that would be lost rather than shortened — every mark,
       every count, every whole word, both resume presses and `More`. Those
       are what the FLOOR is made of, and the floor is what keeps #2278 D1
       dead: a box squeezed past its own unshrinkable content does not
       compress it, it paints it over the reading next door and off the
       screen. So `min-w-0` goes HERE and nowhere inside: each reading keeps
       its automatic minimum, which is its mark and its presses, and only the
       `truncate` spans within it go to nothing.
       A sentence is the right thing to spend because a sentence is the one
       reading with somewhere else to be: its own `title`, the More menu below
       `lg`, the page it links to. A nav label has none of those, which is why
       #2418 took the give off the nav — not so that nothing could give.
       The floor is still arithmetic, and it is checked as arithmetic: 903px
       of unshrinkable row in the worst state the fleet stop can be in (both
       confirm presses up, a refusal beside them) against the 1024px this opens
       at, priced item by item off this markup in `factory-header.test.ts`.
       The two `xl:` readings below are what bought that margin. -->
  <div class="ml-auto flex min-w-0 items-center gap-3 font-mono text-13 text-text-2 lg:gap-6">
    <!-- The working count joins the row at `xl`, not `lg` (#2418 review).
         It is a reading, not a decision, and it is one of the two quietest
         things the cluster carries — so when 1024px has to hold three failure
         readings the artboard was never drawn with AND a two-press confirm,
         this is the first of the two that gives up its place. It reads in the
         More menu until 1280, exactly as it does on a phone. #3467 D30 — and
         not at zero: "0 working" is a reading about nothing, so a quiet
         factory prints none (`countLabel(0)` is still '0' wherever it is
         asked). -->
    {#if counts.working !== 0}
      <span class="hidden items-center gap-2 xl:flex">
        {#if stillReading(counts.working, factoryPulse.hasRead)}
          <span class="text-text-3">reading…</span>
        {:else}
          <StatusMark kind="working" size={7} label="working" />
          <span class="text-text-1">{countLabel(counts.working)}</span> working
        {/if}
      </span>
    {/if}
    <!-- `◆ n` at every width. The mark carries its own accessible name, so
         hiding the word below `lg` costs a screen reader nothing. Only once
         the count is read (design review c4, D35): a magenta diamond over a
         bare `—` claims "a person is needed" about a number nobody has.
         And only above 0 (#2784, N11): a magenta "◆ 0 needs you" claimed a
         person was needed over a set holding nobody. Magenta is rationed. -->
    {#if counts.needsYou !== null}
      {#if counts.needsYou > 0}
        <!-- `data-tour`: the demo tour frames the whole count, mark to words (#3146). -->
        <span class="flex shrink-0 items-center gap-2" data-tour="needs-you-count">
          <StatusMark kind="needs_you" size={7} label="needs you" />
          <span class="text-text-1" data-needs-you-count>{countLabel(counts.needsYou)}</span>
          <span class="hidden lg:inline">needs you</span>
        </span>
      {/if}
    {/if}

    <!-- A stale bundle is a failure signal, not footer trivia: the screen is
         showing an old build of itself, so it says so on every factory page
         for as long as it is true. Below `lg` it says it in the More menu,
         where the sha has room to be read rather than titled. -->
    {#if badge?.stale}
      <span class="hidden min-w-0 items-center gap-2 text-danger lg:flex" title={badge.title}>
        <StatusMark kind="failed" size={7} label="stale build" />
        <span class="max-w-44 truncate">{badge.label}</span>
      </span>
    {/if}

    <!-- Something required is down. A link, not a banner: the page it goes to is
         the one that says what and how to fix it, and the header has one row. -->
    {#if health}
      <a
        href="/health"
        class="hidden shrink-0 items-center gap-2 text-danger hover:text-danger lg:flex"
        title={health.title}
      >
        <StatusMark kind="failed" size={7} label="factory health" />
        <span class="text-text-1">{health.count}</span>
        <span>down</span>
      </a>
    {/if}

    <!-- The stop sits with the readings, not over the tabs: it is a fact about
         the fleet, like the counters, and the header has one row. Its sentence
         and any driver notice are the title — a 56px header has no second line,
         and the words that do not fit must still be reachable.
         Below `lg` the whole stop moves into the More menu (#2418). It is the
         widest thing the row ever carries — a mark, a sentence and two presses
         — and at 390px it cannot be on a row that also keeps three whole nav
         labels. The menu is a better home than the squeeze was: the sentence
         reads out instead of hiding in a `title`, which a touch screen cannot
         open at all, and both presses keep their whole phrase.
         TWO boxes on the row, not one (#2418 review). A box is squeezed as a
         whole: with `min-w-0` it hands the width of the presses inside it
         back to the row and paints `cancel` over the reading next door, and
         without `min-w-0` it cannot be squeezed at all, so its sentence will
         not ellipsise and the row scrolls sideways instead. The stop is the
         only reading here that is both a sentence and a press, so it is two
         items: the sentence gives (`min-w-0`, capped, truncating) and the
         presses never do (`shrink-0`). The whole cost of that is 24px of
         cluster gap between them instead of 8px — the same spacing every
         other reading on this row is held apart by. -->
    {#if stop}
      <span
        class="hidden min-w-0 items-center gap-2 text-danger lg:flex"
        title={notice === '' ? stop.message : `${stop.message} ${notice}`}
      >
        <StatusMark kind="failed" size={7} label={FLEET_STOPPED} />
        <span class="max-w-32 truncate">{FLEET_STOPPED}</span>
        {#if chip !== ''}
          <span class="max-w-40 truncate text-text-2" title={chip}>· {chip}</span>
        {:else if resumeError !== ''}
          <!-- A refused or failed resume, capped and ellipsised with the whole
               server sentence as its title: the message has no length to it,
               and an unbounded reading in this cluster is a row that cannot
               fit (the note above). It sits where the chip would, because a
               refusal and an answer are one slot — the last thing the server
               said about this stop — and only ever one of them is in hand. -->
          <span class="max-w-72 truncate text-text-2" title={resumeError}
            >· {resumeError}</span
          >
        {/if}
      </span>
      <!-- The presses, in their own `shrink-0` box. The press that reverses a
           stopped fleet is the most important thing on the row, and a button
           squeezed to a few pixels of its own word is not a button. Gone once
           the chip is in hand: the resume has happened, and there is nothing
           left to press. -->
      {#if chip === ''}
        <span class="hidden shrink-0 items-center gap-2 text-danger lg:flex">
          {#if confirming}
            <button
              type="button"
              disabled={resuming}
              onclick={confirmResume}
              class="shrink-0 underline underline-offset-2 transition-colors hover:text-text-1"
              >confirm resume</button
            >
            <button
              type="button"
              disabled={resuming}
              onclick={() => (confirming = false)}
              class="shrink-0 text-text-2 transition-colors hover:text-text-1">cancel</button
            >
          {:else}
            <button
              type="button"
              onclick={() => (confirming = true)}
              class="shrink-0 underline underline-offset-2 transition-colors hover:text-text-1"
              >resume</button
            >
          {/if}
        </span>
      {/if}
    {/if}

    <span class="hidden h-5 w-px shrink-0 bg-hair xl:block" aria-hidden="true"></span>
    <!-- Capped and ellipsised like every other sentence on the row. A repo
         slug has no length bound — `delivery_repo` is whatever the server was
         pointed at — and uncapped it was the one reading here the valve above
         could not spend: a plain span wraps at its own hyphens instead, which
         on a 56px row is a second line the header has no height for. -->
    <!-- No readout until the first read lands (D35): a bare `—` there reads
         as a failure nobody had yet. -->
    <span
      class:invisible={stillReading(counts.repo, factoryPulse.hasRead)}
      class="hidden max-w-48 truncate text-text-3 xl:block"
      title={counts.repo ?? UNREADABLE}>{counts.repo ?? UNREADABLE}</span
    >

    <!-- Something red is folded into the menu below `lg`, so the mark says so
         from the row. Without it a stopped fleet, a stale build or a failing
         health check is invisible at phone width — behind a press, with
         nothing to tell a reader to make it. The needs-you count needs no mark
         here because it is still ON the row (and the wordmark's cursor is
         magenta for it besides). -->
    {#if folded}
      <span class="flex shrink-0 items-center lg:hidden">
        <StatusMark kind="failed" size={7} label="something is down, in More" />
      </span>
    {/if}

    <!-- `use:dismissable` — an outside press or Escape closes it (#2278 C15).
         `h-8` is the 32px press target a phone needs (D25); the desktop row is
         56px tall and swallows it. -->
    <details class="relative shrink-0" bind:open={moreOpen} use:dismissable>
      <summary
        class="flex h-8 shrink-0 cursor-pointer list-none items-center text-text-2 transition-colors hover:text-text-1"
        aria-label="More destinations">More</summary
      >
      <div
        class="absolute right-0 z-20 mt-2 flex w-60 flex-col rounded-[11px] border border-hair-strong bg-ink-raised py-1 shadow-[var(--shadow)] lg:w-44"
      >
        <!-- The header's chrome slot (#3139): nothing unless a build fills it
             (the demo's Tours entry, #3145). In the menu, not on the row: the
             row is priced to the pixel at 390 and 1024 (factory-header.test.ts)
             and has no width to give an entry nothing here budgets for. -->
        {#if chromeSlots.header}
          <chromeSlots.header />
        {/if}
        <!-- What the row drops, read here instead — and read WHOLE. A menu has
             lines to spare where the row has none, so the stale build keeps
             its sha, the health count keeps its word, and the stop keeps its
             own sentence, which on the row is only a `title`.
             Two blocks, because the row takes its readings back in two steps
             (#2418 review): the failures below join it at `lg`, and the two
             quiet readings — how many runs are working, and which repo — only
             at `xl`, because 1024px cannot hold the failures and those at the
             same time. Failures first in both: the menu's reading order is
             what matters, not the row's left-to-right. -->
        <div class="flex flex-col gap-2 px-3 pt-2 lg:hidden">
          {#if badge?.stale}
            <span class="flex items-center gap-2 text-danger">
              <StatusMark kind="failed" size={7} label="stale build" />
              <span>{badge.label}</span>
            </span>
          {/if}
          {#if health}
            <a
              href="/health"
              onclick={() => (moreOpen = false)}
              class="flex min-h-8 items-center gap-2 text-danger transition-colors hover:text-danger"
            >
              <StatusMark kind="failed" size={7} label="factory health" />
              <span class="text-text-1">{health.count}</span>
              <span>down</span>
            </a>
          {/if}
          {#if stop}
            <span class="flex flex-col gap-1 text-danger">
              <span class="flex items-center gap-2">
                <StatusMark kind="failed" size={7} label={FLEET_STOPPED} />
                <span>{FLEET_STOPPED}</span>
              </span>
              <span class="text-text-2"
                >{notice === '' ? stop.message : `${stop.message} ${notice}`}</span
              >
              {#if chip !== ''}
                <span class="text-text-2">· {chip}</span>
              {:else if confirming}
                <span class="flex items-center gap-3">
                  <button
                    type="button"
                    disabled={resuming}
                    onclick={confirmResume}
                    class="min-h-8 underline underline-offset-2 transition-colors hover:text-text-1"
                    >confirm resume</button
                  >
                  <button
                    type="button"
                    disabled={resuming}
                    onclick={() => (confirming = false)}
                    class="min-h-8 text-text-2 transition-colors hover:text-text-1">cancel</button
                  >
                </span>
              {:else}
                <button
                  type="button"
                  onclick={() => (confirming = true)}
                  class="min-h-8 self-start underline underline-offset-2 transition-colors hover:text-text-1"
                  >resume</button
                >
              {/if}
              {#if resumeError !== ''}
                <span class="text-text-2">· {resumeError}</span>
              {/if}
            </span>
          {/if}
        </div>
        <div class="flex flex-col gap-2 px-3 py-2 xl:hidden">
          {#if counts.working !== 0}
            <span class="flex items-center gap-2 text-text-2">
              {#if stillReading(counts.working, factoryPulse.hasRead)}
                <span class="text-text-3">reading…</span>
              {:else}
                <StatusMark kind="working" size={7} label="working" />
                <span class="text-text-1">{countLabel(counts.working)}</span> working
              {/if}
            </span>
          {/if}
          <span
            class:invisible={stillReading(counts.repo, factoryPulse.hasRead)}
            class="truncate text-text-3">{counts.repo ?? UNREADABLE}</span
          >
        </div>
        <span class="my-1 h-px bg-hair xl:hidden" aria-hidden="true"></span>
        <!-- Three groups, each under its label (D40): what the factory is,
             what it did, what it runs with. -->
        {#each MORE_GROUPS as g (g.label)}
          <div class="flex flex-col pt-1" role="group" aria-label={g.label}>
            <span class="px-3 pt-1.5 pb-0.5 text-11 text-text-3" data-more-group>{g.label}</span>
            {#each g.items as d (d.href)}
              <a
                href={d.href}
                onclick={() => (moreOpen = false)}
                class="flex min-h-8 items-center px-3 py-1.5 text-text-2 transition-colors hover:bg-ink-selected hover:text-text-1"
                >{d.label}</a
              >
            {/each}
          </div>
        {/each}
        <span class="my-1 h-px bg-hair" aria-hidden="true"></span>
        <button
          type="button"
          onclick={() => theme.toggle()}
          class="flex min-h-8 w-full items-center px-3 py-1.5 text-left text-text-2 transition-colors hover:bg-ink-selected hover:text-text-1"
          >{theme.current === 'dark' ? '☀ light' : '☾ dark'}</button
        >
      </div>
    </details>
  </div>
</header>
