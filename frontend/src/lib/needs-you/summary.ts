/**
 * #2245 — what ONE needs-you row says, in a sentence, decided once.
 *
 * The same row reached two cells of one screen as two different readings.
 * Monitor's "Needs you" caption printed `row.reason` verbatim, so a round-cap
 * hold read
 *
 *     review_rounds_exhausted
 *
 * while the Holds column beside it — which reads the same row's reason through
 * `describeHold` — read "the review round cap was reached". One row, two
 * readings, on one screen (P3 of the UX brief).
 *
 * So the sentence is composed here, once, and the surfaces read it.
 *
 * Three rules.
 *
 * **A hold reason is said in the words the Holds column says it in.** That
 * column is two cells from this one and composes every `epic_holds` row through
 * `HOLD_SPECS` (#2208), so a sentence built here from any other wording puts
 * one row on one screen twice: `budget_preflight` is "held on budget" in
 * `kew.waits` and "held on the dispatch budget" in that table, and a caption
 * preferring the first would have reproduced the very defect this module
 * closes. So for a reason the table claims, the table's words win — and with
 * them the detail, so the two cells read one sentence and not two.
 *
 * **Everything else is said in the server's words.** #2244 put `label` on every
 * judgment row, taken from `kew.waits.StallBucket.label` — the wording the CLI,
 * the TUI and the epic legend already print. `HOLD_SPECS` covers hold reasons
 * and nothing else, so for an `epic_wait`, a merge decision or a work exception
 * that field is the only operator wording anybody wrote, and it is preferred to
 * anything this module could derive. With neither, the raw reason stands, which
 * is the honest end of a vocabulary that grows.
 *
 * **Each thing is said once.** A label beside a reason that still carries the
 * machine token the label is a translation OF reads "the review round cap was
 * reached: review_rounds_exhausted". So the detail is the reason's own tail:
 * what it adds — a count, a path, the layer that refused — and nothing it
 * repeats. The `— raise <key>` clause is the one part of a tail that is an
 * action rather than a fact, so it is kept only where nothing else on the row
 * carries it (`raiseClause`).
 */
import type { DashboardRow } from '$lib/api';
import { capKeyOf, describeHold, type HoldPlace } from '$lib/monitor/hold-reasons';

/**
 * A reason's leading reference — `#2055 — `, `SFD-12 — `. The needs-you set
 * writes one onto every `epic_wait` entry (`item_ref(node_id)` in
 * `service/needs_you.py`), and it is kept rather than cut: it names the row's
 * own item, which is the thing a caption links.
 */
const LEADING_REF = /^(\S+ — )([\s\S]*)$/;

/**
 * A machine token at the head of a reason: `review_rounds_exhausted`,
 * `failed-terminal`, `merge_gate_touched@a1b2c3`, with whatever `: ` follows
 * it. It has to carry an underscore or a hyphen, and that is the whole of the
 * test — it is what tells a driver's own reason string apart from the first
 * word of a sentence the server wrote, so "merge decision on PR #2059" comes
 * through untouched.
 */
const MACHINE_TOKEN = /^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)+(?:@[^\s:]*)?\s*:?\s*/;

/**
 * `describeHold` fills its action from the row's place. Nothing here reads
 * that action — `row.command` is the move, filled in by the server — so no
 * place is named rather than a half-made command line being built and dropped.
 */
const NO_PLACE: HoldPlace = { epic: '', item: '' };

/**
 * What the reason adds to a label that already says what happened.
 *
 * The label's own words come off first, because the needs-you set writes the
 * bucket's label AS the reason when the driver recorded none
 * (`attention.wait_reason`) — and a reason that is the label would otherwise
 * be appended to it. Then the machine token, for the reasons that carry one.
 */
function detailBeyond(label: string, body: string): string {
  const rest = body.startsWith(label) ? body.slice(label.length) : body.replace(MACHINE_TOKEN, '');
  return rest
    .replace(/^[\s:—-]+/, '')
    .replace(/\s*[—-]\s*raise\s.*$/, '')
    .trim();
}

/**
 * The reason's `— raise <key>` clause, kept when nothing else on the row
 * carries it.
 *
 * Both the hold table and `detailBeyond` cut that clause out of the detail,
 * because it is an action and a row that names its action twice reads as two
 * moves. But the action it names is a config edit, not a kew command, and
 * `kew.waits` gives exactly those two buckets (`budget_preflight`,
 * `review_budget`) `command=""` for that reason — so on a budget row there is
 * no `command` to carry it, and cutting it would drop the only thing the reason
 * said an operator can do: which cap to raise.
 *
 * The Holds column prints the same key in its own action cell (`HOLD_SPECS`
 * `raise {cap}`), which this caption has no room for.
 */
