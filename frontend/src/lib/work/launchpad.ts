/**
 * #2302 — "Plan a dispatch": the resolved route as rows, and the exact command.
 *
 * Every word on the launchpad is decided here, so the panel is a renderer. The
 * rule the file exists for is the one the artboard states as a row:
 *
 *     runner   codex    ← kew.toml [routing].simple
 *
 * The arrow is the provenance the backend sends with each field. Nothing in
 * this module derives a route, a posture or a cap: the page would then be a
 * second resolver, free to disagree with the one that launches, which is the
 * failure the whole endpoint exists to avoid.
 *
 * Three things it does decide, because they are about reading rather than about
 * routing:
 *
 * * **The state of each row** — set and valid, default, overridden, or invalid
 *   — which is what makes "this is not what you think it is" visible without a
 *   second colour vocabulary. An invalid row carries `fix`: where the operator
 *   goes to make it valid.
 * * **Ready or refused** (#2535): the closed panel's one-line verdict, a
 *   phrase per gate the real dispatch applies — never a route decision.
 * * **Why Launch is disabled**, as sentences. The last of them is always the
 *   same one: this console does not dispatch. Copying the command and running
 *   it is the act, which is the kew rule that a launch is a deliberate thing a
 *   person does in a terminal.
 */
import type { DispatchPlan } from '$lib/api';
import { modelName } from '$lib/factory/names';

/** The four readings a row can have (the issue's own four words). */
export type RowState = 'set' | 'default' | 'overridden' | 'invalid';

/** Where an invalid row sends the operator. `focus` is an element on screen. */
export type RowFix = { label: string; href?: string; focus?: string };

export type PlanRow = {
  /** The field name, as the row's left column prints it. */
  field: string;
  /** The resolved value. `—` where there is nothing to print. */
  value: string;
  /** The rule that decided it, printed after the arrow. */
  source: string;
  state: RowState;
  /** Set only on an invalid row: what fixes it. */
  fix: RowFix | null;
  /**
   * Set only on an invalid row: what is wrong, in a phrase (#2535). The band
   * prints this and keeps `source` — a refusal can be a paragraph — behind its
   * `details` toggle.
   */
  brief?: string;
};

/** The id the complexity control carries, so a fix can focus it. */
export const COMPLEXITY_CONTROL_ID = 'plan-complexity';

const HEALTH_IMAGES: RowFix = { label: 'Health · agent images', href: '/health#images' };
const HEALTH_POSTURE: RowFix = { label: 'Health · posture by lane', href: '/health#posture' };
const BUDGET: RowFix = { label: 'Budget', href: '/budget' };

/**
 * Which state a `source` string alone implies.
 *
 * An override is the one layer that must never read as "configured": the value
 * on screen is a DB override's or an environment variable's, and sending the
 * operator to the `kew.toml` line would be sending them to a line that is not
 * what is running.
 */
export function sourceState(source: string): RowState {
  if (source.startsWith('DB override') || source.startsWith('env (')) return 'overridden';
  // "claude-code's own default" is the reviewer lane's unpinned model: a
  // default like any other, and named after the runner because that is whose
  // default it is.
  if (source.startsWith('default') || source.endsWith('own default')) return 'default';
  return 'set';
}

const money = (usd: number): string =>
  usd === Math.trunc(usd) ? `$${usd}` : `$${usd.toFixed(2)}`;

/** `$0.00`, or a dash — never a reassuring zero for spend nobody could read. */
const spent = (usd: number | null): string => (usd === null ? '—' : `$${usd.toFixed(2)}`);

function complexityRow(plan: DispatchPlan): PlanRow {
  // A tier nobody named is the default lane's, and reads as exactly that
  // (#2785): `default`, never red. The server already takes the ready queue's
  // label when the item has one, and resolves `medium` — as `kew dispatch`
  // does — for a queued item with none, so what is left unknown here is an
  // item the queue does not hold, whose labels nobody has read. It was
  // `invalid` with a "choose a complexity" fix, which painted a red row on
  // every item for a choice the dispatch itself does not need.
  if (!plan.complexity.known) {
    return {
      field: 'complexity',
      value: 'default',
      source: plan.complexity.source,
      state: 'default',
      fix: null
    };
  }
  return {
    field: 'complexity',
    value: plan.complexity.value,
    source: plan.complexity.source,
    state: 'set',
    fix: null
  };
}

