// Typed client over the kew serve API. Types mirror the Pydantic DTOs in
// kew/src/kew/service/models.py (export_openapi.py keeps them honest in CI).

export type Run = {
  run_id: string;
  issue_num: number;
  title: string;
  runner: string;
  model: string;
  status: string;
  /**
   * What this is DOING, in the console's one motion vocabulary
   * ($lib/factory/motion): "executing", "waiting_kew", "waiting_human" or
   * "settled". Optional because a replayed tick from a server older than
   * #2305 carries none — `motionOf(status, 'run')` is the fallback, and it is
   * the same table the server derives this from. Say `'run'`: a run's `queued`
   * is the dispatcher's own next turn, where a NODE's is a person's.
   */
  motion?: string;
  /** `motion !== "settled"`: an open concern rather than history. */
  alive?: boolean;
  cost_usd: number;
  channel: string; // "local" | "hosted"
  started_at: string | null;
  finished_at: string | null;
  // Flight-recorder columns (F3a) — additive, optional/defaulted backend-side.
  duration_seconds: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  diff_additions: number | null;
  diff_deletions: number | null;
  diff_files: number | null;
  review_cycles: number | null;
  pr_url: string | null;
  pr_state: string | null;
  attempt_number: number;
  error_category: string;
  complexity: string;
  branch: string;
  // The stage this run's recorded kind executes, and the dot-separated detail
  // line under it ("round 1 · PR #2059"); both FUI-13. "unknown" is a real
  // answer — a kind kew's vocabulary has no word for — so render it as
  // one, never as a stage-shaped guess. `stage_detail` is "" when nothing
  // recorded qualifies the stage.
  stage: string; // RUN_STAGES: implement | publish | review | ci_fix | unknown
  stage_detail: string;
  // Sandbox posture, as the host resolved it for this attempt. "not recorded"
  // is the honest reading of a run that predates the posture event, or of one
  // whose event could not be read — it is not "unsandboxed".
  posture: string;
  filesystem_boundary: string;
  network_boundary: string;
  egress_verification_basis: string | null;
  credential_names: string[];
  command_guard: string;
  admission: string;
  // Why the resolved posture is what it is, when the host recorded a reason.
  reason: string | null;
  // C3 (#2300) — the recorded columns the run page reads. `model` is what kew
  // ROUTED to and `reported_model` what the runner said it used, so a
  // substitution is visible as the difference; `planned_cost_usd` is the
  // governor's estimate against `cost_usd`'s actual. "" / null means the row
  // recorded nothing, which the page prints as unavailable rather than blank.
  run_kind: string;
  item_id: string | null;
  reported_model: string | null;
  provider: string | null;
  transport_mode: string;
  planned_cost_usd: number | null;
  accounting_status: string | null;
  // The governed-review verdict standing over this run's work. Served on the
  // DETAIL read only — a list page leaves it null rather than spending an
  // audit read per row.
  verdict: RunVerdict | null;
};

/**
 * One attested governed-review verdict, as the run detail serves it.
 *
 * `scope` is whose decision it is: "run" when THIS run attested it (a governed
 * review emits under its own run id), "item" when no verdict names this run
 * and the reading is the latest standing over the work item — possibly
 * recorded for a later attempt, which the page must say rather than print as
 * this run's own reviewed head.
 */
export type RunVerdict = {
  round: number;
  verdict: string;
  head_sha: string | null;
  reviewer_model: string | null;
  ts: string | null;
  scope: string;
};

export type Meta = {
  billing: string; // "metered" | "subscription"
  plan_label: string;
  plan_monthly_usd: number;
  version: string;
  /** `[serve] palette_ask` — whether the palette may offer ask mode (#2518). */
  palette_ask?: boolean;
};

export type Bundle = {
  present: boolean;
  git_sha: string;
  built_at: number; // unix seconds; 0 when the bundle predates stamping
  stale: boolean; // frontend/ source is newer than the deployed bundle
};

export type Step = {
  id: string;
  parent_id: string | null;
  seq: number;
  ts: number;
  kind: string;
  label: string;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  has_diff: boolean;
};

export type Timeline = {
  run_id: string;
  root_id: string | null;
  head_id: string | null;
  nodes: Step[];
};

// --- Canonical Flight contract (I16/I17) ------------------------------------
// Types mirror EvidenceAvailabilityDTO / FlightEventDTO / EvidenceGapDTO /
// FlightTimelineDTO / ArtifactFileDTO / DiffDTO / ProtectedPromptDTO in
// kew/src/kew/service/models.py field-for-field. This is the *only* contract
// the run-detail view consumes: it reads a replayed ledger and a sealed
// package, never a mutable repository.

export type EvidenceAvailability = {
  dimension: string; // "host_sequence" | "artifact_seal" | "artifact_diff" | …
  availability: string; // "available" | "unavailable"
  // "unobserved" = nothing was ever recorded; "error" = something was and
  // cannot be trusted. The two are different facts and stay distinguishable.
  provenance: string;
  detail: string;
};

export type FlightEvent = {
  seq: number; // position in the host sequence (audit_events.id)
  timestamp: string;
  kind: string; // "evidence.artifact" | "evidence.receipt_fact" | …
  origin: string; // "evidence" | "attestation"
  producer_kind: string; // "host" | "runner"
  producer_name: string;
  producer_version: string;
  attested_by_host: boolean;
  // Covered by the receipt's sealed prefix; false marks a later chained fact.
  sealed: boolean;
  terminal: boolean;
  facts: Record<string, unknown>;
};

export type EvidenceGap = { seq: number; event: string; reason: string };

export type FlightTimeline = {
  run_id: string;
  attempt_number: number;
  // Durable work-item id and, for a numeric tracker only, its number. A Linear
  // item carries `item_id` alone; nothing synthesizes a number.
  item_id: string | null;
  issue_num: number | null;
  source_tracker: string | null;
  source_id: string | null;
  sealed: boolean;
  through_seq: number | null;
  chain_head: string | null;
  chain_verified: boolean;
  artifact_node_id: string | null;
  runner_telemetry: string; // "structured" | "activity_only" | "missing"
  events: FlightEvent[];
  gaps: EvidenceGap[];
  availability: EvidenceAvailability[];
};

export type ArtifactFile = {
  path: string;
  change: string; // "changed" | "deleted" — never "added": there is no base
  mode: string;
  digest: string;
  byte_length: number;
  text: string | null;
  text_availability: string;
  detail: string;
};

// `node_id`/`text` keep the shape the old snapshot diff had; everything else
// states the lineage the change set is bound to. Those extra fields are
// optional so a payload from a store that sealed nothing still types.
export type Diff = {
  node_id: string;
  text: string;
  run_id?: string;
  attempt_number?: number;
  base_sha?: string;
  tree_sha?: string;
  package_digest?: string;
  receipt_digest?: string;
  verification?: string;
  files?: ArtifactFile[];
  availability?: EvidenceAvailability[];
};

// The one response that may carry exact prompt bytes. Every field except
// `text` is the digest-only description a list or a receipt may carry; `text`
// is filled in only on the dedicated protected read, and only while the bytes
// are still retained. `availability: "unavailable"` with a purge reason is a
// different fact from an empty prompt.
export type ProtectedPrompt = {
  run_id: string;
  attempt_number: number;
  artifact_kind: string;
  digest: string;
  byte_length: number;
  storage: string; // "local_protected" | "unavailable"
  availability: string; // "available" | "unavailable"
  detail: string;
  created_at: string;
  terminal_at: string | null;
  expires_at: string | null;
  purged_at: string | null;
  purge_reason: string;
  text: string | null;
};

export type Iteration = {
  iteration_id: string;
  idx: number;
  kind: string;
  status: string;
  cost_usd: number;
  tokens: number;
  run_id: string | null;
  started_at: string | null;
  finished_at: string | null;
};

export type Loop = {
  loop_id: string;
  source: string;
  issue_num: number | null;
  repo: string | null;
  goal_text: string;
  runner_name: string;
  model: string; // durable model half of the route; "" for pre-v35 loops
  drive_mode: string; // "delegated" | "driven" | "unknown"
  status: string;
  /** See `EpicNode.motion`. A `pursuing` loop nothing has advanced for longer
   * than one iteration can take is served as `waiting_kew`, not executing. */
  motion?: string;
  alive?: boolean;
  status_reason: string;
  needs_human: boolean;
  // Derived on read, never stored: a `pursuing` loop that has not iterated in
  // 2x its cadence. Optional so a payload predating it still parses.
  stale?: boolean;
  budget_usd: number | null;
  spent_usd: number;
  wall_clock_secs: number;
  max_steps: number;
  step_count: number;
  created_at: string | null;
  updated_at: string | null;
  finished_at: string | null;
};

export type LoopDetail = Loop & { iterations: Iteration[] };

export type LoopAuditEvent = {
  id: number;
  timestamp: string;
  event: string;
  actor_type: string | null;
  actor_id: string | null;
  loop_id: string | null;
  summary: string;
};

// ── Factory health (#2298) ──────────────────────────────────────────────────
//
// `/api/health` answers one question: is the factory able to work? Everything
// in it is three-state, never two — `null` is "could not be observed", which is
// not the same fact as `false`, and a thing that is off by design is not a
// fault. The types say so: every observable is `boolean | null`.

export type HealthRuntime = {
  name: string;
  /** The runtime `[sandbox] runtime` selects. */
  configured: boolean;
  /** Whether THIS build has a backend that can drive it (openshell: not yet). */
  backed: boolean;
  /** Usable on the host; `null` when the question cannot be put at all. */
  present: boolean | null;
};

export type HealthImage = {
  runner: string;
  /** The image ref, or `null` when this runner has none mapped. */
  ref: string | null;
  /** `images.<runner>` | `image` | `unmapped` — where the ref came from. */
  source: string;
  /** True when some lane routes to this runner. The header counts only these. */
  routed: boolean;
  present: boolean | null;
};

export type HealthPosture = {
  /** `default` | `simple` | `medium` | `complex` | `review`. */
  lane: string;
  runner: string;
  runtime: string;
  contained: boolean;
  egress: string;
  /** `real` when a declared credential is set here; `placeholder` when none is. */
  credential_mode: string;
  dangerously_skip_containment: boolean;
  posture: string;
  /** Why this lane cannot run, when dispatch would refuse it outright. */
  refusal: string | null;
  /**
   * Why the console process ITSELF could not launch this lane (`credential_missing`):
   * a fact about the console's own environment, not the loops' or daemon's that
   * dispatch (#2534). Never counted as down. Optional for an older `kew serve`.
   */
  console_refusal?: string | null;
};

export type HealthCredential = { name: string; set: boolean };

export type HealthRunner = {
  name: string;
  binary: string | null;
  binary_present: boolean | null;
  version: string | null;
  version_supported: boolean | null;
  credential_names: HealthCredential[];
  /** Successful runs on this runner in the observed window (#2566). Optional for an older `kew serve`. */
  succeeded_in_window?: number;
  /**
   * Set when the declared credential is missing from `kew serve`'s own
   * environment only — a run on this runner succeeded in the window, so the
   * loops that dispatch hold one (#2566). A quiet note, never counted.
   */
  credential_note?: string | null;
  routed_tiers: string[];
  review_lane: boolean;
};

