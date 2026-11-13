import type { NeedsYouBlock, TextRun } from '$lib/needs-you/block';

/**
 * Which operator profile a role's preview carries (#3023).
 *
 * `GET /api/prompts/{role}` says it in `variant.profile`: `none` when no
 * profile is active, `v<N>` when the active approved version is rendered into
 * the preview, and `refused: <reason>` when the launch would refuse the active
 * one, so the preview is the base. A role no profile applies to carries no
 * key. Pure, so the page's reading of each state is pinned by a unit test.
 */
export type ProfileState =
  | { kind: 'none' }
  | { kind: 'active'; version: string }
  | { kind: 'refused'; reason: string }
  | { kind: 'not-applicable' }
  /** A label this build does not know: shown as served, claiming nothing. */
  | { kind: 'other'; label: string };

const REFUSED = 'refused: ';

export function profileState(variant: Record<string, string | boolean>): ProfileState {
  const label = variant.profile;
  if (typeof label !== 'string') return { kind: 'not-applicable' };
  if (label === 'none') return { kind: 'none' };
  if (/^v\d+$/.test(label)) return { kind: 'active', version: label };
  if (label.startsWith(REFUSED)) return { kind: 'refused', reason: label.slice(REFUSED.length) };
  return { kind: 'other', label };
}

/**
 * What the role's history read says of the viewer (#3119): whether they pass
 * the approver gate (`can_decide`), and else the server's sentence when it
 * names a fix (`decide_refusal`).
 */
export type Decider = { canDecide: boolean; decideRefusal: string | null };

/** The id of the operator-instructions editor section, which a refused profile's block links to. */
export const EDITOR_ANCHOR = 'operator-instructions';

/**
 * How the pane leads with a refused profile (#3075):
 * - `needs-you`: the shared needs-you block (#3051), in magenta — this viewer
 *   has a move.
 * - `neutral`: one text-15 sentence in the same place, with no magenta — every
 *   launch is still blocked, but the move is another person's.
 */
export type RefusedProfile = { kind: 'needs-you'; block: NeedsYouBlock } | { kind: 'neutral'; runs: TextRun[] };

/**
 * A refused profile as the pane leads with it, or null for every other state.
 *
 * The server's reason already names its own remedy (retire and propose again,
 * or remove `[prompt] template` from kew.toml), so it is said once, its config
 * names in mono, and nothing here adds a fixed remedy that could repeat or
 * contradict it. Magenta means this viewer has a move:
 * - an approver (`canDecide`): the block, with the reason and one link to the
 *   editor below;
 * - a viewer whose move is a config fix (`decideRefusal` set): the block, and
 *   DO THIS is the server's sentence — the one the editor's write area prints;
 * - any other viewer, or a history not read yet: the neutral sentence, which
 *   says an approver must clear it. A history still in flight never flashes
 *   magenta first.
 */
export function refusedProfile(state: ProfileState | null, decider: Decider | undefined): RefusedProfile | null {
  if (state?.kind !== 'refused') return null;
  const summary = `The active operator profile is refused, so every launch for this role refuses: ${state.reason.replace(/\.$/, '')}.`;
  const fix = decider?.decideRefusal || '';
  if (decider === undefined || (!decider.canDecide && fix === '')) {
    return { kind: 'neutral', runs: configRuns(`${summary} An approver must clear it.`) };
  }
  const note = decider.canDecide ? '' : fix;
  return {
    kind: 'needs-you',
    block: {
      label: 'Profile refused',
      summary,
      summaryRuns: configRuns(summary),
      why: '',
      since: '',
      command: '',
      note,
      noteRuns: configRuns(note),
      nodeIds: [],
      links: decider.canDecide
        ? [{ key: 'editor', label: 'Operator instructions', href: `#${EDITOR_ANCHOR}`, external: false }]
        : [],
      external: []
    }
  };
}

/** The four profile writes the Prompts page makes (#3025). */
export type ProfileWrite = 'propose' | 'approve' | 'decline' | 'retire';

/**
 * The exact details `require_write` refuses a write with on its own, before
 * the approver gate runs (`_check_csrf` and `_check_origin` in
 * `kew/src/kew/serve/security.py`). Matched whole, never as a substring: the
 * approver gate's own detail names the actor, and an actor may be called
 * `origin-operator`.
 */
