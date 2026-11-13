/**
 * #2303 — the staleness badge, and the cause it carries.
 *
 * Two things are pinned here. The MAPPING: which badge each `review_state` and
 * each `base.state` earns, and which of them is allowed to be magenta — the
 * colour DESIGN.md rations to "a person is the next move", and the one thing a
 * badge can get wrong that costs every other magenta on the screen its meaning.
 * And the CAUSE: a badge that says "stale" and nothing else sends the reader to
 * GitHub to find out what moved, which is the work it was supposed to save.
 *
 * The component that draws this is covered by render tests in
 * `epic/dag-view.test.ts` (the card) and `epic/node-popover.test.ts` (the
 * popover); everything decidable without a DOM is decided here.
 */
import { describe, expect, it } from 'vitest';
import type { PrFreshnessRow } from './api';
import {
  badgesFor,
  BASE_READING_MAX_AGE_MS,
  byItem,
  commitHref,
  failedReading,
  FRESHNESS_MAX_PAGES,
  freshnessNotice,
  readFreshness,
  shortSha,
  type StalenessBadge,
  unreadBaseNote
} from './freshness';
import type { PrFreshness } from './api';
import freshnessJson from '../../fixtures/factory/prs-freshness.json';

const PR_URL = 'https://github.com/thossullivan/kew/pull/2059';
const REVIEWED = '9f8e7d1c4b2a0e6f3d5c8b1a7e9f2c4d6b8a0e3f';
const MOVED = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678';

/**
 * The clock every badge in this file is read against. It is the fixture's own
 * `composed_at`, so the committed fixture reads as a FRESH page here — the
 * rebase badge is gated on how old the journaled base reading is, and a test
 * reading it against the wall clock would flip the moment the fixture aged.
 */
const NOW_MS = Date.parse('2026-10-03T14:48:00+00:00');

/** A base reading taken a minute ago: the ordinary case, one driver tick back. */
const JUST_READ = '2026-10-03T14:47:00+00:00';

/** A reading one second past the bound the server itself stops acting on. */
const TOO_OLD = new Date(NOW_MS - BASE_READING_MAX_AGE_MS - 1000).toISOString();

/** One `base` reading. `at` — when the merge state was last READ — defaults to
 * the ordinary one-tick-ago reading; the age cases pass their own. */
const base = (
  state: string,
  reason: string,
  since: string | null = null,
  source = 'snapshot',
  at: string | null = JUST_READ
): PrFreshnessRow['base'] => ({ state, reason, since, at, source });

const row = (over: Partial<PrFreshnessRow> = {}): PrFreshnessRow => ({
  item_id: '2055',
  epic_ref: 'launch-fixes',
  pr_number: 2059,
  pr_url: PR_URL,
  last_verdict: {
    round: 2,
    verdict: 'READY',
    head_sha: REVIEWED,
    recorded_at: '2026-10-03T11:12:00+00:00'
  },
  latest_head: { sha: REVIEWED, source: 'snapshot', at: '2026-10-03T11:00:00+00:00' },
  review_state: 'current',
  base: {
    state: 'clean',
    reason: 'the head merges into its base',
    since: '2026-10-03T11:00:00+00:00',
    at: '2026-10-03T14:48:00+00:00',
    source: 'snapshot'
  },
  observed_at: '2026-10-03T14:48:00+00:00',
  ...over
});

/** `badgesFor`/`unreadBaseNote` at this file's fixed clock. The clock is a
 * parameter because one of the badges is gated on the AGE of the reading
 * behind it, and a conclusion read off `Date.now()` is one no test can pin. */
const badgesNow = (r: PrFreshnessRow): StalenessBadge[] => badgesFor(r, NOW_MS);
const noteNow = (r: PrFreshnessRow): string => unreadBaseNote(r, NOW_MS);

describe('which review_state earns a badge', () => {
  it('draws "review stale" for a head that moved after the verdict', () => {
    const [badge] = badgesNow(
      row({
        review_state: 'stale',
        latest_head: { sha: MOVED, source: 'snapshot', at: '2026-10-03T14:02:00+00:00' }
      })
    );

    expect(badge.kind).toBe('review-stale');
    expect(badge.label).toBe('review stale');
  });

  it.each(['current', 'remediating', 'reviewing', 'no_round'])(
    'draws nothing for %s — only a moved head is staleness',
    (review_state) => {
      expect(badgesNow(row({ review_state }))).toEqual([]);
    }
  );
});

