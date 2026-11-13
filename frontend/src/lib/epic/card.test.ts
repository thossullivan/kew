/**
 * #2250 — what one DAG card prints: the UXEpic artboard's status row
 * ("held · needs you · 18d", "queued · awaits arm") and a collapsed chain's
 * id range ("SFD-251 – 255").
 */
import { describe, expect, it } from 'vitest';
import type { EpicNodeAttention } from '$lib/api';
import {
  BADGE_WORD_MAX,
  badgeLabelFits,
  badgesOnRow,
  cardStatus,
  cardWords,
  idRange,
  needsYouRow,
  shortAge
} from './card';

const NOW = Date.parse('2026-10-03T12:00:00+00:00');
const DAY = 24 * 3600 * 1000;
const HOLD = { kind: 'held', reason: 'preserved worktree', url: null } as EpicNodeAttention;

const text = (segs: { text: string }[]): string => segs.map((s) => s.text).join('');

describe('cardStatus', () => {
  it('says status, needs you and age — and not the reason, which is the popover`s', () => {
    const segs = cardStatus(
      {
        id: 'SFD-232',
        status: 'held',
        attention: HOLD,
        status_since: new Date(NOW - 18 * DAY - 3600_000).toISOString()
      },
      NOW
    );
    expect(text(segs)).toBe('held · needs you · 18d');
    expect(text(segs)).not.toContain('preserved worktree');
    expect(segs.find((s) => s.text === 'needs you')!.color).toBe('var(--accent)');
  });

  it('prints no age for finished work, or when the stamp is unreadable', () => {
    const since = new Date(NOW - 3 * DAY).toISOString();
    expect(text(cardStatus({ id: '1', status: 'done', status_since: since }, NOW))).toBe('done');
    expect(text(cardStatus({ id: '1', status: 'todo', status_since: 'nope' }, NOW))).toBe('todo');
    expect(text(cardStatus({ id: '1', status: 'todo', status_since: since }, NOW))).toBe('todo · 3d');
  });
});

describe('the card status row (#2543, D30)', () => {
  it('is one status word: the driver`s reason is the popover`s, not the card`s', () => {
    const blocked = cardStatus(
      { id: '2053', status: 'blocked', blocked_reason: 'needs a design decision: a different user' },
      NOW
    );
    expect(text(blocked)).toBe('blocked');
    const unarmed = cardStatus(
      { id: 'SFD-223', status: 'queued', stuck: 'waiting for human arm' },
      NOW
    );
    expect(text(unarmed)).toBe('queued');
    const review = cardStatus({ id: '2055', status: 'in_review', attention: HOLD }, NOW);
    expect(text(review)).toBe('in review · needs you');
  });

  it('carries each separator with the word after it, so a dropped word drops its own', () => {
    const segs = cardStatus(
      {
        id: 'SFD-232',
        status: 'held',
        attention: HOLD,
        status_since: new Date(NOW - 18 * DAY).toISOString()
      },
      NOW
    );
    const words = cardWords(segs);
    expect(words.map((w) => [w.lead?.text ?? '', w.word.text])).toEqual([
      ['', 'held'],
      [' · ', 'needs you'],
      [' · ', '18d']
    ]);
  });
});

describe('badgeLabelFits', () => {
  it('gives a long status word the row and the badge its mark', () => {
    expect(BADGE_WORD_MAX).toBe(7);
    for (const status of ['todo', 'queued', 'held', 'blocked', 'done', 'failed']) {
      expect(badgeLabelFits(status), status).toBe(true);
    }
    for (const status of ['in_review', 'in_progress', 'cancelled']) {
      expect(badgeLabelFits(status), status).toBe(false);
    }
  });
});

describe('shortAge', () => {
  it('keeps only the largest unit', () => {
    expect(shortAge(45_000)).toBe('45s');
    expect(shortAge(47 * 60_000)).toBe('47m');
    expect(shortAge(2 * 3600_000 + 13 * 60_000)).toBe('2h');
    expect(shortAge(18 * DAY + 10 * 3600_000)).toBe('18d');
  });
});

describe('idRange', () => {
  it('says the shared prefix once', () => {
    expect(idRange(['SFD-251', 'SFD-252', 'SFD-255'])).toBe('SFD-251 – 255');
    expect(idRange(['2244', '2245', '2246'])).toBe('#2244 – 2246');
  });

  it('prints a lone id as itself and nothing for none', () => {
    expect(idRange(['SFD-9'])).toBe('SFD-9');
    expect(idRange([])).toBe('');
  });

  it('keeps the whole last id when the prefixes differ', () => {
    expect(idRange(['ABC-1', 'XYZ-2'])).toBe('ABC-1 – XYZ-2');
  });
});

describe('the needs-you card`s status row (#3051 N7)', () => {
  // Every kind a person is waited on, as the fixtures and live serve them: a
  // merge decision (#2055), an in-review node awaiting a merge, a hold
  // (SFD-229), and an approval on a finished node (SFD-258).
  const kinds: { kind: string; status: string; head: string }[] = [
    { kind: 'merge_decision', status: 'in_review', head: 'in review · needs you' },
    { kind: 'merge_ready', status: 'in_review', head: 'in review · needs you' },
    { kind: 'held', status: 'held', head: 'held · needs you' },
    { kind: 'approval', status: 'done', head: 'done · needs you' }
  ];

  it.each(kinds)('builds the $kind row with needsYouRow, ending in needs you', (k) => {
    const attention = { kind: k.kind, reason: 'r', url: null } as EpicNodeAttention;
    const segs = cardStatus({ id: '1', status: k.status, attention }, NOW);
    expect(segs).toEqual(needsYouRow(k.status));
    expect(text(segs)).toBe(k.head);
    expect(segs.at(-1)!.text).toBe('needs you');
  });

  it('keeps its age after needs you, and needs you never leaves the row for a badge', () => {
    const since = new Date(NOW - 18 * DAY).toISOString();
    const node = { id: '2055', status: 'in_review', status_since: since, attention: HOLD };
    expect(cardStatus(node, NOW).slice(0, 3)).toEqual(needsYouRow('in_review'));
    expect(text(cardStatus(node, NOW))).toBe('in review · needs you · 18d');
    // #2055's REVIEW STALE badge pushed "needs you" off its row: on a
    // needs-you card the badges are marks beside the id instead.
    expect(badgesOnRow(node, 1, false)).toBe(false);
    expect(badgesOnRow({ id: '2033', status: 'in_progress' }, 1, false)).toBe(true);
    expect(badgesOnRow({ id: '2033', status: 'in_progress' }, 0, false)).toBe(false);
    expect(badgesOnRow({ id: '2033', status: 'in_progress' }, 1, true)).toBe(false);
  });
});