function postureRow(plan: DispatchPlan): PlanRow {
  const { posture, refusal_reason, runtime } = plan.containment;
  if (refusal_reason !== null) {
    return {
      field: 'posture',
      value: 'refused',
      source: refusal_reason,
      state: 'invalid',
      fix: HEALTH_POSTURE,
      brief: 'refused'
    };
  }
  return {
    field: 'posture',
    // The runtime is part of the answer: "contained" says nothing about where.
    value: posture === 'uncontained' ? posture : `${posture} · ${runtime}`,
    source: plan.containment.source,
    // Uncontained is a configured posture, not a broken one — but it is only
    // ever reached by an explicit `dangerously_skip_containment`, so it reads
    // as what it is: an override of the default.
    state: posture === 'uncontained' ? 'overridden' : 'set',
    fix: null
  };
}

/**
 * What the two host rows say when the posture is `refused` (#2445).
 *
 * A refused route launches nowhere: the resolution stopped before it had an
 * image or handed a credential over, so there is no answer to print for
 * either. Printing the one the ABSENT route's defaults imply — `none ← this
 * route launches on the host, not in an image`, `cli login ← … the runner logs
 * in itself` — described a host launch that will not happen, under a posture
 * row that said the lane cannot run at all.
 */
const NOT_RESOLVED = 'not resolved: the posture is refused';

/** Whether this plan's lane was refused before any route was resolved. */
const refused = (plan: DispatchPlan): boolean => plan.containment.refusal_reason !== null;

function imageRow(plan: DispatchPlan): PlanRow {
  const { image, image_present } = plan.containment;
  if (refused(plan)) {
    // A dash, not `none`: `none` is an answer ("this route launches on the
    // host"), and no image was resolved either way.
    return {
      field: 'image',
      value: '—',
      source: NOT_RESOLVED,
      state: 'default',
      fix: null
    };
  }
  if (image === null) {
    return {
      field: 'image',
      value: 'none',
      source: 'this route launches on the host, not in an image',
      state: 'default',
      fix: null
    };
  }
  return {
    field: 'image',
    value: image,
    source:
      image_present === null
        ? 'presence not probed yet'
        : image_present
          ? 'present on this host'
          : 'NOT built on this host',
    state: image_present === false ? 'invalid' : 'set',
    fix: image_present === false ? HEALTH_IMAGES : null,
    ...(image_present === false ? { brief: 'not built on this host' } : {})
  };
}

function credentialRow(plan: DispatchPlan): PlanRow {
  const names = plan.containment.credential_names_present;
  const set = names.filter((c) => c.set);
  if (refused(plan)) {
    // Which names are set in this process is a fact about the environment and
    // stays printable; what the runner WOULD do with them is not, because it
    // will not get the chance. The missing credential is itself the commonest
    // refusal, so this row's old "the runner logs in itself" contradicted the
    // very reason printed one row above it.
    const held = set.map((c) => c.name).join(', ');
    return {
      field: 'credential',
      value: held || (names.length > 0 ? 'none set' : '—'),
      source: held === '' ? NOT_RESOLVED : 'set in this process',
      // Never a second red row: the posture row above carries the refusal and
      // the one link that fixes it.
      state: 'default',
      fix: null
    };
  }
  if (names.length === 0) {
    return {
      field: 'credential',
      value: 'none declared',
      source: 'this runner declares no in-container credential',
      state: 'default',
      fix: null
    };
  }
  // Names only, never a value. An unset credential is NOT an invalid row: the
  // subscription path authenticates through the runner's own CLI login, and a
  // red row for the common case is an alarm invented out of a missing variable.
  return {
    field: 'credential',
    value: set.length > 0 ? set.map((c) => c.name).join(', ') : 'cli login',
    source:
      set.length > 0
        ? 'set in this process'
        : `${names.map((c) => c.name).join(', ')} not set — the runner logs in itself`,
    state: set.length > 0 ? 'set' : 'default',
    fix: null
  };
}

