# kew verify

Run the merge verification on a host and record what it returned.

```text
Usage: kew verify [OPTIONS] [REF]
       kew verify nightly [install|uninstall] [OPTIONS]
       kew verify main-health [clear SHA --issue N --reason TEXT]
       kew verify report [--since 30d]

Arguments:
  [REF]       The commit to verify. Defaults to HEAD. `nightly`,
              `main-health` and `report` are subcommands instead.
  [WORDS]...  After `nightly`: install or uninstall. After `main-health`:
              clear SHA.

Options:
  --host TEXT       Where the job runs: local or gcp. Defaults to verify.host.
  --shards INTEGER  Run the parallel pytest phase as N jobs on N hosts.
                    [default: 1]
  --pr-body FILE    The PR description; its Behavior changes section may
                    excuse a held-out base test failure by node id.
  --item TEXT       The work item the candidate implements, whose acceptance
                    contract the verification runs. Defaults to the item whose
                    run opened the branch REF names.
  --epic TEXT       The epic the candidate's node belongs to, when the ledger
                    cannot tell.
  --pr INTEGER      The pull request whose head REF tests. The verify claims
                    that head while it runs, so the drive pushes no
                    remediation onto it; --item claims its node's PR the same
                    way.
  --force-claim     Claim the PR head even while a remediation, resolve,
                    ci-fix or reviewer of its node is running; needs
                    --force-claim-reason.
  --force-claim-reason TEXT
                    Why the claim is forced; attested with your login, or
                    KEW_ACTOR.
  --full            Run the full suite even when [verify] suite is "tiered",
                    and the full vitest and ui-check even when [verify]
                    frontend is "scoped". The defaults already run them in
                    full.
  --explain-selection
                    Run nothing: print the test files REF's change selects,
                    with the import chain behind each, or the rule that forces
                    the full suite.
  --selection-report
                    Run nothing: replay the last merged commits on main and
                    print the median and p75 selected share of tests, three
                    ways.
  --frontend-report
                    Run nothing: replay the last frontend commits on main and
                    print the median and p75 selected share of ui-check
                    modules and vitest files under [verify] frontend =
                    "scoped".
  --last INTEGER    How many commits --selection-report or --frontend-report
                    replays.  [default: 50]
  --issue INTEGER   main-health clear: the issue tracking it.
  --reason TEXT     main-health clear: why main reads green.
  --kew-binary PATH nightly install: the kew the agent runs.
  --env-file PATH   nightly install: a shell env file the agent sources first.
  --since TEXT      report: how far back to read verifies, like 30d, 12h or
                    2w.  [default: 30d]
```

