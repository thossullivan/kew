// FUI-16 — the needs-you and blocked treatment on an epic node.
//
// Three fields change how a node is drawn, and all of them are sentences the
// SERVER wrote. Two came with FUI-10:
//
//   - `attention` — why one node is waiting on a person, composed from the same
//     work projection the judgment queue reads, so the DAG's magenta diamond
//     and the Work surface can never disagree about who is waited on;
//   - `blocked_reason` — what the driver recorded about a blocked node.
//
// The third is older, and #2209 is what brought it onto the status line:
//
//   - `stuck` — the driver's waiting prose, of which exactly one line names no
//     move at all (`waiting.ts` rewrites that one and nothing else).
//
// This module turns them into the artboard's status line and nothing more. It
// invents no wording: every reason is printed verbatim, a null reason prints
// the status word alone (DEpic draws #2053 with a reason and #2040 without, and
// the difference must stay visible), and no colour is named here that
// `$lib/factory/status` did not already ration — magenta comes from the
// `needs_you` mark, lavender from `blocked`.
//
// The other half of the rule is `attentionNotice`. `attention: null` means
// "nobody is waiting" ONLY when the snapshot's `unavailable` list is empty; when
// the join failed, a DAG with no diamonds on it is a lie, so the page says so.
// That list is one list for the whole epic, and it mixes two scopes: the
// epic-wide reads and the per-item `attention:<item>`. A surface speaking for
// the epic (the DAG's own diamonds) reads all of it; a surface speaking for one
// node (the rail's column) reads `nodeAttentionUnavailable`, which is its share.
//
// #2279 adds the epic-wide COUNT, which is no longer a count of this join at
// all: `epicNeedsYou` reads the server's one needs-you set, filtered by epic
// (`needs_you_by_epic`), so the header, an epic legend and an Epics list row
// are three readings of one number instead of three numbers. The join below
// still decides which nodes are DRAWN waiting — it is the same set, node-scoped
// (`compose_node_attention` in kew/src/kew/service/attention.py) — and
// `needsYouReading` still counts it for the one surface the served map cannot
// answer: a scrubbed replay frame, which is a snapshot from the past.
import { rowSentence } from '$lib/needs-you/summary';
import type { EpicNodeAttention } from '$lib/api';
import { statusShape } from '$lib/factory/status';
import { JUDGMENT_ROW_SOURCES } from '$lib/shell/header-state';
import { armAction } from './waiting';
import { labelTone, statusWord } from './render';

/**
 * The node fields this module reads. `EpicNode` satisfies it; the looser shape
 * is what callers in tests (and any future node-like payload) need.
 *
 * `id` is one of them because the status line can carry an ACTION, and an
 * action has to name the node it is about (`armAction` in waiting.ts) — a
 * sentence about "this node" is useless the moment it is copied anywhere.
 */
export type NodeFacts = {
  id: string;
  status: string;
  attention?: EpicNodeAttention | null;
  blocked_reason?: string | null;
  /** The driver's waiting prose (`_waiting_reason` in graph/driver/tick.py). */
  stuck?: string | null;
};

/** One run of same-coloured words on a node's status line. */
export type StatusSegment = { text: string; color: string };

/** The status line's separator, in the artboard's own quiet ink. */
export const SEP = ' · ';
const SEP_COLOR = 'var(--text-5)';

/** The status word the needs-you mark and its magenta are looked up under. */
export const NEEDS_YOU_STATUS = 'needs_you';

/** Whether a person is the next move on this node. */
export function needsYou(node: NodeFacts): boolean {
  return node.attention != null;
}

/**
 * The status the node's MARK is drawn from — `needs_you` when somebody is
 * waited on, so an in-review node whose PR needs a merge decision takes the
 * magenta diamond (the card draws it a size up) rather than the cyan review
 * ring. The node's real status is not lost: the status
 * line below still leads with it.
 */
export function markStatus(node: NodeFacts): string {
  return needsYou(node) ? NEEDS_YOU_STATUS : node.status;
}

/** A reason worth printing, or null. Whitespace is not a reason. */
function reasonOf(raw: string | null | undefined): string | null {
  const text = (raw ?? '').trim();
  return text ? text : null;
}

/** The one reason the driver recorded about a node's own status — the
 * blocked reason, else the arm action its waiting prose names — or null. The
 * status line prints it after the word; the card leaves it to the popover. */
