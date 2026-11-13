/**
 * #2537 — the Run button's model: which printed commands the drive may run,
 * and the one line a block prints about the request it issued.
 *
 * The console still never launches anything. A Run writes a request to the
 * ledger (`POST /api/run-requests`); the supervised `kew drive`, which already
 * runs as the operator, picks it up and runs it as a fresh kew child. So the
 * line under the command reports what THE DRIVE did — queued, running, done,
 * failed, refused and why — never what the console hoped.
 *
 * `RUNNABLE` mirrors `kew.run_requests.RUNNABLE` (a Python test reads this file
 * and holds the two equal): a command outside it keeps Copy alone, because the
 * server would refuse it and a button that can only fail is worse than none.
 */
import { writable } from 'svelte/store';
import { queueRunRequest, type RunRequest } from '$lib/verbs';

/** Verb path and positional argument count, as `kew.run_requests.RUNNABLE`. */
export const RUNNABLE: ReadonlyArray<readonly [readonly string[], number]> = [
  [['dispatch'], 1],
  [['epic', 'continue'], 2],
  [['epic', 'retry'], 2],
  [['epic', 'unhold'], 2]
];

/** `kew.waits._RUNNABLE_ARG`: one plain token, never an option. */
const ARG = /^[A-Za-z0-9._:/@][A-Za-z0-9._:/@-]*$/;

/** Whether the drive would accept `command` as a run request. */
export function isRunnable(command: string): boolean {
  const words = command.trim().split(/\s+/);
  if (words[0] !== 'kew') return false;
  const rest = words.slice(1);
  return RUNNABLE.some(([verb, arity]) => {
    if (verb.some((word, i) => rest[i] !== word)) return false;
    const args = rest.slice(verb.length);
    return args.length === arity && args.every((a) => ARG.test(a));
  });
}

const TERMINAL = new Set(['done', 'failed', 'refused', 'unknown', 'superseded']);
const STATES = new Set(['queued', 'running', ...TERMINAL]);

/**
 * Whether `value` is a request row the button can follow and print (#2878):
 * the whole `kew.run_requests.RunRequest.to_json` contract, every nullable
 * field present as null or its type. A 2xx is not proof of a row: a body of
 * `{}` once reached `followRun` as `undefined`, threw outside every catch and
 * left Run locked at "Sending…", and a row of only id, state and operator
 * printed "failed (exit undefined)". `model` and `superseded_by` alone may be
 * absent: each is optional in `RunRequest`, and an absent one is none.
 */
export function isRunRequest(value: unknown): value is RunRequest {
  if (typeof value !== 'object' || value === null) return false;
  const r = value as Record<string, unknown>;
  const text = (v: unknown) => typeof v === 'string';
  const maybe = (v: unknown, type: 'string' | 'number') => v === null || typeof v === type;
  return (
    text(r.id) &&
    r.id !== '' &&
    text(r.command) &&
    text(r.operator) &&
    text(r.requested_at) &&
    text(r.source) &&
    text(r.state) &&
    STATES.has(r.state as string) &&
    ['item_id', 'drive', 'host', 'log', 'started_at', 'finished_at', 'detail'].every((k) =>
      maybe(r[k], 'string')
    ) &&
    maybe(r.pid, 'number') &&
    maybe(r.exit_code, 'number') &&
    (r.model === undefined || maybe(r.model, 'string')) &&
    (r.superseded_by === undefined || maybe(r.superseded_by, 'string'))
  );
}

/** `kew.run_requests.canonical`: the one spelling the ledger keeps a command in. */
function canonical(command: string): string {
  return command.trim().split(/\s+/).join(' ');
}

/**
 * Whether `request` is the one a POST of (`command`, `model`) asked for: the
 * drive keys its active request on that pair, so an answer naming any other
 * is not this Run's (#2878). No model (null or empty) matches a request sent
 * without one.
 */
export function answersRun(request: RunRequest, command: string, model: string | null): boolean {
  return request.command === canonical(command) && (request.model || null) === (model || null);
}

