# Spec — containment posture and the publication broker (W1.1 / S1)

Dated: 2026-08-06 (rewritten 2026-08-07 after adversarial review)
Status: **REWRITTEN DRAFT — for operator review. Nothing armed.**
Severity: **Critical — container→host code execution, plus three uncontained
flows with host write access to the operator's own `.git`.**

> **This spec replaces a draft that was wrong.** Two adversarial reviews (one
> security, one feasibility) broke it empirically. The corrections are recorded
> at the end; the short version is that the original design (a) still executed
> host git inside the agent's repo, which is exploitable via `core.fsmonitor`
> and `filter.*.clean` regardless of the hardening flags I specified, (b) used
> an extraction model that captures nothing in the normal case, because agents
> deliver **commits**, not working-tree edits, and (c) scoped the fix to one
> door when there are four.

## Problem — four doors, not one

**Door 1 (contained flows).** The sandbox mounts the checkout writable,
`.git` included (`kew/src/kew/sandbox.py:637-638`), and the host later pushes
from that same directory (`kew/src/kew/dispatch_core.py:2466-2472`). A
prompt-injected agent plants `.git/hooks/pre-push`, `core.hooksPath`,
`core.fsmonitor`, a `filter.*` driver, or a `credential.helper`, and the host's
git executes it **as the operator**. Only sandboxed dispatch and the sandboxed
graph reviewer are containerized.

**Doors 2-4 (uncontained flows).** Review remediation, resolve, and
non-sandboxed dispatch run the agent **directly on the host**, in *linked
worktrees* that share the operator's main `.git` (`worktree.py:246-257`,
`review_checkout.py:215-243`). An agent there writes host `.git/config` and
`.git/hooks/` directly — traps that fire on the **operator's own interactive
git commands**. A publication broker does not close these.

**The silent trapdoor that connects them.** `sandbox.enabled` defaults to
`"auto"`, which **downgrades to uncontained host execution** when no runtime is
present (`config.py:480-494`, `dispatch_core.py:3988-3994`). Users can land in
the unsafe mode without choosing it.

**Adjacent host-execution surface (in scope to name, not to fix here):**
`loops/verification.py:104-110` runs `sh -c "<verify command>"` with
`cwd=<agent worktree>`, and `worktree.cleanup_worktree` feeds an
agent-controlled branch name to a destructive host `git branch -D`.

**No second layer.** The in-container guard has zero `.git/` rules — verified.

## Decision (operator, 2026-08-07): posture B

**Fix all four doors, and make the unsafe path a named, loudly-warned choice
rather than a silent fallback.**

- Every flow that *can* be contained, is.
- An uncontained mode remains supported — for no-Docker and local-model users —
  but it is **explicitly chosen**, named, and warned about at the point of use.
  A person opts into it and can explain it to their own security reviewer.

**Naming (operator, 2026-08-07): follow the `--dangerously-skip-permissions`
convention, not "yolo mode."** Working names: config
`[sandbox] dangerously_skip_containment = true`, CLI
`--dangerously-skip-containment`. Rationale: (a) "yolo mode" is **Codex's own
term**, and kew drives Codex as a runner — one session would carry two
different yolo modes; (b) the danger travels *in the name*, so even a
copy-pasted config line or a pasted command warns the next reader; (c) it
matches the convention kew's users already know from Claude Code. The framing
is coherent with what kew already does: the agent runs with permissions
skipped **because the container is the boundary** — skipping the container too
is the dangerous part, and the flag says so.
- Rejected: mandatory containment everywhere (A). It would make Docker a hard
  requirement and worsen an install path already at ~3-4.5× reference friction.
  **Revisit after the program's friction work (B3/B5/B6/B7) lands** — "every
  agent is contained, no exceptions" is a stronger sentence to publish, and it
  would retire the Lemonade question by deciding it.
- Rejected: document-and-ship (C). It permanently forfeits the containment
  claims already withdrawn from the marketing copy.

## Design

### D1 — Explicit posture, no silent downgrade *(do first, smallest, highest value)*