export function recordedReason(node: NodeFacts): string | null {
  return reasonOf(node.blocked_reason) ?? armAction(node.stuck, node.id);
}

/**
 * The status line, word by coloured word (DEpic #2055 and #2053).
 *
 * - the lead is the node's own status word in its own tone, extended with
 *   `blocked_reason` when the driver recorded one ("blocked · needs a design
 *   decision", one lavender run, exactly as the artboard draws it) — or, on a
 *   node nobody has armed, with the action that arms it;
 * - an attentive node then adds a quiet separator and a magenta "needs you"
 *   carrying `attention.reason`.
 *
 * A blocked node that ALSO needs a person says both — the reason it is stopped
 * and the person who can unstop it are different facts, and neither one is the
 * other's summary.
 *
 * The arm action is on the lead and not a segment of its own because it is not
 * a second fact: it IS what `queued` means, in the node's own quiet tone. It
 * belongs here rather than only in the stalled notice on the epic page (#2209,
 * round 2) — that notice needs `heartbeat.last_signal === 'stalled'`, so on a
 * progressing epic the one cue that tells an unarmed node from a step that will
 * arrive by itself appeared nowhere, and the `queued` mark's demotion off the
 * needs-you diamond rested on a sentence nobody could see. `armAction` returns
 * null for every other waiting state, so nothing else on this line is repeated.
 */
export function statusSegments(node: NodeFacts): StatusSegment[] {
  const tone = labelTone(node.status);
  const recorded = recordedReason(node);
  const word = statusWord(node.status);
  const segments: StatusSegment[] = [
    { text: recorded ? `${word}${SEP}${recorded}` : word, color: tone.status }
  ];
  const attention = node.attention;
  if (!attention) return segments;
  const why = reasonOf(rowSentence(attention));
  segments.push({ text: SEP, color: SEP_COLOR });
  segments.push({
    text: why ? `${statusWord(NEEDS_YOU_STATUS)}${SEP}${why}` : statusWord(NEEDS_YOU_STATUS),
    color: statusShape(NEEDS_YOU_STATUS).color
  });
  return segments;
}

/** The whole line as one string, for the `title` of a line the pitch clips. */
export function statusLineText(segments: readonly StatusSegment[]): string {
  return segments.map((s) => s.text).join('');
}

// --- the strip under the graph ---------------------------------------------

/**
 * The needs-you correction to `splitStandalone`'s partition: a node somebody is
 * waiting on goes back into the graph, however settled its own status is.
 *
 * `splitStandalone` sends a done node with no edges to the strip, and the strip
 * draws a 10px mark and a title — no plate, no magenta diamond, no reason. A
 * done node CAN still be waited on (an approval hold, a merge decision on its
 * PR), and when one was, the header counted it while the surface that is
 * supposed to show it drew a plain done row: the count said "1 needs you" and
 * nothing on the page said which. The count is over every node, so the
 * treatment has to be too — and the graph, not the strip, is where the
 * treatment lives.
 *
 * Order is preserved in both halves, and the strip's own claim ("N done")
 * stays true because what leaves it is exactly what it could not draw.
 */
export function keepAttentionInGraph<T extends NodeFacts>(split: {
  graph: T[];
  strip: T[];
}): { graph: T[]; strip: T[] } {
  if (!split.strip.some(needsYou)) return split;
  return {
    graph: [...split.graph, ...split.strip.filter(needsYou)],
    strip: split.strip.filter((n) => !needsYou(n))
  };
}

// --- the epic-wide reading -------------------------------------------------

/** The whole join failed — no node's attention is known (`UNREADABLE_PROJECTION`). */
export const UNREADABLE_JOIN = 'attention';
/** The approval-hold read failed — again every node's, not one's (`UNREADABLE_HOLDS`). */
export const UNREADABLE_HOLDS = 'attention:approval_holds';
/** The epic-hold read failed — again every node's, not one's (`UNREADABLE_EPIC_HOLDS`). */
export const UNREADABLE_EPIC_HOLDS = 'attention:epic_holds';
/** The archive ledger could not be read, so whether this epic is retired — and
 * with it whether ANY node's journaled wait belongs in the set — is unknown
 * (`UNREADABLE_ARCHIVE`). The server drops every journaled wait when this read
 * fails, so the failure is as wide as the epic. */
