// What to DO about a node that is waiting (#2209).
//
// The epic snapshot's `stuck` line is the driver's own prose
// (`_waiting_reason` in kew/graph/driver/tick.py), and most of it already
// names the operator's next move: `awaiting_continuation` says "use `kew epic
// continue` or `kew epic abandon`", `awaiting_publication` says to open an
// external PR, `awaiting_no_change` says to confirm or reject. Exactly one
// does not: `DriverStatus.QUEUED` prints "waiting for human arm", which says
// a person is needed and nothing about what the person does.
//
// So this module rewrites that one line into the action, and leaves every
// other reason verbatim — the driver's wording is the authority, and a
// paraphrase here would be a second, drifting copy of it.
//
// DEPARTURE from #2209's example wording ("arm it: kew epic arm
// go-live-2026-09 SFD-223"): there is no `kew epic arm` command. Arming is a
// marker on the tracker item — `linear_admission_status`
// (kew/graph/adapters/linear_native.py) admits a node only once the issue
// carries both the `kew` membership label and the `kew:ready` arm label — and
// `NodeStatus.QUEUED` is only ever produced there. Printing a command that
// does not exist would send the operator to a "No such command", so the
// action names the marker instead.

/** The driver's fixed wording for `DriverStatus.QUEUED`. Must match
 * `_FIXED_WAITING_REASONS[DriverStatus.QUEUED]` in graph/driver/tick.py; the
 * rewrite below is keyed on it exactly. */
export const ARM_WAIT_REASON = 'waiting for human arm';

/** The arm marker the Linear adapter requires (`_LINEAR_READY_LABEL`). */
export const ARM_LABEL = 'kew:ready';

/**
 * The action for a node nobody has armed, or null when the driver's own reason
 * already names the move and the surface should print it verbatim.
 *
 * The null is the useful half: a surface that can only ADD to a line it is
 * already drawing — the node's own status line in the DAG (`statusSegments` in
 * attention.ts) — asks this, and so stays silent about the eight waiting states
 * the driver words for itself rather than repeating them beside the status word
 * they already start with.
 */
export function armAction(reason: string | null | undefined, nodeId: string): string | null {
  if ((reason ?? '').trim() !== ARM_WAIT_REASON) return null;
  return `arm it: add the ${ARM_LABEL} label to ${nodeId} in Linear`;
}

/**
 * The waiting line as the UI prints it: the action for a node nobody has
 * armed, and the driver's own reason for everything else.
 *
 * Returns the empty string for a node with no reason, so a caller tests one
 * value rather than juggling null.
 */
export function waitAction(reason: string | null | undefined, nodeId: string): string {
  const text = (reason ?? '').trim();
  return armAction(text, nodeId) ?? text;
}
