/**
 * #2248 — the Monitor event stream's time navigation, as URL state.
 *
 * The stream used to be one fixed window: the newest 50 events, drawn over the
 * last 30 minutes, with no way to look further back and no way to narrow what
 * came into view. This module is the state behind the range bar, the brush and
 * the filter row — and it lives in the URL, so a window worth showing somebody
 * is a link rather than a sequence of clicks to repeat.
 *
 * Three rules run through it.
 *
 * **Two windows, not one.** The RANGE is what the overview histogram spans —
 * what a preset sets. The WINDOW is the slice of it the brush holds, and it is
 * what the detail strip and the list are drawn from. They start out equal; the
 * brush is what pulls them apart. The window is always inside the range
 * (`resolveStreamScope` widens the range to contain it rather than clipping
 * it), so "Load older" may reach back past the preset's start without the
 * overview losing the events it fetched. `Custom…` is the one range with no
 * span of its own: its range IS its window and follows it, which is why a pan
 * or a zoom there is bounded by `moveBounds` and not by the range it is moving.
 *
 * **Live is a reading, not a flag.** `live` means the right edge follows the
 * clock and the poll runs, and that is only true while the preset is the live
 * one AND no explicit window is pinned. Brushing writes `from`/`to`, which
 * pins the right edge, which pauses the poll — one fact, derived in one place,
 * rather than a flag that can disagree with the window beside it.
 *
 * **A value this module cannot read is not guessed at.** An unparseable range,
 * a `from` after its `to`, a kind nobody ships: the reading falls back to
 * `Live 30m` (or drops the filter) rather than fetching a window the operator
 * did not ask for. A link from an older build, or one a hand edited, opens on
 * the default instead of on an error.
 */
import type { AuditFilters } from '$lib/api';
import { parseInstant } from '$lib/time';

/** A stretch of time, in ms since the epoch. */
export type Span = { startMs: number; endMs: number };

/**
 * The event families the kind toggles offer, as prefixes of the event name —
 * which is what `/api/audit` filters on (`event_prefix`, an OR of half-open
 * ranges over `event`; see `_audit_prefix_filters` in run_store.py).
 *
 * The prefixes are the names the backend ships (`docs/audit.md`). A family is
 * therefore a narrowing and never an invention: an event name added after this
 * was written belongs to no family, so it is shown while every toggle is idle
 * and hidden the moment one is pressed. That is the safe direction — a new
 * event missing from a narrowed view is a filter doing its job, where a new
 * event silently counted as "money" would be a claim about it.
 */
export const KIND_PREFIXES = {
  dispatch: ['dispatch_', 'continuation_'],
  review: ['review_', 'adversarial_review_'],
  publication: ['publication_', 'merge_', 'certificate_'],
  epic: ['epic_'],
  evidence: ['sandbox_', 'egress_', 'journey_'],
  money: ['budget_', 'spend_', 'runner_usage']
} as const satisfies Record<string, readonly string[]>;

export type StreamKind = keyof typeof KIND_PREFIXES;

/** The toggles, in the order the artboard draws them. */
export const STREAM_KINDS = Object.keys(KIND_PREFIXES) as StreamKind[];

const IS_KIND = new Set<string>(STREAM_KINDS);

/** One button on the range bar. `spanMs` is `null` for the custom range. */
export type Preset = {
  key: string;
  label: string;
  spanMs: number | null;
  /** True for the one preset whose right edge follows the clock. */
  live: boolean;
};

export const MINUTE_MS = 60_000;
export const HOUR_MS = 60 * MINUTE_MS;
export const DAY_MS = 24 * HOUR_MS;

/**
 * The range bar, left to right. The set is the operator's (2026-10-02):
 * `Live 30m`, `1h`, `6h`, `24h`, `7d`, `Custom…`.
 */
