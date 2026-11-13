# `kew dispatch`

```text
Usage: kew dispatch [OPTIONS] ISSUE

Arguments:
  ISSUE

Options:
  --loop
  --max-steps INTEGER
  --dry-run            Preview without running
  --show-prompt        Print the generated prompt and exit
  --model TEXT         Override model for this dispatch
  --approve            Approve this dispatch past the approval gate
  --force-budget       Bypass budget checks
  --json               Print one JSON result summary on stdout; progress stays on stderr
```

Dispatch a single named issue. [`kew run`](run.md) takes work from the ready
queue instead.

`ISSUE` is a GitHub issue number. Linear items run through [`kew epic`](epic.md)
and [`kew delegate`](delegate.md); see
[What works with Linear](../configuration.md#what-works-with-linear).

Dispatch publishes through a fresh host-owned canonical repository by default.
Kew reads the agent checkout as plain files and filters them before publication.
A review-sensitive path stops publication and preserves the checkout. Kew then
records an awaiting-publication state for human action.
Contained runs record this state before returning a held publication to the host.
An existing PR or failed PR lookup does not prove that the current edits were published.
Canonical dispatch publishes those edits or preserves the checkout with a publication deferral.

## Usage

```bash
kew dispatch <issue>                       # single-shot dispatch of one named issue
kew dispatch <issue> --dry-run --show-prompt  # preview the prompt without running
kew dispatch <issue> --approve             # release an issue held by the approval gate
kew dispatch <issue> --loop                # supervise the runner as a first-class loop
kew dispatch <issue> --loop --max-steps N  # loop with explicit step cap
kew dispatch <issue> --json                # one JSON result summary on stdout
```

`--dry-run`, `--show-prompt`, `--model`, `--approve` and `--force-budget` apply
to single-shot dispatch only. Combined with `--loop` they are refused with exit
class 2.

When an approver has activated a dispatch prompt profile, every dispatch runs
the built-in prompt plus that addendum. The addendum renders once, under
`## Operator instructions — dispatch profile v<N>`, before the safety and
publication sections, and those sections say they take precedence over it.
`--show-prompt` prints the addendum too. kew reads it only from the host's
`dispatch.db`, never from the checkout, and records its version on the run row
(`runs.prompt_profile_version_id`, null for a run with no profile). Host
publication still refuses a `[prompt] template` or `append` from `kew.toml`.
With any other publication, a custom `[prompt] template` is refused while a
dispatch profile is active: only the built-in prompt renders the addendum, so
retire the profile or remove the template rather than record a profile the
agent never saw. An active profile whose body could open a Markdown block (a
code fence or an HTML block, which a proposal may no longer contain) is refused
at launch the same way: retire it and propose one without it. So is an active
profile whose body no longer matches the digest it was approved under. A
dispatch reads the profile once, and the implementer renders and records that
same version. That refusal (`prompt_configuration_refused`) happens before any
agent starts, an epic node's acceptance author included, and marks nothing
failed: the issue goes back to `ready`, and a drain stops selecting until the
configuration changes.

## JSON result summary

With `--json`, progress still streams for people on stderr, and stdout carries
exactly one JSON document once the command ends — an agent driving kew reads
the outcome there instead of parsing prose. `kew run` and `kew review` take the
same flag and print the same shape.

```json
{"run_id": "6f1c...", "item_id": "42", "outcome": "success",
 "error_category": null, "pr_url": "https://github.com/owner/repo/pull/7",
 "cost_usd": 1.25, "duration_seconds": 61.5, "reason": null}
```

Every key is always present, `null` when unrecorded. All but `reason` carry the
same names, and for a recorded run the same values, as the run-report
frontmatter. `outcome` is the run's recorded outcome (`success`, `failure`,
`merged`, ...) when an agent ran; otherwise `refused` (an admission, claim,
budget or other could-not-run refusal), `held` (an approval hold), `empty`
(`kew run` found no ready issue), `query_failed` (the queue could not be read)
or `dry_run`. `reason` names why a command refused, held or failed, and is
`null` on success. `error_category` is the run's recorded category, or the
refusal's for a dispatch that never launched.

A refusal prints the same document; exit codes are the command's own, and
`--json` never changes them. `--json` summarises one dispatch, so it is refused
as a configuration failure, still printing a summary, alongside `--loop`,
`--show-prompt` or, on `kew run`, `--drain`.

## Admission (single-shot dispatch only)

