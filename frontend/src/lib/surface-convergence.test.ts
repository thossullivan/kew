/**
 * I26 — the web half of same-revision surface parity.
 *
 * `kew/tests/test_surface_convergence.py` composes one golden WorkView matrix
 * and projects it through the CLI JSON contract, the TUI view model and the
 * safe-action gate. This file reads the SAME committed fixture and projects it
 * through the console's own adapters, so a disagreement between the surfaces
 * fails on both sides of the wire rather than in a screenshot.
 *
 * What is compared is semantics — canonical identity, the orthogonal
 * dimensions, per-dimension availability, the offered set, `view_revision`,
 * and the three route claims. Copy, colour
 * and layout are deliberately outside the comparison, as are the steps the
 * console does not perform (deciding on a pull request, fixing a build): for
 * those the projection offers nothing and the console links to the canonical
 * run instead of inventing a button.
 *
 * `certification` in the fixture is a TEST-ONLY input to `routeClaims`. No
 * production mapper puts certificate facts in a `WorkView` — the Python side
 * proves the composed document has no certificate field at all — which is what
 * makes "verified live" and "certified" independent claims here.
 *
 * The fixture is generated, never hand-edited:
 *   KEW_WRITE_WORK_VIEW_GOLDEN=1 uv run pytest tests/test_surface_convergence.py
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it, vi } from 'vitest';
import {
  CANONICAL_WORK_ACTIONS,
  exceptionLabel,
  exceptionTone,
  offeredActions,
  workActionLabel
} from './dashboard';
import { routeClaims, runLink, type CertificationFacts } from './run-links';
import { performWorkAction, type WorkView } from './api';

type Offer = {
  action: string;
  target_kind: string;
  target_ref: string;
  safest: boolean;
  requires_reason: boolean;
};

type Expected = {
  item_ref: string;
  execution_state: string;
  attempts: number;
  artifact_state: string;
  artifact_publication: string;
  judgment_state: string;
  outcome_state: string;
  source_synchronization: string;
  no_change_state: string;
  readiness_state: string;
  route_claim: string;
  exception: string;
  exception_blocking: boolean;
  safest_action: string;
  blocked_by: string | null;
  actions: Offer[];
  availability: Record<string, string>;
  worst_availability: string;
  pressure_saturated: boolean;
  pressure_countable: boolean;
  claims: { capability: string; execution: string; certification: string };
  view_revision: string;
};

type GoldenCase = {
  name: string;
  summary: string;
  tracker: string;
  claim_shape: string;
  certification: CertificationFacts | null;
  expected: Expected;
  view: WorkView;
};

const FIXTURE = fileURLToPath(
  new URL('../../../kew/tests/fixtures/product_coherence/work_views.json', import.meta.url)
);

const matrix = JSON.parse(readFileSync(FIXTURE, 'utf8')) as { cases: GoldenCase[] };
const CASES = matrix.cases;
const each = CASES.map((c) => [c.name, c] as const);

/** The claims exactly as a console surface reads them: view first, cert beside. */
const claimsOf = (c: GoldenCase) =>
  routeClaims({
    route: c.view.route,
    readiness: c.view.readiness,
    certification: c.certification,
    view_revision: c.view.view_revision
  });

const offerShape = (o: {
  action: string;
  target_kind: string;
  target_ref: string;
  safest: boolean;
  requires_reason: boolean;
}): Offer => ({
  action: o.action,
  target_kind: o.target_kind,
  target_ref: o.target_ref,
  safest: o.safest,
  requires_reason: o.requires_reason
});

describe('the golden matrix', () => {
  it('is the one the backend committed, with every case named', () => {
    expect(CASES.length).toBeGreaterThan(0);
    expect(new Set(CASES.map((c) => c.name)).size).toBe(CASES.length);
    for (const c of CASES) expect(c.expected.view_revision).toMatch(/^wv1:[0-9a-f]{32}$/);
  });

  it('carries both tracker id forms through unparsed', () => {
    const refs = CASES.map((c) => c.expected.item_ref);
    expect(refs.some((r) => r.startsWith('#'))).toBe(true);
    expect(refs.some((r) => /^[A-Z]+-\d+$/.test(r))).toBe(true);
  });
});

