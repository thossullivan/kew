// The popover beside a selected DAG node (#2250, the brief's §2.3 "Click a
// node"), as plain data.
//
// It reads ONLY the snapshot already on screen — title, status, `status_since`,
// `attention`, `url`, `pr_url` — plus the node's drain, which the page already
// has from `/api/epics/{ref}`'s economics. No fetch, and nothing composed out
// of a repo slug: a popover that had to ask the server would open empty on the
// one click a reader makes most, and a surface that invents a URL sends people
// to a 404 with kew's name on it.
//
// The command under the Copy button is `attention.command` and nothing else.
// It is the server's own filled line (`kew.waits.fill_command`, #2244), which
// is the only reading that can promise "runnable as printed" — a command
// composed here out of `attention.action`'s prose was `kew epic unhold` with
// no arguments, and a Copy button over a line that exits 2 is worse than no
// button at all.
//
// The Esc reducer lives here too (`escape`): "closes the popover and keeps the
// selection, then clears the selection" is a two-step rule over two booleans,
// which is exactly the kind of thing that ends up implemented twice — once in
// the canvas and once in the page — and then disagrees.
import { recordedReason } from './attention';
import { rowSentence } from '$lib/needs-you/summary';
import { elapsed, fmtUSD } from './format';
import { displayId, statusWord } from './render';
import { workHref } from './rail';
import { parseInstant } from '$lib/time';
import { badgesFor, unreadBaseNote, type StalenessBadge } from '$lib/freshness';
import type { EpicNode, EpicNodeDetail, EpicNodeRun, PrFreshnessRow } from '$lib/api';
import { runHref } from '$lib/flight-recorder';
import { isUnavailable, type Source } from '$lib/monitor/readouts';
import { safeHref } from '$lib/safe-href';

/** One link under the popover. The markup adds `↗` when `external`. */
export type PopoverLink = {
  key: string;
  /** The link's own text, with no arrow in it — the markup owns that. */
  label: string;
  href: string;
  /** True when the link leaves kew, so the anchor takes `target`/`rel`. */
  external: boolean;
};

/** The popover, top to bottom. Every string field is '' when nobody wrote it. */
export type PopoverModel = {
  /** `#2219` — the id as the graph prints it. */
  id: string;
  /** The full title, unclamped: the node's own label is the clamped one. */
  title: string;
  /** The status mark's own kind — `needs_you` on a node somebody is waiting
   * on, exactly as the node in the graph draws it (`markStatus`). */
  mark: string;
  /** "in review · 12m", or the status word alone when `status_since` is
   * unreadable. Never a fabricated duration. */
  status: string;
  /** `status`'s two halves, drawn apart (#2898): the status word alone, which
   * is the one element the 390 sweep reads, and the age after it ('' when
   * `status_since` is unreadable). */
  word: string;
  age: string;
  /** The reason the driver recorded about the status itself — the blocked
   * reason or the arm action (`recordedReason`). The card prints the status
   * word alone (#2543), so this is the one place it is said. '' when none. */
  recorded: string;
  /** Why a person is the next move, from `attention.reason`. '' when nobody
   * is waiting. */
  reason: string;
  /** The command that clears the wait, runnable as printed — verbatim from
   * `attention.command`, which the server filled and `fill_command` guarded.
   * '' when no kew command clears this wait. */
  command: string;
  /** What the command also does, or what has to happen first — whatever
   * `attention.action` says beyond the command itself. '' when it says
   * nothing more. */
  note: string;
  /** "$12.34", or '' when this node has no recorded drain. */
  drain: string;
  /** In-app links. Always has "Open in Work". */
  links: PopoverLink[];
  /** Links that leave kew. Separate so the markup cannot mix the two up. */
  external: PopoverLink[];
  /**
   * The staleness badges this node earns, each with the causes behind it
   * (#2303). The badge on the card says WHICH it is; this is where the sentence
   * and the commit go, because the card is 196px wide and a sentence on it
   * would only ever be clipped. Empty when nothing moved.
   */
  badges: StalenessBadge[];
  /**
   * What a base NOBODY COULD READ means, in a sentence — '' when the base is
   * clean, or when a badge already says it. An unjournaled merge state is not a
   * clean tree, and a popover that drew nothing for it would be reporting a
   * guarantee nobody read.
   */
  baseNote: string;
};

/** Trimmed, or '' — whitespace is not a reason, a command or a title. */
const said = (raw: string | null | undefined): string => (raw ?? '').trim();

