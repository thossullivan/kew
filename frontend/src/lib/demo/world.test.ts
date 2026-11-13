/**
 * The demo world (#3141): the synthetic factory the demo build bundles.
 *
 * Pinned here:
 *
 * 1. **Every world file types.** Each payload is imported and assigned to its
 *    `api.ts` type, the way `fixtures.test.ts` types the factory set, so
 *    `npm run check` fails on a field the console's contract does not have;
 *    and the folder holds exactly the files imported below.
 * 2. **It tells the story the demo shows**: an epic in flight with a blocked
 *    node and a node waiting on a person, five needs-you items, a refused PR
 *    with a failed merge verify and a review send-back, and spend that
 *    reconciles run → node → epic → budget.
 * 3. **The demo build ships it**, never the factory set.
 *
 * That the committed world is what `generate.mjs` writes, and that the leak
 * scan passes over it, is `scripts/demo-world.test.ts` (it imports the
 * generator, which svelte-check does not read).
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { NOT_FIXTURES, readFixtureSet } from "../../../demo/fixture-bundle";
import { DEMO_FIXTURE_SET } from "../../../demo/fixture-set";
import manifest from "../../../demo/world/manifest.json";
import { mergeWorkList } from "../work/list";
import { needsYouSummary } from "../work/queue";
import type {
  AuditHistogram,
  AuditList,
  AuditStatus,
  BudgetMap,
  Bundle,
  Dashboard,
  Diff,
  DispatchPlan,
  EffectiveConfig,
  EpicDetail,
  EpicNodeDetail,
  EpicSummary,
  EpicTick,
  EpicTickStrip,
  EpicTimeline,
  FleetStatus,
  FlightTimeline,
  Health,
  HealthEnv,
  Hold,
  ItemDescription,
  ItemEvidence,
  ItemPipeline,
  Iteration,
  Loop,
  LoopAuditEvent,
  LoopDetail,
  Meta,
  OutcomeReport,
  OverridableMap,
  PrFreshness,
  ProfileVersions,
  PromptOutcomes,
  Readiness,
  ReadinessStatus,
  Run,
  RunAuditPage,
  Scorecard,
  SessionInfo,
  SpendWindows,
  WorkProjection,
  WorkView,
} from "../api";
import type { ReviewPolicy } from "../config-page";
import type { RunRequest } from "../verbs";

/** `[string, string][]` is a tuple pair; a JSON import only knows `string[]`. */
const pairs = (edges: string[][]): [string, string][] =>
  edges.map((e) => [e[0], e[1]] as [string, string]);

/** A JSON array of objects with different keys types as optional keys; on the wire the key is absent. */
/** A JSON import types a check's status as `string`; this narrows it, refusing a value `kew doctor` never serves. */
function readinessStatus(status: string): ReadinessStatus {
  if (status === "ok" || status === "warn" || status === "fail" || status === "skip") return status;
  throw new Error(`readiness.json: "${status}" is not a doctor status`);
}

function tallied(counts: Record<string, number | undefined>): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [key, count] of Object.entries(counts)) if (count !== undefined) out[key] = count;
  return out;
}

