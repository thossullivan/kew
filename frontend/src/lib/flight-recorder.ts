// The typed client model behind /runs/[id] — the canonical deep evidence view.
//
// Everything here is derived from three canonical Flight reads and nothing
// else: the run row (`/api/runs/{id}`), the receipt-backed evidence projection
// (`/api/runs/{id}/flight`) and the artifact-bound change set
// (`/api/runs/{id}/nodes/{node}/diff`). The old recorder answered out of a
// speculative snapshot tree and a diff computed against whatever the host
// repository held when the page opened; this module has no repository input at
// all, so there is nothing here that can have moved on since the run ended.
//
// Two rules keep the projection honest, both inherited from the server:
//
// 1. **Missing is missing.** A dimension that could not be read becomes an
//    explicit unavailable row with the server's own detail — never a zero,
//    never a value borrowed from a neighbouring field.
// 2. **A claim is not an attestation.** What the host recorded at dispatch and
//    what the runner said about itself are shown as two separate facts, and
//    the runner's half is labelled as the claim it is.

import type {
  Diff,
  EvidenceAvailability,
  FlightEvent,
  FlightTimeline,
  ProtectedPrompt,
  Run,
  RunAuditEvent
} from './api';
import { itemRef } from './utils';
import { runKindLabel } from './factory/names';
import { eventWords } from './monitor/event-line';
// A cycle (`events.ts` imports `runHref` from here), read only inside a function.
import { VERDICT_SCOPE_RUN } from './runs/events';
import noArtifactKinds from './no-artifact-run-kinds.json';
import { safeHref } from './safe-href';

// Dimension names as the server spells them (kew/src/kew/flight/evidence.py).
const DIM_HOST_SEQUENCE = 'host_sequence';
const DIM_ARTIFACT_SEAL = 'artifact_seal';
const DIM_ARTIFACT_DIFF = 'artifact_diff';
const DIM_RUNNER_USAGE = 'runner_usage';

const PROMPT_ARTIFACT_KIND = 'compiled_prompt';
const TRUSTED_BASE_FACT = 'trusted_base_sha';
const EXACT_TREE = 'exact_tree';

const NOT_SEALED = 'nothing is sealed for this attempt';

export type FlightSources = {
  /** The id the route was opened with — the only identity a 404 still has. */
  runId?: string;
  run: Run | null;
  flight: FlightTimeline | null;
  diff: Diff | null;
  /**
   * The run's ledger events as the page read them (`/api/runs/{id}/audit`) —
   * the list drawn above the safest action. Only publication facts are read
   * off it (#3057); absent means none were read.
   */
  events?: RunAuditEvent[] | null;
  /**
   * The attempt the link asked for (`?attempt=`), or null for the latest. The
   * run row always speaks for the latest attempt, so when this names another
   * every figure read off the row is labelled with the attempt it is from.
   */
  attempt?: number | null;
};

export type FactRow = { label: string; value: string; available: boolean; detail: string };

export type IdentityView = {
  runId: string;
  attempt: number;
  title: string;
  status: string;
  /** `#1525` for a GitHub number, bare `SFD-12` for any other tracker. */
  itemRef: string;
  tracker: string;
  sourceId: string;
  branch: string;
  /** The canonical link *to* this view — what the runs list points at. */
  selfHref: string;
  /** The one outbound link the contract carries. Never synthesized. */
  deepLink: { href: string; label: string } | null;
};

export type RouteView = {
  claimed: string;
  claimedProvenance: string;
  reported: string;
  reportedAvailable: boolean;
  attested: boolean;
  detail: string;
};

export type ShaCheck = {
  label: string;
  expected: string;
  actual: string;
  status: 'match' | 'mismatch' | 'unavailable';
  detail: string;
};

export type DiffFileView = {
  path: string;
  change: string;
  mode: string;
  digest: string;
  byteLength: number;
  text: string | null;
  textAvailability: string;
  detail: string;
};

export type DiffView = {
  /** "6 files" — what a closed Changes disclosure says. */
  summary: string;
  available: boolean;
  detail: string;
  nodeId: string;
  verification: string;
  files: DiffFileView[];
  text: string;
};

export type RunnerMetric = {
  metric: string;
  value: string;
  producer: string;
  attested: boolean;
};

/** One row of the cost table: the figure, its value, and where it came from. */
export type CostFigure = { figure: string; value: string; available: boolean; source: string };

export type CostView = {
  amount: string;
  basis: 'host_priced' | 'free_local' | 'unavailable';
  /** The section head's meta, which the basis decides. */
  meta: string;
  /** How the amount was arrived at, in the basis's own words. */
  pricing: string;
  /** Every figure the page prints, each naming its source. */
  figures: CostFigure[];
  hostTokens: FactRow[];
  runnerReported: RunnerMetric[];
  /** The one sentence for an attempt the runner reported nothing about, else ''. */
  runnerSilent: string;
  detail: string;
};

