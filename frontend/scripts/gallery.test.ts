import { readdirSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import {
  LIVE_EPIC_CAP,
  SHOTS,
  isSingleChain,
  liveEpicShots,
  liveFlag,
  pickLiveEpics,
  planShots
} from './gallery.mjs';

type Shot = { name: string; route: string; fixtures?: Record<string, string>; fixtureData?: boolean };

/**
 * Every id the committed fixture set names in a file name: the epics, the
 * nodes, the Work items and the run. Read off the directory rather than
 * listed here, so a new fixture id is covered the day it is committed.
 */
function fixtureIds(): string[] {
  const dir = new URL('../fixtures/factory/', import.meta.url);
  const ids = new Set<string>();
  for (const file of readdirSync(dir)) {
    const epic = /^epics-(.+?)(?:-nodes-(.+?))?(?:-tick-strip|-ticks|-timeline)?\.json$/.exec(file);
    if (epic) {
      ids.add(epic[1]);
      if (epic[2]) ids.add(epic[2]);
    }
    const work = /^(?:verbs-)?work-(.+?)(?:-description|-evidence|-pipeline)?\.json$/.exec(file);
    if (work) ids.add(work[1]);
    const run = /^runs-([0-9a-f]{8})/.exec(file);
    if (run) ids.add(run[1]);
  }
  return [...ids];
}

const namesFixtureData = (route: string, ids: string[]) =>
  ids.some((id) => route.split(/[/?=&]/).includes(id));

describe('the gallery in live mode (#2790)', () => {
  it('takes no shot that names fixture data or answers from an override', () => {
    const ids = fixtureIds();
    expect(ids).toEqual(expect.arrayContaining(['launch-fixes', 'go-live-2026-09', '2055', 'SFD-229', 'f5dcb6a5']));
    const { shots } = planShots(SHOTS, { live: true }) as { shots: Shot[] };
    expect(shots.length).toBeGreaterThan(0);
    const leaks = shots.filter((s) => s.fixtures || namesFixtureData(s.route, ids)).map((s) => s.name);
    expect(leaks).toEqual([]);
  });

  it('lists every shot it leaves out, with the reason, and leaves nothing else out', () => {
    const { shots, skipped } = planShots(SHOTS, { live: true }) as {
      shots: Shot[];
      skipped: { name: string; route: string; reason: string }[];
    };
    expect(skipped.map((s) => s.name)).toEqual(
      expect.arrayContaining(['work-sole-wait', 'work-first-run', 'epic-graph', 'run-events'])
    );
    expect(skipped.every((s) => s.reason.length > 0)).toBe(true);
    expect([...shots, ...skipped].map((s) => s.name).sort()).toEqual(SHOTS.map((s: Shot) => s.name).sort());
  });

  it('is what --live asks for, and the fixture run takes every shot', () => {
    expect(liveFlag(['http://127.0.0.1:8000', '--live'])).toBe(true);
    expect(liveFlag(['http://127.0.0.1:5173'])).toBe(false);
    expect(planShots(SHOTS, { live: false })).toEqual({ shots: SHOTS, skipped: [] });
  });
});

describe('the fixture gallery photographs the c4 fixes (#2790)', () => {
  it('has the palette, the launchpad drawer, the model picker and a zoomed-in selected node', () => {
    const byName = new Map((SHOTS as (Shot & { then?: object[]; shows?: string })[]).map((s) => [s.name, s]));
    for (const name of ['palette-open', 'launchpad-drawer', 'config-model-picker', 'epic-node-zoom-in']) {
      const shot = byName.get(name);
      expect(shot, name).toBeDefined();
      expect(shot?.then?.length, `${name} opens nothing`).toBeGreaterThan(0);
      expect(shot?.shows, `${name} names nothing the ui-check can hold`).toBeTruthy();
    }
    expect(byName.get('config-model-picker')?.route).toBe('/config');
    expect(byName.get('epic-node-zoom-in')?.route).toContain('node=');
  });
});

describe('the live gallery photographs the live roster (#2797)', () => {
  const epic = (epic_ref: string, signal: string | null, archived = false) => ({ epic_ref, signal, archived });

  it('picks a stalled or needs-you epic first, then a progressing one, and caps at two', () => {
    const roster = [
      epic('moving-a', 'progressing'),
      epic('moving-b', 'progressing'),
      epic('waiting', 'progressing'),
      epic('stuck', 'stalled')
    ];
    const { chosen, note } = pickLiveEpics(roster, { waiting: 3 });
    expect(chosen).toEqual([
      { ref: 'waiting', why: 'needs-you (3)' },
      { ref: 'moving-a', why: 'progressing' }
    ]);
    expect(chosen).toHaveLength(LIVE_EPIC_CAP);
    expect(note).toBeNull();
    expect(pickLiveEpics(roster).chosen.map((c: { ref: string }) => c.ref)).toEqual(['stuck', 'moving-a']);
  });

  it('never picks a finished or archived epic, and one progressing epic is enough', () => {
    const roster = [epic('done', 'complete'), epic('shelved', 'stalled', true), epic('moving', 'progressing')];
    expect(pickLiveEpics(roster, { done: 1, shelved: 2 }).chosen).toEqual([{ ref: 'moving', why: 'progressing' }]);
  });

  it('an empty roster yields no epic shots and a manifest note', () => {
    for (const roster of [[], [epic('done', 'complete')]]) {
      const { chosen, note } = pickLiveEpics(roster, {});
      expect(chosen).toEqual([]);
      expect(liveEpicShots(chosen)).toEqual([]);
      expect(note).toMatch(/no active epic/);
    }
  });

  it('shoots graph, timeline and linear per chosen epic, named for its ref', () => {
    const shots = liveEpicShots([{ ref: 'go live/1', why: 'stalled' }]) as Shot[];
    expect(shots.map((s) => s.name)).toEqual([
      'live-epic-graph-go_live_1',
      'live-epic-timeline-go_live_1',
      'live-epic-linear-go_live_1'
    ]);
    expect(shots.map((s) => s.route)).toEqual([
      '/epics/go%20live%2F1?view=graph',
      '/epics/go%20live%2F1?view=timeline',
      '/epics/go%20live%2F1'
    ]);
  });
});

describe('the live gallery takes a linear shot only of a linear epic (#2899)', () => {
  const chain = { nodes: [{ id: 'a' }, { id: 'b' }, { id: 'c' }], edges: [['a', 'b'], ['b', 'c']] };
  const fork = { nodes: [{ id: 'a' }, { id: 'b' }, { id: 'c' }], edges: [['a', 'b'], ['a', 'c']] };

  it('reads a single chain as linear, and a fork, a join, two chains or a cycle as not', () => {
    expect(isSingleChain(chain)).toBe(true);
    expect(isSingleChain({ nodes: [{ id: 'a' }], edges: [] })).toBe(true);
    expect(isSingleChain(fork)).toBe(false);
    expect(isSingleChain({ nodes: chain.nodes, edges: [['a', 'c'], ['b', 'c']] })).toBe(false);
    expect(isSingleChain({ nodes: [...chain.nodes, { id: 'd' }], edges: chain.edges })).toBe(false);
    expect(isSingleChain({ nodes: chain.nodes, edges: [['a', 'b'], ['b', 'c'], ['c', 'a']] })).toBe(false);
    expect(isSingleChain({ nodes: [], edges: [] })).toBe(false);
    expect(isSingleChain(null)).toBe(false);
  });

  it('planShots skips the linear shot of a non-linear epic, with the reason, and keeps it for a linear one', () => {
    const epics = [
      { ref: 'branching', why: 'stalled', linear: false },
      { ref: 'straight', why: 'progressing', linear: true }
    ];
    const { shots, skipped } = planShots(SHOTS, { live: true, epics }) as {
      shots: Shot[];
      skipped: { name: string; route: string; reason: string }[];
    };
    const names = shots.map((s) => s.name);
    expect(names).toEqual(
      expect.arrayContaining([
        'live-epic-graph-branching',
        'live-epic-timeline-branching',
        'live-epic-graph-straight',
        'live-epic-timeline-straight',
        'live-epic-linear-straight'
      ])
    );
    expect(names).not.toContain('live-epic-linear-branching');
    const skip = skipped.find((s) => s.name === 'live-epic-linear-branching');
    expect(skip?.route).toBe('/epics/branching');
    expect(skip?.reason).toMatch(/not a single chain/);
    expect(skipped.some((s) => s.name === 'live-epic-linear-straight')).toBe(false);
  });

  it('skips the linear shot, saying why, when the epic graph could not be read', () => {
    const epics = [{ ref: 'unread', why: 'stalled', linear: null, graphError: '/api/epics/unread answered 500' }];
    const { shots, skipped } = planShots(SHOTS, { live: true, epics }) as {
      shots: Shot[];
      skipped: { name: string; reason: string }[];
    };
    expect(shots.map((s) => s.name)).not.toContain('live-epic-linear-unread');
    expect(skipped.find((s) => s.name === 'live-epic-linear-unread')?.reason).toMatch(/answered 500/);
  });
});
