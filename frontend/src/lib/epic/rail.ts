/**
 * FUI-17 — the epic page's selected-node rail, as a pure model
 * (DEpic x 40–1400, y ≈742–960).
 *
 * The rail is four columns fed by four different reads, and that is the whole
 * reason this module exists: one column's server being down must cost exactly
 * that column. So every column degrades on its own, and each of them degrades
 * the same way — an em dash and the reason for it — rather than blanking the
 * rail, showing a zero, or (worst) borrowing a neighbour's fact to fill a gap.
 *
 * Four rules it keeps, all pinned in `rail.test.ts`:
 *
 * 1. **Nothing is selected until the user selects it.** `railModel` with a null
 *    node is the quiet empty state; it never falls back to "the first node",
 *    "the frontier" or "the one that needs you". The page's selection is the
 *    only input, which is why the empty model carries a hint and no data.
 * 2. **Every figure names its source.** The dollar amount is
 *    `EpicNodeDetail.lifetime_usd` — the node's lifetime issue spend, the same
 *    number the old node pane printed — and never the epic's drain, which is a
 *    different window and would read high. Containment is the route's
 *    *declared* posture off the work view (kew has no risk tiers, so it shows
 *    containment instead; Factory UI plan §1).
 * 3. **An absent fact and an unread one are different.** A node with no
 *    attention hides the "Needs you" column, because nobody is waiting; a
 *    snapshot whose attention join FAILED keeps the column and says so, because
 *    the page cannot tell. The same split runs through the pipeline (a 404 is
 *    "no pipeline recorded", a 500 is "could not be read") and the evidence.
 * 4. **The rail states one moment.** Selected and Needs-you read the node off
 *    the snapshot on screen; the other columns are fed by reads the page takes
 *    of the server as it is *now*. Those two clocks are reconciled here and in
 *    the page: `nodeMoment` is the fingerprint the page rekeys its reads on, so
 *    a driver tick that moves the node retakes them, and `replay` dashes every
 *    read-fed column outright, because no read of now belongs beside a frame
 *    from the journal's past.
 *
 * Every mark, colour and shape below is `$lib/factory/status`'s, reached
 * through `StatusMark` and `Swimlane` in `SelectedRail.svelte`; nothing here
 * names a colour.
 */
import type { EpicNode, EpicNodeAttention, EpicNodeDetail, ItemEvidence, ItemPipeline, WorkView } from '$lib/api';
import { isUnavailable, unreadable, type Source } from '$lib/monitor/readouts';
import { pipelineModel } from '$lib/work/pipeline';
import { evidenceState, ledgerIndex } from '$lib/work/evidence';
import { prRef } from '$lib/work/refs';
import { safeAppHref, safeHref } from '$lib/safe-href';
import type { SwimlaneLane } from '$lib/factory/swimlane';
import { itemRef } from '$lib/utils';
import { fmtTime, utcTitle } from '$lib/time';
import type { NeedsYouBlock } from '$lib/needs-you/block';
import { kindTitle } from '$lib/needs-you/kind';
import { linkMoves } from '$lib/needs-you/moves';
import { rowSentence } from '$lib/needs-you/summary';
import { nodeAttentionNotice } from './attention';
import { fmtUSD } from './format';
import { statusWord } from './render';

/** What a column with nothing to print shows in place of a figure. */
export const RAIL_DASH = '—';

/** The quiet empty state: no selection is a real answer, not a blank rail. */
export const RAIL_EMPTY = 'Select a node to see what it is waiting on.';

/**
 * The window height under which the rail is a line and a disclosure (#2333).
 *
 * The rail is a fixed band beneath the canvas, so every pixel it holds is one
 * the graph does not get: at 1440x600 its 196px left ~130px of graph showing
 * at a time. 700 is the height at which the artboard's own stack — the 56px
 * shell header, the epic strip, a canvas worth panning and the rail — stops
 * fitting; above it nothing collapses and the rail is the band DEpic draws.
 */
export const SHORT_VIEWPORT = 700;