import approvalsJson from "../../../demo/world/approvals.json";
import auditHistogramJson from "../../../demo/world/audit-histogram.json";
import auditStatusJson from "../../../demo/world/audit-status.json";
import auditJson from "../../../demo/world/audit.json";
import authSessionJson from "../../../demo/world/auth-session.json";
import bundleJson from "../../../demo/world/bundle.json";
import configBudgetJson from "../../../demo/world/config-budget.json";
import configEffectiveJson from "../../../demo/world/config-effective.json";
import configHumanReviewJson from "../../../demo/world/config-human_review.json";
import configOverridableJson from "../../../demo/world/config-overridable.json";
import dashboardWorkJson from "../../../demo/world/dashboard-work.json";
import dashboardJson from "../../../demo/world/dashboard.json";
import dispatchResolveJson from "../../../demo/world/dispatch-resolve.json";
import dispatchResolve113Json from "../../../demo/world/dispatch-resolve/113.json";
import dispatchResolve114Json from "../../../demo/world/dispatch-resolve/114.json";
import epicsLedgerV2Nodes101Json from "../../../demo/world/epics-ledger-v2-nodes-101.json";
import epicsLedgerV2Nodes102Json from "../../../demo/world/epics-ledger-v2-nodes-102.json";
import epicsLedgerV2Nodes103Json from "../../../demo/world/epics-ledger-v2-nodes-103.json";
import epicsLedgerV2Nodes104Json from "../../../demo/world/epics-ledger-v2-nodes-104.json";
import epicsLedgerV2Nodes105Json from "../../../demo/world/epics-ledger-v2-nodes-105.json";
import epicsLedgerV2Nodes106Json from "../../../demo/world/epics-ledger-v2-nodes-106.json";
import epicsLedgerV2Nodes107Json from "../../../demo/world/epics-ledger-v2-nodes-107.json";
import epicsLedgerV2Nodes108Json from "../../../demo/world/epics-ledger-v2-nodes-108.json";
import epicsLedgerV2Nodes109Json from "../../../demo/world/epics-ledger-v2-nodes-109.json";
import epicsLedgerV2Nodes110Json from "../../../demo/world/epics-ledger-v2-nodes-110.json";
import epicsLedgerV2Nodes111Json from "../../../demo/world/epics-ledger-v2-nodes-111.json";
import epicsLedgerV2Nodes112Json from "../../../demo/world/epics-ledger-v2-nodes-112.json";
import epicsLedgerV2Nodes113Json from "../../../demo/world/epics-ledger-v2-nodes-113.json";
import epicsLedgerV2TickStripJson from "../../../demo/world/epics-ledger-v2-tick-strip.json";
import epicsLedgerV2TickStripChangesJson from "../../../demo/world/epics-ledger-v2-tick-strip-changes.json";
import epicsLedgerV2TicksJson from "../../../demo/world/epics-ledger-v2-ticks.json";
import epicsLedgerV2TimelineJson from "../../../demo/world/epics-ledger-v2-timeline.json";
import epicsLedgerV2Json from "../../../demo/world/epics-ledger-v2.json";
import epicsJson from "../../../demo/world/epics.json";
import fleetStatusJson from "../../../demo/world/fleet-status.json";
import healthEnvJson from "../../../demo/world/health-env.json";
import healthJson from "../../../demo/world/health.json";
import loopsJson from "../../../demo/world/loops.json";
import loopDetailJson from "../../../demo/world/loops-5f3c9a1e-2b7d-4c80-9e14-a6d2f08b7c35.json";
import loopIterationsJson from "../../../demo/world/loops-5f3c9a1e-2b7d-4c80-9e14-a6d2f08b7c35-iterations.json";
import loopAuditJson from "../../../demo/world/loops-5f3c9a1e-2b7d-4c80-9e14-a6d2f08b7c35-audit.json";
import metaJson from "../../../demo/world/meta.json";
import promptsAdversarialReviewOutcomesJson from "../../../demo/world/prompts-adversarial_review-outcomes.json";
import promptsAdversarialReviewVersionsJson from "../../../demo/world/prompts-adversarial_review-versions.json";
import promptsCiFixOutcomesJson from "../../../demo/world/prompts-ci_fix-outcomes.json";
import promptsCiFixVersionsJson from "../../../demo/world/prompts-ci_fix-versions.json";
import promptsDispatchOutcomesJson from "../../../demo/world/prompts-dispatch-outcomes.json";
import promptsDispatchVersionsJson from "../../../demo/world/prompts-dispatch-versions.json";
import promptsResolveOutcomesJson from "../../../demo/world/prompts-resolve-outcomes.json";
import promptsResolveVersionsJson from "../../../demo/world/prompts-resolve-versions.json";
import promptsReviewRemediationOutcomesJson from "../../../demo/world/prompts-review_remediation-outcomes.json";
import promptsReviewRemediationVersionsJson from "../../../demo/world/prompts-review_remediation-versions.json";
import prsFreshnessJson from "../../../demo/world/prs-freshness.json";
import readinessJson from "../../../demo/world/readiness.json";
import reportModelsJson from "../../../demo/world/report-models.json";
import reportOutcomesJson from "../../../demo/world/report-outcomes.json";
import reportSpendJson from "../../../demo/world/report-spend.json";
import runRequestsJson from "../../../demo/world/run-requests.json";
import runs0d1ef5f0AuditJson from "../../../demo/world/runs-0d1ef5f0-audit.json";
import runs0d1ef5f0FlightJson from "../../../demo/world/runs-0d1ef5f0-flight.json";
import runs0d1ef5f0Json from "../../../demo/world/runs-0d1ef5f0.json";
import runs1b464391AuditJson from "../../../demo/world/runs-1b464391-audit.json";
import runs1b464391FlightJson from "../../../demo/world/runs-1b464391-flight.json";
import runs1b464391Nodes1b4643913DiffJson from "../../../demo/world/runs-1b464391-nodes-1b464391_3-diff.json";
import runs1b464391Json from "../../../demo/world/runs-1b464391.json";
import runs45aac9aaAuditJson from "../../../demo/world/runs-45aac9aa-audit.json";
import runs45aac9aaFlightJson from "../../../demo/world/runs-45aac9aa-flight.json";
import runs45aac9aaJson from "../../../demo/world/runs-45aac9aa.json";
import runs5cad8a97AuditJson from "../../../demo/world/runs-5cad8a97-audit.json";
import runs5cad8a97FlightJson from "../../../demo/world/runs-5cad8a97-flight.json";
import runs5cad8a97Json from "../../../demo/world/runs-5cad8a97.json";
import runs6690a7f4AuditJson from "../../../demo/world/runs-6690a7f4-audit.json";
import runs6690a7f4FlightJson from "../../../demo/world/runs-6690a7f4-flight.json";
import runs6690a7f4Nodes6690a7f43DiffJson from "../../../demo/world/runs-6690a7f4-nodes-6690a7f4_3-diff.json";
import runs6690a7f4Json from "../../../demo/world/runs-6690a7f4.json";
import runs73b221bfAuditJson from "../../../demo/world/runs-73b221bf-audit.json";
import runs73b221bfFlightJson from "../../../demo/world/runs-73b221bf-flight.json";
import runs73b221bfJson from "../../../demo/world/runs-73b221bf.json";
import runs826d8cf0AuditJson from "../../../demo/world/runs-826d8cf0-audit.json";
import runs826d8cf0FlightJson from "../../../demo/world/runs-826d8cf0-flight.json";
import runs826d8cf0Nodes826d8cf03DiffJson from "../../../demo/world/runs-826d8cf0-nodes-826d8cf0_3-diff.json";
import runs826d8cf0Json from "../../../demo/world/runs-826d8cf0.json";
import runs9809cee6AuditJson from "../../../demo/world/runs-9809cee6-audit.json";
import runs9809cee6FlightJson from "../../../demo/world/runs-9809cee6-flight.json";
import runs9809cee6Json from "../../../demo/world/runs-9809cee6.json";
import runs9a8eb37dAuditJson from "../../../demo/world/runs-9a8eb37d-audit.json";
import runs9a8eb37dFlightJson from "../../../demo/world/runs-9a8eb37d-flight.json";
import runs9a8eb37dJson from "../../../demo/world/runs-9a8eb37d.json";
import runs9e5d7d74AuditJson from "../../../demo/world/runs-9e5d7d74-audit.json";
import runs9e5d7d74FlightJson from "../../../demo/world/runs-9e5d7d74-flight.json";
import runs9e5d7d74Json from "../../../demo/world/runs-9e5d7d74.json";
import runsA1fba96aAuditJson from "../../../demo/world/runs-a1fba96a-audit.json";
import runsA1fba96aFlightJson from "../../../demo/world/runs-a1fba96a-flight.json";
import runsA1fba96aJson from "../../../demo/world/runs-a1fba96a.json";
import runsAb4656b0AuditJson from "../../../demo/world/runs-ab4656b0-audit.json";
import runsAb4656b0FlightJson from "../../../demo/world/runs-ab4656b0-flight.json";
import runsAb4656b0Json from "../../../demo/world/runs-ab4656b0.json";
import runsB16e5025AuditJson from "../../../demo/world/runs-b16e5025-audit.json";
import runsB16e5025FlightJson from "../../../demo/world/runs-b16e5025-flight.json";
import runsB16e5025Json from "../../../demo/world/runs-b16e5025.json";
import runsB5b7e790AuditJson from "../../../demo/world/runs-b5b7e790-audit.json";
import runsB5b7e790FlightJson from "../../../demo/world/runs-b5b7e790-flight.json";
import runsB5b7e790Json from "../../../demo/world/runs-b5b7e790.json";
import runsD22c1ed4AuditJson from "../../../demo/world/runs-d22c1ed4-audit.json";
import runsD22c1ed4FlightJson from "../../../demo/world/runs-d22c1ed4-flight.json";
import runsD22c1ed4Json from "../../../demo/world/runs-d22c1ed4.json";
import runsD903a58eAuditJson from "../../../demo/world/runs-d903a58e-audit.json";
import runsD903a58eFlightJson from "../../../demo/world/runs-d903a58e-flight.json";
import runsD903a58eJson from "../../../demo/world/runs-d903a58e.json";
import runsD903a58eNodesD903a58e3DiffJson from "../../../demo/world/runs-d903a58e-nodes-d903a58e_3-diff.json";
import runsF220f966AuditJson from "../../../demo/world/runs-f220f966-audit.json";
import runsF220f966FlightJson from "../../../demo/world/runs-f220f966-flight.json";
import runsF220f966NodesF220f9663DiffJson from "../../../demo/world/runs-f220f966-nodes-f220f966_3-diff.json";
import runsF220f966Json from "../../../demo/world/runs-f220f966.json";
import runsF9910678AuditJson from "../../../demo/world/runs-f9910678-audit.json";
import runsF9910678FlightJson from "../../../demo/world/runs-f9910678-flight.json";
import runsF9910678NodesF99106783DiffJson from "../../../demo/world/runs-f9910678-nodes-f9910678_3-diff.json";
import runsF9910678Json from "../../../demo/world/runs-f9910678.json";
import runsFc4a4ea4AuditJson from "../../../demo/world/runs-fc4a4ea4-audit.json";
import runsFc4a4ea4FlightJson from "../../../demo/world/runs-fc4a4ea4-flight.json";
import runsFc4a4ea4NodesFc4a4ea43DiffJson from "../../../demo/world/runs-fc4a4ea4-nodes-fc4a4ea4_3-diff.json";
import runsFc4a4ea4Json from "../../../demo/world/runs-fc4a4ea4.json";
import runsFf3a322fAuditJson from "../../../demo/world/runs-ff3a322f-audit.json";
import runsFf3a322fFlightJson from "../../../demo/world/runs-ff3a322f-flight.json";
import runsFf3a322fNodesFf3a322f3DiffJson from "../../../demo/world/runs-ff3a322f-nodes-ff3a322f_3-diff.json";
import runsFf3a322fJson from "../../../demo/world/runs-ff3a322f.json";
import runsJson from "../../../demo/world/runs.json";
import verbsWork101Json from "../../../demo/world/verbs-work-101.json";
import verbsWork102Json from "../../../demo/world/verbs-work-102.json";
import verbsWork103Json from "../../../demo/world/verbs-work-103.json";
import verbsWork104Json from "../../../demo/world/verbs-work-104.json";
import verbsWork105Json from "../../../demo/world/verbs-work-105.json";
import verbsWork106Json from "../../../demo/world/verbs-work-106.json";
import verbsWork107Json from "../../../demo/world/verbs-work-107.json";
import verbsWork108Json from "../../../demo/world/verbs-work-108.json";
import verbsWork109Json from "../../../demo/world/verbs-work-109.json";
import work101DescriptionJson from "../../../demo/world/work-101-description.json";
import work101EvidenceJson from "../../../demo/world/work-101-evidence.json";
import work101PipelineJson from "../../../demo/world/work-101-pipeline.json";
import work102DescriptionJson from "../../../demo/world/work-102-description.json";
import work102EvidenceJson from "../../../demo/world/work-102-evidence.json";
import work102PipelineJson from "../../../demo/world/work-102-pipeline.json";
import work103DescriptionJson from "../../../demo/world/work-103-description.json";
import work103EvidenceJson from "../../../demo/world/work-103-evidence.json";
import work103PipelineJson from "../../../demo/world/work-103-pipeline.json";
import work104DescriptionJson from "../../../demo/world/work-104-description.json";
import work104EvidenceJson from "../../../demo/world/work-104-evidence.json";
import work104PipelineJson from "../../../demo/world/work-104-pipeline.json";
import work105DescriptionJson from "../../../demo/world/work-105-description.json";
import work105EvidenceJson from "../../../demo/world/work-105-evidence.json";
import work105PipelineJson from "../../../demo/world/work-105-pipeline.json";
import work106DescriptionJson from "../../../demo/world/work-106-description.json";
import work106EvidenceJson from "../../../demo/world/work-106-evidence.json";
import work106PipelineJson from "../../../demo/world/work-106-pipeline.json";
import work107DescriptionJson from "../../../demo/world/work-107-description.json";
import work107EvidenceJson from "../../../demo/world/work-107-evidence.json";
import work107PipelineJson from "../../../demo/world/work-107-pipeline.json";
import work108DescriptionJson from "../../../demo/world/work-108-description.json";
import work108EvidenceJson from "../../../demo/world/work-108-evidence.json";
import work108PipelineJson from "../../../demo/world/work-108-pipeline.json";
import work109DescriptionJson from "../../../demo/world/work-109-description.json";
import work109EvidenceJson from "../../../demo/world/work-109-evidence.json";
import work109PipelineJson from "../../../demo/world/work-109-pipeline.json";
import work110DescriptionJson from "../../../demo/world/work-110-description.json";
import work110EvidenceJson from "../../../demo/world/work-110-evidence.json";
import work110PipelineJson from "../../../demo/world/work-110-pipeline.json";
import work111DescriptionJson from "../../../demo/world/work-111-description.json";
import work111EvidenceJson from "../../../demo/world/work-111-evidence.json";
import work111PipelineJson from "../../../demo/world/work-111-pipeline.json";
import work112DescriptionJson from "../../../demo/world/work-112-description.json";
import work112EvidenceJson from "../../../demo/world/work-112-evidence.json";
import work112PipelineJson from "../../../demo/world/work-112-pipeline.json";
import work113DescriptionJson from "../../../demo/world/work-113-description.json";
import work113EvidenceJson from "../../../demo/world/work-113-evidence.json";
import work113PipelineJson from "../../../demo/world/work-113-pipeline.json";
import work114DescriptionJson from "../../../demo/world/work-114-description.json";
import work114EvidenceJson from "../../../demo/world/work-114-evidence.json";
import work114PipelineJson from "../../../demo/world/work-114-pipeline.json";

