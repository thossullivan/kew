// The one place the factory UI decides what a status looks like (FUI-04).
//
// Every surface — the Work list, the Monitor tables, the epic DAG, the evidence
// ledger — paints statuses that arrive from five separate vocabularies: the
// epic snapshot's `DriverStatus`, the #2050 run states, the evidence-chain
// states, the work-detail publication field and the raw `RunStatus` enum. Left
// to themselves, each surface invents its own dot colour and its own word, and
// the colour rationing that makes magenta mean "a human is needed" leaks
// (Factory UI plan finding 3: the DAG paints `in_progress` magenta today, which is
// exactly the colour reserved for judgment).
//
// So: one alias table maps every word to one of seventeen *meanings*, and one
// meaning table gives each meaning its shape and its colour. The word stays
// per-input — `in_progress`, `working` and `running` are the same meaning drawn
// the same way, and the word says which vocabulary the caller was handed.
//
// Shapes and colours are lifted from the DEpic legend (y ≈100–115) and the
// DWork state column (x 324–392) in frontend/design/factory-ui/. StatusMark.svelte
// draws them; nothing here knows about SVG.

/** The mark geometries StatusMark.svelte can draw. */
export type MarkShape =
  | 'ring'
  | 'ring-thick'
  | 'ring-dashed'
  | 'ring-x'
  | 'ring-half'
  | 'dot'
  | 'dot-halo'
  | 'x'
  | 'hatch-square'
  | 'diamond-halo'
  | 'bars';

/** What a mark *means*, independent of which vocabulary named it. */
export type StatusMeaning =
  | 'todo'
  | 'working'
  | 'published_waiting'
  | 'done'
  | 'failed'
  | 'held'
  | 'blocked'
  | 'cancelled'
  | 'needs_you'
  | 'queued'
  | 'idle'
  | 'pending'
  | 'not_required'
  | 'unverified'
  | 'missing'
  | 'not_ready'
  | 'multi_task';

export type StatusMark = {
  shape: MarkShape;
  /** A `var(--token)` reference, never a literal hex — themes must switch. */
  color: string;
  /** The word to show beside the mark, in the caller's own vocabulary. */
  word: string;
  meaning: StatusMeaning;
  /** False when the status was not recognised and the todo fallback was used. */
  known: boolean;
};

/** Shape and colour per meaning. No two entries share both. */
export const STATUS_MARKS: Record<StatusMeaning, { shape: MarkShape; color: string }> = {
  todo: { shape: 'ring', color: 'var(--text-3)' },
  working: { shape: 'dot-halo', color: 'var(--accent-2)' },
  // "published, waiting for a human" — an open pull request. Named for what it
  // IS rather than for the status word that reaches it (#2305): it used to be
  // called `in_review`, which read like a stage kew was working through, and
  // `adversarial_review` — where kew's reviewer really is running — was mapped
  // onto it. The two are opposites in the motion vocabulary
  // ($lib/factory/motion): this one is waiting on a person and does not move.
  // It keeps the DEpic legend's thick cyan ring: the mark vocabulary is FUI-04's
  // design decision and no recolour was asked for here. Cyan on a card that
  // does not animate is the review RING, not a claim that anything is running
  // — the halo is what says that, and only `executing` items get one.
  published_waiting: { shape: 'ring-thick', color: 'var(--accent-2)' },
  done: { shape: 'dot', color: 'var(--success)' },
  failed: { shape: 'x', color: 'var(--danger)' },
  held: { shape: 'ring-thick', color: 'var(--warn)' },
  blocked: { shape: 'hatch-square', color: 'var(--status-blocked)' },
  cancelled: { shape: 'ring-x', color: 'var(--status-cancelled)' },
  needs_you: { shape: 'diamond-halo', color: 'var(--accent)' },
  // An epic node nobody has armed yet. Neutral on purpose (#2209): it is a
  // step still ahead, so it must not wear the magenta diamond rationed for
  // "a person is the next move" — and it is NOT `pending`'s shape-and-colour
  // pair either, because a queued node is waiting on a PERSON to arm it
  // rather than on the dispatcher's own turn.
  queued: { shape: 'ring-dashed', color: 'var(--text-4)' },
  idle: { shape: 'ring', color: 'var(--text-5)' },
  // Dashed rings are the "expected, not here yet" family: quiet for a step
  // still ahead, warn for a fact that is recorded but not provable.
  pending: { shape: 'ring-dashed', color: 'var(--text-3)' },
  not_required: { shape: 'ring', color: 'var(--text-4)' },
  unverified: { shape: 'ring-dashed', color: 'var(--warn)' },
  // A promised guarantee that never arrived: the cancelled X, but in alarm.
  missing: { shape: 'ring-x', color: 'var(--danger)' },
  // A review round that ended with a not-ready verdict (#3466): the round is
  // over and went against the change. DESIGN.md's not-ready colour is warn, as
  // the epic timeline draws it; the half-filled ring is its own shape, because
  // it is neither in flight (it used to wear the cyan working halo) nor held.
  not_ready: { shape: 'ring-half', color: 'var(--warn)' },
  multi_task: { shape: 'bars', color: 'var(--text-5)' }
};