/**
 * The rail's tracks, and the two widths it folds them at (#2333).
 *
 * The artboard draws four columns and three of them are FIXED (DEpic x
 * 40–1400), so which viewports can hold them is arithmetic and not taste: 40px
 * of page gutter either side (`sm:px-10`), 840px of fixed track, and whatever
 * the fluid Pipeline track needs to keep FUI-07's compact swimlane inside its
 * own rule. `scripts/ui-check.mjs` reads that last one in a real browser: the
 * four stages `PIPELINE_STAGES` composes printed "run f5dcb6a5" over the
 * Evidence column everywhere up to 1230px of viewport, so the track wants 310
 * and `pipeline` below is that on the 8px grid. A lane somehow wider than that
 * clips at its own rule — the column is `overflow-hidden` — because a cut word
 * is a loss the reader can see and two readings in one place is not.
 *
 * The first fix for this folded at `sm`, which is 600px short of the sum — so
 * the overprint it cured at 390 was alive again at 641 and stayed to ~1230.
 * Hence three tiers, with both breakpoints checked against the arithmetic in
 * `rail-fold.test.ts`:
 *
 * * **one column** below `lg`, which is where the epic page already hides its
 *   minimap and key hints, so the whole surface folds on one number;
 * * **the artboard's four tracks** from `xl` up, the first tier wide enough to
 *   hold them;
 * * **2×2** in between — the fold DESIGN.md already asks of Monitor's four
 *   readouts, and the only one that fits a 1024px window without stacking.
 */
export const RAIL_TRACKS = {
  /** The page gutter either side of the rail from `sm` up (`sm:px-10`). */
  gutter: 40,
  /** Selected (DEpic x 40–340). */
  selected: 300,
  /** Evidence (x 860–1112). */
  evidence: 280,
  /** Needs you (x 1140–1400), a track only when somebody is waiting. */
  attention: 260,
  /**
   * The narrowest the fluid Pipeline track may be and still print its cells
   * inside their own column, rules (`px-7`, 28px either side) included.
   */
  pipeline: 320,
  /** Tailwind's `lg`: one column below this, two above it. */
  twoUp: 1024,
  /** Tailwind's `xl`: the artboard's four tracks from this up. */
  fourUp: 1280
} as const;

/** The separator between the readings on a line, the artboard's own middot. */
const SEP = ' · ';

/** A reading, or the em dash and the reason there is no reading. */
export type RailFact = {
  /** The figure, or `RAIL_DASH`. */
  value: string;
  /** Why the dash. `''` whenever `value` is a real reading. */
  reason: string;
};

/** The "Selected" column (DEpic x 40–340). */
export type RailSelected = {
  /** `#2055` for a GitHub number, the bare key for a Linear item. */
  ref: string;
  title: string;
  /** The node's own status word, for `StatusMark`'s shape and colour. */
  status: string;
  /** That status as a word, for the line beside the mark. */
  state: string;
  containment: RailFact;
  spend: RailFact;
  /** "depends on #2047 · PR #2059". `''` when the node has neither. */
  relations: string;
  workHref: string;
  /** "Open #2055 in Work". */
  workLabel: string;
  /**
   * A failed node's next command, exactly as the server served it
   * (`recovery_command`: retry, or continue for preserved work, #2960); `''`
   * for every other status, and for a failed node no command recovers.
   */
  retry: string;
  /** What the driver recorded about the failure, beside the hint. */
  retryReason: string;
};

/** The "Pipeline · main" column (DEpic x 368–832), a compact FUI-11 swimlane. */
export type RailPipeline = {
  /** "Pipeline · main" — the lane the payload named, never a hardcoded one. */
  heading: string;
  stages: string[];
  lanes: SwimlaneLane[];
  /** `RAIL_DASH` when there is no grid to draw; `''` when there is. */
  placeholder: string;
  /** Why the dash. `''` when a grid was drawn. */
  reason: string;
  /** A partial read, confessed beside a grid that WAS drawn. */
  notice: string;
};

