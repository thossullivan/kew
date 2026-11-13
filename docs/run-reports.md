# Run reports

When `[run_reports]` is enabled, kew writes one small markdown **run report**
per run into a machine-level sink (`~/.local/share/kew/runs` by default). The
sink pools the exhaust of every repo kew orchestrates into one corpus, so
cross-project decision questions — "which model handles docs PRs well", "what
did runs shaped like this cost", "how did this failure mode resolve before" —
have something to retrieve from.

Configuration: [`[run_reports]`](configuration.md#run_reports). Design:
`docs/design/2026-08-21-run-report-sink-design.md`. The kew repo
itself runs with the sink enabled (its committed `kew.toml`, since
2026-08-22) — the corpus below is dogfooded, not hypothetical.

## When reports are written

- **At dispatch completion** — after the completion row, the
  `dispatch_completed` audit event, and the imported runner evidence are all
  in the store, so the report can reference them.
- **Again when outcomes settle** — the post-dispatch outcome sync and
  `kew report sync` re-export the report (idempotent atomic overwrite) once the PR's
  fate is known; that is the point where `outcome: merged`, `pr_url`, and
  `review_cycles` appear. A report read between the two writes carries the
  at-completion truth, not the final word.
- **Not covered:** runs reaped as orphans (killed while unsupervised) and any
  terminal transition that happens outside a host-supervised dispatch or an
  outcome sync. The corpus is the exhaust of supervised work, not a complete
  ledger — the run store and audit chain remain the systems of record.

## Where reports land

```text
<sink_path>/<repo-slug>/<run-id>.md
```

The repo slug is the `owner/repo` identity with unsafe characters replaced
(`o/r` → `o--r`). Writes are atomic (temp file + rename) and **best-effort**:
a report that cannot be written logs a warning and never fails the run.
The governance policy snapshot attests `sink_path`. Changing the destination
therefore changes the `policy_hash` recorded for later runs and audit events.

When GitHub renames the repository, [`kew rename-repo --apply`](cli/rename-repo.md)
moves `<sink_path>/<old-slug>/*.md` to `<sink_path>/<new-slug>/`. It changes
only the `repo` value (when it is the old slug) and the `pr_url` value (when it
is on the old repository), in the frontmatter and in the body lines that
render them; every other byte stays as it was.

## The report format (stable contract, `schema: 1`)

YAML frontmatter of **kew-authored structured fields only** — values copied
from the run record and the audit chain. No transcript text, no agent prose,
nothing an LLM generated. Fields that were not recorded are **omitted, never
fabricated**: a sparse run record produces a sparse-but-honest report, and
consumers must tolerate missing keys.

```markdown
---
run_id: "run-abc123"
repo: "owner/repo"
item_id: "1578"
reported_model: "claude-opus-5"
review_cycles: 1
status: "success"
outcome: "merged"
pr_url: "https://github.com/owner/repo/pull/9"
started_at: "2026-08-21T12:00:00+00:00"
finished_at: "2026-08-21T12:30:00+00:00"
cost_usd: 1.23456
duration_seconds: 1800.0
total_tokens: 50000
input_tokens: 40000
output_tokens: 10000
cache_read_tokens: 30000
attempts: 2
issue: 1578
title: "Add the sink"
branch: "agent/issue-1578"
complexity: "medium"
run_kind: "dispatch"
runner: "claude-code"
model: "claude-opus-5"
self_check: true
evidence: ["audit:2766:evidence.receipt_fact", "audit:2768:evidence.runner_progress"]
schema: 1
exported_at: "..."
---

# Run run-abc123
...
```

Notes on individual fields:

- **`outcome`** is the retrieval-facing terminal result: `merged` when the
  run's PR merged, otherwise the raw run status (`success`, `failure`,
  `killed`, `budget_exceeded`, …). `status` always carries the raw value.
  `merged` appears via the outcome-sync re-export, never at completion time.
- **`cost_usd` / `duration_seconds` / token counts are always present** — for
  these, zero is a recorded fact (a free local run), not an absence: it reads
  `0.0` for costs and durations, `0` for token counts. A run whose cost could
  not be priced still reads `0.0`; the run store's accounting marks
  (`unpriced_model`) remain the authority on that distinction.
- **`evidence`** lists the run's chain-attested audit event ids
  (`audit:<id>:<event>`), scoped to the run's work item and filtered to
  events that carry this run's id. Any claim can be checked against the
  tamper-evident chain with [`kew audit verify`](cli/audit.md). Runs whose
  events carry no run id contribute nothing here — an empty or missing
  `evidence` means "no run-scoped attested events", not "verified clean".
- **`self_check`** is `true` when the dispatch prompt carried the pre-publish
  self-check against the reviewer rubric (see
  [how it works](how-it-works.md#prompt-system)). It is **omitted** otherwise,
  including on review, remediation, resolve and ci-fix runs, whose prompts have
  no such section — absent means "this prompt carried none".
- **Test-integrity fields** — `tests_touched`, `asserts_removed`,
  `skips_added`, `changed_expected`, `test_only_fix_round` (all integers) and
  `integrity_flags` (a list of kew's own flag names, `[]` when clean) — measure
  what the run's `base...head` diff did to its tests. They appear only on runs
  that write a branch (`dispatch`, `ci_fix`, `resolve`, `review_remediation`),
  and only once kew has read that diff: when kew's reviewer checks out a
  published head, the report of the run that published it is re-exported with
  the counts for the PR's `base...head`. A later outcome-sync re-export keeps
  the values that measurement recorded. `test_only_fix_round`
  counts the commits after the first that touched tests and no `src/` file.
  No line of the diff is ever copied into a report — only counts and flag names.
- **`schema`** versions the contract; new fields are added additively.
  String fields (`title`, `error_category`, …) are omitted when unrecorded.

## Using the corpus

The sink is plain markdown, deliberately: index it with whatever retrieval
layer you like (a [Basic Memory](https://github.com/basicmachines-co/basic-memory)
project, grep, your own embedding pipeline). If a report is ever injected into
an agent prompt, treat it as **untrusted data** — the same framing kew applies
to every non-host-authored input. Frontmatter values are escaped defensively,
but `title` and `branch` originate from work items other people can author.

Two operational notes:

- **Retention is yours.** The sink grows one file per run and kew never
  prunes it. The files are plain markdown — archive or delete at will;
  nothing reads them back.
- **The machine-level default pools across repos.** Issue titles, branch
  names, and PR URLs from every repo land in one directory. If some repos
  are confidential (client work), point their `kew.toml` at a separate
  `sink_path` — pooling is a choice, not an obligation.
