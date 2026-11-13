# kew drive

Drive every active epic of the project from one supervised process.

`kew drive` ticks each active, non-archived epic on the project's tracker once
per `--interval`, round after round, until `kew drive stop`. It replaces a shell
loop per epic and the pause and stop files those loops read.

```text
Usage: kew drive [OPTIONS] [COMMAND] [ARGS]...

Options:
  --interval INTEGER       Seconds between rounds over the active epics (default 60).
  --max-parallel INTEGER   Override config.epic.max_parallel for every tick.
  --kew-binary PATH        The kew each tick runs on, resolved (through any symlink)
                           at tick start. Defaults to the kew running this command.
  --detach                 Run in the background; pid and log land beside the ledger.
  --epic-prefix TEXT       Label prefix identifying epic membership (default: "epic:")

Commands:
  pause    Stop ticking EPIC (or every epic) until `kew drive resume`.
  resume   Tick EPIC again; with no EPIC, lift every pause.
  stop     Stop the drive after its current tick; with EPIC, stop ticking
           that epic for the rest of this drive's run.
  status   Print the running drive's heartbeat: its stage, rotation and last
           tick per epic.
  hold     Admit no new work until `kew drive release`; every epic keeps
           ticking, so running work finishes.
  release  Lift `kew drive hold`.
  install    Install the drive as a launchd agent (macOS): it starts at
             login and restarts when it exits abnormally.
  uninstall  Remove the launchd agent `kew drive install` wrote.
```

```text
Usage: kew drive hold
```

```text
Usage: kew drive release
```

```text
Usage: kew drive pause [EPIC]
```

```text
Usage: kew drive resume [EPIC]
```

```text
Usage: kew drive stop [EPIC]
```

```text
Usage: kew drive install [OPTIONS]

Options:
  --interval INTEGER       Seconds between rounds over the active epics (default 60).
  --max-parallel INTEGER   Override config.epic.max_parallel for every tick.
  --kew-binary PATH        The kew the agent runs the drive on.
  --env-file PATH          A shell env file the agent sources before the drive
                           starts (where your credentials already live). Only its
                           path is written.
  --epic-prefix TEXT       Label prefix identifying epic membership (default: "epic:")
```

```text
Usage: kew drive uninstall
```

```text
Usage: kew drive status [OPTIONS]

Options:
  --json   Print one JSON document instead of text.
```

Each epic tick runs in a fresh `kew epic _tick` process on the kew that
`--kew-binary` resolves to when the tick starts, exactly as
[`kew epic run --kew-binary`](epic.md) does, and the review stage runs detached
from the tick, so no tick waits on a review or reads a half-moved checkout.

**One drive per project.** The drive's lock is a row in the project ledger
(`dispatch.db`) recording the holder's pid, host and heartbeat. A second
`kew drive` refuses and names the holder. A drive whose pid is
gone, or whose heartbeat is older than five minutes, is reclaimed by the next
`kew drive`, which says whose lock it took, so a crash never blocks a restart.
The heartbeat is refreshed on its own thread, so a long tick never lets it go
stale.

**Rotation.** Every round re-reads the roster: an epic planned while the drive
runs joins it, and an epic archived with `kew epic archive` leaves it. An epic
whose tick reports `complete` is archived by the drive (an attested
`epic_archived` event with source `drive`) and leaves the rotation. A stalled
epic stays in it — the drive keeps ticking it so an operator's repair is picked
up on the next round. A failed tick is reported and retried on the next round.

**Controls.** `pause`, `resume` and `stop` work from any shell while the drive
runs; each is a ledger write the drive reads before every tick, so a running
tick always finishes. A pause is kept in the ledger and outlives the drive. A
stop is addressed to the running drive alone: with no drive running,
`kew drive stop` refuses, and a stop never carries over to the next drive.