const approvals: Hold[] = approvalsJson;
const auditHistogram: AuditHistogram = { ...auditHistogramJson, buckets: auditHistogramJson.buckets.map((b) => ({ ...b, counts: tallied(b.counts) })) };
const auditStatus: AuditStatus = auditStatusJson;
const audit: AuditList = auditJson;
const authSession: SessionInfo = authSessionJson;
const bundle: Bundle = bundleJson;
const configBudget: BudgetMap = configBudgetJson;
const configEffective: EffectiveConfig = configEffectiveJson;
const configHumanReview: ReviewPolicy = configHumanReviewJson;
const configOverridable: OverridableMap = configOverridableJson;
const dashboardWork: WorkProjection = dashboardWorkJson;
const dashboard: Dashboard = dashboardJson;
const dispatchResolve: DispatchPlan = dispatchResolveJson;
const dispatchResolve113: DispatchPlan = dispatchResolve113Json;
const dispatchResolve114: DispatchPlan = dispatchResolve114Json;
const epicsLedgerV2Nodes101: EpicNodeDetail = epicsLedgerV2Nodes101Json;
const epicsLedgerV2Nodes102: EpicNodeDetail = epicsLedgerV2Nodes102Json;
const epicsLedgerV2Nodes103: EpicNodeDetail = epicsLedgerV2Nodes103Json;
const epicsLedgerV2Nodes104: EpicNodeDetail = epicsLedgerV2Nodes104Json;
const epicsLedgerV2Nodes105: EpicNodeDetail = epicsLedgerV2Nodes105Json;
const epicsLedgerV2Nodes106: EpicNodeDetail = epicsLedgerV2Nodes106Json;
const epicsLedgerV2Nodes107: EpicNodeDetail = epicsLedgerV2Nodes107Json;
const epicsLedgerV2Nodes108: EpicNodeDetail = epicsLedgerV2Nodes108Json;
const epicsLedgerV2Nodes109: EpicNodeDetail = epicsLedgerV2Nodes109Json;
const epicsLedgerV2Nodes110: EpicNodeDetail = epicsLedgerV2Nodes110Json;
const epicsLedgerV2Nodes111: EpicNodeDetail = epicsLedgerV2Nodes111Json;
const epicsLedgerV2Nodes112: EpicNodeDetail = epicsLedgerV2Nodes112Json;
const epicsLedgerV2Nodes113: EpicNodeDetail = epicsLedgerV2Nodes113Json;
const epicsLedgerV2TickStrip: EpicTickStrip[] = epicsLedgerV2TickStripJson;
const epicsLedgerV2TickStripChanges: EpicTickStrip[] = epicsLedgerV2TickStripChangesJson;
const epicsLedgerV2Ticks: EpicTick[] = epicsLedgerV2TicksJson.map((tick) => ({ ...tick, snapshot: { ...tick.snapshot, edges: pairs(tick.snapshot.edges) }, economics: { ...tick.economics, node_drain_usd: tallied(tick.economics.node_drain_usd) } }));
const epicsLedgerV2Timeline: EpicTimeline = epicsLedgerV2TimelineJson;
const epicsLedgerV2: EpicDetail = { ...epicsLedgerV2Json, snapshot: { ...epicsLedgerV2Json.snapshot, edges: pairs(epicsLedgerV2Json.snapshot.edges) } };
const epics: EpicSummary[] = epicsJson.map((summary) => ({ ...summary, counts: tallied(summary.counts) }));
const fleetStatus: FleetStatus = fleetStatusJson;
const healthEnv: HealthEnv = healthEnvJson;
const health: Health = healthJson;
const loops: Loop[] = loopsJson;
const loopDetail: LoopDetail = loopDetailJson;
const loopIterations: Iteration[] = loopIterationsJson;
const loopAudit: LoopAuditEvent[] = loopAuditJson;
const meta: Meta = metaJson;
const promptsAdversarialReviewOutcomes: PromptOutcomes = { ...promptsAdversarialReviewOutcomesJson, kind: "reviewer" };
const promptsAdversarialReviewVersions: ProfileVersions = promptsAdversarialReviewVersionsJson;
const promptsCiFixOutcomes: PromptOutcomes = { ...promptsCiFixOutcomesJson, kind: "implementer" };
const promptsCiFixVersions: ProfileVersions = promptsCiFixVersionsJson;
const promptsDispatchOutcomes: PromptOutcomes = { ...promptsDispatchOutcomesJson, kind: "implementer" };
const promptsDispatchVersions: ProfileVersions = promptsDispatchVersionsJson;
const promptsResolveOutcomes: PromptOutcomes = { ...promptsResolveOutcomesJson, kind: "implementer" };
const promptsResolveVersions: ProfileVersions = promptsResolveVersionsJson;
const promptsReviewRemediationOutcomes: PromptOutcomes = { ...promptsReviewRemediationOutcomesJson, kind: "implementer" };
const promptsReviewRemediationVersions: ProfileVersions = promptsReviewRemediationVersionsJson;
const prsFreshness: PrFreshness = prsFreshnessJson;
const readiness: Readiness = { ...readinessJson, checks: readinessJson.checks.map((c) => ({ ...c, status: readinessStatus(c.status) })) };
const reportModels: Scorecard = reportModelsJson;
const reportOutcomes: OutcomeReport = reportOutcomesJson;
const reportSpend: SpendWindows = reportSpendJson;
const runRequests: { requests: RunRequest[] } = runRequestsJson;
const runs0d1ef5f0Audit: RunAuditPage = runs0d1ef5f0AuditJson;
const runs0d1ef5f0Flight: FlightTimeline = runs0d1ef5f0FlightJson;
const runs0d1ef5f0: Run = runs0d1ef5f0Json;
const runs1b464391Audit: RunAuditPage = runs1b464391AuditJson;
const runs1b464391Flight: FlightTimeline = runs1b464391FlightJson;
const runs1b464391Nodes1b4643913Diff: Diff = runs1b464391Nodes1b4643913DiffJson;
const runs1b464391: Run = runs1b464391Json;
const runs45aac9aaAudit: RunAuditPage = runs45aac9aaAuditJson;
const runs45aac9aaFlight: FlightTimeline = runs45aac9aaFlightJson;
const runs45aac9aa: Run = runs45aac9aaJson;
const runs5cad8a97Audit: RunAuditPage = runs5cad8a97AuditJson;
const runs5cad8a97Flight: FlightTimeline = runs5cad8a97FlightJson;
const runs5cad8a97: Run = runs5cad8a97Json;
const runs6690a7f4Audit: RunAuditPage = runs6690a7f4AuditJson;
const runs6690a7f4Flight: FlightTimeline = runs6690a7f4FlightJson;
const runs6690a7f4Nodes6690a7f43Diff: Diff = runs6690a7f4Nodes6690a7f43DiffJson;
const runs6690a7f4: Run = runs6690a7f4Json;
const runs73b221bfAudit: RunAuditPage = runs73b221bfAuditJson;
const runs73b221bfFlight: FlightTimeline = runs73b221bfFlightJson;
const runs73b221bf: Run = runs73b221bfJson;
const runs826d8cf0Audit: RunAuditPage = runs826d8cf0AuditJson;
const runs826d8cf0Flight: FlightTimeline = runs826d8cf0FlightJson;
const runs826d8cf0Nodes826d8cf03Diff: Diff = runs826d8cf0Nodes826d8cf03DiffJson;
const runs826d8cf0: Run = runs826d8cf0Json;
const runs9809cee6Audit: RunAuditPage = runs9809cee6AuditJson;
const runs9809cee6Flight: FlightTimeline = runs9809cee6FlightJson;
const runs9809cee6: Run = runs9809cee6Json;
const runs9a8eb37dAudit: RunAuditPage = runs9a8eb37dAuditJson;
const runs9a8eb37dFlight: FlightTimeline = runs9a8eb37dFlightJson;
const runs9a8eb37d: Run = runs9a8eb37dJson;
const runs9e5d7d74Audit: RunAuditPage = runs9e5d7d74AuditJson;
const runs9e5d7d74Flight: FlightTimeline = runs9e5d7d74FlightJson;
const runs9e5d7d74: Run = runs9e5d7d74Json;
const runsA1fba96aAudit: RunAuditPage = runsA1fba96aAuditJson;
const runsA1fba96aFlight: FlightTimeline = runsA1fba96aFlightJson;
const runsA1fba96a: Run = runsA1fba96aJson;
const runsAb4656b0Audit: RunAuditPage = runsAb4656b0AuditJson;
const runsAb4656b0Flight: FlightTimeline = runsAb4656b0FlightJson;
const runsAb4656b0: Run = runsAb4656b0Json;
const runsB16e5025Audit: RunAuditPage = runsB16e5025AuditJson;
const runsB16e5025Flight: FlightTimeline = runsB16e5025FlightJson;
const runsB16e5025: Run = runsB16e5025Json;
const runsB5b7e790Audit: RunAuditPage = runsB5b7e790AuditJson;
const runsB5b7e790Flight: FlightTimeline = runsB5b7e790FlightJson;
const runsB5b7e790: Run = runsB5b7e790Json;
const runsD22c1ed4Audit: RunAuditPage = runsD22c1ed4AuditJson;
const runsD22c1ed4Flight: FlightTimeline = runsD22c1ed4FlightJson;
const runsD22c1ed4: Run = runsD22c1ed4Json;
const runsD903a58eAudit: RunAuditPage = runsD903a58eAuditJson;
const runsD903a58eFlight: FlightTimeline = runsD903a58eFlightJson;
const runsD903a58e: Run = runsD903a58eJson;
const runsD903a58eNodesD903a58e3Diff: Diff = runsD903a58eNodesD903a58e3DiffJson;
const runsF220f966Audit: RunAuditPage = runsF220f966AuditJson;
const runsF220f966Flight: FlightTimeline = runsF220f966FlightJson;
const runsF220f966NodesF220f9663Diff: Diff = runsF220f966NodesF220f9663DiffJson;
const runsF220f966: Run = runsF220f966Json;
const runsF9910678Audit: RunAuditPage = runsF9910678AuditJson;
const runsF9910678Flight: FlightTimeline = runsF9910678FlightJson;
const runsF9910678NodesF99106783Diff: Diff = runsF9910678NodesF99106783DiffJson;
const runsF9910678: Run = runsF9910678Json;
const runsFc4a4ea4Audit: RunAuditPage = runsFc4a4ea4AuditJson;
const runsFc4a4ea4Flight: FlightTimeline = runsFc4a4ea4FlightJson;
const runsFc4a4ea4NodesFc4a4ea43Diff: Diff = runsFc4a4ea4NodesFc4a4ea43DiffJson;
const runsFc4a4ea4: Run = runsFc4a4ea4Json;
const runsFf3a322fAudit: RunAuditPage = runsFf3a322fAuditJson;
const runsFf3a322fFlight: FlightTimeline = runsFf3a322fFlightJson;
const runsFf3a322fNodesFf3a322f3Diff: Diff = runsFf3a322fNodesFf3a322f3DiffJson;
const runsFf3a322f: Run = runsFf3a322fJson;
const runs: Run[] = runsJson;
const verbsWork101: WorkView = verbsWork101Json;
const verbsWork102: WorkView = verbsWork102Json;
const verbsWork103: WorkView = verbsWork103Json;
const verbsWork104: WorkView = verbsWork104Json;
const verbsWork105: WorkView = verbsWork105Json;
const verbsWork106: WorkView = verbsWork106Json;
const verbsWork107: WorkView = verbsWork107Json;
const verbsWork108: WorkView = verbsWork108Json;
const verbsWork109: WorkView = verbsWork109Json;
const work101Description: ItemDescription = work101DescriptionJson;
const work101Evidence: ItemEvidence = work101EvidenceJson;
const work101Pipeline: ItemPipeline = work101PipelineJson;
const work102Description: ItemDescription = work102DescriptionJson;
const work102Evidence: ItemEvidence = work102EvidenceJson;
const work102Pipeline: ItemPipeline = work102PipelineJson;
const work103Description: ItemDescription = work103DescriptionJson;
const work103Evidence: ItemEvidence = work103EvidenceJson;
const work103Pipeline: ItemPipeline = work103PipelineJson;
const work104Description: ItemDescription = work104DescriptionJson;
const work104Evidence: ItemEvidence = work104EvidenceJson;
const work104Pipeline: ItemPipeline = work104PipelineJson;
const work105Description: ItemDescription = work105DescriptionJson;
const work105Evidence: ItemEvidence = work105EvidenceJson;
const work105Pipeline: ItemPipeline = work105PipelineJson;
const work106Description: ItemDescription = work106DescriptionJson;
const work106Evidence: ItemEvidence = work106EvidenceJson;
const work106Pipeline: ItemPipeline = work106PipelineJson;
const work107Description: ItemDescription = work107DescriptionJson;
const work107Evidence: ItemEvidence = work107EvidenceJson;
const work107Pipeline: ItemPipeline = work107PipelineJson;
const work108Description: ItemDescription = work108DescriptionJson;
const work108Evidence: ItemEvidence = work108EvidenceJson;
const work108Pipeline: ItemPipeline = work108PipelineJson;
const work109Description: ItemDescription = work109DescriptionJson;
const work109Evidence: ItemEvidence = work109EvidenceJson;
const work109Pipeline: ItemPipeline = work109PipelineJson;
const work110Description: ItemDescription = work110DescriptionJson;
const work110Evidence: ItemEvidence = work110EvidenceJson;
const work110Pipeline: ItemPipeline = work110PipelineJson;
const work111Description: ItemDescription = work111DescriptionJson;
const work111Evidence: ItemEvidence = work111EvidenceJson;
const work111Pipeline: ItemPipeline = work111PipelineJson;
const work112Description: ItemDescription = work112DescriptionJson;
const work112Evidence: ItemEvidence = work112EvidenceJson;
const work112Pipeline: ItemPipeline = work112PipelineJson;
const work113Description: ItemDescription = work113DescriptionJson;
const work113Evidence: ItemEvidence = work113EvidenceJson;
const work113Pipeline: ItemPipeline = work113PipelineJson;
const work114Description: ItemDescription = work114DescriptionJson;
const work114Evidence: ItemEvidence = work114EvidenceJson;
const work114Pipeline: ItemPipeline = work114PipelineJson;