export const PRESETS = [
  { key: 'live-30m', label: 'Live 30m', spanMs: 30 * MINUTE_MS, live: true },
  { key: '1h', label: '1h', spanMs: HOUR_MS, live: false },
  { key: '6h', label: '6h', spanMs: 6 * HOUR_MS, live: false },
  { key: '24h', label: '24h', spanMs: DAY_MS, live: false },
  { key: '7d', label: '7d', spanMs: 7 * DAY_MS, live: false },
  { key: 'custom', label: 'Custom…', spanMs: null, live: false }
] as const satisfies readonly Preset[];

export type PresetKey = (typeof PRESETS)[number]['key'];

/** The default, and what an unreadable range falls back to. */
export const LIVE_PRESET: PresetKey = 'live-30m';

const BY_KEY = new Map<string, Preset>(PRESETS.map((preset) => [preset.key, preset]));

/** The preset a key names, or the live one — a key nobody ships is not a range. */
export function presetFor(key: string): Preset {
  return BY_KEY.get(key) ?? BY_KEY.get(LIVE_PRESET)!;
}

/**
 * Everything the stream's URL carries.
 *
 * `fromMs`/`toMs` are the brushed window, or `null` for "the whole range" —
 * which is what a preset click leaves behind, and what keeps the live preset
 * live.
 */
export type StreamQuery = {
  preset: PresetKey;
  fromMs: number | null;
  toMs: number | null;
  epic: string | null;
  item: string | null;
  kinds: StreamKind[];
};

/** `Live 30m`, unfiltered — the state with no query at all. */
export const LIVE_QUERY: StreamQuery = {
  preset: LIVE_PRESET,
  fromMs: null,
  toMs: null,
  epic: null,
  item: null,
  kinds: []
};

/** An ISO instant, which is how a window is written down. */
export const isoOf = (ms: number): string => new Date(ms).toISOString();

function text(raw: string | null): string | null {
  const value = (raw ?? '').trim();
  return value === '' ? null : value;
}

/** The kinds the params name, deduplicated, in `STREAM_KINDS` order. */
function kindsOf(params: URLSearchParams): StreamKind[] {
  const named = new Set(
    params
      .getAll('kind')
      .flatMap((value) => value.split(','))
      .map((value) => value.trim())
      .filter((value) => IS_KIND.has(value))
  );
  return STREAM_KINDS.filter((kind) => named.has(kind));
}

/**
 * The window the params pin, or `null` when they pin none this module can
 * read: a missing edge, an unparseable instant, or a `from` that is not before
 * its `to`. A half-stated window is no window — drawing one edge against a
 * guess at the other is how a link ends up showing a range nobody chose.
 */
function windowOf(params: URLSearchParams): Span | null {
  const startMs = parseInstant(text(params.get('from')));
  const endMs = parseInstant(text(params.get('to')));
  if (startMs === null || endMs === null || startMs >= endMs) return null;
  return { startMs, endMs };
}

/** The URL, read. Anything it cannot read falls back to `Live 30m`. */
export function parseStreamQuery(params: URLSearchParams): StreamQuery {
  const raw = text(params.get('range'));
  const window = windowOf(params);
  // A custom range IS its window, so a custom range without one is no range.
  const known = raw !== null && BY_KEY.has(raw) && (raw !== 'custom' || window !== null);
  return {
    preset: known ? (raw as PresetKey) : LIVE_PRESET,
    fromMs: window?.startMs ?? null,
    toMs: window?.endMs ?? null,
    epic: text(params.get('epic')),
    item: text(params.get('item')),
    kinds: kindsOf(params)
  };
}

/**
 * The query, written down — only the parts that differ from the default, so
 * the Monitor's own URL stays `/monitor` until the operator navigates.
 */
export function serializeStreamQuery(query: StreamQuery): URLSearchParams {
  const params = new URLSearchParams();
  if (query.preset !== LIVE_PRESET) params.set('range', query.preset);
  if (query.fromMs !== null && query.toMs !== null && query.fromMs < query.toMs) {
    params.set('from', isoOf(query.fromMs));
    params.set('to', isoOf(query.toMs));
  }
  if (query.epic !== null) params.set('epic', query.epic);
  if (query.item !== null) params.set('item', query.item);
  for (const kind of STREAM_KINDS) if (query.kinds.includes(kind)) params.append('kind', kind);
  return params;
}