describe('which base.state earns a badge', () => {
  it.each([
    ['behind', false],
    ['conflicted', true]
  ])('draws "needs rebase" for %s, needsAPerson=%s', (state, needsAPerson) => {
    const [badge] = badgesNow(
      row({ base: base(state, `base is ${state}`) })
    );

    expect(badge.kind).toBe('needs-rebase');
    expect(badge.label).toBe('needs rebase');
    expect(badge.needsAPerson).toBe(needsAPerson);
  });

  it.each(['clean', 'blocked', 'unknown'])('draws no rebase badge for %s', (state) => {
    const badges = badgesNow(
      row({ base: base(state, `base is ${state}`) })
    );

    expect(badges.map((b) => b.kind)).not.toContain('needs-rebase');
  });
});

describe('magenta is rationed to the one move a person owns', () => {
  it('is never spent on a stale review — kew starts the next round itself', () => {
    const [badge] = badgesNow(
      row({
        review_state: 'stale',
        latest_head: { sha: MOVED, source: 'snapshot', at: null }
      })
    );

    expect(badge.needsAPerson).toBe(false);
  });

  it('is never spent on a base kew can catch up to on its own', () => {
    const [badge] = badgesNow(
      row({ base: base('behind', 'the base moved') })
    );

    expect(badge.needsAPerson).toBe(false);
  });

  it('is spent on a conflict, which the factory cannot resolve', () => {
    const [badge] = badgesNow(
      row({ base: base('conflicted', 'conflicts') })
    );

    expect(badge.needsAPerson).toBe(true);
  });
});

describe('a badge carries its cause', () => {
  it('names both commits and the verdict between them', () => {
    const [badge] = badgesNow(
      row({
        review_state: 'stale',
        latest_head: { sha: MOVED, source: 'snapshot', at: '2026-10-03T14:02:00+00:00' }
      })
    );

    const [cause] = badge.causes;
    expect(cause.origin).toBe('head');
    expect(cause.text).toContain('a1b2c3d');
    expect(cause.text).toContain('9f8e7d1');
    expect(cause.text).toContain('round 2');
    expect(cause.commit).toBe('a1b2c3d');
    expect(cause.since).toBe('2026-10-03T14:02:00+00:00');
  });

  it('links the commit under the pull request the server named', () => {
    expect(commitHref(PR_URL, MOVED)).toBe(`${PR_URL}/commits/a1b2c3d`);
  });

  it('invents no link for a URL it does not recognise', () => {
    // A composed URL sends people to a 404 with kew's name on it.
    expect(commitHref('https://gitlab.example/x/-/merge_requests/9', MOVED)).toBe('');
    expect(commitHref(null, MOVED)).toBe('');
    expect(commitHref(PR_URL, '')).toBe('');
  });

  it('suffixes nothing onto a served URL that is not http(s) (C10)', () => {
    expect(commitHref('javascript:alert(1)//pull/1', MOVED)).toBe('');
  });

  it('still says what moved when no verdict was ever recorded', () => {
    // `review_state` is the server's reading; a row could carry `stale` with no
    // verdict row behind it, and a badge with an empty cause list would be the
    // "stale, go and find out" the node exists to remove.
    const [badge] = badgesNow(
      row({
        review_state: 'stale',
        last_verdict: null,
        latest_head: { sha: MOVED, source: 'snapshot', at: null }
      })
    );

    expect(badge.causes).toHaveLength(1);
    expect(badge.causes[0].text).not.toBe('');
  });

  it('takes the base cause from the server sentence, never a word of its own', () => {
    const reason = 'the head and its base have conflicting changes';
    const [badge] = badgesNow(
      row({ base: base('conflicted', reason, '2026-10-03T08:15:00Z') })
    );

    expect(badge.causes[0]).toEqual({
      origin: 'base',
      text: reason,
      commit: '',
      href: '',
      since: '2026-10-03T08:15:00Z'
    });
  });
});

describe('both causes at once', () => {
  it('puts the conflict first — a rebase is the move either way', () => {
    const badges = badgesNow(
      row({
        review_state: 'stale',
        latest_head: { sha: MOVED, source: 'snapshot', at: null },
        base: base('conflicted', 'conflicts')
      })
    );

    expect(badges.map((b) => b.kind)).toEqual(['needs-rebase', 'review-stale']);
  });
});

describe('a base nobody could read is not a clean one', () => {
  it('is said in the popover even though it earns no badge', () => {
    const reason = 'the driver has not journaled a merge state for this pull request yet';

    expect(
      noteNow(row({ base: base('unknown', reason, null, 'unobserved') }))
    ).toBe(reason);
  });

  it('says nothing when the base really is clean, or when a badge already does', () => {
    expect(noteNow(row())).toBe('');
    expect(
      noteNow(
        row({ base: base('behind', 'the base moved') })
      )
    ).toBe('');
  });
});

