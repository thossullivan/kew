/**
 * FUI-23 — the Monitor's live-runs table (DMonitor y ≈294–440), decided in one
 * pure place.
 *
 * Six columns over 56px rows: Item, Stage, Runner, Posture, Elapsed, State. The
 * table answers one question — what is the fleet doing *right now* — so every
 * rule in this file is about not overstating that answer.
 *
 * **The filter is the header's own predicate.** `isWorkingRun` decides which
 * runs are working for the shell's "2 working" counter and for the Monitor's
 * 44px Working readout; this table asks the same function rather than matching a
 * status word, so the rows and the figure above them can never disagree. It also
 * keeps a merely queued `pending` run out: it has been dispatched, but nothing
 * is executing yet, and a table of live runs that lists it is wrong about the
 * fleet.
 *
 * **An item the payload cannot name is not item #0.** `/api/runs` carries
 * `issue_num` alone — its DTO has no `item_id` — so a run on a non-GitHub
 * (Linear) item arrives as the documented `0` sentinel, which is the same
 * "names nothing" `work/list.ts` reads it as. Such a row's Item cell says
 * "no item" and links nowhere: `#0` references an item no tracker has, and
 * `/work?item=0` is a selection the Work screen cannot key.
 *
 * **A run with no start time has no elapsed.** `duration_seconds` is 0 for the
 * whole life of a live run, so falling back to it would draw "0s" beside a run
 * that has been going for an hour. No `started_at` (or one that will not parse)
 * says "start not recorded". The formatting itself is `epic/format.elapsed`,
 * reused rather than restated: the same "47m" the epic panes draw.
 *
 * **No cell is a bare dash (#2847 P11).** A row is a run that IS executing, so
 * a reading it lacks is a fact recorded nowhere, not an unread source: the cell
 * says which one in words, and a missing part of a line is dropped rather than
 * stood in for. `— · review_stage` was two of those at once.
 *
 * **The stage word is the view's; everything after it is the payload's.**
 * FUI-13 serves a stage from a closed vocabulary (`kew.service.runs.RUN_STAGES`)
 * plus a detail line it composed from recorded facts ("round 1 · PR #2059").
 * This file names the stage — DMonitor calls `review` "governed review", because
 * that is what kew's review stage is — and passes the detail through untouched.
 * `unknown` is a real answer there, a run kind kew has no stage word for,
 * so it draws no stage word: a stage-shaped guess in this cell would caption an
 * ungoverned `kew review` run as governed. The detail line still prints,
 * because the serve puts the recorded kind in it precisely so a row with no
 * stage is still traceable — and that kind is worded through `factory/names`,
 * never printed as its snake_case code.
 *
 * **Posture is worded, not reworded.** The artboard draws two words,
 * "sandboxed" and "host worktree", and those are the two boundaries an operator
 * reads this column for. A token with no phrase here is printed as recorded:
 * mapping `native` or `refused` onto one of the two would claim a boundary kew
 * never attested.
 *
 * **A read that did not happen is not an empty table.** "nothing running" is a
 * claim about the fleet. It is made only when the runs payload was actually
 * read; an unread one names itself instead, the discipline `readouts.ts` runs on.
 */
import type { Run } from '$lib/api';
import { elapsed } from '$lib/epic/format';
import { isUnavailable, UNNAMED_STAGE, type Source } from '$lib/monitor/readouts';
import { isWorkingRun } from '$lib/shell/header-state';
import { itemRef } from '$lib/utils';
import { modelName, runKindLabel } from '$lib/factory/names';

/**
 * The Elapsed column's clock. One second, because the column is a stopwatch:
 * the runs themselves ride the shell's 7s poll, and nothing here refetches.
 */
export const ELAPSED_TICK_MS = 1000;

/**
 * The artboard's grid: Item, Stage, Runner, Posture, Elapsed, State.
 *
 * The Stage track is bounded (#3468 D33): as `minmax(0, 1fr)` it took about
 * 600px at 1440 for one word ("implement"), while the runner and posture
 * sentences beside it truncated in 180px and 160px. 280px holds a stage and
 * its detail ("governed review · round 1 · PR #2059"); the rest of the row
 * goes to the two sentence columns.
 */
export const LIVE_RUNS_COLUMNS =
  '96px minmax(0, 280px) minmax(180px, 1fr) minmax(160px, 1fr) 120px 120px';

/** What the table says when the payload was read and nothing is executing. */
export const LIVE_RUNS_EMPTY = 'nothing running';

/**
 * The State word every drawn row carries.
 *
 * A row is in this table because `isWorkingRun` said the run is working, so the
 * State cell restates that one fact rather than deriving a second one from the
 * same status — two derivations of "is this working?" on one row is how a table
 * ends up marking a row it filtered IN as something else. #2050 gives kew a
 * served state vocabulary and FUI-29 reads the word from there; until then this
 * is the single place the Monitor decides it.
 */
