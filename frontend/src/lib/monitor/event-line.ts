/**
 * #2210 — one audit event, read as one line.
 *
 * `/api/audit` carries each event's payload as a JSON excerpt of itself
 * (`_summary` in kew/src/kew/service/audit.py: compact JSON, capped at 200
 * characters with a trailing `…`). The Monitor printed `event · summary`, so a
 * refusal read `dispatch_refused · {"reason":"budget_exceeded","scope":"weekly"`
 * — a row the reader has to parse rather than read.
 *
 * The line this module composes is **kind · subject · fact**: the event name in
 * words, the epic/item/PR the event names, and the one fact worth a row. What
 * the log recorded stays available as `detail`, which the component shows on
 * expand; nothing is ever dropped on the floor.
 *
 * Three rules keep it from inventing readings.
 *
 * **The backend's own words, kept.** `fact` and `line` are the values exactly
 * as recorded — `budget_exceeded`, `READY`, a branch — because this surface is
 * read as evidence. The row prints `plain` instead (#2896 R5): the same fact
 * with each recorded value worded through `$lib/factory/names`, so a status
 * move reads "in review → adversarial review", and the recorded code stays on
 * the row as its `title`. An identifier (a branch, a sha) is never reworded,
 * nor is an `error` or `warning`; a `reason` is worded only when it is a code
 * kew is known to record.
 *
 * **A closed set of keys.** The fact is the first key present from a named
 * list, so a payload field added after this was written leaves the line short
 * (kind and subject) and stays visible in the expand — never promoted to "the
 * key fact" by a guess at its name.
 *
 * **An excerpt the cap cut is still read.** A 200-character truncation is not
 * valid JSON, and the events most worth reading (a refusal, a verdict) are the
 * ones with payloads big enough to be cut. The pairs the excerpt does hold are
 * lifted out with a scanner, and `detail` then shows the raw excerpt rather
 * than a reformatting of something that did not parse.
 *
 * `detail` is also what decides whether the row is a control at all, so the
 * keys the line consumed are tracked as it is composed: a payload whose every
 * field reached the line has nothing left to expand, and one with a field the
 * line does not carry — a nested object, a `null`, a key this module does not
 * read — keeps its expand. `_summary` serializes a non-empty dict on every
 * payload-bearing event, so that test is what stands between the stream and a
 * row full of buttons that open a repeat of themselves.
 */
import type { AuditEvent } from '$lib/api';
import { eventLabel, refusalReasonLabel, valueLabel } from '$lib/factory/names';
import { itemRef } from '$lib/utils';
import { queueAttention } from '$lib/monitor/merge-queue-attention';

/** An event name in words. `publication_output_scan` → "publication output scan". */
export function eventWords(name: string): string {
  return name.replace(/_/g, ' ');
}

/**
 * The row's lead. The shared table words every type kew writes (#2787 N8:
 * "Review.outcome" was this module capitalising a dotted code); a type the
 * table does not know still leads as words, never as an identifier (#2282 D15).
 * The run page's rows and stage names lead the same way (#3458).
 */
export function leadOf(name: string): string {
  const label = eventLabel(name);
  if (label !== name) return label;
  const words = eventWords(name);
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** The reading of one event: the row's line, and what it was read from. */
export type EventLine = {
  /** The event kind in words. */
  kind: string;
  /**
   * The same, as the row's lead: "Review outcome". The worded name from the
   * shared table in `$lib/factory/names` (#2787 N8), so the Monitor and the
   * run page name one event the same way; a type the table does not know
   * leads as its words, capitalised (see leadOf).
   */
  label: string;
  /** "go-live · #2055 · PR #2059", or '' when the event names nothing. */
  subject: string;
  /**
   * The one fact as recorded ("in_review → done"), or '' when the payload
   * carries none this module reads. The row keeps it as its `title`.
   */
  fact: string;
  /**
   * The same fact with its recorded values worded ("in review → done"): what
   * the row prints. Prose the log recorded as prose is its own words already
   * and stands as it is.
   */
  plain: string;
  /**
   * "kind · subject · fact", the whole reading as one string.
   *
   * The Monitor's list does NOT print this: it prints `label` and then the
   * rest, so the lead can be a label while the recorded words stay secondary
   * (#2282 D15). This is the same composition in one piece — what the reading
   * is held to as a whole, and what any surface wanting one line should take.
   */
  line: string;
  /**
   * What the log recorded, for the expand: the payload formatted, or the raw
   * excerpt when it did not parse. Empty when the line already prints
   * everything recorded — an expand that repeats its own row is a control that
   * promises more and shows the same. A cut excerpt always keeps its expand:
   * what the cap took is, by definition, not on the line.
   */
  detail: string;
};

/** The payload's top-level scalars, keyed, or `null` for a prose summary. */
type Fields = ReadonlyMap<string, string>;

/**
 * `"key":value` pairs, read off an excerpt that did not parse. Scalars only:
 * a nested object cut in half has no reading, and the expand shows it whole.
 */
const PAIR = /"([a-z_0-9]+)"\s*:\s*(?:"((?:[^"\\]|\\.)*)"|(-?[0-9][0-9.eE+-]*|true|false))/g;

