---
description: What an agent can write, what kew reads instead before a change can merge, and the code and tests behind each rule.
---

# How kew decides a change is proven

kew treats the agent that writes a change as an untrusted author. What the
agent produces is input for kew to check. It is not evidence that the change is
correct. The rule that follows from this: **a change never defines its own
proof.**

This page lists what an agent can write, and what kew reads instead before the
change can merge. Each section names the code and the tests, so you can check
the claim against the source.

!!! note "What applies where"
    The publication rules apply on the contained dispatch route. The review
    rules apply when `[review] adversarial = true`. The merge verify
    ([`kew verify`](cli/verify.md)) is wired to kew's own repository layout
    today. The [threat model](threat-model.md) lists what kew does not defend.

## The agent's repository

An agent can write anything under its checkout. That includes `.git`: hooks,
config, replace refs and a push URL.

To publish, the host does not run Git in that directory. A second container,
with no network and no credentials, reads the checkout and copies the changed
files out as plain bytes. The host checks each file, rebuilds the commit in a repository that only the host has
touched, and scans every object the push would send for the credentials it
holds and for known key shapes. Then it pushes one commit to the branch the
operator configured.

- **Code:** `kew/src/kew/publication_canonical.py`, `publication_filter.py`,
  `publication_scan.py`
- **Tests:** `kew/tests/test_host_git_boundary.py` records every Git call at
  the one subprocess entry point and fails if a publication flow resolves one
  inside the agent's checkout. `kew/tests/test_publication_output_scan.py` covers the scan.
- **Limit:** this covers publication on the contained route. Other flows run
  hardened, read-only Git in a checkout, for example to see whether it has
  uncommitted changes. A run with
  `[sandbox] dangerously_skip_containment = true` executes as you, on your
  machine. The scan is a backstop: it matches values and shapes, so an
  encrypted or split value passes it.

## The agent's tests

An agent that edits a test can make a broken change pass.

The merge verify runs the base branch's copy of each test file the pull request
changed, against the pull request's source, in a directory outside the
checkout. It uses the base branch's pytest configuration and plugin pins, and
kew's own result reporter. A base test that fails blocks the merge.

The tests are not hidden. The agent can read them. It cannot edit the copy that
runs.

- **Code:** `kew/src/kew/merge_verify.py`, `held_out_selection.py`,
  `held_out_report_plugin.py`
- **Tests:** `kew/tests/test_held_out_base_tests.py`,
  `kew/tests/test_held_out_runs_load_only_pinned_plugins.py`
- **Limit:** only the base copies of files the pull request changed run, or the
  whole base tree when a shared test file changed. A test the pull request adds
  has no base copy. The candidate's code runs in the same process as the tests.

## The agent's reasons

Some changes must change what a test expects. kew asks for a declaration: a
line under **Behavior changes** in the pull request body that names the test
and gives a reason.

The agent cannot write that line. kew places the agent's lines under
**Proposed behavior changes**, a heading no gate reads. The reviewer rules on
each proposal, and kew writes only the accepted lines under **Behavior
changes**.

kew also computes what the diff did to its tests: asserts removed, tests
removed, skips added, expected values changed, or a fix round that touched only
tests. The reviewer's `READY` is refused until each flag has an answer.

- **Code:** `kew/src/kew/pr_text.py`, `behavior_declarations.py`,
  `test_integrity.py`, `review_integrity.py`
- **Tests:** `kew/tests/test_test_integrity.py`
- **Limit:** the flags are patterns over the diff. They show shapes, not
  intent. The reviewer is a model, and kew records its answers without running
  them again.

## The reviewer's verdict

The reviewer runs the pull request's tests. So the code under review can write
any file in the reviewer's checkout.

kew reads no verdict file. The verdict is the reviewer's own final result
event, which the host reads from the runner's output stream. There must be
exactly one, it must come last, it must name the head commit kew recorded, and
the reviewer's process must succeed. Anything else fails the round.

The reviewer's instruction files (`CLAUDE.md`, `.claude/`, `.mcp.json`) come
from the base branch when the round starts, so the pull request cannot commit a
change to them.

- **Code:** `kew/src/kew/review_stream.py`, `review_contract.py`,
  `review_dispatch.py`
- **Tests:** `kew/tests/test_review_verdict_forgery.py`
- **Limit:** only `claude-code` can be the reviewer. The reviewed code runs as
  the same user as the reviewer, so the stage is not yet safe on pull requests
  from untrusted forks. [Security](security.md) describes the residual.

## The gates themselves

A pull request can change the checks that judge it: a lint config, a lockfile,
an ignore file, a workflow or a debt ledger.

`[epic] merge_gate_paths` lists those files. The list includes config files a
tool would read if they appeared, such as a root `ruff.toml` that does not
exist yet. A pull request that touches one needs two independent full reviews
on the same commit, and the merge queue holds it until a person releases that
commit.

- **Code:** `merge_gate_paths` in `kew/src/kew/config.py`, `review_delta.py`,
  `epic_holds.py`
- **Tests:** `kew/tests/test_merge_gate_paths.py` asks the gate runners which
  files they read, so the list cannot fall behind them.
- **Limit:** the list is per project. The merge queue is opt-in.

## The agent's cost report

A contained agent writes the output stream that kew meters.

kew prices the cost cap from per-turn usage that only increases. A summary line
can correct the reported total. It cannot lower the figure the run's cap uses, and a
negative step cost is rejected. A governed dispatch refuses a model kew cannot
price.

- **Code:** `kew/src/kew/dispatch_sandbox.py`, `pricing.py`
- **Tests:** `kew/tests/test_contained_budget_cap.py`
- **Limit:** a contained Codex run reports usage only when it ends, so kew
  cannot stop it mid-run.

## A run that changes nothing

An agent that changes nothing has not shown that the issue needs no change. kew
seals the empty result against the base and holds it until policy or a person
confirms it.

- **Code:** `kew/src/kew/no_change.py`, `no_change_seal.py`
- **Tests:** `kew/tests/test_no_change_seal.py`

## What this does not prove

- A passed gate shows that the checks ran and what they found. It does not show
  that the change is correct.
- The [audit chain](audit.md) shows that recorded rows were not edited. It does
  not show that every event was recorded.
- Governed review is opt-in, and the reviewer is a model.
- kew binds a merge to the commit that was reviewed. A person still approves
  the pull request.
