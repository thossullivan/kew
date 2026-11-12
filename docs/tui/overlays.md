# Overlays

The TUI has four overlay modals that float above the current screen.

## Budget overlay

Press `b` to toggle the budget overlay. It shows real-time spend and limits:

- **Daily** — current spend vs. daily limit
- **Weekly** — current spend vs. weekly limit
- **Per-issue avg** — average cost per issue vs. per-issue limit

Below the limits:

- **Burn rate** — current dollars per hour based on active workers
- **Daily limit in** — estimated time until the daily budget is exhausted at the current pace

Color coding: green (under 60%), yellow (60-90%), red (above 90%).

Press `b` or `Esc` to dismiss.

## Filter overlay

Press `/` to open the filter. Type a search term and press `Enter` to apply.

Filtering is case-insensitive substring matching on the rendered text of each row. It works on whichever panel is currently focused:

| Panel | Searchable fields |
|-------|-------------------|
| Queue | Issue number, title, complexity, model, labels |
| Workers | Run number, title, model |
| Reviews | PR number, title, author |

Press `Esc` in the filter overlay to clear the filter entirely.

The active filter text appears in the breadcrumb bar when a filter is applied.

## Help overlay

Press `?` to show the keybinding reference. It displays all available keybindings grouped by category:

- **Navigation** — panel switching, row selection, scrolling
- **Actions** — dispatch, kill, retry, review, resolve
- **Overlays** — budget, filter, help, quit

The help overlay is context-aware — it shows keybindings relevant to the current screen.

Press `?` or `Esc` to dismiss.

## Approval dialog

Press `A` on a held (needs-approval) queue row to open the approval dialog. It shows:

- The issue label
- Estimated cost
- Approval basis (e.g. `over threshold`)

| Key | Action |
|-----|--------|
| `a` | Approve — releases the hold to `ready` (does not auto-dispatch) |
| `d` | Decline — removes the `needs-approval` label and parks the issue |
| `Esc` | Cancel — dismiss without taking action |

Both decisions are written to the tamper-evident audit log with `source: "tui"`, identical to the `kew approve` / `kew deny` CLI path.
