/**
 * #2516 — what the Cmd-K palette offers, and in what order.
 *
 * Four kinds of place, each from a read the console already makes:
 *
 * * **pages** — every route in `$lib/shell/nav` (the tabs and the More menu),
 *   so the palette can never know of a page the header does not, or miss one
 *   it does;
 * * **epics** — `/api/epics`, linked by `epicHref`;
 * * **items** — the Work list's own merge (`mergeWorkList`), so an item the
 *   palette finds is the row `/work?item=` selects, by the same id;
 * * **runs** — the newest of `/api/runs`, which the shared pulse already holds.
 *
 * Every id stays opaque: it is matched as text and encoded into the link by
 * the helper that owns that link, never parsed. Navigation only — an entry
 * carries an href and nothing that writes.
 */
import type { Dashboard, EpicSummary, Run, WorkProjection } from '$lib/api';
import { runHref } from '$lib/flight-recorder';
import { plainTitle } from '$lib/factory/names';
import { FACTORY_TABS, MORE_DESTINATIONS } from '$lib/shell/nav';
import { mergeWorkList } from '$lib/work/list';
import { epicHref } from '$lib/work/refs';
import type { PaletteCommand } from './actions';

export type PaletteKind = 'page' | 'item' | 'epic' | 'run' | 'action';

export type PaletteEntry = {
  kind: PaletteKind;
  /** Unique across the whole list — the `{#each}` key. */
  key: string;
  /** What the reader types to find it: the item number, epic ref, run id or
   * page name. */
  id: string;
  /** The words beside the id. Empty when no payload names one. */
  title: string;
  /** Extra text that matches but is not drawn as the id (a run's item number). */
  aliases: readonly string[];
  /** In-app, always — empty for an action, which goes nowhere. */
  href: string;
  /** An item entry's durable id, whose actions → lists (#2517). */
  itemId?: string;
  /** What an action entry does instead of going somewhere ($lib/palette/actions). */
  command?: PaletteCommand;
};

/** Everything the palette is built from. Every field may be missing alone. */
export type PaletteInput = {
  epics: EpicSummary[] | null;
  projection: WorkProjection | null;
  dashboard: Dashboard | null;
  runs: Run[] | null;
};

/** How many runs are "recent": the newest, by start. */
export const RECENT_RUNS = 50;

/** How many results the list draws. The ranking is over every entry. */
export const MAX_RESULTS = 50;

/** Every nav route, tabs first, in the header's own order. */
export function pageEntries(): PaletteEntry[] {
  return [...FACTORY_TABS, ...MORE_DESTINATIONS].map((d) => ({
    kind: 'page',
    key: `page:${d.href}`,
    id: d.label,
    title: d.href,
    aliases: [],
    href: d.href
  }));
}

export function epicEntries(epics: EpicSummary[] | null): PaletteEntry[] {
  return (epics ?? []).map((e) => ({
    kind: 'epic',
    key: `epic:${e.epic_ref}`,
    id: e.epic_ref,
    title: e.label === e.epic_ref ? '' : e.label,
    aliases: [],
    href: epicHref(e.epic_ref)
  }));
}

export function itemEntries(input: Omit<PaletteInput, 'epics'>): PaletteEntry[] {
  return mergeWorkList(input)
    .filter((row) => !row.external)
    .map((row) => ({
      kind: 'item',
      key: `item:${row.itemId}`,
      id: row.number,
      title: row.title,
      aliases: [row.ref],
      href: row.href,
      itemId: row.itemId
    }));
}

/** Newest first by start; a run never started sorts last. */
export function runEntries(runs: Run[] | null, limit = RECENT_RUNS): PaletteEntry[] {
  const at = (r: Run) => (r.started_at ? Date.parse(r.started_at) || 0 : 0);
  return [...(runs ?? [])]
    .sort((a, b) => at(b) - at(a))
    .slice(0, limit)
    .map((r) => ({
      kind: 'run',
      key: `run:${r.run_id}`,
      id: r.run_id,
      title: r.title,
      aliases: r.issue_num > 0 ? [String(r.issue_num), `#${r.issue_num}`] : [],
      href: runHref(r.run_id)
    }));
}

/** Pages, then items, epics and runs — also the order a tie is broken in. */
export function paletteEntries(input: PaletteInput): PaletteEntry[] {
  return [
    ...pageEntries(),
    ...itemEntries(input),
    ...epicEntries(input.epics),
    ...runEntries(input.runs)
  ];
}

/** Text as it is compared: trimmed, lower-cased, a leading `#` dropped — so
 * `#2055`, `2055` and ` 2055 ` are one item number, whichever side wrote it. */
