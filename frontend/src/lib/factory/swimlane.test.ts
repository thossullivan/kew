/**
 * FUI-07 — the swimlane's segments, markers and layout.
 *
 * The segment is the part of a pipeline that makes a claim: a solid green line
 * between two cells says the path between them is attested. So the table below
 * walks EVERY pair of flow classes and pins which of the five lines each one
 * draws, and two of those rows are the promises worth naming — an unknown state
 * never earns green, and green needs green at both ends.
 *
 * The last block renders the component server-side and reads the inline styles
 * back against the artboards' own numbers, which is the standing check on the
 * pixels until FUI-02's screenshot harness can shoot the two references.
 */
import { readFileSync } from 'node:fs';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import Swimlane from '../ui/Swimlane.svelte';
import { STATUS_MARKS } from './status';
import {
  captionTone,
  columnCount,
  FLOW_OF,
  laneCells,
  laneMinWidth,
  laneView,
  markerFor,
  SEGMENTS,
  segmentLeft,
  segmentStyle,
  segmentTop,
  STUB_MARK,
  stubOffset,
  stubSize,
  SWIMLANE_SIZES,
  type SegmentKind,
  type SwimlaneLane
} from './swimlane';

const DESIGN = new URL('../../../design/factory-ui/', import.meta.url);
const read = (name: string) => readFileSync(new URL(name, DESIGN), 'utf8');

/** One state word per flow class, for the exhaustive pair table. */
const BY_FLOW = {
  settled: 'attested',
  live: 'working',
  stop: 'pending',
  skipped: 'not_required',
  off: 'inactive'
} as const;

describe('FLOW_OF', () => {
  it('classifies every status meaning, so no cell falls through', () => {
    expect(Object.keys(FLOW_OF).sort()).toEqual(Object.keys(STATUS_MARKS).sort());
  });
});

describe('markerFor', () => {
  it('draws the four markers the artboards name', () => {
    expect(markerFor('attested').mark).toMatchObject({ shape: 'dot', color: 'var(--success)' });
    expect(markerFor('working').mark).toMatchObject({
      shape: 'dot-halo',
      color: 'var(--accent-2)'
    });
    expect(markerFor('needs_you').mark).toMatchObject({
      shape: 'diamond-halo',
      color: 'var(--accent)'
    });
    expect(markerFor('awaiting').mark).toMatchObject({ shape: 'ring-dashed' });
  });

  it('reads the release lane’s “awaiting” as pending, which is a dashed ring', () => {
    const m = markerFor('awaiting');
    expect(m.status).toBe('pending');
    expect(m.flow).toBe('stop');
    expect(m.known).toBe(true);
  });

  it('gives a not-required stage the hollow faint ring, not a filled mark', () => {
    const m = markerFor('not required');
    expect(m.mark).toMatchObject({ shape: 'ring', color: 'var(--text-4)' });
    expect(m.flow).toBe('skipped');
  });

  it('normalises spelling the way the status vocabulary does', () => {
    expect(markerFor('Not Required').status).toBe('not_required');
    expect(markerFor('  in-progress ').flow).toBe('live');
  });

  it('draws an unknown state as pending, never as attested', () => {
    const m = markerFor('backported');
    expect(m.known).toBe(false);
    expect(m.status).toBe('pending');
    expect(m.mark.shape).toBe('ring-dashed');
    expect(m.mark.color).not.toBe('var(--success)');
    expect(m.flow).toBe('stop');
  });

  it('keeps the server’s own word on an unknown state, so the row stays honest', () => {
    expect(markerFor('awaiting_backport').word).toBe('awaiting backport');
    expect(markerFor('awaiting_backport').mark.word).toBe('awaiting backport');
  });

  it('never lets a prototype member through as a known state', () => {
    expect(markerFor('constructor').known).toBe(false);
    expect(markerFor('__proto__').status).toBe('pending');
  });

  it('makes a missing or inactive state the stub, not a status', () => {
    for (const state of [null, undefined, '', '  ', 'inactive']) {
      const m = markerFor(state);
      expect(m.stub).toBe(true);
      expect(m.flow).toBe('off');
      expect(m.mark).toEqual(STUB_MARK);
    }
  });

  it('takes the run vocabulary when the caller says so', () => {
    expect(markerFor('queued').flow).toBe('stop');
    // The epic reading of `queued` is its own neutral mark since #2209; the
    // run reading is still `pending`. Both stop the path, and the two are
    // told apart by colour, not by flow.
    expect(markerFor('queued').mark.color).toBe('var(--text-4)');
    expect(markerFor('queued', 'run').mark.color).toBe('var(--text-3)');
  });
});