/** Every world fixture, typed, by its path in the set. */
const TYPED: Record<string, unknown> = {
  "approvals.json": approvals,
  "audit-histogram.json": auditHistogram,
  "audit-status.json": auditStatus,
  "audit.json": audit,
  "auth-session.json": authSession,
  "bundle.json": bundle,
  "config-budget.json": configBudget,
  "config-effective.json": configEffective,
  "config-human_review.json": configHumanReview,
  "config-overridable.json": configOverridable,
  "dashboard-work.json": dashboardWork,
  "dashboard.json": dashboard,
  "dispatch-resolve.json": dispatchResolve,
  "dispatch-resolve/113.json": dispatchResolve113,
  "dispatch-resolve/114.json": dispatchResolve114,
  "epics-ledger-v2-nodes-101.json": epicsLedgerV2Nodes101,
  "epics-ledger-v2-nodes-102.json": epicsLedgerV2Nodes102,
  "epics-ledger-v2-nodes-103.json": epicsLedgerV2Nodes103,
  "epics-ledger-v2-nodes-104.json": epicsLedgerV2Nodes104,
  "epics-ledger-v2-nodes-105.json": epicsLedgerV2Nodes105,
  "epics-ledger-v2-nodes-106.json": epicsLedgerV2Nodes106,
  "epics-ledger-v2-nodes-107.json": epicsLedgerV2Nodes107,
  "epics-ledger-v2-nodes-108.json": epicsLedgerV2Nodes108,
  "epics-ledger-v2-nodes-109.json": epicsLedgerV2Nodes109,
  "epics-ledger-v2-nodes-110.json": epicsLedgerV2Nodes110,
  "epics-ledger-v2-nodes-111.json": epicsLedgerV2Nodes111,
  "epics-ledger-v2-nodes-112.json": epicsLedgerV2Nodes112,
  "epics-ledger-v2-nodes-113.json": epicsLedgerV2Nodes113,
  "epics-ledger-v2-tick-strip.json": epicsLedgerV2TickStrip,
  "epics-ledger-v2-tick-strip-changes.json": epicsLedgerV2TickStripChanges,
  "epics-ledger-v2-ticks.json": epicsLedgerV2Ticks,
  "epics-ledger-v2-timeline.json": epicsLedgerV2Timeline,
  "epics-ledger-v2.json": epicsLedgerV2,
  "epics.json": epics,
  "fleet-status.json": fleetStatus,
  "health-env.json": healthEnv,
  "health.json": health,
  "loops.json": loops,
  "loops-5f3c9a1e-2b7d-4c80-9e14-a6d2f08b7c35.json": loopDetail,
  "loops-5f3c9a1e-2b7d-4c80-9e14-a6d2f08b7c35-iterations.json": loopIterations,
  "loops-5f3c9a1e-2b7d-4c80-9e14-a6d2f08b7c35-audit.json": loopAudit,
  "meta.json": meta,
  "prompts-adversarial_review-outcomes.json": promptsAdversarialReviewOutcomes,
  "prompts-adversarial_review-versions.json": promptsAdversarialReviewVersions,
  "prompts-ci_fix-outcomes.json": promptsCiFixOutcomes,
  "prompts-ci_fix-versions.json": promptsCiFixVersions,
  "prompts-dispatch-outcomes.json": promptsDispatchOutcomes,
  "prompts-dispatch-versions.json": promptsDispatchVersions,
  "prompts-resolve-outcomes.json": promptsResolveOutcomes,
  "prompts-resolve-versions.json": promptsResolveVersions,
  "prompts-review_remediation-outcomes.json": promptsReviewRemediationOutcomes,
  "prompts-review_remediation-versions.json": promptsReviewRemediationVersions,
  "prs-freshness.json": prsFreshness,
  "readiness.json": readiness,
  "report-models.json": reportModels,
  "report-outcomes.json": reportOutcomes,
  "report-spend.json": reportSpend,
  "run-requests.json": runRequests,
  "runs-0d1ef5f0-audit.json": runs0d1ef5f0Audit,
  "runs-0d1ef5f0-flight.json": runs0d1ef5f0Flight,
  "runs-0d1ef5f0.json": runs0d1ef5f0,
  "runs-1b464391-audit.json": runs1b464391Audit,
  "runs-1b464391-flight.json": runs1b464391Flight,
  "runs-1b464391-nodes-1b464391_3-diff.json": runs1b464391Nodes1b4643913Diff,
  "runs-1b464391.json": runs1b464391,
  "runs-45aac9aa-audit.json": runs45aac9aaAudit,
  "runs-45aac9aa-flight.json": runs45aac9aaFlight,
  "runs-45aac9aa.json": runs45aac9aa,
  "runs-5cad8a97-audit.json": runs5cad8a97Audit,
  "runs-5cad8a97-flight.json": runs5cad8a97Flight,
  "runs-5cad8a97.json": runs5cad8a97,
  "runs-6690a7f4-audit.json": runs6690a7f4Audit,
  "runs-6690a7f4-flight.json": runs6690a7f4Flight,
  "runs-6690a7f4-nodes-6690a7f4_3-diff.json": runs6690a7f4Nodes6690a7f43Diff,
  "runs-6690a7f4.json": runs6690a7f4,
  "runs-73b221bf-audit.json": runs73b221bfAudit,
  "runs-73b221bf-flight.json": runs73b221bfFlight,
  "runs-73b221bf.json": runs73b221bf,
  "runs-826d8cf0-audit.json": runs826d8cf0Audit,
  "runs-826d8cf0-flight.json": runs826d8cf0Flight,
  "runs-826d8cf0-nodes-826d8cf0_3-diff.json": runs826d8cf0Nodes826d8cf03Diff,
  "runs-826d8cf0.json": runs826d8cf0,
  "runs-9809cee6-audit.json": runs9809cee6Audit,
  "runs-9809cee6-flight.json": runs9809cee6Flight,
  "runs-9809cee6.json": runs9809cee6,
  "runs-9a8eb37d-audit.json": runs9a8eb37dAudit,
  "runs-9a8eb37d-flight.json": runs9a8eb37dFlight,
  "runs-9a8eb37d.json": runs9a8eb37d,
  "runs-9e5d7d74-audit.json": runs9e5d7d74Audit,
  "runs-9e5d7d74-flight.json": runs9e5d7d74Flight,
  "runs-9e5d7d74.json": runs9e5d7d74,
  "runs-a1fba96a-audit.json": runsA1fba96aAudit,
  "runs-a1fba96a-flight.json": runsA1fba96aFlight,
  "runs-a1fba96a.json": runsA1fba96a,
  "runs-ab4656b0-audit.json": runsAb4656b0Audit,
  "runs-ab4656b0-flight.json": runsAb4656b0Flight,
  "runs-ab4656b0.json": runsAb4656b0,
  "runs-b16e5025-audit.json": runsB16e5025Audit,
  "runs-b16e5025-flight.json": runsB16e5025Flight,
  "runs-b16e5025.json": runsB16e5025,
  "runs-b5b7e790-audit.json": runsB5b7e790Audit,
  "runs-b5b7e790-flight.json": runsB5b7e790Flight,
  "runs-b5b7e790.json": runsB5b7e790,
  "runs-d22c1ed4-audit.json": runsD22c1ed4Audit,
  "runs-d22c1ed4-flight.json": runsD22c1ed4Flight,
  "runs-d22c1ed4.json": runsD22c1ed4,
  "runs-d903a58e-audit.json": runsD903a58eAudit,
  "runs-d903a58e-flight.json": runsD903a58eFlight,
  "runs-d903a58e.json": runsD903a58e,
  "runs-d903a58e-nodes-d903a58e_3-diff.json": runsD903a58eNodesD903a58e3Diff,
  "runs-f220f966-audit.json": runsF220f966Audit,
  "runs-f220f966-flight.json": runsF220f966Flight,
  "runs-f220f966-nodes-f220f966_3-diff.json": runsF220f966NodesF220f9663Diff,
  "runs-f220f966.json": runsF220f966,
  "runs-f9910678-audit.json": runsF9910678Audit,
  "runs-f9910678-flight.json": runsF9910678Flight,
  "runs-f9910678-nodes-f9910678_3-diff.json": runsF9910678NodesF99106783Diff,
  "runs-f9910678.json": runsF9910678,
  "runs-fc4a4ea4-audit.json": runsFc4a4ea4Audit,
  "runs-fc4a4ea4-flight.json": runsFc4a4ea4Flight,
  "runs-fc4a4ea4-nodes-fc4a4ea4_3-diff.json": runsFc4a4ea4NodesFc4a4ea43Diff,
  "runs-fc4a4ea4.json": runsFc4a4ea4,
  "runs-ff3a322f-audit.json": runsFf3a322fAudit,
  "runs-ff3a322f-flight.json": runsFf3a322fFlight,
  "runs-ff3a322f-nodes-ff3a322f_3-diff.json": runsFf3a322fNodesFf3a322f3Diff,
  "runs-ff3a322f.json": runsFf3a322f,
  "runs.json": runs,
  "verbs-work-101.json": verbsWork101,
  "verbs-work-102.json": verbsWork102,
  "verbs-work-103.json": verbsWork103,
  "verbs-work-104.json": verbsWork104,
  "verbs-work-105.json": verbsWork105,
  "verbs-work-106.json": verbsWork106,
  "verbs-work-107.json": verbsWork107,
  "verbs-work-108.json": verbsWork108,
  "verbs-work-109.json": verbsWork109,
  "work-101-description.json": work101Description,
  "work-101-evidence.json": work101Evidence,
  "work-101-pipeline.json": work101Pipeline,
  "work-102-description.json": work102Description,
  "work-102-evidence.json": work102Evidence,
  "work-102-pipeline.json": work102Pipeline,
  "work-103-description.json": work103Description,
  "work-103-evidence.json": work103Evidence,
  "work-103-pipeline.json": work103Pipeline,
  "work-104-description.json": work104Description,
  "work-104-evidence.json": work104Evidence,
  "work-104-pipeline.json": work104Pipeline,
  "work-105-description.json": work105Description,
  "work-105-evidence.json": work105Evidence,
  "work-105-pipeline.json": work105Pipeline,
  "work-106-description.json": work106Description,
  "work-106-evidence.json": work106Evidence,
  "work-106-pipeline.json": work106Pipeline,
  "work-107-description.json": work107Description,
  "work-107-evidence.json": work107Evidence,
  "work-107-pipeline.json": work107Pipeline,
  "work-108-description.json": work108Description,
  "work-108-evidence.json": work108Evidence,
  "work-108-pipeline.json": work108Pipeline,
  "work-109-description.json": work109Description,
  "work-109-evidence.json": work109Evidence,
  "work-109-pipeline.json": work109Pipeline,
  "work-110-description.json": work110Description,
  "work-110-evidence.json": work110Evidence,
  "work-110-pipeline.json": work110Pipeline,
  "work-111-description.json": work111Description,
  "work-111-evidence.json": work111Evidence,
  "work-111-pipeline.json": work111Pipeline,
  "work-112-description.json": work112Description,
  "work-112-evidence.json": work112Evidence,
  "work-112-pipeline.json": work112Pipeline,
  "work-113-description.json": work113Description,
  "work-113-evidence.json": work113Evidence,
  "work-113-pipeline.json": work113Pipeline,
  "work-114-description.json": work114Description,
  "work-114-evidence.json": work114Evidence,
  "work-114-pipeline.json": work114Pipeline,
};

