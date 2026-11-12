import { runHref } from './flight-recorder';
import { itemRef } from './utils';

// The console's ONE run-link resolver and ONE route-claim projection (I25).
//
// Every surface that shows work — the dashboard's routing card, the runs list,
// an epic's node cards — points at the same canonical run detail (`/runs/[id]`,
// whose href is `flight-recorder.runHref`, imported here rather than rebuilt)
// and describes the route with the same three claims:
//
//   configured capability   — what the lane declares. Configuration, never a
//                             measurement, and never evidence that it works.
//   current execution validity — what a settled probe of this EXACT boundary,
//                             plus the runner's own route claim, says right now.
//   journey certification   — whether a journey certificate stands behind this,
//                             which a passing probe can never establish.
//
// The three are deliberately separate and never fold into one another: a live
// probe cannot certify, a prior certificate cannot prove current reachability,
// and evidence that could not be read renders unavailable — never available,
// never ready. Certificate detail stays internal: certification carries no href
// (there is no certificate page), and boundary fingerprints leave nowhere.

// --- canonical run links ---------------------------------------------------

/** Which surface a link came from. The route is identical for all of them. */
export type RunLinkKind = 'standard' | 'loop' | 'epic' | 'probe';

export type RunLinkSource = {
  runId?: string | null;
  itemId?: string | null;
  issueNum?: number | null;
  kind?: RunLinkKind;
};

export type RunLink = {
  available: true;
  kind: RunLinkKind;
  runId: string;
  href: string;
  label: string;
  title: string;
};

/** No run to link to. The reason is rendered; nothing is invented in its place. */
export type RunLinkUnavailable = { available: false; kind: RunLinkKind; reason: string };

export type RunLinkResult = RunLink | RunLinkUnavailable;

const KIND_WORD: Record<RunLinkKind, string> = {
  standard: 'run',
  loop: 'loop run',
  epic: 'epic node run',
  probe: 'readiness probe run'
};

/**
 * The canonical deep link for one run, from any surface. `runId` is opaque —
 * it is encoded into the path by `runHref` and never parsed. An absent run is
 * an honest `available: false` with the reason, not a dead link.
 */
export function runLink(source: RunLinkSource): RunLinkResult {
  const kind = source.kind ?? 'standard';
  const runId = source.runId ?? '';
  if (runId === '') {
    return {
      available: false,
      kind,
      reason:
        kind === 'probe'
          ? 'no probe evidence is available'
          : 'no run has been recorded for this work yet'
    };
  }
  // 0 is the non-GitHub sentinel, not issue zero — `itemRef` drops it for a
  // Linear key and never renders `#0`.
  const ref = itemRef(source.itemId, source.issueNum || null);
  return {
    available: true,
    kind,
    runId,
    href: runHref(runId),
    label: kind === 'probe' ? 'probe evidence' : ref ? `${ref} · flight recorder` : 'flight recorder',
    title: `open the canonical detail for ${KIND_WORD[kind]} ${runId}`
  };
}

// --- claim sources ---------------------------------------------------------

/** A dimension's read, in the shape every backend observation already has. */
export type ObservationFacts = {
  availability: string; // "available" | "stale" | "unavailable"
  observed_at?: string | null;
  detail?: string | null;
};

export type RouteFacts = {
  runner: string;
  model: string;
  reported_model?: string | null;
  containment?: string;
  host?: string;
  instance?: string;
  provenance?: string;
  claim?: string; // "honored" | "substituted" | "unverified"
};

export type ReadinessFacts = {
  state?: string; // "proven" | "refuted" | "expired" | "unproven" | "unknown"
  fingerprint?: string | null;
  evidence_ref?: string | null;
  detail?: string;
  observation?: ObservationFacts;
};

export type CertificationFacts = {
  state?: string; // "certified" | "recertification_due" | "uncertified" | "unknown"
  certified_at?: string | null;
  evidence_ref?: string | null;
  detail?: string;
  observation?: ObservationFacts;
};

/**
 * What the claims are read from. A `WorkView` satisfies this as it stands —
 * the projection's own route claim, readiness dimension and revision — and a
 * surface that only knows its configured lane uses `configuredRoute`.
 */
export type RouteClaimSource = {
  route: RouteFacts;
  readiness?: ReadinessFacts | null;
  certification?: CertificationFacts | null;
  view_revision?: string;
};

/** Split the dashboard's `runner:model` row form; the model may hold colons. */
export function parseRoute(route: string): { runner: string; model: string } {
  const at = route.indexOf(':');
  if (at < 0) return { runner: route, model: '' };
  return { runner: route.slice(0, at), model: route.slice(at + 1) };
}

