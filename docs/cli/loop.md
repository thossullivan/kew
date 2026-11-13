# `kew loop` — Loop management subcommands

```text
Usage: kew loop COMMAND [ARGS]...
```

!!! warning "Agent containment"
    Kew has no contained loop launch, so a loop's agent runs on the host.
    A runner that contains itself, such as official `codex`, runs under its own boundary.
    Any other runner needs `[sandbox] dangerously_skip_containment = true`.
    Without it, `kew dispatch --loop` and `kew loop resume` refuse with exit code 2.
    The refusal names the flag and comes before any checkout, agent work, or spend.
    It records a `refused` run, a `sandbox_resolved` event with `posture = "refused"`, and a `dispatch_completed` event.
    An admitted host loop prints the uncontained banner before checkout.
    Its `sandbox_resolved` event records `posture = "uncontained"` and `admission = "dangerously_skip_containment"`.
    The finding is #1977.

!!! note "Command verification containment"
    Command verification runs contained when prerequisites are present.
    Prerequisites are a local runtime and an approved image for the loop's runner.
    Kew resolves `[sandbox] images.<runner>`, or `[sandbox] image` for `claude-code`, to an immutable local image ID.
    The host path requires `[sandbox] dangerously_skip_containment = true` and missing prerequisites.
    `[sandbox] enabled = true` refuses missing prerequisites with exit code 2.
    Verification prefers containment even when agent containment is disabled.

    Canonical loops materialize the published commit in a host-owned checkout.
    Its independent clone keeps trusted Git metadata inside the mount without shared object hardlinks.
    Every preparation Git command disables global and system configuration.
    Preparation uses an empty scratch `HOME` and an empty `core.hooksPath` directory.
    Template-free repositories prevent external filter definitions from entering the checkout.
    Published hooks and attributes cannot execute host programs during preparation.
    `kew.loops.verification.CommandCondition.check` runs `/bin/sh -c` inside the approved image.
    It shares `build_checkout_program_argv` with extraction.
    It uses `--network none`, host UID/GID, resource caps, and no credential forwarding.
    HTTP, HTTPS, ALL, FTP, and NO proxy variables are blanked in both cases.
    The root and checkout are read-only by default.
    `[commands] verify_loop_needs_writes = true` permits writes to the disposable checkout.
    Only operator configuration can declare this requirement.
    Writable verification uses the dispatch checkout quotas: 4 GiB total and 500,000 entries.
    The host must retain at least 1 GiB of free space.
    The shared guard polls every two seconds, including quiet execution and closed output pipes.
    Large directory walks can delay the next aggregate check by up to 20 seconds.
    The free-space check continues every two seconds.
    A breach removes the container, fails verification, and records a `checkout-quota refusal` reason.
    The root stays read-only; `/tmp` provides bounded writable space.
    Contained verification has a 600-second maximum timeout, including any CPU-cap retry.
    Exit 0 passes; any non-zero exit fails.
    Verification records redacted stdout and stderr tails.
    Publication evidence sanitization removes host path ancestry before truncation and persistence.
    This applies to normal output, overflow evidence, and preparation errors.
    Both `status_reason` and `verification_checked` retain sanitized evidence.
    Each output stream has a 262,144-byte budget.
    Capture keeps the head and tail, with an omission marker between them.
    Overflow stops and removes the container and records an `output-budget refusal`.
    Both `dispatch --loop` and `loop resume` return 1 for executed verification failures.
    They print the `gate_failed` reason.
    Success returns 0; a containment prerequisite refusal returns 2.
    Records name `contained_verification` or `host_verification` and the image identity.
    Refusal records `refused_verification` and its reason.

    Checkout publication still requires dangerous admission for local Git operations.
    Without admission, checkout publication refuses both new dispatches and resumes.
    New dispatches select the issue, resolve the route, and save the loop record.
    The driver checks admission before worktree setup or subprocess execution against project files.
    Admission may inspect the local image.
    Missing admission records `needs_human` under #1444.
    The diff guard covers `**/tests/**`, `**/test_*.py`, `**/*_test.py`, `**/*.test.*`, `**/*.spec.*`, `**/AGENTS.md`, `**/CLAUDE.md`, `**/conftest.py`, `**/ratchets/**` (the test-quality and silence-on-failure ledgers), and `.github/workflows/**`.
    Contain the admitted host path inside an OS-level sandbox or virtual machine.
    A dedicated unprivileged account reduces its access.

