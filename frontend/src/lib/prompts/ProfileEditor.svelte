<script lang="ts">
  /**
   * A role's operator instructions on the Prompts page (#3025): the active
   * addendum, a pending proposal with Approve and Decline, a box to propose a
   * change, a version history, and Retire.
   *
   * Every write sends what this read showed: an approve or a retire carries
   * the `generation` of the last versions read, never one read again just
   * before the call, so a decision another approver made in between answers
   * 409 rather than being overwritten. A refusal prints as a sentence and
   * leaves the shown history as it was; a success replaces it with the
   * server's answer and calls `onchange` so the page re-reads its preview.
   * A write with no readable answer may have landed: the pane says its result
   * is unknown, never that nothing changed, and every write stays off until a
   * Reload reads the history again, so a lost answer is never retried into a
   * second proposal.
   * Each write names the role, version and generation of the history on
   * screen, and only the newest read for the role still shown lands
   * (`readSequence`), so a slow answer for a role the pane has left never
   * becomes the history another role's buttons act on. The active version's
   * audit event links to `/audit?event=<id>`, which shows that event.
   *
   * Approve and Decline are this editor's only magenta: a pending proposal is
   * a place a person is the next move — and only for a viewer the approver
   * gate would let decide (`can_decide`), in a pane with nothing to reload
   * (#3092). The page's other magenta is a refused profile's needs-you block
   * above the preview, which reads `can_decide` and `decide_refusal` from
   * this read (`decider`) and links here (`EDITOR_ANCHOR`, #3075).
   * Every write passes that gate, Propose too, so a viewer who cannot
   * decide has every write off and reads why once, beside Retire: the
   * server's sentence when it names a fix (`decide_refusal`), else the
   * approver sentence (#3119). A pane that must reload says so once, above
   * the pending versions, and each pending block reads only "Reload to decide."
   *
   * Every decision acts only on the state on screen (#3092): every pending
   * version is offered, not only the newest; approving one older than the
   * active version asks first, naming both; a 409 on any write locks every
   * write until a Reload reads the history and the preview coherently; and a
   * confirmation armed before a read never survives it, so it cannot fire at a
   * version the person never chose.
   */
  import {
    approveProfile,
    declineProfile,
    fetchProfileVersions,
    proposeProfile,
    retireProfile,
    type ProfileVersions,
    type ProfileWriteResponse
  } from '$lib/api';
  import { buttonClasses } from '$lib/factory/classes';
  import { fmtDateTime, utcTitle } from '$lib/time';
  import { SectionHead } from '$lib/ui';
  import { readFailure } from '$lib/work/evidence';
  import { untrack } from 'svelte';
  import {
    bodyLength,
    coherence,
    configRuns,
    decisionSlot,
    EDITOR_ANCHOR,
    olderApproval,
    pendingVersions,
    PROFILE_CHANGED,
    readSequence,
    readWasStale,
    RELOAD_TO_DECIDE,
    WRITE_OUTCOME_UNKNOWN,
    writeBlock,
    writeNeedsReload,
    writeRefusal,
    type Decider,
    type ProfileWrite
  } from './profile-state';

  let {
    role,
    onchange,
    previewGeneration,
    previewPending,
    historyGeneration = $bindable(),
    decider = $bindable()
  }: {
    role: string;
    /** Asks the page to read its preview again; it marks that read pending at once. */
    onchange: () => void;
    /**
     * The profile generation of the preview on screen beside this pane:
     * undefined when none is shown, null when the server could not settle it.
     */
    previewGeneration: number | null | undefined;
    /** A preview read is in flight. */
    previewPending: boolean;
    /**
     * Out: the generation of the history on screen, for the outcomes table
     * beside it to match. Undefined while a history read is in flight, null
     * when no history could be read.
     */
    historyGeneration?: number | null;
    /**
     * Out: what the shown history read says of the viewer — `can_decide` and
     * `decide_refusal` — for the page's refused-profile block (#3075), so the
     * page makes no second read. Undefined while no history of the role is
     * shown.
     */
    decider?: Decider;
  } = $props();

  let versions = $state<ProfileVersions | null>(null);
  let failure = $state<string | null>(null);
  /** The last read answered 409: the role kept changing while it was read. */
  let stale = $state(false);
  let draft = $state('');
  let busy = $state(false);
  let refusal = $state<string | null>(null);
  let confirmRetire = $state(false);
  /** The older version whose approval waits on its confirm step, if any. */
  let confirmApprove = $state<number | null>(null);
  /**
   * A write got no readable answer, so it may have landed, or answered 409,
   * so the role moved under it. Every write stays off until a versions read
   * succeeds and shows the role as it now is; the draft is kept, so a person
   * who sees no new version can send it again.
   */
  let mustReload = $state(false);
  /** History reads in flight, whichever of them will land. */
  let inflight = $state(0);
  /** Re-reads made because the preview and the history disagreed. */
  let attempts = $state(0);
  /** They still disagreed after every re-read: writes stay off until Reload. */
  let diverged = $state(false);

  /**
   * Every read and write takes a ticket, so only the newest answer for the
   * role still shown lands: a slow reload of one role that finishes after the
   * pane moved to another never replaces the other's history.
   */
  const reads = readSequence();

  /** Every armed confirmation, dropped: it was armed against an earlier read. */
  function disarm() {
    confirmRetire = false;
    confirmApprove = null;
  }

  function read(of: string) {
    failure = null;
    stale = false;
    disarm();
    const fresh = reads.begin(of);
    inflight += 1;
    fetchProfileVersions(of)
      .then((v) => {
        if (!fresh(role)) return;
        versions = v;
        mustReload = false;
        disarm();
      })
      .catch((err) => {
        if (!fresh(role)) return;
        if (readWasStale(err)) {
          versions = null;
          stale = true;
        } else {
          failure = readFailure(err, `The ${of} profile history`);
        }
      })
      .finally(() => {
        inflight -= 1;
      });
  }

  $effect(() => {
    const of = role;
    versions = null;
    refusal = null;
    draft = '';
    mustReload = false;
    // Untracked: the read counts itself in `inflight`, which this effect must
    // not re-run on.
    untrack(() => {
      attempts = 0;
      diverged = false;
      read(of);
    });
  });

  /**
   * The history on screen, only while it is the shown role's: every control
   * reads its role, version and generation from this, never from `role`, so
   * no write can pair one role with another role's history.
   */
  const shown = $derived(versions !== null && versions.role === role ? versions : null);
  $effect(() => {
    if (inflight > 0) historyGeneration = undefined;
    else if (shown !== null) historyGeneration = shown.generation;
    else historyGeneration = failure !== null || stale ? null : undefined;
  });
  $effect(() => {
    decider = shown === null ? undefined : { canDecide: shown.can_decide, decideRefusal: shown.decide_refusal };
  });
  const active = $derived(shown?.versions.find((v) => v.state === 'active') ?? null);
  const pending = $derived(shown === null ? [] : pendingVersions(shown.versions));
  const limit = $derived(shown?.max_body_chars ?? 4000);
  const count = $derived(bodyLength(draft));
  /**
   * Whether the history may be shown beside the preview on screen: only when
   * both name one profile generation. Otherwise neither the history nor a
   * control is drawn, the two are read again a bounded number of times, and
   * then the pane says the profile changed until a Reload.
   */
  const settle = $derived(
    coherence({
      preview: previewGeneration,
      history: shown?.generation,
      pending: previewPending || inflight > 0,
      attempts
    })
  );
  $effect(() => {
    const next = settle;
    untrack(() => {
      if (next === 'agree') attempts = 0;
      else if (next === 'diverged') diverged = true;
      else if (next === 'reread') {
        attempts += 1;
        read(role);
        onchange();
      }
    });
  });
  const agreed = $derived(settle === 'agree' && !diverged);

  /**
   * Every write control is off while one is in flight, while a refusal asks
   * for a reload, and while the history and the preview disagree.
   */
  const locked = $derived(busy || mustReload || !agreed);
  /** The viewer passes the approver gate, as the history read says: every write is theirs. */
  const canDecide = $derived(shown?.can_decide === true);
  const canPropose = $derived(!locked && canDecide && shown !== null && count > 0 && count <= limit);
  /** Why no write is the viewer's, printed once for the write area; null when every write is. */
  const writeReason = $derived(writeBlock({ canDecide, decideRefusal: shown?.decide_refusal }));
  /** What each pending block draws where Approve and Decline go. */
  const slot = $derived(decisionSlot({ canDecide, mustReload }));

  async function write(kind: ProfileWrite, call: (of: ProfileVersions) => Promise<ProfileWriteResponse>) {
    const of = shown;
    if (of === null || locked || !of.can_decide) return;
    const fresh = reads.begin(of.role);
    busy = true;
    refusal = null;
    const answer = await call(of);
    busy = false;
    // An answer for a role the pane has since left, or one a later read has
    // overtaken, is not this pane's to show.
    if (!fresh(role)) return;
    if (!answer.ok) {
      // Kept: the draft, so a person who reloads and sees no new version can
      // send it again on purpose.
      mustReload = writeNeedsReload(answer.status);
      refusal = writeRefusal(kind, answer.status, answer.detail);
      disarm();
      return;
    }
    versions = answer.data;
    disarm();
    if (kind === 'propose') draft = '';
    onchange();
  }

  const propose = () => write('propose', (of) => proposeProfile(of.role, draft));
  const approve = (version: number) =>
    write('approve', (of) => approveProfile(of.role, version, of.generation));
  /** Approve, or first ask when `version` is older than the active one. */
  function askApprove(version: number) {
    if (olderApproval(version, active?.version ?? null) === null) approve(version);
    else confirmApprove = version;
  }
  const decline = (version: number) => write('decline', (of) => declineProfile(of.role, version));
  const retire = () => write('retire', (of) => retireProfile(of.role, of.generation));

  function reload() {
    refusal = null;
    disarm();
    attempts = 0;
    diverged = false;
    read(role);
    onchange();
  }

  const countLabel = (n: number) => (n === 1 ? '1 version' : `${n} versions`);

  /** A magenta control: a person is the next move. */
  const MAGENTA = 'min-h-8 rounded-[4px] px-4 font-mono';
  const QUIET =
    'min-h-8 font-mono text-13 text-text-3 underline-offset-4 hover:text-text-1 hover:underline disabled:opacity-45';
