---
description: The kew.toml reference, section by section, with each field's default and how kew reports a configuration error.
---

# Configuration

All configuration lives in `kew.toml` at your repo root. Every field has a sensible default.

## Config errors

Kew reports each configuration error in three lines. The first line names the
file or environment variable. The second line names the key and expected value.
The third line gives a specific fix.
Commands exit with status `2` because configuration failures prevent execution.
See the shared [exit-code scheme](cli/exit-codes.md).

`ConfigError.to_dict()` exposes the path, key, expectation, fix, kind, and
related errors. Tools can use this structure without parsing terminal text.

## Override precedence

kew resolves each configuration value in this order (highest wins):

1. **Environment variables** — e.g. `KEW_MAX_BUDGET` overrides `agent.max_budget_usd`
2. **Database overrides** — set via the `kew serve` web UI Budget panel
3. **`kew.toml`** — the declarative source of truth
4. **Defaults** — built-in sensible defaults

Database overrides are stored in the run database (not in `kew.toml`) and apply on the next dispatch or budget check. The web UI always shows which layer a value comes from. To revert an override, click **Revert** in the Budget panel — this deletes the DB row and the value falls back to `kew.toml`. The overridable fields are the budget caps (`budget.per_issue_usd`, `budget.daily_usd`, `budget.weekly_usd`, `agent.max_budget_usd`), the model/routing knobs (`agent.model`, `review.model`, `routing.default`, `routing.simple`, `routing.medium`, `routing.complex`), and the circuit-breaker sensitivity (`breaker.cost_anomaly_factor`). The three `[human_review]` thresholds are editable through the same verbs but resolve against a separate, generation-carrying policy row rather than this table — see [Human review](#human-review-human_review). `GET /api/config/overridable` returns every one of them with its full resolution story (effective / file / default / override); the Budget panel in the web UI exposes the three budget fields today. On the web console's `/config` page, `agent.model`, the reviewer model (`review.model`) and each `routing.*` entry are picked from a list of the packaged model catalog rather than typed: `GET /api/models` serves it (id, display name, runner, price per Mtok, lifecycle, the lanes this project's effective `[routing]` sends to each model, and kew's shipped lane defaults, labelled apart), and a retired model appears only while the config still names it. Picking a model for a route sets the route's runner to that model's catalog runner; only a route with no model takes a typed runner. Because `review.model` decides which model reviews agent PRs, its write accepts only a catalog id (not an alias) that is routable, not retired, and on the reviewer's runner (`[review] runner`, claude-code); anything else is refused with the reason before an audit row is written. The catalog is read on the write only, so a model the catalog later retires stays in force until an operator moves off it, and a governed review's verdict records the model its reviewer run actually launched on. Spend-authorizing overrides pass the `[approval].approvers` gate.

> **Two copies of `kew.toml` take part in a dispatch, and they can differ.**
> The orchestrator makes its decisions (budget caps, breaker, routing, sandbox posture,
> approval threshold) from the effective config it loads in **your** project directory,
> including uncommitted edits. The agent's clone is checked out from the configured base
> branch (`branches.base`; a contained dispatch falls back to `main`, a host dispatch detects
> the default branch), so anything read *inside* the clone (the committed security-hook patterns, and the file the
> agent itself sees) comes from the **committed** file.
> **Commit your `kew.toml` changes before starting a dispatch run** so both copies agree.
> `kew dispatch` (and other dispatch commands) warn at preflight when the governance sections
> (`budget`, `breaker`, `security`) differ between your working tree and the committed file.
> That comparison is best-effort: it reads your local tracking ref for origin's default branch
> (`origin/HEAD`, then `origin/main` or `origin/master`) without fetching, so a stale tracking
> ref or a configured non-default base branch can miss drift that the clone will see.

## Project selection (`--project` / `KEW_PROJECT`)

By default kew finds `kew.toml` by walking up from your current directory to the git root. **A relative `logging.dir` (and therefore `dispatch.db`) resolves against the directory that `kew.toml` lives in, not against your current directory.** One project, one ledger, from any subdirectory.

kew does not read `dispatch.toml`. If the walk (or the `--project` directory) reaches a `dispatch.toml` before any `kew.toml`, kew refuses to run and exits `2` without parsing it, rather than start on default settings. A `dispatch.toml` next to a `kew.toml` is ignored.

A relative `branches.worktree_base` also resolves against the selected project root. This rule covers its default, TOML value, and `KEW_WORKTREE_BASE` override. Absolute checkout roots remain unchanged.

That was not always true. Before #1156 a relative `logging.dir` resolved against the cwd, so each subdirectory you invoked kew from minted its own `logs/dispatch/dispatch.db`. The consequences were not cosmetic: `kew audit verify` reported a false `COUNT-SHRINK` when it read the ledger the anchors did not belong to, the daily budget cap could be reset by changing directory (spend is summed from the selected ledger), and the circuit breaker's cost-anomaly baseline was empty in a fresh directory. If a fork was left behind, kew now names it at preflight rather than silently ignoring it:

```text
[dispatch] ⚠ Forked dispatch ledger(s) found under /home/me/work/svc: /home/me/work/svc/pkg/logs/dispatch/dispatch.db.
kew is reading and writing /home/me/work/svc/logs/dispatch/dispatch.db only — ...
```

Pick **one** ledger as canonical and archive the other; the keeper is normally the one your `audit-anchors.jsonl` verifies against and that holds the full history (`kew audit verify --anchors audit-anchors.jsonl` from the project root must report every anchor matched). `kew backup` the loser, then move it out of the project root — two live ledgers cannot be merged without breaking the hash chain, since each is a separate chain from its own genesis.

`audit-anchors.jsonl` follows the same rule: `kew audit anchor` writes the project-root file no matter where you run it. A relative path you pass explicitly to `--anchors` still means what you typed, relative to your cwd.

To pin the control plane explicitly, pass the global `--project <dir>` option (or set `KEW_PROJECT=<dir>`):

```bash
kew --project ~/work/service-a epic advance my-epic
KEW_PROJECT=~/work/service-a kew run
```

Both resolve `kew.toml` **and** the run database from that directory regardless of the current working directory — useful when there is no `kew.toml` to discover, or when you want to act on a project you are not standing in. `--project` simply sets `KEW_PROJECT` for the process, so the two are interchangeable; an explicit `KEW_LOG_DIR` still wins over the project-anchored log dir.

Every driver/dispatch command prints a one-line banner before it acts, so you can see which control plane you are touching:

```text
[dispatch] project=/home/me/work/service-a tracker=github db=/home/me/work/service-a/logs/dispatch/dispatch.db
```

## Inference transport

Inference uses the runners' direct provider connections by default. Setting
`inference_transport.mode = "switchyard"` selects the experimental,
fail-closed Switchyard transport. External and host-managed endpoints must be
HTTP(S) origins without user information, paths, queries, or fragments. Plain
HTTP accepts only literal loopback addresses (`127.0.0.0/8` or `::1`).
`localhost` is not sufficient loopback proof. Sidecar mode omits this field.

Switchyard mode requires a nonempty route and either `expected_version` or an
absolute `compatibility_record_path` naming an approved regular, non-symlink
file. External mode (`managed = false`) does not require a configuration path.
Managed mode additionally requires an absolute regular, non-symlink
`config_path`, an absolute regular, non-symlink `binary`, and the binary's
64-character hexadecimal `binary_sha256`. Authenticated managed routes also
declare credential variable names in `credential_env`; values remain in the
host environment and never enter either configuration file. Unknown keys, non-positive startup
deadlines, and any fallback other than `"refuse"` are rejected at load time.
Proxy failure never silently falls back to direct inference.

The sidecar table selects one Docker-owned proxy for each attempt.
This mode remains separate from the managed host process.
Set `sidecar.enabled = true` and omit `endpoint` for this mode.
Sidecar mode requires an exact `expected_version` value.
Kew derives the worker endpoint from verified proxy attachments.
Kew resolves both local images to immutable image IDs before each attempt.
Kew attaches only the proxy to `egress_network`.
The worker receives a fresh synthetic client value.
The worker never receives a reusable upstream provider credential.

Kew copies the exact private route bytes into fresh Docker storage.
Only the proxy mounts that storage.
The proxy receives only route-declared credential environment names.
OAuth-backed Anthropic clients use one lowercase `authorization` header.
The header value must contain `Bearer ` and one token without whitespace.
Those clients require `format = "anthropic_messages"` and `max_retries = 0`.
They must omit `api_key_env`.

Normal completion proves the worker, helpers, proxy, storage, and network absent.
Cleanup failure blocks the attempt result and further retries.
Abrupt supervisor loss can leave these resources running.
Kew does not reconcile resources across supervisor sessions in SC-1.
An operator must remove residual resources after SIGKILL or host failure.

`allow_adaptive` defaults to `false` and is a rollout-gated experiment flag;
it does not enable arbitrary routes. The governed dispatch lifecycle admits
only the exact `switchyard/llm_classifier` route when this flag is `true` and
the route is managed with a pinned binary and digest, immutable route config,
an explicit expected server version, and credential names that match the
reviewed route configuration. Any missed requirement refuses the dispatch
with a stable, sanitized admission reason. The live benchmark and canary
remain passthrough-only, and the Switchyard rollout remains **HOLD** pending
human approval.

`expected_version` is the exact token printed by `switchyard-server --version`,
not a source commit or moving tag. For the pinned compatibility build it is
`"0.1.0"`; use `compatibility_record_path` and `binary_sha256` to bind that
version to reviewed provenance. Kew never downloads or updates Switchyard.

| Field | Operator contract |
| --- | --- |
| `endpoint` | Exact HTTP(S) origin for external and host-managed modes. Omit it for sidecar mode. HTTP requires a literal loopback IP. |
| `route` | Exact route/model exposed by `/v1/models`; live benchmark and canary admission is limited to `switchyard/passthrough`. |
| `allow_adaptive` | Default `false`; rollout-gated experiment opt-in for the governed managed `switchyard/llm_classifier` route only. |
| `expected_version` | Exact reported server version. Required for managed and sidecar launches even when a compatibility record is also retained. |
| `compatibility_record_path` | Optional absolute reviewed provenance record; it is not a substitute for the managed server's version check. |
| `managed` | `false`: the operator owns endpoint startup/shutdown. `true`: Kew owns one pinned process per run. |
| `config_path` | Managed or sidecar: absolute operator-owned route config. Sidecar mode requires an owned `0700` parent and owned `0600` file. Kew records only its digest. |
| `binary` / `binary_sha256` | Managed only: absolute executable and SHA-256 of its exact bytes. |
| `credential_env` | Managed or sidecar: credential names required by the exact route. Supported names are `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, and `ANTHROPIC_API_KEY`. The declaration must exactly match reachable `api_key_env` values. OAuth header routes use no credential name. |
| `startup_timeout_seconds` | Positive bounded wait for managed health/route admission. |
| `fallback` | Must be `"refuse"`; transport failure never changes provider policy by falling back to direct. |
| `sidecar.enabled` | `true`: Kew owns one fresh Docker topology for each attempt. `false`: the declaration remains inactive. |
| `sidecar.image` | Local SC-IMG reference. Kew resolves and launches its immutable image ID. |
| `sidecar.egress_network` | Existing approved proxy egress network. The worker never attaches to it. |

```toml
[project]
name = "my-project"           # Intentional example: choose the project name
# repo = "owner/repo"  # Auto-detected from gh CLI, else the git origin

[agent]
timeout_minutes = 120         # Kill agent after this long
no_output_minutes = 15        # Stop a contained run whose model call is silent
                              # this long (0 = off); tool calls and waits between
                              # turns never count
max_budget_usd = 10.0         # Intentional example: set a per-run cap
budget_stop_grace_seconds = 30 # Wrap-up window after a budget-cap SIGTERM so the
                              # agent can commit WIP before SIGKILL (graceful stop)
# max_steps is scaled by issue complexity (simple=80, medium=200, complex=400)
# Override per-loop with `kew dispatch --max-steps` or `kew loop resume --max-steps`
# runner = ""                 # Force any registered runner; Codex must be explicit
# model = ""                  # Override model (empty = default)

[agent.claude_code]
skip_permissions = true       # --dangerously-skip-permissions
autocompact_pct = 70          # Context compaction threshold

[agent.opencode]
extra_args = []
# agent = ""                  # Optional: select an OpenCode agent preset

[agent.codex]
reasoning_effort = ""        # minimal, low, medium, high, or xhigh
# service_tier = "fast"      # Optional; unset preserves the Codex CLI default

[agent.pi]
extra_args = []               # Pass-through CLI flags appended to every `pi` run

[agent.hermes]
extra_args = []               # Pass-through CLI flags appended to every Hermes run

[agent.lemonade]
base_url = "http://localhost:13305/v1"  # Any OpenAI-compatible local endpoint
# api_key = ""               # Usually unnecessary for a local server
max_steps = 40               # Max model turns per run (outer budget gate also applies)
extra_args = []

# Direct inference is the default and preserves existing runner behavior.
[inference_transport]
mode = "direct"              # direct or switchyard
allow_adaptive = false       # rollout-gated; managed classifier dispatch only

# Configure this table only when mode = "switchyard".
# This complete table is an intentional inactive transport example.
[inference_transport.switchyard]
# endpoint = "https://switchyard.example.com:4000" # external or host-managed only
route = "switchyard/passthrough"
expected_version = "0.1.0"
# compatibility_record_path = "/absolute/reviewed/switchyard-compatibility.md"
managed = false              # sidecar mode and external endpoints keep this false
config_path = "/absolute/operator-owned/switchyard.toml" # managed or sidecar
# binary = "/absolute/bin/switchyard-server"               # managed only
# binary_sha256 = "<64 hex characters>"                     # managed only
credential_env = ["OPENAI_API_KEY"] # exact sidecar upstream names only
startup_timeout_seconds = 20
fallback = "refuse"          # the only supported v1 behavior

# Optional per-attempt Docker sidecar declaration.
[inference_transport.switchyard.sidecar]
enabled = false
image = "kew-switchyard:operator-built" # separate operator-owned sidecar image
egress_network = "kew-allowlist"

[branches]
prefix = "agent/issue-"       # Branch: {prefix}{issue_num}
worktree_base = ".worktrees"  # Checkout base, relative to the selected project
# base = ""                   # Override base branch (auto-detected if empty)

[commands]
ci = "make test"              # CI gate (required to pass)
format = ""                   # Format command (run before CI); e.g. "black ." — empty = no format step
verify = ""                   # Raw default; empty uses commands.ci effectively
test_targeted = ""            # Scoped test run the governed remediation brief names; must contain {paths}
test_policy_mark = ""         # Pytest mark on the repo-wide policy gates unsandboxed prompts always run
verify_loop = ""              # Command the loop gate runs (kew executes; exit 0 = met)
verify_loop_mode = "command"  # Gate mode: "command" or "ci_green" (#289)
ci_poll_timeout_seconds = 1800 # Max seconds to wait for CI before needs_human (#289)
commit_prefix = "feat(#{issue}): "  # Prefixes the commit kew writes; the subject after it is the change itself
co_author = ""                # Co-authored-by trailer added to commits (empty = omit)

[ci]
# CI jobs that skip an agent PR until a human adds `ci-approved` (#2033). A
# check run of one of these names, or a matrix leg ("test (3.12)"), whose every
# run was skipped makes the head wait for approval in the epic driver's merge
# gate and the loop `ci_green` gate. Any other all-skipped check is ignored.
approval_gated_checks = ["test", "agent-image-scan"]

[prompt]
# template = "path/to/custom.md.j2"  # Full Jinja2 override
# append = "Extra instructions..."    # Appended to default prompt
include_safety_rules = true          # No-op for the built-in template: the safety
                                     # block (incl. the anti-merge/approve guard)
                                     # always renders and cannot be dropped (#936)
include_execution_strategy = true    # TDD workflow instructions
include_coderabbit = false           # Opt-in: agents run the CodeRabbit CLI against
                                     # branches.base before pushing; needs the
                                     # coderabbit binary on PATH
include_dispatch_report = true       # Structured PR summary
include_repo_map = true              # Structural code TOC in prompt
repo_map_tokens = 1024               # Max tokens for repo map
repo_map_mode = "hint"               # "hint" (default): map ordered by the issue
                                     # title/body, so repo_map_hash/prefix_key vary
                                     # per issue. "stable": no hint, deterministic
                                     # ordering, constant hash between merges. The
                                     # trade-off is decided by a pre-registered
                                     # experiment (2026-07, since removed),
                                     # not by flipping this default.
[security]
# Intentional examples: replace these patterns with project policy.
blocked_prefix_patterns = ["kubectl delete"]
blocked_exact_patterns = ["DROP DATABASE"]

[retry]
max_attempts = 3              # Total attempts (1 = no retry)
max_resume_attempts = 2       # Of those, how many try --resume first
backoff_base_seconds = 30     # Starting backoff delay
backoff_multiplier = 2.0      # Exponential multiplier (30s, 60s, 120s, ...)
retriable_exit_codes = [1]

[parallel]
max_workers = 1               # Concurrent dispatch workers; the default for [fleet] max_concurrent
shutdown_grace_seconds = 30   # Wait time for workers on shutdown

[fleet]
# max_concurrent = 3          # unset = [parallel] max_workers; agents in flight ANYWHERE (--workers overrides)

[overlap]
strategy = "serialize"        # Concurrent dispatch: "serialize", "warn", or "off"

[overlap.advanced]
merge_context_ttl_minutes = 60  # How long merge context is retained (minutes)

[review]
auto_reply = true             # Reply to PR comment threads after fixing
include_resolved = false      # Include already-resolved comments
max_review_usd = 10.0         # Cap checked before each adversarial reviewer
                              # dispatch, against ALL of the item's review-stage
                              # spend — verdict rounds and governed remediations
                              # share one pool (separate from the impl cap)
max_remediation_usd = 40.0    # Cap checked before each governed remediation leg
                              # (fixing reviewer findings) against that same
                              # pool — set higher than max_review_usd, since a
                              # fix cycle costs far more than one reviewer pass;
                              # raise for large or complex fix cycles.
                              # An exhausted pool HOLDs the node with a
                              # `review_budget` reason naming the spend and this
                              # key; raising the cap clears the hold on the next
                              # tick, no `kew epic unhold` needed.
# --- Governed adversarial-review stage (opt-in) ---
adversarial = false           # Enable the fresh-context adversarial review stage.
                              # The governed reviewer runs the PR's code as the
                              # agent's user, so it is not yet safe on pull
                              # requests from untrusted forks
blocking = true               # true: a non-READY verdict HELDs the node for a human.
                              # false: verdict is attested and the node proceeds
                              # (the advisory adoption on-ramp)
max_rounds = 2                # Remediation cycles before a non-converging node HELDs
stop_rule_after_rounds = 6    # Review stop rule: after this round, a NEEDS_CHANGES
                              # verdict with only low/medium findings is accepted
                              # as READY-with-residuals. The findings are filed as
                              # one GitHub issue (label `bug`) and the node goes to
                              # the merge gate. Any high, critical or unknown
                              # severity keeps remediating, and so does any
                              # recorded acceptance criterion the reviewer
                              # judged `fail`: a residual is a defect in how the
                              # work was done, never the work missing what it
                              # was for. 0 disables the rule.
                              # Takes effect only when max_rounds is larger. At
                              # or above max_rounds the threshold is unreliable,
                              # not just late: round numbers count every recorded
                              # round, while max_rounds counts only remediation
                              # cycles. Usually the cap HOLDs first and the named
                              # round never comes (releasing a
                              # `review_rounds_exhausted` hold restarts numbering
                              # at 1) — but a merge-queue round bumps the round
                              # number without spending a cycle, so the rule can
                              # fire one round PAST the cap and accept a round the
                              # cap would have held
stop_rule_max_severity = "medium"  # The ceiling that rule accepts: "low" takes a
                              # round of lows only, "medium" also takes mediums.
                              # Any other value is a config error at load
model = ""                    # Reviewer runner model ("" = runner default)
runner = ""                   # Reviewer runner: "" or "claude-code" (the same).
                              # The verdict must come from the runner's own
                              # terminal stream event, and only claude-code
                              # emits one kew can read. Any other runner is a
                              # config error at load
publish_status = true         # Mirror the verdict onto the PR head SHA as a
                              # `kew-review` GitHub commit status (pending on
                              # dispatch → success on READY; advisory DEGRADED is
                              # truthful success; blocking/non-READY is failure).
                              # Require it via branch protection on `main` to block
                              # the GitHub merge button against a human merge race
                              # (best-effort: a publish failure never crashes the tick)

[resolve]
strategy = "merge"            # Conflict resolution: "merge" or "rebase"
auto = false                  # Auto-resolve conflicts without prompting

[labels]
ready = "ready"
in_progress = "in-progress"
agent_working = "agent-working"
agent_failed = "agent-failed"
agent_pr = "agent-pr"
ci_approved = "ci-approved"             # Human approval for self-hosted CI on an agent PR (#2033)
pr_pending_review = "pr-pending-review"
blocked = "blocked"
completed_no_changes = "agent-completed-no-changes"
agent_needs_help = "agent-needs-help"
incomplete_run = "incomplete-run"        # Draft PR from a max-steps-incomplete run
budget_exceeded = "budget-exceeded"
large_pr = "large-pr"
ready_to_merge = "ready-to-merge"
overlap_hold = "overlap-hold"
priorities = ["p0-critical", "p1-high", "p2-medium", "p3-low"]

[triggers]
ci_failure_max_retries = 2    # How many times `kew ci-fix` may re-dispatch one PR

[delegation]
# NOTE: kew delegate watch requires tracker.type = "linear". Supervised use is
# safe as of #966 (reservation lifecycle) + #971 (refusal-episode hardening);
# fully unattended use is pending the rest of the delegation-followups epic.
assignee = ""                   # The kew Linear member name (required at watch start)
label = "kew:ready"             # Linear label that signals a delegated issue is ready
poll_interval_seconds = 20      # Throttled Linear API poll floor (seconds)
fast_interval_seconds = 2       # Local tracker-events read cadence (seconds)
refusal_cooldown_seconds = 300  # Skip re-claiming a budget-refused item this long (0 = off)

[budget]
per_issue_usd = 15.0          # Max spend per issue
daily_usd = 50.0              # Daily spend cap
weekly_usd = 200.0            # Weekly spend cap

[breaker]
# Automatic circuit breaker (#245). Kills a single run that burns more than
# cost_anomaly_factor x the median for its (model, complexity, run_kind) bucket
# — dispatch, review, loop, and planning runs are bucketed separately so a
# heavyweight review run isn't judged against cheap first-dispatch medians — and
# engages the fleet stop (`kew stop`) when live daily/weekly spend breaches the
# [budget] limits. The anomaly rule stays dormant until a bucket has min_samples
# completed runs. Degenerate near-zero-cost rows (< $0.01) are excluded from the
# bucket median. The trip point is the largest of median x cost_anomaly_factor,
# the bucket-relative floor (median x anomaly_floor_multiple), and the absolute
# backstop min_anomaly_threshold_usd — so a heavyweight-but-healthy run won't
# trip when the bucket median is small, without one dollar value having to
# serve buckets whose medians differ by orders of magnitude (#1116). An
# under-sampled bucket is judged against median x anomaly_floor_multiple, and
# arms only once it has at least three of its own samples whose median has
# reached the route reference (the same query with the run_kind predicate
# inverted — the median over the route's *other* kinds). That reference gates
# arming and never sets the threshold: it measures other work, so a bucket only
# ever seen cheaper than the rest of its route is no evidence about what a
# normal run of that kind costs, and arming off it would kill a first-ever
# review_remediation at 3x a cheap route's dispatch cost. Excluding the
# bucket's own kind is what makes that gate mean anything — read across all
# kinds it would pass for free on a route running a single kind. A thinner
# bucket, one with no samples at all, one whose route has no other kinds, and
# one with anomaly_floor_multiple = 0 all stay dormant rather than arming on
# the absolute floor alone. Each dispatch logs the armed threshold and which of
# the three terms won.
#
# compaction_allowance_factor discounts autocompaction overhead before the
# anomaly rule fires (#859): each autocompaction rewrites the whole context,
# adding a cache-creation spike, so the run cost is reduced by
# compaction_allowance_factor x the bucket median per compaction before it is
# compared to the threshold. This keeps a long run that legitimately compacts
# several times from tripping on the compaction cost alone. A contained
# (sandboxed) run reads its compactions from the agent's own stream, so there
# at most three compactions are discounted. 0 disables it.
enabled = true
cost_anomaly_factor = 3.0
min_samples = 5
min_anomaly_threshold_usd = 0.0
anomaly_floor_multiple = 3.0
compaction_allowance_factor = 0.5

[approval]
# threshold_usd = 5.0         # Unset = feature off (default). Hold dispatches
                              # whose estimated cost is at or above this.
label = "needs-approval"      # Hold label, created by `kew setup`
# approvers = []              # Optional allowlist of GitHub logins allowed to
                              # approve/deny. Empty = `kew approve`/`kew deny`
                              # admit any actor on the host (only the `/approve`
                              # comment workflow checks write access), but every
                              # prompt profile decision is refused until the list
                              # names someone.
                              # Also the actors (KEW_ACTOR, else the OS user)
                              # whose `kew epic unhold` of a remediation_no_change
                              # or remediation_no_progress hold can earn the head
                              # a re-review; empty = any operator's can.

[routing]
default = "claude-code"       # Required routing value; no source default
# Per-complexity overrides:
# simple = { runner = "opencode", model = "openrouter/google/gemini-3.8-flash" } # Intentional example
# medium = "claude-code"                                      # Intentional example
# complex = { runner = "claude-code", model = "claude-opus-4-6" } # Intentional example
```

### Silent runs (`[agent] no_output_minutes`) {#agent-no-output-minutes}

Beside `timeout_minutes`, `[agent]` takes `no_output_minutes` (default `15`).
A contained run — a worker or a reviewer — whose agent stream writes nothing
for that many minutes while it waits on its model provider is stopped by its
container id, the way the run timeout stops it, and its run row records the
error category `stream_silent`.

Only a wait on the model counts. The clock runs from a line that starts or
continues a model turn (the run's `init`, an assistant message, a tool result
going back to the model) until the next line. It stops while a tool call is
open, after a turn's `result` line until the next turn starts, and while a
background task runs, so an agent that ended a turn and waits an hour on a
long command is never stopped. Every stream line, including `tool_progress`
and `command_lifecycle`, counts as output. Only Claude Code's stream says when
the model is called; runs on other runners are not watched.

A reviewer round stopped this way is not a verdict: it does not count against
the review cycle and runs again on the next tick, and only after three such
rounds in a row is the node held, with the hold naming the silence. `0` turns
the watchdog off; a negative value is refused when the config loads. Runs on
the host (not contained) and placed runs (an agent on a remote host) are not
watched.

## `[prompt]` and prompt profiles {#prompt-config-and-profiles}

`[prompt]` shapes the **dispatch** prompt only: which built-in sections render,
the repo map, a full `template` override and an `append` text. It lives in
`kew.toml`, so it is part of the project's configuration and changes with it.
The other roles' prompts ignore it.

A [prompt profile](prompts.md) is different. It is an operator addendum to one
role's base prompt (`dispatch`, `adversarial_review`, `review_remediation`,
`resolve` or `ci_fix`), and kew never reads one from `kew.toml`. It lives only
in the host's `dispatch.db`. Only the actors `[approval].approvers` names can
propose, approve, decline or retire one, each as a chained audit event. While
the list is empty, the default, every profile decision is refused until you set
it. Every
run records the profile version it ran under.

The two meet in one place: a custom `[prompt] template` is refused while a
profile is active for the role, because only the built-in prompt renders the
addendum. Retire the profile, or drop the custom template. A custom template
therefore never sees a `profile_addendum` variable: kew defines it only when a
profile renders, so `{% if profile_addendum is defined %}` is false without one.
That guard is required around any field a template reads from it: reading a
field of the undefined variable, such as `{{ profile_addendum.body }}`, fails
the render.

## `[routing.cache]` {#routing-cache}

Cache-economics-aware routing and TTL-aware dispatch ordering (Phases 2 & 3 of the cache-aware routing feature). Two independent opt-in switches control the routing governor and dispatch ordering. Their checked defaults appear in the table below.

```toml
[routing]
default = "claude-code"         # required in every [routing] table
cache_aware = false             # routing governor: advise-only vs auto

[routing.cache]
governor_threshold_usd = 0.02   # anti-churn: min savings to override
order_aware = false             # TTL-aware dispatch ordering: advise vs auto
max_reorder_lookahead = 10
```

| Key | Default | Description |
| --- | --- | --- |
| `cache_aware` | `false` | When `true`, the routing governor may substitute a warm, equal-or-stronger model for the proposed route if the expected savings exceed the threshold. **Override-up-only:** the governor never trades quality for cache savings. When `false`, the governor still computes and logs the decision (visible in the audit trail) but returns the proposed route unchanged — advise-only mode. |
| `order_aware` | `false` | When `true`, the dispatch queue reorders ready issues within each priority tier to cluster those sharing a `prefix_key` adjacently, so they dispatch back-to-back inside the provider cache TTL (the first warms the prefix, the rest read cached). When `false`, the reordering decision is computed and attested but not applied. |
| `governor_threshold_usd` | `0.02` | Minimum expected savings for the governor to override the proposed route. Prevents churn for negligible gains. Must be `>= 0`. |
| `max_reorder_lookahead` | `10` | Maximum number of positions the ordering pass scans ahead within a priority tier to find same-prefix issues. Must be `> 0`. Prevents a distant same-prefix issue from starving age order. |

**The governor (cache_aware):** after the existing tier-based candidate selection, a two-stage pipeline runs: `propose_candidate()` (unchanged) → `cache_governor()`. The governor:
1. Checks whether the proposed provider caches and its read multiplier < 1.0 (the engage gate, borrowed from OpenRouter). If not, passthrough.
2. Builds a candidate set: `{proposed} ∪ {warm models whose capability ≥ proposed}`. Capability order comes from the tier ladder (simple < medium < complex).
3. Prices each candidate using `estimate_route_cost` — warm vs cold from the ledger-backed oracle.
4. Picks the minimum expected cost. If a warm, equal-or-stronger model beats the cold proposal by more than `governor_threshold_usd`, it recommends (or applies) the override.

**The ordering pass (order_aware):** after the existing `priority + age` sort, a within-tier reorder clusters issues with the same `prefix_key` adjacently. The cluster key is `(runner, model)` for the tier. The pass is bounded by `max_reorder_lookahead` and never crosses priority tiers. With `max_workers > 1`, simultaneously-fired same-prefix issues all start cold — v1 clusters them adjacently and lets natural worker cadence warm them; the "stagger across worker slots" refinement is deferred.

**Known limitations:**
- `prefix_key` is a hash of `(repo, runner, model, provider, prompt-template version, repo-map hash)`. It does not capture the runner's internal system+tools prompt (e.g. Claude Code builds its own), but those are byte-constant for a given runner version; a runner version bump simply shows up as more cold runs until things re-warm.
- Caches never cross providers — any cross-provider switch is a guaranteed cold start. The governor accounts for this when comparing candidates.
- The governor never changes a run's containment. Containment is resolved for the proposed runner before the governor runs. A swap to another runner is re-resolved under the same `[sandbox]` rules, and kew keeps the proposed route when the new runner would launch under a different posture (for example from `codex`, which is `native` on the Docker runtime, to `claude-code`, which would run `uncontained`) or could not be contained on this host.

**Per-provider cache parameters.** kew ships with built-in cache economics for three providers. Unknown providers (e.g. `z.ai`, GLM) default to `caches=false` (treated as always-cold — conservative). The defaults live in source (`kew.pricing.PROVIDER_CACHE_DEFAULTS`) and are documented below for reference; the config-seam for overrides (`[routing.cache.providers.*]`) is a planned extension — open an issue if your provider needs different values.

| Provider | Caches? | Read mult | Write mult | TTL | Min tokens | Cache style |
| --- | --- | --- | --- | --- | --- | --- |
| `anthropic` | yes | 0.10× | 1.25× | 300s | 1,024 | explicit |
| `openai` | yes | 0.50× | 1.0× (implicit) | 600s | 1,024 | implicit |
| `google` / Gemini | yes | 0.25× | 1.0× (implicit) | 300s | 32,768 | implicit + explicit |
| `z.ai`, GLM, unknown | **no** | — | — | — | — | confirm before enabling |

- **Explicit** providers (Anthropic) charge a write premium (1.25×) on cache creation and a steep read discount (0.10×). Cache TTL resets on each hit (rolling).
- **Implicit** providers (OpenAI, Google) cache automatically with no write premium (1.0×). Google requires a larger minimum prefix (32K tokens) for eligibility.
- Multipliers are time-sensitive — kew's defaults match each provider's published pricing at the time of release. Verify against live docs if you rely on precise cost projections.
- Unknown providers default `caches=false` — treated as always-cold, with no write premium applied to input costs. Set `caches=true` explicitly once caching support is confirmed for your provider.

**Audit trail.** Every governor decision behind a real dispatch is attested (a `--dry-run` preview prints its reasoning and attests nothing, because nothing was dispatched): the `routing_decision` audit event records the proposed model, recommended model, candidate set, warm prefix set, estimated savings, the decision reason, and whether it was applied (`cache_aware=true`) or advise-only (`cache_aware=false`). Every ordering decision is attested as a `cache_reorder` event. Run `kew audit tail | grep routing_decision` to inspect recent governor decisions.

```toml
[pricing]
# Custom model pricing overrides: [input_cost, output_cost] per million tokens
# overrides = { "custom-model" = [3.0, 15.0] } # Intentional example
# Prompt-cache tokens are priced automatically as multiples of the input rate
# (cache write 1.25x, cache read 0.1x), so overrides only need input/output.
# Catalog prices (kew/src/kew/models.json) are the default; refresh them with
# `kew models refresh`. [model_eol].snapshot_models is NOT an operator key: the
# loader fills it in from the curated registry, and writing it here fails
# config load naming the key.

[billing]
mode = "metered"              # "metered" (API key, list price ≈ real spend) or "subscription"
plan_label = ""               # e.g. "Claude Max" — shown in the value banner (empty = omit)
plan_monthly_usd = 0.0        # Monthly subscription cost for the value-leverage banner

[human_review]
max_pending_reviews = 3       # Publications that may await a human at once,
                              # counted per delivery repo — never globally
max_diff_lines = 500          # Additions PLUS deletions; 500 passes, 501 holds
max_paths = 20                # Distinct paths touched; 20 passes, 21 holds
# approvers = ["octocat"]     # Intentional example; empty → [approval].approvers

[alerting]
# webhook_url = ""            # Slack/Discord webhook for notifications
notify_on_failure = true
notify_on_success = false
notify_on_hold = true         # Comment + webhook when a dispatch is held for approval

[ingest]
enabled = false               # master switch for `kew ingest serve`
host = "127.0.0.1"            # receiver bind host; loopback-only
port = 4318                   # OTLP/HTTP default
cap_usd = 0.0                 # 0 = no cap alert
combine_with_dispatch = false # cap against interactive only, or interactive + dispatched

[logging]
dir = "./logs/dispatch"       # relative → resolved against this kew.toml's directory
retention_days = 0            # cutoff (days) for `kew export --purge`; 0 = keep forever
```

The legacy `[dispatch]` section is removed.
Use `[agent]`, `[parallel]`, and `[commands]` for execution settings.
Unknown legacy keys fail configuration validation.

### Human review (`[human_review]`)

One policy decides when a publication stops for a person. Both thresholds are
**inclusive**: exactly `max_diff_lines` changed lines or exactly `max_paths`
paths publish unattended, and one more requires confirmation. The line count is
additions plus deletions — removing 900 lines is not a small change. Work the
host cannot measure (binary blobs, unreadable paths) and anything the
publication filter flags as executable also stops for a person.

A **refusal is not a review.** When the publication filter *rejects* an entry,
no approver, allowlist entry or threshold releases it — there is no
confirmation path at all.

`max_pending_reviews` is capacity, and it is counted for the canonical GitHub
delivery repository named by `[project].repo`. There is no cross-repository
count, so a busy repo cannot stall a quiet one. Lowering the limit **evicts
nothing**: holds already taken stay where they are, and no new hold may be
taken until occupancy falls below the new limit. Every surface *renders* that
state — `GET /api/config/human_review` returns `over_cap` alongside the live
`pending_reviews` count, and the web console shows it — but none of them acts
on it.

A confirmation is bound to the **exact SHA** and to the policy generation it
was given under. Remediation that changes the tree, or an edit to the policy,
invalidates it — the work is presented to a person again rather than inheriting
an approval for something else.

#### Three authorities, deliberately separate

Three different permissions meet around a held publication. They are not one
setting, and none of them implies another:

| Authority | What it decides | Who |
|---|---|---|
| **Spend approval** | Releases a dispatch held on cost, and authorizes a budget override | `[approval].approvers` |
| **Policy-change authority** | Edits the three thresholds above | `[approval].approvers` |
| **Terminal judgment** | Releases one publication hold, for one exact SHA | `[human_review].approvers` (empty → `[approval].approvers`) |

Being trusted to say "this one diff is fine" is not being trusted to raise the
threshold that stopped it. `[human_review].approvers` is consulted only when a
hold is released and is **not editable from any config surface** — not the API,
not the CLI, not the web console.

#### Viewing and editing the policy

The three numeric thresholds are edited through the ordinary config verbs, but
they do **not** land in the `config_overrides` table the other overridable
fields use. They land in a durable policy row scoped to the delivery
repository — that row is what carries the monotonic generation and the
optional expiry:

- **`GET /api/config/human_review`** returns the policy in force: `values`,
  `generation`, `expires_at`, `source` (`system` or `override`), `approvers`,
  `pending_reviews`, `over_cap` and `pending_reversion`. A read never writes.
- **`POST /api/config/human_review.<threshold>`** takes `{value, reason}` plus
  two policy-only fields: `expires_at` (ISO-8601 with an explicit UTC offset; one
  already past is refused) and `if_generation`.
- **`DELETE /api/config/human_review.<threshold>`** drops that override.
  Removing the last one reverts through the same expiry primitive, so the
  chained reversion event is appended before the row disappears.
- **`kew config effective`** reports each threshold from the policy in force,
  naming its generation and expiry rather than the `kew.toml` value an
  override hides.
- The **Config** page in the web console renders exactly that payload — value,
  generation, expiry, pending reversion, over-cap, and the terminal-judgment
  allowlist — and derives none of it. In particular the browser does not decide
  whether an expiry has passed: an expired override is still the override in
  force until a *writer* performs the audited reversion.

`if_generation` is a compare-and-swap. State the generation the surface was
showing and a concurrent edit comes back **409** instead of quietly overwriting
the newer one — which is also what stops a stale delete from removing an
override its author never saw. Sending `expires_at` or `if_generation` for any
*other* config field is a **400**: nothing would ever expire a budget override,
so accepting one would be a lie.

Every edit bumps the generation, passes the `[approval].approvers` gate, and is
audited before it takes effect. When an expiry fires, the policy reverts to the
`kew.toml` values under the next generation and that reversion is itself an
audited event — a lapsed override is never a silent change. Because a
confirmation is bound to the generation it was given under, any edit or expiry
also invalidates confirmations taken under the old one.

`retention_days` is **not** an enforcement setting — nothing is deleted automatically. It is only the cutoff used when an operator explicitly runs `kew export --purge`; rows older than the cutoff are removed at that point and never otherwise. See [Operations](operations.md) for backup/restore and the manual pruning flow.

## `[tracker]`

Which work tracker the project reads from. Today this selects the source for
the `kew epic` graph and driver commands. GitHub remains the PR, review, CI,
and merge host for both tracker rails; Linear supplies project membership,
native relations, item state, and lifecycle write-back.

```toml
[tracker]
type = "github"          # "github" (default) or "linear"

[tracker.linear]
team = ""                # Linear team key used for claims and state changes
in_review_state = "In Review"  # your team's type-`started` review state name
reconcile_interval_seconds = 600  # webhook cache short-circuit; 0 = always fetch
```

With `type = "linear"`, an epic ref names a **Linear project** and dependency
edges come from Linear's native blocks/blocked-by relations — no
`kew:depends` markers are needed. Project membership is not authorization to
spend: an epic item must be unstarted/Todo and carry both `kew` and
`kew:ready`. Parent/container issues (those with children) are retained only
as diagnostic metadata and are never dispatched. The explicit delegation rail
claims assigned-to-kew non-epic items; the epic and delegation rails are
mutually exclusive and share the reservation/fleet-cap machinery.

Authentication is environment-only. Export `LINEAR_API_KEY` in the process
environment used by kew; never add its value to `kew.toml`, `.env` files,
issue descriptions, commands, or logs. The firm-operated Linear member/API
key is sufficient for this contract; an OAuth app or OAuth identity from #837
is not required.

### What works with Linear

Linear items run through two rails: [`kew epic`](cli/epic.md), where a Linear
project is the epic, and [`kew delegate`](cli/delegate.md), for issues
assigned to the kew member. Both rails have run end to end against a live
Linear workspace: dispatch, pull request, governed review, operator merge and
close.

Supported:

- Claim on dispatch: kew assigns its member and moves the item to the team's
  started state.
- Comments: a terminal dispatch failure and a delegation refusal each leave a
  comment on the item.
- Close: the item moves to the team's done state when its pull request merges,
  including a merge made outside kew.
- The no-change outcome: kew comments on the item and, when the outcome
  closes it, moves it to the done state.
- Native blocks/blocked-by relations as dependency edges, the `kew` plus
  `kew:ready` admission gate, and parent issue exclusion.
- The epic item verbs take Linear ids: `kew epic retry`, `kew epic unhold`,
  `kew epic continue` and `kew epic abandon`.
- Every Linear API failure ends the tick with a one-line classified
  diagnostic, never a traceback. Reads retry outages (a DNS or connect failure,
  a timeout, a reset, any 5xx, an edge 429) and then report
  `linear-unavailable`. A write that gets a 5xx or edge-429 reply is not
  retried, because it may have applied; the tick reports
  `linear-outcome-unknown`. TLS, authentication, other 4xx and GraphQL errors
  fail fast as `linear-auth` or `linear-error`. See
  [`kew epic advance`](cli/epic.md#kew-epic-advance-run-one-driver-tick).
- [Linear webhooks](linear-webhooks.md) through `kew ingest serve`. Webhook
  delivery has automated coverage only; without webhooks the driver polls.

GitHub-only at launch:

- Labels. kew writes no labels on a Linear item, and it never moves an item
  into the review state. It reads that state.
- Cost-approval holds and `kew approve` / `kew deny`, including the TUI
  approval dialog.
- The dashboard's queue and candidate views and the web queue actions.

Per-issue commands such as `kew dispatch`, `kew attach` and `kew approve` take
GitHub issue numbers. Linear items run through `kew epic` and `kew delegate`
instead. Pull requests stay on GitHub for both rails, so `kew review <PR>`
works for either.

## `[delegation]` {#delegation}

> **NOTE:** `kew delegate watch` requires `tracker.type = "linear"`. Supervised use
> is safe as of #966 (the reservation lifecycle no longer deadlocks the fleet) and
> #971 (refusal-episode hardening: comment once per episode, a reconcile retry
> surface for a failed unclaim, and a fatal exit on an expired/revoked key). Fully
> unattended operation is pending the remainder of the `delegation-followups` epic.
> See [`kew delegate`](cli/delegate.md) for details.

Assign-to-kew delegation settings. A human assigns a Linear issue to the kew member
(or adds the configured label) and `kew delegate watch` claims → dispatches it as a
one-node ephemeral epic under the [`[fleet] max_concurrent`](#fleet) cap.

```toml
[delegation]
assignee = ""                   # The kew Linear member name (required at watch start)
label = "kew:ready"             # Linear label that signals a delegated issue is ready
poll_interval_seconds = 20      # Throttled Linear API poll floor (seconds)
fast_interval_seconds = 2       # Local tracker-events read cadence (seconds)
refusal_cooldown_seconds = 300  # Skip re-claiming a budget-refused item this long (0 = off)
```

- **`assignee`** (required at watch start, default `""`) — the Linear display name of the
  kew seat. Resolved to a member ID at startup; the watcher refuses to start if blank.
- **`label`** (default `"kew:ready"`) — a Linear label name. Only issues that carry this
  label (at re-verify time) are claimed; other assign-to-kew events are ignored.
- **`poll_interval_seconds`** (default `20`, must be `>= 1`) — the minimum seconds between Linear API polls.
  The watcher may poll less frequently if no events are pending; it never polls faster.
- **`fast_interval_seconds`** (default `2`, must be `>= 1`) — the seconds between local `tracker_events` reads
  (the in-process cursor store). Decoupled from the Linear poll to keep the local loop
  responsive without burning API quota.
- **`refusal_cooldown_seconds`** (default `300`) — after a budget/preflight refusal, the watcher
  skips re-claiming that item for this many seconds, damping the refuse→comment loop a
  surviving trigger label would otherwise drive every poll. kew also comments only once
  per refusal episode (#971), so a persistently over-budget item is not re-commented on
  every cooldown expiry. `0` disables the cooldown.

## Governance attestation

> **Policy attestation (#250).** On every dispatch, kew records a `policy_hash`
> over a redacted snapshot of the governance config — `[budget]`, `[approval]`
> (threshold, label, approvers), `[routing]`, `[security]` patterns, the
> per-run/limit caps, `[run_reports] sink_path` (since 2026-08-25), and the
> deployed `.claude/hooks/security-guard.sh` digest.
> Secrets (e.g. `[alerting] webhook_url`) are never hashed or retained. Resolve
> a hash with `kew audit policy <hash>`.
>
> **Reported-model (#251).** The runner-reported resolved model is recorded per
> run as `reported_model` alongside the configured `[agent] model`, and surfaced
> in `kew report` as a "Reported model" breakdown. Runners that report nothing
> (opencode) produce a null value.

## `[audit]` {#audit}

Tamper-evidence policy for `kew audit verify --anchors` (see
[Audit log & tamper-evidence](audit.md#anchor-witness-model)).

| Key | Default | Description |
| --- | --- | --- |
| `staleness_window` | `0` | How many events the live chain may trail the latest anchor's `count` before verification fails **closed**. In honest operation the live DB is at or ahead of the newest anchor (anchors trail the head), so any positive lag beyond this window signals truncation or a stale/lagging anchor. `0` demands the live chain be at least as long as the latest anchor. Raise it to the number of events a legitimately-behind clone may trail. Env override: `KEW_AUDIT_STALENESS_WINDOW`. |
| `anchor_max_age_days` | `7` | How old the newest anchor may be before `kew doctor` warns that the chain is **unwitnessed**. `staleness_window` measures the chain against the anchors that exist; this measures whether anchors are still being *taken* — a silently dead anchor workflow leaves a chain that verifies perfectly and is witnessed by nobody (#1408). Raise it if your anchor cadence is slower than daily; `0` turns the alarm off. Advisory: it never changes a `kew audit verify` exit code. Env override: `KEW_AUDIT_ANCHOR_MAX_AGE_DAYS`. |

```toml
[audit]
staleness_window = 0
anchor_max_age_days = 7
```

## `[approval]`

Pre-dispatch approval gate. When `threshold_usd` is set, any dispatch whose
*estimated* cost meets or exceeds it requires explicit human sign-off.
Estimates are the median of the last 10 completed runs matching the issue's
complexity and model; with fewer than 3 matches kew assumes the worst case
(`budget.per_issue_usd`) and says so.

```toml
[approval]
# Intentional activated examples; threshold_usd is unset by default.
threshold_usd = 5.0
label = "needs-approval"
approvers = ["alice", "bob"]
```

- **`approvers`** (optional, default `[]`) — a GitHub-login allowlist enforced by
  `kew approve`/`kew deny`. On the kew host an empty list admits any actor:
  `kew approve` and `kew deny` run no write-access check. Through `/approve` and
  `/deny` comments, the `approval.yml` workflow admits only a collaborator with
  repo write access, and the list narrows that further. Set it to narrow
  approval to specific people. Prompt profiles do not share the empty-list
  rule: while the list is empty, every [prompt profile](prompts.md) decision is
  refused. See [`kew approve`](cli/approve.md) for the terminal-free
  remote-approval flow (#236).
- **`[alerting] notify_on_hold`** (default `true`) — when a dispatch is held, kew
  comments a hold notice on the issue (and POSTs to `webhook_url` if set): it
  names `kew approve <issue>` and `kew deny <issue>` on the kew host, and says
  the `/approve`·`/deny` comments work only with a
  `.github/workflows/approval.yml` workflow installed, which neither
  `kew setup` nor `create-kew` installs. Set `false` to suppress.

- **Interactive** (`kew run` on a TTY): kew shows the estimate and its basis
  and asks for confirmation. Declining returns the issue to `ready`.
- **Non-interactive** (`kew run --drain`, scheduler, TUI, no TTY): the issue is held
  with the `needs-approval` label and the queue moves on. Release it with
  `kew dispatch N --approve`.
- `--force-budget` bypasses *budget* checks only — it never bypasses the approval
  gate. The two flags are independent and composable.
- All gate decisions are recorded in the audit trail (`kew audit tail`) as
  `approval_hold`, `approval_granted`, `approval_confirmed`, or
  `approval_declined` events with the estimate, basis, and threshold.

**`commands.verify`** — a command kew injects into the agent's prompt for the *agent* to run as a final check before committing; kew does not execute it itself (contrast `commands.verify_loop` below, which kew's loop gate runs directly). Defaults to the value of `commands.ci` if not set. Use this when your full CI suite is too slow or broad for the agent's inner loop — point `commands.ci` at your full suite and `commands.verify` at a faster subset:

```toml
[commands]
# Intentional example: use different full and inner-loop gates.
ci = "make test"
verify = "make test-fast"
```

**`commands.test_targeted`** — the scoped test invocation kew renders into the
**governed remediation brief** (the round that fixes adversarial-review
findings). That round runs only the tests covering its changed files plus each
finding's regression test — never the full suite, which runs before any merge —
so it cannot be given `commands.ci`. The value must contain the `{paths}`
placeholder (kew refuses to load a config without it: a bare runner such as
`uv run pytest` would run everything, defeating the scope the brief sets); the
agent substitutes the test paths it selected. Empty (the default) is allowed and
the brief then says out loud that no targeted test command is configured, rather
than silently naming none.

**`commands.test_policy_mark`** — the pytest mark carried by the repo-wide policy
gates an unsandboxed prompt **always** has the agent run, even when the changed
files do not import them. Targeted selection picks tests by the files a
remediation touched, so a gate that no module imports is never selected: a
remediation that added silent `except` handlers passed its own green round and only
failed the repo's no-silent-defaults gate in the pre-merge verification. Naming the
gates by mark (e.g. `policy`, applied with `@pytest.mark.policy`) rather than by
path keeps the set honest — a gate added later is in the set the moment it is
marked, whereas a hand list drifts and lets the next unscanned gate through. kew
renders the mark into `commands.test_targeted` in place of `{paths}`, so
`test_targeted = "uv run pytest {paths}"` and `test_policy_mark = "policy"` make the
unsandboxed implementation dispatch prompt and a standalone `kew review`
remediation brief on the host name `uv run pytest -m policy`. Those runs run the full
suite but must still run the gates on their own after their last edit. A governed
remediation brief adds them to its targeted set when the round runs on the host,
including under a runner's own `native` isolation, which has the host's memory. A
sandboxed dispatch, and any remediation round (governed or standalone) kew runs in
its container, run inside the sandbox's memory cap (8 GB, or the lower `[sandbox]
max_memory`), where the full suite and the gates,
selected across the whole tree, count against the memory cap, so those prompts name
only targeted tests and static gates and tell the agent not to run the rest: the
merge verify runs them on the verify host. A sandboxed dispatch prompt also names
`commands.test_targeted` as the runner for its targeted tests, and has the agent run
`commands.verify` only when that command is a scoped check rather than the full
gate. Every
prompt that runs the gates tells the agent to fix a
failing gate in the same run, or else stop with
`KEW_NEEDS_HUMAN: policy gate failed: <test ids>`. kew then refuses to publish the
run and records the reason, which names the failing tests. The value must be a
bare marker name. Empty (the default) adds nothing.

Each sandboxed prompt names the memory cap kew enforces on that runtime: `[sandbox]
max_memory` when it is set, the 8 GB ceiling otherwise. An OpenShell sandbox gets
no cap from kew, so its prompt says that kew enforces none rather than naming one.

**`commands.static`** — the merge gate's static checks (for example ruff, ruff
format, mypy, lint-imports, bandit, semgrep and xenon, or a script that runs
them). The epic drive runs this command on an agent head **before** it launches
an adversarial reviewer. A failure becomes a remediation round. The round's
findings carry the command's output and name each file and line it cites, and
no reviewer launches on that head. A clean head goes to review as before. kew
does not run the command again on a head that already passed. The step runs only
when a reviewer could launch: not while a usage limit is being waited out, not
while a local merge verify runs, and inside the review slot the fleet cap counts.

The command runs **only inside a container**: [`[sandbox]
check_image`](#sandbox) when one is set, else the approved runner image, with no
network, no credentials and none of the host's environment, over a throwaway
clone of the head. It never runs on the host, even with `[sandbox]
dangerously_skip_containment = true`, because mypy loads the plugins the
candidate's `pyproject.toml` names, which runs candidate code. Every git step on
the host (the fetch, the diff, building the merge candidate) runs before any
container starts, in a clone no container mounts, and each container gets a
clone of its own, so nothing the candidate writes there reaches host git.

The container has no network, so the image must already carry everything the
command needs: the tools, and the project's dependencies built in. A clone of
the head has no virtualenv and no `node_modules`. kew's own runner image carries
neither the project's Python environment nor its frontend packages, so a command
such as `uv run ruff check .` cannot pass in it. Point [`[sandbox]
check_image`](#sandbox) at an image built with them; `[sandbox] images` picks the
image an agent runs in, not this one.

A failure counts against the head only when the same command passes on the base
commit. kew runs it there once, only after the head fails. When the base fails
too (a red base, or an image without what the command needs), or the shell
reports that the command is not there or not executable, the check is skipped
and logged, never a finding. With no container available, or a gate that
could not launch or timed out, the step does not run and the head goes to
review. That is not a defect in the head, and the merge verify still runs every
gate. A command that prints more than the output budget (256 KiB per stream) is
not in that list: it counts as a failure with the output it kept, and the base
run decides as for any other failure. In a `check_image`, a head that changes
a file listed in [`[sandbox] check_image_inputs`](#sandbox) (when that list is
empty, any dependency manifest or lock in the tree), and that `[epic]
merge_gate_paths` also names, is not judged by the base: the image's
dependencies predate the head's, so a red result there (a missing tool aside)
is skipped with the reason "the head changes the check image's inputs". Such a
head is held for a person at merge by the merge queue's gate-path hold, and the
merge verify runs the gate. When that diff cannot be taken (the head shares no
history with the base), the base decides as usual. A skipped check is not run
again on the same head.

A skip is recorded, never silent. Whenever the gate did not check the head that
goes to review (a launch the runtime refused, a timeout, a tool the image lacks,
a red base, a stale check image, or a gate that could not run at all), kew
records the skip and its reason for that item and head. The reason is redacted
before it is kept (credentials, secret assignments, URL userinfo and queries,
the directories of an absolute path, IP addresses, GCP resource paths and
`*.internal` hosts, `gs://` buckets, `user@host` and email addresses, the
`[hosts.gcp]` project, zone and instance names and this machine's host name
are removed), because the verdict comment is public. Every epic tick prints a
`pre-review gate skipped: #N — <reason>` line for each node in review whose
current head the gate skipped, read from the run store, so a skip a detached
review child recorded is printed too. `kew epic status` prints one per open
item naming the head (none for a done or cancelled node), and the PR's verdict
comment for that head says the pre-review gate did not check it and why. A gate
that ran and passed adds nothing.

