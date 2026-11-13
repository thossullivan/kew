import type { EpicHeartbeat } from '$lib/api';

export type Staleness =
  | { kind: 'unknown' }
  | { kind: 'fresh'; ageS: number }
  | { kind: 'stale'; lastSeen: string; ageS: number }
  | { kind: 'complete'; finishedAt: string | null }
  | { kind: 'stalled' }
  | { kind: 'oneshot'; ageS: number };

/**
 * The ONE freshness window (#2844 N18): a tick that started `ageS` ago is a
 * heartbeat within max(3x interval, interval + 2x the last tick's duration).
 * The epic page's `staleness()` and the Epics row's `heartbeatFresh`
 * ($lib/epic/roster) both read it, so the list and the page cannot disagree
 * about one driver. A tick from the future (clock skew) is fresh.
 */
export function freshBeat(ageS: number, intervalS: number, durationS: number | null | undefined): boolean {
  return ageS <= Math.max(3 * intervalS, intervalS + 2 * (durationS ?? 0));
}

export function staleness(hb: EpicHeartbeat | null, nowMs: number): Staleness {
  if (!hb || !hb.last_tick_started_at) return { kind: 'unknown' };
  if (hb.last_signal === 'complete')
    return { kind: 'complete', finishedAt: hb.last_tick_ts };   // no stale banner, EVER
  if (hb.last_signal === 'stalled') return { kind: 'stalled' }; // by design, not "gone"
  const ageS = (nowMs - Date.parse(hb.last_tick_started_at)) / 1000;
  if (hb.interval_s == null) return { kind: 'oneshot', ageS };  // no alive/gone claim possible
  if (freshBeat(ageS, hb.interval_s, hb.last_tick_duration_s)) return { kind: 'fresh', ageS };
  return { kind: 'stale', lastSeen: hb.last_tick_started_at, ageS };
}
