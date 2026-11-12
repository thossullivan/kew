/**
 * FUI-02 — the visual-check harness: the fixture API and what it must say.
 *
 * Three things are pinned here.
 *
 * 1. **Every fixture types.** Each payload is imported as a module and
 *    assigned to its `api.ts` type, so `npm run check` (svelte-check) fails on
 *    a field the console's contract does not have — the same way
 *    `surface-convergence.test.ts` reads committed JSON through the real
 *    types rather than a hand-copied shape.
 * 2. **Every fixture says what the artboard draws.** The numbers below come
 *    from `design/factory-ui/{DEpic,DWork,DMonitor}.html`. Where an artboard
 *    prints a placeholder (`[elapsed]`, `[last tick]`), the fixture is EMPTY,
 *    so a screenshot checks the UI's honest empty rendering instead of a
 *    number nobody designed.
 *    The one exception is an age-GATED reading, which a frozen instant turns
 *    into a state the surface can never draw: see "the fixture clock" below.
 * 3. **None of it can reach production.** The plugin is off without
 *    `KEW_UI_FIXTURES=1`, the JSON lives outside `src/` and `static/`, and no
 *    shipped source file mentions it — so there is no path by which a bundle
 *    could contain a fixture string. When a build output is present, it is
 *    grepped for a fixture-only sentinel as well.
 */