export type GapView = {
  seq: number | null;
  event: string;
  reason: string;
  kind: 'gap' | 'dimension';
};

export type SafestAction = {
  action: string;
  reason: string;
  blockedBy: string | null;
  /** The command that performs `action`, or '' when no command does. */
  command: string;
};

/** A link beside a safest action: out to the forge, or to a part of this page. */
export type SafestLink = { href: string; label: string; external: boolean };

export type PromptStatus =
  | 'collapsed'
  | 'loading'
  | 'revealed'
  | 'unavailable'
  | 'refused'
  | 'missing'
  | 'error';

export type PromptPanel = {
  status: PromptStatus;
  digest: string;
  byteLength: number;
  storage: string;
  expiresAt: string | null;
  expired: boolean;
  /** The exact bytes — present ONLY after an explicit reveal answered with
   * them, and dropped again by `collapsePrompt`. Never populated by a load. */
  text: string | null;
  detail: string;
};

export type EvidenceView = {
  events: FlightEvent[];
  sealed: boolean;
  chainVerified: boolean;
  telemetry: string;
  throughSeq: number | null;
};

export type FlightView = {
  identity: IdentityView;
  route: RouteView;
  lineage: FactRow[];
  checks: ShaCheck[];
  diff: DiffView;
  cost: CostView;
  gaps: GapView[];
  evidence: EvidenceView;
  /** "sealed · chain verified · 4/4 checks match · no gaps". */
  evidenceSummary: string;
  /**
   * `attempt 2` when the link asked for an earlier attempt than the run row's,
   * so status, cost, provider, verdict and the safest action say which attempt
   * they come from; '' when the row is the attempt on the page.
   */
  rowAttempt: string;
  safestAction: SafestAction;
  /** Where the safest action is done, when no command performs it. */
  safestLink: SafestLink | null;
  /**
   * The header's sentence when the run and its publication disagree — "Run
   * succeeded · publication blocked" — so a `success` mark never sits alone
   * over a safest action that is blocked (#2541, D37). '' when they agree.
   */
  outcome: string;
  prompt: PromptPanel;
};

/** The canonical deep link for one run. The runs list points here. */
export const runHref = (runId: string) => `/runs/${encodeURIComponent(runId)}`;

const fmtUSD = (n: number) => '$' + n.toFixed(2);
const fmtInt = (n: number) => new Intl.NumberFormat('en-US').format(n);
const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? '' : 's'}`;
const str = (value: unknown) => (value == null ? '' : String(value));

function dimension(
  flight: FlightTimeline | null,
  name: string
): EvidenceAvailability | undefined {
  return flight?.availability.find((row) => row.dimension === name);
}

/** The server's own words for why a dimension is missing, or a fallback. */
function whyMissing(flight: FlightTimeline | null, name: string, fallback: string): string {
  const row = dimension(flight, name);
  if (row === undefined || row.availability === 'available') return fallback;
  return row.detail || fallback;
}

function fact(flight: FlightTimeline | null, name: string): FlightEvent | undefined {
  return flight?.events.find((e) => e.facts.fact === name);
}

// --- identity ---------------------------------------------------------------

function identityOf(sources: FlightSources): IdentityView {
  const { run, flight } = sources;
  const runId = sources.runId || run?.run_id || flight?.run_id || '';
  // 0 is the non-GitHub sentinel, not issue zero: a Linear run must not be
  // rendered `#0`, and nothing here invents a number a tracker never issued.
  const num = flight?.issue_num ?? (run?.issue_num || null);
  const ref = itemRef(flight?.item_id ?? null, num);
  return {
    runId,
    attempt: flight?.attempt_number || run?.attempt_number || 1,
    title: run?.title ?? '',
    status: run?.status ?? '',
    itemRef: ref,
    tracker: flight?.source_tracker ?? (/^#\d+$/.test(ref) ? 'github' : 'unknown'),
    sourceId: flight?.source_id ?? flight?.item_id ?? '',
    branch: run?.branch ?? '',
    selfHref: runHref(runId),
    deepLink: safeHref(run?.pr_url)
      ? { href: safeHref(run?.pr_url)!, label: `pull request${run?.pr_state ? ` · ${run.pr_state}` : ''}` }
      : null
  };
}

// --- route claim ------------------------------------------------------------

