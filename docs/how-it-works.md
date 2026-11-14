---
description: How kew takes a ready issue through selection, its own checkout and a runner session to a pull request, and what happens on failure.
---

# How It Works

A person labels an issue `ready`; `kew run` selects it, builds a checkout and a
prompt, and hands both to the configured runner, which ends in a pull request or
a preserved failure:

``` { .text .kew-diagram }
                    ┌─────────────┐
                    │ Human adds  │
                    │ "ready"     │
                    │ label       │
                    └──────┬──────┘
                           │
                           ▼
               ┌───────────────────────┐
               │     kew run           │
               └───────────┬───────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         ┌─────────┐ ┌─────────┐ ┌──────────┐
         │ Select  │ │ Create  │ │ Build    │
         │ highest │ │ git     │ │ prompt   │
         │ priority│ │ checkout│ │ from     │
         │ issue   │ │         │ │ template │
         └────┬────┘ └────┬────┘ └────┬─────┘
              │            │           │
              └────────────┼───────────┘
                           ▼
               ┌───────────────────────┐
               │  Configured runner    │
               │                       │
               │  Read AGENTS.md       │
               │  Implement (TDD)      │
               │  Run CI               │
               │  Commit               │
               │  kew pushes + opens PR│
               └───────────┬───────────┘
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
       ┌─────────────┐          ┌─────────────┐
       │  Success    │          │  Failure    │
       │             │          │             │
       │ Labels:     │          │ Labels:     │
       │ pr-pending  │          │ agent-      │
       │ -review     │          │ failed      │
       │             │          │             │
       │ Checkout    │          │ Checkout    │
       │ cleaned up  │          │ preserved   │
       └─────────────┘          └─────────────┘
```

## The dispatch loop

In `kew run --drain` mode with multiple workers, the full cycle runs continuously:

1. **Select** -- query GitHub for `ready`-labeled issues, ordered by priority (`p0` > `p1` > `p2` > `p3`), excluding any currently in-flight
2. **Isolate** -- create an isolated checkout per issue on a dedicated branch (`agent/issue-{N}`). Every checkout an agent receives is a standalone **clone with its own `.git`**, never a linked Git worktree of your repository. Legacy linked checkouts are refused on resume. This difference is a [security boundary](security.md#isolated-checkouts), not an implementation detail
3. **Record the plan** -- extract the issue body's acceptance criteria and write them to the ledger, keyed on a digest of that body and bound to this run (`plan_recorded`; an unchanged body records nothing new, an edited one supersedes). Extract-only: no model call, and no network read beyond the issue read that already happened. Read it back with [`kew plan show`](cli/plan.md)
   - **Acceptance contract** -- on an epic driver's host dispatch, when the recorded plan has at least one criterion of `kind = "test"` and the node has no contract yet, a separate `acceptance_author` run goes first. It is a fresh session on the other model family from the implementer's route (a `gpt-` implementer gets `claude-opus-5-5`, a `claude-` implementer gets `gpt-5.6-sol`; the implementer's own model only when the routing names one family). It writes `kew/tests/acceptance/test_issue_<N>.py` with one `test_ac<k>` per such criterion and changes nothing else. Kew runs each test at the base and refuses the run unless pytest's junit report names that `test_ac<k>` and records it failing on an `AssertionError` (a failed `assert`): a test that passes, errors in setup or collection, writes no report, or fails on any other exception (`raise RuntimeError("not implemented")`, `pytest.fail`, or a `pytest.raises` that did not raise) is not red. The report is written by the same process that runs the author's test code, so a test written to overwrite it at exit could forge a red verdict; this check assumes an author that is not hostile. Kew also refuses the run if the author or a test run changed anything else in the checkout: another file, an ignored file, a file's mode, a staged blob or `skip-worktree`/`assume-unchanged` flag in the index, or a hook, config, attributes or `objects/info/alternates` file under `.git`. After a refusal, kew removes the files the run created, writes back the tracked files it changed, puts every index entry and flag back, and resets the branch to the base. An ignored file or git config the run edited cannot be written back and is only named in the refusal. Otherwise it commits the file's exact bytes, with no git filter or hook in between, as the first commit on `agent/issue-<N>` and records the path, sha256, author run and commit in the ledger. The `contract_checks` helper can map each criterion to `pytest kew/tests/acceptance/test_issue_<N>.py::test_ac<k>`. Production dispatch does not call that helper yet. A contract already in the ledger is restored onto the checkout, so a redispatch reuses it instead of authoring a new one
   - **The implementer never edits the contract** -- it disputes a test instead, by ending `KEW_NEEDS_HUMAN: acceptance-disputed {json}` with the criterion, test, what is wrong, evidence and proposed fix. The node is held as `acceptance-disputed` on Needs-you; that is not a failure and spends no review round. A person amends the contract by hand or refers the dispute to the author step with [`kew epic acceptance amend`](cli/epic.md#kew-epic-acceptance-amend-amend-a-nodes-acceptance-contract). Every amendment records a new contract version and an attested `acceptance_amended` audit event with the old and new digests and the dispute it answered
   - **Contained dispatches refuse instead of authoring** -- the author run and its red-at-base pytest both execute agent-written code, and the only launch for them is the host's. So a contained dispatch (the default [`enabled = "auto"`](configuration.md#sandbox) posture with a container runtime) of a node that needs a contract and has none in the ledger is refused before any run, rather than authoring on the host or dispatching without a contract. To proceed, dispatch that node once on the host -- a runner with a native boundary, or with `dangerously_skip_containment = true` -- so the contract is authored and recorded; later contained dispatches of the node restore it and run as usual. A continuation of a preserved checkout prepares the contract the same way before its implementer launches: a recorded contract is restored, and a missing one is authored only while the branch still sits at its base. Once the implementer has committed, a contract can no longer be the branch's first commit, so the continuation is refused with that reason instead; redispatch the node from a fresh base
4. **Prompt** -- build a Jinja2 prompt from the issue body, its recorded acceptance criteria, complexity labels, and your config
5. **Execute** -- spawn the resolved runner with its governed command, timeout, budget controls, and security posture. Before this, the [approval gate](configuration.md#approval) estimates cost and either confirms or holds the issue.
6. **Stream** -- parse the runner's structured events in real time, showing tool use, token counts, and progress
7. **Retry** -- if the agent exits with a retriable code, back off and retry (with `--resume` to reuse session context)
8. **Complete** -- on success, push the branch, create a PR, label it `pr-pending-review`, and remove the checkout. On failure, label `agent-failed` and preserve the checkout for debugging. For managed adaptive Switchyard runs, Kew snapshots the owned proxy before teardown, drains classifier records during teardown, reconciles the served model/tier before pricing, and marks incomplete terminal accounting `unreconciled` in both audit evidence and spend reports.

Canonical publication excludes new regular files beneath five exact cache directory names: `.mypy_cache`, `__pycache__`, `.ruff_cache`, `.pytest_cache`, and `.import_linter_cache`. Tracked writes and deletions beneath these directories remain eligible. It also skips untracked dependency installs: a real `node_modules` or `.venv` directory that the base tracks nothing at or beneath, such as the output of `npm ci` or `uv sync`. Native (host) extraction does not enter that directory, so its symlinks do not refuse the publication, and the skipped directories are logged. A tracked `node_modules` or `.venv` directory is published and validated like any other content, and a symlink named `node_modules` is still refused. When an untracked install reaches the filter anyway -- from a contained checkout whose own ignore rules do not hide it -- its paths leave publication scope before the gates that hold or refuse the whole publication, so neither a package's own `package.json` nor two of its files whose names differ only by case stops the rest of the work from publishing; a symlink or other non-regular entry beneath the install is still refused.

Native extraction also leaves out untracked paths that the base commit's `.gitignore` files ignore, such as a SvelteKit `.svelte-kit/` directory or a mkdocs `site/` build. Kew reads the rules from the base commit in the canonical repository: the root `.gitignore` and every nested one the base tree holds. It never reads them from the agent's checkout, so a `.gitignore` the agent adds or edits cannot change what is left out; the edit itself is published like any other change. `.git/info/exclude` and `core.excludesFile` are not read. Matching follows gitignore rules: negation, anchoring, directory-only patterns, `**`, deeper files taking precedence, and no re-including a file whose parent directory is excluded. A tracked path is never left out. An ignored directory is not entered, so a symlink inside it does not refuse the publication. A symlink or other non-regular entry that is not ignored is still refused. The ignored paths are logged. Contained extraction lists untracked files with `git ls-files --exclude-standard`, so it applies the checkout's own ignore rules in place of both host-side skips.

Native extraction also leaves out an untracked directory that is itself a git worktree or repository, such as a `.wt-main/` an agent created with `git worktree add` inside the workspace. That directory is another repository's checkout, not the run's work, and its symlinks and gitlinks would otherwise refuse the whole publication; it is skipped and logged instead. The test is the one git itself applies, not the name alone: a `.git` directory holding a `HEAD` and object and ref storage (or a `commondir` file naming where those live), or a `.git` file reading `gitdir: <path>`. A directory that merely holds a stray entry called `.git` -- a fixture, a note, an empty directory, a symlink -- is ordinary work and publishes as it always did. The checkout's own `.git` is unaffected, and a directory the base commit tracks anything at or beneath is walked and published as usual. A no-change is never sealed over a workspace whose work sits inside a skipped directory: an empty package would be a claim about a directory nothing read, so the run lands unattested instead.

The two producers differ here, and the difference is the reason the agent prompts forbid the directory outright. Contained extraction has no such skip: `git ls-files --others` reports the nested checkout as one untracked directory, which is not a regular file, and the whole extraction is refused — so on a contained dispatch the correctly-done work elsewhere in the checkout is lost with it. Either way, nothing an agent puts in a nested worktree or clone reaches the branch.

## Dispatch outcomes

Each dispatch ends in one of the following outcomes:

- **Completed** -- the agent called `finish` and committed coherent work. kew pushes the branch, creates a PR labeled `pr-pending-review`, and removes the checkout.
- **No changes** -- the agent ran but made no changes and called `finish`. kew
  records the outcome as a durable *no-change candidate* and decides nothing on
  the tracker: the issue stays open, nothing is commented, and no dependent is
  unblocked. The only thing that moves is the dispatch claim — the run is over,
  so `in-progress`/`agent-working` are cleared and the item is labelled
  `agent-completed-no-changes`, which keeps a finished run from reading as
  in-flight to `kew dispatch` or as an orphan to `kew cleanup`. "There was
  genuinely nothing to do" and "the agent never got as far as writing a file"
  produce the same empty diff, so the item waits for a person to confirm or
  reject the outcome. Before it asks, kew seals the evidence
  behind that decision: it re-reads the workspace, and only when the checkout is
  identical to the base it was cut from does it seal an empty work package the
  candidate is attested against. A workspace that still carries uncommitted or
  unpushed work seals nothing, and its candidate stays unattested rather than
  confirmable. In the epic driver the node reads as
  `awaiting_no_change`, which never satisfies a dependency and is never
  redispatched.
- **Failed** -- the agent crashed, hit a timeout, or exhausted its budget without finishing. kew labels the issue `agent-failed` and preserves the checkout for debugging.
- **Credential publication refused** -- immutable attempt evidence shows that a
  contained producer received a reusable provider credential. Kew records
  `credential_publication_refused` and preserves recoverable work. No retry,
  remediation, approval, or scan can release that lineage automatically.
- **Awaiting publication** -- the agent completed, but the host could not publish
  its branch or PR. kew commits the work before remote publication, records the
  run as `awaiting_publication`, preserves the checkout, and records a
  continuation handle for `kew epic continue`. The epic driver does not dispatch
  implementation again. Host-publication PR discovery and creation use GitHub
  REST, so an exhausted GraphQL budget does not block this recovery path. Each
  tick reconciles the preserved branch against GitHub: an open PR moves the node
  to `in_review`; a merged PR finalizes the run and closes the Linear item (or
  observes GitHub's issue close). A successful run corroborated by an open PR
  also stays `in_review` if an older continuation handle is stale. A provider
  item already marked completed similarly wins over a stale run overlay. Older
  residue can be repaired explicitly with `kew finalize <run-id>`, which is
  chain-attested. The
  `publication_deferred` audit event and dispatch log identify the failed step
  (`commit`, `push`, or `pr_create`) and retain a bounded, secret-free error
  detail.
- **Rescued (incomplete run)** -- the agent exhausted its step budget (`max_steps`) without calling `finish` but left real checkout changes. kew commits and pushes them, opens a **draft** PR labeled `incomplete-run`, flags the issue `agent-needs-help`, and preserves the checkout. The issue is **not** closed and dependents are **not** unblocked. A human reviews the draft, then marks it ready and merges, or re-labels `ready` to retry from scratch. Only max-steps exhaustion triggers this; crashes, timeouts, and budget stops remain hard failures.

### Troubleshooting deferred publication

`publication_deferred` means the implementation completed, but the host
publication broker could not finish its `commit`, `push`, or `pr_create` step.
It is a publication problem, not an agent failure: the run is recorded as
`awaiting_publication`, the checkout is preserved, and the implementation is
not dispatched again.

Read the `publication_deferred` audit event together with the dispatch log.
Both identify the typed step (`step=commit`, `step=push`, or
`step=pr_create`) and include a bounded, sanitized `error` value. The error is
diagnostic evidence, not raw provider output; it should be safe to inspect
without exposing credentials. When the epic frontier reports this as a
waiting cause, its operator hint is: **“restore GitHub access or open the
preserved branch PR.”**

There are three recovery exits, in order of preference:

1. **External PR, then driver reconciliation.** Restore GitHub access or open
   an external PR from the preserved branch. Run the epic driver again; its
   next tick discovers the branch PR, moves the node to `in_review`, and later
   reconciles an external merge.
2. **Explicit finalization.** If publication was completed out of band but
   the run still has older `awaiting_publication` residue, use
   `kew finalize <run-id>`. This chain-attested repair marks the run
   complete; it does not publish a branch or create a PR. Credential-protected
   lineages refuse this readiness transition.
3. **Preserved-checkout continuation (last resort).** If no PR can be opened,
   continue the preserved checkout explicitly with
   `kew epic continue <name> <node>`. This validates the recorded checkout and
   branch, reuses the existing bytes, and hands publication back to the
   normal broker.

## Label lifecycle

| State | Labels | Meaning |
|-------|--------|---------|
| Queued | `ready` + priority | Waiting for dispatch |
| Working | `in-progress` + `agent-working` | The configured runner is implementing |
| Held | `needs-approval` | Approval gate fired — agent paused until a human approves |
| Done | `pr-pending-review` | PR created, needs human review |
| Failed | `agent-failed` | Something went wrong (comment explains why) |
| Rescued | issue: `agent-needs-help` + `incomplete-run`; draft PR: `agent-pr` + `incomplete-run` | Max-steps exhausted; draft PR opened, awaiting human decision |

The **Held** state occurs when the approval gate (if configured) estimates the dispatch cost at or above your threshold. kew removes `in-progress`/`agent-working` and adds `needs-approval` so the issue sits inert in the queue. Release it via `kew approve N`, the TUI `A` key, a `/approve` GitHub comment, or the web inbox (`kew serve`).

Every PR kew opens carries `agent-pr` (`labels.agent_pr`). A second PR label,
`ci-approved` (`labels.ci_approved`), is added only by a human. It lets
self-hosted CI test the PR's current head. kew never adds it, and it removes it
before it pushes new agent commits to the PR, whether the push comes from
remediation, a CI fix, a conflict resolution or a republish. See
[Agent PR CI approval](operations.md#agent-pr-ci-approval).

## Loop guardrails

**Mid-flight budget watchdog.** A delegated loop is stopped as soon as its
running cost — prior spend plus this session's *incremental, usage-derived* cost
(a list-price estimate) — crosses `budget_usd`, transitioning to `budget_limited`
(resume with a bumped budget via `kew loop resume <id> --budget <n> --guidance "…"`).
The check is on that live token-derived running cost, **not** the reported
`spent_usd` aggregate — `spent_usd` is the persisted total and may lag the live
figure depending on the runner. Enforcement is one cost event late (post-hoc):
the loop is killed *after* the event that reports the overshoot, so the figure is
a near-cap, not an exact cap. Runners that emit no
incremental usage cannot be enforced mid-flight and fall back to the wall-clock
cap (the current native runners — claude-code, codex, opencode, pi — all emit
per-turn usage).

## Governance layer

The dispatch loop sits inside a control plane designed to keep autonomous runs observable and stoppable:

- **Tamper-evident audit log** — every dispatch event is written to a hash-chained log; `kew audit verify` checks integrity and exits non-zero on any gap.
- **Fleet kill switch** — `kew stop` kills all in-flight runs and halts dispatch immediately; `kew resume` re-enables it. Issues are reset to `ready` so no work is lost.
- **Approval gates** — configure a cost threshold and kew holds issues under `needs-approval` before the agent runs. Approve via `kew approve`, the TUI `A` key, a `/approve` GitHub comment, or the web inbox (`kew serve`).
- **Outcome-linked spend** — `kew report` correlates API spend to merged PRs, failed runs, and loop steps so you can see what each outcome actually cost.
- **First-class loops** — `kew dispatch <issue> --loop` runs an issue as a supervised multi-step loop (distinct from `kew run`, which runs the whole queue). Pause or resume mid-flight with `kew loop pause` / `kew loop resume`.

## The epic driver tick {#the-epic-driver-tick}

`kew epic advance`/`run` (see [`kew epic`](cli/epic.md)) drive a whole
`epic:<name>`-labeled dependency graph, one issue-per-node, instead of a flat
queue. Because it dispatches *and* merges without a human driving each step,
it owns a few primitives the flat fleet doesn't need:

- **Single-driver lock + atomic reservation** — the whole tick runs under a
  non-blocking advisory lock (a file under `logging.dir`); a second
  `advance`/`run` that can't acquire it exits cleanly instead of racing. Before
  spawning any subprocess, the driver also reserves the node with a run-store
  write guarded by a uniqueness constraint, so the in-flight count is
  authoritative the instant a dispatch is decided — not moments later, once
  the subprocess has started.
- **Driver-owned status overlay** — the driver computes a richer status per
  node than the provider adapter alone can: `IN_PROGRESS`/`FAILED` are derived
  from the run-store (a live pending run, or the newest terminal run being a
  failure kind), `DONE` requires the issue closed *as* `COMPLETED` (a
  not-planned close reads as `CANCELLED`, a terminal state that does **not**
  satisfy dependents), and `HELD` is an overlay-only pseudo-status for a node
  that's approved but waiting on CI (see the merge gate below).
- **Bounded GitHub PR reads for Linear** — one fresh `gh pr list` batch supplies
  the PR facts for every active Linear node in a tick and is reused by the
  merge pass. Completed Linear nodes skip GitHub entirely, and a failed batch
  does not fan out into one fallback query per node.
- **Positive `epic:*` fleet exclusion** — the fleet's issue selector skips any
  issue carrying an epic label outright, regardless of `ready`, so the flat
  fleet can never grab a node the driver owns mid-flight; the fleet's
  conflict auto-resolver excludes epic PRs the same way.
- **Approval-gated, CI-green merge** — the driver merges a node's PR only when
  a verified, non-author human approval is on record, a fresh GitHub lookup
  confirms the reviewer still has write authority, *and* the PR's required
  checks are green. Bot identities, missing permission evidence, and lookup
  failures fail closed. See [the merge gate](cli/epic.md#the-merge-gate) for
  the exact rule and why there is deliberately no autonomous `--auto-merge`.
- **Failure containment** — a `FAILED` node is terminal for the tick (never
  auto-redispatched); its `transitive_dependents` are reported as its **blast
  radius** so an operator can see exactly what a failed, cancelled, or stuck
  node is holding up. `kew epic retry <name> <node>` is the way back in. The one
  exception is a run killed by SIGABRT or SIGKILL that nothing else explains:
  not the wall-clock deadline, not a checkout or host-disk cap, not a pause, a
  budget stop or a circuit breaker. That covers a confirmed OOM kill, and also
  a SIGKILL kew cannot attribute, such as a container runtime restart, a
  stray `docker kill`, or an OOM probe that could not be read. Such a run fails
  as `resource`, with the signal and the runner's last output lines as its
  reason, and the driver re-admits the node once for the commit that run
  started from. Each such retry is one more paid run. A second `resource`
  failure from the same commit holds the node as `resource_retries_spent` for
  a person; the run's reason still names the signal and that output. A node
  gets at most two of these retries in total, across commits: its next
  `resource` failure holds it the same way. To recover, find the cause, then
  run `kew epic unhold` and `kew epic retry`.
- **Preserved dirty-work recovery** — retrying a failed node whose existing
  checkout still contains uncommitted changes does not recreate or reset that
  checkout. The driver records the exact branch, checkout, and original run as
  an `awaiting_continuation` and leaves the bytes untouched. Continue that work
  explicitly with `kew epic continue <name> <node>`; a normal retry never launches a
  second implementation writer against the preserved checkout. After the first
  host restart under a node, the driver continues its dirty work automatically
  once, through normal admission. After the second restart, the node fails and
  the work stays preserved for `kew epic continue`.
- **External merge reconciliation** — `awaiting_merges` remains in the poll
  roster. A hand-merged PR runs through the same hold and configured-label
  cleanup as a driver merge, advances dependents on that tick, and is never
  attested as a kew-performed merge.
- **Provider-aware node dispatch** — each node is dispatched by a detached child
  (`kew _dispatch-node`) that adapts to the configured tracker. For GitHub the
  node id is the issue number and the classic issue → checkout → PR flow runs
  unchanged. For a non-GitHub tracker (Linear), the child instead *fetches* the
  item's title/description/labels from the tracker and *claims* it (assigns the
  viewer, moves it to the started state) **before** the agent spawns; the opaque
  item id (e.g. `SFD-133`) threads through the branch (`agent/issue-sfd-133`),
  checkout, run-store row, and PR body while the GitHub-only issue number stays a
  `0` sentinel. GitHub-issue label edits are skipped for non-GitHub trackers
  (their state is owned by the claim and the merge pass, not labels). A missing
  key, a fetch failure, or a claim failure is a loud exit that writes a terminal
  run record against the node's reservation rather than a silent skip, and a
  refusal *before* the agent spawns (fleet-stop, worker-cap, budget) rolls the
  Linear claim back so the item is never stranded In Progress with nothing
  running.
- **Reliable child resolution + dead-spawn diagnostic** — the detached child is
  spawned via the `kew` console script *next to the driver's own interpreter*
  (the active venv's `bin/`), preferred over an ambient `PATH` `kew` that could
  be a stale or mismatched install, and the resolved path is logged at spawn. A
  child that crashes before writing its first run row (a bad import, a config
  failure) fails silently under DEVNULL stdio; once its reservation ages past the
  startup grace with no run row, the driver fails the node with a terminal
  `dispatch_child_died` diagnostic that names the spawned executable — instead of
  silently re-spawning the same broken child every grace window.
- **Empty graph is an error, not success** — an epic whose tracker query
  resolves to **zero nodes** (a typo'd `epic:<name>`, or an `advance`/`run`
  fired from a cwd wired to the wrong tracker) raises before any tick is
  journaled and exits **2** with a diagnostic naming the empty query (the
  GitHub label or the Linear project) — it no longer prints
  `COMPLETE done=0 remaining=0`. A driver never declares success for work that
  does not exist; `complete` stays reachable only for a non-empty graph whose
  nodes are all DONE. `kew epic run --all` surfaces the empty epic as needing
  an operator (contributing exit **1** to the aggregate) instead of silently
  dropping it or reading it as complete.
- **Post-action tick summary** — the merge pass runs against the snapshot taken
  at tick *start*, so a node the tick itself merges or closes still reads
  `IN_REVIEW` in that snapshot. Before reporting, the tick folds its own actions
  — dispatched, merged, and out-of-band-closed nodes — back onto the overlay and
  derives every printed field (waiting/done/remaining and the tri-state signal)
  from that **post-action** view. A close that unblocks the frontier this tick
  reads `progressing`, not `stalled`, and stdout agrees with the journal on what
  the tick just did — the new state no longer only appears on the *next* tick.
- **Tracker-aware id rendering** — every item id the driver prints (status
  lines, waiting/`blocked on` reasons, a failed node's blast radius) routes
  through one `item_ref` rule: a GitHub numeric id renders `#123` (which GitHub
  turns into a live cross-link), while any other tracker's id renders **bare**
  (`SFD-133`). So a Linear node reads `blocked on SFD-133` while a GitHub node
  stays byte-identically `blocked on #123`.

Each tick reports `complete`/`progressing`/`awaiting_merges`/`stalled`.
`awaiting_merges` keeps polling for asynchronous human merges; `stalled`
exits for repair. Full detail lives in [`kew epic`](cli/epic.md).

## The supervised drive {#the-supervised-drive}

[`kew drive`](cli/drive.md) runs the epic driver tick for every active epic of
one project from a single process, round after round, while the operator
drives. It is supervised and project-scoped: the operator starts it, it ticks
only the epics already planned in this project's ledger, and it stops when
told to. It does not discover work, schedule itself, or run as a service.

- **Supervision** — one drive per project, held by a lock row in the project
  ledger with the holder's pid, host and heartbeat. A second `kew drive`
  refuses and names the holder. `kew drive pause`, `resume` and `stop` are
  ledger writes the drive reads before every tick, so a running tick always
  finishes. Each tick is its own `kew epic _tick` process on the pinned
  `--kew-binary`, exactly as `kew epic run` runs it, and an epic that ticks
  `complete` is archived and leaves the rotation.
- **The standing merge rule** — merging is off unless the operator writes a
  [`[drive.merge]`](configuration.md#drive-merge) rule naming its signer, their
  GitHub login. The drive fingerprints the rule each round and signs a new or
  changed rule onto the audit chain (`drive_merge_rule_signed`) only while
  `gh` is logged in as the signer of record: the rule's own signer for a first
  rule, the previous signer for an edit. A rule changed under any other login
  is refused and nothing merges. A signed rule merges only READY PRs whose net
  paths avoid every gate class and protected path, and a UI change only with a
  browser check of its own head on record; every other READY PR is recorded
  once per head as `drive_merge_declined` and left for a person. Each merge
  runs under the repo-wide merge-queue lock after a
  [`kew verify`](cli/verify.md) of main merged into the head, re-proves every
  gate `kew epic merge` proves, and rechecks the acceptance contract version.
  A contract amended during verification stops the merge. Each merge is
  recorded as an `epic_operator_merge` with `trigger: "drive"`. See [Standing merge rule
  attestation](audit.md#standing-merge-rule-attestation).
- **Outage handling** — a failure is reported and costs a round. A
  tick that exits with an error (GitHub or the tracker unreachable, say) is
  reported and tried again next round; the other epics keep ticking. A merge
  pass that fails is reported and retried next round, and it never merges on a
  fact it could not read: an unreadable signing login refuses the rule, an
  unreadable PR title or main skips that PR, and a main that moved during the
  verification refuses the merge. A heartbeat write that fails is retried; a
  drive whose heartbeat stays stale for five minutes, or whose pid is gone, is
  reclaimed by the next `kew drive`, and a drive that finds its lock taken
  exits without another tick.

<!-- demo-step: needs-you/3 -->
**Nobody at the keyboard**

The epic's header is its heartbeat: the driver's last tick, signal and tick
count. A tick dispatches, merges or holds work, or does nothing, and never
waits on a person; your waits join the needs-you count.
<!-- /demo-step -->

[See it in the demo](https://demo.kew.dev/epics/ledger-v2?tour=needs-you&step=3)

## Build history {#build-history}

[`kew report provenance`](cli/report.md#feature-provenance-report-provenance)
exports, per console feature, the epics, issues, pull requests and runs that
built it, with their cost and how review and verification went. The demo
console draws that export as a line at the foot of each page a feature names.
The published demo bundles kew's own export. A demo build made without it
draws invented sample figures there, and the line says "sample data".

## Governed adversarial review

Every substantive defect that reached the merge surface across the
delegation-hardening waves was caught not by the implementing agent's own tests
(honest, green) but by a **fresh-context adversarial reviewer** run by hand:
same-context self-review structurally cannot see interactions with actors
outside its context. The governed adversarial-review stage makes that manual
loop a pipeline stage the control plane owns, between "an agent opened a PR" and
the human approval surface. It is **opt-in** (`[review].adversarial = true`) and
off by default.

!!! note "Where the stage runs"
    The stage runs on epic nodes that `kew epic` or `kew drive` advances, and on
    one kew-published pull request with `kew review --governed <PR>`. On each
    tick a node due a reviewer has one dispatched against the recorded head SHA;
    the verdict is parsed, the round is recorded and the verdict is attested.
    Then the node moves: `READY` → `in_review`, `NEEDS_CHANGES` → `remediating`,
    round cap or an unparseable verdict → `HELD`. The reviewer runs in its own
    contained checkout and never pushes. Only `claude-code` can be the reviewer.
    `blocking` defaults to `true` once the stage is on, so a node whose head has
    no `READY` verdict does not merge; set `blocking = false` to record verdicts
    without holding the node. A pull request merged out of band while
    the drive's reviewer is still running is attested `MERGED_UNREVIEWED`. The reviewer runs the pull
    request's code as the agent's user, so the stage is not yet safe on pull
    requests from untrusted forks (see [Security](security.md)).

**How the stage works.** When enabled, a node that opens a PR enters
`adversarial_review` instead of going straight to `in_review`. The driver
records the PR head SHA and dispatches a fresh-context, sandboxed reviewer
against *that recorded SHA* — never the implementing session, and with the
head-ref auto-load files (`CLAUDE.md`, `.claude/`, `.mcp.json`, …) masked to
their base-ref copies so code under review can't rewrite the reviewer's own
instructions. Kew resolves both commits inside the reviewer clone and pins them
as `refs/kew/review/base` and `refs/kew/review/head`. The reviewer prompt names
`git diff --no-ext-diff --no-textconv --text --ignore-submodules=none --no-color
refs/kew/review/base...refs/kew/review/head --` as the authoritative PR
boundary; working-tree status can include masking artifacts and does not define
PR scope. The reviewer ends its run with a delimited structured verdict in its
own final result event, which the host reads from the runner's event stream and
stores on the host (not a deletable PR comment). kew reads no verdict file: the
reviewer runs the PR's tests in its container, so any file in the checkout can
be written by the code under review. The verdict must name the recorded head
SHA, and a missing, ambiguous or mismatched verdict, or a non-zero reviewer
exit, fails the round. A `READY` verdict carries executed acceptance evidence; a
`NEEDS_CHANGES` verdict carries evidenced `file:line` findings, which feed an
adapted `kew review` remediation pass (`remediating`) that loops back for
round *N+1* against a new head SHA. Non-convergence within `[review].max_rounds`
(default 2) HELDs the node for a human — never a silent loop.

<!-- demo-step: refused-pr/1 -->
**Sent back by review**

A fresh-context reviewer in its own sandbox read this pull request at its
recorded head and returned `NEEDS_CHANGES`: findings, each with a `file:line`
and the command that shows it. The next round reviews the new head.
<!-- /demo-step -->

[See it in the demo](https://demo.kew.dev/runs/5cad8a97?tour=refused-pr&step=1)

<!-- demo-step: refused-pr/2 -->
**READY after the fix**

Round two's reviewer returned `READY` on the head the fix pushed. READY is not
a merge: the drive merges only after a passing `kew verify` under a signed
merge rule; otherwise a person decides.
<!-- /demo-step -->

[See it in the demo](https://demo.kew.dev/runs/0d1ef5f0?tour=refused-pr&step=2)

A `READY` verdict carries executed acceptance evidence, or says why none could
run.

**An operator can send a READY head back.** When your own review finds blockers
on a head kew's reviewer passed, run `kew epic send-back <epic> <node>
--reason-file <file>`. It records a `NEEDS_CHANGES` round against that head,
with you as the actor and your reason as its one finding, and refuses if the
pull request has moved past it since. The next tick remediates the node like
any other `NEEDS_CHANGES` round, under the node's own item id, so the drive
keeps owning it. See
[`kew epic send-back`](cli/epic.md#kew-epic-send-back-send-a-ready-node-back-for-remediation).

**Reviews run detached from the tick.** A `kew epic advance` / `kew epic run`
tick never waits on a review. For each node that owes a reviewer round, a
remediation or the merge-main step, the tick reserves a slot under the fleet cap,
records a `review_stage` run, and starts a detached `kew epic _review-node`
child. Then it moves on. The child takes the same action a synchronous review
did, records the same verdict and round, and releases the slot. A later tick
picks up what the child recorded. If a child dies mid-run, a later tick reaps
its run as `killed` and frees the slot, just as it does for a dead dispatch
child. No round is recorded for a dead child, so the node is reviewed again.
Each child writes its own log next to the dispatch logs,
`review-stage-<item>-<run8>.log`, and the `review_stage` run records its path.
If the child's action raises, the traceback goes to that log, and the run's
error category reads `review_stage_raised: <Type>: <message>`.

The reviewer has no network, so the host prepares what its acceptance commands
need before it starts. For a repository with `frontend/package-lock.json`, kew
runs `npm ci --ignore-scripts` from the **base** commit's lockfile in the review
image, with no runner credential, caches the result under
`~/.cache/kew/review-node-modules/<sha256 of that lockfile>`, and mounts it
read-only at `/work/frontend/node_modules`; the prompt tells the reviewer to run
`npx --offline vitest run …` and `svelte-check` there. A PR that changes
`frontend/package-lock.json` gets no mount — the PR's own lockfile never chooses
what is installed — and the prompt says so, so that review reads the frontend
instead of running it.

**The reviewer judges intent, not only the diff.** The dispatch that opened the
PR recorded what the issue asked for as an `item_plans` row — the acceptance
criteria kew read out of the body it fetched itself, before implementation, under
a `plan_sha`. The reviewer prompt states those criteria, read from that ledger
row and never from the head ref (the branch under review can rewrite any file in
its own checkout), inside the same untrusted-content fence the issue body gets.
They are a floor, not a ceiling: a criterion never waives a defect, and every
other lens still applies.

The verdict then has to answer for each one — `pass`, `fail` or `unverifiable`,
with the command that settles it. A verdict that leaves a recorded criterion
unjudged fails the round; a `pass` with no executed evidence is recorded as
`unverifiable`; a `fail` needs an evidenced finding naming that criterion; and a
`READY` carrying any `fail` fails the round, because there is no "ready except
for what it was asked to do". A `kind = test` criterion is proven by its
acceptance-contract test (`test_ac<k>`), so its result must also cite that
test's executed outcome: a `pass` that cites no passing run is recorded as
`unverifiable`, and a cited failing run makes the criterion `fail` — which,
like any failed criterion, the review stop rule never accepts as a residual.
The round records the `plan_sha` and the criteria
text it was handed, the PR's verdict comment shows the per-criterion table, and a
remediation brief lists the failing criterion ids above the findings. An item
with no recorded criteria behaves exactly as before, and its verdict comment says
"no acceptance criteria recorded for this item" rather than printing an empty
table — an absent table and an ignored criterion must not look alike. For the
same reason the comment also names every criterion the round did *not* judge: a
plan whose last criteria did not fit the reviewer prompt gets them listed under
the table, so a partial table is never read as a contract fully met.

The review stop rule does not reach past this. After
`[review].stop_rule_after_rounds` it accepts a late NEEDS_CHANGES round whose
findings are all within `[review].stop_rule_max_severity` (low and medium by
default, lows only under `"low"`) as READY-with-residuals, but a round reporting
a failed acceptance criterion keeps remediating whatever its severities: that is
the work missing what it was for, not a residual defect to track and merge
around. And the criteria rules are enforced only against the reviewer prompt
artifact kew currently pins — a consumer repo whose base ref still deploys an
older copy reviews as it did before, judged against no recorded criteria, rather
than losing every round to a rule its reviewer was never told. That round records
no `plan_sha`, because nothing was judged, but its verdict comment reports the
recorded plan it skipped and why: "kew holds criteria it could not show this
reviewer" and "the issue stated none" are opposite facts before a merge, and
syncing the pinned artifact restores the first one.

A round the sandbox refuses **before launch** — the pinned reviewer runner has
no verdict channel, no `[sandbox.images]` mapping, or the local container
runtime cannot resolve the mapped image — is infrastructure, not a verdict: no
reviewer ran and nothing was spent. kew retries the round on the next tick and
reports the node's wait as `review_retrying — review_sandbox_refused: …`
instead of crediting an unparseable verdict, so a momentarily absent image no
longer HELDs the node (and stalls everything downstream). The wait names no
person, so it raises no attention event. The retry is bounded: three consecutive
pre-launch refusals stop being treated as transient and HELD the node under that
same `review_sandbox_refused` reason.

A round a **provider usage limit** ended is infrastructure for the same reason:
the reviewer's stream carries the provider *rejecting* a request — not merely
warning that a usage window is filling up, which a long run reports routinely —
and the run ends with no verdict block — on an error result, or on nothing at all when the limit kills
the reviewer mid-stream — so no reviewer ever finished judging anything. kew
records that round as `review_rate_limited` — no hold, and no round counted
against `[review].max_rounds` — and re-reviews the node once the limit has had
time to reset (the host cannot read the provider's exact reset instant, so it
waits 15 minutes and tries again). The wait is reported on every tick and asks
nothing of you: a provider window resetting is not anybody's job. Its reason
names the agent token the limited round ran under, by env var and fingerprint
(never the value), so the same token reads as one unchanging wait. Replacing
that token ends the wait: the next tick re-reviews under the new one.

That retry is bounded too, just over a longer span than the pre-launch one: once
a limit has ended every reviewer round for **six hours** it is not a window
resetting on its own, and the node is HELD under `review_rate_limit_exhausted` —
a hold whose action is to raise the provider usage limit, or to pin
`[review] runner` to a runner on another provider. The bound is what makes the
wait reachable at all: a node waiting in review carries no hold row, and every
stall clock runs off a hold row or an open PR, so an unbounded wait would
relaunch a reviewer every 15 minutes forever while the epic reported
`progressing`.

Those six hours are six hours of kew *retrying*, not six hours on a wall clock.
Nothing records a reviewer round while the driver is not running, so a gap
longer than an hour between two limited rounds — a host that was stopped
overnight, say — starts a fresh incident: the first limit you hit after the
restart is waited out like any other, not held on the spot under a limit nobody
had asked to reset.

That hour-wide gap is fixed and your driver's cadence is not: `kew epic advance`
run from cron, or `kew epic run --interval` set above an hour, spaces every pair
of limited rounds further apart than any live retry could, so on those hosts no
six-hour span ever accumulates. There the retry is spent by *count* instead —
**25** reviewer rounds in a row ended by a usage limit, with none in between
that got past the provider, earns the same hold. Either bound reaches a person,
and neither can be stretched out by how often you tick.

A reviewer that survived a rejection and then finished cleanly *without* a
verdict block is a different failure — that is a verdict defect, and it keeps the
`review_unparseable` hold. So is a round that only ever saw the provider's
warnings and then failed for its own reasons: nothing rejected it, so nothing
says a usage limit ended it.

Every other failure that lands **before the reviewer starts** — no container
runtime, a runner kew cannot authenticate inside a container, a pre-launch step
that raised, a reviewer container kew could not create or start — HOLDs the node
as `review_launch_failed`. A container that could not be started keeps the run's
`container_start_failed` category, and the hold names the container; so does a
container that refused its agent's startup (`sandbox_startup_refused`). Neither hold is
`review_unparseable`: there was no reviewer and no verdict, so an operator sent
looking for a verdict-channel defect is looking one layer too high. Both reasons
say the reviewer never started and name the fix for the layer that refused — for
an image that was never built on this host, `kew sandbox build --runner
claude-code`. `kew epic unhold` releases them like any other honored hold. A
reviewer that *did* start is a different failure and keeps its own reason. kew
claims `review_launch_failed` only where its own records prove no reviewer run
was ever admitted. A reviewer launch in which a runtime call timed out — every
`create` of the reviewer's container (`runtime_timeout`, below), or the
provisioning container's `rm -f` or the check that it is gone — never started
either, but a loaded host is not something a person must fix: the node waits as
`review_retrying`, and a later tick launches the review again after a back-off
of two minutes that doubles each time. Only the third such launch in a row
holds the node as `review_launch_failed`, and that hold says the reviewer never
started and names the timeout.

A reviewer that started names what went wrong. A run that finished cleanly but
whose runner wrote more than one `result` event (it started a second turn) is a
runner fault: kew takes no verdict from it and re-reviews once per node and
head — a wait, not a hold, so it raises no attention — and only a second such
round holds the node as `review_multiple_results`, with the stream's refusal
(for example "2 result events") in the reason. A retry kew could not record
holds at once instead, so an unrecorded retry never becomes a paid run every
tick. A reviewer run that failed — its container exited non-zero, including one
that died mid-run — and left no verdict holds as `review_failed_no_verdict`,
with its exit and what its stream lacked in the reason; it is not retried. Every
other inadmissible verdict holds as `review_unparseable`, followed by what kew
knows was wrong: results carrying two *different* verdict blocks (one of them
may be forged, so there is no retry), a final message with no verdict block, a
stream kew lost after the container started, or kew's own post-run bookkeeping
raising after the run was recorded (the reason names the raise). Each of these
verdicts carries the same refusal text on the audit chain.

A failed or refused `kew review` used to leave its checkout behind for the next
launch to move aside, with no limit. Now a launch that no attempt ever ran an
agent in discards its checkout (one whose earlier attempt ran keeps it, edits
and all), each PR keeps at most one preserved copy (the newest), and the driver
removes a PR's review checkouts once it sees the PR merged or closed.

A remediation whose publication was deferred (`awaiting_publication`, or a
publication held for review) keeps exactly one copy: the checkout its deferral
recorded, for as long as its run is in a deferred state and that copy is on
disk — whatever the PR head says, since kew's own merge-main step, GitHub's
"Update branch", a rebase or a force-push all move the head without publishing
the deferred work. When the next review moves that checkout aside, kew points
the deferral at the preserved copy, keeps that copy, and prunes the rest as
usual (the newest one kept). A copy that is gone was discarded, so the
deferral then protects nothing, even though its run row stays
`awaiting_publication`; a path kew cannot check counts as present. If kew
cannot record where the deferred work moved — or cannot read its run ledger to
tell whether the checkout holds deferred work at all — it leaves the checkout
where it is and refuses the launch as `deferred_checkout_unrecorded`, which the drive holds
as `admission_refused` until a person publishes or discards that work. A PR
that closed without merging keeps each deferral's copy; a merged PR keeps none.
When a deferred run's recorded checkout is missing or unreadable (its hold
says "preserved checkout unknown"), kew removes nothing.

A container `create` the runtime does not answer within 10 seconds — a host
too loaded to keep up — is retried before the run fails, up to three attempts
with a short backoff. The retries also stop at a ceiling per container
attempt — the container's own four-hour wall-clock limit, counted from that
attempt's launch, not the run's configured timeout — and no retry starts that
could end after it. Before each retry kew removes the container the timed-out
create may still have made (only a never-started one labelled with this run),
so the retry neither collides with the name nor leaks a container; each retry is
recorded as a `container_create_retried` audit event. When every attempt times
out, the run fails as `runtime_timeout` rather than a bare exception. A
remediation that fails this way spends no review round, and the next tick
dispatches it again.

A `create` that fails for any other reason carries the runtime's own words:
docker's stderr, redacted and cut to one bounded line, is part of the run's
recorded failure reason and of the failed node's stuck reason. When the daemon
committed the container even though its client exited non-zero, and that
container is `created`, never started, and labelled with this ledger and this
run, it is this create's own: kew records its id, removes it by that id, and
creates the container once more, whether the failed create reported its own
error or a later create met that container by name. Only when that create
fails too does the run fail as `container_start_failed`, and the item's next
launch is not refused by its name. A container under the name that kew cannot
prove is this run's is left alone, unless it never started and its labels
prove it belongs to a run of this ledger that is no longer running; kew removes
that one by its id even when a loaded daemon cannot be named.

kew removes and kills the container a run launched only by the id the launch
recorded, never by its `kew-<item>` name: the name is host-wide, and on a
loaded host the container under it can be another launch's. A launch that has
recorded no id has nothing removed or killed, and the log says why. Two
cleanups still name a container, because nothing records an id for them: a
review's provisioning container whose client died before writing its id file
is removed by its fixed name (`kew-<item>-env`, `kew-<item>-env-check`), which
only that item's own provisioning uses, and a readiness probe that died mid-call is stopped by its
own per-probe name.

A dispatch whose owner died before its container started is reaped rather than
left `pending`. Admission and the epic tick both apply one rule: the owner is
proven dead (never merely unknown), the ten-minute startup grace has passed,
no attempt was ever registered, and the container is `created` with pid 0 or
absent. The container is found by its recorded id, or by its name together with
the `io.kew.ledger` and `io.kew.run` labels; an id read back only from the
run's cidfile must carry those labels too. A container labelled with another
run is never removed. kew records a `dead_run_container_removal_intended`
event naming the container id, its name, the runtime and the run before it
removes the container by id. It then finalizes the run as
`container_start_failed` (a run already stopped by `kew kill` or `kew stop`
keeps `killed`), frees its slot, and writes one `never_started_run_reaped`
event. Admission, the epic tick and [`kew settle <run-id>`](cli/settle.md)
write the same events with the same payload keys, but not identical fields:
each names its own actor (`policy`/`liveness-reconcile` for admission,
`policy`/`driver_reconcile` for the tick, `human`/`operator` for `kew settle`),
and only the tick records the run's `repo`. If the run was finished by another
process after its container was removed, kew records the removal as
`dead_run_container_removed` and leaves the run alone.

A `stopped` container does not free a slot on its own. A slot is freed when the
runtime says the container is absent, or when kew has removed it itself. On
the admission path, a dead run whose owner and registered client group are both
proven dead past the startup grace has its stopped container (`exited`,
`created` or `dead`, with no process) removed the same way: kew reads its
state by id again, records a `dead_run_container_removal_intended` event with
that state, removes it without `-f`, and records `dead_run_container_removed`.
A removal the runtime refuses keeps the run and its slot. An owner recorded on
another machine has unknown liveness here, so nothing of its run is removed.

Each launch records the daemon its container is created on. Both rules trust
the runtime's "No such container" only from that daemon: after a context
switch, or when the current daemon cannot be named, the run is retained. A run
launched before kew recorded its daemon is judged as before. Neither admission
nor `kew settle` reaps a run that owns a live continuation; the epic tick owns
that recovery, and `kew settle` names `kew epic continue` and
`kew epic abandon`.

A remediation kew refuses because the node's remediation pool
(`[review].max_remediation_usd`) is exhausted HOLDs the node with a
`review_budget` reason that names what the pool allows, what the node already
spent, and the key to raise. It is not left `remediating` — a state the tick
signal reads as progressing while every later tick refuses the dispatch again.
The driver re-checks the pool each tick, so raising the cap clears the hold and
resumes the fix cycle on the next tick without a `kew epic unhold`.

A review remediation never runs twice at a head it did not move. Before it
launches one, the driver reads the last remediation of the standing round. If
that run ended cleanly and the PR still stands at the round's head, it pushed
nothing, and another run would read the same brief and end the same way. When
that run changed the PR body's **Proposed behavior changes** block, the head is
reviewed once more, so the reviewer can rule on the new lines. Otherwise the
node is held `remediation_no_progress`, with the stale verdict's head and round
in the reason. A NEEDS_CHANGES from that one re-review is held the same way.

An approver (`[approval] approvers`; with that list empty, any operator) who
releases that hold, or a `remediation_no_change` hold at the round's own head
or at the head a remediation that changed nothing published after merging a
moved base, has two paths. When the **Proposed behavior changes** block in the
PR body changed after the hold was set, or already holds a line for every test
the round asks about (whoever wrote it, and whenever), `kew epic unhold` earns
that head one re-review instead of a remediation, and the reviewer's ruling on
those lines decides the round: an accepted line reaches **Behavior changes**,
and a NEEDS_CHANGES is held again. A body kew cannot read right after the
release is read again on the next pass, never taken as unchanged. Any other
release allows one more remediation at that head, as before. That includes a
release by someone the approvers list leaves out, a release whose head a base
integration has since moved, and a second release when the body has not
changed since the reviewer last read it at that head. None of them buys a
second re-review.

A round whose every finding asks only for a **Behavior changes** line, but which
no flagged-test record makes a declaration round (a frontend test file, say),
gets a brief that names each requested test and makes one proposed line per
test, with a reason, the remediation's whole job. A test the proposed block
already has a line for is left out of the brief, so the remediation never
replaces that line. A run that ends with no
Dispatch Report, or with no line for a test, proposes nothing and is held.

A remediation whose own **publication** the host deferred for a human — its
diff touched a protected path like `.github/workflows/` — records
`awaiting_publication`, never `success`, and its work stays in a preserved
checkout. The PR head therefore never moves, which reads exactly like a round
nobody has remediated yet, so the next tick HOLDs the node under
`publication_held` (the reason names the checkout) instead of dispatching
another remediation against the same findings. Publish that checkout or discard
it, then `kew epic unhold` — publishing moves the head, and a discard deletes
the path the reason names, which is how the driver tells that the release is
not a bare one it should re-derive. Protected paths are judged on the
remediation's *own* change: a workflow file whose bytes are already what the
PR's base branch holds arrived with a base merge and is published without
asking anyone.

The same deferral catches a base **sync made by copying files**. kew merges the
base into the review checkout before the agent starts, so the base's content is
expected there — but content that is the base branch's *current* head's own,
which neither the PR branch nor the commit kew integrated holds, was fetched and
copied in rather than merged. That inflates the PR's diff against its unmoved
merge base with code the node never wrote, which the next review rounds then
review as if the node had written it, and a copy landing on a file the branch
changed erases that work. Such a publication is held with every copied file
named, and the fix is a real merge: `kew resolve` publishes the base branch's
head as a second parent, and a merge carrying *that head* is never judged. A
publication that names no base head — every dispatch publication — is not judged
for a copy at all, and neither is one whose branch, base head or integrated base
kew could not read: a tree that will not say what it holds is no evidence that it
lacks the content, so the check stands down rather than hold finished work on a
transient git failure.

A node's **acceptance contract** — its acceptance test file, recorded in the
ledger by path and sha256 before the implementer starts — is read-only to every
run that publishes for that item, dispatch and remediation alike. A publication
whose tree would not hold the file byte-identical (edited, rewritten or
deleted) is held as `publication_review_required` with reason
`acceptance-test-changed`, and nothing is pushed. A loop run's sabotage guard
also counts the file among its guarded paths, so an edit to it ends the loop as
sabotage rather than a passing gate.

A remediation publication is itself a merge. kew's checkout integration really
did merge a base commit in, so the published commit records that commit as a
second parent: the PR's diff against its merge base stays the branch's own work
rather than the base's newer files, and the next time the base moves the
integration merges cleanly instead of conflicting on code the node never wrote.
The parent is the commit the host integrated — never a value read back out of
the agent's checkout — and it is recorded only when there is a merge to record:
a branch that already contained that commit, a checkout that recorded none, or
a commit the remote cannot answer for publishes with the single parent as
before. So does a checkout that no longer holds the merge at all: an
integration that conflicted is handed to the agent to resolve, and one the
agent abandoned instead (`git merge --abort`) leaves a tree with none of the
base's content — a merge the commit would claim and its own tree would
contradict, after which the next integration is told "Already up to date" and
merging the PR reverts the base's own commit. Because that parent accounts for
the integrated commit and nothing else, content the base branch has acquired
*since* is still judged a copy.

A publication the **remote** failed is neither of those. A 5xx answer, a
`remote: Internal Server Error`, a rejection carrying no ref-protection or
non-fast-forward cause: the push said nothing wrong, the server did not answer
it, and the commit the host built is still exactly the one to send. Host
publication sends it again — three tries over about a minute — and a remote that
recovers in that window publishes the finished work. One that keeps failing ends
the run on the same path as the deferral above: `awaiting_publication`, the
checkout preserved, the node HELD under `publication_held` with that checkout
named, so an operator pushes the work that already exists instead of a fresh
remediation redoing it from scratch. Rejections the remote decided from the push
itself — a protected branch, a non-fast-forward or a stale force-with-lease, a
push rule or a secret-scanning refusal — are reported on the first answer,
because waiting buys the same rejection.

**Static checks and ui-checks before review.** When `[commands] static` or
`[commands] ui_check` is set, a head that is due a reviewer first goes through
those gates, once a reviewer could launch (no usage-limit wait, no running local
merge verify, a free review slot). They run in the approved image with no
network and no credentials, each container over a clone of its own. Host git
does all its work first, in a clone no container mounts. The static command
runs on the head. The ui-check command runs on the merge candidate, which is the
current `origin/<base>` merged into the head in that discarded clone, and only
for the modules the head touches. A failure, or a base that does not merge, is
recorded as a `NEEDS_CHANGES` round that carries the output. The node is
remediated on the same tick, and no reviewer launches on that head. A clean
head goes to review. A command that fails on the head counts only if it passes
on the base. A check that could not run (no container, a launch the runtime
refused, a timeout, a missing tool, a base that fails the same way) is not a
defect, so the head goes to review without it. See
[`commands.static`](configuration.md) for the image these commands need and the
bound on these rounds.

**Held-out before review.** With `[verify] held_out_before_review = true`, the
same step also runs the merge verify's held-out phase on the merge candidate,
on the verify host, and never on this machine: with `[verify] host = "local"`
it is skipped, since it imports the head's code before anyone reviewed it. It
runs when the change selects a set of base test files; a change that selects
the whole base tree is left to the merge verify. The base branch's own tests
run against the head merged with the current base, and kew judges them against
the PR body's **Behavior changes** section, by the merge's own rule. A failing
test main's health lists as red is main's, not the head's, and every other
failing test runs once more on the base's own code: one that fails there too is
main's as well. A failure that is the head's and that no line declares is
recorded as a `NEEDS_CHANGES` round, and no reviewer launches on that head. The
round names the failing tests, quotes the section as it stands, and states the
rule: fix the source so the base test passes, or, if the change is intended,
declare each test in the remediation's report. kew rebuilds each declared line
from the requested test id and the agent's reason, and adds it to the PR body,
where the reviewer reads it. The implementer's proposed lines answer nothing
here: only a reviewer, or a person, accepts one. A clean or fully declared head goes to review. A
held-out run that could not run, or broke without naming a test, is not a
defect, so the head goes to review without it, as for the other checks. A
head that edits a gate definition (`[epic] merge_gate_paths`, the lock among
them) is not run before review: the merge verify runs it only after a person
releases its merge-gate hold. The merge verify still requires the same
declaration. See
[`kew verify`](cli/verify.md#held-out-base-tests).

**Cheap gates first in the merge verify.** `kew verify` runs the static gates,
the containment test and the held-out phase as one job, and judges it before it
starts the pytest phases. A red static gate, or an undeclared held-out failure,
fails the verification in minutes, with no pytest phase started. See
[What runs on the host](cli/verify.md#what-runs-on-the-host).

**Branches that stop merging.** With siblings landing in parallel, a branch in
review falls behind the base and can stop merging into it — shared export
barrels and fixtures are the usual casualties. When GitHub reports the PR's
merge state as `DIRTY`, the driver spends that node's one action for the tick
on a **merge-main step** instead of a reviewer or a remediator: the governed
`kew resolve` path integrates the base and publishes the integration commit
under a kew-owned producer, so the new head stays bound to a run and the next
remediation is not refused the way it is after a merge commit an operator
pushed by hand. The node keeps its review status, and the next tick reviews the
moved head as a delta round; the merge pass skips the node for that one tick,
because the head it would have judged is already gone. Only `DIRTY` triggers
it: `BEHIND` is stale but still merges cleanly, and `UNKNOWN` is GitHub still
computing.

The step reaches a node anywhere in the review stage — awaiting a reviewer,
remediating, or carrying a standing `READY` verdict — and also one the merge
pass has already parked on `ci_pending`, the hold a red or not-yet-mergeable PR
earns. That last case matters because `ci_pending` is exactly what a `DIRTY`
branch would otherwise sit under forever: the merge pass's only answer to
`DIRTY` is to write that hold again. The step releases it, and the pass
re-derives it on the next tick if the moved head is still not mergeable. A node
held for any *other* reason is left alone until you `kew epic unhold` it —
those holds exist to stop exactly this kind of retry.

The step never pushes onto a branch an operator owns. When the head's newest
commit is not one kew published (kew has published on the branch and none of
those pushes is this head, or you ran `kew publication attest` on the head to
bind your own commits on top of a published one), or while an operator's merge
verify claims the head, the node records `operator-owned: integration skipped`
(a `base_integration_skipped` audit event, once per head) and waits instead. It
is not held: merge the base on the branch yourself, and the first head kew
publishes gives the step back. A head `kew finalize` bound is not yours: it is
the agent's held output you published for it, so the step integrates it like
any head kew published.

The decision holds only for the head it was made on. `kew resolve` is pinned to
that head: if the PR's head has moved by the time it reads the PR (you pushed
on top in between), it checks nothing out and pushes nothing, and the node
records `head moved: integration skipped` (a `base_integration_skipped` event
naming both heads). That is not a hold and does not count toward the step's
bound: the next tick reads the new head and decides again, so a head you pushed
is left to you.

Wherever the step does push, it runs the node's checks on the resolved tree
first: `[resolve] python_checks` when the integration changed Python files and
`[resolve] frontend_checks` (for example `svelte-check`) when it changed
frontend files, under the same containment-first admission as the merge queue's
verify. They are a cheap guard, not the merge gate, so they are meant to be
whole-tree static gates. A check that fails, times out or cannot run pushes
nothing, and the hold names the check's key and its first failing line. The
hold is `base_integration_failed` when the base merged cleanly, and
`base_integration_conflict` when the check refused an agent's conflict
resolution.

Conflicts that are not resolved HOLD the node under `base_integration_conflict`
with the conflicting paths named in the reason; a step that could not run at
all HOLDs under `base_integration_failed` and leaves the branch untouched — a
merge-main step that did not happen is never reported as a branch that is
current. Success is bounded too: a step that keeps reporting that it integrated
the base while leaving the head exactly where it was would otherwise spend an
agent run every tick forever, with nothing held and no stall clock running
against it. After three such steps in a row — three at the *same* head, with no
reviewer or remediation run for the node in between — the driver stops and HOLDs
under `base_integration_failed` rather than starting a fourth. A step that moved
the head did work, so it starts the count over: a long-lived branch integrates
a fast-moving base as often as the base moves.

Round 1 is a full review of `base...head`. From round 2 a round is a **delta
round** when the previous round's head is an ancestor of the new head and its
stored verdict still parses. Kew pins that head as `refs/kew/review/prev-head`
and adds the previous findings (severity, `file:line`, summary) to the prompt as
a quoted JSON block marked as untrusted data. The reviewer confirms each prior
finding is fixed or still open, reviews `prev-head...head` in depth, skims
`base...head` for regressions the delta could cause, and reports the prior
findings' status and what it checked outside the delta in its verdict. After a
force-push or rebase, or when the previous verdict is missing or unreadable, the
round is a full review and the prompt says why. Each round records whether it
was `full` or `delta`. A `READY` still needs one full pass: a delta `READY`
stands only if one of the two rounds before it was a full review. Otherwise kew
records it as `NEEDS_FULL`, which the merge gate treats as not ready, and runs a
full round on the same head next.

A PR whose diff touches a merge gate path (`[epic].merge_gate_paths`: scripts,
scanners, docker, workflows, lockfiles, `kew.toml`) needs two full `READY`
rounds on the same head. Kew reads the touched gate paths off the pinned
`base...head` refs before the reviewer launches. The first full `READY` is
recorded as `NEEDS_FULL`, and the PR comment says a second full review must
confirm it and lists the gate paths. The confirming round is a full review whose
prompt says only that the PR changes merge gate paths: it is not told the
earlier verdict, and it does not spend one of the `[review].max_rounds`
remediation rounds. A delta `READY` never confirms, and a new head starts the
count again. Every round on such a PR is a full review.

The stop rule treats residuals the same way. On a gate-path head, a full round
whose findings it would accept is recorded as `NEEDS_FULL` like a first
`READY`: it is not remediated and spends no remediation round. The confirming
full round then accepts its residuals if it returns only residuals again, and
its `READY` stands if it returns `READY`.

Reviewer and
remediation runs draw from a **separate attested budget pool**
(`[review].max_review_usd`), which bounds review spend per item: implementation
retries never spend it, and review work never spends an item's implementation
budget. The daily and weekly caps (`budget.daily_usd`, `budget.weekly_usd`) are
shared by every run kind. Once implementation spend passes one of them, kew
starts no new review or remediation either, and holds the node with a
`budget:` reason until the window moves on or the cap is raised.

A governed remediation pass runs targeted tests, not the full suite. Its prompt
asks for the tests that cover the changed files and a regression test for each
finding, then lint and format on the touched files. kew does not leave the whole
selection to the agent: it reads the branch diff and names, in the brief, the
test module matching each changed source module (`tests/test_<module>.py` and
`tests/test_<module>_*.py`, when they exist). Each path is named relative to the
package directory holding those tests, and the brief says to run the configured
test command from there — a diff is repo-rooted, but a test runner is configured
for its package, and a repo-rooted path fails collection outright when the two
differ. That list is a floor — the agent may add to it, never drop from it — so
a remediation cannot skip the test module named after the file it just edited. It forbids the full test
suite, `verify.sh`, and the frontend suite, except the frontend tests for
changed frontend files. The full verification runs before any merge. A
standalone `kew review` and an implementation dispatch keep the full-suite
instruction.

Remediation publication is bound to the head that was reviewed. If a reviewer
has already pushed its clean review commit before the host broker runs, the
broker verifies that commit (or a fast-forward containing it), records that
SHA, and completes without a second push. An unrelated branch move remains a
publication failure and is reported as “review completed but was not published
because the branch moved.”

**Advisory → blocking adoption path.** The stage has two modes, and the
recommended path is to adopt it advisory-first:

- **Advisory (`blocking = false`)** — the verdict (or a `DEGRADED` marker if the
  reviewer crashed) is attested and the node proceeds under the pre-existing
  approval + CI rules, plus the one merge verdict rule every merge path applies
  ([`kew merge check`](cli/merge.md#checking-a-hand-merge)): the driver does not
  merge a head whose newest attested verdict is `NEEDS_CHANGES` or
  `NEEDS_FULL`. A non-`READY` verdict remains visible: `DEGRADED` is
  published as `adversarial review: DEGRADED (advisory)` with a successful
  `kew-review` status, so it does not make an otherwise-green PR ineligible for
  delivery. Unrelated failed checks remain blocking. This is the on-ramp: turn
  it on, watch the verdicts against your real PRs, and tune the pinned prompt
  artifact before you let it gate anything.
- **Blocking (`blocking = true`, the default once `adversarial` is on)** — a
  non-`READY` outcome (reviewer crash after one retry, unparseable verdict,
  review-budget refusal, a post-verdict force-push) all land in the same place:
  **HELD: needs-human, with an attested reason**. There is no advisory-degrade
  path inside blocking mode, so a PR-controlled reviewer crash cannot bypass the
  gate.

**THE invariant (enforced at the merge pass, not the status label).** `HELD` is
a live merge candidate — approval + green CI would merge it — so gating only the
status transition would leave a hole. Instead the merge pass gains one
invariant, checked at merge time for every **policy-covered** candidate of a
review-enabled epic:

> merge only if the latest verdict is `READY` **∧** `verdict.head_sha ==` the
> current head SHA **∧** the approval timestamp postdates the verdict.

This is what makes blocking mode real: it closes the HELD-is-mergeable hole,
merges only the pull request head that was reviewed (a post-verdict
force-push can no longer merge unreviewed code), and is the natural home of the
approval-postdates-verdict rule (a stale, pre-existing approval surfaces as
"re-approve" rather than auto-merging). A candidate that fails the invariant is
skipped with a loud driver line and an attested reason — never a silent skip.
Nodes already at/past `in_review` when the flag flips are grandfathered
(`PRE_POLICY`) and merge under the pre-existing rules, plus the merge verdict
rule that applies to every merge; flipping the flag never
throws an approved PR back into review. The grandfather only covers nodes the
review subsystem has never touched: one it has already held (a `review_*`
reason) or already dispatched a reviewer for is never pre-policy, even with zero
recorded rounds — so a round that ended without a verdict (a pre-launch sandbox
refusal awaiting its retry, a crashed launch) cannot be merged unreviewed.

**The GitHub-native gate (`kew-review` status check).** The merge-pass invariant
enforces kew's *own* merge — it cannot reach the GitHub **merge button**. A human
(or an autonomous flow) can click "Merge" on GitHub before the reviewer finishes,
and the verdict then lands on an already-merged PR (incident #1013). To close that
race, kew mirrors the same verdict onto the PR head SHA as a **`kew-review` commit
status** (`[review].publish_status = true`, on by default): **pending** the moment
a reviewer round is dispatched, **success** on a `READY` bound to the current head,
**failure** on `NEEDS_CHANGES` or any crash-path HELD, and back to **pending** when
a post-verdict force-push moves the head (keyed to the exact reviewed SHA, mirroring
the invariant's head-sha binding). Posting is best-effort — a `gh`/GitHub failure is
logged and swallowed, never crashing the tick. Publishing the `kew-review` status alone does not block merges — the check only enforces the merge button once a branch-protection rule on `main` **requires** it. To make it *block* the merge button,
enable a **branch-protection rule on `main` requiring the `kew-review` check**; that
is the piece the code cannot enforce. Apply it once the check first publishes:

```bash
gh api -X PUT repos/OWNER/REPO/branches/main/protection/required_status_checks \
  -f strict=true -f 'contexts[]=kew-review'
```

This is an **additional** GitHub-native gate that mirrors the verdict; it does not
change the verdict contract or the kew-driven merge-pass invariant.

**The verdict is an input to human approval — never a replacement for it.**
Human review has repeatedly caught what the full AI chain missed, and nothing
here dilutes that gate: there is **no auto-merge on `READY`**. Approval stays
human; the chain-attested verdict is *evidence* for that decision — which exact
commit was reviewed, under which policy, at what spend — never a reason to
rubber-stamp. A reviewer can still run tests and lazily approve; that residual
is bounded by the human gate (and a v2 sampled second-reviewer audit), not
hidden.

**Scope (v1).** The stage proves itself on the **epic path**, where a human is
already watching the approval surface. The unattended delegation surface
(`kew delegate watch`) — the surface that arguably needs adversarial review
*most* — is deliberately **out of scope until v1.1**: extending the stage there
is mechanical once the shared reservation reconciler is proven, but no
unattended operation is advertised as review-gated until it actually is.

## Sandboxed dispatch

When `[sandbox] enabled = true`, kew replaces the host-subprocess agent launch with an ephemeral container per dispatch. The flow diverges from the standard dispatch loop at step 5 (Execute):

1. **Standalone checkout** — kew clones the repository into a temporary directory at the base commit of the issue branch. This self-contained checkout is the container's only volume mount — host `~/.claude`, `dispatch.db`, and GitHub credentials are outside the mount and invisible to the container.
2. **Container admission** — kew resolves the image, transport, model, and authentication path. It records that immutable profile before launch. Missing evidence fails closed. A publication-bearing run with `auth_path = "env:<name>"` is refused before the paid child starts. Current provider-key container routes therefore remain on hold. Read-only review, planning, readiness, and non-publication work stay unchanged.
3. **Container run** — an admitted non-publication run uses the hardened `docker run` boundary. Kew uses `--read-only`, `--cap-drop ALL`, and `--security-opt no-new-privileges`. It also sets process, memory, CPU, filesystem, and wall-clock limits. The [container entrypoint contract](sandbox-entrypoint-contract.md#host-side-hardening-kew-applies) defines every limit. Kew forwards only the resolved runner's provider credential name. It never places the value in the command. No GitHub token enters the container. A future contained publication route must prove non-reusable authentication. This guard does not create that route.
4. **Shadow comparison (optional, off by default)** — checkout publication can compare its tree with the host-owned broker tree. Canonical publication does not produce this comparison because the legacy input requires checkout-rooted Git. It emits `shadow.skipped` with reason `canonical_publication` and prints one notice. Shadow observation publishes nothing and cannot fail a dispatch. See [`[publication]`](configuration.md#publication).
5. **Host-brokered push and PR** — before every publication effect, kew checks all attempt profiles and retained continuation generations again. Normalized jobs also check every predecessor producer. Review and resolve bind the observed remote head to retained publication evidence. Exposed, incomplete, or ambiguous protected lineages cannot create a commit, push, or pull request. The host preserves the checkout on refusal. An admitted route uses the host's GitHub credential. Epic children carry detached publication authentication as a host-only capability. The agent never receives that capability.

When the host writes that commit itself, its subject describes the change, not
the ceremony: `<commit_prefix><description>`, where the description is the first
item of the agent's own Dispatch Report "What was done" section when the run
recorded one, and the issue title otherwise. The description is folded to a
single line and trimmed to a conventional subject width, and any closing
reference in it (`Fixes #12`, `closes GH-3`, `resolves owner/repo#4`, or a
keyword before a URL) is removed, so merging the commit closes nothing the
agent or the issue title named. The provenance moves
into the body — `complete work for #<n> (committed by kew)` and a
`Kew-Run-Id:` trailer naming the run. A run with neither a title nor a usable
report keeps the older generic subject, because there is nothing to describe.

The pull request says what the product does once it merges. Its title is the
report's `PR title:` line, a sentence the agent writes for it; without one, the
first sentence of the report's summary paragraph; without either,
`Fix: <issue title>`. A "What was done" bullet never titles a PR, because those
name code paths. The title is stripped of markdown, folded to one line and cut
on a word boundary at 200 characters. A closing keyword followed by a
reference (`fixes #77`) gets a zero-width space after its first letter, so
merging the PR closes nothing it names. The epic driver squash-merges the PR
under the title its PR facts carry, `<title> (#<pr>)`, folded to one line and
with the same break. Behind the merge queue the title comes from the head
re-read after the verification, so a title corrected while it ran is the one
that lands. When the facts carry no title, or a blank one, the squash
keeps GitHub's default subject and the driver logs a warning naming the PR.

The body opens with the closing reference, then a short **Cause** (the issue
title), **Change** (the first "What was done" item) and **Proof** (the first
"Testing" item), escaped: `<`, `>`, `&`, `@` and `*` become HTML entities
GitHub renders as the characters themselves. The report's **Behavior changes**
lines follow verbatim (control characters other than tab removed), in a code
block under **Proposed behavior changes**. The
merge's held-out gate reads only a **Behavior changes** section, so an agent's
own line declares nothing and cannot excuse its own test edits. kew's reviewer
is shown each proposal and rules on it in its verdict: accepted when its reason
matches the issue and the base copy of the test fails only on the declared
behaviour, rejected otherwise. Before kew records the verdict it writes each
accepted line into **Behavior changes**, rebuilt as `- <node id> — <reason>`
from the proposal itself, and re-reads the body to confirm it. A verdict whose
only open items were proposals the reviewer accepted is READY; a READY whose
lines cannot be confirmed in the body is not recorded, and the node is held
(`declaration_record_failed`). A rejected proposal never reaches the body, and
the reviewer gives each rejection a ground. `not_needed` means the test needs
no declaration: its base copy passes against the head, or the file only adds
cases. kew drops that line from the proposed block, leaving the heading and
an empty fence, and it is no finding: it never turns a READY into
NEEDS_CHANGES, so a READY whose only rejections are `not_needed` stays READY
and needs no remediation. It is no finding for a NEEDS_CHANGES either: a
NEEDS_CHANGES with no evidenced finding of its own is unparseable (`NEEDS_CHANGES
with no evidenced findings`), the node is held, and nothing is dropped. kew
drops nothing from a proposed block whose fence never closes, nor when the
drop would change what **Behavior changes** declares. `wrong` means the change does need a declaration and this line is
not it. That rejection is a finding naming the test and the reviewer's reason,
and so is a rejection with no ground or any other ground. The node id,
everything before the ` — `, is copied as written, never escaped, because the
gate must match pytest's id exactly; only the reason is escaped. A review
remediation's own report lines go into the proposed block the same way, for
the next reviewer to rule on.
The reviewer cannot read the PR, so its prompt also lists each test the body's
**Behavior changes** section already declares, marked `declared:` and shown
with the reason its line gives. A base copy of one failing against the head
the way that reason explains is no finding. A declared test failing some other
way is an ordinary finding, which kew never drops: the held-out gate excuses
every failure of a declared test, so the reviewer is the one who catches it.
A `kind: "declaration"` finding loses the tests that section declares, by the
held-out gate's own rule (a line for a test covers each of its parametrized
cases; a line for one case covers that case only), and is dropped when none
are left; one tied to an acceptance criterion stands as written. A
NEEDS_CHANGES left with no finding, and none dropped as malformed, is judged
as a READY, under every READY rule, and the verdict comment names the tests
it excused (`excused as declared:`). kew records the declared set beside the
stored verdict, and every later read of that verdict uses it and the round's
recorded acceptance criteria, so the remediation, the stop rule and the next
delta review read the findings the round was judged on. When the round lacks
something a READY needs, such as acceptance evidence or a coverage list, kew
keeps it as the reviewer wrote it. If the tests it asks about are ones the host
flagged as edited or removed, the head is re-reviewed once, since the body
already carries their lines; a second round at the same head asking for no
test beyond those an earlier one asked for, the ones excused as declared
included, is remediated as an ordinary round. Any
other test, a frontend test file say, has no host-flagged record, so the
round is remediated as an ordinary round straight away.
The body stays under 60,000 characters, below GitHub's 65,536 limit: the prose
lines go first, whole, and a proposed line is never cut. When the proposed
lines alone do not fit, the publication is held for a person before anything
is pushed (`pr_body_too_long`). So is a title or body GitHub would still refuse,
such as a title holding a control character or a line break
(`pr_text_unsendable`): open the PR by hand from the preserved checkout, or
discard it and dispatch again.

Any change to an existing frontend test file (`*.test.ts`, `*.test.tsx`,
`scripts/ui-checks/`) needs a Behavior changes line naming its path, added
lines included: a lone `return;`, a comment around an `it(`, an `it.only` or
a `vi.mock` only adds lines and still changes what the file's cases check.
Deleting one, or renaming it, is a change too, declared by its path on the
base branch. A new frontend test file needs no line.

The interface between kew and the image — mounted checkout, prompt-file→stdin handoff, `"$@"` extra args, one JSON event per line on stdout, and the reviewer verdict channel — is fixed and documented in the [container entrypoint contract](sandbox-entrypoint-contract.md). Per-runner variation lives inside the image; kew's host-side contract does not change.

The security guard (`.claude/hooks/security-guard.sh`) is still active inside the container as **best-effort layer 2** defence-in-depth. It is known-bypassable in-process (#502); the mount boundary is the primary containment. See [Security](security.md#sandboxed-dispatch-credential-posture) and [Configuration](configuration.md#sandbox) for details.

On `[sandbox] runtime = "docker"` and `"podman"`, official Codex follows a
separate host path: Codex-native `workspace-write` governs the agent process,
command network is off, and kew records external container posture as disabled.
On `runtime = "openshell"` kew contains the dispatch like any other and attests
it `sandboxed`, with `workspace-write` nested inside that sandbox (see
[the Codex reference](runners/codex.md#posture-per-runtime)). Either way, on
exit 0 kew's host broker owns commit, push, PR creation, and the dispatch
report. Failure of that ceremony remains a failed dispatch. Usage is reported as
API-equivalent included-plan value, not a native bill.

## Prompt system

The dispatch prompt is built from a Jinja2 template with configurable sections:

- **Safety rules** -- branch protection, no secrets
- **Execution strategy** -- TDD workflow, subagent delegation for plans
- **Token budget** -- complexity-aware guidance (simple/medium/complex)
- **Verification** -- your CI and format commands
- **CodeRabbit** -- opt-in pre-PR review (2 rounds; `prompt.include_coderabbit`, off by default)
- **Acceptance criteria** -- the issue body's own acceptance section, extracted
  verbatim and named by the sha of the plan kew recorded in its ledger for this
  dispatch (see [`kew plan show`](cli/plan.md)). The statements are the issue
  author's words, so they render inside the same untrusted fence as the body.
  Absent when the body states no acceptance section; the sha and the claim that
  the ledger holds the plan are absent when that record could not be written
- **Pre-publish self-check** -- one bounded pass, after the verification
  commands: read the `origin/<base>...HEAD` diff against the SAME pinned
  reviewer rubric the adversarial review loads (its review focus areas only --
  the implementer produces no judgement and no verdict) and against the
  criterion ids above. It does not replace a review round. The run report
  records `self_check` for the dispatches that carried it
- **Dispatch report** -- structured PR summary template. If the agent
  finishes without posting one, kew posts a fail-soft fallback report
  (cost, tokens, duration, attempts) on the PR after dispatch, so the
  review surface stays consistent (#664). When kew posts the agent's own
  report, it is agent text kew acts on none of: a kew line naming the run
  opens the comment, every line of the report is quoted below it, and a kew
  line naming the run again marks where the agent's text ends, so no report
  line renders beside kew's own. A kew marker the agent pasted (`<!-- kew-…`)
  gets a zero-width space after its `<!--`, so no marker parser reads it;
  every `@` mention, HTML tag (kew's own `<redacted>` tokens included) and
  footnote gets one too, on every line, code blocks included, so the report
  pings nobody and nothing in it leaves the quote. The space is invisible, so
  code still reads as the agent wrote it. `kew review`
  does not hand that comment to a remediation agent as review

Override the template entirely with `prompt.template`, or append extra instructions with `prompt.append`.

An approved [prompt profile](prompts.md) adds to a role's base prompt: one block
headed `## Operator instructions — <role> profile v<N>`, rendered before the
safety rules and the publication section, which each say they take precedence
over it. A custom `prompt.template` is refused while a profile is active. Each
run records the profile version it ran under in `prompt_profile_version_id`.

The adversarial reviewer's base is the pinned `adversarial-reviewer` artifact
by default. A project that deploys `.claude/kew/adversarial-reviewer.md` runs
that copy instead, read out of the commit the base ref resolves to, so an
uncommitted edit in the operator's checkout governs no review
([Prompt profiles](prompts.md#replacing-a-base-prompt)).
A profile never replaces the base: an `adversarial_review` profile renders
before the rubric, followed by a host-owned sentence saying the review rules
below take precedence over it. The rubric, then the host-owned diff boundary,
runtime guidance and acceptance criteria render after that sentence, so every
review rule follows the profile. The verdict attests `prompt_artifact_sha`
(the base), `base_ref` and `base_commit` (the commit the base ref resolved
to; when the deployed copy loaded, the base was read out of that commit and
its artifact bytes hash to `prompt_artifact_sha`; when the packaged default
loaded, it is the commit kew looked in and found no deployed copy, or empty
when the base ref did not resolve), `effective_prompt_sha`
(the whole text the reviewer received) and `prompt_profile_version_id`, so a
verdict names the prompt it was judged under. Changing the packaged base takes a
merged PR to the artifact and its pin.
