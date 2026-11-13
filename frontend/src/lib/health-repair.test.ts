/**
 * D41 (#3470) — Health's repair lines and notes in plain words: a missing
 * routed image carries the readiness check's own build command, and the
 * runtimes note and a lane's credential read as sentences.
 */
import { describe, expect, it } from 'vitest';
import type { HealthImage, HealthPosture, Readiness, ReadinessCheck } from './api';
import { credentialWords, imageRepair, observedRuntimesNote } from './health';

const BUILD = 'docker build -f docker/agent.Dockerfile -t kew-agent:0.9.0 .';

const image = (over: Partial<HealthImage> = {}): HealthImage => ({
  runner: 'claude-code',
  ref: 'kew-agent:0.9.0',
  source: 'images.claude-code',
  routed: true,
  present: false,
  ...over
});

const check = (over: Partial<ReadinessCheck> = {}): ReadinessCheck => ({
  id: 'route-image',
  status: 'fail',
  summary: "'kew-agent:0.9.0' is not present locally",
  hint: `from a kew checkout, run '${BUILD}'`,
  fix: BUILD,
  details: [],
  warnings: [],
  ...over
});

const readiness = (...checks: ReadinessCheck[]): Readiness => ({
  schema: 1,
  ok: false,
  checked_at: '2026-11-07T10:00:00Z',
  checks
});

describe('imageRepair', () => {
  it("prints the route-image check's fix as the command", () => {
    expect(imageRepair(image(), readiness(check()))).toEqual({ kind: 'command', command: BUILD });
  });

  it("prints the check's hint when kew cannot build the image", () => {
    const hint = "build or pull 'acme/agent:1' into the local runtime; kew never pulls it";
    const custom = check({ summary: "'acme/agent:1' is not present locally", fix: null, hint });
    expect(imageRepair(image({ ref: 'acme/agent:1' }), readiness(custom))).toEqual({
      kind: 'hint',
      hint
    });
  });

  it('gives no repair to an image the check is not about', () => {
    expect(imageRepair(image({ ref: 'kew-agent-codex:0.9.0' }), readiness(check()))).toBeNull();
  });

  it('gives no repair to a present, unrouted or unmapped image, or with no readiness read', () => {
    expect(imageRepair(image({ present: true }), readiness(check()))).toBeNull();
    expect(imageRepair(image({ routed: false }), readiness(check()))).toBeNull();
    expect(imageRepair(image({ ref: null }), readiness(check()))).toBeNull();
    expect(imageRepair(image(), null)).toBeNull();
    expect(imageRepair(image(), readiness(check({ status: 'ok' })))).toBeNull();
  });
});

describe('observedRuntimesNote', () => {
  it('says the week in a sentence, with no runtime said in words', () => {
    expect(observedRuntimesNote({ docker: 34, '': 2 })).toBe(
      'runs in the last 7 days: 34 on docker, 2 with no runtime recorded'
    );
  });

  it('says when there were no runs', () => {
    expect(observedRuntimesNote({})).toBe('no runs recorded in the last 7 days');
    expect(observedRuntimesNote(undefined)).toBe('no runs recorded in the last 7 days');
  });
});

describe('credentialWords', () => {
  it('says a real credential in words and says nothing otherwise', () => {
    const lane = { credential_mode: 'real' } as HealthPosture;
    expect(credentialWords(lane)).toBe('uses a real credential');
    expect(credentialWords({ ...lane, credential_mode: 'placeholder' } as HealthPosture)).toBe('');
  });
});