function routeOf(sources: FlightSources): RouteView {
  const { run, flight } = sources;
  const reported = flight?.events.filter((e) => e.producer_kind === 'runner') ?? [];
  const last = reported[reported.length - 1];
  return {
    claimed: run ? `${run.runner} / ${run.model}` : '',
    claimedProvenance: 'host record at dispatch',
    reported: last ? `${last.producer_name} ${last.producer_version}`.trim() : '',
    reportedAvailable: last !== undefined,
    // A runner's account of its own work is evidence of a claim, never a host
    // attestation — the ledger stamps that on every imported row.
    attested: last?.attested_by_host ?? false,
    detail: last
      ? 'imported from the runner stream; not host-attested'
      : `the runner contributed no evidence of its own (telemetry: ${
          flight?.runner_telemetry ?? 'unavailable'
        })`
  };
}

// --- artifact lineage -------------------------------------------------------

function lineageOf(sources: FlightSources): FactRow[] {
  const { flight, diff } = sources;
  const missing = whyMissing(flight, DIM_ARTIFACT_SEAL, NOT_SEALED);
  const pairs: [string, string][] = [
    ['base commit', str(diff?.base_sha)],
    ['tree', str(diff?.tree_sha)],
    ['package digest', str(diff?.package_digest)],
    ['receipt digest', str(diff?.receipt_digest)],
    ['verification', str(diff?.verification)],
    ['sealed through', flight?.through_seq == null ? '' : String(flight.through_seq)],
    ['chain head', str(flight?.chain_head)]
  ];
  return pairs.map(([label, value]) => ({
    label,
    value,
    available: value !== '',
    detail: value === '' ? missing : ''
  }));
}

// --- exact-SHA checks -------------------------------------------------------

function compare(
  label: string,
  expected: string,
  actual: string,
  detail: string
): ShaCheck {
  if (!expected || !actual) {
    return { label, expected, actual, status: 'unavailable', detail };
  }
  return {
    label,
    expected,
    actual,
    status: expected === actual ? 'match' : 'mismatch',
    detail: expected === actual ? '' : detail
  };
}

function trustedBaseCheck(sources: FlightSources): ShaCheck {
  const attested = str(fact(sources.flight, TRUSTED_BASE_FACT)?.facts.value);
  return compare(
    'trusted base',
    attested,
    str(sources.diff?.base_sha),
    attested
      ? 'the sealed package is not built on the base the host attested at launch'
      : 'no launch recorded a trusted base for this attempt'
  );
}

function sealedPrefixCheck(flight: FlightTimeline | null): ShaCheck {
  const through = flight?.through_seq ?? null;
  const label = 'sealed prefix';
  if (flight === null || through === null) {
    return {
      label,
      expected: '',
      actual: '',
      status: 'unavailable',
      detail: NOT_SEALED
    };
  }
  const inside = flight.events.filter((e) => e.seq <= through);
  const unsealed = inside.filter((e) => !e.sealed);
  return {
    label,
    expected: `every event through ${through}`,
    actual: unsealed.length
      ? `${unsealed.length} of ${inside.length} are not sealed`
      : `${inside.length} sealed`,
    status: unsealed.length ? 'mismatch' : 'match',
    detail: unsealed.length
      ? 'an event inside the receipt prefix is not covered by the seal'
      : ''
  };
}

function chainCheck(flight: FlightTimeline | null): ShaCheck {
  const ledger = dimension(flight, DIM_HOST_SEQUENCE);
  if (flight === null || ledger?.availability === 'unavailable') {
    return {
      label: 'evidence chain',
      expected: 'verified',
      actual: '',
      status: 'unavailable',
      detail: whyMissing(flight, DIM_HOST_SEQUENCE, 'the host sequence could not be read')
    };
  }
  return compare(
    'evidence chain',
    'verified',
    flight.chain_verified ? 'verified' : 'not verified',
    'the host sequence did not replay to its recorded head'
  );
}

function checksOf(sources: FlightSources): ShaCheck[] {
  return [
    trustedBaseCheck(sources),
    compare(
      'exact tree',
      EXACT_TREE,
      str(sources.diff?.verification),
      'the artifact was not sealed under an exact-tree guarantee'
    ),
    sealedPrefixCheck(sources.flight),
    chainCheck(sources.flight)
  ];
}

// --- sealed diff ------------------------------------------------------------

function diffOf(sources: FlightSources): DiffView {
  const { flight, diff } = sources;
  if (diff === null) {
    return {
      available: false,
      detail: whyMissing(
        flight,
        DIM_ARTIFACT_DIFF,
        flight?.sealed ? 'the sealed package could not be read' : NOT_SEALED
      ),
      nodeId: str(flight?.artifact_node_id),
      verification: '',
      files: [],
      text: '',
      summary: 'no sealed change set'
    };
  }
  return {
    available: true,
    summary: plural((diff.files ?? []).length, 'file'),
    detail: '',
    nodeId: diff.node_id,
    verification: str(diff.verification),
    files: (diff.files ?? []).map((f) => ({
      path: f.path,
      change: f.change,
      mode: f.mode,
      digest: f.digest,
      byteLength: f.byte_length,
      text: f.text,
      textAvailability: f.text_availability,
      detail: f.detail
    })),
    text: diff.text
  };
}