describe('the rebase badge is bounded by the age of its reading (#2303)', () => {
  // The finding: `base` is a JOURNAL row the driver carries forward over ticks
  // GitHub answered UNKNOWN on, and the server refuses to act on one older than
  // `JOURNALED_MERGE_STATE_MAX_AGE_S` (`work.cached_pull_request`). Painting the
  // rationed magenta badge off the same row regardless had the console and the
  // Work projection reaching opposite conclusions from one row — and the
  // console's was the one asking a person to rebase.
  it('mirrors the server bound exactly', () => {
    expect(BASE_READING_MAX_AGE_MS).toBe(60 * 60 * 1000);
  });

  it('withholds the badge for a conflict read longer ago than the server acts on', () => {
    const stale = row({ base: base('conflicted', 'conflicts', null, 'snapshot', TOO_OLD) });

    expect(badgesNow(stale)).toEqual([]);
  });

  it('withholds it for a behind base read that long ago too', () => {
    const stale = row({ base: base('behind', 'the base moved', null, 'snapshot', TOO_OLD) });

    expect(badgesNow(stale)).toEqual([]);
  });

  it('withholds it when nothing records when the base was read', () => {
    // An age nobody can establish is not an age: the same reading the server
    // takes of a merge state with no `merge_state_at` behind it.
    const unaged = row({ base: base('conflicted', 'conflicts', null, 'snapshot', null) });

    expect(badgesNow(unaged)).toEqual([]);
  });

  it('draws it for a reading inside the bound — the bound is ~60 missed ticks', () => {
    const recent = new Date(NOW_MS - BASE_READING_MAX_AGE_MS + 1000).toISOString();
    const [badge] = badgesNow(
      row({ base: base('conflicted', 'conflicts', null, 'snapshot', recent) })
    );

    expect(badge.kind).toBe('needs-rebase');
    expect(badge.needsAPerson).toBe(true);
  });

  it('says what the withheld badge would have said, so no row reads as clean', () => {
    // The badge going quiet must not make the row look clean: the popover's
    // note carries the server's own sentence plus why it is not being acted on.
    const stale = row({
      base: base('conflicted', 'the head and its base have conflicting changes', null, 'snapshot', TOO_OLD)
    });

    const note = noteNow(stale);
    expect(note).toContain('conflicting changes');
    expect(note).toContain('unknown');
  });

  it('says it differently when the reading has no instant at all', () => {
    const unaged = row({ base: base('behind', 'the base moved', null, 'snapshot', null) });

    expect(noteNow(unaged)).toContain('nothing records when that was read');
  });

  it('leaves the head cause alone — review staleness is not a base reading', () => {
    // `review_state` is composed from the head sha, which carries no age bound:
    // a verdict about a commit that is no longer the head stays true.
    const [badge] = badgesNow(
      row({
        review_state: 'stale',
        latest_head: { sha: MOVED, source: 'snapshot', at: null },
        base: base('clean', 'the head merges into its base', null, 'snapshot', TOO_OLD)
      })
    );

    expect(badge.kind).toBe('review-stale');
  });
});

describe('a freshness read that THREW is a confession, not a clean page', () => {
  // The finding: the page swallowed the error and left `freshness` null, and
  // `freshnessNotice(null)` is null — so a persistently failing
  // /api/prs/freshness drew no badge on any node and no confession either,
  // which on screen is exactly an epic where nothing is stale.
  it('names the failure in the same channel a server-reported one uses', () => {
    const reading = failedReading(null, new Error('503 from /api/prs/freshness'));

    expect(freshnessNotice(reading)).toContain('503 from /api/prs/freshness');
  });

  it('keeps the rows a previous read did reach — badges are not blanked', () => {
    const good = { rows: byItem(page({ rows: [row()] })), unavailable: [], truncated: false };

    const reading = failedReading(good, new Error('network down'));

    expect(reading.rows.size).toBe(1);
    expect(freshnessNotice(reading)).toContain('network down');
  });

  it('does not grow a line per failing poll', () => {
    // The poll re-runs every few seconds; appending would turn the strip into
    // a log.
    const once = failedReading(null, new Error('boom'));
    const twice = failedReading(once, new Error('boom'));

    expect(twice.unavailable).toHaveLength(1);
  });

  it('still says something when the failure carries no message', () => {
    const reading = failedReading(null, new Error(''));

    expect(freshnessNotice(reading)).toContain('could not read');
  });

  it('carries a truncation the previous reading had, which is still true', () => {
    const cut = { rows: new Map(), unavailable: [], truncated: true };

    expect(failedReading(cut, new Error('boom')).truncated).toBe(true);
  });
});