export type HealthTrackers = {
  github: {
    reachable: boolean | null;
    auth_ok: boolean | null;
    scopes: string[];
    rate: { limit: number; remaining: number; reset_at: string } | null;
    observed_at: string | null;
    observed_age_s: number | null;
    reason: string;
  };
  linear: {
    configured: boolean;
    credential_set: boolean;
    reachable: boolean | null;
    team: string;
  };
};

export type HealthDriver = {
  epic_ref: string;
  /**
   * False for an archived or complete epic, which has no driver to lose (#2534).
   * Optional for an older `kew serve`, which is read as active.
   */
  active?: boolean;
  /**
   * `live` | `busy` | `waiting` | `gone` | `unknown` — the service's own reading,
   * not a re-threshold. `waiting` is a loop that said, inside twice its cadence,
   * that it is holding off on purpose (#2566).
   */
  liveness: string;
  /** What a `waiting` driver's loop said it waits on, e.g. `slot`. */
  waiting_reason?: string | null;
  waiting_at?: string | null;
  tick_age_s: number | null;
  interval_s: number | null;
  last_tick_at: string | null;
  lock: {
    pid_alive: boolean | null;
    heartbeat_age_s: number | null;
    observed_age_s: number | null;
  };
};

export type HealthWatcher = {
  name: string;
  enabled: boolean;
  running: boolean | null;
  detail: string;
  last_seen_at?: string | null;
  last_seen_age_s?: number | null;
  pollers?: number | null;
};

export type HealthSweep = {
  present: boolean;
  heartbeat_at: string | null;
  heartbeat_age_s: number | null;
  interval_s?: number;
  last_attempt_at?: string | null;
  last_ok_at?: string | null;
  last_sweep_at?: string | null;
  last_result?: string | null;
  query_failures?: number;
  /** GraphQL requests the last cycle's outcome sync made (#2750). */
  graphql_calls?: number;
  last_ok_age_s?: number | null;
  last_attempt_age_s?: number | null;
};

export type Health = {
  checked_at: string;
  observed_window_days: number;
  runtimes: HealthRuntime[];
  images: HealthImage[];
  posture: HealthPosture[];
  /** Runs per recorded `container_runtime`; the empty key is "none recorded". */
  observed_7d: Record<string, number>;
  runners: HealthRunner[];
  trackers: HealthTrackers;
  sweep: HealthSweep;
  drivers: HealthDriver[];
  watchers: HealthWatcher[];
  loops: { pursuing: number; stale: number };
  fleet: { stopped: boolean; pending: number; reason: string };
};

/** One environment variable, by NAME. No surface ever carries its value. */
export type HealthEnvVar = {
  name: string;
  purpose: string;
  required_by: string;
  set: boolean;
};

export type HealthEnv = { as_seen_by: string; vars: HealthEnvVar[] };

/**
 * One resolved field of a dispatch plan, and the rule that decided it (#2302).
 *
 * `source` is the whole point: "codex" means nothing on its own, and
 * "codex ← kew.toml [routing].simple" is an answer an operator can act on.
 */
export type PlanField<T> = { value: T; source: string };

/** The complexity the plan resolved, and whether anyone actually knows it. */
export type PlanComplexity = {
  value: string;
  source: string;
  /** False when no caller supplied one: the tier lives on the tracker label. */
  known: boolean;
};

export type PlanContainment = {
  posture: string;
  /** Why this lane cannot run here at all. A reading, not an error. */
  refusal_reason: string | null;
  runtime: string;
  image: string | null;
  image_present: boolean | null;
  /** Names and a bit. No surface ever carries a credential's value. */
  credential_names_present: HealthCredential[];
  source: string;
};

export type PlanBudget = {
  runtime_cap_usd: number;
  runtime_cap_source: string;
  per_issue_cap_usd: number;
  per_issue_cap_source: string;
  issue_spent_usd: number | null;
  daily_cap_usd: number;
  daily_cap_source: string;
  daily_spent_usd: number | null;
  per_issue_remaining_usd: number | null;
  daily_remaining_usd: number | null;
  runtime_cap_exceeds_issue_headroom: boolean;
  source: string;
};

export type PlanGovernor = {
  would_swap: boolean;
  reason: string;
  recommended_runner: string;
  recommended_model: string;
  est_savings_usd: number;
  source: string;
};

export type PlanAdmission = {
  fleet_stopped: boolean;
  fleet_stopped_reason: string;
  /** `null` when there is no item to ask about, or the log could not be read. */
  open_hold: boolean | null;
  headroom: { usd: number | null; binds: string | null; source: string };
  source: string;
};

export type DispatchPlan = {
  checked_at: string;
  item: string | null;
  issue_num: number | null;
  lane: PlanField<string>;
  complexity: PlanComplexity;
  runner: PlanField<string>;
  model: PlanField<string>;
  transport: PlanField<string>;
  containment: PlanContainment;
  budget: PlanBudget;
  governor: PlanGovernor;
  route_fingerprint: PlanField<string | null>;
  /** `confirmed` | `refuted` | `unverified`, from stored probe evidence only. */
  live_status: { value: string; source: string; probed_at: string | null };
  admission: PlanAdmission;
  /**
   * The exact terminal command that dispatches this item, composed by the
   * server (#2517) so no surface assembles one; `null` when no hand command
   * dispatches it (a review lane, a tracker key). Optional: a serve that
   * predates the field omits it, and that reads as "no command".
   */
  command?: string | null;
};

/** One config field's effective value and the layer that won it. */
export type EffectiveField = {
  value: unknown;
  /** `env (NAME)` | `DB override` | `kew.toml` | `default` | a policy string. */
  source: string;
  generation?: number;
  expires_at?: string | null;
  over_cap?: boolean;
  configured_value?: unknown;
};

export type EffectiveConfig = {
  config_path: string | null;
  fields: Record<string, EffectiveField>;
  /**
   * `""` when the human-review policy was read. Non-empty names why it could
   * not be, so the `human_review.*` rows showing config-layer values are
   * explainable rather than looking like an empty policy. Optional: a serve
   * that predates the field omits it.
   */
  human_review_unavailable?: string;
};

/**
 * One ledger event of one run, as `/api/runs/{id}/audit` serves it.
 *
 * `seq` is the ledger id and also the forward cursor. The payload is the full
 * recorded one, not an excerpt: this feed is already scoped to a single run, so
 * a truncated payload is the one thing its event list cannot work from.
 */
export type RunAuditEvent = {
  seq: number;
  ts: string;
  event: string;
  actor_type: string | null;
  item_id: string | null;
  payload: Record<string, unknown> | null;
};

/**
 * One forward page of a run's events, oldest first.
 *
 * `next_seq` is where to RESUME, not a has-more flag: an empty page hands back
 * the cursor it was asked with, so a live reader polls the same cursor forever
 * and a client paging to the end stops when `events` comes back empty.
 */
export type RunAuditPage = {
  run_id: string;
  events: RunAuditEvent[];
  next_seq: number;
};

async function get<T>(
  path: string,
  fetcher: typeof fetch = fetch,
  signal?: AbortSignal,
): Promise<T> {
  const r = await (signal ? fetcher(path, { signal }) : fetcher(path));
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return (await r.json()) as T;
}

export const fetchMeta = (f: typeof fetch = fetch) => get<Meta>("/api/meta", f);
export const fetchBundle = (f: typeof fetch = fetch) =>
  get<Bundle>("/api/bundle", f);
export const fetchRuns = (f: typeof fetch = fetch) =>
  get<Run[]>("/api/runs", f);
export const fetchRun = (id: string, f: typeof fetch = fetch) =>
  get<Run>(`/api/runs/${encodeURIComponent(id)}`, f);
export const fetchFlight = (
  id: string,
  attempt?: number,
  f: typeof fetch = fetch,
) =>
  get<FlightTimeline>(
    `/api/runs/${encodeURIComponent(id)}/flight${attempt == null ? "" : `?attempt=${attempt}`}`,
    f,
  );
/** The run page's cap, mirroring the server's `MAX_RUN_AUDIT`. */
export const RUN_AUDIT_PAGE = 200;

/**
 * One forward page of a run's ledger events. `afterSeq` is a strict
 * lower-exclusive cursor — pass the previous page's `next_seq` to continue,
 * which is also how a live run resumes after a dropped read.
 */
export const fetchRunAudit = (
  id: string,
  afterSeq = 0,
  limit = RUN_AUDIT_PAGE,
  f: typeof fetch = fetch,
) =>
  get<RunAuditPage>(
    `/api/runs/${encodeURIComponent(id)}/audit?after_seq=${afterSeq}&limit=${limit}`,
    f,
  );
export const fetchTimeline = (id: string, f: typeof fetch = fetch) =>
  get<Timeline>(`/api/runs/${encodeURIComponent(id)}/timeline`, f);
export const fetchDiff = (id: string, node: string, f: typeof fetch = fetch) =>
  get<Diff>(
    `/api/runs/${encodeURIComponent(id)}/nodes/${encodeURIComponent(node)}/diff`,
    f,
  );

/**
 * The exact compiled prompt — the ONE call in this client that can return the
 * bytes. It exists to be called from a click handler and nowhere else: no
 * loader, no preload, no `$effect`, no store hydration and no analytics path
 * may reach it (`src/routes/runs/[id]/page.load.test.ts` enforces that).
 *
 * `cache: 'no-store'` mirrors the server's `private, no-store` on the response
 * so bytes an operator asked for exactly once cannot be re-served from a disk
 * cache or a back-button replay, and `credentials: 'same-origin'` sends the
 * session the route requires even on loopback.
 */
export async function fetchPrompt(
  id: string,
  attempt?: number,
  f: typeof fetch = fetch,
): Promise<ProtectedPrompt> {
  const query = attempt == null ? "" : `?attempt=${attempt}`;
  const path = `/api/runs/${encodeURIComponent(id)}/prompt${query}`;
  const r = await f(path, { cache: "no-store", credentials: "same-origin" });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return (await r.json()) as ProtectedPrompt;
}

export const fetchHealth = (f: typeof fetch = fetch) =>
  get<Health>("/api/health", f);
export const fetchHealthEnv = (f: typeof fetch = fetch) =>
  get<HealthEnv>("/api/health/env", f);

/**
 * Where a role's base prompt comes from (`SourceKind` in
 * `kew/service/prompts.py`): a template packaged with kew, the `[prompt]
 * template` kew.toml configures in its place, the pinned reviewer artifact,
 * the project's own deployed copy of it, or a builder function.
 */
export type PromptSourceKind =
  | "packaged-template"
  | "config-template"
  | "pinned-artifact"
  | "deployed-artifact"
  | "code";

/** `PromptSourceDTO`. `version` and `sha256` are set for the reviewer only. */
export type PromptSource = {
  kind: PromptSourceKind;
  name: string;
  version: number | null;
  sha256: string | null;
  pin_mismatch: boolean;
};

/** One agent role, as `GET /api/prompts` lists it (`PromptRoleDTO`, #3018). */
export type PromptRole = {
  role: string;
  run_kinds: string[];
  source: PromptSource;
  prompt_config_applies: boolean;
  profile_applies: boolean;
};

/**
 * `GET /api/prompts/{role}` (`PromptPreviewDTO`): the role's BASE prompt
 * rendered over a fixed sample item, never a real item's content. 503 when
 * the server was started without a renderer for the role.
 */