export const UNREADABLE_ARCHIVE = 'attention:epic_archive';
/** The entry naming ONE item whose work facts failed (`unreadable_item`). */
export const unreadableItem = (itemId: string): string => `attention:${itemId}`;

/** The server's epic-wide `unavailable` entries: a failure of any one of them
 * leaves EVERY node's attention unknown, so each belongs in a node's own
 * confession as well as the epic's. Kept as one list so a marker added on the
 * server cannot be wired into `attentionUnavailable` and forgotten in
 * `nodeAttentionUnavailable` — which is how a failed epic-hold read came to
 * hide the "Needs you" column instead of clouding it. */
const EPIC_WIDE = [
  UNREADABLE_JOIN,
  UNREADABLE_HOLDS,
  UNREADABLE_EPIC_HOLDS,
  UNREADABLE_ARCHIVE
] as const;

/** The `unavailable` entries that are about the needs-you join, and only those.
 * The vocabulary is the server's (`kew/service/attention.py`): "attention",
 * "attention:approval_holds", "attention:epic_holds", "attention:epic_archive"
 * and "attention:<item>". */
export function attentionUnavailable(unavailable: readonly string[]): string[] {
  return unavailable.filter((u) => u === UNREADABLE_JOIN || u.startsWith('attention:'));
}

/**
 * The same entries, narrowed to the ones that bear on ONE node.
 *
 * All of `EPIC_WIDE` is epic-wide, by definition: `attention` is the whole
 * projection, `attention:approval_holds` the whole approval-hold read,
 * `attention:epic_holds` the whole epic-hold read and
 * `attention:epic_archive` the retirement decision that keeps or drops every
 * journaled wait in the epic, so a failure of any of them leaves every node's
 * attention unknown. The other entry,
 * `attention:<item>`, is the per-item confession `unreadable_item` exists to
 * carry, and it is about that item ALONE — a sibling's unread work facts say
 * nothing about this node. Reading a sibling's entry node-scoped would put a
 * "Needs you" column on every cleanly-read node in the epic and name somebody
 * else's item as the reason, which is the one thing the per-item entry was
 * added to make impossible.
 */
export function nodeAttentionUnavailable(
  unavailable: readonly string[],
  nodeId: string
): string[] {
  const mine = unreadableItem(nodeId);
  return attentionUnavailable(unavailable).filter(
    (u) => (EPIC_WIDE as readonly string[]).includes(u) || u === mine
  );
}

/** A needs-you count, and whether it is a count at all. */
export type NeedsYouReading = { count: number; known: boolean; label: string };

/** `?` rather than `0` when nobody could read the set: "nobody is waiting" and
 * "we could not tell" are different worlds, and only one is good news. */
function reading(count: number, known: boolean): NeedsYouReading {
  return { count, known, label: known ? String(count) : '?' };
}

/**
 * The `unavailable` names that place an entry in an epic's BUCKET without
 * producing a row of their own: the epic walk, and the executing-run read.
 *
 * `node_epics` is the walk that fills in which epic each entry belongs to
 * (`_epic_by_item` in kew/src/kew/service/needs_you.py). A failed walk loses
 * no row — the header's count is still exact, which is why `header-state.ts`
 * deliberately does not watch it — but every entry whose own reader knew no
 * epic then lands in no bucket, so the per-epic numbers below ARE short by it.
 */
/*
 * `executing_runs` (#2784) is the read that takes an item OUT of the set while a
 * run executes on it. It produces no row either, but a failed read leaves a
 * running item counted in its epic's bucket, so a per-epic count rests on it.
 */
export const EPIC_PLACEMENT_SOURCES: readonly string[] = ['node_epics', 'executing_runs'];

/**
 * Every `/api/dashboard` source a per-epic count rests on: the judgment rows
 * themselves plus the walk that places them.
 *
 * The list matters because the payload is fail-soft per source over a much
 * wider set than this one. Reading `known` off the whole `unavailable` list
 * made a failed GLANCE source — the gh-backed `ready_queue`, which goes
 * unavailable on a timeout and backs off for 30s — print `?` on every epic
 * legend and every Epics-list row while the header beside them printed an
 * exact count from the same payload. A count that was taken correctly,
 * reported as unknown, is the null-versus-zero mistake pointing the other way.
 *
 * `kew/tests/test_factory_header_parity.py` holds this set to the sources
 * `needs_you_set` actually has, so one added on the server cannot be forgotten
 * here.
 */
