/**
 * FUI-22 — what the Monitor readout row says, decided in one pure place.
 *
 * Four cells across the top of `/monitor` (DMonitor x 40–1400, y ≈88–258):
 * Working, Needs you, Today, This week. The page is a renderer; every number
 * and every word in it is built here, where a vitest can hold it to the
 * artboard and to the money rule.
 *
 * Two rules run through the whole file.
 *
 * **N5: the gauge reads what the governor enforces.** `SpendCell` carries
 * `spent` (everything observed in the window) and `agentic` (the portion the
 * budget governor actually gates). A gauge drawn from `spent` would show a
 * fleet at 80% of cap that the governor sees at 40%, and would refuse nothing
 * at the moment the bar filled. So the figure and the gauge read `agentic`,
 * and `spent` is not read at all.
 *
 * **A reading that could not be taken is not a zero.** This is the factory
 * header's discipline (`header-state.ts`) carried to a surface with four
 * independent sources: each one fails on its own, prints the em dash rather
 * than a number, and says which source failed. `/api/config/budget` going down
 * must not blank the spend figures beside it, and a spend read that never
 * arrived must not render as "$0 spent today", which is a lie that reads as
 * good news.
 */
import type { BudgetMap, Dashboard, DashboardRow, SpendCell, SpendWindows } from '$lib/api';
import { itemIdOf, orderRows, typeLabel } from '$lib/dashboard';
import type { ReadoutTone } from '$lib/factory/classes';
import { gaugeMoney, gaugeState, type GaugeValueMarker } from '$lib/factory/gauge';
import { workHref } from '$lib/monitor/live-runs';
import { rowSummary } from '$lib/needs-you/summary';
import { safeHref } from '$lib/safe-href';
import {
  countLabel,
  headerCounts,
  isWorkingRun,
  JUDGMENT_ROW_SOURCES,
  UNREADABLE
} from '$lib/shell/header-state';
import { itemRef } from '$lib/utils';
import { epicHref } from '$lib/work/refs';

/** A read that did not happen, carrying what to print instead of a number. */
export type Unavailable = { available: false; reason: string };

/** A payload, or the reason there isn't one. */
export type Source<T> = T | Unavailable;

/**
 * The budget fields whose `file_value` is the prior cap of each window. The
 * *effective* cap is read from the dashboard's `glance.caps`, which already
 * resolves the override; the budget map is consulted for one thing only, the pin showing
 * what the cap was before somebody raised it.
 */
export const DAILY_CAP_FIELD = 'budget.daily_usd';
export const WEEKLY_CAP_FIELD = 'budget.weekly_usd';

/**
 * What the two pulse-backed cells say when they have no payload.
 *
 * "not read" rather than "failed": the shell's poll keeps no error, and its
 * first tick has not landed when the page mounts, so an unread source and a
 * broken one look identical from here. Neither is a zero, and claiming a
 * failure we did not observe would be its own small lie.
 */
export const RUNS_UNREAD = 'runs not read';
export const QUEUE_UNREAD = 'judgment queue not read';
export const SPEND_UNREAD = 'spend not read';

export function isUnavailable<T>(source: Source<T>): source is Unavailable {
  return (
    typeof source === 'object' && source !== null && (source as Unavailable).available === false
  );
}

/**
 * A failed read, said in words. `api.ts` throws `Error('<path> -> <status>')`,
 * so a status code is recoverable and worth printing — "spend unavailable
 * (HTTP 503)" tells an operator the serve answered and refused, where a bare
 * "spend unavailable" is also what a pulled network cable looks like.
 */
export function unreadable(subject: string, err: unknown): Unavailable {
  const status = err instanceof Error ? /-> (\d{3})$/.exec(err.message)?.[1] : undefined;
  return {
    available: false,
    reason: `${subject} unavailable${status ? ` (HTTP ${status})` : ''}`
  };
}

/**
 * One run of caption text. A caption is a list of these so the item's own
 * reference can be a link *inside* the server's sentence ("merge decision on
 * [PR #2059]") instead of being bolted onto the end of it.
 *
 * `count` marks the " and N more" a caption ends on (#2847 P10): the page
 * clamps the sentence before it to one line and never clips the count, which
 * is the one part of the caption that says how big the queue is.
 */