/** `?from=…&to=…`, or '' — what goes on the end of `/monitor`. */
export function streamSearch(query: StreamQuery): string {
  const qs = serializeStreamQuery(query).toString();
  return qs === '' ? '' : `?${qs}`;
}

/** The two windows and the one reading that decides whether the poll runs. */
export type StreamScope = {
  preset: Preset;
  /** True while the right edge follows the clock: the live preset, unbrushed. */
  live: boolean;
  /** What the overview histogram spans. */
  range: Span;
  /** What the detail strip and the list are drawn from. */
  window: Span;
};

const union = (a: Span, b: Span): Span => ({
  startMs: Math.min(a.startMs, b.startMs),
  endMs: Math.max(a.endMs, b.endMs)
});

/** The window the query pins, or `null` — which is what keeps live live. */
function pinnedWindow(query: StreamQuery): Span | null {
  return query.fromMs !== null && query.toMs !== null && query.fromMs < query.toMs
    ? { startMs: query.fromMs, endMs: query.toMs }
    : null;
}

/**
 * True while the right edge follows the clock and the poll runs.
 *
 * A reading of the query ALONE, deliberately: the component decides whether to
 * hold a timer open from it, and a reading that also depended on the clock would
 * make that decision re-evaluate on every tick of the clock it set.
 */
export function isLive(query: StreamQuery): boolean {
  return presetFor(query.preset).live && pinnedWindow(query) === null;
}

/** The names this module owns in a URL — what a page clears before writing. */
export const STREAM_PARAMS = ['range', 'from', 'to', 'epic', 'item', 'kind'] as const;

/**
 * The query against one clock reading.
 *
 * The range is the preset's span ending now; the window is the brush's, or the
 * whole range when nothing is brushed. The range is then WIDENED to hold the
 * window rather than the window clipped to the range: "Load older" pages past
 * the preset's start on purpose, and an overview that hid what the list had
 * just fetched would be the surface disagreeing with itself.
 */
export function resolveStreamScope(query: StreamQuery, nowMs: number): StreamScope {
  const preset = presetFor(query.preset);
  const pinned = pinnedWindow(query);
  const span = preset.spanMs;
  const presetRange = span === null ? null : { startMs: nowMs - span, endMs: nowMs };
  // Neither a span nor a window: a custom range nobody stated. Read as live.
  const window = pinned ?? presetRange ?? { startMs: nowMs - 30 * MINUTE_MS, endMs: nowMs };
  return {
    preset,
    live: preset.live && pinned === null,
    range: presetRange === null ? window : union(presetRange, window),
    window
  };
}

/**
 * The widest range a move may reach. A zoom-out under `Custom…` grows the range
 * rather than being clamped by it, so there has to be something a held-down key
 * stops at: ten years is past any factory's log, and well inside the instants
 * `Date` can hold — `isoOf` throws on one that is not, which would take the
 * whole screen down.
 */
export const MAX_RANGE_MS = 10 * 365 * DAY_MS;

/**
 * What a pan or a zoom is clamped to — the range, except under a preset that
 * has no span of its own.
 *
 * A preset IS a span of the clock, so its range is fixed and the window moves
 * inside it: that clamp is what stops an arrow key held down from walking off
 * the histogram the operator is reading. `Custom…` has no such span — its range
 * IS its window (above) — so the same clamp made a zoom-in final: the zoom-out,
 * the pan and the reset that should undo it each measured the window against a
 * range that had just shrunk with it, and every one of them was a no-op. There
 * the range follows the window instead, and a move is bounded only by
 * `MAX_RANGE_MS`, which is what makes a zoom-in reversible by the key that
 * reverses it.
 *
 * The bound is the window's own span to either side of it: wider than any one
 * move asks for (a zoom-out is 1.5×, an arrow key a tenth), so the move happens
 * exactly rather than being clamped to something near it.
 *
 * And it always CONTAINS the window, which is what the cap alone would not give:
 * a window hand-pinned in the URL can already be wider than `MAX_RANGE_MS`, and
 * bounds that excluded it would have had `brush.ts` answer every move with the
 * bounds themselves — a pan that panned nowhere, a zoom-out that narrowed, and
 * either way the operator's pinned range thrown away by a key that was asked to
 * move within it. Past the cap the bound is the window exactly: the moves that
 * would grow it do nothing, and a zoom-in — which is the way back under the cap
 * — still narrows by its own factor.
 */
