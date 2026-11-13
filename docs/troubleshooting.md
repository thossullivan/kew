# Troubleshooting

Common failure modes and how to resolve them.

## "kew run picks no issue"

kew queries GitHub for issues with the `ready` label. If nothing is picked:

1. **Check labels** — verify the issue has the `ready` label and a priority label (`p0-critical` through `p3-low`):
   ```bash
   gh issue list --label ready
   ```
2. **Check authentication** — ensure `gh` is authenticated and can access the repo:
   ```bash
   gh auth status
   ```
3. **Check config** — verify `project.repo` in `kew.toml` matches your repository, or that `gh` auto-detects it correctly:
   ```bash
   kew run --dry-run
   ```
4. **Already in-progress** — issues labeled `in-progress` or `agent-working` are excluded from selection. Check if the issue is already being worked on.

## "Agent times out"

The agent is killed after `agent.timeout_minutes` (default: 120 minutes). If your agent consistently times out:

1. **Increase the timeout** — set a higher value in `kew.toml`:
   ```toml
   [agent]
   timeout_minutes = 180
   ```
2. **Check the complexity label** — `complex` issues get more token budget guidance but still respect the timeout. Consider breaking the issue into smaller pieces.
3. **Check the issue scope** — issues that touch many files or require cross-cutting changes are more likely to time out. Use the `simple` / `medium` / `complex` labels to set expectations.

A run that failed with the error category `stream_silent` was stopped sooner: its model call wrote nothing for `agent.no_output_minutes` (default 15), so kew read the provider as stalled; a reviewer round stopped this way is retried on the next tick ([Silent runs](configuration.md#agent-no-output-minutes)).

## "Budget exceeded"

kew enforces per-issue, daily, and weekly spend caps configured in `[budget]`.
The daily and weekly caps stop every new run, a work item's own reviews and
remediation included. A node an epic drive holds with the reason
`budget: daily cap $… reached ($… spent)` (or `weekly`) is waiting on one of
these caps. The spend is the window's recorded spend; a reason that adds
`$… reserved by running or starting runs` was refused because of runs still in
flight. The hold clears by itself at the next UTC midnight (or once the
week's oldest spend ages out) or as soon as you raise the cap, and the next tick
runs the review or remediation. A run already in progress finishes.

1. **Check current spend**:
   ```bash
   kew status --cost
   ```
2. **Increase limits** — adjust `[budget]` in `kew.toml`:
   ```toml
   [budget]
   per_issue_usd = 20.0
   daily_usd = 100.0
   weekly_usd = 400.0
   ```
3. **Force past the check** — use `--force-budget` to bypass budget checks for a single hand-run command (`kew dispatch`, `kew review`, `kew resolve`). The epic drive has no bypass:
   ```bash
   kew dispatch 42 --force-budget
   ```
4. **Per-run cap** — the per-run cap (`agent.max_budget_usd`) is separate from the aggregate budget. Increase it if individual runs are hitting the limit:
   ```toml
   [agent]
   max_budget_usd = 15.0
   ```

## "Checkout already exists"

A standalone clone can remain after a crash or interrupted dispatch. kew stores it at `{branches.worktree_base}/agent/issue-{N}`. A relative base starts from the directory where you ran kew.

`kew cleanup` reports orphaned label states; it repairs them only when you pass `--reset-to ready` or `--reset-to agent-failed`. It does not remove clone directories.

1. **Confirm the effective base**:
   ```bash
   kew config effective --sources
   ```
2. **Inspect the clone before removal**:
   ```bash
   git -C .worktrees/agent/issue-42 status --short --branch
   ```
3. **Get any work you need out of the clone first.** Push the branch, or copy the directory elsewhere. A commit made inside the clone lives only in that directory; removing the directory removes the commit.
4. **Remove the clone directory**:
   ```bash
   rm -r -- .worktrees/agent/issue-42
   ```

Replace `.worktrees` when `branches.worktree_base` uses another path.
