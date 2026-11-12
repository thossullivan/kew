/**
 * FUI-20 — the Work screen's Pipeline section, as a pure model.
 *
 * FUI-11 serves an OPEN payload: `lanes` and `stages` are whatever facts kew
 * could compose, in the order it composed them. kew emits one `main` lane
 * over implement / publish / review / merge; kew has no Reproduce column and
 * no `release/[branch]` lane, so neither reaches this
 * grid (Factory UI plan finding 5, recorded as an intentional deviation from DWork).
 * This module therefore builds the grid FROM the payload and never from a
 * table of the four stages it expects — a fifth stage, a second lane or a
 * renamed one lands on screen without a change here.
 *
 * Three rules it exists to keep, all pinned in `pipeline.test.ts`:
 *
 * 1. **The columns are the payload's order.** First lane first, and any stage a
 *    later lane names that no earlier one did is appended after them. A lane is
 *    never truncated and a stage is never dropped: a lane that names the same
 *    stage twice gets two columns for it, because the second cell is a fact
 *    somebody recorded.
 * 2. **A missing lane renders nothing.** No lanes, no stages, or no payload at
 *    all (an id the server does not know is a 404, per `api_work.py`) leaves
 *    `empty` set, and the section is then absent — not a dashed placeholder
 *    grid, which would draw a pipeline that does not exist. The one thing that
 *    survives an absent payload is a NAMED read failure: a 404 is an answer, a
 *    500 is not. An item the server knows but nothing has run for is not
 *    missing: it is served as its stages, every one pending, and drawn with
 *    `NO_RUN_YET` under them (#2572).
 * 3. **Nothing here decides a colour or a shape.** Every mark and every segment
 *    comes from `$lib/factory/swimlane` over `$lib/factory/status`, so an
 *    unrecognised stage state draws the dashed pending ring with its own word
 *    rather than earning the green that means attested.
 *
 * Pixel reference: DWork y ≈276–432 (the main lane).
 */
import type { ItemPipeline, PipelineLane, PipelineStage } from '$lib/api';
import { markerFor, type SwimlaneCell, type SwimlaneLane } from '$lib/factory/swimlane';
import type { StatusVocabulary } from '$lib/factory/status';

/**
 * Which vocabulary the stage states are read in.
 *
 * `'run'` rather than the `'epic'` default, for the one word the two disagree
 * about: `queued`. This payload's states are progress states composed by
 * `service/pipeline.py`, which says `needs_you` in so many words when a person
 * is the next move — so any other word it grows is the machine's own queue, and
 * painting it magenta would spend the colour that is rationed to the needs-you
 * band directly below this section.
 */
export const PIPELINE_VOCABULARY: StatusVocabulary = 'run';

/** One column of the grid. `key` is unique; `stage` is the payload's own name. */
export type PipelineColumn = {
  key: string;
  stage: string;
  /** 0 for a stage named once; 1 for a lane's second cell of the same name. */
  occurrence: number;
};

/** One legend entry: the mark, and the word the artboard puts beside it. */
export type PipelineLegendEntry = {
  key: string;
  /** A status word, so `statusShape` decides the mark. Never a colour here. */
  status: string;
  label: string;
};

/**
 * The legend (DWork y ≈290): a key to the marks and lines below it.
 *
 * "attested" is the artboard's word for the green dot the pipeline's own
 * vocabulary spells `done`, which is the same mark — the legend names what the
 * reader is looking at, and the cells keep the server's word. The marks are
 * `StatusMark`'s, so the legend's dot cannot drift from the lane's: D draws the
 * working dot and the needs-you diamond without their halos, and taking the
 * halo over hand-matching the artboard keeps one mark per meaning. `not ready`
 * postdates the artboard: a review round that ended against the change is
 * amber in its own half-filled ring (#3466), and a lane can draw it.
 */
export const PIPELINE_LEGEND: readonly PipelineLegendEntry[] = [
  { key: 'attested', status: 'attested', label: 'attested' },
  { key: 'working', status: 'working', label: 'working' },
  { key: 'not_ready', status: 'not_ready', label: 'not ready' },
  { key: 'needs_you', status: 'needs_you', label: 'needs you' },
  { key: 'pending', status: 'pending', label: 'pending' }
];