Single-shot `kew dispatch <issue>` admits a named issue only when it carries the
`ready` label (renameable under `[labels]` in
[configuration](../configuration.md)) and no claim label (`in-progress`,
`agent-working`), and only when its branch does not already exist on the remote.
`--approve` releases an issue held with the `needs-approval` label back to
`ready` first. `--dry-run` applies both checks in the real run's order — the
remote branch read first, then the labels — and reports the same verdict in the
same words, so a preview never promises a dispatch the real run then refuses.

`--dry-run` writes nothing. It reads the issue, applies the admission checks,
resolves the route and prints the governor's reasoning — and records no audit
row, no run row and no reservation, so the governance log holds only dispatches
that really happened. It wrote a `routing_decision` audit row until #2302, so an
older ledger can hold routes for dispatches that never ran; those rows are left
exactly as recorded, because kew never rewrites a historical row.

Refusing the one issue named on the command line means nothing ran, so
`kew dispatch <issue>` exits 2 — with or without `--dry-run`. An empty ready
queue is a different outcome: [`kew run`](run.md) exits 0 when it finds no work,
and so does a queue-selected issue lost to a concurrent claim.

**`kew dispatch <issue> --loop` applies none of these three checks.** It reads
the named issue directly and starts the loop whatever its labels say, so it will
dispatch an issue that is not `ready` or one another run has already claimed,
and it never reads the remote branch. It also takes no label claim, so it leaves
no `in-progress` marker for a later dispatch to refuse on. A loop's only
duplicate guard is the host-local checkout claim: a second loop over the same
checkout **on this host** refuses with exit class 2, while a loop started on
another host is not guarded at all. Check the labels yourself before running
`kew dispatch <issue> --loop`.

## `--approve`

