# kew quota

Release a runner lane that kew is holding after the provider refused a run for
quota.

When a runner's provider account rejects a run for quota (for example Claude's
seven-day limit), kew does not mark the work as failed. The item goes back to
ready and kew **holds that runner's lane**. While a lane is held, the epic tick
dispatches nothing to it, and `kew doctor` reports `runner-quota: fail`.

The hold's reason ends with the agent token the provider refused — its env
vars and fingerprint, never its value — so you know which token to raise or
replace:

```text
runner-quota-held: claude-code seven_day quota exhausted, resets 2026-11-17T07:00:00Z; agent token CLAUDE_CODE_OAUTH_TOKEN (fingerprint 1a2b3c4d5e6f)
```

When the hold ends before the provider's reset (the reset was unknown, or more
than eight days out), the reason adds `; runner lane held until <time>`. When
no credential env var was set at the refusal, kew cannot say which token it was
and the reason ends `unknown credential`.

Every surface shortens a reason to 160 characters, so the reason shortens
itself first. A token named by two env vars (both `ANTHROPIC_API_KEY` and
`CLAUDE_CODE_OAUTH_TOKEN` set) leaves room only for the time the lane frees, as
in `runner-quota-held: claude-code seven_day until <time>; agent token
ANTHROPIC_API_KEY/CLAUDE_CODE_OAUTH_TOKEN (fingerprint 1a2b3c4d5e6f)`. The
token's names and fingerprint are always kept, and `kew epic status`,
`kew epic watch`, `kew doctor` and `kew quota clear` all show the same reason.

A hold ends on its own when one of these happens:

- the reset time the provider gave passes (one hour if the provider gave none);
- eight days pass since the refusal, whatever reset the provider gave;
- the runner's credential in the environment is not the one the provider
  refused (for example a new `CLAUDE_CODE_OAUTH_TOKEN`). kew stores only a
  12-hex-character fingerprint of the credential, never the value. If either
  side has no credential to compare, the hold stays.

## Usage-limit warnings

Before a provider refuses a token, it usually warns that the window is filling
up. When a Claude Code run's stream reports that (a `rate_limit_event` whose
status is `allowed_warning`), kew records it. This happens for dispatch and
reviewer runs alike. kew keeps one warning per runner and limit (`five_hour`,
`seven_day`, `seven_day_opus`, `seven_day_sonnet` or `overage`; any other name
is shown as "a usage limit"), with the fingerprint of the credential the run
used. A later notice for the same limit and credential replaces it unless it
gives an earlier reset or none. A warning holds nothing: work keeps dispatching
until the provider actually refuses (see
[Security](../security.md#usage-limit-warnings-are-advisory)).

While the warning applies, `kew epic status` prints a line that names the
token, its fingerprint and the limit:

```text
Usage warning: agent token CLAUDE_CODE_OAUTH_TOKEN (fingerprint 1a2b3c4d5e6f) for claude-code is near its seven_day limit (92% used), resets 2026-10-30T07:00:00Z
```

The same warning is one `usage_limit_warning` event in `kew epic watch`. A
repeated notice in the same window does not raise a second event, so the
event's reason leaves out the percentage used, which rises with each notice. The
`--json` status document lists it under `token_warnings`. kew prints the
credential's env var name and fingerprint, never its value.

A warning stops applying when its reset time passes (five hours after it was
seen if the provider gave none), or when a later run of that runner launched
under a different credential: a new token starts a new window. kew records
each launch's credential fingerprint for this, so the shell that runs
`kew epic status` or `kew epic watch` shows the same warnings whatever
credentials its own environment holds.

## Commands

```text
Usage: kew quota [OPTIONS] COMMAND [ARGS]...

Commands:
  clear  Release one runner's quota hold now, before the provider's reset.
```

`kew quota clear` ends a hold now. Use it when you know the limit is gone
before kew can tell, such as a raised account limit.

```text
Usage: kew quota clear [OPTIONS] RUNNER

  Release one runner's quota hold now, before the provider's reset.

Arguments:
  RUNNER  The runner lane to release, e.g. claude-code.
```

kew records who cleared the hold and when in the dispatch ledger, beside the
hold itself, and prints the hold it cleared. With no active hold, the command
says so and changes nothing. An unknown runner name is refused and changes nothing.

**Example**

```bash
kew doctor --json | jq '.checks[] | select(.id == "runner-quota")'
kew quota clear claude-code
```