function raiseClause(row: DashboardRow, body: string): string {
  if ((row.command ?? '').trim() !== '') return '';
  const cap = capKeyOf(body);
  return cap === '' ? '' : ` — raise ${cap}`;
}

/**
 * One needs-you row's parts, decided once: the reference the server wrote, the
 * reason in an operator's words, what the reason itself adds, and the `— raise
 * <key>` clause where nothing else on the row carries it.
 *
 * `rowSummary` joins them into a sentence; `entryDetail` ($lib/work/entry-detail)
 * prints them as separate lines of a detail block. Both readings have to be the
 * SAME reading — a pane whose headline says "the review round cap was reached"
 * over a Why line composed from some other wording is the two-readings defect
 * this module exists to close, moved one screen along.
 */
export type RowWords = {
  /** The reason's leading `#1116 — `, kept as the server wrote it. '' if none. */
  ref: string;
  /** The reason in an operator's words. '' when nobody has any for it. */
  label: string;
  /** What the reason adds beyond the label: a count, a path, a layer. */
  detail: string;
  /** ` — raise <key>`, or '' when a command on the row already says the move. */
  raise: string;
  /** The reason verbatim — all anybody wrote, where `label` is ''. */
  reason: string;
};

const NO_WORDS: RowWords = { ref: '', label: '', detail: '', raise: '', reason: '' };

export function rowWords(row: DashboardRow): RowWords {
  const reason = (row.reason ?? '').trim();
  const served = (row.label ?? '').trim();
  // No reason at all: the served label is the whole of what anybody wrote.
  if (reason === '') return { ...NO_WORDS, label: served };
  const ref = LEADING_REF.exec(reason);
  const head = ref ? ref[1] : '';
  const body = ref ? ref[2] : reason;
  const known = describeHold(body, NO_PLACE);
  // The hold table first, so this reading and the Holds column's are one
  // reading; the server's label for everything that table does not claim.
  const label = known.label || served;
  // Nobody has words for this reason, so the reason is all anybody wrote.
  if (label === '') return { ...NO_WORDS, reason };
  return {
    ref: head,
    label,
    detail: known.label === '' ? detailBeyond(label, body) : known.detail,
    raise: raiseClause(row, body),
    reason
  };
}

/**
 * The reason the hold table is matched against: the row's own, less the leading
 * `#1116 — ` the needs-you set writes onto an `epic_wait`.
 *
 * `HOLD_SPECS` matches on a PREFIX, so leaving the reference on would match
 * nothing at all. Exported because `rowWords` is not the only reading of that
 * table a surface takes off one row — the Work pane asks it for the action
 * (`entry-detail`) and Monitor's category column for the bucket (`holdKindOf`)
 * — and all three have to be asking about the same string.
 */
export function reasonBody(words: RowWords): string {
  return words.ref === '' ? words.reason : words.reason.slice(words.ref.length);
}

/**
 * One needs-you row as one sentence, behind the item reference the server wrote.
 *
 * The sentence is the SERVER's (`row.sentence`, written by
 * `kew.service.attention.say_wait`, #2539). This module used to compose it
 * from the label and the reason, and so did the epic popover and the rail, each
 * in its own way: one merge decision read four ways across the console. Nothing
 * here builds one any more — a serve that predates the field has its reason as
 * written stand in, which is the server's words too.
 *
 * '' when the row says nothing at all. The caller decides what stands in for
 * that (Monitor prints the row's type).
 */
export function rowSummary(row: DashboardRow): string {
  const ref = LEADING_REF.exec((row.reason ?? '').trim());
  return `${ref ? ref[1] : ''}${rowSentence(row)}`;
}

/**
 * The same sentence for a surface that has already said the reference.
 *
 * The needs-you set writes `#2055 — ` onto every `epic_wait` reason, so a
 * surface that prints that reference in a column or links it in the row's own
 * head would say it twice. The server's sentence never carries it; the reason
 * that stands in for a serve without one has it taken off.
 *
 * `rowSummary` is still the reading for a surface with nowhere else to put it:
 * a Monitor caption with no columns, the entry block's headline.
 */
export function rowSentence(row: { sentence?: string; reason?: string | null }): string {
  const said = (row.sentence ?? '').trim();
  return said !== '' ? said : withoutRef((row.reason ?? '').trim());
}

/** A reason with its leading `#2055 — ` off, or exactly as written if it has none. */
function withoutRef(reason: string): string {
  const ref = LEADING_REF.exec(reason);
  return ref ? ref[2] : reason;
}