const WORLD = new URL("../../../demo/world/", import.meta.url);
const ROOT = fileURLToPath(WORLD);

/** Every `*.json` under `dir`, relative, `/`-separated. */
function jsonUnder(dir: string, root = dir): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return jsonUnder(path, root);
    return name.endsWith(".json") ? [relative(root, path).split(sep).join("/")] : [];
  });
}

/** The prompt previews and the model catalog, which the world copies from the factory set as they are; a role's outcomes and versions are generated. */
const PRODUCT = /^(prompts(-[a-z_]+)?|models)\.json$/;

const epic = TYPED[`epics-${manifest.epic_ref}.json`] as EpicDetail;
const timeline = TYPED[`epics-${manifest.epic_ref}-timeline.json`] as EpicTimeline;
const ticks = TYPED[`epics-${manifest.epic_ref}-ticks.json`] as EpicTick[];
const projection = TYPED["dashboard-work.json"] as WorkProjection;
const spend = TYPED["report-spend.json"] as SpendWindows;
const cents = (usd: number) => Math.round(usd * 100);
const sumCents = (values: number[]) => values.reduce((sum, v) => sum + cents(v), 0);

describe("the world's files", () => {
  it("are exactly the typed files above, the catalogs, the manifest and the timings", () => {
    const onDisk = jsonUnder(ROOT).sort();
    const product = onDisk.filter((file) => PRODUCT.test(file));
    expect(onDisk).toEqual(
      [...Object.keys(TYPED), ...product, ...NOT_FIXTURES].sort(),
    );
    expect(product).toContain("prompts.json");
    expect(product).toContain("models.json");
  });



  it("hold no instant after the world's own epoch", () => {
    const set = readFixtureSet(WORLD);
    expect(set.epoch).toBe(new Date(manifest.epoch).toISOString());
  });
});