/** The line when the POST was accepted but its answer names no request. */
export const RUN_UNREADABLE = "the server's answer named no request — press Run to try again";

export function isTerminal(request: RunRequest): boolean {
  return TERMINAL.has(request.state);
}

/** The line under the command: what the drive did, in its own words. */
export type RunLine = { tone: 'quiet' | 'ok' | 'warn'; text: string };

function lastLine(detail: string | null): string {
  const lines = (detail ?? '').split('\n').filter((l) => l.trim() !== '');
  return lines.length > 0 ? lines[lines.length - 1] : '';
}

export function runLine(request: RunRequest): RunLine {
  const detail = lastLine(request.detail);
  switch (request.state) {
    case 'queued':
      // A queued request with a reason is waiting on a hold that lifts by
      // itself (host load, #2813): the drive says why in its own words.
      return {
        tone: 'quiet',
        text: detail ? `queued — ${detail}` : 'queued — the drive picks it up within seconds'
      };
    case 'running':
      return { tone: 'quiet', text: `running as ${request.operator}` };
    case 'done':
      return { tone: 'ok', text: detail ? `done — ${detail}` : 'done' };
    case 'failed': {
      const code = request.exit_code === null ? '' : ` (exit ${request.exit_code})`;
      return { tone: 'warn', text: `failed${code}${detail ? ` — ${detail}` : ''}` };
    }
    case 'refused':
      return { tone: 'warn', text: `refused by the drive — ${detail}` };
    case 'superseded':
      // The drive's fence (#2975): the item's own run already did this work.
      return { tone: 'warn', text: `not run — ${detail}` };
    default:
      return { tone: 'warn', text: `outcome unknown — ${detail}` };
  }
}

/**
 * The Run button's label for the request it follows (#2878). "Sending…" until
 * the POST answers: no request is in the ledger yet, so the button claims none.
 * "Queued…" only for a row the drive reports queued — it can wait an hour on
 * host load, and the line under the command says why. Only a request the drive
 * has started reads "Running…". Null (nothing sent yet, a failed send, or a
 * settled request) is the idle "Run".
 */
export function runButtonLabel(state: RunRequest['state'] | 'sending' | null): string {
  if (state === 'sending') return 'Sending…';
  if (state === 'queued') return 'Queued…';
  if (state === 'running') return 'Running…';
  return 'Run';
}

/**
 * The Run button's accessible name (#2878): its state's word and the command,
 * so a screen reader hears what the eye reads — "Queued kew dispatch 2060",
 * not a fixed "Run command" over a button that says "Queued…".
 */
export function runButtonName(state: RunRequest['state'] | 'sending' | null, command: string): string {
  return `${runButtonLabel(state).replace(/…$/, '')} ${canonical(command)}`;
}

/**
 * One ledger lookup for a pair (#2975). `empty`: the server answered, and
 * holds no request for it. `found`: its newest. `failed`: no answer that says —
 * the network, a non-2xx, a body that is not a list of requests — which is
 * not evidence the ledger is empty, so it never frees the pair.
 */
export type Lookup =
  | { kind: 'empty' }
  | { kind: 'found'; request: RunRequest }
  | { kind: 'failed' };

/**
 * The newest request for this exact command and model, so a reload still shows
 * it. The drive holds one active request per (command, model) (#2878): a Sol
 * run queued for `kew dispatch 2060` is not Luna's, and must not hold Luna's
 * Run as "Queued…". No model (null or empty) matches a request sent without one.
 *
 * The server filters by the pair before its 20-row limit (an empty `model` is
 * "sent without one"), so Sol's request behind a page of newer requests on
 * other models is still found; the client re-checks every row it is handed.
 */
