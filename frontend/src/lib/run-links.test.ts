import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import {
  runLink,
  CLAIM_TITLE,
  configuredRoute,
  parseRoute,
  routeClaims,
  type CertificationFacts,
  type ObservationFacts,
  type ReadinessFacts,
  type RouteClaimSource
} from './run-links';
import { runHref } from './flight-recorder';
import type { WorkView } from './api';

const obs = (over: Partial<ObservationFacts> = {}): ObservationFacts => ({
  availability: 'available',
  observed_at: '2026-08-18T10:00:00Z',
  detail: null,
  ...over
});

const readiness = (over: Partial<ReadinessFacts> = {}): ReadinessFacts => ({
  state: 'unproven',
  fingerprint: 'fp-boundary-9c1',
  evidence_ref: null,
  detail: '',
  observation: obs({ availability: 'unavailable' }),
  ...over
});

const certification = (over: Partial<CertificationFacts> = {}): CertificationFacts => ({
  state: 'uncertified',
  certified_at: null,
  evidence_ref: null,
  detail: '',
  observation: obs(),
  ...over
});

const source = (over: Partial<RouteClaimSource> = {}): RouteClaimSource => ({
  route: {
    runner: 'claude-code',
    model: 'claude-opus-5',
    reported_model: null,
    containment: 'host',
    provenance: 'declared',
    claim: 'unverified'
  },
  ...over
});

describe('runLink — the one canonical /runs/[id] resolver', () => {
  it('points a standard run at the flight recorder route', () => {
    const link = runLink({ runId: 'run-42', issueNum: 42 });
    expect(link.available).toBe(true);
    if (!link.available) return;
    expect(link.href).toBe('/runs/run-42');
    // One resolver, not two: the href is the existing helper's, verbatim.
    expect(link.href).toBe(runHref('run-42'));
    expect(link.label).toBe('#42 · flight recorder');
    expect(link.kind).toBe('standard');
  });

  it('labels a Linear item by its bare key and never as #0', () => {
    const link = runLink({ runId: 'run-7', itemId: 'SFD-12', issueNum: 0 });
    expect(link.available).toBe(true);
    if (!link.available) return;
    expect(link.label).toBe('SFD-12 · flight recorder');
    expect(link.label).not.toContain('#0');
  });

  it('falls back to a bare label when the work item carries no id', () => {
    const link = runLink({ runId: 'run-7' });
    expect(link.available && link.label).toBe('flight recorder');
  });

  it('keeps an opaque run id opaque — encoded into the path, never parsed', () => {
    const link = runLink({ runId: 'loop/step 1', kind: 'loop' });
    expect(link.available && link.href).toBe('/runs/loop%2Fstep%201');
  });

  it('names the surface a link came from without changing the route', () => {
    const loop = runLink({ runId: 'r1', kind: 'loop' });
    const epic = runLink({ runId: 'r1', kind: 'epic' });
    expect(loop.available && loop.href).toBe(epic.available && epic.href);
    expect(loop.available && loop.title).toContain('loop run');
    expect(epic.available && epic.title).toContain('epic node run');
  });

  it('labels probe evidence as evidence, not as the work item', () => {
    const link = runLink({ runId: 'probe-1', kind: 'probe', issueNum: 42 });
    expect(link.available && link.label).toBe('probe evidence');
  });

  it('renders an absent artifact unavailable, with a reason and no href', () => {
    for (const src of [{ runId: null }, { runId: '' }, { runId: undefined }]) {
      const link = runLink(src);
      expect(link.available).toBe(false);
      if (link.available) return;
      expect(link.reason).toBe('no run has been recorded for this work yet');
      expect(link).not.toHaveProperty('href');
    }
  });

  it('says so plainly when probe evidence itself is absent', () => {
    const link = runLink({ runId: null, kind: 'probe' });
    expect(link.available === false && link.reason).toBe('no probe evidence is available');
  });
});

describe('parseRoute / configuredRoute — the configured lane, as configured', () => {
  it('splits the dashboard row form `runner:model`', () => {
    expect(parseRoute('claude-code:claude-opus-5')).toEqual({
      runner: 'claude-code',
      model: 'claude-opus-5'
    });
  });

  it('keeps a model that itself contains a colon whole', () => {
    expect(parseRoute('codex:gpt-5.6:sol')).toEqual({ runner: 'codex', model: 'gpt-5.6:sol' });
  });

  it('treats a lone token as the runner with no model', () => {
    expect(parseRoute('claude-code')).toEqual({ runner: 'claude-code', model: '' });
    expect(parseRoute('')).toEqual({ runner: '', model: '' });
  });

  it('builds a source that claims configuration and nothing else', () => {
    const src = configuredRoute('claude-code', 'claude-opus-5');
    expect(src.route.provenance).toBe('declared');
    expect(src.route.claim).toBe('unverified');
    expect(src.readiness).toBeNull();
    expect(src.certification).toBeNull();
  });
});