/** Scalars only, for the same reason: one line holds one fact, not a tree. */
function scalars(payload: Record<string, unknown>): Fields {
  const fields = new Map<string, string>();
  for (const [key, value] of Object.entries(payload)) {
    if (value === null || typeof value === 'object') continue;
    fields.set(key, String(value));
  }
  return fields;
}

function scan(text: string): Fields {
  const fields = new Map<string, string>();
  for (const m of text.matchAll(PAIR)) fields.set(m[1], m[2] ?? m[3]);
  return fields;
}

/** What one summary holds: the payload read, or the prose it is instead. */
type Summary = {
  fields: Fields | null;
  prose: string;
  /**
   * Every top-level key of the whole payload, or `null` when there is no whole
   * payload to hold the line against: prose, nothing at all, or an excerpt the
   * cap cut. `scalars` drops nulls and nested objects and this does not, so a
   * field the line cannot carry still counts as something left to show.
   */
  keys: ReadonlySet<string> | null;
};

/** The summary, read as a payload (`fields`) or as prose. */
function read(summary: string | null | undefined): Summary {
  const text = (summary ?? '').trim();
  if (text === '') return { fields: null, prose: '', keys: null };
  if (!text.startsWith('{')) return { fields: null, prose: text, keys: null };
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const payload = parsed as Record<string, unknown>;
      return { fields: scalars(payload), prose: '', keys: new Set(Object.keys(payload)) };
    }
  } catch {
    /* the cap cut it mid-payload — scan what it does hold */
  }
  return { fields: scan(text), prose: '', keys: null };
}

/** One value the line took, and the key it came from — see `Part`. */
type Taken = { key: string; value: string };

function first(fields: Fields, keys: readonly string[]): Taken | null {
  for (const key of keys) {
    const value = (fields.get(key) ?? '').trim();
    if (value !== '') return { key, value };
  }
  return null;
}

/**
 * One piece of the line and the payload keys it spent. The keys are what the
 * expand is decided on: a line that spent them all has nothing more to show.
 */
type Part = { text: string; keys: string[]; plain?: string };

function spent(taken: readonly (Taken | null)[]): string[] {
  return taken.filter((one): one is Taken => one !== null).map((one) => one.key);
}

/** The keys that name an epic, a work item and a pull request, in each case
 * every spelling the backend's payloads use (`grep payload={` in kew/src). */
const EPIC_KEYS = ['epic_ref', 'epic'] as const;
const ITEM_KEYS = ['item_id', 'issue', 'issue_num', 'item'] as const;
const PR_KEYS = ['pr_number', 'pull_request_number'] as const;
const PR_URL_KEYS = ['pull_request_url', 'pr_url'] as const;
const HOST_KEYS = ['host'] as const;
const HOST_ADDRESS_KEYS = ['address'] as const;
const REPO_KEYS = ['repo'] as const;

/** The number at the end of a PR URL, which is how a PR is referred to. */
const URL_NUMBER = /(\d+)\/?$/;

/** The PR the payload names: by number, or by the number ending its URL. */
function prOf(fields: Fields): Taken | null {
  const pr = first(fields, PR_KEYS);
  if (pr) return pr;
  const url = first(fields, PR_URL_KEYS);
  const number = url && URL_NUMBER.exec(url.value)?.[1];
  // No number in it: the URL was not read, so it is still worth an expand.
  return url && number ? { key: url.key, value: number } : null;
}