/** One row of the evidence checklist. */
export type RailEvidenceRow = {
  key: string;
  /** The 1-based position, zero-padded the way both artboards draw it. */
  n: string;
  label: string;
  /** The recorded time (viewer zone), or the state's own word when there is
   * no row. */
  right: string;
  /** The full UTC instant behind `right`, for its `title`; empty when `right`
   * is a state word rather than a time. */
  rightTitle: string;
  /** The attestation state, for `StatusMark`. Never a progress word. */
  state: string;
};

/** The "Evidence" column (DEpic x 860–1112), the FUI-12 chain compacted. */
export type RailEvidence = {
  /** "3 / 4 attested", or `RAIL_DASH`. */
  count: string;
  /** The numerator alone, so the head can colour it green on its own. */
  attested: number;
  /** The denominator the server counted, beside it. */
  total: number;
  rows: RailEvidenceRow[];
  placeholder: string;
  reason: string;
  /** The server's own sentence about why the rows read as they do. */
  notice: string;
};

/** The magenta call to action, or null when there is nowhere to send anyone. */
export type RailCta = {
  label: string;
  href: string;
  /** True when the link leaves the console for the forge. */
  external: boolean;
};

/** The "Needs you" column (DEpic x 1140–1400). */
export type RailAttention = {
  /** "Merge decision" — the kind, in words. */
  title: string;
  /** The server's own sentence. Printed verbatim; this module words nothing. */
  prose: string;
  cta: RailCta | null;
  /**
   * The shared needs-you block (#3051): the kind in its head, the server's
   * sentence, and "Do this" with the computed command — the same block Work
   * draws, so the rail and Monitor print one command. Null with a notice.
   */
  block: NeedsYouBlock | null;
  /** Set only when the attention join failed: the column cannot say "clear". */
  notice: string;
};

export type RailModel = {
  /** True when the user has selected nothing. Every column is then null. */
  empty: boolean;
  /** What the empty rail says. `''` when something is selected. */
  hint: string;
  selected: RailSelected | null;
  pipeline: RailPipeline | null;
  evidence: RailEvidence | null;
  /** Null HIDES the column: nobody is waiting on this node. */
  attention: RailAttention | null;
  /**
   * Under replay, the one sentence the rail draws across the Pipeline and
   * Evidence columns in their place (`RAIL_REPLAY_NOTE`); '' live. The two
   * columns' own dashes and reasons stay on the model for what reads them.
   */
  replay: string;
};

export type RailInput = {
  /** The epic this node belongs to, for the retry hint. Opaque. */
  epicRef: string;
  /** The node the user picked, or null. The only selection input there is. */
  node: EpicNode | null;
  /**
   * True while the graph on screen is a replay frame rather than live state.
   *
   * The node, the edges and the attention above all come off that frame, so
   * they are the frame's moment; the four reads below are reads of NOW — there
   * is no "pipeline as of tick 41" endpoint to ask. Setting this dashes every
   * column those reads feed, with `RAIL_REPLAY_UNREAD` for a reason, rather
   * than dressing a past frame in present facts.
   */
  replay?: boolean;
  /** The snapshot's edges, `[dependency, dependent]`. */
  edges?: readonly (readonly [string, string])[];
  /** The snapshot's `unavailable` — what decides whether this node's null
   * attention is mute or merely unread. Read node-scoped; see `railAttention`. */
  unavailable?: readonly string[];
  /** `/api/epics/{ref}/nodes/{id}` — the lifetime spend lives here. */
  detail?: Source<EpicNodeDetail>;
  /** `/api/verbs/work/{id}` — read for the declared containment, nothing else. */
  view?: Source<WorkView>;
  /** `/api/work/{id}/pipeline` (FUI-11). */
  pipeline?: Source<ItemPipeline>;
  /** `/api/work/{id}/evidence` (FUI-12). */
  evidence?: Source<ItemEvidence>;
};

/** A source that has not been supplied at all — the page has not read it yet. */
const NOT_READ = 'not read yet';