import { readFileSync, readdirSync, existsSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import {
  auditHistogramBody,
  auditListBody,
  auditListPage,
  fixtureApi,
  fixtureBody,
  fixtureFileFor,
  fixtureMiddleware,
  isStreamPath,
  rebaseClock,
  FIXTURE_DIR,
  FIXTURE_EPOCH,
  INSTANT,
} from "../../fixtures/plugin";
import { streamWindow } from "./monitor/stream";
import { bucketSecondsFor } from "./monitor/brush";
import { DAY_MS, isoOf, MINUTE_MS } from "./monitor/stream-query";
import { badgesFor, byItem, BASE_READING_MAX_AGE_MS } from "./freshness";
import { displayStatus, runKpis } from "./kpi";
import { BACKEND_RUN_STATUSES, LIVE_STATUSES } from "./run-status";
import { groupRunsByDay } from "./runs-detail";
import { filterRuns } from "./runs-filter";
import { isWorkingRun } from "./shell/header-state";
import { healthBadge, healthHeadline, healthWarnings } from "./health";
import { epicProgress } from "./epic/progress";
import { describeHold } from "./monitor/hold-reasons";
import { composeHolds, holdKindOf } from "./monitor/holds";
import { liveRunRows } from "./monitor/live-runs";
import { needsYouBlock } from "./needs-you/block";
import { itemHeader } from "./work/header";
import { firstRunCard, isFirstRun } from "./work/first-run";
import { mergeWorkList } from "./work/list";
import { needsYouSummary } from "./work/queue";
import {
  focusedRunEvent,
  runEventsHref,
  verdictScopeNote,
  VERDICT_SCOPE_RUN,
} from "./runs/events";
import type {
  AuditHistogram,
  AuditEvent,
  AuditList,
  AuditStatus,
  BudgetMap,
  Bundle,
  Dashboard,
  DashboardRow,
  DispatchPlan,
  EffectiveConfig,
  ModelsPayload,
  OverridableMap,
  EpicDetail,
  EpicNode,
  EpicNodeDetail,
  EpicSummary,
  EpicTick,
  EpicTickStrip,
  EpicTimeline,
  FleetStatus,
  Health,
  HealthEnv,
  PromptPreview,
  PromptRole,
  ProfileVersions,
  PromptOutcomes,
  PromptSourceKind,
  Diff,
  FlightTimeline,
  Hold,
  ItemDescription,
  ItemEvidence,
  ItemPipeline,
  Meta,
  Run,
  RunAuditPage,
  SessionInfo,
  SpendWindows,
  PrFreshness,
  Readiness,
  WorkProjection,
  WorkView,
} from "./api";
import { RUN_AUDIT_PAGE } from "./api";
import type {
  Iteration,
  Loop,
  LoopAuditEvent,
  LoopDetail,
  OutcomeReport,
  Scorecard,
} from "./api";
import type { RunRequest } from "./verbs";
import type { ReviewPolicy } from "./config-page";
import { STATUS_MARKS as SEVERITY_SHAPE } from "./factory/status";
import { tickMarks, tickSlot } from "./factory/tick-strip";
import { batchMarks } from "./time/batch";

import approvalsJson from "../../fixtures/factory/approvals.json";
import auditJson from "../../fixtures/factory/audit.json";
import auditHistogramJson from "../../fixtures/factory/audit-histogram.json";
import auditStatusJson from "../../fixtures/factory/audit-status.json";
import bundleJson from "../../fixtures/factory/bundle.json";
import budgetJson from "../../fixtures/factory/config-budget.json";
import configEffectiveJson from "../../fixtures/factory/config-effective.json";
import configHumanReviewJson from "../../fixtures/factory/config-human_review.json";
import configOverridableJson from "../../fixtures/factory/config-overridable.json";
import modelsJson from "../../fixtures/factory/models.json";
import dashboardJson from "../../fixtures/factory/dashboard.json";
import dispatchPlanJson from "../../fixtures/factory/dispatch-resolve.json";
import epicJson from "../../fixtures/factory/epics-launch-fixes.json";
import goLiveJson from "../../fixtures/factory/epics-go-live-2026-09.json";
import goLiveNodeJson from "../../fixtures/factory/epics-go-live-2026-09-nodes-SFD-229.json";
import goLiveTicksJson from "../../fixtures/factory/epics-go-live-2026-09-ticks.json";
import goLiveTimelineJson from "../../fixtures/factory/epics-go-live-2026-09-timeline.json";
import epicNodeJson from "../../fixtures/factory/epics-launch-fixes-nodes-2055.json";
import epicTicksJson from "../../fixtures/factory/epics-launch-fixes-ticks.json";
import epicStripJson from "../../fixtures/factory/epics-launch-fixes-tick-strip.json";
import goLiveStripJson from "../../fixtures/factory/epics-go-live-2026-09-tick-strip.json";
import epicTimelineJson from "../../fixtures/factory/epics-launch-fixes-timeline.json";
import epicsJson from "../../fixtures/factory/epics.json";
import sweepJson from "../../fixtures/factory/epics-sweep-2026-10.json";
import sweepTicksJson from "../../fixtures/factory/epics-sweep-2026-10-ticks.json";
import sweepStripJson from "../../fixtures/factory/epics-sweep-2026-10-tick-strip.json";
import sweepTimelineJson from "../../fixtures/factory/epics-sweep-2026-10-timeline.json";
import fleetJson from "../../fixtures/factory/fleet-status.json";
import healthJson from "../../fixtures/factory/health.json";
import reportModelsJson from "../../fixtures/factory/report-models.json";
import reportOutcomesJson from "../../fixtures/factory/report-outcomes.json";
import loopsJson from "../../fixtures/factory/loops.json";
import loopDetailJson from "../../fixtures/factory/loops-3b8e1f27-6c4d-4a9e-b2f5-7d0c9e1a4b63.json";
import loopIterationsJson from "../../fixtures/factory/loops-3b8e1f27-6c4d-4a9e-b2f5-7d0c9e1a4b63-iterations.json";
import loopAuditJson from "../../fixtures/factory/loops-3b8e1f27-6c4d-4a9e-b2f5-7d0c9e1a4b63-audit.json";
import healthEnvJson from "../../fixtures/factory/health-env.json";
import readinessJson from "../../fixtures/factory/readiness.json";
import promptsJson from "../../fixtures/factory/prompts.json";
import prompt_dispatchJson from "../../fixtures/factory/prompts-dispatch.json";
import prompt_adversarial_reviewJson from "../../fixtures/factory/prompts-adversarial_review.json";
import prompt_review_remediationJson from "../../fixtures/factory/prompts-review_remediation.json";
import prompt_resolveJson from "../../fixtures/factory/prompts-resolve.json";
import prompt_ci_fixJson from "../../fixtures/factory/prompts-ci_fix.json";
import prompt_triageJson from "../../fixtures/factory/prompts-triage.json";
import prompt_planningJson from "../../fixtures/factory/prompts-planning.json";
import prompt_acceptance_authorJson from "../../fixtures/factory/prompts-acceptance_author.json";
import prompt_loop_continuationJson from "../../fixtures/factory/prompts-loop_continuation.json";
import prompt_readiness_probeJson from "../../fixtures/factory/prompts-readiness_probe.json";
import metaJson from "../../fixtures/factory/meta.json";
import runsJson from "../../fixtures/factory/runs.json";
import runDetailJson from "../../fixtures/factory/runs-f5dcb6a5.json";
import runFlightJson from "../../fixtures/factory/runs-f5dcb6a5-flight.json";
import runAuditJson from "../../fixtures/factory/runs-f5dcb6a5-audit.json";
import runDiffJson from "../../fixtures/factory/runs-f5dcb6a5-nodes-f5dcb6a5_4-diff.json";
import sessionJson from "../../fixtures/factory/auth-session.json";
import spendJson from "../../fixtures/factory/report-spend.json";
import workProjectionJson from "../../fixtures/factory/dashboard-work.json";
import workViewJson from "../../fixtures/factory/verbs-work-2055.json";
import pipelineJson from "../../fixtures/factory/work-2055-pipeline.json";
import evidenceJson from "../../fixtures/factory/work-2055-evidence.json";
import goLiveWorkViewJson from "../../fixtures/factory/verbs-work-SFD-229.json";
import goLivePipelineJson from "../../fixtures/factory/work-SFD-229-pipeline.json";
import goLiveEvidenceJson from "../../fixtures/factory/work-SFD-229-evidence.json";
import goLiveDescriptionJson from "../../fixtures/factory/work-SFD-229-description.json";
import descriptionJson from "../../fixtures/factory/work-2055-description.json";
import idlePipelineJson from "../../fixtures/factory/work-2060-pipeline.json";
import mergedPipelineJson from "../../fixtures/factory/work-2047-pipeline.json";
import mergedEvidenceJson from "../../fixtures/factory/work-2047-evidence.json";
import mergedDescriptionJson from "../../fixtures/factory/work-2047-description.json";
import idleEvidenceJson from "../../fixtures/factory/work-2060-evidence.json";
import idleDescriptionJson from "../../fixtures/factory/work-2060-description.json";
import freshnessJson from "../../fixtures/factory/prs-freshness.json";
import runRequestsJson from "../../fixtures/factory/run-requests.json";
import prompt_dispatch_versionsJson from "../../fixtures/factory/prompts-dispatch-versions.json";
import prompt_adversarial_review_versionsJson from "../../fixtures/factory/prompts-adversarial_review-versions.json";
import prompt_review_remediation_versionsJson from "../../fixtures/factory/prompts-review_remediation-versions.json";
import prompt_resolve_versionsJson from "../../fixtures/factory/prompts-resolve-versions.json";
import prompt_ci_fix_versionsJson from "../../fixtures/factory/prompts-ci_fix-versions.json";
import prompt_dispatch_outcomesJson from "../../fixtures/factory/prompts-dispatch-outcomes.json";
import prompt_adversarial_review_outcomesJson from "../../fixtures/factory/prompts-adversarial_review-outcomes.json";
import prompt_review_remediation_outcomesJson from "../../fixtures/factory/prompts-review_remediation-outcomes.json";
import prompt_resolve_outcomesJson from "../../fixtures/factory/prompts-resolve-outcomes.json";
import prompt_ci_fix_outcomesJson from "../../fixtures/factory/prompts-ci_fix-outcomes.json";
import promptsVersionsScenarioJson from "../../fixtures/scenarios/prompts-versions.json";
import promptsOutcomesScenarioJson from "../../fixtures/scenarios/prompts-outcomes.json";
import soleWaitJson from "../../fixtures/scenarios/dashboard-sole-wait.json";
import firstRunDashboardJson from "../../fixtures/scenarios/dashboard-first-run.json";
import firstRunProjectionJson from "../../fixtures/scenarios/dashboard-work-first-run.json";
import heldRoundDueJson from "../../fixtures/scenarios/dashboard-work-held-round-due.json";
import firstRunRunsJson from "../../fixtures/scenarios/runs-first-run.json";
import firstRunReadinessJson from "../../fixtures/scenarios/readiness-first-run.json";
import hostRestartDashboardJson from "../../fixtures/scenarios/dashboard-launch-fixes-host-restart-repeated.json";
import hostRestartEpicJson from "../../fixtures/scenarios/epics-launch-fixes-host-restart-repeated.json";
import hostRestartRepeatedJson from "../../fixtures/scenarios/dashboard-host-restart-repeated.json";
import mergeQueueStoppedJson from "../../fixtures/scenarios/dashboard-merge-queue-stopped.json";
import hostRestartPreservedJson from "../../fixtures/scenarios/host-restart-preserved-continue.json";
import healthImageMissingJson from "../../fixtures/scenarios/health-image-missing.json";

/** `[string, string][]` is a tuple pair; a JSON import only knows `string[]`. */
const pairs = (edges: string[][]): [string, string][] =>
  edges.map((e) => [e[0], e[1]] as [string, string]);

const meta: Meta = metaJson;
const bundle: Bundle = bundleJson;
const session: SessionInfo = sessionJson;
const fleet: FleetStatus = fleetJson;
const runs: Run[] = runsJson;
/** The four reads `/runs/f5dcb6a5` takes (C3/#2300): the row with the
 * detail-only verdict, the Flight projection, the first forward page of the
 * run's ledger events, and the sealed change set the projection's
 * `artifact_node_id` names (#3057). */
const runDetail: Run = runDetailJson;
const runFlight: FlightTimeline = runFlightJson;
const runAudit: RunAuditPage = runAuditJson;
const runDiff: Diff = runDiffJson;
const spend: SpendWindows = spendJson;
const budget: BudgetMap = budgetJson;
const approvals: Hold[] = approvalsJson;
const audit: AuditList = auditJson;
const auditStatus: AuditStatus = auditStatusJson;
const dashboard: Dashboard = dashboardJson;
/**
 * The health reading every fixture screen's header reads (#3461, D22): the
 * routed images are present, so nothing is down beside the run the header
 * shows working. One UNROUTED image is missing, grey, and counts nowhere.
 */
const health: Health = healthJson;
/**
 * The health reading the gallery's `/health` shot is taken over
 * (`scenarios/health-image-missing.json`). One routed image is missing and one
 * UNROUTED image is missing, which is the pair #2298 AC3 turns on: the first
 * is red and counts in the header, the second is grey and counts nowhere.
 */
const healthImageMissing: Health = healthImageMissingJson;
/**
 * The dispatch launchpad's plan (#2302). One field per source layer, so the
 * fixture draws the rows the artboard draws: a routed tier, an `[agent]`
 * fallback, a default and a cap.
 */
const dispatchPlan: DispatchPlan = dispatchPlanJson;
const healthEnv: HealthEnv = healthEnvJson;
/**
 * The readiness read `/health` takes beside the health read (#3470 D41): the
 * doctor's `route-image` check fails for the one missing routed image and
 * carries its build command as `fix`.
 */
const readiness: Readiness = {
  ...readinessJson,
  checks: readinessJson.checks.map((c) => ({
    ...c,
    status: c.status as Readiness["checks"][number]["status"],
  })),
};
/**
 * `/scorecard`'s two reads (#3461, D23), in `kew.service.models`' shapes:
 * the per-model table counted off `runs.json` the way `compute_model_scorecard`
 * counts (every run's spend, PRs credited to the authoring run), and eight
 * weeks of outcomes ending in the partial week the set is read in.
 */
const reportModels: Scorecard = reportModelsJson;
const reportOutcomes: OutcomeReport = reportOutcomesJson;
/**
 * `/loops`' four reads (#3461, D23): the one loop Health counts as pursuing,
 * its detail, its iteration strip (oldest first) and its audit (newest first).
 */
const LOOP_ID = "3b8e1f27-6c4d-4a9e-b2f5-7d0c9e1a4b63";
const loops: Loop[] = loopsJson;
const loopDetail: LoopDetail = loopDetailJson;
const loopIterations: Iteration[] = loopIterationsJson;
const loopAudit: LoopAuditEvent[] = loopAuditJson;

/** The prompt catalog (#3019), and each role's preview, rendered by the
 * backend's own builders over its fixed sample item (`kew.prompt_previews`). */
function withSourceKind<T extends { source: { kind: string } }>(
  read: T,
): T & { source: T["source"] & { kind: PromptSourceKind } } {
  return {
    ...read,
    source: { ...read.source, kind: read.source.kind as PromptSourceKind },
  };
}
const prompts: PromptRole[] = promptsJson.map(withSourceKind);
const promptPreviews: Record<string, PromptPreview> = {
  "dispatch": withSourceKind(prompt_dispatchJson),
  "adversarial_review": withSourceKind(prompt_adversarial_reviewJson),
  "review_remediation": withSourceKind(prompt_review_remediationJson),
  "resolve": withSourceKind(prompt_resolveJson),
  "ci_fix": withSourceKind(prompt_ci_fixJson),
  "triage": withSourceKind(prompt_triageJson),
  "planning": withSourceKind(prompt_planningJson),
  "acceptance_author": withSourceKind(prompt_acceptance_authorJson),
  "loop_continuation": withSourceKind(prompt_loop_continuationJson),
  "readiness_probe": withSourceKind(prompt_readiness_probeJson),
};
/**
 * Each profile role's version history (#3025): empty, as the committed
 * previews carry no profile (`variant.profile` is `none`).
 */
const profileVersions: Record<string, ProfileVersions> = {
  "dispatch": prompt_dispatch_versionsJson as ProfileVersions,
  "adversarial_review": prompt_adversarial_review_versionsJson as ProfileVersions,
  "review_remediation": prompt_review_remediation_versionsJson as ProfileVersions,
  "resolve": prompt_resolve_versionsJson as ProfileVersions,
  "ci_fix": prompt_ci_fix_versionsJson as ProfileVersions,
};
/** Each profile role's outcomes per version (#3026), from the real API. */
const profileOutcomes: Record<string, PromptOutcomes> = {
  "dispatch": prompt_dispatch_outcomesJson as PromptOutcomes,
  "adversarial_review": prompt_adversarial_review_outcomesJson as PromptOutcomes,
  "review_remediation": prompt_review_remediation_outcomesJson as PromptOutcomes,
  "resolve": prompt_resolve_outcomesJson as PromptOutcomes,
  "ci_fix": prompt_ci_fix_outcomesJson as PromptOutcomes,
};
/**
 * The answers `ui-checks/prompts-outcomes.mjs` serves (#3026), from the real
 * API over one seeded store: three dispatch versions and the built-in prompt,
 * one under ten items, and the reviewer's verdicts, each beside the history
 * and preview the same store served (one generation per role); and the
 * dispatch table before and after v1 is approved, which adds v1's zero row.
 */
type OutcomesTransition = { before: PromptOutcomes; after: PromptOutcomes };
type OutcomesScenario = {
  dispatch: PromptOutcomes;
  adversarial_review: PromptOutcomes;
  approval: OutcomesTransition;
  history: Record<"dispatch" | "adversarial_review", ProfileVersions>;
  preview: Record<"dispatch" | "adversarial_review", PromptPreview>;
};
const promptsOutcomesScenario: OutcomesScenario = promptsOutcomesScenarioJson as OutcomesScenario;
/**
 * The answers `ui-checks/prompts-write.mjs` serves (#3025), from the real API:
 * the history before, after a proposal and after its approval, the preview
 * an approval leaves, and the 403 and 409 refusals.
 */
type ProfileRefusal = { status: number; body: { detail: string } };
const promptsVersionsScenario: {
  empty: ProfileVersions;
  proposed: ProfileVersions;
  approved: ProfileVersions;
  preview_approved: PromptPreview;
  approve_refused: ProfileRefusal;
  approve_stale: ProfileRefusal;
} = {
  ...(promptsVersionsScenarioJson as unknown as {
    empty: ProfileVersions;
    proposed: ProfileVersions;
    approved: ProfileVersions;
    approve_refused: ProfileRefusal;
    approve_stale: ProfileRefusal;
  }),
  preview_approved: withSourceKind(promptsVersionsScenarioJson.preview_approved),
};
const effectiveConfig: EffectiveConfig = configEffectiveJson;
/**
 * The `/config` page's human-review policy read (#2846). Generation 3 and its
 * expiry match `config-effective.json`'s `human_review.*` rows, so the page
 * loads without a 404 and the policy section agrees with the field table.
 */
const humanReview: ReviewPolicy = configHumanReviewJson;
const overridable: OverridableMap = configOverridableJson;
const models: ModelsPayload = modelsJson;
/**
 * `scenarios/dashboard-sole-wait.json` — the committed set with go-live's hold
 * released, so exactly one thing needs a person. Typed here for the same
 * reason every `factory/` payload is: svelte-check fails the gate on a field
 * `api.ts` cannot describe.
 */
const soleWait: Dashboard = soleWaitJson;
/**
 * `scenarios/*-first-run.json` — a factory with no work and no run history,
 * and one readiness check failing (#2515): the set the `work-first-run` shot
 * and `ui-check` draw the first-run card over.
 */
const firstRunDashboard: Dashboard = firstRunDashboardJson;
const firstRunProjection: WorkProjection = firstRunProjectionJson;
/**
 * `scenarios/dashboard-work-held-round-due.json` — SFD-229 composed, its last
 * review round owing a full one while the go-live hold stops kew running it
 * (#2842): the projection says `review_round_due`, the set says held.
 */
const heldRoundDue: WorkProjection = heldRoundDueJson;
/**
 * `scenarios/*-launch-fixes-host-restart-repeated.json` — the committed set
 * with #2058 failed because the host restarted under it twice (#2812, #2971):
 * the one terminal failure that asks a person, with `kew epic retry` as its
 * move.
 * The row and the node's attention are what the server composes for it.
 */
const hostRestartDashboard: Dashboard = hostRestartDashboardJson;
const hostRestartEpic: EpicDetail = {
  ...hostRestartEpicJson,
  snapshot: {
    ...hostRestartEpicJson.snapshot,
    edges: pairs(hostRestartEpicJson.snapshot.edges),
  },
};
/**
 * `scenarios/dashboard-host-restart-repeated.json` — the two `/api/dashboard`
 * rows a second host death leaves (#2964), a clean checkout's and a preserved
 * one's. They are the real endpoint's output over ledger state the driver
 * wrote: `kew/tests/test_host_restart_reap.py` serves them again and holds
 * every field but the clock's `age` to this file. `ui-check` draws over them.
 */
const hostRestartRepeated: Record<"clean" | "dirty", DashboardRow> =
  hostRestartRepeatedJson;
/**
 * `scenarios/dashboard-merge-queue-stopped.json` — the `/api/dashboard` row a
 * merge queue stopped on an unverified main serves (#2883), as `assemble`
 * serves it: `kew/tests/test_merge_queue_needs_you.py` holds every field but
 * `age` to this file. `ui-check` draws over it.
 */
const mergeQueueStopped: DashboardRow = mergeQueueStoppedJson;
/**
 * `scenarios/host-restart-preserved-continue.json` — the needs-you row and the
 * epic node a node's preserved continuation leaves when it dies with its host
 * a second time (#2960). Both are the real endpoints' output:
 * `kew/tests/test_host_restart_preserved_continuation.py` serves them again
 * and holds every field but the clock's stamps to this file. `ui-check` draws
 * Work, Monitor and the Epic page over them.
 */
const hostRestartPreserved: {
  epic_ref: string;
  row: DashboardRow;
  node: EpicNode;
} = hostRestartPreservedJson;
const firstRunRuns: Run[] = firstRunRunsJson;
const firstRunReadiness: Readiness = {
  ...firstRunReadinessJson,
  checks: firstRunReadinessJson.checks.map((c) => ({
    ...c,
    status: c.status as Readiness["checks"][number]["status"],
  })),
};
const projection: WorkProjection = workProjectionJson;
const workView: WorkView = workViewJson;
const pipeline: ItemPipeline = pipelineJson;
const evidence: ItemEvidence = evidenceJson;
const description: ItemDescription = descriptionJson;
/**
 * #2060, a ready-queue row nothing has run for (#2572): four pending stages, a
 * chain with nothing to attest yet, and the issue body — so selecting it in
 * fixture mode reads three answers rather than three 404s.
 */
const idlePipeline: ItemPipeline = idlePipelineJson;
const mergedPipeline: ItemPipeline = mergedPipelineJson;
const mergedEvidence: ItemEvidence = mergedEvidenceJson;
const mergedDescription: ItemDescription = mergedDescriptionJson;
const idleEvidence: ItemEvidence = idleEvidenceJson;
const idleDescription: ItemDescription = idleDescriptionJson;
const epicNode: EpicNodeDetail = epicNodeJson;
const freshness: PrFreshness = freshnessJson;
/** `GET /api/run-requests` (#2537): no Run has been queued in the fixture. */
const runRequests: { requests: RunRequest[] } = runRequestsJson;
const epicTimeline: EpicTimeline = epicTimelineJson;
/**
 * The two tick strips (#2304). Typed here like every other payload, so
 * svelte-check fails the gate on a field `api.ts` cannot describe — and read
 * below for AC3: the failure in each strip must survive batching.
 */
const epicStrip: EpicTickStrip[] = epicStripJson;
const goLiveStrip: EpicTickStrip[] = goLiveStripJson;
/**
 * The counts map, with the keys this entry does not have dropped.
 *
 * `counts` is a tally, so one epic's map has a key another's does not —
 * go-live has a `queued` node and launch-fixes has none. A JSON module's
 * array normalises its elements into ONE object type, which spells that
 * difference `queued?: undefined`, and `Record<string, number>` rightly
 * refuses it. On the wire the key is simply absent, so absent is what this
 * restores. It is a narrowing loop rather than a cast: every other field of
 * the summary still has to satisfy `EpicSummary` below.
 */
function tallied(counts: Record<string, number | undefined>) {
  const out: Record<string, number> = {};
  for (const [status, count] of Object.entries(counts)) {
    if (count !== undefined) out[status] = count;
  }
  return out;
}
const epics: EpicSummary[] = epicsJson.map((summary) => ({
  ...summary,
  counts: tallied(summary.counts),
}));
// Same `tallied` narrowing as the epic counts: an empty bucket and a counted
// one normalise to one object type with optional keys, which
// `Record<string, number>` rightly refuses. On the wire the key is absent.
const auditHistogram: AuditHistogram = {
  ...auditHistogramJson,
  buckets: auditHistogramJson.buckets.map((bucket) => ({
    ...bucket,
    counts: tallied(bucket.counts),
  })),
};
const epic: EpicDetail = {
  ...epicJson,
  snapshot: { ...epicJson.snapshot, edges: pairs(epicJson.snapshot.edges) },
};
// The #2209 layout fixture: the go-live epic the owner reviewed, six layers
// deep with two nodes per packed row in places, a queued node nobody has
// armed and a done node under an approval hold.
const goLive: EpicDetail = {
  ...goLiveJson,
  snapshot: { ...goLiveJson.snapshot, edges: pairs(goLiveJson.snapshot.edges) },
};
const epicTicks: EpicTick[] = epicTicksJson.map((tick) => ({
  ...tick,
  snapshot: { ...tick.snapshot, edges: pairs(tick.snapshot.edges) },
}));
// The Linear epic's journal. It exists because the page READS it on every
// paint — the ticker seeds itself from the tail — so without it the epic
// 404'd on load and said nothing about it (design review C2, D22).
const goLiveTicks: EpicTick[] = goLiveTicksJson.map((tick) => ({
  ...tick,
  snapshot: { ...tick.snapshot, edges: pairs(tick.snapshot.edges) },
  // `tallied` again, for the same reason the counts need it: the frames'
  // per-node drains are three different key sets (a node that had not been
  // dispatched yet has no drain), which a JSON array normalises into one
  // optional-keyed object. On the wire the key is simply absent.
  economics: {
    ...tick.economics,
    node_drain_usd: tallied(tick.economics.node_drain_usd),
  },
}));
const goLiveTimeline: EpicTimeline = goLiveTimelineJson;
// The edge-less epic (#2543, D27): launch-fixes' nodes with no edge between
// any two and nobody waiting, so the page draws its card grid and opens on no
// node. Its journal and strip are empty — the page reads both on load, and an
// empty read is an answer where a missing file is a 404.
const sweep: EpicDetail = {
  ...sweepJson,
  snapshot: { ...sweepJson.snapshot, edges: pairs(sweepJson.snapshot.edges) },
};
const sweepTicks: EpicTick[] = sweepTicksJson;
const sweepStrip: EpicTickStrip[] = sweepStripJson;
const sweepTimeline: EpicTimeline = sweepTimelineJson;
// The node the Linear epic opens on (D20 selects the node that needs a
// person), so all FOUR of the rail's reads for it have an answer rather than
// a 404: the epic-level node spend, and the three item-level reads below.
const goLiveNode: EpicNodeDetail = goLiveNodeJson;
const goLiveWorkView: WorkView = goLiveWorkViewJson;
const goLivePipeline: ItemPipeline = goLivePipelineJson;
const goLiveEvidence: ItemEvidence = goLiveEvidenceJson;
const goLiveDescription: ItemDescription = goLiveDescriptionJson;

/**
 * Path the app reads -> fixture the middleware answers with. Every entry is
 * derived by the plugin's own rule, never a lookup table in the server: this
 * list exists so the test can prove the file for each read is committed, and
 * that no fixture is orphaned.
 */
const ROUTES: Record<string, string> = {
  "/api/meta": "meta.json",
  "/api/bundle": "bundle.json",
  "/api/auth/session": "auth-session.json",
  "/api/fleet/status": "fleet-status.json",
  "/api/runs": "runs.json",
  "/api/runs/f5dcb6a5": "runs-f5dcb6a5.json",
  "/api/runs/f5dcb6a5/flight": "runs-f5dcb6a5-flight.json",
  "/api/runs/f5dcb6a5/audit": "runs-f5dcb6a5-audit.json",
  "/api/runs/f5dcb6a5/nodes/f5dcb6a5%3A4/diff": "runs-f5dcb6a5-nodes-f5dcb6a5_4-diff.json",
  "/api/report/spend": "report-spend.json",
  "/api/report/models": "report-models.json",
  "/api/report/outcomes": "report-outcomes.json",
  "/api/loops": "loops.json",
  [`/api/loops/${LOOP_ID}`]: `loops-${LOOP_ID}.json`,
  [`/api/loops/${LOOP_ID}/iterations`]: `loops-${LOOP_ID}-iterations.json`,
  [`/api/loops/${LOOP_ID}/audit`]: `loops-${LOOP_ID}-audit.json`,
  "/api/config/budget": "config-budget.json",
  "/api/config/effective": "config-effective.json",
  "/api/config/human_review": "config-human_review.json",
  "/api/config/overridable": "config-overridable.json",
  "/api/models": "models.json",
  "/api/dispatch/resolve": "dispatch-resolve.json",
  "/api/health": "health.json",
  "/api/health/env": "health-env.json",
  "/api/readiness": "readiness.json",
  "/api/approvals": "approvals.json",
  "/api/audit": "audit.json",
  "/api/audit/status": "audit-status.json",
  "/api/audit/histogram": "audit-histogram.json",
  "/api/dashboard": "dashboard.json",
  "/api/dashboard/work": "dashboard-work.json",
  "/api/prs/freshness": "prs-freshness.json",
  "/api/run-requests": "run-requests.json",
  "/api/verbs/work/2055": "verbs-work-2055.json",
  "/api/work/2055/pipeline": "work-2055-pipeline.json",
  "/api/work/2055/evidence": "work-2055-evidence.json",
  "/api/work/2055/description": "work-2055-description.json",
  "/api/work/2060/pipeline": "work-2060-pipeline.json",
  "/api/work/2047/pipeline": "work-2047-pipeline.json",
  "/api/work/2047/evidence": "work-2047-evidence.json",
  "/api/work/2047/description": "work-2047-description.json",
  "/api/work/2060/evidence": "work-2060-evidence.json",
  "/api/work/2060/description": "work-2060-description.json",
  "/api/epics": "epics.json",
  "/api/epics/launch-fixes": "epics-launch-fixes.json",
  "/api/epics/go-live-2026-09": "epics-go-live-2026-09.json",
  "/api/epics/launch-fixes/ticks": "epics-launch-fixes-ticks.json",
  "/api/epics/go-live-2026-09/ticks": "epics-go-live-2026-09-ticks.json",
  "/api/epics/launch-fixes/tick-strip": "epics-launch-fixes-tick-strip.json",
  "/api/epics/go-live-2026-09/tick-strip":
    "epics-go-live-2026-09-tick-strip.json",
  "/api/epics/go-live-2026-09/timeline": "epics-go-live-2026-09-timeline.json",
  "/api/epics/go-live-2026-09/nodes/SFD-229":
    "epics-go-live-2026-09-nodes-SFD-229.json",
  "/api/verbs/work/SFD-229": "verbs-work-SFD-229.json",
  "/api/work/SFD-229/pipeline": "work-SFD-229-pipeline.json",
  "/api/work/SFD-229/evidence": "work-SFD-229-evidence.json",
  "/api/work/SFD-229/description": "work-SFD-229-description.json",
  "/api/epics/launch-fixes/nodes/2055": "epics-launch-fixes-nodes-2055.json",
  "/api/epics/launch-fixes/timeline": "epics-launch-fixes-timeline.json",
  "/api/epics/sweep-2026-10": "epics-sweep-2026-10.json",
  "/api/epics/sweep-2026-10/ticks": "epics-sweep-2026-10-ticks.json",
  "/api/epics/sweep-2026-10/tick-strip": "epics-sweep-2026-10-tick-strip.json",
  "/api/epics/sweep-2026-10/timeline": "epics-sweep-2026-10-timeline.json",
  "/api/prompts": "prompts.json",
  "/api/prompts/dispatch": "prompts-dispatch.json",
  "/api/prompts/adversarial_review": "prompts-adversarial_review.json",
  "/api/prompts/review_remediation": "prompts-review_remediation.json",
  "/api/prompts/resolve": "prompts-resolve.json",
  "/api/prompts/ci_fix": "prompts-ci_fix.json",
  "/api/prompts/triage": "prompts-triage.json",
  "/api/prompts/planning": "prompts-planning.json",
  "/api/prompts/acceptance_author": "prompts-acceptance_author.json",
  "/api/prompts/loop_continuation": "prompts-loop_continuation.json",
  "/api/prompts/readiness_probe": "prompts-readiness_probe.json",
  "/api/prompts/dispatch/versions": "prompts-dispatch-versions.json",
  "/api/prompts/adversarial_review/versions": "prompts-adversarial_review-versions.json",
  "/api/prompts/review_remediation/versions": "prompts-review_remediation-versions.json",
  "/api/prompts/resolve/versions": "prompts-resolve-versions.json",
  "/api/prompts/ci_fix/versions": "prompts-ci_fix-versions.json",
  "/api/prompts/dispatch/outcomes": "prompts-dispatch-outcomes.json",
  "/api/prompts/adversarial_review/outcomes": "prompts-adversarial_review-outcomes.json",
  "/api/prompts/review_remediation/outcomes": "prompts-review_remediation-outcomes.json",
  "/api/prompts/resolve/outcomes": "prompts-resolve-outcomes.json",
  "/api/prompts/ci_fix/outcomes": "prompts-ci_fix-outcomes.json",
};

/** Fixture file -> the value this test assigned an `api.ts` type to above. */
const TYPED: Record<string, unknown> = {
  "meta.json": meta,
  "bundle.json": bundle,
  "auth-session.json": session,
  "fleet-status.json": fleet,
  "runs.json": runs,
  "runs-f5dcb6a5.json": runDetail,
  "runs-f5dcb6a5-flight.json": runFlight,
  "runs-f5dcb6a5-audit.json": runAudit,
  "runs-f5dcb6a5-nodes-f5dcb6a5_4-diff.json": runDiff,
  "report-spend.json": spend,
  "report-models.json": reportModels,
  "report-outcomes.json": reportOutcomes,
  "loops.json": loops,
  [`loops-${LOOP_ID}.json`]: loopDetail,
  [`loops-${LOOP_ID}-iterations.json`]: loopIterations,
  [`loops-${LOOP_ID}-audit.json`]: loopAudit,
  "config-budget.json": budget,
  "config-effective.json": effectiveConfig,
  "config-human_review.json": humanReview,
  "config-overridable.json": overridable,
  "models.json": models,
  "health.json": health,
  "dispatch-resolve.json": dispatchPlan,
  "health-env.json": healthEnv,
  "readiness.json": readiness,
  "approvals.json": approvals,
  "audit.json": audit,
  "audit-status.json": auditStatus,
  "audit-histogram.json": auditHistogram,
  "dashboard.json": dashboard,
  "dashboard-work.json": projection,
  "verbs-work-2055.json": workView,
  "work-2055-pipeline.json": pipeline,
  "work-2055-evidence.json": evidence,
  "work-2055-description.json": description,
  "work-2060-pipeline.json": idlePipeline,
  "work-2047-pipeline.json": mergedPipeline,
  "work-2047-evidence.json": mergedEvidence,
  "work-2047-description.json": mergedDescription,
  "work-2060-evidence.json": idleEvidence,
  "work-2060-description.json": idleDescription,
  "epics.json": epics,
  "epics-launch-fixes.json": epic,
  "epics-go-live-2026-09.json": goLive,
  "epics-launch-fixes-ticks.json": epicTicks,
  "epics-launch-fixes-tick-strip.json": epicStrip,
  "epics-go-live-2026-09-tick-strip.json": goLiveStrip,
  "epics-go-live-2026-09-ticks.json": goLiveTicks,
  "epics-go-live-2026-09-timeline.json": goLiveTimeline,
  "epics-go-live-2026-09-nodes-SFD-229.json": goLiveNode,
  "verbs-work-SFD-229.json": goLiveWorkView,
  "work-SFD-229-pipeline.json": goLivePipeline,
  "work-SFD-229-evidence.json": goLiveEvidence,
  "work-SFD-229-description.json": goLiveDescription,
  "epics-launch-fixes-nodes-2055.json": epicNode,
  "epics-launch-fixes-timeline.json": epicTimeline,
  "epics-sweep-2026-10.json": sweep,
  "epics-sweep-2026-10-ticks.json": sweepTicks,
  "epics-sweep-2026-10-tick-strip.json": sweepStrip,
  "epics-sweep-2026-10-timeline.json": sweepTimeline,
  "prs-freshness.json": freshness,
  "run-requests.json": runRequests,
  "prompts.json": prompts,
  ...Object.fromEntries(
    Object.entries(promptPreviews).map(([role, read]) => [`prompts-${role}.json`, read]),
  ),
  ...Object.fromEntries(
    Object.entries(profileVersions).map(([role, read]) => [`prompts-${role}-versions.json`, read]),
  ),
  ...Object.fromEntries(
    Object.entries(profileOutcomes).map(([role, read]) => [`prompts-${role}-outcomes.json`, read]),
  ),
};

const FIXTURE_FILES = readdirSync(fileURLToPath(FIXTURE_DIR))
  .filter((f) => f.endsWith(".json"))
  .sort();

/**
 * The file the middleware would answer `path` out of, or null when it would
 * 404 — either because the path names no fixture at all or because the file it
 * names is not committed. The plugin's own `fixtureFileFor` does the naming, so
 * this cannot drift from what a reader sees in fixture mode.
 */
function served(path: string): string | null {
  const file = fixtureFileFor(path);
  if (file === null) return null;
  return existsSync(fileURLToPath(new URL(file, FIXTURE_DIR))) ? file : null;
}

/**
 * The design-review gallery's shot list, read as source. `gallery.mjs` drives a
 * browser the moment it is imported, so the list is parsed out of the file.
 */
function galleryShots(): { name: string; route: string }[] {
  const source = readFileSync(
    fileURLToPath(new URL("../../scripts/gallery.mjs", import.meta.url)),
    "utf8",
  );
  const found = source.matchAll(/\{\s*name:\s*'([^']+)',\s*route:\s*'([^']+)'/g);
  const shots = [...found].map((m) => ({ name: m[1], route: m[2] }));
  // Not vacuous: a regex that stopped matching would pass every loop below.
  expect(shots.length).toBeGreaterThan(5);
  return shots;
}

describe("the fixture route mapping", () => {
  it("derives each file name from the path after /api/", () => {
    for (const [path, file] of Object.entries(ROUTES)) {
      expect(fixtureFileFor(path), path).toBe(file);
      expect(fixtureFileFor(`${path}?after=0&limit=50`), path).toBe(file);
    }
  });

  it("serves the sealed change set the run's flight names (#3057)", () => {
    // The run page reads a diff for `artifact_node_id` and nothing else. The
    // fixture named none, so the only run shot drew "the sealed package could
    // not be read" over a run the set calls sealed. The node is the server's
    // own `<run>:<through_seq>`, so its colon must survive the route mapping.
    const node = runFlight.artifact_node_id;
    expect(node).toBe(`${runFlight.run_id}:${runFlight.through_seq}`);
    const path = `/api/runs/${runFlight.run_id}/nodes/${encodeURIComponent(node ?? "")}/diff`;
    expect(fixtureFileFor(path)).toBe("runs-f5dcb6a5-nodes-f5dcb6a5_4-diff.json");
    expect(ROUTES[path]).toBe(fixtureFileFor(path));
    expect(runDiff.node_id).toBe(node);
    expect(runDiff.run_id).toBe(runFlight.run_id);
    expect(runDiff.files?.length).toBe(runDetail.diff_files);
    for (const f of runDiff.files ?? []) {
      if (f.text !== null) expect(new TextEncoder().encode(f.text).length, f.path).toBe(f.byte_length);
    }
    const diffDim = runFlight.availability.find((d) => d.dimension === "artifact_diff");
    expect(diffDim?.availability).toBe("available");
  });

  it("answers every read the artboards need, and holds no orphan", () => {
    const wanted = Object.values(ROUTES).sort();
    expect(FIXTURE_FILES).toEqual(wanted);
    for (const file of FIXTURE_FILES) {
      expect(existsSync(fileURLToPath(new URL(file, FIXTURE_DIR))), file).toBe(
        true,
      );
    }
  });

  it("gives every epic the page's own reads, so none 404s on load (D22)", () => {
    // The epic page reads `/ticks` on every paint — the ticker seeds itself
    // from the journal tail — and `/timeline` whenever the Timeline view is
    // open. The Linear epic had a detail fixture and neither of those, so it
    // 404'd on load (design review C2, D22).
    const detail = Object.keys(ROUTES).filter(
      (path) => /^\/api\/epics\/[^/]+$/.test(path) && path !== "/api/epics",
    );
    expect(detail.length).toBeGreaterThan(1);
    for (const path of detail) {
      expect(ROUTES[`${path}/ticks`], `${path}/ticks`).toBeDefined();
      expect(ROUTES[`${path}/timeline`], `${path}/timeline`).toBeDefined();
    }
  });

  it("gives the node each epic opens on all four of its reads (D20)", () => {
    // The page selects the node that needs a person on first paint, and the
    // selection effect takes that node's whole rail with it: the epic-level
    // node spend plus the three item-level reads. All four are new traffic on
    // load — before D20 nothing was selected, so none of them went out — so a
    // fixture epic missing any one of them 404s on load, which is what the
    // Linear epic did for the three item reads (review r9).
    //
    // Resolved through the middleware's own rule rather than looked up in
    // ROUTES: what the reader sees in fixture mode is the file `fixtureFileFor`
    // names, and whether it is on disk.
    const opening = [
      ["launch-fixes", epic.snapshot!],
      ["go-live-2026-09", goLive.snapshot!],
    ] as const;
    for (const [ref, snapshot] of opening) {
      const node = snapshot.nodes.find((n) => n.attention !== null);
      expect(node, ref).toBeDefined();
      const id = node!.id;
      for (const path of [
        `/api/epics/${ref}/nodes/${id}`,
        `/api/verbs/work/${id}`,
        `/api/work/${id}/pipeline`,
        `/api/work/${id}/evidence`,
      ]) {
        expect(served(path), path).not.toBeNull();
        // ...and committed as a typed, non-orphan fixture, which is what the
        // two assertions above this test hold the whole set to.
        expect(ROUTES[path], path).toBeDefined();
      }
    }
  });

  it("gives the /health shot all four of its reads, and the AC3 image pair", () => {
    // The page's loader fires all four on load (the readiness read, #3470), so a missing one photographs
    // an empty section rather than the surface the cycle is reviewing. And the
    // shot exists for one specific pair (#2298 AC3): a MISSING ROUTED image,
    // which must read red and count in the header, beside a MISSING UNROUTED
    // one, which must read grey and count nowhere. A payload where both are
    // present — or where the unrouted one is routed — photographs neither.
    const shot = galleryShots().find((s) => s.route === "/health");
    expect(shot, "no /health gallery shot").toBeDefined();
    for (const path of ["/api/health", "/api/health/env", "/api/config/effective", "/api/readiness"]) {
      expect(served(path), path).not.toBeNull();
      expect(ROUTES[path], path).toBeDefined();
    }
    // The shot takes the pair from its own scenario (#3461): the committed set
    // is the reading every other screen's header counts, and it counts nothing.
    // Read off `scripts/gallery.mjs`, as `galleryShots` reads it.
    const source = readFileSync(
      fileURLToPath(new URL("../../scripts/gallery.mjs", import.meta.url)),
      "utf8",
    );
    expect(source).toMatch(
      /name:\s*'health',\s*route:\s*'\/health',\s*fixtures:\s*\{\s*'\/api\/health':\s*'scenarios\/health-image-missing\.json'\s*\}/,
    );
    const missing = healthImageMissing.images.filter((i) => i.present === false);
    expect(missing.filter((i) => i.routed)).toHaveLength(1);
    expect(missing.filter((i) => !i.routed)).toHaveLength(1);
  });

  it("serves a health reading that agrees with the header's working run", () => {
    // #3461 (D22): every fixture screen's header drew a red "1 down" — image
    // kew-agent:0.9.0 missing, "the factory cannot work" — beside a run the
    // same header showed working. The committed reading now counts nothing.
    const served: Health = JSON.parse(drive("/api/health").chunks.join(""));
    expect(healthWarnings(served)).toEqual([]);
    expect(healthBadge(served)).toBeNull();
    expect(healthHeadline(served)).toMatch(/^Nothing required is down/);
    // The routed images are there; the one missing image is unrouted, grey.
    expect(served.images.filter((i) => i.routed).every((i) => i.present)).toBe(true);
    expect(served.images.filter((i) => i.present === false)).toHaveLength(1);
    // The run the header draws as working is really in the same set.
    expect(runs.some((r) => isWorkingRun(r))).toBe(true);
    // Not vacuous: the payload the /health shot is taken over still counts one.
    expect(healthBadge(healthImageMissing)?.count).toBe(1);
  });

  it("serves /loops the one loop Health counts, and every read it opens", () => {
    // #3461 (D23): /loops read nothing in fixture mode and said "start kew
    // serve to see loops" while every other page said connected. Health's
    // count and the list are one fact, and the page's three per-loop reads
    // (detail, iteration strip, audit) each answer for the loop it selects.
    const listed: Loop[] = JSON.parse(drive("/api/loops").chunks.join(""));
    expect(listed.map((l) => l.loop_id)).toEqual([LOOP_ID]);
    expect(listed.filter((l) => l.status === "pursuing" && !l.stale)).toHaveLength(
      health.loops!.pursuing,
    );
    expect(listed.filter((l) => l.stale)).toHaveLength(health.loops!.stale);
    for (const path of [
      `/api/loops/${LOOP_ID}`,
      `/api/loops/${LOOP_ID}/iterations?limit=200`,
      `/api/loops/${LOOP_ID}/audit`,
    ]) {
      expect(drive(path).status, path).toBe(200);
    }
    // The detail, the strip and the list say the same loop.
    expect(loopDetail.iterations).toEqual(loopIterations);
    expect(loopDetail.step_count).toBe(loopIterations.length);
    const spent = loopIterations.reduce((sum, it) => sum + it.cost_usd, 0);
    expect(loopDetail.spent_usd).toBeCloseTo(spent, 6);
    expect(listed[0].spent_usd).toBe(loopDetail.spent_usd);
    expect(loopAudit.every((e) => e.loop_id === LOOP_ID)).toBe(true);
  });

  it("serves /scorecard both reads, counted off the runs the set holds", () => {
    // #3461 (D23): /api/report/models and /outcomes had no file, so the page
    // drew "offline · no data" over a 404. The table is the server's own count
    // of runs.json: every run's spend, a PR credited to the run that opened it.
    for (const path of ["/api/report/models?days=30", "/api/report/outcomes?weeks=8"]) {
      expect(drive(path).status, path).toBe(200);
    }
    const authored = runs.filter(
      (r) => r.pr_url && (r.run_kind ?? "dispatch") === "dispatch",
    );
    const [row] = reportModels.rows;
    expect(reportModels.rows).toHaveLength(1);
    expect(row.runs).toBe(runs.length);
    expect(row.spend).toBeCloseTo(
      runs.reduce((sum, r) => sum + r.cost_usd, 0),
      6,
    );
    expect(row.opened).toBe(authored.length);
    expect(row.merged).toBe(authored.filter((r) => r.pr_state === "merged").length);
    expect(reportOutcomes.weeks.at(-1)?.partial).toBe(true);
  });

  it("gives every Work item the gallery shoots its three reads", () => {
    // A selected row takes three item-level reads with it (the page's selection
    // effect: pipeline, evidence, description), and all three go out on load.
    // So a gallery shot of `/work?item=<id>` whose item is missing any one of
    // them photographs a 404 — a read gap in the pane, and a console error in
    // the manifest — rather than the surface the cycle is reviewing. Read off
    // `scripts/gallery.mjs` itself, so a shot added there cannot skip this.
    const shots = galleryShots().filter((shot) =>
      shot.route.startsWith("/work?"),
    );
    expect(shots.length).toBeGreaterThan(0);
    for (const shot of shots) {
      const id = new URLSearchParams(shot.route.split("?")[1]).get("item");
      expect(id, shot.name).not.toBeNull();
      for (const read of ["pipeline", "evidence", "description"]) {
        const path = `/api/work/${id}/${read}`;
        expect(served(path), `${shot.name} -> ${path}`).not.toBeNull();
        expect(ROUTES[path], path).toBeDefined();
      }
    }
  });

  it("type-checks every committed fixture, leaving none unread", () => {
    // The assignments above are the actual check — svelte-check fails the
    // frontend gate on a payload `api.ts` cannot describe. This asserts the
    // set is complete, so a new fixture cannot be added untyped.
    expect(Object.keys(TYPED).sort()).toEqual(FIXTURE_FILES);
    for (const [file, value] of Object.entries(TYPED)) {
      expect(value, file).not.toBeUndefined();
      expect(
        JSON.parse(
          readFileSync(fileURLToPath(new URL(file, FIXTURE_DIR)), "utf8"),
        ),
        file,
      ).not.toBeNull();
    }
  });

  it("refuses anything that is not a plain /api read", () => {
    for (const path of [
      "/",
      "/api",
      "/api/",
      "/epics/launch-fixes",
      "/api/../package.json",
      "/api/%2e%2e/%2e%2e/etc/passwd",
      "/api/runs/../../secret",
      "/api/runs/a.b",
      "/api/runs/%ZZ",
    ]) {
      expect(fixtureFileFor(path), path).toBeNull();
    }
  });

  it("knows the live reads that have no JSON body", () => {
    expect(isStreamPath("/api/epics/launch-fixes/stream")).toBe(true);
    expect(isStreamPath("/api/runs/f5dcb6a5/events")).toBe(true);
    expect(isStreamPath("/api/runs")).toBe(false);
  });
});

describe("the plugin", () => {
  it("is off unless KEW_UI_FIXTURES=1", () => {
    expect(fixtureApi({})).toBe(false);
    expect(fixtureApi({ KEW_UI_FIXTURES: "0" })).toBe(false);
    expect(fixtureApi({ KEW_UI_FIXTURES: "true" })).toBe(false);
  });

  it("serves only a dev server when it is on", () => {
    const plugin = fixtureApi({ KEW_UI_FIXTURES: "1" });
    expect(plugin).not.toBe(false);
    expect(plugin && plugin.name).toBe("kew-fixture-api");
    expect(plugin && plugin.apply).toBe("serve");
  });
});

/** What a fake response was told: status, headers, written chunks, whether it ended. */
interface Recorded {
  status: number | null;
  headers: Record<string, string>;
  chunks: string[];
  ended: boolean;
  nextCalls: number;
}

/**
 * Drive `fixtureMiddleware` with fake req/res/next objects. The fake res
 * records every call, so a test can prove that a passed-on request was not
 * touched at all, and that an owned one was answered and ended.
 */
function drive(url: string, method = "GET"): Recorded & { close: () => void } {
  const rec: Recorded = {
    status: null,
    headers: {},
    chunks: [],
    ended: false,
    nextCalls: 0,
  };
  const listeners: Record<string, (() => void)[]> = {};
  const req = {
    url,
    method,
    on(event: string, fn: () => void) {
      (listeners[event] ??= []).push(fn);
      return req;
    },
  };
  const res = {
    set statusCode(code: number) {
      rec.status = code;
    },
    get statusCode() {
      return rec.status ?? 200;
    },
    setHeader(name: string, value: string) {
      rec.headers[name.toLowerCase()] = value;
      return res;
    },
    write(chunk: string | Buffer) {
      rec.chunks.push(String(chunk));
      return true;
    },
    end(chunk?: string | Buffer) {
      if (chunk !== undefined) rec.chunks.push(String(chunk));
      rec.ended = true;
      return res;
    },
  };
  fixtureMiddleware(
    req as unknown as Parameters<typeof fixtureMiddleware>[0],
    res as unknown as Parameters<typeof fixtureMiddleware>[1],
    () => {
      rec.nextCalls += 1;
    },
  );
  return Object.assign(rec, {
    close: () => (listeners.close ?? []).forEach((fn) => fn()),
  });
}

function untouched(rec: Recorded, path: string) {
  expect(rec.nextCalls, path).toBe(1);
  expect(rec.status, path).toBeNull();
  expect(rec.headers, path).toEqual({});
  expect(rec.chunks, path).toEqual([]);
  expect(rec.ended, path).toBe(false);
}

describe("the fixture middleware", () => {
  const APP_PATHS = [
    "/",
    "/epics/launch-fixes",
    "/work/2055?tab=runs",
    "/_app/immutable/x.js",
    "/@vite/client",
    "/@fs/tmp/x.ts",
    "/apiary",
    "/api-docs",
    "/favicon.png",
  ];

  it("passes every non-/api request to next() and writes nothing", () => {
    for (const path of APP_PATHS) untouched(drive(path), path);
  });

  it("passes a non-/api write to next() too, rather than refusing it", () => {
    // SvelteKit form actions POST to app routes; the 405 is for /api only.
    for (const method of ["POST", "PUT", "DELETE"]) {
      untouched(
        drive("/epics/launch-fixes", method),
        `${method} /epics/launch-fixes`,
      );
    }
  });

  it("serves an /api read from its fixture and never calls next()", () => {
    const rec = drive("/api/report/spend?window=daily");
    expect(rec.nextCalls).toBe(0);
    expect(rec.status).toBe(200);
    expect(rec.headers["content-type"]).toBe("application/json");
    expect(rec.ended).toBe(true);
    expect(JSON.parse(rec.chunks.join(""))).toEqual(spend);
  });

  it("answers a HEAD on a fixture with headers and no body", () => {
    const rec = drive("/api/meta", "HEAD");
    expect(rec.nextCalls).toBe(0);
    expect(rec.status).toBe(200);
    expect(rec.chunks).toEqual([]);
    expect(rec.ended).toBe(true);
  });

  it("answers a missing fixture with a 404 that names the file", () => {
    const rec = drive("/api/runs/nope");
    expect(rec.nextCalls).toBe(0);
    expect(rec.status).toBe(404);
    expect(rec.ended).toBe(true);
    const body = JSON.parse(rec.chunks.join(""));
    expect(body.expected).toBe("frontend/fixtures/factory/runs-nope.json");
    expect(body.detail).toBe("no fixture for /api/runs/nope");
  });

  it("ends a bare /api and a traversal attempt with a 404, never passing them on", () => {
    for (const path of [
      "/api",
      "/api/",
      "/api?x=1",
      "/api/../package.json",
      "/api/%2e%2e/etc",
    ]) {
      const rec = drive(path);
      expect(rec.nextCalls, path).toBe(0);
      expect(rec.status, path).toBe(404);
      expect(rec.ended, path).toBe(true);
      expect(rec.chunks.join(""), path).not.toContain('"expected"');
    }
  });

  it("refuses an /api write with a 405", () => {
    for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
      const rec = drive("/api/fleet/status", method);
      expect(rec.nextCalls, method).toBe(0);
      expect(rec.status, method).toBe(405);
      expect(rec.ended, method).toBe(true);
    }
  });

  it("holds an /api stream open with one comment frame until the client closes", () => {
    for (const path of [
      "/api/epics/launch-fixes/stream",
      "/api/runs/f5dcb6a5/events",
    ]) {
      const rec = drive(path);
      expect(rec.nextCalls, path).toBe(0);
      expect(rec.status, path).toBe(200);
      expect(rec.headers["content-type"], path).toBe("text/event-stream");
      expect(rec.chunks, path).toEqual([
        ": kew fixture mode — no live frames\n\n",
      ]);
      expect(rec.ended, path).toBe(false);
      rec.close();
      expect(rec.ended, path).toBe(true);
    }
  });

  it("ends a HEAD on a stream at once instead of holding it open", () => {
    const rec = drive("/api/epics/launch-fixes/stream", "HEAD");
    expect(rec.nextCalls).toBe(0);
    expect(rec.status).toBe(200);
    expect(rec.headers["content-type"]).toBe("text/event-stream");
    expect(rec.chunks).toEqual([]);
    expect(rec.ended).toBe(true);
  });
});

/** A fixture file's committed bytes. */
function raw(file: string): string {
  return readFileSync(fileURLToPath(new URL(file, FIXTURE_DIR)), "utf8");
}

/** A fixture file's committed payload. */
function committed(file: string): unknown {
  return JSON.parse(raw(file));
}

/**
 * Every instant in `value`, in document order, as epoch ms. The plugin's own
 * `INSTANT` decides what counts, so this cannot disagree with what it moves.
 */
function instantsIn(value: unknown, into: number[] = []): number[] {
  if (typeof value === "string") {
    if (INSTANT.test(value)) into.push(Date.parse(value));
  } else if (Array.isArray(value)) {
    for (const item of value) instantsIn(item, into);
  } else if (value !== null && typeof value === "object") {
    for (const v of Object.values(value as Record<string, unknown>))
      instantsIn(v, into);
  }
  return into;
}

/**
 * The fixture clock — a committed instant must not freeze an age-GATED state
 * out of the surface that exists to photograph it.
 *
 * `$lib/freshness` withholds the needs-rebase badge once the journaled base
 * reading behind it is over `BASE_READING_MAX_AGE_MS` old, because that badge
 * is the one magenta on the epic graph and magenta is an ask of a person. The
 * fixture's only conflicted row carries the instant it was written at, so
 * served verbatim it is past that bound on every day but one — and the fixture
 * surface draws a permanently dark badge, which is pixel for pixel an epic
 * where nothing needs rebasing (reviewer finding, #2303).
 *
 * The middleware shifts every instant it serves by one delta — `now` less
 * `FIXTURE_EPOCH`, the newest instant in the committed set — so the mirror
 * reads as live and every relative offset in it survives, including the ones a
 * surface reads ACROSS two files.
 */
describe("the fixture clock", () => {
  it("leaves a payload that holds no instant exactly as committed", () => {
    const frozen = { rows: [{ round: 2, sha: "9f8e7d1" }] };
    expect(rebaseClock(frozen, Date.now())).toBe(frozen);
    // ...and on the wire too: the spend read holds no instant, so fixture mode
    // answers it with the committed bytes rather than a re-serialisation.
    expect(instantsIn(spend)).toEqual([]);
    expect(drive("/api/report/spend").chunks.join("")).toBe(
      raw("report-spend.json"),
    );
  });

  it("moves the set's newest instant to now and every other instant with it", () => {
    const now = Date.parse("2026-12-25T09:00:00.000Z");
    const rebased = rebaseClock(
      {
        composed_at: FIXTURE_EPOCH,
        base: { at: FIXTURE_EPOCH, since: "2026-10-03T08:15:00+00:00" },
      },
      now,
    ) as { composed_at: string; base: { at: string; since: string } };
    expect(Date.parse(rebased.composed_at)).toBe(now);
    expect(Date.parse(rebased.base.at)).toBe(now);
    // 6h33m before the clock, before and after — one delta, not a re-stamping.
    expect(now - Date.parse(rebased.base.since)).toBe(
      Date.parse(FIXTURE_EPOCH) - Date.parse("2026-10-03T08:15:00+00:00"),
    );
  });

  it("rewrites only full instants, never a sha, a count or a bare date", () => {
    const rebased = rebaseClock(
      {
        composed_at: FIXTURE_EPOCH,
        sha: "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        day: "2026-10-03",
        round: 2,
        verdict: null,
        tags: [FIXTURE_EPOCH, "launch-fixes"],
      },
      Date.parse("2026-12-25T09:00:00.000Z"),
    ) as Record<string, unknown>;
    expect(rebased.sha).toBe("a1b2c3d4e5f60718293a4b5c6d7e8f9012345678");
    expect(rebased.day).toBe("2026-10-03");
    expect(rebased.round).toBe(2);
    expect(rebased.verdict).toBeNull();
    expect((rebased.tags as string[])[1]).toBe("launch-fixes");
    expect(Date.parse((rebased.tags as string[])[0])).toBe(
      Date.parse("2026-12-25T09:00:00.000Z"),
    );
  });

  it("serves a malformed fixture unchanged rather than swallowing it", () => {
    expect(fixtureBody("not json at all", Date.now())).toBe("not json at all");
  });

  it("pins FIXTURE_EPOCH to the newest instant in the committed set", () => {
    // The set is read AS this instant, so serving maps it to now. Behind the
    // set's maximum and the freshest reading lands in the FUTURE, which no
    // gate here is written for; ahead of it and that reading is needlessly
    // stale, which is what left the needs-rebase badge dark to begin with.
    // So: whenever a fixture arrives carrying a newer instant, bump the epoch.
    let newest = -Infinity;
    let carrying = "";
    for (const file of FIXTURE_FILES) {
      for (const ms of instantsIn(committed(file))) {
        if (ms > newest) [newest, carrying] = [ms, file];
      }
    }
    expect(newest, carrying).toBe(Date.parse(FIXTURE_EPOCH));
  });

  it("shifts every fixture by the one set-wide delta, never a per-file one", () => {
    // One delta is the whole guarantee: it is what lets a surface read two
    // fixtures against each other — a run window here, the evidence rows that
    // run attested there — and get the offsets that were committed. A delta
    // taken from each payload's own `composed_at` is NOT that, because the
    // files of one surface do not all declare the same one (PR #2391).
    const now = Date.parse("2026-12-25T09:00:00.000Z");
    const deltas = new Map<string, Set<number>>();
    for (const file of FIXTURE_FILES) {
      const was = instantsIn(committed(file));
      if (was.length === 0) continue;
      const shifted = instantsIn(JSON.parse(fixtureBody(raw(file), now)));
      expect(shifted.length, file).toBe(was.length);
      deltas.set(file, new Set(shifted.map((ms, i) => ms - was[i])));
    }
    // Not vacuous: most of the set carries instants, across several surfaces.
    expect(deltas.size).toBeGreaterThan(5);
    const seen = new Map<number, string[]>();
    for (const [file, perFile] of deltas)
      for (const delta of perFile) seen.set(delta, [...(seen.get(delta) ?? []), file]);
    expect(Object.fromEntries(seen)).toEqual({
      [now - Date.parse(FIXTURE_EPOCH)]: [...deltas.keys()],
    });
  });

  it("serves the event stream inside the live window, on the set's one delta", () => {
    // #2847 P12. The stream's window is the clock's last 30 minutes, and its
    // events used to sit nine days before the set's newest instant: "No events
    // in this window" over a listed event. They are now written in the minutes
    // before FIXTURE_EPOCH, so the one set-wide delta puts them inside it.
    const before = Date.now();
    const served: AuditList = JSON.parse(drive("/api/audit").chunks.join(""));
    const after = Date.now();
    const stamps = served.events.map((e) => Date.parse(e.timestamp));
    expect(stamps).toHaveLength(audit.events.length);
    audit.events.forEach((e, i) => {
      const moved = stamps[i] - Date.parse(e.timestamp);
      expect(moved).toBeGreaterThanOrEqual(before - Date.parse(FIXTURE_EPOCH));
      expect(moved).toBeLessThanOrEqual(after - Date.parse(FIXTURE_EPOCH));
    });
    const window = streamWindow(after);
    for (const ms of stamps) {
      expect(ms).toBeGreaterThanOrEqual(window.startMs);
      expect(ms).toBeLessThanOrEqual(window.endMs);
    }
  });

  it("serves each stream event inside the run it names, as its run row reads", () => {
    // A stream event and the run it names are two reads of one fact. Moved by
    // two deltas, a run nine days old completed "now" on the Monitor while its
    // own row said otherwise; on one delta they keep their committed offsets.
    // Through the middleware, on one frozen clock, so the two reads are served
    // at the same instant and any difference between them is the fixtures'.
    const now = Date.parse("2026-12-25T09:00:00.000Z");
    vi.useFakeTimers({ toFake: ["Date"], now });
    let events: AuditEvent[];
    let rows: Run[];
    try {
      events = (JSON.parse(drive("/api/audit").chunks.join("")) as AuditList).events;
      rows = JSON.parse(drive("/api/runs").chunks.join("")) as Run[];
    } finally {
      vi.useRealTimers();
    }
    const named = events.filter((e) => e.run_id !== null);
    expect(named.length).toBeGreaterThan(2);
    for (const e of named) {
      const run = rows.find((r) => r.run_id === e.run_id);
      expect(run, e.run_id!).toBeDefined();
      const at = Date.parse(e.timestamp);
      expect(at, `${e.event} before ${e.run_id} started`).toBeGreaterThanOrEqual(
        Date.parse(run!.started_at!),
      );
      expect(at, `${e.event} after ${e.run_id} finished`).toBeLessThanOrEqual(
        run!.finished_at ? Date.parse(run!.finished_at) : now,
      );
    }
    // The verdict is the review run's last act: the same instant on both reads.
    const verdict = events.find((e) => e.event === "adversarial_review_verdict")!;
    const review = rows.find((r) => r.run_id === verdict.run_id)!;
    expect(verdict.timestamp).toBe(review.finished_at);
  });

  it("counts the histogram for the window it is asked about, every bucket inside it", () => {
    // #2847. Served as a file, the histogram was a fixed day of quarter-hours
    // whatever the query, and its last bucket ran past the live range. It is
    // now tiled from the query, as `service/audit.py` tiles it.
    const now = Date.now();
    const live = streamWindow(now);
    for (const range of [live, { startMs: live.endMs - DAY_MS, endMs: live.endMs }]) {
      const bucketS = bucketSecondsFor(range.endMs - range.startMs);
      const url =
        `/api/audit/histogram?since=${encodeURIComponent(isoOf(range.startMs))}` +
        `&until=${encodeURIComponent(isoOf(range.endMs))}&bucket_s=${bucketS}`;
      const rec = drive(url);
      expect(rec.status, url).toBe(200);
      const read: AuditHistogram = JSON.parse(rec.chunks.join(""));
      expect(Date.parse(read.since)).toBe(range.startMs);
      expect(Date.parse(read.until)).toBe(range.endMs);
      expect(read.bucket_s).toBe(bucketS);
      for (const bucket of read.buckets) {
        const start = Date.parse(bucket.start);
        expect(start, bucket.start).toBeGreaterThanOrEqual(range.startMs);
        expect(start + bucketS * 1000, bucket.start).toBeLessThanOrEqual(
          range.endMs + bucketS * 1000 - 1,
        );
        expect(start, bucket.start).toBeLessThan(range.endMs);
      }
      const summed = read.buckets.reduce(
        (sum, b) => sum + Object.values(b.counts).reduce((a, n) => a + n, 0),
        0,
      );
      expect(summed).toBe(read.total);
    }
    // The live range counts exactly the events the list beside it serves.
    const listed: AuditList = JSON.parse(drive("/api/audit").chunks.join(""));
    const read = auditHistogramBody(
      `/api/audit/histogram?since=${isoOf(live.startMs)}&until=${isoOf(live.endMs + 1)}` +
        `&bucket_s=${bucketSecondsFor(30 * MINUTE_MS)}`,
      now,
    )!.body as AuditHistogram;
    expect(read.total).toBe(listed.events.length);
    // ...and a scoped read counts only what the scope names.
    const scoped = auditHistogramBody(
      `/api/audit/histogram?since=${isoOf(live.startMs)}&until=${isoOf(live.endMs + 1)}` +
        `&bucket_s=30&item=2033&event_prefix=dispatch_`,
      now,
    )!.body as AuditHistogram;
    expect(scoped.total).toBe(
      listed.events.filter((e) => e.issue_num === 2033 && e.event.startsWith("dispatch_")).length,
    );
  });

  it("lists only the events inside the window it is asked about, a page at a time", () => {
    // #2875. Served as a file, the list ignored `since`/`until`: a custom
    // Monitor range over a past hour listed events near now, outside the window
    // its header named. It now applies the window, filters, cursor and limit
    // the way `service/audit.py` does, newest first.
    const now = Date.parse("2026-12-25T09:00:00.000Z");
    const all = (auditListBody("/api/audit", now).body as AuditList).events;
    expect(all.map((e) => e.id)).toEqual([5, 4, 3, 2, 1]);
    const at = (id: number) => Date.parse(all.find((e) => e.id === id)!.timestamp);
    // [since, until) holding events 2, 3 and 4: 4's instant is `until`-exclusive.
    const window = `since=${encodeURIComponent(isoOf(at(2)))}&until=${encodeURIComponent(isoOf(at(5)))}`;
    const read = (query: string) => {
      const rec = drive(`/api/audit?${query}`);
      expect(rec.status, query).toBe(200);
      return JSON.parse(rec.chunks.join("")) as AuditList;
    };
    vi.useFakeTimers({ toFake: ["Date"], now });
    try {
      const whole = read(window);
      expect(whole.events.map((e) => e.id)).toEqual([4, 3, 2]);
      expect(whole.next_after).toBeNull();
      for (const e of whole.events) {
        expect(Date.parse(e.timestamp)).toBeGreaterThanOrEqual(at(2));
        expect(Date.parse(e.timestamp)).toBeLessThan(at(5));
      }
      // A limit pages it: two rows, then the cursor names the rest.
      const first = read(`${window}&limit=2`);
      expect(first.events.map((e) => e.id)).toEqual([4, 3]);
      expect(first.next_after).toBe(3);
      const second = read(`${window}&limit=2&after=${first.next_after}`);
      expect(second.events.map((e) => e.id)).toEqual([2]);
      expect(second.next_after).toBeNull();
      // The scope filters narrow inside the window.
      expect(read(`${window}&issue=2055`).events.map((e) => e.id)).toEqual([4, 3]);
      expect(read(`${window}&event_prefix=dispatch_`).events.map((e) => e.id)).toEqual([3, 2]);
      // A window that holds none of them lists none of them.
      const past = "since=2026-10-13T08:00:00Z&until=2026-10-13T09:00:00Z";
      expect(read(past).events).toEqual([]);
    } finally {
      vi.useRealTimers();
    }
    // What the route refuses, the fixture refuses.
    for (const bad of ["limit=0", "limit=201", "after=x", "since=x", "issue=2.5"])
      expect(drive(`/api/audit?${bad}`).status, bad).toBe(422);
  });

  it("applies item and issue as two filters, both of which a row must pass", () => {
    // `issue` reads `issue_num`; `item` reads `item_id`, or `issue_num` when
    // the value is numeric (`_audit_item_filters`). A read naming both must
    // pass both, so `item=2055&issue=2033` lists none of the 2055 rows.
    const listedIds = (url: string) =>
      (auditListBody(url).body as AuditList).events.map((e) => e.id);
    expect(listedIds("/api/audit?issue=2055")).toEqual([5, 4, 3]);
    expect(listedIds("/api/audit?item=2055&issue=2033")).toEqual([]);
    expect(drive("/api/audit?item=2055&issue=2033").status).toBe(200);
    // The Monitor's item filter sends an issue number as `?item=`, and the
    // store answers it with `item_id = n OR issue_num = n`. The fixture rows
    // carry no item_id, so an item_id-only match listed nothing for it.
    expect(listedIds("/api/audit?item=2055")).toEqual([5, 4, 3]);
    expect(listedIds("/api/audit?item=2055&issue=2055")).toEqual([5, 4, 3]);
    expect(listedIds("/api/audit?item=SFD-2055")).toEqual([]);
    // The histogram beside the list reads the same scope: `item=2055` counts
    // the issue-2055 events, and `item=2055&issue=2033` counts none.
    const now = Date.parse("2026-12-25T09:00:00.000Z");
    const listed = (auditListBody("/api/audit", now).body as AuditList).events;
    const instants = listed.map((e) => Date.parse(e.timestamp));
    const span =
      `since=${encodeURIComponent(isoOf(Math.min(...instants)))}` +
      `&until=${encodeURIComponent(isoOf(Math.max(...instants) + 1))}&bucket_s=30`;
    const counted = (scope: string) =>
      (auditHistogramBody(`/api/audit/histogram?${span}&${scope}`, now)!.body as AuditHistogram)
        .total;
    expect(counted("item=2055")).toBe(listed.filter((e) => e.issue_num === 2055).length);
    expect(counted("item=2055")).toBe(3);
    expect(counted("item=2055&issue=2033")).toBe(0);
    // A row that carries both keys passes only a read naming both of its own.
    const row = (id: number, item_id: string | null, issue_num: number | null) => ({
      id,
      event: "dispatch_owner_recorded",
      timestamp: "2026-10-03T14:33:12+00:00",
      epic_ref: "launch-fixes",
      issue_num,
      item_id,
      actor_type: "agent",
    });
    const rows = [row(3, "SFD-229", 2055), row(2, "SFD-229", null), row(1, null, 2055)];
    const page = (query: string) =>
      (auditListPage(rows, `/api/audit?${query}`).body as AuditList).events.map((e) => e.id);
    expect(page("item=SFD-229&issue=2055")).toEqual([3]);
    expect(page("item=SFD-229&issue=2033")).toEqual([]);
    expect(page("item=SFD-229")).toEqual([3, 2]);
    expect(page("issue=2055")).toEqual([3, 1]);
    // A numeric item names either key: the row filed under item_id "2055" and
    // the rows filed under issue 2055 (and "02055" is still issue 2055).
    const mixed = [row(4, "2055", null), ...rows];
    const mixedPage = (query: string) =>
      (auditListPage(mixed, `/api/audit?${query}`).body as AuditList).events.map((e) => e.id);
    expect(mixedPage("item=2055")).toEqual([4, 3, 1]);
    expect(mixedPage("item=02055")).toEqual([3, 1]);
  });

  it("refuses a histogram window the server would refuse", () => {
    expect(drive("/api/audit/histogram?since=x&until=y&bucket_s=30").status).toBe(422);
    const day = "since=2026-10-02T00:00:00Z&until=2026-10-03T00:00:00Z";
    expect(drive(`/api/audit/histogram?${day}&bucket_s=60`).status).toBe(422);
    expect(drive(`/api/audit/histogram?${day}&bucket_s=900`).status).toBe(200);
  });

  it("keeps the histogram's background before the stream's oldest event", () => {
    // The committed histogram is the factory's day before the listed events;
    // overlapping them would count the same event twice in the overview.
    const oldest = Math.min(...audit.events.map((e) => Date.parse(e.timestamp)));
    expect(Date.parse(auditHistogram.until)).toBeLessThanOrEqual(oldest);
  });

  it("keeps an item's attested evidence inside the run that attested it", () => {
    // The epic and work surfaces read a work view and that item's evidence as
    // two separate `/api` calls, then draw the rows against the run window.
    // `work-2055-evidence.json` was composed 2h25m after the work view it
    // describes, so a per-payload delta moved the two by different amounts and
    // every attested row landed ~2h25m BEFORE its own run had started — a
    // reading no run can produce (reviewer finding, PR #2391). Driven through
    // the real middleware, which is the path the browser reads.
    for (const id of ["2055", "SFD-229"]) {
      const view: WorkView = JSON.parse(drive(`/api/verbs/work/${id}`).chunks.join(""));
      const rows: ItemEvidence = JSON.parse(
        drive(`/api/work/${id}/evidence`).chunks.join(""),
      );
      const startedAt = Date.parse(view.execution.started_at!);
      expect(Number.isNaN(startedAt), id).toBe(false);
      const attested = rows.rows.filter((row) => row.ts !== "");
      // Not vacuous: both items really do carry attested, timestamped rows.
      expect(attested.length, id).toBeGreaterThan(0);
      for (const row of attested) {
        expect(
          Date.parse(row.ts) - startedAt,
          `${id} ${row.event} @ ${row.ts} vs run start ${view.execution.started_at}`,
        ).toBeGreaterThanOrEqual(0);
      }
    }
  });

  it("still draws the magenta needs-rebase badge today, read off the wall clock", () => {
    const rec = drive("/api/prs/freshness?limit=200&epic_ref=go-live-2026-09");
    expect(rec.status).toBe(200);
    const served: PrFreshness = JSON.parse(rec.chunks.join(""));
    const conflicted = byItem(served).get("SFD-227");
    expect(conflicted).toBeDefined();
    expect(conflicted!.base.state).toBe("conflicted");

    // Date.now() deliberately: this is the clock the browser reads the fixture
    // surface against, and the badge was dark under it.
    const [badge] = badgesFor(conflicted!, Date.now());
    expect(badge).toBeDefined();
    expect(badge.kind).toBe("needs-rebase");
    expect(badge.needsAPerson).toBe(true);

    // Vacuity guard: the gate really does bite, so the pass above is the
    // rebase and not a badge that would draw off any reading at all. The
    // committed bytes, read at a clock past the bound — which is every clock
    // after the hour the fixture was written at.
    const frozen: PrFreshness = JSON.parse(
      readFileSync(
        fileURLToPath(new URL("prs-freshness.json", FIXTURE_DIR)),
        "utf8",
      ),
    );
    const row = byItem(frozen).get("SFD-227")!;
    const pastTheBound = Date.parse(row.base.at!) + BASE_READING_MAX_AGE_MS + 1;
    expect(badgesFor(row, pastTheBound)).toEqual([]);
  });

  it("keeps the review-stale badge and its causes through the rebase", () => {
    // The committed #2055 review is current (#3461), so the moved head is laid
    // on its row here and the row is served through the same rebase.
    const committedRows: PrFreshness = JSON.parse(raw("prs-freshness.json"));
    const row = byItem(committedRows).get("2055")!;
    const stale = {
      ...committedRows,
      rows: [
        {
          ...row,
          last_verdict: {
            ...row.last_verdict!,
            head_sha: "9f8e7d1c4b2a0e6f3d5c8b1a7e9f2c4d6b8a0e3f",
          },
          latest_head: {
            ...row.latest_head!,
            sha: "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
          },
          review_state: "stale",
        },
      ],
    };
    const served: PrFreshness = JSON.parse(fixtureBody(JSON.stringify(stale)));
    const moved = byItem(served).get("2055");
    expect(moved).toBeDefined();
    const [badge] = badgesFor(moved!, Date.now());
    expect(badge.kind).toBe("review-stale");
    expect(badge.causes[0].text).toContain("9f8e7d1");
    expect(badge.causes[0].href).toContain("/commits/a1b2c3d");
  });

  it("serves #2055's review as current, at the head its verdict names", () => {
    // #3461 (D21): the popover read "review stale" at 9f8e7d1 while the run
    // page named 9f2c1ab4 and the pipeline and needs-you named 3670e43f. One
    // story now: round 1 READY at 3670e43f, merge verify failed at that head.
    const served: PrFreshness = JSON.parse(drive("/api/prs/freshness").chunks.join(""));
    const row = byItem(served).get("2055")!;
    expect(row.review_state).toBe("current");
    expect(badgesFor(row, Date.now())).toEqual([]);
    const head = pipeline.merge_verify!.head;
    expect(row.last_verdict?.head_sha).toBe(head);
    expect(row.latest_head?.sha).toBe(head);
    expect(runDetail.verdict?.head_sha).toBe(head);
    expect(runDetail.verdict?.verdict).toBe("READY");
    expect(row.last_verdict?.verdict).toBe("READY");
  });
});

describe("DEpic — the launch-fixes epic", () => {
  const snapshot = epic.snapshot!;

  it("draws 20 nodes and the 9 edges of the artboard", () => {
    expect(snapshot.nodes).toHaveLength(20);
    expect(snapshot.edges).toHaveLength(9);
    const ids = new Set(snapshot.nodes.map((n) => n.id));
    expect(ids.size).toBe(20);
    for (const [from, to] of snapshot.edges) {
      expect(ids.has(from), from).toBe(true);
      expect(ids.has(to), to).toBe(true);
    }
    // The 8 "No dependencies · 8 done" nodes are done and touch no edge.
    const linked = new Set(snapshot.edges.flat());
    const loose = snapshot.nodes.filter(
      (n) => !linked.has(n.id) && n.id !== "2033",
    );
    expect(loose).toHaveLength(8);
    expect(loose.every((n) => n.status === "done")).toBe(true);
  });

  it("counts what the header strip reads: 13 / 20, one of each live status", () => {
    expect(snapshot.counts).toEqual({
      todo: 4,
      in_progress: 1,
      in_review: 1,
      done: 13,
      failed: 0,
      held: 0,
      blocked: 1,
      cancelled: 0,
    });
    const tally: Record<string, number> = {};
    for (const n of snapshot.nodes)
      tally[n.status] = (tally[n.status] ?? 0) + 1;
    for (const [status, count] of Object.entries(snapshot.counts)) {
      expect(tally[status] ?? 0, status).toBe(count);
    }
    expect(snapshot.signal).toBe("progressing");
  });

  it("names the selected node, its review state and its blocked sibling", () => {
    const byId = new Map(snapshot.nodes.map((n) => [n.id, n]));
    expect(byId.get("2055")!.status).toBe("in_review");
    expect(byId.get("2055")!.pr_url).toContain("/pull/2059");
    expect(byId.get("2033")!.status).toBe("in_progress");
    expect(byId.get("2053")!.stuck).toBe(
      "needs a design decision: a different user for reviewed code",
    );
    expect(epicNode.drain_usd).toBe(3.1);
    expect(epicNode.runs[0].run_id).toBe("f5dcb6a5");
    expect(epic.economics.node_drain_usd["2055"]).toBe(3.1);
    expect(epics[0].epic_ref).toBe("launch-fixes");
    expect(epicTicks[0].snapshot.nodes).toHaveLength(20);
  });

  it('leaves "[last tick]" empty rather than inventing a tick time', () => {
    expect(snapshot.tick_ts).toBe("");
    expect(epic.heartbeat!.last_tick_ts).toBeNull();
    expect(epic.heartbeat!.last_tick_started_at).toBeNull();
    expect(epics[0].last_tick_ts).toBeNull();
  });

  it("puts every dispatchable todo on the frontier", () => {
    expect(snapshot.frontier.sort()).toEqual(["2040", "2054", "2058", "2060"]);
    for (const id of snapshot.frontier) {
      const node = snapshot.nodes.find((n) => n.id === id)!;
      expect(node.status, id).toBe("todo");
    }
  });

  it('marks one node "needs you · merge PR #2059" and #2053 blocked with its reason', () => {
    // The detail read composes the needs-you join, so nothing is unavailable
    // and a null `attention` on every other node really means nobody waits.
    expect(snapshot.unavailable).toEqual([]);
    const waiting = snapshot.nodes.filter((n) => n.attention !== null);
    expect(waiting.map((n) => n.id)).toEqual(["2055"]);
    expect(waiting[0].attention).toEqual({
      // `action` is empty for the projection's own kinds: the artifact IS the
      // move, and the rail's call to action is a link to it. Only the epic's
      // journaled waits carry one (#2207).
      action: "",
      // And no kew command makes a merge decision — it happens on the host.
      // The DAG popover prints this field verbatim and reads nothing out of
      // `action` (#2250), so an empty one is no command line on screen.
      command: "",
      kind: "merge_decision",
      reason: "PR open for #2055 — awaiting review",
      url: "https://github.com/thossullivan/kew/pull/2059",
      // The one sentence and link the server writes for this wait (#2539):
      // the same strings the dashboard row for #2055 carries. Its merge verify
      // failed at its head, so the sentence leads with that, and the item's
      // MERGE VERIFY section is the first link, the PR the second (#3460).
      sentence:
        "Merge verify failed at 3670e43f: 2 of main's tests fail on this PR — the merge decision is yours",
      link: {
        label: "Open PR #2059 to merge",
        href: "https://github.com/thossullivan/kew/pull/2059",
      },
      verify_link: {
        label: "See the failed merge verify",
        href: "/work?item=2055#merge-verify",
      },
    });
    const blocked = snapshot.nodes.filter((n) => n.blocked_reason !== null);
    expect(blocked.map((n) => n.id)).toEqual(["2053"]);
    expect(blocked[0].blocked_reason).toBe(blocked[0].stuck);
  });

  it("names the join unavailable on journaled ticks rather than claiming nobody waits", () => {
    // `/ticks` is history: serve composes no join for it (epics.py
    // `_snapshot_dto` without one reports "attention" unavailable).
    for (const tick of [...epicTicks, ...goLiveTicks]) {
      expect(tick.snapshot.unavailable).toEqual(["attention"]);
      expect(tick.snapshot.nodes.every((n) => n.attention === null)).toBe(true);
    }
  });

  it("carries every field the served epic DTOs carry, and no other", () => {
    // Pydantic serializes defaulted fields too, so the served payload has
    // every schema property. Read the committed OpenAPI contract so a new
    // field on the API fails here, not only in svelte-check.
    const openapi = JSON.parse(
      readFileSync(
        fileURLToPath(new URL("../../../kew/openapi.json", import.meta.url)),
        "utf8",
      ),
    );
    const keysOf = (dto: string) =>
      Object.keys(openapi.components.schemas[dto].properties).sort();
    const snapshots = [
      ["epics-launch-fixes.json", snapshot],
      ...epicTicks.map(
        (t, i) => [`epics-launch-fixes-ticks.json[${i}]`, t.snapshot] as const,
      ),
      ...goLiveTicks.map(
        (t, i) => [`epics-go-live-2026-09-ticks.json[${i}]`, t.snapshot] as const,
      ),
    ] as const;
    for (const [where, snap] of snapshots) {
      expect(Object.keys(snap).sort(), where).toEqual(
        keysOf("EpicSnapshotDTO"),
      );
      for (const n of snap.nodes) {
        expect(Object.keys(n).sort(), `${where} node ${n.id}`).toEqual(
          keysOf("EpicNodeDTO"),
        );
        if (n.attention) {
          expect(
            Object.keys(n.attention).sort(),
            `${where} node ${n.id}`,
          ).toEqual(keysOf("NodeAttentionDTO"));
        }
      }
    }
  });
});

/**
 * `counts` is a tally of the node list, not a second hand-written fact.
 *
 * The backend builds it that way — `_status_counts` in graph/driver/tick.py
 * counts `overlays.statuses`, and `list_epics` in service/epics.py hands the
 * latest tick's map straight to the summary — so a fixture whose map disagrees
 * with its own nodes is a payload the served API cannot produce. It is also
 * invisible on the page in the one way that matters: `epicProgress` reads
 * `counts` and NOTHING else, so a status the map omits loses its node from the
 * header's tally, its cell in the bar, its row in the legend and its clause in
 * the bar's accessible name, while the DAG below draws the node as usual.
 *
 * That is exactly what the go-live fixture did: the map omitted `queued`, the
 * status #2209 exists to give its own mark, so the strip read "6 / 17" over a
 * graph of 18 nodes and the one new glyph had no legend row on the route
 * `dag-view.test.ts` names for visual inspection.
 *
 * The launch-fixes assertion above checks counts → nodes, which cannot catch an
 * omission; this checks both directions, and over every epic payload rather
 * than the one a test happened to name.
 */
describe("the fixture tick strips (#2304)", () => {
  /**
   * A strip as the middleware SERVES it: rebased onto now.
   *
   * "Running" is a bounded claim — an open row older than the driver-liveness
   * threshold is a driver that died mid-tick, not a tick in flight
   * (`TICK_LIVE_MAX_MS`) — so the committed instants answer a liveness
   * question only on the day they were written. Same trap as the dark
   * needs-rebase badge in `the fixture clock` above.
   */
  const served = (strip: EpicTickStrip[]): EpicTickStrip[] =>
    rebaseClock(strip, Date.now()) as EpicTickStrip[];

  /** The compact strip's window: the attempts themselves, on one lane. */
  const windowFor = (ticks: EpicTickStrip[], width = 400) => {
    const marks = tickMarks(ticks);
    const startMs = Math.min(...marks.map((m) => m.startMs));
    const endMs = Math.max(...marks.map((m) => m.endMs ?? m.startMs));
    return { startMs, endMs, width };
  };

  it("is mostly quiet, which is the reading the change journal could not give", () => {
    // A strip that is all marks says nothing. The committed set is ~70% quiet,
    // which is the shape of a real driver: `change_count` on the roster row
    // beside it reads 0 for every one of these ticks.
    for (const strip of [served(epicStrip), served(goLiveStrip)]) {
      expect(strip).toHaveLength(24);
      const quiet = strip.filter((t) => tickSlot(t).kind === "quiet");
      expect(quiet.length).toBeGreaterThan(strip.length / 2);
    }
  });

  it("AC3 — a batched failure shows as the batch's colour", () => {
    // The acceptance criterion. At any width where a day of ticks fits on a
    // lane, a failed attempt lands on the same pixels as the quiet ones
    // around it; the batch it joins must come out `failed` — the danger ×'s
    // colour — and not "quiet" because the array happened to end there.
    const failed = epicStrip.find((t) => t.outcome === "failed");
    expect(failed?.error_class).toBe("GithubApiError");

    for (const width of [60, 400, 760]) {
      const batches = batchMarks(tickMarks(epicStrip), windowFor(epicStrip, width));
      const carrying = batches.filter((b) =>
        b.items.some(({ item }) => item.id === failed?.id),
      );
      expect(carrying).toHaveLength(1);
      expect(carrying[0].severity).toBe("failed");
      expect(SEVERITY_SHAPE.failed.color).toBe("var(--danger)");
      // And it is NOT alone in its batch at the narrow widths — which is the
      // only way this assertion means anything.
      if (width === 60) expect(carrying[0].items.length).toBeGreaterThan(1);
    }
  });

  it("paints exactly one slot live, the newest", () => {
    const live = served(epicStrip).filter((t) => tickSlot(t).live);
    expect(live).toHaveLength(1);
    expect(live[0]?.id).toBe(epicStrip[epicStrip.length - 1]?.id);
    // go-live's driver is between ticks, so its strip has no live slot at all.
    expect(served(goLiveStrip).filter((t) => tickSlot(t).live)).toHaveLength(0);
  });

  it("carries every outcome the strip has a mark for", () => {
    const outcomes = new Set([...epicStrip, ...goLiveStrip].map((t) => t.outcome));
    for (const outcome of [
      "dispatched",
      "merged",
      "held",
      "failed",
      "locked_out",
      "empty_graph",
      "nothing",
      null,
    ]) {
      expect(outcomes.has(outcome)).toBe(true);
    }
  });
});

describe("every epic fixture's counts are a tally of its own nodes", () => {
  /** Zeros are kept deliberately — the legend shows a designed "0" row — so
   * they are dropped before comparing with a tally, which has no zeros. */
  const nonZero = (counts: Record<string, number>) =>
    Object.fromEntries(Object.entries(counts).filter(([, n]) => n !== 0));

  const tally = (nodes: readonly { status: string }[]) => {
    const out: Record<string, number> = {};
    for (const n of nodes) out[n.status] = (out[n.status] ?? 0) + 1;
    return out;
  };

  const SNAPSHOTS = [
    ["epics-launch-fixes.json", epic.snapshot!],
    ["epics-go-live-2026-09.json", goLive.snapshot!],
    ...epicTicks.map(
      (t, i) => [`epics-launch-fixes-ticks.json[${i}]`, t.snapshot] as const,
    ),
    ...goLiveTicks.map(
      (t, i) => [`epics-go-live-2026-09-ticks.json[${i}]`, t.snapshot] as const,
    ),
  ] as const;

  it.each(SNAPSHOTS)("%s", (_where, snap) => {
    expect(nonZero(snap.counts)).toEqual(tally(snap.nodes));
  });

  it("serves the list the same counts the detail does", () => {
    // `list_epics` reads the latest tick's own `counts`, so the two payloads
    // for one epic cannot differ — and the omission was in both.
    const detail = new Map([
      ["launch-fixes", epic.snapshot!.counts],
      ["go-live-2026-09", goLive.snapshot!.counts],
    ]);
    // #2206's two shelf fixtures carry no detail payload: the Finished /
    // Archived shelf is drawn from the list alone, so they exist to fill it.
    // They are named rather than merely tolerated — the list and the details
    // still have to cover each other, or a row on the page links to a route
    // the harness has no fixture for.
    const summaryOnly = ["security-wave", "tui-rewrite"];
    expect(epics.map((e) => e.epic_ref).sort()).toEqual(
      [...detail.keys(), ...summaryOnly].sort(),
    );
    for (const summary of epics) {
      if (summaryOnly.includes(summary.epic_ref)) continue;
      expect(summary.counts, summary.epic_ref).toEqual(
        detail.get(summary.epic_ref),
      );
    }
  });

  it("gives the go-live epic's queued node its cell, its row and its word", () => {
    // The whole point of the fixture: #2209's new mark has to be visible on
    // the strip, not only in the graph.
    const progress = epicProgress(goLive.snapshot!.counts);
    expect(progress.total).toBe(goLive.snapshot!.nodes.length);
    expect(progress.legend).toEqual(
      expect.arrayContaining([{ status: "queued", count: 1 }]),
    );
    expect(progress.bar.kind).toBe("cells");
    expect(
      progress.bar.kind === "cells"
        ? progress.bar.cells.filter((c) => c === "queued")
        : [],
    ).toEqual(["queued"]);
    expect(progress.barLabel).toContain("1 queued");
  });
});

describe("DWork — item #2055 and the issue list", () => {
  it("is the projection revision the actions would be decided against", () => {
    expect(workView.identity.item_ref).toBe("#2055");
    expect(workView.identity.epic_ref).toBe("launch-fixes");
    expect(workView.identity.run_id).toBe("f5dcb6a5");
    expect(workView.artifact.pr_url).toContain("/pull/2059");
    expect(workView.judgment.state).toBe("awaiting_human_decision");
    expect(workView.exception.detail).toBe(
      "kew opens the pull request; you decide whether it lands.",
    );
    expect(workView.route.containment).toBe("contained");
    expect(workView.view_revision).not.toBe("");
  });

  it("says one wait in one sentence, so the header stops repeating it", () => {
    // #2280 D10. The server writes ONE sentence for an item awaiting a human
    // decision and puts it in both fields (`_await_decision` in
    // kew/src/kew/service/work.py), and the header prints its exception line
    // only where the needs-you block under it is NOT already saying that
    // sentence (`exceptionLine`). The fixture gave the two fields different
    // strings — which no reachable server state does — so the containment test
    // could not fire and `/work?item=2055` drew the exception line above a
    // block saying something else: one wait, two sentences, in the shot the
    // gallery takes of the surface D10 is about.
    for (const view of projection.views) {
      if (view.judgment.state !== "awaiting_human_decision") continue;
      expect(view.safest_action.reason, view.identity.item_id ?? "").toBe(
        view.exception.detail,
      );
    }
    // The same field of the single-item read, which is a second copy of the
    // same view and must not drift from it.
    expect(workView.safest_action.reason).toBe(workView.exception.detail);

    const row = mergeWorkList({ projection, dashboard, runs }).find(
      (r) => r.itemId === "2055",
    );
    expect(row).toBeDefined();
    const header = itemHeader({ row: row!, runs });
    expect(header.exception?.detail).toBe(workView.exception.detail);
    expect(header.exceptionLine).toBeNull();
    expect(needsYouBlock(row!)?.why).toBe(workView.exception.detail);
  });

  it("draws the DWork swimlane: one lane, four stages, merge needing a person", () => {
    // design/factory-ui/DWork.html, x 468-1392 y 276-522. kew emits neither a
    // Reproduce stage nor a release/[branch] lane.
    expect(pipeline.lanes.map((l) => l.name)).toEqual(["main"]);
    const cells = Object.fromEntries(
      pipeline.lanes[0].stages.map((s) => [s.stage, s]),
    );
    expect(Object.keys(cells)).toEqual([
      "implement",
      "publish",
      "review",
      "merge",
    ]);
    expect([cells.implement.label, cells.implement.detail]).toEqual([
      "run f5dcb6a5",
      "success",
    ]);
    expect([cells.publish.label, cells.publish.detail]).toEqual([
      "scan clean",
      "3670e43f",
    ]);
    // #2279/D8: the fixture used to say "round 1 · reviewing" beside a merge
    // cell saying "needs you · merge decision" — one pull request waiting on
    // kew's reviewer and on a person at the same time. The round landed, so
    // the merge decision beside it is a real one.
    expect([cells.review.label, cells.review.detail]).toEqual([
      "round 1",
      "ready",
    ]);
    expect([cells.merge.label, cells.merge.detail]).toEqual([
      "needs you",
      "merge decision",
    ]);
    expect(cells.merge.state).toBe("needs_you");
    // The same run and pull request the item header names, not a second story.
    expect(cells.implement.evidence?.ref).toBe(workView.identity.run_id);
    expect(cells.merge.evidence?.ref).toBe(workView.artifact.pr_url);
    expect(pipeline.unavailable).toEqual([]);
  });

  it("draws the DWork evidence ledger: 4 of 4 attested, the verdict ready", () => {
    // design/factory-ui/DWork.html, x 853-1392 y 624-928, and the DEpic
    // Evidence column, x 860-1140 y 762-870 — one payload, two surfaces.
    expect(evidence.rows.map((r) => r.n)).toEqual([1, 2, 3, 4]);
    expect(evidence.rows.map((r) => r.label)).toEqual([
      "Sandboxed",
      "Canonical publication",
      "Scan clean",
      "Verdict",
    ]);
    expect(evidence.rows.map((r) => r.summary)).toEqual([
      "sandboxed",
      "canonical",
      "clean · 3670e43f",
      "ready",
    ]);
    // #3461 (D21): the verdict row read "pending" beside a pipeline whose
    // round 1 was READY and a merge decision that was the operator's. The
    // round has returned, so the ledger attests it, at the ledger's own time.
    expect(evidence.rows.map((r) => r.state)).toEqual([
      "attested",
      "attested",
      "attested",
      "attested",
    ]);
    expect([evidence.attested_count, evidence.total]).toEqual([4, 4]);
    expect(evidence.rows[3].ts).toBe(
      audit.events.find((e) => e.event === "adversarial_review_verdict" && e.issue_num === 2055)
        ?.timestamp,
    );
    // The green checks are the artboard's, and they rest on a verified chain.
    expect(evidence.chain_verified).toBe(true);
    expect(evidence.rows.filter((r) => r.state === "attested")).toHaveLength(
      evidence.attested_count,
    );
    // The ledger's third row names the head the swimlane's publish cell shows.
    const cells = Object.fromEntries(
      pipeline.lanes[0].stages.map((s) => [s.stage, s]),
    );
    expect(evidence.rows[2].summary).toContain(cells.publish.detail);
    expect(evidence.unavailable).toEqual([]);
    expect(evidence.reason).toBe("");
  });

  it("draws the DWork description: the body, and Expected beside it", () => {
    // design/factory-ui/DWork.html, x 468-853 y 624-928. The panel may only
    // print a body the server stands behind, so the fixture is the READ that
    // succeeded — the "Open on GitHub" fallback is exercised by the model's own
    // suite (work/evidence.test.ts) rather than by a fixture that lies about a
    // tracker being down.
    expect(description.availability).toBe("available");
    expect(description.provenance).toBe("observed");
    expect(description.reason).toBe("");
    expect(description.observed_at).not.toBeNull();
    expect(description.description).toContain(
      "the glue attack is caught by unparsed lines",
    );
    expect(description.expected).toBe(
      "the comment names the counter that fires; the hold-back keeps one line.",
    );
    // D draws no Actual section on this item, and an empty string is how the
    // panel is told there is none — never a heading with nothing under it.
    expect(description.actual).toBe("");
    // The one link out of the panel, and the same item the header names.
    expect(description.url).toContain("/2055");
    expect(description.item_id).toBe(workView.identity.item_id);
    expect(description.title).toBe("Review gate lows: comment and hold-back");
  });

  it("offers no action, because the projection offers none today", () => {
    // D's Claim / Hold / Flags buttons are drawn from `actions`; the header
    // must render nothing rather than invent them (Factory UI plan §1, Work).
    expect(workView.actions).toEqual([]);
    for (const view of projection.views)
      expect(view.actions, view.identity.item_ref).toEqual([]);
  });

  it("lists the ten rows of the issue list, newest number first", () => {
    const withRuns = projection.views.map((v) => Number(v.identity.item_id));
    const idle = dashboard.glance.ready_queue.map((r) => r.issue_num);
    expect([...withRuns, ...idle].sort((a, b) => b - a)).toEqual([
      2060, 2058, 2055, 2054, 2053, 2050, 2047, 2033, 1969, 1803,
    ]);
    expect(withRuns).toContain(2055);
    expect(idle).toContain(2050);
    // Every run-backed row names a run the runs fixture also knows.
    const runIds = new Set(runs.map((r) => r.run_id));
    for (const view of projection.views) {
      expect(
        runIds.has(view.identity.run_id ?? ""),
        view.identity.item_ref,
      ).toBe(true);
    }
    expect(projection.unreadable).toEqual([]);
  });

  it("carries the same review pressure on the projection and each view", () => {
    expect(projection.pressure.delivery_repo).toBe("thossullivan/kew");
    for (const view of projection.views) {
      expect(view.review_pressure.delivery_repo).toBe("thossullivan/kew");
    }
  });
});

describe("DMonitor — readouts, live runs, holds and the event stream", () => {
  it("spends $178.05 / $250 today and $915.05 / $1,500 this week", () => {
    expect(spend.daily.agentic).toBe(178.05);
    expect(spend.daily.cap).toBe(250);
    expect(spend.weekly.agentic).toBe(915.05);
    expect(spend.weekly.cap).toBe(1500);
    // The gauge reads the ENFORCED figure, so agentic must equal spent here.
    expect(spend.daily.spent).toBe(spend.daily.agentic);
    expect(spend.weekly.spent).toBe(spend.weekly.agentic);
    expect(dashboard.glance.caps).toEqual(spend);
  });

  it("keeps the prior cap $600 readable behind the operator override", () => {
    const weekly = budget["budget.weekly_usd"];
    expect(weekly.file_value).toBe(600);
    expect(weekly.effective).toBe(1500);
    expect(weekly.override!.value).toBe(1500);
    expect(weekly.override!.set_by).toBe("operator");
    expect(budget["budget.daily_usd"].override).toBeNull();
  });

  it("has exactly one working run, a begun pending run", () => {
    // `pending` with a start time, not `running` with none (#2377): `running`
    // is `kpi.ts`'s display-only refinement, so a payload carrying it served a
    // run the backend cannot store — and with no `started_at` the Runs page
    // filed both under "Unknown date". DMonitor's "[elapsed]" is a static
    // artboard's placeholder for a clock it has no reading of; the fixture
    // serves the start time the ledger really holds and the cell draws from it.
    //
    // One, not DMonitor's two (#2842): its #2055 governed review was drawn as
    // working while the same fixture served #2055 as the merge needs-you, and
    // the server drops an item with a run executing on it from that set. The
    // round has returned — which is why the merge is the operator's — so the
    // review run is a finished one.
    const working = runs.filter(isWorkingRun);
    expect(working.map((r) => r.status)).toEqual(["pending"]);
    expect(working.map((r) => r.issue_num)).toEqual([2033]);
    const review = runs.find((r) => r.run_id === "9c41e2b7");
    expect([review?.issue_num, review?.stage, review?.status]).toEqual([
      2055,
      "review",
      "success",
    ]);
    for (const run of working) {
      expect(run.started_at, run.run_id).not.toBeNull();
      expect(run.finished_at, run.run_id).toBeNull();
    }
    expect(working.map((r) => r.model)).toEqual(["claude-opus-5"]);
  });

  it("draws the live-runs Stage and Posture cells from the served fields", () => {
    // design/factory-ui/DMonitor.html, x 156-870 y 320-440: "governed review ·
    // round 1 · PR #2059" over "implement". The stage word is FUI-13's `stage`
    // (the caption "governed review" is the view's, not the payload's) and the
    // rest of the line is `stage_detail`; the Posture column is `posture`.
    const working = runs.filter(isWorkingRun);
    expect(working.map((r) => [r.stage, r.stage_detail])).toEqual([
      ["implement", ""],
    ]);
    expect(working.map((r) => r.posture)).toEqual(["host"]);
    // One implementer; the reviewer's round has returned (#2842) — the readout
    // at y ≈88-258 counts these.
    expect(working.filter((r) => r.stage === "review")).toHaveLength(0);
    expect(working.filter((r) => r.stage === "implement")).toHaveLength(1);
  });

  it("carries every field the served run DTO carries, and no other", () => {
    // Pydantic serializes defaulted fields too, so a new field on RunDTO fails
    // here rather than only in svelte-check (see the epic DTO test above).
    const openapi = JSON.parse(
      readFileSync(
        fileURLToPath(new URL("../../../kew/openapi.json", import.meta.url)),
        "utf8",
      ),
    );
    const keys = Object.keys(
      openapi.components.schemas.RunDTO.properties,
    ).sort();
    for (const run of runs) {
      expect(Object.keys(run).sort(), run.run_id).toEqual(keys);
    }
  });

  it("has two rows that need a human, one of them with a command", () => {
    // The merge decision the artboards draw, and the go-live round-cap hold.
    // The hold is here because of what it alone carries: a command
    // `kew.waits` really computes (`kew epic unhold <epic> <item>`), which is
    // the only way the Do this block and its Copy — on `/work` and in
    // Monitor's holds column — are reachable in fixture mode at all. A merge
    // decision serves no command (the merge is made on the host), so with it
    // alone the whole affordance was photographable from no committed state.
    expect(dashboard.rows).toHaveLength(2);
    expect(dashboard.rows.map((r) => r.type)).toEqual([
      "pr_review",
      "epic_wait",
    ]);
    expect(dashboard.rows[0].pr_number).toBe(2059);
    expect(dashboard.rows.map((r) => r.command)).toEqual([
      "",
      "kew epic unhold go-live-2026-09 SFD-229",
    ]);
    // Per-epic buckets are the server's own filtering of the same rows
    // (#2279), so they sum to the rows that name an epic.
    expect(dashboard.needs_you_by_epic).toEqual({
      "launch-fixes": 1,
      "go-live-2026-09": 1,
    });
    expect(dashboard.banner.fleet_stopped).toBe(false);
    // The artboard's three holds are a blocked node, a budget override and a
    // runner pause — none is a spend approval, so this list is empty.
    expect(approvals).toEqual([]);
    expect(fleet.stopped).toBe(false);
    expect(session.authenticated).toBe(true);
    expect(bundle.present).toBe(true);
    expect(meta.billing).toBe("metered");
  });

  it("files each needs-you row under the word its own sentence is built from", () => {
    // #2280. Monitor's kind column and the sentence beside it are two cells of
    // one row, and the hold vocabulary composes both wherever it claims the
    // reason: the committed round-cap hold is served as an `epic_wait`, so the
    // column read `OTHER` — the word `attention_kind: "held"` earns, which is
    // "no better word" — beside a sentence the `review` bucket wrote, while the
    // identical reason out of `epic_holds` read `REVIEW` two rows up.
    for (const row of dashboard.rows) {
      const claimed = describeHold(
        (row.reason ?? "").replace(/^\S+ — /, ""),
        { epic: row.epic_ref ?? "", item: row.item_id ?? "" },
      );
      if (claimed.label === "") continue;
      expect(holdKindOf(row), row.reason ?? "").toBe(claimed.category);
    }
    // Not vacuous, and the whole reading over the committed payload: the hold
    // is `review`, the merge decision `merge`, and the hold keeps the amber
    // ring its own stamp earns rather than the diamond `merge` would have
    // given it (#2280 D14).
    const rows = composeHolds(
      { epics: [], budget: {}, dashboard },
      Date.parse("2026-09-29T10:41:00+00:00"),
    ).rows;
    expect(rows.map((r) => [r.kind, r.mark])).toEqual([
      ["review", "held"],
      ["merge", "needs_you"],
    ]);
  });

  it("runs the event stream from 14:26:09 to 14:46:40, newest first", () => {
    expect(audit.events).toHaveLength(5);
    expect(auditStatus.events).toBe(audit.events.length);
    const times = audit.events.map((e) => e.timestamp);
    expect(times[0]).toBe("2026-10-03T14:46:40+00:00");
    expect(times[times.length - 1]).toBe("2026-10-03T14:26:09+00:00");
    expect([...times].sort().reverse()).toEqual(times);
    expect(audit.events.map((e) => e.event)).toEqual([
      "adversarial_review_verdict",
      "sandbox_resolved",
      "dispatch_owner_recorded",
      "dispatch_checkout_bound",
      "routing_decision",
    ]);
    expect(audit.next_after).toBeNull();
  });
});

/**
 * The scenario payloads (`fixtures/scenarios/`) a gallery shot swaps in, and
 * the one surface they exist for.
 */
const SCENARIO_DIR = new URL("../../fixtures/scenarios/", import.meta.url);

/** Scenario file -> the value this test assigned an `api.ts` type to above. */
const SCENARIOS: Record<string, unknown> = {
  "dashboard-sole-wait.json": soleWait,
  "dashboard-first-run.json": firstRunDashboard,
  "dashboard-work-first-run.json": firstRunProjection,
  "dashboard-work-held-round-due.json": heldRoundDue,
  "runs-first-run.json": firstRunRuns,
  "readiness-first-run.json": firstRunReadiness,
  "dashboard-launch-fixes-host-restart-repeated.json": hostRestartDashboard,
  "epics-launch-fixes-host-restart-repeated.json": hostRestartEpic,
  "dashboard-host-restart-repeated.json": hostRestartRepeated,
  "dashboard-merge-queue-stopped.json": mergeQueueStopped,
  "host-restart-preserved-continue.json": hostRestartPreserved,
  "health-image-missing.json": healthImageMissing,
  "prompts-versions.json": promptsVersionsScenario,
  "prompts-outcomes.json": promptsOutcomesScenario,
};

describe("the Work pane expands the one wait, from a committed payload", () => {
  // #2280 D11, and the acceptance criterion behind it: the expansion has to be
  // reachable from committed state, or no gallery shot can show the surface the
  // design round is about. The committed set cannot be the payload that shows
  // it — it carries two waits because the world it draws really has two, and
  // #2055's merge decision is in the set by construction (`_work_rows` emits a
  // row for every projection view that is not quiet, and an artifact awaiting a
  // human decision is not). So the one-wait reading is a scenario payload, and
  // `work-sole-wait` is the shot taken over it.
  const soleRows = mergeWorkList({ projection, dashboard: soleWait, runs });
  const queue = needsYouSummary(
    soleRows,
    soleWait,
    Date.parse("2026-09-24T16:48:00+00:00"),
  );

  it("is NOT reachable from the committed set, which is why this file exists", () => {
    const committed = needsYouSummary(
      mergeWorkList({ projection, dashboard, runs }),
      dashboard,
      Date.parse("2026-09-24T16:48:00+00:00"),
    );
    expect(committed.sole).toBeNull();
    expect(committed.entries).toHaveLength(2);
  });

  it("expands exactly one block, with the sentence, the age and the links", () => {
    expect(queue.headline).toBe("1 needs you");
    expect(queue.sole?.itemId).toBe("2055");
    const block = needsYouBlock(
      queue.sole!,
      Date.parse("2026-09-24T16:48:00+00:00"),
    );
    expect(block).not.toBeNull();
    expect(block!.label).toBe("Merge decision");
    // The server's sentence (#2539), verbatim — led by the merge verify that
    // failed at the PR's head (#3460), whose section leads the Do this row.
    expect(block!.summary).toBe(
      "Merge verify failed at 3670e43f: 2 of main's tests fail on this PR — the merge decision is yours",
    );
    expect(block!.why).toBe(workView.exception.detail);
    expect(block!.since).not.toBe("");
    // No command: the merge is made on the host, so `kew.waits` computes none
    // (`work-needs-you` is the shot that photographs a command block). The
    // server's links are Do this instead (#3467 D24): the failed verify is
    // the move and the PR follows it, quiet (design review c9 D10), and
    // neither is repeated in the quiet links under the block.
    expect(block!.command).toBe("");
    expect(block!.moves?.map((l) => [l.label, l.href, l.move])).toEqual([
      ["See the failed merge verify", "/work?item=2055#merge-verify", true],
      ["Open PR #2059 to merge", soleWait.rows[0].link!.href, false],
    ]);
    expect(block!.links.map((l) => l.label)).not.toContain("See the failed merge verify");
    expect(block!.external).toEqual([]);
  });

  it("invents nothing: its row is a row the committed set also serves", () => {
    // The scenario is the same world with go-live's hold released, so every
    // row in it is one the committed payload carries field for field — no
    // value is chosen here for a screenshot.
    expect(soleWait.rows).toHaveLength(1);
    for (const row of soleWait.rows) {
      expect(dashboard.rows).toEqual(expect.arrayContaining([row]));
    }
    // And the per-epic buckets are still the server's own filtering of those
    // rows, so the header's count and each epic's legend cannot disagree.
    const counted: Record<string, number> = {};
    for (const row of soleWait.rows) {
      if (row.epic_ref) counted[row.epic_ref] = (counted[row.epic_ref] ?? 0) + 1;
    }
    expect(soleWait.needs_you_by_epic).toEqual(counted);
    expect(soleWait.unavailable).toEqual([]);
  });

  it("is typed, committed and holds no orphan", () => {
    const files = readdirSync(fileURLToPath(SCENARIO_DIR))
      .filter((f) => f.endsWith(".json"))
      .sort();
    expect(files).toEqual(Object.keys(SCENARIOS).sort());
    for (const [file, value] of Object.entries(SCENARIOS)) {
      expect(value, file).not.toBeUndefined();
      expect(
        existsSync(fileURLToPath(new URL(file, SCENARIO_DIR))),
        file,
      ).toBe(true);
    }
  });

  it("is what a gallery shot actually serves", () => {
    // Read off `scripts/gallery.mjs`, so a scenario nothing photographs — or a
    // shot naming a file that is not committed — fails here rather than in a
    // review that finds the surface missing from the gallery.
    const source = readFileSync(
      fileURLToPath(new URL("../../scripts/gallery.mjs", import.meta.url)),
      "utf8",
    );
    const named = [
      ...source.matchAll(/'scenarios\/([A-Za-z0-9._-]+)'/g),
    ].map((m) => m[1]);
    expect(named).toContain("dashboard-sole-wait.json");
    for (const file of named) expect(SCENARIOS[file], file).not.toBeUndefined();
    expect(galleryShots().map((s) => s.name)).toContain("work-sole-wait");
  });
});

describe("the Work pane draws the first-run card, from a committed payload", () => {
  // #2515 AC4: the card needs a factory with no work and no runs, which the
  // committed set is not, so it is a scenario the gallery and ui-check both
  // swap in.
  const rows = mergeWorkList({
    projection: firstRunProjection,
    dashboard: firstRunDashboard,
    runs: firstRunRuns,
  });

  it("is a first run: no row, no run, every read answered", () => {
    expect(rows).toEqual([]);
    expect(
      isFirstRun({
        rows,
        projection: firstRunProjection,
        dashboard: firstRunDashboard,
        runs: firstRunRuns,
      }),
    ).toBe(true);
  });

  it("is NOT a first run in the committed set", () => {
    expect(
      isFirstRun({
        rows: mergeWorkList({ projection, dashboard, runs }),
        projection,
        dashboard,
        runs,
      }),
    ).toBe(false);
  });

  it("lists exactly the failing allowlist-network check, with its own fix", () => {
    const card = firstRunCard(firstRunReadiness);
    expect(card.checks.map((c) => c.id)).toEqual(["allowlist-network"]);
    expect(card.primary).toBe(card.checks[0].command);
    // One id per doctor check, as `run_checks` reports it.
    expect(new Set(firstRunReadiness.checks.map((c) => c.id)).size).toBe(
      firstRunReadiness.checks.length,
    );
  });

  it("is what the work-first-run gallery shot serves", () => {
    const source = readFileSync(
      fileURLToPath(new URL("../../scripts/gallery.mjs", import.meta.url)),
      "utf8",
    );
    for (const file of [
      "dashboard-first-run.json",
      "dashboard-work-first-run.json",
      "runs-first-run.json",
      "readiness-first-run.json",
    ]) {
      expect(source, file).toContain(`'scenarios/${file}'`);
    }
    expect(galleryShots().map((s) => s.name)).toContain("work-first-run");
  });
});

const SRC = new URL("../", import.meta.url);

function sourceFiles(dir: URL): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(fileURLToPath(dir))) {
    const path = new URL(entry, dir);
    if (statSync(fileURLToPath(path)).isDirectory()) {
      out.push(...sourceFiles(new URL(`${entry}/`, dir)));
    } else if (!entry.endsWith(".test.ts")) {
      out.push(fileURLToPath(path));
    }
  }
  return out;
}