export const LIVE_RUN_STATE = 'working';

/** The Item cell for a run the payload names no item for. */
export const NO_ITEM = 'no item';

/** The Stage cell when neither a stage nor a detail was reported. */
export const STAGE_NOT_RECORDED = 'stage not recorded';

/** The Runner cell when the run recorded neither a model nor a route. */
export const RUNNER_NOT_RECORDED = 'runner not recorded';

/** The Elapsed cell when the run has no start time that parses. */
export const START_NOT_RECORDED = 'start not recorded';

/** One row of the table. Every field is a string the component prints as-is. */
export type LiveRunRow = {
  /** The run id, which is what makes two runs on one item two rows. */
  key: string;
  /** The `?item=` value: the payload's `issue_num` as a string, '' for none. */
  itemId: string;
  /** The Item cell: `#2055`, or `NO_ITEM` when no item is named. */
  ref: string;
  /** Where the Item cell links: the item on the Work screen; '' when unnamed. */
  href: string;
  /** The stage as this table names it, or '' when none was reported. */
  stage: string;
  /** FUI-13's `stage_detail`, its run kind worded; '' when nothing qualifies the stage. */
  stageDetail: string;
  /** The Runner cell: the model's display name, else the route name, else `RUNNER_NOT_RECORDED`. */
  runner: string;
  /** The Runner cell's `title`: the raw id the cell names ('' when none was recorded). */
  runnerId: string;
  /** The Posture cell, worded by `postureWord`. */
  posture: string;
  /** The Elapsed cell: `elapsed` since `started_at`, or `START_NOT_RECORDED`. */
  elapsed: string;
  /** The State cell's status word, for `StatusMark`. Always `LIVE_RUN_STATE`. */
  state: string;
};

export type LiveRunsView = {
  rows: LiveRunRow[];
  /** The section head's right-hand count: "2 working"; '' when there are none. */
  meta: string;
  /** Why the table is empty — `LIVE_RUNS_EMPTY`, or the unread source. */
  notice: string;
};

/**
 * `implement` and `publish` are already the words; `review` is captioned as what
 * kew's review stage is, and `ci_fix` is spelled the way the CI gate is named
 * everywhere else. A stage from a newer serve is named by its own word rather
 * than blanked — see `stageWord`.
 */
export const STAGE_WORD: Record<string, string> = {
  implement: 'implement',
  publish: 'publish',
  review: 'governed review',
  ci_fix: 'CI fix'
};

/**
 * The two postures DMonitor draws, plus the tokens that mean the same boundary.
 *
 * `host` is the containment word the serve uses for an item whose route runs on
 * the machine; `uncontained` is the ledger posture for a run the operator took
 * outside containment (`sandbox.POSTURE_UNCONTAINED`). Both are a worktree on
 * somebody's host, which is the one thing this column exists to make obvious.
 *
 * `pending` is the serve's word for a latest attempt with no posture event
 * (`service/runs._pending_posture`), which is the same absence as the DTO's own
 * "not recorded" default — and neither is "unsandboxed".
 */
export const POSTURE_WORD: Record<string, string> = {
  sandboxed: 'sandboxed',
  host: 'host worktree',
  uncontained: 'host worktree',
  pending: 'not recorded'
};

/** `CI-fix`, `ci fix` and `ci_fix` are one stage word. */
function normalize(raw: string): string {
  return raw.trim().toLowerCase().replace(/[\s-]+/g, '_');
}

/**
 * Own-property lookup, because both tables above are object literals indexed
 * with a word that came off the wire. `POSTURE_WORD['constructor']` answers with
 * `Object.prototype`'s, which is not `undefined`, so a payload naming a
 * prototype member would walk straight past the fallback below and print a
 * function into the cell. Same reachable input, and the same fix, as
 * `factory/status.ts`'s `own`.
 */
