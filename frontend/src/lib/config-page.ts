// Page model for the editable /config console (#659, #713 Group 2). Pure and
// vitest-covered: section grouping, source-badge derivation, request building
// (the routing {runner, model} dict shape is enforced here, not in the form),
// the classified write transport, and the response-driven row transitions all
// live here so `config/+page.svelte` stays a renderer.
//
// It consumes GET /api/config/overridable (the four-layer resolution) and the
// existing gated POST/DELETE /api/config/{field} write endpoints. Rows always
// transition from the WRITE RESPONSE (`{field, effective}`), never a lagging
// read-back.

import type { ConfigField, ConfigOverride, OverridableMap } from './api';

// --- Field metadata ---------------------------------------------------------
// Drives grouping + display. Display sections regroup the raw config sections:
// the agent budget knob sits with Budget; agent.model joins routing under
// "Model & Routing". Rows render in this order; fields absent from the API
// payload are skipped, and API fields with no metadata entry are ignored
// (additivity — the endpoint may grow fields without a client bump).

// `model` and `route` rows pick their model from the catalog (#2542).
export type FieldKind = 'number' | 'text' | 'model' | 'route';

export type FieldMeta = {
  field: string;
  section: string;
  label: string;
  hint: string;
  kind: FieldKind;
  /** A `model` row that may pick only this runner's routable models (#2741). */
  runner?: string;
  /** What applies when the field is unset, said after `unset · ` (D42). */
  unset?: string;
  /** A dollar figure: printed as `$25`, in the money colour (D42). */
  money?: boolean;
};

export const SECTION_ORDER = ['Budget', 'Model & Routing', 'Breaker'] as const;

export const FIELD_META: readonly FieldMeta[] = [
  {
    field: 'budget.per_issue_usd',
    section: 'Budget',
    label: 'Per issue',
    hint: 'Max spend per dispatched issue',
    kind: 'number',
    money: true,
    unset: 'the built-in cap applies'
  },
  {
    field: 'budget.daily_usd',
    section: 'Budget',
    label: 'Daily',
    hint: 'Max spend across all runs today (UTC)',
    kind: 'number',
    money: true,
    unset: 'the built-in cap applies'
  },
  {
    field: 'budget.weekly_usd',
    section: 'Budget',
    label: 'Weekly',
    hint: 'Max spend across all runs today (UTC) and the six days before it',
    kind: 'number',
    money: true,
    unset: 'the built-in cap applies'
  },
  {
    field: 'agent.max_budget_usd',
    section: 'Budget',
    label: 'Per run (agent)',
    hint: 'Hard cap handed to each individual agent run',
    kind: 'number',
    money: true,
    unset: 'the built-in cap applies'
  },
  {
    field: 'agent.model',
    section: 'Model & Routing',
    label: 'Agent model',
    hint: 'Global model override applied to every run',
    kind: 'model',
    unset: "each route's model applies"
  },
  {
    // The reviewer runs on claude-code only (`[review] runner`, #2021), and the
    // server refuses any other runner's model for it, so the picker offers
    // none (#2741).
    field: 'review.model',
    section: 'Model & Routing',
    label: 'Reviewer model',
    hint: 'Model the adversarial reviewer runs on (unset: the agent model)',
    kind: 'model',
    runner: 'claude-code',
    unset: 'uses the agent model'
  },
  {
    field: 'routing.default',
    section: 'Model & Routing',
    label: 'Default route',
    hint: 'Fallback runner + model when nothing more specific matches',
    kind: 'route',
    unset: "the runner's own model applies"
  },
  {
    field: 'routing.simple',
    section: 'Model & Routing',
    label: 'Simple route',
    hint: 'Route for simple-complexity issues',
    kind: 'route',
    unset: 'uses the default route'
  },
  {
    field: 'routing.medium',
    section: 'Model & Routing',
    label: 'Medium route',
    hint: 'Route for medium-complexity issues',
    kind: 'route',
    unset: 'uses the default route'
  },
  {
    field: 'routing.complex',
    section: 'Model & Routing',
    label: 'Complex route',
    hint: 'Route for complex-complexity issues',
    kind: 'route',
    unset: 'uses the default route'
  },
  {
    field: 'breaker.cost_anomaly_factor',
    section: 'Breaker',
    label: 'Cost anomaly factor',
    hint: 'Trip the breaker at N× expected spend (must be ≥ 1.0)',
    kind: 'number',
    unset: 'the built-in factor applies'
  }
];