describe("DRun — the run page's own three reads (C3/#2300)", () => {
  it("records no ledger event at the instant the ROW was written", () => {
    // The run row exists before anything is appended under its id, so no
    // production run has an event at exactly `started_at`. A fixture that
    // invented one made the Gantt bar's `?focusedTime=<started_at>` link
    // appear to resolve while in production it resolved to nothing at all.
    const started = Date.parse(runDetail.started_at ?? "");
    expect(Number.isNaN(started)).toBe(false);
    for (const event of runAudit.events) {
      expect(Date.parse(event.ts), event.event).toBeGreaterThan(started);
    }
  });

  it("opens the gallery's run page on its first event, not on nothing", () => {
    const href = runEventsHref(runDetail.run_id, {
      attempt: runDetail.attempt_number,
      focusedTime: runDetail.started_at,
    });
    const focusedTime = new URL(href, "http://localhost").searchParams.get(
      "focusedTime",
    );

    expect(focusedRunEvent(runAudit.events, focusedTime)?.seq).toBe(
      runAudit.events[0].seq,
    );
  });

  it("names whose governed-review decision the page is showing", () => {
    // The verdict is read for the work ITEM when no verdict names this run, so
    // the scope travels with it rather than the page printing another
    // attempt's round and reviewed head as this run's own evidence.
    expect(runDetail.verdict?.scope).toBe("item");
    expect(verdictScopeNote(runDetail.verdict?.scope ?? "")).toContain(
      "work item",
    );
  });

  it("agrees with its own ledger page about whose verdict that is", () => {
    // The two payloads must be mutually servable. The service reads the scope
    // off the ledger: a verdict event under THIS run's id is projected as
    // scope "run" (with the head and reviewer model taken from that event's
    // payload), and only when none names this run does the item's latest stand
    // in as scope "item". A gallery that claimed item scope while its own
    // audit page carried a verdict event pinned a state no backend can serve —
    // and the fixture is what the design review reads as production truth.
    const attested = runAudit.events.filter(
      (event) => event.event === "adversarial_review_verdict",
    );
    if (runDetail.verdict?.scope === VERDICT_SCOPE_RUN) {
      expect(attested.length).toBe(1);
      expect(attested[0].payload?.head_sha).toBe(runDetail.verdict?.head_sha);
      expect(attested[0].payload?.reviewer_model).toBe(
        runDetail.verdict?.reviewer_model,
      );
    } else {
      // A governed review attests under its OWN run id, so an implementation
      // run's page never carries the verdict standing over its work item.
      expect(attested).toEqual([]);
    }
  });

  it("holds less than one page, so the gallery shows no further-page control", () => {
    expect(runAudit.events.length).toBeLessThan(RUN_AUDIT_PAGE);
    expect(runAudit.next_seq).toBe(
      runAudit.events[runAudit.events.length - 1].seq,
    );
  });
});

