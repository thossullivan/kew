/**
 * #2208 — what one epic-hold reason IS, in the operator's words.
 *
 * An `epic_holds` row reaches the console as a `budget_hold` dashboard row
 * whatever held the node: the type is historical (the budget preflight was the
 * first writer of that table) and the only thing in the payload that says what
 * actually happened is the reason string the driver wrote. Monitor's first-pass
 * column read the type as the category and offered the queue's approve on it,
 * so a round-cap hold was drawn as
 *
 *     BUDGET · review_rounds_exhausted · Approve this hold
 *
 * — the wrong category, the raw reason in place of words, and a button that
 * cannot clear it: `/api/approvals` decides the GitHub needs-approval LABEL
 * (`kew.approval.remote_approve`, keyed on `config.approval.label`) and has
 * never touched an `epic_holds` row, so that write answers `not_held`.
 *
 * So this table reads the REASON, and it mirrors one owner:
 * `kew/src/kew/epic_holds.py`, the hold-reason vocabulary, plus the operator
 * wording `kew/src/kew/waits.py` already attaches to it for the CLI, the TUI
 * and the epic legend. `kew/tests/test_hold_category_parity.py` holds the two
 * halves together — a prefix added in Python with no entry here would otherwise
 * read as `other` on this surface alone, quietly.
 *
 * Three rules.
 *
 * **No default category.** A reason this table has never heard of reads as
 * `other` and prints the raw reason the driver wrote. Filing it under a
 * category this file invented is exactly the failure #2208 names, and `other`
 * plus the real string is the only honest reading of a vocabulary that grows.
 *
 * **The action is the one that actually clears the hold.** A budget hold names
 * the key to raise — read off the reason, because the server writes it there
 * (`_budget_hold_reason`, `review_budget_hold_reason`). Everything else clears
 * with `kew epic unhold`, named with its epic and item so it can be run as
 * printed. The CI gate gets nothing: an operator cannot make a check finish,
 * which is why `classify_wait` returns no attention for it at all.
 *
 * **The sentence says each thing once.** The words come from the table and the
 * detail from the reason's own tail, less the `— raise <key>` clause that has
 * become the action.
 */

/** The kind column's word. `other` is the honest reading, never a default. */
export type HoldCategory = 'budget' | 'review' | 'sandbox' | 'dispatch' | 'merge' | 'ci' | 'other';

/** The epic and item a hold is on, as the row carries them. */
export type HoldPlace = {
  /** `epic_ref`; '' when the payload names none. */
  epic: string;
  /** The durable item id (never `#`-prefixed — this is a command line). */
  item: string;
};

export type HoldWords = {
  category: HoldCategory;
  /** The reason in words; '' when no entry claims it, so `detail` stands alone. */
  label: string;
  /** What the reason itself adds: numbers, paths, the layer that refused. */
  detail: string;
  /** The action that clears it; '' when nobody can act (the CI gate). */
  action: string;
};

/**
 * One reason prefix, as `epic_holds.py` writes it.
 *
 * `action` is a template over two tokens, so the table reads as the sentences
 * it produces: `{cap}` is the key the reason names and `{unhold}` the
 * `kew epic unhold` for this row's epic and item.
 */
export type HoldSpec = {
  prefix: string;
  category: HoldCategory;
  label: string;
  action: string;
};

/** The fallback for a HELD node no prefix claims — `waits.UNBUCKETED_HELD_HINT`. */
export const UNKNOWN_HOLD_ACTION = 'fix the cause, then kew epic unhold';

/** What a hold with no reason recorded says, rather than an empty row. */
export const NO_HOLD_REASON = 'held, with no reason recorded';

/**
 * Every prefix `kew.epic_holds` defines, with the words and the action.
 *
 * Order is significant — first match wins, as in `waits.bucket_for` — and the
 * suite holds it to the one invariant that makes that safe: no prefix here may
 * be a prefix of a LATER one, or the later entry could never be reached.
 */