export type PromptPreview = PromptRole & {
  preview: string;
  preview_sha256: string;
  variant: Record<string, string | boolean>;
  /**
   * Where the operator addendum block sits in `preview` (`AddendumSpanDTO`,
   * #3025): code-point offsets from its heading through its body's last
   * character, as the builder placed it. Null when no addendum is rendered.
   */
  addendum?: AddendumSpan | null;
  /**
   * The role's profile generation this preview's profile was read at — the
   * `generation` its versions read serves — so the page never pairs a preview
   * and a history from two decisions. Null for a role no profile applies to,
   * or when the role changed inside every read.
   */
  profile_generation?: number | null;
  /**
   * Whether `preview` was rendered with the served `[prompt]` configuration
   * (#3071). False for the built-in base: a role `[prompt]` never applies to,
   * a refused profile, or a `variant.refusal`. `prompt_config_applies` says
   * only that the role reads `[prompt]`; this says whether this text did.
   */
  prompt_config_rendered: boolean;
};

export type AddendumSpan = { start: number; end: number };

export const fetchPrompts = (f: typeof fetch = fetch) =>
  get<PromptRole[]>("/api/prompts", f);
export const fetchPromptRole = (role: string, f: typeof fetch = fetch) =>
  get<PromptPreview>(`/api/prompts/${encodeURIComponent(role)}`, f);

/** One addendum of a role (`ProfileVersionDTO`, #3025). */
export type ProfileVersion = {
  version: number;
  state: "proposed" | "active" | "declined" | "retired";
  body: string;
  body_sha256: string;
  proposed_by: string;
  proposed_at: string;
  decided_by: string | null;
  decided_at: string | null;
  audit_event_id: number | null;
};

/**
 * `GET /api/prompts/{role}/versions` (`ProfileVersionsDTO`): the role's
 * addenda, newest first. `generation` is what an approve or a retire sends
 * back — the role as this read showed it, never re-read before the write.
 */
export type ProfileVersions = {
  role: string;
  generation: number;
  /** The acting human passes the approver gate, so every profile write is theirs (#3092). */
  can_decide: boolean;
  /** The gate's own sentence while no approver is configured, naming the fix (#3119); null otherwise. */
  decide_refusal: string | null;
  max_body_chars: number;
  active_version: number | null;
  versions: ProfileVersion[];
};

export const fetchProfileVersions = (role: string, f: typeof fetch = fetch) =>
  get<ProfileVersions>(
    `/api/prompts/${encodeURIComponent(role)}/versions`,
    f,
  );

/**
 * One version of an implementer role's prompt and how its items went
 * (`ImplementerVersionOutcome`, #3026). `version` is null for the built-in
 * prompt. Each rate is null when there was nothing to measure.
 */
export type ImplementerOutcome = {
  version: number | null;
  label: string;
  state: ProfileVersion["state"] | null;
  n: number;
  comparable: boolean;
  round_one_items: number;
  first_pass_rate: number | null;
  ready_items: number;
  mean_rounds_to_ready: number | null;
  merged_items: number;
  median_cost_usd: number | null;
};

/** One version of the reviewer's prompt and its verdict mix (`ReviewerVersionOutcome`). */
export type ReviewerOutcome = {
  version: number | null;
  label: string;
  state: ProfileVersion["state"] | null;
  n: number;
  comparable: boolean;
  verdicts: Record<string, number>;
};

/**
 * `GET /api/prompts/{role}/outcomes` (`PromptOutcomesDTO`, #3026): one row
 * per active or retired version, at zero when nothing ran under it, plus any
 * other version something was recorded under; newest first, the built-in
 * prompt last. A row under `min_items` is not `comparable`. `generation` is
 * the role's profile generation the rows were read at (#3089) — the
 * `generation` its versions read serves — and null when a decision landed
 * inside every read the server tried, so the rows match no history.
 */
export type PromptOutcomes =
  | { role: string; kind: "implementer"; min_items: number; versions: ImplementerOutcome[]; generation: number | null }
  | { role: string; kind: "reviewer"; min_items: number; versions: ReviewerOutcome[]; generation: number | null };

export const fetchPromptOutcomes = (role: string, f: typeof fetch = fetch) =>
  get<PromptOutcomes>(
    `/api/prompts/${encodeURIComponent(role)}/outcomes`,
    f,
  );

/**
 * A profile write's answer, never thrown: the editor reads a 403 or a 409 as
 * a sentence in place. `status: 0` is no HTTP response at all — the write may
 * have committed before its answer was lost — and a success status with
 * `ok: false` is an answer whose body could not be read; the editor treats
 * both as an unknown outcome (`writeOutcomeUnknown`), never as no change.
 */
export type ProfileWriteResponse =
  | { ok: true; data: ProfileVersions }
  | { ok: false; status: number; detail: string };

async function profileWrite(
  path: string,
  body: unknown,
  f: typeof fetch,
): Promise<ProfileWriteResponse> {
  let r: Response;
  try {
    r = await f(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Kew-CSRF": csrfToken(),
      },
      body: JSON.stringify(body),
    });
  } catch (e) {
    return { ok: false, status: 0, detail: (e as Error).message };
  }
  if (!r.ok)
    return { ok: false, status: r.status, detail: await actionDetail(r) };
  try {
    return { ok: true, data: (await r.json()) as ProfileVersions };
  } catch {
    return {
      ok: false,
      status: r.status,
      detail: "The server answered, but not with a readable result.",
    };
  }
}

const profilePath = (role: string) =>
  `/api/prompts/${encodeURIComponent(role)}`;

export const proposeProfile = (
  role: string,
  body: string,
  f: typeof fetch = fetch,
) => profileWrite(`${profilePath(role)}/versions`, { body }, f);

/** `generation` is the one the approver was shown. */
export const approveProfile = (
  role: string,
  version: number,
  generation: number,
  f: typeof fetch = fetch,
) =>
  profileWrite(
    `${profilePath(role)}/versions/${version}/approve`,
    { generation },
    f,
  );

export const declineProfile = (
  role: string,
  version: number,
  f: typeof fetch = fetch,
) => profileWrite(`${profilePath(role)}/versions/${version}/decline`, {}, f);

/** `generation` is the one the person retiring was shown. */
export const retireProfile = (
  role: string,
  generation: number,
  f: typeof fetch = fetch,
) => profileWrite(`${profilePath(role)}/retire`, { generation }, f);

/** One `kew doctor` check verdict (`CheckStatus` in `kew/readiness.py`). */
export type ReadinessStatus = "ok" | "warn" | "fail" | "skip";

/**
 * One `kew doctor` check, as `CheckResult.to_dict` serves it. `id` is one of
 * `CHECK_IDS` (stable, pinned by the doctor tests); `fix` is one runnable
 * command or null — prose belongs in `hint`.
 */
export type ReadinessCheck = {
  id: string;
  status: ReadinessStatus;
  summary: string;
  hint: string | null;
  fix: string | null;
  details: string[];
  warnings: string[];
};

/** `GET /api/readiness` (#2514): the `kew doctor --json` document. */
export type Readiness = {
  schema: number;
  checks: ReadinessCheck[];
  ok: boolean;
  checked_at: string;
};

/**
 * The doctor checks, at most a minute old (the server caches them). Read-only:
 * it reports each check's fix and never runs one.
 */
export const fetchReadiness = (f: typeof fetch = fetch) =>
  get<Readiness>("/api/readiness", f);
/**
 * The resolved route for one planned dispatch (#2302).
 *
 * A read, every time: the endpoint writes nothing — no audit row, no
 * reservation — so the launchpad may call it as often as a choice changes. It
 * never dispatches; the command block is how a dispatch actually happens.
 */
export const fetchDispatchPlan = (
  params: { item?: string | null; complexity?: string | null; lane?: string },
  f: typeof fetch = fetch,
) => {
  const query = new URLSearchParams();
  if (params.item) query.set("item", params.item);
  // An unknown complexity is omitted, not sent as "unknown": the endpoint
  // answers `known: false` for an absent one and 422s for a word it does not
  // know, and "the operator has not chosen yet" is the absent case.
  if (params.complexity) query.set("complexity", params.complexity);
  if (params.lane) query.set("lane", params.lane);
  const search = query.toString();
  return get<DispatchPlan>(
    `/api/dispatch/resolve${search ? `?${search}` : ""}`,
    f,
  );
};

export const fetchEffectiveConfig = (f: typeof fetch = fetch) =>
  get<EffectiveConfig>("/api/config/effective", f);
export const fetchLoops = (status?: string, f: typeof fetch = fetch) =>
  get<Loop[]>(
    `/api/loops${status ? `?status=${encodeURIComponent(status)}` : ""}`,
    f,
  );
export const fetchLoop = (id: string, f: typeof fetch = fetch) =>
  get<LoopDetail>(`/api/loops/${encodeURIComponent(id)}`, f);
export const fetchLoopAudit = (id: string, f: typeof fetch = fetch) =>
  get<LoopAuditEvent[]>(`/api/loops/${encodeURIComponent(id)}/audit`, f);

/**
 * Parse one SSE frame, or nothing (C16). `JSON.parse` in an `onmessage` throws
 * INSIDE the EventSource's event dispatch, where no caller has a `try` — a
 * single malformed frame (a keep-alive comment the server words differently, a
 * proxy's error page on the stream) therefore became an uncaught exception per
 * frame for as long as the stream stayed open. A frame we cannot read is a
 * frame we do not have.
 */
function parseFrame<T>(data: string): T | undefined {
  try {
    return JSON.parse(data) as T;
  } catch {
    return undefined;
  }
}

export function streamEvents(
  id: string,
  onEvent: (e: unknown) => void,
): EventSource {
  const es = new EventSource(`/api/runs/${encodeURIComponent(id)}/events`);
  es.onmessage = (m) => {
    const frame = parseFrame<unknown>(m.data);
    if (frame !== undefined) onEvent(frame);
  };
  return es;
}

// Epic DAG live view (#454). Types mirror the Epic*DTO Pydantic models in
// kew/src/kew/service/models.py field-for-field.

/**
 * Why one node needs a person, composed server-side from the same work
 * projection the judgment queue reads, so the DAG and the work surface cannot
 * disagree. `kind` is an open vocabulary — "merge_decision", "approval" and
 * "work_exception" today; render an unknown kind by its `reason`, never drop
 * it. `url` is the artifact to act on when there is one.
 */
export type EpicNodeAttention = {
  kind: string;
  reason: string;
  url: string | null;
  /**
   * What the operator does next, in the words the driver's own attention stream
   * uses (`kew.waits`). `''` when the kind carries no single next move.
   *
   * Optional on the wire: a serve that predates #2207 omits it, which reads as
   * "no action named" rather than as an empty instruction.
   */
  action?: string;
  /**
   * That move as a line an operator can paste, with this node's epic and item
   * already in it — `''` when no kew command clears the wait. Filled and
   * guarded server-side (`kew.waits.fill_command`), so a surface prints it
   * verbatim and NEVER scrapes one out of `action`: `action` is prose about
   * the move ("kew epic unhold, which also restarts the review cycle"), and
   * its leading words are `kew epic unhold` with no arguments, which exits 2.
   *
   * Optional on the wire for `action`'s reason: a serve that predates it omits
   * it, which reads as "no command to print".
   */
  command?: string;
  /** The wait in one sentence, server-written (#2539). See `WaitLink`. */
  sentence?: string;
  link?: WaitLink | null;
  /**
   * A merge over a merge verify that failed at the PR's current head: the
   * item's MERGE VERIFY section, the first move — `link` is the second (#3460).
   */
  verify_link?: WaitLink | null;
};