/**
 * Every run status in the committed set, as `file -> path -> status`.
 *
 * A run status is recognised structurally, not by key name, because `status`
 * is three different vocabularies in this set: an epic node's `NodeStatus`
 * (`in_progress`, `in_review`, `failed`), a timeline band's reading of the
 * same, and a run's `RunStatus`. Two shapes carry the run one and only the run
 * one — an object with a `run_id` beside its `status` (`RunDTO`,
 * `EpicTimelineRun`, the epic node's run rows, the glance's recent
 * completions), and any object with a `run_status` (`WorkExecution`).
 */
function runStatusesIn(
  value: unknown,
  file: string,
  path = "",
  into: [string, string][] = [],
): [string, string][] {
  if (Array.isArray(value)) {
    value.forEach((item, i) => runStatusesIn(item, file, `${path}[${i}]`, into));
  } else if (value !== null && typeof value === "object") {
    const o = value as Record<string, unknown>;
    // A loop iteration names its parent run but records a moment, not a run
    // status (`IterationRecord.status` in kew/run_store.py is free-form).
    if ("run_id" in o && !("iteration_id" in o) && typeof o.status === "string")
      into.push([`${file}${path}.status`, o.status]);
    if (typeof o.run_status === "string")
      into.push([`${file}${path}.run_status`, o.run_status]);
    for (const [k, v] of Object.entries(o))
      runStatusesIn(v, file, `${path}.${k}`, into);
  }
  return into;
}