describe("the epic in flight", () => {
  const nodes = epic.snapshot?.nodes ?? [];
  const byId = new Map(nodes.map((n) => [n.id, n]));

  it("has 10 to 14 nodes, some done, one running, one blocked and one waiting on a person", () => {
    expect(nodes.length).toBeGreaterThanOrEqual(10);
    expect(nodes.length).toBeLessThanOrEqual(14);
    expect(nodes.filter((n) => n.status === "done").length).toBeGreaterThan(0);
    expect(nodes.filter((n) => n.motion === "executing")).toHaveLength(1);
    expect(nodes.filter((n) => n.status === "blocked").map((n) => n.id)).toEqual([manifest.blocked_item]);
    expect(byId.get(manifest.blocked_item)?.blocked_reason).toBeTruthy();
    expect(byId.get(manifest.awaiting_merge_item)?.attention?.kind).toBe("merge_decision");
    expect(byId.get(manifest.failed_item)?.status).toBe("failed");
    expect(byId.get(manifest.failed_item)?.recovery_command).toBe(
      `kew epic retry ${manifest.epic_ref} ${manifest.failed_item}`,
    );
  });

  it("counts what its nodes say", () => {
    for (const [status, count] of Object.entries(epic.snapshot?.counts ?? {})) {
      expect(nodes.filter((n) => n.status === status).length, status).toBe(count);
    }
  });

  it("has a tick history the replay plays, ending on the snapshot the page draws", () => {
    expect(ticks.length).toBeGreaterThan(10);
    const ids = ticks.map((t) => t.id);
    expect([...ids].sort((a, b) => a - b)).toEqual(ids);
    expect(new Set(ids).size).toBe(ids.length);
    const last = ticks[ticks.length - 1];
    expect(last.snapshot.nodes.map((n) => [n.id, n.status])).toEqual(nodes.map((n) => [n.id, n.status]));
    expect(last.id).toBe(epic.heartbeat?.tick_count);
    // Statuses move over the history: the first tick has nothing done.
    expect(ticks[0].snapshot.counts.done).toBe(0);
  });
});

