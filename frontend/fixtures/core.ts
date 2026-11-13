/**
 * The pure half of the fixture API (#3139): how an `/api` read maps to a
 * fixture file, how a committed instant is moved to now, and how the two reads
 * answered per query — the audit list and its histogram — are computed.
 *
 * Nothing here touches the disk or a server, so two readers share it: the dev
 * plugin (`plugin.ts`, which reads the files and re-exports every name here)
 * and the demo build's shim (`src/lib/demo/`), which answers the same reads in
 * the browser out of a bundled copy of a fixture set. One module is what keeps
 * the demo's answers the dev server's answers.
 */

/** Segments are compared after decoding; a dot cannot appear, so no traversal. */
const SEGMENT = /^[A-Za-z0-9_-]+$/;

/**
 * A path segment may also carry `:`, because one id the server mints does: a
 * sealed artifact's node is `<run>:<through_seq>` (`artifact_node_id` in
 * `kew/flight/evidence.py`). A colon is not a portable file-name character, so
 * it is read as `_` in the file name: `/api/runs/f5dcb6a5/nodes/f5dcb6a5:4/diff`
 * reads `runs-f5dcb6a5-nodes-f5dcb6a5_4-diff.json`.
 */
const PATH_SEGMENT = /^[A-Za-z0-9_:-]+$/;

/** Live reads the app opens as an EventSource rather than a single response. */
const STREAM_TAILS = new Set(['stream', 'events']);

function segments(url: string): string[] | null {
  const path = url.split(/[?#]/)[0];
  if (!path.startsWith('/api/')) return null;
  const parts = path.slice('/api/'.length).split('/').filter((p) => p !== '');
  if (parts.length === 0) return null;
  const decoded: string[] = [];
  for (const part of parts) {
    let segment: string;
    try {
      segment = decodeURIComponent(part);
    } catch {
      return null;
    }
    if (!PATH_SEGMENT.test(segment)) return null;
    decoded.push(segment.replaceAll(':', '_'));
  }
  return decoded;
}

/**
 * The fixture file `url` reads, or null when the path is not a fixture-able
 * `/api` read. Null is also the answer for anything that could escape the
 * fixture directory — a rejected path is never resolved against the disk.
 */
export function fixtureFileFor(url: string): string | null {
  const parts = segments(url);
  if (parts === null) return null;
  return `${parts.join('-')}.json`;
}

/**
 * Reads answered per item rather than once: the read's base name, and the
 * query parameter that names the item. One committed file answering every item
 * printed `kew dispatch 2302` in the drawer for item 2060 (#2845 P5).
 */
const KEYED_READS: Readonly<Record<string, string>> = { 'dispatch-resolve': 'item' };

/**
 * The per-item file a keyed read answers out of — `/api/dispatch/resolve?item=2060`
 * reads `dispatch-resolve/2060.json` — or null when `url` is not a keyed read
 * or names no item. The key is held to the same segment rule as a path, so it
 * cannot leave the read's own folder.
 */
export function keyedFixtureFileFor(url: string): string | null {
  const parts = segments(url);
  if (parts === null) return null;
  const name = parts.join('-');
  const param = KEYED_READS[name];
  if (param === undefined) return null;
  const query = url.split('#')[0].split('?')[1] ?? '';
  const key = new URLSearchParams(query).get(param);
  if (key === null || key === '' || !SEGMENT.test(key)) return null;
  return `${name}/${key}.json`;
}

/** True for the SSE reads (`…/stream`, `…/events`) that have no JSON body. */
export function isStreamPath(url: string): boolean {
  const parts = segments(url);
  return parts !== null && STREAM_TAILS.has(parts[parts.length - 1]);
}

/**
 * A full ISO-8601 instant — date, time AND an explicit zone. Deliberately
 * strict: a bare date (`2026-10-03`), a version, a run id or a sha must never
 * be mistaken for a time and rewritten.
 */
export const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

/**
 * The payload as if the whole fixture set had been read NOW.
 *
 * A fixture is a frozen reading, and most of this console prints one as it
 * stands. But some readings are GATED on their own age: the needs-rebase badge
 * is withheld once the journaled base reading behind it is over an hour old
 * (`$lib/freshness`'s `BASE_READING_MAX_AGE_MS`, mirroring the server's bound),
 * because a magenta badge is an ask of a PERSON and a stale journal row is not
 * grounds for one. Served verbatim, the committed conflicted row is hours —
 * then days — past that bound, so the one magenta badge on the fixture surface
 * is dark on every day but the one the fixture was written, which is the same
 * screen as "nothing needs rebasing". A design surface that cannot draw a state
 * cannot be reviewed for it.
 *
 * So every instant is shifted by ONE delta, `now - epoch` — the same
 * delta for every file, every request and every surface. Relative structure is
 * what the artboards pin — this ran four minutes before that, this verdict
 * predates that push — and the surfaces here read it ACROSS files: the epic
 * page reads a work view and that item's evidence as separate `/api` calls and
 * draws the rows against the run window. A delta taken from each payload's own
 * `composed_at` would move those two by different amounts and tear exactly that
 * relation apart (reviewer finding, PR #2391), so the delta belongs to the set,
 * not to a payload. Nothing is invented: a payload with no instants in it comes
 * back byte for byte as it was committed.
 *
 * `epoch` is the instant the set is read AS: `FIXTURE_EPOCH` for the factory
 * set (the dev plugin's default), and its own for any other set the demo
 * build bundles (#3139).
 */
export function rebaseClock(
  payload: unknown,
  nowMs: number,
  epoch: string
): unknown {
  return shift(payload, nowMs - Date.parse(epoch));
}

/**
 * Every instant in `value`, moved by `deltaMs`; everything else untouched —
 * and `value` itself, by identity, when it holds no instant to move, which is
 * what lets `fixtureBody` answer such a fixture with its committed bytes.
 */
function shift(value: unknown, deltaMs: number): unknown {
  if (typeof value === 'string') {
    if (!INSTANT.test(value)) return value;
    const ms = Date.parse(value);
    return Number.isNaN(ms) ? value : new Date(ms + deltaMs).toISOString();
  }
  if (Array.isArray(value)) {
    const moved = value.map((item) => shift(item, deltaMs));
    return moved.every((item, i) => item === value[i]) ? value : moved;
  }
  if (value !== null && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>);
    const moved = entries.map(([k, v]) => [k, shift(v, deltaMs)] as [string, unknown]);
    return moved.every(([, v], i) => v === entries[i][1]) ? value : Object.fromEntries(moved);
  }
  return value;
}

/**
 * The bytes a fixture file is answered with: its own, unless it holds an
 * instant — in which case the rebased payload, re-serialised.
 */
export function fixtureBody(
  raw: string,
  nowMs: number,
  epoch: string
): string {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    // A fixture that is not JSON is a committed mistake, not a request to
    // handle: serve it as-is and let the reader see the parse error.
    return raw;
  }
  const rebased = rebaseClock(parsed, nowMs, epoch);
  return rebased === parsed ? raw : JSON.stringify(rebased);
}