</script>

<section
  id={EDITOR_ANCHOR}
  class="mt-6 min-w-0 scroll-mt-4 border-t border-hair pt-5"
  aria-label="Operator instructions"
  data-profile-editor
>
  <SectionHead
    label="Operator instructions"
    meta={shown && agreed ? countLabel(shown.versions.length) : ''}
  />

  {#if failure !== null}
    <p class="text-15 text-text-2" data-profile-failure>{failure}, so its operator instructions cannot be shown.</p>
  {:else if stale || diverged}
    <!-- A history that never settled, or one that never matched the preview
         beside it, is not shown at all: no controls, so no write can carry a
         generation from a view of two decisions. -->
    <p class="text-15 text-text-1" role="alert" data-profile-refusal>
      {PROFILE_CHANGED}
      <button type="button" class={QUIET} onclick={reload} data-profile-reload>Reload</button>
    </p>
  {:else if shown === null || !agreed}
    <p class="text-15 text-text-3">Reading the {role} profile history…</p>
  {:else}
    <p class="text-13 break-words text-text-2" data-active-version={active?.version ?? 'none'}>
      {#if active}
        Active: <span class="font-mono text-text-1">v{active.version}</span>, approved by
        <span class="font-mono">{active.decided_by}</span>
        <time datetime={active.decided_at} title={utcTitle(active.decided_at)}>{fmtDateTime(active.decided_at)}</time
        >{#if active.audit_event_id !== null}, <a
            href="/audit?event={active.audit_event_id}"
            class="inline-flex min-h-8 items-center font-mono text-text-1 underline underline-offset-4"
            data-audit-link>audit event #{active.audit_event_id}</a
          >{/if}.
      {:else}
        No addendum is active for this role.
      {/if}
    </p>

    {#if refusal !== null}
      <p class="mt-3 text-15 text-text-1" role="alert" data-profile-refusal>
        {refusal}
        {#if refusal === PROFILE_CHANGED || refusal === WRITE_OUTCOME_UNKNOWN}
          <button type="button" class={QUIET} onclick={reload} data-profile-reload>Reload</button>
        {/if}
      </p>
    {/if}

    {#each pending as p (p.version)}
      <div class="mt-4 min-w-0 rounded-[4px] border border-hair-box px-4 py-3" data-pending={p.version}>
        <p class="text-13 break-words text-text-2">
          <span class="font-mono text-text-1">v{p.version}</span> was proposed by
          <span class="font-mono">{p.proposed_by}</span>
          <time datetime={p.proposed_at} title={utcTitle(p.proposed_at)}>{fmtDateTime(p.proposed_at)}</time> and waits
          for an approver.
        </p>
        <div
          class="mt-2 font-mono text-13 break-words whitespace-pre-wrap text-text-2 [overflow-wrap:anywhere]"
          data-pending-body
        >
          {p.body}
        </div>
        {#if slot === 'reload'}
          <p class="mt-3 text-13 text-text-2" data-decision-reason>{RELOAD_TO_DECIDE}</p>
        {:else if slot === 'empty'}
          <!-- The write area's one reason, beside Retire, says why. -->
        {:else if confirmApprove === p.version}
          <p class="mt-3 text-13 text-text-1" data-approve-confirm>
            {olderApproval(p.version, active?.version ?? null)}
          </p>
          <div class="mt-2 flex flex-wrap gap-3">
            <button
              type="button"
              disabled={locked}
              class={buttonClasses({ variant: 'needs-you', size: 'sm', class: MAGENTA })}
              onclick={() => approve(p.version)}
              data-approve-yes>Approve v{p.version}</button
            >
            <button type="button" class={QUIET} onclick={() => (confirmApprove = null)} data-approve-no
              >Keep v{active?.version}</button
            >
          </div>
        {:else}
          <div class="mt-3 flex flex-wrap gap-3">
            <button
              type="button"
              disabled={locked}
              class={buttonClasses({ variant: 'needs-you', size: 'sm', class: MAGENTA })}
              onclick={() => askApprove(p.version)}
              data-approve>Approve v{p.version}</button
            >
            <button
              type="button"
              disabled={locked}
              class={buttonClasses({
                variant: 'ghost',
                size: 'sm',
                class: `${MAGENTA} border border-accent text-accent hover:bg-accent-wash hover:text-accent`
              })}
              onclick={() => decline(p.version)}
              data-decline>Decline</button
            >
          </div>
        {/if}
      </div>
    {/each}

    <label class="mt-5 block text-13 text-text-2" for="profile-draft-{role}">Propose a change</label>
    <textarea
      id="profile-draft-{role}"
      bind:value={draft}
      rows="5"
      class="mt-2 block w-full min-w-0 rounded-[4px] border border-hair-box bg-ink-raised px-3 py-2 font-mono text-13 text-text-1"
      placeholder="Instructions added under the {role} prompt, above its safety rules."
      data-profile-draft
    ></textarea>
    <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2">
      <span class="font-mono text-11 tracking-normal normal-case" class:text-text-3={count <= limit}
        class:text-danger={count > limit} data-profile-count>{count} / {limit}</span
      >
      <button
        type="button"
        disabled={!canPropose}
        class={buttonClasses({ variant: 'factory', size: 'sm', class: 'min-h-8 px-4' })}
        onclick={propose}
        data-propose>Propose</button
      >
      {#if active}
        {#if confirmRetire && canDecide}
          <span class="text-13 text-text-2" data-retire-confirm>
            Retire <span class="font-mono">v{active.version}</span>? The role runs its base prompt again.
          </span>
          <button
            type="button"
            disabled={locked || !canDecide}
            class={QUIET}
            onclick={retire}
            data-retire-yes>Retire it</button
          >
          <button type="button" class={QUIET} onclick={() => (confirmRetire = false)} data-retire-no>Keep it</button>
        {:else}
          <button
            type="button"
            disabled={locked || !canDecide}
            class={QUIET}
            onclick={() => (confirmRetire = true)}
            data-retire
            >Retire v{active.version}</button
          >
        {/if}
      {/if}
      {#if writeReason !== null}
        <span class="min-w-0 text-13 break-words text-text-2" data-write-reason
          >{#each configRuns(writeReason) as run, i (i)}{#if run.mono}<span class="font-mono">{run.text}</span
              >{:else}{run.text}{/if}{/each}</span
        >
      {/if}
    </div>

    <h3 class="mt-6 text-11 tracking-[0.08em] text-text-3 uppercase">Version history</h3>
    {#if shown.versions.length === 0}
      <p class="mt-2 text-13 text-text-3" data-history-empty>No version has been proposed for this role yet.</p>
    {:else}
      <ol class="mt-2 min-w-0" data-history>
        {#each shown.versions as v (v.version)}
          <li
            class="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-hair-quiet py-2 text-13"
            data-history-version={v.version}
            data-state={v.state}
          >
            <span class="font-mono text-text-1">v{v.version}</span>
            <!-- One colour class only: with both, the stylesheet's order, not this markup, picks the colour. -->
            <span data-status-word class="font-mono {v.state === 'active' ? 'text-accent-2' : 'text-text-2'}"
              >{v.state}</span
            >
            <!-- A proposed row's state word already says "proposed": the line goes on "by …", once. -->
            <span class="min-w-0 break-words text-text-3" data-history-by>
              {#if v.state === 'proposed'}by <span class="font-mono">{v.proposed_by}</span>
                <time datetime={v.proposed_at} title={utcTitle(v.proposed_at)}>{fmtDateTime(v.proposed_at)}</time
                >{:else}proposed by <span class="font-mono">{v.proposed_by}</span>{#if v.decided_by}, decided by
                  <span class="font-mono">{v.decided_by}</span>
                  <time datetime={v.decided_at} title={utcTitle(v.decided_at)}>{fmtDateTime(v.decided_at)}</time
                  >{/if}{/if}
            </span>
            <span class="font-mono text-11 tracking-normal text-text-3 normal-case">{v.body_sha256.slice(0, 7)}</span>
          </li>
        {/each}
      </ol>
    {/if}
  {/if}
</section>