/**
 * A wait's move when no command makes it — a merge, which happens on the forge:
 * `Open PR #2059 to merge`. The label has no arrow in it; the markup adds `↗`.
 * Mirrors `WaitLinkDTO`.
 */
export type WaitLink = { label: string; href: string };

export type EpicNode = {
  id: string;
  title: string;
  status: string;
  /**
   * What this is DOING, in the console's one motion vocabulary
   * ($lib/factory/motion): "executing", "waiting_kew", "waiting_human" or
   * "settled". Optional because a replayed tick from a server older than
   * #2305 carries none — `motionOf(status)` is the fallback, and it is the
   * same table the server derives this from.
   */
  motion?: string;
  /** `motion !== "settled"`: an open concern rather than history. */
  alive?: boolean;
  status_since: string | null;
  url: string | null;
  pr_url: string | null;
  external: boolean;
  stuck: string | null;
  /** null means "nobody is waiting" ONLY when snapshot.unavailable is empty. */
  attention: EpicNodeAttention | null;
  /** What the driver recorded about a blocked node; render nothing when null. */
  blocked_reason: string | null;
  /**
   * A failed node's recovery command, server-filled from its own stuck reason
   * (`kew.waits.failed_node_command`): retry, continue for preserved work, or
   * `''` when no command recovers it. Optional because an older serve omits it.
   */
  recovery_command?: string;
};

export type EpicSnapshot = {
  epic_ref: string;
  tick_id: number;
  tick_ts: string;
  signal: string;
  counts: Record<string, number>;
  frontier: string[];
  nodes: EpicNode[];
  edges: [string, string][];
  /**
   * Derived annotations this snapshot could not compose ("attention",
   * "attention:approval_holds", "attention:epic_holds",
   * "attention:epic_archive", "attention:<item>"). A non-empty list means a
   * node's null `attention` is unknown, not clear — say so instead of drawing
   * a DAG on which nobody appears to need a person.
   */
  unavailable: string[];
};

export type EpicEconomics = {
  window_start: string | null;
  drain_total_usd: number;
  node_drain_usd: Record<string, number>;
  label: string;
};

export type EpicHeartbeat = {
  last_tick_started_at: string | null;
  last_tick_ts: string | null;
  last_tick_duration_s: number | null;
  tick_count: number;
  // The cadence this heartbeat is thresholded against: a poller's own
  // `--interval`, else the configured expectation for an externally-driven
  // epic. `cadence_source` below says which.
  interval_s: number | null;
  last_signal: string | null;
  change_count: number;
  // Which control plane this driver reads ("github" | "linear"), null on a row
  // no tick has refreshed since the column landed.
  tracker?: string | null;
  // The liveness reading: "live" | "gone" | "unknown". "unknown" is NOT "not
  // gone" — it means no cadence was recorded to threshold the heartbeat
  // against, so a surface must say so rather than paint it green.
  driver_status?: string;
  // Where that cadence came from: "recorded" | "expected" | "none".
  cadence_source?: string;
  // The epic's tick-ATTEMPT stamp (#2304): a token that moves whenever the
  // driver opens or closes a row of its attempt log. `tick_count` above cannot
  // serve as that signal — it is written past the lock, the preflight and the
  // non-empty-graph guard, so a driver that is locked out, failing preflight or
  // finding an empty graph never moves it. Re-read a tick strip when THIS
  // changes. Optional because a `kew serve` predating #2304 omits it.
  tick_attempt_stamp?: string;
};

export type EpicSummary = {
  epic_ref: string;
  signal: string | null;
  counts: Record<string, number>;
  drain_total_usd: number;
  last_tick_ts: string | null;
  // The tick's START (written before any work), which is what liveness is read
  // from — `last_tick_ts` only lands when a tick finishes. Optional because a
  // `kew serve` predating #2283 omits it; `$lib/epic/roster` falls back to the
  // finish stamp there.
  last_tick_started_at?: string | null;
  // The rest of the heartbeat the epic page reads (#2844 N18): the last tick's
  // duration, which widens the freshness window, and the newest tick ATTEMPT's
  // instants, which `heartbeatFromTickLog` takes over a staler driver-state
  // stamp. On every row, so the reading never waits on a strip. Optional
  // because a `kew serve` predating #2844 omits them.
  last_tick_duration_s?: number | null;
  last_attempt_started_at?: string | null;
  last_attempt_finished_at?: string | null;
  // That attempt's outcome, null while it runs (#2882): only an attempt that
  // reached a tick advances the heartbeat. Optional for the same reason.
  last_attempt_outcome?: string | null;
  // Whether that attempt got past `begin_epic_tick` (#2882): an open one is a
  // beat only once it has. Optional for the same reason.
  last_attempt_reached_tick?: boolean;
  interval_s: number | null;
  change_count: number;
  label: string;
  // The operator's retirement decision (#2206), derived server-side from the
  // latest attested `epic_archived`/`epic_unarchived` event. Optional because a
  // `kew serve` predating #2206 omits all three — `$lib/epic/roster` reads a
  // missing `archived` as "not archived" rather than hiding the epic.
  archived?: boolean;
  archived_at?: string | null;
  archive_reason?: string;
  // Same tick-ATTEMPT stamp as `EpicHeartbeat.tick_attempt_stamp` (see there):
  // what a roster re-reads a row's tick strip on. Optional for the same reason.
  tick_attempt_stamp?: string;
  // Same liveness reading as `EpicHeartbeat.driver_status` — "live" | "gone" |
  // "unknown" — composed server-side off the newest heartbeat this driver
  // writes, the lock's own included. The row needs it for its tick strip: an
  // open attempt row carries only its START, so without this reading a tick
  // that legitimately runs longer than `TICK_LIVE_MAX_MS`
  // ($lib/factory/tick-strip) reads abandoned while the server calls its
  // driver live. Optional because a `kew serve` predating it omits it, and the
  // strip then falls back to its own floor.
  driver_status?: string;
};

export type MergedAction = { id: string; approved_by: string | null };

// Note: the Pydantic DTO types `actions` as `dict[str, Any]` (its docstring
// documents the enriched merged-entry shape); this narrower type reflects
// that documented shape for the UI's benefit and is a deliberate refinement,
// not a mismatch to "fix".
export type EpicActions = {
  schema?: number;
  dispatched?: string[];
  merged?: MergedAction[];
  /** Out-of-band-merged node ids (#2304): kew did not merge them, but the PR
   * merged and the item closed, so the snapshot already paints them DONE. */
  closed?: string[];
  held?: string[];
  stuck?: { id: string; reason: string }[];
};

/**
 * One epic-driver tick ATTEMPT (#2304) — the tick strip's row.
 *
 * Not an `EpicTick`: that carries a whole DAG snapshot and comes from the
 * change journal, which drops the ~89% of ticks that changed nothing and never
 * saw the failed, locked-out and empty-graph attempts at all.
 *
 * `ts_finished` and `outcome` are null while the tick is still running, which
 * is the one state a strip draws as live.
 */
export type EpicTickStrip = {
  id: number;
  ts_started: string;
  ts_finished: string | null;
  outcome: string | null;
  signal: string | null;
  n_dispatched: number;
  n_merged: number;
  n_held: number;
  n_stuck: number;
  journal_tick_id: number | null;
  error_class: string | null;
  // Past `begin_epic_tick` (#2882): the row opens before the lock, the
  // preflight and the graph build, so an open attempt without it is no beat.
  // Optional: a `kew serve` predating it does not serve it.
  reached_tick?: boolean;
};

export type EpicTick = {
  id: number;
  tick_ts: string;
  snapshot: EpicSnapshot;
  actions: EpicActions;
  economics: EpicEconomics | null;
};

export type EpicNodeRun = {
  run_id: string;
  status: string;
  model: string;
  runner: string;
  cost_usd: number;
  total_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
  pr_url: string | null;
  started_at: string | null;
  finished_at: string | null;
};

export type EpicNodeDetail = {
  id: string;
  runs: EpicNodeRun[];
  lifetime_usd: number;
  drain_usd: number;
};

/**
 * Where an epic's time went, per node (mirrors EpicTimelineDTO, #2270). One
 * read-only ledger projection: status bands from the driver's transitions,
 * runs and the merge from the run history, verdicts from the review rounds.
 * External boundary nodes have no lane. `nodes` arrives ordered by first
 * activity, the ones with none last, so the chart draws in that order.
 */
export type EpicTimelineBand = {
  status: string;
  /** Open at `from`; a null `to` means "still in this status", draw to `now`. */
  from: string;
  to: string | null;
};

export type EpicTimelineRun = {
  run_id: string;
  /** The stored run_kind: dispatch, adversarial_review, review_remediation... */
  kind: string;
  status: string;
  started_at: string | null;
  /** Null while the run is in flight. */
  finished_at: string | null;
  cost_usd: number;
  /** Which attempt this bar is, so its link can select it (#2300). */
  attempt_number: number;
};

export type EpicTimelineRound = {
  round: number;
  /**
   * The round's effective verdict: `READY_WITH_RESIDUALS` when the driver
   * accepted a low/medium-only `NEEDS_CHANGES` round (#2151), else the
   * reviewer's own verdict.
   */
  verdict: string;
  recorded_at: string;
};

export type EpicTimelineNode = {
  id: string;
  title: string;
  bands: EpicTimelineBand[];
  runs: EpicTimelineRun[];
  rounds: EpicTimelineRound[];
  merged_at: string | null;
};

export type EpicTimeline = {
  epic_ref: string;
  now: string;
  span: { start: string | null; end: string };
  nodes: EpicTimelineNode[];
};

export type EpicDetail = {
  epic_ref: string;
  snapshot: EpicSnapshot | null;
  heartbeat: EpicHeartbeat | null;
  economics: EpicEconomics;
};

/** `signal` lets a caller abort a read it no longer wants (#2900 C3). */
export const fetchEpics = (f: typeof fetch = fetch, signal?: AbortSignal) =>
  get<EpicSummary[]>("/api/epics", f, signal);

export const fetchEpic = (ref: string, f: typeof fetch = fetch, signal?: AbortSignal) =>
  get<EpicDetail>(`/api/epics/${encodeURIComponent(ref)}`, f, signal);

export const fetchEpicTicks = (
  ref: string,
  after = 0,
  f: typeof fetch = fetch,
) =>
  get<EpicTick[]>(
    `/api/epics/${encodeURIComponent(ref)}/ticks?after=${after}`,
    f,
  );

/**
 * The driver's last `limit` tick ATTEMPTS, oldest-first (#2304).
 *
 * Deliberately NOT `fetchEpicTicks`: that one returns full DAG snapshots, and
 * building a strip out of two hundred of them is the read that made the epic
 * page slow.
 */
export const fetchEpicTickStrip = (ref: string, limit = 200, f: typeof fetch = fetch) =>
  get<EpicTickStrip[]>(
    `/api/epics/${encodeURIComponent(ref)}/tick-strip?limit=${limit}`,
    f,
  );