export type CaptionSegment = { text: string; href: string | null; count?: true };

/** The caption as one string, for tests and for anything that cannot link. */
export function captionText(caption: readonly CaptionSegment[]): string {
  return caption.map((segment) => segment.text).join('');
}

export type Readout = {
  /** The 11px label: "Working", "Needs you", "Today", "This week". */
  label: string;
  /** What the figure means, which is what colours it. */
  tone: ReadoutTone;
  /** The 44px figure, or `UNREADABLE` when the source could not be read. */
  figure: string;
  /** The 13px measurement right of the label; '' when there is none. */
  meta: string;
  /** The 15px caption under the figure; empty when there is nothing to say. */
  caption: CaptionSegment[];
  /**
   * What the caption says when its sentence does not fit on its line: the
   * queue by kind ("1 merge decision · 1 hold", #3052 N3). Only the Needs you
   * cell carries one; a cut sentence ("PR #2059 is open —…") said nothing.
   */
  summary?: CaptionSegment[];
};

export type SpendReadout = Readout & {
  /** The 15px "/ $250" trailing the figure; '' when there is no reading. */
  capLabel: string;
  /** `meta` as a 390px cell can hold it on one line ("31.5% cap", #2896 R12). */
  metaShort: string;
  /** The exact reading behind a bounded `meta` ("×1000000.0 over cap"), as a title. */
  metaTitle: string;
  /** What `Gauge.svelte` draws, or `null` when there is nothing to draw. */
  gauge: { spent: number; cap: number | null; marker: GaugeValueMarker | null } | null;
};

/** The em dash cell shared by every unreadable source. */
function unread(label: string, tone: ReadoutTone, reason: string): Readout {
  return { label, tone, figure: UNREADABLE, meta: '', caption: [{ text: reason, href: null }] };
}

// --- Today and This week ----------------------------------------------------

/**
 * The two spend windows, as the shell's pulse last read them (#2847 Q2).
 *
 * The Monitor polled `/api/report/spend` on the same 7s beat the pulse reads
 * `/api/dashboard`, whose `glance.caps` is the same two windows — two reads of
 * one figure every beat, per tab, from two config sources that could disagree.
 * The serve now builds both from one (`kew.service.report.governed_spend_windows`)
 * and this page reads the one the pulse already holds.
 *
 * A dashboard that answered without its caps (fail-soft, `caps` named in
 * `unavailable`) is "spend unavailable"; one not read yet is "not read", for
 * the reason `RUNS_UNREAD` gives.
 */
export function spendFromPulse(dashboard: Dashboard | null): Source<SpendWindows> {
  if (dashboard === null) return { available: false, reason: SPEND_UNREAD };
  return dashboard.glance?.caps ?? unreadable('spend', null);
}

/**
 * A spend window against its cap.
 *
 * `marker` is the prior cap, when one applies; it is passed in rather than
 * read here so a broken `/api/config/budget` costs the row its pin and nothing
 * else — the figures either side of it come from a different endpoint and are
 * still true.
 */
export function spendReadout(
  label: string,
  cell: Source<SpendCell>,
  marker: GaugeValueMarker | null = null
): SpendReadout {
  if (isUnavailable(cell)) {
    return {
      ...unread(label, 'money', cell.reason),
      capLabel: '',
      metaShort: '',
      metaTitle: '',
      gauge: null
    };
  }
  // N5. `cell.spent` is deliberately not read: see the file header.
  const enforced = cell.agentic;
  const state = gaugeState(enforced, cell.cap, { marker });
  return {
    label,
    tone: 'money',
    figure: gaugeMoney(enforced),
    // Without a cap there is no ratio to print, so the same two words stand in
    // for both the denominator and the reading, exactly as CapBar says it.
    capLabel: cell.cap == null ? 'no cap' : gaugeMoney(cell.cap),
    meta: state.caption,
    metaShort: state.short,
    metaTitle: state.exact,
    caption: [],
    gauge: { spent: enforced, cap: cell.cap, marker }
  };
}

