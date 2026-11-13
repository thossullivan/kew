# kew ci-fix

Dispatch an agent to auto-fix a failed GitHub Actions run.

```text
Usage: kew ci-fix RUN_ID

Arguments:
  RUN_ID          GitHub Actions run ID (required)
```

**Example:**

```bash
# Fix CI failure from run 12345678
kew ci-fix 12345678
```

The resolved runner uses the same containment policy as ordinary dispatch.
Host execution requires `[sandbox] dangerously_skip_containment = true`.
Contained fixes receive no GitHub credential. Kew commits and publishes their
verified changes from the host, bound to the PR head selected before launch.
The checkout is pinned to the validated SHA before the agent starts.
Kew confirms the checkout HEAD and refuses a changed remote branch head.

Each fix creates a Kew run record with a new Kew run ID.
Posture and completion events use that Kew run ID.
The GitHub Actions run ID remains in `ci_fix_completed.action_run_id`.

## Exit codes

See the shared [exit-code scheme](exit-codes.md).

| Code | Meaning |
| --- | --- |
| `0` | Fix completed; no CI repair needed. |
| `1` | Runner or publication failure. |
| `2` | PR lookup query failure; unavailable CI logs; preparation failure; configuration failure. |

A failed PR lookup is not a successful skip.
No PR, an ineligible PR, and exhausted retries remain successful skips.