// --- cost provenance --------------------------------------------------------

const NOT_RECORDED = 'not recorded';

/** Where each figure came from, as the cost table names it. */
const SOURCE_HOST = 'host-metered';
const SOURCE_PLANNED = 'planned';

/** The sentence under the amount, per basis: never the hosted one for every run. */
const PRICING: Record<CostView['basis'], string> = {
  host_priced: "priced by kew from the host-metered usage at the provider's rates",
  free_local: 'a local model: kew meters its tokens and prices them at nothing',
  unavailable: 'no run row was read, so kew has no usage to price'
};

const COST_META: Record<CostView['basis'], string> = {
  host_priced: 'host-priced',
  free_local: 'local · unpriced',
  unavailable: 'unavailable'
};

/**
 * The host meter's counts. A count the row does not carry is "not recorded",
 * never 0: a run written before the meter existed metered nothing, which is not
 * the same fact as a run that used no tokens.
 */
function hostTokens(run: Run | null): FactRow[] {
  if (run === null) return [];
  const pairs: [string, number | null | undefined][] = [
    ['input', run.input_tokens],
    ['output', run.output_tokens],
    ['cache read', run.cache_read_tokens],
    ['cache write', run.cache_creation_tokens]
  ];
  return pairs.map(([label, n]) => ({
    label,
    value: n == null ? NOT_RECORDED : fmtInt(n),
    available: n != null,
    detail: ''
  }));
}

function runnerSilence(run: Run | null): string {
  return run === null
    ? 'the runner reported no usage, and no host meter was read'
    : 'the runner reported no usage; the host meter is the only source';
}

/**
 * The cost table's rows. `rowAttempt` ("attempt 2", or '') rides on the source
 * of every figure read off the run row, which speaks for the latest attempt:
 * the runner's figures are the requested attempt's own evidence and carry none.
 */
function costFigures(
  run: Run | null,
  host: FactRow[],
  runner: RunnerMetric[],
  rowAttempt: string
): CostFigure[] {
  const planned = run?.planned_cost_usd ?? null;
  const fromRow = (source: string) => (rowAttempt ? `${source} · ${rowAttempt}` : source);
  return [
    ...host.map((t) => ({
      figure: `${t.label} tokens`,
      value: t.value,
      available: t.available,
      source: fromRow(SOURCE_HOST)
    })),
    ...runner.map((m) => ({
      // `tool_uses` → "tool uses": the table prints words, never a metric id.
      figure: eventWords(m.metric),
      value: m.value,
      available: true,
      source: `${m.attested ? 'host-attested' : 'runner claim'}${m.producer ? ` · ${m.producer}` : ''}`
    })),
    ...(run === null
      ? []
      : [
          {
            figure: 'planned cost',
            value: planned === null ? NOT_RECORDED : fmtUSD(planned),
            available: planned !== null,
            source: fromRow(SOURCE_PLANNED)
          }
        ])
  ];
}

function costOf(sources: FlightSources, rowAttempt: string): CostView {
  const { run, flight } = sources;
  const usage = (flight?.events ?? []).filter((e) => e.kind === 'evidence.runner_usage');
  const basis: CostView['basis'] =
    run === null ? 'unavailable' : run.channel === 'local' ? 'free_local' : 'host_priced';
  const host = hostTokens(run);
  const runnerReported = usage.map((e) => ({
    metric: str(e.facts.metric),
    value: `${str(e.facts.value)} ${str(e.facts.unit)}`.trim(),
    producer: `${e.producer_name} ${e.producer_version}`.trim(),
    attested: e.attested_by_host
  }));
  return {
    basis,
    meta: COST_META[basis],
    pricing: PRICING[basis],
    amount:
      basis === 'unavailable'
        ? 'unavailable'
        : basis === 'free_local'
          ? 'free · local'
          : fmtUSD(run?.cost_usd ?? 0),
    figures: costFigures(run, host, runnerReported, rowAttempt),
    hostTokens: host,
    runnerReported,
    runnerSilent: usage.length ? '' : runnerSilence(run),
    detail: usage.length ? '' : whyMissing(flight, DIM_RUNNER_USAGE, '')
  };
}

// --- gaps -------------------------------------------------------------------

function gapsOf(flight: FlightTimeline | null): GapView[] {
  if (flight === null) return [];
  const unread: GapView[] = flight.gaps.map((g) => ({
    seq: g.seq,
    event: g.event,
    reason: g.reason,
    kind: 'gap'
  }));
  const missing: GapView[] = flight.availability
    .filter((row) => row.availability !== 'available')
    .map((row) => ({
      seq: null,
      event: row.dimension,
      reason:
        row.detail ||
        (row.provenance === 'unobserved'
          ? 'nothing was ever recorded'
          : 'something was recorded and cannot be trusted'),
      kind: 'dimension'
    }));
  return [...unread, ...missing];
}