describe("spend", () => {
  it("reconciles exactly: runs to their node, nodes to the epic, the epic to the week", () => {
    const nodeCents = new Map<string, number>();
    for (const node of timeline.nodes) {
      const own = sumCents(node.runs.map((r) => r.cost_usd));
      if (own > 0) nodeCents.set(node.id, own);
      const detail = TYPED[`epics-${manifest.epic_ref}-nodes-${node.id}.json`] as EpicNodeDetail | undefined;
      if (detail) {
        expect(sumCents(detail.runs.map((r) => r.cost_usd)), node.id).toBe(cents(detail.lifetime_usd));
        expect(cents(detail.lifetime_usd), node.id).toBe(own);
      }
    }
    const drains = epic.economics?.node_drain_usd ?? {};
    expect(Object.fromEntries(Object.entries(drains).map(([id, usd]) => [id, cents(usd)]))).toEqual(
      Object.fromEntries(nodeCents),
    );
    const epicCents = cents(epic.economics?.drain_total_usd ?? NaN);
    expect(sumCents(Object.values(drains))).toBe(epicCents);
    expect(sumCents(runs.map((r) => r.cost_usd))).toBe(epicCents);
    expect(cents(spend.weekly.spent)).toBe(epicCents);
    expect(cents(manifest.spend.epic_usd)).toBe(epicCents);
    expect(cents(spend.daily.spent)).toBe(cents(manifest.spend.daily_usd));
    expect(spend.daily.spent).toBeLessThan(spend.weekly.spent);
    expect(spend.weekly.spent).toBeLessThan(spend.weekly.cap ?? NaN);
  });

  it("prices every node's lifetime as its drain, so the popover's lifetime line sums to the card's drain figure", () => {
    // kew's node popover sums a node's LIFETIME spend (`node-popover.ts`,
    // `lifetime_usd`), while its graph card and the epic's figure are the
    // DRAIN's. Every run in this world is one of the drain's, so the two are
    // the same figure here: that is what makes the run → node → epic sum the
    // money tour checks (`demo-tour-money.mjs`) hold in the demo.
    const prefix = `epics-${manifest.epic_ref}-nodes-`;
    const files = Object.keys(TYPED).filter((name) => name.startsWith(prefix));
    expect(files.length).toBe((epic.snapshot?.nodes ?? []).length);
    const drains = epic.economics?.node_drain_usd ?? {};
    for (const name of files) {
      const detail = TYPED[name] as EpicNodeDetail;
      expect(cents(detail.lifetime_usd), name).toBe(cents(detail.drain_usd));
      expect(cents(detail.drain_usd), name).toBe(cents(drains[detail.id] ?? 0));
    }
  });

  it("counts today and this week as kew's governor does: runs started since UTC midnight, and in that day and the six before it", () => {
    // `budget._spend_windows` and `service/report.spend_windows`: the day is
    // the calendar day of now in UTC, the week that day plus the prior six,
    // each over `list_since`, which reads a run's `started_at`.
    const epoch = new Date(manifest.epoch);
    const dayStart = Date.UTC(epoch.getUTCFullYear(), epoch.getUTCMonth(), epoch.getUTCDate());
    const weekStart = dayStart - 6 * 24 * 3_600_000;
    const startedSince = (since: number) =>
      sumCents(runs.filter((r) => r.started_at !== null && Date.parse(r.started_at) >= since).map((r) => r.cost_usd));
    expect(cents(spend.daily.spent)).toBe(startedSince(dayStart));
    expect(cents(spend.weekly.spent)).toBe(startedSince(weekStart));
    expect(cents(manifest.spend.daily_usd)).toBe(startedSince(dayStart));
    expect(cents(manifest.spend.weekly_usd)).toBe(startedSince(weekStart));
    // The day's window is not the week's: some of the week's runs started before today.
    expect(startedSince(dayStart)).toBeGreaterThan(0);
    expect(startedSince(dayStart)).toBeLessThan(startedSince(weekStart));
  });

  it("serves the same windows to the dashboard's glance and the budget page", () => {
    expect(dashboard.glance.caps).toEqual(spend);
    const budget = TYPED["config-budget.json"] as BudgetMap;
    expect(budget["budget.weekly_usd"].effective).toBe(spend.weekly.cap);
    expect(budget["budget.daily_usd"].effective).toBe(spend.daily.cap);
  });
});

describe("the world agrees with itself (design review c9)", () => {
  it("D7: an implementation run shows the verdict standing over its item, as kew's run detail does", () => {
    // kew's `_latest_verdict`: a run's own round first (`scope: "run"`), else
    // the newest standing over its work item (`scope: "item"`).
    const run = TYPED["runs-1b464391.json"] as Run;
    expect(run.run_kind).toBe("dispatch");
    expect(run.pr_state).toBe("merged");
    expect([run.verdict?.verdict, run.verdict?.scope]).toEqual(["READY", "item"]);
  });

  it("D7: a sealed run's flight attests the trusted base its sealed change set is built on, so all four checks match", () => {
    for (const name of Object.keys(TYPED).filter((n) => /^runs-[0-9a-f]{8}-nodes-.+-diff\.json$/.test(n))) {
      const diff = TYPED[name] as { run_id: string; base_sha: string; node_id: string };
      const flight = TYPED[`runs-${diff.run_id}-flight.json`] as {
        through_seq: number;
        artifact_node_id: string;
        events: { seq: number; facts: Record<string, unknown> }[];
      };
      const fact = flight.events.find((e) => e.facts.fact === "trusted_base_sha");
      expect(fact?.facts.value, name).toBe(diff.base_sha);
      expect(fact!.seq, name).toBeLessThanOrEqual(flight.through_seq);
      // kew names the seal's node after its ledger position, `<run>:<through_seq>`.
      expect(flight.artifact_node_id, name).toBe(`${diff.run_id}:${flight.through_seq}`);
      expect(diff.node_id, name).toBe(flight.artifact_node_id);
    }
  });

  it("a reviewed item's last reviewed head is its verified head, and neither is a sealed change set's base", () => {
    const bases = new Set(
      Object.keys(TYPED)
        .filter((n) => /^runs-[0-9a-f]{8}-nodes-.+-diff\.json$/.test(n))
        .map((n) => (TYPED[n] as { base_sha: string }).base_sha)
    );
    let compared = 0;
    for (const node of timeline.nodes) {
      const reviews = node.runs
        .filter((r) => r.kind === "adversarial_review")
        .map((r) => TYPED[`runs-${r.run_id}.json`] as Run)
        .filter((r) => r.verdict?.scope === "run");
      const pipeline = TYPED[`work-${node.id}-pipeline.json`] as { merge_verify: { head: string } | null } | undefined;
      const verified = pipeline?.merge_verify?.head;
      if (reviews.length === 0 || !verified) continue;
      const reviewed = reviews.at(-1)!.verdict!.head_sha ?? "";
      expect(reviewed, node.id).toBe(verified);
      expect(bases.has(reviewed), `${node.id}'s reviewed head is a base sha`).toBe(false);
      expect(bases.has(verified), `${node.id}'s verified head is a base sha`).toBe(false);
      compared += 1;
    }
    expect(compared).toBeGreaterThan(1);
  });

  it("every verdict names a reviewer kew can run: a model kew's catalog lists for claude-code, the one runner kew binds a verdict to", () => {
    // `STREAM_VERDICT_RUNNERS` in kew/src/kew/review_stream.py.
    const catalog = JSON.parse(readFileSync(new URL("../../../../kew/src/kew/models.json", import.meta.url), "utf8")) as {
      models: { id: string; runner: string; aliases?: string[] }[];
    };
    const claudeCode = new Set(catalog.models.filter((m) => m.runner === "claude-code").flatMap((m) => [m.id, ...(m.aliases ?? [])]));
    const reviewers = Object.keys(TYPED)
      .filter((n) => /^runs-[0-9a-f]{8}\.json$/.test(n))
      .map((n) => (TYPED[n] as Run).verdict?.reviewer_model)
      .filter((m): m is string => typeof m === "string");
    expect(reviewers.length).toBeGreaterThan(0);
    for (const model of reviewers) expect(claudeCode.has(model), model).toBe(true);
  });

  it("D15: the change strip holds one attempt per journal row, so the Epics list counts the changes the header does", () => {
    const changes = TYPED[`epics-${manifest.epic_ref}-tick-strip-changes.json`] as EpicTickStrip[];
    const journal = TYPED[`epics-${manifest.epic_ref}-ticks.json`] as { id: number }[];
    expect(changes.map((t) => t.journal_tick_id)).toEqual(journal.map((t) => t.id));
    expect(changes.length).toBe(epic.heartbeat?.change_count);
  });
});