/**
 * The superseded cap of one budget window, or `null` when the cap in force is
 * the one the config file declares.
 *
 * The pin exists to answer "who moved this, and from what" at a glance, so it
 * is drawn if and only if an override is in force — `file_value` on its own is
 * just the current cap said twice.
 */
export function priorCap(budget: Source<BudgetMap>, field: string): GaugeValueMarker | null {
  if (isUnavailable(budget)) return null;
  const entry = budget[field];
  if (!entry || entry.override == null) return null;
  return { value: entry.file_value, label: 'prior cap' };
}

// --- Working ----------------------------------------------------------------

/**
 * A run as this row reads it: enough to say whether it is working, plus the
 * stage it is executing. FUI-13 serves `stage` on every run, so the served
 * shape always has the field — it is still optional here because an older
 * serve on the other end of `/api/runs` omits it, and that is the same "no
 * stage reported" the serve now says in words.
 */
export type StageRun = {
  status: string;
  started_at: string | null;
  /** One of `RUN_STAGES`; absent from an older serve. See `UNNAMED_STAGE`. */
  stage?: string | null;
};

/**
 * The stage word the serve sends for a run kind it has no stage word for
 * (`RUN_STAGES`' `unknown`). It is a real answer, not a missing field, and it
 * is not a stage: counting it as one would caption a fleet "two in unknown",
 * naming a stage kew does not have. It reads as "not reported", exactly like
 * the absent field an older serve sends.
 *
 * Exported because the live-runs table below this caption (FUI-23) draws the
 * same token as an em dash: the two Monitor surfaces that read a stage must
 * agree on which word means "no stage was reported".
 */
export const UNNAMED_STAGE = 'unknown';

/** Stage → the agent doing it, singular and plural, as DMonitor names them. */
const STAGE_NOUN: Record<string, [string, string]> = {
  review: ['reviewer', 'reviewers'],
  implement: ['implementer', 'implementers']
};

/** DMonitor spells its small counts. Above nine, digits read better anyway. */
const NUMBER_WORD = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine'];

function word(n: number): string {
  return NUMBER_WORD[n] ?? String(n);
}

/**
 * The stage bucket a run falls in. FUI-13 serves the round separately, in the
 * run's `stage_detail`, because the round number belongs in the live-runs
 * table (FUI-23) and not in a one-line summary of the whole fleet — so the
 * first word is taken here, and a compound stage from any other serve buckets
 * with the bare one rather than becoming a stage of its own.
 */
function stageKey(stage: string): string {
  return stage.trim().split(/\s+/)[0].toLowerCase();
}

/**
 * "one reviewer, one implementer" — the working runs by stage.
 *
 * Stages kew has a word for come first, in the artboard's order; anything else
 * is named by its own stage word rather than an invented noun. Runs whose
 * stage was not reported — absent, blank, or the served `unknown` — are
 * counted too, so the caption always accounts for every run in the figure
 * above it and can never read as a smaller fleet than the one running.
 */