/** The newest `limit` attempts a CHANGE strip draws (#2844 D23): the ones that
 * journaled a change, failed, or are still running — however many no-op
 * ticks sit between them. Oldest-first, like the tail. */
export const fetchEpicChangeStrip = (ref: string, limit = 200, f: typeof fetch = fetch) =>
  get<EpicTickStrip[]>(
    `/api/epics/${encodeURIComponent(ref)}/tick-strip?changes=true&limit=${limit}`,
    f,
  );

/** One bounded page of a loop's newest iterations, oldest-first (#2304). */
export const fetchLoopIterations = (
  loopId: string,
  limit = 200,
  f: typeof fetch = fetch,
) =>
  get<Iteration[]>(
    `/api/loops/${encodeURIComponent(loopId)}/iterations?limit=${limit}`,
    f,
  );

/** Where this epic's time went, per node (#2270) — the Gantt's one read. */
export const fetchEpicTimeline = (ref: string, f: typeof fetch = fetch) =>
  get<EpicTimeline>(`/api/epics/${encodeURIComponent(ref)}/timeline`, f);

export const fetchEpicNode = (
  ref: string,
  node: string,
  f: typeof fetch = fetch,
) =>
  get<EpicNodeDetail>(
    `/api/epics/${encodeURIComponent(ref)}/nodes/${encodeURIComponent(node)}`,
    f,
  );

// --- Artifact freshness: what a verdict was about, and what moved since ----
// Mirrors PrFreshnessDTO (kew/src/kew/service/models.py). One read for every
// open item with a linked PR, so a surface never asks per node.

/** The last governed-review round recorded for an item. */
export type ReviewVerdictRef = {
  round: number;
  verdict: string;
  /** The head the reviewer actually read — the whole point of the row. */
  head_sha: string;
  recorded_at: string;
};

/** The newest head this host has a record of, and where that came from. */
export type LatestHead = {
  sha: string;
  /** `snapshot` | `review_round` | `unobserved`. */
  source: string;
  at: string | null;
};

/** How a head stands against its base branch. */
export type BaseState = {
  /** `clean` | `behind` | `conflicted` | `blocked` | `unknown`. */
  state: string;
  /** Always a sentence — never a blank box (DESIGN.md). */
  reason: string;
  /** When this state was first journaled; null when nobody looked. */
  since: string | null;
  /** When the state was last READ — NOT the row's `observed_at`, which is the
   * tick that reported it. The driver carries a merge state forward across a
   * tick GitHub answered UNKNOWN on, so a state can be older than its tick.
   * null means nothing read it. */
  at: string | null;
  /** `snapshot` | `unobserved`. */
  source: string;
};

export type PrFreshnessRow = {
  item_id: string;
  epic_ref: string;
  pr_number: number | null;
  pr_url: string | null;
  last_verdict: ReviewVerdictRef | null;
  latest_head: LatestHead;
  /** `current` | `stale` | `remediating` | `reviewing` | `no_round`. */
  review_state: string;
  base: BaseState;
  observed_at: string;
};

export type PrFreshness = {
  rows: PrFreshnessRow[];
  /** `epic_ref|item_id` when the page filled, else null. */
  next_cursor: string | null;
  /** Reads that did not happen; an empty `rows` is only "nothing stale" when
   * this is empty too. */
  unavailable: string[];
  composed_at: string;
};

/**
 * One page of artifact freshness.
 *
 * `epic` narrows the page to one epic ref, server-side. A surface that draws
 * ONE epic has to pass it: the page is bounded, so a global read can fill up
 * before it reaches the epic on screen — and a page that never carried those
 * rows draws no badge on any of them, which reads exactly like "nothing here
 * is stale" (#2303). `freshness.ts`'s `readFreshness` follows `next_cursor`
 * from here and reports what it could not reach.
 */
export const fetchPrFreshness = (
  limit = 50,
  cursor: string | null = null,
  epic: string | null = null,
  f: typeof fetch = fetch,
) =>
  get<PrFreshness>(
    `/api/prs/freshness?limit=${limit}` +
      (cursor ? `&cursor=${encodeURIComponent(cursor)}` : "") +
      (epic ? `&epic=${encodeURIComponent(epic)}` : ""),
    f,
  );

/**
 * The live needs-you join on its own (mirrors EpicAttentionDTO). Attention
 * moves for reasons the tick journal never records — a PR merged by serve's
 * own sweep, an approval hold opening or clearing — so it streams as its own
 * frame and is merged into the snapshot already on screen. `by_node` is
 * complete for the nodes it covers: an absent node has nobody waiting on it,
 * subject to `unavailable` (same vocabulary as `EpicSnapshot.unavailable`).
 */
export type EpicAttention = {
  by_node: Record<string, EpicNodeAttention>;
  unavailable: string[];
};

/** One node's recovery state as it reads now (mirrors NodeRecoveryDTO). */
export type EpicNodeRecovery = {
  stuck: string | null;
  blocked_reason: string | null;
  recovery_command: string;
};

/**
 * Each node's stuck reason and recovery command on their own (mirrors
 * EpicRecoveryDTO). They move without a tick — a continuation claimed or
 * abandoned elsewhere rereads the journaled reason (#2960) — so they stream
 * as their own frame and are merged into the snapshot already on screen.
 */
export type EpicRecovery = {
  by_node: Record<string, EpicNodeRecovery>;
};

export type EpicFrame =
  | { type: "snapshot"; data: EpicTick }
  | { type: "attention"; data: EpicAttention }
  | { type: "recovery"; data: EpicRecovery }
  | { type: "economics"; data: EpicEconomics }
  | { type: "heartbeat"; data: EpicHeartbeat };

export function streamEpic(
  ref: string,
  onFrame: (f: EpicFrame) => void,
): EventSource {
  const es = new EventSource(`/api/epics/${encodeURIComponent(ref)}/stream`);
  es.onmessage = (m) => {
    const frame = parseFrame<EpicFrame>(m.data);
    if (frame !== undefined) onFrame(frame);
  };
  return es;
}

// --- Governance surface: approvals, fleet, budget, auth --------------------
// Types mirror the Pydantic DTOs in kew/src/kew/serve (HoldDTO, FleetStatusDTO)
// and the /api/config/budget shape.

export type Hold = {
  issue_num: number;
  // Durable work-item id (string) alongside the legacy numeric `issue_num` —
  // bare `SFD-12` for a non-GitHub tracker, absent on payloads that predate it.
  item_id?: string | null;
  estimate_usd: number | null;
  basis: string;
};

export type FleetStatus = {
  stopped: boolean;
  pending: number;
};

export type BudgetOverride = {
  value: number;
  set_by: string;
  set_at: string;
  reason: string;
} | null;

export type BudgetField = {
  effective: number;
  file_value: number;
  override: BudgetOverride;
};

export type BudgetMap = Record<string, BudgetField>;

export type SessionInfo = {
  authenticated: boolean;
  csrf_token: string | null;
};

export const fetchApprovals = (f: typeof fetch = fetch) =>
  get<Hold[]>("/api/approvals", f);

export const fetchFleetStatus = (f: typeof fetch = fetch) =>
  get<FleetStatus>("/api/fleet/status", f);

export const fetchBudget = (f: typeof fetch = fetch, signal?: AbortSignal) =>
  get<BudgetMap>("/api/config/budget", f, signal);

export const fetchSession = (f: typeof fetch = fetch) =>
  get<SessionInfo>("/api/auth/session", f);

// Reads the readable (httponly:false) kew_csrf cookie set at login.
function csrfToken(): string {
  const m = /(?:^|;\s*)kew_csrf=([^;]+)/.exec(
    typeof document === "undefined" ? "" : document.cookie,
  );
  return m ? decodeURIComponent(m[1]) : "";
}

