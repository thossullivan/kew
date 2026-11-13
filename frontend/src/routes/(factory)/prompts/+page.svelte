<script lang="ts">
  /**
   * `/prompts` — which prompt each agent role runs (#3019).
   *
   * The left column lists every role `GET /api/prompts` serves, with
   * its run kinds and where its base prompt comes from; the right pane renders
   * the selected role's prompt over the fixed sample item `GET
   * /api/prompts/{role}` uses, so no real item's content is on this page: the
   * base, plus the role's active operator profile when a launch would render
   * it, and a sentence saying which of those it is (#3023). Each
   * `## ` section is a disclosure button (not a `<details>`, which is the app's
   * menu primitive) and only the first is open: the reviewer's
   * prompt runs to hundreds of lines.
   *
   * The selection is `?role=`, written with `replaceState` and read from
   * `page.state` first: shallow routing never moves `page.url` (#2248).
   *
   * A role an operator profile applies to also carries its operator
   * instructions (#3025, `ProfileEditor`): propose, approve, decline and
   * retire. A decision there re-reads the preview, and the preview marks the
   * addendum block with a cyan left rule: the span `addendum` in the preview's
   * answer, where the builder placed it, never a heading guessed at here.
   *
   * Above the editor, `ProfileOutcomes` (#3026) tables what each version of
   * the role's prompt did: one row per version, newest first. A decision in
   * the editor re-reads it too (`reads`), so it never shows the states the
   * decision replaced, and it draws a table only from the generation of the
   * editor's history (`historyGeneration`); when the two differ it asks for
   * the page to be read again (`rereadPreview`).
   */
  import { untrack } from 'svelte';
  import { page } from '$app/state';
  import { replaceState } from '$app/navigation';
  import { fetchPromptRole, type PromptPreview } from '$lib/api';
  import { dataRowClasses, TAP_INLINE } from '$lib/factory/classes';
  import NeedsYouBlock from '$lib/needs-you/NeedsYouBlock.svelte';
  import ProfileEditor from '$lib/prompts/ProfileEditor.svelte';
  import ProfileOutcomes from '$lib/prompts/ProfileOutcomes.svelte';
  import { profileState, refusedProfile, type Decider } from '$lib/prompts/profile-state';
  import { changeLine, SHORT_SHA, sourceChip } from '$lib/prompts/source-chip';
  import { previewBlocks } from '$lib/prompts/sections';
  import { SectionHead, Why } from '$lib/ui';
  import { readFailure } from '$lib/work/evidence';
  import type { PromptsPageData } from './+page';

  let { data }: { data: PromptsPageData } = $props();

  const roles = $derived(data.roles ?? []);
  const selected = $derived(
    page.state.promptRole ?? page.url.searchParams.get('role') ?? roles[0]?.role ?? null
  );
  const known = $derived(roles.find((r) => r.role === selected) ?? null);

  let preview = $state<PromptPreview | null>(null);
  let previewFailure = $state<string | null>(null);
  let pane = $state<HTMLElement | null>(null);
  /** Which `## ` sections are open; a fresh preview opens only the first. */
  let open = $state<boolean[]>([]);
  /** Bumped by a profile decision, so the preview and the outcomes are read again. */
  let reads = $state(0);
  /**
   * A preview read is in flight. Set at once when the editor asks for one, so
   * the editor never compares its new history with the preview it replaces.
   */
  let previewPending = $state(false);
  /** The generation of the history the editor shows, which the outcomes table must match. */
  let historyGeneration = $state<number | null | undefined>(undefined);
  /** What the editor's history read says of the viewer, for a refused profile's lead (#3075). */
  let decider = $state<Decider | undefined>(undefined);

  function rereadPreview() {
    previewPending = true;
    reads += 1;
  }

  $effect(() => {
    const role = known?.role;
    void reads;
    // A re-read of the role already shown keeps it on screen until the new
    // read lands; another role starts from nothing.
    const fresh = untrack(() => preview?.role) !== role;
    if (fresh) preview = null;
    previewFailure = null;
    previewPending = !!role;
    if (!role) return;
    let current = true;
    fetchPromptRole(role)
      .then((read) => {
        if (!current) return;
        // A first read opens only the first section; a re-read after a profile
        // decision opens the addendum too, so the change is in view.
        const blocks = previewBlocks(read.preview, read.addendum);
        const first = blocks.findIndex((b) => b.kind === 'section');
        open = blocks.map((b, i) => i === first || (!fresh && b.addendum));
        preview = read;
      })
      .catch((err) => {
        if (current) previewFailure = readFailure(err, `The ${role} preview`);
      })
      .finally(() => {
        if (current) previewPending = false;
      });
    return () => {
      current = false;
    };
  });

  /**
   * Whether the preview drawn is this role's: its profile generation is then
   * what the editor's history must match before either is shown as a pair.
   */
  const previewOnScreen = $derived(preview !== null && previewFailure === null && preview.role === known?.role);

  /** The preview cut at its headings, the addendum block where the server placed it. */
  const blocks = $derived(preview ? previewBlocks(preview.preview, preview.addendum) : null);
  /**
   * The pane's source: the preview's own once it loads, since that is the one
   * read with the bytes it rendered — the catalog read the source separately,
   * and a copy deployed between the two reads would mislabel them.
   */
  const shown = $derived(preview && preview.role === known?.role ? preview : known);
  const change = $derived(shown ? changeLine(shown) : null);
  /**
   * Why the launch refuses before it builds a prompt: the variant's refusal,
   * which a composer refusal serves beside the base text (#3071), or an empty
   * preview.
   */
  const refusal = $derived.by(() => {
    if (!preview) return null;
    const why = preview.variant.refusal;
    if (typeof why === 'string' && why) return why;
    return preview.preview === '' ? 'The configuration refuses this launch.' : null;
  });
  const variant = $derived(
    preview
      ? Object.entries(preview.variant)
          .filter(([k]) => k !== 'refusal' && k !== 'profile') // each said in its own sentence
          .map(([k, v]) => `${k} ${v}`)
          .join(' · ')
      : ''
  );

  /** The caption's `details ▸` fold (D43): closed on every arrival. */
  let factsOpen = $state(false);

  const profile = $derived(preview ? profileState(preview.variant) : null);
  /**
   * A refused profile leads the pane (#3075): the needs-you block where this
   * viewer has a move, else a neutral sentence, by the editor's history read.
   */
  const profileLead = $derived(refusedProfile(profile, known?.profile_applies ? decider : undefined));

  /** A prompt text run: its own line breaks, and a long line wrapped inside the block. */
  const PRE = 'whitespace-pre-wrap break-words [overflow-wrap:anywhere]';

  function select(role: string) {
    const url = new URL(page.url);
    url.searchParams.set('role', role);
    replaceState(url, { promptRole: role });
    // At phone width the pane is below the list: bring it up to the reader.
    if (pane && window.matchMedia('(max-width: 767px)').matches) {
      pane.scrollIntoView({ block: 'start' });
    }
  }