describe('routeClaims — three separate claims, each with its own evidence', () => {
  it('keeps capability, execution and certification apart and in order', () => {
    const set = routeClaims(source());
    expect(set.all.map((c) => c.kind)).toEqual(['capability', 'execution', 'certification']);
    expect(set.all).toEqual([set.capability, set.execution, set.certification]);
  });

  it('titles every claim in the same words on every surface', () => {
    expect(routeClaims(source()).all.map((c) => CLAIM_TITLE[c.kind])).toEqual([
      'configured capability',
      'current execution validity',
      'journey certification'
    ]);
  });

  it('carries the backend view revision the claims were read at', () => {
    expect(routeClaims(source({ view_revision: 'rev-9' })).revision).toBe('rev-9');
    expect(routeClaims(source()).revision).toBeNull();
  });

  // --- configured capability ------------------------------------------------

  it('reports a configured lane as configured — never as working', () => {
    const c = routeClaims(source()).capability;
    expect(c.state).toBe('configured');
    expect(c.detail).toContain('claude-code / claude-opus-5');
    expect(c.evidence).toBe('available');
    expect(c.tone).not.toBe('success');
  });

  it('names the VM a remote run landed on, instance and all', () => {
    // #2398: "gcp" alone does not tell an operator which machine to look at.
    const c = routeClaims(
      source({
        route: {
          runner: 'claude-code',
          model: 'claude-opus-5',
          reported_model: null,
          containment: 'contained',
          host: 'gcp',
          instance: 'vm-one',
          provenance: 'declared',
          claim: 'unverified'
        }
      })
    ).capability;
    expect(c.detail).toContain('gcp/vm-one');
  });

  it('says nothing about placement for a run on this machine', () => {
    // Local is the default and every run was on it until a VM could host one,
    // so printing it would add a word to every row to say nothing.
    const c = routeClaims(
      source({
        route: {
          runner: 'claude-code',
          model: 'claude-opus-5',
          reported_model: null,
          containment: 'host',
          host: 'local',
          instance: '',
          provenance: 'declared',
          claim: 'unverified'
        }
      })
    ).capability;
    expect(c.detail).toBe('claude-code / claude-opus-5');
  });

  it('reports an unconfigured lane as unconfigured, with no evidence', () => {
    const c = routeClaims(source({ route: { runner: '', model: '' } })).capability;
    expect(c.state).toBe('unconfigured');
    expect(c.evidence).toBe('unavailable');
  });

  // --- current execution validity -------------------------------------------

  it('configured-live-unverified: a configured lane nobody probed is unverified', () => {
    const e = routeClaims(source({ readiness: readiness() })).execution;
    expect(e.state).toBe('unverified');
    expect(e.label).toBe('never probed');
    expect(e.evidence).toBe('unavailable');
    expect(e.evidenceLink).toBeNull();
  });

  it('live verified: a settled hit on this exact boundary reads verified, with its probe', () => {
    const e = routeClaims(
      source({
        readiness: readiness({ state: 'proven', evidence_ref: 'probe-run-1', observation: obs() })
      })
    ).execution;
    expect(e.state).toBe('verified');
    expect(e.evidence).toBe('available');
    expect(e.evidenceLink?.href).toBe('/runs/probe-run-1');
    expect(e.evidenceLink?.label).toBe('probe evidence');
  });

  it('temporarily unavailable: an unreadable probe never reads verified', () => {
    const e = routeClaims(
      source({
        readiness: readiness({
          state: 'proven',
          evidence_ref: 'probe-run-1',
          observation: obs({ availability: 'unavailable', detail: 'the probe store was unreadable' })
        })
      })
    ).execution;
    expect(e.state).toBe('unverified');
    expect(e.label).toBe('not verifiable right now');
    expect(e.evidence).toBe('unavailable');
    expect(e.evidenceNote).toContain('the probe store was unreadable');
  });

  it('a stale reading of a passing probe is last-known, not current', () => {
    const e = routeClaims(
      source({ readiness: readiness({ state: 'proven', observation: obs({ availability: 'stale' }) }) })
    ).execution;
    expect(e.state).toBe('expired');
    expect(e.evidence).toBe('stale');
  });

  it('an expired probe reads expired and says a past probe is not current', () => {
    const e = routeClaims(
      source({
        readiness: readiness({ state: 'expired', observation: obs({ availability: 'stale' }) })
      })
    ).execution;
    expect(e.state).toBe('expired');
    expect(e.detail).toContain('not current');
  });

  it('a refuted boundary reads refuted in the server’s own words', () => {
    const e = routeClaims(
      source({
        readiness: readiness({
          state: 'refuted',
          detail: 'the provider rejected the model id',
          observation: obs()
        })
      })
    ).execution;
    expect(e.state).toBe('refuted');
    expect(e.tone).toBe('danger');
    expect(e.detail).toBe('the provider rejected the model id');
  });

  it('a substituted route outranks a passing probe — the lane was not honored', () => {
    const e = routeClaims({
      route: {
        runner: 'claude-code',
        model: 'claude-opus-5',
        reported_model: 'claude-sonnet-5',
        claim: 'substituted'
      },
      readiness: readiness({ state: 'proven', observation: obs() })
    }).execution;
    expect(e.state).toBe('substituted');
    expect(e.tone).toBe('danger');
    expect(e.detail).toContain('claude-sonnet-5');
  });

  it('an honored route claim is a self-report, not a verification', () => {
    const e = routeClaims({
      route: {
        runner: 'claude-code',
        model: 'claude-opus-5',
        reported_model: 'claude-opus-5',
        claim: 'honored'
      },
      readiness: readiness()
    }).execution;
    expect(e.state).toBe('unverified');
    expect(e.detail).toContain('self-report');
  });

  it('no readiness facts at all is unverified, never verified', () => {
    const e = routeClaims(source()).execution;
    expect(e.state).toBe('unverified');
    expect(e.evidence).toBe('unavailable');
  });

  // --- journey certification -------------------------------------------------

  it('uncertified: no certificate facts renders uncertified with no link', () => {
    const c = routeClaims(source()).certification;
    expect(c.state).toBe('uncertified');
    expect(c.evidence).toBe('unavailable');
    expect(c.evidenceLink).toBeNull();
  });

  it('certified: an available certificate reads certified, as of its own time', () => {
    const c = routeClaims(
      source({
        certification: certification({ state: 'certified', certified_at: '2026-08-01T00:00:00Z' })
      })
    ).certification;
    expect(c.state).toBe('certified');
    expect(c.evidence).toBe('available');
    expect(c.detail).toContain('2026-08-01T00:00:00Z');
  });

  it('recertification due reads due, not certified', () => {
    const c = routeClaims(
      source({ certification: certification({ state: 'recertification_due' }) })
    ).certification;
    expect(c.state).toBe('recertification_due');
    expect(c.tone).toBe('warn');
  });

  it('certificate evidence unavailable never renders certified', () => {
    const c = routeClaims(
      source({
        certification: certification({
          state: 'certified',
          observation: obs({ availability: 'unavailable', detail: 'the certificate ref was unreadable' })
        })
      })
    ).certification;
    expect(c.state).toBe('unknown');
    expect(c.evidence).toBe('unavailable');
    expect(c.evidenceNote).toContain('the certificate ref was unreadable');
  });

  it('a stale certificate reading is not a current certificate', () => {
    const c = routeClaims(
      source({
        certification: certification({
          state: 'certified',
          observation: obs({ availability: 'stale' })
        })
      })
    ).certification;
    expect(c.evidence).toBe('stale');
    expect(c.tone).toBe('warn');
  });

  // --- field-level exclusions -----------------------------------------------

  it('excludes boundary fingerprints and certificate refs from every claim', () => {
    const set = routeClaims(
      source({
        readiness: readiness({
          state: 'proven',
          fingerprint: 'fp-boundary-9c1',
          evidence_ref: 'probe-run-1',
          observation: obs()
        }),
        certification: certification({
          state: 'certified',
          evidence_ref: 'refs/notes/kew-certificates:abc123'
        })
      })
    );
    const rendered = JSON.stringify(set);
    expect(rendered).not.toContain('fp-boundary-9c1');
    expect(rendered).not.toContain('refs/notes/kew-certificates');
    // The probe run is reachable as a run, which is the only detail that leaves.
    expect(rendered).toContain('/runs/probe-run-1');
  });

  it('never offers a certificate page — certification carries no href at all', () => {
    for (const state of ['certified', 'recertification_due', 'uncertified', 'unknown']) {
      const c = routeClaims(
        source({ certification: certification({ state, evidence_ref: 'cert-run-1' }) })
      ).certification;
      expect(c.evidenceLink).toBeNull();
    }
  });
});

