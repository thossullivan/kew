/**
 * #2516 — the Cmd-K palette's entries and ranking, against the golden
 * fixtures the rest of the console is drawn from. The keys and the timer are
 * driven for real in `palette.client.test.ts`; this file holds the readings.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import dashboardJson from '../../../fixtures/factory/dashboard.json';
import projectionJson from '../../../fixtures/factory/dashboard-work.json';
import epicsJson from '../../../fixtures/factory/epics.json';
import runsJson from '../../../fixtures/factory/runs.json';
import type { Dashboard, EpicSummary, Run, WorkProjection } from '$lib/api';
import { FACTORY_TABS, MORE_DESTINATIONS } from '$lib/shell/nav';
import {
  MAX_RESULTS,
  RECENT_RUNS,
  pageEntries,
  paletteEntries,
  paletteKey,
  rankEntries,
  runEntries,
  scoreEntry,
  type PaletteEntry
} from './entries';

const input = {
  epics: epicsJson as unknown as EpicSummary[],
  projection: projectionJson as unknown as WorkProjection,
  dashboard: dashboardJson as unknown as Dashboard,
  runs: runsJson as unknown as Run[]
};
const entries = paletteEntries(input);

describe('an item number', () => {
  it('ranks that item first, above the runs that worked on it', () => {
    // The fixtures hold two runs for 2055 (9c41e2b7, f5dcb6a5) whose item
    // number matches exactly too; the item itself is what was asked for.
    expect(entries.filter((e) => e.kind === 'run' && e.aliases.includes('2055'))).toHaveLength(2);
    for (const query of ['2055', '#2055', ' 2055 ']) {
      const [first] = rankEntries(entries, query);
      expect(first, query).toMatchObject({ kind: 'item', id: '2055', href: '/work?item=2055' });
    }
  });

  it('is followed by Enter to that item’s page', () => {
    const results = rankEntries(entries, '2055');
    expect(paletteKey('Enter', 0, results)).toEqual({ kind: 'go', href: '/work?item=2055' });
  });

  // The live ledger's shape (#2565 review): an item with several runs of its
  // own, and a run whose UUID holds 2, 5, 2, 6 scattered in order.
  const run = (run_id: string, issue_num: number, title = '') =>
    ({ run_id, issue_num, title, started_at: '2026-10-01T00:00:00Z' }) as unknown as Run;
  const live = (itemId: string): PaletteEntry[] => [
    ...pageEntries(),
    {
      kind: 'item',
      key: `item:${itemId}`,
      id: itemId,
      title: 'the item',
      aliases: [`#${itemId.replace(/^#/, '')}`],
      href: '/work?item=2526'
    },
    ...runEntries([
      run('0a2526aa-1111-4111-8111-111111111111', 2526, 'first try'),
      run('1b2526bb-2222-4222-8222-222222222222', 2526, 'second try'),
      run('2c2526cc-3333-4333-8333-333333333333', 2526, 'third try'),
      run('ceac0332-bd9e-47b4-b759-33898f85d023', 1999, 'something else'),
      run('d4d4d4d4-4444-4444-8444-444444444444', 1998, 'retry 2 of 5, 2 left, 6 min')
    ])
  ];

  it('ranks the item above its own runs, and lists no UUID that only scatters the digits', () => {
    for (const itemId of ['2526', '#2526'])
      for (const query of ['2526', '#2526']) {
        const results = rankEntries(live(itemId), query);
        expect(results[0], `${itemId} / ${query}`).toMatchObject({
          kind: 'item',
          href: '/work?item=2526'
        });
        expect(paletteKey('Enter', 0, results)).toEqual({ kind: 'go', href: '/work?item=2526' });
        expect(results.filter((e) => e.kind === 'run'), `${itemId} / ${query}`).toHaveLength(3);
        expect(results.map((e) => e.id)).not.toContain('ceac0332-bd9e-47b4-b759-33898f85d023');
        expect(results.map((e) => e.id)).not.toContain('d4d4d4d4-4444-4444-8444-444444444444');
      }
  });

  it('never matches a run by letters scattered through its UUID', () => {
    // c…a…b sit in order in `ceac0332-bd9e-…`, but nowhere as a word.
    const results = rankEntries(live('2526'), 'cab');
    expect(results.map((e) => e.id)).not.toContain('ceac0332-bd9e-47b4-b759-33898f85d023');
  });

  it('ranks an item whose id carries a prefix above the runs naming its number', () => {
    const [first] = rankEntries(live('gh:2526'), '2526');
    expect(first).toMatchObject({ kind: 'item', id: 'gh:2526' });
  });

  it('finds a tracker key case-blind', () => {
    const [first] = rankEntries(entries, 'sfd-229');
    expect(first?.id.toLowerCase()).toBe('sfd-229');
  });
});

describe('the nav routes', () => {
  it('are every one an entry, linked to its own route', () => {
    const routes = [...FACTORY_TABS, ...MORE_DESTINATIONS];
    expect(routes.length).toBeGreaterThan(0);
    for (const d of routes) {
      expect(entries).toContainEqual(
        expect.objectContaining({ kind: 'page', id: d.label, href: d.href })
      );
    }
    expect(pageEntries()).toHaveLength(routes.length);
  });

  it('are offered with no payload in hand at all', () => {
    const bare = paletteEntries({ epics: null, projection: null, dashboard: null, runs: null });
    expect(bare.map((e) => e.href)).toEqual([...FACTORY_TABS, ...MORE_DESTINATIONS].map((d) => d.href));
  });

  it('are found by name, fuzzily', () => {
    expect(rankEntries(entries, 'monitor')[0]?.href).toBe('/monitor');
    expect(rankEntries(entries, 'scrd')[0]?.href).toBe('/scorecard');
  });
});

describe('epics and runs', () => {
  it('link every epic by its encoded ref', () => {
    for (const epic of input.epics) {
      expect(entries).toContainEqual(
        expect.objectContaining({ kind: 'epic', href: `/epics/${encodeURIComponent(epic.epic_ref)}` })
      );
    }
  });

  it('link a run by its id, newest first, the recent ones only', () => {
    expect(rankEntries(entries, 'f5dcb6a5')[0]).toMatchObject({
      kind: 'run',
      href: '/runs/f5dcb6a5'
    });
    const many = Array.from({ length: RECENT_RUNS + 5 }, (_, i) => ({
      ...input.runs[0],
      run_id: `r${i}`,
      started_at: new Date(Date.UTC(2026, 0, 1, 0, i)).toISOString()
    }));
    const recent = runEntries(many);
    expect(recent).toHaveLength(RECENT_RUNS);
    expect(recent[0].id).toBe(`r${RECENT_RUNS + 4}`);
  });

  it('keeps every key unique, so the list can key on it', () => {
    const keys = entries.map((e) => e.key);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it('never offers a link out of the console', () => {
    for (const e of entries) expect(e.href.startsWith('/'), e.key).toBe(true);
  });
});

describe('the ranking', () => {
  const entry = (id: string, title = ''): PaletteEntry => ({
    kind: 'item',
    key: id,
    id,
    title,
    aliases: [],
    href: `/work?item=${id}`
  });

  it('puts an id match above a title match, and drops what does not match', () => {
    const list = [entry('1', 'mentions 2055'), entry('2055'), entry('3', 'nothing')];
    expect(rankEntries(list, '2055').map((e) => e.id)).toEqual(['2055', '1']);
    expect(scoreEntry(entry('3', 'nothing'), 'zzz')).toBe(0);
  });

  it('shows everything, in entry order, for an empty query, up to the cap', () => {
    expect(rankEntries(entries, '')).toEqual(entries.slice(0, MAX_RESULTS));
  });
});

describe('the keys', () => {
  const three = rankEntries(entries, '').slice(0, 3);

  it('move the cursor with the arrows, wrapping', () => {
    expect(paletteKey('ArrowDown', 0, three)).toEqual({ kind: 'move', cursor: 1 });
    expect(paletteKey('ArrowDown', 2, three)).toEqual({ kind: 'move', cursor: 0 });
    expect(paletteKey('ArrowUp', 0, three)).toEqual({ kind: 'move', cursor: 2 });
  });

  it('close on Esc, and leave every other key to the field', () => {
    expect(paletteKey('Escape', 0, three)).toEqual({ kind: 'close' });
    expect(paletteKey('a', 0, three)).toBeNull();
    expect(paletteKey('Enter', 0, [])).toBeNull();
  });
});

describe('polling', () => {
  it('the palette keeps no clock of its own', () => {
    // The behaviour is driven in `palette.client.test.ts`; this is the rule
    // over the source, so a later edit cannot add one quietly.
    const source = readFileSync(
      fileURLToPath(new URL('./Palette.svelte', import.meta.url)),
      'utf8'
    ).replace(/\/\/.*$/gm, '');
    expect(source).not.toMatch(/setInterval|setTimeout|requestAnimationFrame/);
    expect(source).toContain('factoryPulse.runs');
  });
});