/**
 * "go-live · #2055 · PR #2059" — the refs the event names, in one order.
 *
 * The epic is `event.epic_ref`, the API's own column, which the service reads
 * off the FULL payload (`service/audit.py`) before `_summary` caps the excerpt
 * at 200 characters. The epic used to be parsed out of that excerpt instead,
 * and the events whose payloads are big enough to be cut — an adversarial
 * verdict, a refusal — are exactly the ones that then lost their epic from the
 * row (#2248). The payload's own epic key is still SPENT when it holds one, so
 * a row whose every field reached the line keeps offering no expand.
 *
 * A merge-queue event leads with its repository (#2982): one ledger can hold
 * several queues, and "main is red" is only a fact about one of them.
 */
function subjectOf(event: AuditEvent, fields: Fields | null): Part {
  const namesHost = event.event.startsWith('host_lease_');
  const host = namesHost && fields ? first(fields, HOST_KEYS) : null;
  const address = namesHost && fields ? first(fields, HOST_ADDRESS_KEYS) : null;
  const namesRepo = event.event.startsWith('merge_queue_');
  const repo = namesRepo && fields ? first(fields, REPO_KEYS) : null;
  const epic = fields && first(fields, EPIC_KEYS);
  const item = fields && first(fields, ITEM_KEYS);
  const pr = fields && prOf(fields);
  const ref = itemRef(item?.value, event.issue_num);
  const hostContext = [host?.value ?? '', address?.value ?? ''].filter(
    (part, index, parts) => part !== '' && parts.indexOf(part) === index
  );
  const parts = [
    ...hostContext,
    repo?.value ?? '',
    event.epic_ref ?? epic?.value ?? '',
    ref,
    pr ? `PR #${pr.value}` : ''
  ];
  return {
    text: parts.filter((part) => part !== '').join(' · '),
    keys: spent([host, address, repo, epic, item, pr])
  };
}

/**
 * Keys whose value says what it is: a refusal reason, a verdict, an outcome.
 * Priority order — the first one present is the row's fact.
 *
 * `posture` sits above `reason` because it is the containment CLAIM, and these
 * rows are read for it: `sandbox_resolved` carries `posture` with `reason` at
 * `null` whenever containment held, so a reason-first reading printed "attempt
 * number 1" on a sandboxed run and on an uncontained one alike. A verdict and
 * a gate's `result` still outrank it — `merge_queue` records both, and pass or
 * fail is that row's headline — and the reason that chose a posture is one
 * click away in the expand.
 */
const BARE_FACT = [
  'verdict',
  'result',
  'posture',
  'reason',
  'outcome',
  'decision',
  'status',
  'state',
  'error',
  'rule',
  'warning'
] as const;

/** Named facts whose value is an identifier, printed as recorded, never worded. */
const IDENTIFIER_FACT: ReadonlySet<string> = new Set(['branch', 'head_sha']);

/** Keys that need their own name beside the value to mean anything. */
const NAMED_FACT = [
  'exit_code',
  'cost_usd',
  'estimate_usd',
  'threshold_usd',
  'attempt_number',
  'attempt',
  'scope',
  'step',
  'source',
  'category',
  'kind',
  'branch',
  'head_sha'
] as const;

/** A `*_usd` field reads as money, at the precision the factory prints money. */
function money(value: string): string | null {
  const n = Number(value);
  return Number.isFinite(n) ? `$${n.toFixed(2)}` : null;
}

/**
 * A named fact, and the key it spends — spent only where the value on the line
 * is the one recorded. A money figure is rounded to the cent, so a payload
 * whose only field is a cost keeps its expand: `$0.46` and `0.4567` are not
 * the same claim, and the one in the chain must stay reachable.
 */
function namedFact(key: string, value: string): Part {
  if (key.endsWith('_usd')) {
    const amount = money(value);
    if (amount) return { text: `${eventWords(key.replace(/_usd$/, ''))} ${amount}`, keys: [] };
  }
  const plain = IDENTIFIER_FACT.has(key) ? value : codeLabel(value);
  return { text: `${eventWords(key)} ${value}`, keys: [key], plain: `${eventWords(key)} ${plain}` };
}

