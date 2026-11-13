/**
 * #2248 — the stream's URL state, and the two windows it resolves to.
 *
 * What is pinned here is mostly what a bad value does. This query is a link an
 * operator pastes to a colleague, a bookmark from an older build, a URL a hand
 * edited: every one of those can carry a range this module does not ship, and
 * the only safe answer is the default window rather than a fetch nobody asked
 * for.
 */
import { describe, expect, it } from 'vitest';
import { PAN_FRACTION, panBy, zoom, ZOOM_FACTOR } from './brush';
import {
  applyPreset,
  DAY_MS,
  HOUR_MS,
  isoOf,
  jumpToNow,
  KIND_PREFIXES,
  LIVE_PRESET,
  listFilters,
  LIVE_QUERY,
  MAX_RANGE_MS,
  MINUTE_MS,
  moveBounds,
  parseStreamQuery,
  PRESETS,
  presetFor,
  resolveStreamScope,
  serializeStreamQuery,
  streamFilters,
  streamSearch,
  STREAM_KINDS,
  toggleKind,
  withWindow,
  type Span,
  type StreamQuery
} from './stream-query';

const NOW = Date.parse('2026-10-02T10:04:00.000Z');
const params = (search: string) => new URLSearchParams(search);

/** `Custom…` standing on one pinned window — the range that follows its brush. */
const customScope = (window: Span) =>
  resolveStreamScope(
    { ...LIVE_QUERY, preset: 'custom', fromMs: window.startMs, toMs: window.endMs },
    NOW
  );

describe('parseStreamQuery / serializeStreamQuery', () => {
  it('round-trips from, to, epic, item and kind', () => {
    const query: StreamQuery = {
      preset: '24h',
      fromMs: Date.parse('2026-10-02T02:45:00.000Z'),
      toMs: Date.parse('2026-10-02T04:30:00.000Z'),
      epic: 'ui-refine',
      item: '2219',
      kinds: ['review', 'money']
    };
    const written = serializeStreamQuery(query);
    expect(written.get('from')).toBe('2026-10-02T02:45:00.000Z');
    expect(written.get('to')).toBe('2026-10-02T04:30:00.000Z');
    expect(written.get('epic')).toBe('ui-refine');
    expect(written.get('item')).toBe('2219');
    expect(written.getAll('kind')).toEqual(['review', 'money']);
    expect(parseStreamQuery(written)).toEqual(query);
  });

  it('round-trips every preset', () => {
    for (const preset of PRESETS) {
      const query = applyPreset(LIVE_QUERY, preset.key, NOW);
      expect(parseStreamQuery(serializeStreamQuery(query))).toEqual(query);
    }
  });

  it('writes nothing for the default, so /monitor stays /monitor', () => {
    expect(streamSearch(LIVE_QUERY)).toBe('');
    expect(parseStreamQuery(params(''))).toEqual(LIVE_QUERY);
  });

  it('falls back to Live 30m for a range nobody ships', () => {
    expect(parseStreamQuery(params('range=30d')).preset).toBe(LIVE_PRESET);
    expect(parseStreamQuery(params('range=')).preset).toBe(LIVE_PRESET);
    // A custom range IS its window, so one without a window is no range.
    expect(parseStreamQuery(params('range=custom')).preset).toBe(LIVE_PRESET);
  });

  it('falls back to Live 30m for a window it cannot read', () => {
    for (const search of [
      'from=yesterday&to=2026-10-02T04:30:00Z',
      'from=2026-10-02T02:45:00Z',
      'to=2026-10-02T04:30:00Z',
      // A from at or after its to is not a window either.
      'from=2026-10-02T04:30:00Z&to=2026-10-02T02:45:00Z',
      'from=2026-10-02T04:30:00Z&to=2026-10-02T04:30:00Z'
    ]) {
      const query = parseStreamQuery(params(search));
      expect(query.fromMs, search).toBeNull();
      expect(query.toMs, search).toBeNull();
      expect(resolveStreamScope(query, NOW).live, search).toBe(true);
    }
  });

  it('drops a kind it does not ship and keeps the toggle order', () => {
    const query = parseStreamQuery(params('kind=money&kind=telepathy&kind=dispatch'));
    expect(query.kinds).toEqual(['dispatch', 'money']);
    expect(serializeStreamQuery(query).getAll('kind')).toEqual(['dispatch', 'money']);
  });

  it('reads one kind param holding several, so a hand-typed link works', () => {
    expect(parseStreamQuery(params('kind=epic,review')).kinds).toEqual(['review', 'epic']);
  });

  it('trims a blank filter away rather than fetching on an empty string', () => {
    const query = parseStreamQuery(params('epic=%20&item='));
    expect(query.epic).toBeNull();
    expect(query.item).toBeNull();
  });
});

