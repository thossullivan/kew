---
description: How to open the web console that kew serve runs on localhost against your existing run history, and what each of its views shows.
---

# Web UI

`kew serve` opens a unified cockpit — one 56px header framing all governance views in a SvelteKit SPA. Everything runs on localhost by default and reads your existing run history; no account, no external service.

!!! note "Flight Recorder status"
    Each run's page (`/runs/<id>`) renders its Flight Recorder view from the
    `/api/runs/<id>/flight` route.
    The `/timeline` and `/events` API routes are live, but no web page reads them yet.

```bash
pip install 'kew-cli[serve]'
kew serve   # → in a terminal, prints a tokenized link: http://127.0.0.1:8765/#token=…
```

See [`kew serve` reference](../cli/serve.md) for every flag and example.

## Cockpit shell

Every page sits under the same header: the `kew` wordmark and a cursor coloured
by state, three tabs, counters for what is working and what needs you, the
delivery repo, and a **More** menu. The needs-you counter is drawn only while
somebody is needed: with an empty set the header carries no needs-you mark. An
item a run is executing on reads as working and is never counted as needing
you. The three tabs are the factory:

| Tab | What it does |
|---|---|
| **Work** (`/work`) | The issue list and one item's detail: its pipeline, what it is waiting for, its description and its evidence ledger. Before an issue is chosen, the detail pane shows the needs-you queue — the count the header carries, and a link into each wait. `/work?filter=needs-you` narrows the list to what needs a human, and lists every entry of it — including the ones that name an epic or a pull request rather than a work item. |
| **Monitor** (`/monitor`) | The fleet: runs working, the needs-you list, today's and this week's agentic spend against their caps, live runs, holds and blocks, and the event stream. Each event reads as one line — what happened, the epic, item or PR it names, and the one fact that matters — with the payload the log recorded one click away on the row itself wherever it holds more than the line shows. An approval hold carries **Approve** / **Deny** in the holds column, which is where an epic's approval call to action sends you. |
| **Epics** (`/epics`) | Epic progress — the list, and each epic's DAG with its selected node's runs, evidence and call to action. Each row's needs-you count is the same number as that epic's legend, and a **no epic** row counts the waits that belong to no epic, so the column adds up to the header. A stalled epic says so in one sentence, with a link to its waits on Work. |

<!-- demo-step: needs-you/1 -->
**What needs you**

The header counts the waits that are yours: a merge decision, an approval, a
node only a person can restart. A run in flight is never counted. With nothing
waiting, the header draws no needs-you mark.
<!-- /demo-step -->

