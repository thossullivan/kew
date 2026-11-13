# CLI exit codes

kew uses one two-axis scheme: **did it run**, and **what did it find**?

| Code | Did it run? | What did it find? |
| --- | --- | --- |
| `0` | Yes | Nothing to act on: success, an empty queue, no drift, or no findings. |
| `1` | Yes | Findings or a performed repair: drift, missing lifecycle rows, audit findings, or reported verification failures. |
| `2` | No | An operational failure or missing prerequisite prevented the command from running. |

Configuration loading, validation, snapshot, and effective-resolution failures return `2`.
GitHub query failures and pricing fetch failures return `2`.
Informational output and explicitly advisory lifecycle warnings do not constitute findings.

`kew audit verify` already follows this scheme.
Its existing `0`/`1`/`2` contract is the model for other commands.

## Command families

Every family inherits code `2` for configuration failures.
The table lists command-specific outcomes.
An em dash means the family has no separate finding outcome.
Normal successful writes, exports, and clean shutdowns return `0`.

| Command family | `0` | `1` | `2` |
| --- | --- | --- | --- |
| `approve` | Approval recorded; issue already unheld | — | Authorization refusal; GitHub query failure; missing audit database; configuration failure |
| `attach` | Stream read successfully | — | Missing run or log; configuration failure |
| `audit` | Intact chain, including empty log; successful export or anchor | Tamper, anchor, or policy findings | Missing or unreadable ledger or anchors; configuration failure |
| `backup` | Archive written | — | Missing database; configuration failure |
| `canary` | Every requested runner passed | Runner verification drift | Runner unavailable or incomplete check without drift; configuration failure |
| `ci-fix` | Successful dispatch; no CI repair needed | Reported dispatch failure | PR lookup query failure; unavailable CI logs; Preparation failure; configuration failure |
| `cleanup` | No orphaned states or stale records | Orphaned states found or repaired; stale records repaired | Issue query failure; invalid reset target; configuration failure |
| `config` | Effective configuration resolved | — | Configuration failure |
| `delegate` | Clean watcher shutdown | — | Invalid tracker or missing assignee; unavailable Linear authentication; configuration failure |
| `deny` | Denial recorded; issue already unheld | — | Authorization refusal; GitHub query failure; missing audit database; configuration failure |
| `dispatch` | Successful dispatch; approval hold or declined estimate | Reported dispatch findings or failure | GitHub query failure; admission refusal for a named issue; pre-launch failure; configuration failure |
| `doctor` | Checks passed or skipped; live boundary confirmed | Diagnostic findings; live probe refuted boundary | Live confirmation refused or declined; configuration failure |
| `drive` | Drive stopped after its current tick; pause, resume or stop recorded; detached drive started; status of a running drive; a start that repeats a recorded schema refusal stays down | Another drive already runs; no drive running to stop; status finds no drive running, or a stalled one | Lost the drive lock to another drive; unknown epic; missing or unusable ledger; missing or changed kew binary; configuration failure |
| `run-request` | Request queued, or the same command already queued; requests listed; request shown | No drive running to pick the request up; no such request | A command a request cannot carry; unusable ledger; configuration failure |
| `epic` | Complete; successful graph operation; no epic in scope; Watch found nobody waiting; Successful child dispatch; Epic driven elsewhere; Archive decision recorded | Stalled with operator repair required; graph or node findings; Planning execution or validation failure; Executed child failure; Retry recorded with no live driver; Watch emitted an operator-attention event; Archive decision already in effect | Unavailable tracker; configuration failure; missing driver prerequisite; Planning pre-launch refusal; Child preflight, identity, or admission refusal; Post-creation GitHub query failure; Retry refused on a held node; Retry refused for a non-approver; Operator merge refused by a gate; Archive target unknown to the ledger; Send-back refused |
| `export` | Export or purge completed; no matching records | — | Missing database; invalid format, date, or option combination; configuration failure |
| `finalize` | Run finalized; reconciled run's head claimed | Run state changed before the repair committed | Unknown run or state other than awaiting_publication; reconciled run whose head was not claimed; configuration failure |
| `gc` | Listing printed; everything listed removed | A listed path or tag could not be removed | Issue or PR state unreadable; open PRs unreadable; unusable or unregistered ledger; work root or ledger scope refused; agent image inventory or runtime unreadable with `--images`; no git checkout with `--apply`; gh or git missing; configuration failure |
| `health` | Healthy checks, with advisory warnings allowed | Failed health checks | Missing or unreadable run store; invalid window; configuration failure |
| `hosts` | Host provisioned and its versions recorded; Named lease dropped | — | Unknown host; recipe kew does not ship; unreachable host; recipe self-check failure; Live lease holder on this machine; lease holder's job not proved stopped; configuration failure |
| `ingest` | Metrics ingested; clean receiver shutdown | — | Missing, unreadable, or invalid input; ingestion failure; disabled receiver; configuration failure |
| `init` | kew.toml written; setup and every check passed | kew.toml written; setup or a readiness check needs fixing | Existing kew.toml without --force; unknown runner; the file would not load; confirmation declined; no terminal and no --yes; configuration failure |
| `kill` | Run stopped; run already exited or no longer pending | — | Unverifiable process identity; missing run; configuration failure |
| `loop` | Pause completed; Resume completed | Resume needs human or failed verification; Resume budget limited | Loop operation refusal; Resume prerequisite or query failure; configuration failure |
| `merge` | PR queued, or already queued at that head; queue listed; entry removed; a queue pass settled or requeued its entries, or found the queue empty; merge check passed or overridden | PR to remove is not queued; merge check refused the head's review verdict | PR not open at that head, or queued at another head; head not a full commit id; queue pass held (lock taken, forge or git unreadable, verify host gone); merge check ledger missing or unreadable, or a review verdict row in it undecodable; blank override reason; configuration failure |
| `model-eol` | Every model has a row; advisory lifecycle states allowed | Missing lifecycle rows | Configuration failure |
| `models` | Refresh written; dry run; nothing to change | — | Source fetch failure; fetched value refused; configuration failure |
| `no-change` | Decision recorded: outcome confirmed, rejected, or its sync retried | — | Authority refusal for the resolved forge login; no live outcome for the item; outcome state does not admit the command; item id is not canonical; base moved under a confirm; missing reason; missing ledger; configuration failure |
| `plan` | Plan printed | — | Missing ledger; unreadable ledger; no plan recorded for the item; unreadable stored plan; configuration failure |
| `prompts` | Listing or preview printed; decision recorded: profile proposed, approved, declined, or retired | — | Approver refusal; stale generation; version not proposed; role takes no profile; no active profile to retire; empty or overlong addendum; unreadable addendum file; lost ledger (database gone, registry or anchors kept); unknown role; configuration failure |
| `publication` | Head attested and bound to its producer | — | Sha is not the remote head; parent binds to no producer; head already claimed by another run; unreadable claims on the branch; unreadable remote or commit; malformed arguments; configuration failure |
| `quota` | Hold cleared; no active hold, nothing cleared | — | Unknown runner; configuration failure |
| `rename-repo` | Dry run printed; rename applied and every run report moved; nothing to do | A run report could not be moved; the ledger rolled back | Rename refused by live work or a key collision; rename already recorded; nothing holds the old slug; old name not shown as the new one on GitHub; invalid slug; empty or unchanged project.repo; new repository not on GitHub; missing ledger; configuration failure |
| `report` | Report or evidence pack generated; Outcome synchronization completed; Feature provenance written | — | Configuration failure; GitHub query failure after partial report output; GitHub query failure; Repository unavailable; Webhook delivery failure; Unusable features file, corpus, repo, run store or output directory |
| `resolve` | Conflict dispatch succeeded; no conflicts | Runner or publication failure | Pre-launch refusal (not_run); configuration failure |
| `restore` | Restore completed | Archive verification findings | Schema too new; database locked; existing database without --force; missing archive; configuration failure |
| `resume` | Fleet resumed; fleet already running | — | Configuration failure |
| `review` | Review dispatch succeeded; no review work; governed review READY or nothing due | Runner or publication failure; governed review NEEDS_CHANGES, held or no verdict | Pre-launch refusal (not_run); configuration failure; governed review refused before any reviewer runs |
| `run` | Successful dispatch; empty queue; clean fleet drain; approval hold or declined estimate | Reported dispatch findings or failure | GitHub query failure; pre-launch failure; potentially stranded fleet claim; configuration failure |
| `sandbox` | Image built; smoke checks passed; Egress certified; Allowlist network present or not needed | Build ran and failed; Built image verification failed; Smoke verification findings; Egress certification failed; Allowlist network absent | Docker missing; Docker daemon unreachable; Missing smoke prerequisites; Egress certification could not run; Egress certification could not be recorded; Allowlist network could not be created; configuration failure |
| `schema` | Command tree printed | — | Configuration failure |
| `serve` | Server stopped cleanly; URL printed | — | Unsafe bind refusal; configuration failure |
| `setup` | Setup completed | Setup check or installation findings | Label preflight unavailable; configuration failure |
| `settle` | Never-started run reaped and its slot freed | — | Run outside the never-started rule (including an owner of unknown liveness), left unchanged; container removed for a run another process finished meanwhile; missing run; run no longer pending; configuration failure |
| `skills` | Artifacts applied or current; no diff | Local modifications refused; diff found | Unknown artifact or missing selection; configuration failure |
| `status` | Status read successfully; empty queue | — | Unknown run; invalid request; GitHub query failure; configuration failure |
| `stop` | Fleet stopped; fleet already stopped | — | Configuration failure |
| `switchyard` | Execution, quality, and proxy checks passed | Execution, quality, or proxy evidence findings | Benchmark could not start; configuration failure |
| `triage` | Proposal recorded; decision recorded; item skipped as an epic node; nothing waiting in the queue | Triage unavailable | Station disabled; non-numeric item; GitHub read failure; missing or unreadable ledger; no unresolved proposal for the item; ambiguous decision flags; unrecordable proposal; configuration failure |
| `tui` | Dashboard closed cleanly | — | Configuration failure |
| `verify` | The verification passed on the host | The verification failed: the host's own exit code, verbatim | No exit code from the job; unknown ref; unresolvable base branch; gcp selected without an instance; another local verify holds the lease; configuration failure |

