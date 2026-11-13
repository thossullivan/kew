# `kew delegate` — Assign-to-kew delegation watcher

```text
Usage: kew delegate COMMAND [ARGS]...
```

`kew delegate` lets a human hand off a Linear issue to kew by assigning it to the kew
seat (or adding a configured label). `kew delegate watch` runs a foreground loop that
detects the assignment, claims the issue, and dispatches it as a one-node ephemeral epic
— reusing the same claim → dispatch → close path as `kew epic run`.

> **Status — foreground supervised loop.**
> Run `kew delegate watch` in the foreground under human supervision.
> This supervised loop is the open-source product boundary.
> Running delegation as a durable unattended service is out of scope.
> `--dry-run` previews candidates without claiming issues or spending money.

## Refusal-episode comment semantics (#971)

When a budget/preflight pre-check refuses a delegated issue, kew writes a terminal
refusal row and posts **one** Linear comment naming the cap that bound and the knob
to raise. While the issue stays over budget the watcher keeps re-surfacing it (its
trigger label survives the refusal), but kew comments only **once per episode**: a
repeat refusal updates state silently until the episode ends (a successful dispatch
or any non-refusal outcome resets it). Raise the named `budget.*_usd` limit and the
next re-claim proceeds. See [`[delegation].refusal_cooldown_seconds`](../configuration.md#delegation).

## Assign means go

**There is no confirm step.** The moment an issue is assigned to the kew seat (or
carries the trigger label) and the watcher sees it, kew claims it and spends money
on a contained agent run. Nothing asks you first — that is the point of the feature.

The guards are the *budget caps*, not a prompt:

- `[budget].per_issue_usd`, `daily_usd`, and `weekly_usd` are checked in the claim
  preflight through the shared resolver (env > DB override > `kew.toml` > default),
  exactly as for an epic node. Over cap, kew refuses, comments once naming the cap
  that bound, and leaves the issue alone.
- `[parallel].max_workers` is the shared fleet ceiling — delegated work and
  `kew epic run --all` draw from the same pool, so delegation cannot outrun it.
- The breaker and sandbox containment apply unchanged.

Set those caps to numbers you are willing to lose *before* you assign anything to
kew, and use `--dry-run` first if you want to see what would be picked up.

## Requirements

- `[tracker].type = "linear"` — delegation is a Linear-only feature.
- `[delegation].assignee` set to the kew Linear member name — the watcher resolves
  this to a member ID at startup and refuses to start if it is blank.
- `LINEAR_API_KEY` environment variable set to a Linear personal API key.

## Setting up the kew member seat

kew is a real Linear member seat, not an integration or an OAuth app — its identity
is a personal API key belonging to that seat. Single-tenant and firm-operated: one
seat, one key.

1. **Invite a seat with a plus-addressed email.** Most mail providers (Gmail among
   them) deliver `you+kew@gmail.com` to `you@gmail.com`, so you can create a second
   Linear member without a second mailbox. In Linear: **Settings → Members → Invite**
   and send the invite to `you+kew@gmail.com`.
2. **Accept the invite in an incognito/private window.** Your normal browser is
   already signed in as you; accepting there would just re-authenticate your own
   account. Open the invite link in an incognito window and complete signup there.
3. **Set the member's name to `kew`** (or whatever you put in `[delegation].assignee`
   — the watcher resolves that display name to a member ID at startup and refuses to
   start if it cannot). Keep the two in sync.
4. **Create the API key as kew.** Still in the incognito session:
   **Settings → Security & access → Personal API keys → New key**. Export it as
   `LINEAR_API_KEY` where you run the watcher. Everything kew does — comments, state
   transitions, the post-merge close — is attributed to that seat, so the audit trail
   in Linear reads as "kew did this", not "you did this".
5. **Create the trigger label** (`kew:ready` by default) in the workspace, if you
   intend to delegate by label as well as by assignment.