export async function lookupRunRequest(
  command: string,
  model: string | null = null,
  f: typeof fetch = fetch
): Promise<Lookup> {
  try {
    const query = new URLSearchParams({ command, model: model || '' });
    const r = await f(`/api/run-requests?${query}`);
    if (!r.ok) return { kind: 'failed' };
    const body = (await r.json()) as { requests?: unknown } | null;
    if (!Array.isArray(body?.requests)) return { kind: 'failed' };
    const rows = body.requests.filter(isRunRequest);
    const request = rows.find((row) => answersRun(row, command, model));
    return request === undefined ? { kind: 'empty' } : { kind: 'found', request };
  } catch {
    return { kind: 'failed' };
  }
}

/** Wait `ms`, or less when `wake` is called first. */
function pause(ms: number, onwake: (wake: () => void) => void): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    onwake(() => {
      clearTimeout(timer);
      resolve();
    });
  });
}

/**
 * Ask the ledger until it answers (#2975): a failed lookup keeps the pair
 * blocked and asks again on the follow's beats, slowing after
 * `FOLLOW_PATIENCE` tries; `gone()` ends the asking.
 */
async function lookUntilKnown(
  command: string,
  model: string | null,
  f: typeof fetch,
  everyMs: number,
  gone: () => boolean,
  onwake: (wake: () => void) => void = () => {}
): Promise<Exclude<Lookup, { kind: 'failed' }> | null> {
  for (let tries = 1; !gone(); tries++) {
    const found = await lookupRunRequest(command, model, f);
    if (found.kind !== 'failed') return found;
    if (gone()) break;
    await pause(tries < FOLLOW_PATIENCE ? everyMs : FOLLOW_SLOW_MS, onwake);
  }
  return null;
}

type Reading = { request: RunRequest | null; error: string };
type Listener = (state: Reading) => void;

/** A Run of one (command, model) pair whose POST the console sent (#2975). */
export type PendingRun = {
  command: string;
  model: string | null;
  itemId: string | null;
};

/**
 * One submission, kept above the button that pressed it (#2975). A button can
 * unmount mid-POST — a lane change re-resolves Run away, the operator selects
 * another item — but the POST still commits on the server, so its answer is
 * still this Run's. The submission outlives every button: a button mounted on
 * the same pair joins it and reads "Sending…", and the answer is delivered to
 * whoever listens when it lands, or kept for the next button that mounts.
 * Leaving a submission is never a cancellation: nothing here can take back a
 * POST.
 */
type Submission = PendingRun & {
  key: string;
  listeners: Set<Listener>;
  /** The POST's answer; null while it is open. */
  outcome: Reading | null;
};

const submissions = new Map<string, Submission>();

function runKey(command: string, model: string | null): string {
  return JSON.stringify([canonical(command), model || null]);
}

/**
 * The Runs whose POST is still open, for the controls that would change their
 * route (#2975): while one is pending the launchpad holds its lane, tier and
 * model, so the operator cannot re-route away from a request still being made.
 */
export const pendingRuns = writable<readonly PendingRun[]>([]);

function publish(): void {
  pendingRuns.set(
    [...submissions.values()]
      .filter((s) => s.outcome === null)
      .map(({ command, model, itemId }) => ({ command, model, itemId }))
  );
  pairLedgers.set(new Map([...watches.values()].map((w) => [w.key, pairLedger(w)])));
}

/** Whether a Run of (`command`, `model`) has a POST still open. */
export function isSubmitting(command: string, model: string | null = null): boolean {
  return submissions.get(runKey(command, model))?.outcome === null;
}

function settle(submission: Submission, outcome: Reading): void {
  submission.outcome = outcome;
  // Delivered, the answer is the listeners' to keep; unheard, it waits in the
  // map for the next button on this pair, so a refusal is never dropped.
  if (submission.listeners.size > 0 && submissions.get(submission.key) === submission)
    submissions.delete(submission.key);
  publish();
  for (const listener of [...submission.listeners]) listener(outcome);
}

/** Whether a POST's failure is the server's refusal (4xx): nothing this press
 * sent will run — a 424 is a request whose queued fact could not be written,
 * rolled back with it. */
function refused(status: number): boolean {
  return status >= 400 && status < 500;
}

