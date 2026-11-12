// What the Health page says, decided in one pure place (#2298).
//
// The page answers "is the factory able to work?", and the whole file turns on
// one distinction the console got wrong everywhere else first: a reading has
// THREE states, not two.
//
//   `running`     — it is up, and proven so.
//   `required`    — it is down, and something needs it. This is the only red.
//   `not-enabled` — it is off by design. Grey. Never red, never counted.
//
// A fifth, `waiting`, is a loop that is alive and holding off on purpose — in
// the operator's slot gate, say (#2566). Quiet text-3, never counted.
//
// A fourth exists because honesty demands it: `unknown`, for a fact nobody can
// observe. The delegation watcher writes no heartbeat, so "is it running?" has
// no answer — and an unknown painted red would be an alarm invented out of a
// missing instrument. Dagster's Settings tabs are the grouping this page
// borrows; the states, the colours and the counting rule are kew's.
//
// The counting rule is the other half: the header badge counts only what
// MATTERS. A missing image no lane routes to is a mapped image nobody is
// waiting on; an unreachable tracker no epic uses is a credential nobody needs.
// Both used to be the same red as a broken factory, which is how a badge comes
// to be ignored.
import { STATUS_MARKS, statusShape } from '$lib/factory/status';
import { fmtTime, NO_TIME } from '$lib/time';
import { formatDuration } from '$lib/time/duration';
import type {
  Health,
  HealthDriver,
  HealthImage,
  HealthPosture,
  HealthRunner,
  HealthWatcher,
  Readiness
} from '$lib/api';

/** The five readings any row on this page can have. */
export type HealthState = 'running' | 'required' | 'not-enabled' | 'unknown' | 'waiting';

/**
 * A reading, from "is it enabled?" and "is it up?" asked separately.
 *
 * `enabled: false` short-circuits: a thing that is off by design cannot be a
 * fault, whatever the probe says. `up === null` is unknown — the probe could not
 * answer — and only a definite `false` on something enabled is `required`.
 */
export function readingOf(enabled: boolean, up: boolean | null): HealthState {
  if (!enabled) return 'not-enabled';
  if (up === null) return 'unknown';
  return up ? 'running' : 'required';
}

/**
 * Which status word each reading borrows its MARK from.
 *
 * Status is shape plus colour, from one table (`STATUS_MARKS`), so this page
 * does not pick a fifth set of dots. Each reading maps onto a meaning the
 * factory already draws: a green dot for running, the red × for something down
 * and needed (which is what the issue asks for in those words), the muted idle
 * ring for off-by-design, and the dashed grey ring the app already uses for
 * "expected, not here yet" for a fact nobody can observe.
 */
export const STATE_MARK: Record<HealthState, string> = {
  running: 'success',
  required: 'failure',
  'not-enabled': 'idle',
  unknown: 'pending',
  waiting: 'todo'
};

/**
 * The token each reading paints its WORD in — read off `STATUS_MARKS` rather
 * than restated, so the word beside a mark can never be a different colour from
 * the mark. `required` is the only red on the page.
 */
export const STATE_COLOR: Record<HealthState, string> = {
  running: STATUS_MARKS.done.color,
  required: STATUS_MARKS.failed.color,
  'not-enabled': STATUS_MARKS.idle.color,
  unknown: STATUS_MARKS.pending.color,
  waiting: STATUS_MARKS.todo.color
};

/** The mark one reading draws, for a caller that wants shape and colour. */
export function stateMark(state: HealthState) {
  return statusShape(STATE_MARK[state], 'run');
}

/** The word beside the mark. Terse, and never an adjective of approval. */
export const STATE_LABEL: Record<HealthState, string> = {
  running: 'running',
  required: 'not running',
  'not-enabled': 'not enabled',
  unknown: 'unknown',
  waiting: 'waiting'
};

