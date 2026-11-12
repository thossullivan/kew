/**
 * #2246 — what the Work detail pane says about a needs-you ENTRY, as a pure
 * model.
 *
 * Five of the seven rows on the live Work screen are needs-you entries with no
 * work-projection view behind them (#1147, #1144, #1116, #1111, SFD-232 in the
 * UX brief's screenshot). Those rows now select in place (`list.ts`), which
 * leaves the question the old links dodged: with no view, `needsYouBand`
 * returns `null` and the pane had nothing at all to say about them.
 *
 * So this is the ENTRY half of the one needs-you block (`$lib/needs-you/block`,
 * #2280), built from the `DashboardRow` itself and from nothing else. Four
 * readings, in the artboard's order (`design/factory-ui/UXWork.html`):
 *
 * * the **head word** — the KIND of wait ("Held"), beside "Needs you"; the
 *   reason is the summary's alone (#3051).
 * * **Why** — what the reason itself adds: a count, a path, the layer that
 *   refused.
 * * **Since** — how long it has stood, said `held` or `waiting` as the row's
 *   own attention kind says it is.
 * * **Do this** — the command the server filled in, runnable as printed, and —
 *   for a hold, which is the one wait kew has an action table for — one line on
 *   what running it does.
 *
 * Two rules it exists to keep.
 *
 * **One reading per row.** The what and the why are `rowWords`' — the same
 * derivation `rowSummary` joins into the sentence the list row and Monitor's
 * caption print (#2245). A pane headlined "the review round cap was reached"
 * over a Why line composed from any other wording would be that same
 * two-readings defect one screen along.
 *
 * **No URL is invented.** The secondary links are the ones the row can be
 * addressed BY: the epic page for the epic it names, and whatever single `url`
 * the server put on it. kew serves one artifact URL per needs-you row, so this
 * links exactly that one — it never composes a forge URL from a repo slug, and
 * there is therefore no "Issue ↗" beside a "PR ↗" unless the server served two,
 * which it does not.
 */
import type { DashboardRow } from '$lib/api';
import { elapsed } from '$lib/epic/format';
import { commandNote, describeHold, type HoldPlace } from '$lib/monitor/hold-reasons';
import { EPIC_HOLD } from '$lib/monitor/holds';
import { kindTitle } from '$lib/needs-you/kind';
import { reasonBody, rowSummary, rowWords, type RowWords } from '$lib/needs-you/summary';
import { parseInstant } from '$lib/time';
import { itemRef } from '$lib/utils';
import { attentionState, type WorkRow } from '$lib/work/list';
import { epicHref } from '$lib/work/refs';
import { safeAppHref, safeHref } from '$lib/safe-href';

/**
 * The block's two shapes live in `$lib/needs-you/block`, which is the ONE
 * renderer's model: this module builds the entry half of it, and nothing here
 * describes a field the projection half does not also fill (#2280 D9).
 */
import type { BlockLink, MoveLink, NeedsYouBlock } from '$lib/needs-you/block';
import { linkMoves } from '$lib/needs-you/moves';

export type { BlockLink as DetailLink, NeedsYouBlock as EntryDetailModel };

/** Local aliases, so the signatures below read as they always have. */
type DetailLink = BlockLink;
type EntryDetailModel = NeedsYouBlock;

/**
 * Re-exported where it has always been imported from. The reading itself moved
 * to `$lib/monitor/hold-reasons` with the vocabulary it is about, so Monitor's
 * hold rows can take it without this module and that one importing each other.
 */
export { commandNote };

/** The epic and item the command line is about, as the row carries them. */
function placeOf(entry: DashboardRow): HoldPlace {
  const id = entry.item_id != null && entry.item_id !== '' ? entry.item_id : entry.issue_num;
  return {
    epic: entry.epic_ref ?? '',
    // Never `#`-prefixed: this is a command-line token. The `0` sentinel names
    // nothing (`DashboardRowDTO` in kew/service/models.py).
    item: id == null || id === '' || id === 0 ? '' : String(id)
  };
}