`sandbox.enabled = "auto"` must never resolve to host execution. When no
runtime is present, kew **refuses** and names the two ways forward: install a
runtime, or explicitly set `dangerously_skip_containment`. That mode is
announced with a banner at every dispatch, recorded on the run record and in
the audit event (so the ledger shows which runs were uncontained), and
documented as "this gives the agent your machine."

### D2 — Contain the flows that can be

Review remediation and resolve remediation move onto the containerized path
(they already have a containerized sibling in the graph reviewer). Flows that
cannot be contained — local runners exposing a host shell (Lemonade) — are
**`dangerously_skip_containment`-only**: they refuse to run in a posture that
claims containment.

### D3 — Extraction happens inside the container; the host reads bytes

The host never points git at an agent-writable directory — **enforced by
construction, not by flags**. The container (where untrusted code already runs,
so this grants the attacker nothing new) produces:

- a patch over the host-recorded base SHA — `--binary --full-index`, and
  `--attr-source=<trusted base tree>` so in-tree `.gitattributes` cannot steer
  the diff — **and/or** the commit range `<base>...HEAD`, since the normal
  delivery is N commits with a clean tree;
- a manifest: file inventory, modes, and a content hash computed by the host
  over the artifact.

The host reads that artifact **as opaque bytes**. It never runs `git` with a
repository that resolves inside an agent path — read or write.

**Why not host-side hardened reads** (the original design): verified
exploitable. `core.fsmonitor` and `filter.*.clean` execute under
`GIT_CONFIG_NOSYSTEM` + `GIT_CONFIG_GLOBAL=/dev/null` + `core.hooksPath=/dev/null`
+ `--no-ext-diff --no-textconv`; repo-local `.git/config` cannot be suppressed
(there is no `GIT_CONFIG_NOLOCAL`) and `include.path` extends it arbitrarily.
Enumerating vectors there is the denylist-on-an-evolving-surface pattern this
project rejects elsewhere.

### D4 — Publish from a fresh host-owned clone

Validate the artifact, apply it into a clone **the agent has never touched**,
commit and push from there. The clone's source must not be a repo any agent can
write (`uploadpack.packObjectsHook` is honored from the *source* config, so
"clone from the local repo" is only safe once D1/D2 hold).

**Filter — reject loudly, never silently drop:** anything matching `.git` at
any depth (case-insensitively, after Unicode normalization, including
`.git~1`/trailing-dot/space evasions); symlinks, gitlinks, devices, fifos,
hardlinks; path traversal and absolute paths; control characters in names;
mode normalization to `100644`/`100755` only, with 644→755 escalation rejected
unless allowed; case/normalization collisions against each other and against
destination paths; `.gitattributes` entries introducing `filter`/`diff`
drivers. **Flag for human review rather than auto-publish:**
`.github/workflows/**` and `.github/actions/**` (CI code execution with repo
secrets), `.pre-commit-config.yaml`, `.husky/*`, `conftest.py`, `Makefile`,
`package.json` scripts, `setup.py`, `.envrc`.

### D5 — Shadow mode before flip, kill switch after

Build the fresh clone and compare its tree SHA against the legacy path's,
**publishing from the legacy path**, logging divergence, across N real
dispatches. (`flight/snapshot.py:31-51` already computes worktree tree SHAs.)
Flip behind a default-off flag with a config kill-switch that reverts without a
code change.

## Non-goals

Preventing prompt injection (unchanged threat-model non-goal). Making the
in-process guard authoritative. Fixing `loops/verification.py` host execution
or the `cleanup_worktree` branch-name path — **named here, specced separately**.

## Decomposition (replaces "one L node")

