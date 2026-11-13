# kew resolve

Dispatch an agent to resolve merge conflicts on a PR branch.

The checkout is pinned to the selected SHA before Kew integrates the base branch.
Kew confirms the checkout and remote branch heads before integration and before launching an agent.
Head drift refuses resolution and reports both SHAs.

Conflict resolution supports runners that require **host-publication**,
including official Codex. Canonical mode always uses host publication. The
runner receives no commit, push, or GitHub mutation authority in that mode. Kew
commits the merge resolution in a fresh canonical repository. It pushes from
that repository.

Checkout mode has one agent-publication path. An uncontained agent-style runner
commits and pushes from its checkout. Kew commits and pushes for
host-publication runners and contained runs. A failed host publication preserves
the checkout, a standalone clone, and returns non-zero.

Canonical publication always creates a merge commit. This applies to both
`merge` and `rebase` strategies. A rebased checkout supplies its final tree,
but its rewritten commits are not copied. A review-sensitive path stops the
push and preserves the checkout for human action.

```text
Usage: kew resolve [OPTIONS] PR_NUMBER

Arguments:
  PR_NUMBER       PR number to resolve conflicts for (required)

Options:
  --dry-run       Check for conflicts without dispatching
  --force-budget  Skip budget pre-check
```

**Example:**

```bash
# Check if PR 42 has conflicts
kew resolve 42 --dry-run

# Dispatch agent to resolve conflicts
kew resolve 42
```

## Exit codes

See the shared [exit-code scheme](exit-codes.md).

| Code | Meaning |
| --- | --- |
| `0` | Resolution completed; no conflicts; dry-run preview. |
| `1` | Runner or publication failure. |
| `2` | PR metadata or head branch unavailable; budget refusal; connectivity failure; checkout failure; pre-launch refusal (not_run); configuration failure. |

Messages and run records retain raw runner codes.
The command boundary maps every nonzero executed result to `1`.