These rounds do not count toward `[review] max_rounds`. They have their own
bound: once three of them are recorded in one review cycle, a fourth failure
holds the node as `pre_review_gate_exhausted`, a reason that names the failing
check and its first failing line. `kew epic unhold` releases it and restarts the
review cycle. The same holds with `[review] blocking = false`: the node is held,
never attested degraded and sent on, because no reviewer read that head, and a
head that fails the merge gate's own static checks cannot pass the merge verify
either. Operator config only. Empty (the default) adds no step.

**`commands.ui_check`** — the browser checks for the surfaces a head touches,
run in the same step on the **merge candidate**: the current `origin/<base>`
merged into the head in a throwaway clone. The merge commit never leaves that
clone, which is discarded, so nothing is pushed. `{modules}` is replaced by the
ui-check modules the head adds or changes, plus every module whose
`// ui-check-owns: <glob>` lines own a `frontend/` file the head changed. The
command needs an image with a browser (Chromium) and the frontend's packages
installed. kew's runner image has neither, so a ui-check command fails there
and the step skips it; set [`[sandbox] check_image`](#sandbox) to an image that
has both. A head that touches no module skips the command. The
base control runs the modules the base also has. A changed file under
`frontend/src/` (tests aside) that no module owns is logged as uncovered, and
the browser checks are not counted as passed for that head. A failing check, or
a base that does not merge cleanly, becomes a remediation round in the same way
as `commands.static`. Empty (the default) adds nothing.

**`commands.verify_loop`** — the command kew's **loop verification gate** runs to
decide whether a `kew dispatch <issue> --loop` loop has genuinely met its goal.
Unlike `commands.verify` (which is *prompt text the agent reads* before
committing), `verify_loop` is **executed by kew itself**: exit 0 means the goal
is met and the loop may close as `achieved`; any non-zero exit keeps the loop
open or sends it for review. A loop never closes on the agent's word alone — the
gate's verdict is the sole authority (the anti-proxy invariant). For safety, if
the loop's diff edits any test file, any `conftest.py`, the verify command's
script, or `.github/workflows/**`, the gate returns a **sabotage** verdict and
refuses success (self-sabotage defense). The gate only *returns* this verdict;
kew's loop driver is what acts on it (closing the loop as `achieved`, or routing
a failed/sabotage verdict to `needs_human`). This command must be set by the
**operator** in `kew.toml` only — it is executed as a shell command, so it must
never come from an issue or agent. Empty (the default) falls back to
`commands.ci`. Canonical command verification runs contained when a local runtime and approved runner image are available.
The host fallback requires missing prerequisites and `[sandbox] dangerously_skip_containment = true`.
See [loop containment](cli/loop.md) for admission and timeout rules.