export const HOLD_SPECS: readonly HoldSpec[] = [
  {
    prefix: 'ci_pending',
    category: 'ci',
    label: 'CI checks are still running',
    // Deliberately empty: the gate clears itself, and `classify_wait` answers
    // None for it precisely so no surface puts a person's move on it.
    action: ''
  },
  {
    prefix: 'budget_preflight',
    category: 'budget',
    label: 'held on the dispatch budget',
    action: 'raise {cap}'
  },
  {
    prefix: 'review_budget',
    category: 'budget',
    label: 'held on the review budget',
    action: 'raise {cap}'
  },
  {
    // A daily or weekly cap stopped a review or a remediation (#3205). The
    // reason names the window, not a key, so the action names both caps.
    prefix: 'budget:',
    category: 'budget',
    label: 'held on the daily or weekly cap',
    action: 'wait for the window to move on, or raise budget.daily_usd or weekly_usd'
  },
  {
    prefix: 'review_rounds_exhausted',
    category: 'review',
    label: 'the review round cap was reached',
    // The one hold an unhold alone recovers, and the one that needs saying:
    // the release restarts the review cycle in the same transaction
    // (`REVIEW_CYCLE_RESET_HOLD_REASONS`), because the count the cap is read
    // from only restarts at a retry marker.
    action: '{unhold} — it restarts the review cycle'
  },
  {
    prefix: 'pre_review_gate_exhausted',
    category: 'review',
    label: "the head kept failing the merge gate's checks before review",
    // The pre-review gate's own bound (#3201); like the round cap, the release
    // restarts the review cycle.
    action: 'fix the check its reason names, then {unhold} — it restarts the review cycle'
  },
  {
    prefix: 'review_sandbox_refused',
    category: 'sandbox',
    label: 'the review sandbox refused the launch',
    action: 'run the build or mapping its reason names, then {unhold}'
  },
  {
    prefix: 'review_launch_failed',
    category: 'review',
    label: 'the reviewer never started',
    action: 'fix what its reason names, then {unhold}'
  },
  {
    prefix: 'review_multiple_results',
    category: 'review',
    label: 'the reviewer answered more than once',
    action: 'read the reviewer log its run names, then {unhold}'
  },
  {
    prefix: 'review_failed_no_verdict',
    category: 'review',
    label: 'the reviewer run failed without a verdict',
    action: 'read the exit and reviewer log its reason names, then {unhold}'
  },
  {
    prefix: 'review_gate_paths_unread',
    category: 'review',
    label: 'the merge gate paths of the head could not be read',
    action: 'fix the checkout or git failure its reason names, then {unhold}'
  },
  {
    prefix: 'review_rate_limit_exhausted',
    category: 'review',
    label: 'the reviewer usage limit is not resetting',
    action: 'raise the provider usage limit or pin [review] runner to another provider, then {unhold}'
  },
  {
    prefix: 'review_crash',
    category: 'review',
    label: 'the reviewer crashed',
    action: 'fix what its reason names, then {unhold}'
  },
  {
    prefix: 'review_unparseable',
    category: 'review',
    label: 'the reviewer verdict could not be read',
    action: 'fix what its reason names, then {unhold}'
  },
  {
    prefix: 'review_pr_closed',
    category: 'review',
    label: 'the PR under review was closed',
    action: 'reopen or replace the PR, then {unhold}'
  },
  {
    prefix: 'review_residuals_unfiled',
    category: 'review',
    label: 'the review left residuals nobody filed',
    action: 'file the residuals, then {unhold}'
  },
  {
    prefix: 'sandbox_containment',
    category: 'sandbox',
    label: 'the runner could not be contained',
    action: 'map [sandbox] images.<runner>, then {unhold}'
  },
  {
    // #3193. A remediation refused for its PR head's own lineage. Before the
    // bare prefix it extends: this hold clears itself the first tick the PR
    // head moves, so the move is a pushed fix; the unhold stays available.
    prefix: 'admission_refused@',
    category: 'review',
    label: 'the PR head was refused',
    action: 'push a fix (the hold clears when the PR head moves), or {unhold}'
  },
  {
    prefix: 'admission_refused',
    category: 'dispatch',
    label: 'admission refused the dispatch',
    action: 'fix what its reason names, then {unhold}'
  },
  {
    prefix: 'credential_issuer_refused',
    category: 'dispatch',
    label: 'the credential issuer refused the route',
    action: 're-check the issuer its reason names, then {unhold}'
  },
  {
    prefix: 'preexisting_worktree_refused',
    category: 'dispatch',
    label: 'a worktree for this node is already on disk',
    action: 're-check the worktree its reason names, then {unhold}'
  },
  {
    prefix: 'base_integration_conflict',
    category: 'merge',
    label: 'the base branch no longer merges into the branch',
    action: 'resolve the conflict on the branch, then {unhold}'
  },
  {
    prefix: 'base_integration_failed',
    category: 'merge',
    label: 'the base-integration step could not run',
    action: 'fix what its reason names, then {unhold}'
  },
  {
    prefix: 'merge_gate_touched',
    category: 'merge',
    label: "the PR edits the merge gate's own definition",
    action: 'read the diff, then {unhold}'
  },
  {
    // #2362. The remediation ran and spent its money; what is held is its
    // PUBLICATION, for a protected path in its diff. The detail is the
    // preserved checkout, which is the thing to open.
    prefix: 'publication_held',
    category: 'review',
    label: "a remediation's publication is waiting on a human",
    action: 'publish or discard the preserved checkout, then {unhold}'
  },
  {
    // #2872. The round rejected only undeclared test edits, and a remediation
    // already ran at this head and declared nothing. The detail names the
    // tests; the fix is a Behavior changes line per test in the PR body.
    prefix: 'test_declarations_missing',
    category: 'review',
    label: 'edited tests are waiting on a Behavior changes line',
    action: 'add a line per named test to the PR body, then {unhold}'
  },
  {
    // #3233. A remediation of a standing NEEDS_CHANGES round ran and moved
    // nothing; the detail names the findings left standing.
    prefix: 'remediation_no_change',
    category: 'review',
    label: 'a remediation changed nothing and the findings still stand',
    action: 'read the findings, fix the branch or answer them, then {unhold}'
  },
  {
    // #3475. A remediation at the head pushed nothing and left nothing that
    // earns a re-review there; the detail names the head and the round.
    prefix: 'remediation_no_progress',
    category: 'review',
    label: 'a remediation pushed nothing and the verdict still stands',
    action: 'read the round, fix the branch or the PR body, then {unhold}'
  },
  {
    // #2872. A NEEDS_CHANGES round's flagged-test record could not be written,
    // so the round was not recorded; the next review writes it again.
    prefix: 'declaration_record_failed',
    category: 'review',
    label: "a review round's flagged-test record was not written",
    action: 'fix the log directory the reason names, then {unhold}'
  },
  {
    // #2872. The PR body stayed unreadable after a declaration round's
    // remediation. The review stage clears this hold itself once it reads.
    prefix: 'pr_body_unreadable',
    category: 'review',
    label: 'the PR body could not be read',
    action: 'check gh auth and the network; the hold clears itself'
  },
  {
    // #2731. The implementer disputes a test in its acceptance contract
    // instead of editing it; a person amends the contract or refers it back.
    prefix: 'acceptance-disputed',
    category: 'dispatch',
    label: 'the implementer disputes its acceptance contract',
    action: 'run kew epic acceptance amend, by hand or with --author'
  },
  {
    // #3191. The node ran out of a resource again after the driver spent an
    // automatic resource retry (at that head, or the cap across heads); the
    // release leaves it failed for a retry by hand.
    prefix: 'resource_retries_spent',
    category: 'dispatch',
    label: 'it ran out of a resource again after an automatic retry',
    action: 'find the cause, then {unhold} and kew epic retry'
  }
];