/**
 * Every work view in the committed set, as `file -> path -> view`.
 *
 * A view is recognised by the two dimensions the checks below read together —
 * an `execution` object beside an `exception` one — because that pairing is
 * what `compose_work_view` emits and nothing else in this set has it. Found
 * structurally rather than per file so a payload that grows a view (the
 * projection, a `verbs/work/<id>` read, a scenario) is covered by arriving.
 */
type FixtureWorkView = {
  execution: { state: string; run_status: string; started_at: string | null };
  exception: { state: string; detail: string };
  safest_action: { reason: string };
};

function workViewsIn(
  value: unknown,
  file: string,
  path = "",
  into: [string, FixtureWorkView][] = [],
): [string, FixtureWorkView][] {
  if (Array.isArray(value)) {
    value.forEach((item, i) => workViewsIn(item, file, `${path}[${i}]`, into));
  } else if (value !== null && typeof value === "object") {
    const o = value as Record<string, unknown>;
    const execution = o.execution as Record<string, unknown> | undefined;
    if (
      execution !== undefined &&
      typeof execution?.state === "string" &&
      typeof (o.exception as Record<string, unknown> | undefined)?.state ===
        "string"
    )
      into.push([`${file}${path}`, o as unknown as FixtureWorkView]);
    for (const [k, v] of Object.entries(o))
      workViewsIn(v, file, `${path}.${k}`, into);
  }
  return into;
}