function liveRow(plan: DispatchPlan): PlanRow {
  const { value, source } = plan.live_status;
  return {
    field: 'live boundary',
    value,
    source,
    // `unverified` is an honest unknown — no probe has crossed this leg — and
    // never a fault. `refuted` is: a call across it has failed.
    state: value === 'refuted' ? 'invalid' : value === 'confirmed' ? 'set' : 'default',
    fix: value === 'refuted' ? HEALTH_POSTURE : null,
    ...(value === 'refuted' ? { brief: 'refuted' } : {})
  };
}

function governorRow(plan: DispatchPlan): PlanRow {
  const { would_swap, reason, recommended_runner, recommended_model, est_savings_usd } =
    plan.governor;
  return {
    field: 'governor',
    value: would_swap
      ? `would swap to ${recommended_runner} / ${recommended_model}`
      : 'no swap',
    source: would_swap ? `${reason} · saves ${money(est_savings_usd)}` : reason,
    // Advice is never a fault, and a swap it would apply is not an error in the
    // route — it is the route the launch will take, which the row says plainly.
    state: would_swap ? 'overridden' : 'default',
    fix: null
  };
}

function capRow(plan: DispatchPlan): PlanRow {
  const { runtime_cap_usd, runtime_cap_source, runtime_cap_exceeds_issue_headroom } = plan.budget;
  const state: RowState = runtime_cap_exceeds_issue_headroom
    ? 'invalid'
    : runtime_cap_source === 'override'
      ? 'overridden'
      : runtime_cap_source === 'default'
        ? 'default'
        : 'set';
  return {
    field: 'run cap',
    value: money(runtime_cap_usd),
    source: runtime_cap_exceeds_issue_headroom
      ? `${runtime_cap_source} — one run can overshoot the per-issue headroom`
      : runtime_cap_source,
    state,
    fix: runtime_cap_exceeds_issue_headroom ? BUDGET : null,
    ...(runtime_cap_exceeds_issue_headroom
      ? { brief: 'can overshoot the per-issue headroom' }
      : {})
  };
}

function headroomRow(plan: DispatchPlan): PlanRow {
  const { usd, binds, source } = plan.admission.headroom;
  const exhausted = usd !== null && usd <= 0;
  return {
    field: 'headroom',
    value: usd === null ? '—' : `${money(usd)}${binds ? ` (${binds})` : ''}`,
    source,
    state: exhausted ? 'invalid' : usd === null ? 'default' : 'set',
    fix: exhausted ? BUDGET : null,
    ...(exhausted ? { brief: 'exhausted' } : {})
  };
}

/**
 * One row per resolved field, in the order the panel lists them: what runs,
 * where it runs, what is known about that boundary, and what it may spend.
 */
export function planRows(plan: DispatchPlan | null): PlanRow[] {
  if (plan === null) return [];
  // `||`, not `??`: an unpinned reviewer model comes back as the empty string,
  // and a row with nothing in its value column reads as a rendering bug.
  const field = (name: string, f: { value: string | null; source: string }): PlanRow => ({
    field: name,
    value: f.value || '—',
    source: f.source,
    state: f.value ? sourceState(f.source) : 'default',
    fix: null
  });
  return [
    complexityRow(plan),
    field('lane', plan.lane),
    field('runner', plan.runner),
    field('model', plan.model),
    field('transport', plan.transport),
    postureRow(plan),
    imageRow(plan),
    credentialRow(plan),
    liveRow(plan),
    governorRow(plan),
    capRow(plan),
    headroomRow(plan),
    {
      field: 'spend so far',
      value: `${spent(plan.budget.issue_spent_usd)} this item · ${spent(plan.budget.daily_spent_usd)} today`,
      source: plan.budget.source,
      state: 'set',
      fix: null
    }
  ];
}

