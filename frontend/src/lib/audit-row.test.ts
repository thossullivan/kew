/**
 * C7 (#3470) — an `/audit` row over the summary the server really sends:
 * compact JSON, capped at 200 characters. The row reads in words, and the JSON
 * is only ever the disclosure's.
 */
import { describe, expect, it } from 'vitest';
import type { AuditEvent } from './api';
import { auditRow } from './audit-row';
import auditJson from '../../fixtures/factory/audit.json';

const event = (over: Partial<AuditEvent> = {}): AuditEvent => ({
  id: 1,
  timestamp: '2026-10-03T14:46:40+00:00',
  event: 'adversarial_review_verdict',
  issue_num: 2055,
  item_id: null,
  run_id: '9c41e2b7',
  actor_type: 'host',
  actor_id: 'kew',
  summary: null,
  epic_ref: 'launch-fixes',
  ...over
});

describe('auditRow', () => {
  it('reads a JSON summary in words and keeps the JSON for the disclosure', () => {
    const summary = '{"pr_number":2059,"verdict":"READY","reviewer_model":"claude-opus-5-5"}';
    const row = auditRow(event({ summary }));
    expect(row.lead).not.toContain('_');
    expect(row.code).toBe('adversarial_review_verdict');
    expect(row.words).toContain('PR #2059');
    expect(row.words).not.toMatch(/[{}"]/);
    expect(row.detail).toContain('reviewer_model');
  });

  it('still reads an excerpt the 200-character cap cut', () => {
    const summary = '{"pr_number":2059,"item_id":"2055","verdict":"READY","head_sha":"' + 'a'.repeat(160);
    const row = auditRow(event({ summary: summary.slice(0, 199) + '…' }));
    expect(row.words).toContain('PR #2059');
    expect(row.words).not.toMatch(/^\{/);
    expect(row.detail.startsWith('{')).toBe(true);
  });

  it('never leads a row of the committed fixture with JSON', () => {
    const events = (auditJson as { events: AuditEvent[] }).events;
    expect(events.length).toBeGreaterThan(0);
    for (const ev of events) {
      // The fixture is server-shaped: every summary is the compact JSON
      // `_summary` writes, at most 200 characters.
      expect(ev.summary?.startsWith('{'), `#${ev.id}`).toBe(true);
      expect(ev.summary!.length, `#${ev.id}`).toBeLessThanOrEqual(200);
      const row = auditRow(ev);
      expect(row.lead.startsWith('{'), `#${ev.id} lead`).toBe(false);
      expect(row.words.startsWith('{'), `#${ev.id} words "${row.words}"`).toBe(false);
    }
  });

  it('reads an empty summary as the lead alone', () => {
    const row = auditRow(event({ summary: null }));
    expect(row.words).toBe('launch-fixes · #2055');
    expect(row.detail).toBe('');
  });
});
