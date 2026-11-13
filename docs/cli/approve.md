# kew approve / kew deny

Release or deny a `needs-approval` hold — including **remotely, without a terminal**.

```text
Usage: kew approve [OPTIONS] ISSUE

Options:
  --reason TEXT
```

```text
Usage: kew deny [OPTIONS] ISSUE

Options:
  --reason TEXT
```

```bash
kew approve N [--reason "..."]   # release the hold (records approval_granted)
kew deny    N [--reason "..."]   # deny: remove the hold, park the issue
```

Both record an audited decision (`approval_granted` / `approval_declined`) with the
actor (`KEW_ACTOR`, else the OS user), the reason, and the original estimate, then:
- **approve** → flips `needs-approval` → `ready` (the scheduler picks it up);
- **deny** → removes `needs-approval` only (parks the issue; never closes it).

Both are **idempotent** (keyed on the label) and **fail loudly** (exit 2) if the
run-store db is absent — a release that can't be audited must not silently succeed.

## Remote approval (no terminal)

`kew approve` and `kew deny` on the kew host work without the `approval.yml`
workflow, for an actor the `[approval].approvers` list admits (any actor while
the list is empty), when the run store exists; otherwise they refuse, as above. Releasing
a hold by comment needs a `.github/workflows/approval.yml` workflow in the repo,
and neither `kew setup` nor `create-kew` installs it. kew's own workflow does
not run unchanged in another repository: its preflight step fails without
`kew/pyproject.toml`, and it runs `uv sync` and `uv run kew` in the `kew`
working directory. Write a workflow like it and adapt the preflight check, the
working directory, and how kew is installed and run on your runner. kew's
version runs only while the repository is private, on the self-hosted runner
that holds the run-store (the `KEW_DISPATCH_DB` repo variable). With such a
workflow installed, a governor approves from a phone by commenting on the held
issue:

```text
/approve looks fine, ship it
/deny too expensive for this change
```

The workflow authorizes the commenter (must have repo write access; optionally an
`[approval] approvers` allowlist) and runs `kew approve`/`kew deny` on the
self-hosted runner. **Security:** there is no token, link, or public endpoint — the
trust anchor is GitHub's own auth + write-access, gated at the workflow job level
so an unauthorized comment never reaches the runner.

## Delivery

`kew report --notify` POSTs the spend report to the configured `[alerting]
webhook_url` (Slack-compatible). When a dispatch is held, kew also comments the
release instructions on the issue (and webhooks them) unless
`[alerting] notify_on_hold = false`. The notice names `kew approve <issue>` and
`kew deny <issue>`, and says the `/approve`·`/deny` comments work only with
`.github/workflows/approval.yml` installed.