const SESSION_REFUSALS: ReadonlySet<string> = new Set(['invalid or missing CSRF token', 'origin not allowed']);

/**
 * The approver gate's refusal while `[approval].approvers` is empty
 * (`UNCONFIGURED_APPROVERS` in `kew/src/kew/service/prompt_profiles.py`): no
 * one may decide a profile until it is set, so the page prints it as sent.
 */
const UNCONFIGURED_APPROVERS = 'Set [approval].approvers in kew.toml before deciding prompt profiles.';

/** What a write with no readable answer reads as: it may have landed. */
export const WRITE_OUTCOME_UNKNOWN = 'The result of this request is unknown. Reload to see the current profile.';

/**
 * Whether a write's outcome is unknown: no HTTP response at all (`status` 0,
 * the request may have committed before the answer was lost), a success whose
 * body could not be read, or a server error that may have come after the
 * commit. The page never says such a write changed nothing: it locks every
 * write until a reload shows the role as it now is, so a retry cannot propose
 * the same text twice.
 */
export function writeOutcomeUnknown(status: number): boolean {
  return status === 0 || (status >= 200 && status < 300) || status >= 500;
}

/** What a 409 reads as: another decision moved the role since this read. */
export const PROFILE_CHANGED = 'This profile changed since you opened it. Reload to see the new version.';

/**
 * Whether a failed history read was the server's 409: the role kept changing
 * while it was read, so it answered no history rather than a split one. The
 * page prints `PROFILE_CHANGED` with a Reload, not a read failure.
 */
export function readWasStale(err: unknown): boolean {
  const message = err instanceof Error ? err.message : String(err);
  return / -> 409$/.test(message);
}

/**
 * Reads of the role a pane shows, so only the newest one for the role still
 * shown lands (#3025). `begin(role)` starts a read and returns its ticket; the
 * ticket answers true only while no later read has begun and `current` is
 * still the role it was begun for. A slow answer for a role the pane has left,
 * or one an answer begun later has overtaken, is never shown, so a write can
 * never pair one role's version and generation with another role's controls.
 */
export function readSequence(): { begin: (role: string) => (current: string) => boolean } {
  let latest = 0;
  return {
    begin(role: string) {
      const mine = ++latest;
      return (current: string) => mine === latest && current === role;
    }
  };
}

/** How many times the pane re-reads a preview and a history that disagree. */
export const COHERENT_READ_ATTEMPTS = 3;

/**
 * What the profile editor does about the preview beside it (#3025). The
 * preview and the history are two reads, and a decision between them would
 * show one version's prompt over another's history — and let Retire act on
 * content the person never saw. Each names its profile generation:
 *
 * - `agree`: one generation, or nothing to compare (no preview on screen, or
 *   no history yet). The history and its controls may be shown.
 * - `wait`: they differ while a read is still in flight; show neither pair.
 * - `reread`: they differ and both reads have landed; read both again.
 * - `diverged`: they still differ after `COHERENT_READ_ATTEMPTS` re-reads;
 *   print `PROFILE_CHANGED` and lock every write until a Reload.
 *
 * `preview` is undefined when no preview of the role is on screen, and null
 * for one whose generation the server could not settle — which matches no
 * history.
 */
export type Coherence = 'agree' | 'wait' | 'reread' | 'diverged';

export function coherence(state: {
  preview: number | null | undefined;
  history: number | undefined;
  pending: boolean;
  attempts: number;
}): Coherence {
  if (state.preview === undefined || state.history === undefined) return 'agree';
  if (state.preview === state.history) return 'agree';
  if (state.pending) return 'wait';
  return state.attempts >= COHERENT_READ_ATTEMPTS ? 'diverged' : 'reread';
}

/**
 * A refused profile write as the sentence the page prints in its place. A 403
 * is the approver gate unless its detail is exactly one of `require_write`'s
 * own refusals; a 409 is a role another decision changed since this read; a
 * write with no readable answer is `WRITE_OUTCOME_UNKNOWN`, never "nothing
 * changed".
 */