`kew verify` is the merge gate as one command: it bundles the candidate, runs
kew's own verification job on a [host](../configuration.md#hosts), prints what
the host returned, and records the verification on the audit ledger. `local` is
the default and needs no cloud account; `gcp` ships the candidate to the
configured Compute Engine instance through `gcloud`. A bare `kew verify <ref>`
never touches GitHub. A verify bound to a PR (`--pr`, `--item`'s PR, or the
drive's merge pass) may file [`flaky` issues](#report) once it is recorded,
but a filing that cannot run is logged and recorded and never fails the
verify. So it asks for `git` and a shell and not for the GitHub CLI.

**Example:**

```bash
kew verify                      # HEAD, on the host [verify] host names
kew verify --host gcp           # HEAD, on the configured gcp instance
kew verify --host local v1.4.0  # a tag, on this machine
kew verify --host gcp --shards 3  # the parallel phase across three instances
kew verify --item 42 pr-42-head # a fetched PR head, held to item 42's contract
kew verify nightly              # a full verify of origin/main, bisected if red
kew verify main-health          # main's health, read off the audit ledger
kew verify report --since 30d   # what every red verify was, phase by phase
kew verify --pr 3198 merged     # main with PR 3198 merged in; claims its head
```

`kew verify --host gcp` is experimental, sharded with `--shards` or not: it
runs on a host `kew hosts provision gcp` built, but may still change.

## The verify lease {#lease}

A local verify (`--host local`) holds an exclusive lock on `verify.lease` in
kew's work root for its whole job, held-out phase included, with a small
record beside it: its pid, a start token, the ref and when it started. While
it is held, [`kew drive`](drive.md) launches no new
agent work (see
[`[drive] launches_during_verify`](../configuration.md#drive) for the
exceptions it allows), and a hand-started `kew review` or `kew dispatch` warns
that it runs beside the verify. The lock is released when the verify passes,
fails or times out, and the kernel drops it if the process dies, so a lease
never outlives its verify.

A second local verify never waits for the first. It is refused before any
job, naming the running verify's pid, ref and start time, and records no
verify event: a hand `kew verify` fails as a verification that could not run,
and the drive's merge pass reads it as contention (`merge-verify-lease-busy`),
like a busy merge-queue lock: no hold, nothing remembered as failed, no stall,
and the same head is verified on the first round after the lease is free. A
gcp verify takes no lease.

## Holding the PR head

A verify of a PR head takes minutes to hours. If the drive pushed a
remediation onto that head meanwhile, the merge the verify was for would fail
on a head mismatch. So `kew verify --pr P` claims PR `P` at the head it is
testing for as long as it runs, and the drive pushes nothing onto a claimed
head — no remediation, no merge of main into it, no ci-fix: the NEEDS_CHANGES
findings stay recorded, the drive starts no review-stage child for the node,
`kew epic status` reports it as `merge_claimed — … is verifying PR #P …`, and
the first tick after the claim ends takes the step. A reviewer round pushes
nothing, so a claimed head is still reviewed. `--item` without `--pr` claims
the PR the drive's newest snapshot links to that item, and says so when it
links none.

A remediation or conflict resolution that was already running when the claim
was taken does not push under it either: its finished work waits, intact, and
is published as soon as the claim ends — nothing is redone and nobody has to
release it. A ci-fix runs inside the drive's tick, so it does not wait: it
publishes nothing, its fix commit stays in its preserved worktree, and the
drive fixes the node again once the claim ends. To keep any of that from
happening at all, `kew verify` refuses to claim a head while a reviewer,
remediation or resolve of the PR's node holds the node's review slot, or a
ci-fix of it is running, naming the run. Wait for the run to finish, or pass
`--force-claim` with a `--force-claim-reason` to claim the head anyway; once
the claim is taken, the audit trail records it as `merge_claim_forced` with
who forced it, why, and the runs it overrode.

The heads claimed are REF's commit and, when REF is a merge, both its
parents, because the PR head is one parent of an ad hoc merge (main checked
out, the PR merged in) and of the drive's own merge of main into the head.
The `claiming PR #P at …` line names the PR head the drive last read when it
is one of them, and both parents of a merge it cannot place.

A claim lives 15 minutes from its last renewal, and the running verify
renews it every 5, so a verify of any length keeps it. It is released when the
verify ends — passed, failed, timed out, or stopped by `SIGTERM` or Ctrl-C. A
verify killed outright stops renewing, and its claim lapses within 15 minutes;
the drive never waits out the verify's full ceiling. A second verify of a head
already claimed is refused, naming who holds it and until when.

A claim can lapse under a verify that is still running — a machine asleep
past those 15 minutes, say. The verify still reports its verdict with its own
exit code, but warns that the claim lapsed mid-verify and the drive may have
pushed onto the head since, and its `verify_completed` event records the
claim with `"lapsed": true`. Check the PR head before merging on it.
[`kew merge claim`](merge.md#holding-a-pr-head-by-hand) takes, renews and
releases the same claim for a verify run some other way.

## What runs on the host

The verification is kew's own gate, not a second copy of it, run as two jobs
in one session on the host. The cheap gates run first, and kew judges them
before it starts the suite.

The **gate job**:

1. `umask 022`, so the files one job writes stay readable to the next.
2. `bash ../scripts/scan-agent-image.sh --check-tools` — the pinned-tool
   preflight. It is the image gate's own version check in a mode that builds
   nothing and downloads nothing, so a host whose `trivy` is not the pinned one
   (`kew/src/kew/hosts/provision/trivy.pin`) fails the verification in seconds
   instead of at the gate's **last** step, after the whole suite has already
   passed. A host kew provisioned installs exactly that version.
3. The **virtualenv probe**: `uv sync`, then a check that every module the
   suite needs imports, that every file each installed distribution's `RECORD`
   lists under `site-packages` is present, and a digest of the `site-packages`
   file list. The probe runs by the virtualenv's own interpreter, never through
   `uv run`, which would quietly repair what it is looking for. See
   [A broken environment](#a-broken-environment).
4. Each pinned `[sandbox] images` image, built **when it is missing**. A host
   that already has them rebuilds nothing, and a host without `docker` builds
   nothing at all rather than failing the gate over an absent tool.
5. `KEW_VERIFY_STAGE=gates bash scripts/verify.sh`. This is the first half of
   the local gate that mirrors PR-gating CI, run from `kew/`: the static gates
   (ruff through gitleaks) and the sandbox containment test, and nothing after
   them.
6. The **held-out phase**: the base branch's own `kew/tests`, which no PR can
   have edited, run against the candidate's `src` with the suite's two
   selections. See [Held-out base tests](#held-out-base-tests).

kew reads the gate job's held-out section against the PR body before anything
else runs. A red static gate, a red containment test, or a held-out failure no
**Behavior changes** line declares ends the verification there. The held-out
phase takes seconds, so an undeclared change fails in minutes, not after the
30 to 60 minute suite. No pytest phase starts. The result's error names the
phase it stopped in (`stopped in phase held-out: no pytest phase was started`),
and the log shows one `kew-verify: phase …` line per phase that started.

The **suite job**, in the same checkout, only behind a clean gate job:

1. The virtualenv probe again, after a fresh `uv sync`.
2. `KEW_VERIFY_STAGE=suite bash scripts/verify.sh`, which is the rest of the
   gate: the two pytest phases and the coverage floor, the agent-image scan,
   the docs gate, and the frontend and www gates. The private Switchyard image layer inside it stays opt-in
   through `KEW_SWITCHYARD_IMAGE`, and kew forwards that opt-in **only** to a
   host that can build from the private input, and only together with
   `KEW_SWITCHYARD_SOURCE_ARCHIVE`, the path at which that host finds the
   input. The archive itself never leaves the operator machine, so a remote
   host skips that one layer and gates every other image exactly as CI does.
   An opt-in with no archive path is not an opt-in the gate can honour, so
   the local host skips that layer too rather than failing the verification
   over an input this machine never named. With `[verify] suite = "tiered"`,
   its two pytest phases may run the change's selection instead of the whole
   suite: see [The tiered suite](#tiered).
3. `npm run ui-check` — Playwright screenshots and console checks — when, and
   only when, `frontend/` differs between the base and the candidate. It is
   told where a provisioned host keeps its browser
   (`PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers`, unless the job's shell
   already sets one) and that a browser is required: with no Chromium found,
   the verification **fails** rather than printing `skipped: no Chromium`.
   With `[verify] frontend = "scoped"`, it runs only the modules the change
   owns, after `vitest related` on the changed files: see
   [The scoped frontend](#frontend).

The phase order is kew's, not `verify.sh`'s. `KEW_VERIFY_STAGE` takes only
the exact values `gates` and `suite`; a typo runs the whole gate. CI and a
hand-run `verify.sh` set nothing and still run every gate in their usual order,
and a clean candidate runs every gate it ran before the split. `verify.sh`
reads `KEW_VERIFY_STAGE` and `KEW_VERIFY_SUITE` once and unsets them, so
nothing it starts sees them: a test that runs `verify.sh` itself runs every
gate, even inside the suite job.

The jobs are kew's own gate for this repository's layout — `kew/scripts/verify.sh`
and `frontend/` — not a project-defined command: a merge verify that ran
something other than the gate CI runs would attest nothing.

## A broken environment

The probe runs again right before `scripts/verify.sh`, and once more if the gate
fails. When it finds a module that does not import, a file missing from
`site-packages`, or a `site-packages` that changed since `uv sync`, it prints
one `KEW_VERIFY_ENV broken: …` line naming what it found, and the job stops
there. The verification is recorded as `failed` with the error category
`verify_host_environment_broken` and the probe's finding as its reason — one
environment error, not the hundreds of test failures a virtualenv losing files
mid-suite would otherwise produce. A checkout with no `pyproject.toml` in
`kew/` has no virtualenv to probe, so it syncs and probes nothing.

## A job that outruns its ceiling

On the `local` host the job runs in its **own session**, from a workspace under
[`[paths] work_root`](../configuration.md#paths), and its `TMPDIR` is that
workspace's `tmp/`, so the held-out repository and any mutation tree the job
makes live inside the workspace too. When the job passes
`[verify] timeout_seconds`, kew stops the **whole process group**: `SIGTERM`,
then `SIGKILL` after a 5-second grace, then waits until no process is left in
the group. Only then is the workspace removed, so nothing the job started (a
background process, a test worker) keeps running in a checkout that is being
deleted, and every tree under the job's `TMPDIR` goes with it. If the group
outlives `SIGKILL` for 30 seconds, the workspace is left in place for the
work-root sweep rather than deleted under a live process. The verification is
recorded as incomplete, as any job with no exit code is.

## Held-out base tests

Every other phase runs the candidate's own tests, which the change under
review can edit. The base branch's tests were written before it, so the job
also runs **those** against the candidate's `src`:

- A temporary clone of the checkout, **outside** it, is put at the candidate
  and its `kew/tests` replaced wholesale by the base's. A base test the PR
  deleted comes back and runs; a test only the PR added is not there.
- Only the base versions of the `kew/tests/**/test_*.py` files the candidate
  modified, renamed or deleted are run: a test file it left alone is the same
  on both sides, and the suite job runs it against the same `src`.
  The whole base tree runs instead when the candidate changed anything the
  tests share — a `conftest.py`, `tests/support/`, any other file under
  `kew/tests`, or the `[tool.pytest]` configuration in `kew/pyproject.toml` —
  and when another Python file under the base's `kew/tests` names a changed
  test file's module, since it may import helpers from it.
  A candidate that changed no test file records the phase as `skipped` with
  the reason `no test file changed`; the verify event's `held_out_selection`
  records the mode (`full`, `selected` or `none`) and how many files it
  selected. An acceptance contract is always run.
- It runs `pytest -n auto -m "not serial"` and `pytest -m serial`, the main
  phase's two selections. Sharded, each shard runs its own `--shard=K/N` slice
  and the single-run job runs the serial selection.
- Every held-out run, and the contract's own run, takes the **base's** pytest
  configuration, never the head's. kew reads the file the base's `kew/` keeps
  it in, in pytest's own order (`pytest.toml`, `pytest.ini`, `pyproject.toml`,
  `tox.ini`, `setup.cfg`), from the operator's checkout. It writes that file
  into the held-out project directory under a name of its own and passes it
  with `-c`, with `--rootdir` set to the project directory. So nothing in the
  head's configuration reaches a run of the base's tests, in whatever file it
  sits: not `addopts` (`--deselect`, `-k`, `-p`), `python_files`,
  `python_classes`, `python_functions`, `testpaths`, `norecursedirs`,
  `pythonpath` or `filterwarnings`. The base's `pythonpath` still points at
  the head's `src`, which is the code under test.
- Each run must collect what the base collects. Before each run, kew collects
  the same selection with `--collect-only` in a clone of the base itself, on
  the base's own `src` and environment, so the expected list is fixed before
  the head's code runs. A test the base collects and the run never collects
  is a failure of that test, named by node id as never collected. This
  covers a run that collected nothing at all. A test in a file whose
  collection failed is named as well as that file's failure. Such a failure
  is excused by **Behavior changes** like any other base test failure, so a
  head that renames a parametrization can declare it. One line naming a file
  that failed to collect declares that file's failure and every test of it
  the run never collected: an import the head broke is one cause, so it
  takes one line, not one per test. It never declares a test of that file
  that was collected and failed, and an uncollected contract test is never
  excused.
- Before the runs, a head that changed `kew/pyproject.toml` or `kew/uv.lock`
  has its lock checked against its `kew/pyproject.toml`, from the two files
  alone: no resolver and no network, so a host that cannot reach an index
  still sees it. The lock is stale when it has no entry for the project, or
  that entry records no dependency of a name the project declares
  (`[project]` dependencies and extras, or `[dependency-groups]`; names
  compared as PEP 503 normalizes them). `--frozen` installs a stale lock as
  it is, so a dependency the head added is missing and every base test file
  that imports it fails to collect. When the lock is stale and the phase
  fails, its error starts with ``the head's uv.lock is stale: run `uv lock`
  and commit it``, before the failures it names. The check never fails the
  phase itself, and one that cannot read either file adds nothing.
- Every held-out, base-collection and contract run is
  `uv run --frozen --no-config --python <interpreter>`. `--frozen` installs
  exactly what `uv.lock` says and never re-locks, so a `[tool.uv.sources]`
  entry the head adds to `pyproject.toml` cannot swap a pinned distribution
  while its lock stays the base's. `--no-config` reads no `uv.toml` or
  `[tool.uv]` setting of the head's. The interpreter is the one
  `uv python find` answers, outside any project and with no configuration,
  for `==X.Y.Z`: the exact version of the interpreter the gate job's
  `uv sync` built the checkout environment on, so the held-out runs test on
  the Python the suite runs on. That sync reads the checkout's
  `.python-version` and `requires-python`, the head's included, so the head
  can steer which X.Y.Z it is. What reaches the held-out runs is only that
  version number, read from the environment's own interpreter, and the
  base's collection run, in a clone of the base, is refused by uv on an
  interpreter outside the base's own `requires-python`. The pre-review
  held-out check syncs no checkout environment, and asks instead for the
  base's own `.python-version` or `requires-python`, which kew reads from the
  base. A host with no Python that answers the request, or a synced
  environment whose interpreter does not report its version, ends the job as
  a [broken environment](#a-broken-environment), a host problem, never as a
  held-out run that wrote no report.
- A base test that fails or errors there **fails the verification**, named by
  node id, unless the PR body's **Behavior changes** section names that node
  id on a line that also gives a reason:

  ```markdown
  ## Behavior changes

  - `tests/test_calc.py::test_add`: add now subtracts; the old sum is no longer the contract
  ```

  A test named without a `[...]` part also covers every parametrization of
  it, so `tests/test_calc.py::test_add` excuses `tests/test_calc.py::test_add[a b]`
  too — the way to declare an id with spaces or quotes in it. It never covers
  another test (`test_add_all`), and a file name alone covers only a file that
  failed to collect, and its tests that never ran (above).
  Pass the body with `--pr-body FILE`. Without one, no failure is excused.
  On a PR kew drives, the agent's own lines sit under **Proposed behavior
  changes**, which declares nothing. When a review round, or a pre-review
  round that failed nothing but the held-out check, asks only for lines the
  implementer already proposed in the Dispatch Report kew stored for the run
  it recorded as publishing the head (an implementation or remediation run
  that succeeded, never an operator's commit bound with `kew publication
  attest`; any other head's report is never read), kew writes those lines
  into **Behavior changes** itself and re-reviews the head, with no
  remediation run. It writes a line only for a test the round asked about,
  rebuilt from that test's id and the agent's escaped reason; the tests the
  proposals miss go to a remediation that asks for just those. A pre-review
  round that also failed a static check, a ui-check or the merge gets no
  lines: its remediation answers the whole round.
- Every held-out run, and the contract's own run, is started by kew's own
  report plugin, which the verifying kew writes outside the candidate's
  checkout under a module name drawn at random on the host. It registers
  first, before any `-p` and any conftest, so nothing
  the candidate ships can shadow it or stand in for it. Plugin autoload is
  off (`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`), so no `pytest11` entry point
  loads unless it is named. The pinned plugins are named, but only those the
  base's `kew/uv.lock` installs: `xdist`, `pytest_cov`, `asyncio`
  (pytest-asyncio) and `anyio`.
- A name is not trusted on its own, because `-p asyncio` loads any
  distribution's entry point called `asyncio`. kew's plugin refuses the run
  unless every plugin registered in it, every hook it called and every module
  of pytest, pluggy, xdist, execnet, pytest-cov, pytest-asyncio or anyio it
  imported is one of three things: the plugin's own file, a file in the
  base's `kew/tests`, or a file listed in the `RECORD` of the one installed
  distribution that owns that package. That distribution must be at the
  version the base's `kew/uv.lock` pins, and the file must still hash to its
  `RECORD`. A conftest the head adds above `kew/tests`, a second copy of a
  pinned distribution, a pinned distribution installed from a path, a VCS or
  a URL rather than an index (it carries a `direct_url.json`, and its
  `RECORD` is whatever that source built), a distribution that writes files
  into another's package, an entry point named after a pinned plugin that
  points anywhere else, and a `sitecustomize` or `usercustomize` that Python
  ran at start-up from the checkout are all refused. So is a head whose `kew/uv.lock` moves one
  of those pins (version, source or hashes) away from the base's: such a PR
  fails the held-out phase until the pin lands on main by another route.
- A run's verdict comes from the JSON report kew's plugin writes to a file
  outside the candidate's checkout, cleared before each run. The run's own
  output goes to a file of its own, which kew never reads as a result, so a
  line shaped like a node id is just text. A run that wrote no report, or one
  kew cannot read, fails the verification.
- The host sends back a log per job, and the candidate's own code prints into
  it first. So the held-out phase runs last in the gate job, after the static
  gates and the containment test. Nothing of the suite job is read for it. It
  opens with a line naming a nonce it draws once the static gates have ended,
  and tags every line it reports with that nonce. kew reads only the tagged
  lines after the opening line, and only in the gate job's log. A job's log that opens
  the phase more than once fails it: an opening the gate prints first, or a
  process left running prints after, is never read as the phase's, and
  cannot quietly displace it.
- Not covered: code the candidate ships runs inside the held-out process, and
  the static gates run it as the job's own user before that. A `src` written to
  rewrite the report from inside the run, a `.pth` file or import hook
  installed by a distribution the head locks, a module of the head's `src`
  that a `.pth` file — an editable install's among them — puts on
  `sys.path` at start-up and something imports before kew's plugin loads,
  the head's own build backend, which `uv` runs to build the project while
  it syncs the run's environment, a process the static gates leave running,
  or files it edits that the held-out phase reads later (uv's cache, the
  checkout's git objects) could all forge a verdict. The checks bind what
  the candidate declares, not what its running code does. They run in the
  process that writes the report; xdist workers load the same plugins from
  the same environment but are not audited themselves. A head whose `src`
  skips a base test from inside the test, after it was collected, is not
  caught by the collection check: a skip is not a failure. Everything in
  this list is code in the diff a human reviews before any merge.
- A phase that did not report — fewer runs than expected, or a run that broke
  instead of reaching a verdict — fails a verification the gate passed. A base
  with no `kew/tests` has nothing to hold out and is recorded as `skipped`.
- A node with an **acceptance contract** also gets its contract file in that tree, read from the commit the ledger recorded it
  at, never from the head. A head that weakened or deleted its own copy still
  runs the recorded version. The file must still match the recorded sha256 and
  sit under `kew/tests`, or the verification is refused. A contract test that
  fails **fails the verification**, whatever the PR body declares.
  The contract also runs on its own, by path, under the base's configuration
  with its `addopts` cleared and no conftest above `kew/tests`, and every
  contract test that run collects must pass: a contract that was never
  collected, or one with a test skipped or xfailed, fails the verification
  rather than counting as passed. A contract run that ends red
  without its report naming a failing test inside the contract is refused too.
- `kew verify` finds the node in the ledger, as a `kew drive` merge does: the
  branch REF names is joined to the run that opened it, or `--item` names the
  item outright. The node's epic is established before any contract is read:
  the one epic whose latest journaled tick lists the item. Only that epic's
  contract runs, never another epic's. When no epic or several list the item
  and any epic holds a contract for it, the verification is refused until
  `--epic` names one, and an `--epic` that neither lists the item nor holds a
  contract for it is refused too. It prints the contract it runs, or
  `acceptance contract: none` and why. The summary line carries
  `contract=<path>` when the contract's own run reported,
  `contract_not_run=<path>` when one was selected but the job stopped before
  running it (a gate that failed first, a host that never started), or
  `contract=none`. A commit sha or a detached `HEAD` binds no node unless
  `--item` is given.

The phase's outcome, its failures (declared and undeclared) and its wall time
on the host are recorded on the audit event as `held_out`, and the summary
line prints `held_out=<outcome> held_out_seconds=<n>`.

## Diff-scoped mutation report

The mutation report asks whether the change's tests kill mutants in the
change's own code (`scripts/mutmut-sampler.py --diff-scope`). It never changed
a verdict, so it is not part of the gate: `kew verify` runs no mutation phase,
and records and prints `mutation=deferred`. After [`kew drive`](drive.md)
merges a PR, the report runs as a job of its own on `[verify] host`, against
the commit the merge was verified as and its diff from the main it was
verified on. A report job never takes the host from a gating verification:
the drive runs it after the pass's merges, and leaves it queued while the
merge queue or the host is held.

- Only mutants on the `src/` lines the candidate added or rewrote since the
  merge base are selected, named exactly as `mutmut run` names them, so
  `mutmut show <name>` shows the one reported.
- They run against the test files the candidate touched plus
  `tests/test_<module>.py` and `tests/test_<module>_*.py` for each changed
  module. A change with no such tests, no changed `src/` lines, or no mutants
  on them is recorded as `skipped` with the reason.
- The run is capped at 600 seconds. A run that reaches the cap is stopped,
  keeps every result it finished, and is recorded as `capped` with the cap
  named; the mutants it never reached count as `not_checked`.
- A scoped test that fails mutmut's clean baseline run under `mutants/` — one
  that runs kew in a child process, for example — is deselected and mutmut
  re-run inside the same cap, so that one test no longer leaves every mutant
  unchecked. The deselected tests are recorded as `deselected`; a mutant only
  they would have killed reads as a survivor.
- A run where mutmut still exits nonzero before the cap is recorded as
  `error`, with mutmut's exit code and its own reason (the failing baseline
  test, or the last line it printed), never as `completed`: its zero survivors
  mean nothing was checked, not that every mutant was killed. Its summary
  omits `survived=`.

Each survivor's file, line, mutant name and mutation, the counts, the cap and
the run's wall time are recorded on a `mutation_reported` audit event, with the
PR, the merged commit and the base, and posted to the PR thread with each
survivor's file, line and mutation. The report never changes anything: the
merge has already happened.

## Across several hosts

The parallel pytest phase is the verification's wall clock on one machine, and
it is splittable: `--shard=K/N` runs the K-th of N groups balanced by the
recorded test durations. `--shards N` is that split across machines.

```bash
kew verify --host gcp --shards 3
```

- The **parallel phase** runs as N jobs, `--shard=K/N` each, on the first N
  instances of [`[hosts.gcp] instances`](../configuration.md#hosts). A pool
  shorter than the shards asked for is refused *before* any job runs, rather
  than two shards being doubled up on one instance — one host runs one job at a
  time, so that would queue them instead of halving anything. `--shards` above
  `1` on the `local` host is refused for the same reason.
- The **gate job** (the static gates, the containment test and the held-out
  phase) runs first, on the **first** host of the pool. Every host gets the
  bundle at once, but none starts a shard until kew has judged the gate job
  clean. A red gate job, or an undeclared held-out failure, starts no shard.
- Everything else that runs **once** — the rest of `verify.sh`, the serial
  phase and `ui-check` — runs on the first host too, beside its own shard.
  One host runs one job at a time, so those two queue there by design; the
  shard goes first, so every host starts its slice at the same moment.
- The verification passes only when **every** shard and the single-run job
  pass. Each shard's own exit code, selected and executed test counts and
  seconds are printed, and recorded on the audit event.
- A shard that does not report is a **failed** verification, never a skipped
  one. A host kew could not reach, a job with no exit code, a count that never
  came back: each is named, and none of them is read as a pass.
- **The counts must add up.** kew compares the sum of the shards' selected
  counts against one unsharded collection, measured by a `--collect-only` pass
  on the first host, and fails the verification when they differ — the jobs
  answered, and kew's own check did not. A split that
  could drop a group of tests would otherwise report green having never run
  them, which is worse than not sharding.
- **Every collected test must have run.** A count of the collection says
  nothing about the run: they are two pytest processes, and a `conftest.py`
  hook can keep a test at `--collect-only` and deselect it at run time. So
  each shard's run writes junit, and a check shipped in kew's own job script —
  not read off the candidate's checkout — compares the node ids the shard
  collected against the test cases its run reported. A collected test the run
  never reported fails the shard and is named in its log; kew then holds the
  executed count each shard reports to its selected count, and a shard that
  reports none fails the verification. A skipped test counts as executed: it
  reached the run, and its skip is in the run's output.

One gate a sharded verification does **not** discharge: the **coverage floor**.
It reads one machine's joined coverage data, and a sharded run has none — each
shard's data stays on its own host. The serial phase therefore runs without the
floor here, and the floor stays where it already is: on every PR, in CI, over
the joined coverage of both phases. Everything else the gate runs, it runs.

The candidate reaches the host as a **git bundle** whose `HEAD` is the ref being
verified, with its full history. Nothing else crosses: the job's environment is
an allowlist, a credential-shaped variable name is refused by name, and what
comes back is an exit code, a duration and log text — never an instruction. The
base commit is passed to the memory-channel guard as `MEMORY_GUARD_BASE`, so the
guard classifies the same commits the attestation names.

`[verify] deselect` node ids reach the job as `PYTEST_ADDOPTS`, for tests that
cannot pass on a verify host for environmental reasons alone. It is empty by
default: a verification that quietly skips tests is not the gate CI runs.

## What is recorded

Every verification — passed, failed, or incomplete — appends one
`verify_completed` [audit](../audit.md) event naming the ref sha, the base sha
and ref, the host, the exit code (`null` when the job produced none), the
wall-clock seconds, whether `ui-check` ran, the acceptance contract it
selected (`contract_selected`, `null` for none) and whether that contract's own
run reported (`contract_executed`), and the outcome. A sharded
verification adds the shard count, the unsharded collection it was checked
against, and one row per job — its label, its instance, its exit code, its
seconds, the tests it selected and, for a shard, the tests its run
executed — so the attestation can say which host
failed, or which one went silent, and not merely that one did. A gate nobody
recorded is a gate nobody can show afterwards, which is why a failure is
recorded too.

Every event also carries `suite` (`full`, or `selected` for a
[tiered](#tiered) verify that ran its selection) and `suite_selection`:
`mode`, the `reason` for it, the selected `files`, and the selection's
`digest` and `selector_version` whenever a selection was computed, also when
it forced the full suite. With the default `[verify] suite = "full"` the mode
is `full`, the reason `[verify] suite = "full"`, and no selection is computed.

Every event also carries `frontend_selection`: `mode` (`full`, or `scoped` for
a [scoped frontend](#frontend) verify that ran what its change owns), the
`reason`, the ui-check `modules` it ran, the changed files it handed
`vitest related` (`related`, relative to `frontend/`), the file-reading tests
it handed it beside them (`always`), and the selection's `digest` and
`selector_version`. With the default `[verify] frontend = "full"` the mode is
`full`, the reason `[verify] frontend = "full"`, and nothing else is set. The
event's `ui_check_modules` is the list of browser modules a scoped `ui-check`
ran, possibly empty, and `null` when `ui-check` ran every module or did not
run.

## What every red verify was {#report}

Each verification job prints one `kew-phase: <name> start|end <unix-seconds>`
line per phase: `setup` (the sync, the probes, the image builds, and any time
no other phase covers), `static`, `containment`, `held_out`, `xdist`,
`serial`, `policy` (a selected verify's `-m policy` run), `image_scan`,
`docs`, `vitest`, `svelte_check`, `www` and `ui_check`. A phase that fails
prints no `end` line. A [scoped frontend](#frontend) verify's
`vitest related` run is made inside the suite job's ui-check phase, so its
seconds and its failure are `ui_check`'s; `vitest` then holds only
verify.sh's `npm ci`.
`verify_completed`
records `phases` (each phase's `seconds` and `outcome`), which add up to the
verification's wall time, the `failing_phase`, its `failing_ids` (the
undeclared held-out failures for `held_out`, otherwise the ids pytest's
short test summaries name, never a line a test printed), the PR `head` it
tested, the phases whose inputs the diff touched
(`changed_inputs`), a digest of the PR body's **Behavior changes** section
(none for a verify given no `--pr-body`: the section is unknown, and an
unknown one never reads as a change) and the `log_path` its log is kept at,
beside the ledger under `verify-logs/`. Logs older than 30 days are pruned
each time one is written. A marker's stamp is whole seconds, so when the
marked phases outlast the wall time by that rounding, each is scaled down
by the same factor.
A sharded verification's jobs run at once, so kew reads them side by side,
not one after another. A phase's `seconds` is its slowest job's, and the
phase failed if any job failed it. For a sharded verification the phases
do not add up to the wall time.

Once the same PR's next verify is recorded, a red one gets its class in a
`verify_red_resolved` event:

| Class | When |
| --- | --- |
| `real` | The head changed in code, tests or fixtures, not only in docs or Markdown, and the next verify passed. |
| `declared` | The head is the same, the PR body's **Behavior changes** section changed, and held-out passed, whether or not main moved meanwhile. |
| `flake` | The head, the base and the body are the same, both verifies ran the full suite and the full frontend (no scoped frontend selection), and the next verify passed. |
| `main` | Only the base changed, and the next verify passed. |
| `infra` | The red verify was incomplete, never ran its gate, its error names the host, its auth, a feed or a ceiling, or more than 5 tests failed in it. |
| `unknown` | None of the above, or the next verify was a selection that did not run a test the red one failed. |

A verify is joined to its PR by the PR number when it is bound to one (the
drive's merge pass, `--pr`, `--item`), and otherwise by the head it tested,
which can tell `declared`, `flake` and `main` but never `real`.

A test that failed, then passed on the same head with the same base within 14
days, gets one issue labelled `flaky`, naming the test id, both verifies'
audit rows and both log paths, relative to the ledger's directory
(`verify-logs/…`, so no home path leaves the machine), recorded as
`verify_flaky_filed`, once per test id. Filing is a side effect on a public
repository, so it is held to little: only a verify bound to a PR files (a bare
hand-run `kew verify <ref>` never does), one red files at most 5 issues, each
`gh` call is given 30 seconds, and the filing runs after the verify is
recorded. An issue names a test only when its id is a plain test id (a `.py`
file path of letters, digits, `_`, `.`, `/` and `-` with no `..`, `::` names
of letters, digits and `_`, and an optional `[…]` parametrization of
letters, digits and `_.,:=/+-`) and its test file exists at the head the red
verify tested, and the id is set as inline code in the title and the body.
An id that fails either check is not filed, and is recorded as
`verify_ledger_failed` with step `skip` and the reason. A resolution or a filing that fails is logged as a warning and
recorded as `verify_ledger_failed`; it never changes the verify's result or
exit code. kew neither retries the verify nor takes the test off the gate:
quarantining it is the operator's decision.

`kew verify report --since 30d` prints the reds of the window by failing phase
and class (`pending` for a red no later verify has resolved yet), then, for
the passing verifies, each phase's p50 and p90 wall seconds and the share of
PRs whose diff touched that phase's inputs. Rows run in phase order and
columns in class order, so two runs over the same ledger print the same text.
Verifies recorded before kew kept their phases are left out, and the report
says how many.

## Main's health and the nightly

`kew verify nightly` runs a full verification of `origin/main` and records it
as `verify_completed` with `suite: full` and `kind: nightly`. It holds the
verify lease (the merge-queue lock the drive verifies and merges under) for its
whole run, so the drive yields to it; when another verification holds the lease
it waits, then gives up and says so. `kew verify nightly install` writes a
launchd agent (macOS) that runs it every day at 01:30 local time, the same way
`kew drive install` writes the drive's: no credential in the plist, and
`--env-file` names a file the agent sources first. `kew verify nightly
uninstall` removes it.

Main's health is derived from the audit ledger, with no table of its own: the
newest full verification of a commit on main's first-parent history decides it.
That is a nightly, a PR verification whose candidate landed unchanged (the
commit itself, or a squash merge on the verified base carrying the verified
tree), or an operator's clear. `kew verify main-health` prints it, and says
when the newest nightly is more than 26 hours old.

**A red main blocks every merge.** Main is red while the newest full
verification of main read it red and nothing below has cleared it. While main
is red, every PR verification runs the full suite (no selection narrows it) and
must pass to merge. A PR unrelated to the regression fails, because main's
failing test still fails in it, and its verdict names the red commit and the
regression issue. The drive and the operator scripts both merge through
`kew verify`, so the one rule blocks both.

Any failure of a job that really ran its gates makes main red, whether or not
it names a failing test. That includes a conftest that will not import, the
coverage floor, mypy, ruff, xenon, semgrep, the docs gate and the browser
`ui-check`. A verification reads **incomplete** only when the job never ran
its gates: no exit code (a kill or the OOM killer, however the host reports it), a broken host
environment, or a refusal at the pinned-tool preflight. An incomplete
verification says nothing about main's health. The nightly bisects nothing
and files nothing for it, and main's health stays what it was.

A red that names no failing test has nothing to bisect or to run alone. Its
regression issue names the gate that failed, as far as the log shows it, and
carries the last lines of the stream that gate printed to. The issue says
which stream that is. A host that returns all of stderr after all of stdout
(the `gcp` host) marks where stderr starts, and kew reads stdout first, so a
gate that passed early never names a later failure. The issue is public, so
the whole window is cleaned before the tail is cut. kew drops PEM blocks and
long base64 runs, and redacts credentials (Slack tokens and the user
information of a URL in any scheme among them), `*.internal` host names, and
host paths. The scan-failure issues are cleaned the same way. Only an
operator's audited clear or a newer green nightly clears such a red.

**The bisect.** A red nightly reads the failing test ids off the log and first
runs those tests alone at the red commit. kew reads ids only from pytest's own
short test summary: for each pytest run in the log (each `test session starts`
header, and each job of a sharded verify), its one `short test summary info`
block before its one counts line (`=== 1 failed in 3.21s ===`). A `FAILED`
line a test printed outside that block names no id. A test controls every
line it prints, though, so it can print a whole summary block, a counts line
or a run start, inside its report or after the run ends. Where the log's shape
is ambiguous (a run with a second summary block or counts line, a summary
after the counts line, a session's summary with no failure report before it,
or verbose and `-q` runs in one log), the log names no ids at all: nothing
bisects, and the issue names the gate that failed and carries the log's last
lines, as for any red that names no failing test. This narrows
forgery, it does not rule it out: a test that prints a whole run start and
report can still pass as one more session. A run there that finds none of them,
or runs no test at all, reaches no verdict: it is neither a reproduction nor a
pass.

- If none of them fails alone there, the red **did not reproduce** (a flake, or
  an interaction with the rest of the suite). Nothing bisects and no PR is
  named: the issue says the tests did not reproduce alone (flake or
  interaction) and lists them.
- If one fails, the red **reproduced**, and the nightly walks main's
  first-parent commits since the last green full verification by binary
  search, running only those tests at each probe. The first commit where they
  fail names the PR. Counting the first probe, it runs at most
  ⌈log2 n⌉ + 1 probes over n commits.

Either way kew files one GitHub issue per red main commit, labelled
`regression` (created if the repository lacks it) and deduplicated by sha. It
names the tests, the pinned PR when there is one, and whether those tests were
inside that change's [selection](#explaining-a-test-selection): inside means a
flake or an interaction, outside means the selector missed them. A second
nightly of the same red commit files nothing and bisects nothing.

The issue title is `main is red at <sha>: <what failed>`. A title renders no
inline code, so it names at most three failing ids, and only plain ones: a
plain test id as above, or the bare `.py` path of a test file pytest could not
collect. When tests failed but none of them is plain, the title counts them
(`3 failing tests`); a red with no failing test names the gate. The body
always lists every id, fenced.

**Clearing a red main.** Nothing clears a red main by retrying it. How it
clears depends on that first probe:

- A red that **reproduced** is a real break. While main is red that way,
  every PR verification runs the red's failing node ids once more after its
  gate, with kew's own report plugin, and records each id's outcome in the
  event's `red_tests` field. The PR clears the red once it lands unchanged,
  but only if its gate passed and every failing id, exactly (class and
  parameters included), reads `passed` there. A dropped parameter or a
  deleted class reads `absent`, and a skip or an xfail is not a pass. A
  verification that recorded no outcomes for those ids is no evidence and
  clears nothing. `kew verify main-health` then names the red commit that PR
  cleared.
- A red that **did not reproduce** is a flake, and so is any red kew could not
  reproduce: a probe with no verdict, or a log that named no failing test. A
  landed PR never clears it. Only a newer green nightly, or an operator's
  audited clear, does.

`kew verify main-health clear SHA --issue N --reason TEXT` is that clear. It
writes one `main_health_cleared` audit event with the actor and the reason, and
main reads green at that commit. It refuses a commit that is not on main, and
a commit on main that no full verification read red.

**The image scan.** A PR verification scans the agent images only when its
diff touches an image input: `docker/`, the packaged copy under
`kew/src/kew/_docker/`, a `uv.lock`, the scanner pin
`kew/src/kew/hosts/provision/trivy.pin`, `kew/src/kew/image_inputs.py` (which
computes the inputs label each built image carries), or
`scripts/scan-agent-image.sh`. One that touches none skips the scan, prints `image_scan=skipped` on its summary
line and records why in the event's `image_scan` field.

The nightly scans every image in a job of its own, and a finding in one image
does not stop the scan of the next. Each image's gate asks trivy for a
findings exit of its own, so a finding and a scanner that could not scan are
told apart. The script prints `gate findings in <tag>` or
`gate error in <tag>: …` for each image whose gate did not pass, and fails at
the end. Once every image's gate has run it prints a closing summary,
`gated every image: …`. A log without that line is a scan that stopped
part-way (a build that failed, a timeout, a package floor), whatever an
earlier image found. kew files the advisories it did read, and files the stop
as a scan failure too. kew files one issue per advisory a gating table names and it has not
filed before: a `CVE-…`, a `GHSA-…`, a `RUSTSEC-…`, or any other id trivy
reports. So a new advisory fails no unrelated PR. The `triage input` table
printed after each gate lists every CRITICAL/HIGH, gates nothing, and files
nothing.

Every other failure is read per image. That covers an image whose gate found
something kew could read no advisory id from, a gate that could not scan, and
a refusal of the whole scan (a scanner that differs from the pin, docker
down, a package floor). Each one records an `image_scan_failed` audit event
and files one `image scan failed: <reason>` issue per sha, even when another
image's advisories were filed in the same run.

**The browser check.** The nightly verifies main against itself, so it has no
diff to say whether the console moved: it runs the browser `ui-check` every
time.

## Explaining a test selection

`kew verify --explain-selection REF` runs nothing. It prints which test files
REF's change would select, and why. Today this only explains a selection: a
verify still runs the full suite.

```bash
kew verify --explain-selection pr-42-head   # a branch, against its merge base with main
kew verify --explain-selection 36db2aab     # a merged commit, against its first parent
kew verify --selection-report --last 50     # the last 50 merged commits, three ways
```

The answer is computed from two trees, the selector version and `GATE_RE`,
read from git objects and never from the working tree. The base tree is REF's
merge base with `origin/main` (or `main`). For a commit already on main, it is
the commit's first parent, so a merged commit replays its own diff. The merge
gate paths are the defaults plus the `[epic] merge_gate_paths` of the
`kew.toml` committed in REF, so uncommitted edits, `kew.toml`'s included, do
not change the answer. The header names how many gate paths applied, with a
digest of them, and the `GATE_RE` in force. The same inputs print
byte-identical output, ending in a `digest sha256:` line over everything above
it.

The selector reads the imports of `src/kew` and `tests/` with Python's `ast`,
in **both** trees, and takes their union, so a module the candidate deleted
still selects the files that imported it. Function-local imports count.
Importing `kew.a.b` also counts as importing `kew.a` and `kew`, because Python
runs every package `__init__` on the way down. So do imports spelled as calls:
`importlib.import_module("kew.x")`, `__import__("kew.x")` and
`pytest.importorskip("kew.x")`. An `import_module(f"kew.plugins.{name}")` counts
as importing every module under `kew.plugins`. A call whose argument is some
other name counts as importing every kew module the file names in a string, or
every kew module when it names none. A Python file a test loads by a path
spelled in literals (`importlib.util.spec_from_file_location`, `runpy.run_path`)
is read too, and its imports count as the test's.

A test file is selected when its imports reach a changed module, directly or
through a `tests/support` module, a loaded script, a `conftest.py` below
`tests/` or a package `__init__.py` under `tests/`. The line names the shortest
chain:

```text
tests/test_x.py  tests/test_x.py → kew.run_store_liveness → kew.stop_recovery [changed]
```

A test file is also selected when it reads a changed file outside `src/kew`,
directly or through a `tests/support` module or a script it loads. This rule
answers for anything under `docs/`, `frontend/`, `docker/`, `scripts/` and
`tests/fixtures/`. A file at the repository root or under `www/` is not one: a
script a test runs reads them (`check-docs.sh` greps `README.md` and `www/`),
so a change there runs the full suite. The selector reads the string
constants in each test-side file, and the paths it spells from `__file__` with
`Path(...)`, `/`, `joinpath`, `os.path.join`, `+`, `%`, `str.format`,
f-strings, `.parent`, `.parents[n]`, `os.path.dirname`, `.with_name`,
`.with_suffix` and `.resolve()`. It follows the names those paths pass
through: module-level names, names a function assigns, a loop over a walked
directory or a literal list, a fixture of the same file taken as a parameter,
and what a function of the file returns. A loop over `Path(__file__).parents`
is an upward search: the path after it is read off each directory above the
file. A helper's parameter is read as each argument the file's calls pass it,
when the file only calls the helper by name and no other test file imports
the module, so `_fixture("../scenarios/a.json")`
into a helper that reads `frontend/fixtures/factory/<name>` reads
`frontend/fixtures/scenarios/a.json`. A string such as `../docs/a.md` is
read from the project root, where the tests run. A glob, an `rglob` or a
variable after a literal directory reads that directory. The line names what
the test reads and the file that changed:

```text
tests/test_docs_truthfulness.py  tests/test_docs_truthfulness.py ← reads docs/web/ [changed docs/web/index.md]
```

A kew module under `src/kew` that reads a repo file by a path spelled in
literals off its own `__file__` selects the tests that import it, the same
way:

```text
tests/test_x.py  tests/test_x.py → kew.report_export ← reads frontend/static/favicon.svg [changed frontend/static/favicon.svg]
```

A changed file under one of those directories that no test reads selects no
test of its own.

These files are always selected, each with its reason:

- `always: changed by the candidate` for a test file the candidate added or
  changed.
- `always: marked policy` for a test file that uses `pytest.mark.policy`.
- `always: runs or loads kew dynamically` for a test file that calls
  `subprocess`, `runpy` or `importlib` with a `kew` argument or with Python
  code that imports kew (a string in the file, as `python -c SCRIPT` runs it),
  calls `importlib.reload`, imports a module by a name it builds, or loads a
  file by a path it builds. The same goes for a test that reaches such a
  file through `tests/support`.
- `always: builds a repo path from a variable` for a test file whose path
  could be any file: a variable right after a repo-root anchor
  (`REPO_ROOT / rel`) or an upward search (`parent / rel` in a loop over
  `Path(__file__).parents`), a step up from a path that holds a variable
  (`(ROOT / rel).parent`), a walk of a root that names no directory, a name
  assigned paths that start in different places, or any path under a root a
  `tests/support` module exports (where that root sits is the support
  module's to say). The same goes for a test that reaches, through
  `tests/support` or a `conftest.py`, a file that reads such a path or whose
  function returns a root or a directory a caller may read anything under.
- `always: a path climbs out of a directory it reads with ..` for a test file
  that reads a directory with a variable after it (`ROOT / _DIR / name`) and
  holds a string with a `..` segment that no path it reads resolves, such as
  a `..` that reaches the helper through a parametrized name: the `..` may
  climb out of that directory to any file. The same goes for a test whose
  `tests/support` modules hold either half.
- `always: runs a script` for a test file that runs a script file through
  `subprocess`, `os.system` or the like: `bash scripts/x.sh`, `node x.mjs`,
  `npm run x`, a command word ending in `.sh`, `.js`, `.ts`, `.py` and the
  like, or a script runner beside a command the selector cannot read. The
  selector cannot see what the script reads. Inline code (`bash -c`,
  `bash --noprofile --norc -eo pipefail -c`, `node -e`) is not a script; a
  shell given options then a file (`bash -e x.sh`) runs one. The same goes
  for a test that reaches such a file through `tests/support`.
- `always: imports no kew module` for a test file that reaches no kew module.

A file the test reads takes precedence over these reasons, except for a test
file the candidate changed: the line names the path that fired.

The answer is the full suite instead when the first of these rules fires. The
output names the rule, and the path or module that fired it:

| Rule | Fires on |
| --- | --- |
| `kew.toml could not be read` | a committed `kew.toml` that is not TOML, or whose `merge_gate_paths` is not a list of non-blank strings |
| `tests/conftest.py changed` | `tests/conftest.py` |
| `tests/support changed` | anything under `tests/support/` |
| `uv.lock changed` | any `uv.lock` |
| `a merge gate path changed` | a path matching a default or configured [`[epic] merge_gate_paths`](../configuration.md) entry |
| `GATE_RE matched` | a path the `GATE_RE` environment variable's regular expression matches, when it is set |
| `a non-.py file under src/ changed` | a template, a JSON file or anything else that is not Python |
| `a non-.py file under tests/ changed` | a non-Python file under `tests/` outside `tests/fixtures/`, which kew cannot trace to the tests that read it |
| `an untraced path changed` | any other path outside `src/kew` and `tests/` that the file-read rule does not answer for, such as `README.md`, `www/`, `evals/` or `.github/`. A test may read it, and nothing says which |
| `a Python file could not be parsed` | a file under `src/kew` or `tests/`, or a script a test loads, that is not valid Python in either tree: its imports are unknown |
| `a tests/ file nothing imports changed` | a Python file under `tests/` that pytest does not collect and nothing imports |
| `tests/conftest.py imports it` | a changed module `tests/conftest.py` reaches, through `tests/support` and the names it passes to `importlib` too |
| `tests/conftest.py reads it` | a changed file `tests/conftest.py` reads, itself or through what it reaches, or any file the file-read rule answers for when it reads a path built from a variable or runs a script: every test loads it |
| `the selection reached 70% of test files` | a selection too large to be worth making |

`--selection-report` replays the last `--last` first-parent commits on main,
each against its own first parent. A squash-merged PR is one such commit. It
prints the median and the 75th percentile of the selected share, **by test
count** rather than file count, under three graphs:

- `today`: the graph as it is.
- `thin kew.cli`: a what-if in which `kew/cli/__init__.py` assembles nothing,
  and each name it defines lives where only its users import it. A file that
  takes a name from `kew.cli` (`from kew.cli import x`, `cli.x` on a
  `from kew import cli`, a `patch("kew.cli.x")` target) reaches only what that
  name's definition uses. A test that drives the root `app` reaches the root
  callback and the commands its strings name, with the modules that register
  their sub-commands; it reaches every command when its strings name none. A
  file that uses `kew.cli` as a whole keeps today's edges. When
  `kew/cli/__init__.py` itself changed, only the names whose definitions
  changed count as changed, unless a statement that defines no name changed
  (an `add_typer` line), which changes every command.
- `core cycle cut`: a what-if in which the largest import cycle among kew
  modules keeps only its module-level edges, as if its function-local imports
  had been moved out of it.

A full-suite answer counts as 100%. The test count is static: each `test*`
function, at module level or in a `Test*` class, counts once. A literal
`parametrize` list multiplies the count by its length. The report then prints
how many commits no path rule forced to the full suite (every rule above but
the last three, which a what-if can change), and the median and p75 over those
commits alone under each graph, which is where a what-if can differ. Then it tallies
the rules that fired under each graph. It then prints today's median and p75 before
and after the file-read rule. "Before" is the selector without it, where every
changed docs, frontend or fixture file answered the full suite. Last comes the
slowest replay's time, over the three graphs and the selection before the
file-read rule.

`--explain-selection`, `--selection-report` and `--frontend-report` run
nothing, so each refuses `--host`, `--shards`, `--pr-body`, `--item`, `--epic`
and `--full`, and they refuse each other. `--last` needs `--selection-report`
or `--frontend-report`, and neither takes a REF.

## The tiered suite {#tiered}

With [`[verify] suite = "tiered"`](../configuration.md#verify), a PR's merge
verify runs the two pytest phases on its change's selection — the test files
`--explain-selection` prints for it — plus every test marked `policy`, and
nothing else of the suite. Everything else runs unchanged: the static gates,
gitleaks, the containment test, the image scan as the diff scopes it, the
docs, frontend and www gates, the held-out phase with its own selection, the
acceptance contract, and `ui-check` when `frontend/` changed. The default,
`"full"`, runs the whole suite and changes nothing.

The selection is computed by the operator's kew, from git objects, exactly as
`--explain-selection` computes it; the candidate's own code never decides what
runs. The suite job runs
`KEW_VERIFY_STAGE=suite KEW_VERIFY_SUITE=selected bash scripts/verify.sh`, which
skips its two pytest phases and says so, then the parallel phase
(`-n auto -m "not serial"`) and the serial phase (`-m serial`) on the selected
files, then `uv run pytest -m policy` over the whole tree. A phase none of the
selected files has a test in passes; any failing test fails the verify. Only
that exact value skips verify.sh's phases: a misspelt one runs the whole suite.

The **coverage floor** reads the whole suite's coverage, so it runs only on a
full verify: the nightly, or a verify that fell back to the full suite. A
selected verify says so beside its verdict. CI still applies the floor to every
PR its runners run.

A tiered verify runs the **full suite** instead, and says why, when any of these
holds:

- the verify is the nightly, or sharded (`--shards` above 1), or `--full` was
  passed;
- main is red, or its health cannot be read;
- the nightly is stale or missing: no green nightly of a commit on main is on
  record within 26 hours. The nightly is what catches a failure a selection let
  through, and pins it to a PR. A PR's full verify that landed unchanged still
  counts toward main's health, but it is no nightly and does not keep the
  selection running;
- the selection cannot be computed (an unusable `GATE_RE`, a tree it cannot
  read), or one of its full-suite rules fires, a `conftest.py` change say.

The summary line carries `suite=selected files=N` for a selected verify and
`suite=full` for a tiered verify that fell back, and a line before it gives the
reason: `kew verify: suite: selected, 41 test files plus -m policy (…); the
coverage floor is not applied, it runs only on the full suite`, or
`kew verify: suite: full (main is red at …)`.

`kew verify --explain-selection REF_SHA` reproduces a recorded selection: the
same files and the same `digest` as the event's `suite_selection`, when run
with the same `GATE_RE`.

## The scoped frontend {#frontend}

With [`[verify] frontend = "scoped"`](../configuration.md#verify), a PR's
merge verify that changes `frontend/` runs, of vitest and `ui-check`, only what
its change owns:

- **vitest:** `vitest related --run` on the changed files: vitest's own module
  graph picks the test files that import one. A test that reads files rather
  than importing them (see the list below), or imports a module that does, is
  never related to what
  it reads, so every such test at the base commit runs in every scoped verify,
  as the tiered suite always runs `-m policy`.
- **ui-check:** the modules whose `ui-check-owns:` lines own a changed file or
  a file that imports one, however indirectly; every module whose own file
  imports a changed file (a helper under `scripts/`, another module); every
  module that reads files itself or through a helper it imports, detected as
  for vitest above, which always runs; and every module the change adds or
  edits. `scripts/ui-check-lib.mjs` reads files, yet a module that imports
  from it only a named list of what reads nothing (`SETTLE_MS`, `widestBox`)
  is no reader for it; one that imports `FRONTEND`, `CHECKS_DIR`,
  `BUILD_DIR`, `DEMO_BUILD_DIR`, `docsPassage`, `buildSentinels`,
  `buildLeaks`, `checkModules`, `owningModules`, `selectModules`,
  `scopedModules` or `runChecks`, or imports the library any other way
  (`* as`, `import()`, a re-export), is. A module with no `ui-check-owns:`
  line has an unknown surface, so every frontend change selects it. The
  ownership lines and the import graph coverage is judged on are the base
  commit's, plus the imports of files the change adds: a module the change
  adds runs, but covers nothing, and an import the change adds to an existing
  file covers nothing either. A unit test a module owns covers nothing it
  imports: the modules that render a fixture load the page and get the
  fixture over HTTP.

What counts as reading files is a list of known APIs: the `fs`, `fs/promises`
and `child_process` modules (with or without `node:`), `fs-extra`,
`graceful-fs`, `glob`, `fast-glob`, `tinyglobby` and `globby`, named in any
kind of quotes, `import.meta.glob`, `import.meta.url` and `?raw`, `?url` or
`?inline` imports. A test or module that reads files some other way is
covered only by the nightly.

`svelte-check`, the frontend build, the demo build and the www gates run whole
either way, and the nightly runs the full vitest and `ui-check`. The default,
`"full"`, changes nothing.

The selection is computed by the operator's kew from git objects, as the
[tiered suite](#tiered)'s is. The suite job sets `KEW_VERIFY_FRONTEND=scoped`
on its verify.sh command line, beside `KEW_VERIFY_STAGE=suite`, never exported,
so verify.sh skips its whole `npm test -- --run` and says so (only that exact
value skips it), and nothing after verify.sh sees it; the gate job never sets
it. In its `ui-check` phase, the suite job runs
`npx --no-install vitest related --run --passWithNoTests <changed files> <file-reading tests>`
from `frontend/` (vitest runs a test file it is handed), then
`npm run ui-check -- --builds <modules>`. `--builds` makes the scoped ui-check
run both builds as a full run does.

A scoped verify runs the **full vitest and ui-check** instead, and says why,
when any of these holds:

- anything that keeps a [tiered](#tiered) suite whole: the nightly, a sharded
  verify, `--full`, a red or unreadable main, a missing or stale nightly;
- the change touches the app shell (`src/app.html`, `src/hooks.*`, the root
  and `(factory)` layouts, `src/lib/shell/`), `src/lib/ui`, the design tokens
  (`src/app.css`), the fixtures loader (`fixtures/plugin.ts`,
  `fixtures/core.ts`), `package.json` or a lockfile, a dotfile at
  `frontend/`'s root (`.npmrc`), the build config (`vite.config.*`,
  `svelte.config.*`, any `*.config.*` at `frontend/`'s root, `tsconfig.json`)
  or the ui-check runner (`scripts/ui-check.mjs`, `scripts/ui-check-lib.mjs`);
- the ownership map cannot be read (no module, a module that is no regular
  file);
- the change deletes anything under `frontend/`, whose importers and readers
  can no longer be traced;
- the change touches a file outside `frontend/` that frontend code imports, or
  a non-test frontend file names against `import.meta.url`
  (`kew/src/kew/models.json`, `www/Caddyfile`, anything under `docs/`, which
  the demo build reads its tours from): `vitest related` is handed only the
  frontend paths;
- a changed file under `src/`, `fixtures/`, `static/` or `demo/` (a unit
  test aside) that no module of the base commit owns or imports through a
  file it owns that is no unit test: a scoped ui-check would check nothing
  about it. Only modules with `ui-check-owns:` lines count here, even while
  some module has none: a module with an unknown surface runs, but proves
  nothing about the change. A file under `static/`, or a `.json`, `.jsonl`
  or `.json5` file under `fixtures/` or `demo/` (in any case), counts as
  covered only when an ownership line names it, even when its name reads as
  a unit test or prose, since pages fetch those over HTTP and never import
  them;
- the selection is 70% of the modules or more;
- the change touches nothing under `frontend/`: vitest runs whole, as before,
  and `ui-check` does not run.

A scoped verify is never evidence of main's health, as a selected suite is
not: the full vitest and `ui-check` never ran on it. The drive's standing
merge rule counts its browser check only when it ran at least one module (the
event's `ui_check_modules`).

The summary line carries `frontend=scoped modules=N` for a scoped verify and
`frontend=full` for one that fell back, and a line before it gives the reason:
`kew verify: frontend: scoped, ui-check 3 of 95 modules, vitest related on 5
changed files and 72 file-reading tests (…); svelte-check and the builds run
whole`, or
`kew verify: frontend: full (the app shell: frontend/src/routes/+layout.svelte)`.

### Sizing the scoped frontend

```bash
kew verify --frontend-report --last 50
```

Replays the last 50 first-parent commits on main that change `frontend/`,
each against main's ownership map as it is today, and prints one line per
commit (`scoped` with the modules it selects, or `full` with why), then the
median and p75 selected share of ui-check modules and of vitest files, and
how many fell back. A fallback counts as everything. The vitest share is kew's
own reading of the import graph, an estimate of what `vitest related` picks,
with every file-reading test counted. Every module carries a
`ui-check-owns:` line, yet a change to a file nearly every page imports
(`lib/api.ts`, `lib/factory/names.ts`) still selects 70% or more and falls
back: the modules that own those pages all run. So does a file fewer pages
import, once the modules that own them and the file-reading modules, which
run for every change, make 70% of all modules. Most modules import
`scripts/ui-check-lib.mjs`; only those that import what it reads count as
readers and always run.

## Exit status

See the shared [exit-code scheme](exit-codes.md).

| Code | Meaning |
| --- | --- |
| `0` | The verification passed on the host. `kew verify main-health`: main is not red. |
| `1` | The verification failed: the host's own exit code, verbatim. Any other non-zero number is the gate's too, passed through unchanged. `kew verify nightly`: main's full verification failed a gate, with or without failing test ids. `kew verify main-health`: main is red. |
| `2` | No exit code from the job; unknown ref; unresolvable base branch; gcp selected without an instance; another local verify holds the lease; configuration failure; an unusable `GATE_RE` for `--explain-selection` or `--selection-report`; an option `--explain-selection` or `--selection-report` would ignore. `kew verify nightly`: main's verification was incomplete (no exit code, a broken host environment, or a refused preflight), the verify lease stayed held for the whole wait, `git` or `gh` is missing, or an issue could not be filed. `kew verify main-health clear`: a commit no full verification read red. |

Because the gate's own status is passed through, a non-zero result can be
either the gate's verdict or kew's refusal to run it; the printed summary line
and the recorded outcome (`passed`, `failed`, `incomplete`) say which.

A job a **signal** killed — the host's OOM killer reaching the gate, say —
never reported a status at all, so it is recorded as `incomplete` with a `null`
exit code naming the signal, and exits `2`. It is not passed off as a gate that
answered "failed".

That holds on either host, because what both of them run the gate under is a
*shell*, and a kill reaches kew through one of two shapes: a negative status
when the kernel reached the shell itself, and the signal number offset by `128`
when it reached the gate underneath. kew reads both as the signal they name, so
the status the `gcp` host's remote marker carries for an OOM-killed gate is not
recorded as a verdict the gate never gave.

Only what the job answered decides the outcome. If the host cannot be
**released** afterwards — a Compute Engine instance whose idle stop could not
be scheduled, which may keep running and billing — kew warns, records the
reason beside the verification, and still reports the status the gate returned.
A job that answered nothing at all records both reasons, so the instance is
never lost behind the failure of the job that left it up.

The release covers **bringing the host up** as well as the job. An instance kew
started and then could not reach over SSH is released on the way out and the
reason recorded, rather than being left running and billing with no idle stop
armed because the verification ended before the job.