describe('resolveStreamScope', () => {
  it('reads the default as the last 30 minutes, live', () => {
    const scope = resolveStreamScope(LIVE_QUERY, NOW);
    expect(scope.live).toBe(true);
    expect(scope.range).toEqual({ startMs: NOW - 30 * MINUTE_MS, endMs: NOW });
    expect(scope.window).toEqual(scope.range);
  });

  it('pauses the moment a window is pinned, live preset or not', () => {
    const window = { startMs: NOW - 10 * MINUTE_MS, endMs: NOW - 5 * MINUTE_MS };
    expect(resolveStreamScope(withWindow(LIVE_QUERY, window), NOW).live).toBe(false);
    const day = applyPreset(LIVE_QUERY, '24h', NOW);
    expect(resolveStreamScope(day, NOW).live).toBe(false);
  });

  it('spans the preset in the overview and brushes a slice of it', () => {
    const day = applyPreset(LIVE_QUERY, '24h', NOW);
    expect(resolveStreamScope(day, NOW).range).toEqual({ startMs: NOW - DAY_MS, endMs: NOW });
    const brushed = withWindow(day, { startMs: NOW - 2 * HOUR_MS, endMs: NOW - HOUR_MS });
    const scope = resolveStreamScope(brushed, NOW);
    expect(scope.range).toEqual({ startMs: NOW - DAY_MS, endMs: NOW });
    expect(scope.window).toEqual({ startMs: NOW - 2 * HOUR_MS, endMs: NOW - HOUR_MS });
  });

  it('widens the range to hold a window Load older reached past it', () => {
    const day = applyPreset(LIVE_QUERY, '24h', NOW);
    const older = withWindow(day, { startMs: NOW - 3 * DAY_MS, endMs: NOW - HOUR_MS });
    const scope = resolveStreamScope(older, NOW);
    expect(scope.range).toEqual({ startMs: NOW - 3 * DAY_MS, endMs: NOW });
  });

  it('reads a custom range as its own window', () => {
    const custom = parseStreamQuery(
      params('range=custom&from=2026-09-01T00:00:00Z&to=2026-09-02T00:00:00Z')
    );
    const scope = resolveStreamScope(custom, NOW);
    expect(scope.preset.key).toBe('custom');
    expect(scope.range).toEqual(scope.window);
    expect(scope.window.startMs).toBe(Date.parse('2026-09-01T00:00:00Z'));
  });
});

/**
 * What a pan or a zoom is clamped to.
 *
 * A preset is a span of the clock, so the window moves inside a fixed range.
 * `Custom…` has no span of its own — its range IS its window — and clamping a
 * move to that range made a zoom-in final: the window shrank, the range shrank
 * with it, and the zoom-out, the pans and the reset that should undo it each
 * measured a window that already filled its range and answered with it
 * unchanged. There is then no way back to the range the operator pinned.
 */
