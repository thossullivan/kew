/**
 * The rule the hero states, and the channels it holds on.
 *
 * kew treats the agent as an untrusted author: everything it emits is input,
 * and a change never defines its own proof. Each row names one thing the
 * agent emits and what kew does with it instead of trusting it. The wording
 * is checked against the code and the docs, not written for effect:
 *
 *   repository   canonical publication (kew/src/kew/publication_canonical.py);
 *                kew does run hardened read-only git in agent checkouts
 *                elsewhere, so the row says publication, not "never git"
 *   test edits   the held-out phase of the merge verify (docs/cli/verify.md,
 *                "Held-out base tests"), wired to kew's own repository layout
 *   cost report  monotonic usage on the contained route only
 *                (kew/src/kew/dispatch_sandbox.py)
 *   test reason  the Behavior changes declaration (docs/how-it-works.md)
 *   verdict      governed review (docs/how-it-works.md)
 *   gate edits   [epic] merge_gate_paths (docs/how-it-works.md)
 *
 * None of the rows holds on every route. `scope` names where a row that needs
 * no setting holds (docs/proof.md: publication and the cost cap on the
 * contained dispatch route, the merge verify on kew's own repository layout).
 * `optIn` marks a row that holds only once a setting is on, and `setting` names
 * that row's own setting, as docs/proof.md states it: governed review for the
 * reasons and the verdict, the merge queue with `[epic] merge_gate_paths` for
 * the gates. The hero draws the rows that need no setting first and folds the
 * opt-in ones under one line. Plain JavaScript so the browser check imports
 * the same rows the page renders.
 */

export const PROOF_RULE = 'A change never defines its own proof.';

/** @typedef {{ emits: string; kew: string; scope?: string; optIn?: boolean; setting?: string }} Channel */

/** @type {Channel[]} */
export const PROOF_CHANNELS = [
  {
    emits: 'Its git repository',
    kew: 'Does not publish from it. Reads the files as data, rebuilds the commit on the host and scans it before the push.',
    scope: 'On the contained route.'
  },
  {
    emits: 'Its test edits',
    kew: 'Runs the base branch’s copy of each edited test against the new code.',
    scope: 'In the merge verify, on kew’s own repository layout today.'
  },
  {
    emits: 'Its cost report',
    kew: 'The cap is priced from usage that only increases.',
    scope: 'On the contained route.'
  },
  {
    emits: 'Its reason for a test change',
    kew: 'Only a line the reviewer accepts becomes a declaration.',
    optIn: true,
    setting: '[review] adversarial = true'
  },
  {
    emits: 'The code under review',
    kew: 'The verdict comes only from the reviewer’s own final result event, bound to the head commit.',
    optIn: true,
    setting: '[review] adversarial = true'
  },
  {
    emits: 'Its edits to the gates',
    kew: 'Held: two independent reviews, then a human release.',
    optIn: true,
    setting: '[epic] merge_queue = true, over the files in [epic] merge_gate_paths'
  }
];

/** The rows that need no setting, in order; each names where it holds. */
export const PROOF_ALWAYS = PROOF_CHANNELS.filter((row) => !row.optIn);

/** The rows an opt-in setting adds, folded under PROOF_OPT_IN_SUMMARY. */
export const PROOF_OPT_IN = PROOF_CHANNELS.filter((row) => row.optIn);

/** The one line the opt-in rows fold under. */
export const PROOF_OPT_IN_SUMMARY =
  'With the opt-in settings on: reasons, verdicts and gate edits are checked too';

/** The docs page that walks each channel, and the page that says what kew does not defend. */
export const PROOF_DOCS = '/docs/proof/';
export const THREAT_MODEL_DOCS = '/docs/threat-model/';
