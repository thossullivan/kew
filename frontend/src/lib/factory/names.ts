// Plain names for machine ids (#2541): models, reason codes, event types.
//
// DESIGN.md: a raw code is never the sentence. Each vocabulary below maps its
// codes to the words an operator reads, and every lookup falls back to the code
// itself, so an id this table has not met yet still prints, as itself, rather
// than as a blank or a guess. The caller keeps the code as secondary detail —
// a `title`, a second span — never drops it.
//
// Model names are NOT a frontend table: they are read from the model catalog
// (kew/src/kew/models.json, #2532), the same file pricing and routing read, so
// a model added there is named here without a frontend change.
import { models } from '../../../../kew/src/kew/models.json';

/** Own-property lookup: `constructor` off the wire must not find a function. */
function own(table: Record<string, string>, key: string): string | undefined {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

type CatalogRow = { id: string; display_name?: string; aliases?: readonly string[] };

/** Catalog id (and every alias of it) → its display name. */
export const MODEL_NAMES: Readonly<Record<string, string>> = Object.fromEntries(
  (models as readonly CatalogRow[]).flatMap((row) => {
    const name = (row.display_name ?? '').trim() || row.id;
    return [row.id, ...(row.aliases ?? [])].map((id) => [id, name] as const);
  })
);

/** A model id as an operator names it: "Opus 5.5" for `claude-opus-5-5`. */
export function modelName(id: string): string {
  const key = id.trim();
  return own(MODEL_NAMES, key) ?? key;
}

/**
 * The ledger's event types, worded. Every type kew writes is here — not only
 * the ones a run carries, since a writer may attach a run on some paths and not
 * others — and nothing kew never writes is. `kew/tests/test_event_label_parity.py`
 * reads the backend's audit writes off its source and fails either way, so a
 * new event type cannot ship as a raw code. A type missing here still prints as
 * the recorded code.
 */
export const EVENT_LABELS: Readonly<Record<string, string>> = {
  acceptance_amended: 'Acceptance contract amended',
  acceptance_dispute_referred: 'Acceptance dispute referred to author',
  acceptance_dispute_superseded: 'Acceptance dispute closed by a new plan',
  acceptance_disputed: 'Acceptance contract disputed',
  adversarial_review_retried: 'Adversarial review retried',
  adversarial_review_verdict: 'Adversarial review verdict',
  approval_confirmed: 'Approval confirmed',
  approval_declined: 'Approval declined',
  approval_granted: 'Approval granted',
  approval_hold: 'Held for approval',
  artifact_receipt_attested: 'Artifact receipt attested',
  artifact_receipt_sealed: 'Artifact receipt sealed',
  base_integration_skipped: 'Base integration skipped on an operator-owned branch',
  cache_reorder: 'Queue reordered for cache warmth',
  certificate_appended: 'Certificate appended',
  checkout_no_change_evidence: 'No-change evidence recorded',
  checkout_replaced: 'Checkout replaced',
  checkout_replacement_withdrawn: 'Checkout replacement withdrawn',
  ci_fix_completed: 'CI fix completed',
  circuit_breaker: 'Circuit breaker tripped',
  claim_rollback: 'Claim rolled back',
  claim_rollback_failed: 'Claim rollback failed',
  config_override_reverted: 'Config override reverted',
  config_override_set: 'Config override set',
  contained_stop_reconciled: 'Contained stop reconciled',
  container_create_retried: 'Container create retried',
  continuation_abandoned: 'Continuation abandoned',
  continuation_checkout_bound: 'Continuation checkout bound',
  continuation_claimed: 'Continuation claimed',
  continuation_detected: 'Continuation detected',
  continuation_publication: 'Continuation published',
  continuation_released: 'Continuation released',
  continuation_started: 'Continuation started',
  continuation_terminal: 'Continuation ended',
  credential_already_published: 'Credential already published',
  credential_publication_refused: 'Publication refused: credential',
  dead_run_container_removal_intended: 'Dead run container removal intended',
  dead_run_container_removed: 'Dead run container removed',
  dispatch_checkout_bound: 'Checkout bound',
  dispatch_completed: 'Dispatch completed',
  dispatch_owner_recorded: 'Dispatch owner recorded',
  dispatch_remote_launch: 'Launched on a remote host',
  dispatch_reservation_bound: 'Reservation bound',
  drive_merge_declined: 'Merge declined',
  drive_merge_rule_signed: 'Merge rule signed',
  drive_restarted: 'Drive restarted',
  drive_verify_host_unreachable: 'Verify host unreachable',
  epic_archived: 'Epic archived',
  epic_merge: 'Epic node merged',
  epic_node_host_restart: 'Host restarted under epic node',
  epic_node_retry: 'Epic node retried',
  epic_node_send_back: 'Epic node sent back by operator',
  epic_node_status: 'Epic node status changed',
  epic_node_unhold: 'Epic node released from hold',
  epic_operator_merge: 'Merged by operator',
  epic_unarchived: 'Epic unarchived',
  'evidence.artifact': 'Artifact evidence',
  'evidence.attempted_change': 'Attempted change evidence',
  'evidence.receipt_fact': 'Receipt fact',
  'evidence.runner_progress': 'Runner progress',
  'evidence.runner_usage': 'Runner usage',
  fleet_resume: 'Fleet resumed',
  fleet_stop: 'Fleet stopped',
  host_lease_lapsed: 'Host lease taken from a dead operator',
  host_lease_takeover_pending: 'Host lease takeover pending',
  host_lease_takeover_refused: 'Host lease takeover refused',
  host_lease_takeover_unconfirmed: 'Lease takeover not confirmed',
  host_stop_reconciled: 'Host stop reconciled',
  human_review_override_reverted: 'Human review override reverted',
  human_review_override_set: 'Human review override set',
  human_review_policy_reverted: 'Human review policy reverted',
  image_cve_filed: 'Image CVE issue filed',
  image_scan_failed: 'Nightly image scan failed',
  issue_queued: 'Issue queued',
  iteration_recorded: 'Loop iteration recorded',
  launch_deferred: 'Launch deferred for a running verify',
  ledger_identity_bound: 'Ledger identity bound',
  ledger_registry_rebound: 'Ledger registry rebound',
  linear_claim_completed: 'Linear claim completed',
  linear_claim_launched: 'Linear claim launched',
  linear_claim_orphaned: 'Linear claim orphaned',
  linear_claim_outcome_unknown: 'Linear claim outcome unknown',
  linear_claim_retained: 'Linear claim retained',
  linear_claim_started: 'Linear claim started',
  loop_budget_exceeded: 'Loop budget exceeded',
  loop_checkout_adopted: 'Loop checkout adopted',
  loop_checkout_bound: 'Loop checkout bound',
  loop_finished: 'Loop finished',
  loop_iteration_published: 'Loop iteration published',
  loop_paused: 'Loop paused',
  loop_reassigned: 'Loop reassigned',
  loop_resumed: 'Loop resumed',
  loop_started: 'Loop started',
  main_health_cleared: 'Red main cleared by an operator',
  main_red_tip_probed: 'Red main probed at its tip',
  main_regression_filed: 'Main regression issue filed',
  merge_claim_forced: 'Merge claim forced',
  merge_queue_added: 'Queued for merge',
  merge_queue_attention: 'Merge queue stopped: check main',
  merge_queue_notice: 'Merge queue notice',
  merge_queue_removed: 'Removed from merge queue',
  merge_queue_settled: 'Merge queue verdict',
  merge_verdict_override: 'Merge verdict overridden',
  merge_verify: 'Merge queue decision',
  mutation_report_dropped: 'Mutation report dropped',
  mutation_report_queued: 'Mutation report queued',
  mutation_reported: 'Post-merge mutation report',
  needs_human_raised: 'Raised for a human',
  never_started_run_reaped: 'Never-started run reaped',
  no_change_confirmed: 'No change confirmed',
  no_change_rejected: 'No change rejected',
  no_change_rejudged: 'No change re-judged against a new base',
  no_change_sync_retried: 'No-change sync retried',
  openshell_gateway_restart: 'OpenShell gateway restarted',
  orphan_pr_acked: 'Orphan PR acknowledged',
  orphaned_attempt_retained: 'Orphaned attempt kept',
  orphaned_attempt_terminated: 'Orphaned attempt stopped',
  orphaned_run_reaped: 'Orphaned run reaped',
  plan_recorded: 'Plan recorded',
  plan_superseded: 'Plan superseded',
  prompt_profile_activated: 'Prompt profile activated',
  prompt_profile_declined: 'Prompt profile declined',
  prompt_profile_proposed: 'Prompt profile proposed',
  prompt_profile_retired: 'Prompt profile retired',
  publication_base: 'Publication base recorded',
  publication_capacity_released: 'Publication capacity released',
  publication_capacity_reserved: 'Publication capacity reserved',
  publication_deferred: 'Publication deferred',
  publication_finalized: 'Publication finalized',
  publication_head_attested: 'Publication head attested',
  publication_held: 'Publication held',
  publication_intent: 'Publication intent recorded',
  publication_lineage: 'Publication lineage recorded',
  publication_output_scan: 'Publication output scanned',
  publication_posture: 'Publication posture recorded',
  publication_reconciled: 'Publication reconciled',
  publication_risk_confirmed: 'Publication risk confirmed',
  publication_shadow_outcome: 'Shadow publication outcome',
  repo_renamed: 'Repository renamed',
  'review.outcome': 'Review outcome',
  review_deferred_to_hand_run: 'Review deferred to hand-run review',
  review_merge_blocked: 'Merge blocked by review',
  review_residuals_accepted: 'Review residuals accepted',
  root_checkout_integrity_violation: 'Root checkout integrity violation',
  routing_decision: 'Route chosen',
  run_request_finished: 'Run request finished',
  run_request_queued: 'Run request queued',
  run_request_refused: 'Run request refused',
  run_request_started: 'Run request started',
  run_request_superseded: 'Run request superseded',
  runtime_egress_certification: 'Runtime egress certified',
  sandbox_execution_failed: 'Sandbox execution failed',
  sandbox_posture_refused: 'Sandbox posture refused',
  sandbox_resolved: 'Sandbox resolved',
  session_id_rejected: 'Session id rejected',
  'shadow.skipped': 'Shadow publication skipped',
  sidecar_isolation_evidence: 'Sidecar isolation evidence',
  sidecar_packet_certification: 'Sidecar packet certified',
  stale_container_removal_intended: 'Stale container removal intended',
  stale_container_removed: 'Stale container removed',
  standalone_review_reserved: 'Standalone review reserved',
  stop_label_reset_completed: 'Stop label reset',
  stop_label_reset_requested: 'Stop label reset requested',
  triage_decided: 'Triage decided',
  triage_resolved: 'Triage resolved',
  verification_checked: 'Verification checked',
  verify_completed: 'Merge verification completed',
  verify_flaky_filed: 'Flaky test issue filed',
  verify_ledger_failed: 'Verify ledger step failed',
  verify_red_resolved: 'Red verification resolved',
  work_action_applied: 'Work action applied',
  work_action_requested: 'Work action requested'
};

/** An event type as a sentence head; an unknown type prints as itself. */
export function eventLabel(code: string): string {
  return own(EVENT_LABELS, code.trim()) ?? code;
}

/**
 * Run kinds (`run_kind` in the ledger), worded. The serve names the kind of a
 * run it has no stage for in the stage's detail line (`service/runs.py`,
 * `_stage_detail`), so the Monitor's live-runs table printed `review_stage`
 * raw (#2847 P11). A kind missing here prints with its underscores opened up,
 * never as a snake_case code: the code is still there in its words.
 */
export const RUN_KIND_LABELS: Readonly<Record<string, string>> = {
  review_stage: 'review stage'
};

/** A run kind, worded; an unknown kind prints as itself, spaced. */
export function runKindLabel(code: string): string {
  const key = code.trim();
  return own(RUN_KIND_LABELS, key) ?? key.replace(/_/g, ' ');
}

/**
 * Reason codes: the evidence dimensions a decision is blocked by (the Flight
 * contract's `dimension` ids, `DIM_*` in `kew/flight/evidence.py`, which
 * `kew/tests/test_event_label_parity.py` pins this table to), plus `identity`,
 * which the run page derives for itself. An unknown code prints as itself.
 */
export const REASON_LABELS: Readonly<Record<string, string>> = {
  identity: 'run identity',
  host_sequence: 'audit chain',
  artifact_seal: 'artifact seal',
  artifact_diff: 'sealed change set',
  diff_line_counts: 'change line counts',
  runner_progress: 'runner progress',
  runner_usage: 'runner usage'
};

/** A reason code, worded; an unknown code prints as itself. */
export function reasonLabel(code: string): string {
  return own(REASON_LABELS, code.trim()) ?? code;
}

/**
 * An epic driver's signal (#2844 P8): the tick's verdict, `TerminalSignal` in
 * `kew/graph/driver/result.py`. The Epics list printed `awaiting_merges` as
 * itself. A signal this table has not met prints with its underscores read as
 * spaces — a word, never a snake_case code — and the caller keeps the code as
 * the cell's `title`.
 */
export const SIGNAL_LABELS: Readonly<Record<string, string>> = {
  progressing: 'progressing',
  awaiting_merges: 'awaiting merges',
  stalled: 'stalled',
  complete: 'complete'
};

/** A driver signal, worded; a missing one reads "unknown". */
export function signalLabel(code: string | null | undefined): string {
  const key = (code ?? '').trim();
  if (key === '') return 'unknown';
  return own(SIGNAL_LABELS, key) ?? key.replace(/_+/g, ' ');
}

/**
 * Recorded values, worded (#2896 R5): the from/to of a status move and the one
 * fact an event row carries — a verdict, a status, a reason. The Monitor's
 * stream printed `in_review → adversarial_review` and `base_integration_failed`
 * as recorded. A value this table has not met prints with its underscores read
 * as spaces, never as a snake_case code, and the caller keeps the code as the
 * row's `title`.
 */
export const VALUE_LABELS: Readonly<Record<string, string>> = {
  NEEDS_CHANGES: 'needs changes',
  in_progress: 'in progress',
  in_review: 'in review'
};

/** A recorded value, worded; an unknown one prints as itself, spaced. */
export function valueLabel(code: string): string {
  const key = code.trim();
  return own(VALUE_LABELS, key) ?? key.replace(/_+/g, ' ');
}

/**
 * A failed run's error category (`error_category` in the ledger), worded. The
 * Runs page printed `container_start_failed` as itself (#2973), and Work's
 * merge verify `host_job_deadline_exceeded` (#3466). A category this table has
 * not met prints with its underscores read as spaces, and the caller keeps the
 * code as secondary detail. The table is every category the backend writes as
 * a literal: `kew/tests/test_event_label_parity.py` fails on one it lacks.
 */
export const ERROR_CATEGORY_LABELS: Readonly<Record<string, string>> = {
  admission_hook_refused: 'Refused by a post-admission hook',
  admission_refused: 'Admission refused',
  approval_declined: 'Approval declined',
  approval_hold: 'Held for approval',
  base_integration_exhausted: 'Base integration retries exhausted',
  base_integration_step: 'Base integration step failed',
  budget_exceeded: 'Budget exceeded',
  budget_refused: 'Refused: budget',
  budget_stop: 'Stopped at budget',
  budget_stopped: 'Stopped at budget',
  checkout_identity_changed: 'Checkout changed before launch',
  circuit_breaker: 'Circuit breaker open',
  claim_rollback_failed: 'Claim rollback failed',
  container_start_failed: 'Container start failed',
  containment_refused: 'Containment refused',
  cost_anomaly_flagged: 'Cost anomaly flagged',
  credential_issuer_refused: 'Credential issuer refused',
  credential_lifetime_refused: 'Refused: credential lifetime',
  credential_missing: 'Credential missing',
  credential_publication_refused: 'Publication refused: credential evidence',
  delegation_item_gone: 'Delegated item gone',
  dispatch_child_died: 'Dispatch process died',
  dispatch_child_refused: 'Dispatch process refused',
  exception: 'Unexpected error',
  failed: 'Failed',
  fleet_stopped: 'Fleet stopped',
  github_auth: 'GitHub authentication failed',
  github_unreachable: 'GitHub unreachable',
  host_connection_lost: 'The connection to the verify host was lost',
  host_interrupted: 'The verify host was interrupted',
  host_job_deadline_exceeded: 'The verify host hit its job deadline',
  host_restart_reaped: 'Reaped after a host restart',
  host_restart_repeated: 'Host restarted repeatedly',
  killed: 'Killed',
  launch_failed: 'Launch failed',
  malformed_issue: 'Malformed issue',
  needs_human: 'Needs a person',
  orphaned_pending_preserved: 'Orphaned run kept',
  orphaned_pending_reaped: 'Orphaned run reaped',
  other: 'Other',
  permanent: 'Permanent failure',
  prompt_configuration_refused: 'Prompt configuration refused',
  publication_configuration_refused: 'Publication configuration refused',
  publication_failed: 'Publication failed',
  publication_held: 'Publication held',
  publication_rate_limited: 'Publication rate limited',
  publication_review_required: 'Publication needs review',
  publication_unavailable: 'Publication unavailable',
  quota_exhausted: 'Quota exhausted',
  rate_limited: 'Rate limited',
  readiness_probe_abandoned: 'Readiness probe abandoned',
  readiness_probe_failed: 'Readiness probe failed',
  remote_host_artifact_refused: 'Remote host refused the artifact',
  remote_host_failed: 'Remote host failed',
  remote_host_interrupted: 'Remote host interrupted',
  remote_host_run_kind_refused: 'Remote host refused the run kind',
  requeued_over_cap: 'Requeued over the cap',
  rescued_incomplete: 'Incomplete work rescued',
  reservation_replaced: 'Reservation replaced',
  resource: 'Out of resources',
  review_budget_refused: 'Review refused: budget',
  review_failed_no_verdict: 'Review failed without a verdict',
  review_host_failed: 'Review host failed',
  review_multiple_results: 'Review returned more than one result',
  review_out_of_band: 'Review ran out of band',
  review_stage_finished: 'Review stage finished',
  review_stage_never_started: 'Review stage never started',
  review_stage_raised: 'Review stage raised an error',
  review_stage_spawn_failed: 'Review stage failed to start',
  review_stream_failed: 'Review stream failed',
  runner_preflight_refused: 'Runner preflight refused',
  runtime_timeout: 'Runtime timed out',
  sandbox_launch_error: 'Sandbox launch failed',
  sandbox_no_change_needed: 'No change needed',
  sandbox_push_failed: 'Sandbox push failed',
  sandbox_realized_checkout_refused: 'Sandbox checkout refused',
  sandbox_refused: 'Sandbox refused',
  sandbox_runtime_missing: 'Sandbox runtime missing',
  sandbox_startup_refused: 'Agent never started',
  sandbox_startup_unknown: 'Agent startup unknown',
  sandbox_stream_error: 'Sandbox stream failed',
  sidecar_lifecycle_error: 'Sidecar lifecycle failed',
  stale_pending: 'Stale pending run',
  stream_silent: 'Agent output went silent',
  succeeded: 'Succeeded',
  transient: 'Transient failure',
  transport_admission_refused: 'Transport admission refused',
  transport_unavailable: 'Transport unavailable',
  verify_host_environment_broken: 'The verify host environment is broken',
  watchdog_kill: 'Killed by the watchdog'
};

/** An error category, worded; an unknown one prints as itself, spaced. */
export function errorCategoryLabel(code: string): string {
  const key = code.trim();
  return own(ERROR_CATEGORY_LABELS, key) ?? key.replace(/_+/g, ' ');
}

/**
 * Refusal, hold and reap reasons kew records as codes, worded (#2896). A
 * `reason` is worded only from this list: the field is also where free text
 * lands, and an identifier-shaped reason this list has not met may be a name
 * rather than a code, so it prints exactly as recorded. The list is every
 * reason code the backend writes as a literal, so a live event row reads in
 * words; `kew/tests/test_event_label_parity.py` fails on a code it lacks
 * (#2953).
 */
export const REFUSAL_REASON_LABELS: Readonly<Record<string, string>> = {
  adaptive_admission_context_missing: 'adaptive route admission context missing',
  adaptive_route_requires_expected_version: 'adaptive route requires an expected version',
  adaptive_route_requires_immutable_config: 'adaptive route requires an immutable config',
  adaptive_route_requires_managed_switchyard: 'adaptive route requires a managed Switchyard',
  adaptive_route_requires_pinned_binary: 'adaptive route requires a pinned binary',
  adaptive_route_requires_pinned_binary_digest: 'adaptive route requires a pinned binary digest',
  adaptive_route_requires_reviewed_credentials: 'adaptive route requires reviewed credentials',
  adaptive_route_requires_switchyard: 'adaptive route requires Switchyard',
  adaptive_routing_disabled: 'adaptive routing disabled',
  admission_owner_dead: 'admission owner dead',
  approval_declined: 'approval declined',
  approval_hold: 'held for approval',
  auto_continue_child_failed: 'auto-continue child failed',
  auto_continue_spawn_failed: 'auto-continue spawn failed',
  awaiting_continuation: 'awaiting continuation',
  awaiting_no_change: 'awaiting no-change confirmation',
  awaiting_publication: 'awaiting publication',
  base_integration_failed: 'base integration failed',
  baseline_unavailable: 'baseline unavailable',
  broker_provider_missing: 'broker provider missing',
  budget_exceeded: 'budget exceeded',
  cancelled: 'cancelled',
  canonical_publication: 'canonical publication',
  checkout_over_quota: 'checkout over quota',
  ci_pending: 'CI pending',
  claim_evidence_not_recorded: 'claim evidence not recorded',
  claim_released: 'claim released',
  claimed_owner_unreadable: 'claimed owner unreadable',
  classifier_missing_selection: 'classifier missing selection',
  classifier_unavailable: 'classifier unavailable',
  contained_auth_evidence_missing: 'contained auth evidence missing',
  contained_profile_missing: 'contained profile missing',
  contained_profile_stale: 'contained profile stale',
  containment_refused: 'containment refused',
  continuation_host_only: 'continuation host only',
  counter_reset: 'counter reset',
  credential_already_published: 'credential already published',
  credential_in_output: 'credential in output',
  credential_issuer_refused: 'credential issuer refused',
  credential_missing: 'credential missing',
  credential_scan_missing: 'credential scan missing',
  credential_scan_oversize: 'credential scan oversize',
  credential_scan_timeout: 'credential scan timeout',
  credential_scan_unavailable: 'credential scan unavailable',
  dispatch_exception: 'dispatch exception',
  exception: 'unexpected error',
  exclusive_use_not_proven: 'exclusive use not proven',
  exited: 'exited',
  expired: 'expired',
  final_unavailable: 'final reading unavailable',
  fleet_stopped: 'fleet stopped',
  github_auth: 'GitHub authentication failed',
  github_unreachable: 'GitHub unreachable',
  governor_error: 'governor error',
  held: 'held',
  host_free_space: 'host free space low',
  host_restart: 'host restart',
  image_unmapped: 'image unmapped',
  in_review: 'in review',
  invalid_endpoint: 'invalid endpoint',
  issued_key_evidence_missing: 'issued key evidence missing',
  issued_key_unrevoked: 'issued key unrevoked',
  key_shape_overflow: 'key shape overflow',
  key_shape_settle_overflow: 'key shape settle overflow',
  main_unverified: 'main unverified',
  malformed_metrics: 'malformed metrics',
  merged_unclosed: 'merged unclosed',
  metrics_unavailable: 'metrics unavailable',
  model_override: 'model override',
  model_pinned: 'model pinned',
  no_reading: 'no reading',
  no_route: 'no route',
  no_routing_config: 'no routing config',
  no_warm_alternatives: 'no warm alternatives',
  none: 'none',
  not_probed: 'not probed',
  openshell_gateway_restart: 'OpenShell gateway restart',
  override_resolve_failed: 'override route could not be resolved',
  packet_leak: 'packet leak',
  passthrough: 'passthrough',
  pool_submit_failed: 'pool submit failed',
  positive_control_failed: 'positive control failed',
  positive_control_unavailable: 'positive control unavailable',
  posture_not_recorded: 'posture not recorded',
  process_not_live: 'process not live',
  prompt_configuration_refused: 'prompt configuration refused',
  proposed_already_cheapest: 'proposed already cheapest',
  provider_no_cache_benefit: 'provider no cache benefit',
  proxy_counter_reset: 'proxy counter reset',
  publication_ancestor_evidence_missing: 'publication ancestor evidence missing',
  publication_configuration_refused: 'publication configuration refused',
  publication_evidence_unavailable: 'publication evidence unavailable',
  publication_head_stale: 'publication head stale',
  publication_lineage_ambiguous: 'publication lineage ambiguous',
  publication_lineage_cycle: 'publication lineage cycle',
  publication_lineage_too_deep: 'publication lineage too deep',
  ready: 'ready',
  request_count_mismatch: 'request count mismatch',
  reservation_replaced: 'reservation replaced',
  response_too_large: 'response too large',
  retention: 'retention window passed',
  review_budget: 'review budget',
  review_crash: 'review crash',
  review_pr_closed: 'review PR closed',
  review_residuals_unfiled: 'review residuals unfiled',
  review_rounds_exhausted: 'review rounds exhausted',
  review_unparseable: 'review unparseable',
  route_response_malformed: 'route response malformed',
  runner_preflight_refused: 'runner preflight refused',
  runtime_unavailable: 'runtime unavailable',
  savings_below_threshold: 'savings below threshold',
  sidecar_cleanup_evidence_missing: 'sidecar cleanup evidence missing',
  sidecar_filter_evidence_missing: 'sidecar filter evidence missing',
  sidecar_identity_stale: 'sidecar identity stale',
  sidecar_packet_evidence_missing: 'sidecar packet evidence missing',
  stalled: 'stalled',
  tls_error: 'TLS error',
  transport_admission_refused: 'transport admission refused',
  unauthorized: 'unauthorized',
  unavailable: 'unavailable',
  unknown: 'unknown',
  unparseable: 'unparseable',
  unreachable: 'unreachable',
  unrelated_traffic_detected: 'unrelated traffic detected',
  unspecified: 'unspecified',
  warm_stronger_beats_cold: 'warm stronger beats cold',
  worker_cap_reached: 'worker cap reached',
  worker_probe_unavailable: 'worker probe unavailable'
};

/** A recorded reason, worded when it is a known code; `null` when it is not. */
export function refusalReasonLabel(code: string): string | null {
  return own(REFUSAL_REASON_LABELS, code.trim()) ?? null;
}

/** One run of a title: plain words, or a backtick span drawn in mono. */
export type TitleSpan = { text: string; mono: boolean };

/**
 * A title's markdown code spans, as runs (#2848, P13). Issue titles carry
 * backticks — "`kew drive install` as a service" — and Work, the epic page and
 * the palette printed them. A span opens on a run of backticks and closes on
 * the next run of the SAME length, as CommonMark has it, so ``a`b`` keeps its
 * inner backtick; a run that closes nothing is markdown noise and is dropped,
 * so no title prints the character. Every surface draws a title through this
 * (`ui/Title.svelte`) and names it in plain text with `plainTitle`.
 */
export function titleSpans(title: string): TitleSpan[] {
  const spans: TitleSpan[] = [];
  const push = (text: string, mono: boolean) => {
    if (text === '') return;
    const last = spans[spans.length - 1];
    if (last && last.mono === mono && !mono) last.text += text;
    else spans.push({ text, mono });
  };
  const runs = /`+/g;
  let at = 0;
  let open: RegExpExecArray | null;
  while ((open = runs.exec(title)) !== null) {
    push(title.slice(at, open.index), false);
    at = runs.lastIndex;
    let close: RegExpExecArray | null;
    while ((close = runs.exec(title)) !== null && close[0].length !== open[0].length);
    if (close === null) {
      // Nothing closes this run: drop it and read on from just after it.
      runs.lastIndex = at;
      continue;
    }
    push(title.slice(at, close.index), true);
    at = runs.lastIndex;
  }
  push(title.slice(at), false);
  return spans;
}

/** A title as plain text: its code spans' words, no backticks. */
export function plainTitle(title: string): string {
  return titleSpans(title)
    .map((span) => span.text)
    .join('');
}
