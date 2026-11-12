/**
 * #2298 — the Health page's readings and what the header badge counts.
 *
 * The rule under every case: three states, not two. A thing that is off by
 * design is grey and counts nowhere; a thing nobody can observe is `unknown` and
 * counts nowhere; only a thing that is down AND needed is red.
 *
 * The routed/unrouted image pair is the issue's own acceptance criterion (AC3),
 * and it is asserted here against the scenario the gallery's `/health` shot is
 * taken over (`scenarios/health-image-missing.json`) — so the number the browser
 * check reads and the number this suite reads are the same number. The shared
 * `factory/health.json` has every routed image present (#3461), so no other
 * screen's header counts "1 down" beside the run it shows working.
 */
import { describe, expect, it } from 'vitest';
import healthJson from '../../fixtures/factory/health.json';
import imageMissingJson from '../../fixtures/scenarios/health-image-missing.json';
import type { Health, HealthImage, HealthRunner, HealthWatcher } from './api';
import {
  BUDGET_FIELDS,
  dataAgeLabel,
  driverState,
  emptyKind,
  fieldValueLabel,
  githubRequired,
  healthBadge,
  healthHeadline,
  healthWarnings,
  heartbeatLabel,
  loopsNote,
  laneDangers,
  laneWhy,
  imageState,
  postureState,
  readingOf,
  relativeAge,
  runnerState,
  runtimeState,
  watcherState,
  STATE_COLOR
} from './health';

const fixture: Health = healthJson;
/** The `/health` shot's payload: the shared one with its routed image missing. */
const imageMissing: Health = imageMissingJson;

const image = (over: Partial<HealthImage> = {}): HealthImage => ({
  runner: 'claude-code',
  ref: 'kew-agent:0.9.0',
  source: 'images.claude-code',
  routed: true,
  present: true,
  ...over
});

const runner = (over: Partial<HealthRunner> = {}): HealthRunner => ({
  name: 'claude-code',
  binary: 'claude',
  binary_present: true,
  version: '2.1.4',
  version_supported: true,
  credential_names: [{ name: 'ANTHROPIC_API_KEY', set: true }],
  routed_tiers: ['default'],
  review_lane: true,
  ...over
});

describe('the three states', () => {
  it('reads a disabled thing as not enabled whatever the probe says', () => {
    expect(readingOf(false, false)).toBe('not-enabled');
    expect(readingOf(false, true)).toBe('not-enabled');
  });

  it('reads an unobservable thing as unknown, never as down', () => {
    expect(readingOf(true, null)).toBe('unknown');
  });

  it('reserves red for something enabled and definitely down', () => {
    expect(readingOf(true, false)).toBe('required');
    expect(readingOf(true, true)).toBe('running');
  });

  it('paints only `required` in the danger token', () => {
    expect(STATE_COLOR.required).toBe('var(--danger)');
    expect(STATE_COLOR['not-enabled']).toBe('var(--text-5)');
    expect(STATE_COLOR.unknown).not.toBe('var(--danger)');
  });
});

describe('images', () => {
  it('fails a missing image a lane routes to', () => {
    expect(imageState(image({ present: false }))).toBe('required');
  });

  it('greys a missing image nothing routes to', () => {
    expect(imageState(image({ routed: false, present: false }))).toBe('not-enabled');
  });

  it('fails a routed runner with no image mapped at all', () => {
    expect(imageState(image({ ref: null, source: 'unmapped', present: null }))).toBe('required');
  });

  it('calls an unprobeable routed image unknown rather than missing', () => {
    // A host with no docker cannot answer "is this image here?".
    expect(imageState(image({ present: null }))).toBe('unknown');
  });
});

describe('runtimes', () => {
  it('greys a runtime this project did not select', () => {
    expect(runtimeState({ configured: false, backed: true, present: false })).toBe('not-enabled');
  });

  it('calls a named runtime with no backend in this build unknown', () => {
    // openshell: accepted by config, no backend yet. Named, never failed.
    expect(runtimeState({ configured: true, backed: false, present: null })).toBe('unknown');
  });

  it('fails the configured runtime when it is absent', () => {
    expect(runtimeState({ configured: true, backed: true, present: false })).toBe('required');
  });
});