async function postJSON<T>(
  path: string,
  body: unknown,
  f: typeof fetch = fetch,
): Promise<T> {
  const r = await f(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Kew-CSRF": csrfToken() },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw await refusal(path, r);
  return (await r.json()) as T;
}

async function del<T>(path: string, f: typeof fetch = fetch): Promise<T> {
  const r = await f(path, {
    method: "DELETE",
    headers: { "X-Kew-CSRF": csrfToken() },
  });
  if (!r.ok) throw await refusal(path, r);
  return (await r.json()) as T;
}

/**
 * The error a refused write throws: the server's own `detail` sentence when
 * the body carries one — a refusal that says why, or the demo build's "this
 * would…" (#3139) — and `<path> -> <status>` when it does not.
 */
async function refusal(path: string, r: Response): Promise<Error> {
  const detail = await actionDetail(r);
  return new Error(detail !== "" ? detail : `${path} -> ${r.status}`);
}

// Token in POST body ONLY — never a query/path parameter. Returns CSRF token.
export const login = (token: string, f: typeof fetch = fetch) =>
  postJSON<{ csrf_token: string }>("/api/auth/login", { token }, f);

export const decideHold = (
  issue: string,
  action: "approve" | "deny",
  reason: string,
  f: typeof fetch = fetch,
) =>
  postJSON<{ status: string; issue: string }>(
    `/api/approvals/${encodeURIComponent(issue)}`,
    { action, reason },
    f,
  );

// Mirrors FleetStopResultDTO (kew/src/kew/service/models.py): `runs_killed` is
// a list of OBJECTS, not of run-id strings — the backend has always returned
// the killed run with its item and its cost, and typing it as `string[]` meant
// any consumer reading `.run_id` type-checked against a lie (#2297).
export type KilledRun = {
  run_id: string;
  issue: number;
  item_id: string | null;
  cost_usd: number;
};

export type FleetStopResult = {
  stopped: boolean;
  already_stopped: boolean;
  runs_killed: KilledRun[];
};

export const stopFleet = (reason: string, f: typeof fetch = fetch) =>
  postJSON<FleetStopResult>("/api/fleet/stop", { reason }, f);

export const setBudget = (
  field: string,
  value: number,
  reason: string,
  f: typeof fetch = fetch,
) =>
  postJSON<{ field: string; effective: number }>(
    `/api/config/${field}`,
    { value, reason },
    f,
  );

export const revertBudget = (field: string, f: typeof fetch = fetch) =>
  del<{ field: string; effective: number }>(`/api/config/${field}`, f);

// --- Config page: every overridable field with its full resolution story -----
// Mirrors the GET /api/config/overridable payload (api_config.get_overridable):
// grouped by raw config section, each field carrying the four-layer resolution.
// Values are `unknown` because routing.* fields resolve to a {runner, model}
// dict while budget/breaker fields resolve to numbers.

export type ConfigOverride = {
  value: unknown;
  set_by: string;
  set_at: string;
  reason: string;
} | null;

export type ConfigField = {
  effective: unknown;
  file_value: unknown;
  default_value: unknown;
  override: ConfigOverride;
};

// section (budget / agent / routing / breaker) -> dotted field -> resolution
export type OverridableMap = Record<string, Record<string, ConfigField>>;

export const fetchOverridable = (f: typeof fetch = fetch) =>
  get<OverridableMap>("/api/config/overridable", f);

// --- Model catalog: GET /api/models (#2542) ---------------------------------
// The packaged catalog, one row per model id. A retired row is served only
// while the effective config still names it (`configured: true`).

export type CatalogModel = {
  id: string;
  display_name: string;
  provider: string;
  runner: string;
  price: { input_per_mtok: number; output_per_mtok: number } | null;
  lifecycle: { status: string; shutdown: string | null; replacement: string | null };
  routable: boolean;
  /** Lanes this project's effective config routes to the model. */
  routed_lanes: string[];
  /** Lanes kew's shipped catalog defaults to the model — not this project's routing. */
  default_lanes: string[];
  aliases: string[];
  configured: boolean;
};

export type ModelsPayload = { reviewed_at: string; models: CatalogModel[] };

export const fetchModels = (f: typeof fetch = fetch) => get<ModelsPayload>("/api/models", f);

// --- Scorecard: per-model cost + outcome breakdown ---------------------------

export type ComplexityCell = {
  complexity: string;
  runs: number;
  opened: number;
  merged: number;
  review_total: number;
  review_n: number;
  spend: number;
  // Planned/actual cost pair (#1092). `planned_spend` is the token x
  // pricing-bound figure dispatch planned against; `paired_spend` is the actual
  // spend of the same `planned_n` runs. Runs predating the column recorded no
  // bound and are excluded from all three — never counted as $0 planned.
  planned_spend: number;
  paired_spend: number;
  planned_n: number;
};

export type ScorecardRow = {
  model: string;
  // Execution channel computed on the backend (kew.pricing.is_free_model):
  // "local" = free/local inference (ollama/, lemonade/), "hosted" otherwise.
  // The headline reads this instead of re-deriving the heuristic (N11).
  channel: string;
  // Lifecycle status from the model-eol snapshot: "ok" | "untracked" | "watch"
  // | "scheduled" | "retiring" | "retired". Empty when the model is absent from
  // the snapshot or the snapshot is not loaded.
  lifecycle_status: string;
  runs: number;
  opened: number;
  merged: number;
  review_total: number;
  review_n: number;
  spend: number;
  planned_spend: number;
  paired_spend: number;
  planned_n: number;
  by_complexity: ComplexityCell[];
};

export type Scorecard = { days: number; rows: ScorecardRow[] };

// Default scorecard window. 30 days (a month of work) rather than a few days so
// the leverage story — frontier spend vs local-model $0 for the same merged
// work — is visible on open even when recent activity is all one model. The
// selector still offers 3/7/30/90; this only sets which window opens first.
export const DEFAULT_SCORECARD_DAYS = 30;

export const fetchScorecard = (
  days = DEFAULT_SCORECARD_DAYS,
  f: typeof fetch = fetch,
) => get<Scorecard>(`/api/report/models?days=${days}`, f);

// --- Outcomes: what the factory delivered, week by week (C3/#2301) ----------
// Mirrors OutcomeReportDTO. Every week in the window is a row, including the
// ones with nothing in them: a gap in the ledger is a flat point, and a
// missing row would be a hole in the shared x-axis. `merged_main` is the one
// field not from the ledger — a local `git log origin/main` read — and is null
// when that clone could not be read, which is why `merged_main_unavailable`
// exists for the chart to print.

export type OutcomeQuantiles = {
  p50: number | null;
  p90: number | null;
  max: number | null;
};

export type OutcomeRounds = { p50: number | null; max: number | null };

export type OutcomeCost = { mean: number | null; p50: number | null };

export type OutcomeWeek = {
  /** ISO date of the week's Monday, UTC. */
  week_start: string;
  /** Distinct PRs squash-merged into origin/main, or null when unreadable. */
  merged_main: number | null;
  /** Distinct merged PRs with any kew run against them. */
  merged_kew_touched: number;
  /** Of those, the ones an implementation run of kew's own opened. */
  merged_kew_authored: number;
  /** Sample size behind ready_hours and rounds_to_ready. */
  ready_items: number;
  /** Dispatch to first READY verdict, in hours. */
  ready_hours: OutcomeQuantiles;
  rounds_to_ready: OutcomeRounds;
  cost_per_merged: OutcomeCost;
  /** Spend on runs DISPATCHED in the week. */
  spend_total: number;
  /** The part of spend_total carrying no PR. */
  spend_unattributed: number;
  nodes_done: number;
  /** When the newest merged-PR snapshot in the week was written. */
  pr_sync_observed_at: string | null;
  /** True for the week in progress. */
  partial: boolean;
};

export type OutcomeReport = {
  weeks: OutcomeWeek[];
  /** Sample size below which a week is greyed as too thin to read. */
  min_sample: number;
  merged_main_head: string | null;
  merged_main_unavailable: string | null;
};

// Eight weeks: two months of delivery, long enough for a trend and short
// enough that every point is a week the operator remembers.
export const DEFAULT_OUTCOME_WEEKS = 8;

export const fetchOutcomes = (
  weeks = DEFAULT_OUTCOME_WEEKS,
  f: typeof fetch = fetch,
) => get<OutcomeReport>(`/api/report/outcomes?weeks=${weeks}`, f);

// --- Budget: spend vs effective caps over the governor's windows ------------
// Mirrors SpendWindowsDTO / SpendCellDTO. `agentic` is the ENFORCED figure:
// the cost of every run kew records, reviews included, over the governor's
// window (today's UTC day / that day and the six before it), computed
// identically to budget.check_budget — this is what a
// cap bar must compare against `cap` (N5). `interactive` (Claude Code OTel
// interactive_usage) is observed spend over the same window that the governor
// does NOT gate. `cap` is the effective config cap (null = "no cap");
// `spent` === agentic + interactive is the observed total, enforced portion
// aside.

export type SpendCell = {
  window_days: number;
  spent: number;
  cap: number | null;
  agentic: number;
  interactive: number;
};
export type SpendWindows = { daily: SpendCell; weekly: SpendCell };

export const fetchSpend = (f: typeof fetch = fetch) =>
  get<SpendWindows>("/api/report/spend", f);

// --- Audit: hash-chained governance event log (F4) --------------------------
// Types mirror AuditEventDTO / AuditListDTO / AuditStatusDTO. The list is
// newest-first; `next_after` is the cursor to pass as ?after= for the next
// (older) page, or null when the current page is the tail.

export type AuditEvent = {
  id: number;
  timestamp: string;
  event: string;
  issue_num: number | null;
  item_id: string | null;
  run_id: string | null;
  actor_type: string | null;
  actor_id: string | null;
  summary: string | null;
  // Epic attribution, read server-side from the FULL payload — present even
  // when `summary` was truncated past it.
  epic_ref: string | null;
};

export type AuditList = {
  events: AuditEvent[];
  next_after: number | null;
};

export type AuditStatus = {
  events: number;
  verified: boolean;
  anchors_matched: number;
  anchors_total: number;
};

// `since` is inclusive, `until` exclusive, so adjacent windows tile the log.
// `item` is a durable item id (or an issue number); `epic` an epic ref, which
// matches that epic's node events AND its epic-level events. `event_prefix`
// values OR together.
export type AuditFilters = {
  event?: string;
  actor_type?: string;
  issue?: number;
  since?: string;
  until?: string;
  item?: string;
  epic?: string;
  event_prefix?: string[];
  limit?: number;
  after?: number;
};

/** The filters both audit reads share, as query params. */
const auditScopeParams = (filters: AuditFilters): URLSearchParams => {
  const q = new URLSearchParams();
  if (filters.event) q.set("event", filters.event);
  if (filters.actor_type) q.set("actor_type", filters.actor_type);
  if (filters.issue != null) q.set("issue", String(filters.issue));
  if (filters.since) q.set("since", filters.since);
  if (filters.until) q.set("until", filters.until);
  if (filters.item) q.set("item", filters.item);
  if (filters.epic) q.set("epic", filters.epic);
  for (const prefix of filters.event_prefix ?? [])
    if (prefix) q.append("event_prefix", prefix);
  return q;
};

export const fetchAudit = (
  filters: AuditFilters = {},
  f: typeof fetch = fetch,
) => {
  const q = auditScopeParams(filters);
  if (filters.limit != null) q.set("limit", String(filters.limit));
  if (filters.after != null) q.set("after", String(filters.after));
  const qs = q.toString();
  return get<AuditList>(`/api/audit${qs ? `?${qs}` : ""}`, f);
};

// Mirrors AuditHistogramDTO. Buckets are dense: every bucket in the window is
// present, empty ones with `counts: {}`. The bucket counts sum to `total`.
export type AuditHistogramBucket = {
  start: string;
  counts: Record<string, number>;
};

export type AuditHistogram = {
  since: string;
  until: string;
  bucket_s: number;
  buckets: AuditHistogramBucket[];
  total: number;
};

/** Counts per event name over [since, until). Over 500 buckets is a 422. */
export const fetchAuditHistogram = (
  window: { since: string; until: string; bucket_s: number },
  filters: AuditFilters = {},
  f: typeof fetch = fetch,
) => {
  const q = auditScopeParams({ ...filters, ...window });
  q.set("bucket_s", String(window.bucket_s));
  return get<AuditHistogram>(`/api/audit/histogram?${q.toString()}`, f);
};

export const fetchAuditStatus = (f: typeof fetch = fetch) =>
  get<AuditStatus>("/api/audit/status", f);

// --- Console dashboard (#700, Group 1) — aggregate judgment-queue payload ----
// Types mirror the Dashboard*DTO Pydantic models in
// kew/src/kew/service/models.py field-for-field. Rows are a discriminated
// union on `type`; unknown types are ignored by contract (additivity).

export type DashboardBanner = {
  fleet_stopped: boolean;
  no_live_driver: boolean;
};

export type DashboardRow = {
  type: string;
  consequence: string;
  age: string; // ISO timestamp of the row's underlying event
  reason: string;
  primary_action: string;
  issue_num: number | null;
  // Durable string id mirroring `issue_num`; the real key for a non-GitHub
  // (Linear) row whose `issue_num` is the 0 sentinel. Render via `itemRef`.
  item_id?: string | null;
  estimate_usd: number | null;
  epic_ref: string | null;
  url: string | null;
  pr_number: number | null;
  // True when the work projection could not read THIS row's item (I19). Served
  // on the `work_unreadable` row and on whatever higher-consequence row
  // displaced it, since the set collapses to one entry per item. Absent from a
  // `kew serve` that predates #2207 — read as "it was read".
  work_unread?: boolean;
  // What this row IS and what to run (#2244). All three are additive and
  // absent from a `kew serve` that predates them — read an absent one as ''.
  //
  // `attention_kind` is the `AttentionKind` the row's wait maps to
  // (`budget_exhausted`, `held`, `merge_ready`, `publication_held`,
  // `sandbox_refused`) or one of the work projection's own (`merge_decision`,
  // `work_exception`, `approval`). An open vocabulary: tolerate an unknown
  // one. It is the category to read, never the row `type` — every `epic_holds`
  // row is served as `budget_hold` whatever held the node.
  attention_kind?: string;
  // The reason in an operator's words, from `kew.waits.StallBucket.label`.
  // '' when no bucket claims the reason, and then `reason` is all anybody
  // wrote. Python owns this wording; nothing here may invent it.
  label?: string;
  // A command with this row's epic and item already in it, runnable as
  // printed, or '' when no kew command clears the row (a cap to raise, a merge
  // decision, an approval this console has a button for). Never a template:
  // the first-pass UI printed `kew epic continue <name> <node>` unfilled, and
  // a `kew epic resume` that has never existed.
  command?: string;
  // The row in ONE sentence, written by the server (`attention.say_wait`,
  // #2539) with no leading item reference. Work, Monitor, the epic popover and
  // the rail print it and never paraphrase it. Optional on the wire: a serve
  // that predates it omits it, and the reason as written stands in.
  sentence?: string;
  // The item's own title as its tracker names it (#2786), or '' where the
  // server holds none. Optional on the wire: a serve that predates it omits it.
  title?: string;
  // A merge decision's move, where no command makes it.
  link?: WaitLink | null;
  // A merge over a merge verify that failed at the PR's current head leads with
  // the failure: the item's MERGE VERIFY section, before `link` (#3460).
  verify_link?: WaitLink | null;
  // Every pytest node id a `test_declarations_missing` hold asks a person to
  // declare, untruncated (#2884): the reason is capped and ends `+N more`.
  // Empty or absent on every other row, and from a serve that predates it.
  node_ids?: string[];
  // The repository a row names when it names no item — a held merge-queue
  // main (#2982) is one per repo. Absent on every other row.
  repo?: string | null;
};

export type RouteRow = {
  complexity: string;
  route: string;
  provenance: string; // "file" | "default"
};

export type ReadyIssue = {
  issue_num: number;
  // Durable string id mirroring `issue_num` (see `ReadyIssueDTO`). Render via
  // `itemRef`; queueing stays GitHub-numeric (`issue_num`).
  item_id?: string;
  title: string;
  complexity: string;
  route: string;
  estimate_usd: number | null;
};

export type QueueCandidate = {
  issue_num: number;
  item_id?: string;
  title: string;
};

export type Completion = {
  run_id: string;
  issue_num: number;
  // The real key for a non-GitHub completion whose `issue_num` is 0. Render via
  // `itemRef`.
  item_id?: string;
  title: string;
  status: string;
  cost_usd: number;
  model: string;
  finished_at: string;
};

export type DashboardGlance = {
  fleet: FleetStatus | null;
  caps: SpendWindows | null;
  routing: RouteRow[];
  ready_queue: ReadyIssue[];
  queue_candidates: QueueCandidate[];
  recent_completions: Completion[];
  // Human-review capacity (I19), read once with the rows from the same
  // snapshot. `null` is "the reading itself could not be taken" — deliberately
  // NOT a zeroed reading, which would render an unreadable queue as an empty
  // one. Optional so a payload from a serve that predates I19 still types.
  pressure?: ReviewPressure | null;
};

export type Dashboard = {
  banner: DashboardBanner;
  rows: DashboardRow[];
  glance: DashboardGlance;
  as_of: Record<string, string>;
  unavailable: string[];
  /**
   * `rows` counted per epic — the needs-you set, filtered once, on the server
   * (#2279). The header counts `rows`, an epic page's legend and the Epics
   * list read their own bucket out of this, so no two of the three can derive
   * a different number from one set. An epic with nothing waiting is absent
   * (which is 0); an entry naming no epic is in no bucket, so the header's
   * count is this map's sum plus the fleet-wide entries. Optional for a
   * `kew serve` that predates the field.
   */
  needs_you_by_epic?: Record<string, number>;
  /**
   * The entries naming no epic, counted (#2784) — the bucket the map above has
   * no key for, drawn as the Epics list's "no epic" row. The header's count is
   * the map's sum plus this. Optional for a `kew serve` that predates it.
   */
  needs_you_no_epic?: number;
};

export const fetchDashboard = (f: typeof fetch = fetch) =>
  get<Dashboard>("/api/dashboard", f);

// --- Canonical work projection (I02 / I19 / I20) ----------------------------
// Types mirror the WorkViewDTO family in kew/src/kew/service/models.py
// field-for-field. This is the ONLY contract the console reads customer state,
// review capacity and next actions from: the six orthogonal dimensions, the
// four I19 ones beside them, the ONE derived exception, the ONE safest action,
// and the actions the same verdict stands behind. Nothing here is re-derived
// client-side — a surface that recomputed any of it would be a second, quietly
// disagreeing resolver, which is precisely what the projection exists to end.

export type RemoteObservation = {
  dimension: string;
  // "available" | "stale" | "unavailable". `unavailable` means the read did not
  // happen or failed — every dimension resting on it stays unknown, and an
  // empty result is only ever reported under `available`.
  availability: string;
  provenance: string; // "observed" | "cached" | "unobserved" | "error"
  observed_at: string | null;
  detail: string | null;
};

export type WorkIdentity = {
  source_tracker: string | null;
  item_id: string | null;
  item_ref: string; // display form: "#42" for GitHub, "SFD-12" bare
  delivery_repo: string | null;
  epic_ref: string | null;
  node_id: string | null;
  run_id: string | null;
  attempt_number: number;
};

export type WorkSource = {
  tracker: string | null;
  item_state: string; // "open" | "closed" | "unknown"
  synchronization: string; // "synchronized" | "pending" | "not_applicable" | "unknown"
  observation: RemoteObservation;
};

export type WorkRoute = {
  runner: string;
  model: string;
  reported_model: string | null;
  containment: string; // "host" | "contained" | "unknown"
  // Where the attempt ran (#2398). Optional because a console can be newer than
  // the kew it is talking to: a server that predates the field sends neither,
  // and a route that renders nothing about placement is right for that server.
  host?: string; // the fleet host: "local" | "gcp"
  instance?: string; // the machine within that host; "" for local, which is one
  provenance: string; // "declared" — never an enforced/measured claim
  claim: string; // "honored" | "substituted" | "unverified"
};

export type WorkExecution = {
  state: string; // "not_started" | "in_flight" | "completed" | "terminal"
  run_status: string | null;
  attempts: number;
  started_at: string | null;
  finished_at: string | null;
  error_category: string;
};

export type WorkArtifact = {
  state: string; // "absent" | "preserved" | "published"
  publication: string; // "not_required" | "pending" | "published" | "unknown"
  branch: string | null;
  pr_url: string | null;
  orphan: boolean;
};

export type WorkJudgment = {
  state: string; // "not_applicable" | "awaiting_human_decision" | "accepted" | …
  observation: RemoteObservation;
  decided_at: string | null;
};

export type WorkOutcome = {
  state: string; // "in_progress" | "delivered" | "abandoned" | "failed" | "unknown"
  detail: string;
};

export type WorkIntegration = {
  checks: string; // "passing" | "failing" | "pending" | "none" | "unknown"
  mergeability: string; // "clean" | "conflicted" | "unknown" | "not_applicable"
  review_decision: string; // "approved" | "changes_requested" | "review_required" | …
  observation: RemoteObservation;
};

export type WorkReadiness = {
  state: string; // "proven" | "refuted" | "expired" | "unproven" | "unknown"
  fingerprint: string | null;
  evidence_ref: string | null;
  detail: string;
  observation: RemoteObservation;
};

export type WorkNoChange = {
  state: string; // "not_applicable" | "pending" | "authorized" | "synchronizing" | …
  judgment_state: string;
  policy: string;
  reasons: string[];
  sync_outstanding: number;
  sync_failed: number;
  effects_outstanding: number;
};

export type ReviewPressure = {
  delivery_repo: string;
  engaged: boolean;
  // False means the forge could not be read (or the reading is too old to count
  // from). That is NOT zero, and the server reports it as saturated.
  countable: boolean;
  used: number;
  total: number;
  held: number;
  oldest_hold_age_seconds: number | null;
  diff_burden_lines: number;
  saturated: boolean;
  reasons: string[];
  enforcement: string; // "inactive" | "active"
  enforcement_detail: string;
  policy_generation: number;
  observation: RemoteObservation;
};

export type WorkException = {
  state: string;
  detail: string;
  blocking: boolean;
  dimension: string;
};

export type SafestAction = {
  action: string;
  reason: string;
  // Set when the safest action is only "find out": a dimension the decision
  // rests on could not be read.
  blocked_by: string | null;
};

export type WorkAction = {
  action: string;
  intent: string;
  target_kind: string; // "run" | "no_change_judgment" | "publication_job" | …
  target_ref: string; // read back out of the projection's own facts
  safest: boolean;
  requires_reason: boolean;
};

export type WorkView = {
  identity: WorkIdentity;
  source: WorkSource;
  route: WorkRoute;
  execution: WorkExecution;
  artifact: WorkArtifact;
  judgment: WorkJudgment;
  outcome: WorkOutcome;
  integration: WorkIntegration;
  readiness: WorkReadiness;
  no_change: WorkNoChange;
  review_pressure: ReviewPressure;
  exception: WorkException;
  safest_action: SafestAction;
  // Empty is a real answer: nothing kew can do is safe right now.
  actions: WorkAction[];
  view_revision: string;
  composed_at: string;
  observations: RemoteObservation[];
};

export type WorkProjection = {
  views: WorkView[];
  pressure: ReviewPressure;
  projection_revision: string;
  composed_at: string;
  // Items whose facts could not be read. Named, never silently dropped.
  unreadable: string[];
};

export type WorkActionResult = {
  action: string;
  target_kind: string;
  target_ref: string;
  applied: boolean;
  idempotent: boolean;
  detail: string;
  expected_revision: string;
  // Recomposed AFTER the effect — the caller's next decision is made against
  // the post-write world, not the one it asked about.
  view: WorkView;
};

/** The whole projection the console's rows were built from, one snapshot. */
export const fetchWorkProjection = (f: typeof fetch = fetch, signal?: AbortSignal) =>
  get<WorkProjection>("/api/dashboard/work", f, signal);

/**
 * The two reads the Cmd-K palette (#2516) needs beyond what the shared pulse
 * already holds: the epics and the work projection. Settled separately, so an
 * epic list that will not load still leaves every item findable — a failed
 * read is `null`, never an empty list standing in for one.
 */
export async function fetchPaletteSources(
  f: typeof fetch = fetch,
): Promise<{ epics: EpicSummary[] | null; projection: WorkProjection | null }> {
  const [epics, projection] = await Promise.allSettled([
    fetchEpics(f),
    fetchWorkProjection(f),
  ]);
  return {
    epics: epics.status === "fulfilled" ? epics.value : null,
    projection: projection.status === "fulfilled" ? projection.value : null,
  };
}

/** One palette entry as the ask route sees it: an opaque id and its label. */
export type PaletteAskEntry = { id: string; label: string };

export type PaletteAskResponse = {
  results: Array<{ id: string; reason: string }>;
  cost_usd: number;
};

/**
 * The ask route's 429 (#2676): the day's spend has reached `[budget]
 * daily_usd`. `sentence` is the server's own one-line refusal naming the cap.
 */
export class PaletteAskRefused extends Error {
  constructor(readonly sentence: string) {
    super(sentence);
  }
}

/**
 * Ask the server's model to rank entries the palette already offers (#2518).
 * A write (it spends), so it carries the CSRF token like every other. Throws
 * `PaletteAskRefused` on the over-budget 429 and the demo build's 405 when
 * either carries a `detail` sentence, and a plain error on any other non-2xx;
 * the palette shows either as a line of text.
 */
export async function askPaletteModel(
  body: { query: string; entries: PaletteAskEntry[] },
  f: typeof fetch = fetch,
): Promise<PaletteAskResponse> {
  const path = "/api/palette/ask";
  const r = await f(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Kew-CSRF": csrfToken() },
    body: JSON.stringify(body),
  });
  // The two refusals that say why and stay true on a retry — the over-budget
  // 429, and the demo build's 405 "Demo: this would…" (#3139) — are read the
  // way `refusal` reads every other refused write (`actionDetail`), and the
  // sentence is what the palette shows. Any other status (a 401/403 auth or
  // CSRF failure, a 404 once ask is turned off, a 5xx) stays the plain error
  // below: the palette words it itself, and Enter can ask again.
  if (r.status === 429 || r.status === 405) {
    const sentence = await actionDetail(r);
    if (sentence !== "") throw new PaletteAskRefused(sentence);
  }
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return (await r.json()) as PaletteAskResponse;
}

