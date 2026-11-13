# Panels

Each cockpit panel shows a different slice of your dispatch state. Press the panel number (`1`-`4`) to expand any panel to full screen with a detail pane on the right.

## [1] Queue

The queue panel shows your GitHub issues in four sections (the first appears only when held issues exist):

**⏸ NEEDS APPROVAL** — when held issues exist, a `⏸ NEEDS APPROVAL` header row appears at the very top of the queue panel. Held rows below it show the issue number, estimated cost, and the approval basis (e.g. `#42 · est $5.00 · over threshold`). Select a held row and press `A` to open the approve/decline dialog.

**Ready** — issues with the `ready` label, sorted by priority then age:

- Priority badge (P0-P3) color-coded by urgency
- Issue number and title
- Complexity label (simple / medium / complex)
- Assigned model

**Backlog** — issues without `ready`, shown dimmed below a separator. Sorted by priority then age. Shows relative age (3d, 2w, 1mo).

**Failed** — issues labeled `agent-failed`, shown in red. Includes extra labels like `budget-exceeded` or `agent-needs-help` when present.

**Actions on queue items:**

- `d` — dispatch the selected issue (ready items only)
- `t` — toggle the `ready` label (moves between ready/backlog/failed)
- `r` — retry a failed issue
- `A` — open the approve/decline dialog for a held (needs-approval) issue

When expanded (`1`), the right pane shows the full issue body rendered as markdown.

## [2] Workers

The workers panel shows active and recently completed runs:

**Active workers** (top section):

- Issue number and title
- Model name
- Elapsed time
- Progress bar — color transitions from cyan (early) to yellow (mid) to red (high burn)
- Token count

**Completed runs** (below separator, most recent 10):

- Status indicator (green OK / red ERR)
- Issue number and title
- Duration and cost
- Relative time (e.g., "3h ago")

**Actions on worker items:**

- `x` — kill the selected running worker (sends SIGTERM)
- `r` — retry a failed run

When expanded (`2`), the right pane shows a live event stream with tool calls, token counts, and progress for the selected run.

## [3] Reviews

The reviews panel shows pull requests with the `pr-pending-review` label:

- CI status indicator (green check, red X, or yellow pending)
- PR number and title
- Author
- Comment count
- Age

**Actions on review items:**

- `R` — dispatch an agent to address review comments
- `M` — dispatch merge conflict resolution

When expanded (`3`), the right pane shows the full PR description and review state.

## [4] Analytics

The cockpit analytics panel shows a compact 5-line summary:

- **Today** — number of runs
- **Success** — success rate (color-coded: green above 80%, yellow 60-79%, red below 60%)
- **Avg cost** — average USD per run
- **Avg time** — average duration in minutes
- **Budget** — daily spend vs. daily limit

Press `4` to open the full analytics dashboard. See [Analytics](analytics.md) for details.