/** `MAX_BUCKETS` in `kew/src/kew/service/audit.py`: past it the server 422s. */
const MAX_HISTOGRAM_BUCKETS = 500;

export type Counted = { name: string; ms: number; scoped: AuditScope | null };
type AuditScope = {
  epic: string | null;
  item: string | null;
  issue: string | null;
  actor: string | null;
};

export type Listed = {
  id: number;
  event: string;
  timestamp: string;
  epic_ref: string | null;
  issue_num: number | null;
  item_id: string | null;
  actor_type: string | null;
};

/** One listed event as the filters and the histogram read it. */
function counted(e: Listed): Counted {
  return {
    name: e.event,
    ms: Date.parse(e.timestamp),
    scoped: {
      epic: e.epic_ref,
      item: e.item_id,
      issue: e.issue_num === null ? null : String(e.issue_num),
      actor: e.actor_type
    }
  };
}

/** The committed histogram's shape: one fixed run of buckets, counted by name. */
export type Background = { buckets: { start: string; counts: Record<string, number> }[] };

/**
 * What the overview counts: every event the list serves, at its own instant,
 * and the committed histogram's buckets as the factory's background — which
 * ends before the list's oldest event, so nothing is counted twice. A
 * background bucket names no epic, item or actor, so a read scoped to one
 * counts none of it rather than guessing.
 */
export function countedEvents(listed: Listed[], background: Background): Counted[] {
  return [
    ...listed.map(counted),
    ...background.buckets.flatMap((bucket) =>
      Object.entries(bucket.counts).flatMap(([name, n]) =>
        Array.from({ length: n }, () => ({ name, ms: Date.parse(bucket.start), scoped: null }))
      )
    )
  ];
}

/** `MAX_ISSUE_NUM_DIGITS` in `kew/src/kew/ids.py`. */
const MAX_ISSUE_NUM_DIGITS = 18;

