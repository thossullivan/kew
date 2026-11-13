/**
 * #3458 — the run page's events read as sentences, never as JSON.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import type { RunAuditEvent } from '$lib/api';
import { eventTone, isErrorEvent, isHoldEvent, runEventLine, stageOf } from './event-words';
import {
  bandPresses,
  canClearRunFilter,
  clearRunFilter,
  filterRunEvents,
  parseRunFilter,
  placeLaneLabels,
  runFilterQuery,
  runPhases,
  runPresets,
  runStages,
  stageSelected,
  toggleStage
} from './events';

const FRONTEND = fileURLToPath(new URL('../../..', import.meta.url));

/** Every run ledger the fixtures and the demo world serve. */
function ledgers(): { file: string; events: RunAuditEvent[] }[] {
  const files = [
    join(FRONTEND, 'fixtures/factory/runs-f5dcb6a5-audit.json'),
    ...readdirSync(join(FRONTEND, 'demo/world'))
      .filter((name) => /^runs-.*-audit\.json$/.test(name))
      .map((name) => join(FRONTEND, 'demo/world', name))
  ];
  return files.map((file) => ({
    file,
    events: (JSON.parse(readFileSync(file, 'utf8')) as { events: RunAuditEvent[] }).events
  }));
}

const ev = (event: string, payload: Record<string, unknown> | null = null): RunAuditEvent => ({
  seq: 1,
  ts: '2026-09-24T15:23:53+00:00',
  event,
  actor_type: 'policy',
  item_id: '2055',
  payload
});