/**
 * One item's view — the exact revision an action is decided against. The id is
 * opaque: a Linear key rides through the path unchanged (encoded, never parsed).
 */
export const fetchWorkView = (itemId: string, f: typeof fetch = fetch) =>
  get<WorkView>(`/api/verbs/work/${encodeURIComponent(itemId)}`, f);

/**
 * A non-throwing result: the caller renders 409 (the world moved, or that is
 * not on offer) inline on the row, which `get`/`postJSON`'s throw would turn
 * into an unhandled rejection. `status: 0` is a transport failure — a fact
 * about the network, never a verdict from the server.
 */
export type WorkActionResponse =
  | { ok: true; data: WorkActionResult }
  | { ok: false; status: number; detail: string };

async function actionDetail(r: Response): Promise<string> {
  try {
    const body = (await r.json()) as { detail?: unknown };
    return typeof body?.detail === "string" ? body.detail : "";
  } catch {
    return "";
  }
}

/**
 * Request ONE action the projection already offered, against the revision it
 * was offered at. This client names no action of its own: `action` comes from
 * a `WorkView.actions` entry, and which run / job / judgment it touches is
 * resolved server-side from the same facts.
 */
export async function performWorkAction(
  itemId: string,
  action: string,
  expectedRevision: string,
  reason = "",
  f: typeof fetch = fetch,
): Promise<WorkActionResponse> {
  const path = `/api/verbs/work/${encodeURIComponent(itemId)}/action`;
  let r: Response;
  try {
    r = await f(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Kew-CSRF": csrfToken(),
      },
      body: JSON.stringify({
        action,
        expected_revision: expectedRevision,
        reason,
      }),
    });
  } catch (e) {
    return { ok: false, status: 0, detail: (e as Error).message };
  }
  if (!r.ok)
    return { ok: false, status: r.status, detail: await actionDetail(r) };
  // A 2xx whose body is not JSON is a transport failure dressed as a success
  // (a proxy's HTML interstitial, a truncated response). It used to throw out
  // of here into a caller that only handles the typed result, which left the
  // Console row that asked for the action busy for good (C16).
  try {
    return { ok: true, data: (await r.json()) as WorkActionResult };
  } catch {
    return {
      ok: false,
      status: r.status,
      detail: "The server answered, but not with a readable result.",
    };
  }
}