/**
 * Whether an event is the work item `item=` names, as `_audit_item_filters`
 * reads it: its `item_id`, or — when the value is an issue number — its
 * `issue_num`, the two keys the same item can be recorded under. The Monitor
 * sends an issue number as `item=`, so this is never an `item_id`-only match.
 */
function isItem(scope: AuditScope | null, value: string): boolean {
  if (scope === null) return false;
  if (scope.item === value) return true;
  const numeric = /^[0-9]+$/.test(value) && value.length <= MAX_ISSUE_NUM_DIGITS;
  return numeric && scope.issue === String(BigInt(value));
}

/**
 * Whether one counted event passes the read's filters, as the store applies
 * them: `item` matches `item_id` or a numeric value's `issue_num` (`isItem`),
 * `issue` matches `issue_num` alone, and a read naming both must pass both
 * (`_audit_where`).
 */
function inScope(e: Counted, q: URLSearchParams): boolean {
  const prefixes = q.getAll('event_prefix');
  if (prefixes.length > 0 && !prefixes.some((p) => e.name.startsWith(p))) return false;
  const event = q.get('event');
  if (event !== null && e.name !== event) return false;
  const item = q.get('item');
  if (item !== null && !isItem(e.scoped, item)) return false;
  const wanted: [string | null, keyof AuditScope][] = [
    [q.get('epic'), 'epic'],
    [q.get('issue'), 'issue'],
    [q.get('actor_type'), 'actor']
  ];
  return wanted.every(([value, key]) => value === null || e.scoped?.[key] === value);
}

/**
 * One `/api/audit/histogram` read over `counted`, answered for the window it
 * asks about (#2847).
 *
 * The committed histogram is one fixed day of 15-minute buckets, and served as
 * a file it ignored the query: the Monitor's live 30-minute range asked for
 * 30-second buckets and got a day of quarter-hours, some of them outside the
 * range it drew. The server tiles `[since, until)` by `bucket_s` and counts
 * the log into it; so does this, over the events the fixture set holds. Every
 * bucket is therefore inside the requested range, dense, and summing to
 * `total` — the shape `service/audit.py` serves. A read with no window is not
 * one the console makes; it answers null, and the caller serves the committed
 * file, rebased like any other.
 */
export function auditHistogramPage(
  counted: Counted[],
  url: string
): { status: number; body: unknown } | null {
  const q = new URLSearchParams(url.split('#')[0].split('?')[1] ?? '');
  const [since, until, width] = [q.get('since'), q.get('until'), q.get('bucket_s')];
  if (since === null || until === null || width === null) return null;
  const startMs = Date.parse(since);
  const endMs = Date.parse(until);
  const bucketS = Number(width);
  if (Number.isNaN(startMs) || Number.isNaN(endMs) || !Number.isInteger(bucketS) || bucketS < 1)
    return { status: 422, body: { detail: 'since, until and bucket_s must be read' } };
  const bucketMs = bucketS * 1000;
  const count = Math.ceil((endMs - startMs) / bucketMs);
  if (endMs <= startMs || count > MAX_HISTOGRAM_BUCKETS)
    return { status: 422, body: { detail: `${count} buckets requested` } };
  const buckets = Array.from({ length: count }, (_, i) => ({
    start: new Date(startMs + i * bucketMs).toISOString(),
    counts: {} as Record<string, number>
  }));
  let total = 0;
  for (const e of counted) {
    if (e.ms < startMs || e.ms >= endMs || !inScope(e, q)) continue;
    const counts = buckets[Math.floor((e.ms - startMs) / bucketMs)].counts;
    counts[e.name] = (counts[e.name] ?? 0) + 1;
    total += 1;
  }
  return {
    status: 200,
    body: {
      since: new Date(startMs).toISOString(),
      until: new Date(endMs).toISOString(),
      bucket_s: bucketS,
      buckets,
      total
    }
  };
}

/** `MAX_LIMIT` in `kew/src/kew/service/audit.py`, and the route's default page. */
const MAX_LIST_LIMIT = 200;
const DEFAULT_LIST_LIMIT = 100;

/** An optional integer query param: absent, a whole number, or unreadable. */
function intParam(q: URLSearchParams, key: string): number | null | undefined {
  const raw = q.get(key);
  if (raw === null) return null;
  const n = Number(raw);
  return raw.trim() !== '' && Number.isInteger(n) ? n : undefined;
}

