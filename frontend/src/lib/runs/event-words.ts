/**
 * #3458 — one run-page event, read as a sentence.
 *
 * The run page ended every row in `JSON.stringify(event.payload)`, so a route
 * choice read `{"recommended_runner":"claude-code","applied":false}` and a
 * deferral read as an object the reader had to parse. This module is the run
 * page's counterpart to the Monitor's `$lib/monitor/event-line.ts`: the event
 * name in words, and one line of what its payload says.
 *
 * Three rules, the same ones the Monitor keeps:
 *
 *   * **The backend's own words, kept.** A recorded value — a branch, an error,
 *     a verdict — is printed as recorded, never reworded into a guess.
 *   * **A closed set of keys.** A kind this module has no sentence for says
 *     only what a few named keys hold (`error`, `reason`, `status`, …), and a
 *     kind with none of them says nothing past its label. It never falls back
 *     to JSON: the raw payload stays one disclosure away on the row.
 *   * **A hold is not a failure.** A deferral or a hold is the run waiting on
 *     something, which DESIGN.md paints amber; red is for a step that failed.
 */
import type { RunAuditEvent } from '$lib/api';
import { errorCategoryLabel } from '$lib/factory/names';
import { leadOf } from '$lib/monitor/event-line';

/** The signal a row and its phase mark are drawn in. */
export type EventTone = 'held' | 'failed' | 'plain';

/**
 * Whole words in a recorded event name that mean the run is waiting, not
 * broken. Matched as tokens of the name split on `_` and `.`, never as
 * substrings: `threshold_breached` holds "hold" and is not a hold, and
 * `epic_node_unhold` is a release, not a hold.
 */
const HOLD_WORDS: ReadonlySet<string> = new Set(['defer', 'deferred', 'deferral', 'held', 'hold']);

/** Words in a recorded event name that mean the step did not succeed. */
const ERROR_WORDS = ['fail', 'error', 'refus', 'denied', 'kill', 'reject', 'abort'];

const text = (value: unknown): string | null =>
  typeof value === 'string' && value.trim() !== '' ? value.trim() : null;

const count = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) ? value : null;

const plural = (n: number, one: string, many = `${one}s`): string => `${n} ${n === 1 ? one : many}`;

/** Whether the recorded name says the run is held (a deferral, a hold). */
export function isHoldEvent(event: RunAuditEvent): boolean {
  return event.event
    .toLowerCase()
    .split(/[_.]/)
    .some((word) => HOLD_WORDS.has(word));
}

/**
 * Whether this event reads as a failure.
 *
 * Read from the recorded name and from an error field in the payload — never
 * from a status this module keeps its own list of, because a run page must
 * answer for event names from a newer host too. A hold is not a failure even
 * when it records the error that held it: `publication_deferred` carries the
 * push's error and is the run waiting for a base merge.
 */
export function isErrorEvent(event: RunAuditEvent): boolean {
  if (isHoldEvent(event)) return false;
  const name = event.event.toLowerCase();
  if (ERROR_WORDS.some((word) => name.includes(word))) return true;
  const payload = event.payload ?? {};
  for (const key of ['error', 'error_category', 'failure_layer']) {
    if (text(payload[key]) !== null) return true;
  }
  return exitedNonZero(event.event, payload);
}

/**
 * A completion whose process exited non-zero failed, even with an empty
 * `error_category`: the backend falls back to the exit code for the run's
 * status and sends `error_category: ""` (dispatch_core.py).
 */
function exitedNonZero(name: string, payload: Payload): boolean {
  const exit = count(payload.exit_code);
  return name === 'dispatch_completed' && exit !== null && exit !== 0;
}

/** The tone a row is drawn in: amber for a hold, red for a failure. */
export function eventTone(event: RunAuditEvent): EventTone {
  if (isHoldEvent(event)) return 'held';
  return isErrorEvent(event) ? 'failed' : 'plain';
}

type Payload = Record<string, unknown>;

