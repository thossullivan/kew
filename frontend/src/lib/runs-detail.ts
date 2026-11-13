import type { Run } from './api';

export type DayGroup = { day: string; label: string; runs: Run[]; subtotal: number };

const UTC_LABEL = new Intl.DateTimeFormat('en-US', {
  timeZone: 'UTC',
  month: 'short',
  day: 'numeric',
  year: 'numeric'
});

// UTC calendar day (YYYY-MM-DD) for a run, or null when neither timestamp parses.
function utcDay(run: Run): string | null {
  const ts = run.started_at ?? run.finished_at;
  if (!ts) return null;
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return null;
  return d.toISOString().slice(0, 10);
}

// Group runs into calendar days on the UTC boundary, preserving the input
// order of both days and runs. Each group carries a spend subtotal.
export function groupRunsByDay(runs: Run[]): DayGroup[] {
  const groups: DayGroup[] = [];
  const byDay = new Map<string, DayGroup>();
  for (const run of runs) {
    const day = utcDay(run) ?? 'unknown';
    let group = byDay.get(day);
    if (!group) {
      const label = day === 'unknown' ? 'Unknown date' : UTC_LABEL.format(new Date(`${day}T00:00:00Z`));
      group = { day, label, runs: [], subtotal: 0 };
      byDay.set(day, group);
      groups.push(group);
    }
    group.runs.push(run);
    group.subtotal += run.cost_usd || 0;
  }
  return groups;
}

// Fraction of prompt tokens served from the cache, as a rounded percentage.
// Returns null when the run recorded no prompt tokens (zero-token guard).
export function cacheHitPct(run: Run): number | null {
  const prompt = run.input_tokens + run.cache_read_tokens + run.cache_creation_tokens;
  if (prompt <= 0) return null;
  return Math.round((100 * run.cache_read_tokens) / prompt);
}

// Compact diff summary chip (`+412 −87 · 9 files`), or null when the run has
// no recorded diff at all. A present-but-partial diff treats missing legs as 0.
export function diffStat(run: Run): string | null {
  if (run.diff_additions == null && run.diff_deletions == null && run.diff_files == null) return null;
  const add = run.diff_additions ?? 0;
  const del = run.diff_deletions ?? 0;
  const files = run.diff_files ?? 0;
  return `+${add} −${del} · ${files} file${files === 1 ? '' : 's'}`;
}
