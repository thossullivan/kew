import { describe, expect, it } from 'vitest';
import { tickerLines } from './ticker';
import type { EpicTick } from '$lib/api';

const tick = (actions: EpicTick['actions']): EpicTick => ({
  id: 1,
  tick_ts: '2026-07-02T09:12:00.000Z',
  snapshot: { signal: null, counts: {}, nodes: [], edges: [], frontier: [] } as unknown as EpicTick['snapshot'],
  actions,
  economics: null
});

describe('tickerLines', () => {
  it('dispatched', () => {
    const lines = tickerLines(tick({ dispatched: ['514'] }));
    expect(lines).toEqual([{ icon: '▸', text: 'dispatched #514', ts: '2026-07-02T09:12:00.000Z' }]);
  });

  it('merged with approver', () => {
    const lines = tickerLines(tick({ merged: [{ id: '512', approved_by: 'alice' }] }));
    expect(lines[0]).toEqual({
      icon: '✓',
      text: 'merged #512 · approved by @alice',
      ts: '2026-07-02T09:12:00.000Z'
    });
  });

  it('merged with approved_by null omits the approver clause', () => {
    const lines = tickerLines(tick({ merged: [{ id: '512', approved_by: null }] }));
    expect(lines[0].text).toBe('merged #512');
  });

  /**
   * The reviewer's finding (PR #2438): `closed` — the out-of-band-merged set —
   * was written into the journal's `actions` column and read by nobody. This
   * is the ONE reader of that column, so the defect it was added for survived:
   * a tick whose only action was an out-of-band close painted its node DONE in
   * the snapshot while the ticker beside it showed no line at all.
   */
  it('reads the out-of-band close, and says kew did not do the merge', () => {
    const lines = tickerLines(tick({ closed: ['517'] }));
    expect(lines).toEqual([
      { icon: '✓', text: 'closed #517 · merged out of band', ts: '2026-07-02T09:12:00.000Z' }
    ]);
  });

  it('held', () => {
    const lines = tickerLines(tick({ held: ['508'] }));
    expect(lines[0]).toEqual({ icon: '⏸', text: 'held #508', ts: '2026-07-02T09:12:00.000Z' });
  });

  it('stuck carries the reason', () => {
    const lines = tickerLines(tick({ stuck: [{ id: '509', reason: 'merge-failed' }] }));
    expect(lines[0]).toEqual({ icon: '✗', text: 'stuck #509 · merge-failed', ts: '2026-07-02T09:12:00.000Z' });
  });

  it('orders merged -> closed -> dispatched -> held -> stuck', () => {
    const lines = tickerLines(
      tick({
        dispatched: ['514'],
        merged: [{ id: '512', approved_by: 'alice' }],
        closed: ['517'],
        held: ['508'],
        stuck: [{ id: '509', reason: 'merge-failed' }]
      })
    );
    expect(lines.map((l) => l.icon)).toEqual(['✓', '✓', '▸', '⏸', '✗']);
    expect(lines.map((l) => l.text.split(' ')[0])).toEqual([
      'merged',
      'closed',
      'dispatched',
      'held',
      'stuck'
    ]);
  });

  it('empty actions -> no lines', () => {
    expect(tickerLines(tick({}))).toEqual([]);
  });
});