/**
 * Meanings whose word is rendered muted rather than in the mark's own colour,
 * per the DWork state column: `working` is cyan and `success` green, while
 * `idle` and `not required` keep a `--text-3` word beside a coloured mark.
 */
export const QUIET_MEANINGS: ReadonlySet<StatusMeaning> = new Set<StatusMeaning>([
  'todo',
  'idle',
  'queued',
  'pending',
  'not_required',
  'cancelled',
  'multi_task'
]);

/**
 * Which vocabulary the caller's word came from.
 *
 * Only needed for a word two vocabularies spell the same and mean differently.
 * Today that is exactly one word, `queued`: the epic snapshot's
 * `DriverStatus.QUEUED` is where a node sits until a PERSON arms it
 * (`linear_admission_status` returns it precisely when the arm marker is
 * absent), while kpi.ts's `displayStatus` invents `queued` for an untouched
 * pending run the dispatcher will start on its own.
 *
 * `'epic'` is the default because it is the reading that names the next move:
 * the epic's `queued` takes its own `queued` mark, whose dashed ring says "not
 * armed yet", and the node's own status line in the DAG then says to add the
 * `kew:ready` label the Linear adapter looks for (`armAction` in
 * epic/waiting.ts, printed by `statusSegments` in epic/attention.ts — on every
 * frame, not only when the heartbeat reads stalled). No command appears in that
 * sentence because there is no `kew epic arm` to print: arming is a marker on
 * the tracker item, which `waiting.ts` records and `waiting.test.ts` pins. The
 * run reading degrades the word to `pending`, "the dispatcher's turn, not
 * yours". A caller that forgets to say gets the epic one, so a node nobody
 * armed is never drawn as a step that will arrive on its own.
 *
 * Until #2209 the epic reading was `needs_you`, the magenta diamond: queued
 * nodes were then indistinguishable from the ones a person really is holding
 * up, and an epic with a dozen unarmed nodes read as a dozen emergencies.
 */
export type StatusVocabulary = 'epic' | 'run';

