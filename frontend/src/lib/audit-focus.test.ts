import { describe, expect, it } from 'vitest';
import type { AuditEvent } from '$lib/api';
import { load } from '../routes/(factory)/audit/+page';
import { auditFocusEvent, auditFocusId, auditFocusQuery, readAuditFocus } from './audit-focus';

const event = (id: number): AuditEvent => ({
  id,
  timestamp: '2026-10-17T09:40:00+00:00',
  event: 'prompt_profile_activated',
  issue_num: null,
  item_id: null,
  run_id: null,
  actor_type: 'human',
  actor_id: 'alice',
  summary: null,
  epic_ref: null
});

describe('auditFocusId', () => {
  it('reads a positive whole number', () => {
    expect(auditFocusId('7')).toBe(7);
  });

  it('reads anything else as no deep link', () => {
    for (const raw of [null, '', '0', '-3', '1.5', '7x', ' 7', '07', '1e3', '99999999999999999999']) {
      expect(auditFocusId(raw), String(raw)).toBeNull();
    }
  });
});

describe('auditFocusQuery', () => {
  it('asks for the newest event at or below the id, one of them', () => {
    expect(auditFocusQuery(7)).toEqual({ after: 8, limit: 1 });
  });
});

describe('readAuditFocus', () => {
  const answering = (status: number, body: unknown) =>
    (async () => new Response(JSON.stringify(body), { status })) as unknown as typeof fetch;

  it('finds the event the read answers', async () => {
    const focus = await readAuditFocus(7, answering(200, { events: [event(7)], next_after: null }));
    expect(focus.state).toBe('found');
  });

  it('names an id the log does not hold as missing', async () => {
    expect(await readAuditFocus(7, answering(200, { events: [event(5)], next_after: null }))).toEqual({
      id: 7,
      state: 'missing'
    });
  });

  it('names a failed read as unread, never throwing', async () => {
    expect(await readAuditFocus(7, answering(500, { detail: 'boom' }))).toEqual({ id: 7, state: 'unread' });
    const offline = (async () => {
      throw new TypeError('Failed to fetch');
    }) as unknown as typeof fetch;
    expect(await readAuditFocus(7, offline)).toEqual({ id: 7, state: 'unread' });
  });
});

describe('the audit page load', () => {
  it('keeps the list and the chain status when only the focus read fails', async () => {
    const list = { events: [event(120)], next_after: 119 };
    const status = { verified: true, events: 120, anchors_matched: 1, anchors_total: 1 };
    const f = (async (input: RequestInfo | URL) => {
      const url = new URL(String(input), 'http://kew.test');
      if (url.pathname === '/api/audit/status') return new Response(JSON.stringify(status));
      if (url.searchParams.get('limit') === '1') return new Response('{"detail":"boom"}', { status: 500 });
      return new Response(JSON.stringify(list));
    }) as unknown as typeof fetch;
    const data = await load({ fetch: f, url: new URL('http://kew.test/audit?event=9') });
    expect(data.audit).toEqual(list);
    expect(data.status).toEqual(status);
    expect(data.focus).toEqual({ id: 9, state: 'unread' });
  });
});

describe('auditFocusEvent', () => {
  it('takes the answer only when it is the event asked for', () => {
    expect(auditFocusEvent(7, [event(7)])?.id).toBe(7);
    // Event 7 is gone; the read answered the next older one, which is not it.
    expect(auditFocusEvent(7, [event(5)])).toBeNull();
    expect(auditFocusEvent(7, [])).toBeNull();
  });
});