// --- safest action ----------------------------------------------------------

/** The ledger event the host writes when it could not publish a run's work. */
const PUBLICATION_DEFERRED = 'publication_deferred';

/** Events that settle a deferral: the preserved work reached the forge. */
const PUBLICATION_SETTLED = new Set(['publication_finalized', 'publication_reconciled']);

/** What a deferred publication is blocked on, for the head's "blocked by". */
const DIM_PUBLICATION = 'publication';

/**
 * The one run status `kew finalize` accepts without a live publication job
 * (`finalize_publication_run` in `kew/service/verbs.py`). Any other status is
 * refused with "not awaiting publication", so the command is offered only here.
 */
const AWAITING_PUBLICATION = 'awaiting_publication';

/**
 * The latest `publication_deferred` event no later event settled, or null.
 * Events are read in ledger order (`seq`), whatever order they arrived in.
 */
function standingDeferral(events: RunAuditEvent[] | null | undefined): RunAuditEvent | null {
  let standing: RunAuditEvent | null = null;
  for (const e of [...(events ?? [])].sort((a, b) => a.seq - b.seq)) {
    if (e.event === PUBLICATION_DEFERRED) standing = e;
    else if (PUBLICATION_SETTLED.has(e.event)) standing = null;
  }
  return standing;
}

/**
 * The action a deferred publication leaves (#3057). The host preserved the
 * work and publishing that same artifact is what remains — the server's own
 * verdict for this state is `finalize_publication`, performed by
 * `kew finalize <run>` (`SAFEST_COMMAND` in `kew/service/needs_you.py`). The
 * reason is the deferral's own words, so the action never reads past the
 * event drawn above it.
 */
function deferralAction(event: RunAuditEvent, runId: string): SafestAction {
  return {
    action: 'publish the preserved artifact this run already produced',
    reason: deferralReason(event),
    blockedBy: DIM_PUBLICATION,
    command: runId ? `kew finalize ${runId}` : ''
  };
}

/** The deferral's own words: where the host stopped, and why. */
function deferralReason(event: RunAuditEvent): string {
  const payload = event.payload ?? {};
  const step = str(payload.step).trim();
  const error = str(payload.error).trim();
  const where = step ? `the host deferred publication at ${step}` : 'the host deferred publication';
  return error ? `${where}: ${error}` : where;
}

/**
 * A deferral a normalized publication job owns leaves the run status alone
 * (`_defer_onto_job` in `kew/dispatch_core.py`), and the job's state is not on
 * this page — so whether the worker since settled it is unknown here, and no
 * command is offered for it.
 */
function jobDeferralAction(event: RunAuditEvent): SafestAction {
  return {
    action: "find out whether this run's publication job still holds its work",
    reason: deferralReason(event),
    blockedBy: DIM_PUBLICATION,
    command: ''
  };
}

/**
 * What a ledger deferral still asks of the reader, or null once the run has
 * moved past it. The run's status is the server's record of whether the
 * deferral is still open: `awaiting_publication` is the state `kew finalize`
 * settles. A job-owned deferral keeps the run's status, so it stands only
 * while no pull request shows the work already reached the forge.
 */
function deferralActionOf(event: RunAuditEvent | null, run: Run | null, runId: string): SafestAction | null {
  if (event === null || run === null) return null;
  if (run.status === AWAITING_PUBLICATION) return deferralAction(event, runId);
  if (str(event.payload?.job_id).trim() && !run.pr_url) return jobDeferralAction(event);
  return null;
}

/**
 * The run kinds that produce no artifact, so an unsealed run of one is not
 * blocked on a seal: there is nothing to seal. kew seals an artifact receipt
 * only on dispatch_core's path (`artifact_receipt.seal_package_receipt`, via
 * `dispatch_core` and `no_change_seal`), and these are exactly the kinds kew
 * never publishes for (`publication_credentials._NON_PUBLICATION_RUN_KINDS`),
 * mirrored in `no-artifact-run-kinds.json`, which
 * `kew/tests/test_no_artifact_run_kinds.py` holds equal to that set. Every
 * other kind — `dispatch`, `review_remediation` and the rest — keeps the seal
 * rule below unchanged.
 */
export const NO_ARTIFACT_RUN_KINDS: ReadonlySet<string> = new Set(noArtifactKinds.kinds);

/** What the sentence calls a run of each such kind, in the console's plain names. */
const NO_ARTIFACT_NOUN: Record<string, string> = {
  planning: `${runKindLabel('planning')} run`,
  readiness_probe: runKindLabel('readiness_probe'),
  triage: `${runKindLabel('triage')} run`
};