describe("the needs-you set", () => {
  it("is an approval hold, two READY PRs' merge decisions (one held by its failed verify), a failed node to retry and an orphan PR", () => {
    expect(dashboard.rows.map((r) => [r.attention_kind, r.item_id])).toEqual([
      ["approval", manifest.hold_id],
      ["merge_decision", manifest.awaiting_merge_item],
      ["merge_decision", manifest.refused_item],
      ["host_restart_repeated", manifest.failed_item],
      ["", ""],
    ]);
    // #3453 — the orphan names no item, so Work's own Acknowledge is the one way to clear it.
    const orphan = dashboard.rows.at(-1);
    expect([orphan?.type, orphan?.pr_number, orphan?.epic_ref]).toEqual(["orphan_pr", manifest.orphan_pr, null]);
    expect(dashboard.rows).toHaveLength(manifest.needs_you);
    const approvals = TYPED["approvals.json"] as Hold[];
    expect(approvals.map((h) => h.item_id)).toEqual([manifest.hold_id]);
    const awaiting = timeline.nodes.find((n) => n.id === manifest.awaiting_merge_item);
    expect(awaiting?.rounds.at(-1)?.verdict).toBe("READY");
  });

  it("is what the Work list flags: one row per wait", () => {
    const rows = mergeWorkList({ projection, dashboard, runs });
    const flagged = rows.filter((r) => r.needsYou).map((r) => r.itemId).sort();
    expect(flagged).toEqual(
      [
        manifest.hold_id,
        manifest.awaiting_merge_item,
        manifest.refused_item,
        manifest.failed_item,
        `orphan_pr:${manifest.orphan_pr}`,
      ].sort(),
    );
    expect(needsYouSummary(rows, dashboard).entries).toHaveLength(manifest.needs_you);
  });
});

describe("the issues kew has not touched", () => {
  it("are open candidates Work's picker can queue, none of them an item the world already holds", () => {
    const candidates = dashboard.glance.queue_candidates.map((c) => c.item_id);
    expect(candidates).toEqual(manifest.queue_candidates);
    const listed = new Set(mergeWorkList({ projection, dashboard, runs }).map((r) => r.itemId));
    for (const id of manifest.queue_candidates) expect(listed.has(id)).toBe(false);
  });
});

describe("the refused PR", () => {
  const item = manifest.refused_item;
  const pipeline = TYPED[`work-${item}-pipeline.json`] as ItemPipeline;
  const node = timeline.nodes.find((n) => n.id === item);

  it("failed its merge verify on main's held-out tests", () => {
    expect(pipeline.merge_verify?.outcome).toBe("failed");
    expect(pipeline.merge_verify?.head.startsWith(manifest.refused_head)).toBe(true);
    expect(pipeline.merge_verify?.held_out?.failed).toEqual(manifest.held_out_failed);
    expect(pipeline.merge_verify?.held_out?.declared).toEqual(manifest.held_out_declared);
  });

  it("was sent back by review, then passed after a fix", () => {
    expect(node?.rounds.map((r) => r.verdict)).toEqual(["NEEDS_CHANGES", "READY"]);
    const kinds = node?.runs.map((r) => r.kind);
    expect(kinds).toEqual(["dispatch", "adversarial_review", "review_remediation", "adversarial_review"]);
  });

  it("waits on a person's merge decision: the failed verify held the merge and the READY stands", () => {
    const row = mergeWorkList({ projection, dashboard, runs }).find((r) => r.itemId === item);
    expect(row?.needsYou).toBe(true);
    expect(row?.view?.judgment.state).toBe("awaiting_human_decision");
    const merge = pipeline.lanes.flatMap((l) => l.stages).find((s) => s.stage === "merge");
    expect([merge?.state, merge?.label, merge?.detail]).toEqual(["needs_you", "needs you", "merge decision"]);
  });

  it("is one chain of heads: round 2 read the head the remediation pushed, the head the verify failed on", () => {
    const reviews = (node?.runs ?? [])
      .filter((r) => r.kind === "adversarial_review")
      .map((r) => TYPED[`runs-${r.run_id}.json`] as Run);
    expect(reviews.map((r) => [r.verdict?.round, r.verdict?.scope])).toEqual([
      [1, "run"],
      [2, "run"],
    ]);
    const [first, second] = reviews.map((r) => r.verdict?.head_sha ?? "");
    expect(second).toBe(pipeline.merge_verify?.head);
    expect(second.startsWith(manifest.refused_head)).toBe(true);
    expect(first.startsWith(manifest.refused_head)).toBe(false);
    expect(pipeline.merge_verify?.is_current_head).toBe(true);
    const review = pipeline.lanes.flatMap((l) => l.stages).find((s) => s.stage === "review");
    expect(review?.evidence?.ref).toBe(manifest.refused_head);
  });

  it("has its run's detail, flight, ledger page and sealed diff", () => {
    for (const read of ["", "-flight", "-audit"]) {
      expect(TYPED[`runs-${manifest.run_id}${read}.json`], read).toBeDefined();
    }
    const flight = TYPED[`runs-${manifest.run_id}-flight.json`] as FlightTimeline;
    const node = (flight.artifact_node_id ?? "").replace(":", "_");
    expect(TYPED[`runs-${manifest.run_id}-nodes-${node}-diff.json`]).toBeDefined();
  });
});

describe("the demo build", () => {
  it("bundles the demo world, never the factory set", () => {
    expect(fileURLToPath(DEMO_FIXTURE_SET)).toBe(ROOT);
    const set = readFixtureSet(DEMO_FIXTURE_SET);
    expect(set.source).toBe("frontend/demo/world/");
    expect(set.epoch).toBe(new Date(manifest.epoch).toISOString());
    expect(Object.keys(set.files)).toContain("dashboard.json");
    for (const file of NOT_FIXTURES) expect(Object.keys(set.files)).not.toContain(file);
  });

});