> Anyone who can assign an issue to the kew seat — or apply the trigger label — can
> spend your budget. Treat seat membership *and* the ability to apply that label as
> spend permissions — see [Assign means go](#assign-means-go) above.

## `kew delegate watch [--dry-run]`

```text
Usage: kew delegate watch [OPTIONS]

Options:
  --dry-run    Log 'would claim / would dispatch' without reserving or spending.
```

```bash
kew delegate watch                # foreground watcher — Ctrl-C to stop
kew delegate watch --dry-run      # preview mode: no claims, no spend
```

Starts a foreground polling loop (like `kew ingest serve` — no daemon, no autostart).
On each tick it reads the local event cursor from the run store (every
`[delegation].fast_interval_seconds` seconds) and polls Linear (at most once per
`[delegation].poll_interval_seconds` seconds) to find issues that are **either** assigned to
the kew member **or** carry the configured delegation label — either trigger alone
surfaces a candidate. For each candidate it:

1. **Re-verifies** the live issue: it must remain actionable and assigned to kew or carry the configured delegation label.
2. **Reserves** a fleet slot and checks the delegation budget without changing Linear ownership or workflow state.
3. **Dispatches** a child under the `"delegation"` pseudo-epic, sharing the fleet cap with `kew epic run --all`.
4. The child checks budget, worker cap, route, runner, and containment before committing its pending row and reservation binding.
5. After durable admission, the child claims Linear immediately before agent execution.

A pre-admission refusal leaves the original assignee and unstarted state unchanged.
An unassigned, label-triggered item therefore remains unassigned after a budget refusal.
After the budget increases, the watcher retries when the refusal cooldown expires.
After a claim, a refusal retains the claim and never resets Linear automatically.
`issueUpdate` has no version precondition.
The item stays assigned in its claim state. A replacement dispatch re-claims it.
For manual recovery, unassign the item in Linear and move it back to its unstarted state.
No kew command releases a retained claim: this manual recovery is the only way back.
`kew epic status` shows a `retained claim` line for each affected epic item.
Each line shows its latest recorded reason and the current recovery instructions.
Historical events use current instructions, including events recorded before this change.

Ctrl-C shuts down cleanly.

## Configuration

The `[delegation]` block in `kew.toml`:

```toml
[tracker]
type = "linear"               # delegation is Linear-only

[delegation]
assignee = "kew"                # the kew Linear member name (required at watch start)
label = "kew:ready"             # Linear label that signals a delegated issue is ready
poll_interval_seconds = 20      # throttled Linear API poll floor (seconds, >= 1)
fast_interval_seconds = 2       # local tracker-events read cadence (seconds, >= 1)
refusal_cooldown_seconds = 300  # skip re-claiming a budget-refused item this long (0 = off)
```

| Field | Default | Description |
|-------|---------|-------------|
| `assignee` | `""` | The kew Linear member name. Required at watch start. |
| `label` | `"kew:ready"` | Linear label that signals a delegated issue is ready. |
| `poll_interval_seconds` | `20` | Throttled Linear API poll floor (seconds). |
| `fast_interval_seconds` | `2` | Local tracker-events read cadence (seconds). |
| `refusal_cooldown_seconds` | `300` | Skip re-claiming a budget-refused item for this long, damping the refuse→comment loop (`0` disables). |

Field-by-field notes live in [`[delegation]`](../configuration.md#delegation) in the
configuration reference. Precedence is the shared resolver's: env > DB override >
`kew.toml` > default — so a budget override set in the `kew serve` Budget panel
governs a delegation run exactly as it governs an epic-driver run.

## Optional: the webhook tunnel

Without a webhook, the watcher's local `tracker_events` read has nothing new to find
and pickup latency is bounded by `[delegation].poll_interval_seconds` (20s by default) —
which is fine, and needs no inbound network at all.

Wiring the Linear webhook receiver makes assignment events land locally within
`fast_interval_seconds` instead. It needs a public URL pointing at `kew ingest serve`, so
in development that means a tunnel. The full recipe — creating the webhook in Linear,
the signing secret, and the `cloudflared` tunnel — is in
[Linear webhooks — dev-tunnel recipe](../linear-webhooks.md#dev-tunnel-recipe).

Delegation is correct either way: the throttled poll is the floor, the webhook is
only an accelerator, and the claim reservation (not the event stream) is what
prevents a double dispatch.

## Exit codes

See the shared [exit-code scheme](exit-codes.md).

| Code | Meaning |
|------|---------|
| `0` | Clean shutdown (Ctrl-C). |
| `2` | Misconfiguration (`tracker.type` not `linear`, `delegation.assignee` not set). |
| `2` | `LINEAR_API_KEY` not set at startup, or Linear authentication failed at runtime (expired/revoked key) — the watcher terminates loudly rather than retrying forever (#971). |

## See also

[Configuration — `[delegation]`](../configuration.md#delegation) — full field reference.

[Configuration — override precedence](../configuration.md#override-precedence) — how the `[budget]` caps that guard an assign=go dispatch are resolved.

[Linear webhooks](../linear-webhooks.md) — the optional receiver + dev-tunnel recipe that shortens pickup latency.

[`kew epic`](epic.md) — the epic driver that delegated work shares its fleet cap and claim/dispatch/close path with.