/**
 * The entry's secondary links: the epic page, then the artifact.
 *
 * `/epics/<ref>?node=<id>` is the in-app "Show in epic" the brief asks for — the
 * epic page with the node named, which it may not read yet and which costs
 * nothing when it does not. With no epic, Monitor is the honest destination: it
 * is the screen the approval and hold columns live on, and it is where
 * `entryHref` used to send exactly these rows.
 */
function linksFor(
  entry: DashboardRow,
  command: string
): { links: DetailLink[]; external: DetailLink[]; moves: MoveLink[] } {
  const links: DetailLink[] = [];
  const external: DetailLink[] = [];
  const place = placeOf(entry);
  if (place.epic !== '') {
    const node = place.item === '' ? '' : `?node=${encodeURIComponent(place.item)}`;
    links.push({
      key: 'epic',
      label: 'Show in epic',
      href: `${epicHref(place.epic)}${node}`,
      external: false
    });
  } else {
    links.push({ key: 'monitor', label: 'Show in Monitor', href: '/monitor', external: false });
  }
  // Every served URL below becomes an href only through safeHref (C10).
  const failed = entry.verify_link ?? null;
  const failedHref = safeAppHref(failed?.href);
  if (failed != null && failedHref != null) {
    // A merge over a verify that failed at this head (#3460): the failure is
    // the first place to look, so it leads the links; the PR follows.
    links.unshift({ key: 'verify', label: failed.label, href: failedHref, external: false });
  }
  const link = entry.link ?? null;
  const linkHref = safeHref(link?.href);
  if (link != null && linkHref != null) {
    // A merge's move, named by the server (#2539): "Open PR #2059 to merge".
    // It IS the row's one URL, so it replaces the bare artifact link.
    const served = { key: 'artifact', label: link.label, href: linkHref, external: true };
    if (command !== '') {
      external.push(served);
      return { links, external, moves: [] };
    }
    // With no command to clear the row, the link IS the move (#3467 D24): it
    // leaves the quiet row for Do this. A failed merge verify leads it, in the
    // order #3460 set, and is then the magenta control itself (`linkMoves`).
    const verify = links[0]?.key === 'verify' ? links.shift()! : null;
    return { links, external, moves: linkMoves(verify, served) };
  }
  const url = safeHref(entry.url);
  if (url != null) {
    external.push({
      key: 'artifact',
      // The ONE URL the server served for this row, named by what it is where
      // the row says so (#3467 D30: never a bare "Open"). Never a second link
      // built out of a repo slug.
      label: artifactLabel(entry, url),
      href: url,
      external: true
    });
  }
  return { links, external, moves: [] };
}

/**
 * What the row's one URL opens: its PR where the row names one (by number, or
 * by a PR-shaped URL, #2893), else the item's own issue — the only other URL
 * the server serves a needs-you row (`SFD-229` → its Linear issue).
 */
function artifactLabel(entry: DashboardRow, url: string): string {
  if (entry.pr_number != null) return `PR #${entry.pr_number}`;
  const pr = PR_URL.exec(url);
  if (pr !== null) return `PR #${pr[1]}`;
  const ref = itemRef(entry.item_id, entry.issue_num === 0 ? null : entry.issue_num);
  return ref === '' ? 'Open the artifact' : `Issue ${ref}`;
}

/** A forge's PR path, as `namesPr` in `$lib/work/list` reads one. */
const PR_URL = /\/(?:pull|pulls|pr|merge_requests)\/(\d+)\/?$/;

/**
 * The block for one Work row, or `null` when the row is not an entry's.
 *
 * `null` is the ordinary case — an item the projection composed has a view, and
 * the pane draws `NeedsYou.svelte`'s band over it as it always did. This block
 * REPLACES that band for an entry row, which has no view for the band to read.
 */