function own(table: Record<string, string>, key: string): string | undefined {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

/** The word as shown, for a token no table here names: separators opened up. */
function humanize(key: string): string {
  return key.replace(/_/g, ' ');
}

/**
 * The Stage cell's first word.
 *
 * Nothing ('') for `unknown` and for a stage nobody reported (an older serve
 * omits the field): both are "not reported", and naming a stage kew did not
 * report is the one thing this cell must not do. Any other token — a stage a
 * newer serve grew, a lane this build does not know — is printed as its own word, because a
 * blank cell hides a run while a strange word only asks a question.
 */
export function stageWord(stage: string | null | undefined): string {
  const key = normalize(stage ?? '');
  if (key === '' || key === UNNAMED_STAGE) return '';
  return own(STAGE_WORD, key) ?? humanize(key);
}

/**
 * The Stage cell as the artboard draws it: one 15px line, the word this table
 * gives the stage and then the detail line the serve composed — "governed
 * review · round 1 · PR #2059". A stage nobody reported still carries its
 * detail, which is where FUI-13 puts the recorded kind; the missing word is
 * dropped rather than drawn as a dash, and a line with neither says so.
 */
export function stageLine(row: Pick<LiveRunRow, 'stage' | 'stageDetail'>): string {
  return [row.stage, row.stageDetail].filter(Boolean).join(' · ') || STAGE_NOT_RECORDED;
}

/**
 * FUI-13's detail line with the run kind in it worded. The serve joins its
 * parts with ` · ` and names the kind raw (`review_stage`); every other part
 * ("round 1", "PR #2059") has no word in `RUN_KIND_LABELS` and passes through.
 */
export function stageDetail(detail: string | null | undefined): string {
  return (detail ?? '')
    .split(' · ')
    .map((part) => part.trim())
    .filter(Boolean)
    .map(runKindLabel)
    .join(' · ');
}

/** The Posture cell: the artboard's wording, or the token as recorded. */
export function postureWord(posture: string | null | undefined): string {
  const key = normalize(posture ?? '');
  if (key === '') return 'not recorded';
  return own(POSTURE_WORD, key) ?? humanize(key);
}

/**
 * How long this run has been going, or `START_NOT_RECORDED` when it cannot be said.
 *
 * Negative durations are clamped by `elapsed` itself, so a host clock running
 * ahead of the serve's draws "0s" rather than a run that started in the future.
 */
export function runElapsed(startedAt: string | null | undefined, nowMs: number): string {
  if (!startedAt) return START_NOT_RECORDED;
  const started = Date.parse(startedAt);
  if (Number.isNaN(started)) return START_NOT_RECORDED;
  return elapsed(nowMs - started);
}

/**
 * The Work screen's own route for one item.
 *
 * The id comes off the wire and may be a Linear key, so it is escaped and never
 * parsed. A run payload carries `issue_num` alone, and the number as a string is
 * the same `item_id` `work/list.ts` keys a run-backed row on, so the link lands
 * on the row the Work list draws for it.
 */
export function workHref(itemId: string): string {
  return `/work?item=${encodeURIComponent(itemId)}`;
}

/**
 * The item this run is on, as a `?item=` key — or '' when the payload names
 * none.
 *
 * A run DTO carries `issue_num` and no `item_id`, so a run on a non-GitHub
 * item is served as the `0` sentinel: a number that names nothing, which is
 * exactly how `work/list.ts` reads it when it indexes run titles. Anything not
 * a positive issue number is that same absence, and the Item cell says so
 * rather than drawing a reference to item zero.
 */
export function runItemKey(issueNum: number | null | undefined): string {
  if (issueNum == null || !Number.isInteger(issueNum) || issueNum <= 0) return '';
  return String(issueNum);
}

/**
 * The Runner cell.
 *
 * The model is the most specific name kew records for the agent doing the
 * work: two routes can share a runner name and differ only in model. The route's
 * configured name is the fallback for a run recorded before a model was, and a
 * run with neither named says so rather than leaving an empty cell or a dash.
 */
function runnerId(run: Run): string {
  return (run.model ?? '').trim() || (run.runner ?? '').trim();
}

/** The model as the catalog names it ("Opus 5.5"); the raw id is the `title` (#2541). */
function runnerLabel(id: string): string {
  return id ? modelName(id) : RUNNER_NOT_RECORDED;
}

/**
 * The working runs, in the order the payload lists them — which for `/api/runs`
 * is newest first, the artboard's order. Nothing is sorted here: a table that
 * reorders a poll's rows makes a row move under the pointer for no reason.
 */
export function liveRunRows(runs: readonly Run[], nowMs: number): LiveRunRow[] {
  return runs.filter(isWorkingRun).map((run) => {
    const itemId = runItemKey(run.issue_num);
    const runner = runnerId(run);
    return {
      key: run.run_id,
      itemId,
      ref: itemId ? itemRef(null, run.issue_num) : NO_ITEM,
      href: itemId ? workHref(itemId) : '',
      stage: stageWord(run.stage),
      stageDetail: stageDetail(run.stage_detail),
      runner: runnerLabel(runner),
      runnerId: runner,
      posture: postureWord(run.posture),
      elapsed: runElapsed(run.started_at, nowMs),
      state: LIVE_RUN_STATE
    };
  });
}

/** The rows, the head's count, and the reason there is nothing to draw. */
export function liveRunsView(runs: Source<readonly Run[]>, nowMs: number): LiveRunsView {
  if (isUnavailable(runs)) return { rows: [], meta: '', notice: runs.reason };
  const rows = liveRunRows(runs, nowMs);
  // No "0 working" over "nothing running": one of the two says it, not both.
  if (rows.length === 0) return { rows, meta: '', notice: LIVE_RUNS_EMPTY };
  return { rows, meta: `${rows.length} working`, notice: '' };
}