export const EPIC_BUCKET_SOURCES: readonly string[] = [
  ...JUDGMENT_ROW_SOURCES,
  ...EPIC_PLACEMENT_SOURCES
];

/** The bucket's own failed sources, in the payload's order. Nothing else in
 * `unavailable` can change a per-epic count. */
export function bucketUnavailable(unavailable: readonly string[]): string[] {
  return unavailable.filter((u) => EPIC_BUCKET_SOURCES.includes(u));
}

/**
 * The epic legend's needs-you count, read off the SERVER's one set (#2279/D6).
 *
 * The header counted `rows`, this legend counted the diamonds the epic's own
 * node join had drawn on its own snapshot, and the Epics list counted nothing
 * — so a live console showed `1 needs you` in the header over legends saying 1
 * and 2, and no reading could be checked against another. The set is composed
 * once (`needs_you_set` in kew/src/kew/service/needs_you.py) and filtered once
 * (`needs_you_by_epic`), and this is where both surfaces read the result:
 * `epicNeedsYou(dashboard, ref)` here, and the same call per row on the Epics
 * list, so the three numbers are arithmetic on one set rather than three
 * opinions about one phrase.
 *
 * An epic with nothing waiting is absent from the map, which is `0` — the
 * server omits an empty bucket rather than serving one per epic it walked.
 *
 * `null` for a dashboard nobody has loaded yet, and `known: false` whenever
 * one of the BUCKET's own sources confessed a failure: a legend that printed
 * `0` over a payload composed from half its readers would be the one lie this
 * module exists to refuse, and a legend that printed `?` because the ready
 * queue timed out would be the other.
 */
export function epicNeedsYou(
  dashboard: { needs_you_by_epic?: Record<string, number>; unavailable?: string[] } | null,
  epicRef: string
): NeedsYouReading {
  if (dashboard == null) return reading(0, false);
  const counts = dashboard.needs_you_by_epic ?? {};
  const count = Object.hasOwn(counts, epicRef) ? counts[epicRef] : 0;
  return reading(count, bucketUnavailable(dashboard.unavailable ?? []).length === 0);
}

/**
 * The confession that belongs beside a SERVED count that is not a count.
 *
 * `epicNeedsYou` reads `known` off the bucket's own sources, and a legend
 * printing `?` with nothing saying why is the half-answer FUI-16 refused
 * for the node join. The same filter is applied here, so the notice names
 * exactly what made the count unknowable and never a source that did not —
 * "could not read ready_queue" under a count the ready queue cannot touch
 * reads as an excuse for a number that was fine. The vocabulary is the set's
 * own source names (`needs_you_set` in kew/src/kew/service/needs_you.py —
 * `work_rows`, `epic_waits`, `node_epics`), not this module's, so the words
 * are the server's; a dashboard nobody has loaded yet names itself.
 */
export function needsYouSetNotice(
  dashboard: { unavailable?: string[] } | null
): string | null {
  return notice(
    dashboard == null ? ['the needs-you set'] : bucketUnavailable(dashboard.unavailable ?? [])
  );
}

/** One entry of the served set, as an epic surface reads it off
 * `dashboard.rows`. `DashboardRow` satisfies it. */
export type NeedsYouEntry = {
  item_id?: string | null;
  epic_ref?: string | null;
  reason?: string | null;
};

/**
 * The entries in an epic's bucket that no node of the drawn graph carries.
 *
 * The legend's count is the bucket, and a bucket is not a count of nodes: the
 * set holds waits that are about the EPIC rather than about one node of it —
 * a stalled driver has no node to put a diamond on (`_stalled_rows` in
 * kew/src/kew/service/needs_you.py, and `kew/service/attention.py` says so
 * outright: "a stalled driver, an orphan PR and an epic CI hold are not facts
 * about one node"). Unsaid, the number in the node-status legend simply
 * exceeded the diamonds under it with nothing on the page to account for the
 * difference — which is the defect `keepAttentionInGraph` already fixed for
 * the entries that ARE nodes.
 *
 * So: every entry whose item is not drawn waiting on this graph, in the set's
 * own order, for the page to name. An entry naming no item at all is one of
 * them by definition.
 */
