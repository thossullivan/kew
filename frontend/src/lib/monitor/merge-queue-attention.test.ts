/**
 * #2982 — each merge-queue attention is read by its reason code.
 *
 * The codes are the backend's (`kew/src/kew/merge_queue_attention.py`), read
 * here off that file so a code added there and not worded here fails.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import type { AuditEvent } from '$lib/api';
import { overviewFamily } from '$lib/monitor/brush';
import { eventLine } from '$lib/monitor/event-line';
import { QUEUE_REASONS, queueAttention } from '$lib/monitor/merge-queue-attention';
import { composeStream, eventCategory } from '$lib/monitor/stream';

const BACKEND = readFileSync(
  new URL('../../../../kew/src/kew/merge_queue_attention.py', import.meta.url),
  'utf8'
);

/** `NAME = "code"` lines between the event names and `PERSON_NEXT`. */
function backendCodes(): { codes: string[]; personNext: string[] } {
  const consts = new Map(
    [...BACKEND.matchAll(/^([A-Z_]+) = "([a-z_]+)"$/gm)].map((m) => [m[1], m[2]])
  );
  const block =
    /PERSON_NEXT: frozenset\[str\] = frozenset\(\s*\{([^}]*)\}/.exec(BACKEND)?.[1] ?? '';
  const personNext = block
    .split(',')
    .map((name) => consts.get(name.trim()) ?? '')
    .filter(Boolean);
  const codes = [...consts.entries()]
    .filter(([name]) => !name.endsWith('_EVENT'))
    .map(([, code]) => code);
  return { codes, personNext };
}

function event(name: string, payload: Record<string, unknown> | null): AuditEvent {
  return {
    id: 1,
    timestamp: '2026-10-17T10:00:00.000Z',
    event: name,
    issue_num: null,
    item_id: null,
    run_id: null,
    actor_type: 'policy',
    actor_id: 'merge-queue',
    summary: payload === null ? null : JSON.stringify(payload),
    epic_ref: null
  };
}

const LONG = 'x'.repeat(400);

describe('merge queue attention reason codes', () => {
  it('words every code the backend emits, with its urgency', () => {
    const { codes, personNext } = backendCodes();
    expect(codes.length).toBeGreaterThan(0);
    expect(Object.keys(QUEUE_REASONS).sort()).toEqual([...codes].sort());
    for (const code of codes) {
      expect(QUEUE_REASONS[code].personNext, code).toBe(personNext.includes(code));
    }
  });

  it.each(Object.entries(QUEUE_REASONS))('%s leads with its action and its tone', (code, want) => {
    const name = want.personNext ? 'merge_queue_attention' : 'merge_queue_notice';
    const e = event(name, {
      reason: code,
      pr_number: 11,
      repo: 'o/a',
      sha: 'a'.repeat(40),
      message: LONG
    });
    expect(queueAttention(e)).toEqual({ code, ...want });
    expect(eventLine(e).label).toBe(want.action);
    expect(eventLine(e).plain).not.toContain(code);
    expect(eventLine(e).subject).toBe('o/a · PR #11');
    expect(eventCategory(e)).toBe(want.personNext ? 'needs-you' : 'neutral');
  });

  it('reads the code, the PR and the repo when the summary cap cut the message', () => {
    const payload = { reason: 'main_red', pr_number: null, repo: 'o/b', sha: null, message: LONG };
    const cut = JSON.stringify(payload).slice(0, 199) + '…';
    const e = { ...event('merge_queue_attention', null), summary: cut };
    expect(queueAttention(e)?.code).toBe('main_red');
    expect(eventLine(e).subject).toBe('o/b');
  });

  it('keeps an attention with no code a person’s move, worded as before', () => {
    const e = event('merge_queue_attention', {
      pr: null,
      sha: 'a',
      message: 'main is red'
    });
    expect(eventCategory(e)).toBe('needs-you');
    expect(eventLine(e).label).toBe('Merge queue stopped: check main');
  });

  it('does not take a code out of the message', () => {
    const e = event('merge_queue_notice', {
      pr: 1,
      message: '"reason":"main_red"'
    });
    expect(queueAttention(e)?.personNext).toBe(false);
  });

  it('puts the repo and PR in the row’s lead, which is never truncated', () => {
    const e = event('merge_queue_attention', {
      reason: 'declaration_edited',
      pr_number: 37,
      repo: 'o/a',
      message: LONG
    });
    const reading = composeStream({ events: [e], next_after: null }, Date.parse(e.timestamp));
    const row = reading.available ? reading.rows.find((r) => r.kind === 'event') : undefined;
    expect(row?.kind === 'event' && row.lead).toBe(
      `${QUEUE_REASONS.declaration_edited.action} · o/a · PR #37`
    );
    expect(row?.kind === 'event' && row.rest).not.toContain('PR #37');
  });

  it('counts only a person’s move as needs-you on the histogram', () => {
    expect(overviewFamily('merge_queue_attention')).toBe('needs-you');
    expect(overviewFamily('merge_queue_notice')).toBe('other');
  });
});