/**
 * One image row's reading.
 *
 * An unrouted image is `not-enabled`: it is mapped, nothing routes to it, so its
 * absence blocks nothing. A routed image whose presence could not be probed is
 * `unknown` — a host with no docker cannot answer "is this image here?", and a
 * red × over a question nobody asked is the failure mode this page exists to
 * avoid.
 */
export function imageState(image: HealthImage): HealthState {
  if (!image.routed) return 'not-enabled';
  if (image.ref === null) return 'required';
  return readingOf(true, image.present);
}

/** One runtime's reading. An unbacked runtime is named, never failed. */
export function runtimeState(runtime: {
  configured: boolean;
  backed: boolean;
  present: boolean | null;
}): HealthState {
  if (!runtime.configured) return 'not-enabled';
  if (!runtime.backed) return 'unknown';
  return readingOf(true, runtime.present);
}

/** One watcher's reading, asked the way the payload states it. */
export function watcherState(watcher: HealthWatcher): HealthState {
  return readingOf(watcher.enabled, watcher.running);
}

/**
 * One runner's reading: the binary, and then a credential it can use.
 *
 * A runner with a binary and no credential is `required`, because a lane routed
 * to it cannot dispatch — and it is exactly the case the old reachability check
 * got wrong, asking `CLAUDE_AUTH_ENVS` (which has no `CLAUDE_CODE_OAUTH_TOKEN`)
 * instead of the runner's own declaration.
 */
export function runnerState(runner: HealthRunner): HealthState {
  if (runner.binary_present === null) return 'unknown';
  if (!runner.binary_present) return 'required';
  if (runner.credential_names.length === 0) return 'running';
  if (runner.credential_names.some((c) => c.set)) return 'running';
  // Missing from `kew serve`'s own environment only: a run on this runner
  // succeeded in the window, so the loops hold one (#2566). A note, not down.
  return runner.credential_note ? 'running' : 'required';
}

/**
 * What a posture row says one click away, behind `why ▸` (#2536): the
 * sentences the row's one line of readouts stands for, in the order a reader
 * needs them — why the lane cannot run, what the config overrode, and the
 * console's own refusal. Empty when the readouts are the whole story, and then
 * the row draws no toggle at all.
 */
export function laneWhy(lane: HealthPosture): string[] {
  const lines: string[] = [];
  if (lane.refusal !== null) lines.push(`Dispatch refuses this lane: ${lane.refusal}`);
  if (lane.dangerously_skip_containment) {
    lines.push('Containment is skipped by config (dangerously_skip_containment).');
  }
  if (lane.console_refusal) {
    lines.push(`This console cannot launch it (${lane.console_refusal}); the loops may.`);
  }
  return lines;
}

/**
 * A lane's reading. A refused lane cannot run at all, so it is the red one.
 *
 * `console_refusal` never reaches here as red: it is the console process's own
 * missing credential, and the console is not what dispatches (#2534). The
 * server leaves `refusal` null for it, so the lane reads `unknown`.
 */
export function postureState(lane: HealthPosture): HealthState {
  if (lane.refusal !== null) return 'required';
  return lane.contained ? 'running' : 'unknown';
}

/**
 * A driver's reading, from the server's own liveness word — never re-derived.
 *
 * An archived or complete epic has no driver to lose (#2534), so its silence is
 * `not-enabled`: grey, never counted, whatever the heartbeat says. `busy` is a
 * driver whose lock holder is provably alive inside one long foreground stage —
 * it cannot tick until that stage returns, and it is working, not gone.
 */
export function driverState(driver: HealthDriver): HealthState {
  if (driver.active === false) return 'not-enabled';
  if (driver.liveness === 'live' || driver.liveness === 'busy') return 'running';
  if (driver.liveness === 'waiting') return 'waiting';
  if (driver.liveness === 'gone') return 'required';
  return 'unknown';
}