describe('runners', () => {
  it('fails a runner whose binary is not on PATH', () => {
    expect(runnerState(runner({ binary_present: false }))).toBe('required');
  });

  it('accepts the subscription credential, not just the API key', () => {
    // CLAUDE_CODE_OAUTH_TOKEN is absent from CLAUDE_AUTH_ENVS, which is why the
    // old reachability check called this operator credential-less.
    const subscription = runner({
      credential_names: [
        { name: 'ANTHROPIC_API_KEY', set: false },
        { name: 'CLAUDE_CODE_OAUTH_TOKEN', set: true }
      ]
    });
    expect(runnerState(subscription)).toBe('running');
  });

  it('fails a reachable runner with none of its declared credentials set', () => {
    const none = runner({ credential_names: [{ name: 'ANTHROPIC_API_KEY', set: false }] });
    expect(runnerState(none)).toBe('required');
  });

  it('does not fail a runner that declares no credential at all', () => {
    expect(runnerState(runner({ credential_names: [] }))).toBe('running');
  });
});

describe('lanes, watchers and drivers', () => {
  it('fails a lane dispatch would refuse outright', () => {
    const lane = fixture.posture[0];
    expect(postureState(lane)).toBe('running');
    expect(postureState({ ...lane, refusal: 'image_unmapped' })).toBe('required');
  });

  it('folds a lane`s sentences behind why, and has none for a clean lane (#2536)', () => {
    const lane = fixture.posture[0];
    expect(laneWhy(lane)).toEqual([]);
    expect(
      laneWhy({
        ...lane,
        refusal: 'image_unmapped',
        dangerously_skip_containment: true,
        console_refusal: 'credential_missing'
      })
    ).toEqual([
      'Dispatch refuses this lane: image_unmapped',
      'Containment is skipped by config (dangerously_skip_containment).',
      'This console cannot launch it (credential_missing); the loops may.'
    ]);
  });

  it('calls an uncontained lane unknown rather than broken', () => {
    // Running on the host is a posture an operator opted into by name. It is a
    // claim to read, not a fault to alarm on; the posture column says so.
    expect(postureState({ ...fixture.posture[0], contained: false })).toBe('unknown');
  });

  it('never fails a watcher that is off by design', () => {
    const off: HealthWatcher = { name: 'delegation', enabled: false, running: null, detail: '' };
    expect(watcherState(off)).toBe('not-enabled');
  });

  it('never fails a watcher that writes no heartbeat', () => {
    const on: HealthWatcher = { name: 'delegation', enabled: true, running: null, detail: '' };
    expect(watcherState(on)).toBe('unknown');
  });

  it('takes a driver reading from the server word, not a fresh threshold', () => {
    expect(driverState(fixture.drivers[0])).toBe('running');
    expect(driverState(fixture.drivers[1])).toBe('unknown');
    expect(driverState({ ...fixture.drivers[0], liveness: 'gone' })).toBe('required');
  });
});

