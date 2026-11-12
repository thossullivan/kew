# ─── kew recipes ──────────────────────────────────────────────────
# Copy these into your justfile. Adjust `kew` to
# `uv run --group kew-cli kew` if using uv dependency groups.

# Dispatch: one-time setup (labels, security hooks, directories)
dispatch-setup:
    kew setup

# Dispatch: pick next ready issue and implement
dispatch:
    kew run

# Dispatch: drain the queue until empty
dispatch-drain:
    kew run --drain

# Dispatch: parallel drain with N workers
dispatch-parallel workers="3":
    kew run --drain --workers {{workers}}

# Dispatch: one named issue
dispatch-issue number:
    kew dispatch {{number}}

# Dispatch: preview next issue without running
dispatch-dry-run:
    kew run --dry-run

# Dispatch: queue status, run history, and cost
dispatch-status:
    kew status --history --cost

# Review: dispatch agent to address PR comments
review pr:
    kew review {{pr}}

# Review: preview PR comments without dispatching
review-dry pr:
    kew review {{pr}} --dry-run