/**
 * An answer that is not this press's request, reconciled against the ledger
 * (#2975). A 409: the pair already has a request queued or running that this
 * press did not make — another tab's, a terminal's, one a gone button saw
 * accepted — and the server runs a pair once. A 2xx naming no request: the
 * handler finished, so whatever it queued is already in the ledger. Either
 * way the ledger's unsettled request is the one to follow; only a lookup that
 * answers with none makes `message` the answer and gives Run back.
 */
async function reconcile(
  command: string,
  model: string | null,
  message: string,
  f: typeof fetch,
  everyMs: number
): Promise<Reading> {
  // The POST stays open, so the pair stays held, until the ledger answers.
  const found = await lookUntilKnown(command, model, f, everyMs, () => false);
  if (found?.kind === 'found' && !isTerminal(found.request))
    return { request: found.request, error: '' };
  return { request: null, error: message };
}

/** One random key per Run press (#2975): hex, from the generator every browser
 * has — `randomUUID` is missing outside a secure context. */
function submissionKey(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

/**
 * The POST, until its answer says what happened (#2975). Every attempt sends
 * the press's one key, which the server writes in the same transaction as the
 * request it queues, so every attempt after the one that queued gets that
 * request back — whatever it has since become — and one that queued nothing
 * leaves the next free to. So an answer that never reached us whole (the
 * network dropped it, a 5xx) is asked again on the follow's beats, and the
 * pair stays held meanwhile: one ledger read cannot say the POST queued
 * nothing, because the server may still be writing it. A 2xx naming no
 * request for this pair is a finished handler, so the ledger can say: it is
 * reconciled there (`reconcile`), held until a lookup answers. Only a request,
 * a refusal or that reconciliation settles the Run.
 */
async function post(
  submission: Submission,
  f: typeof fetch,
  everyMs: number
): Promise<Reading> {
  const { command, itemId, model } = submission;
  const key = submissionKey();
  for (let tries = 1; ; tries++) {
    const res = await queueRunRequest(command, itemId, f, model, key);
    if (res.ok) {
      const request = (res.data as { request?: unknown } | null)?.request;
      if (isRunRequest(request) && answersRun(request, command, model)) return { request, error: '' };
      return reconcile(command, model, RUN_UNREADABLE, f, everyMs);
    } else if (res.status === 409) return reconcile(command, model, res.message, f, everyMs);
    else if (refused(res.status)) return { request: null, error: res.message };
    const wait = tries < FOLLOW_PATIENCE ? everyMs : FOLLOW_SLOW_MS;
    await new Promise((resolve) => setTimeout(resolve, wait));
  }
}

function submit(
  command: string,
  itemId: string | null,
  model: string | null,
  opts: { f?: typeof fetch; everyMs?: number }
): Submission {
  const submission: Submission = {
    key: runKey(command, model),
    command: canonical(command),
    model: model || null,
    itemId,
    listeners: new Set(),
    outcome: null
  };
  submissions.set(submission.key, submission);
  publish();
  void post(submission, opts.f ?? fetch, opts.everyMs ?? FOLLOW_EVERY_MS)
    .catch(() => ({ request: null, error: RUN_UNREADABLE }))
    .then((outcome) => {
      if (outcome.request !== null) notePair(outcome.request);
      settle(submission, outcome);
    });
  return submission;
}

/**
 * Listen to `submission` until its POST answers, then follow the request it
 * names. The returned function leaves it — and the follow — without touching
 * the POST.
 */
function join(
  submission: Submission,
  onchange: Listener,
  opts: { f?: typeof fetch; everyMs?: number }
): () => void {
  let stop: (() => void) | null = null;
  let left = false;
  const take = (outcome: Reading) => {
    submission.listeners.delete(take);
    if (left) return;
    if (outcome.request === null) {
      onchange(outcome);
      return;
    }
    try {
      stop = followRun(outcome.request, onchange, opts);
    } catch {
      // A listener that throws on its first reading lands in the error state,
      // so Run is pressable again (#2878).
      onchange({ request: null, error: RUN_UNREADABLE });
    }
  };
  if (submission.outcome === null) submission.listeners.add(take);
  else {
    if (submissions.get(submission.key) === submission) submissions.delete(submission.key);
    take(submission.outcome);
  }
  return () => {
    left = true;
    submission.listeners.delete(take);
    stop?.();
  };
}

/**
 * One Run: queue `command`, then follow the request until the drive settles
 * it. `onchange` receives every reading; the returned function stops
 * listening (the block unmounted, or another item was selected) and never
 * cancels the POST (#2975). `opts.model` is the launchpad's per-dispatch model
 * (#2741), sent beside the command.
 *
 * A Run of a pair whose POST is still open sends nothing: it joins that
 * submission, so one (command, model) never has two Runs unsettled.
 */
export function startRun(
  command: string,
  itemId: string | null,
  onchange: (state: Reading) => void,
  opts: { f?: typeof fetch; everyMs?: number; model?: string | null } = {}
): () => void {
  const model = opts.model ?? null;
  const open = submissions.get(runKey(command, model));
  const submission =
    open !== undefined && open.outcome === null
      ? open
      : submit(command, itemId, model, opts);
  return join(submission, onchange, opts);
}

/**
 * Pick up the submission a button that unmounted left behind (#2975): a POST
 * still open for (`command`, `model`), or its answer that landed with no button
 * there to hear it. Null when there is none, and the ledger is the record.
 */
export function resumeRun(
  command: string,
  model: string | null,
  onchange: Listener,
  opts: { f?: typeof fetch; everyMs?: number } = {}
): (() => void) | null {
  const submission = submissions.get(runKey(command, model));
  return submission === undefined ? null : join(submission, onchange, opts);
}

/**
 * What the console knows of a pair's ledger (#2975), for every control that
 * hands its command over — Run and both Copies. `checking`: no lookup has
 * answered yet, or the last one failed and is being asked again. `held`: a
 * POST of it is open, or the ledger has a request of it queued or running.
 * `clear`: the ledger answered, and holds nothing of it unsettled. Only
 * `clear` may send or copy the command: anything else may be a request
 * already made, and a second would run beside it.
 */
export type PairLedger = 'checking' | 'held' | 'clear';

/** One pair's ledger, read for as long as some control shows its command. */
type Watch = {
  key: string;
  command: string;
  model: string | null;
  known: boolean;
  request: RunRequest | null;
  listeners: Set<(request: RunRequest | null) => void>;
  refs: number;
  /** Bumped by every reading, so a lookup that answers later is not taken. */
  generation: number;
  unfollow: (() => void) | null;
  following: string | null;
  stopped: boolean;
  wake: (() => void) | null;
  opts: { f?: typeof fetch; everyMs?: number };
};

const watches = new Map<string, Watch>();

/** Each watched pair's ledger, by `runKey` — read with `pairClear`. */
export const pairLedgers = writable<ReadonlyMap<string, PairLedger>>(new Map());

function pairLedger(w: Watch): PairLedger {
  if (isSubmitting(w.command, w.model)) return 'held';
  if (!w.known) return 'checking';
  return w.request !== null && !isTerminal(w.request) ? 'held' : 'clear';
}

/** Whether (`command`, `model`) is reconciled empty in `ledgers` (#2975): the
 * one predicate under Run, both Copies and `copy()`. A pair nobody watches is
 * not known, so it is not clear. */
export function pairClear(
  ledgers: ReadonlyMap<string, PairLedger>,
  command: string,
  model: string | null
): boolean {
  return ledgers.get(runKey(command, model)) === 'clear';
}

/** Whether (`command`, `model`) has a request in `ledgers` still unsettled:
 * the reason a pair is not clear, apart from its ledger not having answered. */
export function pairHeld(
  ledgers: ReadonlyMap<string, PairLedger>,
  command: string,
  model: string | null
): boolean {
  return ledgers.get(runKey(command, model)) === 'held';
}

/** Take `request` as the pair's newest, and follow it while it is unsettled. */
function setRequest(w: Watch, request: RunRequest): void {
  w.generation += 1;
  w.known = true;
  w.request = request;
  w.wake?.();
  if (isTerminal(request) || w.following !== request.id) {
    w.unfollow?.();
    w.unfollow = null;
    w.following = null;
  }
  if (!isTerminal(request) && w.following === null && !w.stopped) {
    w.following = request.id;
    w.unfollow = followRun(
      request,
      (reading) => {
        if (reading.request !== null && reading.request.id === w.following) {
          w.request = reading.request;
          publish();
        }
      },
      w.opts
    );
  }
  publish();
}

/** A request a POST answered with, for its pair's watch. */
function notePair(request: RunRequest): void {
  const w = watches.get(runKey(request.command, request.model ?? null));
  if (w !== undefined) setRequest(w, request);
}

async function look(w: Watch): Promise<void> {
  const generation = w.generation;
  const found = await lookUntilKnown(
    w.command,
    w.model,
    w.opts.f ?? fetch,
    w.opts.everyMs ?? FOLLOW_EVERY_MS,
    () => w.stopped || w.generation !== generation,
    (wake) => (w.wake = wake)
  );
  w.wake = null;
  if (found === null || w.stopped || w.generation !== generation) return;
  if (found.kind === 'found') setRequest(w, found.request);
  else {
    w.known = true;
    publish();
  }
  for (const listener of [...w.listeners]) listener(w.request);
  w.listeners.clear();
}

/**
 * Read the ledger for (`command`, `model`) for as long as the caller shows its
 * command (#2975), shared by every caller on the pair: one lookup, asked again
 * until it answers, then a follow of the request it found. `onfound` (if any)
 * receives the lookup's request, or null when the ledger holds none — at once
 * when the pair is already known. The returned function stops watching; the
 * last caller out stops the reading.
 */
export function watchPair(
  command: string,
  model: string | null,
  onfound: ((request: RunRequest | null) => void) | null = null,
  opts: { f?: typeof fetch; everyMs?: number } = {}
): () => void {
  const key = runKey(command, model);
  let w = watches.get(key);
  if (w === undefined) {
    w = {
      key,
      command: canonical(command),
      model: model || null,
      known: false,
      request: null,
      listeners: new Set(),
      refs: 0,
      generation: 0,
      unfollow: null,
      following: null,
      stopped: false,
      wake: null,
      opts
    };
    watches.set(key, w);
    publish();
    void look(w);
  }
  const watch = w;
  watch.refs += 1;
  const listener = onfound ?? (() => {});
  if (watch.known) listener(watch.request);
  else watch.listeners.add(listener);
  let left = false;
  return () => {
    if (left) return;
    left = true;
    watch.listeners.delete(listener);
    watch.refs -= 1;
    if (watch.refs > 0) return;
    watch.stopped = true;
    watch.wake?.();
    watch.unfollow?.();
    if (watches.get(key) === watch) watches.delete(key);
    publish();
  };
}

/** The follow's beat while the reading moves. */
export const FOLLOW_EVERY_MS = 2000;
/** Unchanged reads in a row before the follow slows to `FOLLOW_SLOW_MS`. */
export const FOLLOW_PATIENCE = 5;
/** The slow beat: a request the drive has sat on for ten seconds is not going
 * to settle in the next two, and a queue with no drive may sit for hours. */
export const FOLLOW_SLOW_MS = 30_000;
/** What the line says when the session lapsed under a follow. */
export const FOLLOW_SIGNED_OUT = 'signed out — sign in again to see what the drive did';

/** One read of a request: the row, `'unauthorized'` on a 401, or `null` for any
 * other failure (the follow keeps the reading it has and tries again). */
async function readRunRequest(
  id: string,
  f: typeof fetch
): Promise<RunRequest | 'unauthorized' | null> {
  try {
    const r = await f(`/api/run-requests/${encodeURIComponent(id)}`);
    if (r.status === 401) return 'unauthorized';
    if (!r.ok) return null;
    const row: unknown = await r.json();
    return isRunRequest(row) ? row : null;
  } catch {
    return null;
  }
}

const hidden = (): boolean => typeof document !== 'undefined' && document.hidden;

/**
 * One poll per request id, however many blocks show it (#2789, K3). Work can
 * draw the same command twice for one item, and each block used to run its own
 * 2s follow of the same row. The poll lives as long as some block listens.
 */
class Follow {
  private readonly listeners = new Set<Listener>();
  private timer: ReturnType<typeof setTimeout> | null = null;
  private unchanged = 0;
  private stopped = false;
  /** True when a beat fell due while the tab was hidden: the read is owed, and
   * paid the moment the tab is visible again. */
  private owed = false;
  private readonly onVisibility = () => {
    if (!hidden() && this.owed) {
      this.owed = false;
      void this.read();
    }
  };

  constructor(
    private current: RunRequest,
    private readonly f: typeof fetch,
    private readonly everyMs: number,
    private readonly onDone: () => void
  ) {
    if (typeof document !== 'undefined')
      document.addEventListener('visibilitychange', this.onVisibility);
  }

  join(listener: Listener): () => void {
    this.listeners.add(listener);
    try {
      listener({ request: this.current, error: '' });
    } catch (error) {
      // A listener that cannot take its first reading never joined: leave no
      // poll behind it, and let the caller put it in the error state.
      this.listeners.delete(listener);
      if (this.listeners.size === 0) this.stop();
      throw error;
    }
    if (!this.stopped && this.timer === null && !this.owed && !isTerminal(this.current))
      this.schedule();
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0) this.stop();
    };
  }

  /** Two seconds while the reading moves; thirty once it has sat still for
   * `FOLLOW_PATIENCE` reads (an unreadable read is not movement either). */
  private schedule(): void {
    const wait = this.unchanged >= FOLLOW_PATIENCE ? FOLLOW_SLOW_MS : this.everyMs;
    this.timer = setTimeout(() => {
      this.timer = null;
      // A hidden tab asks nothing: the beat is owed until somebody can see it.
      if (hidden()) this.owed = true;
      else void this.read();
    }, wait);
  }

  private async read(): Promise<void> {
    const next = await readRunRequest(this.current.id, this.f);
    if (this.stopped) return;
    if (next === 'unauthorized') {
      // The session, not the drive: no amount of polling reads this row again.
      this.emit({ request: null, error: FOLLOW_SIGNED_OUT });
      this.stop();
      return;
    }
    const moved = next !== null && JSON.stringify(next) !== JSON.stringify(this.current);
    this.unchanged = moved ? 0 : this.unchanged + 1;
    if (next !== null) this.current = next;
    this.emit({ request: this.current, error: '' });
    if (isTerminal(this.current)) this.stop();
    else this.schedule();
  }

  private emit(state: Reading): void {
    for (const listener of [...this.listeners]) listener(state);
  }

  private stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    if (typeof document !== 'undefined')
      document.removeEventListener('visibilitychange', this.onVisibility);
    this.onDone();
  }
}

const follows = new Map<string, Follow>();

/**
 * Follow a request already in the ledger (one issued before a reload) until the
 * drive settles it. Every block following the same request id shares one poll;
 * the returned function leaves it, and the last one out stops it. The poll
 * skips while the tab is hidden, slows after `FOLLOW_PATIENCE` unchanged reads,
 * and stops on a 401 with `FOLLOW_SIGNED_OUT` as the error.
 */
export function followRun(
  request: RunRequest,
  onchange: Listener,
  { f = fetch, everyMs = FOLLOW_EVERY_MS }: { f?: typeof fetch; everyMs?: number } = {}
): () => void {
  if (isTerminal(request)) {
    onchange({ request, error: '' });
    return () => {};
  }
  let follow = follows.get(request.id);
  if (follow === undefined) {
    const id = request.id;
    const created: Follow = new Follow(request, f, everyMs, () => {
      if (follows.get(id) === created) follows.delete(id);
    });
    follow = created;
    follows.set(id, follow);
  }
  return follow.join(onchange);
}