describe('what the header badge counts', () => {
  it('counts the scenario’s missing ROUTED image and not its unrouted one', () => {
    // AC3, over the payload the browser check is taken on.
    const warnings = healthWarnings(imageMissing);
    const images = warnings.filter((w) => w.kind === 'image');
    expect(images).toHaveLength(1);
    expect(images[0].label).toContain('kew-agent:0.9.0');
    expect(JSON.stringify(warnings)).not.toContain('opencode');
  });

  it('counts nothing on the shared fixture, whose routed images are all present', () => {
    // #3461: every screen reads this payload; none may read "1 down" beside a working run.
    expect(healthWarnings(fixture)).toEqual([]);
    expect(healthBadge(fixture)).toBeNull();
  });

  it('points each warning at the section that explains it', () => {
    for (const warning of healthWarnings(imageMissing)) {
      expect(warning.anchor.startsWith('#')).toBe(true);
    }
  });

  it('counts an unreachable tracker only when something uses it', () => {
    const broken: Health = {
      ...fixture,
      trackers: {
        ...fixture.trackers,
        github: { ...fixture.trackers.github, auth_ok: false }
      }
    };
    expect(healthWarnings(broken).some((w) => w.kind === 'tracker')).toBe(true);

    const idle: Health = {
      ...broken,
      drivers: [],
      fleet: { stopped: false, pending: 0, reason: '' }
    };
    expect(githubRequired(idle)).toBe(false);
    expect(healthWarnings(idle).some((w) => w.kind === 'tracker')).toBe(false);
  });

  it('counts an unreachable GitHub and a bad credential as one line, not two', () => {
    const unreachable: Health = {
      ...fixture,
      trackers: {
        ...fixture.trackers,
        github: { ...fixture.trackers.github, reachable: false, auth_ok: null }
      }
    };
    const lines = healthWarnings(unreachable).filter((w) => w.kind === 'tracker');
    expect(lines.map((l) => l.label)).toEqual(['GitHub could not be reached']);

    const unauthorized: Health = {
      ...fixture,
      trackers: {
        ...fixture.trackers,
        github: { ...fixture.trackers.github, reachable: true, auth_ok: false }
      }
    };
    const authLines = healthWarnings(unauthorized).filter((w) => w.kind === 'tracker');
    expect(authLines.map((l) => l.label)).toEqual(['the GitHub credential is not working']);
  });

  it('counts a Linear outage only on a Linear project', () => {
    const base: Health = {
      ...fixture,
      trackers: {
        ...fixture.trackers,
        linear: { ...fixture.trackers.linear, reachable: false }
      }
    };
    expect(healthWarnings(base).some((w) => w.label.includes('Linear'))).toBe(true);

    const onGithub: Health = {
      ...base,
      trackers: {
        ...base.trackers,
        linear: { ...base.trackers.linear, configured: false }
      }
    };
    expect(healthWarnings(onGithub).some((w) => w.label.includes('Linear'))).toBe(false);
  });

  it('shows nothing in the header when nothing required is down', () => {
    const well: Health = {
      ...fixture,
      images: fixture.images.map((i) => ({ ...i, present: true }))
    };
    expect(healthBadge(well)).toBeNull();
    expect(healthBadge(null)).toBeNull();
  });

  it('carries every counted line in the header title', () => {
    const badge = healthBadge(imageMissing);
    expect(badge?.count).toBe(healthWarnings(imageMissing).length);
    expect(badge?.title).toContain('kew-agent:0.9.0');
  });

  it('counts nothing at all when the reading could not be taken', () => {
    // An unread payload is not a broken factory. The badge stays silent.
    expect(healthWarnings(null)).toEqual([]);
  });
});

describe('#2534 — down means required AND down', () => {
  // The healthy drive the issue saw read "59 down". Each of its three causes is
  // a fixture here over an otherwise-well payload, and each must read 0.
  const well: Health = {
    ...fixture,
    images: fixture.images.map((i) => ({ ...i, present: true }))
  };
  const goneDriver = {
    ...fixture.drivers[0],
    epic_ref: 'adversarial-review-stage',
    liveness: 'gone',
    tick_age_s: 6_000_000,
    last_tick_at: '2026-07-23T10:00:00+00:00'
  };

  it('starts from a payload with nothing down', () => {
    expect(healthBadge(well)).toBeNull();
  });

  it('does not count a finished or archived epic’s silent driver', () => {
    const finished: Health = {
      ...well,
      drivers: [{ ...goneDriver, active: false }]
    };
    expect(driverState(finished.drivers[0])).toBe('not-enabled');
    expect(healthWarnings(finished)).toEqual([]);
    expect(healthBadge(finished)).toBeNull();
  });

  it('does not count a lane only the console itself cannot launch', () => {
    // The server leaves `refusal` null for the console's own missing credential.
    const consoleOnly: Health = {
      ...well,
      posture: well.posture.map((lane) => ({
        ...lane,
        contained: false,
        posture: 'unknown',
        refusal: null,
        console_refusal: 'credential_missing'
      }))
    };
    expect(consoleOnly.posture.map(postureState)).not.toContain('required');
    expect(healthBadge(consoleOnly)).toBeNull();
  });

  it('reads a driver busy in a foreground stage as running, not gone', () => {
    const busy: Health = {
      ...well,
      drivers: [{ ...goneDriver, liveness: 'busy' }]
    };
    expect(driverState(busy.drivers[0])).toBe('running');
    expect(healthBadge(busy)).toBeNull();
  });

  it('still counts one real required failure as 1', () => {
    const down: Health = {
      ...well,
      drivers: [
        { ...goneDriver, active: false },
        { ...goneDriver, epic_ref: 'live-epic', active: true }
      ]
    };
    expect(healthBadge(down)?.count).toBe(1);
    expect(healthBadge(down)?.title).toContain('live-epic');
  });

  it('needs GitHub only for an active epic’s driver', () => {
    const idle: Health = {
      ...well,
      drivers: [{ ...goneDriver, active: false }],
      fleet: { stopped: false, pending: 0, reason: '' }
    };
    expect(githubRequired(idle)).toBe(false);
  });
});

