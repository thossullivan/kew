/**
 * #2210 — the drain column's caption, read once for the whole list.
 *
 * `EpicSummaryDTO.label` is the drain FIGURE's caption ("drain spend" is its
 * server-side default, `kew/src/kew/service/models.py`), not the epic's name.
 * The list printed it beside every `epic_ref`, so a roster of sixty-six epics
 * read as sixty-six epics all called "drain spend" (owner review, 2026-09-29).
 */
import { describe, expect, it } from 'vitest';
import epicsJson from '../../../fixtures/factory/epics.json';
import { DRAIN_CAPTION, DRAIN_COLUMN, drainTitle } from '$lib/epic/drain';

/** Each row's value title, under the head's one title. */
const rowTitles = (labels: readonly string[]) =>
  labels.map((label) => drainTitle(DRAIN_CAPTION, label));

describe('the drain column heading', () => {
  it('prints one word and is titled with what the figure is (#2844 P9)', () => {
    expect(DRAIN_COLUMN).toBe('Spend');
    expect(DRAIN_CAPTION).toBe('drain spend');
  });

  it('keeps a row caption the heading could not carry, and only then', () => {
    expect(drainTitle(DRAIN_COLUMN, 'since last tick')).toBe('since last tick');
    expect(drainTitle('drain spend', 'drain spend')).toBeUndefined();
    expect(drainTitle(DRAIN_COLUMN, '')).toBeUndefined();
  });

  it('keeps every row of a uniform non-default roster captioned on its value', () => {
    // The head is "drain spend" whatever the rows say, so a roster that all
    // reads "since last tick" must still say so — on each value.
    expect(rowTitles(['since last tick', 'since last tick'])).toEqual([
      'since last tick',
      'since last tick'
    ]);
  });

  it('titles no value whose caption the head already says', () => {
    expect(rowTitles(['drain spend', '', '  '])).toEqual([undefined, undefined, undefined]);
  });

  it('captions only the artboard fixture rows that differ from the head', () => {
    // The fixture carries both captions; only the non-default ones get a title.
    const labels = epicsJson.map((e) => e.label);
    expect(new Set(labels).size).toBeGreaterThan(1);
    expect(rowTitles(labels)).toEqual(labels.map((l) => (l === DRAIN_CAPTION ? undefined : l)));
  });
});