/** Why every column fed by a per-selection read dashes under replay. */
export const RAIL_REPLAY_UNREAD = 'not in the journal; replay draws the graph alone';

/**
 * What a replaying rail says in place of its Pipeline and Evidence columns
 * (design review c9 D13): the reason once, as a sentence, rather than the same
 * mono line under each heading and a bare dash beside one of them.
 */
export const RAIL_REPLAY_NOTE =
  "Replay draws the graph alone: this node's pipeline and evidence are not in the journal.";

/**
 * The source a column may actually draw from, given the moment on screen.
 *
 * Under replay that is never the page's read. The rail has two clocks — the
 * frame the graph is showing, and the four reads taken of the server as it is
 * now — and printing one beside the other is the rail asserting two moments at
 * once. So a replaying rail is handed the dash and the reason for it, and every
 * figure left on screen is one the frame itself carried.
 */
function reading<T>(input: RailInput, source: Source<T> | undefined): Source<T> | undefined {
  if (!input.replay) return source;
  return { available: false, reason: RAIL_REPLAY_UNREAD };
}

/**
 * The selected node's own facts, as one comparable string.
 *
 * The Selected and Needs-you columns read the node off the snapshot on screen,
 * which moves under the page with every driver tick; the other three columns
 * come from reads the page takes once per selection. Keying those reads on this
 * fingerprint — rather than on the node id alone — retakes them the moment the
 * node's state moves, so the rail cannot show a merged node's hidden Needs-you
 * column beside a pipeline read from before the merge (or the reverse).
 *
 * It covers the node fields that name a STATE a read can contradict, and only
 * those. A tick that moved nothing the rail keys on is not a contradiction and
 * does not pay for four more requests.
 *
 * That is why the attention `reason` is deliberately absent. It is the server's
 * free prose, recomposed on every 1s poll from live readings rather than from
 * this node's state — a `review_capacity_saturated` exception renders the
 * oldest local hold's age into it (`service/work.py`'s `_burden`), so the
 * sentence moves on a wall clock while the node stands still. Folding it in
 * made a single selected node refire the page's read effect on that clock
 * forever: three columns blinking to em dashes and four more requests each
 * time, for as long as the tab stayed open. The prose is still printed live —
 * `railAttention` reads it straight off the snapshot — it just does not key
 * the reads. What decides the Needs-you column is presence, `kind` and `url`,
 * and those are all here.
 *
 * The fields are joined on NUL, not a space: `stuck` is free prose, and a
 * printable separator would let one field's change cancel another's and hold
 * the reads on a moment the node has left.
 */
export function nodeMoment(node: EpicNode | null | undefined): string {
  if (!node) return '';
  const attention = node.attention;
  return [
    node.id,
    node.status,
    node.stuck ?? '',
    node.pr_url ?? '',
    // Presence, kept apart from `kind`: a null attention HIDES the column, so
    // that is its own fact and not something an empty kind may stand in for.
    attention ? 'needs-you' : '',
    attention?.kind ?? '',
    attention?.url ?? ''
  ].join('\u0000');
}

/**
 * A failed per-selection read, as a source.
 *
 * A 404 is an ANSWER — this node has no such record — and reads as *absent*,
 * the quiet sentence the column prints without a warning; every other status is
 * a read that failed, and `unreadable` names it with the code the serve
 * returned. The distinction is the whole difference between an ordinary node
 * nobody has run yet and a kew that could not be asked.
 */
export function railSource<T>(subject: string, absent: string, err: unknown): Source<T> {
  const status = err instanceof Error ? /-> (\d{3})$/.exec(err.message)?.[1] : undefined;
  return status === '404' ? { available: false, reason: absent } : unreadable(subject, err);
}

/** The reading, or the dash and the source's own reason for there being none. */
function factOf<T>(source: Source<T> | undefined, read: (data: T) => string): RailFact {
  if (source === undefined) return { value: RAIL_DASH, reason: NOT_READ };
  if (isUnavailable(source)) return { value: RAIL_DASH, reason: source.reason };
  return { value: read(source), reason: '' };
}