describe('#2566 — a serve-only credential and a slot-waiting loop are not down', () => {
  // The live ledger after #2558 read "3 down", and none was a failure: the
  // serve process's own missing credential, and two loops parked in the slot
  // gate. Each is a fixture over an otherwise-well payload.
  const well: Health = {
    ...fixture,
    images: fixture.images.map((i) => ({ ...i, present: true }))
  };
  const serveOnly = runner({
    credential_names: [
      { name: 'ANTHROPIC_API_KEY', set: false },
      { name: 'CLAUDE_CODE_OAUTH_TOKEN', set: false }
    ],
    succeeded_in_window: 3,
    credential_note: 'not visible to kew serve; the loops hold their own'
  });
  const silent = {
    ...fixture.drivers[0],
    epic_ref: 'host-contract',
    tick_age_s: 900,
    interval_s: 60
  };

  it('counts a credential only kew serve lacks as 0 down', () => {
    const reading: Health = { ...well, runners: [serveOnly] };
    expect(runnerState(serveOnly)).toBe('running');
    expect(healthBadge(reading)).toBeNull();
  });

  it('still counts the credential when no run proves the loops hold one', () => {
    const unproven = { ...serveOnly, succeeded_in_window: 0, credential_note: null };
    expect(runnerState(unproven)).toBe('required');
    expect(healthBadge({ ...well, runners: [unproven] })?.count).toBe(1);
  });

  it('reads a fresh waiting heartbeat as waiting, quiet, and 0 down', () => {
    const waiting = { ...silent, liveness: 'waiting', waiting_reason: 'slot' };
    expect(driverState(waiting)).toBe('waiting');
    expect(STATE_COLOR.waiting).toBe('var(--text-3)');
    expect(healthBadge({ ...well, drivers: [waiting] })).toBeNull();
  });

  it('counts a driver whose waiting heartbeat went stale as gone, 1 down', () => {
    // The server stops saying `waiting` past twice the cadence.
    const stale = { ...silent, liveness: 'gone', waiting_reason: null };
    expect(driverState(stale)).toBe('required');
    expect(healthBadge({ ...well, drivers: [stale] })?.count).toBe(1);
  });

  it('reads the whole live shape — three readings — as 0 down', () => {
    const live: Health = {
      ...well,
      runners: [serveOnly],
      drivers: ['host-contract', 'openshell-runtime'].map((epic_ref) => ({
        ...silent,
        epic_ref,
        liveness: 'waiting',
        waiting_reason: 'slot'
      }))
    };
    expect(healthWarnings(live)).toEqual([]);
  });
});

describe('#2849 — the headline names every red word the page draws', () => {
  const well: Health = {
    ...fixture,
    images: fixture.images.map((i) => ({ ...i, present: true }))
  };
  const loopsOnly = {
    ...fixture.posture[0],
    credential_mode: 'placeholder',
    console_refusal: 'credential_missing'
  };

  it('says nothing past the verdict when no lane draws red', () => {
    expect(laneDangers(well.posture[0])).toEqual([]);
    expect(healthHeadline(well)).toBe('Nothing required is down.');
  });

  it('names the placeholder credential on loops-only lanes beside "nothing required"', () => {
    const reading: Health = {
      ...well,
      posture: [
        { ...loopsOnly, lane: 'default' },
        { ...loopsOnly, lane: 'review' }
      ]
    };
    expect(healthBadge(reading)).toBeNull();
    expect(healthHeadline(reading)).toBe(
      'Nothing required is down · credential placeholder on 2 loops-only lanes.'
    );
  });

  it('names skipped containment, and drops "loops-only" when a lane in the count is not', () => {
    const skipped = { ...well.posture[1], dangerously_skip_containment: true };
    expect(laneDangers(skipped)).toEqual(['containment skipped']);
    const reading: Health = { ...well, posture: [skipped, { ...loopsOnly, dangerously_skip_containment: true }] };
    expect(healthHeadline(reading)).toBe(
      'Nothing required is down · containment skipped on 2 lanes · credential placeholder on 1 loops-only lane.'
    );
  });

  it('keeps the counted verdict first when something required is down', () => {
    expect(healthHeadline({ ...imageMissing, posture: [loopsOnly] })).toBe(
      '1 thing needs attention before the factory can work · credential placeholder on 1 loops-only lane.'
    );
  });
});

