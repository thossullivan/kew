/**
 * The audit page's event deep link (#3025): `/audit?event=<id>` shows that
 * one event even when it is older than the newest page the table loads.
 *
 * `GET /api/audit` pages newest first with `after` as an exclusive upper id
 * bound, so `after = id + 1, limit = 1` answers the newest event at or below
 * `id`: the event itself when it exists, never another one in its place.
 */
import { fetchAudit, type AuditEvent, type AuditFilters } from '$lib/api';

/**
 * The event `?event=<id>` links to: `found` with the event, `missing` when the
 * log holds no such id, `unread` when the read of it failed — said in its own
 * sentence, beside a list and a chain status that still render.
 */
export type AuditFocus =
  | { id: number; state: 'found'; event: AuditEvent }
  | { id: number; state: 'missing' }
  | { id: number; state: 'unread' };

/** The highest id `/api/audit` binds into SQLite (`SQLITE_INT_MAX` less one, for `after`). */
const MAX_ID = Number.MAX_SAFE_INTEGER - 1;

/** The event id `?event=` names: a positive whole number, or null. */
export function auditFocusId(raw: string | null): number | null {
  if (raw === null || !/^[1-9]\d*$/.test(raw)) return null;
  const id = Number(raw);
  return id <= MAX_ID ? id : null;
}

/** The one-event read for *id*. */
export function auditFocusQuery(id: number): AuditFilters {
  return { after: id + 1, limit: 1 };
}

/** The event that read answered, only when it is the one asked for. */
export function auditFocusEvent(id: number, events: AuditEvent[]): AuditEvent | null {
  const found = events[0];
  return found !== undefined && found.id === id ? found : null;
}

/**
 * Read the one linked event. Never rejects: a failed read is `unread`, so the
 * page keeps everything the other reads answered.
 */
export async function readAuditFocus(id: number, f: typeof fetch = fetch): Promise<AuditFocus> {
  try {
    const answer = await fetchAudit(auditFocusQuery(id), f);
    const event = auditFocusEvent(id, answer.events);
    return event === null ? { id, state: 'missing' } : { id, state: 'found', event };
  } catch {
    return { id, state: 'unread' };
  }
}