/**
 * Which mark each row state borrows, from the factory's one table.
 *
 * No new shape and no colour this panel picked for itself: a valid row is the
 * green dot, a default is the dashed "expected, nothing set here" ring, an
 * override is the amber held ring — somebody reached in and changed it, which
 * is the same family as money and held — and an invalid row is the red × this
 * app already uses for a missing guarantee.
 */
export const STATE_MARK: Record<RowState, string> = {
  set: 'success',
  default: 'queued',
  overridden: 'held',
  invalid: 'failure'
};

/** The word beside the mark. Terse, and never an adjective of approval. */
export const STATE_LABEL: Record<RowState, string> = {
  set: 'set',
  default: 'default',
  overridden: 'overridden',
  invalid: 'invalid'
};

/** Every invalid row, for the summary line above the rows. */
export const invalidRows = (rows: PlanRow[]): PlanRow[] => rows.filter((r) => r.state === 'invalid');

/**
 * The rows the opened plan shows before "show all fields" (#2535): the ones
 * that are NOT what the operator would assume — overridden or invalid. A row
 * that is set or default is the expected answer, and fourteen of them in a
 * column buried the one that was not. The route itself is not lost with them:
 * the drawer prints runner and model above the table.
 */
export const attentionRows = (rows: PlanRow[]): PlanRow[] =>
  rows.filter((r) => r.state === 'overridden' || r.state === 'invalid');

/**
 * The route in one phrase: `runner / model`, each exactly as resolved (#2535).
 *
 * The model is the id the resolver returned, character for character — no
 * alias folded, no family shortened — so `claude-opus-5-5` reads as itself
 * and never as the `claude-opus-5` it begins with.
 */
export function routeSummary(plan: DispatchPlan): string {
  return `${plan.runner.value || '—'} / ${plan.model.value || '—'}`;
}

/**
 * The route as the closed line prints it (#2785): the model by its name
 * (`codex / GPT-5.6 Sol`). `routeSummary`, the id verbatim, is its title.
 */
export function routeName(plan: DispatchPlan, override = ''): string {
  const model = override || plan.model.value;
  const name = model === '' ? '—' : modelName(model);
  return `${plan.runner.value || '—'} / ${name}${override === '' ? '' : ' (override)'}`;
}

/** Ready, or refused with the one phrase that says why. */
export type Verdict = { ready: true; reason: '' } | { ready: false; reason: string };

/**
 * The plan's verdict for the closed one-line summary (#2535).
 *
 * Refused is what the real dispatch would refuse, most specific first, in the
 * same order `launchBlockers` lists them — one phrase each, because the line
 * has room for a word, not the remedy. The remedy is in the opened plan. An
 * unknown tier or an override is not a refusal: the dispatch reads the tier
 * off the item, and an override is a route like any other.
 */
export function planVerdict(plan: DispatchPlan): Verdict {
  const { fleet_stopped, open_hold, headroom } = plan.admission;
  if (fleet_stopped) return { ready: false, reason: 'fleet stopped' };
  if (open_hold === true) return { ready: false, reason: 'approval hold open' };
  if (headroom.usd !== null && headroom.usd <= 0) {
    return { ready: false, reason: 'no budget headroom' };
  }
  if (refused(plan)) return { ready: false, reason: 'posture refused' };
  if (plan.containment.image_present === false) {
    return { ready: false, reason: 'image not built' };
  }
  return { ready: true, reason: '' };
}

/**
 * The exact command, or `''` when this plan has no hand command at all.
 *
 * `kew dispatch` takes an issue number and reads the complexity off the item's
 * own labels — there is no `--complexity` flag — so the command carries no tier
 * and the selected one is a what-if (`commandNote` says so). A non-numeric
 * tracker key and the two reviewer lanes have no single command to print, and
 * printing an invented one is worse than printing none: a command parsed or
 * guessed by a surface is the one thing DESIGN.md forbids outright.
 */
export function dispatchCommand(plan: DispatchPlan | null): string {
  if (plan === null) return '';
  if (plan.lane.value !== 'implement') return '';
  if (plan.issue_num === null) return '';
  return `kew dispatch ${plan.issue_num}`;
}