Approve this dispatch past the [approval gate](../configuration.md#approval).
Use it to release an issue held with the `needs-approval` label. Logged as
`approval_granted`. Without `[approval]` configured the flag is an accepted
no-op.

## `--loop` (first-class loops)

With `--loop`, kew creates a **Loop**: a goal it pursues to a terminal state,
recording each agent step as an iteration and verifying the result against a
gate before declaring success.

- **Routed like ordinary dispatch:** the loop's runner and model are resolved
  through the `[routing]` table using the issue's complexity label, exactly as a
  single-shot dispatch would — so a `simple`-labelled issue runs its loop on the
  simple tier's runner/model, not the `[agent]` default. The route is resolved
  from the **effective** config — `kew.toml` plus any `config_overrides` raised
  in the [web UI](../configuration.md), so a runtime `agent.model` / `routing.*`
  override governs the loop exactly as it governs every other dispatch kind. The
  resolved runner, model and complexity tier are all recorded on the loop, so
  `kew loop resume` continues on that same route instead of re-resolving the
  default.
- **Native runners only (v1):** all supported runners (Claude Code, OpenCode,
  pi) declare `loop_style = "native"` and drive the loop themselves. If a
  `[routing]` entry ever resolves to a runner that is not native-loop-capable,
  `--loop` fails closed with that reason — it never silently degrades to a
  single-shot dispatch.
- **Official Codex:** supported for standard issue dispatch with host-brokered
  commit, push, PR, and report. `--loop` runs Codex under the default
  `[publication] publish = "canonical"`. Codex contains itself, so its loop needs no
  `dangerously_skip_containment` (#1977).
  With `publish = "checkout"`, `--loop` refuses Codex: checkout publication
  has no host publication broker.
- **Agent containment:** a loop's agent runs on the host; kew has no contained loop launch.
  A runner that does not contain itself needs `[sandbox] dangerously_skip_containment = true`.
  Without it the loop refuses before any checkout or agent work and returns exit class 2.
  See [`kew loop`](loop.md) for the refusal evidence (#1977).
- **Verification admission:** Command verification runs contained when a local runtime and approved runner image are available.
  New dispatches select the issue, resolve the route, and save the initial loop record first.
  The driver checks admission before worktree setup or subprocess execution against project files.
  Missing prerequisites require `[sandbox] dangerously_skip_containment = true` for host fallback.
  Refusal records `needs_human` under #1444 and returns exit class 2.
  Checkout publication still requires dangerous admission for local Git operations.
  See [`kew loop`](loop.md) for the containment and resume rules.
- **Guardrails** come from config and are **load-bearing**: `agent.timeout_minutes`
  bounds the wall clock (threaded into the run timeout), and the loop is stamped
  `needs_human` if recorded steps exceed `max_steps` (scaled by issue complexity:
  simple=80, medium=200, complex=400). `agent.max_budget_usd` is
  passed to the runner as a *best-effort* dollar cap. The loop **refuses to start**
  if the resolved model is not in the pricing table (no silent $0 / unbounded run)
  and if no verify command (`commands.verify_loop`, falling back to `commands.ci`)
  is configured.
- The loop and its iteration timeline are queryable in the run store; the
  read-only Loop card (Flight Recorder) renders status, the step timeline, the
  verification result, and the audit trail.

> **Not to be confused with `kew run --drain`.** `kew run --drain` runs *continuous
> dispatch* — it keeps picking the next ready issue until the queue is empty.
> `kew dispatch <issue> --loop` runs **one** first-class, verified loop pursuing a
> single goal. Different flag, different command, different meaning.

Detached issuance applies only to direct OpenRouter routes.
The parent resolves the item's runner and inference transport before issuance.
Native Claude, native Codex, and Switchyard routes require no bounded key or provisioning credential.
Switchyard sidecars remain excluded when their upstream route uses OpenRouter.
These routes receive no issued-key marker and retain their existing upstream authentication.
Issuance and refusal events record the route decision in `credential_route`.
The driver mints each detached run key before spawning `kew epic _dispatch-node`.
This includes delegation runs.
Children never hold management authority.
Every launcher removes `OPENROUTER_PROVISIONING_KEY`, including launchers for Kew itself.
The parent supplies the bounded secret, `KEW_ISSUED_RUN_KEY_HASH`, and `--run-id`.
The child verifies the issuer hash and secret fingerprint against that run's durable issuance event.
The child records phase `inherited` and neither mints nor revokes the inherited key.
A marker on any excluded route also refuses inheritance.
A mismatch refuses launch with `issued credential mismatch` and publishes nothing.
Missing provisioning or failed issuance refuses direct OpenRouter detached dispatch and releases its reservation.
The parent never substitutes the host inference key after an issuance failure.
Generic helpers strip the bounded key and marker when the dispatch process holds an issued-key marker.
Only explicit dispatch handoff, agent launch, and container launch receive the issued key.
Container arguments forward the key by environment name; helpers such as `docker inspect` receive no bounded key.

The parent records `issued_by="driver"` in the run's `sandbox_resolved` issuance event.
The next driver tick revokes credentials for terminal runs or expired issuance events without run rows.
Reconciliation uses bounded pages; large backlogs can require additional ticks.
Driver exit attempts the same cleanup without revoking active detached children.
The standalone delegation watcher reconciles terminal and expired keys once per pass.
Watcher exit reconciles again, including exceptional exits, while preserving running children.
A shared seen set prevents duplicate revocation attempts within one pass and its exit cleanup.
If no driver or watcher returns, the key's own expiry is the backstop.
Credential reconciliation preserves every pending run.
Owner and durable-container recovery must confirm death before an orphan becomes terminal.
Unknown ownership or container state keeps the credential unrevoked for a later pass.

## Dispatch ownership and orphan recovery

Dispatch proves process-group ownership before writing its pending row.
The membership scan excludes zombies.
Descendants of the dispatch process may share its group.
A foreign peer in the group refuses admission and reports its PID and command.
Run kew without a pipeline, or under `setsid` where available.
For example, `kew dispatch 42 | tee log` can share its foreground group with `tee`.
An unavailable process scan retries three times over about two seconds before refusing admission.

Admission records a kernel start token beside each owner's PID, PGID, birth time, parent, and command.
Darwin reads the token through libproc; Linux reads `/proc/<pid>/stat` and the boot time.
Host execution requires non-null `owner_start_token` and `attempt_start_token` values.
Registration verifies ancestry and rereads the attempt token before the startup gate can open.
The gate rechecks both tokens immediately before releasing the child.

Host admission retries identity capture three times.
Missing owner tokens refuse admission with `owner-identity-unavailable`.
The operator message names the platform and its inability to provide verified kernel process identity.
Missing attempt tokens produce `spawn_registration_failed` with `attempt-identity-unavailable`.
A missing owner token at registration produces the same event with `owner-identity-unavailable`.
The child remains behind the gate and terminates without executing work.
The run becomes `refused` with exit code 2 and `not-run` accounting for an initial refusal.
Its reservation is released using the run identity.
Container execution keeps its durable container identity path.

Liveness probes read PID, PGID, state, birth, parent, and command together.
Legacy rows can use `lstart` for liveness and reaping when kernel tokens are unavailable.
This fallback never authorizes host execution or a kill target.
A verified birth mismatch proves that the recorded owner died.
A complete snapshot that lacks the recorded owner also proves death.
Surviving group members never establish owner liveness.
A process group proves empty when no member survives the zombie-excluding scan.
A group whose signal probe is refused — macOS answers that for a group of
unreaped members — is read from that same scan, not left unknown.
Missing birth evidence or unavailable inspection keeps liveness unknown.
Signal checks require matching kernel tokens and recorded identity.
Attempt termination also checks registered ancestry and retained member births.

Run admission, identity admission, epic reservations, and continuation claims share one owner snapshot before `BEGIN IMMEDIATE`.
Recovery terminates surviving authenticated attempts through the fleet signal checks before finalizing their dead owners.
Token mismatches leave attempts untouched and retain capacity.
Each transition checks pending status and the same recorded owner identity.
Successful recovery records `killed`, internal process status `137`, and `orphaned_pending_reaped`.
Reservation release names the captured reservation run ID.
The `orphaned_attempt_terminated` and `orphaned_run_reaped` audit events record termination and reaping.
Unresolved attempts produce `orphaned_attempt_retained`.
The locked capacity count uses only SQL.
It rechecks carried death proofs against the recorded owner and reservation generations.
Live owners, unknown liveness, and reservations without matching run rows retain capacity.
Contained reconciliation requires client death after the startup grace, and a container that is absent or that kew has removed.
A stopped container frees nothing on its own: kew reads its state by durable ID, records a `dead_run_container_removal_intended` event naming the container, its state, the runtime and the run, removes it without `-f`, and then records `dead_run_container_removed`.
A refused removal retains capacity.
An owner recorded on another machine has unknown liveness, so its run retains capacity.

Each run also records the machine and boot its owner ran under.
Linux uses `/etc/machine-id` for machine identity, then `/var/lib/dbus/machine-id`.
A Linux host with no valid id in either file, such as a minimal container, records no machine, so none of its runs can be proved to have died with an earlier boot.
macOS uses the operating system host UUID.
Hostnames do not establish machine identity.
New rows prefix that identity with `machine-id:`.
Legacy hostname-based rows therefore prove nothing.
Contained admission records boot evidence even when process identity is unavailable.
After the machine restarts, the epic driver's first tick reaps a pending run from an earlier boot of the same machine, with no startup grace.
It signals none of that run's recorded process IDs, since the new boot may have given them to other processes.
A contained run still needs its container proved absent or stopped.
Admission that runs before that first tick frees the run's slot as soon as the container is proved absent or stopped, even if the run started moments before the restart, and leaves the reap to the tick.
A boot recorded by another machine, or a boot this machine cannot read, proves nothing.
The first time the host restarts under an epic node's implementation run, the run is recorded as `host_restart_reaped`.
The terminal row, retry marker, and `host_restart` retry event commit together.
The marker names the reaped run and supersedes that run whatever the clocks say, so the node is dispatched again and the run does not count as a failure.
It also names every implementation run that an earlier `kew epic retry` had already cleared, so those stay cleared by name, not by time.
A replacement run is never superseded by it, even when the clock was set back after the reboot or behind an earlier `kew epic retry`.
Review runs are never named.
A host restart does not start a new review cycle: the cycle still starts at the last `kew epic retry`.
Work treats every run the marker names as an earlier attempt, so it does not offer a dispatch while the replacement is in flight.
If the checkout is dirty, kew preserves it as `awaiting_continuation` instead, and continues it automatically once.
Under `publish = "checkout"`, a checkout whose tree is clean but whose HEAD commit is not on the base branch counts as dirty too, because no remote holds that commit.
kew checks this in your own repository against a freshly fetched base, never against the checkout's own refs, which the agent could have moved.
A commit already on the node's item branch on origin is published too, so a retry that restarted clean at that branch's head is re-admitted once, not preserved.
If the base or item branch cannot be fetched, the commit counts as unpublished work.
A contained run works in its own clone under the system temp directory, so kew reads that clone first.
If the clone is dirty, kew copies it to the host dispatch checkout and preserves it there, leaving the clone as it was.
If kew cannot prove the clone's state, the node is held rather than retried, and its run stays pending.
That covers a recorded clone that is gone after the restart, since nothing then says whether it held work, and a tree that already stands at the host checkout.
It also covers a contained run that recorded no clone at all, such as one launched by a kew version that did not record it: its clone is somewhere under the system temp directory as `kew-sandbox-<item>-*`, and kew cannot tell which one it is or whether it is clean.
The hold reads `preexisting_worktree_refused` and names the clone; `kew epic status` and the console show the node as held.
A gone or unrecorded clone never becomes provable, so once you have recovered its work, `kew epic unhold <epic> <node>` releases it for good: the next tick re-admits the node once instead of holding it again.
A delegated or review run's dirty clone is brought home and preserved the same way before its reservation is released.
If it cannot be, the run stays pending and its reservation stays held, with the reason logged on every pass, because those runs have no node to hold.
That includes a delegated run that recorded no clone. Only a reviewer, which never owns a clone, is reaped without one.
Once you have recovered its work, `kew epic unhold delegation <node>` releases it: the next pass reaps the run and frees its reservation.
A run that started after such a hold was written is the dispatch that disproved it. The node shows as in progress, not held, even if the driver died before it removed the old hold.
If the driver dies after recording the run as preserved but before recording its continuation, the next tick records the continuation from the same checkout before it releases the node's reservation.
A host death under dirty work counts toward the same once-only budget as a clean re-admission.
The automatic continuation passes through normal admission, including the per-epic budget, the worker cap, and fair share.
With `max_parallel = 0`, nothing is claimed and the work stays `awaiting_continuation`.
A continuation that admission defers waits for a later tick, as fresh work does, and the epic keeps reporting progress.
If a continuation claim is cancelled before its agent launches, such as after a failed spawn, it is retried once.
A continuation child that fails before its run starts, or never starts at all, counts as a cancellation.
After a second cancellation, such as a deleted checkout, the work stays `awaiting_continuation` for `kew epic continue <epic> <node>`.
The second host death of the node, clean or dirty, fails the node and asks for a person in the epic's attention stream.
If the checkout is clean, the run is recorded as `host_restart_repeated`.
Its attention entry carries the `host_restart_repeated` kind, since no hold stands on the failed node.
A continuation whose checkout is clean becomes terminal, and the attention entry supplies `kew epic retry <epic> <node>`.
If the checkout is still dirty, it stays preserved and the attention entry supplies `kew epic continue <epic> <node>`.
If a clean run's node still keeps preserved work that an earlier continuation left incomplete, the entry supplies `kew epic continue <epic> <node>` instead.
`kew epic retry` refuses that node and names `kew epic continue`, or `kew epic abandon <epic> <node> --reason TEXT` to discard the work.
If more than one such continuation is recorded for the node, or one awaits continuation beside it from another original run, `kew epic continue` and `kew epic abandon` both refuse the history as ambiguous, so the entry supplies no command, and `kew epic retry` refuses the node and lists the original runs.
`kew epic continue` and `kew epic abandon` accept the same proof of death that reaped the runs: an owner recorded under an earlier boot of this machine is dead, and its recorded process ids are not probed, because a later boot may have given them to other processes.
As with the reap, a contained run's container must still be proved absent or stopped first.
`kew epic continue` probes every owner the node's continuation history records, not only the original run: a later continuation whose process or container may still be live in the checkout blocks it.
An attention entry or console row journaled before this reason existed still reads as retry until the epic ticks again; kew rereads that entry against the node's current continuation history, so it supplies `kew epic continue` (or nothing, for an ambiguous history) without waiting for that tick.
Once the preserved work is taken up — a continuation owns it, `kew epic abandon` re-admitted the node, or the continuation completed — the entry asks nobody and supplies no command, without waiting for the next tick.
If a `kew epic continue` dispatch exits nonzero or fails to launch, the preserved work awaits continuation again, and the entry supplies `kew epic continue` again, also without waiting for a tick.
The epic page's selected-node panel prints the recovery command the server serves for each failed node: `kew epic retry` for an ordinary failure, `kew epic continue` for preserved work, and nothing when no command recovers the node.
An open epic page updates that command as these changes happen, without a reload and without waiting for a tick.
While a continuation of the node is in progress, no command is supplied: its owner holds the work.
A printed command names the epic and node exactly as stored; when either cannot be passed as a plain shell argument, no command is printed.

Contained orphan cleanup persists its runtime and stop intent before termination.
It rechecks the durable container ID, stops that container, then stops the authenticated client.
It verifies both deaths before finalization.
Runtime failures, inspection failures, or unproven termination retain the pending row and cleanup intent.
Later driver ticks retry that intent, including after a store reopen.
A running container cannot hide a dead dispatch owner from cleanup.