/** One sentence per kind the run ledger records; '' when it holds nothing to say. */
const SENTENCES: Readonly<Record<string, (p: Payload) => string>> = {
  dispatch_owner_recorded: (p) => (count(p.owner_pid) !== null ? `owned by process ${p.owner_pid}` : ''),
  routing_decision: (p) => {
    const runner = text(p.recommended_runner);
    if (runner === null) return '';
    if (p.applied === true) return `routed to ${runner}`;
    if (p.applied === false) return `${runner} recommended, not applied`;
    return `${runner} recommended`;
  },
  sandbox_resolved: (p) =>
    [text(p.posture), text(p.network_boundary) && `network ${text(p.network_boundary)}`]
      .filter(Boolean)
      .join(', '),
  dispatch_checkout_bound: (p) => (text(p.branch) ? `working on ${text(p.branch)}` : ''),
  artifact_receipt_sealed: (p) =>
    count(p.files) !== null ? `${plural(count(p.files)!, 'file')} sealed` : 'change set sealed',
  publication_output_scan: (p) => {
    const findings = count(p.findings);
    const head = findings === null ? 'output scanned' : findings === 0 ? 'no findings' : plural(findings, 'finding');
    const attempt = count(p.attempt_number);
    return attempt !== null && attempt > 1 ? `${head}, attempt ${attempt}` : head;
  },
  publication_intent: (p) => (text(p.branch) ? `publishing ${text(p.branch)}` : ''),
  publication_deferred: (p) => {
    const step = text(p.step) ?? text(p.category) ?? 'publication';
    const why = text(p.error) ?? text(p.reason);
    return why ? `${step} deferred: ${why}` : `${step} deferred`;
  },
  publication_held: (p) => (text(p.reason) ? `held: ${text(p.reason)}` : 'held'),
  // The backend writes `error_category` (empty on success), `reason`,
  // `exit_code` and `cost_usd` (dispatch_core.py); `status` only some
  // fixtures carry, so it is read only when it is there. A category reads in
  // the shared table's words; its raw code stays behind the row disclosure.
  // A non-zero exit is a failure even with no category. An early exit records
  // its category again as its reason; that repeat says nothing and is dropped.
  dispatch_completed: (p) => {
    const category = text(p.error_category);
    const cost = count(p.cost_usd);
    const exit = count(p.exit_code);
    const recorded = text(p.reason) ?? text(p.error);
    const why = recorded === category ? null : recorded;
    const what =
      category !== null ? errorCategoryLabel(category) : exitedNonZero('dispatch_completed', p) ? `exited ${exit}` : null;
    const failure = what === null ? null : why ? `${what}: ${why}` : what;
    const head = failure !== null ? 'failed' : (text(p.status) ?? (exit === 0 ? 'finished' : null));
    return [head, failure, cost !== null && `$${cost.toFixed(2)}`].filter(Boolean).join(' · ');
  },
  adversarial_review_verdict: (p) => {
    const verdict = text(p.verdict);
    if (verdict === null) return '';
    return count(p.round) !== null ? `round ${p.round}: ${verdict}` : verdict;
  }
};

/** The keys a kind with no sentence of its own is still read for, in order. */
const FALLBACK_KEYS = ['error', 'reason', 'status', 'verdict', 'branch'] as const;

function fallbackSummary(payload: Payload): string {
  for (const key of FALLBACK_KEYS) {
    const value = text(payload[key]);
    if (value !== null) return key === 'error' || key === 'reason' ? value : `${key} ${value}`;
  }
  return '';
}

/** Whether a recorded string is itself a JSON object or array. */
function isJsonValue(value: unknown): boolean {
  const raw = text(value);
  if (raw === null || !/^[{[]/.test(raw)) return false;
  try {
    const parsed: unknown = JSON.parse(raw);
    return typeof parsed === 'object' && parsed !== null;
  } catch {
    return false;
  }
}

/**
 * The payload as a sentence reads it: an `error` or `reason` that is itself
 * a JSON object or array is evidence, not words, so it is left out of the row
 * (it stays behind the row disclosure) and the rest of the sentence stands.
 * A value that only looks like JSON (`[sandbox] container exited 137`) is
 * prose and is kept.
 */
function readable(payload: Payload): Payload {
  const json = ['error', 'reason'].filter((key) => isJsonValue(payload[key]));
  if (json.length === 0) return payload;
  return Object.fromEntries(Object.entries(payload).filter(([key]) => !json.includes(key)));
}

/** One run event as a row reads it. */
export type RunEventLine = {
  /** The event name in words — the shared table's, or the code's own words
   * capitalised (the Monitor's `leadOf`), never the code itself. */
  label: string;
  /** One line of what the payload says, or '' when it says nothing worth a row. */
  summary: string;
  tone: EventTone;
};

/** The row's words: label, one-line summary, tone. Never a JSON string. */
export function runEventLine(event: RunAuditEvent): RunEventLine {
  const payload = readable(event.payload ?? {});
  const sentence = Object.hasOwn(SENTENCES, event.event) ? SENTENCES[event.event] : null;
  const said = sentence ? sentence(payload) : fallbackSummary(payload);
  return { label: leadOf(event.event), summary: said.replace(/\s+/g, ' ').trim(), tone: eventTone(event) };
}

// --- phase names -------------------------------------------------------------

/**
 * The stage a recorded event opens on the run's clock, as the timeline names
 * it. Matched by exact name first and then by prefix; a kind neither knows is
 * its own stage, named in words (the Monitor's `leadOf`), so a segment is
 * never drawn unnamed and never named by a raw code.
 */
const STAGE_BY_NAME: Readonly<Record<string, string>> = {
  dispatch_owner_recorded: 'dispatch',
  dispatch_reservation_bound: 'dispatch',
  dispatch_remote_launch: 'dispatch',
  routing_decision: 'dispatch',
  dispatch_checkout_bound: 'work',
  continuation_checkout_bound: 'work',
  loop_checkout_bound: 'work',
  credential_publication_refused: 'publish',
  credential_already_published: 'publish',
  continuation_publication: 'publish',
  verification_checked: 'review',
  dispatch_completed: 'done'
};

const STAGE_BY_PREFIX: readonly (readonly [string, string])[] = [
  ['sandbox_', 'sandbox'],
  ['sidecar_', 'sandbox'],
  ['container_', 'sandbox'],
  ['runtime_egress', 'sandbox'],
  ['evidence.', 'work'],
  ['artifact_', 'seal'],
  ['publication_', 'publish'],
  ['adversarial_review', 'review'],
  ['review', 'review']
];

/** The stage name the timeline gives the segment an event opens. */
export function stageOf(event: string): string {
  if (Object.hasOwn(STAGE_BY_NAME, event)) return STAGE_BY_NAME[event];
  for (const [prefix, stage] of STAGE_BY_PREFIX) if (event.startsWith(prefix)) return stage;
  return leadOf(event);
}