describe.each(each)('%s — same facts, same reading', (_name, c) => {
  it('reads the canonical identity the backend composed', () => {
    expect(c.view.identity.item_ref).toBe(c.expected.item_ref);
    expect(c.view.view_revision).toBe(c.expected.view_revision);
    expect(c.view.execution.attempts).toBe(c.expected.attempts);
  });

  it('reads every orthogonal dimension without re-deriving one', () => {
    expect(c.view.execution.state).toBe(c.expected.execution_state);
    expect(c.view.artifact.state).toBe(c.expected.artifact_state);
    expect(c.view.artifact.publication).toBe(c.expected.artifact_publication);
    expect(c.view.judgment.state).toBe(c.expected.judgment_state);
    expect(c.view.outcome.state).toBe(c.expected.outcome_state);
    expect(c.view.source.synchronization).toBe(c.expected.source_synchronization);
    expect(c.view.no_change.state).toBe(c.expected.no_change_state);
    expect(c.view.readiness.state).toBe(c.expected.readiness_state);
    expect(c.view.route.claim).toBe(c.expected.route_claim);
  });

  it('keeps each remote reading answerable on its own', () => {
    const seen = Object.fromEntries(c.view.observations.map((o) => [o.dimension, o.availability]));
    expect(seen).toEqual(c.expected.availability);
  });

  it('renders the exception the projection named, never one of its own', () => {
    expect(exceptionLabel(c.view.exception.state)).toBeTruthy();
    expect(c.view.exception.state).toBe(c.expected.exception);
    expect(c.view.exception.blocking).toBe(c.expected.exception_blocking);
    const tone = exceptionTone(c.view.exception);
    expect(tone).toBe(
      c.view.exception.state === 'none' ? 'neutral' : c.expected.exception_blocking ? 'danger' : 'warn'
    );
  });
});

describe.each(each)('%s — offered actions', (_name, c) => {
  it('offers exactly what the projection stood behind, safest first', () => {
    expect(offeredActions(c.view).map(offerShape)).toEqual(c.expected.actions);
  });

  it('labels only canonical verbs — a button is never invented for a step', () => {
    for (const offer of offeredActions(c.view)) {
      expect(CANONICAL_WORK_ACTIONS as readonly string[]).toContain(offer.action);
      expect(workActionLabel(offer.action)).toBeTruthy();
      expect(offer.target_ref).toBeTruthy();
    }
  });

  it('links to the canonical run when there is nothing safe to press', () => {
    if (offeredActions(c.view).length > 0) return;
    const link = runLink({
      runId: c.view.identity.run_id,
      itemId: c.view.identity.item_id,
      issueNum: null
    });
    expect(link.available).toBe(true);
    if (link.available) expect(link.href).toContain(encodeURIComponent(link.runId));
  });

  it('cannot ask for a verb this view withheld', () => {
    const offered = offeredActions(c.view).map((o) => o.action);
    const withheld = (CANONICAL_WORK_ACTIONS as readonly string[]).filter(
      (a) => !offered.includes(a)
    );
    // The withheld set is what the backend gate rejects with a 409; the console
    // has no second list it could offer one from.
    expect(withheld.length).toBeGreaterThan(0);
    for (const verb of withheld) expect(offered).not.toContain(verb);
  });

  it('sends only a verb and the revision it was offered at', async () => {
    const offer = offeredActions(c.view)[0];
    if (!offer) return;
    const fakeFetch = vi.fn(async () => new Response('{}'));
    await performWorkAction(
      c.view.identity.item_id ?? '',
      offer.action,
      c.view.view_revision,
      '',
      fakeFetch as unknown as typeof fetch
    );
    const [path, init] = fakeFetch.mock.calls[0] as unknown as [string, RequestInit];
    // The opaque id rides through encoded; the target the verb touches is
    // resolved server-side from the same facts and is not in the request.
    expect(path).toBe(
      `/api/verbs/work/${encodeURIComponent(c.view.identity.item_id ?? '')}/action`
    );
    expect(JSON.parse(init.body as string)).toEqual({
      action: offer.action,
      expected_revision: c.view.view_revision,
      reason: ''
    });
  });
});