describe('moveBounds', () => {
  it('is the range itself under a preset that has a span', () => {
    const day = applyPreset(LIVE_QUERY, '24h', NOW);
    const scope = resolveStreamScope(day, NOW);
    expect(moveBounds(scope)).toEqual(scope.range);
    const brushed = resolveStreamScope(
      withWindow(day, { startMs: NOW - 2 * HOUR_MS, endMs: NOW - HOUR_MS }),
      NOW
    );
    expect(moveBounds(brushed)).toEqual(brushed.range);
  });

  it('reaches a window’s own span to either side of it under Custom…', () => {
    const custom = applyPreset(applyPreset(LIVE_QUERY, '6h', NOW), 'custom', NOW);
    const scope = resolveStreamScope(custom, NOW);
    expect(scope.range).toEqual(scope.window);
    // Wider than any one move asks for — a zoom out is 1.5×, an arrow key a
    // tenth — so the move happens exactly rather than clamped to near it.
    expect(moveBounds(scope)).toEqual({
      startMs: NOW - 9 * HOUR_MS,
      endMs: NOW + 3 * HOUR_MS
    });
  });

  it('stops a zoom-out at the widest range the stream navigates', () => {
    const scope = customScope({ startMs: NOW - MAX_RANGE_MS, endMs: NOW });
    const bounds = moveBounds(scope);
    expect(bounds.endMs - bounds.startMs).toBe(MAX_RANGE_MS);
    // A held-down `−` has nowhere further to go, which is the point of a cap.
    expect(zoom(bounds, scope.window, 1 / ZOOM_FACTOR, DAY_MS)).toEqual(scope.window);
    // And still an instant `isoOf` can write down.
    expect(() => isoOf(bounds.startMs)).not.toThrow();
  });

  /**
   * A hand-edited URL can pin a window wider than the cap, and bounds that
   * excluded it made every key a collapse: `brush.ts` answers a window at least
   * as wide as its bounds with the BOUNDS, so a pan, a zoom-out and a zoom-in
   * all returned the same centred ten years — a pan that panned nowhere, a
   * zoom-out that narrowed, and twenty of the operator's thirty years gone.
   */
  it('holds a range wider than the cap rather than collapsing it', () => {
    const wide = { startMs: NOW - 30 * 365 * DAY_MS, endMs: NOW };
    const scope = customScope(wide);
    const bounds = moveBounds(scope);
    expect(bounds).toEqual(wide);
    // The moves that would grow it past the cap do nothing at all — and in
    // `EventStream` a move that lands on its own window pins no window, so the
    // range the operator stated survives the key.
    expect(panBy(bounds, wide, -PAN_FRACTION)).toEqual(wide);
    expect(panBy(bounds, wide, PAN_FRACTION)).toEqual(wide);
    expect(zoom(bounds, wide, 1 / ZOOM_FACTOR, DAY_MS)).toEqual(wide);
    // And a zoom-in — the way back under the cap — narrows by its own factor,
    // about the centre it holds, rather than by three times it.
    const inward = zoom(bounds, wide, ZOOM_FACTOR, DAY_MS);
    expect(inward.endMs - inward.startMs).toBe((wide.endMs - wide.startMs) * ZOOM_FACTOR);
    expect((inward.startMs + inward.endMs) / 2).toBe((wide.startMs + wide.endMs) / 2);
  });
});

