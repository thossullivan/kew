/**
 * #3051 — the KIND of a wait, in the words a needs-you head prints beside
 * "Needs you": "Held", "Merge decision".
 *
 * A head that printed the wait's reason ("the review round cap was reached")
 * said it twice, because the sentence under it is the server's and opens with
 * that same clause (`kew.service.attention.say_wait`). So the head says what
 * KIND of decision this is, and the reason is the sentence's alone. The Work
 * block and the Epic rail's Needs-you column both head with these words; they
 * used to be the rail's own table.
 */

/** The kinds `service/attention.py` composes, in the words a head prints. */
const KIND_TITLES: Record<string, string> = {
  merge_decision: 'Merge decision',
  approval: 'Approval hold',
  work_exception: 'Needs a decision',
  // `kew.waits`'s own kinds, composed from the epic's journaled node states and
  // its open holds (#2207). `kindTitle` would print any of them as words
  // anyway; naming them here is how they read as English rather than as a
  // de-underscored enum.
  merge_ready: 'Awaiting merge',
  held: 'Held',
  budget_exhausted: 'Held on budget',
  publication_held: 'Awaiting publication',
  sandbox_refused: 'Sandbox refused',
  host_restart_repeated: 'Host restarted twice'
};

/**
 * The words for one attention kind. An unrecognised kind is printed as words
 * rather than dropped; no kind at all is '' — a head with nothing beside
 * "Needs you" rather than "Needs you · Needs you".
 */
export function kindTitle(kind: string | null | undefined): string {
  const key = (kind ?? '').trim();
  if (Object.hasOwn(KIND_TITLES, key)) return KIND_TITLES[key];
  const words = key.replace(/[_-]+/g, ' ').trim();
  return words ? words[0].toUpperCase() + words.slice(1) : '';
}