/** The payload, or null — with the reason kept for the column to print. */
function payload<T>(source: Source<T> | undefined): { data: T | null; reason: string } {
  if (source === undefined) return { data: null, reason: NOT_READ };
  if (isUnavailable(source)) return { data: null, reason: source.reason };
  return { data: source, reason: '' };
}

/** The `?item=` link to the Work screen. The id is opaque — encoded, not parsed. */
export function workHref(itemId: string): string {
  return `/work?item=${encodeURIComponent(itemId)}`;
}

/**
 * What this node depends on, in the snapshot's own edge order.
 *
 * Edges are `[dependency, dependent]` (the adapters write
 * `Edge(src=dep, dst=node)`), so a node's dependencies are the sources of the
 * edges that point AT it. Reading them the other way round would print the
 * work this node is blocking as the work blocking it.
 */
export function dependsOn(
  nodeId: string,
  edges: readonly (readonly [string, string])[] | undefined
): string[] {
  const deps: string[] = [];
  for (const [src, dst] of edges ?? []) {
    if (dst !== nodeId || src === nodeId || deps.includes(src)) continue;
    deps.push(src);
  }
  return deps;
}

/** "depends on #2047 · PR #2059" — only the halves that exist. */
export function relationLine(node: EpicNode, deps: readonly string[]): string {
  const parts: string[] = [];
  if (deps.length) parts.push(`depends on ${deps.map((d) => itemRef(d, null)).join(', ')}`);
  if (node.pr_url) parts.push(`PR ${prRef(node.pr_url)}`);
  return parts.join(SEP);
}

/** The "Selected" column. */
export function railSelected(input: RailInput): RailSelected | null {
  const node = input.node;
  if (!node) return null;
  const ref = itemRef(node.id, null);
  const failed = node.status === 'failed';
  return {
    ref,
    title: node.title,
    status: node.status,
    state: statusWord(node.status),
    containment: factOf(reading(input, input.view), (view) => view.route.containment),
    // `lifetime_usd` — the node's lifetime issue spend. NOT the epic's drain,
    // which is a window over the whole graph and would read high here.
    spend: factOf(reading(input, input.detail), (detail) => fmtUSD(detail.lifetime_usd)),
    relations: relationLine(node, dependsOn(node.id, input.edges)),
    workHref: workHref(node.id),
    workLabel: `Open ${ref} in Work`,
    // The one affordance the old node pane had that the rail must not lose.
    retry: failed ? failedCommand(node) : '',
    retryReason: failed ? (node.stuck ?? statusWord(node.status)) : ''
  };
}

/**
 * A failed node's next move: the command the server served on the node, and
 * nothing when it served none.
 *
 * Never inferred from `attention`: the server clears attention while a
 * continuation executes the node's preserved work, without rewriting the
 * journaled failed snapshot, and retry is refused there (#2960). The server
 * fills `recovery_command` from the node's own stuck reason through the same
 * guard every command goes through (`kew.waits.fill_command`), so `''` — no
 * command recovers it, or its arguments are not plain — prints nothing, and
 * so does a serve that predates the field.
 */
function failedCommand(node: EpicNode): string {
  return node.recovery_command ?? '';
}

/** What an empty pipeline payload means: an answer, not a failure. */
export const PIPELINE_ABSENT = 'no pipeline recorded for this node yet';

/** The "Pipeline · main" column. */
export function railPipeline(input: RailInput): RailPipeline | null {
  if (!input.node) return null;
  const { data, reason } = payload(reading(input, input.pipeline));
  const model = pipelineModel(data);
  const names = (data?.lanes ?? []).map((lane) => lane.name).filter((name) => name !== '');
  const drawn = !model.empty;
  return {
    heading: names.length ? `Pipeline${SEP}${names.join(', ')}` : 'Pipeline',
    stages: model.stages,
    lanes: model.lanes,
    placeholder: drawn ? '' : RAIL_DASH,
    reason: drawn ? '' : reason || PIPELINE_ABSENT,
    // Only ever printed beside a grid: with no grid the dash's reason is the
    // whole story, and two confessions would read as two separate failures.
    notice: drawn ? model.notice : ''
  };
}