/** How `ItemPipeline.unavailable`'s tokens read in a sentence. */
const UNAVAILABLE_WORDS: Record<string, string> = {
  'pipeline:review_rounds': 'the review rounds',
  'pipeline:audit': 'the audit chain',
  'pipeline:judgment': 'the judgment record',
  'pipeline:merge_verify': 'the merge verify record'
};

export type PipelineModel = {
  columns: PipelineColumn[];
  /** The column headings, for `Swimlane`'s `stages`. */
  stages: string[];
  /** One lane per payload lane, its cells aligned to `columns`. */
  lanes: SwimlaneLane[];
  /** What the payload could not answer, in a sentence. `''` normally. */
  notice: string;
  /** True when there is no pipeline to draw, so the section is absent. */
  empty: boolean;
  /** True when every stage is pending with nothing behind it: nothing ran. */
  noRun: boolean;
};

/** The sentence under the stages of an item nothing has run for (#2572). */
export const NO_RUN_YET = 'No run yet';

/**
 * Whether the payload is the empty pipeline: every cell of every lane pending,
 * none carrying evidence, and nothing unread. An unread fact keeps this false —
 * "we could not look" must not be worded as "nothing happened".
 */
function nothingRan(pipeline: ItemPipeline | null | undefined): boolean {
  if (!pipeline || (pipeline.unavailable ?? []).length > 0) return false;
  const stages = (pipeline.lanes ?? []).flatMap((lane) => lane.stages ?? []);
  return (
    stages.length > 0 &&
    stages.every((stage) => normalize(stage.state ?? '') === 'pending' && stage.evidence == null)
  );
}

/** Own-property lookup, so a payload word cannot reach `Object.prototype`. */
function own<T>(table: Record<string, T>, key: string): T | undefined {
  return Object.hasOwn(table, key) ? table[key] : undefined;
}

/** `in-progress`, `In Progress` and `in_progress` are one word. */
function normalize(raw: string): string {
  return raw.trim().toLowerCase().replace(/[\s-]+/g, '_');
}

/** A column's identity: a stage name is not one on its own (see below). */
const slotKey = (stage: string, occurrence: number): string => `${occurrence} ${stage}`;

/** A lane's own cells, keyed by the column each of them belongs in. */
function laneSlots(lane: PipelineLane): Map<string, PipelineStage> {
  const times = new Map<string, number>();
  const slots = new Map<string, PipelineStage>();
  for (const stage of lane.stages ?? []) {
    const occurrence = times.get(stage.stage) ?? 0;
    times.set(stage.stage, occurrence + 1);
    slots.set(slotKey(stage.stage, occurrence), stage);
  }
  return slots;
}

/**
 * The grid's columns, in the payload's own order.
 *
 * The key carries the occurrence rather than the name alone, so a lane that
 * repeats a stage gets a column per cell instead of having its second reading
 * quietly dropped — the same promise `columnCount` makes in `swimlane.ts`.
 */
export function pipelineColumns(
  lanes: readonly PipelineLane[] | null | undefined
): PipelineColumn[] {
  const columns: PipelineColumn[] = [];
  const seen = new Set<string>();
  for (const lane of lanes ?? []) {
    const times = new Map<string, number>();
    for (const stage of lane.stages ?? []) {
      const occurrence = times.get(stage.stage) ?? 0;
      times.set(stage.stage, occurrence + 1);
      const key = slotKey(stage.stage, occurrence);
      if (seen.has(key)) continue;
      seen.add(key);
      columns.push({ key, stage: stage.stage, occurrence });
    }
  }
  return columns;
}

/**
 * One payload stage as a swimlane cell.
 *
 * The two caption lines are the server's `label` and `detail` verbatim; the
 * only decision is WHICH of them carries the status colour, and the artboard
 * makes it by content: "run f5dcb6a5" over a green "success" colours the second
 * line, while the merge cell's magenta "needs you" over a quiet "merge
 * decision" colours the first. Both are the same rule — the line that IS the
 * status word takes the status's colour — and a cell with only one line is that
 * line, so a lone "not started" stays the quiet word pending deserves rather
 * than reading as a fact in `--text-1`.
 */