/**
 * The one next action for a run of a kind that publishes nothing, whatever its
 * status. A review run points at its verdict only when the page shows the
 * run's OWN round (scope `run`); one still running, one that failed, or one
 * the page can only show the work item's standing verdict for has none below.
 */
function noArtifactAction(run: Run): SafestAction {
  const kind = run.run_kind ?? '';
  let action: string;
  if (kind === 'adversarial_review') {
    const own = run.verdict?.scope === VERDICT_SCOPE_RUN;
    action = `Nothing to do on this run: a review run publishes nothing.${own ? ' Its verdict is below.' : ''}`;
  } else {
    action = `Nothing to do on this run: a ${NO_ARTIFACT_NOUN[kind] ?? `${runKindLabel(kind)} run`} publishes nothing.`;
  }
  return { action, reason: '', blockedBy: null, command: '' };
}

/**
 * The ONE next action for this run, derived only from the canonical Flight
 * contract and only about the evidence on this page.
 *
 * `blockedBy` names the dimension the decision depends on when the honest
 * answer is "find out" — recommending the optimistic action off a dimension
 * that could not be read would be a guess. This deliberately mirrors
 * `SafestActionDTO`: when the work projection reaches the API (I19), this page
 * should render the server's resolver and this function should go, rather than
 * become a second opinion competing with it.
 */
function safestActionOf(sources: FlightSources): SafestAction {
  const { run, flight, diff } = sources;
  if (run === null && flight === null) {
    return {
      action: 'go back to the runs list',
      reason: 'this server has no run with that id',
      blockedBy: 'identity',
      command: ''
    };
  }
  if (flight === null) {
    return {
      action: 'find out why this run has no readable evidence',
      reason: 'the evidence projection could not be read from this server',
      blockedBy: DIM_HOST_SEQUENCE,
      command: ''
    };
  }
  if (!flight.chain_verified) {
    return {
      action: 'verify the audit chain before trusting anything on this page',
      reason: 'the host sequence did not replay to its recorded head',
      blockedBy: DIM_HOST_SEQUENCE,
      command: ''
    };
  }
  if (flight.gaps.length > 0) {
    return {
      action: 'read the evidence gaps before acting on this run',
      reason: `${flight.gaps.length} ledger row(s) claim to be evidence and could not be read`,
      blockedBy: DIM_HOST_SEQUENCE,
      command: ''
    };
  }
  const deferred = deferralActionOf(
    standingDeferral(sources.events),
    run,
    run?.run_id || flight.run_id
  );
  if (deferred !== null) return deferred;
  if (run && NO_ARTIFACT_RUN_KINDS.has(run.run_kind ?? '')) return noArtifactAction(run);
  if (!flight.sealed) {
    return {
      action: 'wait — nothing is sealed for this attempt yet',
      reason: whyMissing(flight, DIM_ARTIFACT_SEAL, 'no receipt covers this attempt'),
      blockedBy: DIM_ARTIFACT_SEAL,
      command: ''
    };
  }
  if (diff === null) {
    return {
      action: 'find out why the sealed package could not be read',
      reason: whyMissing(flight, DIM_ARTIFACT_DIFF, 'the sealed package could not be opened'),
      blockedBy: DIM_ARTIFACT_DIFF,
      command: ''
    };
  }
  if (run?.pr_url) return { ...pullRequestAction(run.pr_state), blockedBy: null, command: '' };
  return {
    action: 'review the sealed change set',
    reason: 'the artifact is sealed and the chain verified; nothing is published yet',
    blockedBy: null,
    command: ''
  };
}

/** The pull request's recorded state, or '' when the ledger recorded none it names. */
export type PullRequestState = 'open' | 'merged' | 'closed' | '';

/** `pr_state` as kew writes it (GitHub's OPEN / MERGED / CLOSED), or ''. */
export function pullRequestStateOf(state: string | null | undefined): PullRequestState {
  const s = (state ?? '').trim().toLowerCase();
  return s === 'open' || s === 'merged' || s === 'closed' ? s : '';
}

const SEALED = 'the artifact is sealed, the chain verified and';

/**
 * The safest action for a sealed run with a pull request, chosen by the state
 * the ledger last recorded for it (design review c9).
 *
 * The page used to ask for a review whatever the state: beside a merged pull
 * request that review comes after the fact, and beside one closed without a
 * merge it is a move with no point. Only an open one asks anything of a
 * person. The state is a cached reading (`_pr_state` in kew/service/work.py
 * treats it as possibly stale), so an open one is said to be open "when last
 * read", and a state the ledger did not record is never assumed open.
 */
