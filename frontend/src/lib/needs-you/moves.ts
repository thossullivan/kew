/**
 * Which served link is a needs-you block's one magenta move (design review c9,
 * D10).
 *
 * A wait no kew command clears has its move served as a link instead: a merge
 * decision's "Open PR #2059 to merge" (#3467 D24). When a merge verify failed
 * at that PR's head (#3460), the server also serves a link to the failure, and
 * the safest next move is to read it — not to open a pull request kew has just
 * seen fail and merge it. So the failure is the move, and the PR link follows
 * it, quiet: it is still where the decision is made once the failure is read.
 *
 * Work's block and the Epic rail's block both ask this, so the two cannot
 * disagree about which control is the primary.
 */
import type { BlockLink, MoveLink } from '$lib/needs-you/block';

/** The Do this links, in order, with exactly one marked `move` (or none at all). */
export function linkMoves(verify: BlockLink | null, served: BlockLink | null): MoveLink[] {
  if (verify !== null) {
    return [{ ...verify, move: true }, ...(served === null ? [] : [{ ...served, move: false }])];
  }
  return served === null ? [] : [{ ...served, move: true }];
}