/**
 * Every row in the set that claims a run is executing right now, as
 * `where -> started_at`: a `RunDTO` whose status is live, and a work view
 * whose execution dimension is `in_flight`. These are the rows whose SERVED
 * age decides whether the set reads as working or as abandoned.
 */
function inFlightRowsIn(
  value: unknown,
  file: string,
): [string, string | null][] {
  const rows: [string, string | null][] = [];
  const walk = (v: unknown, path: string): void => {
    if (Array.isArray(v)) {
      v.forEach((item, i) => walk(item, `${path}[${i}]`));
      return;
    }
    if (v === null || typeof v !== "object") return;
    const o = v as Record<string, unknown>;
    if ("run_id" in o && typeof o.status === "string" && LIVE_STATUSES.has(o.status))
      rows.push([
        `${file}${path}`,
        typeof o.started_at === "string" ? o.started_at : null,
      ]);
    for (const [k, value_] of Object.entries(o)) walk(value_, `${path}.${k}`);
  };
  walk(value, "");
  for (const [where, view] of workViewsIn(value, file))
    if (view.execution.state === "in_flight")
      rows.push([`${where}.execution`, view.execution.started_at]);
  return rows;
}

describe("every fixture run status is one the backend can emit", () => {
  // #2377. The fixtures carried `failed` and `running` — the epic node
  // vocabulary and `kpi.ts`'s display-only refinement of `pending`, neither of
  // which `run_store.RunStatus` has. So fixture mode drew a Runs page the
  // backend cannot produce: a 100% success rate with a failure in the list
  // (`failed` is terminal under no vocabulary), an "Unknown date" group for the
  // two in-flight runs, and a header counting work no reading called live.
  // Every design review, the gallery and `ui-check` read these payloads, so the
  // drift has to fail here rather than in a screenshot nobody can explain.
  const scenario = (file: string): unknown =>
    JSON.parse(readFileSync(fileURLToPath(new URL(file, SCENARIO_DIR)), "utf8"));
  const ALL: [string, unknown][] = [
    ...FIXTURE_FILES.map((f) => [f, committed(f)] as [string, unknown]),
    ...Object.keys(SCENARIOS).map((f) => [f, scenario(f)] as [string, unknown]),
  ];

  it("finds a run status to check in the files that carry runs", () => {
    // Guard on the guard: the check below is vacuous if the walk stops finding
    // run objects, which a renamed field or a reshaped payload would do
    // silently.
    const byFile = new Map(
      ALL.map(([file, payload]) => [file, runStatusesIn(payload, file).length]),
    );
    expect(byFile.get("runs.json")).toBe(runs.length);
    expect(byFile.get("runs-f5dcb6a5.json")).toBe(1);
    expect(byFile.get("dashboard-work.json")).toBe(projection.views.length);
    expect(byFile.get("epics-launch-fixes-timeline.json")).toBeGreaterThan(0);
    const total = [...byFile.values()].reduce((a, b) => a + b, 0);
    expect(total).toBeGreaterThan(0);
  });

  it("holds every one of them to run-status.ts's backend set", () => {
    const outside = ALL.flatMap(([file, payload]) =>
      runStatusesIn(payload, file).filter(
        ([, status]) => !BACKEND_RUN_STATUSES.has(status),
      ),
    );
    expect(outside, "statuses run_store.RunStatus has no word for").toEqual([]);
  });

  it("serves the in-flight run as a pending run that has begun", () => {
    // `pending` is the only status the host writes while a run executes, and a
    // stored run always has a start time (`RunRecord.started_at`). Both facts
    // are what the Monitor header's "2 working" and the Runs page's day
    // grouping read, through `kpi.ts`'s `displayStatus`.
    const live = runs.filter((r) => r.status === "pending");
    expect(live.map((r) => r.issue_num)).toEqual([2033]);
    for (const run of live) {
      expect(run.started_at, run.run_id).not.toBeNull();
      expect(run.finished_at, run.run_id).toBeNull();
      expect(
        displayStatus({ status: run.status, startedAt: run.started_at }),
      ).toBe("running");
    }
  });

  it("files every run under a dated day group, none under Unknown date", () => {
    // `groupRunsByDay` keys on `started_at ?? finished_at`, so the two runs
    // with neither were collected into an "Unknown date" heading on /runs —
    // which the server cannot produce, because a stored run has a start time.
    const days = groupRunsByDay(runs);
    expect(days.map((d) => d.day)).not.toContain("unknown");
    expect(days.map((d) => d.label)).not.toContain("Unknown date");
    expect(days.flatMap((d) => d.runs)).toHaveLength(runs.length);
  });

  it("counts the failure in the success rate the Runs page draws", () => {
    // The whole visible symptom: with `failed` the run was in no terminal set,
    // so the denominator was the four successes and the page read 100% beside
    // a failure in its own list.
    const kpis = runKpis(runs);
    expect(kpis.total).toBe(7);
    expect(kpis.terminal).toBe(6);
    expect(kpis.ok).toBe(5);
    expect(kpis.successRate).toBe(83);
    expect(kpis.live).toBe(1);
    expect(filterRuns(runs, "", "failed").map((r) => r.run_id)).toEqual([
      "c40b19de",
    ]);
  });

  /**
   * The clock a payload's own rows are read against.
   *
   * `FIXTURE_EPOCH` for almost everything: fixture mode shifts the set by
   * `now - FIXTURE_EPOCH`, so a row's served age is its age at the epoch, on
   * every day the dev server is opened. A payload that states its own `now` is
   * read against THAT instead — `nowOf` in `epic/timeline.ts` only falls back
   * to the viewer's clock when the server sent none — so a timeline's open
   * bands and bars are measured from the instant it was composed at.
   */
  const clockOf = (payload: unknown): number => {
    const now = (payload as { now?: unknown } | null)?.now;
    return typeof now === "string" && INSTANT.test(now)
      ? Date.parse(now)
      : Date.parse(FIXTURE_EPOCH);
  };

  it("serves every in-flight row inside kew's own pending bound", () => {
    // A start time is a reading the whole set is GATED on, which is why it
    // cannot be committed wherever the story reads nicely. `runs.json` sits
    // nine days behind the epoch, so start times taken from its own cluster
    // were served 8d23h old: DMonitor drew "8d23h" in the Elapsed cells, and a
    // pending row that old is one `service/work.py` reads as a host that died
    // (`_abandoned_pending_run`, `PENDING_RUN_MAX_AGE_S`) rather than as work
    // in progress — the fixture would claim "2 working" about two abandoned
    // runs (reviewer finding, PR #2441). So a row the set calls in flight is
    // anchored to the clock its own surface reads, not to its neighbours, and
    // this is the bound it is anchored by.
    // `kew/tests/test_factory_status_parity.py` holds this mirror to the
    // Python constant.
    const PENDING_RUN_MAX_AGE_MS = 4 * 60 * 60 * 1000;
    const inFlight = ALL.flatMap(([file, payload]) =>
      inFlightRowsIn(payload, file).map(
        ([where, startedAt]) =>
          [where, startedAt, clockOf(payload)] as [string, string | null, number],
      ),
    );
    // Not vacuous: the runs payload carries two, the work projection one, and
    // the two epic timelines their own open bars.
    expect(inFlight.length).toBeGreaterThanOrEqual(6);
    for (const [where, startedAt, clock] of inFlight) {
      expect(startedAt, where).toBeTruthy();
      const age = clock - Date.parse(startedAt!);
      expect(Number.isNaN(age), where).toBe(false);
      // Positive: a run that starts after the set is read has not begun.
      expect(age, `${where} is served from the future`).toBeGreaterThan(0);
      expect(age, `${where} is read ${age}ms old`).toBeLessThan(
        PENDING_RUN_MAX_AGE_MS,
      );
    }
  });

  it("draws the Monitor's elapsed cells in minutes, never in days", () => {
    // The reading itself, through the view DMonitor draws it with, at the clock
    // the browser reads the fixture surface against. The bound above is the
    // reason; this is what a reviewer sees if it is broken.
    const rows = liveRunRows(runs, Date.parse(FIXTURE_EPOCH));
    expect(rows.map((r) => r.elapsed)).toEqual(["30m"]);
    for (const row of rows) expect(row.elapsed, row.ref).toMatch(/^\d+[ms]$/);
  });

  it("derives the in-flight exception detail from the run status it serves", () => {
    // `_rule_in_flight` writes the detail as f"run {run_status}" and the safest
    // action's reason off the same word, so the two cannot disagree with the
    // execution dimension beside them. #2377 changed `run_status` to `pending`
    // and left "run running" standing in the derived detail — the display-only
    // word surviving in the one field that is not a free-text caption (reviewer
    // finding, PR #2441).
    const views = ALL.flatMap(([file, payload]) => workViewsIn(payload, file));
    const inFlight = views.filter(([, v]) => v.exception.state === "in_flight");
    // Not vacuous: the projection really does carry an in-flight view.
    expect(inFlight.length).toBeGreaterThan(0);
    for (const [where, view] of inFlight) {
      expect(view.exception.detail, where).toBe(
        `run ${view.execution.run_status}`,
      );
      expect(view.safest_action.reason, where).toBe(
        `run ${view.execution.run_status} — a second dispatch would duplicate it`,
      );
    }
  });
});