## Dispatch and epic boundaries

Dispatch uses separate internal sentinels for an empty queue and a failed query.
The public command boundary translates an empty queue to `0` and a failed query to `2`.
Internal sentinels are not public exit codes.

A nonzero dispatch outcome before launch returns `2`.
Host timeout wrapper failures before agent output return `2`, including missing or non-executable agents.
Without workload output, wrapper statuses `125`, `126`, and `127` refuse startup and release the CI-fix attempt slot.
Starting the host wrapper transfers cleanup ownership and supplies only provisional startup evidence.
The receipt stays `pending` and occupies a slot without consuming an attempt.
Agent events or log lines settle startup; timeout diagnostics do not establish agent execution.
Without agent output, status `124` and other nonsignal statuses outside `125`–`127` establish execution.
Agent output takes precedence over a later status `125`, `126`, or `127`.
Signal death before agent output leaves startup UNKNOWN and retains the attempt.
Its terminal receipt stores `unknown`, not `started`.
Direct launches use the same evidence rule, without reserving wrapper failure statuses.
Missing direct executables raise before cleanup ownership transfers.
Approval holds and declined estimates keep `0`.
A fleet drain with `fetch_failed` or `race_detected` claims returns `2` and lists potentially stranded issues.
A fleet drain retains dispatch, resolve, and required query results.
Any refusal or required query failure returns `2`; otherwise, any executed failure returns `1`.
Capacity reads retain failures and block new workers until a later read succeeds.
A capacity read failure returns `1` when retained worker outcomes establish execution; otherwise, it returns `2`.
Other required failures keep their existing precedence.
Failed overlap-hold releases and workers exceeding shutdown grace are required failures.
Failed pool submissions and claim rollbacks are required failures and name the affected issue or PR.
Operational claim failures stop selection after the current batch.
The boundary lists each non-clean item.
An achieved dispatch loop returns `0`.
Dispatch and resume share terminal-state translation.
Other terminal loop states return `1` after execution, or `2` before execution.
Loop query failures return `2`.
Review and resolve map `not_run` outcomes to `2`.
Their executed outcomes return `0` on success and `1` on failure.
Review, resolve, and CI-fix reject the final attempt's startup refusal before publication or success cleanup.
Cumulative launch evidence controls rollback and the executed-versus-not-run public code.
A refused retry after confirmed or UNKNOWN execution returns `1` and retains the refusal reason.
The refused wrapper's raw exit code remains in runner records, including raw `0`.
Messages and records retain raw runner codes.
Container launch evidence requires the current attempt's entrypoint acknowledgement.
Starting the Docker client or creating a container does not establish workload execution.
Exceptions after confirmed startup return `1`, including finalization, publication bookkeeping, and sidecar teardown failures.
Checkout-release failures log diagnostics and preserve completed outcomes for dispatch, CI-fix, resolve, and review.
Interrupted admission alternates finalization and cancellation-journal writes while SIGINT remains deferred.
Both paths share five bounded backoffs after their initial attempts.
A successful journal write preserves recovery for admission, orphan finalization, or the next reservation-reaper tick.
Each recovery path replays cancellation markers before generic dead-owner finalization.
Replay retains a marker when neither the receipt nor the run changes.
Late settled evidence can correct a receipt after another path terminalizes the run.
If both paths exhaust retries, cancellation remains primary and the last failure becomes its cause.
The error log identifies the run, its reservation, and both persistence failures.
The pending row remains available for dead-owner reconciliation.
Cancellation finalization atomically settles the receipt, terminalizes the run, and releases its reservation.
Settled refusal writes `refused` even after cleanup ownership transfers, so it consumes no CI-fix attempt.
Settled startup writes `started`; an owned start without settled evidence writes `unknown`.
Retained settled startup can upgrade a refused terminal receipt after a failed receipt write.
A later refusal cannot erase an earlier started receipt or its consumed attempt.
Cancellation before startup writes `refused`.
CI-fix maps PR lookup failures, unavailable CI logs, and preparation failures to `2`.
Legitimate CI-fix skips keep `0`.
`doctor route-live-confirm` returns `0` for confirmation, `1` for refutation, and `2` for refusal or declined spending.

