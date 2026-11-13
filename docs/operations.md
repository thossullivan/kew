# Operations

Where kew stores data, how to back it up and restore it, how retention works, and what forward-only migrations mean for upgrades.

## Where data lives

kew keeps its entire durable record in one SQLite file:

```text
<logging.dir>/dispatch.db          # all tables (see below)
<logging.dir>/dispatch.db-wal      # WAL file — exists at runtime
<logging.dir>/dispatch.db-shm      # shared-memory index — exists at runtime
```

`logging.dir` defaults to `./logs/dispatch` (relative to your repo root). Override it in `kew.toml`:

```toml
[logging]
dir = "./logs/dispatch"   # default
```

or at runtime with the `KEW_LOG_DIR` environment variable.

`dispatch.db` holds five logical datasets in one file:

| Table | Contents |
| --- | --- |
| `runs` | Every dispatch run — outcome, cost, tokens, model, branch |
| `audit_events` | Hash-chained governance event log (see [Audit log & tamper-evidence](audit.md)) |
| `policy_snapshots` | Retained governance config snapshots bound to each dispatch |
| `loops` | Active and closed loop records |
| `interactive_usage` | Off-chain interactive usage from `kew ingest file` / `kew ingest serve` (schema v15, #256); surfaced in `kew report` but not covered by `kew audit verify` |
| `epic_graph_cache` | Off-chain per-epic cache of the last provider graph fetch (schema v28, #894); lets the Linear driver / `kew epic status` short-circuit the GraphQL fetch when tracker events show nothing changed. A disposable performance artifact, never covered by `kew audit verify` |
| `review_rounds` | Off-chain per-node governed-review state (round counter + per-round `{head_sha, verdict, sidecar_digest, sidecar_path, accepted_verdict, plan_sha, criteria_json}`, schema v29 + v32 + v59 + v60, #977/#1030/#2151/#2291); lets the driver resume the round count across a restart, locate a NEEDS_CHANGES round's stored findings to remediate against, and read which recorded acceptance criteria the round was judged against (`plan_sha`/`criteria_json` null = judged against nothing stated, never "against the current plan"). Operational state — the attested per-round fact is the `adversarial_review_verdict` audit event — so it is never covered by `kew audit verify` |

The WAL (`-wal`, `-shm`) sidecars exist only while kew is running. A clean shutdown checkpoints WAL into the main file. For backups, `kew backup` uses SQLite's online-backup API, so the WAL is always included consistently regardless of checkpoint state.

Per-run log files (`dispatch-*.log`, `dispatch-*.events.jsonl`) live alongside `dispatch.db` in `logging.dir` and are **never** included in a backup. `--include-logs` bundles the per-run derived-evidence sidecars (`dispatch-*.evidence.json`, `sandbox-*.evidence.json`) only — the sanctioned structured evidence, not raw agent output.

## Ledger identity and fork refusal

### Raw connection exception

`tests/test_production_store_open_boundaries.py::_ALLOWED_RAW_OPENS` documents the raw production open allowlist.
Its source scan requires an exact match with that allowlist.

| Module | Function | Call | Justification |
| --- | --- | --- | --- |
| `run_store_committed.py` | `_committed_connection` | `sqlite3.connect` | Dedicated read-only connection using a `file:` URI with `mode=ro`. Provides snapshot isolation. Never uses the writer's connection. |

This connection reads committed posture and work facts from an existing ledger.
It cannot migrate or write the ledger.
The separate connection prevents uncommitted writer changes from entering those facts.

### Canonical ledger

kew records the canonical ledger for a project in a machine-local registry at
`.git/kew/ledger.json` in Git's common directory, so the operator checkout and
any linked worktrees the operator created share one registry. Agent checkouts
are standalone clones with their own `.git` and do not share it. `kew epic plan`
creates a standalone clone with `--no-hardlinks`. Its object store is independent
from the operator repository. `kew epic continue` and resumed first-class loops
can reuse existing checkouts. Resume refuses linked worktrees and unsafe
standalone clones before reuse. Refusal preserves those checkouts without
mutation (#1813). Outside Git,
the registry is `.kew/ledger.json` under the project root. The registry binds the
canonical project root, database path, and ledger UUID. It identifies which
`dispatch.db` belongs to the project.

Run `kew audit verify` as the routine health check. A healthy result exits `0`
and reports `Chain intact: <N> event(s).`; when anchors exist, it also reports
their match and anchor-chain status. Identity checks happen before the database
is opened. kew fails closed with `WRONG-LEDGER` if the configured database,
registry, UUID, project, or anchors disagree, and with
`REGISTERED-LEDGER-MISSING` if recorded ledger history exists but the canonical
registry or its registered database is missing. Do not copy, rename, or edit
registry files to bypass either refusal.

Use explicit rebind only after identifying the one ledger that should be
canonical:

```bash
# Recover an identity-aware ledger (use the complete canonical UUID):
kew audit ledger-rebind --target /path/to/dispatch.db \
  --expected-ledger-id 01234567-89ab-cdef-0123-456789abcdef \
  --reason "recover moved canonical ledger" --yes

# Adopt a legacy ledger (use the full SHA-256 of its latest legacy anchor):
kew audit ledger-rebind --target /path/to/dispatch.db \
  --expected-anchor-hash <64-character-latest-anchor-hash> \
  --reason "adopt verified legacy ledger" --yes
```

`ledger-rebind` is an explicit recovery or adoption decision, never a way to
merge, splice, or reconcile two chains. It requires exactly one full expected
identity (`--expected-ledger-id`) or legacy anchor hash
(`--expected-anchor-hash`), plus a nonempty `--reason` and `--yes`; mismatches
are refused before the registry is changed.

## Backup

`kew backup` snapshots `dispatch.db` and `kew.toml` into a single compressed archive using SQLite's online-backup API — consistent and safe to run against a live database.

```bash
kew backup                          # write kew-backup-<ts>.tar.gz to logging.dir
kew backup -o /backups/             # write to a different directory
kew backup -o /backups/daily.tar.gz # explicit archive name
kew backup --include-logs           # also bundle per-run .evidence.json sidecars
```

The archive contains:

| File | Always included |
| --- | --- |
| `dispatch.db` | Yes |
| `kew.toml` | Yes (if found) |
| `manifest.json` | Yes — schema version, chain head, run/event counts, chain integrity flag |
| `logs/` | Derived-evidence sidecars only, with `--include-logs` |

**What is NOT captured:** environment variable overrides (`KEW_LOG_DIR`, `KEW_RUNNER`, etc.) and any config values supplied only via env vars are not persisted in the snapshot. If you rely on env-var overrides in CI, document them alongside your backup cadence.

See [`kew backup`](cli/backup.md) for the full flag reference.

## Restore

`kew restore` validates an archive's schema compatibility and chain integrity, then places the DB non-destructively.

```bash
# Restore into the configured log dir (reads logging.dir from kew.toml):
kew restore /backups/kew-backup-20260624T120000Z.tar.gz

# Already have a dispatch.db? Pass --force; the existing file is moved aside:
kew restore --force kew-backup-20260624T120000Z.tar.gz

# Also restore kew.toml (existing one moved aside):
kew restore --force --with-config kew-backup-20260624T120000Z.tar.gz

# Skip chain verification (e.g. archive itself records chain_ok=false):
kew restore --no-verify kew-backup-20260624T120000Z.tar.gz
```

**Stop kew before restoring.** `kew restore` refuses to overwrite a database that another kew process is holding open. The check is a short `BEGIN IMMEDIATE` attempt; if it fails, the restore aborts with exit code 2 and a clear message. The automatic lock check is best-effort — under WAL a read-only connection (e.g. an idle TUI) does not hold a write lock, so it may pass the check; stopping kew is the real guarantee.

Non-destructive guarantees:

- If `dispatch.db` exists at the target, restore refuses unless `--force` is passed.
- With `--force`, the existing DB is moved aside to `dispatch.db.bak-<ts>` (and the `-wal`/`-shm` sidecars likewise) before the restored file is placed.
- With `--with-config`, the existing `kew.toml` is moved to `kew.toml.bak-<ts>` before the archived one is placed.
- The restored DB is schema-migrated forward to the running kew's schema version on first open (the migration runs once at restore time).

See [`kew restore`](cli/restore.md) for the full flag reference.

## Suggested cadence

A daily backup run is enough for most teams. Example cron or CI step:

```bash
# cron: daily at 03:00
0 3 * * * cd /path/to/your/repo && kew backup -o /backups/kew/
```

In a GitHub Actions workflow:

```yaml
- name: kew backup
  run: kew backup -o artifacts/
- uses: actions/upload-artifact@v4
  with:
    name: kew-backup-${{ github.run_id }}
    path: artifacts/kew-backup-*.tar.gz
    retention-days: 30
```

Rotate old archives with standard tools (`find /backups/kew/ -mtime +30 -delete`).

## Anchor cadence and the staleness alarm

The audit chain is witnessed by the daily `audit-anchor` workflow
(`.github/workflows/audit-anchor.yml`, `17 4 * * *`), which appends one line to
`audit-anchors.jsonl` and pushes it. It runs on the **self-hosted** runner, with
the same shared-clone preflight `ci.yml` uses: the canonical ledger exists only
on that machine, so a hosted fallback would anchor an empty chain and report
success. It runs only while the repository is private and is skipped in any
other (see [Public repository runner rule](#public-repository-runner-rule)).
That workflow has no `pull_request` trigger, so it never runs PR-authored code;
adding such a trigger means revisiting its runner in the same change.

The failure mode this job has actually had is silence: it stopped anchoring and
nothing said so for weeks (#1408 / #1121; the windows are recorded in
[Audit log & tamper-evidence](audit.md#unwitnessed-windows-recorded-never-backfilled)).
`kew audit verify` cannot catch that — an unwitnessed chain verifies perfectly
— so the alarm is anchor **age**:

```bash
kew audit verify --anchors audit-anchors.jsonl   # prints "Newest anchor: … (N day(s) old)"
kew doctor                                       # warns + exits 1 past [audit] anchor_max_age_days (default 7)
```

When `anchor-age` warns:

1. Check the workflow's recent runs (`gh run list --workflow audit-anchor.yml`).
   A run that dies in the preflight means the runner's shared clone is in a bad
   state — delete `_work/kew` on the runner host to force a fresh clone. A run
   that anchors and then fails at "Commit and push" was rejected
   non-fast-forward: it sat in the runner queue and pushed from a stale
   checkout ("fetch first"). The step now rebases onto `origin/main` with
   three attempts. Read which line repeated: `fetch failed` three times means
   the runner cannot reach GitHub; `push rejected` three times means
   something else is pushing to `main` continuously; a rebase *conflict*
   (someone edited the anchors file tail) fails with its own `::error::` and
   needs a human.
2. Check that the `KEW_PROJECT` repository variable still points at the
   canonical project directory on that runner; the job refuses to anchor
   without it rather than anchoring the wrong ledger.
3. Re-run it (`gh workflow run audit-anchor.yml`) and confirm a new line lands
   in `audit-anchors.jsonl`.

Do **not** backfill the missed days. An anchor's value is that it was witnessed
at the time; an anchor written today for a July chain state attests to nothing,
and would misrepresent the gap as covered.

## Retention

kew **never** prunes run rows automatically. `retention_days` sets a cutoff, but it is only enforced when you explicitly ask for it:

```toml
[logging]
dir = "./logs/dispatch"
retention_days = 90   # 0 = keep forever (default)
```

With `retention_days > 0`, run `kew export --purge` to delete records older than the cutoff. The purge happens **after** the export completes, so you always keep an exported copy first. There is no background or on-dispatch pruning — nothing is deleted unless you run that command (and with `retention_days = 0`, `--purge` refuses and tells you to set a cutoff first).

## Renaming the repository

When GitHub renames the project's repository, the ledger still records the old
`owner/name` on its runs, launches, merge queue and review policy. GitHub
redirects the old name only until another repository takes it. Move the rows
once, with [`kew rename-repo`](cli/rename-repo.md):

1. Stop the drive (`kew drive stop`) and any `kew serve`, and wait for in-flight
   runs, merge claims and queued merges to finish.
2. Back up the ledger: `kew backup`.
3. Edit `kew.toml`: `[project] repo = "<owner>/<new-name>"`.
4. Dry run: `kew rename-repo --from <owner>/<old-name>`. Check the counts and
   any line saying `--apply would refuse now`.
5. Apply: `kew rename-repo --from <owner>/<old-name> --apply`.
6. Verify the chain: `kew audit verify --anchors audit-anchors.jsonl`.

Run it while GitHub still redirects the old name: `--apply` checks that
`gh api repos/<owner>/<old-name>` answers the new name. After another
repository takes the old name, that check fails, and only
`--from-name-reused` (you vouching for the rename) lets it run.

`--apply` never rewrites audit rows; it appends one `repo_renamed` event, and
the audit readers count the old slug's rows recorded before it. The rename is
recorded once: rows written under the old name afterwards are refused, not
moved. Run reports move with only their `repo` and `pr_url` values changed.
Anything that reads the old slug from a published export (for example a demo
built from `kew report provenance`) should be exported before step 5.

## Cutting a release

A person cuts a release, with an explicit version. Commit subjects never cut
one: a push to `main` runs the Release workflow's tests, builds the web bundle
and the wheel, and checks them, and publishes nothing.

1. In a pull request, set the new version in `kew/pyproject.toml`,
   `kew/src/kew/__init__.py` and `create-kew/package.json`; run `uv lock` in
   `kew/` and commit `kew/uv.lock` (a lock that disagrees with
   `kew/pyproject.toml` fails `tests/test_packaging_metadata.py`); add a
   `## v<version>` section to `CHANGELOG.md`. Merge it.
2. Run the **Release** workflow on `main` with `release_version` set to that
   version: `gh workflow run release.yml --ref main -f release_version=<version>`.
3. Approve the `pypi` and `npm` environments when the run waits on them.

The run does this, and stops at the first refusal:

- The `release` job runs only in the repository `REPO_SLUG` names in
  `www/src/lib/data/install.js`. A fork, or the private development
  repository, runs the tests and never reaches a release step.
- While the suite runs, the `canonical` job checks the request:
  `.github/scripts/release_preflight.py` refuses unless the run is on
  `refs/heads/main`, `release_version` is a plain `X.Y.Z` equal to all three
  version fields, and `CHANGELOG.md` has a non-empty section for the version.
  The `release` job checks again and adds the tag: no tag `v<version>` may
  exist here or on `origin`.
- The web bundle and the wheel are built and checked (twine, the web UI
  inside the wheel, a cold install whose `kew --version` must print the
  version), the CycloneDX SBOM is generated, and the wheel, the sdist, the
  SBOM and the release notes are uploaded as workflow artifacts. The `release`
  job only reads: it runs the repository's code, and anything that code runs
  could read a write token from the runner.
- Then the `tag-release` job, the only job that may write, creates the tag
  `v<version>` and the GitHub release at the tested commit, with that
  `CHANGELOG.md` section and a `run: <run id>` line as the notes, and attaches
  the artifacts. It checks out nothing and runs no code from the repository:
  only `gh`, against the artifacts.
- `publish-testpypi` (when the repository variable
  `TESTPYPI_PUBLISHER_REGISTERED` is `true`), then `publish-pypi` and
  `publish-npm` (when `PUBLISH_RELEASE` is `true`) publish through trusted
  publishing, each in its environment. None of them runs without
  `release_version`.

### When a release fails

- **A job failed:** fix the cause and use **Re-run failed jobs** on the same
  run. A re-run (`GITHUB_RUN_ATTEMPT` above 1) finishes only what that run
  started: `tag-release` accepts an existing tag only when it points at the
  same commit and the release on it carries this run's `run: <run id>` line,
  and then attaches only the files that release does not have yet: a file
  already attached is never replaced, even by a rebuilt one, so "Re-run all
  jobs" cannot swap a finished release's files. A new run refuses any existing
  tag. The PyPI upload skips files PyPI already holds (`skip-existing`), and
  the npm job skips a version npm already has.
- **The run refuses an existing tag or release** (made by hand, by another
  run, or at another commit): nothing was created or replaced. Check what the
  tag and the release hold; if they are not a release you mean to keep,
  delete both (below) and dispatch the workflow again.
- **The version can never publish** (the tag exists but the packages were
  never accepted): delete the release and its tag, fix the cause in a pull
  request, and dispatch the workflow again:
  `gh release delete v<version> --cleanup-tag`.
- **A broken version is already on PyPI:** PyPI never accepts a file name
  twice, even after a delete, so it cannot be replaced. Yank it on PyPI
  (deprecate it on npm) and fix forward as the next patch version.

### Before the first release

The workflow publishes through trusted publishing (OIDC), with no stored
token. Register, once, the publisher each index trusts; the owner and
repository are `REPO_SLUG`'s value in `www/src/lib/data/install.js`
(`thossullivan/kew`):

- **PyPI** (`kew-cli`) and **TestPyPI**: a (pending) trusted publisher with
  owner `thossullivan`, repository `kew`, workflow `release.yml`, and
  environment `pypi` (PyPI) or `testpypi` (TestPyPI).
- **npm** (`create-kew`): a trusted publisher for repository
  `thossullivan/kew`, workflow `release.yml`, environment `npm`.

Create the GitHub environments `pypi`, `testpypi` and `npm`, each limited to
deployments from `main` and with a required reviewer. Then set the repository
variables the workflow reads:

| Variable | Gates |
|---|---|
| `PUBLISH_RELEASE` | `true` lets `publish-pypi` and `publish-npm` run for a run with `release_version` |
| `TESTPYPI_PUBLISHER_REGISTERED` | `true` lets `publish-testpypi` rehearse the upload on TestPyPI |

The repository guard only keeps forks and the private repository from
reaching a release step. The boundary that stops anyone else publishing is
trusted publishing plus the environments' protection rules.

### Release day: the install command moves to PyPI

Until `kew-cli` is on PyPI, the README, Getting Started and the site install
kew from the git source, and `create-kew` already installs `kew-cli[serve]`
from PyPI, held to its own major version (npm publishes it only after PyPI).
Keep the
`[serve]` extra in every command: without it there is no web console. Once the
first release is live on PyPI, one pull request makes these edits, in order:

| # | File | Change | Gates that pin it |
|---|---|---|---|
| 1 | `www/src/lib/data/install.js` (`INSTALL`, about line 17) | `uv tool install "kew-cli[serve]"` | `www/tests/repository-slug.test.mjs` (the command and its `INSTALL_PARTS` pieces); `tests/test_docs_truthfulness.py::TestFrontDoorTruthfulness::test_install_copy_uses_the_source_command` |
| 2 | `www/src/lib/Install.svelte` (about line 59) | the "Install from source" label | the same `TestFrontDoorTruthfulness` test |
| 3 | `README.md` (the `INTERIM-INSTALL` block, about lines 100-105) | the PyPI command; drop the markers | `tests/test_docs_truthfulness.py::test_2030_git_install_names_the_missing_web_bundle` (its git-install sentence) |
| 4 | `docs/getting-started.md` (the `INTERIM-INSTALL` block, about lines 38-48) | the PyPI command and the sentence about the git install's missing web UI | the same `test_2030` test |
| 5 | the tests named above | expect the PyPI command | the merge gate runs their old copies against the new tree, so list them under **Behavior changes** |

`www/scripts/check-install.mjs` (run by `www/deploy.sh`) already accepts
`uv tool install "kew-cli[serve]"`, `pipx install "kew-cli[serve]"` and
`pip install kew-cli`, and for those it checks PyPI instead of GitHub: the
project must be published and must declare the extra the command names. After
the pull request merges, deploy the site from a clean `main` with
`www/deploy.sh`.

## Making the public snapshot

The public repository starts as one commit: a `git archive` of private
`main`, with no history and no tags. Make it with:

```sh
bash scripts/make-public-snapshot.sh <output-dir>
```

It refuses unless the checkout is on `main` with no uncommitted change to a
tracked file and `<output-dir>` is missing or empty and outside the checkout.
It then:

1. exports `main` with `git archive`, which leaves out every path
   `.gitattributes` marks `export-ignore` (each line says why);
2. lists the files left out, and refuses an `export-ignore` line that leaves
   nothing out;
3. commits the export as one commit in a throwaway repository inside
   `<output-dir>` and checks it: the private checkout's private-name scans,
   which are export-ignored and so never ship, run against the snapshot
   through `KEW_TREE_SCAN_ROOT` (read in `kew/tests/support/tree_scan.py`);
   the snapshot's own `kew/tests/test_tracked_tree.py` runs against its own
   tree; and the pinned gitleaks scans it with the snapshot's `.gitleaks.toml`.
   The script refuses to run in a checkout that does not hold the private-name
   scans;
4. removes that repository and checks that `<output-dir>` holds exactly what
   the archive wrote.

It never pushes and never creates a repository. To leave another file out,
add an `export-ignore` line with a reason; `kew/tests/test_public_snapshot.py`
refuses a line naming a path that does not exist, a glob, and a path any test,
workflow or gate script reads, so the public repository's own CI never misses a
file. Read the listing before pushing: once pushed, treat the contents as
public for good.

## Cache Amortization Falsification Probe (D3)

> **Decision D3** (context-memory design): the falsification test ships first.
> Two dispatches with equal prefix_key <5 min apart; if run 2's first-turn
> `cache_read_tokens` ≈ 0, the amortization language dies and D becomes purely
> per-run accounting.

This probe answers whether kew's fleet-amortization claim is real: do two agents dispatched
in quick succession actually share a prompt-cache warm line, or does each pay full input cost?

### What the probe measures

`prefix_pair_probe` from `kew.analytics` finds all pairs of completed runs where:

- both runs share the same `prefix_key` (identical stable prompt prefix), and
- the second run started within the provider's cache TTL of the first (default 300 s for
  Anthropic; pass `ttl_seconds=` to override).

Still-`pending` runs are excluded: their token counts are placeholders that would inject a
spurious cache-miss verdict.

For each qualifying pair it returns `run2_first_turn_cache_read_tokens` — the cache-read
tokens on the **first turn** of the second run, parsed from that run's stream-json
transcript (`log_file`). First-turn semantics are essential: every multi-turn run
self-caches from turn 2 onward, so the run-level total is almost always large and would
report "amortization is real" even when the first turn served **zero** cross-agent cache
read — the exact D3 kill scenario. Only the first turn's cache reads reflect a warm line
left by a *sibling* run. A warm hit produces a large value; ≈ 0 means no cross-agent cache
was served. The value is `None` when the second run's transcript is unavailable (the
first-turn value cannot be determined — do **not** fall back to the run total).

### Operator procedure

1. **Dispatch the same trivial issue twice, under 5 minutes apart**, against the same
   runner and model so both runs share a `prefix_key`.  A dummy issue that always takes
   < 2 min is ideal (e.g. "echo hello").

2. **Wait for both runs to finish**, then open a Python REPL or script in your repo root:

   ```python
   from kew.run_store import SqliteRunStore
   from kew.analytics import prefix_pair_probe

   store = SqliteRunStore("logs/dispatch/dispatch.db")
   pairs = prefix_pair_probe(store, ttl_seconds=300)
   for p in pairs:
       print(
           f"run1={p['run1_id']}  run2={p['run2_id']}"
           f"  delta={p['delta_seconds']:.0f}s"
           f"  run2_first_turn_cache_read={p['run2_first_turn_cache_read_tokens']}"
       )
   store.close()
   ```

3. **Interpret the result:**

   | `run2_first_turn_cache_read_tokens` | Verdict |
   |---|---|
   | ≫ 0 (e.g. tens of thousands) | Cache warm — amortization is real |
   | ≈ 0 | Cache miss — delete fleet-amortization language from the spec (D3) |
   | `None` | Run 2's transcript is missing — re-run the probe with a completed run that has a `log_file` |

### Verdict recording

Record the outcome against decision D3. If the result is a cache miss, remove all
fleet-amortization language from kew's documentation; leave per-run accounting only.

## Switchyard passthrough operations

Switchyard is disabled by default. Keep `[inference_transport].mode = "direct"`
until the human-reviewed passthrough UAT gate
(a maintainer-held record that is
not published) records a
`GO` for the exact Kew commit, runner, model, Switchyard binary/config digests,
and corpus being used. A gate for one identity does not authorize another.

### Startup and preflight

Before either lifecycle, verify the audit chain and configuration while the
fleet is stopped:

```bash
kew stop --reason "Switchyard evidence or maintenance window"
kew audit verify
```

In **external mode** (`managed = false`), the endpoint operator owns startup,
upstream credentials, availability, logs, retention, and shutdown. Start it by
the reviewed vendor procedure, then run `kew health --json`. Kew performs
bounded `/health` and `/v1/models` checks and refuses an absent route. It does
not start, restart, or stop an external endpoint.

In **managed mode** (`managed = true`), do not start the configured binary by
hand. For each Kew run, Kew revalidates the absolute executable's SHA-256 and
reported version, reads the route config once, writes those bytes to a private
run-owned snapshot, launches one new process group on the configured loopback
endpoint, and waits up to `startup_timeout_seconds`. The runner starts only
after health and exact-route admission. Final release, cancellation, or startup
failure terminates and reaps the process group and deletes the snapshot.

Managed mode supplies the proxy a deny-by-default process environment. Routes
that need no upstream authentication leave `credential_env` empty. For authenticated
routes, export the approved value in the host environment and declare only its
name in `credential_env`. Kew requires that declaration to match the exact
route's `api_key_env` references, then launches the proxy with only those
matched variables. Never place a secret value in Kew or Switchyard
configuration. See
[Switchyard credential boundary](security.md#switchyard-credential-boundary).

### Canary and benchmark window

The live commands are evidence tools, not routine dispatch checks:

```bash
install -d -m 700 /private/approved/switchyard-canary
kew canary --runner <runner> --model <model> \
  --record /private/approved/switchyard-canary --json
kew switchyard benchmark \
  --corpus /private/approved/switchyard-corpus.json \
  --runner <runner> --model <model> > /private/approved/benchmark.json
```

Create the record directory and retention deadline before the window. Run one
approved model sequentially, exclude all other traffic from the managed
endpoint, and stop on any identity/digest mismatch. Benchmark exit `1` means
failed evidence; exit `2` means the request was refused or unavailable. Neither
is partial rollout approval. Preserve only sanitized results and digests in the
UAT report, then delete the raw canary directory by its deadline.

### Failure response

| Symptom | Meaning and response |
| --- | --- |
| Config load refusal | Fix the endpoint origin, exact version, absolute paths, binary digest, or `fallback = "refuse"`; do not weaken validation. |
| Credential declaration mismatch or unavailable | Stop before launch. Confirm the exact route target/client references, reviewed name-only `credential_env`, and nonempty host variable. Never add unrelated names or copy values into configuration. |
| `wrong_version`, digest mismatch, or dry-run failure | Treat as provenance failure. Keep direct mode and obtain a reviewed build/config. |
| `unhealthy`, `missing_route`, TLS error, or startup timeout | No runner is admitted. Repair the owned endpoint; never retry through direct mode under the same admitted run. |
| `transport_unavailable` during a run | The proxy was lost after admission. Treat the run as failed evidence; inspect sanitized Kew status and operator proxy logs separately. |
| Benchmark counter reset, overlap, or unexpected request delta | All proxy observations for the window are invalid. Runner usage remains readable, but rerun the entire approved window. |
| Credential/authentication failure | Permanent for that attempt. Revoke if exposure is suspected; do not print credentials or retry with a broader credential. |
| Raw evidence disclosure or unexpected model/route | Stop the fleet, revoke affected credentials, quarantine/delete raw material under incident policy, and keep the rollout gate on `HOLD`. |

### Shutdown

Managed processes are run-scoped and close automatically. Confirm the canary's
`cleanup_complete` check and benchmark exit status; a missing cleanup result is
a failed gate. For an external endpoint, first stop Kew admissions, wait for or
terminate its clients under the normal incident procedure, then use the
endpoint operator's bounded shutdown procedure. Never kill processes by a
broad name match.

After either lifecycle, run `kew audit verify`, reduce evidence, delete the raw
record directory by its recorded deadline, and record deletion in the UAT
report. Proxy logs have their own operator-defined retention and must not be
copied into Kew's audit chain.

For routine rotation, latch dispatch first, allow or terminate existing runs,
replace the provider key in the host environment, and resume only after a new
managed canary. For suspected disclosure, revoke the provider key immediately,
run `kew stop`, confirm the managed process is gone, inspect only sanitized
logs/evidence, rotate, and repeat the full approval window. A same-user or
privileged OS process may inspect a live child environment; treat host-account
compromise as credential exposure.

### Switchyard rollback

1. Run `kew stop --reason "Switchyard rollback"` to terminate in-flight Kew
   agents and latch new dispatch.
2. Change `[inference_transport].mode` to `"direct"`. Leave historical run and
   audit rows intact.
3. Stop an external proxy with its operator procedure. Managed processes are
   released with their Kew runs; confirm no approved endpoint remains in use.
4. Run `kew health` and verify the Switchyard check is skipped, then run one
   approved direct runner canary.
5. Run `kew audit verify`. Only a human operator should run `kew resume` after
   direct health, canary, and audit integrity pass.

Rollback does not erase prior transport evidence: transport fields are
additive and old rows remain readable. Do not re-enable Switchyard until a
human approves the evidence and any changed binary, route config, model,
runner, or corpus digest.

### V1 limitations

- Passthrough is the only rollout-eligible route. Random, classifier, stage,
  and automatic fallback routing remain disabled.
- Codex and Claude Code are the only proven transport adapters. Other runners
  refuse before launch.
- Health proves endpoint/route presence, not provider authorization or future
  request success.
- Proxy metrics are process-wide and are never authoritative per-run billing.
- Managed processes are deliberately not reused between Kew runs.
- Kew does not install/update Switchyard or automatically delete canary raw
  records.

## Public repository runner rule

The public repository never registers a self-hosted runner, and it requires
approval before workflows run for pull requests from forks. Both are
repository settings, and on launch day they are the security boundary:

1. In the public repository, **Settings → Actions → Runners** lists no
   self-hosted runner. Never register one there, and never add the repository
   to a runner group that holds one.
2. **Settings → Actions → General → Approval for running fork pull request
   workflows from contributors** is set to *Require approval for all external
   contributors*.

For a `pull_request` event GitHub runs the workflow file from the pull
request's merge ref, so a fork can rewrite any check written inside a
workflow. The workflows still route by visibility, as a convenience and a
second line: every job that can use the self-hosted runner names it only when
`github.event.repository.visibility` is exactly `private` (the scheduled
`audit-anchor` and `canary` workflows read visibility from the API, because a
schedule event carries no repository object). Any other value, including an
empty one, selects `ubuntu-latest`. Each such job starts with a
`Runner boundary` step that fails on a runner that is not GitHub-hosted in a
repository that is not private. The host-state workflows (`approval`,
`audit-anchor`, `canary`) are skipped outside a private repository, since a
hosted runner has none of the state they act on.

While the repository is private, pull-request CI for an agent PR waits for a
human, see [Agent PR CI approval](#agent-pr-ci-approval).

### The Switchyard image on the hosted runner

The `kew-switchyard` image builds only from its pinned source input. That
input exists on the private runner alone, because the pinned Switchyard input
includes unreleased upstream patches; it builds on the private runner until
they are public. So the Switchyard image tooling is **opt-in**:
`KEW_SWITCHYARD_IMAGE=1` runs it, and unset or empty — the default on every
machine that does not hold the input, which is the hosted runner, the GCP
verify VM and a contributor's laptop — skips three things, each printing that
reason:

- the tests marked `requires_switchyard_image` (the Switchyard topology tests
  and the pinned proxy build), deselected by `kew/tests/conftest.py`;
- the Switchyard image check in the release `serial-tests` prerequisites;
- the Switchyard layer of `scripts/scan-agent-image.sh`, which on a default
  run never even validates the private source archive.

Any other value is a usage error on all three paths, so a typo cannot opt in
or out silently. Nothing else skips: a default run builds the `kew-allowlist`
network and the pinned agent image
(`.github/scripts/provision_hosted_docker_inputs.sh`), runs every other serial
test, and scans every agent image.

`ci.yml` and `release.yml` opt in exactly where the input exists — the private
runner — with this line at the top of each:

```yaml
KEW_SWITCHYARD_IMAGE: ${{ github.event.repository.visibility == 'private' && '1' || '' }}
```

The archive secret is mapped only into the steps that line opts in, so a
default run never receives it. Because the marked tests are absent from every
default run, an empty selection would otherwise be invisible: each opted-in
job runs `.github/scripts/assert_switchyard_tests_selected.sh` before its
suite, which fails when `KEW_SWITCHYARD_IMAGE=1` but no marked test was
selected. Default runs do not exercise the sidecar packet certification at
all — see
[Worker namespace filter proof](security.md#worker-namespace-filter-proof).

To opt the hosted runner in, once the patches are public, change that
expression to a plain `'1'` in both files (or delete the switch and the three
skip branches outright). The hosted runner then needs the pinned source
input: rebuild the archive from the public commit and check it against the
digest pinned in `docker/switchyard.Dockerfile` before the build uses it.

Every `uses:` in the workflows is pinned to a full commit SHA with its tag in a
trailing comment, and the gitleaks and trivy downloads are checked against
SHA-256 values copied from their releases' checksum files.

## Agent PR CI approval

While the repository is private, `ci.yml` runs its `test` and
`agent-image-scan` jobs on the self-hosted runner (a maintainer's machine), with a
secret in the environment. An agent-authored PR can add an ordinary test file,
so those jobs do not run for an agent PR until a human approves it (#2033).

- An agent PR is one labelled `agent-pr` (`labels.agent_pr`), or one whose
  head branch starts with `agent/issue-` (`branches.prefix`). kew opens the PR
  before it can label it, so the first `opened` event has no label yet, and the
  branch name covers that gap.
- **Agent PRs wait for a human `ci-approved` label before self-hosted CI
  runs.** Adding the label (`labels.ci_approved`) is a `labeled` event, and
  that starts CI.
- **A new agent push clears it.** Before kew pushes new agent commits to a
  branch with an open PR (review remediation, `kew ci-fix`, `kew resolve`, a
  loop iteration, or a dispatch republish), it removes `ci-approved` from that
  PR. When it cannot remove the label, it does not push. Every new agent head
  needs a fresh approval.
- The gate is a job-level `if:` that GitHub evaluates before any runner picks
  the job up, so a skipped job runs nothing on the host. It needs no hosted
  job.
- Human PRs run as before. A human push to an agent PR is still gated: the
  agent label stays, and so does an approval the human already gave.
- A `labeled` event for any other label runs no job, and it cannot cancel a
  run in progress.
- The public repository's hosted path is unchanged.
- A loop's `ci_green` gate never counts a skipped check as green. A head whose
  tests were skipped waits with `gate_waiting: awaiting ci-approved: tests did
  not run` (see [`kew loop`](cli/loop.md)).
- The epic driver does not merge on GitHub's `CLEAN` alone, since GitHub
  counts a skipped required check as passing. It reads the head's check runs
  with the same rule and holds an unapproved agent PR with `awaiting
  ci-approved: tests did not run` (see [`kew epic`](cli/epic.md)). The epic
  reports `AWAITING_MERGES` for it, not `STALLED`, and writes no hold.
- Only `push` runs the self-hosted jobs without the agent check. Every other
  event, `pull_request_target` included, must pass it.

The gate relies on the agent holding no GitHub credential. That holds under
canonical publication, which is the default. With `[sandbox]
dangerously_skip_containment = true` and an agent-publishing runner, the agent
can push and edit labels itself, and the gate does not hold. A change to a
workflow file is held for a human by the publication filter, so an agent PR
cannot remove the gate on its own.

Two one-time steps, once per repository: create the label (`kew setup`
creates it with the others, or add `ci-approved` by hand), then add it to each
open agent PR you want CI to test.

## Operator checkout integrity incidents

Every dispatch snapshots the operator checkout's `HEAD`, current branch, and the
configured base ref before the agent starts, and compares them after it exits. A
run must never move the checkout you dispatched *from*, only its own clone.
If the snapshot moved (or could not be read back), kew records a
`root_checkout_integrity_violation` audit event carrying the before/after values
and the reason, visible with:

```bash
kew audit tail --event root_checkout_integrity_violation
```

What the incident does to the run depends on whether the run had already
published:

| Run state when the incident is detected | What the operator sees |
| --- | --- |
| **No PR published** | Fails closed. The item is labelled `agent-failed` with a comment naming the reason, the clone is preserved for incident review, and the dispatch exits non-zero. `kew epic status` shows the node failed. |
| **PR already published** | The published outcome stands. Labels are left exactly as the run's own outcome set them: a clean exit keeps `pr-pending-review` on the item and `kew epic status` shows the node **in review** while the PR is open; a non-zero exit or a kill keeps its normal handling (`agent-failed`, or reset to `ready`, and the status reflects that). A GitHub item gets a comment naming the incident and the PR number; a Linear item keeps the audit event only. |

The second row is the point: the violation happened in *your* checkout, but the
PR is reviewable work that reached the remote regardless. Relabelling it
`agent-failed` hid a finished node and asked a human to redo work that was
already waiting for review. The incident is still recorded in full — it is
reported, not enforced, against an artifact that already exists.

Only positive evidence of a published PR softens the incident. If the PR lookup
cannot be answered (GitHub unreachable, credentials expired), kew treats the run
as unpublished and fails closed, so an outage can never disarm the check.

Either way, inspect the checkout before dispatching again:

```bash
git -C /path/to/checkout status
git -C /path/to/checkout log --oneline -5
git -C /path/to/checkout reflog -20    # what moved HEAD, and when
```

## Migration safety

kew uses forward-only SQLite migrations. Each release can only add schema steps, never remove them. Opening a database that was written by an **older** kew version applies any missing migration steps automatically — you never need to run a migration command manually.

Opening a database written by a **newer** kew is refused with a clear error:

```text
SchemaTooNewError: dispatch.db is schema vN; this kew supports up to vM. Upgrade kew to open it.
```

kew never silently corrupts a database it cannot understand. To resolve this, upgrade the kew CLI to match or exceed the version that wrote the database.

The same schema guard applies during `kew restore`: if the archive's manifest records a schema version higher than the running kew supports, the restore is refused before any files are touched.
