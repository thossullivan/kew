# kew doctor

```text
Usage: kew doctor [OPTIONS] [COMMAND] [ARGS]...

Options:
  --json
```

```text
Usage: kew doctor route-live-confirm [OPTIONS]

Options:
  --complexity TEXT
  --reclaim / --no-reclaim
  --force-probe
  -y, --yes
```

Run local environment diagnostics and surface known issues that cause silent
failures or wasted dispatch spend.

```bash
kew doctor                # human-readable checks; exit 0 clean / 1 issue found
kew doctor --json         # the same checks as one JSON document on stdout
kew doctor route-live-confirm --yes   # spends up to $0.02 on ONE bounded call
```

`kew doctor` itself is read-only and free. `kew doctor route-live-confirm` is
the single exception in kew — see [Live route confirmation](#live-route-confirmation).

## JSON output

`kew doctor --json` prints one JSON document on stdout and nothing else there;
the prose report is not printed. The exit code is the same as without the flag.

```json
{
  "schema": 1,
  "ok": false,
  "checks": [
    {
      "id": "allowlist-network",
      "status": "fail",
      "summary": "'kew-allowlist' is not present",
      "hint": "kew installs no firewall rules on it, so a plain bridge gives the container full egress until you restrict it yourself",
      "fix": "kew sandbox network",
      "details": [],
      "warnings": []
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `schema` | The document version, `1`. It changes only on a breaking change. |
| `ok` | `true` when no check is `warn` or `fail` — the same condition as a clean exit. |
| `checks[].id` | The stable check id from the table below. |
| `checks[].status` | `ok`, `warn`, `fail` or `skip`. `warn` and `fail` are both issues, so either fails the run: `fail` means the checked thing is broken or missing, `warn` means it works but needs attention. `skip` means the check could not run and is never a pass. |
| `checks[].summary` | One line saying what the check found. |
| `checks[].hint` | What to do about it, in prose, or `null`. |
| `checks[].fix` | One runnable command that fixes it, shell-quoted, or `null` when no single command does. Install links and other prose go in `hint`. A command that would spend money is never a `fix`. |
| `checks[].details` | Extra lines the check reported, such as each stale tag. |
| `checks[].warnings` | Advisory lines that need attention but do not change `status` — a passing check can carry one, such as the open-egress note on a plain-bridge `allowlist-network`. |

Every text field is credential-redacted before it is rendered, the same way
the prose report is, so a probe that echoes a token cannot leak it into the
JSON.

The human report renders from the same results: each check prints as
`<id>: PASS|WARN|FAIL|skipped (<summary>)`, followed by its details, its
`hint:` and its `fix:`, and the closing line names the ids of every issue.
Every check id below is reported exactly once, in the table's order. A check
that does not apply on this host, or that an earlier failure blocked, is
`skip` with the reason: the OpenShell checks on any runtime other than
`[sandbox] runtime = "openshell"`, the OpenShell settings when the gateway did
not answer, and the gcp host checks when `[hosts.gcp]` names no instance. A
check that itself crashes is reported as `fail` naming the error, and the
remaining checks still run.

In Python, `kew.cli.doctor.run_checks(config)` returns the same results as a
list of `kew.readiness.CheckResult`. It prints nothing and creates no file.
Every subprocess it starts has its own timeout, and all of them share one
overall deadline (120 seconds); a check not started before that deadline is
`skip`. It blocks while the probes run, so an async caller should run it off
its event loop.

## Checks

Every check id, as it appears in `id` and at the start of each report line:

| Check | What it verifies |
| --- | --- |
| `project-config` | A config file is in force, and which path. Without one kew runs on built-in defaults (`kew serve` included), so this fails with fix `kew init`, which writes `kew.toml` for the project. |
| `required-tools` | Every command-line tool kew shells out to resolves on `PATH`. A failure names the missing tools, with each one's install source as a detail. |
| `runner-credential` | Every runner a configured route resolves to has its container credential in the environment — for Claude Code, `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`. Containers cannot inherit a CLI login, so a missing credential fails, the hint names each variable and how to obtain one, and the fix is the `export` to run. The check reads names only and never reports a value. A runner that is not credentialed from the environment is `skip` with the reason. Codex's native posture runs on its own host login. An OpenShell gateway authenticates runners through its providers. |
| `gh-auth` | `gh auth status` succeeds. A missing or invalid login fails with the fix `gh auth login`. When `gh` cannot reach GitHub, the check fails with a different summary, because logging in again does not fix a network fault. |
| `containment` | The default route's sandbox resolves as a dispatch would resolve it, and names the posture it resolved to. A refusal fails with the stable refusal reason and its configuration fix. |
| `container-runtime` | The configured `[sandbox] runtime` binary is on `PATH`. It also verifies that `<runtime> info` responds within five seconds. A failure exits `1` and explains how to install a runtime or admit host execution. |
| `route-image` | The effective default-route image exists locally. The check applies `[sandbox.images]` before the stock `kew-agent:latest` fallback. It runs `<runtime> image inspect` and never pulls an image. A missing stock image prints the local build command. |
| `allowlist-network` | `[sandbox] network` selects `"allowlist"`. It also verifies that the local `kew-allowlist` network exists. The fix, [`kew sandbox network`](sandbox.md#kew-sandbox-network), creates only a bridge network. Kew installs no firewall rules on that network. Existence is not a boundary: when the network is a plain bridge with `Internal=false`, the check passes and then warns that egress is open unless the host filters it, and that kew cannot see host filters. |
| `agent-image` | The local sandbox agent image is at least as new as its build inputs. A stale kew-owned tag prints the rebuild command for the packaged runner it belongs to — Claude Code, Codex or OpenCode. A stale custom tag states that kew cannot rebuild it. |
| `retained-agent-tags` | The local runtime has no unprotected tags in the exact repositories of the packaged runner images — `kew-agent`, `kew-agent-codex` and `kew-agent-opencode`. This warning is separate from image freshness. |
| `reservations` | No pseudo-epic reservation (delegation or review) is pinned — a reservation whose run has ended (or whose dispatch never started) still holds a fleet slot. A slot leaks when `kew delegate watch` dies after dispatching but before releasing; the leaked slot counts against the fleet cap forever. This warns (exit `1`) only once the reservation has sat unreleased past a grace, so the healthy window between a run finishing and the watcher's next sweep is not flagged. The hint points at `kew epic run <epic>` (any driver tick sweeps them) or restarting the watcher. Skipped when there's no dispatch database yet; the database is opened **read-only** so the check never migrates it, and is skipped (not crashed) when the database is a newer schema, an older schema that predates delegation reservations, or otherwise unreadable. |
| `model-eol` | Checks every resolved route and pinned reviewer model against the [curated lifecycle registry](model-eol.md) — read-only and offline; no network call and no freshness claim. A model with no curated row is a `registry gap`; a model whose row is `untracked` is `lifecycle unverified` (covered, but no first-party lifecycle evidence has been reviewed yet); `watch`, `scheduled`, `retiring`, and `retired` are actionable states that warn with the shutdown date and replacement the registry records. All three are advisory and never block dispatch, though any of them counts as an issue for `kew doctor`'s own exit code — a `registry gap` or an actionable status is a prompt to update the registry or move the route in a reviewed PR. |
| `anchor-age` | How old the newest anchor timestamp in `audit-anchors.jsonl` is (the maximum, whatever the line order), against `[audit] anchor_max_age_days` (default 7 days). Chain verification cannot raise this alarm: a chain nobody has anchored for a month still verifies perfectly, so a dead anchor workflow is invisible to `kew audit verify` (#1408). Warns (exit `1`) with the timestamp the chain has been unwitnessed since. Skipped — never passed — when there is no anchors file, the file cannot be parsed, no anchor carries a readable timestamp, or the threshold is `0`. |
| `runner-liveness` | When the repository has a `RUNNER_TARGET` Actions variable, checks the registered self-hosted runners through GitHub and warns about any that are offline. This is skipped when the variable is absent or GitHub cannot be queried. |
| `route-live` | Reports the **stored** verdict for the complex route's exact execution boundary — runner executable or image ID, endpoint, credential path, and requested model, fingerprinted together. Read-only and free. Every other check on this page is static, so a boundary nobody has probed reads `live-unverified` (a skip, never a pass); only a probe that ran and *failed* is reported as an issue. |
| `runner-quota` | Reports a runner lane the provider refused for quota. When a run's stream carries a rejected rate limit (Claude Code's `rate_limit_event` with `status=rejected`, or the CLI's out-of-credits error), the run is recorded as `quota_exhausted` — not a failure: the item goes back to ready, the run row is `refused`, and the runner's lane is held until the reset time the provider gave (one hour when it gave none, never more than eight days). A different credential in the environment than the one the provider refused releases the hold at the next dispatch. While the hold stands, `kew epic run` dispatches nothing to that lane and reports the waiting nodes as `runner-quota-held`. This check reads the last rejection each runner left in the dispatch database — no provider call — and fails with the limit, its reset time and the agent token the provider refused (its env var names and fingerprint, never the value) while a lane is held, naming `kew quota clear <runner>` as the fix (see [`kew quota`](quota.md)). Skipped when there's no dispatch database. |
| `degraded-review` | Reads the hash-chained adversarial-review verdicts and warns when a reviewer runner/model route has produced three or more consecutive crash verdicts — `DEGRADED` (advisory review) or `UNPARSEABLE` (blocking review, where the node is also held). The route and count point operators toward launch, credential, or sandbox diagnostics. |
| `host-local`, `host-gcloud`, `host-gcloud-auth`, `host-gcp-instance` | Reports the configured host providers ([`[hosts]`](../configuration.md#hosts)). `local` is always listed: it is the default and needs no cloud account. When `[hosts.gcp]` names an instance, the gcp host is probed in dependency order -- the Google Cloud CLI is on PATH, it holds a usable login, and the instance is reachable -- and each failure prints the command that fixes it. An expired login is the common one and prints `fix: gcloud auth login`. A missing Google Cloud CLI has no command fix; its install link is the `hint`. The ids are `host-local`, then `host-gcloud` (the CLI is on PATH), `host-gcloud-auth` (the login) and `host-gcp-instance` (the instance answers). A probe blocked by an earlier failure is reported as `not probed`, never as a pass. Every probe is read-only: nothing is started, stopped or copied. |
| `shadow-db` | No `dispatch.db` in the working directory shadows the configured canonical store. A shadow misleads raw SQLite queries, which read it as empty or stale. The shadow is reported, never deleted. |
| `editable-install` | An editable install of kew is not behind `origin/main`. |
| `temporary-storage` | Reports apparent bytes for same-user pytest roots and recognized task storage. Recognized task children are `.venv`, `venv`, `uv-cache`, and `uv-tools` beneath `kew-*` temporary directories. It also measures the whole work root ([`[paths] work_root`](../configuration.md#paths)), which counts toward the threshold. The check reports explicit and default uv caches separately. It warns above 2 GiB of observed test, task and work-root storage. It inspects at most 100,000 entries for five seconds. Errors and limits produce an incomplete lower bound, reported as `skipped` — advisory, never a pass, and not an issue on its own. It never follows directory symlinks, reads file contents, or deletes data. |

Pytest retains only the latest completed failed-run directory.
Successful runs retain no test artifacts through normal pytest cleanup.
Active runs, abrupt termination, and preserved evidence can leave more data.
The uv default cache is reusable storage, not disposable temporary storage.

### Retained agent tags

This check inspects only local tags from the repositories of the packaged
runner images: `kew-agent`, `kew-agent-codex` and `kew-agent-opencode`.
Doctor derives that list from the packaged runner images themselves, so the
sweep widens with a new packaged runner rather than from a separate list.
Podman prefixes unqualified local build tags with `localhost`.
For Podman only, the check also recognizes the `localhost/` spelling of
each of those repositories as the corresponding local repository.
Other registry-prefixed names and prefix lookalikes stay outside its inventory.
Untagged images also stay outside this check.

The check protects these references:

- Every current packaged runner pin.
- Every `latest` tag.
- Every configured sandbox image reference.
- Images referenced by running or stopped containers.

The check compares immutable image IDs to recognize aliases.
It reports tag counts and image counts separately.
Each image identity contributes one logical size.
Logical sizes can share layers and do not measure physical storage recovery.

Failed or incomplete reads produce an `inventory unknown` warning.
That warning never includes a trusted stale-tag list.
Every `retained-agent-tags` warning exits `1`, including `inventory unknown`.
Check the configured runtime availability and permissions.
Inspect the inventory error reported by `kew doctor`.
Then rerun `kew doctor`.
These diagnostic steps do not change container runtime state.
The check runs for native routes when the configured runtime exists.
It never performs a live readiness probe.
It never queries a registry.

Kew does not remove images or tags.
If a current pin was removed, rebuild it with the matching command:

```bash
kew sandbox build --runner claude-code
kew sandbox build --runner codex
kew sandbox build --runner opencode
```

Each check reports one of three outcomes:

- **pass** — the check ran and found no problem.
- **stale / issue** — a problem was found; `kew doctor` exits `1` and prints a
  safe fix. Kew-owned agent tags get exact rebuild commands. Custom tags do not.
- **skipped** — the check could not run (e.g. the sandbox runtime isn't
  installed, the image isn't built locally, or you're not in a kew source
  checkout). Skipped checks are **not** counted as passing.

The three containment checks never skip silently. Each prints `PASS`, `WARN`,
or `FAIL` with a reason. A native route prints `PASS` because containers are
not needed on that route. An admitted host route prints `WARN` and exits `1`.
A fail-closed route prints its stable refusal reason and its configuration fix.

The containment posture is resolved exactly as a dispatch resolves it, which on
`[sandbox] runtime = "openshell"` includes reading the egress certification off
this project's `dispatch.db` — read-only, like every other ledger read here, so
a boundary `kew sandbox certify` has already proven is reported as proven rather
than as uncertified. A ledger that is absent or unreadable is said out loud and
leaves the boundary unproven, because "kew could not check" is not a pass.

## OpenShell runtime checks

On `[sandbox] runtime = "openshell"` nine further checks run. They read the
**running gateway** rather than a config file on this host, because the two can
disagree: a gateway somebody else's service manager started never read this
host's settings, so a file saying "telemetry off" proves nothing about the
process.

| Check | What has to be true |
| --- | --- |
| `openshell-gateway` | the gateway answered |
| `openshell-version` | the CLI **and** the gateway are `[sandbox.openshell] version` |
| `openshell-tls` | the gateway reports TLS on **and** the endpoint kew dials is `https://` |
| `openshell-telemetry` | the gateway has telemetry off |
| `openshell-image-pins` | the supervisor and sandbox images are pinned by digest |
| `openshell-providers` | the gateway holds every provider in `[sandbox.openshell] providers` |
| `openshell-agent-authority` | agent proposals are off and approval is manual |
| `openshell-landlock` | the container VM kernel reports Landlock ABI 3 or higher |
| `openshell-certification` | a fresh passing egress certification matches every mapped boundary |

Every per-runner check covers each runner in `[sandbox.openshell] providers`,
not only the default route's: a second runner whose policy drifted is a
boundary a dispatch can still select. A runtime with no mapped runner fails
those checks rather than passing them, because an unmapped runtime proves
nothing about any boundary.

A gateway kew cannot read fails as one `openshell-gateway` issue rather than as
eight separate ones — a read that did not complete says nothing about the
setting it was going to report. The failure names which of the two it was: the
gateway did not answer, or it answered with a report kew cannot parse. The
certification check is read-only, so bare `kew doctor` still probes nothing and
spends nothing.

No check compares the gateway's policies against this build's, because the
gateway holds none to compare: kew hands each sandbox its policy file at create
time. `openshell-certification` covers those bytes — the certification is bound
to the packaged policy's digest.

The setup these checks describe is written out in order in the
[OpenShell runtime runbook](../openshell.md).

## Exit codes

See the shared [exit-code scheme](exit-codes.md).

`0` — all checks passed or were skipped. `1` — at least one issue was found.

The same agent-image staleness check also runs (as a non-blocking warning)
during sandboxed dispatch, so a stale image is surfaced even if you never run
`kew doctor` directly.

## Live route confirmation

Every check above is static: it reads configuration, capability manifests,
images, and binaries. None of them can tell you that a turn actually completes
across the boundary a dispatch would launch. `kew doctor route-live-confirm`
is the one command in kew that spends money to answer that, and it is a
separate, explicitly confirmed subcommand precisely so nothing passive — bare
`kew doctor`, the web UI, or the TUI — can ever trigger a charge.

```bash
kew doctor route-live-confirm --complexity complex --yes
```

What one confirmation does, exactly:

- **One call, one attempt, no retry.** A single `reply exactly OK` turn across
  the resolved boundary. A failure is *evidence*, not something to try again.
- **Bounded.** One turn (`--max-turns 1`), a 90-second wall-clock cap kew
  enforces itself, and a conservative $0.02 planned ceiling declared before
  anything is spent.
- **Empty and credential-free.** The call runs in an empty host-owned git
  repository — never a customer checkout — with every tracker, forge, and
  publication credential stripped from the child environment by name. Only the
  inference credential travels, because that is the boundary under test.
- **Exact.** Evidence is filed under the fingerprint of the whole realized
  boundary. One character of drift — a rebuilt image, a moved binary, a
  different endpoint or model — is a different question and gets a fresh
  answer rather than reusing an old one.
- **Reused for 24 hours.** A repeat confirmation inside that window answers
  from stored evidence and spends nothing. `--force-probe` overrides it.
- **Accounted for honestly.** The spend lands on exactly one operational run
  row with `run_kind = readiness_probe` and no work-item identity, so it counts
  toward total and budget spend while being excluded from customer outcomes,
  epic economics, completion rates, and scorecards. A cost the runner does not
  report is recorded as *unknown* and folded in at the declared ceiling — never
  as $0.

### After a crash

If the process dies mid-probe, the lease is reconciled to `abandoned` and
charged at the declared ceiling, and whatever container or process group it
left behind stays listed as unacknowledged. The next confirmation **refuses**
rather than spending again: an expired lease means nobody reported an outcome,
which is exactly when resources are most likely still alive. Release them and
say so explicitly:

```bash
kew doctor route-live-confirm --reclaim --yes
```

### Options

| Option | Effect |
| --- | --- |
| `--complexity` | Routing tier whose exact boundary to confirm (default `complex`). |
| `--reclaim` | Release and acknowledge probes an earlier crash stranded on this boundary, then proceed. |
| `--force-probe` | Spend even when fresh evidence already answers. |
| `--yes` / `-y` | Skip the spend confirmation prompt. |

### Live confirmation exit codes

| Code | Meaning |
| --- | --- |
| `0` | Boundary confirmed live. |
| `1` | Completed probe refuted the boundary. |
| `2` | Probe refused; spend confirmation declined. |
