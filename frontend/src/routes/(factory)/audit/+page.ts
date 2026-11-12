import { fetchAudit, fetchAuditStatus, type AuditList, type AuditStatus } from '$lib/api';
import { auditFocusId, readAuditFocus, type AuditFocus } from '$lib/audit-focus';

export const ssr = false;

export type { AuditFocus };

/**
 * Fetch the first (newest) page of audit events + chain-verification status
 * from `kew serve`. On any failure return nulls so the page renders an
 * "offline / no data" empty state rather than erroring.
 *
 * `?event=<id>` (#3025) also reads that one event, so a link to an event older
 * than the first page still shows it. That read is optional and on its own: a
 * failure of it says so in its own sentence and never costs the list or the
 * chain status.
 */
export async function load({
  fetch,
  url
}: {
  fetch: typeof globalThis.fetch;
  url: URL;
}): Promise<{ audit: AuditList | null; status: AuditStatus | null; focus: AuditFocus | null }> {
  const id = auditFocusId(url.searchParams.get('event'));
  const focusing = id === null ? Promise.resolve(null) : readAuditFocus(id, fetch);
  try {
    const [audit, status] = await Promise.all([fetchAudit({ limit: 50 }, fetch), fetchAuditStatus(fetch)]);
    return { audit, status, focus: await focusing };
  } catch {
    return { audit: null, status: null, focus: null };
  }
}
