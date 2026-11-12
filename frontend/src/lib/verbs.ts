// Typed callers for the operator-verb write API (`POST /api/verbs/*`) plus the
// approvals endpoint and the fleet stop, wired into the factory screens' rows
// and header (#703, Group 5). Every call returns a discriminated `VerbResult` so a row
// can transition from the WRITE RESPONSE (the feedback contract — the endpoints
// return the target's new authoritative state) and render failures INLINE on the
// row, never a page-top message that scrolls away. Pure and vitest-covered so
// `+page.svelte` stays a renderer.

import type { FleetStopResult } from './api';

// --- Result + error taxonomy ------------------------------------------------

export type VerbErrorKind =
  | 'confirm_required' // gated verb invoked without confirm (client short-circuit or HTTP 400)
  | 'reason_required' // a verb that must be explained was sent without a reason (client short-circuit)
  | 'not_approver' // actor not in [approval].approvers (HTTP 403 VerbDenied)
  | 'unauthenticated' // no / expired session (HTTP 401)
  | 'not_found' // unknown issue, nothing to revert (HTTP 404)
  | 'server' // any other non-2xx
  | 'network'; // fetch itself threw (offline, DNS, …)

export type VerbError = {
  ok: false;
  kind: VerbErrorKind;
  status: number;
  message: string;
};

export type VerbResult<T> = { ok: true; data: T } | VerbError;

// Write-response shapes — mirror the dicts returned by kew.service.verbs and the
// approvals / config-override service functions field-for-field.
export type ResumeState = { resumed: boolean; driver_stale: boolean };
export type QueueState = { issue: number; ready: boolean; driver_stale: boolean };
export type AckState = { pr_number: number; acked: boolean; acked_at: string | null };
export type DecideState = { status: string; issue: string };

// --- Transport --------------------------------------------------------------

// Reads the readable (httponly:false) kew_csrf cookie set at login. `document`
// is absent under the node test env — a missing token is fine there because
// fetch is injected and only the header's presence is asserted.
function csrfToken(): string {
  const m = /(?:^|;\s*)kew_csrf=([^;]+)/.exec(
    typeof document === 'undefined' ? '' : document.cookie
  );
  return m ? decodeURIComponent(m[1]) : '';
}

function classifyStatus(status: number): VerbErrorKind {
  if (status === 400) return 'confirm_required';
  if (status === 401) return 'unauthenticated';
  if (status === 403) return 'not_approver';
  if (status === 404) return 'not_found';
  return 'server';
}