describe('claim honesty guardrails', () => {
  it('a passing execution probe cannot imply certified', () => {
    const set = routeClaims(
      source({
        readiness: readiness({ state: 'proven', evidence_ref: 'probe-run-1', observation: obs() })
      })
    );
    expect(set.execution.state).toBe('verified');
    expect(set.certification.state).toBe('uncertified');
  });

  it('a prior certificate cannot imply current reachability', () => {
    const set = routeClaims(
      source({
        readiness: readiness({ state: 'unproven' }),
        certification: certification({ state: 'certified', certified_at: '2026-08-01T00:00:00Z' })
      })
    );
    expect(set.certification.state).toBe('certified');
    expect(set.execution.state).toBe('unverified');
  });

  it('absent evidence never renders a claim available or ready', () => {
    const set = routeClaims(
      source({
        route: { runner: '', model: '' },
        readiness: readiness({ observation: obs({ availability: 'unavailable' }) }),
        certification: certification({
          state: 'certified',
          observation: obs({ availability: 'unavailable' })
        })
      })
    );
    for (const claim of set.all) {
      expect(claim.evidence).toBe('unavailable');
      expect(['verified', 'certified', 'configured']).not.toContain(claim.state);
      expect(claim.evidenceLink).toBeNull();
    }
  });
});