**Attention.** After each round the drive reads every epic's last journaled
tick for the attention events `kew epic watch` reports, and delivers each new
one once, naming its owner: a PR awaiting its merge decision is the
maintainer's, and every other kind (a hold, an exhausted budget, a held
publication, a sandbox refusal) is the operator's. On macOS each one raises a
desktop notification; everywhere it is a line in the drive's log, and the
console's Needs-you shows it for as long as it stands. Routine events (an epic
that finished, a merge, a retry) go to the log only. Which events were
delivered is a cursor in the ledger, so a restarted drive does not notify them
again, and a condition that persists across ticks (a verify host still down)
notifies once; if it clears and comes back, it notifies again. An epic whose
driver lock is held by a live process that stopped beating its heartbeat (a
`kew epic run` stopped with Ctrl-Z, say) raises one operator event naming that
pid, once per holder: kew never takes the lock from a live process, so its
ticks only lock out until that process is killed. `kew drive status` lists
each such epic, with the pid and the kill that frees it.

**Admission.** The drive admits new work only under a cap: [`[drive]
max_parallel`](../configuration.md#drive) (default 3), never above the fleet
cap, and lowered while this machine's one-minute load average is above a
`[drive.load]` threshold. It works
the cap out before every tick and hands it to that tick, and the tick counts
every run in flight against it — dispatches, reviews, remediations and ci-fix
runs alike, on every host, so a run placed on a remote instance holds a slot.
A full cap holds ready nodes as `admission-held: worker cap N consumed`. The
drive logs the cap each time it changes.

**Host load.** With [`[drive] max_load_per_cpu` or
`max_swap_used_pct`](../configuration.md#drive) set, the drive also holds
admission while the host is above either: ready nodes wait as
`admission-held: host load …`, running work continues, and the episode raises
one attention event. A queued run request that would spend stays queued while
the episode stands, with the host load as its reason (`kew run-request list`
and the console show it), and starts once admission resumes. The drive reads
the host again just before taking such a request, so one queued before the
first tick, or while the drive sleeps, never starts on an overload no tick has
seen; that read can open an episode or restart its count, but never counts
toward ending one. Admission resumes by itself after two consecutive tick
reads below both thresholds. While `kew drive hold` also stands, ready nodes
name the hold, since that is what the work waits on.

**A running local verify.** While a `kew verify --host local` runs — by hand,
from the merge queue or nightly — it holds a lease in the work root, and the
drive launches no new agent work beside it: no dispatch, continuation or
ci-fix, no review stage (reviewer, remediation or merge-main step), and no
console run request that would dispatch or continue. Each launch reads the
lease at its own spawn, so a verify that starts mid-tick also defers the
launches after it. A deferred node is neither held nor reserved — the detached
review stage starts no child for it — and it records one `launch_deferred`
audit event per verify, `deferred: verify running (<ref>, since <t>)`, however
many ticks it waits. A ready node waits as `admission-held: deferred: verify
running …`, a review-stage node with the deferral itself, in the tick's report
and in `kew epic status` (a node held on `ci_pending` keeps its hold line,
with the deferred ci-fix named after it); a deferred run request stays queued
with the same reason. The first tick after the verify ends launches what it
deferred. The lease is machine-wide, so the drive's own merge verify can find
another one running — a hand `kew verify`, a nightly, another project's drive.
The merge pass reads that as contention (`merge-verify-lease-busy`), not a
failure or a stall, and verifies the same head once the lease is free.

What the lease does not quiet: work already running is never stopped, a
hand-started `kew review` or `kew dispatch` is never blocked (it prints a
warning naming the running verify), the merge queue's mutation reports still
run, and
[`[drive] launches_during_verify`](../configuration.md#drive) (default `0`)
lets that many nodes launch per verify. The drive logs when a verify starts
and ends.

**Hold.** `kew drive hold` closes admission: every epic keeps ticking, so the
runs and reviews already in flight finish and are read, and a node already
cleared to merge still merges. No tick starts a new dispatch, review,
remediation or ci-fix run. Ready nodes
wait as `admission-held: admission closed by kew drive`. Like a pause, the hold
is kept in the ledger and outlives the drive. `kew drive release` lifts it, and
the next tick admits under the cap again.

**Detached.** `--detach` refuses while a drive runs, then starts the drive in
its own session and returns. Its output is appended to `drive.log`, and the
running drive writes its pid to `drive.pid`, both in the ledger's directory
(`[logging].dir`).

**Surviving a reboot.** A `--detach`ed drive dies with its host. On macOS,
`kew drive install` writes a per-user launchd agent,
`~/Library/LaunchAgents/dev.kew.drive.<project>.<hash>.plist`, and loads it.
At login the agent runs `kew drive` with the install's options, in the project
directory (`RunAtLoad`). launchd starts it again whenever it exits abnormally
(`KeepAlive` on `SuccessfulExit: false`, at most once a minute). A
`kew drive stop` exits cleanly, so a stopped drive stays stopped until the next
login or `kew drive install`. Output goes to `drive.log` beside the ledger, as
with `--detach`. Running `kew drive install` again replaces the agent. While
another drive holds the lock (one started by hand, say), the agent's drive
refuses and launchd retries it once a minute, so it takes over when that drive
ends.

No credential goes into the plist. Its environment is `PATH`, `KEW_PROJECT`
(this project) and the `KEW_*` config variables set when you ran
`kew drive install` (`KEW_LOG_DIR`, `KEW_MAX_BUDGET`, `KEW_REPO`, …; a relative
`KEW_LOG_DIR` is written resolved), so the agent's drive uses the same ledger,
lock and budget cap. Run `kew drive install` again after changing them. The
agent's `PATH` is not your shell's: it is the kew binary's directory, then
`/opt/homebrew/bin` and `/usr/local/bin` where they exist and only you or root
can write them, then `/usr/bin:/bin:/usr/sbin:/sbin`. Pass `--env-file` with
the shell env file your credentials already live in (lines like
`export GITHUB_TOKEN=…`). The agent sources that file each time it starts the
drive, and the plist names only its path. The file is run as shell code, as
you: it can extend `PATH` (`export PATH="$PATH:$HOME/.docker/bin"`) and set
`KEW_ACTOR` or anything else the drive reads, except what picks the project,
ledger and caps. After sourcing it, the agent sets `KEW_PROJECT` and every
`KEW_*` config variable back to its install value, and unsets those the install
did not set, so the file cannot point the agent at another project or ledger.

Install refuses a `KEW_REPO` that is not `owner/name` or an `https://` or
`ssh://` URL with no user, query or fragment in it (a token can ride in any of
them), and refuses to write a plist that would hold a credential
by any route (a URL with userinfo, or a GitHub, Anthropic, Linear or Slack
token shape in a path or setting). Keep credentials in the env file.
It also refuses, before changing anything, a program argument (such as an
`--epic-prefix`) that holds a line break or is only `}`: kew could not read it
back from `launchctl print` to check that a restored agent is the one it saved.
`kew drive uninstall` unloads the agent (launchd stops a drive it started) and
deletes the plist.

Reinstalling keeps the running agent until the new plist is written and
recorded. Before it replaces anything, install copies the previous plist to
`<label>.plist.previous` beside it (mode 0600) and notes the reinstall in
`.<label>.switch` beside it too (mode 0600). If launchd then refuses to load
the new one, or `launchctl bootstrap` runs past 30 seconds, install boots out
whatever launchd registered under the label (a failed bootstrap can still
register the new job), puts the previous plist back, loads it again and fails,
naming launchd's refusal. A bootstrap that succeeds counts only once the job
launchd then holds is the new plist's: the same program and arguments,
environment variables, working directory, stdout and stderr paths, and launch
policy (`RunAtLoad`, and `KeepAlive` in its exact form, so a job kept alive
unconditionally or only after a clean exit is not kew's restart-on-failure),
as `launchctl print` shows them. Settings `launchctl print` does not show,
such as `ThrottleInterval` and `ProcessType`, are covered by
`KEW_AGENT_DEFINITION_SHA256`, the hash of the whole definition, which install
writes into the agent's environment: the loaded job must carry the new plist's.
One that differs in any of them, or whose
`KEW_AGENT_DEFINITION_SHA256` or `KeepAlive` conditions launchd does not show, is rolled back the same way. Install makes the same
check against the previous plist before it counts the reinstall as undone. If the
previous plist does not load either, the `.previous` copy and the `.switch`
note stay, and the next `kew drive install` or `status` tries the restore
again. If that retry fails during an install, the install stops
without changing the `.previous` copy or the `.switch` note, and says the
previous agent is not yet restored: `kew drive status` keeps retrying the
restore until launchd loads it. A reinstall refuses to stop a recorded agent that launchd still holds
when its plist has been deleted, since nothing would be left to put back: run
`kew drive uninstall`, then install. Only launchd's own "could not find
service" answer counts as nothing loaded; if `launchctl print` fails any other
way, install stops before it writes or unloads anything and asks you to rerun
it once launchd answers. The same goes for the check after a restore: if
launchd took the previous plist but `launchctl print` cannot confirm what it
holds, install leaves the restored job registered, keeps the `.previous` copy
and the `.switch` note, and the next `kew drive install` or `status` settles
it. If the
install is killed partway, the next `kew drive install`, `status` or
`uninstall` finishes it: a new agent launchd already loaded from the plist in
place stays, and anything short of that is rolled back to the previous plist
and job. `kew drive status` says which happened. While an install is still
running, it holds a lock (`~/Library/LaunchAgents/.<label>.lock`, mode 0600).
The lock follows the label, which depends only on the project, so it holds
even between installs that use different ledgers (`KEW_LOG_DIR`). During
that time a second `install` or an `uninstall` is refused, and `status`
reports the agent without settling anything. `uninstall` takes the lock even
when `~/Library/LaunchAgents` has been removed, recreating the directory, and
kew never runs a `launchctl bootout` or `bootstrap` without it. Each
`launchctl` that install or uninstall runs is started by a small kew helper process that holds that lock and waits
for it, so a `launchctl` still blocked in launchd when its install is killed
keeps the lock held until it exits, whatever it does with the descriptors it
inherits, and nothing settles the
reinstall under it. Only an install that died is ever rolled back, and only
once its last `launchctl` has exited. The helper runs in a session of its
own, so Ctrl-C does not reach it; an interrupted install waits for its
`launchctl` to exit before it rolls back. A second Ctrl-C during that wait
ends the install without rolling back: it says `launchctl` is still running,
and the next `kew drive install`, `status` or `uninstall` settles the
reinstall once it has exited.

If an install is killed after it writes the plist but before the ledger
records it, `kew drive status` still finds kew's plist at its path. It reports
that plist as `not recorded in the ledger`, and `kew drive uninstall` removes
it. Kew claims an unrecorded plist only when all of it is the agent kew writes
for this project: its label and program, its working directory and
`KEW_PROJECT`, its launch policy, one log for output and errors, and kew's
mark (`KEW_DRIVE_AGENT=1` in its environment). Uninstall checks it again just
before deleting it.

The agent runs unattended with your rights, so `kew drive install` refuses,
naming the file and the fix, anything another account could change:

- an env file that is a symlink, not a regular file, not owned by you, or
  group- or world-writable (`chmod 600` it), or that sits in a directory that
  is not yours or is group- or world-writable. The agent checks the file again
  before every start; one that fails is not sourced, the drive does not start,
  and launchd retries once a minute until it is fixed;
- a relative `--kew-binary`, or a kew binary (resolved through any symlink;
  the plist names the real path) or its directory that someone other than you
  or root can write;
- a symlinked `~/Library/LaunchAgents`, plist or `drive.log`. The plist is
  written to a fresh file beside it (mode 0644) and renamed into place; a
  ledger directory kew creates is mode 0700;
- a ledger directory or existing `drive.log` that is not owned by you or is
  group- or world-writable (`chmod go-w` the directory, `chmod 600` the log).
  Install creates `drive.log` (mode 0600) before it writes the plist;
- a plist at the agent's path that is not the one kew writes. Install neither
  stops nor replaces it; move it aside first.

`kew drive uninstall` likewise removes only what install wrote: the recorded
label must be this project's, the plist must be at
`~/Library/LaunchAgents/<label>.plist`, and the plist must be kew's. If launchd
still holds the agent after `launchctl bootout`, uninstall fails and keeps the
record.

A drive can start over a lock whose holder did not stop: the host rebooted
under it, it crashed, or it went silent. That drive resumes from the ledger as
any start does (rotation, pauses, holds, cap). It also records one
[`drive_restarted`](../audit.md#drive-restarts) event naming the previous pid
and why it ended: `gone after reboot`, `exited` or `stalled`. The event is read
off the lock row the previous drive left in the ledger. The new drive also
writes it into its own lock row as it takes the lock, and clears it there once
the event is recorded. If that drive is killed in between, the next drive
records the restart, once, as well as its own. Its write is required:
if it fails, the drive puts the lock row back as it found it and exits abnormally, so
the next start (launchd retries a minute later) reclaims the lock again and
records the restart. Run one kew version per ledger: an older kew that takes
the lock over drops the restart records a newer one left in the lock row, so
a restart still owed at that moment goes unrecorded.

`kew drive status` reports an installed agent on its own line: `loaded`,
`not loaded` (launchd will not start the drive at the next exit or login),
`disabled`, or `missing` when the plist the install wrote is gone. `--json`
carries the same under `agent` (`null` when none is installed).

**Heartbeat.** The running drive writes a heartbeat to the ledger every 30
seconds and whenever its stage changes: the time, its stage (`ticking` an epic,
`sleeping` between rounds, `stopped`), the epics in its rotation (paused and
stopped ones are left out) and, per epic, when its last tick started and
finished and how it exited. `kew drive status` prints it, and says whether the
drive is running, gone (stalled) or not running at all.

A heartbeat whose ledger open is refused (the open names why, for example a
ledger schema newer than this kew) is not retried forever: after 3 refusals in
a row the heartbeat gives up. When the ledger's schema is newer than this
drive's, or the drive's own kew has changed, the drive ends through the restart
below; otherwise it reports itself lost and exits 2. A drive that exits on its own
like this records why, and `kew drive status` prints that record under the
last heartbeat: the pid, the exit code, how long ago and the reason, for
example `kew changed under the drive: restarting; …` (`--json`: `exit`, with
`code`, `reason`, `pid` and `at`).

**Restarting on new code.** At start the drive records which kew it runs: the
kew version and the git head of the checkout its package lives in, or a
content hash of the installed package when it is not a checkout's source tree
(an install under `site-packages` never follows a checkout's head). Before
every launch (each epic tick, each run-request pass, each merge pass) it reads
that identity again, and it reads the ledger's schema version on the ledger
connection it already holds. When the identity has changed, the drive:

1. launches nothing new, including a tick admitted just before the change;
2. raises one attention event, `kew changed under the drive: restarting`, or
   `ledger schema N is newer than this drive's M: restarting` when a
   `--kew-binary` child also migrated the ledger past this drive's
   `CURRENT_SCHEMA_VERSION`;
3. waits for the running tick child to finish (ticks run one at a time, so it
   restarts between ticks), and for the run-request child it started, if one
   runs (a console-queued `kew review`, `kew dispatch`, …): it checks every
   five seconds and execs only once that child has exited and its request is
   recorded as finished;
4. re-execs itself with the same interpreter and argv. The new process keeps
   the pid and takes back the same drive lock and heartbeat, so the epics are
   never left without a drive.

When the ledger's schema is newer than this drive's but its own identity has
not changed (a pinned drive whose `--kew-binary` children run a newer kew), a
re-exec would load the same code and refuse the same ledger. The drive then
launches nothing new, raises `ledger schema N is newer than this drive's M and
the drive's own kew has not changed: upgrade the kew the drive runs`, lets the
running tick finish, records that reason and exits 2. Upgrade the kew the
drive runs, then start it again.

A refusal for a newer ledger schema — this one, or the same refusal at the
ledger open of a drive that starts on a too-old kew — is also written to
`drive-refusal.json` beside the ledger, naming the reason, both schema numbers
and the drive's code identity: the refused drive cannot write to the ledger it
refuses. The first refusal exits 2 as above. A start that finds the same
refusal recorded within the last 10 minutes, on the same code identity, prints
it and exits 0, so launchd leaves the drive down instead of restarting it every
ten seconds. `kew drive status` prints that refusal and its fix: upgrade the
kew the drive runs, then `kew drive install`, or run `kew drive` by hand. A
drive whose kew changed (a new code identity) refuses on exit 2 again; a start
the ledger does not refuse, and `kew drive install`, clear the record.

`kew drive status` shows a recorded refusal only while it still applies: the
ledger's schema is still newer than the refused drive's, and no drive is
running on the ledger. When the ledger is too new for the kew running
`status` itself, `--json` prints a document whose `state` is `refused`, naming
the refusal.

If the re-exec fails, the drive records why, `kew drive status` names it, and
it exits 2. The launchd agent `kew drive install` writes restarts only an
abnormal exit (`KeepAlive` `{"SuccessfulExit": false}`), so it starts the drive
again on the new code.

**Reservations.** Every epic node a tick dispatches holds a fleet slot under a
reservation until its run ends. Before every round the drive sweeps the
reservations of every epic in the ledger, not only the epics it ticks — one
under an archived, paused or never-ticked epic is swept too — and releases,
with a warning naming the epic, item, run and reservation time:

- a reservation with no run row more than ten minutes (the startup grace)
  after it was written: the dispatch child never started, and its run reads
  FAILED as `dispatch_child_died`;
- a reservation whose run has ended;
- a reservation with no run row yet whose tracker item is closed.

A reservation whose run is still `pending` is left to its own epic's tick,
which releases it only once the run's owner is proved dead. So a pending run
whose owner died under an epic nobody ticks any more (archived, paused, or off
the rotation) still holds its slot: the sweep never releases it. Run one tick
of that epic by hand, `kew epic advance <epic>`: its reconciler probes the
run's owner and, once the owner is proved dead, finalizes the run and releases
the slot. An item whose tracker state cannot be read keeps its reservation.

The drive holds no epic's lock while it sweeps, so it re-checks before every
write: it settles a reservation only while that same reservation (item and
run) still holds the slot. A closed item's release re-reads the run rows in
the same transaction that deletes, so a dispatch child that started during
the tracker read keeps its reservation. A refusal hold is lifted only when it
was written before the sweep's check and no newer reservation of the item
exists. When one reservation cannot be settled, the drive logs it by epic,
item and run and goes on to the rest. The next round tries it again, and a
failing sweep never stops the drive.

`kew drive status` lists every reservation holding a slot, whether or not a
drive is running. For each one it shows the epic, item, run, age (`45s`,
`12m`, `2h`, `73d`), the run's owner pid with what this machine can tell of
that owner (`alive`, `dead` or `unprobeable`; a run placed on another machine
is always `unprobeable`), and what the sweep makes of the reservation. `--json`
carries the same fields under `reservations` (`age_s`, `owner_pid`, `owner`,
`verdict`).

The console reads the same heartbeat. While it is fresh, every epic in the
rotation reads `live` if its own tick is inside its cadence and `busy`
otherwise — waiting its turn, or with its detached review still running — never
"driver gone". The drive is gone only once its heartbeat is older than three of
its intervals (90 seconds); then every epic in its rotation reads gone too,
unless its own last tick is still inside its cadence. A
heartbeat left by a drive that stopped or lost its lock is ignored. Archived
and finished epics are never counted in the console's header, whatever the
heartbeat says; `/api/health` carries the drive's own reading under `drive`.

**Run requests.** The drive also runs what the console's **Run** button and
[`kew run-request queue`](run-request.md) queue: before every round, after
every tick and on every step of its sleep it takes the oldest queued request
and runs it as a fresh kew child, as the operator who queued it, one request at
a time. A Run pressed while the drive sleeps starts within five seconds; one
pressed during a tick starts when the tick ends. A request is refused, with the
reason recorded on it, when it has waited in the queue for more than an hour,
when its operator is no longer in `[approval].approvers`, or while the fleet is
stopped, the drive is held by `kew drive hold` or the daily budget is spent.
While the drive holds admission for host load, a request that would spend
waits in the queue with that reason instead, and starts once the hold ends
(the hour still applies). A `kew dispatch` or `kew epic continue` request also
waits while a local merge verify runs, and starts once it ends; that wait does
not count toward the hour. A request still
running when the drive stops keeps running; the next drive waits for it and
then marks it `unknown` — never run again, because nobody can say whether it
took effect. If the drive stopped before recording the child's pid, the next
drive can't check that child, so it waits an hour from the claim before marking
the request `unknown`. A drive that restarts on new code takes its lock back
under the same token; a request that token still shows `running` holds the
queue the same way, until its child exits. The drive then records the child's
exit code, or marks the request `unknown` when it cannot get that code.

A `kew dispatch` Run from the console's launchpad may carry a model picked for
that one dispatch. The model is a field of the request, never an option in its
command: it must be a catalog id, neither retired nor unroutable, on the runner
every tier of the dispatch resolves to, and it is checked when the request is
queued and again when the drive takes it (a route that moved in between refuses
the request with the reason). The drive runs it as `kew dispatch <N> --model
<id>`, and the dispatch's `routing_decision` audit row records the override
with its source, `console launchpad`. A `--model` typed in a terminal is
recorded the same way, with the source `kew dispatch --model`.

## Merging {#merging}

With a signed [standing merge rule](../configuration.md#drive-merge)
(`[drive.merge]`), the drive also merges, after each round's ticks, the READY
PRs of the epics that round ticked that the rule covers. A PR outside the rule
— a gate-class or protected path, or a UI change with no browser check of its
head on record — is left for a person and the reason recorded once per head.

Merges run one at a time, under the same repo-wide merge-queue lock as
[`kew epic merge`](epic.md), held from the verification until the merge lands,
so no two PRs ever verify at once. For each PR the drive fetches main and the PR
head, merges main into the head in a fresh worktree, and runs
[`kew verify`](verify.md) over that commit on `[verify] host`. If main moved
while the verification ran, the merge is refused and the next round retries it.
Every gate `kew epic merge` proves still holds — membership, no hold, live work,
the READY verdict bound to the head, the open PR at that head, both CI gates,
re-proven after the verification — and the acceptance contract id and version
must still match the contract that passed. A contract amendment stops the merge
and requires another verification. The merge is a squash carrying the PR's
title and body, bound to the verified head with `--match-head-commit`.

With `[drive.merge] gate = "verify"` the two CI gates are not read: the passing
`kew verify` of the candidate is the evidence the tests ran, for when GitHub
Actions cannot run. Every other gate still holds. Since the verify then is the
only proof, the PR must also be read targeting the configured repository and
base branch, both before the verify and again after it: a PR retargeted to
another branch, or one whose base cannot be read, is refused.

After the standing rule, every round also takes one pass of the
[merge queue](merge.md): the PRs operators queued with `kew merge queue add`,
verified and merged in the order they were queued.

The diff-scoped mutation report is not on that path: the gating verify reports
`mutation=deferred`, and each merge — the standing rule's or the merge
queue's — queues a report job for the commit it verified. The queue runs at the end of a merge pass, after every merge the pass
attempted, on `[verify] host`; while the merge queue or the host is held it
waits for a later pass. A report runs holding the merge queue, so a gating
verify that arrives meanwhile waits for the next round instead of failing on a
busy host. The queue is recorded (`mutation_report_queued`), so a drive that
restarts before a report ran still runs it. Each report is recorded as
`mutation_reported` (or `mutation_report_dropped` when it cannot run) and
posted to the merged PR's thread
([Diff-scoped mutation report](verify.md#diff-scoped-mutation-report)).

Before it verifies anything, each merge pass probes `[verify] host`. When the
host cannot be reached — an expired cloud login, an instance that is gone —
the drive pauses merges and only merges: the epics keep dispatching and
reviewing. The first failed probe sends one notification (to `[alerting]
webhook_url`, and in the drive log) naming the fix — for gcp, `gcloud auth
login` — and the first probe that answers again resumes merges with no
operator action. The suite never runs on the operator machine instead unless
`[drive.merge] local_fallback` is on.

Exit codes follow the [exit-code scheme](exit-codes.md).

**Example:**

```bash
kew drive --detach --kew-binary /opt/kew/current/bin/kew
kew drive install --kew-binary /opt/kew/current/bin/kew --env-file ~/.config/kew/env
kew drive pause auth-rework     # the other epics keep ticking
kew drive resume auth-rework
kew drive status                # stage, rotation, last tick per epic, reservations
kew drive stop                  # exits after the current tick
```
