/**
 * #2246 — the Work screen's keyboard, as a pure model.
 *
 * No Work, Monitor or Epic page handled an arrow key or a j/k (P9 of the UX
 * brief). The list is a 420-row master beside a detail pane, which is the one
 * shape where a mouse is the slow way to read: moving the selection is one
 * keystroke and the pane follows.
 *
 * Both halves are pure so they can be held to their promises without a DOM —
 * the suite here has no jsdom, and a keyboard handler written inline in a
 * `+page.svelte` is a handler nothing can test:
 *
 * * `workIntent` turns one key press into one named intent, or into nothing.
 * * `nextRow` moves the selection through the list the reader can actually see.
 *
 * The two rules that matter are both about NOT acting. A press carrying a
 * platform modifier belongs to the browser (⌘R, ^W, ⌥←) and is never claimed;
 * and a press while the reader is typing in a field belongs to the field, so
 * `j` goes into the search box rather than moving the selection out from under
 * it. Escape is the one exception, because leaving a field is the thing Escape
 * is for.
 *
 * Since #2512 the page reaches these through the console's key layer
 * (`$lib/keys/registry`), which applies both refusals before any surface
 * sees a press — so the one press that can still come from a field, Escape,
 * is read by the search field itself, through `workIntent` like every other.
 */
import { bindShortcuts, legendOf, type KeyDef, type KeyHint, type Shortcut } from '$lib/keys/registry';
import type { WorkRow } from '$lib/work/list';

/** What one press means. Named, so nothing branches on a raw key string. */
export type WorkIntent =
  /** Move the selection one row towards the top of the filtered list. */
  | 'up'
  /** … and one row towards the bottom. */
  | 'down'
  /** Move focus into the detail pane. */
  | 'open'
  /** Clear the selection, back to the needs-you summary. */
  | 'clear'
  /** Focus the search field. */
  | 'search'
  /** Copy the selected row's command. */
  | 'copy'
  /** Toggle the needs-you filter. */
  | 'filter';

/**
 * The part of a `KeyboardEvent` this reads, so a test can press a key without
 * a DOM and the page can pass the event itself.
 */
export type KeyPress = {
  key: string;
  metaKey?: boolean;
  ctrlKey?: boolean;
  altKey?: boolean;
  /**
   * True when the press landed in something the reader is typing into — an
   * input, a textarea, or anything `contenteditable`. The page decides this
   * from the event target; this module decides what to do about it.
   */
  typing?: boolean;
};

/**
 * The one table (#2512: it is also what the page registers with the console's
 * key layer, and what the legend is drawn from). Lowercase `key` values, so a
 * shifted letter matches nothing. In the legend's order; the arrows and `/`
 * are answered but not drawn — the artboard's strip has no room for them.
 */
export const WORK_KEYS: readonly (KeyDef & { intent: WorkIntent })[] = [
  { key: 'j', intent: 'down', description: 'move', legend: true },
  { key: 'k', intent: 'up', description: 'move', legend: true },
  { key: 'Enter', intent: 'open', description: 'open', legend: true },
  { key: 'Escape', intent: 'clear', description: 'clear', legend: true },
  { key: 'c', intent: 'copy', description: 'copy command', legend: true },
  { key: 'f', intent: 'filter', description: 'needs you', legend: true },
  { key: 'ArrowUp', intent: 'up', description: 'move up' },
  { key: 'ArrowDown', intent: 'down', description: 'move down' },
  { key: '/', intent: 'search', description: 'search' }
];

const INTENTS: Record<string, WorkIntent> = Object.fromEntries(
  WORK_KEYS.map((k) => [k.key, k.intent])
);

/**
 * One press as one intent, or `null` for a press this screen does not claim.
 *
 * Own-property lookup for the same reason `list.ts` uses one: the key comes off
 * an event, and `INTENTS['constructor']` would otherwise answer with
 * `Object.prototype`'s.
 */
export function workIntent(press: KeyPress): WorkIntent | null {
  if (press.metaKey === true || press.ctrlKey === true || press.altKey === true) return null;
  const intent = Object.hasOwn(INTENTS, press.key) ? INTENTS[press.key] : null;
  if (intent === null) return null;
  // Typing: the field keeps every key it can use, and Escape leaves it.
  if (press.typing === true) return intent === 'clear' ? 'clear' : null;
  return intent;
}