const META_BY_FIELD: Record<string, FieldMeta> = Object.fromEntries(
  FIELD_META.map((m) => [m.field, m])
);

// --- Route value shape ------------------------------------------------------

export type RouteValue = { runner: string; model: string };

export function isRoutingField(field: string): boolean {
  return field.startsWith('routing.');
}

export function isRouteValue(v: unknown): v is RouteValue {
  // Both properties, both strings — a partial object must never read as a
  // route (or worse, be POSTed as one).
  return (
    typeof v === 'object' &&
    v !== null &&
    'runner' in v &&
    'model' in v &&
    typeof (v as Record<string, unknown>).runner === 'string' &&
    typeof (v as Record<string, unknown>).model === 'string'
  );
}

// Structural equality good enough for the payload's scalar + {runner, model}
// values — used to tell which layer produced the effective value.
function sameValue(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (isRouteValue(a) && isRouteValue(b)) {
    return a.runner === b.runner && a.model === b.model;
  }
  return false;
}

// --- Source badge derivation ------------------------------------------------
// Precedence for the effective value is env > override > kew.toml > default.
// The payload bakes env into `file_value` (no standalone env value), so a plain
// env-set field is indistinguishable from a kew.toml one and reads as kew.toml.
// The one env case we CAN detect is env shadowing an override: an override is
// present but the effective value doesn't match it — surfaced as `env-hint`.

export type Source = 'override' | 'env-hint' | 'kew.toml' | 'default';

export function deriveSource(f: ConfigField): Source {
  if (f.override !== null) {
    return sameValue(f.effective, f.override.value) ? 'override' : 'env-hint';
  }
  return sameValue(f.file_value, f.default_value) ? 'default' : 'kew.toml';
}

export type BadgeTone = 'neutral' | 'accent' | 'warn' | 'info';

export type SourceBadge = { label: string; tone: BadgeTone };

const BADGES: Record<Source, SourceBadge> = {
  override: { label: 'override', tone: 'warn' },
  'env-hint': { label: 'env', tone: 'info' },
  'kew.toml': { label: 'kew.toml', tone: 'accent' },
  default: { label: 'default', tone: 'neutral' }
};

export function sourceBadge(s: Source): SourceBadge {
  return BADGES[s];
}

/**
 * FUI-28 — the colour the factory says a source badge's tone in.
 *
 * The restyle dropped the pill: a boxed badge is the card grammar, and on the
 * ink ground the layer in force reads better as its own word beside the value.
 * The tone still decides the colour, so the four layers stay as distinct as
 * `sourceBadge` made them — `--warn` for an override in force (the one layer a
 * person put there), `--accent-2` for an env hint, `--text-2` for the committed
 * file and `--text-3` for a value nobody has moved.
 */
export const SOURCE_TONE_CLASS: Record<BadgeTone, string> = {
  warn: 'text-warn',
  info: 'text-accent-2',
  accent: 'text-text-2',
  neutral: 'text-text-3'
};

// --- Value formatting -------------------------------------------------------

/** True when a value says nothing is set: null, empty, or a route with neither half. */
export function isUnset(kind: FieldKind, value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (kind === 'route') return !isRouteValue(value) || (value.runner === '' && value.model === '');
  return String(value) === '';
}

