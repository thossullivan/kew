/**
 * #2304 — which roster rows get their strip re-read, and when.
 *
 * The epics roster polls every 5 seconds and can hold seventy epics; the loops
 * roster polls on the shell's pulse. A strip per row is one read per row, so
 * the naive version is seventy requests every five seconds on a page #2296
 * already had to make cheaper. Two bounds make it affordable, and both are
 * decisions rather than guesses — which is why they live here with a test
 * rather than inline in a page:
 *
 * 1. **A cap on rows.** Only the first `cap` rows of a shelf are strip-read.
 *    Both rosters are ordered with the rows that have moved at the top, so a
 *    row past the cap shows no strip rather than a wrong one. Neither list is
 *    virtualised, so this is a hard bound and not a viewport guess.
 * 2. **A stamp, not a timer.** A strip can only change when the thing ATTEMPTS
 *    a step, and both payloads carry evidence of that. A row is re-read when
 *    its stamp differs from the stamp its strip was read at, so a quiet roster
 *    asks for nothing at all on its second poll, however long it stays open.
 *    The word "attempt" is load-bearing: an epic's heartbeat instants only
 *    move for a tick that got past the lock, the preflight and the
 *    non-empty-graph guard, so the epic stamp leads with the server's
 *    attempt-log stamp instead (see `epicStripRows`).
 */
import type { EpicSummary, Loop } from '$lib/api';

/** How many rows of a shelf carry a strip. */
export const STRIP_ROW_CAP = 20;

/** A row reduced to the two things this decision needs. */
export type StripRow = { readonly key: string; readonly stamp: string };

/**
 * The keys whose strip must be read now: inside the cap, and either never read
 * or read before the row's latest movement.
 *
 * `read` maps key -> the stamp the currently-held strip was read at. Pass the
 * same map the fetch writes into and this converges.
 */
export function stripKeysToFetch(
  rows: readonly StripRow[],
  read: ReadonlyMap<string, string>,
  cap: number = STRIP_ROW_CAP
): string[] {
  return rows
    .slice(0, Math.max(0, Math.trunc(cap)))
    .filter((row) => read.get(row.key) !== row.stamp)
    .map((row) => row.key);
}

/**
 * An epic's stamp: its tick-ATTEMPT stamp first, then the newest instant the
 * server says its driver touched (START preferred over finish).
 *
 * The attempt stamp leads because the two instants cannot carry this on their
 * own. Both are written by the driver's `begin_epic_tick`, which `advance`
 * only reaches past the single-driver lock, the tool preflight and the
 * non-empty-graph guard — so a row whose driver is locked out, failing
 * preflight or finding an empty graph never moves either one, and its strip
 * would stay frozen on its last reading through exactly the attempts the strip
 * was added to show (review round 3). `tick_attempt_stamp` moves whenever a
 * row of `epic_tick_log` is opened or closed, which is the same fact the strip
 * is drawn from.
 *
 * The instants stay in the key behind it: a `kew serve` predating #2304 sends
 * no attempt stamp, and there the old signal is still better than none. An
 * epic that has never been driven stamps `':'`, which is itself a stable key.
 */
export const epicStripRows = (epics: readonly EpicSummary[]): StripRow[] =>
  epics.map((epic) => {
    const touched = epic.last_tick_started_at ?? epic.last_tick_ts ?? '';
    return { key: epic.epic_ref, stamp: `${epic.tick_attempt_stamp ?? ''}:${touched}` };
  });

/**
 * A loop's stamp. `LoopDTO` carries no iteration instant — the newest one is a
 * grouped read the roster does not make — so the stamp is the three fields that
 * DO move when a loop takes a step: its step count, its status and its own
 * `updated_at`. A loop that is finished and untouched stamps the same forever,
 * which is what stops the poll re-reading it.
 */
export const loopStripRows = (loops: readonly Loop[]): StripRow[] =>
  loops.map((loop) => ({
    key: loop.loop_id,
    stamp: `${loop.step_count}:${loop.status}:${loop.updated_at ?? ''}`
  }));

/**
 * Read the strips `stripKeysToFetch` names, landing each response only while
 * it is still the newest read of its row.
 *
 * The stamp is claimed BEFORE the request, so a poll arriving mid-flight does
 * not ask for the same strip twice. Claiming is not enough on its own: a row
 * that moves while its first read is slow starts a second read, and if the
 * second answers first the first then lands over it — an older strip under
 * the newer stamp, which nothing re-reads until the row moves again (#2844
 * review round 3). So a response lands only when `read` still holds the stamp
 * it was asked at, and a failure releases only its own claim.
 */
export function readStrips<T>(
  rows: readonly StripRow[],
  read: Map<string, string>,
  fetchStrip: (key: string) => Promise<T>,
  land: (key: string, strip: T) => void,
  cap: number = STRIP_ROW_CAP
): void {
  for (const key of stripKeysToFetch(rows, read, cap)) {
    const stamp = rows.find((row) => row.key === key)?.stamp;
    if (stamp === undefined) continue;
    read.set(key, stamp);
    fetchStrip(key)
      .then((strip) => {
        if (read.get(key) === stamp) land(key, strip);
      })
      .catch(() => {
        // A strip is an extra reading, never the row. Release the stamp so
        // the next poll retries, and leave the last good strip up.
        if (read.get(key) === stamp) read.delete(key);
      });
  }
}