describe('a WorkView is a claim source as it stands', () => {
  const view = {
    route: {
      runner: 'claude-code',
      model: 'claude-opus-5',
      reported_model: 'claude-sonnet-5',
      containment: 'contained',
      provenance: 'declared',
      claim: 'substituted'
    },
    readiness: {
      state: 'unproven',
      fingerprint: 'fp-1',
      evidence_ref: null,
      detail: 'this exact boundary has never been probed',
      observation: {
        dimension: 'route_boundary',
        availability: 'unavailable',
        provenance: 'unobserved',
        observed_at: null,
        detail: 'this exact boundary has never been probed'
      }
    },
    view_revision: 'rev-42'
  } as unknown as WorkView;

  it('reads the backend route claim and revision straight off the view', () => {
    const set = routeClaims(view);
    expect(set.revision).toBe('rev-42');
    expect(set.execution.state).toBe('substituted');
    expect(set.capability.detail).toContain('contained');
    expect(set.certification.state).toBe('uncertified');
  });
});

// --- Task 93: the surfaces themselves --------------------------------------
// There is no component renderer in this suite, so navigation and claim
// honesty are verified where they are decided: the shared projection above,
// and the surfaces' own source — which must reach for it rather than word a
// verdict or build a second `/runs/[id]` path of its own.

describe('every existing work surface links and claims through this module', () => {
  // FUI-17 replaced the epic page's node detail Card — the one surface that
  // listed a node's runs with their claims — with the selected-node rail,
  // which sends the reader to Work for them instead of drawing a second run
  // list. It is therefore no longer a run-card surface; it stays in the list
  // below only for the two prohibitions, which hold for any page whether it
  // draws run cards or not.
  const epicPage: [string, string] = [
    'epic page',
    readFileSync(new URL('../routes/(factory)/epics/[ref]/+page.svelte', import.meta.url), 'utf8')
  ];
  /** The surfaces that draw a run card, and so must draw all three claims. */
  const surfaces: Array<[string, string]> = [
    ['runs list', readFileSync(new URL('../routes/(factory)/runs/+page.svelte', import.meta.url), 'utf8')]
  ];
  /** Every page checked here, run cards or not. */
  const pages: Array<[string, string]> = [...surfaces, epicPage];

  it.each(surfaces)('%s renders all three claims, titled from one table', (_name, source) => {
    expect(source).toContain('claims.all');
    expect(source).toContain('CLAIM_TITLE[claim.kind]');
    expect(source).toContain('evidence {claim.evidence}');
  });

  it.each(pages)('%s words no route verdict of its own', (_name, source) => {
    for (const verdict of ["'certified'", "'verified'", "'uncertified'", "'refuted'", "'proven'"]) {
      expect(source).not.toContain(verdict);
    }
  });

  it.each(surfaces)('%s resolves its run link through runLink', (_name, source) => {
    expect(source).toContain("from '$lib/run-links'");
    expect(source).toContain('runLink({');
    expect(source).toContain('link.href');
    // Absent artifact: the reason is rendered where the link would have been.
    expect(source).toContain('link.reason');
  });

  it.each(pages)('%s builds no second /runs/[id] resolver', (_name, source) => {
    // A prose mention is fine; a hand-built path or a second href helper is not.
    for (const built of ["'/runs/", '"/runs/', '`/runs/', 'runHref']) {
      expect(source).not.toContain(built);
    }
  });

  it('offers no certificate page from any surface', () => {
    for (const [, source] of pages) {
      expect(source).not.toContain('/certificates');
      expect(source).not.toContain('evidence_ref');
    }
  });
});
