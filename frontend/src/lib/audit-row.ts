/**
 * C7 (#3470) — one `/audit` row, read the way the Monitor reads it.
 *
 * `/api/audit` carries each event's payload as compact JSON capped at 200
 * characters (`_summary` in kew/src/kew/service/audit.py). The page printed it
 * as it arrived, so a row read `{"pr_number":2059,"item_id":"2055",…`. This is
 * the Monitor's own reading (`eventLine`), not a second one: a worded lead, the
 * subject and the one fact in words, and the recorded excerpt kept for the
 * row's disclosure. Nothing composes a reading here that the Monitor does not.
 */
import type { AuditEvent } from '$lib/api';
import { eventLine } from '$lib/monitor/event-line';

export type AuditRow = {
  /** The event in words: "Review verdict". */
  lead: string;
  /** The event code as recorded, the lead's `title`. */
  code: string;
  /** Subject and fact in words — "launch-fixes · #2055 · PR #2059 · READY" — or ''. */
  words: string;
  /** The same reading with its recorded values, the words' `title`. */
  recorded: string;
  /** The recorded payload for the disclosure, or '' when the words say it all. */
  detail: string;
};

export function auditRow(event: AuditEvent): AuditRow {
  const line = eventLine(event);
  return {
    lead: line.label,
    code: event.event,
    words: [line.subject, line.plain].filter((part) => part !== '').join(' · '),
    recorded: [line.subject, line.fact].filter((part) => part !== '').join(' · '),
    detail: line.detail
  };
}
