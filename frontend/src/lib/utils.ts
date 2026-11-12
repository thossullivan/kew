import { clsx, type ClassValue } from 'clsx';
import { extendTailwindMerge } from 'tailwind-merge';

/**
 * tailwind-merge has no idea what `.text-11` … `.text-44` are — the factory
 * type scale is plain CSS in `app.css`, and an unrecognised `text-*` is read
 * as a *colour*. That made `cn('text-44', 'text-accent')` drop the 44px on the
 * floor. Registering the five sizes as font sizes keeps size and colour
 * independent, and makes two of them conflict the way `text-sm text-base` do.
 */
const twMerge = extendTailwindMerge({
  extend: { classGroups: { 'font-size': [{ text: ['11', '13', '15', '26', '44'] }] } }
});

/** Merge Tailwind classes with conflict resolution. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/**
 * The middle-dot separator, with its spaces, for markup that puts it at an
 * element's edge (design review c9 D14). Svelte trims whitespace at the start
 * and end of an element's text, so `<span>Centred on #107 · </span>needs you`
 * rendered "Centred on #107 ·needs you". As an expression (`{SEP}`) the spaces
 * survive; `separator-edges.test.ts` holds every component to that.
 */
export const SEP = ' · ';

/** Map a knowledge-estimate level to a 0..1 fill fraction. */
export const LEVEL_FILL: Record<string, number> = {
  high: 1,
  medium: 0.6,
  low: 0.3,
  unknown: 0.12
};

/** Human label for a knowledge level. */
export const LEVEL_LABEL: Record<string, string> = {
  high: 'solid',
  medium: 'developing',
  low: 'shaky',
  unknown: 'unmeasured'
};

/** Token color for a knowledge level (CSS var reference). */
export function levelColor(level: string | null | undefined): string {
  switch (level) {
    case 'high':
      return 'var(--success)';
    case 'medium':
      return 'var(--accent)';
    case 'low':
      return 'var(--danger)';
    default:
      return 'var(--faint)';
  }
}

// GitHub work-item ids are all-digits; every other tracker (Linear: `SFD-12`)
// is non-numeric. Mirrors `kew.ids.GITHUB_ID_RE`.
const GITHUB_ID_RE = /^[0-9]+$/;

/**
 * Human-facing reference for a work item — the frontend twin of
 * `kew.ids.item_ref_or_num`. Prefers the durable string `itemId` (`#5` for a
 * GitHub number, bare `SFD-12` for any other tracker) and falls back to the
 * legacy numeric `issueNum` (rendered `#N`) for payloads that predate the
 * `item_id` field. Byte-identical to the old `#{issue_num}` for every GitHub id.
 * Returns '' when neither id is present.
 */
export function itemRef(
  itemId: string | null | undefined,
  issueNum: number | null | undefined
): string {
  const id = itemId != null && itemId !== '' ? itemId : issueNum != null ? String(issueNum) : null;
  if (id == null) return '';
  return GITHUB_ID_RE.test(id) ? `#${id}` : id;
}

/** Clamp a number into [min, max]. */
export function clamp(n: number, min = 0, max = 1): number {
  return Math.min(max, Math.max(min, n));
}

/** Format a 0..1 score as a percentage string. */
export function pct(n: number | null | undefined): string {
  if (n == null) return '—';
  return `${Math.round(n * 100)}%`;
}

/** Format a timestamp as a relative time string (e.g. '5m ago', '3h ago', '2d ago', '—'). */
export function timeAgo(iso: string | null | undefined, nowMs?: number): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  
  const now = nowMs ? new Date(nowMs) : new Date();
  const diffMs = now.getTime() - d.getTime();
  
  const diffSeconds = Math.floor(diffMs / 1000);
  const diffMinutes = Math.floor(diffSeconds / 60);
  const diffHours = Math.floor(diffMinutes / 60);
  const diffDays = Math.floor(diffHours / 24);
  
  if (diffSeconds < 0) return '—'; // Future time
  if (diffSeconds < 60) return 'now';
  if (diffMinutes < 60) return `${diffMinutes}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  return `${diffDays}d ago`;
}