describe('segmentStyle', () => {
  /** Every ordered pair of flow classes, and the line it draws. */
  const PAIRS: Array<[keyof typeof BY_FLOW, keyof typeof BY_FLOW, SegmentKind]> = [
    ['settled', 'settled', 'attested'],
    ['settled', 'live', 'working'],
    ['settled', 'stop', 'pending'],
    ['settled', 'skipped', 'quiet'],
    ['settled', 'off', 'hairline'],
    ['live', 'settled', 'quiet'],
    ['live', 'live', 'working'],
    ['live', 'stop', 'pending'],
    ['live', 'skipped', 'quiet'],
    ['live', 'off', 'hairline'],
    ['stop', 'settled', 'pending'],
    ['stop', 'live', 'working'],
    ['stop', 'stop', 'pending'],
    ['stop', 'skipped', 'pending'],
    ['stop', 'off', 'hairline'],
    ['skipped', 'settled', 'quiet'],
    ['skipped', 'live', 'working'],
    ['skipped', 'stop', 'pending'],
    ['skipped', 'skipped', 'quiet'],
    ['skipped', 'off', 'hairline'],
    ['off', 'settled', 'hairline'],
    ['off', 'live', 'hairline'],
    ['off', 'stop', 'hairline'],
    ['off', 'skipped', 'hairline'],
    ['off', 'off', 'hairline']
  ];

  it.each(PAIRS)('%s → %s draws the %s segment', (from, to, kind) => {
    expect(segmentStyle(BY_FLOW[from], BY_FLOW[to])).toEqual(SEGMENTS[kind]);
  });

  it('covers every ordered pair of flow classes exactly once', () => {
    const flows = Object.keys(BY_FLOW);
    expect(PAIRS).toHaveLength(flows.length * flows.length);
    expect(new Set(PAIRS.map(([a, b]) => `${a}>${b}`)).size).toBe(PAIRS.length);
  });

  it('gives the last column no segment at all', () => {
    expect(segmentStyle('attested')).toEqual(SEGMENTS.none);
    expect(segmentStyle('attested', undefined)).toEqual(SEGMENTS.none);
    expect(SEGMENTS.none.width).toBe(0);
  });

  it('distinguishes a null next cell — inactive — from no next cell at all', () => {
    expect(segmentStyle('attested', null)).toEqual(SEGMENTS.hairline);
  });

  it('never paints green under an unknown state on either end', () => {
    expect(segmentStyle('backported', 'attested').kind).toBe('pending');
    expect(segmentStyle('attested', 'backported').kind).toBe('pending');
  });

  it('draws the five lines the plan specifies', () => {
    expect(SEGMENTS.attested).toMatchObject({ color: 'var(--success)', width: 2, dashed: false });
    expect(SEGMENTS.working).toMatchObject({ color: 'var(--accent-2)', dashed: false });
    expect(SEGMENTS.pending).toMatchObject({ color: 'var(--text-5)', width: 2, dashed: true });
    expect(SEGMENTS.hairline).toMatchObject({ width: 1, dashed: true });
  });

  it('walks the DWork main lane exactly as the artboard draws it', () => {
    const lane = ['not_required', 'attested', 'attested', 'working', 'needs_you'];
    const kinds = lane.map((state, i) => segmentStyle(state, lane[i + 1]).kind);
    expect(kinds).toEqual(['quiet', 'attested', 'working', 'pending', 'none']);
  });
});

