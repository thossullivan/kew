// Replay-mode pure logic (#454 Decision 2 / spec §Replay): dwell timing,
// wall-clock display, and scrubber positioning. The wall clock renders through
// the console's one formatter ($lib/time, #2205) so a replayed tick reads in
// the same zone as the live ticker rows above it; the elapsed offset beside it
// is a difference between two instants and has no zone at all.

import { fmtTime, NO_TIME, parseInstant } from '$lib/time';

/** Auto-advance dwell time per frame: ~30s total playback, clamped so very
 * short/long replays stay scrubbable. */
export function dwellMs(frameCount: number): number {
  const raw = 30_000 / frameCount;
  return Math.max(300, Math.min(2000, raw));
}

/** "HH:MM · +XhYYm" — wall time of this frame plus elapsed-since-first-frame
 * offset. First frame renders "+0h00m". Either timestamp being unparseable
 * falls back to the one formatter's own '—' rather than garbling a bogus
 * HH:MM or NaN-derived offset out of an invalid Date. */
export function wallClock(tickTs: string, firstTs: string): string {
  const tickMs = parseInstant(tickTs);
  const firstMs = parseInstant(firstTs);
  if (tickMs === null || firstMs === null) return NO_TIME;
  const diffMs = tickMs - firstMs;
  const totalMin = Math.max(0, Math.round(diffMs / 60_000));
  const oh = Math.floor(totalMin / 60);
  const om = totalMin % 60;
  return `${fmtTime(tickMs, false)} · +${oh}h${String(om).padStart(2, '0')}m`;
}

/** Scrubber stop positions in [0, 1], proportional to REAL tick_ts spacing
 * (not frame index) so long gaps between ticks read as long gaps on the bar. */
export function scrubberStops(ticks: { tick_ts: string }[]): number[] {
  if (ticks.length === 0) return [];
  if (ticks.length === 1) return [0];
  const times = ticks.map((t) => Date.parse(t.tick_ts));
  const t0 = times[0];
  const span = times[times.length - 1] - t0;
  if (span <= 0) return times.map((_, i) => i / (times.length - 1));
  return times.map((t) => (t - t0) / span);
}

/**
 * The clock one frame's cards are read against (#2305).
 *
 * Two different facts used to share one flag, and this is the one that does
 * NOT belong to the stream. A REPLAYED frame is a tick out of the journal, and
 * the only honest "now" for it is the tick's own instant: read against today's
 * wall clock, a week-old tick draws a running bar nearly full and ages every
 * card to this morning — history, in the colours of work in flight.
 *
 * A LIVE frame is read against the wall clock whether or not the stream is
 * still up. A dropped stream stops the motion, because a bar that keeps
 * growing on a severed channel draws seconds nothing confirmed; it does not
 * move the frame's clock. Reading a live frame against its own `tick_ts` on a
 * drop would jump every age BACKWARDS to the last tick and freeze it there,
 * understating how long the work has been in flight for as long as the server
 * stays down — the opposite of what stopping is for.
 *
 * An unreadable `tick_ts` falls back to the wall clock rather than to nothing.
 */
export function frameClock(
  replay: boolean,
  tickTs: string | null | undefined,
  wallNow: number
): number {
  if (!replay) return wallNow;
  return parseInstant(tickTs) ?? wallNow;
}

/**
 * The replay speeds a link can carry (`?speed=`, #3142). A multiplier on the
 * playback pace `dwellMs` sets, not on the journal's own clock: at 1x a
 * two-day epic still plays in about thirty seconds, and 60x is the same
 * replay sixty times faster.
 */
export const REPLAY_SPEEDS = [1, 10, 60] as const;
export type ReplaySpeed = (typeof REPLAY_SPEEDS)[number];

/** `?speed=` as a speed this page offers; anything else is the default 1x. */
export function parseSpeed(raw: string | null | undefined): ReplaySpeed {
  const n = Number(raw);
  return REPLAY_SPEEDS.find((s) => s === n) ?? 1;
}

/** `?t=` as whole seconds since the first frame, or null when it names none. */
export function parseT(raw: string | null | undefined): number | null {
  if (raw == null || !/^\d+$/.test(raw)) return null;
  return Number(raw);
}

/** The pause between two frames at a speed: `dwellMs`'s pace, `speed` times faster. */
export function playbackDwellMs(frameCount: number, speed: ReplaySpeed): number {
  return dwellMs(frameCount) / speed;
}

/**
 * Each frame's moment as `?t=` spells it: whole seconds since the first frame,
 * floored, so the `t` a frame writes reads back to that same frame. Never
 * decreasing — an unreadable `tick_ts` takes the moment before it — so the
 * frame a `t` names is one frame and not a guess between two.
 */
export function frameOffsets(ticks: { tick_ts: string }[]): number[] {
  const first = parseInstant(ticks[0]?.tick_ts);
  let last = 0;
  return ticks.map((tick) => {
    const ms = parseInstant(tick.tick_ts);
    if (first !== null && ms !== null) last = Math.max(last, Math.floor((ms - first) / 1000));
    return last;
  });
}

/**
 * The frame on screen at `t` seconds: the last one recorded at or before it.
 * Pure, so the same `t` over the same journal always draws the same graph; a
 * `t` past the end is the last frame, the epic as it stands.
 */
export function frameAtT(ticks: { tick_ts: string }[], t: number): number {
  const offsets = frameOffsets(ticks);
  let index = 0;
  offsets.forEach((offset, i) => {
    if (offset <= t) index = i;
  });
  return index;
}