describe('every event the fixtures and the demo world carry', () => {
  const all = ledgers();

  it('reads them from more than one ledger', () => {
    expect(all.length).toBeGreaterThan(5);
  });

  it('gets a label and a summary in words, never JSON', () => {
    for (const { file, events } of all) {
      for (const event of events) {
        const line = runEventLine(event);
        const where = `${file} seq ${event.seq} (${event.event})`;
        expect(line.label, where).not.toBe('');
        expect(line.summary, where).not.toBe('');
        expect(line.summary.startsWith('{'), where).toBe(false);
        expect(line.summary, where).not.toMatch(/[{}"]/);
      }
    }
  });

  it('opens a named stage on the timeline', () => {
    for (const { events } of all) {
      for (const stage of runStages(runPhases(events, 0))) expect(stage.label).not.toBe('');
    }
  });
});

describe('the sentences', () => {
  it('says what the fixture run did, in its own recorded words', () => {
    expect(runEventLine(ev('routing_decision', { recommended_runner: 'claude-code', applied: false })).summary).toBe(
      'claude-code recommended, not applied'
    );
    expect(
      runEventLine(ev('publication_deferred', { category: 'push', step: 'push', error: 'protected branch; base merge required' }))
        .summary
    ).toBe('push deferred: protected branch; base merge required');
    expect(runEventLine(ev('publication_held', { reason: 'base merge required before publication' })).summary).toBe(
      'held: base merge required before publication'
    );
    expect(runEventLine(ev('dispatch_completed', { status: 'success', cost_usd: 3.1 })).summary).toBe('success · $3.10');
    expect(runEventLine(ev('publication_output_scan', { findings: 0, attempt_number: 1 })).summary).toBe('no findings');
    expect(runEventLine(ev('adversarial_review_verdict', { verdict: 'READY', round: 1 })).summary).toBe('round 1: READY');
  });

  it('falls back to the label for a kind it has no sentence for, never to JSON', () => {
    const line = runEventLine(ev('brand_new_event', { nested: { a: 1 }, count: 3 }));
    // Worded the way the Monitor words a kind its table has not met.
    expect(line.label).toBe('Brand new event');
    expect(line.summary).toBe('');
  });

  it('reads a completion the way the backend records it', () => {
    // dispatch_core.py: error_category, reason, exit_code, cost_usd; no status.
    const failed = ev('dispatch_completed', {
      error_category: 'sandbox_failure',
      reason: 'container exited 137',
      exit_code: 1,
      cost_usd: 0.0
    });
    expect(runEventLine(failed)).toEqual({
      label: 'Dispatch completed',
      summary: 'failed · sandbox failure: container exited 137 · $0.00',
      tone: 'failed'
    });
    // A category the shared table words reads in its words, not its code.
    const known = runEventLine(ev('dispatch_completed', { error_category: 'sandbox_launch_error', exit_code: 1 }));
    expect(known.summary).toBe('failed · Sandbox launch failed');
    expect(known.summary).not.toContain('sandbox_launch_error');
    const ok = ev('dispatch_completed', { error_category: '', reason: null, exit_code: 0, cost_usd: 1.234567 });
    expect(runEventLine(ok)).toEqual({ label: 'Dispatch completed', summary: 'finished · $1.23', tone: 'plain' });
  });

  it('reads the fixture and the demo world completions, which carry a status', () => {
    expect(runEventLine(ev('dispatch_completed', { status: 'success', cost_usd: 3.1 })).summary).toBe('success · $3.10');
    const demo = runEventLine(ev('dispatch_completed', { status: 'failure', error_category: 'host_restart' }));
    expect(demo.summary).toBe('failed · host restart');
    expect(demo.tone).toBe('failed');
  });

  it('leaves out only an error or a reason that is itself JSON, and keeps the rest of the sentence', () => {
    const json = runEventLine(ev('publication_deferred', { step: 'push', error: '{"code":409,"why":"protected"}' }));
    expect(json.summary).toBe('push deferred');
    expect(runEventLine(ev('brand_new_event', { reason: '["a","b"]' })).summary).toBe('');
    expect(
      runEventLine(ev('dispatch_completed', { error_category: 'permanent', reason: ' {"a":1}', exit_code: 1 })).summary
    ).toBe('failed · Permanent failure');
    // A value that only looks like JSON is prose, and the row prints it.
    expect(
      runEventLine(ev('dispatch_completed', { error_category: 'permanent', reason: '[sandbox] container exited 137', exit_code: 1 }))
        .summary
    ).toBe('failed · Permanent failure: [sandbox] container exited 137');
    expect(runEventLine(ev('brand_new_event', { reason: '{not json' })).summary).toBe('{not json');
    expect(runEventLine(ev('brand_new_event', { reason: 'saw {a} twice' })).summary).toBe('saw {a} twice');
  });

  it('reads a non-zero exit with no category as a failure', () => {
    // dispatch_core.py falls back to the exit code for the status and sends
    // error_category "" on this path.
    const exited = ev('dispatch_completed', { error_category: '', reason: 'max_turns', exit_code: 1, cost_usd: 2 });
    expect(isErrorEvent(exited)).toBe(true);
    expect(runEventLine(exited)).toEqual({
      label: 'Dispatch completed',
      summary: 'failed · exited 1: max_turns · $2.00',
      tone: 'failed'
    });
    expect(runEventLine(ev('dispatch_completed', { error_category: '', exit_code: 2 })).summary).toBe('failed · exited 2');
    // Exit 0 is not a failure, and only a completion's exit code is read.
    expect(isErrorEvent(ev('dispatch_completed', { error_category: '', exit_code: 0 }))).toBe(false);
    expect(isErrorEvent(ev('brand_new_event', { exit_code: 1 }))).toBe(false);
  });

  it('does not repeat a category its early exit records again as the reason', () => {
    // _emit_early_exit: reason = reason or admission_reason or error_category.
    const early = runEventLine(ev('dispatch_completed', { error_category: 'github_auth', reason: 'github_auth', exit_code: 1 }));
    expect(early.summary).toBe('failed · GitHub authentication failed');
    expect(early.summary).not.toContain('github_auth');
  });

  it('still reads a named key on a kind it has no sentence for', () => {
    expect(runEventLine(ev('brand_new_event', { reason: 'budget exceeded' })).summary).toBe('budget exceeded');
    expect(runEventLine(ev('brand_new_event', { status: 'ok' })).summary).toBe('status ok');
  });

  it('says nothing past its label for a known kind with an empty payload', () => {
    expect(runEventLine(ev('routing_decision', null)).summary).toBe('');
    expect(runEventLine(ev('constructor', null)).label).toBe('Constructor');
  });
});

describe('a hold is not a failure', () => {
  it('reads a deferral as held, even when it records the error that held it', () => {
    const deferred = ev('publication_deferred', { error: 'protected branch' });
    expect(isHoldEvent(deferred)).toBe(true);
    expect(isErrorEvent(deferred)).toBe(false);
    expect(eventTone(deferred)).toBe('held');
    expect(eventTone(ev('publication_held', { reason: 'x' }))).toBe('held');
  });

  it('still reads a failure as failed', () => {
    expect(eventTone(ev('sandbox_execution_failed'))).toBe('failed');
    expect(eventTone(ev('dispatch_completed', { status: 'failure', error: 'boom' }))).toBe('failed');
    expect(eventTone(ev('dispatch_completed', { status: 'success' }))).toBe('plain');
  });

  it('matches hold words whole, so a threshold or an unhold is not a hold', () => {
    const breached = ev('threshold_breached', { error: 'over budget' });
    expect(isHoldEvent(breached)).toBe(false);
    expect(eventTone(breached)).toBe('failed');
    expect(isHoldEvent(ev('epic_node_unhold'))).toBe(false);
    expect(eventTone(ev('epic_node_unhold'))).toBe('plain');
    expect(isHoldEvent(ev('epic_node_held'))).toBe(true);
    expect(isHoldEvent(ev('publication.deferred'))).toBe(true);
    expect(isHoldEvent(ev('approval_hold'))).toBe(true);
  });

  it('offers no Errors preset for a successful run whose only blemish is a deferral', () => {
    const fixture = ledgers()[0].events;
    const errors = runPresets(fixture, runPhases(fixture, 0)).find((preset) => preset.id === 'errors');
    expect(errors?.available).toBe(false);
  });
});

describe('the named stages', () => {
  it('names the fixture run dispatch, sandbox, work, seal, publish, done', () => {
    const fixture = ledgers()[0].events;
    expect(runStages(runPhases(fixture, 0)).map((stage) => stage.label)).toEqual([
      'dispatch',
      'sandbox',
      'work',
      'seal',
      'publish',
      'done'
    ]);
  });

  it('marks the publish stage held and nothing failed', () => {
    const stages = runStages(runPhases(ledgers()[0].events, 0));
    expect(stages.find((stage) => stage.label === 'publish')?.held).toBe(true);
    expect(stages.some((stage) => stage.failed)).toBe(false);
  });

  it('names a kind it has no stage for in words, never by its code', () => {
    expect(stageOf('routing_decision')).toBe('dispatch');
    expect(stageOf('brand_new_event')).toBe('Brand new event');
    expect(stageOf('run_started')).toBe('Run started');
  });

  it('gives each preset the count of the rows it shows, and selects whole stages', () => {
    const fixture = ledgers()[0].events;
    const phases = runPhases(fixture, 0);
    const stages = runStages(phases);
    for (const preset of runPresets(fixture, phases).filter((p) => p.available)) {
      expect(preset.count, preset.id).toBe(filterRunEvents(fixture, preset.filter, phases).length);
      for (const stage of stages) {
        const some = stage.phases.some((name) => preset.filter.phases.includes(name));
        // A stage the preset touches is selected whole, so the lane draws it pressed.
        if (some) expect(stageSelected(preset.filter, stage), `${preset.id} ${stage.label}`).toBe(true);
      }
    }
    const slowest = runPresets(fixture, phases).find((p) => p.id === 'slowest');
    // work, seal and publish: the publish stage's four rows (1046-1049) with them.
    expect(filterRunEvents(fixture, slowest!.filter, phases).map((e) => e.seq)).toEqual([
      1044, 1045, 1046, 1047, 1048, 1049
    ]);
    expect(slowest?.count).toBe(6);
  });

  it('counts and shows only the failed events under Errors, when a kind also succeeded', () => {
    const events: RunAuditEvent[] = [
      { ...ev('dispatch_completed', { error_category: 'sandbox_failure' }), seq: 1 },
      { ...ev('routing_decision', { recommended_runner: 'codex' }), seq: 2 },
      { ...ev('dispatch_completed', { error_category: '' }), seq: 3 },
      // A non-zero exit with no category is a failure too.
      { ...ev('dispatch_completed', { error_category: '', reason: 'max_turns', exit_code: 1 }), seq: 4 },
      { ...ev('dispatch_completed', { error_category: '', exit_code: 0 }), seq: 5 }
    ];
    const phases = runPhases(events, 0);
    const errors = runPresets(events, phases).find((p) => p.id === 'errors');
    expect(errors?.available).toBe(true);
    expect(errors?.count).toBe(2);
    expect(filterRunEvents(events, errors!.filter, phases).map((e) => e.seq)).toEqual([1, 4]);
    // The narrowing travels in the URL with the rest of the filter.
    expect(parseRunFilter(runFilterQuery(errors!.filter))).toEqual(errors!.filter);
    // And "clear" drops it, like the kinds it came with.
    expect(canClearRunFilter(parseRunFilter('is:error'))).toBe(true);
    expect(clearRunFilter(parseRunFilter('phase:a is:error'))).toEqual({ phases: ['a'], types: [], text: '' });
  });

  it('selects every phase a stage holds, and clears it on a second click', () => {
    const [dispatch] = runStages(runPhases(ledgers()[0].events, 0));
    const once = toggleStage({ phases: [], types: [], text: '' }, dispatch);
    expect(once.phases).toEqual(['dispatch_owner_recorded', 'routing_decision']);
    expect(toggleStage(once, dispatch).phases).toEqual([]);
    const other = { phases: ['sandbox_resolved'], types: [], text: '' };
    expect(toggleStage(other, dispatch, true).phases).toEqual([
      'sandbox_resolved',
      'dispatch_owner_recorded',
      'routing_decision'
    ]);
    expect(toggleStage(toggleStage(other, dispatch, true), dispatch, true).phases).toEqual(['sandbox_resolved']);
  });
});

describe('the stage names under the lane', () => {
  it('never overlap and never leave the plot', () => {
    const items = [
      { x: 0, width: 53 },
      { x: 1, width: 46 },
      { x: 36, width: 26 },
      { x: 150, width: 26 },
      { x: 270, width: 46 },
      { x: 357, width: 26 }
    ];
    const places = placeLaneLabels(items, 358);
    for (let i = 0; i < places.length; i += 1) {
      expect(places[i].x).toBeGreaterThanOrEqual(0);
      expect(places[i].x + items[i].width).toBeLessThanOrEqual(358);
      for (let j = 0; j < i; j += 1) {
        if (places[i].row !== places[j].row) continue;
        const apart = places[i].x >= places[j].x + items[j].width || places[j].x >= places[i].x + items[i].width;
        expect(apart).toBe(true);
      }
    }
    // A short run of names fits on one line, nudged rather than stacked.
    expect(places.every((place) => place.row === 0)).toBe(true);
  });

  it('opens a second line only when the first is full', () => {
    const places = placeLaneLabels(
      [
        { x: 0, width: 80 },
        { x: 0, width: 80 }
      ],
      100
    );
    expect(places.map((place) => place.row)).toEqual([0, 1]);
  });
});

describe('the presses one band of the lane is shared into', () => {
  const items = [
    { x: 0, width: 2 },
    { x: 1, width: 34 },
    { x: 35, width: 116 },
    { x: 356, width: 2 }
  ];

  it('never overlap, cover their own mark and stay inside the plot', () => {
    const presses = bandPresses(items, 358, 32);
    for (let i = 0; i < presses.length; i += 1) {
      const { x, width } = presses[i];
      expect(x).toBeGreaterThanOrEqual(0);
      expect(x + width).toBeLessThanOrEqual(358);
      const centre = items[i].x + items[i].width / 2;
      expect(centre >= x && centre < x + width, `item ${i}`).toBe(true);
      for (let j = 0; j < i; j += 1) {
        expect(x >= presses[j].x + presses[j].width || presses[j].x >= x + width).toBe(true);
      }
    }
  });

  it('widens a lone mark to the minimum press, pulled inside the plot', () => {
    expect(bandPresses([{ x: 356, width: 2 }], 358, 32)).toEqual([{ x: 326, width: 32 }]);
    expect(bandPresses([{ x: 100, width: 2 }], 358, 32)).toEqual([{ x: 85, width: 32 }]);
  });

  it('answers in the order it was given, not the order along the band', () => {
    const presses = bandPresses(
      [
        { x: 200, width: 10 },
        { x: 0, width: 10 }
      ],
      358,
      32
    );
    expect(presses[0].x).toBeGreaterThan(presses[1].x);
  });
});