/**
 * Status word → meaning, across every vocabulary that reaches the UI.
 *
 * The epic block is the WHOLE `DriverStatus` enum
 * (kew/src/kew/graph/driver/overlay.py), not the eight words `STATUS_COLOR`
 * in epic/render.ts happens to colour: the snapshot serves all fourteen, and a
 * word this table omits degrades to the todo ring — which would paint a live
 * governed-review round exactly like work nobody has started. The three
 * `awaiting_*` states are the ones a node sits in while a PERSON must act on
 * work that already happened (`_FIXED_WAITING_REASONS` in
 * graph/driver/tick.py names the command for each: `kew finalize`, `kew epic
 * continue`, confirm-or-reject), so they take the magenta needs-you diamond.
 * `queued` does NOT: it is work that has not started, and #2209 gave it its
 * own neutral mark — see `StatusVocabulary`.
 *
 * Three deliberate collapses: `flawed` (#2050) takes the failed X, because the
 * shape says "this went wrong" and the word says how; a budget stop takes the
 * hold ring, because a cap is a stop, not a defect; and `refused` joins the
 * budget stops, because `_REFUSAL_TERMINAL` in kew/service/work.py groups them
 * as "somebody's decision rather than a fault" — a refused node is intact and
 * re-admittable, never a failure.
 *
 * What this table does NOT cover, deliberately: the other `state` fields of the
 * work-detail DTOs (`WorkExecution`, `WorkOutcome`, `WorkJudgment`,
 * `WorkReadiness`, `WorkIntegration` and friends in kew/service/work.py, typed
 * in api.ts). Those are per-field facts — `in_flight`, `proven`, `conflicted`,
 * `changes_requested` — rather than one status vocabulary, no artboard assigns
 * them marks, and the surface that would draw them is FUI-11's pipeline
 * projection, which is not built (there is no `kew/service/pipeline.py`).
 * Inventing marks for them here would be exactly the borrowed justification
 * this file exists to avoid; they take the honest `known: false` fallback —
 * their own word beside a muted ring — until that lane lands and fixes a stage
 * vocabulary worth pinning.
 *
 * `tests/test_factory_status_parity.py` fails if either backend enum, or the
 * publication field, grows a word this table does not have.
 */
const MEANING_OF: Record<string, StatusMeaning> = {
  // The epic snapshot's `DriverStatus` (graph/driver/overlay.py) — all 14.
  todo: 'todo',
  queued: 'queued',
  in_progress: 'working',
  awaiting_publication: 'needs_you',
  awaiting_continuation: 'needs_you',
  in_review: 'published_waiting',
  // kew's own fresh-context reviewer, running against the recorded head: the
  // same meaning as any other work kew is executing, and no longer the
  // published-and-waiting ring it used to share with `in_review` (#2305).
  adversarial_review: 'working',
  remediating: 'working',
  awaiting_no_change: 'needs_you',
  done: 'done',
  blocked: 'blocked',
  failed: 'failed',
  cancelled: 'cancelled',
  held: 'held',
  // The #2050 run-state vocabulary.
  working: 'working',
  idle: 'idle',
  success: 'done',
  flawed: 'failed',
  manual: 'needs_you',
  // The evidence chain (FUI-12).
  attested: 'done',
  pending: 'pending',
  missing: 'missing',
  unverified: 'unverified',
  // The pipeline's review cell for a NEEDS_CHANGES / NEEDS_FULL round
  // (`_REVIEW_VERDICT` in kew/service/pipeline.py).
  not_ready: 'not_ready',
  // `WorkArtifactDTO.publication` (kew/service/work.py `_artifact_dto`) — all
  // three. `published` used to be the odd one out: the field's other two words
  // were here, so a work item whose PR is up painted the same muted ring as one
  // nobody has opened yet.
  not_required: 'not_required',
  published: 'done',
  // The run_store `RunStatus` enum, plus kpi.ts's display-only refinements
  // (`pending`, `queued` and `awaiting_publication` are already above).
  running: 'working',
  failure: 'failed',
  refused: 'held',
  killed: 'failed',
  budget_exceeded: 'held',
  budget_stopped: 'held',
  budget_stop: 'held',
  // The run_store `LoopStatus` literal — the six states of a goal loop. A loop
  // that ran out of steps without meeting its goal (`unmet`) is finished and
  // intact rather than broken, so it takes the muted cancelled ring and not the
  // failure X; a loop stopped by its cap joins the other budget stops as a
  // hold, and `needs_human` is the loop's own way of saying needs you.
  pursuing: 'working',
  achieved: 'done',
  unmet: 'cancelled',
  budget_limited: 'held',
  needs_human: 'needs_you',
  paused: 'held',
  // Joins the UI adds itself.
  needs_you: 'needs_you',
  multi_task: 'multi_task',
  // Not a word any server serves: it is this table's own MEANING name, and it
  // has to be a key here because `statusWord` prints a meaning by looking it
  // up as a status (`stateWord` in work/list.ts does exactly that, so the Work
  // list's STATE column reads whatever this resolves to). Every other meaning
  // name happens to be a served word too; this one, renamed in #2305 to say
  // what it is rather than which status reaches it, needs the entry spelled
  // out. `WORD_OF` keeps the printed word the operator's own, "in review" —
  // the rename is about what the CODE calls it, and the STATE column is
  // pinned to 88px for the words it already prints (D12).
  published_waiting: 'published_waiting'
};