/**
 * What to do about a missing routed image (D41, #3470): the readiness
 * `route-image` check's own `fix` when kew can build the image, its `hint`
 * sentence when it cannot (a custom image). Nothing is composed here — the
 * doctor owns the command (`_image_fix` in `kew/cli/doctor.py`), so a page and
 * `kew doctor` can never print two different builds.
 *
 * The doctor inspects only the default route's image, so a check is this
 * image's only when its summary names the image's ref; any other missing image
 * gets no repair line rather than someone else's command.
 */
export type ImageRepair = { kind: 'command'; command: string } | { kind: 'hint'; hint: string };

export function imageRepair(image: HealthImage, readiness: Readiness | null): ImageRepair | null {
  if (!image.routed || image.present !== false || image.ref === null) return null;
  const ref = image.ref;
  const check = readiness?.checks.find(
    (c) => c.id === 'route-image' && c.status === 'fail' && c.summary.includes(ref)
  );
  if (check === undefined) return null;
  if (check.fix !== null && check.fix.trim() !== '') return { kind: 'command', command: check.fix };
  if (check.hint !== null && check.hint.trim() !== '') return { kind: 'hint', hint: check.hint };
  return null;
}

/**
 * The runtimes section's note as a sentence (D41): which runtime the last
 * week's runs used, with the runs that recorded none said in words rather than
 * as an empty key.
 */
export function observedRuntimesNote(observed: Record<string, number> | null | undefined): string {
  const entries = Object.entries(observed ?? {}).filter(([, n]) => n > 0);
  if (entries.length === 0) return 'no runs recorded in the last 7 days';
  const parts = entries.map(([runtime, n]) =>
    runtime === '' ? `${n} with no runtime recorded` : `${n} on ${runtime}`
  );
  return `runs in the last 7 days: ${parts.join(', ')}`;
}

/** The lane's credential posture in words, or '' when there is nothing to say. */
export function credentialWords(lane: HealthPosture): string {
  return lane.credential_mode === 'real' ? 'uses a real credential' : '';
}

/** One counted warning: what is wrong, and where on the page to look. */
export type HealthWarning = { kind: string; label: string; anchor: string };

/**
 * Whether the GitHub tracker is a thing this factory needs.
 *
 * Every epic is tracked somewhere, and a GitHub-tracked project cannot read an
 * issue, open a PR or publish a branch without it — so an active epic's driver, or
 * a queue with anything in it, makes GitHub required. With no epic and nothing
 * pending, an unreachable GitHub is a reading, not an outage worth a badge.
 */
export function githubRequired(health: Health): boolean {
  return health.drivers.some((d) => d.active !== false) || health.fleet.pending > 0;
}

/** Whether Linear is this project's tracker at all. */
export function linearRequired(health: Health): boolean {
  return health.trackers.linear.configured;
}

/**
 * Everything the header badge counts, most structural first.
 *
 * Deliberately small. Every entry is something a person can act on now, and
 * nothing here is counted twice: a lane refused for a missing image appears as
 * the lane, and its image appears as the image, because the operator fixes one
 * thing and both clear.
 */