describe('shortSha', () => {
  it('shortens to the seven characters a commit is spoken about by', () => {
    expect(shortSha(REVIEWED)).toBe('9f8e7d1');
    expect(shortSha('  abc  ')).toBe('abc');
    expect(shortSha(null)).toBe('');
    expect(shortSha(undefined)).toBe('');
  });
});

describe('the committed fixture', () => {
  it('draws no badge for the fixture PR, its READY round current at the head it is on', () => {
    // #3461: #2055 tells one story — round 1 READY at 3670e43f, the head it is
    // still on. A moved head is pinned on inline rows above, and served to the
    // browser check through `page.route` (`epic-popover-pinned.mjs`).
    const fixture = byItem(freshnessJson).get('2055');
    expect(fixture).toBeDefined();
    expect(fixture!.last_verdict?.head_sha).toBe(fixture!.latest_head?.sha);

    expect(badgesNow(fixture!)).toEqual([]);
    expect(noteNow(fixture!)).toBe('');
  });

  it('draws the magenta rebase badge for the conflicted fixture PR', () => {
    const conflicted = byItem(freshnessJson).get('SFD-227');
    expect(conflicted).toBeDefined();

    const [badge] = badgesNow(conflicted!);
    expect(badge.kind).toBe('needs-rebase');
    expect(badge.needsAPerson).toBe(true);
  });

  it('reads an unread page as "no badge", never as "nothing is stale"', () => {
    expect(byItem(null).size).toBe(0);
  });
});

/** One page of the endpoint's answer, with everything defaulted away. */
const page = (over: Partial<PrFreshness> = {}): PrFreshness => ({
  rows: [],
  next_cursor: null,
  unavailable: [],
  composed_at: '2026-10-03T14:48:00+00:00',
  ...over
});

describe('reading one epic across every page it takes', () => {
  it('follows next_cursor until the server stops handing one out', async () => {
    const asked: (string | null)[] = [];
    const pages = [
      page({ rows: [row({ item_id: 'a' })], next_cursor: 'launch-fixes|a' }),
      page({ rows: [row({ item_id: 'b' })], next_cursor: 'launch-fixes|b' }),
      page({ rows: [row({ item_id: 'c' })] })
    ];

    const reading = await readFreshness((cursor) => {
      asked.push(cursor);
      return Promise.resolve(pages[asked.length - 1]);
    });

    // The first ask carries no cursor; each later one carries the previous
    // page's — which is the only thing that makes this a page walk and not
    // three reads of page one.
    expect(asked).toEqual([null, 'launch-fixes|a', 'launch-fixes|b']);
    expect([...reading.rows.keys()]).toEqual(['a', 'b', 'c']);
    expect(reading.truncated).toBe(false);
    expect(freshnessNotice(reading)).toBeNull();
  });

  it('stops at the page cap and SAYS the rest is unknown', async () => {
    // A server that keeps answering with a cursor: the walk must not spin a
    // poll that re-runs every few seconds, and it must not pretend it is done.
    const reading = await readFreshness(() =>
      Promise.resolve(page({ rows: [row()], next_cursor: 'launch-fixes|2055' }))
    );

    expect(reading.truncated).toBe(true);
    expect(freshnessNotice(reading)).toContain('staleness unknown');
  });

  it('asks no more pages than the cap', async () => {
    let calls = 0;
    await readFreshness(() => {
      calls += 1;
      return Promise.resolve(page({ rows: [row()], next_cursor: 'c' }));
    });

    expect(calls).toBe(FRESHNESS_MAX_PAGES);
  });

  it('carries every page\'s unavailable, so an empty read is never a clean one', async () => {
    // The finding this test exists for: the page ignored `unavailable`, so a
    // sub-read the server could not do left every card with no badge — which
    // on screen is exactly what "nothing is stale" looks like.
    const reading = await readFreshness(() =>
      Promise.resolve(page({ unavailable: ['snapshot_pr_facts: database is locked'] }))
    );

    expect(reading.rows.size).toBe(0);
    expect(freshnessNotice(reading)).toContain('database is locked');
  });

  it('names a failed read even when rows came back too', async () => {
    const reading = await readFreshness(() =>
      Promise.resolve(page({ rows: [row()], unavailable: ['review_rounds:x: locked'] }))
    );

    expect(reading.rows.size).toBe(1);
    expect(freshnessNotice(reading)).toContain('review_rounds:x: locked');
  });

  it('says nothing at all before the first read lands', () => {
    // "Not read yet" is not a confession: the poll is in flight and the strip
    // would flash a warning on every navigation.
    expect(freshnessNotice(null)).toBeNull();
  });
});