describe.each(each)('%s — route claims', (_name, c) => {
  it('projects the three claims the fixture declares', () => {
    const claims = claimsOf(c);
    expect(claims.capability.state).toBe(c.expected.claims.capability);
    expect(claims.execution.state).toBe(c.expected.claims.execution);
    expect(claims.certification.state).toBe(c.expected.claims.certification);
  });

  it('reads them at the backend revision they were composed at', () => {
    expect(claimsOf(c).revision).toBe(c.expected.view_revision);
  });

  it('keeps every claim in its fixed order with its own evidence', () => {
    const claims = claimsOf(c);
    expect(claims.all.map((x) => x.kind)).toEqual(['capability', 'execution', 'certification']);
    expect(claims.execution.evidence).toBe(c.expected.availability.route_boundary);
  });

  it('never offers a certificate page — certificate detail stays internal', () => {
    expect(claimsOf(c).certification.evidenceLink).toBeNull();
  });

  it('does not let a live probe imply a certificate, or the reverse', () => {
    const withoutCert = routeClaims({
      route: c.view.route,
      readiness: c.view.readiness,
      certification: null,
      view_revision: c.view.view_revision
    });
    const withoutProbe = routeClaims({
      route: c.view.route,
      readiness: null,
      certification: c.certification,
      view_revision: c.view.view_revision
    });
    // Dropping the certificate cannot change execution validity, and dropping
    // the probe cannot change certification: neither is inferred from the other.
    expect(withoutCert.execution).toEqual(claimsOf(c).execution);
    expect(withoutProbe.certification).toEqual(claimsOf(c).certification);
  });
});

describe('claim truth across the matrix', () => {
  it('covers every claim shape the release names', () => {
    for (const shape of [
      'configured_live_unverified',
      'live_verified',
      'prior_certificate_during_outage',
      'recertification_due',
      'certificate_evidence_unavailable',
      'anchored_certified'
    ]) {
      expect(CASES.some((x) => x.claim_shape === shape)).toBe(true);
    }
  });

  it('proves a passing probe never reads as certified', () => {
    const verified = CASES.filter((x) => claimsOf(x).execution.state === 'verified');
    expect(verified.length).toBeGreaterThan(0);
    expect(verified.some((x) => claimsOf(x).certification.state === 'uncertified')).toBe(true);
  });

  it('proves a standing certificate never reads as current reachability', () => {
    const certified = CASES.filter((x) => claimsOf(x).certification.state === 'certified');
    expect(certified.length).toBeGreaterThan(0);
    expect(certified.some((x) => claimsOf(x).execution.state !== 'verified')).toBe(true);
    for (const x of certified) {
      expect(claimsOf(x).certification.detail).toContain('not current reachability');
    }
  });

  it('proves unreadable certificate evidence never renders ready', () => {
    const blind = CASES.filter((x) => x.certification?.observation?.availability === 'unavailable');
    expect(blind.length).toBeGreaterThan(0);
    for (const x of blind) {
      const claim = claimsOf(x).certification;
      expect(claim.state).toBe('unknown');
      expect(claim.label).toBe('certificate evidence unavailable');
    }
  });

  it('proves a stale live reading is expired, not verified', () => {
    const stale = CASES.filter((x) => x.expected.availability.route_boundary === 'stale');
    expect(stale.length).toBeGreaterThan(0);
    for (const x of stale) expect(claimsOf(x).execution.state).toBe('expired');
  });
});
