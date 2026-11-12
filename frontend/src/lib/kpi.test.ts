import { describe, expect, it } from 'vitest';
import { runKpis, displayStatus, TERMINAL_STATUSES } from './kpi';

describe('runKpis', () => {
  it('renders — (successRate null) when the only run is in flight', () => {
    // A stranger's first run: one pending run, nothing terminal yet.
    const k = runKpis([{ status: 'pending' }]);
    expect(k).toEqual({ total: 1, terminal: 0, ok: 0, successRate: null, live: 1 });
  });

  it('counts only terminal runs in the denominator', () => {
    // 1 success + 1 failure terminal, 1 pending in flight.
    const k = runKpis([{ status: 'success' }, { status: 'failure' }, { status: 'pending' }]);
    expect(k.total).toBe(3);
    expect(k.terminal).toBe(2);
    expect(k.ok).toBe(1);
    expect(k.successRate).toBe(50); // 1/2 terminal, NOT 1/3
    expect(k.live).toBe(1);
  });

  it('treats budget_* and killed as terminal failures', () => {
    const k = runKpis([
      { status: 'success' },
      { status: 'budget_exceeded' },
      { status: 'budget_stopped' },
      { status: 'killed' }
    ]);
    expect(k.terminal).toBe(4);
    expect(k.ok).toBe(1);
    expect(k.successRate).toBe(25);
    expect(k.live).toBe(0);
  });

  it('is empty/zero-safe with no runs', () => {
    const k = runKpis([]);
    expect(k).toEqual({ total: 0, terminal: 0, ok: 0, successRate: null, live: 0 });
  });

  it('exposes the shared terminal status set, refusals and budget stops included', () => {
    expect([...TERMINAL_STATUSES].sort()).toEqual(
      [
        'budget_exceeded',
        'budget_stop',
        'budget_stopped',
        'failure',
        'killed',
        'refused',
        'success'
      ].sort()
    );
  });

  it('counts live runs as the pending ones, not as everything non-terminal', () => {
    const k = runKpis([
      { status: 'pending' },
      { status: 'pending' },
      { status: 'awaiting_publication' },
      { status: 'refused' },
      { status: 'budget_stop' },
      { status: 'success' },
      { status: 'a_status_from_a_newer_host' }
    ]);

    expect(k.live).toBe(2);
    // awaiting_publication is settled but unjudged, so it is in neither the
    // live count nor the success-rate denominator.
    expect(k.terminal).toBe(3);
    expect(k.successRate).toBe(33);
  });
});

describe('displayStatus', () => {
  it('shows "running" for a pending run with steps of activity', () => {
    expect(displayStatus({ status: 'pending', steps: 16 })).toBe('running');
  });

  it('shows "running" for a pending run that has a started timestamp', () => {
    expect(displayStatus({ status: 'pending', startedAt: '2026-07-15T10:00:00Z' })).toBe('running');
  });

  it('shows "queued" for a fresh, untouched pending run', () => {
    expect(displayStatus({ status: 'pending' })).toBe('queued');
    expect(displayStatus({ status: 'pending', steps: 0, startedAt: null })).toBe('queued');
  });

  it('passes terminal statuses through unchanged', () => {
    expect(displayStatus({ status: 'success' })).toBe('success');
    expect(displayStatus({ status: 'failure' })).toBe('failure');
    expect(displayStatus({ status: 'budget_exceeded' })).toBe('budget_exceeded');
    expect(displayStatus({ status: 'killed', startedAt: '2026-07-15T10:00:00Z' })).toBe('killed');
  });
});