/**
 * The run vocabulary's reading of a word `MEANING_OF` maps for the epic
 * snapshot. Refinements only: every key here is also a key of `MEANING_OF`, and
 * status.test.ts pins the whole set of words the two vocabularies disagree
 * about, so a second collision cannot slip in unnoticed.
 */
const RUN_MEANING_OF: Record<string, StatusMeaning> = {
  // kpi.ts's display-only refinement of a `pending` run with no steps and no
  // start time: queued for the dispatcher, never for a person.
  queued: 'pending'
};

/** Words that are not just the key with its separators opened up. */
const WORD_OF: Record<string, string> = {
  multi_task: 'tasks',
  // See `MEANING_OF`: a meaning name is printed by looking it up as a word, and
  // "published waiting" is not what an operator calls an open pull request.
  published_waiting: 'in review'
};

/** The five mark sizes the artboards draw. Nothing in between. */
export const MARK_SIZES = [7, 10, 12, 14, 18] as const;

export type MarkSize = (typeof MARK_SIZES)[number];

/** `in-progress`, `In Progress` and `in_progress` are one status. */
function normalize(raw: string): string {
  return raw.trim().toLowerCase().replace(/[\s-]+/g, '_');
}

/** The word as shown: the status itself, with its separators opened up. */
function humanize(key: string): string {
  return key.replace(/_/g, ' ');
}

/**
 * Own-property lookup, because the tables above are object literals.
 *
 * `MEANING_OF['constructor']` answers with `Object.prototype`'s, which is not
 * `undefined`, so a status word that happens to name a prototype member would
 * walk straight past the unknown fallback and index `STATUS_MARKS` with a
 * function — shape and colour `undefined`, and StatusMark.svelte drawing the
 * empty SVG the fallback exists to prevent. The status word comes off the wire,
 * and `constructor` and `__proto__` survive `normalize` unchanged, so both are
 * reachable input. (The camelCase members are only saved by the lowercasing,
 * which is not a property anyone should have to remember.)
 */
function own<T>(table: Record<string, T>, key: string): T | undefined {
  return Object.hasOwn(table, key) ? table[key] : undefined;
}

/**
 * The mark for a status word. Never throws and never returns nothing: an
 * unrecognised status degrades to the todo ring with its own word echoed, so a
 * server that grows a new state renders honestly instead of blanking a row
 * (the same forward-compatibility rule as `statusColor`).
 *
 * Pass `vocabulary` when the word came from the run vocabulary rather than an
 * epic snapshot; it changes only the words the two spell the same and mean
 * differently (see `StatusVocabulary`).
 */
export function statusShape(
  kind: string | null | undefined,
  vocabulary: StatusVocabulary = 'epic'
): StatusMark {
  const key = kind == null ? '' : normalize(kind);
  const meaning =
    (vocabulary === 'run' ? own(RUN_MEANING_OF, key) : undefined) ?? own(MEANING_OF, key);
  if (meaning === undefined) {
    return {
      ...STATUS_MARKS.todo,
      word: key === '' ? 'todo' : humanize(key),
      meaning: 'todo',
      known: false
    };
  }
  return {
    ...STATUS_MARKS[meaning],
    word: own(WORD_OF, key) ?? humanize(key),
    meaning,
    known: true
  };
}