export function pipelineCell(stage: PipelineStage): SwimlaneCell {
  const label = stage.label ?? '';
  const detail = stage.detail ?? '';
  const isStatus = detail === '' || normalize(label) === normalize(stage.state ?? '');
  return {
    state: stage.state,
    primary: label,
    secondary: detail,
    statusLine: isStatus ? 'primary' : 'secondary',
    // A published PR nobody scanned keeps its pending mark, and the path runs
    // through it all the same (#3466).
    ...(stage.passed === true ? { passed: true } : {})
  };
}

/** Whether a stage says the path has actually arrived at it. */
function running(stage: PipelineStage): boolean {
  const flow = markerFor(stage.state, PIPELINE_VOCABULARY, stage.passed === true).flow;
  return flow === 'settled' || flow === 'live';
}

/**
 * One payload lane, aligned to `columns`.
 *
 * A column this lane never named gets an inactive cell, which is the 6px stub
 * on a dashed hairline — the artboard's own drawing for "this lane has no stage
 * here" (DWork release lane). That is a statement about the grid, not a claim
 * about work, which is why it is not a `pending` cell.
 *
 * The lane's name is drawn quiet unless something in it is settled or live:
 * `muted` means nobody is running this lane, and a lane of nothing-yet cells is
 * exactly that.
 */
export function pipelineLane(lane: PipelineLane, columns: readonly PipelineColumn[]): SwimlaneLane {
  const slots = laneSlots(lane);
  return {
    label: lane.name ?? '',
    muted: !(lane.stages ?? []).some(running),
    cells: columns.map((column) => {
      const stage = slots.get(column.key);
      return stage ? pipelineCell(stage) : { state: null };
    })
  };
}

/**
 * What the payload could not read, in a sentence — or `''` when it read
 * everything.
 *
 * FUI-11's promise is that a stage is only honestly "pending" when its reason
 * is absent from `unavailable`, so the reasons are printed rather than folded
 * into the cells: an unread fact and an unfinished step look alike on the grid,
 * and this line is the only thing that tells them apart. An entry this UI has
 * no phrase for is printed verbatim, because a token the reader can search for
 * beats a reassuring silence.
 */
export function pipelineNotice(unavailable: readonly string[] | null | undefined): string {
  const named = (unavailable ?? []).map((key) => own(UNAVAILABLE_WORDS, key) ?? key);
  if (named.length === 0) return '';
  // The list is printed as it arrives, uncapitalised: an entry with no phrase
  // for it is a token the reader may want to search the logs for, and
  // `Pipeline:audit` is not the string kew wrote anywhere.
  return `Not every fact could be read: ${named.join(', ')} — a stage here may say less than what happened.`;
}

/** The whole section, from one payload. `null` is an item with no pipeline. */
export function pipelineModel(pipeline: ItemPipeline | null | undefined): PipelineModel {
  const lanes = pipeline?.lanes ?? [];
  const columns = pipelineColumns(lanes);
  return {
    columns,
    stages: columns.map((column) => column.stage),
    lanes: columns.length === 0 ? [] : lanes.map((lane) => pipelineLane(lane, columns)),
    notice: pipelineNotice(pipeline?.unavailable),
    empty: columns.length === 0,
    noRun: nothingRan(pipeline)
  };
}

/** What a failed `fetchPipeline` meant. */
export type PipelineReadFailure = {
  /** True when the answer is "this item has no pipeline" — draw nothing. */
  absent: boolean;
  /** The sentence to print when it is not. `''` when `absent`. */
  notice: string;
};

/**
 * Read a failed pipeline fetch, the way `refusedPrompt` reads a failed prompt
 * fetch: `get()` throws `Error("<path> -> <status>")`, and the status is the
 * whole difference between an answer and a gap.
 *
 * A 404 IS the answer — `api_work.py` 404s an id it does not know, so drawing
 * a notice for it would put a warning over a pipeline that does not exist. (An
 * idle ready-queue issue is not one of those: it is served as four pending
 * stages, #2572.) Anything else is a read that failed, and gets said out loud.
 */
export function pipelineReadFailure(err: unknown): PipelineReadFailure {
  const message = err instanceof Error ? err.message : String(err);
  const status = /-> (\d{3})$/.exec(message)?.[1] ?? '';
  if (status === '404') return { absent: true, notice: '' };
  return {
    absent: false,
    notice: status
      ? `The pipeline could not be read (${status}) — the stages this item moved through are missing.`
      : 'The pipeline could not be read — the stages this item moved through are missing.'
  };
}
