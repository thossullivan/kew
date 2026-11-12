/**
 * #2246 — the Work screen's keyboard (P9 of the UX brief: no Work, Monitor or
 * Epic page handled an arrow key or a j/k).
 *
 * Two things are held here, and both of them are about a keyboard taking a key
 * it had no business taking.
 *
 * **It declines the presses that are not its.** ⌘R must reload, ^W must close
 * the tab, and a `j` typed into the search box must be a `j` in the search box —
 * a list that stole it would move the selection out from under a reader who was
 * typing. Escape is the one key a field does not keep, because leaving a field
 * is what Escape is for.
 *
 * **It moves one row, and never wraps.** `nextRow` walks the FILTERED list, so
 * a hidden row is neither landed on nor stepped over, and holding `j` at the
 * bottom stays at the bottom rather than silently returning to the top of a
 * list the reader thought they were walking down.
 *
 * There is no jsdom in this suite, which is exactly why both halves are pure:
 * a handler written inline in a `+page.svelte` is a handler nothing can press.
 */
import { describe, expect, it } from 'vitest';
import {
  KEY_GLYPH,
  KEY_LEGEND,
  keyGlyph,
  LIST_COLUMN_INTENTS,
  needsListColumn,
  nextRow,
  ONE_COLUMN_BELOW_W,
  workIntent,
  type WorkIntent
} from './keys';
import type { WorkRow } from './list';

const row = (itemId: string, needsYou = false): WorkRow => ({
  itemId,
  number: itemId,
  ref: `#${itemId}`,
  title: `item ${itemId}`,
  state: 'idle',
  needsYou,
  view: null,
  tier: null,
  entry: null,
  attention: null,
  href: `/work?item=${itemId}`,
  external: false
});

const ROWS = [row('2060'), row('2055', true), row('2047'), row('1803')];

describe('workIntent', () => {
  it('names the nine presses the screen answers to', () => {
    const table: [string, WorkIntent][] = [
      ['ArrowUp', 'up'],
      ['k', 'up'],
      ['ArrowDown', 'down'],
      ['j', 'down'],
      ['Enter', 'open'],
      ['Escape', 'clear'],
      ['/', 'search'],
      ['c', 'copy'],
      ['f', 'filter']
    ];
    for (const [key, intent] of table) expect(workIntent({ key }), key).toBe(intent);
  });

  it('claims nothing else, including a key that names a prototype member', () => {
    for (const key of ['a', 'J', 'K', 'C', 'F', '?', 'Tab', ' ', 'constructor', '']) {
      expect(workIntent({ key }), key).toBeNull();
    }
  });

  it('leaves a press carrying a platform modifier to the browser', () => {
    // ⌘R, ^W, ⌥↓ — none of them is this screen's to take.
    expect(workIntent({ key: 'j', metaKey: true })).toBeNull();
    expect(workIntent({ key: 'f', ctrlKey: true })).toBeNull();
    expect(workIntent({ key: 'ArrowDown', altKey: true })).toBeNull();
    expect(workIntent({ key: 'Escape', metaKey: true })).toBeNull();
  });

  it('leaves every key but Escape to the field a reader is typing in', () => {
    for (const key of ['j', 'k', 'c', 'f', '/', 'Enter', 'ArrowDown']) {
      expect(workIntent({ key, typing: true }), key).toBeNull();
    }
    expect(workIntent({ key: 'Escape', typing: true })).toBe('clear');
  });
});

describe('nextRow', () => {
  it('moves exactly one row, skipping nothing in the list it was given', () => {
    const walked: string[] = [];
    let at: string | null = nextRow(ROWS, null, 'down');
    while (at !== null) {
      walked.push(at);
      const to = nextRow(ROWS, at, 'down');
      if (to === at) break;
      at = to;
    }
    expect(walked).toEqual(ROWS.map((r) => r.itemId));
  });

  it('wraps at neither end', () => {
    const last = ROWS[ROWS.length - 1].itemId;
    expect(nextRow(ROWS, last, 'down')).toBe(last);
    expect(nextRow(ROWS, ROWS[0].itemId, 'up')).toBe(ROWS[0].itemId);
  });

  it('enters the list from the end the move came from', () => {
    expect(nextRow(ROWS, null, 'down')).toBe('2060');
    expect(nextRow(ROWS, null, 'up')).toBe('1803');
    expect(nextRow(ROWS, '', 'down')).toBe('2060');
    // A `?item=` for a row the filter or the search just hid is the same case:
    // the move enters the visible list rather than refusing to happen.
    expect(nextRow(ROWS, '9999', 'up')).toBe('1803');
  });

  it('walks the filtered list, which is the list on screen', () => {
    const flagged = ROWS.filter((r) => r.needsYou);
    expect(nextRow(flagged, null, 'down')).toBe('2055');
    expect(nextRow(flagged, '2055', 'down')).toBe('2055');
    // The rows the filter hid are never landed on.
    expect(nextRow(flagged, '2060', 'down')).toBe('2055');
  });

  it('answers null for an empty list rather than an id nothing holds', () => {
    expect(nextRow([], null, 'down')).toBeNull();
    expect(nextRow([], '2055', 'up')).toBeNull();
  });
});