export function moveBounds(scope: StreamScope): Span {
  if (scope.preset.spanMs !== null) return scope.range;
  const { startMs, endMs } = scope.window;
  const centre = (startMs + endMs) / 2;
  const reach = Math.min(endMs - startMs, MAX_RANGE_MS / 2);
  return union(scope.window, { startMs: centre - reach, endMs: centre + reach });
}

/**
 * The filters one window's reads carry — the list and the histogram take the
 * same ones, so the overview cannot count events the list below it is hiding.
 *
 * `since` is inclusive and `until` exclusive, which is how the API tiles
 * adjacent windows; the end instant is pushed out by a millisecond so an event
 * stamped exactly at the window's right edge — the newest event, in live mode,
 * every time — is inside the window the operator is looking at.
 */
export function streamFilters(query: StreamQuery, window: Span): AuditFilters {
  const prefixes = query.kinds.flatMap((kind) => [...KIND_PREFIXES[kind]]);
  return {
    since: isoOf(window.startMs),
    until: isoOf(window.endMs + 1),
    ...(query.epic !== null ? { epic: query.epic } : {}),
    ...(query.item !== null ? { item: query.item } : {}),
    ...(prefixes.length > 0 ? { event_prefix: prefixes } : {})
  };
}

/**
 * The filters the LIST read carries: the window's, except in live mode.
 *
 * Live mode is "what happened last", and `rowsFor` promises that an idle fleet
 * still shows its last events under one long quiet row. Bounding the live read
 * by the 30-minute strip broke that: a fleet quiet for longer than the strip
 * printed "no events yet" over a log full of events. So the live list asks for
 * the newest page with the epic/item/kind filters only — no `since`, no `until`
 * (which also keeps a future-skewed event inside `MAX_SKEW_MS` on the list).
 * The histogram and every non-live range keep the window's bounds.
 */
export function listFilters(query: StreamQuery, window: Span, live: boolean): AuditFilters {
  const filters = streamFilters(query, window);
  if (!live) return filters;
  const { since: _since, until: _until, ...unbounded } = filters;
  return unbounded;
}

/** A preset click: it sets the range, and resets the brush to all of it. */
export function applyPreset(query: StreamQuery, key: PresetKey, nowMs: number): StreamQuery {
  const preset = presetFor(key);
  // Custom has no span of its own, so it keeps the range it is standing on.
  if (preset.spanMs === null) {
    const { range } = resolveStreamScope(query, nowMs);
    return { ...query, preset: key, fromMs: range.startMs, toMs: range.endMs };
  }
  return { ...query, preset: key, fromMs: null, toMs: null };
}

/** The brush moved: the window is pinned, which is what pauses the poll. */
export function withWindow(query: StreamQuery, window: Span): StreamQuery {
  return { ...query, fromMs: window.startMs, toMs: window.endMs };
}

/** "Jump to now" / `L`: unpin the window, and return to the live preset. */
export function jumpToNow(query: StreamQuery): StreamQuery {
  return { ...query, preset: LIVE_PRESET, fromMs: null, toMs: null };
}

/** A kind toggle, kept in `STREAM_KINDS` order so the URL is stable. */
export function toggleKind(query: StreamQuery, kind: StreamKind): StreamQuery {
  const on = query.kinds.includes(kind);
  return {
    ...query,
    kinds: STREAM_KINDS.filter((k) => (k === kind ? !on : query.kinds.includes(k)))
  };
}
