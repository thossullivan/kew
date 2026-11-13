/**
 * #2982 — what the merge queue's attention asks for, read off its reason code.
 *
 * The queue emits one stable `reason` per attention (`kew/src/kew/
 * merge_queue_attention.py`), first in the payload so the 200-character
 * summary cap never cuts it. Every attention used to read "Merge queue
 * stopped: check main" in magenta, whether main was red or the queue had
 * already requeued itself over an amended contract. The code now decides the
 * row's lead and whether a person is the next move — magenta only then
 * (DESIGN.md).
 *
 * An attention with no code (written before #2982) or with a code this table
 * has never heard of keeps the event's own reading: `merge_queue_attention` is
 * a person's move, `merge_queue_notice` is not. That is the safe direction —
 * the backend raises attention only when a person is next.
 */
import type { AuditEvent } from '$lib/api';
import { eventLabel } from '$lib/factory/names';

export type QueueAttention = {
  /** The recorded code, or '' when the event carries none this reads. */
  code: string;
  /** The row's lead: what happened, and the move. */
  action: string;
  /** True when a person is the next move — the needs-you mark. */
  personNext: boolean;
};

/** Keyed on the backend's codes; held to them by `merge-queue-attention.test.ts`. */
export const QUEUE_REASONS: Readonly<Record<string, Omit<QueueAttention, 'code'>>> = {
  main_red: {
    action: 'Merge queue stopped: main is red — fix main',
    personNext: true
  },
  main_unverified: {
    action: 'Merge queue stopped: main holds an unverified merge — check main',
    personNext: true
  },
  declaration_edited: {
    action: 'Dropped from merge queue: declaration edited — re-review, then re-add',
    personNext: true
  },
  head_held: {
    action: 'Merge queue held a head — release it to verify',
    personNext: true
  },
  contract_amended: {
    action: 'Merge queue requeued: acceptance contract amended',
    personNext: false
  },
  review_withdrawn: {
    action: 'Dropped from merge queue: review withdrawn before the merge',
    personNext: false
  }
};

const QUEUE_EVENTS: ReadonlySet<string> = new Set(['merge_queue_attention', 'merge_queue_notice']);

/** `{"reason":"main_red",…}` — the code is the payload's first key. */
const LEADING_REASON = /^\{"reason":"([a-z_]+)"/;

/** The queue attention *event* is, or `null` when it is not one. */
export function queueAttention(event: AuditEvent): QueueAttention | null {
  if (!QUEUE_EVENTS.has(event.event)) return null;
  const code = LEADING_REASON.exec((event.summary ?? '').trim())?.[1] ?? '';
  const known = Object.hasOwn(QUEUE_REASONS, code) ? QUEUE_REASONS[code] : undefined;
  if (known) return { code, ...known };
  return {
    code: '',
    action: eventLabel(event.event),
    personNext: event.event === 'merge_queue_attention'
  };
}