/**
 * How a token still reads as part of an invocation: a word, a path, a flag, a
 * `#`-ref or one of `kew.waits`' own `<placeholder>`s. Anything else — a
 * comma, an em dash, a full stop — is where the hint stops being a command
 * line and starts being prose about it.
 */
const COMMAND_TOKEN = /^[\w./:#@=<>-]+$/;

/** The same token with the punctuation that ENDS the invocation still on it:
 * in `kew epic unhold, which also restarts…` the comma starts the clause, so
 * the walk steps over that word and the clause is what is left. */
const LAST_TOKEN = /^[\w./:#@=<>-]+[,.;:]$/;

/**
 * What the attention's own action sentence says BEYOND the command itself.
 *
 * `action` is `kew.waits`' hint, and it comes in three shapes:
 *
 *   `kew epic continue <name> <node>`                      — all command
 *   `kew epic unhold, which also restarts the review cycle` — command, then prose
 *   `re-check the worktree its reason names, then kew epic unhold` — all prose
 *
 * The command is NOT read out of here. `attention.command` is the server's
 * own, with this node's epic and item already in it and `fill_command` as the
 * guard that it is runnable as printed (#2244). This function used to scrape
 * one out of the hint instead, and since the hint is a TEMPLATE or a sentence
 * and never a filled line, every command it could produce was argument-less:
 * `kew epic unhold` exits 2 on "Missing argument 'NAME'", and `kew epic
 * continue <name> <node>` exits 2 on the placeholder.
 *
 * So what is left for the popover to print under the server's command is
 * whatever the sentence adds to it: a hint that BEGINS with an invocation has
 * that invocation dropped (shape 1 leaves nothing, shape 2 leaves its clause),
 * and prose that merely mentions one is kept whole (shape 3) — there the
 * sentence is the instruction, and its command is the last thing it asks for.
 */
export function actionNote(action: string | null | undefined): string {
  const hint = said(action);
  const words = hint.split(/\s+/).filter((w) => w !== '');
  if (words[0] !== 'kew') return hint;
  let at = 0;
  while (at < words.length && COMMAND_TOKEN.test(words[at])) at += 1;
  if (at < words.length && LAST_TOKEN.test(words[at])) at += 1;
  return words
    .slice(at)
    .join(' ')
    .replace(/^[\s—-]+/, '')
    .trim();
}

/** The PR's number off its own URL, so "PR #2231" names the thing it opens
 * rather than being composed from a repo slug the snapshot does not carry. */
function prLabel(url: string): string {
  const num = /\/(?:pull|pulls|merge_requests)\/(\d+)/.exec(url);
  return num ? `PR #${num[1]}` : 'PR';
}

/**
 * The popover for one node, off the snapshot in hand.
 *
 * `nowMs` is a parameter (not `Date.now()` inside) for `format.ts`'s reason:
 * a duration printed from a clock the test cannot set is a reading nobody can
 * assert.
 */
export function nodePopover(
  node: EpicNode,
  drain: number | null,
  nowMs: number = Date.now(),
  freshness: PrFreshnessRow | null = null
): PopoverModel {
  const attention = node.attention;
  const since = parseInstant(node.status_since);
  const word = statusWord(node.status);
  const age = since === null ? '' : elapsed(nowMs - since);
  const note = actionNote(attention?.action);
  const external: PopoverLink[] = [];
  // A served URL is a link only through the one gate (C10, #3470).
  const issue = safeHref(said(node.url)) ?? '';
  const pr = safeHref(said(node.pr_url)) ?? '';
  if (issue !== '') external.push({ key: 'issue', label: 'Issue', href: issue, external: true });
  if (pr !== '') external.push({ key: 'pr', label: prLabel(pr), href: pr, external: true });
  return {
    id: displayId(node.id),
    title: said(node.title),
    mark: attention ? 'needs_you' : node.status,
    status: age === '' ? word : `${word} · ${age}`,
    word,
    age,
    recorded: recordedReason(node) ?? '',
    // The server's one sentence for this wait (#2539), never a paraphrase.
    reason: attention ? rowSentence(attention) : '',
    command: said(attention?.command),
    note,
    drain: drain == null ? '' : fmtUSD(drain),
    links: [{ key: 'work', label: 'Open in Work', href: workHref(node.id), external: false }],
    external,
    // `nowMs` goes in: the rebase badge is gated on how old the journaled
    // base reading is (`freshness.BASE_READING_MAX_AGE_MS`).
    badges: freshness === null ? [] : badgesFor(freshness, nowMs),
    baseNote: freshness === null ? '' : unreadBaseNote(freshness, nowMs)
  };
}

/** One run in the popover's history: a link to its run page and its outcome. */
export type PopoverRun = {
  readonly id: string;
  /** The run page, in-app (`/runs/<id>`). */
  readonly href: string;
  /** The run's own status, as words: `success`, `failed`, `running`. */
  readonly verdict: string;
  readonly cost: string;
  /** How long ago it started, or '' when the ledger has no start. */
  readonly ago: string;
};

/** The run history the popover leads with (#2544), as a Dagster asset leads
 * with its materializations. */
export type PopoverHistory = {
  /** Newest first, at most `HISTORY_RUNS`. */
  readonly runs: readonly PopoverRun[];
  /** `3 runs · $1.20`, `no runs yet`, or why the history could not be read. */
  readonly summary: string;
};

/** How many runs the popover lists; the rest are one click away in Work. */
export const HISTORY_RUNS = 3;

const startedMs = (r: EpicNodeRun): number => parseInstant(r.started_at) ?? -Infinity;

/**
 * The node's run history for the popover: its last runs, newest first, each
 * with its outcome and cost and a link to its run page. Null while the read is
 * in flight, so nothing flickers in and out under the title; an unreadable
 * read says so in the summary rather than reading as "no runs".
 */
export function runHistory(
  detail: Source<EpicNodeDetail> | undefined,
  nowMs: number = Date.now()
): PopoverHistory | null {
  if (detail === undefined) return null;
  if (isUnavailable(detail)) return { runs: [], summary: detail.reason };
  if (detail.runs.length === 0) return { runs: [], summary: 'no runs yet' };
  const runs = [...detail.runs]
    .sort((a, b) => startedMs(b) - startedMs(a))
    .slice(0, HISTORY_RUNS)
    .map((r): PopoverRun => {
      const started = parseInstant(r.started_at);
      return {
        id: r.run_id,
        href: runHref(r.run_id),
        verdict: r.status.replace(/_/g, ' '),
        cost: fmtUSD(r.cost_usd),
        ago: started === null ? '' : `${elapsed(nowMs - started)} ago`
      };
    });
  const n = detail.runs.length;
  return { runs, summary: `${n} ${n === 1 ? 'run' : 'runs'} · ${fmtUSD(detail.lifetime_usd)}` };
}

/** Whether a node is selected, and whether its popover is open. */
export type Focus = {
  readonly selected: string | null;
  readonly popover: boolean;
};

/** Nothing selected, nothing open. */
export const NO_FOCUS: Focus = Object.freeze({ selected: null, popover: false });

/**
 * Esc, once.
 *
 * The brief's rule: the first Esc closes the popover and KEEPS the selection,
 * so the bottom rail — the panel the operator is actually reading — stays on
 * the node after the box over the graph is out of the way. A second Esc drops
 * the selection as well. A click on the background is the second step
 * directly, which is `NO_FOCUS`.
 */
export function escape(focus: Focus): Focus {
  if (focus.popover) return { selected: focus.selected, popover: false };
  return NO_FOCUS;
}

/** Where the epic page's address stood: its path and the node `?node=` names. */
export type NodeAddress = { readonly path: string; readonly node: string | null };

/**
 * Whether a navigation that lands on `url` opens the popover of the node its
 * `?node=` names (#3149), the page's address having been `before` (null on
 * the page's first reading).
 *
 * `?node=` names the SELECTION, not an open popover: a close with × or Esc
 * keeps the node in the address and the selection on it. So a navigation
 * that lands on the node already selected (the same address with another key
 * added, as a demo tour's next step makes) leaves a popover the reader shut
 * shut; only a node the address did not name before, or another page, opens
 * one. A replay link (`?mode=replay`) never opens one here: replay opens the
 * popover by its own first-frame rule, against the frame, not the live graph.
 */
export function linkOpensPopover(before: NodeAddress | null, url: URL): boolean {
  const node = url.searchParams.get('node');
  if (node === null || url.searchParams.get('mode') === 'replay') return false;
  return before === null || before.path !== url.pathname || before.node !== node;
}

/** Selecting a node: it becomes the selection AND opens its popover, which is
 * one click doing both of the brief's two things. Selecting nothing is
 * `NO_FOCUS`, never a popover with no node under it. */
export function select(id: string | null): Focus {
  return id == null ? NO_FOCUS : { selected: id, popover: true };
}
