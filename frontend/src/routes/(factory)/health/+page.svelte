<script lang="ts">
  /**
   * `/health` — can the factory work? (#2298)
   *
   * The grouping is Dagster's Settings tabs read as a list of questions: what
   * can run a container, what images exist, what posture each lane resolves to,
   * which runners and credentials are reachable, which trackers answer, which
   * daemons are beating, and what the ceilings are. The look is kew's: hairline
   * sections, one status mark vocabulary, no cards, no toasts, no modal errors.
   *
   * Every reading on the page comes from `$lib/health`, which owns the three
   * states (running / not running but required / not enabled, plus honest
   * unknown) and the counting rule. Nothing here re-derives a verdict: a section
   * asks the pure function and draws the answer, so the header badge and the row
   * beside it can never disagree.
   *
   * The page polls nothing. It is a read taken when you arrive, with a refresh
   * control that says how old the data may be — the host probes behind it are
   * cached for up to a minute server-side, so a ticking "live" claim would be
   * false. `data is at most N s old` is the honest version.
   */
  import { Command, CopyButton, SectionHead, StatusMark, Why } from '$lib/ui';
  import { chipClasses, dataRowClasses } from '$lib/factory/classes';
  import { fetchEffectiveConfig, fetchHealth, fetchHealthEnv, fetchReadiness } from '$lib/api';
  import type { EffectiveConfig, Health, HealthEnv, Readiness } from '$lib/api';
  import {
    BUDGET_FIELDS,
    STATE_LABEL,
    STATE_MARK,
    dataAgeLabel,
    driverState,
    credentialWords,
    emptyKind,
    fieldValueLabel,
    loopsNote,
    healthHeadline,
    healthWarnings,
    heartbeatLabel,
    imageRepair,
    imageState,
    laneDangers,
    laneWhy,
    observedRuntimesNote,
    postureState,
    relativeAge,
    runnerState,
    runtimeState,
    watcherState,
    type HealthState
  } from '$lib/health';

  let {
    data
  }: {
    data: {
      health: Health | null;
      env: HealthEnv | null;
      config: EffectiveConfig | null;
      readiness?: Readiness | null;
    };
  } = $props();

  let health = $state<Health | null>(data.health);
  let env = $state<HealthEnv | null>(data.env);
  let config = $state<EffectiveConfig | null>(data.config);
  let readiness = $state<Readiness | null>(data.readiness ?? null);
  let refreshing = $state(false);
  /**
   * `now` advances only on a refresh, never on a timer: the age label is about
   * the reading in hand, and a second-by-second countdown would be a poll in
   * everything but name (#2285's reading habits).
   */
  let now = $state(Date.now());

  const liveMode = $derived(health !== null);
  const warnings = $derived(healthWarnings(health));
  const ageLabel = $derived(dataAgeLabel(health?.checked_at ?? null, now));

  async function refresh() {
    refreshing = true;
    const [h, e, c, r] = await Promise.all([
      fetchHealth().catch(() => null),
      fetchHealthEnv().catch(() => null),
      fetchEffectiveConfig().catch(() => null),
      fetchReadiness().catch(() => null)
    ]);
    // Keep the last good reading for any arm that failed: a dropped read is not
    // evidence that the thing it describes went away.
    if (h !== null) health = h;
    if (e !== null) env = e;
    if (c !== null) config = c;
    if (r !== null) readiness = r;
    now = Date.now();
    refreshing = false;
  }

  /** The one filter on the page: hide everything that is reading fine. */
  let onlyProblems = $state(false);
  /** Which `why ▸` folds are open, by the key each one names (#2536). */
  let whyOpen = $state<Record<string, boolean>>({});
  function keep(state: HealthState): boolean {
    return !onlyProblems || state === 'required';
  }

  const runtimes = $derived(health?.runtimes ?? []);
  const images = $derived(health?.images ?? []);
  const lanes = $derived(health?.posture ?? []);
  const runners = $derived(health?.runners ?? []);
  const drivers = $derived(health?.drivers ?? []);
  const watchers = $derived(health?.watchers ?? []);
  const envVars = $derived(env?.vars ?? []);

  const shownRuntimes = $derived(runtimes.filter((r) => keep(runtimeState(r))));
  const shownImages = $derived(images.filter((i) => keep(imageState(i))));
  const shownLanes = $derived(lanes.filter((l) => keep(postureState(l))));
  const shownRunners = $derived(runners.filter((r) => keep(runnerState(r))));
  const shownDrivers = $derived(drivers.filter((d) => keep(driverState(d))));
  const shownWatchers = $derived(watchers.filter((w) => keep(watcherState(w))));
  const shownEnv = $derived(envVars.filter((v) => !onlyProblems || !v.set));

  /**
   * The two empty states, as sentences. "Nothing configured" is something to set
   * up; "nothing matches the filter" is a filter to clear. One blank box for
   * both leaves the reader guessing which (DESIGN.md: say what an empty read
   * means).
   */
  function emptyLine(total: number, shown: number, noun: string): string {
    const kind = emptyKind(total, shown);
    if (kind === 'none-configured') return `No ${noun} configured.`;
    return `No ${noun} matches the filter — ${total} hidden.`;
  }

  const observedLabel = $derived(observedRuntimesNote(health?.observed_7d));

  const budgetRows = $derived(
    BUDGET_FIELDS.map((field) => ({ field, entry: config?.fields?.[field] })).filter(
      (row) => row.entry !== undefined
    )
  );

  // Below 640 a three-column row left 200px to the state word and cut the
  // names beside it to "clau…" (#2788 N13): the row becomes two lines, the
  // name and its state over the full-width middle cell; a two-column row gives
  // its state only the width the word takes.
  const THREE_COLS =
    'grid-cols-[minmax(0,1fr)_auto] gap-y-1 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_200px] ' +
    'max-sm:[&>:nth-child(2)]:col-span-full max-sm:[&>:nth-child(2)]:row-start-2';
  const TWO_COLS = 'grid-cols-[minmax(0,1fr)_auto] sm:grid-cols-[minmax(0,1fr)_200px]';
  const row2 = dataRowClasses({ height: 50, class: TWO_COLS });
  const headRow2 = dataRowClasses({ head: true, height: 50, class: TWO_COLS });
  const row3 = dataRowClasses({ height: 50, class: `${THREE_COLS} max-sm:h-auto max-sm:py-2.5` });
  const headRow3 = dataRowClasses({ head: true, height: 50, class: THREE_COLS });
  // A runner id is never cut, so a row that carries one is at least 50px and
  // grows when the id wraps rather than painting over the next row (#2897, #2952).
  const idRow = dataRowClasses({
    height: 50,
    class: `${THREE_COLS} h-auto min-h-[50px] py-2.5`
  });