/** What an evidence payload with no rows means. */
export const EVIDENCE_ABSENT = 'no guarantees recorded for this node yet';

/**
 * The checklist rows.
 *
 * The `chain_verified` guard and the zero-padded index are `work/evidence.ts`'s
 * (`evidenceState`, `ledgerIndex`) — the DWork ledger draws the same chain, and
 * the rule that a check requires the host's own fact on a chain that verifies
 * must hold identically on both surfaces, so there is one copy of it rather than
 * two that agree today.
 */
export function railEvidenceRows(evidence: ItemEvidence): RailEvidenceRow[] {
  return (evidence.rows ?? []).map((row) => ({
    key: `${row.n} ${row.event}`,
    n: ledgerIndex(row.n),
    label: row.label,
    state: evidenceState(evidence, row),
    // The recorded time, or the state's own word — never another row's time.
    right: row.ts ? fmtTime(row.ts) : row.state,
    rightTitle: row.ts ? utcTitle(row.ts) : ''
  }));
}

/** The "Evidence" column. */
export function railEvidence(input: RailInput): RailEvidence | null {
  if (!input.node) return null;
  const { data, reason } = payload(reading(input, input.evidence));
  const rows = data ? railEvidenceRows(data) : [];
  const attested = data && data.chain_verified ? data.attested_count : 0;
  const total = data?.total ?? rows.length;
  const drawn = rows.length > 0;
  return {
    count: drawn ? `${attested} / ${total} attested` : RAIL_DASH,
    attested,
    total,
    rows,
    placeholder: drawn ? '' : RAIL_DASH,
    reason: drawn ? '' : reason || EVIDENCE_ABSENT,
    notice: drawn ? (data?.reason ?? '') : ''
  };
}

/**
 * Where the column sends the reader.
 *
 * kew performs no merge (the surface-convergence rule), so the call to action
 * is a link to the forge and nothing else. An approval is the one kind with no
 * URL of its own: it is decided on Monitor's holds column, whose approval rows
 * carry the same Approve and Deny the console draws (`monitor/holds.ts`). Before
 * the cutover this pointed at `/inbox`, which was the console's approvals view —
 * that path now lands on the Work screen, which has no decide control on it, so
 * the CTA names the screen that does rather than the one it used to be.
 */
export function railCta(kind: string, url: string | null | undefined): RailCta | null {
  // A served URL is a link only through safeHref (C10).
  const href = safeHref(url);
  if (href != null) {
    const ref = prRef(href);
    const named = ref.startsWith('#') ? `PR ${ref}` : 'the pull request';
    return {
      label: kind === 'merge_decision' ? `Open ${named} to merge` : `Open ${named}`,
      href,
      external: true
    };
  }
  if (kind === 'approval')
    return { label: 'Open Monitor to approve', href: '/monitor', external: false };
  return null;
}

/**
 * The "Needs you" column, or null to hide it.
 *
 * Null means nobody is waiting — which is only honest while THIS node's
 * attention join succeeded. When it failed, `nodeAttentionNotice` names what
 * could not be read and the column stays, saying so: a rail with no needs-you
 * column is a claim that this node needs nobody, and an unread join cannot
 * make it.
 *
 * The scope is the node's, not the epic's. `unavailable` is one list for the
 * whole snapshot, and an `attention:<item>` entry in it is about that item
 * alone — so a sibling whose work facts could not be read must not raise a
 * column on this node naming somebody else's item as the reason. The epic
 * header keeps the wider reading, because its count is over every node; the
 * rail, which speaks for one, keeps this one.
 */