/** One recorded code: a single run of word characters, nothing free-form. */
const CODE = /^[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*$/;

/**
 * A recorded value, worded only when it is a code (#2896). A status move or a
 * named fact can still carry free-form text — `src/a_b.py` — and wording it
 * rewrote the path inside it, so anything that is not one bare code prints
 * exactly as recorded.
 */
function codeLabel(value: string): string {
  return CODE.test(value.trim()) ? valueLabel(value) : value;
}

/** Bare facts that hold text, not codes: printed exactly, whatever their shape. */
const VERBATIM_FACT: ReadonlySet<string> = new Set(['error', 'warning']);

/**
 * A bare fact, worded by its field (#2896). An `error` or `warning` is text —
 * `classifier_unavailable` there is the message — so it is never worded. A
 * `reason` is worded only when `REFUSAL_REASON_LABELS` knows its code; any
 * other reason prints as recorded. Every other bare fact is a code.
 */
function bareLabel(key: string, value: string): string {
  if (VERBATIM_FACT.has(key)) return value;
  if (key === 'reason') return refusalReasonLabel(value) ?? value;
  return codeLabel(value);
}

/**
 * "in_review → done" — a payload that records a move, read as the move.
 *
 * `epic_node_status` is the stream's most repeated row: the driver emits one
 * per node per transition, and the list printed `epic node status · ui-refine ·
 * #2219` over and over, the one thing that made each row different being in the
 * payload behind it (#2248). A `from`/`to` pair is the whole reading, so it
 * outranks the single-key facts below.
 *
 * Both halves must be present and scalar. A payload carrying `from` as an
 * object (`run_store_publication_jobs.py` records an override that way) has no
 * pair here, because `scalars` already dropped it — so this stays a reading of
 * two recorded words and never a half-stated arrow.
 */
function transitionOf(fields: Fields): Part | null {
  const from = (fields.get('from') ?? '').trim();
  const to = (fields.get('to') ?? '').trim();
  if (from === '' || to === '') return null;
  return {
    text: `${from} → ${to}`,
    keys: ['from', 'to'],
    plain: `${codeLabel(from)} → ${codeLabel(to)}`
  };
}

/** The one fact the line carries, or '' when the payload holds none it reads. */
function factOf(fields: Fields): Part {
  const moved = transitionOf(fields);
  if (moved) return moved;
  const bare = first(fields, BARE_FACT);
  if (bare) {
    return { text: bare.value, keys: [bare.key], plain: bareLabel(bare.key, bare.value) };
  }
  const named = first(fields, NAMED_FACT);
  return named ? namedFact(named.key, named.value) : { text: '', keys: [] };
}

/** *fields* without *key*. */
function withoutKey(fields: Fields, key: string): Fields {
  const rest = new Map(fields);
  rest.delete(key);
  return rest;
}

/** The payload as the expand shows it: formatted when it parsed, else raw. */
function detailOf(summary: string): string {
  try {
    return JSON.stringify(JSON.parse(summary), null, 2);
  } catch {
    return summary;
  }
}

/** True when every field the payload holds reached the line. */
function printsEverything(keys: ReadonlySet<string> | null, parts: readonly Part[]): boolean {
  if (keys === null) return false;
  const printed = new Set(parts.flatMap((part) => part.keys));
  return [...keys].every((key) => printed.has(key));
}

/** One event, read as one line. Pure: same event in, same line out. */
export function eventLine(event: AuditEvent): EventLine {
  const { fields, prose, keys } = read(event.summary);
  const kind = eventWords(event.event);
  const subject = subjectOf(event, fields);
  // A merge-queue attention leads with what its reason code asks (#2982), so
  // the code itself is spent by the lead rather than printed again as a fact.
  const queue = queueAttention(event);
  const lead: Part = queue?.code ? { text: queue.action, keys: ['reason'] } : { text: '', keys: [] };
  const factFields = fields && queue?.code ? withoutKey(fields, 'reason') : fields;
  const fact = factFields === null ? { text: prose, keys: [] } : factOf(factFields);
  const nothingMore = fields === null || printsEverything(keys, [subject, fact, lead]);
  return {
    kind,
    label: queue ? queue.action : leadOf(event.event),
    subject: subject.text,
    fact: fact.text,
    plain: fact.plain ?? fact.text,
    line: [kind, subject.text, fact.text].filter((part) => part !== '').join(' · '),
    detail: nothingMore ? '' : detailOf((event.summary ?? '').trim())
  };
}
