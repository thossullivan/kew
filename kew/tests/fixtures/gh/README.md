# Recorded `gh` fixtures

JSON payloads recorded from real `gh issue view`/`gh pr view --json ...` output,
used by `tests/test_driver_facts_real_gh.py` to drive the real
`GitHubFactsReader`/gh-CLI parsing seam through a hermetic `gh` stub binary
(no network, no mocks of kew's own modules).

Each file is the verbatim stdout of the corresponding `gh` invocation, so parser
drift on the driver surface (the #807 status-crash class) is caught pre-merge:

| File | Records |
|---|---|
| `issue_view_7.json` | `gh issue view 7 --json number,state,stateReason,labels,closedByPullRequestsReferences` — OPEN issue with a linked PR |
| `issue_view_5.json` | same fields — CLOSED/NOT_PLANNED issue with no linked PR |
| `pr_view_42.json` | `gh pr view 42 --json number,state,reviewDecision,mergeStateStatus,headRefOid,commits,latestReviews,author` — APPROVED, CLEAN merge state |
| `repository_permission_write.json` | `gh api repos/o/r/collaborators/alice/permission` — current `User` collaborator with write authority |

The extra fields real `gh` emits (label ids/colors, commit `authors`, review
`authorAssociation`, etc.) are kept on purpose: they are the shapes the reader
must stay tolerant of.