export function healthWarnings(health: Health | null): HealthWarning[] {
  if (health === null) return [];
  const warnings: HealthWarning[] = [];
  for (const image of health.images) {
    if (imageState(image) !== 'required') continue;
    warnings.push({
      kind: 'image',
      label:
        image.ref === null
          ? `no image mapped for ${image.runner}`
          : `image ${image.ref} is missing`,
      anchor: '#images'
    });
  }
  for (const runner of health.runners) {
    if (runnerState(runner) !== 'required') continue;
    warnings.push({
      kind: 'runner',
      label: !runner.binary_present
        ? `${runner.name} is not on PATH`
        : `${runner.name} has no credential set`,
      anchor: '#runners'
    });
  }
  for (const lane of health.posture) {
    if (lane.refusal === null) continue;
    warnings.push({
      kind: 'posture',
      label: `the ${lane.lane} lane cannot run: ${lane.refusal}`,
      anchor: '#posture'
    });
  }
  if (githubRequired(health)) {
    // Two different repairs, so two different lines: a 401 is a credential to
    // fix, and no answer at all is a network or a missing `gh`. Only one can be
    // true of one reading, so this never counts the same outage twice.
    if (health.trackers.github.auth_ok === false) {
      warnings.push({
        kind: 'tracker',
        label: 'the GitHub credential is not working',
        anchor: '#trackers'
      });
    } else if (health.trackers.github.reachable === false) {
      warnings.push({
        kind: 'tracker',
        label: 'GitHub could not be reached',
        anchor: '#trackers'
      });
    }
  }
  if (linearRequired(health) && health.trackers.linear.reachable === false) {
    warnings.push({ kind: 'tracker', label: 'Linear is unreachable', anchor: '#trackers' });
  }
  for (const driver of health.drivers) {
    if (driverState(driver) !== 'required') continue;
    warnings.push({
      kind: 'driver',
      label: `no driver is ticking ${driver.epic_ref}`,
      anchor: '#drivers'
    });
  }
  return warnings;
}

/**
 * What the header shows, or `null` when it shows nothing.
 *
 * `null` for an unread payload AND for a healthy one: a header badge is an
 * interruption, so it appears only when something required is actually down. The
 * title carries every counted line, because a 56px header has no second row and
 * the words that do not fit must still be reachable.
 */
export function healthBadge(health: Health | null): { count: number; title: string } | null {
  const warnings = healthWarnings(health);
  if (warnings.length === 0) return null;
  return {
    count: warnings.length,
    title: warnings.map((w) => w.label).join(' · ')
  };
}

/**
 * The words a lane draws in red beside its readouts (#2849).
 *
 * Neither is a refusal — dispatch still runs the lane — so neither is counted
 * in the badge. But a red word the headline never mentions reads as a page
 * contradicting itself ("Nothing required is down." over a red row), so the
 * row and the headline both read this one list.
 */
export function laneDangers(lane: HealthPosture): string[] {
  const words: string[] = [];
  if (lane.credential_mode !== 'real') words.push(`credential ${lane.credential_mode}`);
  if (lane.dangerously_skip_containment) words.push('containment skipped');
  return words;
}

/**
 * The page's one-line verdict: the counted faults, then every red word the
 * page draws outside them, each with how many lanes carry it — "Nothing
 * required is down · credential placeholder on 2 loops-only lanes." A count is
 * "loops-only" when every lane in it is one the console cannot launch.
 */
export function healthHeadline(health: Health): string {
  const count = healthWarnings(health).length;
  const parts = [
    count === 0
      ? 'Nothing required is down'
      : `${count} ${count === 1 ? 'thing needs' : 'things need'} attention before the factory can work`
  ];
  const byWord = new Map<string, HealthPosture[]>();
  for (const lane of health.posture) {
    for (const word of laneDangers(lane)) byWord.set(word, [...(byWord.get(word) ?? []), lane]);
  }
  for (const [word, lanes] of byWord) {
    const loopsOnly = lanes.every((l) => Boolean(l.console_refusal)) ? 'loops-only ' : '';
    parts.push(`${word} on ${lanes.length} ${loopsOnly}${lanes.length === 1 ? 'lane' : 'lanes'}`);
  }
  return `${parts.join(' · ')}.`;
}

/**
 * How a heartbeat reads: the absolute time AND the age beside it.
 *
 * Both, always. An age alone ("4m ago") cannot be compared against a log line,
 * and an absolute stamp alone makes a reader do arithmetic to find out whether
 * anything is wrong. A heartbeat that was never written says "never" — not
 * "0s ago", which is the opposite claim.
 */
export function heartbeatLabel(at: string | null, ageS: number | null): string {
  if (at === null || at === '') return 'never';
  const when = absoluteTime(at);
  if (when === NO_TIME) return 'never';
  return ageS === null ? when : `${when} · ${relativeAge(ageS)}`;
}

