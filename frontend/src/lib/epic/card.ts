// What one DAG card prints (#2250, the UXEpic artboard's node card), as data.
//
// The card is small — 196x92 — so it says less than the popover does: ONE
// status word, the magenta "needs you" when a person is the next move, and
// how long the node has been in that state. Every reason — the one the driver
// recorded ("blocked · needs a design decision") and why a person is needed —
// is a sentence, and the popover is where sentences go; on the card it was
// clipped mid-word (#2543, D30: `IN REVIE… > REVIEW STALE`).
import {
  NEEDS_YOU_STATUS,
  needsYou,
  SEP,
  type NodeFacts,
  type StatusSegment
} from './attention';
import { elapsed } from './format';
import { displayId, labelTone, statusWord } from './render';
import { statusShape } from '$lib/factory/status';
import { parseInstant } from '$lib/time';

const SEP_COLOR = 'var(--text-5)';

/** "18d", "3h", "12m": the largest unit of `elapsed`, which is all a card has
 * room for. */
export function shortAge(ms: number): string {
  return /^\d+[a-z]+/.exec(elapsed(ms))?.[0] ?? '';
}

type CardFacts = NodeFacts & { status_since?: string | null };

/**
 * The head of every needs-you card's status row — `IN REVIEW · NEEDS YOU`,
 * `HELD · NEEDS YOU` — and the ONE place it is built (#3051 N7), whatever the
 * kind of wait: a merge decision, a hold, an approval.
 */
export function needsYouRow(status: string): StatusSegment[] {
  return [
    { text: statusWord(status), color: labelTone(status).status },
    { text: SEP, color: SEP_COLOR },
    { text: statusWord(NEEDS_YOU_STATUS), color: statusShape(NEEDS_YOU_STATUS).color }
  ];
}

/**
 * The card's status row, word by coloured word.
 *
 * The status word alone, in the tone the status line gives it. Attention makes
 * it `needsYouRow` — the magenta "needs you" WITHOUT its reason — and a node
 * that is not finished adds its age. No reason is printed: `NodePopover`
 * carries both.
 */
export function cardStatus(node: CardFacts, nowMs: number): StatusSegment[] {
  const out: StatusSegment[] = needsYou(node)
    ? needsYouRow(node.status)
    : [{ text: statusWord(node.status), color: labelTone(node.status).status }];
  const since = parseInstant(node.status_since ?? null);
  if (since !== null && node.status !== 'done' && node.status !== 'cancelled') {
    const age = shortAge(nowMs - since);
    if (age !== '') {
      out.push({ text: SEP, color: SEP_COLOR });
      out.push({ text: age, color: 'var(--text-2)' });
    }
  }
  return out;
}

/**
 * The longest status word that leaves room for a staleness badge's label on
 * the same row. The row is 170px of 11px mono at 0.08em tracking (~7.9px a
 * character) and a labelled badge is ~101px plus its 6px gap, which leaves
 * 63px: seven characters. `IN REVIEW` next to `REVIEW STALE` was the row D30
 * found cut to `IN REVIE…`.
 */
export const BADGE_WORD_MAX = 7;

/** Whether a card can print its badge's label beside its status word, or has
 * to draw the badge as its mark alone (the label is still its `aria-label`,
 * and the popover says it in full). */
export function badgeLabelFits(status: string): boolean {
  return statusWord(status).length <= BADGE_WORD_MAX;
}

/**
 * Whether the card's staleness badges sit on its status row, or are drawn as
 * their marks in the id row instead.
 *
 * On the status row, the row's 170px is shared with them, and a needs-you
 * card's `IN REVIEW · NEEDS YOU` is 21 characters: beside a badge the row ran
 * out of room and "needs you" wrapped out of sight, so #2055 read `IN REVIEW`
 * alone in magenta (#3051 N7). A needs-you card keeps the whole row for the
 * words, and its badges are marks beside the id — as they are zoomed out. The
 * label is still each mark's `aria-label`, and the popover says it in full.
 */
export function badgesOnRow(node: NodeFacts, badgeCount: number, zoomedOut: boolean): boolean {
  return !zoomedOut && badgeCount > 0 && !needsYou(node);
}

/** One word of the card's status row, with the separator that leads it. */
export type CardWord = { lead: StatusSegment | null; word: StatusSegment };

/**
 * `cardStatus` regrouped so every separator travels with the word after it.
 * The row drops whole words when it runs out of room (a badge beside it, a
 * long status), and a separator left at the end of the line, or a word cut
 * in half, is the clipping D30 is about.
 */
export function cardWords(segs: readonly StatusSegment[]): CardWord[] {
  const out: CardWord[] = [];
  let lead: StatusSegment | null = null;
  for (const seg of segs) {
    if (seg.text === SEP) {
      lead = seg;
      continue;
    }
    out.push({ lead, word: seg });
    lead = null;
  }
  return out;
}

/**
 * The id range a collapsed chain covers, with the shared prefix said once:
 * "SFD-251 – 255", "#2244 – 2246". Members are in reading order, so the
 * range is first to last as the graph reads, not a numeric min/max.
 */
export function idRange(ids: readonly string[]): string {
  if (ids.length === 0) return '';
  const first = displayId(ids[0]);
  if (ids.length === 1) return first;
  const last = displayId(ids[ids.length - 1]);
  const cut = Math.max(first.lastIndexOf('-'), first.startsWith('#') ? 0 : -1);
  const prefix = cut >= 0 ? first.slice(0, cut + 1) : '';
  const tail = prefix !== '' && last.startsWith(prefix) ? last.slice(prefix.length) : last;
  return `${first} – ${tail}`;
}
