# Analytics Dashboard

Press `4` from the cockpit to open the full analytics dashboard. This screen shows fleet performance metrics with breakdowns by model, complexity, and daily activity.

## Lookback periods

Press `h` to cycle through time windows:

- **today** — runs from the current day
- **7d** — last 7 days
- **30d** — last 30 days
- **all** — all recorded runs

The dashboard auto-refreshes every 2 seconds.

## Top stats

Five stat cards across the top:

| Card | Description |
|------|-------------|
| **Runs** | Total completed runs in the period |
| **Success** | Success rate — green (80%+), yellow (60-79%), red (below 60%) |
| **Avg Cost** | Average USD per run |
| **Avg Time** | Average duration in minutes |
| **Total Spend** | Total USD spent in the period |

A warning appears if fewer than 5 runs are available — statistics may not be meaningful at low volumes.

## Left column

**Daily activity chart** — stacked bar chart showing successes (green) and failures (red) per day, with total count on the right.

**Budget gauges** — three horizontal bars showing spend against limits:

- Daily budget
- Weekly budget
- Per-issue budget

Bar color transitions: cyan (safe) to yellow (70%) to red (90%+).

## Right column

**Failure breakdown** — horizontal bars showing failure counts by category (timeouts, budget exceeded, etc.).

**By Model** — table showing each model's run count, success rate, and average cost.

**By Complexity** — table showing simple/medium/complex run counts, success rates, average cost, and average duration. Color-coded by complexity level.

## Keybindings

| Key | Action |
|-----|--------|
| `h` | Cycle lookback period |
| `Shift+J` / `Shift+K` | Scroll both columns |
| `b` | Toggle budget overlay |
| `e` | Export run history to JSON |
| `P` | Purge runs older than 90 days |
| `?` | Help overlay |
| `q` | Quit |
| `Esc` | Back to cockpit |
