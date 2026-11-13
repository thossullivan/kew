# kew report

Generate an outcome-linked spend report in markdown: what the fleet did, what it cost, what shipped. Headline metric: **cost per merged PR**.

```text
Usage: kew report [OPTIONS]

Options:
  --days INTEGER     Reporting window in days [default: 7]
  -o, --output PATH  Write markdown to a file (default: stdout)
  --notify           POST the report to the configured webhook
  --models           Render the per-model outcome scorecard instead of the spend report
```

The report contains up to eight sections: header (repo, period, generated-at), totals (spend, runs by status, tokens, spend vs configured budget caps), outcomes (PRs opened, PRs merged, cost per merged PR, median cost per run), the top 5 most expensive issues, per-model and per-runner breakdowns, a cache economics section (when cache token data exists), held/blocked runs, and a footer stating the cost-per-merged-PR formula.

**Planned vs actual cost.** Every run records both a **planned bound** (its tokens priced from the model catalog, with any `[pricing]` override on top — the figure dispatch plans against) and the **recorded actual** (`estimated_cost_usd`: the runner's native provider-billed total when it reports one, else the same token bound). Total spend is always the actual. When any run in the window recorded a bound, the totals section adds a **Planned bound** line naming both halves and their ratio — the third honest cost pair alongside list-vs-subscription. Runs from before this pair shipped — and runs on a model with no pricing entry, which have no rate to price against — carry no bound and are excluded from the comparison, never counted as `$0` planned.

When governed-review runs have positive estimated spend, the totals section adds a **Governed-review spend** line — the `adversarial_review` + `review_remediation` run-kind spend as its own bucket (it is a subset of total spend, not additive). Reviewer runs are a separate budget pool (`[review].max_review_usd`) and are bucketed separately in the cost-anomaly breaker medians too (#445), so a heavyweight review run is never judged against cheap first-dispatch baselines.

**Cost per merged PR** = total spend in the window ÷ distinct PRs from window runs that those runs record as merged (see below). Spend on failed and killed runs is deliberately included — that is the honest unit economics. Zero merged PRs renders `n/a`.

**How a PR's state is read.** Several runs can name one PR (the dispatch that wrote it, a failed attempt on the same branch, review and remediation runs), and their recorded states can differ: the outcome sync does not refresh a failed, refused or held run whose branch also has a successful run, and it stops refreshing a run once it reads `CLOSED`. Every figure — the report's PRs merged, the scorecard's merge rate, and the evidence pack's headline and outcome ledger — reads a PR's state by one rule: **a PR is merged if any of its runs recorded it `MERGED` or recorded a merge time; otherwise its state is the newest state any of its runs recorded.** The rule reads the runs inside the report's window: a PR whose only run in the window is a stale one, and whose merge was recorded on runs before the window, counts as not merged in that window. So the evidence pack's headline merged count equals the sum of its scorecard's merged column over the same window.

## Cache economics

When any run in the reporting window recorded cache-creation or cache-read tokens, the report adds a **Cache economics** section showing a per-(runner, model, provider) breakdown and the aggregate savings vs an all-cold baseline.

| Column | Definition |
| --- | --- |
| **Runner / Model / Provider** | The three-part identity of the route that produced the cache data |
| **Read tokens** | Prompt tokens served from the provider's cache (at the read-discounted rate) |
| **Write tokens** | Prompt tokens written to cache (at the write-premium rate) |
| **Uncached** | Input tokens that were neither cached nor written — plain prefill |
| **Hit ratio** | `read_tokens / (read + write + uncached)` — the fraction of input work served from cache |
| **Runs (cold)** | Total runs in the group, with the count of runs that had zero cache activity (neither read nor write) in parentheses |

The summary line reports total savings:

```text
Cache saved **$4.20** vs all-cold baseline ($42.00) — **10.0%** savings
```

The "all-cold baseline" is a counterfactual: what the input tokens would have cost if the provider had no prompt caching at all (full prefill at the base input rate, no write premium or read discount). The savings line is the difference between actual and that baseline.

A one-line reminder also appears after the interactive usage section (when present):

```text
*Cache saved you $4.20 (10.0% of input cost)*
```

**Capturing a pre-auto baseline.** Before enabling `cache_aware` or `order_aware`, run `kew report` with the feature in advise-only mode (default). The cache section shows the savings the feature *would* achieve, letting you compare before-and-after numbers without the confound of the feature acting on itself. The ledger also records every `routing_decision` and `cache_reorder` audit event with `applied: false`, so you can rebuild the counterfactual from the audit trail.

**Examples:**

```bash
# See cache savings for the last 7 days (advise-only mode by default)
kew report

# Monthly cache economics
kew report --days 30

# Inspect governor decisions (advise or applied)
kew audit tail | grep routing_decision
```

PR outcomes (merge state, review cycles, diff stats) are persisted to the local SQLite store.
`kew report` attempts to sync the latest state from GitHub before reading.
Reports, scorecards, and evidence exports require a resolved repository before generation.
An unresolved repository prints `Report repository unavailable. Set project.repo in kew.toml.` and returns exit code 2.
This refusal writes no file and sends no webhook.
After any query failure, the report renders using persisted outcomes, which may be stale.
It then prints `Report outcome sync incomplete: query failures N.` to stderr and returns exit code 2.
Degraded Markdown and scorecards begin with a heading before any file write or webhook delivery:

```text
# Outcome data is stale: N GitHub queries failed; PR states may be out of date
```

Degraded HTML exports begin with the same warning.
JSON exports and HTML data islands include `{"outcome_sync": {"query_failures": N, "stale": true}}`.
Healthy content has no warning and records `stale: false` in exports.
Evidence exports also return exit code 2 after synchronization failures.
This rule also applies to `--models`, `--output`, and `--notify`.
Confirmed PR absence does not count as a query failure.
An empty store returns exit code 0.
Run [`kew report sync`](#outcome-sync-report-sync) to refresh outcomes without generating a report.

**Examples:**

```bash
# Last 7 days to stdout
kew report

# Monthly rollup
kew report --days 30

# Write to a file (e.g. from cron, then mail it to your boss)
kew report -o spend-report.md
```

## Outcome sync (`report sync`)

Backfill PR outcomes (merged/closed state, review cycles, diff stats) for past runs.

```text
Usage: kew report sync [OPTIONS]
```

`kew report sync` walks every run whose PR outcome isn't yet final — those with no persisted
outcome **and** those still marked open — and fetches the current state of each PR from
GitHub (so a merge or close that landed since the last sync is picked up here). Runs
already in a terminal state (merged/closed) are skipped. Results are written to the local
SQLite store so that `kew report` can read them without making live GitHub calls.

When to use it:

- **After connectivity gaps** — if kew was offline while PRs were merging, run
  `kew report sync` to catch up before generating a report.
- **Scheduled sweep** — add `kew report sync` to cron to keep outcome data fresh without
  waiting for the next `kew report` run.
- **CI/CD integration** — call `kew report sync` in a post-merge workflow to record merge
  state close to the event.

Behaviour:

- **Partial progress**: successful outcomes and confirmed missing PRs are persisted.
  Missing PRs are recorded as `ABSENT` and checked again on later sweeps.
  Query failures leave existing outcomes unchanged.
- **Exit status**: any required GitHub query or orphan warning write failure returns `2`.
  Authentication errors, network errors, and invalid responses count as query failures.
  Otherwise, sync returns `0`, including zero work and confirmed missing PRs.
- **Summary**: `synced N, no PR M, query failures K`.
  The first two counts describe runs.
  Query failures count distinct failed branches, failed orphan detection, and failed comment reads or writes.
- **Deduplication**: multiple runs that share a branch (e.g. retries on the same
  issue) count as one GitHub call.
- **Repo resolution**: uses `[project] repo` when set, otherwise auto-detects it
  via `gh repo view` (the same detection `kew report` and dispatch use). If no repo
  can be determined, `kew report sync` reports repository unavailability and exits `2`.
- **Orphan reconciliation**: a failed PR query preserves stored orphans and increments the query failure count.
  Comment failures preserve successful warnings and the detected orphan set.
  An unavailable comment read prevents posting and never counts as an existing warning.
  Reconciliation reports posted warnings, confirmed existing warnings, and separate comment-read and comment-write failure counts.
  Each failed comment operation reports a static category.

`kew report` also syncs outcomes automatically before building the report, so
you only need to run `kew report sync` explicitly when you want to update the store without
generating a report.

**Examples:**

```bash
# Pull the latest PR outcomes for all un-synced runs
kew report sync

# Schedule a daily sync (crontab example)
0 6 * * * cd /path/to/repo && kew report sync
```

See the [CLI exit-code scheme](exit-codes.md).

## Model scorecard (`--models`)

`kew report --models` renders a per-model outcome scorecard — merge rate, mean review submissions, and cost per merged PR, broken down by model and complexity — measured from your own repo's actual outcomes, not a benchmark. The normal spend report is unchanged; `--models` renders only the scorecard.

**Metrics**

| Column | Definition |
| --- | --- |
| **Runs** | All runs using that model (throughput; spend basis — includes failed and abandoned runs) |
| **Merge rate** | `merged / opened` PRs **authored** by the model (see Attribution). Always shown with raw counts, e.g. `71% (12/17)`. Renders `—` when no PRs were opened. |
| **Reviews** | Mean `review_cycles` per authored PR — the count of PR review submissions, **including bot reviewers** (e.g. CodeRabbit), and **0** for a PR that merged with no reviews. Renders `—` when no data is available. |
| **$/merged PR** | Total spend for that model's runs ÷ distinct merged PRs it authored. Spend includes all runs (failed, abandoned, and runs it spent reviewing other models' PRs), so a model with many no-PR runs shows a higher value. Renders `—` when no PRs merged. |
| **Planned vs actual** | Planning accuracy: recorded actual ÷ planned bound, over the runs carrying both halves of the pair. `1.00x` means the token × pricing-bound estimate predicted spend exactly; below `1.00x` means the bound over-records (an OpenRouter run has measured ~`0.10x`). Renders `—` when no run in the bucket recorded a bound — runs from before the pair shipped are excluded rather than counted as `$0` planned. |

Rows are grouped by the configured `model` field (the routing lever, always present). Each model row is followed by `· simple / medium / complex / unknown` complexity sub-rows; sub-row runs and PR counts sum to the model total.

**Attribution.** When multiple runs share one PR — cross-model retries, and every governed review — the PR (opened, merged, `review_cycles`) is credited to the **authoring** run: the earliest-`started_at` successful run whose `run_kind` is not a review kind (`review`, `adversarial_review`, `review_remediation`, and the other kinds that work on a PR someone else wrote). A failed attempt that recorded the PR URL takes the credit only when no authoring run on the PR succeeded. Reviews start after the dispatch they review and carry the same PR URL, so crediting the latest run would hand every reviewed PR's merge to the reviewing model (#1131). When a PR has review runs only — an out-of-band PR that kew reviewed but did not write — the latest run is used as a fallback so the PR is still counted somewhere. The authoring run decides only which model gets the credit; whether the PR merged is read across all of its runs, by the rule under **How a PR's state is read** above.

Each run's `estimated_cost_usd` counts toward the model that ran it, regardless of outcome or run kind. So a failed opus retry raises opus's $/merged PR and earns it no merge, and spend a model incurs reviewing someone else's PR sits on its own row against the PRs it authored. `review_cycles` follows the PR, not the reviewer: it counts GitHub review submissions on the authored PR (bots included), which is a property of that PR's code, not of whichever model kew ran as reviewer.

**Honest scope.** `opened` and `merged` count PRs that had at least one successful, synced kew run — a PR whose kew runs all failed (and so no PR URL was ever recorded by kew) is not counted. The merge rate answers "of the PRs this model successfully produced, how many merged?" not "every PR the model ever touched."

**Small-sample and honest-null handling.** Rows with 1 or 2 opened PRs are marked `*` in the merge-rate cell; rows with no opened PRs show `—`. Zero-denominator cells always render `—` (em dash), never `0%`. If a model has no opened PRs yet (fresh repo or only failed runs), it still lists with its runs and spend while merge rate, Reviews, and $/merged render `—`. If there are no runs at all in the window, the output renders a single line: `No model activity in the last N days.`

`--days` and `-o` apply as they do for the spend report. `--models --notify` is rejected with an error (webhook delivery is for the spend report, not the scorecard).

**Illustrative output:**

```text
# Model scorecard — owner/repo

- **Period:** 2026-05-27 → 2026-06-26 (30 days)
- **Generated:** 2026-06-26 14:00 UTC

| Model | Runs | Merge rate | Reviews | $/merged PR |
| --- | --- | --- | --- | --- |
| claude-opus-4-8 | 24 | 71% (12/17) | 1.4 | $3.10 |
| · simple | 6 | 83% (5/6) | 1.0 | $1.20 |
| · medium | 10 | 67% (6/9) | 1.5 | $2.90 |
| · complex | 8 | 50%* (1/2) | 2.0 | $5.40 |
| claude-sonnet-4-6 | 31 | 58% (11/19) | 2.1 | $0.92 |
| · simple | 18 | 73% (8/11) | 1.8 | $0.40 |
| · medium | 13 | 38% (3/8) | 2.7 | $1.60 |

\* low sample (<3 PRs) · Reviews = PR review submissions (incl. bots) · $/merged PR includes failed and abandoned runs
```

**Examples:**

```bash
# Scorecard for the last 30 days
kew report --models --days 30

# Write to a file
kew report --models -o scorecard.md

# Rejected — --notify is for the spend report, not the scorecard
kew report --models --notify
```

## Evidence pack (`report export`)

`kew report export` writes a **self-contained evidence pack** — one HTML file (inline CSS/JS, no CDN references, prints cleanly) or its machine-readable JSON twin. It is the client-facing deliverable: a single artifact proving what ran, what it cost, and what merged over a window.

```text
Usage: kew report export [OPTIONS]

Options:
  -o, --output PATH     Output file path for the evidence pack  [required]
  -f, --format [html|json]  Export format  [default: html]
  --since TEXT          Window start (ISO 8601, e.g. 2026-06-01). Default: 30 days ago.
```

v1 sections:

- **Economics summary** — total spend, PRs opened/merged, cost per merged PR, and spend-vs-cap for the rolling 1-day and 7-day windows.
- **Outcome ledger** — issue → run(s) → PR → merge state, grouped **per epic** (runs not attributed to an epic fall into an `(unassigned)` bucket). A row's merge state is the state of the PR it names, read by the same rule as the headline.
- **Run-kind breakdown** — runs, success rate, and cost split by dispatch / review / loop / planning.
- **Model scorecard** — per-model runs, merge rate, mean review submissions, spend, and cost per merged PR, with a per-complexity drill-down. Reuses the same scorecard data as `kew report --models` (no re-derivation), rendered into the pack rather than recomputed. It reads the runs from the pack's own `--since`, so it covers the same runs as the economics summary.
- **Audit attestation** — the [tamper-evident audit chain](../audit.md) verified in-process (the same `kew audit verify --anchors` machinery, no subprocess): event count, the verified id range, the chain-head hash, and — when an `audit-anchors.jsonl` file is present at the repo root — how many anchors matched plus any gaps (mismatched / absent / ahead). Verify-on-demand only: the pack embeds the head hash so a reader can re-run `kew audit verify` later; nothing is published externally.

The HTML embeds a machine-readable JSON data island (`<script type="application/json" id="kew-evidence-data">`) so a single HTML file is also a data carrier; `--format json` writes the same payload as a standalone file. Report data is read through the service layer (`kew.service.report`), not by reaching into the run store directly.

**Examples:**

```bash
# Self-contained HTML evidence pack (last 30 days)
kew report export --output evidence-pack.html

# Machine-readable JSON twin
kew report export --output evidence-pack.json --format json

# Explicit window start
kew report export --output pack.html --since 2026-06-01
```

## Feature provenance (`report provenance`)

`kew report provenance` writes the build history behind each console feature: which epics, issues, pull requests and runs built it, what they cost, and how review and verification went. The demo console's "How this was built" panel reads it.

```text
Usage: kew report provenance [OPTIONS]

Options:
  --features PATH  TOML file listing each console feature and the work that built it  [required]
  --out PATH       Directory to write provenance.json and runs.json into  [required]
  --corpus PATH    Run-report corpus directory (default: the run_reports sink)
  --repo TEXT      OWNER/NAME whose runs to export (default: project.repo)
```

**Repo scope.** Both files hold only runs whose recorded repo is the project repo (`--repo`, else `project.repo` in `kew.toml`); a run recorded against another repo, or with no repo at all, is left out whatever its PR URL says. When no run in the ledger is recorded against the project repo, one stderr note names the repo, and both files are still written. A run of the project repo whose PR URL is not exactly `https://github.com/<project repo>/pull/<n>` is left out too: it adds no row, PR, count or cost, and one stderr note counts how many were dropped without naming them. Review rounds and held-out counts are scoped the same way. A round counts only when a review verdict recorded against the project repo in the audit names its round, head and verdict. A round with no such verdict, such as one the merge queue recorded, is left out and counted in one stderr note per issue. The held-out join reads the pull request's heads only from those rounds and from audit rows recorded against the project repo, and joins only a merge verify (`verify_completed`) recorded against the project repo. Some reads are still keyed by issue number alone, so an issue of another repo with the same number can still reach them: the `epic` column, the drive snapshot head the held-out join starts from, and where a review cycle starts after `kew epic retry` (#3170).

**The features file** lists one `[[features]]` table per feature. `routes` is required. A feature names its `epics` (ledger epic refs), its `issues` (issue numbers), or both. Any other key is refused. A seed for the console's main surfaces lives at `frontend/demo/features.toml`. Correct it against your ledger before release.

```toml
[[features]]
id = "monitor"
title = "Monitor"
routes = ["/monitor"]
epics = ["epic:live-stream"]
issues = [2847, 2889]
```

An epic's issues are the nodes in that epic's own latest driver snapshot, so a node two epics share counts for each. An epic the ledger does not hold, and an issue with no run, review round or epic node in the ledger, are each named on stderr, and the feature is still written from what the ledger does hold.

**`provenance.json`** has one entry per feature with these fields:

| Field | What it holds |
| --- | --- |
| `epics`, `issues` | The epics the file names, and every issue they cover plus the issues it names |
| `prs` | Each PR URL the feature's runs opened, with the merge queue's `landed_sha` (`null` for a PR merged by hand) |
| `runs` | Run count by run kind |
| `list_cost_usd` | Total recorded cost of those runs, at list price |
| `review_rounds` | Governed-review rounds by verdict, every review cycle included |
| `held_out` | From each issue's latest merge verify (`verify_completed`, read across the whole ledger): how many issues had held-out failures, and how many tests failed or were undeclared |
| `integrity` | Runs the run-report corpus flagged for test-integrity changes, and the count of each flag |

`integrity_source` is `corpus` or `none`. The corpus is the only record of integrity flags. Without `--corpus`, the command reads the configured `[run_reports] sink_path`. If that directory does not exist either, every integrity count is 0 and one note goes to stderr.

**`runs.json`** has one row per run of the project repo, built from the run report's own fields. A row keeps only these fields: `run_id`, `item_id`, `epic`, `pr_url`, `run_kind`, `model`, `runner`, `status`, `outcome`, `cost_usd`, `duration_seconds`, `total_tokens`, `review_cycles`, `attempts`, `started_at`, `finished_at` and `error_category`. A model id the [model catalog](models.md) does not name is written as `"other"`, and so is an error category outside the fixed list kew publishes (`PUBLISHED_ERROR_CATEGORIES`, the same list as `frontend/demo/error-categories.json`). An empty `item_id` is written as `null`. A run whose `run_id` is not a uuid (a legacy id) is left out of both files, and one stderr note counts how many were dropped. Prompts, logs, titles, branches, self-check text and event payload text are never written to either file.

Both files carry `as_of`: the newest `finished_at` among the exported runs (`null` when none has finished), never the time the command ran, so the same ledger always writes the same bytes.

The command reads only the local ledger and corpus, and makes no GitHub call.

A features file that cannot be read or has the wrong shape, a `--corpus` that is not a directory, no repo to scope to, a missing run store, or an `--out` that cannot be written exits `2` and writes neither file. Otherwise the command exits `0`, notes on stderr included.

See the [CLI exit-code scheme](exit-codes.md).

`frontend/demo/provenance/` holds committed samples (`*.sample.json`) built from an invented ledger. The real files are generated into the same directory at release and are git-ignored.

**Example:**

kew's own `kew.toml` sets no `project.repo`, so name the repo to export:

```bash
kew report provenance --features frontend/demo/features.toml \
    --out frontend/demo/provenance --repo OWNER/NAME
```
