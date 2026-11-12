import { queueChip, driverStaleNotice, type QueueState, type VerbResult } from './verbs';

// What is left of the console's glance layer (#704, Group 4). The strip itself —
// fleet status, recent completions, caps, routing provenance, the ready-queue
// card — went with the legacy console (#3454); the queue-an-issue outcome is
// what Work's `QueuePicker` (#3453) still reads. Kept free of Svelte/DOM so it
// is unit-testable.

/**
 * Response-driven transition for a queue write. The picked row goes
 * pending→queued from the WRITE RESPONSE (never an optimistic guess): a success
 * carries the confirmed chip and the no-loop-running notice; a failure carries
 * the message to render INLINE on the row. Mirrors how the banner Resume verb
 * transitions from its own response.
 */
export type QueueOutcome =
  | { ok: true; issue: number; chip: string; notice: string | null }
  | { ok: false; issue: number; message: string };

export function queueOutcome(issue: number, res: VerbResult<QueueState>): QueueOutcome {
  if (res.ok) {
    return { ok: true, issue, chip: queueChip(res.data), notice: driverStaleNotice(res.data.driver_stale) };
  }
  return { ok: false, issue, message: res.message };
}
