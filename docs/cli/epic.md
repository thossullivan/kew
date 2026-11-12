# `kew epic` — Epic dependency-graph build, validate, and drive

```text
Usage: kew epic COMMAND [ARGS]...
```

`kew epic` turns a set of work items into a dependency graph, then
**drives** that graph: dispatching ready nodes in dependency order and
mechanically merging PRs a human has already approved.

Where the epic lives depends on the configured tracker
(see [`[tracker]` configuration](../configuration.md#tracker)):

| Tracker | Epic membership | Dependency edges | Driver support |
|---------|-----------------|------------------|----------------|
| `github` (default) | the **sub-issues** of the epic's parent issue, else issues carrying the `epic:<name>` label | the union of GitHub **blocked-by** relations and `<!-- kew:depends N M -->` markers during the one-release overlap | `advance`, `run`, and `retry` |
| `linear` | admitted issues in the Linear **project** named `<name>` | Linear's native **blocks / blocked-by** relations | `advance`, `run`, and `retry` |

## GitHub: two keys, native first

GitHub exposes the same primitives Linear always had, so kew reads those first.
The existing label and marker convention remains the fallback. `kew epic plan`
writes **both** keys, so an epic kew planned is readable either way.

| Fact | Native key (preferred) | Fallback key |
|------|------------------------|--------------|
| Membership | the epic's parent issue and its sub-issues | issues carrying the `epic:<name>` label |
| Dependency edges | each node's `blocked_by` relations | `<!-- kew:depends N M -->` in the node's body |

Which key is used is decided per read, and never silently:

- **The capability probe** runs once per process and stays in memory.
  It queries `blockedBy(first: 1)` and `subIssues(first: 1)` on the epic's
  first issue. A structured GraphQL field error selects the fallback adapter.
  GitHub's exact missing-field message also selects the fallback adapter.
  Transport, authentication, rate-limit, and other failures stop the read.
  The visible `Linkage:` line names the actual membership key and fallback reason.
- **Membership** resolves to a parent issue when the epic ref is an issue
  number. A named epic accepts a parent from a labelled node only when the
  parent is local and carries `<!-- kew:epic-container <name> -->` for this
  epic. An unrelated local parent is ignored. A foreign parent stops the read
  and names both the labelled issue and the foreign parent. A labelled,
  top-level issue with sub-issues still counts without the marker. Its label
  remains the explicit opt-in for a hand-built container. Two accepted
  candidates are ambiguous and stop the read. Zero accepted candidates means
  the label query is used.
  Cross-repository sub-issues are excluded from membership.
  The driver channel reports the skipped count once per read.
- **Edges** use the union of `blocked_by` and `kew:depends` during the
  one-release overlap. This union preserves every marker blocker after a
  partial native write. Duplicate edges are removed. A relation pointing at
  an issue outside the epic is kept. The frontier fails closed on a dependency
  it cannot resolve. A cross-repository relation stays qualified. Status and
  validation output therefore name its repository and issue number. An edge
  exists while either key names it. Remove both the `blocked_by` relation and
  the `kew:depends` marker to remove a dependency during this overlap.
- One GraphQL query per epic per tick returns number, state, state reason,
  labels, `blocked_by` and the closing PR references for **every** node, and
  the driver's facts reader is primed from it — so a tick no longer runs a
  `gh issue view` per node.

GitHub's UI can re-parent a node without editing issue prose. During the
overlap, a dependency change also requires the matching issue-body edit.

**The container issue is not a node.** `kew epic plan` writes
`<!-- kew:epic-container <name> -->` in the container body. The convention
adapter excludes every issue with this marker, independent of the queried epic
name. `kew epic plan` also leaves `epic:<name>` off the container. A later
label edit therefore cannot make the container dispatchable. This marker is
reserved kew metadata. Do not copy it into work issue bodies.

**Both keys, for one release.** `epic plan` keeps writing the `epic:<name>`
label and the `<!-- kew:depends -->` marker alongside the sub-issue and
`blocked_by` relations. The overlap is deliberate and temporary: it is what
lets a repository whose API has no relations — or one planned by an older kew —
keep reading the same epic. The native writes run after issue creation. Each
write failure is printed. Edge reads use the union above, so a partial
`blocked_by` write retains marker blockers. Membership has no equivalent union.
The mandatory read-back gate requires every existing and newly created node. A
missing node exits with code 1 and names the required repair. The native
writes have shipped; the marker has not been dropped yet. The release that
drops it ends the overlap, removes the marker half from edge reads, and stops
writing new markers.

Release and publication procedures are maintained outside this repository and are not published.

On Linear, native `blocks`/`blocked-by` relations are the DAG, but a project
is only the candidate population. An epic item is dispatchable only when it is
in an unstarted/Todo state and carries both the `kew` membership label and the
`kew:ready` human-arm label. Backlog and Triage items, unarmed Todo items,
and items with missing admission facts remain queued; they do not enter the
frontier. Clearing `kew:ready` withdraws admission on the next tick.

Linear children claim items after the pending row and reservation binding commit.
Fetching an item never claims it.
After a claim, a refusal retains the claim and never resets Linear automatically.
Linear's `issueUpdate` has no version precondition, so verification reads cannot make an automatic reset safe.
The item stays assigned in its claim state. A replacement dispatch re-claims it.
For manual recovery, unassign the item in Linear and move it back to its unstarted state.
No kew command releases a retained claim: this manual recovery is the only way back.
`kew epic status` shows a `retained claim` line for each affected epic item.
Each line shows its latest recorded reason and the current recovery instructions.
Historical events use current instructions, including events recorded before this change.
`linear_claim_completed` records the claim snapshot.
`linear_claim_retained` records the refusal reason, reservation identity, claim snapshot's `updatedAt`, and recovery instructions.
The operator warning names the item and gives the same recovery instructions.
The `on_refuse` callback finishes before the terminal refusal row, event, or reservation release.
Callback failures remain visible and preserve the refusal exit code.

For example, in a Linear project:

| Admission state | Linear item | Labels | Driver behavior |
|-----------------|-------------|--------|-----------------|
| Queued | `SFD-101` (unstarted/Todo) | `kew` only | Remains queued; it is not dispatched |
| Armed | `SFD-102` (unstarted/Todo) | `kew` + `kew:ready` | Enters the frontier when its dependencies are satisfied |

The delegation rail is separate: an explicitly delegated, non-epic Linear item
(normally assigned to the configured kew member) may be claimed by
`kew delegate watch`. Project membership alone never implies delegation, and
an item admitted to the epic rail is excluded from delegation. The two rails
share the item-id reservation and fleet cap, so one item cannot be claimed by
both.

Auth is environment-only (`LINEAR_API_KEY`); the key is never put in
`kew.toml`, issue content, or logs. With `tracker.type = "linear"`, kew reads
membership, admission, and native dependency state from Linear, reads PR,
review, and CI facts from GitHub, and closes the Linear issue after its GitHub
PR is observed merged. OAuth/app identity work from #837 is not required for
this single, firm-operated member/API-key contract.

The Linear project query includes **every** issue in the project, including
archived issues, so an archived completed dependency remains available to
satisfy a native edge. A Linear issue with children is an organizer/container,
not a work node: kew keeps its bounded hierarchy metadata for diagnostics but
never dispatches it and never turns parent/child hierarchy into a dependency
edge. Native relations between actual work items remain intact.

All subcommands take the epic name as their first argument: the part after
`epic:` on GitHub (an epic labeled `epic:auth-rework` is `kew epic <cmd>
auth-rework`), or the project name on Linear.

## `kew epic plan` — turn a plan into a drivable epic

```text
Usage: kew epic plan [OPTIONS] NAME

Options:
  --from PATH           Implementation plan file to turn into issues
  --from-manifest PATH  Create from an existing manifest (skips the planning agent)
  --dry-run             Validate and preview; create nothing
  --amend               Append new nodes to an existing epic instead of refusing it
```

```bash
kew epic plan checkout-flow --from docs/design/2026-07-02-checkout.md
kew epic plan checkout-flow --from docs/design/2026-07-02-checkout.md --dry-run
kew epic plan checkout-flow --from-manifest .kew-logs/epic-plan-checkout-flow-20260702-101500.json
kew epic plan checkout-flow --from docs/design/2026-07-02-checkout-v2.md --amend
```

A governed dispatch of the planning agent — the agent provides the judgment,
kew provides the guardrails. The agent reads the plan, browses the codebase,
and batches tasks into well-sized draft issues, written to a JSON manifest —
not to GitHub. Then kew takes over:

The planning agent receives a standalone `--no-hardlinks` clone. It uses the
same containment policy as ordinary dispatch. Host execution requires
`[sandbox] dangerously_skip_containment = true`.

- the draft DAG is validated (cycles, dangling deps, plan reference in every
  body) **before** anything is created
- every body must carry a `## Acceptance Criteria` checklist of 2 to 6
  item-specific, checkable criteria — a statement of an observable outcome plus,
  where possible, the command that proves it. A draft with no criteria, fewer
  than 2, more than 6, any generic one ("All new and existing tests pass",
  "CI gate passes", "All tasks N-M implemented"), or any left as the prompt's
  `[...]` skeleton is rejected and nothing is created; `--dry-run` prints each
  node's criteria so they can be read before the issues exist
- each criterion line ends in a kind marker: `(kind: test)` when an automated
  test can decide it, `(kind: behavior)` when a reviewer must judge it,
  `(kind: static)` for a static check. kew records that kind with the
  criterion; a line without a marker is recorded as `behavior`
- kew creates the issues itself, in dependency order, applying `epic:<name>`
  plus the priority/complexity/type labels — `ready` is never applied (the
  manifest has no free-form label field)
- `<!-- kew:depends -->` markers are written by kew from the manifest's
  `depends_on`, using real issue numbers
- GitHub's own linkage is written alongside them: a container issue for the
  epic (typed `Epic` where the organization defines issue types), each node
  added as one of its **sub-issues**, and each `depends_on` recorded as a
  `blocked_by` relation
- a mandatory validate gate requires every expected issue number after three
  reads, then validates the real graph and prints the Mermaid render
- the epic is registered in the local run store the moment its issues are
  created, so `kew epic run --all` sees it before its first tick (a
  freshly-planned epic used to be invisible to `--all` until driven by hand)

Every run saves its manifest under `logging.dir`; `--from-manifest` re-uses
one without re-running the agent (cheap retry after `--dry-run`; after a
mid-creation failure add `--amend`, as described below). Re-planning an epic
that already has issues (open or closed) is refused by default — bare
`epic plan` plans a fresh epic only.
Requires the GitHub tracker; unlike the driver commands, `epic plan` does not
create Linear projects or issues.

**Amending a running epic.** `--amend` appends new nodes to an existing epic
through the same governed pipeline. New `local_id`s must not collide with the
epic's existing node ids, the `<=10 target_files` dispatchability gate still
applies, and `depends_on` may reference either other new nodes or the numbers
of existing epic issues — kew writes `<!-- kew:depends -->` markers for both,
adds the new nodes as sub-issues of the epic's container issue and records
their `blocked_by` relations, then re-validates the merged graph after
creation. Amending an epic that has no accepted container creates a marked
container. Kew attempts to add every existing node before adding the new nodes.
The read-back gate exits with code 1 when a successful read lacks any expected node. An
unrelated local parent is not reused. A foreign parent stops the read and must
be fixed before amendment.

**Re-applying a manifest is safe.** Every work issue `epic plan` creates
carries `<!-- kew:plan-node <epic> <local_id> <digest> -->`, where the digest
fingerprints the draft's title and body. Before it creates anything, `--amend`
reads those markers from every `epic:<name>` issue (open or closed) that the
`gh` login it runs as wrote. A marker in an issue anyone else wrote is never
reused. When such an issue carries this manifest's digest for a `local_id`
kew has made no issue of its own for, kew warns, naming the issue and both
logins: after a change of `gh` login since the earlier run, the node is
created again. A `local_id` whose oldest issue kew made is open (or closed as
completed) and has the same digest reuses that issue: no second issue is
created, its number serves the new nodes'
`depends_on`, and its membership and `blocked_by` writes are retried if the
epic does not list it yet. After a creation failure part-way through, re-run
the same command with `--amend` (kew prints it) and only the missing nodes are
created. A changed title or body is refused (see below); changes to a
created node's `depends_on`, priority, complexity, type or `target_files` are
not applied to its existing issue.

`--amend` refuses before it creates anything when:

- a `local_id` already names an issue made from a different title or body (a
  later manifest picked a used id: choose a fresh one);
- two live issues kew made (open, or closed as completed) claim the same
  `local_id`: kew names the oldest to keep and the others to close as not
  planned, then re-run. A later copy closed for any other reason is not
  counted;
- the oldest issue kew made for a `local_id` was closed for a reason other
  than completed (such as not planned), because dependents made with it may
  name it and would count it as done: reopen it, or pick a fresh id;
- the label lists 1000 or more issues, the most one read returns;
- the label cannot be read, or it holds plan-node markers and the `gh` login
  cannot be read.

A bare re-run (without `--amend`) is refused while the label holds issues an
earlier `epic plan` made, even when the epic lists none of them yet, and
while it holds plan-node markers in issues another login wrote (kew names
each issue and its author). A bare plan also refuses when the epic label cannot be read, lists 1000 or more
issues, or holds plan-node markers while the `gh` login cannot be read. A draft
body that carries the plan-node marker itself is refused, as one carrying a
`kew:depends` marker is, and so is an empty `local_id`. In a
`kew:depends` marker a zero-padded number such as `#02` or `02` reads as
member `2`.

Planning uses the shared [exit-code scheme](exit-codes.md).

| Code | Planning outcome |
| --- | --- |
| `0` | Successful creation or dry-run validation |
| `1` | Executed agent failure; manifest validation failure; export failure after execution; round-tripped graph validation failure |
| `2` | Missing prerequisite; checkout failure; containment refusal; runner preflight refusal; other pre-launch failure; post-creation GitHub query failure |

Planning retains launch evidence across re-split retries.
A retry refusal after earlier execution returns `1`.

**Commit the plan before driving.** Every issue body points dispatch agents
at the plan file, and dispatch clones are cut from `origin/<base>`. If
the plan isn't pushed there, the agents can't read it (`epic plan` warns
when it detects this). Review the created issues, commit/push the plan, then
start the driver: `kew epic run <name>`.

## `kew epic graph` and `kew epic validate`

```text
Usage: kew epic graph [OPTIONS] NAME

Options:
  --format TEXT       mermaid or dot (default: mermaid)
  --epic-prefix TEXT  Label prefix identifying epic membership (default: "epic:")
```

```text
Usage: kew epic validate [OPTIONS] NAME

Options:
  --epic-prefix TEXT  Label prefix identifying epic membership (default: "epic:")
```

- `kew epic graph <name>` renders the dependency DAG as a Mermaid diagram
  (default) or Graphviz DOT, color-coded by each node's provider-derived
  status (`done`/`ready`/`in_progress`/`in_review`/`blocked`/`failed`/`todo`).
- `kew epic validate <name>` checks the graph for self-edges, dangling
  dependencies (an edge to an issue outside the epic that doesn't exist),
  cycles, and orphans. On success it prints the count of the epic's own
  member nodes and the topological level count, then lists on a separate line
  any external nodes (resolvable dependencies outside the epic). Exits non-zero if the graph is not a valid DAG. An issue whose body carries
  more than one `kew:depends` comment gets a warning naming it: the comments'
  dependencies are combined, so the graph is still right and the exit code is
  unchanged, but they should be merged into one. A comment inside inline code
  or a fenced block is documentation, not a dependency, and is ignored. A
  bare `#N` token in a marker means issue `N`, the same as `N`;
  `owner/repo#N` and native ids such as `ENG-123` are read unchanged.

Both commands are read-only — they never dispatch, merge, or write to the
run store.

## `kew epic status`

```text
Usage: kew epic status [OPTIONS] NAME

Options:
  --epic-prefix TEXT  Label prefix identifying epic membership (default: "epic:")
  --json              Print one JSON document instead of text.
```

On the GitHub tracker, every graph read prints its actual membership key.
For example, it prints `Linkage: sub-issues of #123`.
The fallback prints `Linkage: label epic:<name>`; when the probe found the
native fields unavailable it appends the reason. A label epic on a repository
that has the fields (no parent issue yet) prints the label line alone.
The probe queries `blockedBy` and `subIssues` on the epic's first issue.
Structured field errors select the label and marker adapter for that process.
GitHub's exact missing-field error has the same result.
Transport, authentication, and rate-limit failures stop the read.

Snapshot of the driver's view of the epic: per-status node counts,
the dispatch **frontier** (nodes ready to dispatch next), in-flight nodes,
**held** nodes (approved but waiting on CI), failed/cancelled nodes, **stuck**
nodes with their blast radius, a done/remaining count, and the
**signal** (`complete`/`progressing`/`awaiting_merges`/`stalled` — see
[The terminal signal](#the-terminal-signal)). This is the same
overlay/frontier logic `kew epic advance` uses, but `status` never dispatches
or merges. It is not write-free: the facts it reads reconcile an
`awaiting_publication` run whose PR is observably open or merged, so a status
read can terminal that run and emit a `publication_reconciled` audit event.

On Linear, any API failure ends `status` **(exit 2)** with the same
classified one-line diagnostic as `kew epic advance` (`linear-unavailable`,
`linear-auth` or `linear-error`), never a traceback.

When this epic's **per-epic** driver lock is currently held, `status` also
prints a `Poller: epic=<name> pid=<pid> held-since=<ts> holds epic <name>'s
driver lock` line so you can see who is driving it right now, from which
process, and since when (and why a concurrent `run` is locked out). If the
recorded holder's process is no longer alive the line reads `has a STALE hold
on …`. A stale lock — a dead-PID holder, or one whose heartbeat has not been
refreshed within `HEARTBEAT_STALE_SECONDS` (300s) — is reclaimed
**automatically** by the next driver that tries to acquire it (the steal is
logged), so a TaskStop'd driver's orphaned lock never demotes later drivers to
silent observers. `--takeover` (see below) is only needed for a *legacy* lock
that predates heartbeats and whose staleness can't be proven. Because locks are
keyed per epic, a different epic being driven never shows up here.

The lock records its holder's kernel start time, so kew can tell the process
that took the lock from a recycled pid. A heartbeat gone stale on that very
process — one stopped with Ctrl-Z or `SIGSTOP`, suspended, or hung — does not
release the lock: kew never takes it from a live process. That epic is
**stalled**: every tick it locks out reads `stalled`, `status` prints its
`Signal` as `stalled` with a warning naming the pid, `kew drive status` lists
it, and a running `kew drive` raises one attention event for it. Kill that pid
(`kill -9 <pid>`: a stopped process acts on a plain `kill` only once it is
continued) and the next tick reclaims the lock, or `kill -CONT <pid>` to resume
a driver that was only stopped. A lock written by an older kew, without the
start time, keeps the heartbeat rule above.

`status` also prints a `PR linkage:` line. Each PR entry includes its link
source: `ledger`, `closing-ref`, or `branch`. See
[How a node is linked to its PR](#how-a-node-is-linked-to-its-pr).
Kew observes each dispatch PR's GitHub closing reference, such as `Closes #42`.
Status warns when that separate fact is missing and names the affected PR.

For each open item whose last head went to review without the
[pre-review gate](../configuration.md) checking it (a refused launch, a
timeout, a tool the check image lacks, a red base), `status` prints a
`pre-review gate skipped: #N at <head>: <reason>` line. A node that is done or
cancelled prints none: its PR merged or closed. Each tick prints the skip as
`pre-review gate skipped: #N — <reason>` for every node in review whose
current head the gate skipped. The tick reads the skip from the run store, so
it prints one recorded by a detached `kew epic _review-node` child too, not only
one from a review this tick ran. The PR's verdict comment for that head says so
too. The reason is redacted before it is kept: credentials, secret
assignments, URL userinfo and queries, the directories of an absolute path,
IP addresses, GCP resource paths and `*.internal` hosts, `gs://` buckets,
`user@host` and email addresses, the `[hosts.gcp]` project, zone and instance
names and this machine's host name are removed. A gate that ran and passed prints nothing.

```bash
kew epic status auth-rework
kew epic status auth-rework --json
```

### `--json`

`--json` prints **one JSON document** on stdout, read from the same snapshot
as the text report. The linkage line goes to stderr instead, so stdout parses
whole. A failure keeps its exit code and prints no document (see
[Exit codes](exit-codes.md)).

```json
{
  "schema": 1,
  "epic": "auth-rework",
  "signal": "stalled",
  "counts": {"done": 1, "failed": 1, "todo": 1},
  "nodes": [
    {"id": "12", "title": "Add token store", "status": "done",
     "dependencies": [], "external": false, "url": "…", "pr_url": "…",
     "stuck": null, "waiting": null},
    {"id": "13", "title": "Rotate keys", "status": "failed",
     "dependencies": ["12"], "external": false, "url": "…", "pr_url": null,
     "stuck": "failed-terminal", "waiting": null}
  ],
  "frontier": [], "in_flight": [], "held": [], "failed": ["13"],
  "cancelled": [], "done": ["12"], "remaining": 2,
  "stuck": [{"id": "13", "reason": "failed-terminal", "blast_radius": ["14"]}],
  "waiting": [],
  "pr_links": [{"id": "12", "pr_number": 40, "source": "ledger", "has_closing_ref": true}],
  "retained_claims": [],
  "token_warnings": [{"runner": "claude-code",
    "reason": "agent token CLAUDE_CODE_OAUTH_TOKEN (fingerprint 1a2b3c4d5e6f) for claude-code is near its seven_day limit (92% used), resets 2026-10-30T07:00:00Z",
    "identity": "1a2b3c4d5e6f:seven_day:2026-10-30T07:00:00Z"}],
  "lock_holder": null,
  "driver": null
}
```

`nodes` lists **every** node in the graph, external dependencies included,
with the ids it depends on. A node's `status` is the driver's overlay status
(the same names as `counts`); for a graph that failed validation it is the
tracker's own status. `stuck` and `waiting` carry the node's reason when it
has one. `token_warnings` lists each agent token usage-limit warning that still
applies, one per runner and limit (see
[`kew quota`](quota.md#usage-limit-warnings)), and is empty when there is none.
`lock_holder` names the driver lock's holder (`alive: false` for a
stale hold; `stalled: true` and the `action` that clears it for a live holder
that stopped beating its heartbeat, which also makes `signal` `stalled`), and
`driver` is the driver-liveness reading. `schema` changes
only on a breaking change.

## `kew epic heartbeat` — say a loop is waiting on purpose

```text
Usage: kew epic heartbeat [OPTIONS] NAME

Options:
  --waiting TEXT  Why the loop is not ticking right now, e.g. `slot`.  [required]
```

A loop parked in a shared slot gate does not tick, and to kew a loop that does
not tick looks the same as a dead one. Call `kew epic heartbeat` from the loop
script on each wait. While the last call is inside **twice** the driver's
cadence, the console's Health page reads that driver `waiting` (quiet, not red)
and the header does not count it as down. Past that window it reads `gone`
again, so a loop that died while waiting is still caught. A driver that is
actually ticking reads `live` whatever the loop last said. A driver with no
recorded cadence cannot be thresholded, so a heartbeat does not change it.

The command writes one ledger row per epic, overwritten on each call. It does
not dispatch anything and does not take the driver lock. An empty reason is
refused and nothing is written.

```bash
# in the operator's loop script, each time the slot gate is busy
kew epic heartbeat host-contract --waiting slot
```

## `kew epic watch` — one operator-attention event stream

```text
Usage: kew epic watch [OPTIONS] NAME

Options:
  --json              Emit one JSON object per event (JSON Lines)
  --follow, -f        Keep polling, emitting each new state once, until --timeout or Ctrl-C
  --interval FLOAT    Seconds between polls under --follow (default: 30)
  --timeout FLOAT     Give up after this many seconds under --follow (0 = never)
  --replay            Ignore what earlier --follow watches emitted: every open state once
  --epic-prefix TEXT  Label prefix identifying epic membership (default: "epic:")
```

Every state of an epic that needs a **person** used to surface somewhere
different: a READY review verdict in the ledger, a stop-rule acceptance only as
an `awaiting merge:` tick line, a spent remediation pool only as a
`Review skipped` line, a sandbox refusal only as a transient `Sandbox:` line,
and a held node only in `kew epic status`. `watch` emits **one structured event
per such state**, at most one per item, each naming the item and the action.

| `kind` | What happened | The action |
| --- | --- | --- |
| `merge_ready` | A PR is open in review awaiting a human merge (or a head-check gate) | merge to resume |
| `budget_exhausted` | A budget or remediation-pool hold — `budget_preflight` or `review_budget` — or a daily or weekly cap hold, `budget:` | `budget_preflight`, `review_budget`: raise the cap its reason names, then un-hold. `budget:`: nothing to un-hold; it clears itself at the next UTC midnight, once the week's oldest spend ages out, or when you raise the cap it names |
| `sandbox_refused` | A containment or review-sandbox refusal — `sandbox_containment` or `review_sandbox_refused` | run the build or mapping its reason names, then un-hold |
| `publication_held` | A completed branch kew could not publish | restore host access or open the preserved branch PR |
| `held` | Any other hold the driver honors, with its reason verbatim — including `review_launch_failed`, a review whose reviewer never started, `review_multiple_results` and `review_failed_no_verdict`, a reviewer that ran and left no verdict (the reason says why), and `review_rate_limit_exhausted`, a reviewer usage limit that has not reset in six hours (the reason names the agent token the last limited round ran under — env var and fingerprint, never the value — or `unknown credential`), and `review_gate_paths_unread`, a residual-only round whose head's merge gate paths could not be read on five ticks in a row (the reason names the head and the failed read) | fix the cause, then [`kew epic unhold`](#kew-epic-unhold-release-a-driver-hold) |
| `host_restart_repeated` | The host restarted under the node's run a second time, so the node failed instead of being re-admitted again | find out why the host keeps going down, then `kew epic retry` the node |
| `usage_limit_warning` | An agent token is near a provider usage limit; `item` is the runner and the limit (`claude-code:seven_day`; the runner alone for a limit kew does not name), and the reason names the token's env var, its fingerprint, the limit and its reset. Nothing is held yet. One event per token, limit and window — see [`kew quota`](quota.md#usage-limit-warnings) | replace the token before the limit refuses it, or expect dispatches and reviews to wait for the reset |
| `epic_complete` | Every node is DONE; `item` is the epic itself | nothing is left to drive |

Each JSON object carries `kind`, `epic`, `item`, `reason`, `action`,
`blast_radius` and `identity`, plus `observations_stale_since` on a snapshot
watch — see [A snapshot watch reads the driver's last
tick](#a-snapshot-watch-reads-the-drivers-last-tick). The `kind` vocabulary is
additive — a reader must tolerate one it does not know, and likewise a key.

`identity` is what **changes when the item's state changes**: the PR head and
review round (`head:SHA round:N`) for a review state, the run kew could not
publish (`run:RUN-ID`) for a publication one, and empty for a state carrying
neither. It is there because item, kind and reason are not enough to tell two
states apart — one node went READY at one head, failed its merge verify, took
an operator fix and went READY again at another, emitting the same item, kind
and reason text both times. De-duplicate on `identity` together with them,
never on them alone.

A `ci_pending` hold whose stall clock is still running, a node blocked on an
upstream node, and a merged-but-unclosed issue are **waits, not people** — they
clear without an operator, so they emit nothing. Failed and cancelled nodes are
not in the stream either: they are already named by `kew epic status`, by the
`stalled` signal, and by `kew epic run`'s non-zero exit.

An expiring stall clock never ends a wait. Past `[epic] stall_days` the driver
re-reports an untouched in-review PR as `review-stalled` and an aged
`ci_pending` gate as `ci-stalled`, and moves both (with any other aged hold)
from its waiting list to its stuck list — so `watch` reads that list too. The
PR keeps emitting `merge_ready`, the aged `ci_pending` gate emits `held`, and
every other aged hold keeps its own kind and action, each with `check the PR`
or the hold's own hint. The stream goes quiet when nobody is needed any more,
never because a wait got older.

Reason strings are flattened to one short printable line before they are
emitted, because an `admission_refused` reason carries the tail of a detached
child's stderr.

Under `--follow` the watch **keeps running after an event**: it polls every
`--interval` seconds until its `--timeout` or your Ctrl-C, and emits each **open**
state **once**. A state whose `identity` changes — a new head, a new
review round, a new unpublished run — is a new state and is emitted again; an
unchanged one stays quiet. A state that **clears and later comes back** is also
emitted again, because it is a second thing for a person and many kinds (a
sandbox refusal after a `kew epic unhold`, for instance) carry no `identity` to
tell the two apart. Without `--follow` it takes one snapshot and exits.

That memory **survives a restart**. A `--follow` watch records the state it last
emitted per item — its `kind`, `reason` and `identity` — in a file named after
that epic's driver lock with `.watch.json` appended
(`epic-driver-<epic>.lock.watch.json` under `[logging] dir`, or beside `[epic]
lock_path` when that is set). A watch restarted in a
supervisor loop therefore does not repeat what the last one already said: it
prints on stderr how many open states it is holding its peace about, and emits
only what has changed since. A memory file this watch cannot read — corrupt,
belonging to another epic, written by another kew's record version, or holding a
record that does not read back — is read as **no** memory for whatever it cannot
read: it names on stderr which of those it was and replays, rather than
swallowing events.

`--replay` ignores that memory for one run and emits every open state once, as a
watch with no memory would; it still records what it said, so the next plain
`--follow` is quiet again. A **snapshot** watch (no `--follow`) neither reads
nor writes the memory: it is a probe meant to answer on every run.

Two watches on the same epic share one memory file — each poll replaces it
whole, so the later writer wins and a restart can replay the other's last poll.
Run one watch per epic.

Exit codes follow the [standard scheme](exit-codes.md):

- At least one event was emitted — there is something for you. Exits **1**.
- Nobody was waited on. Exits **0**.
- The epic could not be read. Exits **2**.

A `--follow` watch that reaches its `--timeout` with no event says so and exits
**0** — it ran and found nothing, which is not the same as not running. Ctrl-C
ends it the same way, reporting what it found rather than a traceback.

### A snapshot watch reads the driver's last tick

A watch without `--follow` is meant to be **polled**, so it is priced like it:
it reads the observations the epic driver recorded on its last tick out of the
ledger and composes the events from those. No host call per node, no repository
detection, no tool preflight, and a **read-only** ledger open — so a tick that
is mid-drive, holding both the driver lock and a ledger write transaction,
delays it by nothing. It used to cost about 90 seconds of waiting on a 23-node
epic, which timed out pollers that allowed it 240.

The price of that is **staleness**, which every event names rather than hides:
`observations_stale_since` carries the instant of the driver's **last finished
tick** — the last time anything looked at the epic, not the tick the state
first appeared on, which is older and freezes while a held epic sits — and
human mode prints `observations stale since <t>` once. A poller ages them
instead of mistaking the driver's last reading for this one. Anything the
driver has not ticked since — a PR merged a minute ago, a hold cleared by hand
— shows up on the epic's next tick, not before. Two more consequences worth
knowing: a node stopped at a head-check gate reads as a plain `in_review`
awaiting merge rather than naming the gate, and an epic **no tick has journaled
yet** falls back to the full live read below, because nothing recorded is not
the same as nothing waiting. A ledger an older kew wrote takes that fallback
too: the read-only open can neither migrate nor adopt it, so a journal it has
no table for — or a ledger kew has not registered yet — is read live, migrated
and registered on the way. So does an epic whose last tick reports an
**invalid graph** (a cycle, a dangling or a self edge): that tick froze before
it observed anything, so its snapshot records no per-node state to report, and
the live read is also the one that can answer — it rebuilds the graph, so it
names the holds and merge-ready PRs the instant the cycle is broken. And so
does a snapshot carrying a **node status this kew has no name for**, which a
ledger a newer kew wrote can: the snapshot is read whole or not at all, because
dropping the one node nobody can read is how an epic with a node waiting on a
person reports that nobody is waiting.

Under `--follow`, and on that fallback, `watch` reads live and is read-only in
exactly the sense `kew epic status` is: it never dispatches and never merges,
but the facts readers it shares with `status` can reconcile an
`awaiting_publication` run whose PR is observably open or merged. A snapshot
watch off the journal writes nothing at all.

```bash
kew epic watch auth-rework --json
kew epic watch auth-rework --follow --interval 60 --json  # until Ctrl-C
kew epic watch auth-rework --follow --replay --json       # re-emit every open state
```

## `kew epic advance` — run one driver tick

```text
Usage: kew epic advance [OPTIONS] NAME

Options:
  --max-parallel INTEGER   Override config.epic.max_parallel for this tick.
  --takeover               Also reclaim a legacy no-heartbeat lock (a provably-stale lock is reclaimed automatically).
  --epic-prefix TEXT       Label prefix identifying epic membership (default: "epic:")
```

Runs a single **tick** of the epic driver:

1. Acquires this epic's **per-epic** driver lock (see below); a concurrent
   `advance`/`run` on the SAME epic that can't acquire it exits cleanly, naming
   the live holder (`epic=<name> pid=<pid> held-since=<ts>`) and its heartbeat
   age, plus the manual workaround: `its agents keep running; tick it manually:
   kew epic advance <name>`. A different epic driving concurrently never
   contends for this lock. A **stale** holder — a dead PID, or a heartbeat not
   refreshed within `HEARTBEAT_STALE_SECONDS` (300s, a frozen/hung driver; a
   live holder refreshes it every 60s, however long its tick runs) — is
   reclaimed **automatically**: the acquiring driver steals the lock and logs
   the steal, so no live epic is left permanently observing a dead driver's
   lock. A live holder with a fresh heartbeat is never taken over. `--takeover`
   is only for a *legacy* lock written before heartbeats existed, whose
   staleness cannot be proven (e.g. a recycled PID); it still never stomps a
   live, freshly-ticking driver.
2. Reconciles reservations, Linear merged items, and no-change effects.
3. Reads provider facts and computes each node's status **overlay**.
4. For GitHub, reconciles linked PRs. This writes branch-discovered PR URLs to
   implementation runs. It also cleans stale closed-PR review state. An open
   dispatch PR gets its missing `Closes #N` marker restored. The marker
   `<!-- kew:no-autoclose -->` disables this repair.
5. Resolves live-reviewer races and runs the governed review stage. Each node
   due a review or a remediation gets one this tick. Up to `max_parallel` of
   them run at once, fewer when the `[parallel] max_workers` fleet cap or this
   epic's fair share of it has fewer free slots. The tick waits for all of them
   before the merge pass. When several run at once, each console line carries
   its node's item id, for example `[dispatch] [42] Dispatching review for PR #7...`. With
   `max_parallel = 1` they run one after another.
6. **Merge pass**: for every node with an open or merged PR, merges or closes
   it if — and only
   if — the merge gate below is satisfied; otherwise leaves it open, held,
   or marks it stuck.
7. **Dispatch pass**: computes the ready frontier (deps satisfied, not
   reserved/in-flight/FAILED/CANCELLED/BLOCKED/IN_REVIEW/HELD or
   `awaiting_publication`) and dispatches
   up to `max_parallel - in_flight` of them, through the normal
   budget/cost-approval/fleet-stop gates.
8. Prints a tick summary and releases the lock. Every ready node the tick
   withheld prints a `waiting:` line naming the limiter that withheld it — the
   per-epic budget, the `[parallel] max_workers` fleet cap, this epic's fair
   share of it, or human-review backpressure — on a progressing tick, not only
   once the epic stalls.

If the epic's tracker query resolves to **zero nodes** — a typo'd epic name,
or running from a cwd wired to the wrong tracker — `advance` **errors (exit 2)**
naming the empty query (the GitHub label or the Linear project) instead of
reporting `COMPLETE done=0 remaining=0`. A driver never declares success for
work that does not exist; a legitimately-finished epic still reports `complete`
via its non-empty DONE set.

If Linear is the tracker, every Linear API failure ends `advance` with a
one-line classified diagnostic instead of a traceback, and no admission or
write-back is inferred from a partial or unavailable response.

- **Retried, then `linear-unavailable`:** for a read, a DNS or connect
  failure, a timeout, a reset connection, any HTTP 5xx (including a gateway or
  Cloudflare 52x page) and a 429 that is not Linear's JSON rate-limit error; for
  a write, a DNS or connect failure before anything is sent, or a response lost
  in transit after sending. The client retries with bounded exponential
  backoff (~3 attempts); an outage that outlasts that budget **errors (exit
  2)** as `linear-unavailable`, a transient infrastructure blip to re-run once
  Linear recovers, not a stuck epic.
- **Not retried, `linear-outcome-unknown`:** a 5xx or edge-429 reply to a
  Linear write. The write may have applied behind the error, and sending it
  again could duplicate it. A claim made by the dispatch child records the
  unknown outcome, then kew verifies the claim against Linear and retains it
  instead of launching an agent. A write made by the
  tick itself, such as a close, **errors (exit 2)** saying the outcome is
  unknown: check the item's state, assignee and comments in Linear, then
  re-run; the next tick re-reads the item first.
- **Retried, then `linear-error`:** Linear's own JSON rate-limit error that does
  not clear within the same budget **errors (exit 2)**.
- **Fail fast, no retry:** a TLS failure, an authentication failure (401/403),
  any other 4xx and a Linear GraphQL error **error (exit 2)** as `linear-auth`
  (fix the key) or `linear-error` (fix the cause). Retrying a client-side error
  only wastes time.

A completed host-publication run whose GitHub publication fails is surfaced as
`awaiting_publication`. Its committed checkout is preserved and the node is not
redispatched. Restore GitHub access or open a PR from the preserved branch;
Linear-backed drivers reconcile an existing branch PR on their next tick.
See also [Troubleshooting deferred publication](../how-it-works.md#troubleshooting-deferred-publication).

`--max-parallel 0` admits nothing new for the tick — no fresh dispatch and no
automatic continuation claim — while running work finishes; `kew epic run`
keeps 0 across its per-tick config reloads. Ready nodes wait as
`admission-held: admission closed by --max-parallel 0 (an operator stop)`,
not as a `kew drive` hold.

```bash
kew epic advance auth-rework
kew epic advance auth-rework --max-parallel 2
```

Sample output:

```text
epic auth-rework: tick signal=progressing graphql_points=5
  dispatched: ['42', '43']
  merged: ['40']
  held: ['41']
  in_flight: ['42', '43']
done=3 remaining=5
```

`graphql_points` is what the tick spent from the token's hourly GitHub GraphQL
budget (5,000 points). It adds up each query's own `rateLimit.cost` and counts
every remaining per-node `gh` read at GitHub's 1-point minimum. A tick reads all
of an epic's PRs in one query. It asks only about live nodes: a closed node
whose PRs are already known, and a node with no run and no branch, cost nothing
(#2582). The figure is left out when nothing metered the tick, as on a
Linear-backed epic.

## `kew epic run` — poll wrapper

```text
Usage: kew epic run [OPTIONS] [NAME]

Options:
  --interval INTEGER       Seconds between ticks while PROGRESSING (default: 60).
  --max-parallel INTEGER   Override config.epic.max_parallel for every tick.
  --all                    Drive every known epic round-robin in one poll loop.
  --takeover               Also reclaim a legacy no-heartbeat lock (a provably-stale lock is reclaimed automatically).
  --kew-binary PATH        Run each tick in a fresh process on this kew executable, resolved once at tick start.
  --epic-prefix TEXT       Label prefix identifying epic membership (default: "epic:")
```

Repeatedly calls `advance` every `--interval` seconds until the epic completes
or needs repair:

- **`complete`** — every node is DONE. Exits **0**.
- **`awaiting_merges`** — the frontier is empty and nothing is in-flight, but
  the only remaining work is PRs open in review awaiting a human's merge (and
  nothing is stuck). A non-alarming line lists the pending PRs and the driver
  keeps polling. A hand-merge is observed on a later tick, which clears
  configured review labels and advances the frontier.
- **`stalled`** — the frontier is empty, nothing is in-flight, no hold is
  live, and work remains that needs an operator. Exits **1**, with the
  stuck-node blast radius printed, so it never loops forever on a dead DAG.

An approved PR that's still waiting on CI reads as **`progressing` (HELD)**,
not `stalled` — `kew epic run` keeps polling instead of giving up on work
that's legitimately in flight.

### Exit codes

See the shared [exit-code scheme](exit-codes.md).

`kew epic run`'s exit code is API — orchestrating wrappers, cron jobs, and
background-task harnesses branch on it, so the codes are stable and documented:

| Code | Meaning | What to do |
|---|---|---|
| **0** | `complete` — every node is DONE. | Nothing remains. |
| **2** | `linear-unavailable` — Linear's API stayed unreachable past the client's bounded retry. A transient blip, not operator repair. | **Re-run** once Linear recovers. |
| **2** | `linear-outcome-unknown` — a Linear write got a 5xx or edge-429 reply and may have applied. Not retried. | Check the item in Linear, then **re-run**. |
| **2** | `linear-auth` / `linear-error` — Linear rejected the call (authentication, TLS, another 4xx, a GraphQL error or a rate limit that did not clear). Not transient. | Fix the key or the cause, then re-run. |
| **1** | `stalled` with a **failed-terminal**, cancelled, held, or otherwise operator-repair node. | Repair the node(s) — e.g. `kew epic retry <name> <node>`, or [`kew epic unhold <name> <item>`](#kew-epic-unhold-release-a-driver-hold) to release a wedged hold — then re-run. |

`awaiting_merges` is nonterminal for both single and `--all` drivers. **1**
stays the conservative "needs repair" code, and **2** is reserved for
classified infrastructure or configuration failures.

If another live driver holds the epic's lock, `kew epic run` prints the holder and exits **0**.
That driver keeps the epic moving.

```bash
kew epic run auth-rework
kew epic run auth-rework --interval 30 --max-parallel 3
```

Run it under a scheduler (cron, a `systemd` timer, a loop of your own) to
drive an epic unattended — it self-terminates instead of running forever.

### Upgrading kew under a running driver

Python imports modules lazily, so a driver running from an installation that
changes mid-run — an editable install of a checkout that a merge moves, or a
`pip install --upgrade` — can run one tick on code from two versions.
`--kew-binary` closes that window. Point it at the `kew` executable of a
pinned, non-editable install, typically through a symlink you repoint on each
upgrade:

```bash
kew epic run auth-rework --kew-binary /opt/kew/current/bin/kew
```

At the start of every tick the driver resolves that path through any
symlinks, binds it to the executable's bytes, and runs the tick in a fresh
process on it. Repointing `current` during a tick does not affect that tick.
The next tick runs the new version. Each tick prints the kew version and
package directory it runs from, at start and at end. Exit codes are the same
as without the flag. A tick that cannot load `kew.toml` is skipped and
retried on the next interval. `--kew-binary` drives one named epic; it is
refused with `--all`.

Like `kew epic advance`, if Linear is the tracker, any Linear API failure
makes `kew epic run` **error (exit 2)** with the same classified one-line
diagnostic (`linear-unavailable` past the client's bounded retry,
`linear-outcome-unknown`, or `linear-auth` / `linear-error`), never a traceback, and it never enters the
poll loop.

### Config reload between ticks

`kew epic run` re-reads `kew.toml` (plus any allowlisted DB config overrides)
**at the start of every tick**, so a change to approvers, concurrency, or
budget policy takes effect on the next tick without restarting the driver.
Two things are deliberately not reloadable, and each says so in its own way:

- **Pinned identity.** The tracker type, project repo, dispatch DB, and lock
  path are fixed at launch — a live driver retargeting any of them would
  abandon its reservations and its lock. A reload that changes one **exits 2**
  and names the field with both values:

  ```text
  [dispatch] kew epic run: config reload changed pinned tracker/repo/DB/lock
  identity (project.repo: acme/api -> acme/web) — stop and relaunch the driver.
  ```

  Stop the driver, finish the edit, and relaunch it.

- **A config that will not load.** A malformed or half-saved `kew.toml` (an
  editor autosave mid-tick is the common case) is an operator mid-edit, not a
  retarget. The driver **warns and keeps governing with the last config that
  loaded** — it does not exit, and it does not run half-applied settings:

  ```text
  [dispatch] kew epic run: config reload failed (kew.toml: Expected '=' after a
  key in a key/value pair) — keeping the last-good config; the fixed kew.toml is
  picked up on the next tick.
  ```

### Admission refusals from a dispatch child

The driver dispatches each node through a **detached** `kew epic _dispatch-node`
child whose stdin, stdout and stderr are `DEVNULL`. When admission refuses that
child before any agent spawns — an unpriced or stale-priced model, a missing
credential, a refused Switchyard route — the child exits **non-zero** and leaves
the reason on the node as an `admission_refused:*` hold. The next tick reads the
hold back and logs it:

```text
epic auth-rework: node 412 held — admission_refused: Exception during dispatch of #412: No pricing entry for model 'openrouter/x': a governed dispatch would meter it at $0. — fix the cause, then un-hold with `kew epic unhold <epic> 412` (held since 2026-08-26T23:53:50.123456+00:00)
```

Fix what the reason names, then release the node with
[`kew epic unhold`](#kew-epic-unhold-release-a-driver-hold). Refusals that clear
on their own — the fleet worker cap, the fleet kill-switch — are reported but
**not** held: the node stays admissible for the next tick.

### `--all` — drive every epic from one loop

`kew epic run --all` advances **every known epic** round-robin in a single
poll loop: each tick advances each not-yet-terminal epic once, acquiring and
releasing that epic's **own per-epic lock** — so two epics never lock each
other out and can tick concurrently. A "known" epic is any epic recorded
in the local run store — that includes freshly-planned epics (`kew epic plan`
registers them at creation time) as well as epics with driver history. At
startup `--all` logs the discovered roster and its discovery source (the local
`dispatch.db`), so a missing epic is diagnosable from the output. Per-epic
signals are reported per line, and the loop exits only when **all** epics are
terminal. This replaces stacking one `kew epic run <name>`
per epic. If an epic's lock is already held by another live driver, `--all`
surfaces the holder and skips that epic (it's being driven elsewhere) while
continuing to round-robin the rest. An epic whose query
resolves **empty** is surfaced with its diagnostic and counted as needing an
operator (contributing exit **1** to the aggregate) — it is never silently
dropped or read as `complete`. A Linear failure hits every epic on the
roster: each is surfaced with its classified one-line diagnostic
(`linear-unavailable`, `linear-outcome-unknown`, `linear-auth` or
`linear-error`) and marked as needing an operator (exit **1**), and the round-robin keeps processing the rest rather
than aborting on a traceback.

The aggregate exit code is the worst per-epic code.
Any epic that needs repair makes `--all` exit **1**.
Otherwise `--all` exits **0**: every epic is `complete` or driven elsewhere.
See [Exit codes](#exit-codes).

`--all` scopes itself to the **active project's tracker**. An epic whose
recorded tracker differs from the configured `[tracker] type` — inferred from
the shape of its recorded work-item ids (a numeric GitHub issue number vs. an
opaque Linear key like `SFD-12`) — belongs to a different project and cannot be
driven by the active tracker's sources. `--all` skips it with a one-line
`(skipped: tracker=linear)` note. A skipped epic contributes to **neither** the
STALLED classification **nor** the aggregate exit code — so a store that also
holds seven complete Linear epics no longer makes a healthy GitHub run exit
non-zero and demand an operator. An epic with no recorded items yet (freshly
planned, never driven) has no inferable tracker and is driven under the active
tracker rather than guessed away. If **no** discovered epic belongs to the
active tracker, `--all` reports that nothing is in scope and exits **0**.

`--all` also skips any epic an operator has
[archived](#kew-epic-archive-and-kew-epic-unarchive-retire-an-epic), with a
one-line `(skipped: archived)` note naming `kew epic unarchive` as the way back.
Like a foreign-tracker skip, an archived epic contributes to neither the STALLED
classification nor the aggregate exit code. If every in-scope epic is archived,
`--all` says so — naming the count and the reversing verb, so an empty run is
never a silent one — and exits **0**.

When every epic is terminal, `--all` prints **one combined fleet summary** that
names every driven epic under its exit classification — `complete`,
`stalled`, or `locked-out` (driven elsewhere). The summary is
printed regardless of the aggregate exit code, so a mixed-outcome fleet stays
legible: the `complete` and `locked-out` epics are still named even when the
worst-wins exit only reflects the `stalled` ones needing an operator.

```bash
kew epic run --all
kew epic run --all --interval 30
```

## `kew epic archive` and `kew epic unarchive` — retire an epic

```text
Usage: kew epic archive [OPTIONS] NAME

Options:
  --reason TEXT  Why this epic is retired (recorded on the chain).
```

```text
Usage: kew epic unarchive [OPTIONS] NAME

Options:
  --reason TEXT  Why this epic is active again (recorded on the chain).
```

An epic has no end. `complete` is a reading the driver can take back the moment
a merged PR reopens something, and an abandoned epic never reaches a terminal
signal at all — so the roster only ever grows. `kew epic archive` retires one
from the active views:

- it leaves the console's **needs you** count (its stalled-driver row is no
  longer a thing a person has to act on), and its page stops asking for
  anything: the legend reads `0 needs you` and the DAG draws no needs-you
  diamonds. An item-level wait on one of its nodes — an open pull request, a
  spend approval — is still counted on the header, Monitor and Work, where it
  names no epic;
- `kew epic run --all` no longer drives it, with a `(skipped: archived)` note;
- the web UI's Epics list moves it from **Active** to the collapsed
  **Finished / Archived** shelf.

**Nothing is deleted.** Its ticks, runs, drain and journal stay in the ledger,
the detail page at `/epics/<name>` still serves everything it served before,
and `kew epic advance <name>` / `kew epic run <name>` still drive it by name —
archiving retires an epic from the views that ask for your attention, it does
not take anything away from you.

The decision is recorded as exactly one attested
[`epic_archived` audit event](audit.md#event-types) carrying the epic ref,
the operator, and your `--reason` (which is optional). That event **is** the
state: there is no archive column. `kew epic unarchive` records an
`epic_unarchived` event that **outranks** it rather than retracting it, so the
whole history of a retirement — archived, revived, archived again, and why each
time — reads back out of `kew audit tail`.

```bash
kew epic archive tui-rewrite --reason "superseded by the factory UI"
kew epic unarchive tui-rewrite --reason "picking it back up"
kew audit tail --event epic_archived
```

### Exit codes

| Code | Meaning |
|---|---|
| **0** | the decision was recorded. |
| **1** | nothing to decide: `archive` on an already-archived epic, or `unarchive` on one that is not archived. A second archive is not a second decision, and attesting it would put a change on the chain that changed nothing. |
| **2** | the epic is unknown to this ledger (a typo'd name would otherwise retire nothing), or there is no ledger at all. |

## `kew epic continue` — continue preserved work

```text
Usage: kew epic continue NAME NODE
```

`epic continue` is the explicit recovery path for a dirty checkout preserved
after its dispatch owner died. Before claiming anything, kew verifies that the
recorded checkout is inside `[branches] worktree_base`, is still on the
recorded branch, is still dirty, and has no live or unproven owner. A missing,
clean, escaped, wrong-branch, or concurrently owned checkout is refused without
changing the continuation record.

The successful claim and its fleet worker slot are one transactional operation.
The continuation then runs the same item against the existing checkout and uses
the normal host-publication broker, so existing edits are not replaced by a
fresh checkout. Two concurrent `resume` callers produce at most one
continuation. A failed continuation returns to `awaiting_continuation` for a
later operator attempt; a successful published continuation is terminal.

Each released claim records an immutable `continuation_released` event with its
identity, generation, run ID, and reason.
Released generations without launched runs do not become publication ancestors.
A missing run row or an initial pending row has no launch evidence.
A finish, process, container, session, duration, usage, or cost supplies execution evidence.
Retained route profiles, `dispatch_started`, and `publication_intent` also supply execution evidence.
Released runs with execution evidence remain publication ancestors.
An older generation without claim or release evidence refuses resume and names
`kew epic abandon` as recovery.
Abandon also refuses incomplete owner history; it cannot prove missing owners have stopped.

Both verbs resolve the operator from `KEW_ACTOR`, then the OS user.
Both enforce `[epic].approvers`, falling back to `[approval].approvers`.
An empty allowlist permits the local operator.

```bash
kew epic continue auth-rework 87
kew epic continue auth-rework SFD-12
```

## `kew epic abandon` — release preserved work

```text
Usage: kew epic abandon [OPTIONS] NAME NODE

Options:
  --reason TEXT  Why this continuation is abandoned. [required]
```

The reason is required and must contain non-whitespace text.
Abandon finalizes one recoverable continuation with complete owner history.
Awaiting continuations and terminal continuations without proven success remain recoverable.
It probes retained worker identities from claims, releases, and checkout ownership records.
The checkout claim guard remains held during these probes.
Every consumed generation requires claim evidence, including generations with release events.
Missing claims or inconsistent generation counts refuse abandonment and name the history gap.
These refusals also apply to records with legacy evidence gaps.
It records an immutable `continuation_abandoned` event with the operator, reason,
generation, item, epic, and available run identity.
The transaction releases the matching reservation and records a retry marker.
The command releases its preserved-checkout claim through the existing checkout helper.
Existing audit rows and run history remain unchanged.

The next driver tick can dispatch the node with a fresh checkout under normal admission rules.
Abandon does not remove preserved files itself.
The fresh dispatch can replace that checkout, so copy any wanted edits before abandoning.
Fresh dispatch refuses active continuations across epics; use `kew epic continue` or `kew epic abandon` first.
Filesystem identity prevents alias or relocation bypasses.
Missing-path ownership applies only to the same item and branch, across epics.
A completed continuation never blocks another item.
Completion requires a terminal timestamp and a successful owner run.
Other terminal continuations require `kew epic continue` or `kew epic abandon` before fresh dispatch.
A pre-launch refused replacement requires matching checkout withdrawal evidence and cleared holds before another dispatch, even before run retention.
An existing recorded checkout requires the same filesystem identity for replacement.
When the recorded checkout is missing, abandonment can rebind only to the same item and branch.
The candidate can use the same path or a different path.
Setup creates an absent candidate and validates the generation of an existing candidate.
A failed generation capture refuses replacement.
The sibling `<checkout>.generation` sidecar stores JSON with `token`, `device`, and `inode` fields.
The token remains 32 lowercase hexadecimal characters.
Birth binding has two modes.
With `st_birthtime`, reads validate device, inode, birth identity, and the in-checkout nonce.
This mode records `birth_field` as `"st_birthtime"` and stores `birth_time`.
Without an immutable birth identity, the sidecar records `birth_field` as `"none"` and omits `birth_time`.
This mode validates device, inode, and the in-checkout nonce.
The nonce inside the checkout provides a consistency check, never identity evidence.
Reads ignore a recorded birth field when the current platform cannot read it.
Those reads still validate device, inode, and the in-checkout nonce.
Mutable directory timestamps never bind a generation.
Immutable audit evidence stores device, inode, available birth identity, nonce, and token in `checkout_id` as canonical JSON.
Resume, launch, publication, and base recording compare live kernel identity with that persisted evidence.
The checkout lease holds a directory descriptor until the run ends, including failure paths.
Each boundary validates `os.fstat(fd)` and `os.stat(path)` against the recorded binding.
Restoration cannot bind a new checkout generation to an earlier run.
A changed binding refuses before launch and requires fresh-dispatch recovery.
No agent-writable file authorizes identity. See the [checkout binding boundary](../threat-model.md#checkout-binding-boundary).
Creating, deleting, or renaming checkout root files preserves the generation.
The `.git/kew-generation` file contains the same random token as the sibling sidecar.
Immutable audit evidence holds the complete binding. The sidecar is a convenience copy.
Missing, unreadable, or mismatched factors refuse recovery and replacement.
Git metadata must remain a contained directory, and the nonce must remain a regular file.
A recreated directory cannot authenticate through reused device and inode values alone.
Token-only legacy sidecars refuse without validated abandonment authority.
After `kew epic abandon`, fresh admission retires a token-only sidecar through the revocation helper before capture.
Admission validates the continuation, item, branch, checkout scope, holds, and unconsumed authority before retirement.
Replacement evidence records `legacy_sidecar_retired=true` when admission retires that sidecar.
Remove the stale sidecar through kew's `revoke_checkout_generation` helper or run `kew epic abandon`.
The revocation helper removes both the sibling sidecar and the in-checkout nonce without reading their contents.
The next legitimate checkout creation mints a new bound token.
An absent candidate has no generation until setup creates its directory.
Missing-path authority refuses any directory that appears before setup validates that authority.
The `checkout_replaced` event records `rebound_from` with the original recorded path.
It also records `rebound_reason` as `"recorded checkout missing"`.
Withdrawal evidence preserves these fields and the original continuation family.
Validated replacement expiry consumes abandonment exactly once.
Loop dispatch consumes abandonment authority like fresh dispatch, before its first step launches.
Loops record replacement evidence and validated expiry under the loop run's identity.
Loops retain the checkout generation captured during setup.
Every paid attempt, including retries, validates that generation and its directory binding before launch.
Host publication validates the same binding again after the worker returns.
An identity mismatch refuses with exit code 2.
Refusal before the first worker launch still permits replacement withdrawal.
Loop setup requires a run store and run identity for this transaction.
An existing recorded checkout also prevents loop replacement at a different path.
Pre-launch loop withdrawal records checkout removal without reopening consumed abandonment.
An attempt that fails before expiry leaves abandonment available for another attempt.
Replacement expiry still requires the recorded generation and abandonment authority.
Abandon refuses continuing, successfully completed, or already abandoned continuations.
Resume also accepts terminal continuations without proven success, subject to its existing checkout and owner checks.
Already abandoned continuations need fresh dispatch; resume does not reopen them.

Exit codes follow the [exit-code scheme](exit-codes.md):

- **0**: the continuation was abandoned.
- **1**: the verb ran and refused; the output states the refusal.
- **2**: the verb could not run, including a missing ledger, unknown target, or missing authority.
  Missing or blank reasons also exit **2**.

```bash
kew epic abandon auth-rework 87 --reason "Restart after the cost breaker"
```

## `kew epic retry` — re-admit a failed node

```text
Usage: kew epic retry [OPTIONS] NAME NODE
```

`<node>` is the GitHub issue number. Re-admits a **FAILED** node to the
dispatch frontier without deleting its run history:

- Restores the configured ready label and clears failure labels in one edit.
- Releases only an observed terminal reservation generation; live or
  indeterminate reservations survive.
- Records a non-destructive **retry marker** so the driver's status overlay
  treats the stale failed run as superseded (rather than discarding it) —
  the node falls through to its provider-derived status (typically back to
  `TODO`, re-entering the frontier on the next tick).

Cost and token history from the failed run(s) are preserved — `kew report`
still shows them. A run that fails *after* the retry marker is a fresh
failure, not the superseded stale one.

A node the driver is actually **holding** is refused: retry exits **2** with
`held (<reason>): retry cannot clear a hold — run kew epic unhold <epic> <item>`
and records nothing — no marker, no label edit, no audit event. Admission state
is not what a hold gates, so a retry that reported success used to leave the
node HELD exactly as it found it (#2154). Release the hold first — see
[Hold semantics](#hold-semantics-who-sets-and-who-clears-a-hold). Where the
recovery needed retry's fresh round allowance rather than its admission reset —
a `review_rounds_exhausted` hold — the release grants it:
[`kew epic unhold`](#kew-epic-unhold-release-a-driver-hold) restarts the review
cycle for that reason, so you never have to race the driver's next review pass
with a retry. A node whose
dirty work is preserved is refused the same way, pointing at
`kew epic continue`.

"Actually holding" is the driver's reading, not the mere presence of a row: the
refusal matches exactly the reasons the overlay routes to HELD — `ci_pending`,
the governed-review reasons, and the `budget_preflight` / `budget:` / `sandbox_containment`
/ `admission_refused` / `review_budget` / `base_integration_conflict` /
`base_integration_failed` / `merge_gate_touched` / `publication_held` /
`test_declarations_missing` / `declaration_record_failed` /
`pr_body_unreadable` / `remediation_no_change` / `remediation_no_progress` /
`resource_retries_spent` /
`pre_review_gate_exhausted` prefixes. A hold reason the overlay does
not recognise leaves the node reading FAILED and still dispatchable, so retry
stays its recovery path and runs normally.

One exception, and it is about visibility rather than about retry. The driver
writes two reasons of its own when it refuses a node *before* any agent starts
— `credential_issuer_refused` and `preexisting_worktree_refused` — and keeping
them unrecognised is what leaves the node dispatchable. A later dispatch that
succeeds clears the hold, because the refusal was a property of that one
attempt. If one does survive into the review stage, a node that has come back
with review findings reads **held** with that reason rather than `remediating`:
remediation is the only thing that moves such a node, and it will not run while
a hold stands, so reporting `remediating` would be a stall with nothing to show
for it. `kew epic retry` is accepted on both reasons for as long as the node is
still awaiting dispatch — that is what the refusal leaves it free to do. Once
the node has come back with review findings, nothing will dispatch it again, so
retry has no recovery left to offer and refuses like any other held node: clear
the row with `kew epic unhold <name> <node>` and remediation resumes.

Retry authorizes new agent spend, so it binds the same approver gate as the
console's retry and the TUI's: an actor outside `[approval].approvers` is
refused with exit **2** and nothing is recorded — no marker, no reservation
release, no label edit, no audit event. An approved retry's `epic_node_retry`
event carries the bound policy hash.

Retry never starts an unsafe background process. If the epic lock is not held,
it exits **1** with `no live driver — relaunch required: kew epic run <name>`;
an active or indeterminate lock is treated as live.
The retry is already recorded; relaunch the driver to act on it.

Under `kew drive`, the daemon holds no epic lock between its ticks. When a
running drive (the one `kew drive status` reports) has the epic in its
rotation, retry says the drive picks the item up on its next tick and names
no relaunch command.

```bash
kew epic retry auth-rework 87
```

### Automatic resource retries

One kind of failure the driver retries by itself. A run killed by SIGABRT or
SIGKILL that nothing else explains (not the wall-clock deadline, a checkout or
host-disk cap, a pause, a budget stop or a circuit breaker) fails as
`resource`, with the signal and the runner's last output lines as its reason.
That covers a confirmed OOM kill, and also a SIGKILL kew cannot attribute: a
container runtime restart, a stray `docker kill`, an OOM probe that could not
be read.

The driver re-admits such a node through this same retry path, once for the
commit the failed run started from, and records it as an `epic_node_retry`
event with the actor `policy/driver_resource_retry` and `reason`, `head` and
`run_id` in its payload. Each retry is one more paid run.

- A second `resource` failure from the same commit holds the node as
  `resource_retries_spent`, with a reason naming that commit. The run's own
  reason still names the signal and the output.
- Every relaunch starts from the current main, so the per-commit key alone
  would buy a node that always runs out of memory one more run each time main
  moves. A node gets at most **two** automatic resource retries in total,
  across commits. Its next `resource` failure holds it the same way.
- A held node, or one with preserved dirty work, is not re-admitted, exactly
  as `kew epic retry` refuses it.
- A newer run already in flight for the node, such as a dispatch by hand,
  stands in front of the older failure, which then re-admits nothing.

To recover a `resource_retries_spent` node, find why it runs out of a
resource, then run `kew epic unhold` and `kew epic retry`. The release leaves
the node failed; the driver does not hold it again for the same run. After
the cap, a `resource` failure after your retry holds it again, without a
third automatic retry.

## `kew epic unhold` — release a driver hold

```text
Usage: kew epic unhold [OPTIONS] NAME ITEM

Options:
  --epic-prefix TEXT  Label prefix identifying epic membership (default: "epic:")
```

`<item>` is the work-item id (a GitHub issue number, or a tracker id like
`SFD-12`). The operator counterpart to the driver's own post-terminal hold
bookkeeping: when a sticky **hold** (typically `ci_pending`) wins overlay
precedence and wedges the node — the case the containment arc kept clearing by
hand via raw store calls — `unhold` releases it:

- Clears the `epic_holds` row for that epic/item via the store API.
- On a **GitHub** tracker, also strips the stale `pr-pending-review` label
  (mirror of the manual unwedge playbook). A non-github tracker owns node state
  in the tracker itself, so no label is touched.
- Prints the cleared reason and the hold's held-since timestamp (UTC).
- Records an `epic_node_unhold` audit event carrying the released reason and
  whether the review cycle restarted with it, so a release that grants extra
  reviewer rounds is on the tamper-evident chain exactly as the
  `epic_node_retry` a [retry](#kew-epic-retry-re-admit-a-failed-node) writes is.
- For a `review_rounds_exhausted` or `pre_review_gate_exhausted:*` hold
  **only**, restarts the node's review cycle in the same transaction — the same fresh round allowance
  [`kew epic retry`](#kew-epic-retry-re-admit-a-failed-node) grants. That cap is
  read from the node's round count, so a release leaving the count standing
  would be re-held for the identical reason by the driver's next review pass,
  and retry — the only other command that restarts the cycle — now refuses a
  held node. The success line says `review cycle restarted` when it happened.
  Every other reason clears against a change you make outside kew (a raised
  `review.budget`, a mapped sandbox image, a green CI run), so nothing is reset
  for those: the released node gets a real reviewer round on the next pass.
- For a `merge_gate_touched@<head>` hold **only**, records that head as reviewed
  in the same transaction. The [serial merge queue](#the-serial-merge-queue-opt-in)
  derives that hold from the PR's diff, so clearing the row on its own releases
  nothing — the next merge pass intersects the same unchanged diff with
  `[epic] merge_gate_paths` and holds the node again. The record covers the head
  you read and nothing else: a later push to the PR is held for a fresh read.
  The success line says `reviewed head recorded`, and the audit event carries
  the head as `merge_gate_head_reviewed`.
- For a `remediation_no_change` hold **only**, reads the pull request's head
  before releasing and records it on the audit event as `pr_head`. On an
  operator send-back the release answers the send-back for that head only: if a
  base integration moves the PR before a reviewer reads it, the send-back
  stands on the new head. A head that cannot be read is recorded as none: the
  command warns that a send-back on the node still stands, and the release
  answers no send-back.

Exits **2** with a clear message when `<item>` is not a valid item id for the
configured tracker, or when no hold exists for that epic/item.

This is where a hold fits the operator loop: a sticky hold makes `kew epic run`
exit **1** (`stalled`, needs repair — see [Exit codes](#exit-codes)). `unhold`
is the manual escape hatch when no automatic path clears the row — see
[Hold semantics](#hold-semantics-who-sets-and-who-clears-a-hold) for the full
set/clear map.

```bash
kew epic unhold auth-rework 87
kew epic unhold auth-rework SFD-12
```

## `kew epic send-back` — send a READY node back for remediation

```text
Usage: kew epic send-back [OPTIONS] NAME NODE

Options:
  --reason-file FILE  File holding the blockers the remediation must fix.
                      [required]
  --epic-prefix TEXT  Label prefix identifying epic membership (default: "epic:")
```

kew's own reviewer can pass a head that your own review finds blockers on. The
drive then reads the node as `in_review — awaiting merge` and does nothing about
what you post on the PR. `send-back` records your blockers as a review round, so
the drive fixes them itself:

- Records a `NEEDS_CHANGES` round against the head the node's READY verdict
  stands on (a READY-with-residuals acceptance counts too). It first reads the
  pull request from GitHub: that head must still be the pull request's head, so
  the round always lands on a head a reviewer passed. The round's stored
  findings hold one `high` finding, and its summary is the text of
  `--reason-file`.
- Attests the round as an `adversarial_review_verdict` with you as the actor,
  and records an `epic_node_send_back` audit event carrying the reason.
- On the drive's next tick the node reads `remediating`. The drive's own review
  stage runs the remediation under the node's item id, so the drive keeps
  owning the node, its claim binding and its run lineage. `kew review <PR>`
  and a commit of your own on the agent branch both end that ownership. The
  remediation's brief carries your reason. When it lands a new commit, the
  reviewer re-reviews the head, and your finding is part of that review.
- The send-back opens a fresh `[review] max_rounds` budget. The rounds before
  it, the ones that got the node to the READY you rejected, stop counting, so
  a node that needed a remediation to reach READY is still remediated. The
  send-back round itself does not count, the same as a merge-queue round. The
  review rounds after it count like any other.
- With `[review] blocking = true`, the merge gate reads the round as
  not-READY, so the node does not merge until a later round passes. The gate
  re-reads the latest round immediately before it merges, after the merge
  queue's verification too, so a send-back made while the queue is verifying
  the node still stops the merge. If the pull request merged while the
  send-back ran, the round stays recorded and the command warns that the
  send-back came too late, without the line saying the drive remediates it.
- The send-back stands until a remediation answers it, even when the head
  moves first: a merge-main step that pushes onto the branch fixes none of
  your finding, so the drive remediates the moved head rather than handing it
  to a reviewer. The finding's location is `operator-send-back@<head>`.
- With no live driver for the epic, the command says so, and the send-back
  still succeeds: the round is recorded, and the next `kew epic run` picks it
  up.
- Under advisory review (`[review] blocking = false`) a node whose latest round
  is DEGRADED awaits merge just as a READY one does, so it can be sent back.
  A refusal of any other verdict names the review mode it was read under.

Exits **2** with nothing recorded when the reason is empty or longer than
16,000 characters, when the node is held (run `kew epic unhold` first), when
its latest round is not READY, when it has no linked pull request, when its
pull request is merged, closed or cannot be read, when its pull request's head
moved since the READY review (wait for the drive to review the new head, then
send it back), when a reviewer, remediation or dispatch holds its reservation,
or when you are not in `[approval].approvers`. A send-back authorizes a
remediation run, the same as a retry.

```bash
kew epic send-back prompt-profiles 3023 --reason-file codex-round-1.md
```

## `kew epic acceptance amend` — amend a node's acceptance contract

```text
Usage: kew epic acceptance [OPTIONS] COMMAND [ARGS]...

Commands:
  amend  Record an amendment to NAME's node ITEM's acceptance contract.
```

```text
Usage: kew epic acceptance amend [OPTIONS] NAME ITEM

Options:
  --reason TEXT    Why the contract changes; attested.  [required]
  --author         Refer the node's open dispute to the acceptance author
                   instead: its next dispatch re-runs the author with the
                   dispute as input.
  --checkout PATH  Checkout whose HEAD carries the amended file (default: the
                   project root).
```

A node's acceptance contract (`kew/tests/acceptance/test_issue_<N>.py`, see
[How it works](../how-it-works.md)) is never edited by its implementer. When the
implementer believes a contract test is wrong, it disputes it: its final message
is

```text
KEW_NEEDS_HUMAN: acceptance-disputed {"criterion": "AC2", "test": "test_ac2", "problem": "...", "evidence": "...", "proposed_fix": "..."}
```

(the whole reason is capped at 500 characters, like any `KEW_NEEDS_HUMAN`
reason; a dispute that does not parse is an ordinary needs-human stop). The
node is held as `acceptance-disputed` and shows on Needs-you. That is not a
failure: nothing is published as failed, and no review round is spent. An
`acceptance_disputed` audit event records the dispute. A person resolves it in
one of two ways:

- **By hand.** Commit the amended file, then run `kew epic acceptance amend
  <epic> <N> --reason TEXT`. The file as committed at HEAD becomes the
  contract's next version.
- **By the author step.** `kew epic acceptance amend <epic> <N> --author
  --reason TEXT` refers the dispute to the acceptance author and releases the
  hold. The node's next host dispatch runs `acceptance_author` again with the
  dispute as input. The amended file passes the same checks a first contract
  does: it is the only change, it defines exactly the `test_ac<k>` tests, and
  each one fails on the checkout. Then it is committed and recorded as the next
  version. A contained dispatch refuses instead, as it does for a first
  contract. If the plan's test criteria changed since the dispute was filed,
  the dispatch writes a new contract for the new plan instead and closes the
  dispute, recorded as an `acceptance_dispute_superseded` audit event: its
  tests belonged to the old contract.

Every amendment writes one `acceptance_amended` audit event: who, why, the old
and new sha256, and the dispute it answered. It also closes the dispute and
releases the `acceptance-disputed` hold, re-admitting the node in the same
transaction as [`kew epic retry`](#kew-epic-retry-re-admit-a-failed-node)
would, so the implementer continues on the amended contract. `--author`
releases and re-admits the same way when it refers the dispute. No second approver is needed. The command refuses and records
nothing when the node has no contract, the file is not committed at HEAD, or
it is unchanged from the current version. Releasing the hold with
[`kew epic unhold`](#kew-epic-unhold-release-a-driver-hold) instead keeps the
contract as it stands; the disputing run was a needs-human stop, so the node
then needs a `kew epic retry` to dispatch again.

```bash
git commit -m "test: fix test_ac2 for #412" kew/tests/acceptance/test_issue_412.py
kew epic acceptance amend auth-rework 412 --reason "AC2 asks for 401, not 403"
kew epic acceptance amend auth-rework 412 --author --reason "let the author decide"
```

## The merge gate

The driver merges a node's PR **only** when both of these hold:

1. **A verified human approval.** GitHub's own `reviewDecision == APPROVED`
   computation (not the mere presence of a review — this accounts for
   reviews GitHub has marked stale after a new push), from a reviewer who is
   **not** the PR author, is a GitHub `User` rather than a bot, and currently
   has `write`, `maintain`, or `admin` repository permission. Kew reads that
   permission fresh for every merge decision and fails closed if the lookup or
   identity evidence is missing. When `[epic] approvers` is non-empty, the
   reviewer must also be in that allowlist.
2. **CI-green.** The PR's effective required status checks are green,
   verified via `mergeStateStatus == CLEAN` (read from GitHub's own
   effective-rules computation, which understands both classic branch
   protection and rulesets). GitHub counts a skipped required check as
   passing, so the driver also reads the head's own check runs, with the same
   rule as the loop `ci_green` gate: each check is decided by its latest run
   that was not skipped, a failure wins, and at least one check must have
   succeeded. A check whose every run was skipped did not run. When it is one
   of `[ci] approval_gated_checks`, an agent PR whose self-hosted tests wait
   for a human `ci-approved` label (#2033) waits
   with `awaiting ci-approved: tests did not run` and merges once its tests
   have run and passed. Any other all-skipped check (push-only,
   paths-filtered, matrix-excluded) is ignored. A head with no check runs has
   only its legacy combined commit status, and a status never makes a head
   green on its own (an unrelated early status can post success before the
   head's check runs register): failure or error holds, pending waits, and
   success waits for a human with `commit statuses green but no check runs:
   merge by hand`. A head with neither check runs nor statuses has no CI
   evidence: the driver does not merge it and waits with `no CI evidence on
   head: merge by hand`. These waits are human gates: the epic reports
   `AWAITING_MERGES`, never `STALLED`, `kew epic status` shows the reason on
   the node's line, and no hold is written, so `kew epic unhold` is never
   needed.

The token needs `Pull requests: write` permission for merges and marker repairs.

An approved PR that isn't green yet is **HELD** (`ci_pending`) — not stuck —
and the driver merges it automatically the moment CI turns green, so you
don't have to babysit the timing. A genuinely red or pending required check
(or a stale base) is a sticky **hold** the operator needs to look at.

The merge itself is always:

```text
gh pr merge <pr> --repo <repo> --squash --match-head-commit <sha>
```

— **never** `--admin` or `--force` (those flags let `gh` merge over a red or
pending required check, which would silently defeat the CI-green gate
above). No label ever triggers a merge — a dispatched agent's `gh` token
could set a label on its own PR, so a label is not a trustworthy signal.

### The serial merge queue (opt-in)

Both gates above prove a PR is green **on its own head**. Neither can prove it
is green against the base it is about to land on, and two siblings each green
alone can be red together: one PR adds a required API field its own fixtures
carry, a sibling merged an hour earlier added a fixture that does not, and the
combined tree fails a gate no per-PR check ever ran.

Set `[epic] merge_queue = true` and that combined tree is checked before every
merge, one candidate at a time repo-wide (under `merge-queue.lock`, a sibling of
`[epic] lock_path`, held until the merge it clears has landed), in node order. The flag has two prerequisites, both refused
loudly rather than silently worked around:

- `[review] adversarial = true`, or the config fails to load. A red combined tree
  is recorded as a `NEEDS_CHANGES` review round, and the governed review stage is
  the only thing that remediates one — without it the queue would repeat the same
  refusal every tick with nothing to fix the tree.
- `[commands] verify_loop`, or the config fails to load. The queue does **not**
  fall back to `[commands] ci`: on this repo that is pytest alone, so the
  fallback merged combined trees nothing had linted, typed or scanned while the
  node's evidence said the queue had verified them. The command a combined tree
  must pass is chosen for the queue, or the queue does not run.

With both in place, for each candidate:

1. the base branch is fetched from the **real upstream** — the sibling merged one
   verification ago exists only there — its commit OID is recorded, and it is
   merged into the READY head in a scratch clone kew creates and discards, never
   the operator's checkout;
2. the PR's own diff against that base (`git diff --name-only <base>...<head>`)
   is intersected with `[epic] merge_gate_paths`, **before any command runs**: a
   PR that edits the gate script, the build backend, a `conftest.py`, a
   scanner's own allowlist (`.gitleaks.toml`, `mkdocs.yml`) or the
   container the gates build is a PR that can decide what proves it, so it is
   **held** as `merge_gate_touched@<head>: <paths>` for a human to read and
   `kew epic unhold`. That release records the head it covers, because the queue
   re-derives this hold from the PR's unchanged diff on every pass — without the
   record the released node would be held again on the very next one. A later
   push moves the head and is held again. The same hold, released the same way,
   covers a PR whose diff raises a test-integrity flag (a removed assert, a
   deleted test, an added skip, ...) that its READY review verdict does not mark
   `ACCEPTED` in its **Test changes** section:
   `merge_gate_touched@<head>: unanswered test-integrity flags <flags>`.
   It also covers a READY — or a `NEEDS_CHANGES` whose residuals the stop rule
   accepted — whose `not_checked` names checks the offline review sandbox
   could not run — `docker`, `network`, `host` or `other`:
   `merge_gate_touched@<head>: review sandbox could not run: <kinds>`. A
   stored verdict whose sidecar file is missing, no longer matches the digest
   its round recorded, or does not parse is held as `other` until
   `kew epic unhold` releases that head. A PR that carries more than one of these is held once, under one reason that
   names them all —
   `merge_gate_touched@<head>: <paths>; unanswered test-integrity flags <flags>; review sandbox could not run: <kinds>`
   — so the person who releases it sees everything the release answers, and
   one `kew epic unhold` of that head releases them all. The release answers
   only what that reason named: a later re-review of the same head that names
   a check the released reason did not is held again, under one reason naming
   everything. A `full_suite` entry adds nothing here, since the verify
   command runs the suite. A `browser` entry on a PR whose diff touches
   `frontend/` and nothing under `www/` is not a hold: the node waits
   (`merge-verify-browser-unchecked`) until a passing `kew verify` that ran the
   UI check is on record for that head or for a merge of it, and an unhold
   does not answer it. On any other diff no `kew verify` answers it — none
   runs the UI check off `frontend/`, and none runs `www/`'s own
   `npm run browser-check` — so `browser` is held like `docker` and named with
   it, whatever verify is on record. Where an epic merges with no merge queue,
   `full_suite` and `browser` are both held like `docker`: that path runs no
   verify and reads no diff;
3. `[commands] verify_loop` runs in that combined tree, **contained** — in the
   image mapped for `[agent] runner` (falling back to the stock `[sandbox]
   image`), with no network and none of the host's environment. Gates that
   cannot run there are named in `[epic] merge_queue_uncontained_gates`,
   recorded on the attestation as not proven, and left to CI;
4. when that command succeeds the merge proceeds — still holding the queue lock,
   so the next candidate is verified against a base that already contains this
   merge — but only while the head still equals the head that was verified and
   the base still points at the recorded OID: a verification takes minutes, and
   a head or a base that moved in the meantime was never verified (the
   `--match-head-commit` argv below then makes the head binding atomic at
   GitHub);
5. on a failure — a red gate, or a conflict with the base — the node is **not**
   merged: the failure is recorded as a `NEEDS_CHANGES` review round naming the
   failing gate, which the review stage remediates like any other review
   finding, and the merge gate stays closed until a re-review returns READY.
   The base binding is re-checked *before* a red is attributed to the PR: a tree
   that went red against a base something outside kew has since moved proves
   nothing about the PR, and a `NEEDS_CHANGES` round is not a withheld merge the
   next tick retries — it stands until a remediation and a re-review clear it.
   That case is reported as a moved base instead, and the next tick verifies the
   new combined tree.

A queue that could not decide never releases a merge: no verify command
configured, no admitted posture to run it under, a scratch tree another reviewer
holds, a base merge git could not run, a base OID or a PR diff kew could not
read, a command that timed out or a container that could not start, a
post-verification head kew could not read, a queue lock the filesystem would
not grant at all, and a busy queue each hold the node with that reason. Every
decision that got as far as binding a base — the green and the red alike — is
attested as a `merge_verify` audit event, carrying the base OID the tree was
verified against, the containment posture it ran under, and any gates declared
unrunnable under containment. The red carries them too: it is a decision about
one specific combined tree, and without the OID nothing can say afterwards
*which* main that head was red against.

Those reasons do not all mean the same thing to `kew epic run`, and the tick
signal says which is which. A busy queue and a scratch tree another reviewer
holds are **contention** — another process is doing that work right now, and both
locks are released when their holder finishes or dies — so the tick reports
`progressing` and the next tick retries; they never end the run with a repair
exit code. A head that moved (before the tree was cut, or during the
verification), a base that moved under the verification, and a recorded
`NEEDS_CHANGES` round read as `awaiting_merges`: nothing to repair, the node is
awaiting re-review (or, for a moved base, a fresh combined tree next tick).
Every other reason — no verify command, no admitted posture, an unusable queue
lock, a base merge git could not run, a timeout, a container that could not
start, an unreadable head, a `merge_gate_touched` hold — is something only an
operator can clear, and reports `stalled`.

A verification can take many minutes, so while one runs the driver refreshes its
own epic lock heartbeat in the background — otherwise a healthy driver blocked on
a long gate would look stale (past 300s) and a second driver of the same epic
would reclaim the lock and tick alongside it.

The verify command is operator-owned, but what it **executes** is not: every
gate worth running runs files from the tree under test. That is why it runs
contained rather than as a host child of the driver that holds the merge
credentials, why a PR that edits the gate definitions is held instead of
verified, and why (like every `[commands] verify_loop`) the command itself must
never be set from issue or agent text. Where no image resolves and
`[sandbox] dangerously_skip_containment = true` is set, the queue falls back to
host execution exactly as the loop gate does; with neither, every candidate is
held `merge-verify-uncontained` rather than run.

For a Linear epic, this is still a human merge gate on GitHub. Linear is the
work-item and lifecycle system; GitHub remains authoritative for the PR,
review, CI, and merge decision. An operator may merge the PR externally after
the required human approval and green CI. The next driver tick reconciles that
merged PR and writes Linear Done; Linear Completed by itself never proves that
the PR merged.

### How a node is linked to its PR

The `Closes #N` keyword in a PR body is a **convenience, not the key**. GitHub
fills a pull request's closing reference only from that keyword, so any rewrite
of the body — by a reviewer, by a human, by `gh pr edit --body-file`, or by kew
itself — used to make a published node invisible to the driver: it read `todo`,
returned to the frontier, and was redispatched over work that already existed.

The driver collects candidates from three sources:

1. **`ledger`** — the PR URL kew persisted to the run store when it published
   the artifact. This is kew's own record and survives every later body edit.
2. **`closing-ref`** — GitHub's `closedByPullRequestsReferences`, the keyword's
   derived link. Still consulted, no longer relied on.
3. **`branch`** — a PR whose head branch is the node's
   `agent/issue-<slug>` branch (`[branches] prefix`). The reader uses the broad
   PR batch first. It performs a bounded `--head` lookup when that batch misses.

The resolver ranks every candidate by state. The newest `OPEN` PR wins first.
The newest `MERGED` PR wins next. The newest closed-unmerged PR wins next.
An unread candidate remains linked as `UNKNOWN`. The ledger breaks ties within
one state. The driver logs every candidate when sources disagree.

A closed-unmerged winner reads `todo`. A tick removes its stale pending-review
label. That tick clears only a `ci_pending` hold. The node is not dispatched
over its closed PR: `kew epic status` and every tick report it
`stuck: #N (closed-unmerged: PR #M closed without merging)` and name the
restart, `kew epic retry <name> <node>`. A retry newer than the node's newest
implementation run releases it, the next tick dispatches it afresh, and the new
PR outranks the closed one. The closed PR stays in the ledger. If the closed
PR's branch is still on the remote, delete it first: a ready node whose
`agent/issue-<slug>` branch already exists is refused and reported
`stuck: … (branch-exists: …)`, never skipped silently. An unread `UNKNOWN` winner
reads `in_review`, so a failed read never places the node on the frontier.

`kew epic status` prints the source it used for each PR'd node:

```text
PR linkage: #1706->PR #1723 (ledger), #1710->PR #1725 (branch)
```

The link source shows where Kew found the PR. The closing-keyword warning is a
separate fact. Each tick repairs missing markers on open dispatch PRs. Kew never
edits hand-made PRs found from a `closing-ref`. Add
`<!-- kew:no-autoclose -->` to a body to disable marker repair. A merged PR with
an open item reads `held` with reason
`merged_unclosed`. The merge pass closes the item as completed on that tick.
The next tick reads the closed item as `done`. The node never enters the
dispatch frontier.

### The governed-review invariant

When the [governed adversarial-review stage](../how-it-works.md#governed-adversarial-review)
is enabled in **blocking** mode (`[review].adversarial = true`,
`[review].blocking = true`), the merge pass gains **one more invariant**, checked
at merge time for every policy-covered candidate:

> merge only if the latest verdict is `READY` **∧** `verdict.head_sha ==` the
> current head SHA **∧** the approval timestamp postdates the verdict.

This is enforced at the merge pass itself, not the status label — `HELD` is a
live merge candidate, so an approved + green-CI `HELD` node with a non-`READY`
verdict is **skipped**, loudly, with an attested reason. It closes three holes
at once: a `HELD`-is-mergeable bypass, a post-verdict force-push merging
unreviewed code (the head at merge must be the reviewed head), and a stale pre-existing
approval auto-merging (the approval must postdate the verdict, else it surfaces
as "re-approve"). Nodes already in review when the flag flipped are grandfathered
(`PRE_POLICY`) and merge under the pre-existing approval + CI rules above.

In every review mode, and for grandfathered nodes too, the merge pass also
applies the [merge verdict rule](merge.md#checking-a-hand-merge): it does not
merge a head whose newest attested review verdict is `NEEDS_CHANGES` or
`NEEDS_FULL`, and reports the skip as `merge-verdict-needs-changes` or
`merge-verdict-needs-full`.

The verdict is **evidence for the human approval, never a substitute** — there is
still no auto-merge on `READY`. Each round's verdict is a chain-attested
`adversarial_review_verdict` event (see [`kew audit`](audit.md) / the
[audit trail](../audit.md)) binding the PR, item, head SHA, policy in force, and
review-pool spend, so "did the merged code survive review" is answerable from the
chain alone.

### The review stop rule

Some PRs ran 10 to 14 review rounds on low and medium findings. The stop rule
ends that. After round `[review].stop_rule_after_rounds` (default `6`; `0`
turns the rule off), a `NEEDS_CHANGES` verdict whose findings are all within
`[review].stop_rule_max_severity` — `"medium"` (the default) accepts `low` and
`medium`, `"low"` accepts a round of lows only — is accepted as
**READY-with-residuals**:

1. The driver files the round's findings as one GitHub issue, labelled `bug`.
   The issue lists each finding's severity, `file:line` and summary, and links
   the PR and the sidecar digest. Its body says it is a residual of the stop
   rule. If an earlier accepted round already filed a **still-open** issue for
   the same findings, that issue is reused instead of a second one being filed,
   and the reusing round is recorded on it as a comment carrying its own round,
   head and finding table. Findings match id-for-id, or — because a merge
   shifts lines and a fresh reviewer run rewords prose — on the files they
   name. A residual in a file no open residual issue covers gets its own issue,
   and so does one whose only tracker has been closed.
2. The driver attests a `review_residuals_accepted` audit event. The event
   carries the issue number, the round, the head SHA, the sidecar digest, the
   finding ids and the file keys the next round matches against.
3. The driver comments on the PR with the issue link. It sets the `kew-review`
   status to success and names the issue in the status description.
4. The driver marks the round accepted in the `review_rounds` ledger
   (`accepted_verdict = READY_WITH_RESIDUALS`, schema v59), so a reader polling
   recorded rounds sees the merge-ready state instead of the reviewer's standing
   `NEEDS_CHANGES`.

The merge gate requires **both** the acceptance event and that ledger mark, so a
round carrying only one of them never merges — the ledger a watcher polls and the
PR kew squashes can never disagree. If the mark cannot be written, the node keeps
its standing `NEEDS_CHANGES` round and the next tick re-reads the same acceptance
and marks it: no hold and no `DEGRADED` round, because the round the retry needs
is the one still on record, and nothing merges until the mark lands. A mark that
matches **no** recorded round is not a retryable write — the ledger and the
acceptance disagree about which round was accepted — so it takes the hold
(blocking) or `DEGRADED` (advisory) path below, and its diagnostic says to leave
the residual issue open because the chain acceptance stands.

The **overlay** reads the acceptance the same way, which is what makes that retry
reachable: until both halves are on record the node stays `remediating` — the
review stage's to drive — instead of reading `in_review`, a state the stage skips.
It dispatches no remediator there: the one action it owes the node is the mark. An
accepted round the ledger does not carry yet is therefore marked on the next tick
whatever its sidecar now says, which is also how an upgrade settles a round
accepted before schema v59 (the mark did not exist, so no pre-v59 acceptance has
one) — the attested acceptance carries its own round, head and issue number, so
nothing has to be re-reviewed or re-filed to finish it.

A **held** node is the one place that retry cannot come from the lifecycle: a
hold outranks `remediating` in the overlay, and the review stage drives review
statuses only. So the stage marks an attested-but-unmarked acceptance on a held
node before anything else it does that tick — a ledger write plus the round's
`kew-review` status, no issue filed and no agent dispatched. The hold itself is
untouched and clears the way it always did (`ci_pending`, the everyday case,
clears when CI turns green); what the mark restores is that both readers agree,
so the merge gate stops refusing the node before the merge pass reaches the CI
check that would release the hold.

The overlay and the merge gate then read that round as `READY`. The rest of the
invariant still applies: the head must not have moved since the verdict, the
approval must postdate it, and CI must pass its human gates. A push after the
acceptance voids it, the same way a push voids a `READY`: the node goes back to
`adversarial_review` and the next round is reviewed afresh on the new head. If
that round reports the same unfixed findings, it reuses the open issue already
filed for them and comments its round on it — the acceptance is void, the
residual is not, and it does not collect one issue per round. The reviewer's own
`NEEDS_CHANGES` round and verdict event are never rewritten.

Any finding above the configured ceiling keeps the node in remediation — `high`
and `critical` always, and `medium` too under `stop_rule_max_severity = "low"`.
A severity that is missing or not one of the four known values counts as
`high`, and so does an unreadable ceiling: nothing is accepted under one. A
`NEEDS_CHANGES` with no findings never qualifies. The rule reads only a
recorded `NEEDS_CHANGES`, from a full or a delta round alike. It never touches
`NEEDS_FULL`, which a delta `READY` becomes when no full round sits in the two
before it. The rule is checked before
the `max_rounds` cap, so it only matters when `max_rounds` is larger than
`stop_rule_after_rounds`.

If the issue cannot be filed, or its audit event cannot be written, the node
does not merge. In **blocking** mode it is held with reason
`review_residuals_unfiled`: fix the cause, such as `gh` auth or a missing `bug`
label, then run `kew epic unhold`. The next tick retries the rule. In
**advisory** mode nothing is held — the driver attests a `DEGRADED` round and
proceeds, the same as a round cap or an unparseable verdict does there, so a
transient `gh` error never parks an advisory node behind an operator command.

The driver checks for the audit event before it files, and the check and the
filing run inside the node's review reservation, so neither a re-tick nor a
second concurrent driver tick files a second issue for a round already recorded.
If the issue exists but the event write failed, the driver log names the issue
and says which of the two cleanups it needs — they are opposite, so follow the
message rather than assuming:

- A **freshly filed** issue is not on the chain (the acceptance that would have
  put it there is the write that failed), and the retry files a new one. Close
  it before you unhold, or it is orphaned.
- A **reused** issue — one an earlier accepted round already filed for the same
  findings — is on the chain, and the retry reuses it again. Leave it open:
  closing it closes the live residual tracker the node merges pointing at.

### There is no `--auto-merge`

kew deliberately does **not** offer an autonomous, no-human merge mode.
An adversarial design pass found the concept unsound — there is no
forgery-proof way to mark a PR "safe to merge without review" (any label a
dispatched agent's own credential can set is exactly as forgeable as the
signal it would replace), and gating on "required review satisfied" is
self-contradictory (a PR can't be CI-green under a review-required branch
until it's approved, so autonomous merge would have to fake its own
approval). It's also the case kew's own thesis argues against — see
[Philosophy](../philosophy.md): **"Offload the work. Keep the judgment."**

### `kew epic merge` — the operator merges one node

```text
Usage: kew epic merge [OPTIONS] NAME ITEM

  Merge ONE READY node of an epic, on the operator's own authority.

Options:
  --head TEXT         Full 40-character sha of the head to merge.  [required]
  --epic-prefix TEXT  Label prefix identifying epic membership (default: "epic:")
```

The merge pass above needs a verified approval from a **non-author** human with
write permission. In a repository where one person authors every agent PR the
forge cannot produce that approval — GitHub does not let an author approve their
own PR — so the pass correctly never fires and READY nodes wait for a merge that
will never come. `kew epic merge` is the operator performing that merge by hand.

It replaces the forge approval and **nothing else**. Every other invariant the
merge pass proves still has to hold, and each one is proven against exactly the
`--head` you name:

1. `[epic] merge_queue = true`. With the queue off the command **refuses** — the
   queue is the only thing that proves the *combined* tree is green, and without
   it this command would merge a head verified only against itself.
2. `ITEM` is a node of `NAME`, by the epic's own journaled membership — the
   snapshot the driver writes, not a label. A mistyped epic name **refuses**:
   holds are keyed on the epic string as typed, so under a name nothing ever
   wrote the hold gate below would pass vacuously and the merge would be attested
   under an epic the node was never in. An epic with no journaled tick yet
   refuses too — run `kew epic advance` first.
3. The node carries no hold. Release it with
   [`kew epic unhold`](#kew-epic-unhold-release-a-driver-hold) first.
4. The node is still **live work**: the tracker item is not closed, and no
   zero-diff judgment is still deciding what the absence of a diff meant. The
   driver's merge pass only ever considers nodes the overlay reads as
   `in_review` or `held`, so a node reading `done`, `cancelled` or
   `awaiting_no_change` is one it would never merge — and an item a human
   **cancelled** can keep an open, green, `READY`-reviewed PR behind it, which
   every other gate here passes. Merging it would land work your own tracker
   says was abandoned. An item kew could **not read** refuses too: a failed
   tracker read is folded into an empty item state, which the overlay's
   precedence reads as live work, so a tracker outage must not clear this gate.
5. The latest review round is `READY` — or is a `NEEDS_CHANGES` round whose
   residuals the [stop rule](#the-review-stop-rule) accepted — **and** was
   recorded against `--head`. A verdict on another head reviewed other code.
6. The PR is open and its head on the forge is `--head`, read fresh (never from
   a driver tick's batch). An unreadable head refuses; it is never read as
   "unchanged".
7. CI is green on **both** gates the merge pass applies: GitHub's own computed
   `mergeStateStatus`, *and* the head's own check runs. Both, because
   `mergeStateStatus` counts a **skipped** required check as passing and
   private-repository CI skips an agent PR's self-hosted tests until a human adds
   `ci-approved` — read through the first gate alone, a PR whose tests never ran
   looks green. A red, pending, unreadable or never-run check refuses: the
   operator's authority replaces the *approval*, not the tests.
8. The [serial merge queue](#the-serial-merge-queue-opt-in) verifies the combined
   tree **live**, under the repo-wide queue lock: the contained verify command,
   the gate-definition check, and the base-OID re-read that proves nothing landed
   underneath the verification. A green from an earlier tick is not accepted — the
   base moves, and a stale green describes a tree that no longer exists.
9. Every fact another actor can have moved under that verification (which can run
   for up to 30 minutes) is re-proven *inside* the lease, against a fresh read:
   the PR's open state, the forge head, its base branch, both CI gates, the
   item's own live-work state, the hold, and the review verdict. The hold and
   the verdict especially — the driver's sticky `ci_pending` hold and the review stage's rounds are both written without taking the queue
   lock, so a concurrent `kew epic advance` can hold this node, or a concurrent
   review round can land `NEEDS_CHANGES` against this very head, while the tree is
   being verified. The round named in the attestation is the **re-proven** one,
   never a superseded `READY`. Main is read again immediately before the merge.
   A moved main refuses and retries on the next pass. The merge argv binds the
   head at GitHub with `--match-head-commit`, and the merge runs inside the queue
   lease. After the merge, the landed tree must equal the verified candidate
   tree. A mismatch marks main suspect, raises durable attention, and records no
   verified-merge attestation.

No label and no other agent-writable signal contributes to any of it. This is not
an `--auto-merge`: the judgment is the operator's, typed by hand, one node at a
time.

The merge is recorded as an attested `epic_operator_merge` audit event naming the
actor (`KEW_ACTOR`, else the OS user), head, base, PR, verified base, verified
tree, and landed commit.

Every refusal exits **2** and merges nothing, naming which gate refused. The next
`kew epic advance` reconciles the merged PR and closes the item, exactly as it
does for a PR merged out of band.

```bash
kew epic merge auth-rework 87 --head 4f0c9a1b2d3e4f5061728394a5b6c7d8e9f01234
kew epic merge auth-rework SFD-12 --head 4f0c9a1b2d3e4f5061728394a5b6c7d8e9f01234
```

## Hold semantics — who sets and who clears a hold

A stored **hold** is an `epic_holds` row keyed on the epic and work-item id.
While a hold is live it routes the node to **HELD** (see
[The terminal signal](#the-terminal-signal)) — the driver keeps polling that
node instead of dispatching it. `merged_unclosed` is a facts-computed HELD
state. It has no store row. The hold reasons and cleanup paths are:

| Reason | Set by | Cleared by |
|---|---|---|
| `merged_unclosed` | Facts report a merged PR and an open work item. No `epic_holds` row is written. | The merge pass closes the item on this tick. The next tick reads `done`. |
| `ci_pending` | The **merge pass** (`_classify_open`, `graph/driver/merge.py`) when an approved PR isn't CI-green — a real non-green `mergeStateStatus` (`BLOCKED`/`BEHIND`/`DIRTY`/`UNSTABLE`), or a `CLEAN` PR whose head has a failed check, a failed or errored commit status, or no successful check. A transient `UNKNOWN` and checks still running are **not** held; they re-poll next tick. Tests awaiting `ci-approved` and a head with no CI evidence are human gates (`AWAITING_MERGES`), never a hold, and they release a `ci_pending` hold an earlier tick left. | `_do_merge` clears it when CI turns green. `_handle_merged` clears it for an out-of-band merge. The GitHub link-reconcile step clears it for a closed-unmerged winner. That step never clears another hold reason. `kew epic unhold` can also clear it. |
| `budget_preflight:*` | The **dispatch budget gate** (#661) when preflight refuses a node over its cost cap — the reason string carries the observed/cap numbers and the knob to raise. | `_reconcile_budget_holds` at the start of each tick, once the cap admits the node again (raise `budget.<limit>_usd`, or `kew dispatch <n> --force`); or `kew epic unhold`. |
| `budget:*` | The **review stage** when the daily or weekly cap (`budget.daily_usd`, `budget.weekly_usd`) refuses a node's review or remediation before launch (#3205): `budget: daily cap $20.00 reached ($5.00 spent, $10.00 reserved by running or starting runs)`. The reason names the window, its recorded spend and, when other runs are in flight, what they reserve. The refused run's own ceiling is in neither number. No review round, remediation attempt or verdict is recorded. | `_reconcile_window_budget_holds` at the start of each tick, once both caps admit one more review or remediation: at the next UTC midnight, once the week's oldest spend ages out, or when the cap is raised. While a cap still refuses, the hold is rewritten to name the window the check reports, the daily cap first: a `daily` hold the weekly cap still refuses after UTC midnight is rewritten to name `weekly`, and a `weekly` hold is rewritten to name `daily` whenever the daily cap also refuses, even though the week has not moved on. `kew epic unhold` also clears it, but the next launch past the cap is held again. |
| `review_residuals_unfiled` | The **review stage** in **blocking** mode when the [stop rule](#the-review-stop-rule) accepts a round but its residual issue or audit event could not be written, or its ledger mark matches no recorded round. Advisory mode attests `DEGRADED` instead and sets no hold. A mark whose *write* failed is retried on the next tick instead: it sets no hold in either mode, and the merge gate blocks the node until it lands. | `kew epic unhold` only. The next tick retries the rule. |
| `admission_refused:*` | The **detached dispatch child** when admission refuses it before any agent spawns (an unpriced or stale-priced model, a missing credential, a GitHub CLI that is not logged in — run `gh auth login` — or an invalid or expired GitHub token, both `github_auth`, a refused transport route). The reason string carries what the child printed before it exited — its streams are `DEVNULL`, so this hold is that diagnostic's only durable trace. The driver logs it every tick it stays live. | `kew epic unhold` only. Nothing auto-clears it: kew cannot observe a fix for a config it never got far enough to resolve. |
| `admission_refused@<head12>:*` | The **review stage** when a remediation is refused for its PR head's publication lineage (`credential_publication_refused`, see below the table). | The first tick that reads a different PR head clears it, and the new head is reviewed again like any other. `kew epic unhold` also clears it. |
| `base_integration_conflict:*` | The **review stage's merge-main step** (`graph/driver/base_integration.py`) when the base no longer merges into a reviewed PR branch and the governed `kew resolve` run could not resolve the conflicts. The reason names the conflicting paths (up to ten, then a count). | `kew epic unhold` only, after you resolve the conflicts on the branch. Nothing auto-clears it: re-running the same resolution every tick would spend another agent run reaching the same answer. |
| `merge_gate_touched@*` | The **serial merge queue** (`graph/driver/merge_queue.py`) when the PR's diff against the base touches a path in `[epic] merge_gate_paths` — the gate script, the build backend, a `conftest.py`, the container the gates build, a scanner's own allowlist (`.gitleaks.toml`, `mkdocs.yml`). The command is operator-owned; the files it executes are not, so a green from a gate the PR itself rewrote is not evidence. The reason names the head it was written against and the touched paths, and no verify command is run. | `kew epic unhold` only, once a human has read the diff. The release records the head it covers — the queue re-derives this hold from the same diff every pass, so nothing else could clear it — and covers *that head only*: a later push is held again. |
| `publication_held:*` | The **review stage** when a previous remediation for the node's current head exists whose own publication the host **deferred** for a human (a protected path in its diff, or a base sync the remediation made by copying files instead of merging — the fix for that one is `kew resolve`). The remediation's work sits in a preserved checkout, the PR head never moved, and the standing NEEDS_CHANGES verdict therefore still reads as un-remediated — so without this hold the driver dispatches another remediation every tick (#2362 cost three runs and ~$44 on one node). The reason names the preserved checkout. The deferred run itself records `awaiting_publication`, never `success`. | `kew epic unhold`, after you publish that checkout or discard it. Publishing it moves the head, and the next tick reviews the new one instead of reaching this hold. Discarding it deletes the path the reason names, and the driver reads that absence as the discard, so the release sticks. A bare release — an unchanged head and the checkout still on disk — is re-derived and held again, exactly like a base-integration conflict. |
| `test_declarations_missing:*` | The **review stage** when the standing NEEDS_CHANGES round rejected only undeclared edited or removed tests (`undeclared_test_edits` / `tests_removed`), a remediation has already run against the same head and committed nothing, and the PR body still does not declare them (#2872). That remediation's brief asked for a Dispatch Report ending in a **Behavior changes** section, and no code change. The host rebuilds each report line that names exactly one requested test before its ` — ` as `- <node id> — <reason>`, the node id as flagged and the agent's whole reason with its markup escaped, and adds it to the PR body's **Behavior changes** section. A reason that names another node id is refused; a frontend path it mentions declares nothing. The implementer's own lines, shown under **Proposed behavior changes**, are never declarations by themselves. A round counts as asking only for declarations when every finding the reviewer filed carries `"kind": "declaration"` and a `node_ids` list of tests the host flagged. That is the reviewer's structured answer; the host never infers it from where a finding sits or what it says. A finding without that kind, even one in the same test file, sends the round to ordinary remediation. Only the tests those `node_ids` name are requested, so a flag rejected over several edits never waives an edit no finding asked about. A line that names any other test is refused. Matching follows the merge's held-out gate: a line for the test function answers a request for one of its parametrized cases, but a line for one case (`::test_mode[fast]`) never answers a request for the whole function, because the gate would excuse only that case. Every carried line is rebuilt from the requested id itself. The host re-reads the body just before writing and inserts only the missing lines into that latest body, then re-reads it to confirm both the lines and the rest of the body, retrying once. GitHub's pull-request PATCH has no compare-and-swap, so an edit landing between that last read and the write can still be overwritten. This is an accepted residual: it needs a person to save the body in the same second. If an edit you made to the body disappears right after a remediation, re-apply it. A second remediation would end the same way. A PR body the host cannot read is not taken as missing the lines: the node waits and the next pass reads it again, for up to five passes in a row, after which it is held as `pr_body_unreadable`. A late round is never accepted as READY-with-residuals while a finding asks for a declaration, whatever its severity: accepting it would never write the line. The reason names, in full, as many of the tests to declare as fit and counts the rest (`+N more`). The driver's log line and the round's flagged-test record (`<sidecar>.declarations`, beside the stored sidecar) list them all, and so does the console: the hold's needs-you row carries every id in full (`node_ids` on `/api/dashboard`), and Work lists each one under **Declare**, with a button that copies them all. The list is stored with the hold and deleted when the hold is released, so a later hold on the same node never shows an earlier one's ids. | Add one `- <node id> — <reason>` line per named test to the PR body's **Behavior changes** section, then `kew epic unhold`. The next pass finds the lines in the body and re-reviews the head instead of holding it again. A bare release with the body unchanged is held again. If that re-review rejects the same tests although the body declares them, the reviewer disagrees with the declaration itself. The node is then remediated as an ordinary round, with the ordinary brief and within `max_rounds`, and is never held as undeclared again. |
| `remediation_no_change:*` | The **review stage** when an ordinary NEEDS_CHANGES round still stands and a remediation of it already ran to success and changed nothing (#3233). The round stands on the head it was filed against, and on a head that such a remediation published. An operator send-back also stands on any head no agent change has answered (#3276). kew merges the base into a remediation's checkout before the agent starts, so a remediation that changed nothing still publishes a new commit when the base had moved. A head moved by that merge, by a merge-main step or by base integration does not answer a round. The proof is the remediation's completed outcome. It records the tree kew's merge left before the agent ran beside the tree it published: equal means the agent changed nothing. An outcome recorded before kew kept the trees falls back to the head the checkout was cut at beside the head it published. A run with no completed outcome proves nothing. Examples are a publication refused because the branch moved, or an outcome that was never written. Such a run leaves an ordinary round to be remediated as before, and leaves a send-back standing. A second remediation would read the same brief and end the same way, once per tick. Nothing else bounds that loop, because a remediation that changes nothing records no review round. The reason counts the findings still standing and names the first one. It also says when the head has moved since the remediation that changed nothing. A remediation of an earlier round does not count, so the first remediation of every round still runs. A remediation whose publication was deferred is held as `publication_held` instead. | Read the findings, fix the branch or answer them, then `kew epic unhold`. A fix you push moves the head, and the next pass re-reviews it. On an operator send-back, the release is your word that the head the PR stands at when you release it answers the send-back. The reviewer reads that head instead of a remediation running on it. If a base integration moves the PR before the reviewer reads it, the send-back stands again on the new head. A release on an unchanged head allows one more remediation, and the node is held again if that one changes nothing too — unless an approver (`[approval] approvers`; any operator when that list is empty) released a hold set at the round's own head, or at the head a no-op remediation published over a moved base, and either changed the PR body's **Proposed behavior changes** block since the hold or that block already holds a line for every test the round asks about, whoever wrote it: then that head is reviewed once instead, and the reviewer rules on those lines. A second release on a block the reviewer already read at that head buys a remediation, not another re-review. A PR body that cannot be read right after such a release is read again on the next pass rather than spending the release on a remediation, unless no body could earn the re-review: when the hold recorded no proposed block and the round asks for more than lines, the release buys its remediation without reading the body. |
| `remediation_no_progress:*` | The **review stage** when a review remediation ended at the head a NEEDS_CHANGES round stands on and pushed nothing (#3475). No second remediation launches at that head. When the remediation changed the PR body's **Proposed behavior changes** block, the head is reviewed once more instead, so the reviewer can rule on the new lines. When it did not, or when that one re-review still returns NEEDS_CHANGES, the node is held. This catches the runs `remediation_no_change` cannot judge, such as one with no completed outcome. The reason names the head and the round whose verdict still stands. | Read the round, then push a fix or propose lines in the PR body's **Proposed behavior changes** block, then `kew epic unhold`. A fix you push moves the head, and the next pass re-reviews it. An approver's release (any operator's when `[approval] approvers` is empty) on an unchanged head earns it one re-review when the proposed block changed since the hold, or already holds a line for every test the round asks about and the reviewer has not already read that block at that head; the reviewer's ruling on those lines decides the round. Any other release allows one more remediation there. |
| `declaration_record_failed:*` | The **review stage** when a reviewer returned NEEDS_CHANGES on a diff with flagged edited or removed tests, and the round's flagged-test record (`<sidecar>.declarations`) could not be written beside the stored sidecar (#2872). The record is written whole or not at all, and the round is **not recorded** without it: a round standing with no record would read as ordinary findings and be remediated at the same head with the ordinary brief, the loop the declaration path exists to end. The reason names the write error; the driver's log line names the path. The same hold is earned when the reviewer accepted proposed Behavior changes lines and kew could not confirm them written into the PR body (#3475): the verdict is not recorded, so a READY never stands without its lines. | Fix what refused the write (a full disk, a read-only or missing log directory, or `gh` auth for the PR body), then `kew epic unhold`. No round stands at the head, so the next pass reviews it again and writes the record and the lines then. |
| `pr_body_unreadable:*` | The **review stage** when a declaration round's remediation has run and the PR body could not be read on five passes in a row, so whether its **Behavior changes** lines landed is unknown (#2872). Fewer failures only wait, and any successful read starts the count over. | Nothing, usually: the stage keeps reading the body while the node is held and clears this hold itself on the first read that succeeds, then acts on what it read — a re-review when the lines are there, `test_declarations_missing` when they are not. If it persists, check `gh auth status` and the network. |
| `acceptance-disputed` | The **dispatch** of a node whose implementer disputed its acceptance contract instead of editing it. | [`kew epic acceptance amend`](#kew-epic-acceptance-amend-amend-a-nodes-acceptance-contract), by hand or with `--author`; or `kew epic unhold`, which keeps the contract as it stands. |
| `resource_retries_spent:*` | The **epic driver** when a node's run fails as `resource` again after the driver has already spent the [automatic resource retry](#automatic-resource-retries) that run needed: the one retry for the commit the run started from (the reason names that commit), or the node's two retries across every commit. | Find why it runs out of a resource, then `kew epic unhold` and `kew epic retry`. The release leaves the node failed, and the driver does not hold it again for the same run. |
| `pre_review_gate_exhausted:*` | The **review stage**, in blocking and advisory (`[review] blocking = false`) mode alike, when a head fails the [pre-review gate](../configuration.md) (`[commands] static`, or `[commands] ui_check` on the merge candidate) after three remediated gate rounds in one review cycle. The reason names the failing check and its first failing line. No reviewer read the head. | Fix the check on the branch, then `kew epic unhold`, which also restarts the review cycle. |
| `base_integration_failed:*` | The same step when it could not run at all — a refused checkout, a rejected push, a raised seam — and when its bound is spent: three steps in a row reported success at the same head, with no reviewer or remediation run for the node in between, and the head never moved and still does not merge. (A step that moves the head starts the count over.) The branch is unchanged. | `kew epic unhold` only, after you fix what refused the step (or integrate the base on the branch yourself). The released node gets a fresh allowance. |

Governed review remediation also uses `admission_refused:*` for publication configuration refusals and required-containment refusals.
It also holds a node after three identical `credential_publication_refused` refusals in a row before launch (same detail, same head), with the reason `admission_refused@<head12>: credential_publication_refused: <detail>`; the first two retry, since unreadable evidence refuses the same way and can clear.
A credential refusal is about the head's own lineage, so its hold names the PR head that was refused (`admission_refused@<head12>`) and clears itself on the first tick where the PR head differs: a new head is new evidence, so the node goes back through review on it. Refusals on the new head are counted afresh: three identical ones in a row hold the node again under the new head (the first two retry), and a refusal at the branch run cap (below) holds it at once. A hold with no head, such as a configuration refusal or a dispatch child's, still clears only with `kew epic unhold`: a new head would be refused the same way.
A branch has a run cap. The publication guard reads at most 32 runs on a branch, a hard cap because it bounds a security check. From 24 runs, the verdict comment on the PR warns that the branch is nearing the cap. At 32, kew cannot bind another head it pushes there. The next send-back is then refused before launch and held at once, with no retries, under `admission_refused@<head12>: branch_run_cap: ...`, which raises one attention event. The reason names your options: push a fix yourself (the head moves, so the hold clears and the new head is reviewed), merge the PR by hand, or close it.
Containment holds name the missing prerequisite and the `[sandbox] allow_uncontained_flows = true` exception.
The first refusal writes one terminal run row before checkout materialization.
Later ticks skip remediation while the hold exists.
Fix the prerequisite or configure the exception, then run `kew epic unhold <epic> <item>`.

A hold governs **live work**, so it stops governing a node that has landed. If
the tracker has closed the item *and* its linked PR is merged, the node reads
`done` whatever hold row is still sitting on it — nothing clears a hold once
its node stops being dispatched, and a stale one used to outrank the tracker
in `kew epic status`, in every tick, and in the snapshot each tick journals.
The web console applies the same rule to an already-journaled snapshot, from the
merge evidence it has — kew's own `epic_merge` attestations, which the merge pass
writes only after the PR merged *and* it read the item closed — so for an epic
**kew merged**, `/api/epics` and `kew epic status` report the same counts and the
same signal. A merge performed outside kew writes no such attestation, so the
console goes on reading whatever the last tick recorded until a tick observes the
closed item. The console's counts and this command's therefore differ whenever
the tracker moved after the last tick: an abandoned journal whose PRs a human
merged, and a node reopened after the last tick, whoever merged it — the
console still counts that node done off the journal and kew's `epic_merge`
attestation. `kew epic status` reads the tracker live, so it is the surface that
will tell you in the meantime. See
[Why the list agrees with `kew epic status`](../web/index.md#why-the-list-agrees-with-kew-epic-status)
for the full rule and what else does not land a node.

`kew epic unhold` is the manual escape hatch for **either** row when a hold is
stuck and no automatic path will clear it — see
[`kew epic unhold`](#kew-epic-unhold-release-a-driver-hold). It is also the
*only* operator command that clears one:
[`kew epic retry`](#kew-epic-retry-re-admit-a-failed-node) refuses a held node
(exit **2**, naming the reason) instead of re-admitting a node whose hold would
still wedge it.

## The terminal signal

Every tick (`advance`, and `status`, which never dispatches or merges) reports one of these
signals:

| Signal | Meaning |
|---|---|
| `complete` | Every node is DONE. |
| `progressing` | Something moved this tick (a dispatch or a live in-flight run), **or** at least one node is in the governed-review lifecycle (`adversarial_review` / `remediating` — a reviewer or remediator is real compute in flight), **or** at least one node is HELD on `ci_pending` within its stall clock — an approved PR waiting on CI is legitimate progress, not a stall. |
| `awaiting_merges` | The frontier is empty and nothing is in-flight, but the only remaining work is one or more PRs open in review **awaiting a human's merge** (and nothing is stuck) — the normal human-gate state, not a broken pipeline. A running driver keeps polling and observes the merge on a later tick. `advance` reads the PRs off its merge pass and `status`, which runs no merge pass, reads them off its in-review waiting rows, but both then apply the same **only** test, so one epic state never gets two readings: every other waiting row must be a node blocked behind one of those PRs, directly or through another such node. A PR the merge pass held only because GitHub has not finished computing its mergeability (`mergeability-unknown`), or because the head's own checks are still running or could not be read (`ci-pending`), counts as one of those PRs: neither writes a hold row, so neither has a stall clock, and it is the same node `status` sees as plain `in_review`. A node an earlier red-CI pass already holds on `ci_pending` is **not** one of them even while its checks read pending (`ci-pending-held`): that hold row, and the stall clock on it, stay — so the node reads `progressing` inside its `stall_days` and `stalled` once past it, exactly as `status` reads it. An epic that also needs `kew epic publish`, `kew epic continue`, a no-change decision or a human arm reads `stalled`, and so does one whose remaining node is blocked on an issue **outside the epic** — merging the PRs cannot clear that, and so does one held by a merge-queue refusal an operator must fix (an unverifiable head, a refused containment posture, a gate that could not run). |
| `stalled` | The frontier is empty, nothing is in-flight, no review or remediation is running, no hold is live, and not everything is DONE — remaining work is FAILED, CANCELLED, or stuck and needs an operator. |

`kew epic run` uses this signal to decide whether to keep polling (`complete`
→ exit 0, `stalled` → exit non-zero, `awaiting_merges`/`progressing` → keep
going). `awaiting_merges` prints a non-alarming line listing the pending PRs;
a `stalled` tick exits **1** when a node needs operator repair — see
[Exit codes](#exit-codes).

## Fleet isolation

The fleet dispatcher never selects an epic-labeled issue, even if it also
carries `ready` — epic membership is a positive exclusion in the fleet's
issue selector, and it also excludes epic PRs from the auto-conflict-resolver
so the fleet can't force-push a branch the driver is tracking. Epic nodes are
**driver-owned**: only `kew epic advance`/`run` ever dispatch or merge them.

## Config

See [`[epic]`](../configuration.md#epic) in Configuration for `max_parallel`,
`stall_days`, `approvers`, and `lock_path`. Merges are always squash merges.

## Tick journal

Every `kew epic advance` and `kew epic run` tick appends a **changed-only
snapshot** of the driver state to the `epic_ticks` table in `dispatch.db` —
capturing the signal, per-node statuses, counts, edges, and frontier, plus the
actions taken that tick (dispatched / merged / held / stuck) and a hash of the snapshot
state. The driver also stamps the `epic_driver_state` heartbeat table twice per
tick: once at the start of the tick (after tool preflight succeeds, inside the
single-driver lock) and once at the end of the tick (still inside the
single-driver lock), recording the tick's duration, its terminal signal, and a
reference (`last_tick_id`) to the corresponding `epic_ticks` row.

A tick whose state matches the latest row appends nothing, so the journal's
row count (the CHANGE column) counts changes, not ticks. When each node's
merge state was last read from GitHub is not part of that state: the hash
leaves it out, and the driver records it in the mutable
`epic_merge_state_reads` table instead. Journal rows are never rewritten.

`kew epic status` and the read-only commands (`graph`, `validate`) never
journal — they only read the driver's current view without persisting history.

The journal feeds the web UI's `/epics` endpoint (Phase B/C) to display a
time-lapse replay of the epic's progress across ticks. Journal rows are never
pruned in v1 — the full history remains for audit and replay. See the web
view: [../web/epics.md](../web/epics.md).

## Related

- [How It Works: the epic driver tick](../how-it-works.md#the-epic-driver-tick)
- [Epic DAG design](../design/2026-07-01-epic-dag-design.md) — the original phased design