export function entryDetail(row: WorkRow, nowMs: number = Date.now()): EntryDetailModel | null {
  return row.entry === null ? null : entryBlock(row.entry, nowMs);
}

/**
 * The same block from the entry alone.
 *
 * Exported because a row the PROJECTION composed carries the same entry
 * (`WorkRow.attention`) without the row existing because of it, and the one
 * renderer must read the same fields for both (#2280 D9).
 */
export function entryBlock(entry: DashboardRow, nowMs: number = Date.now()): EntryDetailModel {
  const words = rowWords(entry);
  const command = (entry.command ?? '').trim();
  // The action is the hold table's, where the table has one for this row at all
  // (`holdAction`), with the row's own epic and item filled in — so the note
  // that follows the command is about THIS row. The command itself is never
  // built here: `row.command` is the server's, filled in by
  // `kew.waits.fill_command`, and a second filler would be a second chance to
  // print a command line that does not run.
  const action = holdAction(entry, words);
  const { links, external, moves } = linksFor(entry, command);
  const summary = rowSummary(entry);
  return {
    summary,
    // The KIND of wait ("Held", "Merge decision"), never the reason: the
    // summary is the server's sentence and already opens with the reason, so a
    // head that printed it too said it twice (#3051 N8).
    label: kindTitle(entry.attention_kind),
    // The server's sentence usually carries the detail already (#2539), and a
    // Why line repeating it is one thing said twice.
    why: summary.includes(words.detail) ? '' : words.detail,
    since: rowSince(entry, nowMs),
    command,
    note: commandNote(action, command),
    nodeIds: (entry.node_ids ?? []).filter((id) => id !== ''),
    moves,
    links,
    external
  };
}

/**
 * The hold table's action for this entry, or '' because the table has nothing
 * to say about it.
 *
 * `describeHold` answers for EVERY string: a reason no prefix claims falls to
 * the unbucketed-hold hint, which is `fix the cause, then kew epic unhold
 * <epic> <item>` — the right answer for a held node whose reason the table has
 * never heard of, and a flatly wrong one for a row that is not a hold. Asking
 * it about every entry put that unhold under a merge decision whose own served
 * command was `kew epic merge`, and under an orphan PR and a driver nobody is
 * ticking, which no unhold has ever cleared.
 *
 * So the table is asked only about the rows it describes: the `epic_holds` ones
 * (`EPIC_HOLD`, the same question `holdKindOf` asks before it reads a category
 * off a reason). Every other entry's move is the command the server served and
 * nothing else — there is no second table of actions for it, and the honest
 * note is none.
 */
function holdAction(entry: DashboardRow, words: RowWords): string {
  if (entry.type !== EPIC_HOLD) return '';
  return describeHold(reasonBody(words), placeOf(entry)).action;
}

/**
 * How long the row has stood, in the word the row's own kind earns.
 *
 * `heldFor` (`$lib/monitor/holds`) words every age `held <age>`, which is true
 * of every row it is asked about: Monitor's Holds column only calls it on an
 * `epic_holds` row. This block prints the line for every needs-you entry, and
 * most of them are not holds — a node awaiting a merge, an approval, an orphan
 * PR, a driver nobody is ticking — so `held` there asserts a hold the row does
 * not have.
 *
 * Which of the two it is comes from `attentionState`, the kind table the list's
 * own State column reads for these same rows, so the pane and the row it is
 * open over cannot disagree about whether this wait is a hold. `waiting` is the
 * artboard's own word for the stamp over a merge decision (`waiting 1h 01m` in
 * design/factory-ui/UXWork.html).
 */
export function rowSince(entry: DashboardRow, nowMs: number): string {
  const began = parseInstant(entry.age);
  if (began === null) return '';
  const word = attentionState(entry.attention_kind) === 'held' ? 'held' : 'waiting';
  return `${word} ${elapsed(nowMs - began)}`;
}
