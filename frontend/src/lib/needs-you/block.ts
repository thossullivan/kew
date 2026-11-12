/**
 * #2280 D9 — the needs-you block, as ONE model both halves of the Work screen
 * fill.
 *
 * The same wait read three ways on one screen (design review cycle 1): the
 * header printed the projection's exception sentence in mono magenta, the band
 * under it printed the safest action's sentence with no age and no command, and
 * the detail block an ENTRY row got — `Why`, `Since`, `Do this`, a command with
 * Copy — was reachable only for the five rows the projection had never
 * composed. A row that the projection DID compose, which is every row an
 * operator actually merges, got the thinnest of the three.
 *
 * So there is one block and one renderer (`NeedsYouBlock.svelte`), and this is
 * its model. It is filled from two readings of the same row, in this order:
 *
 * * **the needs-you entry** (`WorkRow.attention`) — the server's own set row,
 *   which carries the sentence every other surface prints (`rowSummary`), the
 *   stamp the wait began at (`age`, the row's `status_since`) and the command
 *   `kew.waits.fill_command` computed. This is the half that can say `Since`
 *   and `Do this` at all;
 * * **the projection's band** (`needsYouBand`) — which names the KIND of
 *   decision in the words the epic rail heads with ("Merge decision"), and
 *   whose own sentence is the summary or the `Why`.
 *
 * A row can have either, or both. With both, the entry's facts win every field
 * it fills — it is the set the header counts and Monitor lists — and the band
 * contributes the two things it alone knows: the title and, where the entry's
 * reason adds nothing, the sentence behind the decision. With the band alone,
 * its sentence is the summary and the `Why` is the projection's exception
 * detail — the fact the header above stops repeating once this block is the
 * one saying it (#2280 D10, `exceptionLine` in `$lib/work/header`).
 *
 * Nothing here words a fact. Every string is the server's, from one of those
 * two readings.
 */
import type { WorkException } from '$lib/api';
import { entryBlock } from '$lib/work/entry-detail';
import { needsYouBand, type NeedsYouBand } from '$lib/work/evidence';
import type { WorkRow } from '$lib/work/list';

/** One secondary link under the block. The markup adds `↗` when `external`. */
export type BlockLink = {
  key: string;
  /** The link's own text, with no arrow in it — the markup owns that. */
  label: string;
  href: string;
  /** True when the link leaves kew, so the anchor takes `target`/`rel`. */
  external: boolean;
};

/** A Do this link: `move` is the one that IS the move, drawn magenta. */
export type MoveLink = BlockLink & { move: boolean };

/** A run of a printed sentence, set in Plex Mono where `mono` (a config key, a file). */
export type TextRun = { text: string; mono: boolean };

/** The block, top to bottom. Every string field is '' when nobody wrote it. */
export type NeedsYouBlock = {
  /**
   * The kind of wait, in the head beside "Needs you": "Merge decision" from the
   * projection, else the entry's kind ("Held", `$lib/needs-you/kind`). Never
   * the reason, which the summary already says (#3051). '' when nobody named
   * one, which leaves the head reading "Needs you" alone.
   */
  label: string;
  /** The one sentence every surface prints for this row (`rowSummary`). */
  summary: string;
  /**
   * `summary` cut into runs where it names a config key or file, which print
   * in mono (#3075). Absent, the summary prints as one prose run.
   */
  summaryRuns?: TextRun[];
  /** What the wait adds beyond that sentence: a count, a path, a decision. */
  why: string;
  /**
   * How long the wait has stood, in the word its own kind earns: `held 68d6h`
   * for a hold, `waiting 1h00m` for everything else. '' when the row carries no
   * readable stamp — never a fabricated duration.
   */
  since: string;
  /** The command, runnable as printed. '' when no kew command clears the row. */
  command: string;
  /**
   * What running it does, or — where the action is not the command — what has
   * to happen first. '' when nobody wrote an action for this row.
   */
  note: string;
  /** `note` cut into runs, as `summaryRuns` cuts the summary (#3075). */
  noteRuns?: TextRun[];
  /**
   * The node ids a declaration hold asks to be declared, each in full (#2884).
   * The reason the summary is written from names one and counts the rest, so
   * this is the only place the operator can read what to declare. Empty on
   * every other wait.
   */
  nodeIds?: string[];
  /**
   * #3467 D24 — the Do this row's links, where no command clears the row but
   * the server served its move as a link ("Open PR #2059 to merge"). The one
   * marked `move` is the magenta control; the rest are quiet. A merge verify
   * that failed at the PR's head is the move and the PR follows it (#3460,
   * design review c9 D10: `linkMoves`). Absent or empty on every other block,
   * whose Do this is the command or the note.
   */
  moves?: MoveLink[];
  /** In-app links, so a reader can reach the epic without leaving the console. */
  links: BlockLink[];
  /** Links that leave kew. Separate so the markup cannot mix the two up. */
  external: BlockLink[];
};

/**
 * The block for a row the projection composed and the needs-you set does not
 * name.
 *
 * It can say what the decision is, what the wait adds beyond that, and make
 * the projection's call to action its Do this. It says nothing else: there is no entry behind it, so no stamp
 * and no computed command exist to print. An empty `Since` is the honest
 * reading — the alternative is a duration derived from whatever timestamp
 * happened to be in reach.
 *
 * The `Why` is the projection's EXCEPTION detail, by the same rule the merged
 * block below uses for the band's prose: the row's own sentence is the
 * summary, and the `Why` is whichever of the projection's two sentences that
 * summary has not already said (#2280 D10). For a merge decision the server
 * writes one sentence into both (`_await_decision` in kew/service/work.py), so
 * the row is empty and nothing is printed twice; where the detail says more
 * than the action's reason does — the diff lines a publication hold is held
 * on, the count of effects a zero-diff outcome still owes — that fact is said
 * HERE, which is why the header above may stop repeating it.
 */
function bandBlock(band: NeedsYouBand, exception: WorkException | null): NeedsYouBlock {
  const detail = (exception?.detail ?? '').trim();
  return {
    label: band.title,
    summary: band.prose,
    why: band.prose.includes(detail) ? '' : detail,
    since: '',
    command: '',
    note: '',
    nodeIds: [],
    // No command ever clears a band-only row, so its call to action IS the
    // move (#3467 D24): "Open PR #2059 to merge" is Do this, the one magenta
    // control, exactly as the entry's served link is.
    moves: band.cta
      ? [{ key: 'cta', label: band.cta.label, href: band.cta.href, external: band.cta.external, move: true }]
      : [],
    links: [],
    external: []
  };
}

/**
 * The one needs-you block for one Work row, or `null` — which is the ordinary
 * case and renders NOTHING.
 *
 * `null` means neither reading says a person is next: the row is not in the
 * needs-you set and the projection's safest action is one kew performs. Magenta
 * is rationed to the rows that are, so a block saying "nothing needs you" is
 * exactly what this must not return.
 */
export function needsYouBlock(
  row: WorkRow | null | undefined,
  nowMs: number = Date.now()
): NeedsYouBlock | null {
  if (row == null) return null;
  const band = needsYouBand(row.view);
  const entry = row.attention;
  if (entry === null) return band === null ? null : bandBlock(band, row.view?.exception ?? null);
  const block = entryBlock(entry, nowMs);
  if (band === null) return block;
  return {
    ...block,
    // The projection names the KIND of decision; the entry's own words are the
    // reason, which is already the summary's. Where the reason adds nothing of
    // its own, the band's sentence is what the Why row has to say.
    label: band.title || block.label,
    why: block.why || (band.prose === block.summary ? '' : band.prose)
  };
}