/**
 * The row one `up`/`down` lands on: the id to select, or `null` for an empty
 * list.
 *
 * Two promises, both of them about not surprising a reader whose eyes are on
 * the pane rather than the list.
 *
 * * **It wraps at neither end.** Holding `j` at the bottom of the list stays on
 *   the bottom row; it does not jump to the top, which would silently move the
 *   reader to a different item than the one they were walking towards.
 * * **It skips nothing.** `rows` is the FILTERED, searched list — the rows on
 *   screen — so one press is one row, and a row the filter hid is never landed
 *   on and never stepped over.
 *
 * With nothing selected, or with a selection the filtered list does not hold (a
 * `?item=` for a row the search just hid), the move enters the list from the end
 * it came from: `down` takes the first row, `up` the last.
 */
export function nextRow(
  rows: WorkRow[],
  current: string | null | undefined,
  direction: 'up' | 'down'
): string | null {
  if (rows.length === 0) return null;
  const at = rows.findIndex((r) => r.itemId === current);
  if (at < 0) return direction === 'down' ? rows[0].itemId : rows[rows.length - 1].itemId;
  const to = direction === 'down' ? at + 1 : at - 1;
  // No wrap: the end of the list is the end of the list.
  if (to < 0 || to >= rows.length) return rows[at].itemId;
  return rows[to].itemId;
}

/**
 * The width below which the Work page holds ONE column (#2281 D2, DESIGN.md
 * Responsive Behavior). The same 1024 the Tailwind `lg:` prefixes in the page
 * take, spelled once so the keyboard and the markup cannot drift apart.
 */
export const ONE_COLUMN_BELOW_W = 1024;

/**
 * The intents whose CONTROL lives inside the list column.
 *
 * Both of them sit in the list's own header strip: the search field and the
 * filter menu. Every other intent acts on the selection, the detail pane or
 * the clipboard, none of which the list column holds.
 */
export const LIST_COLUMN_INTENTS: ReadonlySet<WorkIntent> = new Set<WorkIntent>([
  'search',
  'filter'
]);

/**
 * Whether carrying out `intent` has to put the list back in the column first
 * (#2281 D2).
 *
 * Below 1024 the list and the detail take TURNS in one column, so an open item
 * puts the whole list — search field and filter menu included — inside a
 * `display:none` subtree. A handler that does not read the breakpoint then
 * aims those two keys at controls that are not in flow: `/` called `focus()`
 * on a hidden input, which is a no-op and the only press on this page that
 * answered a reader with nothing whatever, and `f` narrowed a list nobody
 * could see. Reaching for either control on a one-column screen means "back
 * to the list", so the press does that first.
 *
 * `width` is the measured viewport, and `0` — server-rendered, or not measured
 * yet — reads as the two-column desktop the page is designed at, so nothing is
 * dismissed on a guess.
 */
export function needsListColumn(intent: WorkIntent, width: number, selected: boolean): boolean {
  if (!selected || width <= 0 || width >= ONE_COLUMN_BELOW_W) return false;
  return LIST_COLUMN_INTENTS.has(intent);
}

export type { KeyHint };

/**
 * The legend, drawn from `WORK_KEYS` by the registry's `legendOf` — so the
 * keys a reader is told about and the keys the page registers are one list,
 * and `keys.test.ts` holds every hint to an intent.
 */
export const KEY_LEGEND: readonly KeyHint[] = legendOf(WORK_KEYS);

/**
 * The page's shortcuts for the key layer: every key in `WORK_KEYS`, each
 * carrying out the intent `workIntent` names for it. The registry has already
 * left modifier presses to the browser and typed ones to the field.
 */
export function workShortcuts(run: (intent: WorkIntent, event: KeyboardEvent) => void): Shortcut[] {
  return bindShortcuts('work', WORK_KEYS, (event) => {
    const intent = workIntent({ key: event.key });
    if (intent !== null) run(intent, event);
  });
}

/** How each legend key is drawn: `↵` and `esc` read better than their names. */
export const KEY_GLYPH: Record<string, string> = {
  Enter: '↵',
  Escape: 'esc',
  ArrowUp: '↑',
  ArrowDown: '↓'
};

/** The glyph for a legend key, or the key itself. */
export function keyGlyph(key: string): string {
  return Object.hasOwn(KEY_GLYPH, key) ? KEY_GLYPH[key] : key;
}