describe('heartbeats and the refresh control', () => {
  it('says "never" for a heartbeat that was never written', () => {
    expect(heartbeatLabel(null, null)).toBe('never');
    expect(heartbeatLabel('', null)).toBe('never');
  });

  it('shows the absolute time and the relative age together', () => {
    const label = heartbeatLabel('2026-09-24T15:46:12+00:00', 46);
    expect(label).toContain('·');
    expect(label).toContain('46s ago');
  });

  it('drops the age when the stamp could not be aged', () => {
    const label = heartbeatLabel('2026-09-24T15:46:12+00:00', null);
    expect(label).not.toContain('ago');
  });

  it('scales an age through the factory’s own units', () => {
    expect(relativeAge(5)).toBe('5s ago');
    expect(relativeAge(130)).toBe('2m ago');
    expect(relativeAge(7300)).toBe('2h ago');
    expect(relativeAge(200000)).toBe('2d ago');
  });

  it('states an upper bound on the data’s age, never a false "live"', () => {
    const taken = '2026-09-24T15:46:58+00:00';
    const now = new Date(taken).getTime() + 11_500;
    expect(dataAgeLabel(taken, now)).toBe('data is at most 12 s old');
  });

  it('says when old data was checked instead of a bound in raw seconds', () => {
    const taken = '2026-09-24T15:46:58+00:00';
    // 12 days, 8 hours and 12 minutes later: the bound would read 1066320 s.
    const now = new Date(taken).getTime() + (12 * 86_400 + 8 * 3_600 + 12 * 60) * 1000;
    expect(dataAgeLabel(taken, now)).toBe('data was checked 1w 5d ago');
    // The two-minute edge keeps the bound; one second later it switches.
    const edge = new Date(taken).getTime() + 119_000;
    expect(dataAgeLabel(taken, edge)).toBe('data is at most 119 s old');
    expect(dataAgeLabel(taken, edge + 1_000)).toBe('data was checked 2m ago');
  });

  it('says there is no data rather than inventing an age', () => {
    expect(dataAgeLabel(null, Date.now())).toBe('no data yet');
    expect(dataAgeLabel('not-a-date', Date.now())).toBe('no data yet');
  });
});

describe('the two empty states', () => {
  it('distinguishes nothing configured from nothing matching', () => {
    expect(emptyKind(0, 0)).toBe('none-configured');
    expect(emptyKind(5, 0)).toBe('none-matching');
    expect(emptyKind(5, 2)).toBe('not-empty');
  });
});

describe('the budgets section', () => {
  it('lists every cap the fixture’s effective config carries', () => {
    expect(BUDGET_FIELDS).toContain('review.max_rounds');
    expect(BUDGET_FIELDS).toContain('epic.max_parallel');
    expect(BUDGET_FIELDS).toContain('agent.timeout_minutes');
  });

  it('prints a structured value as JSON rather than [object Object]', () => {
    expect(fieldValueLabel({ runner: 'codex', model: '' })).toBe('{"runner":"codex","model":""}');
    expect(fieldValueLabel(null)).toBe('not set');
    expect(fieldValueLabel(undefined)).toBe('not set');
    expect(fieldValueLabel(4)).toBe('4');
  });
});

describe('the daemons note', () => {
  it('says no zero count', () => {
    expect(loopsNote(0, 0)).toBe('no loop pursuing');
    expect(loopsNote(1, 0)).toBe('1 loop pursuing');
    expect(loopsNote(3, 0)).toBe('3 loops pursuing');
    expect(loopsNote(0, 2)).toBe('no loop pursuing · 2 stale');
    expect(loopsNote(2, 1)).toBe('2 loops pursuing · 1 stale');
  });
});