## Publication mode

Canonical publication is the default loop path.
The loop prompt tells the agent to edit and verify only.
The agent never commits, pushes, or creates a pull request.

Kew binds the trusted base and checkout identity before the first agent launch.
After each session, kew publishes the checkout from a host-owned repository.
Each update uses an exact lease on the previously published head.
The first non-empty publication opens one pull request.
A no-change session records `checkout_no_change_evidence` and pushes nothing.
Resume uses the original recorded base and publication lineage.

The diff guard reads `CanonicalPublication.changed_paths`.
The `ci_green` gate polls the recorded published commit SHA.
The command gate materializes that commit in a temporary host-owned checkout.
It executes project code inside the approved image when prerequisites are present.
Canonical `ci_green` needs no local execution; it polls the recorded publication SHA.
It is green only when at least one check concluded `success` and no check
failed. A check whose every run was skipped did not run. When that check is one
of `[ci] approval_gated_checks` (the jobs private-repository CI skips for an
agent PR until a human adds `ci-approved`, #2033), the loop stops as
`needs_human` with
`gate_waiting: awaiting ci-approved: tests did not run`. Add the label, wait for
CI, and resume the loop. Any other all-skipped check (push-only,
paths-filtered, matrix-excluded) is ignored, as GitHub ignores it. A skipped
run of a check beside a run of that check that did run (a later workflow for
an unrelated label) does not count. A commit status that cannot be read keeps
the loop polling until the overall timeout.
A head with no check runs has only its legacy commit status, and a status
never makes the head green: failure or error is red, pending keeps polling,
and success keeps polling through the grace period (the head's check runs may
still register), then stops as not green (`commit statuses green but no check
runs`). No statuses at all is not green either (`no CI evidence on head`).

## `kew loop pause`

Request termination of a running first-class loop.

### Usage

```text
Usage: kew loop pause LOOP_ID

Arguments:
  loop_id   ID of the loop to pause (use `kew status` to list)
```

**Example:**

```bash
kew loop pause abc123
```

### What it does

`kew loop pause` sets a `pause_requested` flag on the loop record. The
orchestrator polls the pause flag at most once every three seconds. A positive
poll marks the loop paused and terminates the running process group. The host
process group receives SIGTERM. Kew sends SIGKILL after a ten-second grace if it
remains alive. This sequence can interrupt an iteration. A write in flight can
remain partial or be lost.

Key properties:

- **Polled termination.** The poll can stop the agent during an iteration.
- **Non-destructive to the checkout.** The pause does not wipe the checkout.
  Committed and flushed work survives. An interrupted write might not.
- **Resumable** — once paused, resume the loop with:
  ```bash
  kew loop resume abc123 --guidance "..."
  ```

### Distinct from `kew stop`

| Command | Scope | Mechanism | Effect on checkout |
|---|---|---|---|
| `kew loop pause` | Single loop | Three-second poll, then SIGTERM→SIGKILL | Preserved, but an in-flight write can be interrupted |
| `kew stop` | Entire fleet | Hard kill (SIGTERM→SIGKILL) + latch | Running agents terminated, runs marked `killed`; dispatch blocked |

Use `kew loop pause` when you want to inspect or redirect a single loop.
Use `kew stop` when you need to halt all dispatch immediately (e.g. an incident).

> If a pause request and `kew stop` land in the same moment, the loop may record
> `paused` (the underlying run is still marked `killed`). Either way the checkout is
> preserved and the loop can be continued with `kew loop resume`.

### Error conditions

- **Loop not found or not in `pursuing` state** — exits with a non-zero code
  and a message. A loop that is already `needs_human`, `achieved`, `unmet`, or
  `paused` cannot be paused again.

---

## `kew loop resume`

Resume a paused first-class loop with operator guidance.

### Usage

```text
Usage: kew loop resume [OPTIONS] LOOP_ID

Arguments:
  loop_id   ID of the loop to resume (use `kew status` to list)

Options:
  --guidance TEXT      Human guidance injected into the continuation session (required)
  --budget FLOAT       New cumulative loop budget threshold in USD
  --max-steps INT      New per-session step cap (overwrites the loop's cap)
  --timeout INT        New per-session wall-clock cap in minutes (overwrites the loop's cap)
  --runner TEXT        Reassign to a different native runner (e.g. claude-code, opencode)
  --model TEXT         Override the model for the continuation (must be priced)
```

**Examples:**

```bash
# Resume a needs_human loop with guidance
kew loop resume abc123 --guidance "The failing test expects a UTC timestamp, not local time."

# Resume a budget_limited loop with a larger budget
kew loop resume abc123 --guidance "Keep going — expand the test coverage too." --budget 5.0

# Resume with new step and time caps
kew loop resume abc123 --guidance "Try a simpler approach." --max-steps 30 --timeout 20

# Reassign to opencode and override the model
kew loop resume abc123 --guidance "Try with opencode." --runner opencode --model claude-sonnet-4-6
```

> **Not to be confused with `kew resume`.** `kew resume` clears the *fleet-stop
> latch* engaged by `kew stop` and re-enables dispatch for all issues. See
> [`kew resume`](resume.md).

### What it does

`kew loop resume` continues a paused loop on the **same loop record** — same
checkout, same branch, same `loop_id`. It:

1. Validates the loop is in a resumable state (`needs_human`, `budget_limited`, or `paused`).
2. Restores or reuses the loop's checkout with the prior agent changes.
   Under the default `publish = "canonical"` mode a loop whose checkout
   directory is gone cannot resume: a publication base is recorded against
   that directory's host identity, and neither binding can move. The resume
   is refused and you start a fresh loop with `kew dispatch <issue> --loop`. The work
   already pushed to the branch is untouched.
3. Applies any cap bumps (`--budget`, `--max-steps`, `--timeout`) to the loop
   record before launching.
4. Injects your `--guidance` into a fresh continuation prompt alongside the
   original goal and the reason the loop stopped.
5. Launches a new agent session against the same checkout.
6. Re-runs the **verification gate** when the session ends — `achieved` still
   requires a real gate pass. The resume does **not** bypass the gate.

The loop's `step_count` and `spent_usd` accumulate across resumes, so the
timeline in `kew status` and the TUI Flight Recorder reflects the loop's full
lifetime spend.

### Resumable states

| Status | Resumable? | Typical cause |
|--------|-----------|---------------|
| `needs_human` | Yes | Agent got stuck, gate failed (sabotage guard), or a run error (timeout, crash) |
| `budget_limited` | Yes | Session hit `agent.max_budget_usd` before the gate passed |
| `paused` | Yes | Operator requested a pause |
| `achieved` | No | Loop already succeeded — resume rejected |
| `unmet` | No | Loop was explicitly closed as unmet — resume rejected |

### Budget semantics

Step and wall-clock caps apply to each continuation session. The loop records
accumulated steps and spend across every session.

!!! note "Budget accumulation across resumes"
    The watchdog adds prior cumulative spend to the current session cost. It
    stops the session when that sum exceeds the loop's budget threshold. Use
    `--budget` to replace that threshold before the continuation starts. The
    watchdog depends on reported usage. Reports can arrive late, so the
    threshold is not a guaranteed invoice ceiling.

### Step cap

The step cap (`max_steps`) is a runaway guard — if the agent takes more steps
than the cap, the loop is classified `needs_human` before the verification gate
runs.

The default cap is **scaled by issue complexity**:

| Complexity | Default `max_steps` |
|-----------|---------------------|
| `simple`  | 80                  |
| `medium`  | 200                 |
| `complex` | 400                 |

Override with `--max-steps` at dispatch time or on resume:

```bash
kew loop resume L1 --guidance "keep going" --max-steps 500
```

### Runner and model reassignment

A loop records **both halves of its route** — the runner *and* the model it was
dispatched on, plus the complexity tier they were resolved at — so a resume
continues on the route the loop started on. A loop routed by complexity (a
`simple`-labelled issue landing on the simple tier) keeps that tier for every
later session instead of falling back to the `[routing].default` / `[agent]`
route. Loops created before this was recorded (schema v34 and earlier) have no
stored model and still resolve from config.

A flag-less `kew loop resume` picks its route in this order:

1. **A runtime route override** — a `config_overrides` row for `agent.model` or
   `routing.*`, set in the [web UI](../configuration.md) or by a second operator.
   That surface exists to steer the effective route without a commit, so it wins:
   the continuation re-resolves from the effective config **at the loop's own
   recorded tier** (never `[routing].default`). Without this, an in-flight loop
   would be unsteerable short of killing and re-dispatching it.
2. **The loop's own recorded route.** Absent such an override, the record decides
   — so an edit to a committed `kew.toml` does *not* re-route a live loop, and an
   earlier explicit `--runner` / `--model` reassignment is not forgotten.
3. **The config route at the loop's tier**, for a pre-v35 record that has no
   route to continue.

An explicit `--runner` / `--model` on the resume itself outranks all three — it
is you naming the route for this loop.

Use `--runner` to continue the loop on a different native runner:

```bash
kew loop resume abc123 --guidance "Try opencode this time." --runner opencode
```

**Native runners** are `claude-code`, `opencode`, and `pi`. Passing any other value is rejected
with an error. The loop's existing checkout and branch carry over.
The new runner inherits all prior work.

Official `codex` can run first-class loops with canonical publication.
Codex contains itself, so its loop needs no `dangerously_skip_containment` (#1977).
Its loop agent runs under Codex's native boundary.
Checkout publication still refuses host-publication runners.

`--runner` on its own swaps *only* the runner: the continuation keeps the model
already recorded on the loop, rather than reverting to `[agent].model` — **but
only if the new runner can actually run that model.** kew never invents the
missing half of a route in either direction, so if the pair would be
cross-provider (say `--runner claude-code` on a loop recorded against
`openrouter/deepseek/deepseek-v4-flash`) the resume is refused before anything
runs, naming both halves:

```text
cannot resume abc123: runner 'claude-code' cannot run the recorded model
'openrouter/deepseek/deepseek-v4-flash'; pass an explicit --model to name the
route you want
```

Pair `--runner` with `--model` to change both halves — an explicit `--model` is
you naming the route, so it is never second-guessed (it still has to be priced).
The same refusal applies to the legacy path where a pre-v35 loop would otherwise
pair the new runner with `[agent].model`.

Compatibility is decided by the runner registry's model namespaces — the same
prefixes routing already uses to infer a runner from a model:

| Runner | Models it can be handed implicitly |
|---|---|
| `claude-code` | `claude-*` |
| `opencode` | `openrouter/*`, `ollama/*`, `gpt-*`, `gemini-*`, `o1-/o3-/o4-*`, any `provider/model` |
| `pi`, `hermes` | `openrouter/*` |
| `lemonade` | `lemonade/*` |

Anything outside the table needs an explicit `--model`.

Whenever the runner a session runs under differs from the one on the record, a
`loop_reassigned` audit event is written before the continuation starts,
capturing the old and new runner names — including the legacy path where a
pre-v35 loop with no recorded model resolves from config. Both halves of the
route that actually ran are then written to the record, so the *next* resume
continues on it rather than reverting or inheriting a pair no session ran.

Use `--model` to override the model for the continuation session:

```bash
kew loop resume abc123 --guidance "go" --runner opencode --model claude-sonnet-4-6
```

The model must be **priced** — either a row (or a declared alias) in the model catalog,
`kew/src/kew/models.json`, or added to `config.pricing.overrides`. The id must match
exactly: an id the catalog does not name is unpriced, even when it starts with a priced
model's id. If you want to run
OpenCode with a Gemini or GPT model, add it to `config.pricing.overrides` first (or use a free
`ollama/*` model — `ollama/` models are zero-cost and bypass the priced check). Passing an
unpriced model is rejected before any mutation occurs.

### Audit trail

A `loop_resumed` event is written to the audit trail before the session starts,
recording `loop_id`, `guidance`, and the `budget_usd` and `max_steps` cap values. This is followed by
the normal `loop_started` event.
`loop_iteration_published` or `checkout_no_change_evidence` records publication next.
Then `iteration_recorded`, `verification_checked`, and the terminal event follow.
