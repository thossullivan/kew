// Pure formatting helpers for the epic node detail pane (#454 Task C5).
// Extracted so duration/spend/token-split rendering is unit-testable — the
// [ref] page and DagView have no component-render test infra (see brief:
// "C7 verifies visually").

import { parseInstant } from '$lib/time';

/** ms -> compact elapsed string: "45s" / "47m" / "2h13m" / "3d4h". Clamped to
 * 0 so clock skew never renders a negative duration. */
export function elapsed(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h${String(m % 60).padStart(2, '0')}m`;
  const d = Math.floor(h / 24);
  return `${d}d${h % 24}h`;
}

/** "IN_PROGRESS · 47m" — status label plus elapsed time since status_since.
 * No status_since (or unparseable) -> label alone, never a fabricated
 * duration. The stamp is read through `$lib/time` for the same reason the
 * clock readings are (#2205): a bare `Date.parse` takes an offset-less
 * timestamp for the viewer's wall clock, which here would not print a wrong
 * time but a wrong *duration* — "47m" for a node that started a moment ago. */
export function statusDuration(status: string, statusSince: string | null, nowMs: number): string {
  const label = status.toUpperCase();
  const since = parseInstant(statusSince);
  if (since === null) return label;
  return `${label} · ${elapsed(nowMs - since)}`;
}

export const fmtUSD = (n: number): string => '$' + n.toFixed(2);

/** Token count with a cache-read percentage split, e.g.
 * "12.3k tok · 40% cached". Zero total renders with no split — there is
 * nothing honest to divide. */
export function fmtTokenSplit(totalTokens: number, cacheReadTokens: number): string {
  const tok = totalTokens >= 1000 ? (totalTokens / 1000).toFixed(1) + 'k' : String(totalTokens);
  if (totalTokens <= 0) return `${tok} tok`;
  const pct = Math.round((cacheReadTokens / totalTokens) * 100);
  return `${tok} tok · ${pct}% cached`;
}