| # | Node | Size | Note |
|---|---|---|---|
| 1 | **D1 explicit posture + `dangerously_skip_containment` flag, banner, audit fact** | S | Ship first; removes the trapdoor on its own |
| 2 | Inventory every host git write and its cwd; land the criterion-1 assertion so it **fails today** | S | Makes the problem measurable before it is fixed |
| 3 | **D2 containerize review + resolve remediation**; Lemonade → `dangerously_skip_containment`-only | L | Without this the broker is decorative |
| 4 | D3 extraction + D4 filter as a standalone module, **no callers**, tested per class against a corpus of real merged agent branches | M | |
| 5 | **D5 shadow mode** on sandboxed dispatch | M | The only honest proof no file class is dropped |
| 6 | Flip sandboxed dispatch behind a default-off flag, preserving #958 / #1290 / rescue / continuation semantics | M | |
| 7 | Own spec: **resolve** (merge commits + force-push rewrites cannot be expressed as a diff) | M | Separate; must not block 1-6 |
| 8 | Own spec: **ci-fix** (has no host publication today — the agent pushes from inside) | M | Separate |

## Acceptance criteria

1. **No host git process — read or write — resolves a repository inside an
   agent-writable path.** Enforced by a subprocess-level assertion, not review
   of known call sites. *(The original "no host git **write**" wording passed
   while the read-side RCE was live.)*
2. **Env scrub**: no git process whose repo resolves inside an agent path
   carries `GH_TOKEN`, `GIT_CONFIG_*`, or the publication token in its
   environment. *(Today the token is injected as `GIT_CONFIG_KEY_*`, which any
   `fsmonitor`/filter child would inherit.)*
3. **Hostile-fixture suite, parametrized — not a hand-listed set**: planted
   `pre-push`, `core.hooksPath`, `core.fsmonitor`, `filter.*.clean`,
   `include.path`-delivered config, `.gitattributes -diff` (silent content
   drop), `.gitignore`-hidden untracked files, `.git`-as-gitfile redirection,
   and repo-discovery walk-up. Each asserts a sentinel **did not** fire and
   content was **not** silently altered.
4. **Fail closed on a malformed agent repo**: `.git` missing / a file / a
   symlink, or toplevel ≠ checkout dir → publication aborts with a typed error,
   never diffs a different repository.
5. **Integrity**: published content is byte-identical to what the container
   produced, against a hash the **host** computed over the artifact.
6. **Empty extraction is a hard failure, never a no-change completion.** If the
   container reports changes and extraction yields nothing, abort. *(Today the
   zero-change path calls `mark_completed_no_changes` **and**
   `_auto_unblock_dependents` — a silent drop would discard the work, close the
   issue, and unblock its dependents.)*
7. **Posture**: with no runtime present, dispatch refuses rather than
   downgrading; uncontained execution requires `dangerously_skip_containment`
   explicitly set, emits a banner, and writes an audit fact naming the run as
   uncontained.