export function pullRequestAction(state: string | null | undefined): { action: string; reason: string } {
  switch (pullRequestStateOf(state)) {
    case 'open':
      return {
        action: 'review the pull request against this sealed change set',
        reason: `${SEALED} its pull request was open when last read`
      };
    case 'merged':
      return {
        action: 'nothing to do: this sealed change set is what landed',
        reason: `${SEALED} its pull request was merged`
      };
    case 'closed':
      return {
        action: 'nothing to do: the pull request was closed without a merge, and nothing is waiting on this run',
        reason: `${SEALED} its pull request was closed without a merge`
      };
    default:
      return {
        action: 'open the pull request to see where this sealed change set stands',
        reason: 'the artifact is sealed and the chain verified; the pull request’s state is not recorded'
      };
  }
}

/**
 * Where the reader goes to do the safest action when no command does it: the
 * pull request, the part of this page the action names, or back to the list.
 * Null for an action that carries a command (the command is the way) and for
 * one that names nothing a link could reach.
 */
function safestLinkOf(sources: FlightSources, safest: SafestAction): SafestLink | null {
  if (safest.command) return null;
  const local = (href: string, label: string) => ({ href, label, external: false });
  if (sources.run === null && sources.flight === null) return local('/runs', 'Runs');
  if (safest.blockedBy === DIM_HOST_SEQUENCE) return sources.flight ? local('#evidence', 'Evidence') : null;
  if (safest.blockedBy === DIM_ARTIFACT_DIFF) return local('#changes', 'Changes');
  if (safest.blockedBy !== null) return null;
  // A run that publishes nothing has nothing to review but its own verdict.
  const run = sources.run;
  if (run && NO_ARTIFACT_RUN_KINDS.has(run.run_kind ?? '')) {
    return run.verdict?.scope === VERDICT_SCOPE_RUN ? local('#review', 'Review') : null;
  }
  if (sources.diff === null) return null;
  const pr = safeHref(sources.run?.pr_url);
  if (!pr) return local('#changes', 'Changes');
  // Always a quiet link beside the sentence, never the primary (the page draws
  // magenta only for a command). With no recorded state, opening it IS the
  // move, and the link says so.
  const unknown = pullRequestStateOf(sources.run?.pr_state) === '';
  return { href: pr, label: unknown ? 'open the pull request' : 'pull request', external: true };
}

/**
 * The run's own outcome and its publication's, joined, when they disagree: a
 * run that succeeded while the next step is blocked reads "Run succeeded ·
 * publication blocked" rather than a bare `success`. '' when nothing disagrees.
 */
export function outcomeOf(status: string | null | undefined, safest: SafestAction): string {
  if ((status ?? '').trim().toLowerCase() !== 'success' || safest.blockedBy === null) return '';
  return 'Run succeeded · publication blocked';
}

// --- protected prompt -------------------------------------------------------

const EMPTY_PROMPT: PromptPanel = {
  status: 'missing',
  digest: '',
  byteLength: 0,
  storage: '',
  expiresAt: null,
  expired: false,
  text: null,
  detail: 'this run retained no prompt'
};

/**
 * The digest-only description of the run's prompt, from the artifact evidence
 * row — no bytes, and no network call. Revealing the bytes is a separate,
 * explicitly-clicked read (`fetchPrompt`), never part of building this view.
 */
function promptOf(flight: FlightTimeline | null, now: Date): PromptPanel {
  const rows = (flight?.events ?? []).filter(
    (e) => e.facts.artifact_kind === PROMPT_ARTIFACT_KIND
  );
  const row = rows[rows.length - 1];
  if (row === undefined) return { ...EMPTY_PROMPT };
  const expiresAt = row.facts.expires_at == null ? null : str(row.facts.expires_at);
  const expired = expiresAt !== null && new Date(expiresAt).getTime() <= now.getTime();
  const storage = str(row.facts.storage);
  const retained = !expired && storage !== 'unavailable';
  return {
    status: retained ? 'collapsed' : 'unavailable',
    digest: str(row.facts.digest),
    byteLength: Number(row.facts.byte_length ?? 0),
    storage,
    expiresAt,
    expired,
    text: null,
    detail: expired ? `the retention window closed at ${expiresAt}` : ''
  };
}

/** Whether the operator may ask for the bytes at all. */
export function canReveal(panel: PromptPanel): boolean {
  return panel.status === 'collapsed' || panel.status === 'refused' || panel.status === 'error';
}

/** The panel while the explicit read is in flight. */
export const loadingPrompt = (panel: PromptPanel): PromptPanel => ({
  ...panel,
  status: 'loading',
  text: null
});

/**
 * Fold a protected read into the panel. A response whose bytes retention has
 * removed is *unavailable with provenance*, never an empty prompt: "there was
 * never one" and "it was purged" are different facts about the run.
 */