export function writeRefusal(write: ProfileWrite, status: number, detail: string): string {
  if (writeOutcomeUnknown(status)) return WRITE_OUTCOME_UNKNOWN;
  if (status === 409) return PROFILE_CHANGED;
  if (status === 403 && detail === UNCONFIGURED_APPROVERS) return UNCONFIGURED_APPROVERS;
  if (status === 403 && !SESSION_REFUSALS.has(detail)) return `Only an approver can ${write} a profile.`;
  if (status === 401 || status === 403) return 'This session cannot change a profile. Sign in again, then retry.';
  if (status === 400 && detail) return `The server refused it: ${detail}`;
  return `The ${write} was refused (HTTP ${status}), so nothing changed.`;
}

/**
 * Whether a refused write locks every write until a reload (#3092): its
 * outcome is unknown, or it was a 409 — another decision moved the role, so
 * any control on screen would act on a state the person no longer sees.
 */
export function writeNeedsReload(status: number): boolean {
  return writeOutcomeUnknown(status) || status === 409;
}

/**
 * A draft's length as the server counts it (#3092): the code points of the
 * trimmed text, as Python's `len` counts them, never UTF-16 units — so a body
 * of non-BMP characters the server accepts is never refused here.
 */
export function bodyLength(draft: string): number {
  return Array.from(draft.trim()).length;
}

/** Every pending proposal, in the order served (newest first), each one decidable. */
export function pendingVersions<V extends { state: string }>(versions: readonly V[]): V[] {
  return versions.filter((v) => v.state === 'proposed');
}

/**
 * The confirm step before approving `version` over a newer active one, or
 * null when no step is needed: approving it replaces newer text with older.
 */
export function olderApproval(version: number, active: number | null): string | null {
  if (active === null || version >= active) return null;
  return `v${version} is older than the active v${active}. Approve it anyway?`;
}

/**
 * Why a viewer the approver gate would refuse has no profile write (#3119):
 * Propose, Approve, Decline and Retire all pass the same gate.
 */
export const APPROVER_ONLY = 'Only an approver can change a profile.';

/**
 * Why no profile write is the viewer's, or null when every write is theirs:
 * the server's own sentence when it names a fix (`decide_refusal`: the
 * kew.toml one while no approver is configured, the missing-config one on a
 * server started without a config), else `APPROVER_ONLY`. The
 * pane prints it once for its whole write area, never once per control.
 */
export function writeBlock(state: { canDecide: boolean; decideRefusal: string | null | undefined }): string | null {
  if (state.canDecide) return null;
  return state.decideRefusal || APPROVER_ONLY;
}

/** What a pending block reads in place of Approve and Decline while the pane must reload. */
export const RELOAD_TO_DECIDE = 'Reload to decide.';

/**
 * What a pending block draws where Approve and Decline go (#3092, #3119):
 * - `controls`: the viewer may decide and nothing asks for a reload, so the
 *   two magenta controls — a person is the next move.
 * - `reload`: a refusal asks for a reload. The block reads only
 *   `RELOAD_TO_DECIDE`; the full sentence and its Reload show once, above.
 * - `empty`: the viewer cannot decide. The write area's one reason
 *   (`writeBlock`) says why, so the block repeats nothing.
 */
export type DecisionSlot = 'controls' | 'reload' | 'empty';

export function decisionSlot(state: { canDecide: boolean; mustReload: boolean }): DecisionSlot {
  if (!state.canDecide) return 'empty';
  return state.mustReload ? 'reload' : 'controls';
}

/**
 * A config key or file a sentence names, set in Plex Mono where it is printed:
 * the approver gate's `[approval].approvers`, the custom-template refusal's
 * `[prompt] template` (#3075), and `kew.toml`.
 */
const CONFIG_NAMES = /(\[approval\]\.approvers|\[prompt\] template|kew\.toml)/;

/** `sentence` cut into runs, each config name its own `mono` run, in order. */
export function configRuns(sentence: string): TextRun[] {
  return sentence
    .split(CONFIG_NAMES)
    .filter((part) => part !== '')
    .map((part) => ({ text: part, mono: CONFIG_NAMES.test(part) }));
}
