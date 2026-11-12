# TUI Dashboard

kew's terminal dashboard provides a live view of your dispatch queue, active workers, PR reviews, and cost analytics — all from a single screen.

```text
Usage: kew tui
```

```bash
kew tui
```

## The cockpit

The main screen is a 2x2 grid showing four panels:

| | Left | Right |
|---|---|---|
| **Top** | **[1] Queue** — issues sorted by priority | **[2] Workers** — active agents + completed runs |
| **Bottom** | **[3] Reviews** — PRs pending review | **[4] Analytics** — cost and success summary |

Press a panel number (`1`-`4`) to expand it to full screen. Press `Esc` to return to the cockpit. Press `Tab` to cycle focus between panels.

## Navigation

| Key | Action |
|-----|--------|
| `j` / `k` | Navigate up/down in focused list |
| `Tab` | Cycle focus between panels |
| `1` `2` `3` `4` | Expand panel to full screen |
| `Esc` | Back to cockpit |
| `/` | Filter current panel |
| `?` | Help overlay |
| `q` | Quit |

`Shift+J` / `Shift+K` scroll secondary panes — the detail pane in the expanded queue/reviews views and both columns in the analytics screen. They are not bound in the cockpit itself.

## Actions

These keybindings work on the selected item in the focused panel:

| Key | Action | Available in |
|-----|--------|-------------|
| `d` | Dispatch selected issue | Queue |
| `t` | Toggle `ready` label | Queue |
| `A` | Open approve/decline dialog | Queue (held rows) |
| `r` | Retry failed run | Workers |
| `x` | Kill running worker | Workers |
| `R` | Dispatch review on PR | Reviews |
| `M` | Resolve merge conflicts on PR | Reviews |
| `b` | Toggle budget overlay | All panels |
| `e` | Export run history to JSON | Analytics |

All dispatch actions check your budget before proceeding. If the budget is exceeded, a dialog appears with the option to override.

## Approval holds

Issues in the `needs-approval` state appear at the **top** of the Queue panel under a "⏸ NEEDS APPROVAL" header row. Each held row shows the issue number, estimated cost, and the approval basis (e.g. `#42 · est $5.00 · over threshold`).

Select a held row and press `A` to open the approve/decline dialog:

| Key | Action |
|-----|--------|
| `a` | Approve — records `approval_granted`, releases the hold to `ready` |
| `d` | Decline — records `approval_declined`, removes the `needs-approval` label, parks the issue |
| `Esc` | Cancel |

**Approve does not auto-dispatch.** It releases the hold to `ready`; dispatch it like any ready issue with `d` (or a running `kew dispatch`/`kew loop` process will pick it up).

Both decisions are written to the tamper-evident audit log with `source: "tui"`, identical to the `kew approve` / `kew deny` CLI path. If `[approval] approvers` is set in your config and the local operator is not in the allowlist, the action is rejected with an error notification — consistent with the CLI authorization check.

## Status bar

The top bar shows at a glance:

- **Project name** and daily spend vs. budget
- **Worker count** (green when active)
- **Ready queue** count
- **Active tokens** across running workers

Spend estimates update in real time as agents run.

## Pages

- [Panels](panels.md) — what each of the four panels shows
- [Analytics](analytics.md) — the full analytics dashboard
- [Overlays](overlays.md) — budget, filter, help, and approval overlays