/** The first spec whose prefix the reason carries — `waits.bucket_for`. */
function specFor(reason: string): HoldSpec | null {
  return HOLD_SPECS.find((spec) => reason.startsWith(spec.prefix)) ?? null;
}

/**
 * A config key named for raising, read off the reason the server wrote:
 * `budget.per_issue_usd`, `[review].max_review_usd`. '' when it names none —
 * an older serve, or a writer that stopped saying it — so the action can say
 * so instead of printing a key nobody wrote.
 */
export function capKeyOf(reason: string): string {
  const named = /\braise\s+(\[[a-z]+\]\.[A-Za-z0-9_]+|[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/.exec(
    reason
  );
  return named ? named[1] : '';
}

/**
 * The `kew epic unhold` for one row, as the operator can run it.
 *
 * Narrowed by what the row actually names: a hold whose payload carries no epic
 * leaves the arguments off rather than printing a placeholder somebody would
 * paste.
 */
export function unholdCommand(place: HoldPlace): string {
  const args = [place.epic, place.epic ? place.item : ''].filter((arg) => arg !== '');
  return ['kew epic unhold', ...args].join(' ');
}

/**
 * The reason's own contribution: everything after the `<prefix>: ` the table
 * matched, less the trailing `— raise <key>` clause, which has become the
 * action and must not be said twice.
 *
 * The `merge_gate_touched@<head>` separator is cut with the prefix: the head
 * the hold was written against is bookkeeping for the merge queue's own
 * receipt, not something a reader of this row can act on.
 */
function detailOf(reason: string, spec: HoldSpec): string {
  // A head-scoped prefix (`admission_refused@`) ends on the separator itself,
  // so the head that follows it is cut the same way.
  const after = spec.prefix.endsWith('@')
    ? `@${reason.slice(spec.prefix.length)}`
    : reason.slice(spec.prefix.length);
  // Only the head separator is cut, and only where it stands: a reason tail is
  // untrusted text that may carry colons of its own (a stderr tail, a path),
  // and a blanket "drop everything before the first colon" would eat it.
  const tail = after.startsWith('@') ? after.replace(/^@[^:\s]*/, '') : after;
  return tail
    .replace(/^[:\s]+/, '')
    .replace(/\s*[—-]\s*raise\s.*$/, '')
    .trim();
}

/** The template's two tokens, filled from the reason and the row. */
function actionOf(action: string, reason: string, place: HoldPlace): string {
  return action
    .replace('{cap}', capKeyOf(reason) || 'the cap its reason names')
    .replace('{unhold}', unholdCommand(place));
}

/**
 * What an unclaimed reason earns: `other`, the raw reason, and the same hint
 * the CLI and TUI print for an unbucketed hold — said with this row's own
 * epic and item, because the command is the whole of the hint.
 */
const UNKNOWN_SPEC: HoldSpec = {
  prefix: '',
  category: 'other',
  label: '',
  // Written as the hint itself, with the command turned into the token, so the
  // one string this file and `waits.UNBUCKETED_HELD_HINT` must agree on is
  // written once (`kew/tests/test_hold_category_parity.py` compares them).
  action: UNKNOWN_HOLD_ACTION.replace('kew epic unhold', '{unhold}')
};

/**
 * One hold reason as a category, a sentence and an action.
 *
 * Unknown is `other` with the raw reason and the unbucketed hint — never a
 * guessed category, which is the whole of #2208.
 */
export function describeHold(reason: string, place: HoldPlace): HoldWords {
  const text = reason.trim();
  const spec = specFor(text);
  if (spec === null) {
    return {
      category: UNKNOWN_SPEC.category,
      label: UNKNOWN_SPEC.label,
      detail: text || NO_HOLD_REASON,
      action: actionOf(UNKNOWN_SPEC.action, text, place)
    };
  }
  return {
    category: spec.category,
    label: spec.label,
    detail: detailOf(text, spec),
    action: actionOf(spec.action, text, place)
  };
}

/** The row's sentence: the words, then whatever the reason itself added. */
export function holdSentence(words: HoldWords): string {
  if (words.label === '') return words.detail;
  return words.detail === '' ? words.label : `${words.label}: ${words.detail}`;
}

/**
 * What to say under the command, given the hold table's action for the reason.
 *
 * The table's action is a whole sentence with the command inside it — `{unhold}
 * — it restarts the review cycle`, `map [sandbox] images.<runner>, then
 * {unhold}` — and the command is already printed above in its own block. So the
 * command is cut out of the front where the action begins with it, leaving the
 * clause that says what the release ALSO does; where the action does not begin
 * with it, the action stands whole, because then it names something that has to
 * happen before the command will help.
 */
export function commandNote(action: string, command: string): string {
  const said = action.trim();
  if (said === '') return '';
  if (command !== '' && said.startsWith(command)) {
    const rest = said.slice(command.length).replace(/^[\s—-]+/, '').trim();
    return rest;
  }
  return said === command ? '' : said;
}