</script>

<div class="flex min-h-0 flex-1 flex-col gap-9 overflow-y-auto px-4 pt-8 pb-9 sm:px-10">
  <header class="flex flex-wrap items-end justify-between gap-6">
    <div class="flex flex-col">
      <p class="text-11 text-text-3">Factory</p>
      <h1 class="mt-2 text-26 text-text-1">Health</h1>
      <p class="mt-3 text-15 text-text-2">
        {#if !liveMode}
          The health read could not be taken — is <span class="font-mono">kew serve</span> running?
        {:else if health !== null}
          {healthHeadline(health)}
        {/if}
      </p>
    </div>
    <!-- Below 640 the chip and Refresh share a line and the data's age takes its
         own: in one row the chip wrapped to three lines inside its pill (#2849). -->
    <div class="flex flex-wrap items-center gap-x-4 gap-y-2">
      <button
        type="button"
        class={chipClasses(onlyProblems, 'whitespace-nowrap')}
        aria-pressed={onlyProblems}
        onclick={() => (onlyProblems = !onlyProblems)}
      >
        {onlyProblems ? 'showing problems only' : 'show problems only'}
      </button>
      <!-- The refresh control carries the data's age, because the page does not
           poll and must not imply that it does. -->
      <button
        type="button"
        class="min-h-8 rounded-[4px] border border-hair-strong px-4 text-13 text-text-1 hover:border-text-5 hover:bg-ink-selected disabled:opacity-45 sm:min-h-0"
        disabled={refreshing}
        onclick={refresh}
      >
        {refreshing ? 'refreshing…' : 'Refresh'}
      </button>
      <span class="text-13 text-text-3 max-sm:basis-full">{ageLabel}</span>
    </div>
  </header>

  {#if warnings.length > 0}
    <!-- In place, as a band on the page: no toast, no modal. Each line links to
         the section that explains it. -->
    <section aria-label="What is down" class="flex flex-col border-y border-hair py-3">
      {#each warnings as warning (warning.kind + warning.label)}
        <a
          href={warning.anchor}
          class="flex items-center gap-2.5 py-2.5 text-13 text-text-1 hover:text-text-2 sm:py-1.5"
        >
          <StatusMark kind={STATE_MARK.required} vocabulary="run" size={12} />
          <span>{warning.label}</span>
        </a>
      {/each}
    </section>
  {/if}

  <section id="runtimes" aria-label="Container runtimes" class="flex flex-col scroll-mt-8">
    <SectionHead label="Container runtimes" note={observedLabel} />
    {#if shownRuntimes.length === 0}
      <p class="py-4 text-13 text-text-3">{emptyLine(runtimes.length, 0, 'runtime')}</p>
    {:else}
      <div class={headRow2}>
        <span>runtime</span><span class="text-right">state</span>
      </div>
      {#each shownRuntimes as runtime (runtime.name)}
        <div class={row2}>
          <span class="truncate text-13 text-text-1">
            {runtime.name}
            <span class="ml-2 text-13 text-text-4">
              {runtime.configured ? 'the sandbox runtime' : runtime.backed ? 'available' : 'no backend in this build'}
            </span>
          </span>
          <span class="flex justify-end">
            <StatusMark
              kind={STATE_MARK[runtimeState(runtime)]}
              vocabulary="run"
              word
              label={STATE_LABEL[runtimeState(runtime)]}
              size={12}
              class="text-11"
            />
          </span>
        </div>
      {/each}
    {/if}
  </section>

  <section id="images" aria-label="Agent images" class="flex flex-col scroll-mt-8">
    <SectionHead label="Agent images" note="only a routed image counts as a fault" />
    {#if shownImages.length === 0}
      <p class="py-4 text-13 text-text-3">{emptyLine(images.length, 0, 'image')}</p>
    {:else}
      <div class={headRow3}>
        <span>runner</span><span>image</span><span class="text-right">state</span>
      </div>
      {#each shownImages as image (image.runner)}
        <div class={idRow}>
          <!-- A runner id as typed: data-13-mono, --text-3, never cut; it
               wraps inside its row and the row grows (#2952). -->
          <span class="flex min-w-0 flex-col sm:flex-row sm:flex-wrap sm:items-baseline sm:gap-x-2">
            <span class="min-w-0 font-mono text-13 text-text-3 wrap-anywhere">{image.runner}</span>
            <span class="text-11 text-text-4">
              {image.routed ? 'routed' : 'not routed'}
            </span>
          </span>
          <span class="truncate font-mono text-13 text-text-2">
            {image.ref ?? 'none mapped'}
            <!-- A config key (`images.claude-code`) as typed: mono, never
                 uppercased by a label face (#2845). -->
            <span class="ml-2 text-13 text-text-3">{image.source}</span>
          </span>
          <span class="flex justify-end">
            <StatusMark
              kind={STATE_MARK[imageState(image)]}
              vocabulary="run"
              word
              label={image.present === false && image.routed
                ? 'missing'
                : STATE_LABEL[imageState(image)]}
              size={12}
              class="text-11"
            />
          </span>
        </div>
        <!-- D41 (#3470) — a missing routed image says how to get it: the
             readiness check's own build command with Copy, or, for an image kew
             cannot build, the check's own sentence. -->
        {@const repair = imageRepair(image, readiness)}
        {#if repair !== null}
          <div class="flex flex-col gap-1.5 border-b border-hair-quiet py-2.5" data-image-repair>
            {#if repair.kind === 'command'}
              <!-- Where the doctor says to run it: the build reads `docker/`. -->
              <p class="m-0 text-13 text-text-3" data-command-where>Run it from a kew checkout:</p>
              <div
                class="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1.5 rounded-[7px] border border-hair-box bg-ink-raised px-3 py-2"
                data-command-block
              >
                <span aria-hidden="true" class="shrink-0 font-mono text-13 text-text-5">$</span>
                <span class="min-w-0 flex-1 font-mono text-13 text-text-1 wrap-anywhere"
                  ><Command command={repair.command} /></span
                >
                <CopyButton
                  text={repair.command}
                  label={`Copy the build command for ${image.ref}`}
                  class="shrink-0 rounded-[7px]"
                />
              </div>
            {:else}
              <p class="m-0 text-13 text-text-3">{repair.hint}</p>
            {/if}
          </div>
        {/if}
      {/each}
    {/if}
  </section>

  <section id="posture" aria-label="Lane posture" class="flex flex-col scroll-mt-8">
    <SectionHead label="Posture by lane" note="as dispatch itself resolves it" />
    {#if shownLanes.length === 0}
      <p class="py-4 text-13 text-text-3">{emptyLine(lanes.length, 0, 'lane')}</p>
    {:else}
      <div class={headRow3}>
        <span>lane</span><span>containment</span><span class="text-right">state</span>
      </div>
      {#each shownLanes as lane (lane.lane)}
        <div class={idRow}>
          <!-- A runner id as typed: data-13-mono, never a label face, and never
               cut — on phones it sits under the lane; wider, it sits beside the
               lane while it fits and wraps under it when it does not (#2897). -->
          <span class="flex min-w-0 flex-col text-13 sm:flex-row sm:flex-wrap sm:items-baseline sm:gap-x-2">
            <span class="truncate text-text-1">{lane.lane}</span>
            <span class="min-w-0 font-mono text-13 text-text-3 wrap-anywhere">{lane.runner}</span>
          </span>
          <!-- #2536 — the cell is one line of readouts; a refusal's reason and
               the console's own refusal are sentences, so they sit under the
               row behind `why ▸` rather than in uppercase beside it. The
               readouts wrap rather than truncate, so a credential's words are
               never the part a narrow cell cuts (#3470). -->
          <span class="flex min-w-0 items-baseline gap-3">
            <span class="flex min-w-0 flex-wrap items-baseline gap-x-2 text-13 text-text-2" data-lane-posture>
              <span class="min-w-0 wrap-anywhere"
                >{lane.refusal !== null ? 'cannot run' : `${lane.posture} · ${lane.runtime} · egress ${lane.egress}`}</span
              >
              {#if credentialWords(lane) !== ''}
                <span class="text-13 text-text-4" data-lane-credential>{credentialWords(lane)}</span>
              {/if}
              <!-- Every red word here is one the headline names (#2849). -->
              {#each laneDangers(lane) as word (word)}
                <span class="text-11 text-danger">{word}</span>
              {/each}
              {#if lane.console_refusal}
                <span class="text-11 text-text-4">loops only</span>
              {/if}
            </span>
            {#if laneWhy(lane).length > 0}
              <Why
                bind:open={() => whyOpen[`posture-${lane.lane}`] ?? false,
                (v) => (whyOpen[`posture-${lane.lane}`] = v)}
                controls={`posture-why-${lane.lane}`}
              />
            {/if}
          </span>
          <span class="flex justify-end">
            <StatusMark
              kind={STATE_MARK[postureState(lane)]}
              vocabulary="run"
              word
              label={lane.refusal !== null
                ? 'refused'
                : lane.contained
                  ? 'contained'
                  : 'uncontained'}
              size={12}
              class="text-11"
            />
          </span>
        </div>
        {#if laneWhy(lane).length > 0}
          <ul
            id={`posture-why-${lane.lane}`}
            hidden={!whyOpen[`posture-${lane.lane}`]}
            class="flex flex-col gap-1 border-b border-hair-quiet py-2.5 text-13 text-text-2"
          >
            {#each laneWhy(lane) as line (line)}<li>{line}</li>{/each}
          </ul>
        {/if}
      {/each}
    {/if}
  </section>

  <section id="runners" aria-label="Runners" class="flex flex-col scroll-mt-8">
    <SectionHead label="Runners and credentials" note="names only — no value is ever read" />
    {#if shownRunners.length === 0}
      <p class="py-4 text-13 text-text-3">{emptyLine(runners.length, 0, 'runner')}</p>
    {:else}
      <div class={headRow3}>
        <span>runner</span><span>credentials</span><span class="text-right">state</span>
      </div>
      {#each shownRunners as runner (runner.name)}
        <div class={idRow}>
          <!-- A runner id as typed: data-13-mono, --text-3, never cut (#2952). -->
          <span class="flex min-w-0 flex-col sm:flex-row sm:flex-wrap sm:items-baseline sm:gap-x-2">
            <span class="min-w-0 font-mono text-13 text-text-3 wrap-anywhere">{runner.name}</span>
            <span class="text-11 text-text-4">
              {[...runner.routed_tiers, runner.review_lane ? 'review' : '']
                .filter(Boolean)
                .join(', ') || 'no lane'}
              {#if runner.version}· {runner.version}{/if}
            </span>
          </span>
          <span class="truncate text-13 text-text-2">
            {#if runner.credential_names.length === 0}
              <span class="text-text-4">declares none</span>
            {:else}
              {#each runner.credential_names as cred (cred.name)}
                <span class="mr-3 font-mono text-13" class:text-text-4={!cred.set}>
                  {cred.name}
                  <span class="text-11">{cred.set ? 'set' : 'not set'}</span>
                </span>
              {/each}
              {#if runner.credential_note}
                <!-- Missing from kew serve's own environment only: a run on
                     this runner succeeded in the window (#2566). A note. -->
                <span class="text-13 text-text-3" data-credential-note>
                  {runner.credential_note}
                </span>
              {/if}
            {/if}
          </span>
          <span class="flex justify-end">
            <StatusMark
              kind={STATE_MARK[runnerState(runner)]}
              vocabulary="run"
              word
              label={runner.binary_present === false
                ? 'not on PATH'
                : STATE_LABEL[runnerState(runner)]}
              size={12}
              class="text-11"
            />
          </span>
        </div>
      {/each}
    {/if}
  </section>

  <section id="trackers" aria-label="Trackers" class="flex flex-col scroll-mt-8">
    <SectionHead label="Trackers" note="from the sweep's cached reading — no call on this page" />
    {#if health === null}
      <p class="py-4 text-13 text-text-3">The tracker reading could not be taken.</p>
    {:else}
      <div class={row3}>
        <span class="text-13 text-text-1">GitHub</span>
        <span class="truncate text-13 text-text-2">
          {#if health.trackers.github.reachable === null}
            no reading yet — the serve sweep takes one each cycle
          {:else}
            {health.trackers.github.auth_ok === false
              ? 'the credential is not working'
              : `auth ok · scopes ${health.trackers.github.scopes.join(', ') || 'none reported'}`}
            {#if health.trackers.github.rate}
              · core {health.trackers.github.rate.remaining}/{health.trackers.github.rate.limit}
            {/if}
            · read {heartbeatLabel(
              health.trackers.github.observed_at,
              health.trackers.github.observed_age_s
            )}
          {/if}
        </span>
        <span class="flex justify-end">
          {#if health.trackers.github.auth_ok === true}
            <StatusMark kind={STATE_MARK.running} vocabulary="run" word label="reachable" size={12} class="text-11" />
          {:else if health.trackers.github.auth_ok === false}
            <StatusMark kind={STATE_MARK.required} vocabulary="run" word label="auth failing" size={12} class="text-11" />
          {:else}
            <StatusMark kind={STATE_MARK.unknown} vocabulary="run" word label="unknown" size={12} class="text-11" />
          {/if}
        </span>
      </div>
      <div class={row3}>
        <span class="text-13 text-text-1">Linear</span>
        <span class="truncate text-13 text-text-2">
          {#if !health.trackers.linear.configured}
            this project is tracked on GitHub
          {:else}
            {health.trackers.linear.team ? `team ${health.trackers.linear.team}` : 'no team set'} · key {health.trackers.linear.credential_set
              ? 'set'
              : 'not set'}
          {/if}
        </span>
        <span class="flex justify-end">
          <StatusMark
            kind={STATE_MARK[
              !health.trackers.linear.configured
                ? 'not-enabled'
                : health.trackers.linear.reachable === false
                  ? 'required'
                  : 'unknown'
            ]}
            vocabulary="run"
            word
            label={!health.trackers.linear.configured
              ? 'not enabled'
              : health.trackers.linear.reachable === false
                ? 'unreachable'
                : 'not probed'}
            size={12}
            class="text-11"
          />
        </span>
      </div>
    {/if}
  </section>

  <section id="daemons" aria-label="Daemons" class="flex flex-col scroll-mt-8">
    <SectionHead
      label="Daemons"
      note={health === null
        ? ''
        : loopsNote(health.loops.pursuing, health.loops.stale)}
    />
    {#if health === null}
      <p class="py-4 text-13 text-text-3">The daemon reading could not be taken.</p>
    {:else}
      <div class={row3}>
        <span class="text-13 text-text-1">serve sweep</span>
        <span class="truncate text-13 text-text-2">
          {#if !health.sweep.present}
            not running in this serve process
          {:else}
            {health.sweep.interval_s != null ? `every ${health.sweep.interval_s}s` : 'interval not reported'}
            · {health.sweep.last_result != null ? `last cycle ${health.sweep.last_result}` : 'no cycle yet'}
            {#if health.sweep.graphql_calls != null}
              · {health.sweep.graphql_calls} GraphQL {health.sweep.graphql_calls === 1 ? 'call' : 'calls'}
            {/if}
          {/if}
          · heartbeat {heartbeatLabel(health.sweep.heartbeat_at, health.sweep.heartbeat_age_s)}
        </span>
        <span class="flex justify-end">
          <StatusMark
            kind={STATE_MARK[health.sweep.present ? 'running' : 'required']}
            vocabulary="run"
            word
            label={health.sweep.present ? 'running' : 'not running'}
            size={12}
            class="text-11"
          />
        </span>
      </div>
      {#each shownWatchers as watcher (watcher.name)}
        <div class={row3}>
          <span class="truncate text-13 text-text-1">{watcher.name}</span>
          <span class="truncate text-13 text-text-2">
            {watcher.detail}
            {#if watcher.enabled && watcher.last_seen_age_s != null}
              · last seen {relativeAge(watcher.last_seen_age_s)}
            {:else if watcher.enabled && watcher.pollers != null}
              · {watcher.pollers} poller{watcher.pollers === 1 ? '' : 's'}
            {/if}
          </span>
          <span class="flex justify-end">
            <StatusMark
              kind={STATE_MARK[watcherState(watcher)]}
              vocabulary="run"
              word
              label={STATE_LABEL[watcherState(watcher)]}
              size={12}
              class="text-11"
            />
          </span>
        </div>
      {/each}
      {#if shownWatchers.length === 0}
        <p class="py-4 text-13 text-text-3">{emptyLine(watchers.length, 0, 'watcher')}</p>
      {/if}
    {/if}
  </section>

  <section id="drivers" aria-label="Epic drivers" class="flex flex-col scroll-mt-8">
    <SectionHead label="Epic drivers" note="absolute time, with the age beside it" />
    {#if shownDrivers.length === 0}
      <p class="py-4 text-13 text-text-3">{emptyLine(drivers.length, 0, 'epic driver')}</p>
    {:else}
      <div class={headRow3}>
        <span>epic</span><span>last tick</span><span class="text-right">state</span>
      </div>
      {#each shownDrivers as driver (driver.epic_ref)}
        <div class={row3}>
          <span class="truncate text-13 text-text-1">{driver.epic_ref}</span>
          <span class="truncate text-13 text-text-2">
            {heartbeatLabel(driver.last_tick_at, driver.tick_age_s)}
            {#if driver.interval_s != null}
              <span class="ml-2 text-11 text-text-4">every {driver.interval_s}s</span>
            {/if}
            {#if driver.lock.heartbeat_age_s != null}
              <span class="ml-2 text-11 text-text-4">
                lock {relativeAge(driver.lock.heartbeat_age_s)}{driver.lock.pid_alive === false
                  ? ' · holder gone'
                  : ''}
              </span>
            {/if}
          </span>
          <span class="flex justify-end">
            <StatusMark
              kind={STATE_MARK[driverState(driver)]}
              vocabulary="run"
              word
              label={driver.active === false
                ? 'finished'
                : driver.liveness === 'gone'
                  ? 'no heartbeat'
                  : driver.liveness === 'waiting' && driver.waiting_reason
                    ? `waiting: ${driver.waiting_reason}`
                    : driver.liveness}
              size={12}
              class="text-11"
            />
          </span>
        </div>
      {/each}
    {/if}
  </section>

  <section id="budgets" aria-label="Budgets and caps" class="flex flex-col scroll-mt-8">
    <SectionHead
      label="Budgets and caps"
      note={config === null ? 'not served' : `read-only · ${config.config_path ?? 'defaults only'}`}
    />
    {#if budgetRows.length === 0}
      <p class="py-4 text-13 text-text-3">
        {config === null
          ? 'The configuration read could not be taken.'
          : 'No caps configured.'}
      </p>
    {:else}
      <div class={headRow3}>
        <span>field</span><span>value</span><span class="text-right">source</span>
      </div>
      {#if config?.human_review_unavailable}
        <!-- Why the three `human_review.*` thresholds read off the config layer
             rather than off the durable policy. Said in place, as a sentence. -->
        <p class="flex items-baseline gap-3 py-2.5 text-13 text-text-3">
          No human-review policy to read
          <Why
            bind:open={() => whyOpen['human-review'] ?? false, (v) => (whyOpen['human-review'] = v)}
            controls="human-review-why"
          />
        </p>
        <p id="human-review-why" hidden={!whyOpen['human-review']} class="pb-2.5 text-13 text-text-3">
          {config.human_review_unavailable}
        </p>
      {/if}
      {#each budgetRows as { field, entry } (field)}
        <div class={row3}>
          <span class="truncate font-mono text-13 text-text-2">{field}</span>
          <span class="truncate font-mono text-13 text-text-1">
            {fieldValueLabel(entry?.value)}
            {#if entry?.over_cap}
              <span class="ml-2 text-11 text-warn">under its own occupancy</span>
            {/if}
          </span>
          <!-- A file or key (`kew.toml`), as typed: never uppercased (#2845). -->
          <span class="truncate text-right font-mono text-13 text-text-4">{entry?.source}</span>
        </div>
      {/each}
    {/if}
  </section>

  <section id="env" aria-label="Environment" class="flex flex-col scroll-mt-8">
    <SectionHead
      label="Environment"
      note={env === null ? 'not served' : `as seen by ${env.as_seen_by}`}
    />
    {#if shownEnv.length === 0}
      <p class="py-4 text-13 text-text-3">
        {env === null
          ? 'The environment read could not be taken.'
          : emptyLine(envVars.length, 0, 'variable')}
      </p>
    {:else}
      <div class={headRow3}>
        <span>name</span><span>what it is for</span><span class="text-right">set</span>
      </div>
      {#each shownEnv as variable (variable.name)}
        <div class={row3}>
          <span class="truncate font-mono text-13 text-text-1">{variable.name}</span>
          <span class="truncate text-13 text-text-2">
            {variable.purpose}
            <span class="ml-2 text-13 text-text-4">{variable.required_by}</span>
          </span>
          <span class="flex justify-end">
            <StatusMark
              kind={STATE_MARK[variable.set ? 'running' : 'not-enabled']}
              vocabulary="run"
              word
              label={variable.set ? 'set' : 'not set'}
              size={12}
              class="text-11"
            />
          </span>
        </div>
      {/each}
    {/if}
  </section>
</div>