/**
 * What runs and what picked it, then what the command does NOT carry (#3467
 * D31). The model and its source are the plan's own fields: "this plans
 * simple" read as if the tier chose the model while the drawer above it named
 * `kew.toml [routing].simple` as the layer that did. An unknown tier says
 * nothing — the dispatch reads it off the item, so no model here is the one
 * that will run. An override names itself, since it is what Run sends.
 */
export function commandNote(plan: DispatchPlan | null, override = ''): string {
  if (plan === null || dispatchCommand(plan) === '') return '';
  if (!plan.complexity.known) return '';
  const tier = "the dispatch reads the tier from the item's labels";
  if (override !== '') return `runs ${override}, the override this dispatch sends; ${tier}`;
  const { value, source } = plan.model;
  const runs = value === '' ? `runs ${source}` : `runs ${value} from ${source}`;
  return `${runs}; ${tier}`;
}

/** The sentence a lane with no hand command gets instead of one. */
export function noCommandReason(plan: DispatchPlan | null): string {
  if (plan === null) return '';
  if (plan.lane.value !== 'implement') {
    return `the ${plan.lane.value} lane is launched by the epic driver's review stage, not by a dispatch command`;
  }
  if (plan.issue_num === null) {
    return 'kew dispatch takes a GitHub issue number; this item has a tracker key';
  }
  return '';
}

/**
 * What stands in a dispatch's way, most specific first, under the drawer's Run. `brief` names a refusal rather
 * than printing its remedy, for a surface that prints the remedy elsewhere.
 *
 * The last line is always there, and is the point: this console never
 * dispatches itself — a Run is a request the drive carries out (#2537). Everything above it is a gate the real dispatch would apply
 * anyway, said before the operator spends a trip to the terminal on it.
 */
export function launchBlockers(
  plan: DispatchPlan | null,
  { brief = false }: { brief?: boolean } = {}
): string[] {
  if (plan === null) return ['the plan could not be read'];
  const reasons: string[] = [];
  const { fleet_stopped, fleet_stopped_reason, open_hold, headroom } = plan.admission;
  if (fleet_stopped) {
    reasons.push(
      fleet_stopped_reason
        ? `the fleet is stopped — ${fleet_stopped_reason}`
        : 'the fleet is stopped'
    );
  }
  if (open_hold === true) reasons.push('an approval hold on this item is still open');
  if (headroom.usd !== null && headroom.usd <= 0) {
    reasons.push(`no budget headroom left (${headroom.binds ?? 'budget'})`);
  }
  if (plan.containment.refusal_reason !== null) {
    // "here" is the console's own process, which is what the resolution read:
    // a credential absent from `kew serve`'s environment may well be in the
    // shell the operator pastes the command into, and this panel must not
    // claim their command will fail (#2445).
    // `brief` is the opened plan's list (#2535): the remedy is already one
    // `details` away on the posture line above it, so it is not printed twice.
    reasons.push(
      `this lane cannot run in the console's process — ${
        brief ? 'the posture is refused' : plan.containment.refusal_reason
      }`
    );
  }
  const noCommand = noCommandReason(plan);
  if (noCommand !== '') reasons.push(noCommand);
  reasons.push('the console never dispatches itself — Run hands the command to the drive, or copy it and run it');
  return reasons;
}

/**
 * Which plan read owns the screen, by monotonic request id.
 *
 * The launchpad revalidates as choices change, so two reads are in flight the
 * moment an operator clicks twice: complexity=simple, then complexity=complex.
 * Nothing can unsend the first `fetch`, so the guard is not cancellation — it
 * is that only the newest ticket's answer is believed, and a slow old one is
 * dropped on arrival. Without it the screen settles on whichever response the
 * network happened to deliver last, which is a route the operator did not ask
 * for shown as the route that would run.
 */
export class PlanRequests {
  #latest = 0;

  /** Take a read, handing back its ticket. */
  begin(): number {
    this.#latest += 1;
    return this.#latest;
  }

  /** Whether `ticket`'s answer is still the one the screen is waiting for. */
  owns(ticket: number): boolean {
    return ticket === this.#latest;
  }
}