/** A dollar figure as money: `$25`, `$1,500`, `$2.50`. */
export function formatMoney(value: number): string {
  const digits = Number.isInteger(value) ? 0 : 2;
  return '$' + value.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/**
 * A value as the page prints it. An unset value says so in words (D42) — the
 * row's own `unset` sentence when it has one, after `unset · ` — never a bare
 * `—`, which reads as a failed read. A budget cap prints as money.
 */
export function formatValue(kind: FieldKind, value: unknown, field?: string): string {
  const meta = field === undefined ? undefined : META_BY_FIELD[field];
  if (isUnset(kind, value)) return meta?.unset ? `unset · ${meta.unset}` : 'unset';
  if (kind === 'route') {
    const route = value as RouteValue;
    const runner = route.runner || 'default runner';
    return route.model ? `${runner} · ${route.model}` : runner;
  }
  if (kind === 'number' && typeof value === 'number') {
    if (meta?.money) return formatMoney(value);
    return Number.isInteger(value) ? String(value) : value.toFixed(2);
  }
  return String(value);
}

/** Whether a row's value is a dollar figure, drawn in the money colour. */
export function isMoneyField(field: string): boolean {
  return META_BY_FIELD[field]?.money === true;
}

// --- Grouping into rendered sections ----------------------------------------

export type FieldRow = {
  field: string;
  label: string;
  hint: string;
  kind: FieldKind;
  /** See `FieldMeta.runner`. */
  runner?: string;
  resolution: ConfigField;
  source: Source;
};

export type ConfigSection = { title: string; rows: FieldRow[] };

function findField(map: OverridableMap, field: string): ConfigField | undefined {
  const section = field.split('.', 1)[0];
  return map[section]?.[field];
}

export function buildRow(field: string, resolution: ConfigField): FieldRow {
  const meta = META_BY_FIELD[field];
  return {
    field,
    label: meta?.label ?? field,
    hint: meta?.hint ?? '',
    kind: meta?.kind ?? 'text',
    ...(meta?.runner ? { runner: meta.runner } : {}),
    resolution,
    source: deriveSource(resolution)
  };
}

export function buildSections(map: OverridableMap): ConfigSection[] {
  return SECTION_ORDER.map((title) => {
    const rows = FIELD_META.filter((m) => m.section === title)
      .map((m) => {
        const resolution = findField(map, m.field);
        return resolution ? buildRow(m.field, resolution) : null;
      })
      .filter((r): r is FieldRow => r !== null);
    return { title, rows };
  }).filter((section) => section.rows.length > 0);
}

// --- Request building -------------------------------------------------------
// buildOverrideValue turns raw form input into the JSON value the endpoint
// expects, enforcing the routing dict shape (a bare string is the runner-name
// footgun the backend also rejects — guard it here so the form can't send it).

export function buildRouteValue(runner: string, model: string): RouteValue {
  const r = runner.trim();
  const m = model.trim();
  if (r === '' && m === '') {
    throw new Error('A routing override needs a runner, a model, or both.');
  }
  return { runner: r, model: m };
}

export function parseNumeric(raw: string | number): number {
  // A `bind:value` on <input type="number"> yields a JS number at runtime
  // even where the dialog type says string — .trim() on it threw
  // "e.trim is not a function" on every numeric override (live 2026-07-19).
  const s = String(raw).trim();
  const n = Number(s);
  if (s === '' || Number.isNaN(n)) {
    throw new Error('Enter a number.');
  }
  return n;
}

export type WriteBody = {
  value: unknown;
  reason: string;
  // Policy-only (I13C): the API refuses either on an ordinary override, so
  // they are present ONLY on a human_review write that supplied them.
  expires_at?: string;
  if_generation?: number;
};

export type WriteRequest = {
  path: string;
  method: 'GET' | 'POST' | 'DELETE';
  body?: WriteBody;
};

export function buildSetRequest(field: string, value: unknown, reason = ''): WriteRequest {
  if (isRoutingField(field)) {
    if (typeof value === 'string') {
      throw new Error(
        `${field}: a bare string is the runner-name footgun — pass a {runner, model} object.`
      );
    }
    if (!isRouteValue(value)) {
      throw new Error(`${field}: routing overrides need a {runner, model} object.`);
    }
  }
  return { path: `/api/config/${field}`, method: 'POST', body: { value, reason } };
}

export function buildRevertRequest(field: string): WriteRequest {
  return { path: `/api/config/${field}`, method: 'DELETE' };
}

// --- Classified write transport (mirrors verbs.ts) --------------------------

export type WriteErrorKind =
  | 'not_approver' // HTTP 403 VerbDenied — actor not in [approval].approvers
  | 'unauthenticated' // HTTP 401 — no / expired session
  | 'bad_value' // HTTP 400 — validation rejected the value
  | 'not_found' // HTTP 404 — no override to revert
  | 'stale_generation' // HTTP 409 — the policy moved under an edit (I13C CAS)
  | 'server' // any other non-2xx
  | 'network'; // fetch itself threw

export type WriteState = { field: string; effective: unknown };

export type WriteError = {
  ok: false;
  kind: WriteErrorKind;
  status: number;
  message: string;
};

/** Success carries the endpoint's own payload; failure is classified. */
export type Result<T> = { ok: true; data: T } | WriteError;

export type WriteResult = Result<WriteState>;

function classifyStatus(status: number): WriteErrorKind {
  if (status === 400) return 'bad_value';
  if (status === 401) return 'unauthenticated';
  if (status === 403) return 'not_approver';
  if (status === 404) return 'not_found';
  if (status === 409) return 'stale_generation';
  return 'server';
}

// The 403 explanation is the friendly not-an-approver line rendered inline on
// the row; other kinds prefer the server's own `detail` when present.
function messageFor(kind: WriteErrorKind, detail: string): string {
  switch (kind) {
    case 'not_approver':
      return "You're not an approver — config writes are reserved for [approval].approvers.";
    case 'unauthenticated':
      return 'Sign in to change config.';
    case 'network':
      return 'Network error — check the connection and retry.';
    case 'stale_generation':
      // The server names both generations; that beats anything we could say.
      return detail || 'The policy changed while you were editing — reload and retry.';
    default:
      return detail || 'The server rejected this change.';
  }
}

function csrfToken(): string {
  const m = /(?:^|;\s*)kew_csrf=([^;]+)/.exec(
    typeof document === 'undefined' ? '' : document.cookie
  );
  return m ? decodeURIComponent(m[1]) : '';
}

async function detailOf(r: Response): Promise<string> {
  try {
    const body = (await r.json()) as { detail?: unknown };
    if (body && typeof body.detail === 'string') return body.detail;
  } catch {
    // Non-JSON / empty body — fall through to the kind's default message.
  }
  return '';
}

async function send<T = WriteState>(req: WriteRequest, f: typeof fetch): Promise<Result<T>> {
  const headers: Record<string, string> = { 'X-Kew-CSRF': csrfToken() };
  if (req.body !== undefined) headers['Content-Type'] = 'application/json';
  let r: Response;
  try {
    r = await f(req.path, {
      method: req.method,
      headers,
      ...(req.body !== undefined ? { body: JSON.stringify(req.body) } : {})
    });
  } catch (e) {
    return { ok: false, kind: 'network', status: 0, message: (e as Error).message };
  }
  if (!r.ok) {
    const kind = classifyStatus(r.status);
    return { ok: false, kind, status: r.status, message: messageFor(kind, await detailOf(r)) };
  }
  return { ok: true, data: (await r.json()) as T };
}

/** Set an override. Rejects a bad routing shape client-side (never fires). */
export function setOverride(
  field: string,
  value: unknown,
  reason: string,
  f: typeof fetch = fetch
): Promise<WriteResult> {
  let req: WriteRequest;
  try {
    req = buildSetRequest(field, value, reason);
  } catch (e) {
    return Promise.resolve({ ok: false, kind: 'bad_value', status: 0, message: (e as Error).message });
  }
  return send(req, f);
}

/** Revert an override, restoring the file/default value. */
export function revertOverride(field: string, f: typeof fetch = fetch): Promise<WriteResult> {
  return send(buildRevertRequest(field), f);
}

// --- Response-driven row transitions ----------------------------------------
// The write response is only `{field, effective}`; we synthesize the override
// provenance from what the client already knows (the submitted reason, the
// signed-in actor, the moment of the write) so the row can show who/when/why
// without a follow-up read.

export function applyOverride(
  row: FieldRow,
  effective: unknown,
  opts: { value?: unknown; reason?: string; setBy?: string; setAt?: string } = {}
): FieldRow {
  const override: ConfigOverride = {
    // The SUBMITTED value, not the resolved effective one — when an env var
    // shadows the field, they differ, and deriveSource needs both to keep
    // labeling the row `env-hint` instead of `override`.
    value: 'value' in opts ? opts.value : effective,
    reason: opts.reason ?? '',
    set_by: opts.setBy ?? 'you',
    set_at: opts.setAt ?? ''
  };
  const resolution: ConfigField = { ...row.resolution, effective, override };
  return { ...row, resolution, source: deriveSource(resolution) };
}

export function applyRevert(row: FieldRow, effective: unknown): FieldRow {
  const resolution: ConfigField = { ...row.resolution, effective, override: null };
  return { ...row, resolution, source: deriveSource(resolution) };
}

// --- Human-review policy projection (I13W) ----------------------------------
// GET/POST/DELETE on the human_review fields return the WHOLE policy state as
// `kew.service.config.ReviewPolicyState.as_dict()` renders it: the effective
// values, the monotonic generation, the optional UTC expiry, the pending
// reversion and the over-cap bit. Everything below PROJECTS that contract.
//
// There is deliberately no client-side policy model. The client never decides
// whether an expiry has passed (an expired override is still the override in
// force until a *writer* durably reverts it — a read is not an effect), and it
// never recomputes over-cap from pending vs. limit: capacity is scoped to the
// canonical delivery repository and rendered by the service. Deriving either
// here would let the console disagree with the policy the publication path
// actually enforces.
//
// Three authorities meet on this page and stay distinct: spend approval and
// policy-change authority are the `[approval].approvers` gate every write
// here passes, while terminal judgment — releasing one publication hold — is
// `[human_review].approvers`, which this surface reports and never edits.

export const REVIEW_SECTION = 'Human review';

export const REVIEW_POLICY_PATH = '/api/config/human_review';

/** What an expiry will restore, exactly as the service computed it. */
export type ReviewReversion = {
  at: string;
  reason: string;
  to: Record<string, number>; // thresholds plus the `generation` it lands on
};

export type ReviewPolicy = {
  delivery_repo: string;
  values: Record<string, number>;
  generation: number;
  expires_at: string | null;
  source: string; // the server's word: "system" | "override"
  approvers: string[];
  pending_reviews: number;
  over_cap: boolean;
  pending_reversion: ReviewReversion | null;
};

/** A policy write echoes the edited field alongside the whole new state. */
export type ReviewWriteState = ReviewPolicy & { field: string; effective: unknown };

export const REVIEW_FIELD_META: readonly FieldMeta[] = [
  {
    field: 'human_review.max_pending_reviews',
    section: REVIEW_SECTION,
    label: 'Pending reviews',
    hint: 'Publications that may await a person at once, per delivery repo',
    kind: 'number'
  },
  {
    field: 'human_review.max_diff_lines',
    section: REVIEW_SECTION,
    label: 'Changed lines',
    hint: 'Additions plus deletions; exactly this many still publish unattended',
    kind: 'number'
  },
  {
    field: 'human_review.max_paths',
    section: REVIEW_SECTION,
    label: 'Changed paths',
    hint: 'Distinct paths touched; exactly this many still publish unattended',
    kind: 'number'
  }
];

const REVIEW_FIELDS: readonly string[] = REVIEW_FIELD_META.map((m) => m.field);

export type ReviewRow = {
  field: string;
  label: string;
  hint: string;
  value: number | null;
  display: string;
};

/** One row per threshold, valued only from `policy.values`. */
export function buildReviewRows(policy: ReviewPolicy): ReviewRow[] {
  return REVIEW_FIELD_META.map((m) => {
    // A threshold the payload omits reads as unknown, never as a client-side
    // default — a wrong number here would read as policy truth.
    const raw = policy.values?.[m.field];
    const value = typeof raw === 'number' ? raw : null;
    return {
      field: m.field,
      label: m.label,
      hint: m.hint,
      value,
      // Omitted is unknown, not unset: say the read is missing (D42).
      display: value === null ? 'not read' : formatValue('number', value)
    };
  });
}

const REVIEW_BADGES: Record<string, SourceBadge> = {
  override: { label: 'override', tone: 'warn' },
  system: { label: 'system', tone: 'accent' }
};

/** Badge the source word the service sent — unrecognized words pass through. */
export function reviewSourceBadge(policy: ReviewPolicy): SourceBadge {
  return REVIEW_BADGES[policy.source] ?? { label: policy.source || 'system', tone: 'neutral' };
}

/** The edit in force: a policy has a generation, not just a layer. */
export function generationNote(policy: ReviewPolicy): string {
  return policy.expires_at === null
    ? `generation ${policy.generation} · no expiry`
    : `generation ${policy.generation} · expires ${policy.expires_at}`;
}

/** Over-cap is a rendering of the server's bit, never an eviction. */
export function capacityNote(policy: ReviewPolicy): string | null {
  if (!policy.over_cap) return null;
  const raw = policy.values?.['human_review.max_pending_reviews'];
  const limit = typeof raw === 'number' ? `the limit of ${formatValue('number', raw)}` : 'the limit';
  return (
    `${policy.pending_reviews} publications are awaiting a person, over ${limit}. ` +
    'Nothing was evicted — every hold stays where it is, and no new hold may be taken ' +
    'until occupancy falls back under the limit.'
  );
}

/** The reversion the service computed; absent when nothing is pending. */
export function reversionNote(policy: ReviewPolicy): string | null {
  const r = policy.pending_reversion;
  if (!r) return null;
  return `Reverts to the system policy at ${r.at} (${r.reason}), as generation ${r.to?.generation}.`;
}

/** Terminal judgment — a different authority from the gate on these writes. */
export function approverNote(policy: ReviewPolicy): string {
  return policy.approvers.length === 0
    ? 'Terminal judgment (releasing one hold) falls back to [approval].approvers.'
    : `Terminal judgment (releasing one hold): ${policy.approvers.join(', ')}.`;
}

export type ReviewSetOptions = {
  /** ISO-8601 with an explicit UTC offset. Blank/absent means no expiry. */
  expiresAt?: string | null;
  /** Compare-and-swap: the generation the surface was showing. */
  ifGeneration?: number | null;
};

export function buildReviewSetRequest(
  field: string,
  value: unknown,
  reason = '',
  opts: ReviewSetOptions = {}
): WriteRequest {
  if (!REVIEW_FIELDS.includes(field)) {
    throw new Error(`${field}: not one of the human_review thresholds.`);
  }
  const body: WriteBody = { value, reason };
  // Whether an expiry is usable is the service's call (it refuses one already
  // past); the client only declines to send an empty box.
  const expires = (opts.expiresAt ?? '').trim();
  if (expires !== '') body.expires_at = expires;
  if (opts.ifGeneration !== undefined && opts.ifGeneration !== null) {
    body.if_generation = opts.ifGeneration;
  }
  return { path: `/api/config/${field}`, method: 'POST', body };
}

/** Read the policy in force. Never writes — an expiry is a writer's effect. */
export function fetchReviewPolicy(f: typeof fetch = fetch): Promise<Result<ReviewPolicy>> {
  return send<ReviewPolicy>({ path: REVIEW_POLICY_PATH, method: 'GET' }, f);
}

/** Edit one threshold, optionally with an expiry and a generation CAS. */
export function setReviewOverride(
  field: string,
  value: unknown,
  reason: string,
  opts: ReviewSetOptions = {},
  f: typeof fetch = fetch
): Promise<Result<ReviewWriteState>> {
  let req: WriteRequest;
  try {
    req = buildReviewSetRequest(field, value, reason, opts);
  } catch (e) {
    return Promise.resolve({
      ok: false,
      kind: 'bad_value',
      status: 0,
      message: (e as Error).message
    });
  }
  return send<ReviewWriteState>(req, f);
}

/** Drop one threshold's override; the service appends the reversion event. */
export function revertReviewOverride(
  field: string,
  f: typeof fetch = fetch
): Promise<Result<ReviewWriteState>> {
  return send<ReviewWriteState>(buildRevertRequest(field), f);
}