[See it in the demo](https://demo.kew.dev/work?tour=needs-you&step=1)

Press <kbd>?</kbd> on any page to see what it answers and every shortcut it
takes; <kbd>Esc</kbd> closes it and puts the focus back where it was.
<kbd>⌘</kbd> <kbd>K</kbd> (<kbd>Ctrl</kbd> <kbd>K</kbd>) opens the palette:
go to any page, epic, item or run, or run an action the server offers. See
[Keys and the palette](keys-and-palette.md) for every shortcut and every palette
action.

### First run

When the console has no work and no run history, `/work` shows the
**first-run card** in its detail pane. The card appears only after every
read has returned. If a read fails or is still loading, that does not prove
the factory is empty, so the card stays hidden.

The card lists, in order:

1. **What to fix.** Lists each readiness check that fails, then each one that
   warns, by its id. These are [`kew doctor`](../cli/doctor.md)'s checks,
   served by `/api/readiness`. Each one shows the check's own fix command, or
   its hint when the check has no command. The card has no fix commands of its
   own. If the readiness reading is still loading or failed, the card says so.
   It does not claim the checks passed.
2. **The next command:** [`kew setup`](../cli/setup.md). It sets the
   repository up for kew: labels, the security hook, and log directories.
3. **Then:** the first dispatch. When the dashboard serves an open issue (a
   ready one, or one kew has not touched), this is `kew dispatch N` for it, with
   its own quiet **Copy**. With none, the card says the step in words and
   prints no command.

One command is the next move: the first listed fix, or `kew setup` when no
check needs a fix. Only that command has the magenta **Copy** button, and
<kbd>c</kbd> presses that button.

With no `kew.toml` in force, the `project-config` check fails and the next
command is [`kew init`](../cli/init.md) instead. It writes the config and
runs setup itself. `kew init` is then the next move and the first command on
the card. The other checks read kew's built-in defaults, so their fixes may
change once the config exists. They wait behind one closed line after
`kew init`: "N more checks read built-in defaults; they re-run after
kew init". Open it to see them. Their commands are never the magenta one.

### Why the list agrees with `kew epic status`

Both read one rule: a node whose tracker item is closed behind a merged pull
request is `done`. `kew epic status` reads it off the tracker live; the console
has no tracker, so it reads the newest journaled snapshot and reconciles it
against the one record in the ledger that carries both halves of that rule:
kew's own `epic_merge` attestation, which the merge pass writes only after the
pull request merged *and* it read the tracker item closed. That reconciliation
is what keeps a **finished** epic off the active list once its driver has stopped
ticking: the last tick an abandoned journal recorded can be months old, with
`held` and `remediating` rows on nodes whose work has since landed, and nothing
will ever write a later one. The epic page, the live stream, the list and the
needs-you set all apply it, so none of them can call a node waiting on you
while another calls the same node done; `/api/epics/{ref}/ticks` does not,
because a journaled tick is the record of what that tick observed.

A merged PR on its own is not a landing. A merged PR row in the run ledger proves
that a pull request merged and says nothing about the tracker item, and the two
do not follow from each other: kew's own merge pass closes the item itself and
re-reads the tracker afterwards, precisely because a merge does not reliably
close one (a missing closing keyword on GitHub; on Linear, no keyword closes
anything). So a merged row never promotes a node. A merge performed outside kew
reads as whatever the last tick recorded until a tick observes the closed item —
and `kew epic status`, which reads the tracker live, will tell you in the
meantime. If the last tick recorded a node as `merged_unclosed` — the PR merged
while the tracker item was still open, which the merge pass closes on its next
tick — or as `merged-not-closed`, the merge pass's own word for that same state
after a close that did not take, the console holds it, with that reason, exactly
as `kew epic status` does.

Nor does an attestation of some *other* pull request land a node, nor an older
attestation than the state it would repair. Evidence counts only when it names the
pull request the node is actually on **and** the journal has not recorded that node
moving since the merge, so an item reopened for more work after an earlier attested
merge keeps reading as live work — on its new PR, or, before that PR exists, on the
status the journal timestamped after the merge. A node the journal recorded with no
PR at all and no move since, which is every closed Linear issue, names no second
pull request, so its attestation stands.

### Merge verify

Under the pipeline, an item with a pull request shows its latest merge verify
([`kew verify`](../cli/verify.md)) as one reading:

- **Merge verify failed at `3670e43f`: 2 of main's tests fail on this PR.** The
  held-out phase ran the base branch's tests against the pull request, and the
  ones that failed are listed beneath it by test id. A failing test the PR body
  declared under **Behavior changes** is listed last, with the note "excused by
  a declared behavior change"; the count is the tests nobody declared, which
  are the ones that refused the merge. The list shows at most 50 ids, each cut
  to 300 characters; the rest are counted ("and 21 more").
- **Merge verify failed at `3670e43f` before main's tests ran.** The held-out
  phase reported nothing. That follows a failed preflight, image build or setup
  as readily as the PR's own gate, so the reading says when the verify failed
  and blames nothing.
- A failed verify whose held-out phase did report, with no undeclared test
  failure, says so: the PR's own checks failed while main's tests pass, or
  main's tests were skipped.
- **Merge verify passed at `3670e43f`.** The head the verify ran on.
- **Merge verify did not finish at `3670e43f`.** The verify never reported an
  outcome, and no verify at that head ever did, so nothing about the tests is
  claimed. When the host recorded why
  (for example `host_job_deadline_exceeded`), that error category is printed
  beneath, exactly as recorded.
- **Merge verify passed at an older head, `3670e43f`; the pull request has
  moved since.** The verify ran on a head the pull request has since moved
  past, so it is not a reading of the PR as it stands. Every outcome names an
  older head this way.
- **No merge verify is linked to this pull request's heads.** The item is
  published and no verify event joins one of its heads (see below).
- **The merge verify record could not be read.** The audit, or the review
  rounds or audit rows the join reads heads from, did not answer; nothing is
  claimed either way.

A verify event names no item. It names the commit it verified (`ref_sha`), the
base it verified against (`base_sha`) and the verified commit's parents
(`ref_parents`). The drive's merge verify and an ad hoc operator merge both
verify a merge commit of the base and the PR head. The drive merges main into
the PR head, so the head is the first parent; the ad hoc merge checks out main
and merges the PR in, so the head is the second parent.

The reading comes from the `verify_completed` audit events that join one of the
heads this item's pull request has had in its current review cycle: the head the
drive last saw, the head of each review round of the cycle, or the head named by
the latest publication scan, credential finding or review verdict. It is the
newest of them with a verdict at the head it joined on, and an unfinished one
only when that head has no verdict (see below). An event
joins when its verified commit is one of those heads, or when the verified
commit has exactly two parents, one of them is its `base_sha`, and the other one
is one of those heads. No other parent joins: a pull request stacked on this
one has this one's head as its parent, and its verify is not a verify of this
pull request. A verify event recorded before `ref_parents` existed links only
when it verified the bare head, which is why the page says "no merge verify is
linked" rather than "none is recorded".

The newest joining event picks the head. At that head, a verify that did not
finish never replaces a verdict: when the same head has an earlier verify that
passed or failed, the newest of those is the reading. So a re-verify that ended
with no verdict leaves a failure, and the merge decision's lead with it, in
place, while a later pass at the same head replaces the failure. The unfinished
re-verify is still in [`kew audit`](../cli/audit.md).

A failed verify is drawn in red; it is magenta
only when the item is already waiting on a person. The verify's own error text
is never shown here; [`kew audit`](../cli/audit.md) has it.

### What "needs you" counts

One number, read from one place. The server composes the **needs-you set** once
per request — every open spend approval, every epic hold, every stalled driver,
every epic node whose state waits on a person (awaiting a merge, a continuation,
a host publication, a no-change decision — and, on a **stalled** epic, an arm),
every work-projection exception, and every orphan PR — and each entry names its
epic, its item and the action.

An epic node's state is the driver's last journaled reading, so two waits are
checked against what the server already knows before they are counted:

- a wait on a **pull request** (awaiting merge, or its review or checks stalled)
  leaves the set as soon as any of the node's runs records that pull request
  merged (a merged state or a merge time, the rule every report reads)
  — the next poll, not the next driver tick, and without asking GitHub on every
  poll. Only the pull request the node names counts: a node reopened onto a
  second pull request still waits on that one;
- an **unarmed** Linear node (waiting for the `kew:ready` label) is yours only
  when its epic has **stalled**: the driver stopped, so the label and a
  `kew epic run <epic>` are what start it again, and that is the command the
  row carries. While the driver is running it picks the label up by itself,
  and the node stays a quiet ring with the action on its status line.

A stalled epic's banner says how many of its nodes wait on you, with a link to
the Work screen's needs-you list, where each one carries its command.

<!-- demo-step: needs-you/2 -->
**The needs-you queue**

Before you choose an item, the Work detail pane lists the set the header
counts, one line per wait. Each line names the item, what it waits for and how
long it has stood.
<!-- /demo-step -->

[See it in the demo](https://demo.kew.dev/work?tour=needs-you&step=2)

### Run

Where the command is one the running [`kew drive`](../cli/drive.md) may carry
out (`kew dispatch N`, `kew epic continue|retry|unhold EPIC ITEM`), the block
and the dispatch launchpad offer **Run** beside **Copy**. The launchpad's Run and
Copy are quiet buttons: an item waiting to be dispatched is not waiting on you,
so with no needs-you block the Work screen draws no magenta. Run queues the command
as a request in the ledger, naming you as the operator. The drive picks it up
within seconds and runs it as you, and the line under the command reports what
it did: queued, running, done, failed with the command's last output, or refused
and why (the fleet is stopped, the drive is held, or the daily budget is spent).
With no drive running, Run says so and queues nothing. Copy still works for any
command. The console process itself never launches anything. See
[`kew run-request`](../cli/run-request.md), which queues the same request from a
terminal.

Each wait is said in **one sentence**, written by the server, and every surface
prints that sentence as it is: the Work screen, Monitor, an epic node's popover
and the selected-node rail. A merge decision reads "PR #N is open — the merge
decision is yours", and because no kew command makes a merge its move is a
link, **Open PR #N to merge ↗**, rather than a command. On the Work screen that
link is the block's **Do this** and its one magenta control. Every block shows
**Since**, how long the wait has stood, under its sentence. When the merge verify
failed at the pull request's current head, the sentence leads with the failure
instead — "Merge verify failed at `3670e43f`: 2 of main's tests fail on this PR —
the merge decision is yours" — and its first link, **See the failed merge
verify**, opens the item's Merge verify section; the PR link follows it. A verify
that passed, none linked, or one that failed at an older head leaves the
sentence as it was. On the Work screen every
item the header counts wears the magenta diamond beside its state and is listed
first. On the Epics list a **stalled** epic is magenta when a person is its next
move and amber otherwise; red is kept for failed.

The header's counter, Monitor's holds column, an epic's `needs you` legend entry
and the Work screen's needs-you filter all read that one set, so the four cannot
report different numbers for the same fleet. The two surfaces that show a
**number per epic** rather than a row — an epic page's legend, and every row of
the Epics list — read the same set counted per epic by the server, so the
header's figure is the sum of those buckets plus the entries that belong to no
drain (a spend approval on a loose issue, an orphan PR).

A wait with no person in it is deliberately absent:

- a node held on a **CI check** is waiting on a machine's clock, not on you;
- a **run kew is executing** is kew's own work in progress — it shows as
  working, and never as something you were asked for. That holds while the run
  is plausibly alive: a pending run row is the only fact saying kew is working
  on something, and nothing finalizes one whose host died, so a row older than
  twice your `agent.timeout_minutes` (never less than four hours) stops being
  evidence of a run and comes back to you as a *run that stopped reporting*;
- a pull request a **governed review round** is still reading is *in review*
  until the verdict lands. The merge decision becomes yours at that point, and
  not before — or sooner, if the round is plainly dead: the same bound applies,
  and a review run past it comes back to you as a *review round that stopped
  reporting* rather than waiting on a run nobody is running;
- a pull request **between** kew's review rounds is kew's turn too. Where the
  last round returned findings or asked for a full re-review, the next move is
  kew's own revision or its next round, and the item reads as working — *kew is
  revising*, or *kew re-reviews next*. Only the verdict that ends the cycle
  (ready, or ready with residuals filed) makes the merge your decision. A node
  the driver **held** instead — the review round cap, a base-integration
  conflict — stays yours, with the command that releases it;
- an attempt that **ended in failure** keeps the failure mark, the reason the
  driver recorded and the blast radius it names. `kew epic retry <epic> <node>`
  re-admits it; what it does not do is spend the colour that means somebody was
  asked on a node already drawn as broken.

An epic's bucket holds waits that are about the **epic** and not about one of
its nodes — a stopped driver has no node to mark — so the epic page names those
under its graph: every unit of the legend's number is either a diamond on the
DAG or a line saying why it is not.

A count that could not be taken reads as an em dash (`—`), never as `0`: a
source that failed is named rather than reported as a quiet fleet. A per-epic
bucket whose epic walk failed reads `?` on the legend and on the list row, for
the same reason — and the header's own count stays exact, because every entry
is still served, only unplaced. Only the set's own readers can cloud these
numbers: a glance elsewhere in the payload that failed (the ready queue timing
out, say) leaves every needs-you figure exactly as it was read.

**More** holds the pages that keep their own older layout:

| Destination | Group | What it does |
|---|---|---|
| **Health** | Factory | Can the factory work? Runtimes, agent images, per-lane containment, runners and credentials, trackers, daemon heartbeats, the caps in force, and the environment the server sees. |
| **Loops** | Factory | Active loop status and history. |
| **Runs** | History | Fleet history — every dispatched run. Each run opens its Flight Recorder page. |
| **Scorecard** | History | Cost-per-model table: merge rate, review rounds, and $/merged PR. |
| **Audit** | History | The hash-chained governance log and its verification status. |
| **Budget** | Settings | Edit spend caps live (CSRF-protected writes). Changes take effect immediately. |
| **Config** | Settings | Settings, with runtime overrides you can set and revert. |
| **Prompts** | Settings | Each role's prompt source, version and rendered preview, and its operator profile: propose, approve, decline and retire behind the approver gate. See [Prompts](#prompts). |

The menu draws them in three labelled groups: Factory says how the machine is,
History what it did, and Settings what it runs with.

`/` redirects to `/work`, keeping the `#token=` fragment the startup link carries
(and `?filter=approvals`, the console's old approvals view, becomes
`?filter=needs-you`). The old `/inbox` path lands on `/work?filter=needs-you`
once you have a session — see [Signing in](#signing-in) — and `/dashboard` on
`/work`. `/console`, the legacy judgment queue, is gone: every action it had
lives on Work, Monitor or Budget, and the path lands on `/work?filter=needs-you`.

### Holds and blocks

Monitor's left column lists what is standing still: an epic node the driver
could not advance, a spend cap an override is in force on, and every hold on an
epic node. A hold row names the epic and the item it is on — both links — what
held the node in words, how long it has stood, and the action that ends it.
The word beside the mark follows the row's state: **HELD** in amber over every
hold, whatever held it, and the person's move in magenta (**MERGE**,
**APPROVAL**) over a wait that is yours. Every magenta row carries its action
line: a command, a button, or the link the move is made through.

The action, by what held the node:

- a **budget** hold names the cap key to raise — `budget.per_issue_usd`,
  `[review].max_review_usd`, whichever its reason was written against;
- a **review**, **sandbox**, **dispatch** or **merge** hold names the
  `kew epic unhold <epic> <item>` that clears it, with the repair that comes
  first where there is one. The review round cap is the one hold that unhold
  alone recovers: its release restarts the review cycle in the same
  transaction, because the count the cap is read from only restarts with it;
- a **ci** hold — a node waiting on its own checks — offers nothing at all,
  because nothing a person does makes a check finish. The needs-you set leaves
  it out for the same reason.

The category is read from the hold's own reason, so a reason this version has
never heard of reads as **other** beside the reason the driver wrote, rather
than being filed under a category it was guessed into.

The one write this column has is an approval hold's **Approve** / **Deny**. An
epic hold carries no button: it clears with `kew epic unhold`, or on its own
when the driver finds the cap that wrote it admits the node again — and the row
names which.

Both approval actions are audited identically to CLI approval
(`source="web"`, policy-bound), so the web surface does not weaken the
[audit trail](../audit.md).

<!-- demo-step: needs-you/5 -->
**A wait with its command**

A magenta row in Holds and blocks is a person's move. This one carries the
command the server computed for it: press Copy and run it in your terminal.
<!-- /demo-step -->

[See it in the demo](https://demo.kew.dev/monitor?tour=needs-you&step=5)

<!-- demo-step: needs-you/4 -->
**A ready pull request**

This pull request passed kew's review, and its merge is a person's decision.
The row links to the pull request on the forge, where the merge is made.
<!-- /demo-step -->

[See it in the demo](https://demo.kew.dev/monitor?tour=needs-you&step=4)

## Signing in

`kew serve` prints a tokenized link at startup — `http://127.0.0.1:8765/#token=…` — when it runs in a terminal. Under a service manager or with output redirected, run `kew serve url` to get it. Open it in a browser: the token in the URL fragment is exchanged once for an HttpOnly session cookie (never sent to any log). After that the fragment is stripped and all write surfaces unlock.

If you open the cockpit without the token fragment, auth-gated pages show a prompt to open the link from your terminal (`kew serve --new-token` rotates it).

When a session lapses, any page sends you to `/inbox`, which shows that prompt instead of forwarding you on: every other URL is behind the header, whose shared poll would be refused in turn. Signed in, `/inbox` keeps redirecting to `/work?filter=needs-you` as before.

## Timestamps

Every time the console prints is shown in **your** time zone, 24-hour, and
carries the full UTC instant in its tooltip: hover a time to read
`2026-09-29 13:33:18 UTC`. The API answers in ISO-8601 UTC with an offset, and
the browser converts once — so the event stream's rows, the time axis above
them and an epic's `last tick` are three readings of the same clock, and the
tooltip is what two operators in two zones compare.

The CLI and the audit log are unaffected: both state UTC, which is what the
hash chain records.

## Runs

Table of every dispatched run. Shows an offline state when `kew serve` is unreachable.

## Budget

Editable spend caps. Two gauges across the top read today's and this week's spend against the cap in force, and pin the superseded cap when an override raised it — the enforced (agentic) figure only; interactive spend is stated beneath each gauge, because the governor does not gate it. Each cap row shows its effective value, its `kew.toml` base value and where the value in force comes from. **Override…** opens the editor under the row; **Save** writes the new value, and **Revert** removes the override and restores the `kew.toml` value.

Every write requires a CSRF token (`X-Kew-CSRF` header read from the `kew_csrf` cookie) and passes an Origin check, so the budget endpoint cannot be driven by cross-site requests.

## Prompts

`/prompts` lists every agent role, its run kinds and where its base prompt comes from: the pinned reviewer artifact (`pinned v9 · 3f2a1c0`, its version and short sha256), the template `[prompt] template` in `kew.toml` configures in its place (`kew.toml template`, shown only when one is set), a template packaged with kew (`built-in template`) or a prompt built in code (`built-in code`). Selecting a role renders its base prompt over a fixed sample item, never a real item's content, with every `## ` section folded except the first, and says how that base is changed. The source above a rendered prompt is the one read with those bytes, so a copy deployed after the list loaded shows as `deployed copy`. When the configuration refuses the launch (for example `[sandbox] enabled = false` without the skip), there is no prompt to render, and the pane says why instead. The selected role is kept in the URL (`?role=`).

For a role that takes an operator profile, the pane also shows its profile history and the active version, with who approved it, when, and a link to its audit event (`/audit?event=N`). The rendered preview marks the addendum with a cyan left rule from its heading to the end of its body. An approver can propose, approve, decline and retire a version here, with the same approver gate and generation check as `kew prompts`. The gate is the `kew serve` process's actor (`KEW_ACTOR`, else the OS user that started it), not each signed-in viewer, so every session on one server gets the same answer (see [Single-operator](#security-posture)). When that actor is not an approver, the pane hides Approve and Decline on a pending version but still shows the Propose box with its Propose button disabled and, with a version active, a disabled "Retire vN"; beside them the write area says once "Only an approver can change a profile.", or, while nobody can pass the gate, the server's own fix instead: "Set [approval].approvers in kew.toml before deciding prompt profiles." with no approvers configured, or "This server was started without a configuration, so no approver can be checked." on a server started without a config. `kew serve` reads `[approval].approvers` once, at start, so a change to the list takes effect only after you restart it. Approving a version older than the active one asks you to confirm first. A decision against a profile that changed since the pane loaded reads "This profile changed since you opened it. Reload to see the new version." and changes nothing.

When the active profile is refused (its body no longer matches the digest it was approved under, a line of its body opens a code fence or an HTML block, or a `[prompt] template` is set while a profile is active), every launch for the role refuses, so the pane leads with the server's reason, which names the fix. An approver, or a viewer whose fix is the server's own (no `[approval].approvers` configured, or a server started without a config), sees it in the magenta needs-you block; anyone else reads it as plain text that says an approver must clear it.

## Scorecard

Cost-per-model table for the last N days (default 30). Columns: model name, run count, merge rate (merged / opened), average review rounds, and **$/merged PR** — the cost-efficiency signal for choosing between models. A model the backend reports on the free or local channel carries a "local · unpriced" badge next to its name — the channel is the server's own reading, never a guess from the model id. Unpriced does not mean free.

## Health

`/health` answers one question — is the factory able to work? — and the header
links to it whenever something required is down.

Every reading has **three** states, not two: running; not running but required (a
red ×); or not enabled, which is grey and is never a failure. A watcher that is
off by design, a runtime this build has no backend for, and an image no lane
routes to are all readings, not faults. A fact that cannot be observed at all
reads **unknown** rather than being guessed in either direction.

The header badge counts only what matters: a missing image **only** if a lane
routes to it, and an unreachable tracker **only** if an epic uses it.

Sections, each with its own anchor:

| Section | What it says |
|---|---|
| Container runtimes | Which runtimes this build can drive, which one `[sandbox] runtime` selects, and whether it is on the host — beside the runtimes your last week of runs actually recorded. |
| Agent images | Each runner's image, where the ref comes from (`[sandbox] images.<runner>`, the legacy `image`, or unmapped), whether it is present locally, and whether any lane routes to it. |
| Posture by lane | Per dispatch lane and for the `[review]` lane: the containment dispatch itself resolves, the egress boundary, and whether a real credential is present or only a placeholder. |
| Runners and credentials | Binary on PATH, version, and each credential **name** the runner declares with whether it is set. Never a value. |
| Trackers | The GitHub reading the serve sweep cached (auth, scopes, remaining core quota) and whether Linear is configured. The page makes no GitHub call of its own. |
| Daemons | The serve sweep's cadence and heartbeat, the delegation watcher, the SSE fan-out, and the loop counts. |
| Epic drivers | Per-epic liveness with the absolute time of the last tick and its age; a heartbeat that was never written says "never". |
| Budgets and caps (`#budgets`) | Read-only: every spend cap, review round cap and capacity ceiling with the layer it came from — `env (NAME)`, `DB override`, `kew.toml` or `default`. |
| Environment (`#env`) | Variable **names** with what each is for and whether it is set, labelled "as seen by the serve process". |

Nothing on the page polls. The refresh control states how stale the data may be
("data is at most 12 s old") and takes a fresh reading on click — the host probes
behind it are cached server-side and refreshed by the serve sweep, so a ticking
"live" claim would be false.

No response from `/api/health` or `/api/health/env` contains the **value** of any
environment variable. `/api/config/effective` reports effective configuration, so
three of its values (the repo, the log directory and the worker count)
legitimately come from the `KEW_*` selectors — none of which is a credential.

`GET /api/readiness` serves the `kew doctor --json` document — the same check
ids, statuses and `fix` commands, from the same checks — plus `checked_at`. One
run answers every request for 60 seconds. The API reports each `fix`; it never
runs one.

## Authentication (in brief)

- A random **token** is printed at startup in the `#token=` URL fragment, only to a terminal (`kew serve url` prints it otherwise). It is never sent to the server or written to a log; the browser exchanges it once for an HttpOnly, `SameSite=Strict` **session cookie**.
- Writes additionally require a CSRF token (`X-Kew-CSRF`) and pass an **Origin** check; every request is validated against a **Host-header allowlist** (DNS-rebinding defense).
- **Loopback by default.** Binding a non-loopback address requires explicit `--expose` (or `[flight_recorder].expose = true`); when exposed, all API reads require a session too.

Full model — token storage, rotation, expose rules, actor honesty — in [Security › Approval inbox](../security.md#approval-inbox) and the [`kew serve` reference](../cli/serve.md#authentication).

## Security posture

- **Localhost-first.** Default bind is `127.0.0.1`; the read UI is local-only unless you opt in.
- **No TLS.** kew does not terminate TLS — front it with a reverse proxy (nginx, Caddy) for HTTPS, and enforce an auth proxy when exposing beyond the machine.
- **Single-operator.** Web approvals are attributed to the OS user running `kew serve` (the token proves possession, not distinct identity) — the same trust model as CLI approval on the host.

## Related

- [`kew serve`](../cli/serve.md) — command reference (flags, API routes, examples).
- [`kew stop`](../cli/stop.md) / [`kew approve`](../cli/approve.md) — CLI equivalents for inbox actions.
- [Security](../security.md) — security model for the inbox auth.
- [TUI](../tui/index.md) — in-terminal equivalent.