function normal(text: string): string {
  return text.trim().toLowerCase().replace(/^#/, '');
}

/** A query of digits alone is an item number, and an item number is matched
 * as text — never as digits scattered in order through some other id. */
const NUMBER_QUERY = /^[0-9]+$/;

/**
 * How well `text` answers `query` (both normalized), or 0 for not at all:
 * exact, then one of its words exactly (`2055` in `gh:2055`), then prefix,
 * then a word starting with it, then anywhere, then — only when `fuzzy` — the
 * query's letters in order (the fuzzy tail, scored by how tightly they sit).
 */
function textScore(text: string, query: string, fuzzy: boolean): number {
  if (text === '' || query === '') return 0;
  if (text === query) return 1000;
  if (text.split(/[^a-z0-9]+/).includes(query)) return 900;
  if (text.startsWith(query)) return 800 - Math.min(text.length - query.length, 100);
  const at = text.indexOf(query);
  if (at > 0 && /[^a-z0-9]/.test(text[at - 1])) return 500;
  if (at > 0) return 400;
  if (!fuzzy) return 0;
  let from = 0;
  let first = -1;
  for (const ch of query) {
    const found = text.indexOf(ch, from);
    if (found < 0) return 0;
    if (first < 0) first = found;
    from = found + 1;
  }
  const spread = from - first - query.length;
  return Math.max(1, 100 - spread);
}

/**
 * The entry's score for a query, 0 when it does not match.
 *
 * The id outranks the title at every tier, so typing `2055` puts item 2055
 * above a run whose title happens to mention it. A run's item number is an
 * alias — somebody else's id — and scores between an id and a title: above a
 * run that merely mentions 2055, below item 2055 itself (`2055` or `#2055`,
 * either side) and below any id that so much as starts with it.
 *
 * The fuzzy tail is for words (`hlth` → Health). An item-number query never
 * takes it, and a run's id never does: a run id is a UUID, and four digits sit
 * in order somewhere in most of them.
 */
export function scoreEntry(entry: PaletteEntry, query: string): number {
  const q = normal(query);
  if (q === '') return 1;
  const fuzzy = !NUMBER_QUERY.test(q);
  const id = textScore(normal(entry.id), q, fuzzy && entry.kind !== 'run');
  const alias = Math.max(0, ...entry.aliases.map((a) => textScore(normal(a), q, fuzzy)));
  const title = textScore(plainTitle(entry.title).toLowerCase(), q, fuzzy);
  return Math.max(id * 3, alias > 0 ? alias * 2 + 1 : 0, title * 2);
}

/** The entries that match, best first; equal scores keep the entry order. */
export function rankEntries(
  entries: readonly PaletteEntry[],
  query: string,
  limit = MAX_RESULTS
): PaletteEntry[] {
  return entries
    .map((entry, index) => ({ entry, index, score: scoreEntry(entry, query) }))
    .filter((r) => r.score > 0)
    .sort((a, b) => b.score - a.score || a.index - b.index)
    .slice(0, limit)
    .map((r) => r.entry);
}

/** What a key does in the palette's field. */
export type PaletteAction =
  | { kind: 'move'; cursor: number }
  | { kind: 'go'; href: string }
  | { kind: 'command'; command: PaletteCommand }
  | { kind: 'actions'; itemId: string }
  | { kind: 'close' };

/**
 * The palette's keys: ↑/↓ move the cursor (wrapping), Enter goes to the entry
 * under it — or, on an action, runs it (through its confirm step) — → on an
 * item lists that item's actions, Esc closes. Anything else is the field's
 * (`null`), → included everywhere but on an item, where the caret is.
 */
export function paletteKey(
  key: string,
  cursor: number,
  results: readonly PaletteEntry[]
): PaletteAction | null {
  const n = results.length;
  switch (key) {
    case 'ArrowDown':
      return { kind: 'move', cursor: n === 0 ? 0 : (cursor + 1) % n };
    case 'ArrowUp':
      return { kind: 'move', cursor: n === 0 ? 0 : (cursor - 1 + n) % n };
    case 'Enter': {
      const entry = results[Math.min(Math.max(cursor, 0), n - 1)];
      if (!entry) return null;
      return entry.command ? { kind: 'command', command: entry.command } : { kind: 'go', href: entry.href };
    }
    case 'ArrowRight': {
      const entry = results[Math.min(Math.max(cursor, 0), n - 1)];
      return entry?.itemId ? { kind: 'actions', itemId: entry.itemId } : null;
    }
    case 'Escape':
      return { kind: 'close' };
    default:
      return null;
  }
}

export const KIND_LABEL: Record<PaletteKind, string> = {
  page: 'page',
  item: 'item',
  epic: 'epic',
  run: 'run',
  action: 'do'
};