describe('laneCells', () => {
  it('pads a lane with fewer stages than the grid has columns', () => {
    const padded = laneCells([{ state: 'attested' }], 5);
    expect(padded).toHaveLength(5);
    expect(padded.slice(1).every((c) => c.state === null)).toBe(true);
  });

  it('pads with inactive rather than repeating the last real cell', () => {
    const padded = laneCells([{ state: 'attested' }], 3);
    expect(markerFor(padded[2].state).flow).toBe('off');
  });

  it('never drops a cell the caller sent', () => {
    const cells = [{ state: 'attested' }, { state: 'working' }, { state: 'pending' }];
    expect(laneCells(cells, 2)).toEqual(cells);
  });
});

describe('columnCount', () => {
  it('counts the named stages', () => {
    expect(columnCount(['Implement', 'Publish', 'Review', 'Merge'])).toBe(4);
  });

  it('widens for a lane that carries more cells than there are names', () => {
    const lane: SwimlaneLane = { label: 'main', cells: [{}, {}, {}] };
    expect(columnCount(['Implement'], [lane])).toBe(3);
  });
});

describe('laneView', () => {
  const lane: SwimlaneLane = {
    label: 'release/[branch]',
    cells: [{ state: null }, { state: 'awaiting', primary: 'awaiting backport' }]
  };

  it('resolves a short lane against the stage names and pads the rest', () => {
    const view = laneView(lane, 5, ['Reproduce', 'Implement', 'Publish', 'Review', 'Merge']);
    expect(view.map((v) => v.stage)).toEqual([
      'Reproduce',
      'Implement',
      'Publish',
      'Review',
      'Merge'
    ]);
    expect(view.map((v) => v.marker.stub)).toEqual([true, false, true, true, true]);
    // Hairlines throughout, and nothing leading out of the last column.
    expect(view.map((v) => v.segment.kind)).toEqual([
      'hairline',
      'hairline',
      'hairline',
      'hairline',
      'none'
    ]);
  });

  it('leaves a column no stage named unlabelled rather than inventing a name', () => {
    const view = laneView({ label: 'main', cells: [{}, {}] }, 2, ['Implement']);
    expect(view[1].stage).toBeNull();
  });

  // #3466: SFD-229 published with no scan recorded, and its review round ended
  // "needs changes". The publish cell is pending, which used to stop the path
  // there, so the lane drew both of its lines as the never-reached dash.
  const SFD_229: SwimlaneLane = {
    label: 'main',
    cells: [
      { state: 'done' },
      { state: 'pending', passed: true },
      { state: 'not_ready' },
      { state: 'needs_you' }
    ]
  };

  it('runs the path through a passed publish, keeping its pending mark', () => {
    const view = laneView(SFD_229, 4, ['implement', 'publish', 'review', 'merge'], 'run');
    expect(view[1].marker.status).toBe('pending');
    expect(view[1].marker.mark.shape).toBe('ring-dashed');
    expect(view[1].marker.mark.color).not.toBe('var(--success)');
    expect(view.map((v) => v.segment.kind)).toEqual(['attested', 'working', 'pending', 'none']);
  });

  it('stops the path at a pending stage the server did not say is over', () => {
    const notPassed = { ...SFD_229, cells: SFD_229.cells.map(({ state }) => ({ state })) };
    const view = laneView(notPassed, 4, [], 'run');
    // The line into the publish is the never-reached dash again; the one out of
    // it into a live cell is the working line, as any line into one is.
    expect(view.map((v) => v.segment.kind)).toEqual(['pending', 'working', 'pending', 'none']);
  });

  it('lets only a stop through on `passed`: an off cell stays the stub', () => {
    expect(markerFor('pending', 'run', true).flow).toBe('settled');
    expect(markerFor(null, 'run', true).flow).toBe('off');
    expect(markerFor('working', 'run', true).flow).toBe('live');
  });
});