/**
 * A source for a surface that knows only what was configured/recorded: the
 * lane, and no evidence at all about it. That is not a gap to paper over —
 * it is the honest input for "configured, live-unverified, uncertified".
 */
export function configuredRoute(runner: string, model: string): RouteClaimSource {
  return {
    route: { runner, model, reported_model: null, provenance: 'declared', claim: 'unverified' },
    readiness: null,
    certification: null
  };
}

// --- claim projection ------------------------------------------------------

export type ClaimKind = 'capability' | 'execution' | 'certification';
export type ClaimEvidence = 'available' | 'stale' | 'unavailable';
export type ClaimTone = 'neutral' | 'accent' | 'success' | 'warn' | 'danger';

export type RouteClaim = {
  kind: ClaimKind;
  state: string;
  label: string;
  detail: string;
  evidence: ClaimEvidence;
  evidenceNote: string;
  /** Where the evidence itself can be read, when it is a run. Never a certificate. */
  evidenceLink: RunLink | null;
  tone: ClaimTone;
};

/** The human title for each claim — the same words on every surface. */
export const CLAIM_TITLE: Record<ClaimKind, string> = {
  capability: 'configured capability',
  execution: 'current execution validity',
  certification: 'journey certification'
};

export type RouteClaimSet = {
  capability: RouteClaim;
  execution: RouteClaim;
  certification: RouteClaim;
  all: RouteClaim[];
  /** The backend revision the claims were read at, when the source carries one. */
  revision: string | null;
};

const NO_READ = 'nothing about this could be read here';

function availabilityOf(observation: ObservationFacts | undefined): ClaimEvidence {
  const value = observation?.availability;
  return value === 'available' || value === 'stale' ? value : 'unavailable';
}

function note(observation: ObservationFacts | undefined, fallback: string): string {
  const detail = observation?.detail;
  if (detail) return detail;
  const at = observation?.observed_at;
  return at ? `read ${at}` : fallback;
}

/**
 * Where the attempt ran, when that is somewhere other than this machine.
 *
 * Local is deliberately silent: it is the default and every run was on it
 * until a VM could host one, so printing it would add a word to every row to
 * say nothing. A remote placement names the instance too — "gcp" alone does
 * not tell an operator which machine to go and look at.
 */
function placementSuffix(route: RouteFacts): string {
  const host = (route.host || '').trim();
  if (host === '' || host === 'local') return '';
  const instance = (route.instance || '').trim();
  return instance ? ` · ${host}/${instance}` : ` · ${host}`;
}

function capabilityClaim(route: RouteFacts): RouteClaim {
  const lane = [route.runner, route.model].filter(Boolean).join(' / ');
  if (lane === '') {
    return {
      kind: 'capability',
      state: 'unconfigured',
      label: 'not configured',
      detail: 'no route was recorded for this work',
      evidence: 'unavailable',
      evidenceNote: NO_READ,
      evidenceLink: null,
      tone: 'warn'
    };
  }
  const contained = route.containment === 'contained' ? ' · contained' : '';
  return {
    kind: 'capability',
    state: 'configured',
    label: 'configured',
    detail: `${lane}${contained}${placementSuffix(route)}`,
    evidence: 'available',
    evidenceNote: 'declared by the route configuration — not a measurement',
    evidenceLink: null,
    tone: 'neutral'
  };
}

function claimSuffix(route: RouteFacts): string {
  if (route.claim !== 'honored') return '';
  return ' The runner self-reported the declared model — a self-report, not a host attestation.';
}

function substitutedClaim(route: RouteFacts): RouteClaim {
  const reported = route.reported_model || 'another model';
  return {
    kind: 'execution',
    state: 'substituted',
    label: 'substituted',
    detail: `the runner reported ${reported} where ${route.model || 'the declared model'} was declared`,
    evidence: 'available',
    evidenceNote: 'from the run’s own record of the lane it ran on',
    evidenceLink: null,
    tone: 'danger'
  };
}

function probeLink(readiness: ReadinessFacts | null | undefined): RunLink | null {
  const link = runLink({ runId: readiness?.evidence_ref ?? null, kind: 'probe' });
  return link.available ? link : null;
}

type ExecutionVerdict = { state: string; label: string; detail: string; tone: ClaimTone };

