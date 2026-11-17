# kew cleanup

`kew cleanup` finds orphaned label states left after a crash or an interrupted
dispatch, such as an issue stuck under a label that no live run owns, and
repairs them when you pass `--reset-to`. Without `--dry-run` it also sweeps
stale pending runs and removes work trees past retention that no live run
references.

```text
Usage: kew cleanup [OPTIONS]

Options:
  --reset-to TEXT  Reset orphaned issues to: ready or agent-failed
  --dry-run        Show what would change without making changes
```

`--dry-run` changes no run records and no labels.
It still opens the run store, which creates the ledger on a fresh host and migrates an older one.
It lists the stale pending run records the sweep would mark failed.
It reports orphaned issues as if those records were already failed.
It does not preview stop-label resets that `kew stop` left queued.
The real run retries those resets first.
When resets are queued, the dry run's orphan report and exit code can differ from the real run's.

**Example:**

```bash
# Preview orphaned issues
kew cleanup --dry-run

# Reset orphaned issues back to ready
kew cleanup --reset-to ready
```
