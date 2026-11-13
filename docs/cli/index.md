---
description: Every kew command, grouped by what it is for, with a link to its reference page and the exit-code scheme they all follow.
---

# CLI Reference

kew's commands cover dispatch, review and merge, epics, setup, and the data
kew records. `kew run` takes work from the ready queue; `kew dispatch <issue>`
names one issue. Work-item arguments are GitHub issue numbers. Linear items run
through [`kew epic`](epic.md) and [`kew delegate`](delegate.md).

```text
Usage: kew [OPTIONS] COMMAND [ARGS]...

Options:
  --version
  --install-completion
  --show-completion
  --project DIR
```

## Runs

| Command | Description |
|---------|-------------|
| [`kew run`](run.md) | Take the next ready issue from the queue and dispatch the configured runner. |
| [`kew dispatch`](dispatch.md) | Dispatch an issue. With --loop, supervise the runner as a first-class loop. |
| [`kew finalize`](finalize.md) | Repair a run left awaiting host publication after out-of-band publication. |
| [`kew publication attest`](publication.md) | Name an operator-pushed head as the published output of its producer. |
| [`kew no-change`](no-change.md) | Decide a no-change outcome: confirm, reject, or retry its sync. |
| [`kew stop`](stop.md) | Kill all in-flight agent runs and halt dispatch until `kew resume`. |
| [`kew kill`](kill.md) | Stop one run through its authenticated process identity. |
| [`kew settle`](settle.md) | Reap one dead run whose container never started, and free its slot. |
| [`kew resume`](resume.md) | Clear the fleet-stop latch and re-enable dispatch. |
| [`kew loop`](loop.md) | Manage first-class loops. |
| [`kew attach`](attach.md) | Attach to a running agent's live event stream. |

## Review and merge

| Command | Description |
|---------|-------------|
| [`kew review`](review.md) | Gather PR review comments and dispatch agent to address them. |
| [`kew resolve`](resolve.md) | Resolve merge conflicts on an agent PR branch. |
| [`kew ci-fix`](ci-fix.md) | Dispatch CI failure auto-fix for a GitHub Actions run. |
| [`kew merge queue`](merge.md) | Queue READY PR heads; verify and merge them one at a time, or in trains. |
| [`kew approve`](approve.md) | Release a needs-approval hold (records approval_granted). |
| [`kew deny`](approve.md) | Deny a needs-approval hold (records approval_declined; parks the issue). |
| [`kew verify`](verify.md) | Run the merge verification on a host and record what it returned. |

## Epics

| Command | Description |
|---------|-------------|
| [`kew epic`](epic.md) | Build, validate, visualize, and drive an epic dependency graph (reads: GitHub or Linear). |
| [`kew drive`](drive.md) | Drive every active epic of the project from one supervised process under an admission cap and load gate; pause, resume, hold, release or stop it from any shell. |
| [`kew run-request`](run-request.md) | Queue a bounded kew command for the running drive to run as you: the console's Run button, from a terminal. |
| [`kew plan show`](plan.md) | Print the acceptance criteria kew recorded for a work item. |
| [`kew triage`](triage.md) | Classify a work item and record the proposal; a person decides. |
| [`kew delegate`](delegate.md) | Watch for assign-to-kew delegation and dispatch it. |

## Setup

| Command | Description |
|---------|-------------|
| [`kew init`](init.md) | Write kew.toml from detection and flags, then run setup and doctor. |
| [`kew setup`](setup.md) | Create labels, security hooks, and directories. |
| [`kew config`](config.md) | Show effective runtime configuration and winning sources. |
| [`kew doctor`](doctor.md) | Run local environment diagnostics. |
| [`kew health`](health.md) | Attest the control plane is functioning: meter, budgets, audit, runner. |
| [`kew skills`](skills.md) | Install and maintain kew's shipped agent skills and security-guard hook. |
| [`kew schema`](schema.md) | Print every visible command, its options, and exit codes as JSON. |
| [`kew sandbox`](sandbox.md) | Build packaged agent images, create the allowlist network, certify a runtime's closed egress, and smoke-test the contained dispatch path. |
| [`kew hosts provision`](hosts.md) | Run kew's checked-in provisioning recipe on a host and record its versions. |
| [`kew hosts release`](hosts.md#kew-hosts-release) | Drop a named holder's lease on a host, by hand. |
| [`kew canary`](canary.md) | Attest kew still reads fresh vendor CLI output correctly. |
| [`kew switchyard`](switchyard.md) | Human-gated Switchyard passthrough evidence tools. |
| [`kew serve`](serve.md) | Serve the kew web UI + API + approval inbox on localhost. |
| [`kew tui`](../tui/index.md) | Launch the terminal dashboard. |

## Data

| Command | Description |
|---------|-------------|
| [`kew report`](report.md) | Generate an outcome-linked spend report in markdown; `report sync` backfills PR outcomes; `report provenance` writes the build history behind each console feature. |
| [`kew export`](export.md) | Export run history to JSON/CSV or a model-eol inventory. |
| [`kew audit`](audit.md) | Inspect the audit trail (audit_events in dispatch.db). |
| [`kew backup`](backup.md) | Snapshot the dispatch.db record (+ config) into a portable archive. |
| [`kew restore`](restore.md) | Restore a backup archive into the configured log directory. |
| [`kew ingest`](ingest.md) | Ingest external runner usage. |
| [`kew gc`](gc.md) | List, and with `--apply` remove, the checkouts and image tags closed work left on disk. |
| [`kew cleanup`](cleanup.md) | Find and fix orphaned label states left after a crash. |
| [`kew rename-repo`](rename-repo.md) | Move the ledger's rows from a renamed repository's old slug to `project.repo`. |
| [`kew models`](models.md) | Refresh model catalog prices and lifecycle from OpenRouter and model-eol, with a report for the PR. |
| [`kew model-eol`](model-eol.md) | Check configured model ids against the curated, reviewed lifecycle registry (read-only, offline). |
| [`kew quota clear`](quota.md) | Release a runner lane held after a provider quota refusal. |
| [`kew prompts`](prompts.md) | See and change each agent role's prompt profile: list, show, propose, approve, decline, retire. |
| [`kew status`](status.md) | Show dispatch queue status. |

## CLI exit-code scheme

The [exit-code scheme](exit-codes.md) uses two axes: did it run, and what did it find?
Code `0` means success without findings.
Code `1` means findings or a performed repair.
Code `2` means the command could not run.