describe("nothing fixtured can reach production", () => {
  it("is never imported by a shipped source file", () => {
    for (const file of sourceFiles(SRC)) {
      const source = readFileSync(file, "utf8");
      // Both directories: `scenarios/` is dev-only for exactly the same
      // reasons `factory/` is, and a guard naming one of them would not have
      // noticed the other arriving.
      expect(source, file).not.toContain("fixtures/factory");
      expect(source, file).not.toContain("fixtures/scenarios");
    }
  });

  // jsdom is the other dev-only thing in this project (the operator's decision
  // 4 of 2026-10-02 allowed it for component interaction tests, #2248). Same
  // rule, so the same place: it may be named in devDependencies and in
  // vite.config.ts, and nowhere a bundle can reach.
  it("never ships jsdom, which is a test environment and not a dependency", () => {
    const pkg = JSON.parse(
      readFileSync(
        fileURLToPath(new URL("../../package.json", import.meta.url)),
        "utf8",
      ),
    ) as {
      dependencies?: Record<string, string>;
      devDependencies?: Record<string, string>;
    };
    expect(pkg.devDependencies?.jsdom).toBeDefined();
    expect(pkg.dependencies?.jsdom).toBeUndefined();
    // A USE of the package, not the word: a comment that explains why a suite
    // does without jsdom (src/lib/work/keys.ts, #2246) reaches no bundle.
    const uses = /(?:from\s*|import\s*\(\s*|require\s*\(\s*)["']jsdom["']/;
    for (const file of sourceFiles(SRC)) {
      expect(readFileSync(file, "utf8"), file).not.toMatch(uses);
    }
  });

  it("lives outside src/ and static/, so no build copies it", () => {
    for (const dir of [FIXTURE_DIR, SCENARIO_DIR].map((d) => fileURLToPath(d))) {
      expect(dir).not.toContain(`${fileURLToPath(SRC)}`);
      expect(dir).not.toContain("/static/");
    }
  });

  it("is gated on KEW_UI_FIXTURES in vite.config.ts, not on a dev guess", () => {
    const config = readFileSync(
      fileURLToPath(new URL("../../vite.config.ts", import.meta.url)),
      "utf8",
    );
    expect(config).toContain("fixtureApi()");
    expect(config).not.toContain("fixtures/factory");
  });

  it("leaves no fixture string in a build output that is present", () => {
    // `vite build` is not run from a unit test (minutes, and it needs
    // `svelte-kit sync`); when a build IS present — locally after `npm run
    // build`, or in CI after the bundle step — every file in it is grepped for
    // a string that exists nowhere but the fixtures.
    const build = new URL("../../build/", import.meta.url);
    if (!existsSync(fileURLToPath(build))) return;
    const sentinel = runs[0].run_id; // "9c41e2b7" — a fixture-only run id
    for (const file of sourceFiles(build)) {
      expect(readFileSync(file, "utf8"), file).not.toContain(sentinel);
    }
  });
});

describe("the repeated host restart scenario draws a failed node, not a dispatchable one", () => {
  // #2971: #2058 FAILED with `host_restart_repeated`. A frontier is what the
  // next tick may dispatch, and the ready queue is what the dashboard offers to,
  // so neither may hold it — and Work then draws it from its attention alone,
  // never as an idle queue row the attention was merged onto.
  it("keeps the failed node off the frontier and out of the ready queue", () => {
    const snapshot = hostRestartEpic.snapshot!;
    const node = snapshot.nodes.find((n) => n.id === "2058")!;
    expect(node.status).toBe("failed");
    expect(snapshot.frontier).not.toContain("2058");
    for (const id of snapshot.frontier) {
      const frontier = snapshot.nodes.find((n) => n.id === id)!;
      expect(frontier.status, id).toBe("todo");
    }
    expect(
      hostRestartDashboard.glance.ready_queue.map((issue) => issue.item_id ?? String(issue.issue_num)),
    ).not.toContain("2058");
  });

  it("draws #2058 on Work as the attention's own needs-you row", () => {
    const row = mergeWorkList({ projection, dashboard: hostRestartDashboard, runs }).find(
      (r) => r.itemId === "2058",
    )!;
    expect(row.entry?.attention_kind).toBe("host_restart_repeated");
    expect(row.state).toBe("manual");
    expect(row.needsYou).toBe(true);
    expect(row.tier).toBeNull();
  });
});

describe("the prompt catalog fixtures (#3019)", () => {
  const KINDS: PromptSourceKind[] = [
    "packaged-template",
    "config-template",
    "pinned-artifact",
    "deployed-artifact",
    "code",
  ];

  it("serves a preview for every role the catalog lists, under the same source", () => {
    expect(prompts.length).toBeGreaterThan(1);
    expect(Object.keys(promptPreviews).sort()).toEqual(
      prompts.map((r) => r.role).sort(),
    );
    for (const role of prompts) {
      expect(KINDS, role.role).toContain(role.source.kind);
      const read = promptPreviews[role.role];
      expect(read.role).toBe(role.role);
      expect(read.source).toEqual(role.source);
      expect(read.preview.length, role.role).toBeGreaterThan(0);
    }
  });

  it("holds the reviewer pinned, with a version and a sha256, as the page's chip needs", () => {
    const reviewer = prompts.find((r) => r.source.kind === "pinned-artifact");
    expect(reviewer?.source.version).toBeGreaterThan(0);
    expect(reviewer?.source.sha256).toMatch(/^[0-9a-f]{64}$/);
  });
});

describe("the operator profile fixtures (#3025)", () => {
  it("serves an empty history for exactly the roles a profile applies to", () => {
    const applies = prompts.filter((r) => r.profile_applies).map((r) => r.role).sort();
    expect(Object.keys(profileVersions).sort()).toEqual(applies);
    for (const [role, read] of Object.entries(profileVersions)) {
      expect(read.role).toBe(role);
      expect(read.versions).toEqual([]);
      expect(promptPreviews[role].variant.profile).toBe("none");
    }
  });

  it("walks propose then approve to v1 active, and previews it under its heading", () => {
    const { proposed, approved, preview_approved } = promptsVersionsScenario;
    expect(proposed.versions.map((v) => [v.version, v.state])).toEqual([[1, "proposed"]]);
    expect(approved.active_version).toBe(1);
    expect(approved.generation).toBe(proposed.generation + 1);
    expect(preview_approved.variant.profile).toBe("v1");
    expect(preview_approved.preview).toContain(
      `## Operator instructions — dispatch profile v1\n\n${approved.versions[0].body}`,
    );
  });

  it("carries the 403 and the 409 an approval can answer", () => {
    expect(promptsVersionsScenario.approve_refused.status).toBe(403);
    expect(promptsVersionsScenario.approve_stale.status).toBe(409);
  });
});