8. **Preserved behaviours** with named tests: zero-change (#958),
   `PublicationHeadMovedError` / head-binding (#1290), runner-self-publish
   detection, dirty-worktree preservation and rescue (#1286 class),
   continuation republication, `_agent_branch_start_point` reuse.
9. **Coverage assertion**: a test enumerating every `git push` / `git commit` /
   `gh api …/pulls` call site that fails when a new one appears outside the
   broker. *(Three independent push implementations exist today; this is what
   stops a fourth.)*
10. Containment CI gate extended to the git-metadata path — the withdrawn
    containment claim is re-made **only** once this exists.

## Corrections carried from the adversarial reviews

- Dropped the "clone copies a credential-bearing HTTPS remote" claim: origin is
  SSH and the credential is injected at push time, so it was wrong for this
  codebase.
- "Shared broker" was wrong: there are three independent push implementations
  (`dispatch_core._push_and_open_pr`, `worktree.publish_existing_pr_branch`,
  `resolve._push_clean_integration`) and ci-fix has no host publication at all.
- `gh pr create` does not appear in this codebase; PRs are created via
  `gh api POST repos/{repo}/pulls`.
- Effort "L" was wrong for the full scope; it is the decomposition above.
- Extraction must not use `--exclude-standard` (agent-written `.gitignore`
  hides files) and must not trust the agent repo's refs for the base commit.
- `sandbox.py` passes no `--user`; container-created file ownership needs
  addressing or the macOS-only assumption stating.

---

## Amendment — 2026-08-08, after building nodes 1-3

Re-estimated from what the first three nodes actually cost. Two things the
original decomposition got wrong, both discovered by building it.

### Correction 1: doors 2-4 is five flows across three primitives, not two

The spec named `worktree.py:246-257` and `review_checkout.py:215-243` as the
uncontained flows. That undercounted. **Three** primitives create linked
worktrees — `setup_worktree` (`worktree.py:241`), `checkout_existing_worktree`
(`:295`), and `reuse_or_restore_worktree` (`:490`) — and every one hands an
agent a checkout whose `.git` is the operator's:

| Flow | Primitive | Status |
|---|---|---|
| review remediation | (its own) | **fixed** — #1454, standalone clone |
| resolve remediation | `checkout_existing_worktree` | open |
| ci-fix | `checkout_existing_worktree` | open |
| dispatch, non-sandboxed | `setup_worktree` | open |
| loops (delegate + resume) | `setup_worktree` / `reuse_or_restore_worktree` | open |

A note recorded during node 3 said `checkout_existing_worktree` was shared with
the dispatch path. It is not — dispatch uses `setup_worktree`. The practical
consequence is the opposite of reassuring: fixing one primitive does not reach
the other two, so this is three changes, not one.

The exposure is identical in each: an agent runs `git config core.hooksPath`,
it lands in the operator's `.git/config`, and the trap fires on the operator's
own next `git commit`. No kew dispatch required to trigger it.

### Correction 2: node 3 was one row and is at least four changes

Shipped so far:

* **3a** — uncontainable runners refuse explicitly (#1453). Also unbroke
  lemonade, which could not dispatch at all on a host with a runtime.
* **3b** — review remediation gets a standalone clone (#1454). Cost three
  follow-on fixes found by three different mechanisms: the reuse fetch (suite),
  the lost committer identity (CI), the hardlinked object store (review).

Remaining: **3c** the other four flows above, **3d** containerize execution for
review + resolve — which is what D2 originally meant, and has not started.

### Revised table

| # | Node | Was | Now | Status |
|---|---|---|---|---|
| 1 | D1 explicit posture | S | S | done (#1423) |
| 2 | Criterion-1 assertion | S | S | done (#1452) |
| 3a | Uncontainable runners explicit | *(in 3)* | S | done (#1453) |
| 3b | Review remediation → standalone clone | *(in 3)* | M | done (#1454) |
| 3c | The other four flows → standalone clones | *(in 3)* | M | done (#1457, #1458) |
| 3d-1 | Posture attested on both remediation flows | *(in 3d)* | S | done (#1460) |
| 3d-2 | Contained-execution seam, no callers | *(in 3d)* | M | done (#1461) |
| 3d-3a | `run_agent` can execute in a container | *(in 3d)* | M | done (#1463) |
| 3d-3b | Wire review + resolve onto it | *(in 3d)* | M | done — **node 3 COMPLETE** |
| 4a | D4 filter, no callers | *(in 4)* | M | done |
| 4b | D3 artifact reader, no callers | *(in 4)* | M | done |
| 4c | D3 in-container producer | *(in 4)* | M | done (#1474) — **node 4 COMPLETE** |
| 5a | Tree applier, no working tree | *(in 5)* | M | done (#1475) |
| 5b | Extraction in a second container | *(in 5)* | M | done (#1476) |
| 5c | Compare against a host-owned clone | *(in 5)* | M | done (#1478) |
| 5d | Wire into dispatch, default off | *(in 5)* | M | done (#1481, #1482) — **node 5 COMPLETE** |
| 6 | Flip behind a default-off flag | M | M | **next — gated on shadow data** |
| 7 | Own spec: resolve | M | M | separate |
| 8 | Own spec: ci-fix | M | M | separate |

Node 3 as written was "L". Measured, it is S+M+M+L. That is the estimate error
worth carrying forward, not the individual sizes.

### Why the estimate moved

Every node so far produced defects outside its own scope — a live lemonade
break, an identity loss, a shared object store, #1455. That is the method
working, but a node that keeps producing findings cannot be estimated as nearly
finished. **Treat remaining sizes as lower bounds.**

### Unplaced work

* **#1444** — `loops/verification.py` runs `sh -c` on the host in an agent's
  tree. Same family as 3c; the flows overlap.
* **#1445** — `cleanup_worktree` feeds an agent-controlled branch name to a
  destructive host `git branch -D`.
* **#1455** — a merge that could not run is reported as conflicts with zero
  conflicted files. Surfaced during 3b; governs the resolve path 3c touches.

All three sit on surfaces 3c and 3d are about to change. Deciding whether they
ride along or stay separate is cheaper before 3c than after.

### Node 3d-3: the decision, and the evidence that made it

Slice 3 looked like a wiring change and is not. The two flows already have a
lifecycle — `run_tracked_agent` owns retry, budget, cost, pause, worker cap and
finalization — and the contained path has a **parallel** one
(`_record_sandbox_start` / `_stream_sandbox_log` / `_finalize_sandbox_run`).
Two options:

* **(a)** replicate the contained lifecycle for review and resolve.
* **(b)** teach the existing lifecycle to swap its *execution step* for a
  container, keeping everything around it.

(b) was the better architecture on taste. It became the only defensible option
on evidence: **(a) would silently delete `max_remediation_usd`.**

The contained path enforces no per-run budget cap, no mid-run budget kill, no
breaker and no pause — only an admission gate that runs once, before the agent
starts, and takes no cost argument. `max_remediation_usd` (#993) is precisely a
runtime kill cap, passed as `budget_override_usd` at `kew/src/kew/review.py:762`, because a
remediation can legitimately cost more than an implementation run. Copying the
contained lifecycle would trade the only cost control these flows have for a
containment boundary. Full finding: **#1462**.

The mechanism for (b) is smaller than the framing suggests, because the
execution step is a single call — `run_agent(...)` in `_run_retry_attempt`, at
`kew/src/kew/dispatch_core.py:1208`. Underneath, `_run_process` does one `popen(cmd)`. For a
contained run, `cmd` is the container argv. The only genuine impedance is the
kill: killing the local `docker run` client does not stop the container, so
termination has to go through `docker kill <name>`.

That already exists. `run_agent` takes `container_handle`, threads it to
`_terminate_group`, and calls `container_handle.kill()`. It has no production
caller — the contained path never goes through `run_agent`. `ContainerHandle`
needs only `(proc, name, runtime)`, and `launch_container` shows both `name` and
`runtime` are recoverable from the argv, so the handle can be constructed inside
`_run_process` after `popen`.

So (b) is: build the container argv, hand it to `run_agent` as the command,
construct the handle from it. Budget, breaker, pause, retry, and
`DispatchResult` accounting then apply to contained runs unchanged, and a
tested-but-dead kill path becomes reachable.

**Scope held to the remediation flows.** Converging sandboxed *dispatch* onto
the same path — and retiring `dispatch_sandbox`'s duplicate cost accounting,
whose own docstrings twice say it "mirrors `DispatchResult.compute_cost`" — is
issue #1462, not this node.

### What the wiring cost that the design did not predict

Two things surfaced only once review and resolve were actually moved.

**The cap would have been deleted by the move.** `max_remediation_usd` reaches
the agent as `--max-budget-usd` in its argv, and `container_args` omits that
flag deliberately. Neither flow passed a kew-side `budget_check`, so the cap was
enforced *by the agent*, and — because the flag is gated on
`runner.name == "claude-code"` — **only for claude-code**. Containing these
flows would have removed the only cost control they had, while reading as a
pure relocation. `budget_watchdog` now decides by the real condition: kew skips
its own watchdog exactly when the agent self-caps, which is claude-code **on
the host**. Contained runs are watched, and the stop reaches the container by
name.

**The attestation function computed its own subject.** `attest_flow_posture`
derived the posture and hardcoded the reason to "has no contained path" — true
when written, false the moment these flows gained one. It now records the
decision the caller made. *An attestation that derives what it attests will
eventually attest something that is no longer so.*

`run_remediation_contained` (#1461) is deleted. It was written before measuring
what a parallel contained lifecycle costs; execution now lives in `run_agent`,
and keeping a second launcher would have been the duplication this node exists
to avoid. `resolve_remediation_containment` — the decision — stays.

### Node 4b: the artifact format, and why content is content-addressed

D3 said "the host reads that artifact as opaque bytes". Building the reader
forced the format, and one decision carries most of the security:

    <artifact>/manifest.json      {"base": "<sha>", "entries": [...]}
    <artifact>/blobs/<sha256>     content, addressed by its own hash

**Content is stored by digest, not by path.** The manifest's paths are
attacker-controlled; if the artifact laid content out at those paths, merely
*unpacking* it would already have written wherever the agent chose, before the
filter had an opinion. Content-addressing makes the path pure data — carried,
filtered, and only turned into a filesystem operation after `filter_manifest`
approves it. The reader therefore opens exactly one shape of name, 64 hex
characters, and a manifest path never reaches an `open()`.

**The host re-hashes.** The manifest is written inside the container, so its
`sha256` fields are claims. The host recomputes and compares; an unreferenced
blob is refused rather than ignored, because "skip what we did not expect" is
how a second payload rides along.

**The artifact directory is agent-writable too** — it is produced into the
mounted checkout. So `manifest.json`, each blob, and `blobs/` itself can each be
a symlink out of the container's world. Every open is `O_NOFOLLOW` and every
directory is `lstat`-checked. This is the same defect class as the prompt-handoff
symlink (#1463 review) and the hardlinked object store (#1454 review), which is
three instances now: **anything an agent can write is a name the host must not
follow.**

Writing the tests found a crash-instead-of-refusal in the reader: `mode in
ALLOWED_MODES` raises `TypeError` on an unhashable value, so a manifest with
`"mode": {}` crashed rather than being refused — at the exact boundary whose
job is to turn malformed input into a refusal.

### Node 5: a feature whose default config cannot run it is broken, not off

Shadow mode compares in-process and publishes nothing, so it was built to be
unable to break a dispatch: every exception at the seam is caught and logged.
That property is what made the last defect invisible. On kew's own config
`project.repo` is empty, the canonical fetch had nothing to resolve, and every
comparison would have logged `failed` — indistinguishable, in the log line,
from *the broker is wrong*. The whole point of shadow mode is to produce
evidence about the broker; a silent `failed` for every dispatch would have been
read as that evidence. Fixed in #1482 by falling back to the **operator's**
`origin`, never the agent's: reading the agent's remote would let a dispatch
point the canonical fetch at a server it controls.

It was found only by running the hook in situ against the real config, real
image and real remote. The unit suite could not find it, and one of its tests
was passing for the wrong reason — this machine's `project_root()` happens to
have no `origin`, so the missing-repo test exercised the crash path by accident
rather than by construction.

Both images (`agent`, `agent-opencode`) now carry the producer, and both
produce `match` on a synthetic agent branch with an edit, an addition and a
deletion. That is the pipeline proven, not the coverage: one branch exercises
three file classes. **Node 6 is gated on shadow data from real dispatches**,
because flipping on evidence not yet collected is exactly what building shadow
mode was meant to avoid. The first expected divergence is a **symlink** — the
producer carries only regular files, which is a known capability gap rather
than a defect, and the case node 6 has to decide.

### Follow-up unlocked

`ReviewCheckout.detached` is now conservative rather than necessary *for the
reason it was written* — a standalone clone holds any branch regardless of what
the operator's worktrees have checked out, so detaching no longer prevents a
checkout collision.

**It is not a clean deletion, and this note previously said it was.** `detached`
acquired a second job: it is the only reason the host publishes a remediation
for an agent-style runner (`review.py:818`, `host_publication or
checkout.detached`). Only `codex` is `publication_style == "host"`; claude-code
and every other runner are `"agent"`. So today, who publishes a remediation
depends on whether the operator happens to have that branch checked out —
worktree present, the host publishes; absent, the agent does. Deleting the flag
would move that publication from the host to the agent in exactly the case that
currently gets the safer path, which is posture B in reverse.

The underlying defect is that a posture varies with local state nobody chose it
to vary with. **#1485** carries the decision; it edits the same condition node 6
does, so it belongs with node 6 rather than before it.