export function revealedPrompt(panel: PromptPanel, dto: ProtectedPrompt): PromptPanel {
  const merged: PromptPanel = {
    ...panel,
    digest: dto.digest || panel.digest,
    byteLength: dto.byte_length || panel.byteLength,
    storage: dto.storage || panel.storage,
    expiresAt: dto.expires_at ?? panel.expiresAt
  };
  if (dto.availability === 'available' && dto.text != null) {
    return { ...merged, status: 'revealed', text: dto.text, detail: dto.detail };
  }
  return {
    ...merged,
    status: 'unavailable',
    text: null,
    detail: dto.detail || dto.purge_reason || 'the bytes are no longer retained'
  };
}

/** Explain a refusal from the protected route without ever holding bytes. */
export function refusedPrompt(panel: PromptPanel, err: unknown): PromptPanel {
  const message = err instanceof Error ? err.message : String(err);
  const status = /-> (\d{3})$/.exec(message)?.[1] ?? '';
  const refusals: Record<string, [PromptStatus, string]> = {
    '403': ['refused', 'prompt reads are disabled while kew serves exposed'],
    '401': ['refused', 'this read needs a signed-in session — sign in from the inbox'],
    '404': ['missing', 'this run retained no prompt']
  };
  const [next, detail] = refusals[status] ?? [
    'error' as PromptStatus,
    `the protected read failed (${status || 'no response'})`
  ];
  return { ...panel, status: next, text: null, detail };
}

/** Drop the bytes. The panel keeps its digest-only description. */
export const collapsePrompt = (panel: PromptPanel): PromptPanel => ({
  ...panel,
  status: 'collapsed',
  text: null,
  detail: ''
});

// --- summaries --------------------------------------------------------------

/** "evidence.runner_usage" → "runner usage": a host-sequence kind in words. */
export function eventKindLabel(kind: string): string {
  return kind.replace(/^evidence\./, '').replaceAll('_', ' ');
}

function evidenceSummaryOf(flight: FlightTimeline | null, checks: ShaCheck[], gaps: GapView[]): string {
  const matched = checks.filter((c) => c.status === 'match').length;
  return [
    flight?.sealed ? 'sealed' : 'unsealed',
    flight?.chain_verified ? 'chain verified' : 'chain unverified',
    `${matched}/${checks.length} checks match`,
    gaps.length ? plural(gaps.length, 'gap') : 'no gaps'
  ].join(' · ');
}

/**
 * What a shut Compiled prompt disclosure says: whether a prompt was recorded
 * and how big it is, never the panel's internal status word.
 */
export function promptSummary(panel: PromptPanel): string {
  const size = panel.byteLength ? `prompt recorded, ${fmtSize(panel.byteLength)}` : 'prompt recorded';
  // `missing` also covers a recorded prompt whose read came back not found:
  // its digest says one was recorded, so the line must not deny it.
  if (panel.status === 'missing') return panel.digest ? `${size}; it could not be read` : 'no prompt was recorded for this run';
  if (panel.status === 'unavailable') return `${size}; its bytes are no longer kept`;
  return panel.status === 'revealed' ? `${size}, shown below` : size;
}

/** 8123 → "8.1 kB"; under a kilobyte, the bytes. */
const fmtSize = (n: number) => (n < 1000 ? plural(n, 'byte') : `${(n / 1000).toFixed(1)} kB`);

/** `attempt N` when the run row is not the attempt the link asked for, else ''. */
function rowAttemptOf(sources: FlightSources): string {
  const { run, attempt } = sources;
  if (run === null || attempt == null || !run.attempt_number) return '';
  return run.attempt_number === attempt ? '' : `attempt ${run.attempt_number}`;
}

// --- the view ---------------------------------------------------------------

/** Project the three canonical reads into one deep evidence view. */
export function buildFlightView(sources: FlightSources, now: Date = new Date()): FlightView {
  const { flight } = sources;
  const safest = safestActionOf(sources);
  const checks = checksOf(sources);
  const gaps = gapsOf(flight);
  const rowAttempt = rowAttemptOf(sources);
  return {
    identity: identityOf(sources),
    route: routeOf(sources),
    lineage: lineageOf(sources),
    checks,
    diff: diffOf(sources),
    cost: costOf(sources, rowAttempt),
    gaps,
    evidenceSummary: evidenceSummaryOf(flight, checks, gaps),
    rowAttempt,
    evidence: {
      events: flight?.events ?? [],
      sealed: flight?.sealed ?? false,
      chainVerified: flight?.chain_verified ?? false,
      telemetry: flight?.runner_telemetry ?? 'missing',
      throughSeq: flight?.through_seq ?? null
    },
    safestAction: safest,
    safestLink: safestLinkOf(sources, safest),
    outcome: outcomeOf(sources.run?.status, safest),
    prompt: promptOf(flight, now)
  };
}