function defaultMessage(kind: VerbErrorKind): string {
  switch (kind) {
    case 'confirm_required':
      return 'This action needs an explicit confirm.';
    case 'reason_required':
      return 'This action needs a reason.';
    case 'not_approver':
      return "You're not an approver — this write is reserved for [approval].approvers.";
    case 'unauthenticated':
      return 'Sign in to act on this row.';
    case 'not_found':
      return 'Not found — it may already be resolved.';
    case 'network':
      return 'Network error — check the connection and retry.';
    case 'server':
      return 'The server rejected this action.';
  }
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

// The not_approver / unauthenticated explanations are deliberately the friendly
// defaults (the server `detail` is a technical allowlist string); other kinds
// prefer the server's own detail when present.
function messageFor(kind: VerbErrorKind, detail: string): string {
  if (kind === 'not_approver' || kind === 'unauthenticated') return defaultMessage(kind);
  return detail || defaultMessage(kind);
}

async function errorFrom(r: Response): Promise<VerbError> {
  const kind = classifyStatus(r.status);
  const detail = await detailOf(r);
  return { ok: false, kind, status: r.status, message: messageFor(kind, detail) };
}

async function send<T>(
  path: string,
  method: 'POST' | 'DELETE',
  body: unknown | undefined,
  f: typeof fetch
): Promise<VerbResult<T>> {
  const headers: Record<string, string> = { 'X-Kew-CSRF': csrfToken() };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let r: Response;
  try {
    r = await f(path, {
      method,
      headers,
      ...(body !== undefined ? { body: JSON.stringify(body) } : {})
    });
  } catch (e) {
    return { ok: false, kind: 'network', status: 0, message: (e as Error).message || defaultMessage('network') };
  }
  if (!r.ok) return errorFrom(r);
  // A 2xx whose body is not JSON (a proxy's interstitial, a truncated reply) is
  // not the write-response contract, and the throw escaped into a caller that
  // only ever handles a `VerbResult` — so the row that asked stayed
  // busy for good, its `s.busy = false` never reached (C16). It is reported as
  // a `server` error, because the one thing we cannot say is what happened.
  try {
    return { ok: true, data: (await r.json()) as T };
  } catch {
    return {
      ok: false,
      kind: 'server',
      status: r.status,
      message: 'The server answered, but not with a readable result.'
    };
  }
}

const confirmRequired = (verb: string): VerbError => ({
  ok: false,
  kind: 'confirm_required',
  status: 0,
  message: `${verb} needs an explicit confirm.`
});

// --- Typed verb callers -----------------------------------------------------

/**
 * Resume the fleet (approver-gated + explicit-confirm). Short-circuits to a
 * `confirm_required` result WITHOUT hitting the network when `confirm` is not
 * true — the dialog must run first (proves "no call fires without confirm").
 */
export function resumeFleet(confirm: boolean, f: typeof fetch = fetch): Promise<VerbResult<ResumeState>> {
  if (confirm !== true) return Promise.resolve(confirmRequired('Resume'));
  return send<ResumeState>('/api/verbs/resume', 'POST', { confirm: true }, f);
}

/**
 * Stop the fleet — the kill switch (`POST /api/fleet/stop`, write-gated). It
 * needs BOTH a written reason and an explicit confirm, and short-circuits
 * WITHOUT touching the network when either is missing: the reason is recorded
 * against the latch, and a blank one is not a reason. The reason is sent
 * trimmed, as it was read.
 */
export function stopFleetVerb(
  reason: string,
  confirm: boolean,
  f: typeof fetch = fetch
): Promise<VerbResult<FleetStopResult>> {
  const why = reason.trim();
  if (why === '') {
    return Promise.resolve({
      ok: false,
      kind: 'reason_required',
      status: 0,
      message: 'Stopping the fleet needs a reason.'
    });
  }
  if (confirm !== true) return Promise.resolve(confirmRequired('Stop the fleet'));
  return send<FleetStopResult>('/api/fleet/stop', 'POST', { reason: why }, f);
}

/** Confirmed-state chip for a fleet-stop response. */
export function stopFleetChip(s: FleetStopResult): string {
  if (s.already_stopped) return 'already stopped';
  const n = s.runs_killed.length;
  return n === 0 ? 'stopped' : `stopped — ${n} run${n === 1 ? '' : 's'} killed`;
}

/** Queue an open, surfaced issue for dispatch (approver-gated + explicit-confirm). */
export function queueIssue(
  issue: number,
  confirm: boolean,
  f: typeof fetch = fetch
): Promise<VerbResult<QueueState>> {
  if (confirm !== true) return Promise.resolve(confirmRequired('Queue'));
  return send<QueueState>(`/api/verbs/queue/${issue}`, 'POST', { confirm: true }, f);
}

/** Acknowledge an orphan PR (ledger-only; never closes the PR). */
export function ackOrphan(pr: number, f: typeof fetch = fetch): Promise<VerbResult<AckState>> {
  return send<AckState>(`/api/verbs/orphans/${pr}/ack`, 'POST', undefined, f);
}

/**
 * Approve or deny an approval / budget hold (rewired from the old inbox).
 * `issue` is the durable work-item id (a GitHub number or a bare tracker id
 * like `SFD-12`) — encoded into the route path.
 */
export function decideHold(
  issue: string,
  action: 'approve' | 'deny',
  reason: string,
  f: typeof fetch = fetch
): Promise<VerbResult<DecideState>> {
  return send<DecideState>(`/api/approvals/${encodeURIComponent(issue)}`, 'POST', { action, reason }, f);
}

// --- Row-transition helpers (pure) ------------------------------------------

/**
 * Whether an ack response should drop the orphan row from the queue: the write
 * succeeded, so the row has left the queue's world (acked rows are excluded from
 * the next read; we remove optimistically on the response, not the read-back).
 */
export function ackRemovesRow(res: VerbResult<AckState>): boolean {
  return res.ok;
}

/**
 * Whether an approve/deny response should drop the hold from the surface that
 * offered it. The decision has landed, so the row has left the queue's world;
 * we remove on the WRITE RESPONSE, not on the read-back (CS-6), so a lagging
 * poll cannot resurrect a row the operator already actioned.
 *
 * The Monitor's holds column (FUI-24) drops a decided row on it; any other
 * surface that decides a hold must too, because two surfaces disagreeing about
 * when a decided row leaves is how one of them shows a hold that is already
 * resolved.
 */
export function decideRemovesRow(res: VerbResult<DecideState>): boolean {
  return res.ok;
}

/**
 * The heartbeat-stale warning for queue / resume responses: when the write
 * lands but no loop is running to pick the work up, the row says so with the
 * exact recovery command. Null when a live driver exists.
 */
export function driverStaleNotice(driverStale: boolean): string | null {
  return driverStale ? 'No loop is running to pick this up — start one with `kew run`.' : null;
}

/** Confirmed-state chip for a resume response. */
export function resumeChip(s: ResumeState): string {
  return s.resumed ? 'resumed' : 'already running';
}

/** Confirmed-state chip for a queue response. */
export function queueChip(s: QueueState): string {
  return s.ready ? 'queued' : 'not queued';
}

/** Confirmed-state chip for an ack response. */
export function ackChip(_s: AckState): string {
  return 'acknowledged';
}

// --- Run requests (#2537) -----------------------------------------------------

/** One run request as `kew.run_requests.RunRequest.to_json` writes it. */
export type RunRequest = {
  id: string;
  command: string;
  operator: string;
  requested_at: string;
  source: string;
  state: 'queued' | 'running' | 'done' | 'failed' | 'refused' | 'unknown' | 'superseded';
  item_id: string | null;
  drive: string | null;
  host: string | null;
  pid: number | null;
  log: string | null;
  started_at: string | null;
  finished_at: string | null;
  exit_code: number | null;
  detail: string | null;
  /** The launchpad's per-dispatch model (#2741); null when none was chosen. */
  model?: string | null;
  /** The run a `superseded` request was fenced by (#2975). */
  superseded_by?: string | null;
};
export type DriveState = { state: string; running: boolean; reason: string | null };
export type RunRequestQueued = { request: RunRequest; created: boolean; drive: DriveState };

/**
 * Queue one printed command for the running `kew drive` (`POST
 * /api/run-requests`). The console launches nothing: a 409 is "no drive
 * running", or (#2975) the pair already queued or running under another press,
 * and a 422 a command the drive may not run, each carried back as the server's
 * own sentence. A 424 is a request whose queued fact could not be written, so
 * neither was: nothing queued, press Run again.
 */
export function queueRunRequest(
  command: string,
  itemId: string | null,
  f: typeof fetch = fetch,
  model: string | null = null,
  key: string | null = null
): Promise<VerbResult<RunRequestQueued>> {
  // No model, no key: an untouched picker sends no override (#2741).
  const body: Record<string, string | null> = model
    ? { command, item_id: itemId, model }
    : { command, item_id: itemId };
  // The press this attempt is for (#2975): every attempt with one key gets back
  // the request that key queued, so a lost answer can be asked again.
  if (key) body.key = key;
  return send<RunRequestQueued>('/api/run-requests', 'POST', body, f);
}
