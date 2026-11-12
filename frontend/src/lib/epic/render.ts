// Pure render helpers for DagView.svelte (plan §FUI-15,
// frontend/design/factory-ui).
//
// Kept out of the .svelte file so they're unit-testable: this repo has no
// component-render test infra (see the brief — DagView is verified visually
// against the DEpic artboard, x 40–1400, y ≈189–669).
//
// Everything here is either GEOMETRY the artboard measures or a lookup that
// DEFERS to `$lib/factory/status`. Nothing in this file decides what a status
// looks like any more: FUI-04 made `statusShape` the one authority, and the
// magenta `in_progress` this file used to hand out was exactly the rationing
// leak that motivated it (Factory UI plan finding 3 — magenta means "a human is
// needed", and work in progress needs nobody).
import { statusShape, type StatusMeaning } from '$lib/factory/status';

/**
 * The `DriverStatus` words this module has always coloured.
 *
 * A subset of the fourteen the snapshot can serve, and deliberately still a
 * subset: `STATUS_COLOR` is a convenience map for callers iterating a count
 * table, never the authority (factory/status.test.ts pins that it stays
 * strictly smaller than `DriverStatus`). Use `statusColor` for a status off
 * the wire — it answers for all fourteen.
 */
const COLOURED_STATUSES = [
  'todo',
  'in_progress',
  'in_review',
  'done',
  'failed',
  'held',
  'blocked',
  'cancelled'
] as const;

/** Status → CSS custom property, sourced from `STATUS_MARKS` via
 * `statusShape` so the DAG and every other factory surface cannot drift
 * apart. Frozen: it is one shared object handed to every caller. */
export const STATUS_COLOR: Record<string, string> = Object.freeze(
  Object.fromEntries(COLOURED_STATUSES.map(s => [s, statusShape(s).color]))
);

/** The colour for any status word the server can serve, not just the eight
 * above. An unrecognised status degrades to the todo mark's colour rather
 * than throwing — snapshots are server-driven, and forward compat with a new
 * status name should render honestly, not crash the view. */
export function statusColor(status: string): string {
  return statusShape(status).color;
}

/** The status word as the node's status line prints it ("in review", not
 * `in_review`). The mark beside it carries the shape, so unlike the old
 * in-card caption this never smuggles a glyph into the text. */
export function statusWord(status: string): string {
  return statusShape(status).word;
}

/**
 * Statuses whose label is dimmed: settled history, and work nobody has
 * started. The artboard draws `done` AND `todo` dim (#2000 and #2040 are the
 * same two greys) and everything else bright, which is the useful reading —
 * the bright labels are exactly the nodes with something happening to them.
 *
 * `queued` belongs with `todo` and not with the bright half (#2209): it is a
 * node nobody has armed, so nothing is happening to it — and the active branch
 * below would paint its status line in the `queued` mark's own `--text-4`,
 * which app.css documents as NON-TEXT (3.4:1 on --ink, 2.7:1 on the light ink;
 * `tokens.test.ts` pins both as failing AA). A mark may be drawn in it; a word
 * may not.
 */
const DIM_MEANINGS: ReadonlySet<StatusMeaning> = new Set<StatusMeaning>([
  'todo',
  'queued',
  'done',
  'cancelled'
]);

export type LabelTone = {
  /** Colour of the `#1234` id span. */
  id: string;
  /** Colour of the title span. */
  title: string;
  /** Weight of the title span — 400 dim, 500 bright. */
  weight: number;
  /** Colour of the 11px status line under the label. */
  status: string;
  /** False for the dim (todo/queued/done/cancelled) treatment. */
  active: boolean;
};

/**
 * The label's two-tone treatment, measured off the DEpic artboard: a done or
 * todo node is `--text-3` id over a `--text-2` title at weight 400, and an
 * active one is `--text-1` throughout at weight 500.
 *
 * The status line follows the same split rather than always taking the mark's
 * colour: "done" in green under every finished node would spend the loudest
 * ink in the graph on the part of it nobody needs to look at.
 */
export function labelTone(status: string): LabelTone {
  const mark = statusShape(status);
  if (DIM_MEANINGS.has(mark.meaning)) {
    return {
      id: 'var(--text-3)',
      title: 'var(--text-2)',
      weight: 400,
      status: 'var(--text-3)',
      active: false
    };
  }
  return {
    id: 'var(--text-1)',
    title: 'var(--text-1)',
    weight: 500,
    status: mark.color,
    active: true
  };
}

/** Node ids are driver ints today ("594") but external refs ("repo#5")
 * already carry their own shape — only bare ints get the # prefix. */
export function displayId(id: string): string {
  return /^\d+$/.test(id) ? `#${id}` : id;
}
