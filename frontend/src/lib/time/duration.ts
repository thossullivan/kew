/**
 * #2299 — `formatDuration`: how long something took, in one or two units.
 *
 * The console had `elapsed()` (epic/format.ts), which prints the compact
 * "2h13m" the epic cards were drawn with. This is the reading for everywhere
 * else — the ruler's own spans, a batch's range, a run's duration in a
 * popover — and it fixes the two things a naive formatter gets wrong:
 *
 * * **A unit stops being worth showing.** Past its limit the finer unit is
 *   noise: "1h 40m", never "100m", and "4d" rather than "100h".
 * * **But not too early.** A duration stays in its own unit up to that limit,
 *   so 95 seconds reads "1m 35s" and not "0h 2m" — the limits (120 s, 120 min,
 *   48 h, 14 d) are how far each unit carries before the pair coarsens.
 *
 * Always one or two units: the largest unit with something in it, plus the
 * finest unit still worth showing when that is a different one.
 */

const SECOND = 1_000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const WEEK = 7 * DAY;

/** The ladder, finest first: how long the unit is, and what it is called. */
const UNITS = [
  { ms: SECOND, suffix: 's' },
  { ms: MINUTE, suffix: 'm' },
  { ms: HOUR, suffix: 'h' },
  { ms: DAY, suffix: 'd' },
  { ms: WEEK, suffix: 'w' }
] as const;

/**
 * How long each unit stays the finest one shown, by index into `UNITS`.
 *
 * Seconds up to 120 s, minutes up to 120 min, hours up to 48 h, days up to
 * 14 d. Weeks have no limit: they are the coarsest unit there is here, and a
 * duration measured in months is not a thing kew records.
 */
const UNIT_LIMITS_MS: readonly number[] = [120 * SECOND, 120 * MINUTE, 48 * HOUR, 14 * DAY];

/** A duration that cannot have been meant — negative, NaN — is nothing, never
 * a number with a minus sign in front of it. */
const clamp = (ms: number): number => (Number.isFinite(ms) && ms > 0 ? ms : 0);

const whole = (ms: number, unitMs: number): number => Math.floor(ms / unitMs);

/**
 * "45s", "1m 35s", "1h 40m", "3d 4h", "2w 3d".
 *
 * Sub-second durations read "0s": the finest unit this axis speaks is the
 * second, and "0s" is an honest "no time at all" where "0.4s" would be a
 * precision none of the surfaces that call this can draw.
 */
export function formatDuration(ms: number): string {
  const total = clamp(ms);
  const limited = UNIT_LIMITS_MS.findIndex((limit) => total < limit);
  const fineIndex = limited === -1 ? UNITS.length - 1 : limited;
  const fine = UNITS[fineIndex];
  // The head is the finest unit still worth showing, or the rung above it when
  // there is at least one of those in the duration. The limits above put the
  // two at most one rung apart, which is what keeps the reading to two units.
  const next = UNITS[fineIndex + 1];
  const head = next !== undefined && total >= next.ms ? next : fine;
  const headValue = whole(total, head.ms);
  const rest = head === fine ? 0 : whole(total - headValue * head.ms, fine.ms);
  const lead = `${headValue}${head.suffix}`;
  return rest === 0 ? lead : `${lead} ${rest}${fine.suffix}`;
}