describe('KEY_LEGEND', () => {
  it('promises only keys `workIntent` actually answers', () => {
    expect(KEY_LEGEND.length).toBeGreaterThan(0);
    for (const hint of KEY_LEGEND) {
      expect(hint.keys.length).toBeGreaterThan(0);
      for (const key of hint.keys) {
        expect(workIntent({ key }), `${key} (${hint.label})`).not.toBeNull();
      }
    }
  });

  it('draws a key the way a reader reads it, never as its event name', () => {
    expect(keyGlyph('Enter')).toBe('↵');
    expect(keyGlyph('Escape')).toBe('esc');
    expect(keyGlyph('j')).toBe('j');
    expect(keyGlyph('constructor')).toBe('constructor');
    for (const key of KEY_LEGEND.flatMap((h) => h.keys)) {
      expect(keyGlyph(key), key).not.toBe('');
    }
    // Every glyph the table carries is for a key something answers to.
    for (const key of Object.keys(KEY_GLYPH)) {
      expect(workIntent({ key }), key).not.toBeNull();
    }
  });
});

/**
 * #2281 D2 — the keyboard on a screen that holds ONE column.
 *
 * Below 1024 the list and the detail take turns, so an open item puts the whole
 * list — search field and filter menu with it — inside a `display:none`
 * subtree. The handler read no breakpoint, so `/` called `focus()` on an input
 * that was not in flow (a no-op: the only press on the page that answered with
 * nothing at all) and `f` narrowed a list nobody could see. Both are reaches
 * for a control in the list column, and on a one-column screen that means
 * "back to the list".
 */
describe('the list column, below 1024', () => {
  const BELOW = ONE_COLUMN_BELOW_W - 1;

  it('names the two intents whose control is in the list column', () => {
    expect([...LIST_COLUMN_INTENTS].sort()).toEqual(['filter', 'search']);
    // Every one of them is an intent `workIntent` can actually return.
    const reachable = new Set(
      ['ArrowUp', 'ArrowDown', 'j', 'k', 'Enter', 'Escape', '/', 'c', 'f'].map((key) =>
        workIntent({ key })
      )
    );
    for (const intent of LIST_COLUMN_INTENTS) expect(reachable.has(intent), intent).toBe(true);
  });

  it('brings the list back for `/` and `f` while an item is open', () => {
    expect(needsListColumn('search', BELOW, true)).toBe(true);
    expect(needsListColumn('filter', BELOW, true)).toBe(true);
  });

  it('leaves the selection alone at every width from 1024 up', () => {
    for (const width of [ONE_COLUMN_BELOW_W, 1280, 1440]) {
      expect(needsListColumn('search', width, true), String(width)).toBe(false);
      expect(needsListColumn('filter', width, true), String(width)).toBe(false);
    }
  });

  it('leaves the selection alone with nothing selected — the list is already there', () => {
    expect(needsListColumn('search', BELOW, false)).toBe(false);
    expect(needsListColumn('filter', BELOW, false)).toBe(false);
  });

  it('reads an unmeasured width as the desktop, never dismissing on a guess', () => {
    // 0 is the server render and the first frame. Clearing a reader's item
    // because the window has not been measured yet would be the worse failure.
    expect(needsListColumn('search', 0, true)).toBe(false);
    expect(needsListColumn('filter', -1, true)).toBe(false);
  });

  it('never touches the selection for an intent that is not a list control', () => {
    // `up`/`down` write the selection, `open` and `copy` act on the detail pane
    // which IS the column below 1024, and `clear` already empties it.
    for (const intent of ['up', 'down', 'open', 'clear', 'copy'] as WorkIntent[]) {
      expect(needsListColumn(intent, BELOW, true), intent).toBe(false);
    }
  });

  it('is the same 1024 the markup takes its `lg:` prefixes at', () => {
    expect(ONE_COLUMN_BELOW_W).toBe(1024);
  });
});