</script>

<div class="flex min-h-0 flex-1 flex-col overflow-y-auto md:overflow-y-hidden">
  <header
    class="flex shrink-0 flex-wrap items-baseline gap-x-4 gap-y-1 border-b border-hair px-5 pt-5 pb-[18px] md:px-10"
  >
    <h1 class="text-26 text-text-1">Prompts</h1>
    <span class="text-13 text-text-3">the prompt each agent role runs, and its operator instructions</span>
  </header>

  {#if data.failure !== null}
    <p class="px-5 pt-6 text-15 text-text-2 md:px-10" data-prompts-failure>
      {data.failure}, so no role can be listed. Is <span class="font-mono">kew serve</span> running a
      version that serves <span class="font-mono">/api/prompts</span>?
    </p>
  {:else if roles.length === 0}
    <p class="px-5 pt-6 text-15 text-text-2 md:px-10">This server lists no agent roles.</p>
  {:else}
    <div class="flex flex-col md:grid md:min-h-0 md:flex-1 md:grid-cols-[400px_minmax(0,1fr)]">
      <aside
        aria-label="Agent roles"
        class="min-w-0 border-b border-hair px-5 pt-[22px] pb-4 md:overflow-y-auto md:border-r md:border-b-0 md:px-7"
      >
        <SectionHead label="Roles" meta={`${roles.length}`} />
        {#each roles as r, i (r.role)}
          <button
            type="button"
            data-role={r.role}
            aria-current={r.role === selected ? 'true' : undefined}
            onclick={() => select(r.role)}
            class={dataRowClasses({
              rule: i === roles.length - 1 ? 'none' : 'quiet',
              class:
                'h-auto min-h-14 w-full grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-3 gap-y-1 py-2 text-left transition-colors hover:bg-ink-selected'
            })}
            class:bg-ink-selected={r.role === selected}
          >
            <span
              class="col-span-2 min-w-0 text-15 break-words"
              class:text-text-1={r.role === selected}
              class:text-text-2={r.role !== selected}>{r.role}</span
            >
            <!-- Run kinds are ids: mono 11 in their own case, not the label caps.
                 A role whose one run kind is its own name says nothing here
                 (D43): the subtitle would only repeat the title. The span stays
                 so the source chip keeps its cell. -->
            <span
              class="min-w-0 font-mono text-11 tracking-normal break-words normal-case text-text-3"
              data-role-kinds
              >{r.run_kinds.length === 1 && r.run_kinds[0] === r.role ? '' : r.run_kinds.join(' · ')}</span
            >
            <span
              class="justify-self-end rounded-full border border-hair-strong px-2.5 font-mono text-13 whitespace-nowrap text-text-2"
              data-source-chip>{sourceChip(r)}</span
            >
          </button>
        {/each}
      </aside>

      <section
        bind:this={pane}
        aria-label="Prompt preview"
        class="min-w-0 px-5 pt-[22px] pb-9 md:overflow-y-auto md:px-10"
        data-prompt-pane
      >
        {#if known === null}
          <p class="text-15 text-text-2">
            No agent role is named <span class="font-mono">{selected}</span>; pick one from the list.
          </p>
        {:else}
          <div class="flex flex-wrap items-baseline gap-x-4 gap-y-1">
            <h2 class="font-mono text-15 text-text-1">{known.role}</h2>
            <span class="font-mono text-13 text-text-2" data-pane-source>{sourceChip(shown ?? known)}</span>
          </div>
          {#if change}
            <p class="mt-2 text-13 text-text-2" data-change-line>
              {change.before}<code class="font-mono text-text-1">{change.code}</code>{change.after}
            </p>
          {/if}
          {#if previewFailure !== null}
            <p class="mt-5 text-15 text-text-2" data-preview-failure>
              {previewFailure}, so its prompt cannot be shown here.
            </p>
          {:else if preview === null || blocks === null}
            <p class="mt-5 text-15 text-text-3">Rendering the {known.role} prompt over a sample item…</p>
          {:else if refusal !== null}
            {@render profileNeedsYou()}
            <p class="mt-5 text-15 text-text-2" data-preview-refused>
              The launch refuses before it builds a prompt, so there is none to show. {refusal}
            </p>
            {#if variant}
              <p class="mt-2 text-13 text-text-3">
                <Why label="details" controls="preview-facts" bind:open={factsOpen} class={TAP_INLINE} />
              </p>
              <p
                id="preview-facts"
                hidden={!factsOpen}
                class="mt-1 font-mono text-11 tracking-normal text-text-3 normal-case wrap-anywhere"
                data-preview-facts
              >
                {variant}
              </p>
            {/if}
            {@render profileLine(false)}
          {:else}
            <!-- Which [prompt] the text was rendered with is the server's word
                 for THIS text: a refused profile leaves the base even where
                 [prompt] applies (#3071). -->
            {@render profileNeedsYou()}
            <p class="mt-5 text-13 text-text-3" data-preview-caption>
              Rendered over a sample item{preview.prompt_config_rendered ? ', with the served ' : ', without any '}<span
                class="font-mono">[prompt]</span
              >{preview.prompt_config_rendered ? ' configuration' : ' override'}.
              <!-- D43: the sha and the variant's key=value facts are provenance,
                   one press away rather than the caption's last words. -->
              <Why
                label="details"
                controls="preview-facts"
                bind:open={factsOpen}
                class={`ml-1 ${TAP_INLINE}`}
              />
            </p>
            <p
              id="preview-facts"
              hidden={!factsOpen}
              class="mt-1 font-mono text-11 tracking-normal text-text-3 normal-case wrap-anywhere"
              data-preview-facts
            >
              sha256 {preview.preview_sha256.slice(0, SHORT_SHA)}{variant ? ` · ${variant}` : ''}
            </p>
            {@render profileLine(true)}
            <!-- One mono 13 block; its text runs are pre-wrap, and a line with no
                 space in it breaks anywhere, so nothing in it scrolls sideways. -->
            <div
              class="mt-3 min-w-0 rounded-[4px] border border-hair-box bg-ink-raised px-4 py-3 font-mono text-13 text-text-2"
              data-preview
            >
              {#each blocks as s, i (i)}
                <!-- The operator addendum's heading and body carry a cyan left
                     rule: they are the part of this prompt the console changes.
                     The block is the span the server says the builder placed
                     it at, so a `## ` line inside it stays inside the rule. -->
                {@const addendum = s.addendum}
                {#if s.kind === 'text'}
                  {#if s.text !== ''}
                    <div class={PRE}>{s.text}</div>
                  {/if}
                {:else}
                <button
                  type="button"
                  aria-expanded={open[i] ?? false}
                  onclick={() => (open[i] = !open[i])}
                  class="flex min-h-8 w-full cursor-pointer items-baseline gap-2 py-2 text-left text-text-1"
                  class:addendum
                  data-section
                  data-addendum={addendum ? 'heading' : undefined}
                >
                  <span aria-hidden="true" class="w-3 shrink-0 text-text-3">{open[i] ? '▾' : '▸'}</span>
                  <span class="min-w-0 {PRE}">{s.heading}</span>
                </button>
                {#if open[i] && s.body !== null}
                  <div class={PRE} class:addendum data-addendum={addendum ? 'body' : undefined}>{s.body}</div>
                {/if}
                {/if}
              {/each}
            </div>
          {/if}
          {#if known.profile_applies}
            <ProfileOutcomes role={known.role} revision={reads} {historyGeneration} onstale={rereadPreview} />
            <ProfileEditor
              role={known.role}
              bind:historyGeneration
              bind:decider
              onchange={rereadPreview}
              previewGeneration={previewOnScreen ? (preview?.profile_generation ?? null) : undefined}
              {previewPending}
            />
          {/if}
        {/if}
      </section>
    </div>
  {/if}
</div>

<!-- #3075 — a refused profile blocks every launch for the role, so it leads
     the pane, before the neutral render metadata, in the server's words (its
     config names in mono). Magenta — the shared needs-you block — only where
     this viewer has a move; for anyone else, one neutral sentence. -->
{#snippet profileNeedsYou()}
  {#if profileLead !== null && profile !== null}
    <div data-profile-state={profile.kind} data-profile-lead={profileLead.kind}>
      {#if profileLead.kind === 'needs-you'}
        <NeedsYouBlock block={profileLead.block} />
      {:else}
        <p class="mt-5 text-15 break-words text-text-1" data-profile-refused-neutral>
          {#each profileLead.runs as run, i (i)}{#if run.mono}<span class="font-mono">{run.text}</span
              >{:else}{run.text}{/if}{/each}
        </p>
      {/if}
    </div>
  {/if}
{/snippet}

<!-- The role's operator profile state. `rendered` is false beside a launch
     refusal: no prompt was built, so no sentence may say one was. A refused
     profile is said in the needs-you block above; here it leaves only the
     neutral fact that the text shown is the base. -->
{#snippet profileLine(rendered: boolean)}
  {#if profile?.kind === 'refused'}
    {#if rendered}
      <p class="mt-2 text-13 break-words text-text-2" data-profile-base>
        This is the base prompt, without the refused profile.
      </p>
    {/if}
  {:else if profile !== null && profile.kind !== 'not-applicable'}
    <p class="mt-2 text-13 break-words text-text-2" data-profile-state={profile.kind}>
      {#if profile.kind === 'none'}
        No operator profile is active for this role{rendered ? ', so this is its base prompt' : ''}.
      {:else if profile.kind === 'active' && rendered}
        Operator profile <span class="font-mono text-text-1">{profile.version}</span> is active and is rendered here
        where a launch renders it.
      {:else if profile.kind === 'active'}
        Operator profile <span class="font-mono text-text-1">{profile.version}</span> is active; a launch renders it once
        the refusal above is fixed.
      {:else}
        Operator profile: <span class="font-mono">{profile.label}</span>.
      {/if}
    </p>
  {/if}
{/snippet}

<style>
  .addendum {
    border-left: 2px solid var(--accent-2);
    padding-left: 10px;
  }
</style>