describe('streamFilters', () => {
  it('passes the window and the filters the list and the histogram share', () => {
    const query: StreamQuery = {
      preset: '6h',
      fromMs: null,
      toMs: null,
      epic: 'ui-refine',
      item: '2219',
      kinds: ['money']
    };
    const filters = streamFilters(query, { startMs: NOW - HOUR_MS, endMs: NOW });
    expect(filters.since).toBe('2026-10-02T09:04:00.000Z');
    // `until` is exclusive, so the right edge is pushed out by a millisecond:
    // in live mode the newest event is stamped exactly at it.
    expect(filters.until).toBe('2026-10-02T10:04:00.001Z');
    expect(filters.epic).toBe('ui-refine');
    expect(filters.item).toBe('2219');
    expect(filters.event_prefix).toEqual([...KIND_PREFIXES.money]);
  });

  it('sends no event_prefix while every toggle is idle', () => {
    const filters = streamFilters(LIVE_QUERY, { startMs: NOW - HOUR_MS, endMs: NOW });
    expect(filters.event_prefix).toBeUndefined();
    expect(filters.epic).toBeUndefined();
    expect(filters.item).toBeUndefined();
  });

  it('ORs the prefixes of every kind pressed', () => {
    const query = toggleKind(toggleKind(LIVE_QUERY, 'epic'), 'dispatch');
    expect(query.kinds).toEqual(['dispatch', 'epic']);
    const filters = streamFilters(query, { startMs: NOW - HOUR_MS, endMs: NOW });
    expect(filters.event_prefix).toEqual([...KIND_PREFIXES.dispatch, ...KIND_PREFIXES.epic]);
  });

  it('every kind prefix is a prefix of the name, not the whole name', () => {
    for (const kind of STREAM_KINDS)
      for (const prefix of KIND_PREFIXES[kind]) expect(prefix).toMatch(/^[a-z][a-z_]*$/);
  });
});

describe('the range bar', () => {
  it('a preset sets the range and resets the brush to all of it', () => {
    const brushed = withWindow(applyPreset(LIVE_QUERY, '24h', NOW), {
      startMs: NOW - 2 * HOUR_MS,
      endMs: NOW - HOUR_MS
    });
    const next = applyPreset(brushed, '6h', NOW);
    expect(next.fromMs).toBeNull();
    const scope = resolveStreamScope(next, NOW);
    expect(scope.window).toEqual(scope.range);
    expect(scope.range).toEqual({ startMs: NOW - 6 * HOUR_MS, endMs: NOW });
  });

  it('a preset keeps the filters — the range bar is not a reset button', () => {
    const filtered = { ...LIVE_QUERY, epic: 'ui-refine', item: '2219', kinds: ['money' as const] };
    expect(applyPreset(filtered, '7d', NOW)).toMatchObject({
      epic: 'ui-refine',
      item: '2219',
      kinds: ['money']
    });
  });

  it('Custom keeps the range it is standing on', () => {
    const custom = applyPreset(applyPreset(LIVE_QUERY, '6h', NOW), 'custom', NOW);
    expect(resolveStreamScope(custom, NOW).range).toEqual({
      startMs: NOW - 6 * HOUR_MS,
      endMs: NOW
    });
  });

  it('L returns to live from anywhere', () => {
    const away = withWindow(applyPreset(LIVE_QUERY, '7d', NOW), {
      startMs: NOW - 6 * DAY_MS,
      endMs: NOW - 5 * DAY_MS
    });
    const back = jumpToNow(away);
    expect(resolveStreamScope(back, NOW).live).toBe(true);
    expect(streamSearch(back)).toBe('');
  });

  it('presetFor answers with the live preset for a key nobody ships', () => {
    expect(presetFor('30d').key).toBe(LIVE_PRESET);
    expect(presetFor('7d').label).toBe('7d');
  });
});

describe('listFilters', () => {
  const window: Span = { startMs: NOW - 30 * MINUTE_MS, endMs: NOW };
  const filtered: StreamQuery = { ...LIVE_QUERY, epic: 'ui-ux', item: '2248' };

  it('reads the live list unbounded, so an idle fleet still shows its last events', () => {
    const live = listFilters(filtered, window, true);
    expect(live).not.toHaveProperty('since');
    expect(live).not.toHaveProperty('until');
    expect(live).toMatchObject({ epic: 'ui-ux', item: '2248' });
  });

  it('keeps the window bounds for every non-live range', () => {
    expect(listFilters(filtered, window, false)).toEqual(streamFilters(filtered, window));
  });
});