/**
 * `HH:MM:SS` in the reader's own zone, through the app's ONE clock formatter.
 *
 * Not a second zone of its own: `fmtTime` is the only place a clock reading is
 * formatted (`time.test.ts` enforces that), so a heartbeat on this page reads
 * exactly like a tick on the Epics list. A stamp that will not parse comes back
 * as `NO_TIME` rather than as something invented.
 */
export function absoluteTime(at: string): string {
  return fmtTime(at);
}

/** A duration in seconds as the terse age the factory writes elsewhere. */
export function relativeAge(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/**
 * The refresh control's own honesty: how stale the data on screen may be.
 *
 * The page cannot claim to be live — the host probes behind it are cached for up
 * to a minute — so it states an upper bound instead of a timestamp that implies
 * one. Rounded up, because "at most" has to stay true.
 *
 * Past two minutes the bound stops being useful ("at most 1066320 s old") and
 * the shared formatter rounds DOWN, which would make "at most" a lie. So an old
 * reading says when it was taken instead — "data was checked 12d 8h ago" — a
 * measurement that reads naturally rounded down.
 */
export function dataAgeLabel(checkedAt: string | null, now: number): string {
  if (checkedAt === null || checkedAt === '') return 'no data yet';
  const taken = new Date(checkedAt).getTime();
  if (Number.isNaN(taken)) return 'no data yet';
  const seconds = Math.max(0, Math.ceil((now - taken) / 1000));
  if (seconds < 120) return `data is at most ${seconds} s old`;
  return `data was checked ${formatDuration(now - taken)} ago`;
}

/**
 * Which of the two empty states a list is in.
 *
 * "Nothing configured" and "nothing matches the filter" are different facts and
 * need different sentences: one is a thing to set up, the other a filter to
 * clear. One blank box for both leaves the reader guessing which.
 */
export type EmptyKind = 'none-configured' | 'none-matching' | 'not-empty';

export function emptyKind(total: number, shown: number): EmptyKind {
  if (shown > 0) return 'not-empty';
  return total === 0 ? 'none-configured' : 'none-matching';
}

/** The section anchors the page offers, so a link can land on one. */
export const HEALTH_SECTIONS = [
  'runtimes',
  'images',
  'posture',
  'runners',
  'trackers',
  'daemons',
  'drivers',
  'budgets',
  'env'
] as const;

/**
 * The caps the Budgets section lists, in the order it lists them.
 *
 * Written out rather than filtered off a prefix: the order is the reader's
 * (money first, then rounds, then capacity), and `kew/tests/test_service_health
 * .py` holds the same set on the server as `CAP_FIELDS`.
 */
export const BUDGET_FIELDS: readonly string[] = [
  'budget.daily_usd',
  'budget.weekly_usd',
  'budget.per_issue_usd',
  'agent.max_budget_usd',
  'review.max_review_usd',
  'review.max_remediation_usd',
  'review.max_rounds',
  'review.stop_rule_after_rounds',
  'retry.max_attempts',
  'parallel.max_workers',
  'epic.max_parallel',
  'agent.timeout_minutes'
];

/** A config value as the page prints it — never an object dumped raw, never a bare dash. */
export function fieldValueLabel(value: unknown): string {
  if (value === null || value === undefined) return 'not set';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

/**
 * The Daemons section's note in words (#3470): a count is said only when it
 * is not zero (DESIGN.md), so an idle host reads "no loop pursuing" rather
 * than "0 loops pursuing · 0 stale".
 */
export function loopsNote(pursuing: number, stale: number): string {
  const lead = pursuing > 0 ? `${pursuing} loop${pursuing === 1 ? '' : 's'} pursuing` : 'no loop pursuing';
  return stale > 0 ? `${lead} · ${stale} stale` : lead;
}
