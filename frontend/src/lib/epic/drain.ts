/**
 * #2210 — what heads the Epics list's Drain column, and what each row adds.
 *
 * `EpicSummary.label` captions the drain FIGURE ("drain spend", "since last
 * tick"); it is not the epic's name. The list printed it next to every
 * `epic_ref`, which put the same three words on every row and read as the
 * epics' titles. It belongs over the column it describes, once.
 *
 * The head prints one word (`DRAIN_COLUMN`) and is titled with what the
 * figure is (`DRAIN_CAPTION`) — always, whatever the rows carry (#2844 review
 * round 3). It once took the caption the rows agreed on, so a roster that all
 * read "since last tick" titled the head "since last tick" and the head no
 * longer said the figure was drain spend. A row whose caption differs from
 * the head's keeps it in its own `title` (`drainTitle`), so a figure measured
 * over a different window still says so where it applies.
 */

/** The column's own word, printed on the head.
 * `Spend` and not `Drain` (#2844 P9): Work names the same money "this run ·
 * item total", and the head should say what the figure is, not the mechanism
 * that accrues it. The caption ("drain spend") is the head's title. */
export const DRAIN_COLUMN = 'Spend';

/** What the figure is, worded as the server captions it by default
 * (`EpicEconomicsDTO.label`, kew/src/kew/service/models.py). The SPEND head's
 * `title`, on every roster. */
export const DRAIN_CAPTION = 'drain spend';

/**
 * The row's own caption, when the heading above it is not already saying it.
 * `undefined` rather than an empty string: it is spent on a `title` attribute,
 * and a blank tooltip is a tooltip.
 */
export function drainTitle(heading: string, label: string | null | undefined): string | undefined {
  const own = (label ?? '').trim();
  return own === '' || own === heading ? undefined : own;
}
