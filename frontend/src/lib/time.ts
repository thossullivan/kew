/**
 * #2205 — the one timestamp formatter the console has.
 *
 * The first-pass UI printed the same moment three ways on one screen: the
 * Monitor's event rows read 13:33, its time axis read 16:35, and the epic
 * header read `last tick 16:26:16`. Nothing was wrong with any single
 * reading — they were simply rendered in different zones, and a screen that
 * states a time twice in two zones is a screen that cannot be read.
 *
 * So there is exactly one rule, and it lives here:
 *
 * - **The wire is UTC.** The API returns ISO-8601 with an offset. A stamp
 *   that arrives without one is read as UTC, never as the viewer's wall
 *   clock — a legacy row written before the backend was timezone-aware
 *   would otherwise be silently shifted by the viewer's offset.
 * - **The screen is local.** Every timestamp renders in the viewer's own
 *   zone, so "13:33" is the time they were looking at the clock when it
 *   happened.
 * - **The tooltip is UTC.** Every rendered time carries the full UTC instant
 *   in its `title`, which is what two operators in two zones can compare and
 *   what makes the hour that repeats on a DST fall-back unambiguous.
 * - **An unreadable stamp is a dash.** Never a fabricated time, and never the
 *   epoch, which is what `new Date(null)` would hand back.
 *
 * `timeZone` is a parameter only so a test can hold a zone still across a DST
 * boundary; production never passes it and gets the viewer's zone.
 */

/** What a timestamp the backend cannot have meant renders as. */
export const NO_TIME = '—';

/** A trailing `Z` or `±HH:MM` / `±HHMM` — i.e. the wire contract was kept. */
const HAS_OFFSET = /(?:Z|[+-]\d{2}:?\d{2})$/i;
/** Enough of an ISO date-time that appending `Z` is a reading, not a guess. */
const ISO_DATETIME = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/;

/** The widest instant a `Date` can hold; past it every formatter throws. */
const MAX_INSTANT_MS = 8.64e15;

/**
 * Milliseconds since the epoch, or `null` for anything unparseable. Epoch
 * milliseconds pass through so a caller that already has an instant (the
 * event-stream axis derives its ticks from arithmetic on the rows' own
 * instants) formats through the same path as a caller holding a string.
 *
 * A number outside `Date`'s range is `null` rather than a value: `Intl` and
 * `toISOString` both throw `RangeError` on one, and a timestamp must not be
 * able to take a whole screen down.
 */
export function parseInstant(ts: string | number | null | undefined): number | null {
  if (ts === null || ts === undefined) return null;
  if (typeof ts === 'number')
    return Number.isFinite(ts) && Math.abs(ts) <= MAX_INSTANT_MS ? ts : null;
  const text = ts.trim();
  const iso =
    ISO_DATETIME.test(text) && !HAS_OFFSET.test(text) ? `${text.replace(' ', 'T')}Z` : text;
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? null : ms;
}

/**
 * "HH:MM:SS" (or "HH:MM" with `withSeconds=false`) in the viewer's zone.
 *
 * 24-hour and zero-padded regardless of the viewer's *locale*: the artboard
 * draws the time column at a fixed width in a mono face, and an `en-US`
 * viewer getting "1:33:18 PM" would overrun it. It is the zone that follows
 * the viewer, not the notation.
 *
 * The `Intl.DateTimeFormat` is built per call rather than memoized. A cached
 * formatter outlives a change of zone, and the one place the zone moves is a
 * test that walks a DST boundary — a cache there would answer with the zone
 * the first assertion happened to run under.
 */
export function fmtTime(
  ts: string | number | null | undefined,
  withSeconds = true,
  timeZone?: string
): string {
  const ms = parseInstant(ts);
  if (ms === null) return NO_TIME;
  return new Intl.DateTimeFormat('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
    ...(withSeconds ? { second: '2-digit' as const } : {}),
    hourCycle: 'h23',
    timeZone
  }).format(ms);
}

/**
 * "2026-09-29 15:33:18" — a date *and* time in the viewer's zone, for the
 * readings whose day matters as much as their hour: an audit row, the moment a
 * budget override was set, when a config value was last written.
 *
 * ISO-first and locale-independent, unlike the `toLocaleString()` each of
 * those pages used to call for itself — which printed `29/09/2026, 15:33:18`
 * or `9/29/2026, 3:33:18 PM` depending on who was looking, in a column beside
 * times this formatter renders. Same `title` convention: the UTC instant.
 */
export function fmtDateTime(ts: string | number | null | undefined): string {
  const ms = parseInstant(ts);
  if (ms === null) return NO_TIME;
  const parts = new Intl.DateTimeFormat('en-CA', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23'
  }).formatToParts(ms);
  const at = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((part) => part.type === type)?.value ?? '';
  return `${at('year')}-${at('month')}-${at('day')} ${at('hour')}:${at('minute')}:${at('second')}`;
}

/**
 * "Sep 21" — the UTC calendar day a *bucket* is named by, not an instant.
 *
 * The one formatter here that is deliberately NOT in the viewer's zone, and
 * the rule above is why rather than an exception to it: the outcome
 * scorecard's weeks are cut in UTC by SQL (`date(ts, 'weekday 0', '-6 days')`)
 * and by `kew.merged_main`, so the label is the name of a seven-day bucket the
 * server already decided. Rendered locally, the Monday of a week would print
 * as the Sunday before it for every viewer west of UTC, and the axis would
 * disagree with every number plotted against it.
 *
 * No clock: a bucket has no hour, and there is nothing here for a zone to
 * shift once the date itself is fixed.
 */
export function fmtUtcMonthDay(ts: string | number | null | undefined): string {
  const ms = parseInstant(ts);
  if (ms === null) return NO_TIME;
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC'
  }).format(ms);
}

/**
 * "2026-09-29 13:33:18 UTC" — the `title` that goes on every rendered time,
 * so the local reading above can always be resolved to the one instant the
 * backend recorded. Empty for an unreadable stamp: a tooltip saying nothing
 * is better than a tooltip saying the epoch, and `title=""` renders no
 * tooltip at all.
 */
export function utcTitle(ts: string | number | null | undefined): string {
  const ms = parseInstant(ts);
  if (ms === null) return '';
  return `${new Date(ms).toISOString().slice(0, 19).replace('T', ' ')} UTC`;
}