function executionVerdict(
  readiness: ReadinessFacts | null | undefined,
  evidence: ClaimEvidence
): ExecutionVerdict {
  const state = readiness?.state ?? 'unknown';
  if (state === 'refuted') {
    return {
      state: 'refuted',
      label: 'refuted',
      detail: readiness?.detail || 'a probe of this exact boundary failed',
      tone: 'danger'
    };
  }
  if (evidence === 'unavailable') {
    const never = state === 'unproven' || readiness == null;
    return {
      state: 'unverified',
      label: never ? 'never probed' : 'not verifiable right now',
      detail:
        readiness?.detail ||
        'no settled probe of this exact route boundary could be read from here',
      tone: 'neutral'
    };
  }
  if (state === 'expired' || evidence === 'stale') {
    return {
      state: 'expired',
      label: 'last verified, not current',
      detail: readiness?.detail || 'a past probe of this boundary is not current reachability',
      tone: 'warn'
    };
  }
  if (state === 'proven') {
    return {
      state: 'verified',
      label: 'verified live',
      detail: readiness?.detail || 'a settled probe of this exact boundary passed',
      tone: 'success'
    };
  }
  return {
    state: 'unverified',
    label: 'unverified',
    detail: readiness?.detail || 'nothing has proven this exact boundary usable',
    tone: 'neutral'
  };
}

function executionClaim(route: RouteFacts, readiness: ReadinessFacts | null | undefined): RouteClaim {
  // A substituted lane outranks any probe: whatever the boundary can do, the
  // declared route was not the one served.
  if (route.claim === 'substituted') return substitutedClaim(route);
  const evidence = availabilityOf(readiness?.observation);
  const verdict = executionVerdict(readiness, evidence);
  return {
    kind: 'execution',
    ...verdict,
    detail: `${verdict.detail}${claimSuffix(route)}`,
    evidence,
    evidenceNote: note(readiness?.observation, NO_READ),
    evidenceLink: evidence === 'unavailable' ? null : probeLink(readiness)
  };
}

type CertificationVerdict = { state: string; label: string; detail: string; tone: ClaimTone };

const CERTIFICATION_VERDICTS: Record<string, CertificationVerdict> = {
  certified: {
    state: 'certified',
    label: 'certified',
    detail: 'a journey certificate stands behind this — a past certificate, not current reachability',
    tone: 'success'
  },
  recertification_due: {
    state: 'recertification_due',
    label: 'recertification due',
    detail: 'the standing certificate no longer covers this journey',
    tone: 'warn'
  },
  uncertified: {
    state: 'uncertified',
    label: 'uncertified',
    detail: 'no journey certificate stands behind this',
    tone: 'neutral'
  }
};

function certificationClaim(certification: CertificationFacts | null | undefined): RouteClaim {
  const base = {
    kind: 'certification' as const,
    // Certificate detail stays internal: no ref, no page, no link — ever.
    evidenceLink: null
  };
  if (certification == null) {
    return {
      ...base,
      state: 'uncertified',
      label: 'uncertified',
      detail: 'no journey certificate evidence is available in this view',
      evidence: 'unavailable',
      evidenceNote: NO_READ,
      tone: 'neutral'
    };
  }
  const evidence = availabilityOf(certification.observation);
  const evidenceNote = note(certification.observation, NO_READ);
  if (evidence === 'unavailable') {
    // An unreadable certificate is not an absent one, and emphatically not a
    // present one: the claim goes unknown rather than either.
    return {
      ...base,
      state: 'unknown',
      label: 'certificate evidence unavailable',
      detail: certification.detail || 'the certificate evidence could not be read',
      evidence,
      evidenceNote,
      tone: 'warn'
    };
  }
  const verdict = CERTIFICATION_VERDICTS[certification.state ?? ''] ?? {
    state: 'unknown',
    label: 'not established',
    detail: 'no certificate verdict was reported for this journey',
    tone: 'neutral' as ClaimTone
  };
  const at = verdict.state === 'certified' && certification.certified_at;
  return {
    ...base,
    ...verdict,
    detail: at ? `certified ${certification.certified_at} — ${verdict.detail}` : verdict.detail,
    evidence,
    evidenceNote,
    // A stale reading of a certificate is not a current certificate.
    tone: evidence === 'stale' ? 'warn' : verdict.tone
  };
}

/**
 * The three claims, in their fixed order, each with its own evidence. Nothing
 * here is inferred from a neighbouring claim.
 */
export function routeClaims(source: RouteClaimSource): RouteClaimSet {
  const capability = capabilityClaim(source.route);
  const execution = executionClaim(source.route, source.readiness);
  const certification = certificationClaim(source.certification);
  return {
    capability,
    execution,
    certification,
    all: [capability, execution, certification],
    revision: source.view_revision || null
  };
}