// --- Item pipeline projection (FUI-11) --------------------------------------
//
// The DWork swimlane's data. Deliberately NOT a fixed grid: the payload lists
// whatever lanes and stages the server's facts support, so the table renders
// what it is given. kew composes one lane over implement / publish /
// review / merge and never emits a Reproduce stage or a backport lane, but the
// client must not assume that.

/**
 * What one stage's claim rests on. `kind` is open ("run", "output_scan",
 * "review_verdict", "pull_request" today); `ref` is the identifier within it.
 * Null when nothing was recorded — never invented.
 */
export type PipelineEvidence = {
  kind: string;
  ref: string;
};

/**
 * One cell of the swimlane. `state` is a PROGRESS vocabulary, not an
 * attestation one (that is the evidence chain's job): "pending", "working",
 * "done", "not_ready", "needs_you", "failed", "not_required" and "unknown" are composed
 * today, and it is open — render an unrecognised state by its `label` rather
 * than dropping the cell. `unknown` means a fact could not be read, and the
 * reason is named in `ItemPipeline.unavailable`.
 */
export type PipelineStage = {
  stage: string;
  state: string;
  label: string;
  detail: string;
  evidence: PipelineEvidence | null;
  /**
   * The stage is over though `state` does not say so: a published PR nobody
   * scanned is `pending` (the scan it lacks), and its PR is up all the same.
   * Absent from a host older than #3466.
   */
  passed?: boolean;
};

/** One row of the swimlane, named for the branch the work lands on. */
export type PipelineLane = {
  name: string;
  stages: PipelineStage[];
};

/**
 * The held-out phase of one merge verify (#3140): main's tests run against the
 * pull request. `failed` is every one that failed, by pytest node id;
 * `declared` the ones the PR body's **Behavior changes** excused; `undeclared`
 * the rest, any one of which fails the verify. Each list carries at most its
 * first 50 ids, each clipped to 300 characters with an ellipsis; `*_more`
 * counts the ids of that list left out.
 */
export type MergeVerifyHeldOut = {
  outcome: string;
  failed: string[];
  declared: string[];
  undeclared: string[];
  failed_more: number;
  declared_more: number;
  undeclared_more: number;
};

/**
 * The latest `verify_completed` for the item's pull request, joined by head:
 * the verified commit, or the non-base parent of a two-parent merge of the
 * base, against the pull request's heads in its current review cycle. `outcome` is
 * `passed`, `failed` or `incomplete`; `head` the pull-request head it joined
 * on, and `is_current_head` whether that is the head the PR has now.
 * Allowlisted fields only — the event's free-text error never crosses.
 */
export type MergeVerify = {
  outcome: string;
  head: string;
  is_current_head: boolean;
  error_category: string | null;
  held_out: MergeVerifyHeldOut | null;
  recorded_at: string;
};

export type ItemPipeline = {
  item_id: string;
  lanes: PipelineLane[];
  // Facts that could not be read. A stage is only honestly "pending" when its
  // reason is absent from here.
  unavailable: string[];
  composed_at: string;
  // The latest merge verify; `null` when none is linked to the PR's heads (or,
  // with `pipeline:merge_verify` in `unavailable`, none could be read).
  // Optional so a server that predates #3140 still reads as "nothing to say".
  merge_verify?: MergeVerify | null;
};

/**
 * One item's pipeline. The id is opaque: a Linear key rides through the path
 * unchanged (encoded, never parsed). An item nothing has run for answers its
 * stages, every one pending (#2572); it throws on 404 only for an id the
 * server does not know.
 */
export const fetchPipeline = (itemId: string, f: typeof fetch = fetch) =>
  get<ItemPipeline>(`/api/work/${encodeURIComponent(itemId)}/pipeline`, f);

// --- Item evidence chain (FUI-12) -------------------------------------------
//
// The DWork evidence ledger and the DEpic Evidence column read this: the
// ordered guarantees the item's route promises, each attested on the audit
// chain or honestly not. `state` is an ATTESTATION vocabulary, never a
// progress one — the swimlane (`ItemPipeline`) answers how far the work got.
//
// The rule a surface must not break: a green check requires `state ===
// "attested"`, which the server grants only for the host's own fact on a chain
// that verifies. Everything else is "unverified", "pending" or "missing".

/**
 * One row of the checklist. `n` is the 1-based position both artboards render
 * zero-padded; `event` is the audit event behind the guarantee; `summary` is
 * the already-worded second line ("clean · 3670e43f", or the stage's own words
 * while nothing is recorded); `ts` is the recorded row's ISO timestamp and is
 * empty when there is no row — never substitute another row's time.
 *
 * `state` is open (tolerate an unknown value by showing no check):
 * - "attested" — the host's own fact, on a chain that verifies;
 * - "unverified" — recorded, but not vouched for. NEVER draw a check here;
 * - "pending" — expected, not recorded yet;
 * - "missing" — expected, and its stage has already passed.
 */
export type EvidenceRow = {
  n: number;
  label: string;
  event: string;
  summary: string;
  ts: string;
  state: string;
};

export type ItemEvidence = {
  item_id: string;
  rows: EvidenceRow[];
  // The artboard's "3 / 4 attested". Read the numerator from here rather than
  // counting checks client-side, so both surfaces agree with the server.
  attested_count: number;
  total: number;
  // The audit chain's own verdict (the one /api/audit/status shows). False
  // means no row may read as attested, whatever else it says.
  chain_verified: boolean;
  // One sentence for the reader: why the rows read as they do.
  reason: string;
  // Facts that could not be read, machine-side.
  unavailable: string[];
  composed_at: string;
};

/**
 * One item's evidence chain. The id is opaque: a Linear key rides through the
 * path unchanged (encoded, never parsed). Unlike the pipeline this never 404s
 * on an item with no runs — the guarantees exist before the work does, and are
 * then all "pending" with `reason` saying so.
 */
export const fetchEvidence = (itemId: string, f: typeof fetch = fetch) =>
  get<ItemEvidence>(`/api/work/${encodeURIComponent(itemId)}/evidence`, f);

// --- Item description (FUI-31) ----------------------------------------------
//
// The DWork description panel reads this: what a person wrote about the item on
// its tracker. It is the only REMOTE read on the Work screen, so honesty comes
// before content — `availability` says whether there is anything to trust, and
// `reason` says why when there is not.
//
// The rule a surface must not break: render the body only for `availability`
// "available" or "stale" (label the stale one), and fall back to `reason` for
// everything else — plus the "Open on GitHub" link where `url` is set, which is
// a reading's field and so is null for every `unavailable` answer. An empty
// `description` under any other availability is a failed read, not a short
// issue.

/**
 * One item's tracker description, split where the body says so.
 *
 * - `availability` — "available" (read, or read recently), "stale" (an older
 *   reading, served because the refresh failed; `reason` says why) or
 *   "unavailable" (no reading at all). Open: treat an unknown value as
 *   unavailable rather than assuming three words.
 * - `provenance` — "observed" (read just now), "cached" (served from the TTL
 *   cache) or "error".
 * - `observed_at` — ISO time of the reading itself, null when there is none.
 *   Never substitute `composed_at`: that is when the payload was assembled.
 * - `url` / `title` — the READING's, so both are absent unless one was taken:
 *   an `unavailable` answer carries `url: null`, and a surface that wants a
 *   link out of it has none to offer (it must not compose one of its own).
 * - `expected` / `actual` — filled only from headings that name them, so both
 *   are empty for the many issues that have no such sections. Everything else,
 *   headings included, stays in `description` in the order it was written.
 */
export type ItemDescription = {
  item_id: string;
  availability: string;
  provenance: string;
  observed_at: string | null;
  title: string;
  url: string | null;
  description: string;
  expected: string;
  actual: string;
  reason: string;
  composed_at: string;
};

/**
 * One item's description. The id is opaque: a Linear key rides through the path
 * unchanged (encoded, never parsed). This never rejects — a tracker that is
 * down answers 200 with `availability: "unavailable"` and a reason, which is
 * what the panel renders in place of a body it cannot stand behind.
 */
export const fetchDescription = (itemId: string, f: typeof fetch = fetch) =>
  get<ItemDescription>(`/api/work/${encodeURIComponent(itemId)}/description`, f);