export function offGraphNeedsYou<T extends NeedsYouEntry>(
  rows: readonly T[] | undefined,
  epicRef: string,
  nodes: readonly NodeFacts[]
): T[] {
  const drawn = new Set(nodes.filter(needsYou).map((n) => n.id));
  return (rows ?? []).filter(
    (row) => (row.epic_ref ?? '') === epicRef && !drawn.has(row.item_id ?? '')
  );
}

/** The one line that says how much of the count the graph cannot show. Null
 * when the graph shows all of it, which is the ordinary case. */
export function offGraphNotice(entries: readonly NeedsYouEntry[]): string | null {
  if (!entries.length) return null;
  const subject = entries.length === 1 ? 'one of them is' : `${entries.length} of them are`;
  return `${subject} not a node on the graph below`;
}

/** Where the stall banner sends the reader: Work, filtered to the set. */
export const NEEDS_YOU_WORK_HREF = '/work?filter=needs-you';

/**
 * The stall banner's link (#2784): `N waits on Work →`, off the epic's bucket
 * of the served set — the legend's own reading — or null when nothing waits.
 *
 * The banner used to print the journal's `stuck` lines, six of them in ~11px
 * mono on go-live, raw codes (`awaiting_continuation`) and backticked commands
 * lifted out of reason text included. Each wait is a row on Work, with its
 * command computed in the set, so the banner says one sentence and sends the
 * reader there. Unknown is null too: a confident number over a set composed
 * from half its readers is the lie `epicNeedsYou` refuses, and the legend
 * beside the banner already confesses it.
 */
export function stalledWaitsLink(reading: NeedsYouReading): string | null {
  if (!reading.known || reading.count <= 0) return null;
  return `${reading.count} ${reading.count === 1 ? 'wait' : 'waits'} on Work →`;
}

/**
 * The "no epic" bucket (#2784): the set's entries that name no epic, which
 * `needs_you_by_epic` has no key for. The header read 11 over an Epics list
 * totalling 10 because the eleventh — an epic-less PR wait — had no row there.
 *
 * Read off the served `needs_you_no_epic`; a `kew serve` predating the field
 * gets the same count from `rows`, which is what the server counts. `known`
 * follows every bucket source (`EPIC_BUCKET_SOURCES`), the placement reads
 * included: a failed `node_epics` walk files an epic's entry under no epic, and
 * a failed `executing_runs` read leaves a running item counted, so either one
 * makes this number as unknown as an epic row's (#2842).
 */
export function noEpicNeedsYou(
  dashboard: {
    rows?: readonly { epic_ref?: string | null }[];
    needs_you_no_epic?: number;
    unavailable?: string[];
  } | null
): NeedsYouReading {
  if (dashboard == null) return reading(0, false);
  const count =
    dashboard.needs_you_no_epic ?? (dashboard.rows ?? []).filter((row) => !row.epic_ref).length;
  return reading(count, bucketUnavailable(dashboard.unavailable ?? []).length === 0);
}

/**
 * The same count over the snapshot's own nodes — the replay reading.
 *
 * Kept for the one surface the served map cannot answer: a scrubbed replay
 * frame is a snapshot from the past, and the needs-you set is always now. The
 * LIVE legend reads `epicNeedsYou`; this counts the diamonds on the frame in
 * front of you, which is the only honest number there.
 */
export function needsYouReading(
  nodes: readonly NodeFacts[],
  unavailable: readonly string[]
): NeedsYouReading {
  return reading(nodes.filter(needsYou).length, attentionUnavailable(unavailable).length === 0);
}

/** The notice's one wording, shared so the epic's and a node's confession can
 * never drift apart. Null when the list is empty — nothing to confess. */
function notice(failed: readonly string[]): string | null {
  if (!failed.length) return null;
  return `needs you unknown — could not read ${failed.join(', ')}`;
}

/** The notice that must appear when the needs-you join could not be composed,
 * naming what failed. Null when there is nothing to confess. Epic-wide: the
 * header's count is over every node, so any failed entry clouds it. */
export function attentionNotice(unavailable: readonly string[]): string | null {
  return notice(attentionUnavailable(unavailable));
}

/** The same confession for ONE node's needs-you column, naming only what was
 * unread ABOUT THAT NODE. Null when this node's attention was read cleanly,
 * however many siblings' were not. */
export function nodeAttentionNotice(
  unavailable: readonly string[],
  nodeId: string
): string | null {
  return notice(nodeAttentionUnavailable(unavailable, nodeId));
}