describe('captionTone', () => {
  const attested = markerFor('attested');
  const needsYou = markerFor('needs_you');
  const notRequired = markerFor('not required');

  it('colours the status line in the status’s colour and the other quiet', () => {
    expect(captionTone(attested, 'primary', 'secondary')).toBe('var(--text-1)');
    expect(captionTone(attested, 'secondary', 'secondary')).toBe('var(--success)');
    expect(captionTone(needsYou, 'primary', 'primary')).toBe('var(--accent)');
    expect(captionTone(needsYou, 'secondary', 'primary')).toBe('var(--text-3)');
  });

  it('keeps a quiet meaning quiet, so "not required" does not shout', () => {
    expect(captionTone(notRequired, 'primary', 'primary')).toBe('var(--text-3)');
  });

  it('draws both lines of an inactive cell quiet', () => {
    const stub = markerFor(null);
    expect(captionTone(stub, 'primary', 'primary')).toBe('var(--text-3)');
    expect(captionTone(stub, 'secondary', 'secondary')).toBe('var(--text-3)');
  });
});

describe('geometry', () => {
  it('starts a segment at the marker’s centre', () => {
    expect(segmentLeft(14)).toBe(7);
    expect(segmentLeft(12)).toBe(6);
  });

  it('puts a segment’s centreline on the marker’s, as the artboards do', () => {
    // DWork: `top: 6px` for the 2px lines, `top: 7px` for the 1px hairline.
    expect(segmentTop(14, 2)).toBe(6);
    expect(segmentTop(14, 1)).toBe(7);
    // DEpic rail: `top: 5px` for the 2px lines.
    expect(segmentTop(12, 2)).toBe(5);
  });

  it('draws the stub as the artboard’s 6px dot, centred', () => {
    expect(stubSize(14)).toBe(6);
    expect(stubOffset(14)).toBe(4);
    expect(stubSize(12)).toBeCloseTo(5.14, 2);
  });

  it('offers exactly the two sizes the plan names', () => {
    expect(Object.keys(SWIMLANE_SIZES).sort()).toEqual(['12', '14']);
    expect(SWIMLANE_SIZES[14].laneWidth).toBe(148);
    expect(SWIMLANE_SIZES[14].rowRule).toBe(true);
    expect(SWIMLANE_SIZES[12].rowRule).toBe(false);
  });

  it('floors a stage column above the widest word it has to hold (#2372)', () => {
    // 11px mono tracked 0.14em is about 8.2px a character, so "IMPLEMENT" and
    // "REPRODUCE" — the longest stage names DWork draws — need about 74px of
    // header. A caption's longest unbreakable token is a short SHA: eight
    // characters of 13px mono, about 63px, and it still owes the column beside
    // it `captionGutter`. The floor clears both, which is the whole fix: at
    // 390px the columns used to be narrower than their own labels.
    const header = 9 * 8.2;
    for (const size of [14, 12] as const) {
      const g = SWIMLANE_SIZES[size];
      expect(g.minColumn).toBeGreaterThan(header);
      expect(g.minColumn).toBeGreaterThan(8 * 7.8 + g.captionGutter);
    }
  });

  it('floors the lane at the lane column plus one minColumn per stage', () => {
    // DWork's five stages beside the 148px lane column.
    expect(laneMinWidth(14, 5, true)).toBe(148 + 5 * 112);
    // The four kew actually serves on `/work?item=2055`.
    expect(laneMinWidth(14, 4, true)).toBe(148 + 4 * 112);
    // The DEpic rail has no lane column to pay for.
    expect(laneMinWidth(12, 5, false)).toBe(5 * 84);
    // A grid with no columns is still owed its lane column, and never less.
    expect(laneMinWidth(14, 0, true)).toBe(148);
    expect(laneMinWidth(14, 0, false)).toBe(0);
  });
});