function workingCaption(runs: readonly StageRun[]): string {
  if (runs.length === 0) return '';
  const counts = new Map<string, number>();
  let unstaged = 0;
  for (const run of runs) {
    const key = run.stage ? stageKey(run.stage) : '';
    if (key === '' || key === UNNAMED_STAGE) unstaged += 1;
    else counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  if (counts.size === 0) return 'stage not reported';
  const known = Object.keys(STAGE_NOUN).filter((key) => counts.has(key));
  const rest = [...counts.keys()].filter((key) => !(key in STAGE_NOUN)).sort();
  const parts = [...known, ...rest].map((key) => {
    const n = counts.get(key)!;
    const noun = STAGE_NOUN[key];
    return `${word(n)} ${noun ? noun[n === 1 ? 0 : 1] : `in ${key}`}`;
  });
  if (unstaged > 0) parts.push(`${word(unstaged)} not reported`);
  return parts.join(', ');
}

/**
 * Runs currently working, over a caption naming what they are doing.
 *
 * The count is not defined here: `isWorkingRun` is the factory header's own
 * predicate, so the 44px figure on Monitor and the "2 working" in the header
 * above it cannot drift apart.
 */
export function workingReadout(runs: Source<readonly StageRun[]>): Readout {
  if (isUnavailable(runs)) return unread('Working', 'working', RUNS_UNREAD);
  const live = runs.filter(isWorkingRun);
  const caption = workingCaption(live);
  return {
    label: 'Working',
    tone: 'working',
    figure: String(live.length),
    meta: '',
    caption: caption ? [{ text: caption, href: null }] : []
  };
}

// --- Needs you --------------------------------------------------------------

/** How the top judgment row refers to itself: its PR, or its item id. */
function rowRef(row: DashboardRow): string {
  return row.pr_number != null ? `PR #${row.pr_number}` : itemRef(row.item_id, row.issue_num);
}

/**
 * One sentence with the row's own reference linked inside it.
 *
 * The sentence already names the row ("merge decision on PR #2059"), so the
 * link is threaded through it rather than appended — an appended link would
 * print the reference twice. When the sentence does not name the row, the whole
 * of it becomes the link instead, which still gets the reader to the item.
 */
function threadRef(text: string, ref: string, href: string | null): CaptionSegment[] {
  if (!href) return [{ text, href: null }];
  const at = ref ? text.indexOf(ref) : -1;
  if (at === -1) return [{ text, href }];
  return [
    { text: text.slice(0, at), href: null },
    { text: ref, href },
    { text: text.slice(at + ref.length), href: null }
  ].filter((segment) => segment.text !== '');
}

/**
 * One row's sentence with the row's own reference linked, wherever the caller
 * sends that reference.
 *
 * The sentence is `rowSummary`'s and not this file's (#2245), so the two cells
 * of Monitor that print one row print one reading of it. Only the destination
 * differs between them, which is the argument.
 */
function summaryCaption(row: DashboardRow, href: string | null): CaptionSegment[] {
  return threadRef(rowSummary(row) || typeLabel(row.type, 1), rowRef(row), href);
}

/**
 * The row's sentence with its own reference linked, outward to whatever the
 * row's `url` names.
 *
 * Exported for the holds column (FUI-24), which prints the same sentences for
 * the same rows and must link them the same way. The served `url` is a link
 * only through `safeHref` (#3470 C10): a refused one leaves the sentence as
 * text.
 */
export function rowCaption(row: DashboardRow): CaptionSegment[] {
  return summaryCaption(row, safeHref(row.url));
}

/**
 * Where the Needs you caption's link goes: the Work row that can act on this
 * item, else wherever the entry actually lives.
 *
 * The caption used to link `url`, which is a pull request on GitHub — one click
 * out of the console onto a page that cannot clear a hold. The artboard draws
 * this cell's link as `DWork.html`, its own stand-in for `/work`, which is the
 * same decision.
 *
 * A row naming no item cannot be selected there, and the fallbacks are the ones
 * `work/list.ts`'s `entryHref` gives those same entries: the artifact the row
 * named, else its epic. An orphan PR carries a `url` and no item, and dropping
 * to no link at all would have taken away the one reference it has. The
 * `/monitor` that chain ends on there is no use from here: this cell is on it.
 * The served `url` passes `safeHref` (#3470 C10); a refused one falls through
 * to the epic as if the row carried none.
 */
function captionHref(row: DashboardRow): string | null {
  const id = itemIdOf(row) ?? '';
  // `/work?item=0` is a selection the Work screen cannot key (`live-runs.ts`).
  if (id !== '' && id !== '0') return workHref(id);
  const url = safeHref(row.url);
  if (url) return url;
  return row.epic_ref ? epicHref(row.epic_ref) : null;
}

/**
 * The caption under the Needs you figure: the top row said in one sentence,
 * then how many rows it stands in front of.
 *
 * The sentence is `rowSummary`'s, not this file's (#2245). Monitor printed
 * `reason` verbatim here while the Holds column two cells away printed the same
 * row's reason in words, so one row read `review_rounds_exhausted` and "the
 * review round cap was reached" on one screen.
 *
 * "and N more" counts the rows this one is standing in for. Without it the
 * caption describes the top row beneath a figure of 12, which reads as a
 * description of all twelve. `served` is the figure's own count and not the
 * length of `rows`: the figure counts every row the set carries
 * (`headerCounts`, #2207) while `orderRows` drops the types this client has no
 * word for, so counting the rows in hand here would put two queue sizes in one
 * cell the day the serve adds a row type.
 */
function needsYouCaption(rows: readonly DashboardRow[], served: number): CaptionSegment[] {
  const top = rows[0];
  if (top === undefined) return [];
  const caption = summaryCaption(top, captionHref(top));
  const more = served - 1;
  if (more > 0) caption.push({ text: ` and ${more} more`, href: null, count: true });
  return caption;
}

/**
 * What one kind of row is called in the caption's summary, singular and plural.
 *
 * The queue's own type words (`typeLabel`) where they read as what a person
 * decides; two of them do not. A `pr_review` row is a merge decision — that is
 * the sentence the server writes on it — and an `epic_wait` or a budget hold is
 * a hold, the word the Holds column beside this cell lists them under.
 */
const SUMMARY_KIND: Record<string, [string, string]> = {
  pr_review: ['merge decision', 'merge decisions'],
  epic_wait: ['hold', 'holds'],
  budget_hold: ['hold', 'holds']
};

/** How many kinds the summary names before it counts the rest. */
const SUMMARY_KINDS = 2;

function kindWord(type: string, count: number): string {
  const pair = SUMMARY_KIND[type];
  return pair ? pair[count === 1 ? 0 : 1] : typeLabel(type, count);
}

/**
 * The queue by kind, in the order its rows sort: "1 merge decision · 1 hold"
 * (#3052 N3). The caption's sentence clamps to one line, and at 1440 the
 * fixture's read "PR #2059 is open —…" — a cut that names nothing. This never
 * clips: it names the kinds, links the first to the top row as the sentence
 * would have, and counts what it does not name, including the rows `orderRows`
 * dropped for a type this client has no word for, so its numbers add up to the
 * figure above it.
 */
function needsYouSummary(rows: readonly DashboardRow[], served: number): CaptionSegment[] {
  const top = rows[0];
  if (top === undefined) return [];
  const kinds = new Map<string, { type: string; count: number }>();
  for (const row of rows) {
    const key = kindWord(row.type, 1);
    const kind = kinds.get(key) ?? { type: row.type, count: 0 };
    kind.count += 1;
    kinds.set(key, kind);
  }
  const named = [...kinds.values()].slice(0, SUMMARY_KINDS);
  const parts: CaptionSegment[] = named.map(({ type, count }, i) => ({
    text: `${count} ${kindWord(type, count)}`,
    href: i === 0 ? captionHref(top) : null
  }));
  const rest = served - named.reduce((sum, { count }) => sum + count, 0);
  if (rest > 0) parts.push({ text: `${rest} more`, href: null });
  return parts.flatMap((part, i) => (i === 0 ? [part] : [{ text: ' · ', href: null }, part]));
}

/**
 * The judgment queue, over a link to the row at the top of it.
 *
 * `headerCounts` owns the count, including the case this cell exists to get
 * right: a fail-soft dashboard that lost one of its row sources knows neither
 * how many rows there are nor that there are none, so the figure is an em dash
 * and the caption names the sources that went missing rather than describing
 * whichever rows happened to survive.
 */
export function needsYouReadout(dashboard: Source<Dashboard>): Readout {
  if (isUnavailable(dashboard)) return unread('Needs you', 'attention', QUEUE_UNREAD);
  const counts = headerCounts(dashboard, null);
  if (counts.needsYou === null) {
    const out = new Set(dashboard.unavailable ?? []);
    const lost = JUDGMENT_ROW_SOURCES.filter((name) => out.has(name));
    return unread('Needs you', 'attention', `${lost.join(', ')} unavailable`);
  }
  return {
    label: 'Needs you',
    tone: 'attention',
    figure: countLabel(counts.needsYou),
    meta: '',
    caption: needsYouCaption(orderRows(dashboard.rows), counts.needsYou),
    summary: needsYouSummary(orderRows(dashboard.rows), counts.needsYou)
  };
}