/** An optional instant query param: absent, epoch ms, or unreadable. */
function instantParam(q: URLSearchParams, key: string): number | null | undefined {
  const raw = q.get(key);
  if (raw === null) return null;
  const ms = Date.parse(raw);
  return Number.isNaN(ms) ? undefined : ms;
}

/**
 * `/api/audit`, answered for the query it asks (#2875).
 *
 * Served as a file, the list ignored the query: a custom Monitor range over a
 * past hour listed the events near now, outside the window the header named,
 * and the list does not clip on the client — the server does. So this applies
 * what `service/audit.py` applies: `[since, until)`, the scope filters the
 * histogram shares, the strict `after` id cursor, newest first, `limit` per
 * page, and `next_after` only when a further page exists. Every row of a read
 * is therefore one the server would have served for it.
 *
 * One read over `events`, so the dev plugin and the demo build answer it from
 * the same rows.
 */
export function auditListPage(events: Listed[], url: string): { status: number; body: unknown } {
  const q = new URLSearchParams(url.split('#')[0].split('?')[1] ?? '');
  const [since, until] = [instantParam(q, 'since'), instantParam(q, 'until')];
  const [after, limit, issue] = [intParam(q, 'after'), intParam(q, 'limit'), intParam(q, 'issue')];
  const page = limit ?? DEFAULT_LIST_LIMIT;
  if ([since, until, after, limit, issue].includes(undefined) || page < 1 || page > MAX_LIST_LIMIT)
    return { status: 422, body: { detail: `${url} is not a read the server would answer` } };
  const matched = events
    .filter((e) => {
      const at = counted(e);
      return (
        (since === null || at.ms >= since!) &&
        (until === null || at.ms < until!) &&
        (after === null || e.id < after!) &&
        inScope(at, q)
      );
    })
    .sort((a, b) => b.id - a.id);
  const rows = matched.slice(0, page);
  const more = matched.length > page;
  return { status: 200, body: { events: rows, next_after: more ? rows[rows.length - 1].id : null } };
}

/** `MAX_RUN_AUDIT` in kew/service/audit.py: the most events one run page holds. */
const MAX_RUN_AUDIT = 200;

/** Whether a read's fixture file is a run's audit page (`runs-<id>-audit.json`). */
export function isRunAuditFile(file: string | null): boolean {
  return file !== null && /^runs-[^/]+-audit\.json$/.test(file);
}

/**
 * `/api/runs/{id}/audit`, answered for the cursor it asks about (#3461, C11).
 *
 * Served as a file, the page ignored `after_seq`: a live run's page polls
 * every second from the last `next_seq` it read and appends what comes back,
 * so every poll handed it the whole run again and the event list rebuilt and
 * jumped to its foot each second. This applies what `run_audit_page` in
 * `service/audit.py` applies: events with a seq past the strict lower-exclusive
 * cursor, oldest first, `limit` of them, and `next_seq` the last one served —
 * or the cursor itself when none is, so a poll past the end returns nothing.
 *
 * One read over the committed page, so the dev plugin and the demo build
 * answer it from the same rows.
 */
export function runAuditPage(
  page: { run_id: string; events: { seq: number }[] },
  url: string
): { status: number; body: unknown } {
  const q = new URLSearchParams(url.split('#')[0].split('?')[1] ?? '');
  const [after, limit] = [intParam(q, 'after_seq'), intParam(q, 'limit')];
  const cursor = after ?? 0;
  const size = limit ?? MAX_RUN_AUDIT;
  if (after === undefined || limit === undefined || cursor < 0 || size < 1 || size > MAX_RUN_AUDIT)
    return { status: 422, body: { detail: `${url} is not a read the server would answer` } };
  if (!Array.isArray(page.events)) return { status: 200, body: page };
  const events = page.events
    .filter((e) => e.seq > cursor)
    .sort((a, b) => a.seq - b.seq)
    .slice(0, size);
  return {
    status: 200,
    body: { ...page, events, next_seq: events.length > 0 ? events[events.length - 1].seq : cursor }
  };
}

/**
 * True for a request this middleware owns: `/api` itself or anything under
 * `/api/`. Everything else — the SvelteKit routes the harness photographs,
 * `/_app/...` assets, Vite's `/@vite/...` and `/@fs/...` — belongs to Vite.
 */
export function ownsPath(url: string): boolean {
  const path = url.split(/[?#]/)[0];
  return path === '/api' || path.startsWith('/api/');
}