describe('Swimlane.svelte', () => {
  /** The rendered markup, comments stripped and whitespace between tags gone. */
  function draw(props: Record<string, unknown>): string {
    return render(Swimlane, { props: { stages: [], lanes: [], ...props } })
      .body.replace(/<!--[\s\S]*?-->/g, '')
      .replace(/>\s+</g, '><');
  }

  const STAGES = ['Reproduce', 'Implement', 'Publish', 'Review', 'Merge'];
  const MAIN: SwimlaneLane = {
    label: 'main',
    cells: [
      { state: 'not required', primary: 'not required', secondary: 'low', statusLine: 'primary' },
      { state: 'attested', primary: 'run f5dcb6a5', secondary: 'success' },
      { state: 'attested', primary: 'scan clean', secondary: '3670e43f' },
      { state: 'working', primary: 'round 1', secondary: 'reviewing' },
      {
        state: 'needs_you',
        primary: 'needs you',
        secondary: 'merge decision',
        statusLine: 'primary'
      }
    ]
  };
  const RELEASE: SwimlaneLane = {
    label: 'release/[branch]',
    muted: true,
    cells: [{}, { state: 'awaiting', primary: 'awaiting backport' }]
  };

  it('lays the DWork pipeline out on the artboard’s grid', () => {
    const html = draw({ stages: STAGES, lanes: [MAIN, RELEASE] });
    expect(html).toContain('grid-template-columns: 148px repeat(5, minmax(0, 1fr));');
    expect(html).toContain('padding-top: 14px; padding-bottom: 16px;');
    expect(html).toContain('>Lane</div>');
    expect(html).toContain('>Implement</div>');
    // The lane a human is not running is named quiet.
    expect(html).toContain('text-text-1">main');
    expect(html).toContain('text-text-2">release/[branch]');
  });

  it('draws the DWork main lane’s four segments', () => {
    const html = draw({ stages: STAGES, lanes: [MAIN] });
    // not required → attested: walked, nothing proven along it.
    expect(html).toContain('left: 7px; top: 6px; border-top: 2px solid var(--hair-strong);');
    expect(html).toContain('left: 7px; top: 6px; border-top: 2px solid var(--success);');
    expect(html).toContain('left: 7px; top: 6px; border-top: 2px solid var(--accent-2);');
    expect(html).toContain('left: 7px; top: 6px; border-top: 2px dashed var(--text-5);');
    // The Merge column leads nowhere, so exactly four lines are drawn.
    expect(html.match(/border-top: 2px/g)).toHaveLength(4);
  });

  it('draws an inactive cell as the 6px dot on a 1px dashed hairline', () => {
    const html = draw({ stages: STAGES, lanes: [RELEASE] });
    expect(html).toContain('left: 7px; top: 7px; border-top: 1px dashed var(--hair-strong);');
    expect(html).toContain(
      'left: 4px; top: 4px; width: 6px; height: 6px; background-color: var(--hair-strong);'
    );
  });

  it('colours the caption lines the way the artboard does', () => {
    const html = draw({ stages: STAGES, lanes: [MAIN] });
    expect(html).toContain('color: var(--text-1);">run f5dcb6a5</span>');
    expect(html).toContain('color: var(--success);">success</span>');
    expect(html).toContain('color: var(--accent);">needs you</span>');
    expect(html).toContain('color: var(--text-3);">merge decision</span>');
    expect(html).toContain('color: var(--text-3);">not required</span>');
  });

  it('draws the compact DEpic rail: 12px cells, stage names in the cell, no lane column', () => {
    const html = draw({
      stages: STAGES,
      lanes: [MAIN],
      size: 12,
      bg: 'var(--ink-raised)'
    });
    expect(html).toContain('grid-template-columns: repeat(5, minmax(0, 1fr));');
    expect(html).toContain('height: 12px;');
    expect(html).toContain('gap: 8px;');
    expect(html).toContain('left: 6px; top: 5px; border-top: 2px solid var(--success);');
    expect(html).toContain('text-11 text-text-3">Reproduce</span>');
    expect(html).not.toContain('>Lane</div>');
    // No row rule on the rail — the column it sits in already has a border.
    expect(html).not.toContain('border-hair-quiet');
  });

  it('pads a short lane out to the column count instead of collapsing the grid', () => {
    const html = draw({ stages: STAGES, lanes: [RELEASE] });
    const cells = html.match(/style="height: 14px;"/g);
    expect(cells).toHaveLength(5);
  });

  it('reads an inactive cell with nothing to say as the artboard\u2019s em dash', () => {
    const html = draw({ stages: STAGES, lanes: [RELEASE] });
    expect(html.match(/class="text-text-3">\u2014<\/span>/g)).toHaveLength(4);
    expect(draw({ stages: STAGES, lanes: [RELEASE], placeholder: '' })).not.toContain('\u2014');
  });

  it('never covers a cell that does have something to say', () => {
    const html = draw({
      stages: ['Merge'],
      lanes: [{ label: 'release', cells: [{ primary: 'skipped this release' }] }]
    });
    expect(html).toContain('>skipped this release</span>');
    expect(html).not.toContain('\u2014');
  });

  it('renders an unknown state as a dashed pending ring, with its own word', () => {
    const html = draw({
      stages: ['Backport'],
      lanes: [{ label: 'release', cells: [{ state: 'backported' }] }]
    });
    expect(html).toContain('aria-label="backported"');
    expect(html).toContain('stroke-dasharray');
    expect(html).not.toContain('var(--success)');
  });

  it('scrolls inside its own box instead of squeezing the columns (#2372)', () => {
    const html = draw({ stages: STAGES, lanes: [MAIN, RELEASE] });
    // The box scrolls, not the page: a scroll container's automatic minimum
    // size is zero, which is what keeps a 390px page at 390px.
    expect(html).toContain('overflow-x-auto');
    // Every band carries the same floor — the header rule, the 6px ticks and
    // both lane rows — so they stay on one set of columns as it scrolls.
    const floor = `min-width: ${148 + 5 * 112}px;`;
    expect(html.split(floor)).toHaveLength(5);
    // Desktop is untouched: the tracks are still the artboard's own.
    expect(html).toContain('grid-template-columns: 148px repeat(5, minmax(0, 1fr));');
  });

  it('floors the compact rail without charging it for a lane column', () => {
    const html = draw({ stages: STAGES, lanes: [MAIN], size: 12 });
    expect(html).toContain(`min-width: ${5 * 84}px;`);
    expect(html).not.toContain('148px');
  });

  it('hands a caption snippet the resolved cell', () => {
    const html = draw({ stages: ['Merge'], lanes: [MAIN], laneLabels: false });
    // No snippet here; this pins that the default caption is what renders.
    expect(html).toContain('font-mono text-13');
  });
});

describe('the artboards', () => {
  it('still draw the DWork lanes this component reproduces', () => {
    const dwork = read('DWork.html');
    expect(dwork).toContain('left: 7px; right: 0; top: 6px; height: 2px; background: #2ee6a6;');
    expect(dwork).toContain('border-top: 2px dashed #5a5070;');
    expect(dwork).toContain('left: 4px; top: 4px; width: 6px; height: 6px;');
    expect(dwork).toContain('grid-template-columns: 148px repeat(5, minmax(0, 1fr))');
  });

  it('still draw the DEpic rail at 12px', () => {
    const depic = read('DEpic.html');
    expect(depic).toContain('left: 6px; right: 0; top: 5px; height: 2px; background: #2ee6a6;');
    expect(depic).toContain('grid-template-columns: repeat(5, minmax(0, 1fr))');
  });
});
