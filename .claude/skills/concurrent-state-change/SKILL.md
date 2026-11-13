---
name: concurrent-state-change
description: Use when touching locks, reservations, lifecycle/state-machine transitions, repair/reconcile loops, or caches fronting a verification — graph/driver/lock.py, graph/driver/tick.py reconcile, delegate/claim.py, delegate/run.py, delegate/watch.py, run_store reservations, service/audit.py cache
---
# Concurrent-State Change

Every invariant below traces to a verified incident from the 2026-07 delegation
waves (PRs #966/#968/#974/#975). The failure class is constant: the mechanism
is correct in isolation and wrong against another actor — a concurrent driver,
a live claim, a repair loop, the next cooldown tick. Walk your change against
each invariant before opening the PR.

## Invariants (each traces to an incident)

- **A repair loop must prove "dead", never infer it from state-shape.** An
  item mid-startup looks identical to a stranded one. Check for a live
  reservation/run row before repairing (PR #975 finding 1: reconcile
  `reset_to_ready` a live re-claimed item during the detached-child startup
  window; guard now at `src/kew/delegate/run.py` reconcile pass).
- **The fix must fire on the DEFAULT path.** A remedy gated behind a new flag
  does not fix the incident that motivated it (PR #974 finding 1: steal only
  under `--takeover`, so a plain `kew epic run` still parked observer against
  the exact #970 stale lock; `src/kew/graph/driver/lock.py`).
- **Every write to shared state re-checks ownership first.** Heartbeat
  refresh, lock-record update, row finalize: verify you still hold it — a
  stolen-from writer must detect loss, not clobber the thief (PR #974
  finding 2).
- **Check-then-act on shared state is a race until proven serialized.** The
  check must live inside the same transaction/critical section as the act —
  the `BEGIN IMMEDIATE` idiom of `reserve_epic_node_item_under_cap`
  (`src/kew/run_store.py`); incidents: #966 Task 5 cap TOCTOU, PR #974
  finding 3 (check-then-unlink steal → two live drivers).
- **Releasing a resource re-arms whatever throttled its acquisition.** Fixing
  a leak can create a loop: released item + surviving trigger + poll = churn
  (PR #966 finding 1: refusal → release → 20s re-claim → refusal, ~288
  comments/day; cooldown damping now in `src/kew/delegate/run.py`).
- **A cache in front of a verification must bound how long it can lie.** Key
  on content every mutation changes, or TTL-bound it — "invalidated on
  growth" is not "invalidated on modification" (PR #968: middle-row tamper
  masked indefinitely; TTL cache now in `src/kew/service/audit.py`).
- **Rollback/cleanup calls fail too.** Order state rollback before
  best-effort I/O (comments, notifications) and leave a detectable retry
  surface when rollback itself throws (PR #966 finding 3 + #975 Task 2:
  failed `reset_to_ready` stranded In-Progress with no repair path).

## Checklist

- [ ] List every OTHER actor that reads/writes the state you touch (epic
      driver, watch loop, reconcile tick, TUI, a second copy of your own
      process). For each: what happens if it runs between your check and
      your act?
- [ ] Failing test first — real store + real concurrency where claimed
      (threads or subprocesses; copy from `tests/delegate/test_atomic_cap.py`
      or `tests/test_driver_lock.py`). Mock only at tracker/sink seams.
- [ ] Adding a flag? Confirm the motivating incident is fixed with the flag
      ABSENT.
- [ ] Adding a repair/reconcile pass? Add the false-positive test: a
      live/starting item must survive it.
- [ ] Kill your process mid-transition (SIGTERM, exit 143): does restart
      reconcile the orphan, and does a peer NOT double-run it?

## Acceptance

Run from `kew/`:

    uv run pytest tests/delegate/ tests/test_driver_lock.py tests/test_driver_tick.py tests/test_serve_api_audit.py -q