export function railAttention(input: RailInput): RailAttention | null {
  if (!input.node) return null;
  const attention = input.node.attention;
  if (!attention) {
    const notice = nodeAttentionNotice(input.unavailable ?? [], input.node.id);
    return notice ? { title: 'Needs you', prose: '', cta: null, block: null, notice } : null;
  }
  // The sentence and the link are the server's (#2539): the same string Work,
  // Monitor and the popover print for this wait.
  // A served link passes the href gate (C10) or the CTA falls back to the URL.
  const link = attention.link ?? null;
  const linkHref = safeAppHref(link?.href);
  const title = kindTitle(attention.kind);
  const prose = rowSentence(attention);
  const served =
    link != null && linkHref != null
      ? { label: link.label, href: linkHref, external: /^https?:/i.test(linkHref) }
      : null;
  const cta = served ?? railCta(attention.kind, attention.url);
  return {
    title: title || 'Needs you',
    prose,
    cta,
    block: railBlock(title, prose, attention, cta, served !== null),
    notice: ''
  };
}

/**
 * A merge over a verify that failed at this head (#3460): the item's MERGE
 * VERIFY section, which leads the block's links — the PR comes after it.
 */
function verifyLinks(attention: EpicNodeAttention): (RailCta & { key: string })[] {
  const served = attention.verify_link ?? null;
  const href = safeAppHref(served?.href);
  if (served == null || href == null) return [];
  return [
    {
      key: 'verify',
      label: served.label,
      href,
      external: /^https?:/i.test(href)
    }
  ];
}

/**
 * The Needs-you column as the one needs-you block (#3051).
 *
 * The command is the server's (`kew.waits.fill_command`), printed verbatim —
 * the line Monitor prints for the same hold. `action` is prose ABOUT the move
 * ("kew epic unhold, which also restarts the review cycle"), so it is the
 * block's note only where there is no command to say the move instead. No
 * `Show in epic` link: the reader is on the epic.
 */
function railBlock(
  label: string,
  summary: string,
  attention: EpicNodeAttention,
  cta: RailCta | null,
  servedMove: boolean
): NeedsYouBlock {
  const command = (attention.command ?? '').trim();
  const verify = verifyLinks(attention)[0] ?? null;
  const served = cta ? { key: 'cta', ...cta } : null;
  // Design review c9 D10/D14: with no command and a link the SERVER served as
  // the move (`attention.link`, a merge's "Open PR #N to merge"), the links
  // are Do this, by the rule Work's block uses (`linkMoves`): the failed
  // verify is the move where there is one. In the rail's compact block Do
  // this comes before the sentence, so the footer can never cut it off. The
  // `railCta` fallback is this file's own guess at a link from a URL, and it
  // stays quiet, as Work's served `url` does (`entry-detail`).
  const moves = command === '' && servedMove ? linkMoves(verify, served) : [];
  const link = moves.length > 0 ? [] : [...(verify ? [verify] : []), ...(served ? [served] : [])];
  return {
    label,
    summary,
    why: '',
    since: '',
    command,
    note: command === '' ? (attention.action ?? '').trim() : '',
    nodeIds: [],
    moves,
    links: link.filter((l) => !l.external),
    external: link.filter((l) => l.external)
  };
}

/** The whole rail, from one selection and the reads made for it. */
export function railModel(input: RailInput): RailModel {
  const selected = railSelected(input);
  if (!selected)
    return {
      empty: true,
      hint: RAIL_EMPTY,
      selected: null,
      pipeline: null,
      evidence: null,
      attention: null,
      replay: ''
    };
  const attention = railAttention(input);
  return {
    empty: false,
    hint: '',
    selected: oneCommand(selected, attention),
    pipeline: railPipeline(input),
    evidence: railEvidence(input),
    attention,
    replay: input.replay ? RAIL_REPLAY_NOTE : ''
  };
}

/**
 * One wait, one command on the rail (#3051): when the Needs-you block prints
 * the move, the Selected column's failed-node hint stands down rather than
 * drawing the same command a second time beside it.
 */
function oneCommand(selected: RailSelected, attention: RailAttention | null): RailSelected {
  if (!attention?.block?.command) return selected;
  return { ...selected, retry: '', retryReason: '' };
}