**`commands.verify_loop_needs_writes`** — defaults to `false`.
Set it to `true` only when the command needs a writable disposable verification checkout.
The container root stays read-only. Only operator configuration may set this declaration.

## `[agent.opencode]`

Settings for the [OpenCode](https://opencode.ai) multi-provider runner. OpenCode covers Gemini, GPT/o-series, Ollama, OpenRouter, and 70+ other providers through a single integration.

**Routing:** Any model written as `provider/model` (e.g. `google/gemini-2.5-pro`, `openai/gpt-5.1-codex`, `ollama/llama3`) routes to OpenCode automatically. Bare prefixes `gemini-`, `gpt-`, `o1-`, `o3-`, `o4-`, and `ollama/` also route to OpenCode.

**Credentials:** OpenCode manages its own provider credentials. Run `opencode auth login <provider>` once before dispatching jobs to a new provider. Kew does not store API keys for OpenCode-routed providers.

```toml
[agent.opencode]
extra_args = []   # Pass-through CLI flags appended to every `opencode run`
agent = ""        # Optional: select an OpenCode agent preset via `--agent <name>`
```

**Known limitations (v1):**
- No native budget enforcement — kew's outer dispatch budget gates apply instead.
- No attached-server mode — every dispatch spawns a fresh `opencode run` process.

## `[agent.codex]`

Settings for the [official Codex CLI](runners/codex.md). Codex is selected only
with an explicit `runner = "codex"` route and requires a nonempty priced model;
GPT/o-series inference remains mapped to OpenCode.

```toml
[routing]
default = "claude-code"
# Intentional routing example.
simple = { runner = "codex", model = "gpt-5.6-sol" }

[agent.codex]
# Intentional activated examples.
reasoning_effort = "high" # "", minimal, low, medium, high, or xhigh
service_tier = "fast"     # Optional; the only accepted nonempty value
```

There is deliberately no `extra_args` field. The official CLI must be version
0.145.0 or newer and authenticated with `codex login`. kew preserves `HOME` for
that official CLI login while sanitizing provider, GitHub, and AWS credentials
from the process environment.

When `service_tier` is unset, kew sends no service-tier override. When it is
`fast`, kew explicitly sends both `-c service_tier="fast"` and
`-c features.fast_mode=true`; this remains effective under
`--ignore-user-config`. Codex itself validates account and model eligibility.
The current Codex JSONL and kew run schema do not expose a reported service
tier, so kew does not infer or persist one.

On `runtime = "docker"` and `"podman"` Codex runs on the host with Codex-native
`workspace-write`; kew admits that runner-owned boundary as posture `native`,
brokers commit, push, PR, and report, and needs no container runtime or image
for it. Such a native Codex run can read any file the host user can read,
including credential files such as `~/.codex/auth.json`, and writes only its
checkout and a temp directory of its own under the work root, never the shared
`$TMPDIR` or `/tmp`. On `runtime = "openshell"` kew contains the dispatch itself instead, so
it needs `[sandbox.images].codex` and `[sandbox.openshell].providers.codex` —
see [the Codex reference](runners/codex.md#posture-per-runtime) for the runs on
that runtime that stay `native`. Its container credential declaration remains
empty on every runtime (`required_credentials == ()`); do not mount a personal
`.codex` directory or provide an API/access-token fallback.

## `[agent.pi]`

Settings for the [Pi](https://pi.dev) runner (`@earendil-works/pi-coding-agent`), a minimal, multi-provider terminal coding agent. Kew drives it via `pi --mode json -p <prompt>`, consuming pi's JSON event stream.

**Routing:** Pi is selected explicitly only — set `runner = "pi"` in `[agent]` or a `[routing]` entry. Pi shares the `provider/model` naming convention with OpenCode, so kew does not auto-route models to it (that would collide with OpenCode). Models are written `provider/id`, e.g. `anthropic/claude-sonnet-4-6` or `openai/gpt-5.1`.

**Credentials:** Pi manages its own provider credentials — set the relevant env var (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, …) or run `pi` then `/login` for OAuth once. Kew does not store API keys for pi-routed providers. A missing credential is reported as a permanent failure.

```toml
[agent.pi]
extra_args = []   # Pass-through CLI flags appended to every `pi` run (e.g. ["--thinking", "high"])
```

**Known limitations (v1):**
- No native budget enforcement — kew's outer dispatch budget gates apply instead.
- Pi reports per-message token usage and native cost; kew accumulates tokens and prices them via its own pricing tables (same as other runners).

## `[agent.lemonade]`

Settings for kew's own tool-use loop over a local OpenAI-compatible endpoint. Supports
AMD Lemonade, LM Studio, llama.cpp's `llama-server`, vLLM, Ollama, and any other
server that speaks the OpenAI `/v1/chat/completions` API. Cost is $0 (local); nothing
leaves the machine. See the [Lemonade runner guide](runners/lemonade.md) for
prerequisites, model selection, and examples.

**Routing:** Set `model = "lemonade/<model-id>"` in `[agent]`; the `lemonade/` prefix
auto-routes to this runner — no explicit `runner` field needed.

```toml
[agent.lemonade]
base_url = "http://localhost:13305/v1"  # Any OpenAI-compatible local endpoint
# api_key = ""               # Usually unnecessary for a local server; leave blank
max_steps = 40               # Max model turns per run (outer dispatch budget gate also applies)
extra_args = []              # Pass-through CLI flags appended to every kew-lemonade run
```

**Environment variable overrides** (useful in CI):

| Variable | Overrides |
|----------|-----------|
| `KEW_RUNNER` | `agent.runner` |
| `KEW_TIMEOUT` | `agent.timeout_minutes` |
| `KEW_REPO` | `project.repo` |
| `KEW_MAX_BUDGET` | `agent.max_budget_usd` |
| `KEW_LOG_DIR` | `logging.dir` |
| `KEW_MAX_WORKERS` | `parallel.max_workers` (and `fleet.max_concurrent`, unless `KEW_FLEET_MAX_CONCURRENT` is set — see [`[fleet]`](#fleet)) |
| `KEW_FLEET_MAX_CONCURRENT` | `fleet.max_concurrent` |
| `KEW_WORKTREE_BASE` | `branches.worktree_base` |
| `KEW_WORK_ROOT` | `paths.work_root` — see [`[paths]`](#paths) |
| `KEW_PROJECT` (or `--project`) | config + DB directory — see [Project selection](#project-selection-project-kew_project) |

The older names `DISPATCH_TIMEOUT`, `DISPATCH_REPO`, `DISPATCH_LOG_DIR`, `DISPATCH_MAX_BUDGET`, `DISPATCH_MAX_WORKERS` and `WORKTREE_BASE` are no longer read. kew prints a warning naming the replacement when one is set.

## `[flight_recorder]` {#flight_recorder}

Settings for `kew serve` — the approval inbox and web UI.

```toml
[flight_recorder]
host = "127.0.0.1"    # Bind host (default: loopback)
port = 8765           # Bind port
expose = false        # Required to bind a non-loopback host
extra_origins = []    # Extra browser Origins to trust for writes (reverse proxy)
trusted_proxies = []  # Reverse-proxy addresses whose X-Forwarded-* kew believes
```

- **`host`** (default `"127.0.0.1"`) — the address `kew serve` binds to. Loopback (`127.0.0.0/8`, `localhost`, `::1`) is accepted without further configuration. Any non-loopback value requires `expose = true` (see below).
- **`port`** (default `8765`) — the port `kew serve` listens on. Must be in the range 1–65535.
- **`expose`** (default `false`) — opt-in to binding a non-loopback `host`. When `true`, `kew serve` may bind an externally reachable address, and all reads (not just writes) require a valid session. Must be set explicitly to prevent accidental exposure. Exposing additionally requires a non-empty `trusted_proxies`: kew never terminates TLS, so `kew serve` refuses to start rather than serve the token over plaintext (see below).
- **`extra_origins`** (default `[]`) — additional browser `Origin` values to trust for writes. Needed only when fronting `kew serve` with a TLS reverse proxy: the browser sends its public origin (e.g. `https://kew.example.com`, no internal port), which never matches `host:port`, so writes would silently `403`. Each entry must be a bare origin — scheme + host, optional port, no path/query/fragment (e.g. `["https://kew.example.com"]`). Reads are unaffected. See [Reverse proxy + TLS](cli/serve.md#reverse-proxy-tls).
- **`trusted_proxies`** (default `[]`) — the addresses of the reverse proxies in front of `kew serve`, as IP addresses or CIDR networks (e.g. `["127.0.0.1"]`, `["10.0.0.0/24"]`). A network must carry no host bits (`10.0.0.0/24`, not `10.0.0.5/24`) and `/0` is rejected like `*`; both fail at config load. It narrows two things:
    - `X-Forwarded-For` / `X-Forwarded-Proto` are honoured **only** from these peers. Empty means kew believes no forwarded header from anyone — not even from `127.0.0.1`, which the ASGI server would trust by default, and which would otherwise let any local process rewrite the peer address the read guard checks.
    - Listing at least one address is how an operator declares that a TLS terminator sits in front. Because kew never terminates TLS, `expose` with an empty `trusted_proxies` would send the admin token and the session cookie over plaintext HTTP, so `kew serve` refuses to start in that combination instead of degrading.

    Hostnames are never resolved and `"*"` is rejected: an entry the proxy allowlist cannot parse would match no peer and be silently inert, so it fails at config load instead. See [Reverse proxy + TLS](cli/serve.md#reverse-proxy-tls).

> **Migration note.** Before this field existed, a non-loopback `[flight_recorder].host` was accepted silently. If you have an existing `kew.toml` with a non-loopback `host` and no `expose` setting, any `kew` command will now fail to load config with an error like:
>
> ```text
> [flight_recorder].host='0.0.0.0' is non-loopback; set [flight_recorder].expose = true to bind it intentionally.
> ```
>
> Add `expose = true` to your `[flight_recorder]` section to restore the previous behavior. Be aware that this makes the inbox reachable beyond localhost — see [Security: Approval inbox](security.md#approval-inbox) for the full threat model.

The `--expose` CLI flag overrides `expose = false` in config for a single `kew serve` invocation.

## `[serve]` {#serve}

Opt-in console features that spend. Off by default.

```toml
[serve]
palette_ask = false     # Let the Cmd-K palette ask a model to rank its entries
palette_ask_model = ""  # Model for palette ask; empty falls back to [agent] model
```

- **`palette_ask`** (default `false`) — turns on the palette's ask mode. A query
  ending in `?` (or `Tab` on any query) sends the query and the ids and labels of
  the entries the palette is showing to `POST /api/palette/ask`; the model ranks
  up to five of **those** entries with a one-line reason each, and any id it
  names that the palette did not offer is dropped. It never acts: a ranked
  entry is an ordinary palette entry, and selecting one does exactly what
  selecting it directly does, confirm step included. While `false` the route
  answers `404` and the palette offers no ask. The call is the same non-agent
  JSON call the [triage](#triage) `local` backend makes, to the endpoint in
  [`[agent.lemonade]`](#agentlemonade); each one is recorded as an
  `interactive_usage` row (`ingest_source = "palette_ask"`) priced like any
  other model use. The row totals every call the ask made — an answer the
  model had to be asked to repair counts both turns — and an ask that fails
  after a turn came back (the route answers `503`) still records that turn's
  spend. No item body, token or cookie is sent — titles only. An ask counts
  against `[budget] daily_usd`: once today's dispatched and
  `interactive_usage` spend has reached it, the route answers `429` with one
  sentence naming the cap, before any model call and without recording
  anything.
- **`palette_ask_model`** (default `""`) — the model asked. Empty falls back to
  `[agent] model`; with neither set the route answers `503`.

## `[budget]` {#budget}

Spend caps, in USD at API-equivalent list price: kew records the
API-equivalent list price of the tokens each run used, whatever your plan
bills. The
three keys and their defaults are in the full example above: `per_issue_usd`,
`daily_usd` and `weekly_usd`.

kew checks every cap before it dispatches, against the runs it has recorded:
a dispatch whose issue, day or week is already past its cap is refused. The
caps can be overridden live on the web console's Budget page (see
[Override precedence](#override-precedence)).

<!-- demo-step: where-did-the-money-go/3 -->
**Spend against caps**

The Budget page shows today's and this week's spend against their caps: the
list price of every run kew started, reviews included. Today is the UTC day;
the week is today and the six days before it.
<!-- /demo-step -->

[See it in the demo](https://demo.kew.dev/budget?tour=where-did-the-money-go&step=3)

The daily and weekly caps stop every new run: dispatches, CI fixes, conflict
resolution, loops, planning, readiness probes, and a work item's own reviews
and remediation. kew refuses a run when a window's recorded spend is past its
cap, or when runs are already in flight and their worst case plus the new
run's own ceiling would pass it. A run's ceiling is `[review] max_review_usd`
for a reviewer, `[review] max_remediation_usd` for a remediation, and
`[agent] max_budget_usd` for anything else. A run already in progress is never
stopped. A review or remediation also needs its item's review pool
(`max_review_usd`, `max_remediation_usd`) to have room; passing one check is
not enough.

When a cap refuses a work item's review or remediation, the drive holds the
node with the reason `budget: daily cap $50.00 reached ($51.20 spent)` (or
`weekly`). The spend is the window's recorded spend, the figure the Budget page
shows. When runs in flight caused the refusal, the reason names their
reservations apart: `budget: daily cap $20.00 reached ($5.00 spent, $10.00
reserved by running or starting runs)`. The refused run's own ceiling is in
neither number. The hold is not a review round and not a remediation attempt,
and it writes no verdict. Each tick checks both caps again, daily before
weekly, and rewrites the hold when the first cap that refuses is not the one
it names: a daily hold becomes a weekly one once the day rolls over while the
week is still past its cap, and a weekly hold becomes a daily one whenever the
daily cap refuses too, even while the weekly cap still does. Spend moving
within the same window does not rewrite it. It clears by itself once the window moves on at UTC
midnight or the cap is raised; a cap raised in the console applies at once. The
next tick then runs the review or remediation. `--force-budget` still bypasses
the caps on a hand-run `kew dispatch`, `kew review` or `kew resolve`; the
drive has no bypass.

## `[ingest]`

Cross-runner usage ingestion. Lets kew maintain one self-hosted record spanning dispatched runs and interactive usage from Claude Code. Off by default; opt in by setting `enabled = true`.

```toml
[ingest]
enabled = false               # master switch for `kew ingest serve`
host = "127.0.0.1"            # receiver bind host; loopback-only
port = 4318                   # OTLP/HTTP default
cap_usd = 0.0                 # 0 = no cap alert
combine_with_dispatch = false # cap against interactive only, or interactive + dispatched
```

- **`enabled`** (default `false`) — must be `true` for `kew ingest serve` to start. `kew ingest file` works regardless.
- **`host`** (default `"127.0.0.1"`) — the bind host for `kew ingest serve`. Only loopback addresses are accepted: IPv4 `127.0.0.0/8`, the literal `localhost`, or IPv6 `::1`. Any non-loopback value is rejected at startup. The receiver is local-only by construction; it is not designed to accept remote connections.
- **`port`** (default `4318`) — the OTLP/HTTP standard port. Must be in the range 1–65535.
- **`cap_usd`** (default `0.0`) — when greater than zero, `kew report` emits one webhook notification if the relevant spend meets or exceeds this value. This is a **reminder alert, not an enforced limit** — kew cannot interrupt an interactive session. Effectively requires `[alerting].webhook_url` to deliver.
- **`combine_with_dispatch`** (default `false`) — controls the spend basis for `cap_usd`. `false` = interactive-only spend; `true` = interactive + dispatched combined.

Cap alerts fire once per `kew report` invocation — they are stateless. Re-running `kew report` while spend remains above cap will re-alert.

See [`kew ingest`](cli/ingest.md) for the full guide, including the Claude Code environment variables for `kew ingest serve` and how interactive usage appears in `kew report`.

## `[billing]`

Records how API-equivalent costs map to your actual spend. kew always computes cost from published per-token API pricing. `[billing]` tells kew what that number means for your account so the value/leverage banner in `kew report` can frame it honestly (e.g. "included in subscription" vs. "real spend").

```toml
[billing]
mode = "metered"       # default
plan_label = ""        # e.g. "Claude Max"
plan_monthly_usd = 0.0
```

- **`mode`** (default `"metered"`) — `"metered"` means you pay per token via an API key; list price ≈ real spend. `"subscription"` means token usage is included in a flat plan.
- **`plan_label`** (default `""`) — display name of the plan, shown alongside cost figures in `kew report`. Empty = omit.
- **`plan_monthly_usd`** (default `0.0`) — monthly cost of the subscription, used to compute value leverage in the report banner.

## `[fleet]` {#fleet}

How many agent runs the **whole fleet** may have in flight — on this machine, on
a Compute Engine instance, or on both at once.

`[parallel] max_workers` was this bound for as long as there was one machine to
spend it on: a worker was a process here, so counting workers and counting
agents were the same count. Once a run can land on a VM
([`[sandbox] placement`](#sandbox-host)) they come apart, and VMs **replace**
local slots rather than add to them — the cap is about how many agents exist
anywhere.

```toml
[fleet]
# max_concurrent = 3   # unset = [parallel] max_workers
```

- **`max_concurrent`** (default: unset, falls back to `[parallel] max_workers`)
  — the fleet-wide in-flight cap. It is counted off the **ledger** (every run in
  a running state, whatever host it sits on), not off any machine's process
  table, so the next run waits on the cap whichever host it would have gone to.
  Values below `1` are refused, exactly as for `max_workers`: this is the number
  the drain sizes its worker pool from and the epic driver spends as its
  dispatch budget, so a `0` would halt the fleet rather than unbound it.
  Overridable with `KEW_FLEET_MAX_CONCURRENT`, and with `--workers` on
  [`kew run`](cli/run.md) — that flag sets this cap, not just the local worker
  count, so it is never shadowed by a configured `max_concurrent`.
  `KEW_MAX_WORKERS` moves this cap too, for the same reason: the environment
  layer sits above the file, so an exported worker count would otherwise be
  silently ignored by exactly the operator who set a `max_concurrent` in
  `kew.toml`. An explicit `KEW_FLEET_MAX_CONCURRENT` wins over it, and
  [`kew config effective`](cli/config.md) names whichever variable is in force
  as this cap's source — never `kew.toml`, whose number the environment replaced.

Per-host capacity is separate and sits with the host:
[`[hosts.gcp] max_runs`](#hosts) bounds one instance, while this bounds the sum.

## `[overlap]`

Controls how kew handles concurrent dispatch to overlapping files. When two queued issues touch the same paths, the configured strategy decides what happens.

```toml
[overlap]
strategy = "serialize"   # default

[overlap.advanced]
merge_context_ttl_minutes = 60
```

- **`strategy`** (default `"serialize"`) — `"serialize"` queues the second dispatch behind the first; `"warn"` proceeds but emits a warning; `"off"` disables overlap detection entirely.
- **`[overlap.advanced].merge_context_ttl_minutes`** (default `60`) — how long (in minutes) the merge context window is retained between overlapping dispatches.

The `overlap_hold` label (default `"overlap-hold"`, configured under `[labels]`) is applied to an issue that is queued behind a running dispatch under the `"serialize"` strategy.

## `[triage]`

The triage station: a classifier reads one work item and proposes what it is;
a person decides. Off by default. **In this release every outcome is a
proposal** — the station applies no label, writes no comment, never marks an
item `ready` and never closes one. See [`kew triage`](cli/triage.md).

```toml
[triage]
enabled = false
intake_label = "kew:triage"
backend = "local"
model = ""
fallback_backend = "runner"
min_confidence = 0.8
human_types = ["security", "question"]
dedupe_candidates = 10
max_body_chars = 20000
max_usd_per_item = 0.5
skip_epic_nodes = true
```

- **`enabled`** (default `false`) — must be `true` for `kew triage <item>` to
  run. `kew triage queue` and `kew triage decide` stay available either way, so
  an operator can always drain a queue they no longer want to grow.
- **`intake_label`** (default `"kew:triage"`) — the label an operator puts on an
  item to say "triage this". kew only ever **reads** it: the shadow-mode
  station is driven by `kew triage <item>`, so the label reaches the classifier
  as one of the item's existing labels and nothing scans for it yet.
- **`backend`** (default `"local"`) — `"local"` or `"runner"`. `"local"` sends
  one non-agent JSON call to the OpenAI-compatible endpoint configured in
  [`[agent.lemonade]`](#agentlemonade). `"runner"` is reserved for a registered
  runner and is not wired yet: selecting it records the `unavailable` outcome
  with that reason rather than failing quietly.
- **`model`** (default `""`) — the model the classifier asks for. Empty falls
  back to `[agent] model`; with neither set, triage records `unavailable`.
- **`fallback_backend`** (default `"runner"`) — tried when `backend` cannot
  answer. When it cannot either, the outcome is `unavailable` — never a
  dispatch.
- **`min_confidence`** (default `0.8`) — decisions below this are
  `human_decision`. Must be between `0.0` and `1.0`.
- **`human_types`** (default `["security", "question"]`) — classifications that
  are a person's to route whatever the confidence. Each must be one of `bug`,
  `feature`, `refactor`, `question`, `chore`, `security`.
- **`dedupe_candidates`** (default `10`) — how many duplicate candidates the
  host looks up with `gh search issues`, over open and closed issues. The model
  may only name a `duplicate_of` from that list. `0` disables the search.
- **`max_body_chars`** (default `20000`) — the cap on the item text handed to
  the classifier. Over-length bodies are truncated with an explicit marker.
- **`max_usd_per_item`** (default `0.5`) — the ceiling on one item's
  classification spend. The shipped `local` backend talks to a local endpoint
  with no per-token price (see [`[billing]`](#billing)), so nothing is metered
  against this ceiling yet; it is the value a priced backend will be held to.
- **`skip_epic_nodes`** (default `true`) — leave items an epic already planned
  alone: their plan came from the epic, so re-triaging them proposes nothing
  new. Membership is read from the ledger's own epic journal; a journal that
  cannot be read triages the item and says so, because a skip taken on no
  evidence is worse than a redundant proposal.

Every unresolved proposal is one row in the console's needs-you set, carrying
the exact `kew triage decide` command for that item. Each decision is a
`triage_decisions` ledger row and a `triage_decided` audit event; a person's
resolution is a `triage_resolved` event naming them (see
[Audit trail](audit.md)).

## `[resolve]`

Merge conflict resolution options when a branch falls behind its base before dispatch.

```toml
[resolve]
strategy = "merge"   # default
auto = false
```

- **`strategy`** (default `"merge"`) — `"merge"` or `"rebase"`. Controls which git strategy is used when reconciling a stale branch with its base.
- **`auto`** (default `false`) — when `true`, kew attempts to resolve conflicts automatically without prompting. When `false`, a conflict surfaces as a failure and the issue may be routed to `needs_human`.
- **`python_checks`** (default `""`) — the command a resolution must pass before kew pushes it, when the integration changed Python files (`.py`, `.pyi`, deletions included). For example `"cd kew && uv run ruff check . && uv run mypy src/"` for a repository whose Python project lives in `kew/`. Empty = Python changes are pushed unchecked, logged at WARNING with the key to set.
- **`frontend_checks`** (default `""`) — the same for frontend files (`.ts`, `.tsx`, `.svelte`, `.js`, `.jsx`, `.mjs`, `.cjs`). For example `"cd frontend && npm ci && npm run check"` for a repository whose frontend lives in `frontend/`. A merge that applies cleanly can still stop compiling, and only a TypeScript check catches a removed TypeScript symbol coming back.

Each command runs from the root of the resolved worktree, which is a fresh checkout, so `cd` into the project it checks first. The checks are a cheap guard before a push, not the merge gate (the merge verify runs the full suite), so make them whole-tree static gates. A semantic conflict breaks files the integration never touched: main removes `helper()` from `util.py` while the branch adds a caller of it, and only `util.py` changed. `{paths}` is optional; when a command contains it, it is replaced by the changed files of that kind, relative to the worktree root, and the command is skipped (logged at WARNING) when none of them still exists. Because those paths are root-relative, a `cd <subdir> && …` command must not pass `{paths}` as they are: `kew/src/kew/x.py` does not exist from inside `kew/`.

kew's own `kew.toml` runs both in its [`[sandbox] check_image`](#sandbox):
`python_checks = "cd kew && uv run ruff check . && uv run ruff format --check . && uv run mypy src/"`
and `frontend_checks = "kew-check-frontend npm run check"`. Its `[commands]
static` is the static phase of `kew/scripts/verify.sh` (ruff, ruff format, mypy,
lint-imports, xenon and `pytest -m policy`), run from `kew/` in the same image.

Without [`[sandbox] check_image`](#sandbox), both run on the resolved tree under the same containment-first admission as the merge queue's verify (`[sandbox] dangerously_skip_containment` is the only way they run on the host). With `check_image` set, they run only in that image: one the runtime cannot resolve refuses the check, which holds the push, and they never fall back to the runner image or the host, whatever `dangerously_skip_containment` says. A contained check runs in the approved image with no network over a read-only checkout: it cannot install dependencies or write into the tree. The examples above install their dependencies (`uv run` creates `kew/.venv`, `npm ci` fills `node_modules`), so they work only on the host under `dangerously_skip_containment`, or in a check image that has the dependencies built in ([`[sandbox] check_image`](#sandbox), whose example shows the commands that work there). Under containment without such an image they fail, and every merge-main step that touches a file of their kind is held. A check that fails, times out or has no admitted posture pushes nothing, and the hold names the key of the check and its error lines (svelte-check's `Error:` blocks with their `path:line:col`, `error:` lines, vitest's `FAIL` lines and assertion errors; up to five, then a count), sanitized like the rest of the reason; a check that printed none names its first failing line. A conflict resolution a check refused also leaves `<log>.resolution.json` beside its run's log (owner-only, credentials redacted): that check's command and whole output, each path beneath the checked tree named from the repository root.

## `[publication]`

The host-owned publication broker moves an agent's file tree onto a branch.
Canonical publication is the default.

```toml
[publication]
publish = "canonical"  # default: "canonical" or "checkout"
shadow = false          # default
shadow_evidence = 10    # default
```

- **`publish`** (default `"canonical"`) — extract and filter checkout files,
  then commit and push from a fresh host-owned bare repository. Use
  `"checkout"` only as a temporary recovery escape hatch. That mode runs Git
  in the agent checkout. It requires `[sandbox] dangerously_skip_containment =
  true` and records `uncontained-publication` posture. kew builds its commit
  in that checkout from Git plumbing with hooks, fsmonitor, filters, signing
  and system/global configuration off. Before the push it runs the same
  publication filter as canonical mode over every pushed change: it refuses
  symlinks, gitlinks, mode escalations and `.gitattributes` drivers, and it
  holds protected paths for a person. Other Git in the checkout, such as
  fetching the remote head, still reads the checkout's own configuration.
- **`shadow`** (default `false`) — compare the broker tree against checkout
  publication only when `publish = "checkout"`. Canonical mode does not produce
  comparison evidence. It emits `shadow.skipped` with reason
  `canonical_publication` and prints one notice. Shadow mode publishes nothing,
  blocks nothing, and cannot fail a dispatch.
- **`shadow_evidence`** (default `10`) — how many non-matching comparisons keep
  their workspace under `<logging.dir>/shadow/`. A comparison that agrees is
  removed; one that does not keeps a bare clone and the extracted artifact so
  there is something to inspect, and the path is printed with the finding. Only
  the most recent `shadow_evidence` are kept, because an unbounded pile of
  clones turns an observability feature into a disk-space problem.

Shadow mode needs a container runtime and an agent image containing the
extraction producer; a comparison that cannot run is reported as `failed` and is
never mistaken for agreement.

## `[epic]` {#epic}

Settings for the [Epic DAG driver](cli/epic.md) (`kew epic advance` / `kew epic run` / `kew epic retry`) — dispatching an `epic:<name>`-labeled dependency graph in order and mechanically merging PRs a human has already approved.

```toml
[epic]
# max_parallel = 2          # unset = the [fleet] max_concurrent cap
stall_days = 3
driver_expected_interval_seconds = 300   # expected cadence of an externally-driven advance
auto_ci_fix = false         # opt-in: dispatch a bounded ci-fix run on red PR CI
merge_queue = false         # opt-in: verify base+head together before each merge
# approvers = ["alice"]     # Intentional example; empty falls back to approval
# lock_path = "./logs/dispatch/epic-driver.lock"  # unset = <logging.dir>/epic-driver.lock
```

- **`max_parallel`** (default: unset, falls back to the [`[fleet] max_concurrent`](#fleet) cap) — the maximum number of epic nodes dispatched at once. Overridable per-invocation with `--max-parallel`.
- **`stall_days`** (default `3`) — the idle-time bound used by the per-node "stuck" clock for nodes without a more specific liveness signal (in-progress runs use run-store liveness; in-review PRs use the PR's `pushedDate`; a red required check goes stuck immediately). Must be `>= 1`.
- **`driver_expected_interval_seconds`** (default `300`) — how often an *externally driven* advance is expected to run: a cron/systemd timer or a wrapper script calling `kew epic advance`. `kew epic run --interval N` records its own real cadence; a one-shot advance has none, so it records this expectation instead — which is what lets a health reading say a driver is **gone** rather than reporting "not gone" forever. It is recorded *separately* from a measured cadence, in its own column: every drive that supplies it refreshes that column — so editing this value takes effect at the next drive — and a reading says which of the two it used (`recorded` for a poller's own cadence, `expected` for this one). A measured cadence is **retired, not overwritten**, by a drive that has none: a one-shot advance drops the recorded interval only when the gap it just observed is longer than that interval allows (the same "older than twice the cadence" rule the reading below uses), so moving an epic off `kew epic run --interval 60` onto a slower cron hands the reading to this expectation instead of thresholding the retired poller's 60s forever, while a one-shot that lands in a *live* poller's inter-tick gap leaves its cadence alone. The driver lock is not what protects that: it is held for the duration of a tick, not between ticks, so a one-shot in the gap does run and does reach the heartbeat write. A driver is read as gone once its *freshest* heartbeat — the newest of the tick stamps and the driver lock's own mid-tick heartbeat — is older than twice the cadence, or once its lock is held by a pid that is no longer a live process; with no cadence and no lock evidence at all, the reading is `unknown` — never "alive". `kew epic status` reads the lock file directly; the web console cannot, so `kew serve`'s sweep snapshots each epic's lock once a cycle for the console's readings to use. Must be `>= 1`.
- **`approvers`** (default `[]`) — an additional allowlist of GitHub logins whose PR approval may satisfy the [merge gate](cli/epic.md#the-merge-gate). Empty falls back to `[approval] approvers`. With both lists empty, the reviewer must still be a non-author human with fresh `write`, `maintain`, or `admin` repository permission; external identities and bots never qualify.
- **`auto_ci_fix`** (default `false`) — when `true`, a tick whose node has a red (definitively failed) PR CI may dispatch a bounded ci-fix run (reusing the ci-fix machinery — budget-capped, at most one attempt per node per tick) instead of parking for a human. **Infra-dead CI** — a run whose jobs report zero executed steps (a billing failure or offline runner, which looks identical to a code failure) — is never fixable: the node is parked with a `ci-infra-dead` diagnostic and **no** agent run is dispatched. Off by default: recovery only engages when an operator opts in.
- **`merge_queue`** (default `false`) — when `true`, the driver runs a **serial merge queue** before each merge, instead of merging a PR that is only green on its own head (two siblings each green alone can be red together). For every candidate, in node order and one at a time repo-wide — under `merge-queue.lock`, a sibling of `lock_path`, held until the merge it clears has landed so the next verification reads a base that contains it — the base branch is fetched from the real upstream, **its commit OID is recorded**, and it is merged into the READY head in a scratch clone; `[commands] verify_loop` runs on that combined tree; and the merge proceeds only if that command succeeds **and** the head still equals the head that was verified **and** the base branch still points at the recorded OID (a base that moved under the verification refuses with `merge-verify-base-moved`, and the next tick verifies the new combined tree). A red combined tree (including a conflict with the base) is recorded as a `NEEDS_CHANGES` review round naming the failing gate, which the review stage then remediates. A queue that could not decide — busy, no verify command configured, no admitted posture to run it under, a base merge git could not run, a timeout, a container that could not start, an unreadable post-verification head — holds the node and says why; it never merges on an unverified tree. While a verification runs the driver refreshes its own epic lock heartbeat in the background, so a gate that legitimately outlasts the 300s staleness window cannot invite a second driver of the same epic.

    The command is `[commands] verify_loop` and **only** that key: with `merge_queue = true` and no `verify_loop` the config fails to load, rather than falling back to `[commands] ci` — usually a narrower gate than the one a combined tree has to pass. Like `verify_loop` it is **operator-config-only** — never set it from issue or agent text.

    The command string is operator-owned, but the files it *executes* (the gate script, the build backend, a `conftest.py`) come from the PR. Two defences follow from that, and neither is optional:

    * it runs under the **same containment-first admission as the loop gate** — the image mapped for `[agent] runner`, falling back to the stock `[sandbox] image`, with no network and none of the host's environment. `[sandbox] dangerously_skip_containment` is **not** needed for the queue; where it is set and no image resolves, the queue reaches the host exactly as the loop gate does, and where neither is available every candidate is held `merge-verify-uncontained`;
    * before any command runs, the PR's own diff against the recorded base is intersected with `merge_gate_paths` (below), and a hit **holds** the node.

    The combined tree is mounted read-only, like every other disposable verification checkout: a verify command that writes into its own tree (a cache directory, a build artefact) needs `[commands] verify_loop_needs_writes = true`, or the queue will read its failure as a red combined tree.

    One prerequisite is still enforced rather than assumed: `[review] adversarial = true` (the config fails to load otherwise — the review stage is the only thing that remediates the round a red tree records).
- **`merge_gate_paths`** (default: the gate-definition list below) — the paths whose content *defines* what the verify command checks. Before any verification the queue reads `git diff --name-only <base>...<head>` and holds a node that touches one as `merge_gate_touched@<head>: <paths>`; `kew epic unhold` releases it once a human has read the diff, and records that head so the next pass — which re-derives the hold from the same unchanged diff — lets it through. A later push moves the head and is held again. Patterns follow `PurePosixPath.full_match` (so `**/pyproject.toml` catches the root one and every nested one), and a trailing `/` means "this directory and everything under it". The shipped default is `["**/scripts/", "**/ratchets/", "**/pyproject.toml", "**/setup.cfg", "**/setup.py", "**/tox.ini", "**/pytest.ini", "**/conftest.py", "**/ruff.toml", "**/.ruff.toml", "**/mypy.ini", "**/.mypy.ini", "**/.coveragerc", "**/.importlinter", "**/.bandit", "**/uv.lock", "**/uv.toml", "**/.python-version", "**/.semgrep/", "**/.semgrepignore", "**/trivy.y*ml", "**/.trivyignore", "**/.xenon-excludes", "**/.gitleaks.toml", "**/.gitleaksignore", "**/.gitattributes", "**/mkdocs.y*ml", "**/docker/", "**/.dockerignore", "**/package*.json", "**/.npmrc", "**/.nvmrc", "**/tsconfig*.json", "**/*.config.*", "**/Caddyfile", "**/demo/world/", "**/demo/leak-scan*", "**/demo/public-repo.json", ".github/workflows/", "kew.toml"]` — every file entry is `**/`-anchored because gate definitions live wherever the project they gate lives (this repo pins semgrep at `kew/.semgrep/pinned.yml` and locks tool versions in both `uv.lock` and `kew/uv.lock`). It covers **every gate's own configuration, not only the Python ones**: a scanner's allowlist is as much a gate definition as the script that invokes it, so `.gitleaks.toml` / `.gitleaksignore` (a PR that allowlists away its own leaked credential would otherwise be scanned by rules it wrote), `.gitattributes` (its `export-ignore` lines decide what the public snapshot leaves out), `mkdocs.y*ml` (whose `exclude_docs` decides what may reach a published site), `tsconfig*.json` (what `tsc` *is*), `.dockerignore` (the build context of the images under `docker/`) and `Caddyfile` (a site's security headers, its Content-Security-Policy among them, which define any check that serves a build with those headers) are all in it, and so are the demo world under `demo/world/` (the demo browser checks take their expected figures from its files), the demo leak scan with its allowlist (`demo/leak-scan*`), and `demo/public-repo.json` (the repository the demo checks match a served build's PR links against to tell the real build history from the samples). Pinning a scanner's *rules* covers only half of it: `.semgrepignore` and `.trivyignore` choose what the scanner is allowed to look **at**, and are obeyed whatever ruleset `--config` pinned, so they are in it too — a gate that scans nothing passes everything.

It also covers the configs a tool **finds by convention and that need not exist yet** — the widest form of the hole, because no scan of the current tree can see them. `ruff` reads a `.ruff.toml` or `ruff.toml` in preference to `pyproject.toml`; `pytest` reads `pytest.ini`, `tox.ini` or `setup.cfg`; `.python-version` and `uv.toml` choose the interpreter and the resolution every `uv run` gate executes under; `.npmrc` / `.nvmrc` are the same move for the JS gates. A PR that *adds* a root `.ruff.toml` carrying `select = []` would otherwise turn the lint gate off and then be verified green by the gate it had just written. The generic `**/*.config.*` entry covers the framework configs nothing points at: `vite.config.*` and `vitest.config.*` are what `vite build` and `vitest run` *are*, and `svelte.config.*` decides what `svelte-check` checks — `svelte-kit sync` generates the `.svelte-kit/tsconfig.json` the tracked `tsconfig.json` extends, and its `prerender.handleHttpError` hook decides which broken links a site build survives.

Setting it to `[]` turns the check off — an explicit decision to let a PR redefine the gate that proves it.
- **`merge_queue_uncontained_gates`** (default `[]`) — names of gates the contained verification *cannot* run (a `docker build`, anything needing the network). They are recorded on every merge-verify attestation as `skipped_gates`, with the reason, so the green says what it did not prove, and exported to the verify command as `KEW_MERGE_QUEUE_UNCONTAINED_GATES` (comma-separated) so a command that can skip them does. They are expected to be covered by CI, which the merge gate checks separately.
- **`lock_path`** (default: unset, falls back to `<logging.dir>/epic-driver.lock`) — base path for the driver's advisory lock. Locks are keyed **per epic**: the epic name is woven into the filename (e.g. `epic-driver-<name>.lock`), so two epics can `advance`/`run` concurrently while a second driver of the *same* epic exits cleanly rather than double-dispatching. The lock names its holder (pid, epic, held-since, heartbeat, and the holder process's start time) and, while held, refreshes the heartbeat every 60s in the background, so a tick that dispatches for longer than 300s is never mistaken for a hung one. A **stale** lock — a dead-PID holder — is reclaimed **automatically** by the next driver (the steal is logged), so a killed driver's orphaned lock never leaves later drivers silently observing a dead epic. A holder whose pid is alive with the recorded start time is **never** stolen, however old its heartbeat and with or without `--takeover`: a hung but live driver is released by killing it (the refusal's log line names the pid), after which its dead pid is stolen as above. The start time is recorded in epoch seconds, so drivers running under different time zones (launchd vs a shell) agree on it. Any other live holder — a recycled pid with another start time, a start time that can't be read, or a lock written by an older kew with none recorded — keeps the heartbeat rule: a heartbeat not refreshed within 300s makes it stale, and `kew epic advance --takeover` reclaims a *legacy* lock predating heartbeats whose staleness can't be proven; a live driver with a fresh heartbeat is never stolen or taken over.

Long-running drivers reload mutable controls, including `max_parallel`,
approvers, review grants, and breaker settings, before each tick. An explicit
`--max-parallel` remains pinned. Tracker, repository, dispatch DB, and lock
identity are pinned at startup; changing any of them fails closed and requires
a relaunch. An invalid reload is rejected before any new controls apply.

See [`kew epic`](cli/epic.md) for the full command reference, the merge gate, and why there is no `--auto-merge`.

## `[drive]` {#drive}

Admission for [`kew drive`](cli/drive.md): how much work the drive's epic ticks may have in flight at once, and how this machine's load lowers that.

```toml
[drive]
max_parallel = 3
# max_load_per_cpu = 4     # unset = off; hold admission above this load per CPU
# max_swap_used_pct = 90   # unset = off; hold admission above this swap use

[drive.load]
thresholds = [{ above = 14, max_parallel = 2 }, { above = 25, max_parallel = 1 }]  # Intentional example; empty = no load gate
```

- **`max_parallel`** (default `3`) — the most runs the drive's ticks admit up to. Dispatches, reviews, remediations and ci-fix runs all count, and so do runs on every host: a run placed on a remote instance holds a slot exactly as a local one does (the fleet count of [`[fleet]`](#fleet)). The drive never goes above the fleet cap (`[fleet] max_concurrent`, else `[parallel] max_workers`), because every run re-checks its admission against that cap in its own process. With the shipped `[parallel] max_workers = 1`, raise the fleet cap to let the drive run more than one at a time. Must be `>= 1`.
- **`load.thresholds`** (default `[]`) — while this machine's one-minute load average is above `above`, the cap is at most `max_parallel` (which may be `0`: admit nothing). Every threshold the load is above applies, so the lowest wins; a threshold never raises the cap. The load is read before every tick. If it cannot be read, the drive takes the lowest threshold's cap rather than the full one.
- **`launches_during_verify`** (default `0`) — how many nodes the drive launches new agent work for while a local merge verify (`kew verify --host local`) holds its lease, counted per verify: dispatches, continuations, ci-fixes, review stages (reviewer, remediation, merge-main step) and console run requests that dispatch or continue. A node admitted once is not counted again during the same verify. At `0` nothing new launches until the verify ends; each deferred node records one `launch_deferred` event per verify, and the first tick after it launches what waited. Running work is never stopped, a hand-started `kew review` or `kew dispatch` only warns, and the merge queue's mutation reports are not deferred. Must be `>= 0`. See [`kew drive`](cli/drive.md).
- **`max_load_per_cpu`** (default: unset, off) — hold admission while this machine's one-minute load average divided by its CPU count is above this value. Must be a finite number `> 0`.
- **`max_swap_used_pct`** (default: unset, off) — hold admission while more than this percentage of swap is in use (read from `/proc/meminfo` on Linux and `/usr/sbin/sysctl vm.swapusage` on macOS). A machine with no swap configured reads as 0% used. Must be `> 0` and `<= 100`.

With either set, the drive reads the host before every tick. Above a threshold it holds admission: running work continues, ready nodes wait as `admission-held: host load … (held since …)` in `kew epic status`, and the episode raises one attention event — the drive log and its notification — however long it lasts. Admission resumes by itself once the load and the swap are both at or below their thresholds for two consecutive tick reads (a round with no epic to tick — an empty rotation, or every epic paused — still takes one such read); a reading back above them restarts that count. While an episode stands, a queued run request that would spend stays queued with the episode's reason and starts once admission resumes (one still queued after an hour is refused as stale, as always). The drive also reads the host just before taking such a request; that read can open an episode or restart its count, but never counts toward ending one. A host the drive cannot read holds admission rather than admitting: no load average or CPU count, a load that is not a finite number `>= 0`, or — while `max_swap_used_pct` is set — swap it cannot read or a swap share outside 0–100%. With neither set, the host is not read and nothing changes.

`kew drive hold` closes admission whatever the cap says, and `kew drive release` reopens it. The drive reads the load and the hold before every tick, so either reaches the next tick and a running tick always finishes. `[drive]` itself is read when the drive starts: restart the drive to change the cap or the thresholds.

## `[report]` {#report}

The [`kew report export`](cli/report.md) command creates a self-contained HTML
evidence pack and its JSON twin.

The evidence pack always uses kew's name, logo, and light palette. The removed
`brand_name`, `brand_logo`, and `brand_accent` keys are rejected.

The palette comes from `frontend/src/app.css`. The four IBM Plex faces remain
inlined as base64 woff2. The pack needs no network requests and stays one file.

The attestation line identifies the kew version. It also includes the export
timestamp and reporting period.

## `[run_reports]` {#run_reports}

Per-run decision-intelligence exports. When enabled, every run that reaches a
terminal state writes one small markdown **run report** into a machine-level
sink shared by every repo kew orchestrates — the corpus for cross-project
questions like "which model handles docs PRs well" or "what did runs shaped
like this cost". See [Run reports](run-reports.md) for the report format
contract.

```toml
[run_reports]
enabled = false                        # default off — no behavior change until you opt in
sink_path = "~/.local/share/kew/runs"  # machine-level sink; reports land at <sink>/<repo-slug>/<run-id>.md
```

- **`enabled`** (default `false`) — turn the exporter on. Export is
  best-effort: a report that cannot be written logs a warning and never fails
  the run.
- **`sink_path`** (default `"~/.local/share/kew/runs"`) — where reports land,
  `~` expanded. The default is deliberately machine-level (not per-repo) so
  every repo's exhaust pools into one queryable corpus; point a repo's own
  `kew.toml` at a different path to keep that repo's exhaust separate.

## `[sandbox]` {#sandbox}

Containment uses either an ephemeral runner container or a runner-native
boundary declared in runner code. With the default containment setting, kew accepts
a native declaration first; otherwise it sandboxes whenever a container runtime
is present, and refuses the dispatch when none is found rather than
silently falling back to the host.

```toml
[sandbox]
enabled = "auto"                                   # "auto" (default) | true | false
runtime = "docker"                                 # "docker" (default) | "podman" | "openshell"
image = "kew-agent:docker-d148816b388f"            # pinned Claude Code image, built locally
network = "allowlist"                              # container network mode ("allowlist" default | "open" opt-out)
proxy_environment = "none"                         # "none" (default) | "inherit"
host = "local"                                     # where agent containers run: "local" (default) | "gcp"
# placement = "prefer-gcp"                          # fleet placement policy: unset = whatever `host` says | "local" | "gcp" | "prefer-gcp"
# max_memory = "3g"                                # lower the 8g per-container memory cap (lower-only)
# max_cpus = 2                                     # lower the 4-CPU per-container cap (lower-only)
dangerously_skip_containment = false               # run agents on the host, no boundary (opt-in)
# allow_uncontained_flows = false                  # unset by default; exception only for enabled = true

[sandbox.hosts]                                    # run kind -> host, overriding `host`
# dispatch = "gcp"                                 # implementation runs on the VM, reviews stay local

[sandbox.images]                                   # runner name -> container image
# Intentional runner-image mapping example.
opencode = "kew-agent-opencode:docker-d148816b388f" # pinned OpenCode image, built locally
codex = "kew-agent-codex:docker-d148816b388f"       # pinned Codex image, built locally

[sandbox.openshell]                                # only read when runtime = "openshell"
gateway = ""                                       # "" (default) = the CLI's own default; OPENSHELL_GATEWAY wins
version = "0.1.2"                                  # the openshell CLI version kew's client is written against

[sandbox.openshell.providers]                      # runner name -> gateway credential provider
# Intentional runner-provider mapping example.
claude-code = "kew-claude-code"                    # the provider that holds this runner's credential
```

### Where an agent container runs {#sandbox-host}

- **`host`** (default `"local"`) — the machine an agent container runs on.
  `"local"` is this machine and needs no cloud account. `"gcp"` ships the run
  to the Compute Engine instance `[hosts.gcp]` names: the checkout travels as a
  git bundle (the same transport `kew verify` uses), the container is launched
  there with the argv kew uses locally, its JSON event stream is relayed back
  over the SSH channel, and the finished work is extracted on the instance and
  returned as an artifact. **Publication stays on this machine** — the instance
  returns data, and nothing on it can push or merge.
- **`hosts`** — per-run-kind overrides of `host`, keyed by run kind
  (`dispatch`, `adversarial_review`, `review_remediation`, `ci_fix`, …). A kind
  with no entry uses `host`. Both halves are checked when the config loads: a
  key naming no run kind kew runs is refused by name, because an override is
  read once by exact key and a typo would otherwise leave that kind on `host`
  with nothing said.
- **`placement`** (default: unset — reads `host`, so effectively `"local"`) —
  the fleet's placement *policy*, which `host` is the one-host case of:
    * `"local"` — every run on this machine. Needs no cloud account, and stays
      the default.
    * `"gcp"` — every run on a Compute Engine instance. A run arriving when
      every instance in the `[hosts.gcp]` pool is at `max_runs` **waits**; it
      does not quietly land here, because an explicit pin is not a preference.
      Only a `dispatch` run is ever placed on an instance, so a contained
      remediation, resolve or CI fix this pin governs is refused before
      admission — pin `[sandbox.hosts] dispatch = "gcp"` instead to keep them
      running here.
    * `"prefer-gcp"` — gcp while an instance has room, this machine when none
      does. The policy that keeps the queue moving through a full cloud, and
      the one to use when the point is to get load off the laptop rather than
      to pin work to the cloud.

  Left unset it reads whatever `host` says, so an operator who already pinned
  `host = "gcp"` keeps that behaviour. A per-kind `hosts` entry is an explicit
  pin and still outranks the policy for that kind.

  Where each in-flight run actually went is recorded in the ledger and reported
  by `kew status` (an `In flight:` block naming host and instance) and by the
  console's route line.

A remote host holds no GitHub, Linear, Google or cloud credential. The one
exception is the runner's own declared model credential (for `claude-code`,
`CLAUDE_CODE_OAUTH_TOKEN`): it may reach a **dispatch** run's container only,
it travels on the SSH session's stdin into `docker run --env NAME` so it is
never written to the instance's disk and never appears on a command line, and
the launch audit event records it **by name**. Every other credential-shaped
name is refused, for every run kind.

An unreachable instance, a lost SSH session or an instance pre-empted mid-run
(spot) ends the run as *interrupted* — its own error category, never a silent
success — and this machine's checkout is left exactly as it was.

Two things must be true of the instance, and a placement checks both before it
launches anything. The pinned agent image kew can supply: an instance without
it builds it from the repository it was just shipped. The egress network
`network` names (`kew-allowlist` by default) it cannot — kew creates that
network on no machine — so an instance without it refuses the placement,
naming the command that creates one there
([Hosts](hosts.md#a-host-that-runs-agent-containers)), instead of leaving the
instance's own daemon to refuse the container and the run to read as an agent
that failed with an empty event stream. A launch the daemon refuses for any
other reason ends the placement the same way, carrying what the daemon said.

!!! note "What these keys reach"

    They select the instance a `dispatch` run's agent container runs on, for
    `kew dispatch` and the epic driver alike. Publication stays here: the work
    comes back as a verified artifact and the branch and PR are pushed from
    this machine. A dispatch on `runtime = "openshell"`, one whose transport
    needs a local sidecar, or one with no model credential for the launch to
    hand over cannot be placed at all — it runs here under `prefer-gcp` and
    waits under a `gcp` pin. `local`
    stays the default and the only setting that needs no cloud account; see
    [Hosts](hosts.md#placing-agent-runs).

With `enabled = true`, review remediation and conflict resolution fail closed when containment prerequisites are missing.
Missing prerequisites include the runtime, runner image mapping, and declared credential in the environment.
`allow_uncontained_flows = true` explicitly permits their host fallback with `admission = "policy"`.
The setting defaults to unset (`None`); unset and `false` permit no exception.
With auto or disabled containment, `true` is accepted as a no-op.
`kew config effective --sources` reports the no-op; `--json --sources` also retains its configured value.
Auto fallback records `admission = "prerequisite_gap"`; explicit `enabled = false` retains `admission = "policy"`.
CI-fix retains its stricter dispatch gate and exit code 2 (could not run), including when this setting is `true`.
Review and resolve containment refusals return exit code 2 (could not run).

Resource caps apply to each container. Concurrent capacity therefore multiplies
by the [`[fleet] max_concurrent`](#fleet) cap. Four concurrent agents can request
four memory caps at once.
kew warns and continues when one container's memory cap exceeds the runtime's
reported memory.

The caps themselves are not knobs — there is no setting that raises one or sets
it back to "none", because "none" is the state containment removes. Two of them
are **lower-only**, so you can fit them to a container VM that is smaller than
your host:

- **`max_memory`** (unset by default) — a Docker size string from `1g` up to the
  hard `8g` cap: `"3g"`, `"1536m"`, or a bare byte count (`4000000000`). Unset
  keeps `8g`.
  Container swap always equals the memory cap (that is how Docker spells "no
  swap"), so lowering one lowers both. The two `tmpfs` mounts are sized from the
  cap — `$HOME` gets `min(4g, max_memory / 2)` and `/tmp` gets
  `min(1g, max_memory / 4)` — because `tmpfs` pages are host RAM charged to the
  container's memory cgroup, so filling one stays an OOM kill of the container
  rather than of your host. At `max_memory = "3g"` that is `1536m` and `768m`.
- **`max_cpus`** (unset by default) — a whole number of CPUs from `1` up to the
  hard cap of `4`. Unset keeps `4`. The effective value is still clamped to the
  container daemon's own CPU count, because a cap above it is rejected outright
  and that means no container at all. The agent container is also told that
  number as its test-runner pool size (`PYTEST_XDIST_AUTO_NUM_WORKERS`,
  `VITEST_MAX_WORKERS`, `VITEST_MAX_THREADS`, `VITEST_MAX_FORKS`): `--cpus` is a
  CPU quota, so `nproc` inside the container still prints the host's count, and
  `pytest -n auto` sized to the host runs a small container out of memory.

A value above the cap, `0`, a blank string, and `"unlimited"` are **configuration
errors at load**, never clamped: a silently clamped `16g` would run every
container under a cap you did not choose and hide the typo that produced it.

**Sizing rule of thumb:** `containers x max_memory <= your container VM's
memory`, where `containers` is the [`[fleet] max_concurrent`](#fleet) cap — the
most agents that can be in flight here at once. A 16 GiB OrbStack VM
fits two agents at the default `8g`; `max_memory = "3g"` fits five. Oversubscribe
the VM and the containers survive their own cgroup limits while the host swaps.

Each dispatch's effective caps are recorded on the audit ledger in its
`sandbox_resolved` event as `max_memory` and `max_cpus`, so a run report shows
what that run ran under rather than what the config file says today. `max_cpus`
there is the ceiling the launch asked for; the daemon clamp is applied on top of
it at launch time. Both are `null` on any posture that launched no kew container
— `native`, `uncontained`, `uncontained_flow`, `host`, `refused` — because no
cgroup enforced a cap there.

Both settings are Docker flags, so **neither applies on `runtime = "openshell"`**:
`openshell sandbox create` takes an image, credential providers, an egress policy
and labels, and no resource flag. A dispatch that configured either on that
runtime is warned at launch that it cannot take effect, and its
`sandbox_resolved` event attests `null` for both.

These two settings size the **agent** containers, which are the ones
`max_workers` multiplies. The short-lived helper containers (publication
extraction, merge-queue command verification) stay at the hard caps.

- **`enabled`** (default `"auto"`) — tri-state master switch:
    - `"auto"` — a runner-declared native boundary is admitted as posture `native`. For other runners, kew probes for the configured runtime once per dispatch batch. Runtime found **and an agent credential in the environment** → dispatch is sandboxed and the banner says so (`Sandbox: on (orbstack)`). No runtime, or no credential → **kew refuses the dispatch** and names both ways forward.
    - `true` — hard-require containment. A runner-native declaration satisfies this requirement; other runners are refused (fail closed) when no runtime is available **or no agent credential is in the environment**.
    - `false` — do not use a container. This is uncontained execution, so it additionally requires `dangerously_skip_containment = true`.

    **For dispatch, no value of `enabled` reaches the host path on its own.** Until 2026-08, `"auto"` on a machine without a container runtime silently ran the agent directly on your host, announced by one line among the dispatch output. That made it possible to be in the unsafe posture without having chosen it — so the choice now has a name.

- **`dangerously_skip_containment`** (default `false`) — run agents directly on your machine, with **no container boundary**. The agent can read and write any file your user can, and prompt injection from an issue body or a fetched page reaches your host. The in-process guard is a second layer, not a boundary: it blocks known-destructive commands and nothing more.

    Every dispatch under this setting prints a banner, and is recorded on the audit ledger with posture `uncontained` — a distinct token from the old `guard-only`/`disabled`, so "which runs had no boundary" has exactly one answer.

    It is **supported, not deprecated**: machines without Docker, and local-model runners that expose a host shell, genuinely need it. It just has to be chosen. The name follows Claude Code's `--dangerously-skip-permissions` convention rather than "yolo mode" — kew *drives* Codex, and yolo is Codex's own term, so one session would otherwise carry two different ones.

    It is not a blanket "stop checking": a runner with no mapped image is still refused, because that is a fixable kew-side gap rather than a property of your machine.

!!! note "Containers cannot inherit CLI login"
    On a machine authenticated via `claude` CLI login, the credential lives in the OS keychain / `~/.claude` — which kew deliberately never mounts into the container. A sandboxed agent therefore needs an **environment** credential, forwarded by name only (never by value in argv): either `ANTHROPIC_API_KEY`, or — to bill your existing Claude subscription instead of an API key — run `claude setup-token` once and export the result as `CLAUDE_CODE_OAUTH_TOKEN`. Without one, kew refuses to dispatch: the runtime is present, so the machine *can* contain, and dropping out of containment over a fixable credential gap would trade a solvable problem for an unsolvable one. `kew sandbox smoke` checks this as its final `auth` layer.

!!! warning "Breaking: `enabled = false` now needs a second key"

    Existing `enabled = true` TOML keeps working unchanged. Existing
    `enabled = false` **does not**: it is uncontained execution, so it now also
    requires `dangerously_skip_containment = true` and is refused without it.

    Setting both `enabled = true` and `dangerously_skip_containment = true` is
    rejected at config load — one demands containment and the other skips it,
    and silently picking a winner would hand an uncontained agent to a config
    that asked for a guarantee.

- **`runtime`** (default `"docker"`) — which container boundary contains the agent. One of `"docker"`, `"podman"` or `"openshell"`; any other value fails at config load, because a name kew has no backend for could only be discovered later as "runtime not found" — a missing-Docker message for what is really a typo. kew resolves the CLI through the subprocess allowlist by bare name (not a path), so OrbStack and other Docker drop-ins work transparently.

    `"openshell"` dispatches into an NVIDIA OpenShell sandbox through the gateway named in `[sandbox.openshell]`. kew creates the sandbox from the same immutable image ID it would have launched under Docker, uploads the standalone checkout to `/work`, and runs the agent through the in-sandbox launcher; the wall-clock cap, the transcript cap, the startup acknowledgement and host-owned publication are all unchanged. Twelve things differ, deliberately:

    - **Admission requires an egress certification.** kew will not claim a container boundary it has not watched hold, so admission requires a passing certification from `kew sandbox certify --runtime openshell` that is less than 24 hours old and bound to the exact image, policy, gateway version and in-sandbox launcher this dispatch would use. Without one the refusal is attested as `openshell-egress-uncertified`, and the message names that command. This gate is asked first, before the launch-path checks in the bullets below, because it is the OpenShell refusal a shipped command clears; a certified dispatch that then has a launch-path gap is refused for it as `openshell-runtime-unavailable`. A runner kew packages no OpenShell policy for (`pi`, `hermes`) is refused before this gate, as `openshell-policy-unpackaged`: there is no boundary to certify for it and no command that would produce one, so that message names the missing policy and no command. Neither refusal is waived by `dangerously_skip_containment` — naming a runtime asks for containment, and answering that with a host run would be the silent downgrade kew refuses to make.
    - **Credentials never enter the sandbox.** The gateway holds them in a named provider (`[sandbox.openshell] providers`) and injects them at the endpoint its policy governs; the agent sees the variable *names* bound to a self-describing placeholder. A runner with no mapped provider is refused with `broker_provider_missing`, not `credential_missing` — "export the variable" is the wrong remedy here.
    - **The agent's environment is an allowlist.** `openshell sandbox exec` forwards the client's environment into the sandbox, so whatever kew hands that call *is* the agent's environment. kew passes `PATH`, `HOME` and the locale variables, plus each credential variable the resolved runner declares bound to the placeholder above — and nothing else. A variable you export for your own tools therefore does not reach an agent on this runtime, and neither do the ten proxy variables: `proxy_environment` is a Docker-argv setting whose default (`"none"`) blanks every one of them there, so not forwarding them is how this boundary honours it. `proxy_environment = "inherit"` is not forwarded either — an inherited proxy URL can carry userinfo and would point the agent at a destination no packaged egress policy permits — so on this runtime that setting cannot take effect: kew warns at launch that it is being ignored, and the run attests no inherited proxy names rather than claiming ten crossed. `OPENSHELL_GATEWAY` is left out too — the endpoint travels as `--gateway-endpoint`, and the agent being contained has no business learning it.
    - **The tree kew publishes is downloaded back.** The agent edits the uploaded copy at `/work`, so when its sandbox ends kew downloads that copy into a NEW host directory beside the original (`<checkout>-realized`) and publishes that one. Never onto the original: a download into an existing tree keeps every file the agent deleted, so the deletion would never reach the PR. The checkout quota (4 GiB / 500k entries) is checked on arrival — the one moment it is knowable on this runtime — and a breach refuses publication rather than pushing the tree, after which the refused tree is removed. The gateway's own CLI performs the transfer and kew cannot stop it halfway, so what is checked before it starts is room, not size: a host that cannot hold a cap-sized checkout and still leave the 1 GiB free-space floor is refused without downloading anything. **The quota is not a streamed limit.** A sandbox tree larger than the cap is transferred in full before it is measured, so while that transfer runs it can take the host device past the floor — up to full — and only then is it refused and removed. The transfer timeout bounds how long that lasts, not how many bytes land. A pre-existing `<checkout>-realized` of any kind — including a symlink, whatever it points at — is refused without downloading, and never removed. `.git` must survive the transfer or the run refuses for that too, and file modes are restored from the git index when the transfer dropped them, which the run's evidence records beside the sizes. Whatever the gateway denied the agent is read out of `openshell logs` into that same evidence first, because deleting the sandbox is what destroys it.
    - **Only a caller holding this config can inspect a sandbox it did not launch.** The epic driver's reaper does: it asks the gateway for the sandbox by the name the run row carries and reads `running`, `stopped`, `absent` or `unknown` — a status this build has never seen, an unreachable gateway and a deadline all read `unknown`, which keeps the run's capacity rather than freeing it. A sweep that holds only a run row (`kew fleet stop`'s reconciler, the pre-admission orphan pass in the ledger) has no `[sandbox.openshell]` to name the gateway with, so it still reports the state as unknown *with that reason* rather than guessing; the driver is what finalizes those runs.
    - **A gateway restart is recovered into a continuation.** Restarting the gateway restarts every sandbox it governs: the processes inside die and the files survive. kew detects that as an exec stream that ended with no exit code while the sandbox is still present and the sandbox reports its agent ended. kew's local `openshell sandbox exec` client dying is not the agent dying, so kew asks the sandbox, through a second short exec, whether the process its launcher exec'd is still running: a running agent keeps the run alive, and a sandbox that cannot answer holds it. Each sandbox is named `kew-<slug>-<token>`, the token a digest of the project and the run, so two projects sharing a gateway never share a sandbox name; the run also records the gateway's own sandbox id, and recovery touches the sandbox only when the id answering to the name is that one. Recovery then downloads `/work` out of the sandbox and merges it into the **host dispatch checkout** for that item — `<worktree_base>/agent/issue-<slug>` — binds the recovered tree to the dead run, and deletes the sandbox. From there the work is an ordinary dirty orphan: the node is handed the same `awaiting_continuation` handoff a crashed host dispatch with dirty work gets, so `kew epic continue` picks it up. The host path is the destination precisely because it is the only path a continuation can be resumed from; recovering into the temp-directory clone the sandbox was uploaded from would leave the bytes somewhere nothing records a way back to. The recovery is attested as an `openshell_gateway_restart` event naming that `checkout`. Whatever already stands at the host path belongs to some other dispatch of the item, so a present path refuses the merge rather than mixing two runs' work; the sandbox is kept instead, and the node is held with the refusal rather than silently redispatched over it. A download the gateway refuses — or one that handed back a tree no continuation can be read from, such as a sandbox left on a detached HEAD by an interrupted rebase, or one kew cannot bind to the dead run — likewise leaves the sandbox in place so the work can still be fetched by hand, and says so in the log and in that event's `checkout_recovered: false`; the run is finalized either way, so no reservation is held open waiting for a gateway. A sandbox whose delete kew cannot confirm (the delete timed out and reading it back could not prove it gone) is never left behind a finished run: the run stays pending, holding its reservation, until a recovery releases the sandbox. The merge is not atomic, so the same event's `checkout_merge` says which tree you are left with — `unmerged` for a host path left exactly as it was, or `part_merged` for one that holds some of the sandbox's files and not the rest. The recovery holds to the same checkout caps as the realization, checked the same way: the host must have room for a cap-sized checkout above the free-space floor before the download starts, the downloaded tree is measured against the 4 GiB / 500k-entry quota on arrival (not while it streams), and the host checkout's device must hold what is about to be merged; a breach is `unmerged` and the sandbox is kept. The merge never writes through a symlink in the host checkout: where the sandbox has an entry of the same name, the link itself is replaced and its target is left alone. An unreadable tree is `unmerged`: it is removed again, because a tree left standing at the host path would refuse the item's next dispatch over work that is not the item's.
    - **Not every runner launches here.** The sandbox is created under a packaged egress policy and the agent is exec'd through a named program, so a runner with neither — `pi` and `hermes` in this build — is refused at admission (as `openshell-policy-unpackaged`, ahead of the egress gate above), before any sandboxed posture is recorded. The same config admits them under `docker`.
    - **A sidecar Switchyard worker is a Docker topology.** A managed sidecar creates its worker container and installs its network filter through the Docker daemon, which the gateway does not expose, so that combination is refused at admission as `openshell-runtime-unavailable` — after the egress gate, before the posture is recorded and before the proxy container is created — rather than launched. Use `[inference_transport] mode = "direct"` here, or keep the sidecar on `runtime = "docker"`.
    - **A Switchyard transport has no endpoint to arrive through.** Without a sidecar, Switchyard reaches the agent as an endpoint override the launch assigns — in the container argv on Docker, and nowhere at all here: the exec forwards only the names in the allowlist above, and the packaged egress policy permits the runner's upstream host and no other destination. So `mode = "switchyard"` is refused at admission as `openshell-runtime-unavailable`, beside the sidecar case and for the same reason — left admitted, the run attested a Switchyard transport while the agent talked straight to the upstream its policy does permit. Use `[inference_transport] mode = "direct"` here, or keep Switchyard on `runtime = "docker"`.
    - **The remediation flows run uncontained or not at all.** `kew review`, `kew resolve`, `kew ci-fix` and an epic's plan run swap one execution step for a container command line rather than handing the whole launch to the gateway, and a gateway sandbox has no command line to be the subject of. Where they would be contained, all four are refused at admission, before any sandboxed posture is recorded: as `openshell-egress-uncertified` without a matching certification, and as `openshell-runtime-unavailable` with one. `kew review` and `kew resolve` have a documented host fallback, so under `enabled = "auto"` (or with `allow_uncontained_flows = true`) they run on the host and record the gap, exactly as they do on a machine with no container runtime at all. `kew ci-fix` and an epic's plan run resolve through the dispatch admission, which has no such fallback: they refuse whatever `enabled` says, and `dangerously_skip_containment` does not excuse it — naming a runtime is asking for containment. Set `runtime = "docker"` to contain them.
    - **kew sets no resource cap on the sandbox.** `max_memory` and `max_cpus` are Docker `--memory` / `--cpus` flags, and `openshell sandbox create` (v0.1.2) takes an image, credential providers, an egress policy and labels — no resource flag at all. So the per-container caps kew enforces under Docker are the gateway's business here: kew warns at launch when either is configured, and the run's `sandbox_resolved` event attests `null` for both rather than a cap no cgroup held. The caps kew enforces in its own watchdog — the wall clock, the transcript byte cap and the host free-space floor — are unchanged. Keep a run that needs a memory ceiling on `runtime = "docker"`.
    - **The image ID is still read locally.** v0.1.2 of the CLI has no image surface, so kew resolves the tag to its immutable ID through the Docker/Podman driver the gateway runs images under. The dispatching host needs one even though it runs nothing in it.
- **`image`** (default `"kew-agent:docker-d148816b388f"`) — the OCI image used for a `claude-code` dispatch container. **kew never pulls it.** Run `kew sandbox build` to build the digest pin from the inputs shipped with `kew-cli`. Launch passes `--pull=never`. The container is ephemeral (`--rm`) and mounts only the standalone checkout. It never mounts host `~/.claude`, `dispatch.db`, or GitHub credentials. The stock image ships only the `claude` CLI, so this field is the **`claude-code` image**. Every other non-native runner needs an entry in `[sandbox.images]`.
- **`images`** (default `{}`) — a map of runner name (`claude-code`, `codex`, `opencode`, `pi`, `hermes`, `lemonade`) to the container image that runs it. The image kew launches is chosen by the **resolved runner** for that dispatch, not by the environment: routing picks the runner, the runner picks the image. An entry here wins over `image` for a container runner; `claude-code` falls back to `image` when unmapped, which is why existing configs keep working untouched. A runner-native declaration takes precedence over image lookup *on the runtime it is declared for*, which is why a `codex` mapping is launched on `openshell` and not on `docker` or `podman` (see [the Codex reference](runners/codex.md)). Keys must be runner names the registry knows — an unknown key is rejected at config load rather than sitting inert, since lookup is by exact name and a typo would never match anything.

    Intentional per-runner image override examples.

    ```toml
    [sandbox.images]
    claude-code = "kew-agent:docker-d148816b388f"
    opencode = "kew-agent-opencode:docker-d148816b388f"
    codex = "kew-agent-codex:docker-d148816b388f"
    ```

    Every image here must already exist in the local runtime.
    `kew sandbox build` builds the packaged Claude Code image.
    `kew sandbox build --runner opencode` builds the packaged OpenCode image.
    `kew sandbox build --runner codex` builds the packaged Codex image, which
    the OpenShell runtime launches; a `codex` mapping is still not launched on
    the docker runtime, because that runner declares native containment.
- **`check_image`** (default `""`) — the image the pre-review gate's `[commands] static` and `ui_check` halves and the [`[resolve]`](#resolve) checks run in, instead of the runner image. Empty keeps them in the runner image. Those checks run with no network over a read-only checkout, so a check that needs the repository's own dependencies (`uv run`, `npm run`) can only pass in an image that already has them. Like every other image, it is resolved to its immutable ID before anything runs, and kew never pulls it. A `check_image` the runtime cannot resolve is a refusal, never a fallback: the gate's halves are skipped with that reason and a resolution check fails closed, holding the push. It never runs on the host, whatever `dangerously_skip_containment` says. The merge verify does not use it.

    kew's own check image is built by `bash scripts/build-check-image.sh`, which prints the tag it built; `--print-tag` prints the tag without building. The tag is `kew-check:<12 hex>` over `scripts/check-image.Dockerfile`, `kew/pyproject.toml`, `kew/uv.lock`, `frontend/package.json`, `frontend/package-lock.json`, the `[sandbox.images] claude-code` pin it builds `FROM` and the semgrep and setuptools pins (`SEMGREP_VERSION` and `SETUPTOOLS_VERSION` in `kew/scripts/tool-pins.env`, which the image installs exactly; without either pin the script names no image), so moving a lock file names an image that has to be built again. kew's own `kew.toml` pins `check_image` to the tag the script prints for its tree, and a policy test fails when a lock, the Dockerfile, the base pin or a tool pin moves the tag without the re-pin. The image adds kew's locked Python environment (without kew itself: the checks import kew from the checkout's `kew/src`), semgrep, the frontend's `node_modules` and the ui-checks' Chromium. `uv run` there uses that environment offline and never creates `kew/.venv`, and every tool cache goes under `/tmp`. A frontend command runs through `kew-check-frontend`, which copies the checkout's `frontend/` into `/tmp` beside the image's `node_modules`, because a read-only checkout has room for neither `node_modules` nor svelte-kit's generated dir. The container's root stays read-only; kew mounts one small writable tmpfs at `/opt/kew-check/frontend/node_modules/.vite-temp` in every `check_image` run, where Vite bundles `vite.config.ts` (that copy's `node_modules` is the image's, so without it every frontend check fails with ENOENT on any tree). With `check_image` set to the tag the script printed, kew's `[resolve]` checks are `python_checks = "cd kew && uv run ruff check . && uv run mypy src/"` and `frontend_checks = "kew-check-frontend npm run check"`.

    Rebuild after every lock-file change and point `check_image` at the new tag. Until then the old image still resolves, and checks the head against the old dependencies. Once main moves a lock file, a stale check image makes the `[resolve]` checks fail, and every resolution push they gate is held, until the image is rebuilt with `bash scripts/build-check-image.sh` and `check_image` names the new tag; the hold says so.
- **`check_image_inputs`** (default `[]`) — the files `check_image` is built from, as glob patterns matched like `[epic] merge_gate_paths` (full-path match; a trailing `/` means everything under it). In the pre-review gate, a head that changes one of them, and that `merge_gate_paths` also names, has a red result in the check image skipped instead of judged by the base (see `commands.static` above). Empty falls back to every dependency manifest and lock anywhere in the tree (`pyproject.toml`, `uv.lock`, `uv.toml`, `setup.py`, `setup.cfg`, `.python-version`, `package*.json`, `.npmrc`, `.nvmrc`). That fallback is broad: a head can skip the gate by touching an unrelated `package.json`. Set the list to exactly what the image's build copies. Blank entries are rejected. kew's own check image is built from `kew/pyproject.toml`, `kew/uv.lock`, `frontend/package.json` and `frontend/package-lock.json`, so that is kew's list.

### `[sandbox.openshell]`

Read only when `runtime = "openshell"` — `gateway` and `version` by kew's
`openshell` CLI client, `providers` by the credential broker. All three are
inert under `docker` and `podman`.

- **`gateway`** (default `""`) — the OpenShell gateway endpoint, passed to every call as `--gateway-endpoint`. Empty leaves the CLI's own default in place; `OPENSHELL_GATEWAY` — the CLI's own variable — wins over this value when it is set, and kew records which of the two decided the endpoint so a failure names the gateway it actually contacted. kew ships no default endpoint: nothing here points at a host you did not name.
- **`version`** (default `"0.1.2"`) — the CLI version kew's client is written against. kew compares it to the installed `openshell --version` and **refuses** a mismatch rather than driving an argv surface it has not been verified against. Raise it only after re-verifying the calls in `kew/src/kew/openshell.py` against the new CLI.
- **`providers`** (default `{}`) — a map of runner name (`claude-code`, `codex`, `opencode`, `pi`, `hermes`, `lemonade`) to the OpenShell credential **provider** that authenticates it. On this runtime no credential *value* passes through the agent's environment: the gateway holds the credential in the provider and injects it at the endpoint the policy governs, and the sandbox sees each credential variable bound to a placeholder. So this map, not your environment, is what admission checks — kew asks the gateway (`openshell provider get`) whether the mapped provider is really there, and refuses the dispatch as `broker_provider_missing` when it is not. An unmapped runner, a provider the gateway does not have, and a gateway that could not answer are three distinct refusals, because each needs a different fix; none of them is answered by exporting a variable. Keys must be runner names the registry knows, and provider names are limited to letters, digits, `.`, `_` and `-` (at most 64 characters) because the name is attested in the run's auth path as `broker:openshell:<provider>`.

    An OpenRouter-backed runner keeps its per-run bounded key: kew mints the key as it does on every runtime, puts it in a per-run provider named `kew-run-<run_id>`, and deletes that provider **before** revoking the key at cleanup — so the gateway is never left injecting a dead credential. Those runs attest `broker:openshell:kew-run-<run_id>` as their auth path. That per-run provider is what authenticates them, so such a runner needs no entry in this map at all when the dispatch holds a bounded key of its own — minted here because the host has a provisioning key, or handed to a detached dispatch child by the fleet driver that minted it. Either way the key goes into that run's own provider, the same config that admits under `docker` admits here, and the gateway is not asked about a provider the launch never uses.

    That applies to a **direct** route only. kew mints a bounded per-run key when the worker talks straight to OpenRouter, so a `[inference_transport] mode = "switchyard"` route mints nothing and needs its long-lived provider mapped here like any other runner — admission asks the gateway for it, and refuses as `broker_provider_missing` when it is absent.

    A **sidecar** route is not brokered at all. Its worker authenticates to the in-network sidecar with a synthetic credential kew mints per attempt, which no gateway provider holds; the gateway governs the sidecar's own egress, never the worker's identity. So a sidecar launch needs no entry in this map, is attested with the auth path `synthetic:per-attempt` rather than a provider, and resolves exactly as it does under `docker`.

    Every provider named here must already exist on the gateway; kew creates
    only the per-run ones.

!!! warning "A runner kew cannot contain is refused, not downgraded (fail closed)"
    Unless the resolved runner declares native containment in its own code, a
    dispatch is refused when a container runtime **is** present — under
    `enabled = "auto"` as well as `true` — if either half of containment is
    missing:

    - **No image.** No `[sandbox.images]` entry, and not `claude-code`. It is **never** launched in the claude image as a fallback: running an OpenRouter model string against the `claude` CLI just burns the dispatch on a 404. The message names the runner and the exact key to set: `[sandbox] images.<runner> = "<image ref>"`.
    - **No declared credential.** kew forwards a runner's credential env vars into the container by name. A mapped image alone would launch a container with no provider credential, authenticating as nothing — so mapping an image for such a runner refuses until its declaration lands.

    Native status cannot be granted in TOML: it is a security capability on the
    runner protocol. Official Codex is the current native runner; its fixed
    `workspace-write`, network-off, and empty-extra-writable-roots flags are the
    admitted boundary. Runners without that code declaration still follow the
    image and credential rules above exactly.

    `auto` means *contain when the host can contain*. The host runtime is
    available here. The gap is on kew's side. Kew does not silently change the
    posture to uncontained. The refusal is **per-dispatch**.
    Sibling `claude-code` nodes in the same epic still dispatch contained. The
    audit ledger records `posture = "refused"`. Its `reason` is `image_unmapped`
    or `credential_undeclared` ([Audit log](audit.md)).

    When runtime detection succeeds, the skip flag cannot override a missing
    image. The dispatch still refuses with `image_unmapped`. Add the runner's
    image mapping or select another runner. The same rule applies to an
    undeclared container credential.

    Under an epic driver the refused node is **held**, not failed: the driver's worker slot is released immediately (a terminal `refused` run row keyed to its reservation, so it never waits out the reconcile grace), and the node returns to the frontier when you map an image and run `kew epic unhold <node>`. `kew epic status` shows it as `held on containment`.

    Prove a mapping before you route work at it: `kew sandbox smoke --runner <runner>` launches a real container from that runner's resolved image and names the first failing layer.

    With **no** runtime present, `enabled = "auto"` refuses every non-native
    runner rather than downgrading — set `dangerously_skip_containment = true`
    to take the host path deliberately. A native runner does not depend on an
    external runtime and is unaffected.

    An image must implement kew's container entrypoint contract: mounted checkout, prompt read from the mounted prompt file on stdin, extra args as `"$@"`, one JSON event per line on stdout.
- **`network`** (default `"allowlist"`) — the container egress mode.
    - `"allowlist"` (default) — maps the container to an operator-provisioned network named `kew-allowlist`.
      `kew sandbox build` checks for this network but never changes it.
      Provision the network with restrictions for approved inference endpoints and GitHub.
      A plain bridge has unrestricted egress and is not an allowlist.
    - `"open"` — explicit opt-out to full egress (omits `--network`; the Docker default bridge is used). Required until you configure the `kew-allowlist` network. **With open egress a prompt-injected agent could exfiltrate the inference credential via outbound HTTP** — see [SECURITY.md](https://github.com/thossullivan/kew/blob/main/SECURITY.md) for the threat model.
    - Any other value — passed through as `--network <value>` for custom Docker network names.
- **`proxy_environment`** (default `"none"`) — controls ten proxy variables in agent containers: the six inheritable names below, plus `FTP_PROXY`, `ALL_PROXY`, `ftp_proxy`, and `all_proxy`, which kew always assigns empty.
    - `"none"` — explicitly assigns empty values to `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, `http_proxy`, `https_proxy`, and `no_proxy`.
      These assignments override Docker client proxy configuration and Podman host proxy forwarding for these names.
    - `"inherit"` — forwards only these names that are present in the host environment, including names with empty values.
      kew passes each name with `-e NAME`, so proxy values never enter its argv.
      kew explicitly blanks each absent name with `-e NAME=`.
      Runtime proxy defaults cannot supply values for these names.
      Enable this option only when agents may read proxy URLs and any embedded credentials.
      kew refuses a container launch or image build when a present HTTP or HTTPS proxy value does not parse as a URL, because its credentials could not be redacted; the refusal names the variable, never its value.

    `sandbox_resolved` records the selected mode as `proxy_environment`.
    Its `proxy_variables` object contains separate `inherited` and `blanked` name lists.
    Under `"none"`, `blanked` contains all ten names and `inherited` stays empty.
    Under `"inherit"`, `inherited` contains present host names and `blanked` contains absent names.
    Native, uncontained, and refused launches record two empty lists because kew applies no container assignments.
    A contained launch on `runtime = "openshell"` records two empty lists for the same reason: these assignments are Docker arguments, and a gateway sandbox has no argv to carry them, so neither policy reaches the agent there.
    Under `"inherit"`, contained launches also include forwarded proxy names in `credential_names` — except on `runtime = "openshell"`, where nothing is forwarded, so nothing is named.
    These audit fields contain names only, never proxy values.

    Explicit image builds and smoke-triggered builds use the same policy.
    Under `"none"`, kew blanks all six build arguments and their build-process environment variables.
    Under `"inherit"`, kew passes present host names as `--build-arg NAME` and blanks absent names.
    Proxy values never enter build argv, and Docker client proxy configuration supplies no fallback values.
    Builds and container launches also blank `FTP_PROXY` and `ALL_PROXY`, including lowercase forms, under both policies.
    The build log records `proxy_environment` and the inherited and blanked name lists in `proxy_variables`.

    Launch diagnostics scan the proxy values received by that launch.
    Both policies also scan credential-shaped host proxy values.
    A proxy URL is credential-shaped if it contains userinfo, a non-root path, a query, or a fragment.
    Unparseable proxy URLs are credential-shaped.
    A bare `NO_PROXY` host list is not a credential.
    Split `NO_PROXY` and `no_proxy` on commas and trim whitespace before bounded decoding.
    Host entries include hostnames, optional numeric ports, leading dots, wildcards, IP addresses, and CIDR entries.
    Each decoding stage excludes host entries from component selection.
    Parse each remaining entry at its first `=` as `name=value`; otherwise treat it as a bare value.
    The shared opaque-key classifier identifies credential names at every bounded decoding stage.
    Register selected values and bare entries at every stage using the existing length tiers and provenance rule.
    Under `"inherit"`, noncredential assignment names also contribute forms under that rule.
    Credential names remain metadata.
    The complete inherited value remains a form, including complete host lists.
    Encoded commas remain inside their entry during decoding.
    Under `"none"`, ordinary host proxy URLs and bare host lists do not suppress launch diagnostics.
    Unscoped redaction scans credential-shaped host proxy values.
    Launch scopes retain inherited values until diagnostics are sanitized, then release them.

    <!-- proxy-redaction-rules:start -->
    Percent decoding stops when stable or after four stages from the original spelling.
    Redaction retains every intermediate spelling, including both literal-plus and space-plus decoding variants.
    The shared engine decodes retained text through normalized stage zero and four positional stages.
    See the closed transform list and explicit non-goals in [Security](security.md).
    Hexadecimal and base64 patterns match on every stage.
    Component selection and credential-name classification use the same bounded stages.
    A credential-key name at any stage classifies its value as a credential.
    Each form uses its shortest bounded percent-decoded length after normalization and escape decoding.
    This length selects the matching tier.
    Forms with six or more decoded characters match substrings.
    Shorter forms match at ASCII alphanumeric token boundaries on any decoded stage.
    Short block-coded forms require a complete maximal run that decodes to exactly the form.
    Short provenance-only ASCII numbers remain visible unless a credential-key name identifies their value.
    Short forms embedded inside longer alphanumeric tokens remain visible when every stage lacks a boundary.
    Ordinary structured string leaves use the same in-place replacement.
    Each occurrence receives a local replacement, with no occurrence-count limit.
    Replacements preserve unrelated text.
    Each variable selects its marker at scope entry using all its forms and their matching rules.
    A marker containing its variable's form uses the collision-safe local character from the first substitution.
    After every substitution pass, recheck all short and long forms under their respective matching rules.
    After the initial substitution, marker collisions receive at most three additional substitution passes.
    Residual matches receive a local single-character marker, starting with `█`.
    The selected character appears in no form or encoding alphabet and prevents matches across replacement boundaries.
    Residual substitution rechecks newly exposed token boundaries until no form matches.
    An output self-check repeats the engine and falls back to residual cleanup.
    Only an uncleared output uses the fixed refusal marker and a surface-only diagnostic.
    Each retained text field is bounded to 65,536 characters after replacement.
    Existing display and report limits can shorten fields further.
    JSON redaction applies these limits to string fields and preserves the surrounding JSON syntax.
    <!-- proxy-redaction-rules:end -->

- **`on_missing_runtime`** — **removed.** It chose between falling back to the host and aborting when no runtime was found. Refusing is now the only default, so its one remaining meaning was the unsafe half, which lives on as `dangerously_skip_containment` under a name that admits what it does. A config still carrying the key fails at load with a line naming the replacement rather than a generic unknown-key error.

The effective posture (`sandboxed`, `native`, or `uncontained`) is recorded on the audit ledger as a `sandbox_resolved` event per dispatch — along with the resolved `runner`, the `image` it ran in, and (on a refusal) the `reason` it was refused for — so `kew audit` shows whether and how a run was contained. Native runs record `runtime = null` and `image = null` because no external container was used.

## `[hosts]` {#hosts}

A **host** is a machine kew ships a git bundle to and runs one job on — a
verification run for a merge candidate, for example. `local` is the default and
needs no cloud account at all; `gcp` runs the job on a Compute Engine instance
through the `gcloud` CLI. [Hosts](hosts.md) is the operator page: what a host
holds, how to create and provision one, its quota and idle stop, and what it
costs.

```toml
[hosts]
provider = "local"                                 # "local" (default) | "gcp"

[hosts.gcp]                                        # only read when provider = "gcp"
project = ""                                       # GCP project; empty means no gcp host
zone = ""                                          # the instance zone, e.g. us-central1-a
instance = ""                                      # Compute Engine instance name
instances = []                                     # the pool a sharded verify spreads across
max_runs = 1                                       # agent runs one instance may host at a time (1 is the only accepted value)
idle_stop_minutes = 30                             # idle stop scheduled when a job releases the host
job_timeout_minutes = 240                          # deadline for a job's ssh session that has no ceiling
```

- **`provider`** (default `"local"`) — where a job runs. `"local"` clones the
  bundle into a temporary directory on this machine and runs it there, so it
  works with no cloud account. `"gcp"` requires all three `[hosts.gcp]`
  identifiers and is **refused at config load** without them: a provider that
  cannot name its instance would only fail later, at the first job. Any other
  value fails at load rather than being discovered as a missing provider.
- **`project`** / **`zone`** / **`instance`** (all default `""`) — the three
  values that address one Compute Engine instance. Whenever all three are set,
  `kew doctor` probes the host even if `provider` is still `local` — a
  configured host nobody has logged into yet is exactly what that check is for.
- **`instances`** (default `[]`) — the instances a **sharded** verification
  spreads across, in order: [`kew verify --shards N`](cli/verify.md) takes the
  first N of them, and the first also runs everything that runs once. Empty
  means the pool is `instance` alone, which is what an unsharded verification
  uses. `instance` defaults to the pool's first host, so a pool need not repeat
  it. A blank entry, or the same instance twice, is refused at load: one host
  runs one job at a time, so two shards aimed at one instance would queue
  behind each other rather than halve anything.
- **`max_runs`** (default `1`, and `1` is the only value accepted) — how many
  agent runs **one instance** may host at a time. The per-host half of the
  fleet's slot accounting: `[fleet] max_concurrent` bounds the whole fleet, and
  this bounds each machine in it, so a `prefer-gcp` [placement](#sandbox-host)
  knows when an instance is full and has to look elsewhere. Values below `1` are
  refused: a host with room for zero runs would never be placed on, so it would
  only ever bill for being up. Values **above `1` are refused too**, rather than
  quietly lowered — a host runs one job at a time (it holds the instance's lock
  for the whole session, and every job on that machine shares the one checkout
  kew ships), so a second run placed on a busy instance is refused rather than
  run beside the first, and under `prefer-gcp` that refusal would land where the
  fallback to this machine belongs. Raising the ceiling is a change to the host
  transport — a checkout per run on the instance — not a config edit.
- **`idle_stop_minutes`** (default `30`) — the delay in the idle stop that
  `release()` arms on the instance, as a `systemd` timer rather than a
  `shutdown` (which would close logins before it fired — see
  [Idle stop](hosts.md#idle-stop)). The host stops *itself*, so a crashed
  driver cannot leave a VM running and billing. Values below `1` are refused: a
  zero delay is an immediate stop, not an idle grace. An idle stop is **never
  armed while the ledger still shows another run on that instance**: a placement
  is recorded before the instance is claimed, so another run can already be
  attached to that machine — queued for it, or mid-placement from another
  process — while this one finishes, and arming the stop would take the machine
  down under it.
- **`job_timeout_minutes`** (default `240`) — the deadline for one job's SSH
  session **when the job names no ceiling of its own**, counted from when the
  session opens. That is [`verify.timeout_seconds`](#verify) `= 0`, which is
  unbounded everywhere else: a connection that has died silently would hold the
  host lock for ever, and without keepalives and a deadline one such session
  held a verify open for 66 minutes with nothing left of it on the instance.
  Reaching the deadline means the session stopped making progress rather than
  that the job was slow, so the session (its whole process group) is killed, the
  host lock released, and the job reported as an **interruption** —
  `error_category = "host_job_deadline_exceeded"` on the verify event, never a
  failed gate. A ceiling the caller *did* name is honoured as asked and is never
  cut down to this one: cutting it would end a merely slow job and attest it as
  a lost connection. The default still sits above the longest ceiling kew ships
  (`verify.timeout_seconds = 7200`), so neither number surprises the other.
  Values below `1` are refused — a zero deadline is already expired when the job
  starts.

Every SSH and SCP session this provider opens also carries keepalives
(`ServerAliveInterval=15`, `ServerAliveCountMax=4`), so a peer that has gone
away is detected in about a minute instead of never. A session that ends with
no exit marker is reported as a lost connection
(`error_category = "host_connection_lost"`) — an interruption worth retrying,
never a test failure, because `gcloud`'s own exit status cannot tell a failed
job from a failed connection.

`gcloud` is an **optional external CLI**, like `docker` and `gh`: kew probes it
at use, reports it through [`kew doctor`](cli/doctor.md), and never depends on
it as a Python package. With no `gcloud` installed the gcp host is simply
unavailable and `local` is unaffected.

### What a host receives, and what it returns

Code goes in as a **git bundle**. Results come back as **data** — an exit code,
a wall-clock duration, and log text. Nothing a host returns is treated as an
instruction.

The job environment is an **allowlist**. A job gets the variables it declares
and nothing else. On the `local` host it also inherits `PATH`, `HOME`, `LANG`,
`LC_ALL` and `TZ` from kew's own environment when they are set, because the job
runs on this machine and needs a usable shell; a remote host supplies its own.
A declared variable whose **name** looks like a credential is refused — with the
variable named in the error — even when a caller asks for it:

- anything containing `TOKEN`, `KEY`, `SECRET` or `PASSWORD`
- anything in the `GH_*`, `GITHUB_*`, `LINEAR_*`, `ANTHROPIC_*`, `OPENAI_*`,
  `OPENROUTER_*`, `GOOGLE_*` or `CLOUDSDK_*` namespaces

A declared **name** must also be a variable name — a letter or underscore
followed by letters, digits or underscores. A remote job's names are
interpolated into the host's command line, where a value can be quoted but a
name cannot, so an unusual name is refused rather than escaped.

A job's wall-clock ceiling is the job's own, on either host: a verification
given 20 minutes gets 20 minutes on the gcp host too, and a job that outruns
its ceiling is reported as having **no exit code** rather than as a failure.

Only one job runs per host at a time, held by a lock kew owns rather than by
anything the cloud enforces: two jobs on one instance would clone over each
other's checkout. A second job against the same host is refused, naming the pid
that holds it. The lock covers the whole lifecycle, not just the job: bringing
the host up and releasing it are writes to the host as well, and a process
scheduling the idle stop on an instance another job has just claimed would stop
that job mid-run. A release that cannot take the lock does nothing — the holder
that has it owns its shutdown.

## `[drive.merge]` {#drive-merge}

The standing merge rule: what [`kew drive`](cli/drive.md) may merge with no
person in the loop. Off by default.

```toml
[drive.merge]
enabled = false                  # true to let the drive merge
signer = ""                      # your GitHub login; required once enabled
ui_paths = ["frontend/"]         # what counts as a UI change
local_fallback = false           # true to verify locally while [verify] host is down
exclude_epics = []               # epics a person always merges
gate = "github"                  # "verify" to merge on a passing kew verify alone
```

- **`enabled`** (default `false`) — merge READY PRs the rule covers. Off, every
  READY PR waits for a person.
- **`signer`** — the GitHub login that signs the rule. The drive records the
  rule on the [audit chain](audit.md#standing-merge-rule-attestation) the first
  time it reads it and again whenever it changes, and only while `gh` is
  logged in as the signer of record: the rule's own `signer` for a first rule,
  the previous rule's signer for an edit (so a signer can hand the rule over,
  but nobody else can take it). A rule changed under any other login is
  refused, and nothing merges until the signer runs the drive or the rule is
  put back.
- **`ui_paths`** (default `["frontend/"]`) — globs naming a UI change. A UI PR
  merges only when a passing [`kew verify`](cli/verify.md) of its own head that
  ran the browser check (`ui_check`) is on record.
- **`exclude_epics`** (default `[]`) — epics the standing rule never merges,
  for an epic merged by hand (for example after a live browser probe). Their
  READY PRs stay on the needs-you set, with the reason recorded once per head,
  and the drive still ticks them. The drive still reads whether each one
  merges into the base: a PR that conflicts with it is held `ci_pending:dirty`
  rather than reported merge-ready, and the epic's tick resolves it as it would
  in any other epic. The tick reads the conflict itself, before it resolves, so
  the hold and the resolve land in the same round; a hold the resolve itself
  writes (its conflict or its retry bound) is never replaced or released by it,
  and no second resolve runs past it. Because only the review stage resolves,
  a non-empty `exclude_epics` needs `[review] adversarial = true`; kew refuses
  the config otherwise. A person still does the merge. Names match trimmed, case-insensitively,
  with or without `epic:`. Setting it changes the rule, so the signer signs it
  again. It does not stop the epic tick's own merge of a PR a person approved
  on GitHub (a verified human approval): that merge is the person's decision,
  with or without the drive.
- **`local_fallback`** (default `false`) — while `[verify] host` cannot be
  reached, run the merge verification on the operator machine instead. Off,
  an unreachable host pauses merges until it answers again (see
  [`kew drive`](cli/drive.md#merging)). Turning it on changes the rule, so the
  signer signs it again.
- **`gate`** (default `"github"`) — what stands in for the tests. Under
  `"github"` a PR needs green GitHub checks on its head as well as the passing
  `kew verify` of main merged into it that the drive runs before every merge.
  Under `"verify"` that passing verify alone is enough, for when GitHub
  Actions cannot run; the PR must then read as targeting the configured
  repository and base branch before and after the verify, or it is refused.
  Setting it to `"verify"` changes the rule, so the signer signs it again.

The rule covers a READY PR whose **net** paths — the diff against the merge
base — avoid every gate class (workflows, scripts, docker, provisioning,
lockfiles, `pyproject.toml`, `conftest.py`, `kew.toml`, `frontend/scripts`)
and every protected publication path. Everything else is left awaiting a merge
on the needs-you set, with the reason recorded. See
[`kew drive`](cli/drive.md#merging) for how a merge runs.

## `[merge_queue]` {#merge-queue}

The project's merge queue ([`kew merge queue`](cli/merge.md)).

```toml
[merge_queue]
train = 1                        # PRs verified together in one kew verify
```

- **`train`** (default `1`) — how many queued PRs one pass verifies together:
  main merged with each of them, in queue order, under one `kew verify`. When
  it passes, all of them merge in order. When it fails, the queue bisects the
  train to find the first PR that breaks it, fails that PR with the reason, and
  verifies the rest again. `1` is a plain one-at-a-time queue.

  A train above `1` verifies only its final tree, and its PRs still merge one
  at a time. So main briefly holds prefixes of the train that were never
  verified on their own. If a later merge in the train is refused, main can be
  left on a broken prefix until the next pass verifies main and raises
  `merge_queue_attention` ([`kew merge queue`](cli/merge.md)). The default
  `train = 1` has no such window: each merge lands the tree that was verified
  for it.

## `[paths]` {#paths}

Where kew keeps the trees it makes: local verify checkouts, sandbox clones,
reviewer checkouts, held-out and mutation trees, and Switchyard, canary and
readiness-probe scratch directories. (A container ID file lives only while its
container starts, so it stays in the operating system's temp directory.)

```toml
[paths]
work_root = "~/.local/state/kew/tmp.noindex"   # the default
```

| Key | Default | Meaning |
| --- | --- | --- |
| `work_root` | `~/.local/state/kew/tmp.noindex` | The directory every long-lived kew tree is created under. `KEW_WORK_ROOT` overrides it. |

The default is deliberately **not** the operating system's temp directory:
macOS cleans `$TMPDIR` on its own schedule, and a verify that ran across that
cleanup lost files out of its own virtualenv and reported hundreds of test
failures the candidate never caused. Spotlight skips a folder whose name ends in
`.noindex`, so a checkout under the default root is not indexed while a suite
runs in it. kew creates the root, private to your user, when it is missing,
and leaves a `.kew-work-root` marker file in a root it created.

**Each project has its own subdirectory.** The root is shared by every project
on the machine, so kew makes each tree under `<work_root>/<ledger id>/`, where
the ledger id is the project ledger's own identity (the UUID in its registry).
A command that has no registered ledger in reach makes its trees under
`<work_root>/unscoped/`, which nothing ever sweeps. A new or replaced ledger has
a new id, so it never sees trees an older ledger made.

**Retention is kew's own.** A sweep lists only its own ledger's subdirectory. A
tree there is never removed while any run or continuation in the ledger
references it in one of these states: `pending`; any held state, including a
held publication (`awaiting_publication`, `publication_review_required`,
`held_publication`); `awaiting_continuation`; or an unabandoned continuation.
Any other entry kew made there (its name starts with `kew-`) is removed once
nothing anywhere in it — the whole tree, walked without following symlinks —
has changed for **7 days**. A checkout's claim lock (`<checkout>.lock`) stays
while its checkout does, a lock another process holds is never removed, and a
tree whose claim lock a live process holds is kept. A ledger subdirectory that
is a symlink, or does not resolve directly inside the root, is never swept, and
kew refuses to make a tree through it.
Nothing else is ever removed, including the `locks/` subdirectory of the root.
The sweep removes nothing, and logs why, when the ledger cannot be read or has
no identity, or when the root has no `.kew-work-root` marker. It also refuses,
whatever the marker says, a relative root, the filesystem root, your home
directory or any of its ancestors, and the project checkout or any of its
ancestors. The sweep runs at the start of every `kew verify` and on
`kew cleanup` (not `--dry-run`).

Trees made before this setting existed are not moved: kew reads them from the
paths their run evidence recorded.

## `[verify]` {#verify}

`kew verify` runs one merge verification: it bundles the candidate, runs kew's
own gate on a [host](#hosts), and records what the host returned. This section
says where that job runs by default and what it carries.

```toml
[verify]
host = "local"                                     # "local" (default) | "gcp"
deselect = []                                      # pytest node ids the host run deselects
timeout_seconds = 7200                             # wall-clock ceiling; 0 = unbounded
```

- **`host`** (default `"local"`) — where `kew verify` runs the job when
  `--host` is not given. It is deliberately separate from `[hosts] provider`:
  that is the host kew *has*, this is the host a verification *runs on*, so an
  operator can keep a configured gcp instance and still default the gate to the
  machine in front of them. Any value other than `local` or `gcp` fails at
  load.
- **`deselect`** (default `[]`) — pytest node ids the host run deselects,
  reaching the job as `PYTEST_ADDOPTS`. It exists for tests that cannot pass on
  a verify host for environmental reasons alone, and it is empty by default: a
  verification that quietly skips tests is not the gate CI runs. Each entry
  must be a single shell word, so a stray flag cannot ride along inside one.
- **`timeout_seconds`** (default `7200`) — the ceiling for each job, on
  either host: the gate job and the suite job behind it each get it. A ceiling
  of `0` leaves the job unbounded. A job that outruns its ceiling produces no
  status at all, so the verification is recorded as `incomplete` rather than
  as a failure.
- **`held_out_before_review`** (default `false`) — before a reviewer launches
  on an agent head, run the merge verify's held-out phase on that head merged
  with the current base, on this section's `host`. A base test that fails and
  that no **Behavior changes** line in the PR body declares sends the head back
  as a remediation round, and no reviewer is paid for it. The round names the
  tests and asks for a fix or a declaration. The merge verify still requires
  the same declaration. See
  [how it works](how-it-works.md#governed-adversarial-review). The run imports
  the head's `src` before anyone has reviewed it, so it runs only on a verify
  host that is not this machine: with `host = "local"` the check is skipped for
  each head, with the reason "the pre-review held-out check never runs head
  code on this machine; set [verify] host = gcp". A head that edits one of
  `[epic] merge_gate_paths` is not run before review either. A change that
  selects the whole base tree (one under `tests/support/`, say) is left to the
  merge verify, which runs that tree anyway. A failing test that main's health
  lists as red, or that fails on the base's own `src` too, is main's and never
  sent back.
- **`held_out_before_review_timeout_seconds`** (default `1200`) — the ceiling of
  that pre-review run. A run that outruns it is skipped for the head, never
  counted against it.
- **`suite`** (default `"full"`) — which tests a PR's merge verify runs.
  `"full"` runs the whole suite. `"tiered"` runs the test files its change
  selects plus every `policy` test in the two pytest phases, and everything
  else unchanged; it falls back to the full suite whenever the selection's
  safety is in doubt — main red, no green nightly of main within 26 hours,
  a full-suite rule, `--full` — and says why. The coverage floor runs only on a
  full verify. The nightly always runs the full suite. Turn it on only once the
  nightly has been green for a while and the selection report shows the
  selection is worth it. See [the tiered suite](cli/verify.md#tiered).
- **`frontend`** (default `"full"`) — what of vitest and `ui-check` a PR's
  merge verify runs when its change touches `frontend/`. `"full"` runs both
  whole. `"scoped"` runs `vitest related` on the changed files plus every
  test that reads files rather than importing them, and the ui-check modules
  that own the change (by the base commit's ownership lines) or that it adds
  or edits, and runs both whole whenever that is in doubt (the tiered suite's
  doubts, a change to the app shell, `lib/ui`, the design tokens, the
  fixtures loader, `package.json` or a lockfile, `.npmrc`, any deletion under
  `frontend/`, a changed file outside `frontend/` that frontend code imports,
  an unreadable ownership map, a selection of 70% or more) and says why. A
  scoped verify is never evidence of main's health.
  `svelte-check`, the builds and the www gates always run whole, and the
  nightly runs everything. Keep it off until `kew verify --frontend-report`
  reads a median of 50% or less. See
  [the scoped frontend](cli/verify.md#frontend).

Every verification appends one `verify_completed` [audit](audit.md) event — ref
sha, base sha and ref, host, exit code, seconds, whether `ui-check` ran, and the
outcome — whether it passed, failed, or never finished. See
[`kew verify`](cli/verify.md) for what the job itself runs.