Epic continue translates continuation outcomes using launch evidence.
Successful continuations return `0`; launched failures return `1`; pre-launch failures return `2`.
UNKNOWN execution also returns `1` on failure.
Result collection and checkout-release failures retain the attempt's launch evidence.
Internal continuation results retain their raw codes.
The hidden `epic _dispatch-node` boundary uses the same `0`/`1`/`2` scheme.
Its dispatch outcome and ledger retain raw runner exit codes, including `127`.

Epic planning retains launch evidence through dispatch and re-split retries.
Checkout, containment, runner preflight, and other failures before launch return `2`.
Executed failures, manifest validation failures, and export failures after execution return `1`.
Post-creation GitHub query failures return `2`; graph validation findings return `1`.
Reports require a resolved repository before generation or delivery.
Degraded reports begin with an outcome-staleness warning and return `2` after delivery.
JSON exports and HTML data islands include `outcome_sync.query_failures` and `outcome_sync.stale`.

The epic driver API remains unchanged.
`COMPLETE` returns `0`.
`STALLED` returns the repair code `1`.
`AWAITING_MERGES` remains nonterminal and keeps polling in both drivers.
See the stable [epic exit table](epic.md#exit-codes) for driver details and aggregation rules.

## Wrapper coordination

Wrappers must read this scheme page, not per-verb tables.
Use `2` for operational failure handling and `1` for findings or repair handling.

Container startup uses the acknowledgement contract in [sandbox entrypoints](../sandbox-entrypoint-contract.md#agent-startup-evidence).

| Container evidence | Public exit on failure | Refusal rollback |
| --- | --- | --- |
| Current attempt acknowledgement, except an eventless exec failure | `1` | Forbidden |
| Acknowledgement without an agent event, exit `126` or `127`, and no earlier execution | `2` | Allowed |
| Completed wrapper without acknowledgement; no earlier execution, including raw exit `0` | `2` | Allowed |
| Explicit entrypoint refusal with no earlier execution: prompt `64`, OpenCode catalog `78` | `2` | Allowed |
| Final attempt refused after earlier confirmed or UNKNOWN execution, including raw exit `0` | `1` | Forbidden |
| Stream failure with missing acknowledgement: UNKNOWN | `1` | Forbidden |

Generic JSON and previous-attempt acknowledgements never establish startup.
UNKNOWN retains work and a failure record naming the missing acknowledgement.
