---
description: What kew's security hook blocks, where it runs, and how each runner is contained, since containment and not the hook is the boundary.
---

# Security

The security hook runs as a Claude Code `PreToolUse` handler on the **Bash**, **Write**, and **Edit** tools. In a Claude Code session it runs before each of those tool calls, even with `--dangerously-skip-permissions` enabled. It runs for Claude Code only. Other runners do not load it.

**This is defense-in-depth, not a security boundary.** The hook is a pattern match against known-destructive shell commands; it *catches* casual and accidental damage, it does not *prevent* a sufficiently motivated or prompt-injected agent from working around it. The actual isolation boundary is [container or runner-native containment](#sandboxed-dispatch-credential-posture) — run the guard alongside containment, not instead of it.

**Write/Edit protection (dispatched agents only):**

- Blocks every Write/Edit under any `.claude/` directory (hooks, settings, permissions, installed [skills](skills.md), and the guard script itself) with path canonicalization to prevent symlink/relative-path bypasses. The rule is keyed on the `AGENT_ASSIGNED_WORKTREE` marker, which kew sets on the agent's process environment. `kew setup` installs the same hook at the project root, so in your own interactive session (no marker) Write/Edit of `.claude/settings.json` or the hook is **allowed** — otherwise you could not edit your own hooks. Contained dispatch sets the same marker inside the container as the mount path (`-e AGENT_ASSIGNED_WORKTREE=/work`, #1691), so this rule and the cross-checkout ref rule apply to contained agents too. The Bash-side `.claude` write rules below apply in every session.

**Bash command blocking:**

Every row below is backed by an execution test in `kew/tests/test_security.py`
— the test runs the shipped hook against the command and asserts the denial —
or by the regression corpus that suite replays
(`kew/tests/fixtures/guard/main_blocked.txt`, every input the previous guard
denied). Every additional example input quoted in the rows is pinned too:
`TestDocsSecurityExamplesArePinned` extracts every backticked span from the
table below and fails unless that span is either executed against the shipped
hook with its documented decision or declared a shape fragment (#1686).

| Category | Shapes the guard denies |
|----------|----------|
| Force push | `--force`, `--force-with-lease`, `--force-if-includes`, a clustered short flag (`git push -uf origin main`), or a `+refspec`, anywhere in a `git push` segment — including behind `git -c` / `-C` / `--git-dir` / `--work-tree` / `--no-pager` |
| Destructive git | `git reset --hard`, `git clean -fd`, `git branch -D main`, `git checkout --orphan` |
| System destruction | `sudo …`, `chmod 777`, and the exact forms `rm -rf /`, `rm -rf /*`, `rm -rf .` (a subpath such as `rm -rf ./build` is not blocked) |
| Subshell / process substitution | `$(...)`, backticks, `<(...)`, `>(...)` |
| Indirect invocation | `sh -c "..."`, `bash -c "..."`, `eval …` |
| Base64 decode | `base64 -d`, `base64 --decode` |
| Credential exfiltration | `env` as the literal first word of a segment — bare, carrying only assignments, or launching a program (`env FOO=bar make`, `env python3 script.py`); behind a path or a wrapper only the bare form is caught, so `/usr/bin/env python3 script.py` and `command env make` pass — and `printenv` — including `\printenv`, `command printenv`, and a `printenv` reached through a `&`, `&&`, `\|\|` or `;` split — `echo $ANTHROPIC…`, `echo $GITHUB_TOKEN`, `echo $GH_TOKEN` |
| `/proc` environment reads | `/proc/self/environ`, `/proc/thread-self/environ`, `/proc/<pid>/environ` read by `cat less more head tail xxd strings cp dd od grep base64 tr`, by redirect — with a command word before it (`cat < /proc/self/environ`, `python3 < /proc/self/environ`) and with none at all (`< /proc/self/environ`, `exec < /proc/self/environ`, and the `0<` form, #1686; a two-digit fd prefix such as `10<` is not modelled), by `dd if=…`, or as a bare `environ` after `cd /proc/self` |
| Pipe into an interpreter | a pipe into `bash sh zsh dash ksh python python3 perl ruby node` — by name or absolute path, quoted, or behind `command` / `exec` / `nohup` / `time` / `env` / `xargs` — when the interpreter carries no program argument or names stdin explicitly (`-`, `-s`, `/dev/stdin`, `--init-file`). An older substring rule runs first and denies the raw text `\| bash`, `\| sh` or `\| zsh` — a name *beginning* with one of those, regardless of arguments — so `curl x \| bash ./script.sh` is denied, and so, collaterally, are `cat x.sh \| shfmt -d`, `cat x.sh \| shellcheck -` and `echo a \| shasum`. Shells outside both lists are **not** covered: `curl x \| csh`, `\| tcsh`, `\| fish` pass |
| `.claude` writes from Bash | any redirect whose target names `.claude`, and `cp` / `mv` / `tee` / `install` / `dd` / `rsync` / `ln` / `sed -i` naming `.claude` — matched on the wrapper-peeled command word — the same peel the pipe rule uses — so `/bin/cp`, `command cp`, `\cp`, `nohup cp` and `time mv` into `.claude/` are denied as well (#1692). Fail-closed on expansion: any `$` after a `>` in the segment, or a `$` anywhere in one of those commands, is denied unread (`echo x > $TMPDIR/y`, `cp a "$dst"`, `make > $LOGDIR/x`, and collaterally `awk '{ if (x > 1) print $2 }'`), because Bash expands the variable only after the guard runs. Applies in every session, not only to agents |
| File exfiltration | `curl -T`, `curl --upload-file`, `curl -d @file`, `--data-binary @file`, `--data-raw "@file"`, `--json @file`, `-F name=@file`, `wget --post-file`, `wget --body-file` |
| Cross-checkout Git ref mutation | `git commit`, `git reset`, `git checkout -B`, `git update-ref`, `git branch -f/--force` run outside the assigned `agent/issue-*` checkout, or targeting a ref outside `agent/issue-*`. Dispatched agents only, host-subprocess and contained: the rule is keyed on the `AGENT_ASSIGNED_WORKTREE` marker, which kew sets on the agent process and, as `/work`, inside the container (#1691); your own interactive session is not its subject |
| Custom patterns | Via `[security]` config: `blocked_prefix_patterns`, `blocked_exact_patterns` |

**Documented non-goals of the denylist.** The guard is a segment matcher, not a
shell parser, and the header of `security-guard.sh` lists what it deliberately
does not model: comment and quote masking, heredoc bodies, backslash-newline
continuation, shell expansions, `eval` and interpreter *payloads*, `sh -c`
payload inspection beyond the prefix list, reserved-word heads, `xargs -a`,
wrappers outside the documented list, subshell/group forms without an inner
space (`(cmd)`, `{cmd;}`), options on the listed wrappers (`command -p`,
`timeout -k 1 5`), `NAME=value` assignment prefixes, `/proc` environ reads
through readers outside the listed set, and interpreters outside the pipe
rule's fixed list. Each of those shapes needs a real parser. Treat anything not
in the table above as **not blocked**, and rely on containment for it.

If the hook blocks a command, Claude sees a denial message and adjusts its approach. Your existing hand-managed hooks are preserved (the generator only overwrites files it created).

Dispatches also pass the assigned checkout to the managed guard and compare the
operator checkout's HEAD and base ref before and after the run. A movement is
always recorded as a `root_checkout_integrity_violation` audit event with the
before/after values. A run that has published nothing is failed closed and its
checkout preserved for incident review; a run that already published a PR keeps
that outcome and its labels and is commented instead (GitHub items; a Linear item keeps the audit event only), because the incident
cannot un-publish work that already reached the remote (see
[Operations](operations.md#operator-checkout-integrity-incidents)).
The marker is a path, not a credential. When it is absent the rule does not
apply — an interactive session commits freely. Contained dispatch sets it to
`/work`, the container path of the mount (#1691).

See [Audit log & tamper-evidence](audit.md) for the hash-chained audit log and its honest limits.

Custom patterns are configured via the `[security]` section in `kew.toml`:

```toml
[security]
blocked_prefix_patterns = ["kubectl delete"]
blocked_exact_patterns = ["DROP DATABASE"]
```

The guard is managed through [`kew skills`](cli/skills.md) like any other shipped artifact (`kew setup` installs it initially). `kew skills update security-guard` re-renders the hook from your current `kew.toml`, so custom patterns are re-applied rather than lost — see [Guard updates preserve your config](skills.md#guard-updates-preserve-your-config).

## Untrusted content in agent prompts

Every flow renders attacker-reachable text into the agent's prompt. Kew
fences these fields and asks the agent to treat them as data:

- the issue body (dispatch);
- PR review comments, with their file paths and authors (review);
- the CI failure log (ci-fix);
- the list of conflicted paths (resolve);
- the issue title (dispatch and ci-fix), fenced inline on the line it renders
  in. The dispatch prompt renders it once, in the issue heading; the PR title
  and the Dispatch Report line point back at it;
- the repo map, when `include_repo_map` is on (dispatch);
- the recently merged PR context (dispatch);
- `diff_summary` (ci-fix), when a caller supplies one;
- the review prompt's merge-conflict section: the head and base branch names
  and every conflicted path, each fenced inline.

Other attacker-reachable text is interpolated **unfenced**:

- the PR's head branch name (review, resolve, ci-fix) and base branch name
  (review, resolve), outside the review conflict section. Kew reads them from
  the PR, and the prompt tells the agent to `git push origin <branch>` with
  the head name.

An approved prompt profile (for dispatch, review remediation, resolve, CI fix
or the adversarial reviewer) also renders **unfenced**, as operator instructions, but it is not
attacker-reachable: it is proposed and activated only through the approver gate
(`[approval].approvers`; while it is empty every profile decision is refused), every
decision is a chained audit event, and kew reads it from the host's
`dispatch.db` at launch, never from the checkout. Each role renders only its own
profile, and its run records the version it ran under. For dispatch and the
fix-up roles, the safety rules and the publication section render after it,
and each says it takes precedence over it. For the adversarial reviewer, the
profile renders before the whole reviewer rubric, followed by the host-owned
sentence "The review rules below take precedence over the operator
instructions above." The rubric, the diff boundary, runtime guidance and
acceptance criteria all follow it, so no review rule sits above the profile
for it to override. A proposal with any line that could open a Markdown block
(a ```` ``` ```` or `~~~` fence, or `<` at the line start), or with an unclosed
`<!--`, is refused and the refusal names the line. Inline `` `code` `` is
allowed. A paste mistake therefore cannot carry the safety rules into a code or
HTML block. An active profile that fails the same check at launch (one approved
before the rule) is a `prompt_configuration_refused` and never renders. Any custom
`[prompt] template` is refused while a profile is active, so a run never
records a profile its agent did not receive. In review remediation, resolve
and CI fix that refusal starts no runner and records a `refused` run under the
same category (a review's `review.outcome` says `refused`, not `failed`), and
the epic node is held with the reason, not retried: a remediating node, or a
red-CI node under `auto_ci_fix`, on an `admission_refused` hold, and a node
whose base integration was refused on a `base_integration_failed` hold. Each
clears with `kew epic unhold` once the profile is retired or replaced.

The trust rule in full is on the [prompt profiles](prompts.md) page: a profile
is an addendum, never a replacement; it is written only through the
approver-gated service layer; and a contained agent cannot change one, because
kew never reads a profile from the checkout, a worktree, the environment or
`kew.toml`. An uncontained host run has the operator's privileges and can edit
the ledger itself ([checkout binding boundary](#checkout-binding-boundary)), so
profiles assume containment.
`GET /api/prompts/<role>` previews the active profile where the launch
renders it, and its `variant.profile` names the version (`v3`), says `none`, or
gives the launch's refusal (`refused: <reason>`) over the base prompt. The
dispatch preview checks the served `[prompt] template` too, so a profile the
launch would refuse beside a custom template previews as refused. It renders
the served `[prompt]` as the launch does, and a `[prompt]` the launch's
composer refuses (an append or a custom template under host publication)
previews the base prompt with `variant.refusal` saying why, and the profile
state beside it. The console's
Prompts page says which of the three the preview is, and says it beside a
launch refusal too.

The console can also propose, approve, decline and retire a profile on that
page. The four writes (`POST /api/prompts/<role>/versions`,
`…/versions/<N>/approve`, `…/versions/<N>/decline` and `…/retire`) need a
session and the CSRF header like every console write, and then the same
approver gate as the CLI. An approval or a retirement sends the role
generation the page showed. If another decision changed the role after that,
the write answers 409 and changes nothing. A server started without a
configuration refuses every profile write, because it has no approver list
to check.

`GET /api/prompts/<role>/versions` says in `can_decide` whether the acting
name passes that approver gate. It is false for everyone when no
`[approval].approvers` are configured, or the server has no configuration.
Every write passes it, Propose and Retire as
well as Approve and Decline, so the page offers a write only when `can_decide`
is true. Otherwise it turns every write off and prints the reason once, beside
Retire: the server's own refusal from `decide_refusal` when nobody can pass
the gate (no `[approval].approvers` in kew.toml, or no configuration at all),
or "Only an approver can change a profile." for a viewer who is not an
approver. The flag only decides what the page draws: every write is still
gated on the server. After a 409 the page turns off every write until a Reload
reads the history and the preview at one generation, and each pending version
reads "Reload to decide." in place of Approve and Decline until then. Before it
approves a version older than the active one, it asks you to confirm.

`GET /api/prompts/<role>/outcomes` is a read behind the same guard as the
preview. It reports, per profile version, what kew already recorded about the
role's runs: first-pass approval, rounds to READY and merged cost for an
implementer role, and the verdict mix for the reviewer. Merged cost is the
whole item's cost: every run on the item, review and remediation runs
included, whatever its prompt version. A run's outcome window starts when the
run launched if its launch receipt reached `started`, and when it was admitted
otherwise, and ends at the launch of the item's next run of that role. It
writes nothing.

The fenced fields get these controls:

- **Both safety blocks in every flow.** The "treat as data" preamble and the
  "NEVER `gh pr merge` / `gh pr review --approve` / add merge labels" rule
  render in all four templates, not just dispatch.
- **Per-render random sentinel.** Each fenced body sits between two
  copies of a sentinel drawn from a CSPRNG at render time
  (`<<<KEW-UNTRUSTED-…>>>`). Because the content author cannot predict the
  token, they cannot forge a closing delimiter to break out — the previous
  static HTML-comment markers were spoofable. An issue body, comment body,
  CI log, or conflicted-path list that already contains the sentinel is
  rejected. The other fenced fields (title, repo map, merged context, diff
  summary, review conflict section) use a token derived from the SHA-256 of
  the field's own text instead, so the repo map and title above the issue
  body keep the prompt's prefix byte-stable; a content author cannot embed the
  hash of their own text in that text.
- **Inline fences are named in the Safety Rules.** A one-line field (the
  issue title, a branch name or a conflicted path) sits between two copies
  of its token on the line it renders in, with every line break in it,
  Unicode separators included, collapsed to a space. The "treat as data"
  preamble describes a block between two sentinel lines, so the Safety Rules
  of each prompt that renders an inline fence (dispatch, ci-fix, review)
  also say that text between two identical tokens on one line is untrusted
  data. Where the dispatch agent creates the PR itself, it is told to pass the
  issue title in single quotes, not double quotes, so the shell cannot expand
  anything in it.
- **Reserved fence vocabulary.** The token is unguessable but its
  `<<<KEW-UNTRUSTED-` prefix is public, so content that spells a
  sentinel-*shaped* marker of its own has that marker replaced with
  `[kew: sentinel-shaped marker removed]`. This applies to the fenced bodies
  and to the unfenced slots above — each template strips the vocabulary from
  every value it interpolates outside a fence, so a field that is not fenced
  cannot open a fence either. The boundaries in a rendered
  prompt are therefore the host's and only the host's, which is what lets kew
  tell its own text apart from quoted text afterwards. A typed shape is
  neutralized rather than rejected, so a hostile title or body cannot block
  its own run.
- **Explicit caps and markers.** Bodies are capped with an explicit truncation
  marker; an empty body renders `(no spec provided)` rather than a blank, so a
  missing spec is never mistaken for an intentional one.

This is defense-in-depth for the prompt layer.
The posture record names the effective command guard and containment boundaries.

## Sandboxed dispatch: credential posture

Containment is the layer above the in-process guard. Its default is `[sandbox] enabled = "auto"`. kew accepts a runner-native boundary declared in runner code. Otherwise, it uses the configured container runtime. If no runtime is available, kew refuses the run. `[sandbox] enabled = false` also refuses unless `[sandbox] dangerously_skip_containment = true` is set. That escape hatch runs the agent on the host without a container boundary. The first container run announces `Sandbox: on (<runtime>)`.

Each dispatch attempts to record its effective posture in `sandbox_resolved`.
Every record carries its `attempt_number`.
The record names the whole effective boundary:

- `filesystem_boundary` names the container mount, native workspace, or host.
- `network_boundary` records the observed egress classification.
- `egress_verification_basis` records `operator-asserted` for direct contained routes.
- `credential_names` records a names-only credential exposure bound.
  Contained runs list injected credential names.
  Host and native runs list every parent non-`KEW_*` name.
  Publication and runner filters can remove names before launch.
- `command_guard` records `loaded` only for a configured executable hook.
  The hook bytes must match the trusted project copy.
  Other values are `absent` or `not-applicable`.
- `admission` distinguishes policy from explicit dangerous host admission.
- `reason` retains a refusal cause.

`loaded` requires Claude Code and a target checkout with the trusted hook.
Other runners record `not-applicable` for the Claude command guard.
Sidecar lifecycle writes require durable acknowledgment before execution.
Other audit writes remain best-effort.
A failed best-effort write leaves no posture record.
`kew status --run`, Kew Serve, the TUI run view, and evidence packs read this event.
They never substitute requested sandbox configuration.
They select the newest event belonging to the latest durable run attempt.
File stores read rows and posture events through a separate read-only connection in one committed snapshot.
DTOs and evidence packs use the matching row from that snapshot.
An event cannot certify a different row attempt.
Each selection reads the newest event and at most 32 fallback events.
Test-only memory stores verify the row with at most three passes and suppress evidence during open transactions.
They report pending when retries keep racing.
Legacy events without `attempt_number` belong only to attempt one.
They reject certification from earlier attempts, including later appends carrying an older attempt number.
A missing current-attempt event renders pending posture and unavailable egress.
An absent historical record renders `posture: not recorded`.
The TUI refreshes selected posture on its timer, even when selection stays unchanged.

Native containment is not configurable. A runner's `containment_posture`
protocol method must declare it in code, so TOML cannot promote an arbitrary
host process to a trusted boundary. Official Codex currently declares `native`
on every runtime but `openshell`: the fixed CLI launch uses `workspace-write`,
disables command network access, and grants no extra writable roots. Native
audit events carry no container runtime or image.

The declaration is read per runtime, because "is the runner's own boundary the
one kew should accept" has a different answer where kew holds a boundary of its
own. Codex declares `native` on `docker` and `podman`, where kew has no lane
for its agent image, and declines it on `runtime = "openshell"`, where kew
creates the sandbox under a packaged egress policy and the credential never
enters it. Configuration still cannot grant a posture — it only selects which
boundary is on offer, and the runner answers.

"On offer" is per dispatch, not per config file. A run kew cannot actually put
inside the selected runtime is offered no boundary at all, so the runner is
asked the plain "do you contain yourself" question and keeps its own posture:

- a run the runtime has no launch path for — a remediation flow on `openshell`,
  whose agent is a container command line the gateway cannot take, or a sidecar
  Switchyard topology;
- a runtime that is not installed on the dispatching host. A name in the config
  file is a request, not a boundary, and there is nothing absent software can
  hold;
- a preserved-checkout continuation, which must run against its existing host
  worktree on every runtime;
- any runtime under `[sandbox] enabled = false`.

A runner holding no boundary of its own still hears the refusal that names the
gap in each of those cases — `runtime_unavailable` for a missing runtime,
`continuation_host_only` for a continuation. An uncertified OpenShell boundary is
**not** on this list: that boundary exists on the host and the operator can prove
it, so a runner that declines its own posture there is refused rather than
quietly answered with a host run.

When a dispatch is sandboxed, kew enforces the following credential posture:

| Credential | Where it lives | Container sees it? |
|---|---|---|
| GitHub token (`GH_TOKEN` / `GITHUB_TOKEN`) | Host-only — used by the canonical repository and PR creation | No |
| The **resolved runner's own** provider credential | Forwarded **by name only** (`-e ANTHROPIC_API_KEY`, never `-e NAME=value`); the value comes from the launch environment | Yes (intentional) |
| Any **other** runner's provider credential | Host env — deliberately not in this container's allowlist | No |
| `dispatch.db` / audit log | Host filesystem, outside the mount | No |
| `~/.claude` config and hooks | Host filesystem, outside the mount | No |

Current inference credentials have these route-specific controls:

| Current route | Credential reusable after the run? | Kew bounds issuer spend and expiry? | Compensating control |
|---|---|---|---|
| Contained claude-code with `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` | Yes | No | The container receives only the runner credential. Publication stays host-only. Exposure lineage blocks publication. Direct egress is operator-asserted. Kew permanently records `operator-managed-unverified`. |
| Native Codex ChatGPT login | Yes | No | The native `workspace-write` boundary disables command network access. It restricts writable paths. |
| Direct OpenRouter with `OPENROUTER_API_KEY` and host `OPENROUTER_PROVISIONING_KEY` | Until revocation or expiry, within its spend limit | Yes — per-run key with a finite no-reset limit and a UTC expiry, revoked at run end; identity recorded, value never | The host issues and revokes the key. Publication waits for recorded successful revocation. Direct egress is operator-asserted. Kew permanently records `operator-managed-unverified` for contained routes. |
| Direct OpenRouter without `OPENROUTER_PROVISIONING_KEY` | No ambient OpenRouter credential enters a child | No | Direct routes refuse before launch, contained or on the host. Host children also lose ambient OpenRouter credentials. |
| Switchyard sidecar synthetic per-attempt values | Synthetic worker value: no. Proxy provider credential: yes. | No | Kew substitutes a per-attempt synthetic client value. The isolated worker network requires packet certification and filter readback. |

Direct OpenRouter runs require a bounded key, contained or on the host.
Create an OpenRouter management key at [Management Keys](https://openrouter.ai/settings/management-keys).
Set it as `OPENROUTER_PROVISIONING_KEY` in the host environment before running Kew.
Missing provisioning causes `OpenRouter provisioning key required for direct routes`.
The pre-launch refusal returns exit 2, records a `refused` run row, and releases the reservation.
Every container environment removes the ambient `OPENROUTER_API_KEY` by name before explicit bounded-key injection.
This removal also covers native and sidecar routes without an issued key.
Uncontained/native host launches also remove ambient OpenRouter credentials.
Detached issuance applies only to direct OpenRouter routes.
The parent resolves the item's runner and inference transport before issuance.
Native Claude, native Codex, and Switchyard routes require no bounded key or provisioning credential.
Switchyard sidecars remain excluded when their upstream route uses OpenRouter.
These routes receive no issued-key marker and use their own authentication sources.
Issuance and refusal events record the route decision in `credential_route`.
The driver mints each detached run key before spawning `kew epic _dispatch-node`.
This includes delegation runs.
Children never hold management authority.
Every launcher removes `OPENROUTER_PROVISIONING_KEY`, including launchers for Kew itself.
The parent supplies the bounded secret and `KEW_ISSUED_RUN_KEY_HASH` only for direct OpenRouter.
The parent passes `--credential-route <digest>` with `--run-id` to each detached child.
The SHA-256 digest covers the resolved runner, model, transport, sidecar flag, and bounded-key decision.
Transport distinguishes direct inference, native authentication, and sidecar inference.
External Switchyard transport retains its separate transport name.
The parent records `credential_route_digest` in the issuance or exclusion event.
The child records the same digest in its admission evidence.
The child checks the digest before runner preflight and at the actual launch resolution.
Changed labels or routing overrides cause `credential route changed`, exit 2, and reservation release before launch.
An excluded handoff keeps only the resolved route's required credential names; the names are part of the route digest.
Excluded handoffs remove all other inference names, the issued-key marker, and management credentials.
Native Claude selects one authentication mode from the parent's environment.
The route evidence records `claude_auth_mode`; its value and credential names enter the digest.
A parent/child mode mismatch causes `credential route changed` before launch.
Only `1` or `true` enables a cloud selector, ignoring case.
Kew refuses conflicting Bedrock and Vertex selectors.

| Claude mode | Retained environment names |
|---|---|
| Anthropic API | `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_BASE_URL` |
| Bedrock | `CLAUDE_CODE_USE_BEDROCK`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_REGION`, `AWS_DEFAULT_REGION`, `AWS_PROFILE`, `AWS_BEARER_TOKEN_BEDROCK`, `ANTHROPIC_BEDROCK_BASE_URL` |
| Vertex | `CLAUDE_CODE_USE_VERTEX`, `GOOGLE_APPLICATION_CREDENTIALS`, `CLOUD_ML_REGION`, `ANTHROPIC_VERTEX_PROJECT_ID`, `GOOGLE_CLOUD_PROJECT`, `ANTHROPIC_VERTEX_BASE_URL` |

These sets follow Claude Code's [environment reference](https://code.claude.com/docs/en/env-vars).
Cloud names follow its [Bedrock](https://code.claude.com/docs/en/amazon-bedrock) and [Vertex](https://code.claude.com/docs/en/google-vertex-ai) guides.
Kew already uses `ANTHROPIC_BASE_URL` for endpoint routing.
No keep set includes management or publication credential names.
These handoffs do not mount cloud credential files into containers.

Native Codex keeps no inference credentials and uses its own login.
Switchyard keeps its resolved upstream `credential_env_names`, which its supervisor validates against the route configuration.
These names can include `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY`.
Credential values are never part of the digest.
A detached direct OpenRouter child requires a verified issued key.
Missing or invalid issued credentials cause `issued credential mismatch` and exit 2.
There is no fallback to the operator's `OPENROUTER_API_KEY`.
The child identifies detached execution from `--credential-route` or `--run-id`.
The child verifies the issuer hash and secret fingerprint against that run's durable issuance event.
The child records phase `inherited` and neither mints nor revokes the inherited key.
A marker on any excluded route also refuses inheritance.
A mismatch refuses launch with `issued credential mismatch` and publishes nothing.
Missing provisioning or failed issuance refuses direct OpenRouter detached dispatch and releases its reservation.
The parent never substitutes the host inference key after an issuance failure.
Generic helpers strip the bounded key and marker when the dispatch process holds an issued-key marker.
Only explicit dispatch handoff, agent launch, and container launch receive the issued key.
Container arguments forward the key by environment name; helpers such as `docker inspect` receive no bounded key.
Agent, container, and discovery probe children never receive the provisioning credential.
The value never enters argv, logs, or audit records.
The issuer sends `limit`, `expires_at`, `limit_reset: null`, and `include_byok_in_limit: true`.
The spend limit uses the effective per-run cap from budget preflight.
Remediation uses its budget override. Adversarial review uses `review.max_review_usd`.
Without a per-run cap, Kew uses the configured daily remaining budget.
Expiry uses UTC and covers the complete bounded retry schedule.
Lifetime is `attempts * timeout + 1.25 * sum(backoff intervals) + 300 seconds`.
A driver-issued key for a child that may run contained covers the longer of that schedule and the four-hour container wall clock plus one attempt's timeout.
Tracked runs use `retry.max_attempts` and the effective per-attempt timeout, including overrides.
Backoff intervals are `retry.backoff_base_seconds * retry.backoff_multiplier**i`, for `i` from zero through `attempts - 2`.
The factor 1.25 covers maximum retry jitter. The final 300 seconds provide cleanup grace.
The shared container streamer has one four-hour attempt and no retry backoff.
Kew refuses launch when the remaining retry schedule exceeds the remaining key lifetime.
Kew also checks each attempt's timeout immediately before process execution.
Nonfinite or unrepresentable lifetimes are refused before issuance.
Retries share one key and one total spend ceiling.

The route profile records public issuer identity and bounds.
Issuance events also record the secret's SHA-256 digest.
A `sandbox_resolved` revocation event records `credential_issuer.revoked` and a safe failure reason.

Issued keys use the `issued:openrouter:<hash>` authentication family.
Launch requires a `sandbox_resolved` issuance event for the same run id and attempt.
Its `credential_issuer` identity must contain issuer `openrouter` and the matching `key_hash`.
Missing or mismatched issuance evidence refuses launch with `issued_key_evidence_missing`.
The publication gate checks the same issuance evidence again.
Canonical and checkout publication require revoke-before-publish evidence: a matching revocation event with `credential_issuer.revoked=true`.
Missing or failed revocation holds publication with `issued_key_unrevoked` and the issuer's reason.
Missing revocation evidence uses reason `revocation evidence missing`.
A later retry releases publication after successful revocation evidence arrives.
The publication gate reads evidence; it never calls the issuer.

The host-authenticated issuance record itself is the durable revocation obligation.
Recorded issuance survives a crash before the first DELETE marker.
Before POST, `mint_run_credential` commits an issuance INTENT in the existing meta rows.
The `run_key_intent:<run_id>` row records run id, deterministic key name, requested limit, expiry, issuing pid, and timestamp.
New deterministic names use `kew-run:<ledger_id>:<run_id>`.
The ledger ID is the canonical UUID bound in the store’s `ledger_identity` table.
Recovery uses the exact name stored before issuance.
The issuer reply's `data.name` must exactly equal the qualified name persisted in the issuance intent.
Missing, unqualified, differently qualified, and foreign-ledger names raise `MalformedIssuerReply` before any credential reaches a caller.
Malformed replies with validated hashes enter durable revocation before the error propagates.
Cleanup records confirmed deletion or absence; unconfirmed outcomes remain pending for hash-based retries.
Issuance without a bound ledger identity is refused with `issuer refused`.
After the response, kew writes the pre-launch marker, records issuance evidence, then settles the intent.
Issuance evidence uses a strict append-and-readback operation before kew yields any issued key.
Readback must match the event ID, run ID, host actor, key hash, bounds, and secret fingerprint.
Missing or mismatched evidence raises `IssuanceEvidenceRefused` before review or producer launch.
The run is refused, the operator receives a safe reason, and the intent remains pending for exact-name recovery.
A crash before the marker leaves the intent open, even when POST never happened.
Recovery waits until the requested expiry before looking up an unresolved issuance.
A pending run row remains protected.
Reconciliation scans one bounded page of intents alongside the lifecycle scan.
Recovery uses paginated management GET requests, includes disabled keys, and matches the exact key name.
Kew does not assume an undocumented idempotency API or replay POST.
Matching keys require DELETE with confirmed deletion or absence before settlement.
Absence settles an intent only after a negative lookup strictly after the requested expiry.
A local inactivity timeout does not cancel server work or prove list consistency.
Before expiry, negative lookups keep the intent pending.
Retry delays grow through 30, 60, 120, 240, and 300 seconds, then remain capped.
Unsupported or failed lookup leaves the intent open and emits one warning naming the run.
Expiry alone never proves deletion.
The issuer rejects use after `expires_at`; a credential materializing after that bound is unusable.
This relies on the issuer's expiration rule, not a client cancellation assumption.
See [OpenRouter's expiration field](https://github.com/OpenRouterTeam/terraform-provider-openrouter/blob/main/docs/data-sources/api_keys.md).

Each reconciliation pass also lists issuer entries with the `kew-run:` prefix, including disabled entries.
The sweep waits 300 seconds after issuer `created_at` before deleting an unowned entry.
Pending intents, matching pre-launch markers, and matching issuance records preserve local ownership.
Ownership requires the listed name to equal the persisted qualified name; matching hashes alone do not establish ownership.
A closed absence intent cannot protect a later-visible entry.
The sweep uses the same durable deletion retries and confirmed revocation events as normal recovery.
Malformed list metadata never authorizes deletion.
Projects may share one management account because each ledger has its own namespace.
Sweeps and intent recovery act only within the current ledger’s namespace.
Foreign, unqualified, and malformed names are skipped before ownership checks.
Each sweep logs one skipped count at debug level without credential names.

Generic children always lose both OpenRouter credentials, including before any bounded scope exists.
This rule covers discovery, control, git, gh, container control, ps, and timeout calls.
Host agent launches also remove ambient OpenRouter credentials.
A timeout wrapper executing that agent belongs to the same launch boundary.
Only explicit issued credentials enter these child environments.
Contained agent and sidecar launchers inject only their explicitly issued inference credential.
Management authority never enters a child environment.
Retained image, smoke, and staleness diagnostics redact credential values before truncation.
Recovery requires a terminal run row or explicit terminal evidence for an absent row.
Absence of a run row is never treated as terminal by itself.
The pre-launch window starts when the parent prepares a key and ends when the child writes its pending row.
Before issuance evidence, `mint_run_credential` commits a durable `run_key_prelaunch` marker in the existing meta rows.
The marker records run id, key hash, issuing process pid, issuance time, and `expires_at`.
The pending row committed by `save` or `try_admit_run` supersedes the marker.
A first terminal row also supersedes the marker and permits reconciliation.
The retained marker protects the key if someone later removes its run row without terminal evidence.
Driver cleanup closes prepared-but-unspawned keys, including Popen and environment-construction failures.
Cleanup writes a pre-launch tombstone before DELETE and records the confirmed outcome.
Successful Popen transfers key ownership to the child; driver exit preserves that key before admission.
For absent rows, recovery requires a reaping tombstone, a pre-launch tombstone, or key expiry.
A hard driver death before Popen leaves the marker protected until expiry.
After five minutes, one operator warning lists the waiting run, key hash, issuing pid, and expiry.
Skipped pre-launch keys also produce debug logs; reconciliation never probes process liveness.
Recovery never revokes a pending run, even after key expiry.
Each DELETE first persists a durable `revocation_pending` marker and a safe reason.
The shared reconciler handles foreground keys and driver-issued keys.
Driver ticks, driver exit, and delegation watcher passes run the same reconciler.
Foreground credential finalization and publication retries also use this machinery.
An `issued_key_unrevoked` publication retry first reconciles the issued key, then checks the evidence again.
The check covers every producer attempt and ancestor run.
Predecessor publication jobs use the same reconciliation path.
Each pass attempts at most one DELETE per key hash.
Retry delays start at 30 seconds, double, and stop increasing at 300 seconds.
The durable retry counter saturates at 32; retries continue until the issuer confirms deletion.
Repeated failures retain the pending reason without filling the publication evidence window.
An HTTP 200 response must contain `deleted=true`; HTTP 204 also confirms deletion.
HTTP 404 or an explicit key-absence response records `revoked=true` with reason `already absent`.
Timeouts, network errors, HTTP 429, HTTP 5xx, and malformed responses remain pending.
HTTP 401 or 403 records `invalid provisioning credential` and emits one operator warning per key.
Replace `OPENROUTER_PROVISIONING_KEY` in the host process environment, then retry publication or restart the driver.
Other refused responses remain pending with the safe issuer reason.
Key expiry alone never releases publication.
After expiry, confirmed absence uses the same `already absent` path and releases the hold.

Raw ambient `env:` credentials publish only a head the host scanned clean immediately before the push.

Detached key revocation runs on the next driver tick after terminal status or pending-row reaping.
The reconciler reads at most 100 lifecycle events per page and uses indexed run lookups.
Large backlogs can require additional ticks.
Successful revocations are skipped; failures retry at most once per hash per tick.
Driver exit also attempts reconciliation, including exceptional exits.
Active detached children keep their keys when the parent exits.
The standalone delegation watcher reconciles terminal and reaped runs once per pass.
Watcher exit reconciles again, including exceptional exits, while preserving running children.
A shared seen set prevents duplicate revocation attempts within one pass and its exit cleanup.
If no driver or watcher returns, expiry is the backstop for key use; publication stays held.
The parent records issuance with `issued_by="driver"` and revocation through `sandbox_resolved` events.

Foreground runs attempt revocation on success, failure, timeout, interrupt, and breaker stops.
A host SIGKILL cannot execute cleanup.
The next reconciliation uses the issuance record to confirm deletion or absence.
For uncontained/native host runs without provisioning, `credential_issuer` is null with reason `no provisioning key`.
Other routes retain their explicit No rows. Switchyard retains its separate upstream credential owner.

Publication-bearing contained work has an additional credential gate. Each
producer attempt must have an immutable `run_route_profile`. A contained
profile with `auth_path = "env:<name>"` records that the worker held a reusable
provider credential. A `native` profile records a runner that can read host
credential files. Kew admits both launches. Before a push, the gate admits
their output only when a clean [publication output scan](#publication-output-scan)
is recorded for the publishing run and that exact head. Kew also checks the
profile evidence before commit creation, push, pull-request creation, and
publication reconciliation.

The check covers every producer attempt and its bounded continuation ancestry.
A later retry, native remediation, or generic review cannot stand in for the
scan of the head being pushed. Known-contained legacy runs without profiles fail closed.
Protected attempts with missing authentication evidence also fail closed.
Ambiguous, cyclic, or unreadable ancestry fails closed. The terminal category
is `credential_publication_refused`.

Continuation ancestry uses retained `continuation_claimed` audit payloads.
Kew validates every generation in one exact continuation family.
Missing generations, conflicting claims, invalid identities, cycles, and
bounded-query overflow fail closed.

Normalized publication jobs also retain predecessor history.
Kew checks every predecessor producer before each commit or remote effect.
Review remediation and conflict resolution bind the observed remote head to a
unique publication job when that evidence exists.
Known-contained branches without a unique head binding fail closed.
A newer run on the same branch cannot replace that head binding.

Direct container routes with ambient keys use `env:<name>` provider authentication.
Their output publishes only after a clean scan of the pushed head.
Issued OpenRouter routes require matching issuance and successful revocation evidence before publication.
All other publication checks still apply.
Read-only review, planning, readiness, and non-publication local work remain available.

Ordinary host rows need no scan record to publish, but every push of their
output is still scanned and a finding refuses. Native rows need the clean scan
record, like `env:` rows. The scan does not certify host credential
inaccessibility.

### Publication output scan

Before every push of agent output, the host scans the exact commit it is about
to push. That covers canonical publication of fresh dispatches, loop
iterations, review remediation, CI fixes, and conflict resolution; the host
republish of an agent-pushed review head; the checkout escape hatch; and the
normalized publication worker.

- **Scope:** every object the push transfers, enumerated with
  `git rev-list --objects <head> --not <tips the remote already has>` and read
  as raw bytes: every blob (including one a later commit removed), every tree
  (file names), and every commit whole (message, author and committer names and
  emails, encoding, signature, any other header). In a merge, the merged parent
  is an excluded tip. Canonical publication and the republish of an
  agent-pushed review head push one host-built commit with a host message and
  identity. The checkout escape hatch pushes the agent's own commits: it pushes
  exactly the scanned commit id, no tags (`--no-follow-tags`), and its excluded
  tip is the trusted base or the remote head as the remote reports it, never
  the checkout's own `origin/<branch>` ref. The scan and that push run git with
  replace objects and grafts ignored (`GIT_NO_REPLACE_OBJECTS`,
  `core.useReplaceRefs=false`, an empty `GIT_GRAFT_FILE`) and with no hooks or
  fsmonitor, so an agent-written `refs/replace` ref cannot show the scan a
  decoy. Alternates and a shallow file in the checkout are read the same way
  by the scan and by the push.
- **Values:** an exact set of names, resolved from the host environment:
  every runner's declared credentials (`ANTHROPIC_API_KEY`,
  `CLAUDE_CODE_OAUTH_TOKEN`, `OPENROUTER_API_KEY`), the publication and
  provisioning credentials kew holds (`GH_TOKEN`, `GITHUB_TOKEN`,
  `KEW_DETACHED_PUBLICATION_TOKEN`, `OPENROUTER_PROVISIONING_KEY`), and any
  issued run key still registered. The host's Codex CLI login file
  (`$CODEX_HOME/auth.json`, else `~/.codex/auth.json`) adds its secret fields
  as values: `OPENAI_API_KEY` and the `access_token`, `refresh_token` and
  `id_token` under `tokens`, named `codex_auth:<field>` in a finding. No name
  heuristic: a variable that only
  looks secret, such as `GIT_AUTHOR_NAME` or `SSH_AUTH_SOCK`, is not a needle.
- **Admitted credentials:** the needles must hold every credential the
  publishing run's lineage was admitted with. For each contained or native
  attempt with `auth_path = "env:<name>"`, `<name>` is a needle even if no
  runner declares it, and the publishing process must hold its value (16
  characters or more). For a native attempt, the Codex login file must be a
  readable regular file within its bound, valid JSON, holding at least one
  secret field. Otherwise the scan refuses with `credential_scan_unavailable`
  and records nothing: a clean verdict from a scan without those values would
  not prove them absent. For any other run, an absent Codex login file adds
  nothing, and an unreadable or malformed one adds nothing and logs a warning.
- **Encodings:** raw; standard and URL-safe base64 at every byte alignment; hex
  in either case; URL percent-encoding.
- **Key shapes:** `sk-ant-` Anthropic keys, `sk-or-v1-` OpenRouter keys,
  OpenAI `sk-proj-`, `sk-svcacct-` and `sk-admin-` keys and legacy `sk-` keys
  (each with at least 40 key characters, so `sk-learn` does not match), and
  GitHub `gh[pousr]_` and `github_pat_` tokens. A key-shape match in a file
  counts unless that exact string is already in the trusted base commit's
  version of every path the file is pushed at, so a placeholder the repository
  already carries does not block edits to its file. A path that is new at the
  base, a file name, and a commit object always count. A held credential value
  counts wherever it appears, including where the base already had it.
- **Bound:** values shorter than 16 characters are not scanned.
- **Read bounds:** the canonical reader's limits apply: 64 MiB per object,
  512 MiB per push, and 300 seconds. Sizes are checked before any object is
  read, and objects are streamed one at a time.
- **Key-shape bound:** each distinct key-shape string is held once for the
  whole push, and each base path is read once. Every held string is settled
  against every path its files are pushed at, under the same 300-second
  deadline, but repeats cost a lookup rather than a search: files pushed at the
  same base content share one answer, so the settling costs the base content
  read times the distinct strings, whatever the push repeats. Two bounds apply.
  At most 256 *distinct* strings are held, however many files repeat each one:
  one placeholder the base carries in a thousand files is within the bound. At
  most 65536 (file, string) pairs are held at all, which is what the settling
  keeps in memory. Over either bound the push refuses with
  `credential_in_output`, whatever the base holds: kew cannot clear that many,
  and the refusal says which bound it was without naming any value.

The host records `publication_output_scan` bound to the run, attempt, base, and
exact head. It records the verdict (`clean` or `found`), the number of files
scanned, and the match count per class; commit-object matches count under
`commit_`-prefixed classes. Each finding also names its source (the credential
name, `issued_run_key`, or the key-shape class), the object type (`blob`,
`tree`, `commit`), and the encoding, and the refusal message repeats that, for
example `CLAUDE_CODE_OAUTH_TOKEN in commit (value_raw)`. It never records or
prints a value, a fragment, a path, or a matching line. On a path where the
host pushes, a finding refuses with reason `credential_in_output`: nothing is
pushed, no publication intent is recorded, and the checkout and branch stay
local for the operator.

On the republish of an agent-pushed review head, the agent pushed its commits
itself before the host scan, so a finding there is already on the remote
branch. For a finding in the commits the agent pushed, the host refuses with
reason `credential_already_published`, marks the scan record
`already_published`, and records a `credential_already_published` audit
event naming the credential or key-shape class and the object type, never the
value. The operator must rotate that credential and remove the commit from the
branch. On this path kew moves the branch tip in two ways, each with
`--force-with-lease` against the agent head it verified: to the host-built
republish commit, or, for a change-then-revert head, back to the reviewed
head. It never rewrites history it has not verified, and it never removes a
commit with a finding; removal is the operator's action. The scan covers every
commit the agent pushed after the reviewed head, not only the head's final
tree: the host fetches that range whole, up to 256 commits deep and within
the scan's time bound. A range it cannot fetch whole, or a head that does not
descend from the reviewed head, refuses with `credential_scan_unavailable`.
The host also scans the commit it would publish on top;
the remote does not hold that commit, so a finding only there refuses with
`credential_in_output`, like any host push.

A recorded finding is final for its head. Every later push of that head
refuses with `credential_in_output`, whatever the run's posture (host,
uncontained, `env:`, or native), including the publication worker on each
later reconcile tick.

A missing
scan record refuses with `credential_scan_missing`; a scan that cannot read the
commit refuses with `credential_scan_unavailable`; a push over a read bound
refuses with `credential_scan_oversize`, and a scan past its deadline with
`credential_scan_timeout`. A clean scan binds one head and never admits
another. A `found` record for a head is final: however many other heads the
run scans later, and whatever a later scan of that head records, it refuses. A publication job committed before this scan existed has a head with
no scan record; its push step scans that head in the canonical repository
first and records the result, so a clean head is pushed and a finding refuses.

The checkout escape hatch scans against its trusted base, and a continuation
takes that base from the canonical remote's current base branch. If that
commit is not in the agent checkout (the base moved after the checkout was
made), the escape hatch refuses before scanning and names the preserved
checkout. Kew does not fetch into the agent checkout. Publish canonically, the
default, or re-run the continuation from a fresh base.

The scan matches what kew holds and what has a known shape. A credential the
host process does not hold, such as a native CLI login cache, is caught only by
its shape. Encryption, or a value split across files, defeats a value match.
Review a pull request before you merge it.

Canonical publication is the default. Kew reads checkout files as data after
the agent exits. It skips `.git` and rejects symlinks and special files. Kew
filters an artifact before rebuilding the tree in a fresh bare repository.
The invariant is exact: **no host Git process resolves its repository inside
the agent checkout after the agent has run, and no Git process rooted there
receives the publication credential.** Host Git does run in two trusted
places: the fresh canonical repository kew provisions for each publication,
and the operator's own project checkout (remote discovery, cleanup). Those
processes may inherit the operator's environment, including `GH_TOKEN`; that
is the operator's own repository, not the agent's. Publication of a fresh
branch requires a trusted base recorded before the agent ran.
An existing head requires a durable publication intent from the owning checkout lineage.
The intent must match the checkout generation and original base.
A fresh run cannot reuse another run's intent merely because the item and branch match.
Publication also refuses when it cannot record its intent durably before the push.
Older intents without matching ownership data require operator recovery.

### Publication intent scopes

The host records `publication_intent` before pushing a commit.
The recorder verifies that the event was stored before allowing publication.
The payload names the scope, branch, commit, tree, and parent.
The audit envelope's `item_id` stores the scope key.

| Scope | Scope key | Eligible branch |
| --- | --- | --- |
| `scope = "item"` | Tracker item ID | Every retained run names the same tracker item. |
| `scope = "branch"` | PR head branch | Every retained run names no tracker item. |

Standalone review PR numbers remain accounting keys.
They do not identify tracker items for publication intents.
A branch whose runs all name one tracker item requires item-scoped evidence.
On a branch whose runs name more than one tracker item (or a tracker item and none), the lookup reads claims under each item a branch run names and under the branch scope.
Exactly one claim (one run under one scope) may name the exact head across those scopes.
It comes from the host publisher, or from a named human's attestation recorded before the branch's runs became mixed: `kew publication attest` refuses a mixed branch.
The claiming run must have a row on the branch and route profile evidence of its own.
Two claims refuse as ambiguous. With no such claim, mixed ownership retains the legacy producer checks.
Older intents without `scope` retain item scope.

CI-fix publication uses the fixing agent's kew run UUID.
The GitHub Actions run ID never identifies the publication producer.
Conflict resolution binds its tracked run to the PR head branch before launch.
Clean integrations reuse the retained producer run because they launch no agent.
Without a retained producer, clean integrations retain the legacy path.

### Checkout binding boundary

Checkout binding defends against replacement by another process, including concurrent dispatch, resume, and operator commands.
It also defends against a contained agent, which cannot write outside its mount.
A host agent runs with the operator's privileges and can also edit the ledger.
A host agent is outside this boundary. Containment is the defense for that case.

Immutable audit evidence records device, inode, available birth identity, nonce, and token before launch.
The existing `checkout_id` field stores the complete binding as canonical JSON.
Resume, launch, base recording, and publication compare live kernel identity with that evidence.
The checkout admission lease holds a directory descriptor for the run.
Each boundary compares `os.fstat(fd)` and `os.stat(path)` with the persisted evidence.
The run closes the descriptor when it releases its checkout lease, including failure paths.
The sidecar and `.git/kew-generation` provide consistency checks, never identity evidence.
No agent-writable file authorizes checkout identity.

The following criteria cover post-run publication for dispatch and review remediation.
They also cover resolve and CI fix flows.
First-class loops are inside #1218's covered post-run publication case under canonical publication.
The host publishes each completed iteration through the same publication seams as dispatch.
Loops commit inside their own checkouts only under the admitted `publish = "checkout"` mode.
That mode requires `[sandbox] dangerously_skip_containment = true`.

| #1218 criterion | Mechanism | Owning test |
|---|---|---|
| Origin credentials never reach the agent checkout. | Userinfo is stripped from the remote URL kew writes into every agent-visible checkout. | `tests/test_worktree.py::test_agent_visible_origin_strips_credentials`; `tests/test_worktree.py::test_clone_local_checkout_strips_origin_credentials` |
| Checkout `remote.origin.url` and `remote.origin.pushurl` cannot redirect publication. | Operator configuration selects the remote. A fresh host-owned repository performs the push. | `tests/test_publication_canonical.py::test_checkout_remote_metadata_cannot_redirect_canonical_push` |
| Checkout `pre-push`, `pre-commit`, and redirected hooks never execute. | Canonical publication never runs Git against the agent checkout. | `tests/test_publication_canonical.py::test_checkout_hook_and_fsmonitor_never_run`; `tests/test_host_git_boundary.py::test_canonical_worker_paths_never_run_checkout_hooks` |
| Checkout changes to `remote.origin.url` and `remote.origin.pushurl` never reach host publication. | `publication_canonical.publish_checkout` passes the operator origin to `publication_repository.provision`. The fresh bare repository never reads checkout remotes. | `tests/test_host_git_boundary.py::test_no_host_git_resolves_inside_an_agent_path`; `tests/test_publication_canonical.py::test_checkout_remote_metadata_cannot_redirect_canonical_push` |
| Checkout publication needs explicit host-risk admission. | The selector requires `[sandbox] dangerously_skip_containment = true`. | `tests/test_publication.py::test_checkout_publication_requires_explicit_uncontained_admission`; `tests/test_dispatch_core.py::TestGitPushBranchRetry::test_checkout_escape_refuses_before_publication_without_admission` |

Under canonical publication the agent is told not to push and its
environment carries no publication credential: `GH_TOKEN`, `GITHUB_TOKEN`,
the enterprise token names, the `GIT_CONFIG_*` family, `GIT_ASKPASS`,
`GIT_SSH_COMMAND`, `SSH_AUTH_SOCK`, `SSH_AGENT_PID` and `LINEAR_API_KEY` are
removed by name for every runner. On a **contained** run that is a boundary.
On an **uncontained** host run (`dangerously_skip_containment = true`) it is
a scrub, not a boundary: `HOME` stays, so credential helpers,
`~/.git-credentials`, `~/.netrc` and default SSH keys remain reachable to an
agent that goes looking. That is the posture the flag admits, and the reason
it is named dangerously. Read-only workflows that relied on `SSH_AUTH_SOCK`
or `LINEAR_API_KEY` inside an uncontained agent run no longer see them under
canonical publication. First-class loops publish canonically by default (#1844).
Command verification still inherits the operator environment (#1727).
It runs agent-authored project code in a host-owned checkout (#1946).

`[publication] publish = "checkout"` is a temporary escape hatch. It restores
the prior checkout-rooted Git path. Selection fails unless `[sandbox]
dangerously_skip_containment = true`. An admitted selection prints a notice.
Its publication audit event records `posture = "uncontained-publication"`.
Admitted checkout publication can execute checkout hooks.
This setting admits the same host risk as uncontained agent execution.
`[sandbox] enabled = true` requires containment and rejects the dangerous flag.
Therefore, required containment disables host-checkout publication by design.

**This table describes sandboxed runs only.** An agent on the host path is an
ordinary child process of the orchestrator and **inherits the operator's
environment**. Ordinary dispatch reaches the host path only with `[sandbox]
dangerously_skip_containment = true` (when the runtime is missing, `enabled =
false`, the runner's credential is missing, or the runner is uncontainable).
On the container path, with a runtime present, an unmapped image or an
undeclared in-container credential still refuses. With no runtime, or `enabled
= false`, the flag admits the host run before image mapping is consulted. Review remediation and conflict
resolution use policy-permitted host fallback (`uncontained_flow`). First-class loops always launch their agent
on the host (see below). Their host verification admission also uses the dangerous flag.
`kew ci-fix` and `kew epic plan` use ordinary dispatch resolution, including
the required host-execution escape hatch. On every host path the agent
inherits every provider key, plus `GH_TOKEN` / `GITHUB_TOKEN` unless the
detached-publication handoff has claimed them for the host's push phase. kew
strips only its own `KEW_*` variables there (`orchestrator._build_child_env`).
There is no credential allowlist on that path. The command guard stands between
an injected agent and those values for **Claude Code only** (it is a Claude
Code `PreToolUse` hook); other runners on the host path have no equivalent
guard, and the guard's non-goals above apply. Native Codex is the one host-path
exception — it sanitizes its own process environment (see [Official Codex host
boundary](#official-codex-host-boundary)).

First-class loops have no contained agent launch (#1977).
A loop's agent runs on the host under the same admission as ordinary host dispatch.
A runner that contains itself records `posture = "native"`.
Any other runner requires `[sandbox] dangerously_skip_containment = true` and records `posture = "uncontained"`.
Without the flag, `kew dispatch --loop` and `kew loop resume` refuse with exit code 2.
The refusal names the flag and comes before any checkout, agent work, or spend.
It writes the pre-launch refusal evidence of a refused remediation run.
That evidence is a `refused` run row, a `sandbox_resolved` refusal, and a `dispatch_completed` event.
`[sandbox] enabled = true` rejects the flag, so it refuses loops on runners that do not contain themselves.

First-class loop command verification executes agent-authored project files.
Canonical mode materializes the published commit in a temporary host-owned checkout.
Its independent clone keeps trusted Git metadata inside the mount without shared object hardlinks.
This checkout blocks agent-controlled Git metadata from reaching host Git.
Every preparation Git command uses `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null`, and `GIT_CONFIG_NOSYSTEM=1`.
Preparation sets `HOME` and `XDG_CONFIG_HOME` to an empty scratch directory.
It sets `core.hooksPath` to an empty directory through `-c`.
It disables filesystem monitoring and global attributes through `-c`.
An empty `GIT_TEMPLATE_DIR` prevents templates from injecting hooks or local filter definitions.
Fresh repositories contain no external clean, smudge, or process filter definitions.
Published `.gitattributes` can name filters but cannot define their commands.
HTTPS fetch authentication uses the selected source host without ambient Git discovery.

Command verification runs contained when prerequisites are present.
Prerequisites are a local runtime and an approved image for the loop's runner.
Kew uses `[sandbox] images.<runner>`, or `[sandbox] image` for `claude-code`.
It resolves the image locally and executes its immutable image ID without pulling.
Verification needs no model credentials or runner-native boundary.

The command gate (`kew.loops.verification.CommandCondition.check`) runs the
operator-configured command (`[commands] verify_loop`, else `[commands] ci`)
as `/bin/sh -c` inside that image.
Extraction and verification share `build_checkout_program_argv`.
Both use `--network none`, host UID/GID, resource caps, and no runner credential forwarding.
Both explicitly control ten proxy variables in uppercase and lowercase.
Both blank all ten under either policy: they carry `--network none`, so a forwarded proxy value can never be reached from them and would only place its embedded credentials inside the container.
The configured `proxy_environment` still governs the agent container, and still scopes redaction of what these launches return.
Explicit `-e NAME=` assignments override Docker client proxy defaults, including `allProxy`.
The container root and checkout are read-only by default.
`[commands] verify_loop_needs_writes = true` permits writes to the disposable checkout.
Only operator configuration can declare this requirement.
Writable verification shares the dispatch checkout guard.
It limits aggregate checkout size to 4 GiB and entry count to 500,000.
It requires at least 1 GiB of host free space.
The guard polls every two seconds, including silent commands and commands that close both output pipes.
Directory walks use the shared adaptive backoff, capped at 20 seconds.
The free-space probe runs independently of that backoff.
A breach removes the container using its recorded ID and fails verification with a `checkout-quota refusal` reason.
These periodic checks can overshoot between probes; they are not filesystem quotas.
The root remains read-only; `/tmp` remains a bounded writable tmpfs.
Contained verification has a 600-second maximum timeout, including any CPU-cap retry.
Exit 0 passes; any non-zero exit fails.
The gate captures redacted stdout and stderr tails.
The publication evidence sanitizer removes host path ancestry from normal output, overflow evidence, and preparation errors.
Credential redaction and path sanitization run before evidence truncation.
Both `status_reason` and `verification_checked` store the sanitized evidence, including successful verification.
The host reads both pipes concurrently in chunks of at most 65,536 bytes.
Each stream has a 262,144-byte budget, with 131,072 bytes reserved for each end.
Overflow inserts an omission marker and drops the middle.
The host stops reading after the first excess byte and removes the recorded container ID with `rm -f`.
The gate records an `output-budget refusal` and fails verification.
Runtime inspection and removal target only the validated ID from that launch's container identity file.
Completion requires the configured container name, exited state, and matching exit code from inspection by ID.
Name conflicts fail with `container name already in use` and never trigger a retry.
A launch without a recorded ID removes nothing, even when another container holds the configured name.
After client exit, Kew deletes an identity file without a valid ID.
An interruption before the client records an ID preserves that file.
Unconfirmed removal preserves the container identity file and emits a warning.

The host path requires `[sandbox] dangerously_skip_containment = true` and missing prerequisites.
`[sandbox] enabled` controls agent containment; verification prefers containment even when it is false.
`[sandbox] enabled = true` permits contained command verification and refuses missing prerequisites.
It rejects configurations that also set the dangerous flag.
Checkout publication still requires dangerous admission for its local Git operations.
New dispatches select the issue, resolve the route, and save the initial loop
record first. The driver then checks admission before worktree setup or subprocess execution
against project files. Admission may inspect the local image.
A resume validates its route first, then applies the same admission.
Refusal records `needs_human` and `refused_verification` with the missing prerequisite.
The CLI returns exit code 2 for canonical command containment refusal.
Both `dispatch --loop` and `loop resume` return 1 for executed verification failures, including output overflow.
Both print the `gate_failed` reason; successful verification returns 0.
The finding remains #1444.

The `loop_started` and `verification_checked` events record `contained_verification` or `host_verification`.
They include the configured image reference and immutable image ID when contained.
Canonical `ci_green` needs no local execution; it polls the recorded publication SHA.
The admitted host command inherits the operator environment; environment stripping remains tracked separately.
Contain host execution inside an OS-level sandbox or VM.
A dedicated unprivileged account reduces host access.
The dangerous flag records risk acceptance and supplies no containment.

The diff-guard covers `**/tests/**`, `**/test_*.py`, `**/*_test.py`, `**/*.test.*`, `**/*.spec.*`, `**/AGENTS.md`, `**/CLAUDE.md`, `**/conftest.py`, `**/ratchets/**` (the test-quality and silence-on-failure ledgers), and `.github/workflows/**`.
Test files and agent-instruction files match at any depth.
It does not cover the verify script itself.

### The credential env allowlist is per-runner

Each runner declares the env var names it can authenticate with in-container (`required_credentials`), and that declaration **is** the container env allowlist: `build_run_argv` injects exactly the resolved runner's names and no other credential. The one non-credential variable it sets is the guard marker `AGENT_ASSIGNED_WORKTREE=/work` (#1691), a path.

| Runner | Injected | Not injected |
|---|---|---|
| `claude-code` | `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` | `OPENROUTER_API_KEY` |
| `opencode`, `pi`, `hermes` | `OPENROUTER_API_KEY` | `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` |
| `codex` | *(none; its credential declaration is empty)* | all provider, GitHub, and AWS credentials |
| `lemonade` | *(nothing — cannot be contained at all; see below)* | everything |

`[sandbox] runtime = "openshell"` keeps the same rule on a boundary that has no `-e` flags. `openshell sandbox exec` forwards the client's environment into the sandbox, so kew builds that environment as an allowlist — `PATH`, `HOME`, the locale variables, and each credential variable the resolved runner declares bound to the broker placeholder (the gateway holds the real credential). Nothing else crosses: not a host variable kew does not enumerate, not the proxy variables the Docker argv blanks, not the gateway endpoint. The narrowing is applied in the client, at the one call that reaches into a sandbox, so a caller that hands it a whole process environment still leaks nothing through it.

This is a **security boundary, not a config convenience**. A prompt-injected agent in an opencode container that could read `CLAUDE_CODE_OAUTH_TOKEN` could spend the operator's Anthropic subscription; the reverse holds for `OPENROUTER_API_KEY`. Consequences of the design:

- **By name only.** Widening a runner's declaration must never widen *how* credentials travel — no `-e NAME=value`, no mounted credential file, no host path. A credential value therefore never reaches an argv, a process list, a log line, the run row, or the audit ledger.
- **Fail closed, never borrow.** A non-native runner that declares no container credential (or that kew does not recognise) is *refused*, not launched with a neighbour's key — an image mapping alone does not make that runner containable. Native Codex uses its host CLI login and never enters this injection path.
- **Preflight, not post-mortem.** If the resolved runner's own credential is absent from the environment, the dispatch is refused *before* a container starts, with provider-specific guidance (`claude setup-token` for claude, `OPENROUTER_PROVISIONING_KEY` for a direct OpenRouter route). Containers cannot inherit CLI login, so launching anyway means a "Not logged in" container that burns the dispatch at $0 (#721).

### Retained credential fields

Kew treats values under credential keys as opaque.
It replaces the whole value without decoding it.
This rule covers display, exceptions, evidence, reports, publication, and audit diagnostics.
One classifier serves text matching, JSON traversal, and structured runner fields.
Keys are case-insensitive after decoding numeric Unicode, hexadecimal, bytes, and named Unicode escapes.
Named escapes use the standard library Unicode name lookup.
Invalid escapes remain unchanged.

Normalize keys before matching.
Split camelCase and acronym boundaries into words, then lowercase each word.
Also retain unsplit lowercase words for mixed-case header spellings.
Every non-alphanumeric character delimits words, including hyphens, underscores, dots, colons, slashes, and spaces.
Match whole word stems from this closed set:

<!-- opaque-credential-stems:start -->
- `auth`
- `authorization`
- `token`
- `secret`
- `password`
- `passwd`
- `passphrase`
- `credential`
- `cookie`
- `bearer`
<!-- opaque-credential-stems:end -->

The word `key` requires another word from this closed qualifier set:

<!-- opaque-credential-key-qualifiers:start -->
- `api`
- `access`
- `private`
- `signing`
- `session`
- `client`
<!-- opaque-credential-key-qualifiers:end -->

Normalize inflections before matching these whole words.

<!-- opaque-credential-inflections:start -->
Strip one trailing digit run before testing plural suffixes.
The digit suffix expression is `\d+$`.
Keep the unchanged word and test each suffix once.
- `ies` -> `y`
- `es` -> ``
- `s` -> ``
<!-- opaque-credential-inflections:end -->

Thus `passwords`, `credentials`, `cookies`, `token2`, `secret_1`, and `password01` match.
A normalized metric word permits numeric strings after the value passes validation:

<!-- opaque-credential-metric-words:start -->
- `total`
- `count`
- `used`
- `usage`
- `input`
- `output`
- `read`
- `write`
- `cache`
- `cached`
- `creation`
- `max`
- `min`
- `limit`
- `budget`
- `per`
- `rate`
- `remaining`
- `estimated`
- `planned`
- `cost`
<!-- opaque-credential-metric-words:end -->

Numbers, booleans, and null values retain their types and values.
Opaque scalar handling applies to strings.
Credential collections still lose their complete value.
Metric keys retain complete numeric strings, including `output_tokens` and `cache_read_tokens`.
The numeric grammar accepts signed integers, decimals, and exponents.
Optional unit suffixes are `k`, `m`, `g`, `B`, `kB`, `KB`, `KiB`, `MB`, `MiB`, `GB`, `GiB`, `TB`, and `TiB`.
Spaces or tabs may separate a number from its unit.
Other strings and collections under credential-bearing metric keys are redacted.
For example, `secret_count: hunter2hunter2` becomes `secret_count: <redacted>`.
Known credential-value matching still applies independently of key classification.

Repository credential variable names and the legacy spelling `apikey` also match.
Exact names or hyphen/underscore-delimited tokens match.
The rule also covers `apiSecret`, `auth.token`, `x.authorization`, and `provider/signing/key`.
For example, `proxy-authorization` and `provider_api_key` match.
Substrings inside words do not match.
`tokens_total` remains readable.
Cookie fields lose their complete value, including every cookie and attribute.
A credential value must parse as one scalar before scalar boundaries apply.
Otherwise, replace its opaque block through the next key at the same or lower indentation.
Without that key, replace through the end of the retained field.
This covers YAML/JSON sequences, nested mappings, block scalars, tags, and unbalanced quotes.
A sequence item terminates an opaque block only when the key carried an inline value and the item carries a key of its own.
Nested keys never terminate their containing credential block.

`keyboard`, plain `key`, `tokenizer`, `tokenize`, `passwordless`, and `status` remain readable.
`author`, `authentic`, and `secretary` also remain readable.
The classifier recognizes these compatibility examples and repository credential variable names:

<!-- opaque-credential-keys:start -->
- `ANTHROPIC_API_KEY`
- `ANTHROPIC_AUTH_TOKEN`
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_SECURITY_TOKEN`
- `AWS_SESSION_TOKEN`
- `CLAUDE_CODE_OAUTH_TOKEN`
- `CODEX_ACCESS_TOKEN`
- `GEMINI_API_KEY`
- `GH_ENTERPRISE_TOKEN`
- `GH_TOKEN`
- `GITHUB_ENTERPRISE_TOKEN`
- `GITHUB_TOKEN`
- `GOOGLE_API_KEY`
- `GOOGLE_APPLICATION_CREDENTIALS`
- `KEW_DETACHED_PUBLICATION_TOKEN`
- `KEW_SERVE_TOKEN`
- `LEMONADE_API_KEY`
- `LINEAR_API_KEY`
- `NPM_TOKEN`
- `OPENAI_API_KEY`
- `OPENROUTER_API_KEY`
- `OPENROUTER_PROVISIONING_KEY`
- `PYPI_TOKEN`
- `access-token`
- `access_token`
- `api-key`
- `api_key`
- `apikey`
- `auth-token`
- `authorization`
- `bearer`
- `client-secret`
- `client_secret`
- `cookie`
- `passwd`
- `password`
- `private-key`
- `private_key`
- `secret`
- `session-token`
- `set-cookie`
- `token`
- `x-api-key`
- `x-auth-token`
<!-- opaque-credential-keys:end -->

Fixed internal vocabulary remains exempt at its existing structured identity boundary.
These exemptions cover known runner, model, tier, and classification enumerations.
Free-text display has no vocabulary exemption.

Proxy matching covers complete URLs, normalized URLs, usernames, passwords, and complete userinfo.
Username-only userinfo receives the same protection.
Both `token@host` and `token:@host` contribute username forms.
The `none` policy derives no path, query, or fragment forms from host proxies.
Complete URL and userinfo forms remain a backstop under `none`.
The `inherit` policy derives component forms from the values received by that launch.
Host-only proxy values retain only complete URL and userinfo protection inside a launch scope.
Outside a launch scope, credential-shaped host proxies contribute named components and the whole fragment.
The parser splits path segments, query parameters, and fragment assignments before percent decoding.
Encoded delimiters remain inside their component.
Selected inherited components contribute matching forms before encoding expansion.
Outside a launch scope, named components still require six decoded characters.
Credential names use the shared stem rule.
Named path and query components use `name=value`.
Path segments also use the credential name immediately before them.
Query parameters never borrow names from adjacent parameters.
Every nonempty inherited component is selected, except short provenance-only ASCII numbers.
This includes path segments, query values, noncredential query names, and the whole fragment.
Fragment assignments follow query rules, including their values and noncredential names.
The numeric exemption applies only below six decoded characters.
For example, `?v=1` and `/2` contribute no standalone numeric forms.
Opaque credential keys override this exemption, so `token=1234` contributes a redaction form.
Complete proxy values, userinfo, usernames, and passwords retain their existing selection rules.
Recognized query and fragment credential labels never become component patterns by themselves.
Inherited ordinary words, including `gateway`, receive component protection.
Non-inherited path words remain readable inside a `none` launch scope.
Forms with six or more decoded characters match as substrings on every stage.
Every stage also reads the text without ECMA-48 escape sequences (CSI, the DCS, SOS, OSC, PM and APC strings in their 7- and 8-bit spellings, `ESC (`-style character set selections and single-character escapes such as `ESC 7`), so a colour or cursor code printed inside a known secret does not hide it; the replaced span covers the escape codes inside the secret too.
A form with twenty or more decoded non-whitespace characters also matches with whitespace, line breaks included, between its characters, so a long secret that output wrapped onto two lines is still replaced.
Whitespace a decoding stage produced inside a form, such as a `+` read as a space, is any whitespace run or none in that match.
Plain text is matched against known secrets before a credential field's value is replaced to its line end, so a secret wrapped after a label such as `token=` or `Authorization: Bearer` is replaced whole. One case runs the other way: when a configured secret itself contains a credential label (a value such as `password=…`), replacing it removes the label too, so text that continues that field's value past the end of the secret is not replaced.
Shorter forms never match across whitespace: `ab cde` is not the secret `abcde`, and a secret under twenty characters is not joined across a line break.
A continuation line that starts with a prefix is not joined: a prompt (`> `), a box border (`│ `), a log prefix, a line number or a diff marker sits between the two halves.
A secret split across separate stream events or separate JSON strings is not joined, because each is redacted on its own.
The last output lines a killed runner's reason quotes are redacted again as one text, but each line is first redacted as it arrives: when the first half of a wrapped secret followed a credential label, that line's field value is already replaced and the second half on the next line is not recognized.
Short forms require ASCII alphanumeric boundaries on any stage, using the union of stage matches.
Neither adjacent character may be an ASCII letter or digit on that stage.
For example, `%3Aabcde` exposes a boundary after decoding the colon.
`C:\Users\nabcde` is redacted because stage one decodes `\n` to a newline.
`abcde%58` also permits replacement of `abcde` because stage zero supplies a boundary.
This five-character over-redaction follows the provenance rule.
Short forms embedded inside longer alphanumeric text remain visible when every stage lacks a boundary.
All retained surfaces replace matching source spans in place, including ordinary structured string leaves.
Explicit opaque credential fields retain their existing containment rule.

<!-- proxy-encoding-grammar:start -->
The accepted encoding grammar is this closed list:

- Stage zero uses per-character NFKC normalization and removes Unicode Cf format characters.
- Four further positional stages decode percent bytes in UTF-8 and Latin-1 views, with any hexadecimal case.
- Each stage preserves literal-plus and space-plus views.
- Python and JSON escapes include `\\`, `\xHH`, `\uHHHH`, joined surrogate pairs, `\UHHHHHHHH`, `\N{NAME}`, octal, and line continuations.
- Case-insensitive hexadecimal patterns match UTF-8 bytes without positional hexadecimal decoding.
- Standard and URL-safe base64 patterns cover three byte alignments, whitespace wrapping, and optional padding.
The closed list excludes UTF-16 text, quoted-printable, HTML entities, and compression.
These formats are not decoded.
<!-- proxy-encoding-grammar:end -->

Stage zero maps each normalized character to its original character span.
Each later stage scans left to right once and applies at most four further stages.
Backslash pairs are consumed before another escape can be decoded on a later stage.
Every decoded character maps to one contiguous original span; these mappings remain monotone.
Percent bytes have both UTF-8 and Latin-1 views.
For example, `caf%E9-1` matches `café-1`.
The source forms retain every intermediate spelling within their bounded decoding budget.
Component selection and credential-name classification preserve bounded percent decoding and both plus interpretations.
A credential-key name at any stage classifies its value as a credential.

Hexadecimal matching never decodes arbitrary text as hexadecimal bytes.
Its case-insensitive patterns detect long forms even at odd nibble parity.
Base64 patterns use byte offsets zero, one, and two for each alphabet.
Partially determined edge sextets use character classes.
Whitespace may separate encoded characters, including MIME line wraps at 76 columns.
Patterns accept padded and unpadded spellings.
Short block-coded forms match only when the entire maximal coding run decodes to exactly that form.
Thus unrelated container identifiers and image digests remain intact.

The engine collects matches across every stage and maps each match to its original source hull.
It merges overlapping or adjacent hulls before replacing them right to left in one assembly pass.
Each hull receives its variable's marker; hulls involving different variables receive the residual character.
Each variable selects its marker at scope entry using its forms and the same engine.
Self-colliding markers use the residual character from the first replacement.
Four collision passes re-run the complete engine; residual replacements then continue until no match remains.
Residual characters start with `█` and occur in no form or encoding alphabet.
They exclude ASCII alphanumerics, percent, plus, backslash, equals, slash, underscore, and hyphen.
They remain opaque boundaries and cannot complete an escape or coding run.
Each residual pass removes non-residual source characters, which ensures termination.
An independent output self-check runs the same stages and matching rules after replacement.
A failed check enters residual cleanup.
If cleanup cannot clear the output, the engine refuses the field with `<retained field refused>` and logs its surface.
This refusal is the only whole-field outcome for ordinary text and string leaves.
Retained text remains bounded to 65,536 characters after replacement and checking.

`NO_PROXY` and `no_proxy` split comma entries before bounded decoding and trim whitespace.
Each stage exempts host entries and classifies remaining assignments through the shared opaque-key classifier.
Selected values and bare non-host entries contribute forms under the existing length tiers and provenance rule.
Complete inherited values remain protected, including complete host lists.

Readiness probes retain their launch policy and environment through parsing, judgment, and settlement.
Review provisioning scopes streaming, exceptions, and warnings under its configured proxy policy.
Host loop verification scopes command evidence under `inherit`.
Contained loop verification carries the configured proxy policy through admission, container argv, recorded fields, and evidence redaction.
Switchyard command scopes sanitize unexpected exceptions; expected failures use fixed diagnostics.

### Isolated checkouts

Every checkout kew hands an agent for dispatch, loops, planning, review remediation, resolve, and ci-fix is a **standalone clone with its own `.git`**. None is a linked Git worktree of your repository. Planning clones also use `--no-hardlinks`, so Git objects do not share inodes with the operator repository.

That distinction is the boundary, not a detail. A linked Git worktree's `.git` is a *file* pointing into your main `.git`, which means an agent working there:

- writes **your** `.git/config` with `git config core.hooksPath` or `core.fsmonitor`;
- resolves `--git-common-dir` to **your** `.git`, so it can drop a file into `.git/hooks/` directly;
- shares **your** object store — a local `git clone` hardlinks object files by default, so the agent's `.git/objects/<xx>/<sha>` is the same inode as yours and can be overwritten in place.

The first two plant traps that fire on **your own next `git commit`** — no kew dispatch required to trigger them. The third can corrupt your repository outright.

Kew clones with `--no-hardlinks` and carries your committer identity into the clone. `git clone` copies objects and refs, but never configuration. Kew refuses to replace checkouts with uncommitted changes. Kew never fetches through a replacement checkout. It reads the origin URL from the operator repository. It queries the exact remote branch with `git ls-remote`. Local tracking refs never prove publication. A missing branch or query failure preserves the checkout. Kew checks the advertised commit with sanitized, read-only Git plumbing. Kew refuses replacement when that commit is missing or not an ancestor of HEAD. Kew also refuses when HEAD contains commits beyond the advertised branch head. Kew quarantines validated checkouts beside their original path before replacement. The warning prints the quarantine path. Checkouts created by older versions can be linked Git worktrees. New dispatches and resume paths fail closed for linked worktrees. Their errors explain how to push or copy work before migration. They never fetch through or delete the linked checkout on refusal.

### Review and conflict resolution record their posture

`kew review` and conflict resolution run an agent against an existing PR branch.
Their recorded posture states whether they used a container or the host.
Contained execution uses the same image, credential, and network rules.

With `enabled = true`, review remediation and conflict resolution fail closed when containment prerequisites are missing.
Missing prerequisites include the runtime, runner image mapping, and declared credential in the environment.
`allow_uncontained_flows = true` explicitly permits their host fallback with `admission = "policy"`.
The setting defaults to unset; unset and `false` permit no exception.
Auto fallback records `admission = "prerequisite_gap"`; explicit `enabled = false` retains `admission = "policy"`.
CI-fix retains its stricter dispatch gate and exit code 2 (could not run), including when this setting is `true`.
Review and resolve containment refusals return exit code 2 (could not run).

Detached dispatch children receive the exact reservation ID acquired by their parent.
The reservation ID also serves as its generation token.
Admission checks this identity and records its binding within the run-row transaction.
Every pre-admission gate enforces this identity, including refusal reporting and reservation release.
A replaced reservation produces `reservation_replaced`; that refusal releases nothing and publishes nothing.
Direct callers without a parent identity record `pinned=false` in any reservation binding.
Reconciliation prefers a pinned child over an unpinned child for the same reservation.

Linear children claim items after the pending row and reservation binding commit.
Fetching an item never claims it.
After a claim, a refusal retains the claim and never resets Linear automatically.
Linear's `issueUpdate` has no version precondition, so verification reads cannot make an automatic reset safe.
The item stays assigned in its claim state. A replacement dispatch re-claims it.
For manual recovery, unassign the item in Linear and move it back to its unstarted state.
No kew command releases a retained claim: the replacement dispatch comes from the epic, whose tick re-dispatches the node and re-claims the item, and the Linear steps above undo it by hand.
`kew epic status` shows a `retained claim` line for each affected epic item.
Each line shows its latest recorded reason and the current recovery instructions.
Historical events use current instructions, including events recorded before this change.
`linear_claim_completed` records the claim snapshot.
`linear_claim_retained` records the refusal reason, reservation identity, claim snapshot's `updatedAt`, and recovery instructions.
The operator warning names the item and gives the same recovery instructions.
The `on_refuse` callback finishes before the terminal refusal row, event, or reservation release.
Callback failures remain visible and preserve the refusal exit code.

The shared refusal writer stores `pre_admission_refusal=true` with every pre-admission refusal row.
Publication excludes marked rows before applying the 32-producer history limit.
Exclusion also requires zero usage, no elapsed work, no launch handles, and no route or reservation binding.
Conflicting launch evidence keeps the row in publication history.
Older unmarked rows retain the narrow containment and reservation-replacement checks.

These route profiles and audit events agree, but they use different labels. The profile/event mappings are `contained`/`sandboxed`, `native`/`native`, and `host`/`uncontained_flow`.

**Publication stays on the host** for dispatch, review remediation, and contained conflict resolution. The broker binds each update to the recorded PR head. Conflict resolution rejects unmerged entries and tracked conflict markers. It requires a clean checkout and a new local commit. The commit must descend from the recorded PR head and fetched base. The remote tip must match before cleanup. Any missing or unproven state fails closed and preserves the checkout.

**The cost cap follows the agent in.** claude-code normally enforces its own limit from a command-line flag, which the container command does not carry — so for a contained run kew watches the accumulated cost itself and stops the agent gracefully at the cap, giving it a window to commit work in progress first. A stop reaches the container by name; killing the local runtime client would leave the agent running.

### Adversarial review verdict channel

The governed adversarial reviewer runs the PR's own tests as acceptance evidence.
Those tests run in the reviewer's container, as the same user, over the same `/work` mount.
Any file in that checkout, at any depth, can therefore be written by the code under review.
Kew reads no file in the checkout as a verdict (#2021).

The verdict travels in the reviewer's own final result event.
The host reads the runner's typed terminal record as it streams the container's stdout.
For `claude-code` that record is the single top-level `result` event.
A test's ordinary stdout reaches that stream only inside a `tool_result`, which is never read as a verdict.
A review launch gives the runtime client a second pipe for the container's stderr, which it would otherwise merge into the pipe carrying stdout.
The reviewer's own output is therefore the only thing the round is read from, so a stray non-event line on stderr — a runtime warning from the reviewed code, a daemon notice — cannot fail a genuine round.
Those lines are drained (an unread pipe would block the container) and counted, never folded into a verdict; the daemon's CPU-cap refusal and the entrypoint's pre-startup diagnostic are still read from them.
The final message must carry the verdict JSON between `<<<KEW-REVIEW-VERDICT-BEGIN>>>` and `<<<KEW-REVIEW-VERDICT-END>>>`.
The host stores the extracted block in its log directory, which is never mounted into the container, and attests a digest of that copy.

The round fails, and the blocking stage holds the node, when any of these hold:

- the reviewer exits non-zero;
- the stream carries zero or several result events;
- the result is not the stream's last event: any later non-blank line, whether an event other than a `system` notice or text that does not parse as one, fails the round (Claude Code's own `system` notices after its result, such as `task_updated`, are allowed);
- the host reader split a line, because it ran past the reader's 1 MiB line limit or was glued onto a write with no newline;
- the result reports an error, or its text exceeds 960 KiB (64 KiB under the reader's line limit, so an over-cap result is still read whole and refused by size);
- the final message carries zero or several verdict blocks;
- the block's `head_sha` differs from the head SHA the host recorded.

The reviewer is told not to start background tasks, and to run bounded, targeted commands in the foreground.
A background task that finishes re-invokes the reviewer, and each re-invocation emits another result event after the verdict, which fails the round on the several-result-events rule.
That instruction rides both the pinned reviewer prompt and the host-appended runtime guidance, so a repository whose base ref still carries an older pinned copy is told anyway.

`[review] runner` accepts only a runner with such a record; any other value is a configuration error at load.
Only `claude-code` qualifies today.
The reviewer launcher refuses any other runner again before any container or credential access.

Residual boundary: code under review runs as the agent's user, so it can open the agent's own stdout through `/proc` and write a forged result line.
A forged line beside the genuine result makes two result events, which fails the round.
A write with no newline before the genuine result glues onto it, so the genuine line no longer parses; any line that does not parse as an event fails the round, wherever a forged line sits.
A forged line can stand alone only when the genuine result never arrives, because the reviewed code stopped or killed the agent.
A killed agent exits non-zero, which fails the round.
Whether an agent stopped by a catchable signal can still exit successfully depends on the agent CLI's signal handling, which kew does not control.
Running the reviewed code as a different user from the agent would close that gap.
The governed reviewer runs the PR's code as the agent's user, so it is not yet safe on pull requests from untrusted forks.

### Usage-limit warnings are advisory

kew reads an agent token's usage-limit warning (see [`kew quota`](cli/quota.md#usage-limit-warnings)) from the runner's own stream, which a contained agent can write to, so the warning is advisory and never gates dispatch.
What a forged notice can do is bounded: a limit name kew does not know is shown as "a usage limit", a reset before the notice or more than eight days after it is read as unknown, a notice cannot replace a warning still in force with an earlier reset or none, and recording one never fails the run that carried it.

### The adversarial reviewer's own containment boundary

The reviewer is the launch kew exposes most: its prompt requires it to execute
the reviewed PR's tests, and the host installs that PR's locked dependency tree
first — running the PR's own build backend — so the reviewer's container runs
code no human has read. What that container can reach, and what credential is in
it, therefore depends on `[sandbox] runtime`.

**On `docker` and `podman`** the reviewer gets the same boundary a dispatch does,
and no more: the runner's credential variables are forwarded **by value** into
the container, on `[sandbox] network`, with no destination rules kew can verify.
Every reviewer launch now records that posture as `sandbox_resolved` with
`egress_verification = operator-managed-unverified` and
`egress_verification_basis = operator-asserted`. The exposure is not removed on
this route — it is made visible, which it was not before, because the reviewer
attested no posture at all.

**On `openshell`** the reviewer is a boundary of its own, not a dispatch:

- it launches through the gateway and **never** falls back to the docker route. A
  host that cannot give it a sandbox refuses the round with `sandbox_refused`;
- the credential stays in an OpenShell provider. The variables inside the sandbox
  hold `kew-openshell-broker-placeholder`, and the posture names them as
  `provider:<name>` plus `placeholder:<VAR>` — never a value;
- egress is `docker/openshell/policy-review-<runner>.yaml`: the inference host
  only, enforced. No package index, no forge, and no interpreter the reviewed
  code runs is governed at all — under `enforcement: enforce` an ungoverned
  binary reaches nothing;
- it requires a fresh certification bound to **that** policy's digest
  (`kew sandbox certify --runtime openshell --flow review`). A dispatch
  certification does not satisfy it, and the refusal names the flow whose proof
  is missing. With the proof the posture records
  `egress_verification = openshell-certified`.

**Provisioning never shares the reviewer's sandbox.** The locked install runs in
its own sandbox, created with **no provider at all** — no credential, not even a
placeholder — under
`docker/openshell/policy-review-provision-<runner>.yaml`, which permits the
package index hosts and governs `uv` alone. The environment it builds is
downloaded back to the host under the realized-checkout caps and moved into the
reviewed checkout; the install sandbox is deleted before the reviewer's is
created, and a delete kew cannot confirm fails the round closed rather than
letting the review start beside it.

That provisioning boundary is deliberately **not** certifiable: the
certification probe is a Node program and node is ungoverned there by design, so
a probe would report a failed control for a policy working exactly as written.
`--flow review-provision` says so and runs no probe.

**One limitation of the gateway route.** `openshell sandbox exec` gives the host
a single merged stream, so the second stderr pipe a review gets on the docker
route (above) does not exist there. A runtime warning written to stderr by the
reviewed code therefore lands in the pipe the verdict is read from and fails the
round under the "any line that is not a typed event" rule. It fails **closed** —
such a line is never read as a verdict — so the cost is a refused round on a
genuine review, not an accepted forgery.

### A pinned image tag is checked against the image's inputs label

A `docker-<digest12>` tag such as `kew-agent:docker-<digest12>` only *names*
the inputs an image was built from: `docker tag` can point it at any image.
So every agent image kew builds — with `kew sandbox build`, in the image scan
gate (`scripts/scan-agent-image.sh`), or on a remote host — carries the full
SHA-256 of its `docker/` inputs as the image label
`dev.kew.agent-image-inputs`, and before a container is created for a kew pin
tag, kew reads that label from the same `image inspect` that resolves the
image's immutable ID. The pinned tag is checked against the image's inputs
label before launch: on this machine, a label that is missing, or that names
inputs other than the ones the tag's digest names, refuses the dispatch or
review with the rebuild command (`kew sandbox build --runner <runner>`). An
image built before the label existed is refused the same way, and rebuilding
it is the remedy. On a remote host, where kew builds the pin itself, such an
image is rebuilt from kew's packaged inputs before the launch, and a rebuild
whose image still lacks the label refuses the placement. Images that are not
kew pin tags — an operator's own image, an immutable ID — are left alone.

The label catches a stale image: one built before the label existed, built
from other inputs, or retagged by mistake. It is not a defence against
tampering, because anyone who can run `docker` can build an image with any
label. Access to the container runtime is already full control of what it
runs.

### Host-side resource-cap coverage

The contained `execute_dispatch` path streams through `ContainerHandle`. It
enforces the UTF-8 byte read bound, transcript cap, checkout quota, and
independent four-hour watchdog. A breach kills the named container.

`kew review`, `kew resolve`, and their retries stream through the shared
orchestrator. That path uses byte-bounded reads and the checkout watchdog. Its
agent deadline drives the named-container watchdog and
`timeout --foreground -k 30s`. It does not use `ContainerHandle` iteration.
Killing the Docker client does not stop its container. The host watchdog
therefore enforces the wall clock with `docker kill <name>` at the deadline;
the wrapper's `-k` only stops a stuck client. The 64 MiB transcript cap applies
to `execute_dispatch` and to the review-environment provisioning exec below;
nothing else today.

The OpenShell review-environment install is the one stream with no container
handle of its own: `openshell sandbox exec` hands the host a plain pipe, so the
wall clock and the transcript cap are enforced over it directly
(`sandbox.bounded_pipe_lines`). That read is output-independent — a build
backend that stays alive and prints nothing is cut off at the install deadline
rather than holding the review stage — and either breach ends the workload by
killing the gateway client and deleting the install sandbox, which is what a
`docker kill` does on the same breach. The checkout quota is not enforced
there: the tree the install writes into lives inside the sandbox, so there is
no host directory to walk. Its size is bounded on arrival instead, by the
realized-checkout caps the download is subject to.

### A never-started container does not keep the item's name

A contained dispatch creates `kew-<item>`, labels it with this project's
ledger id (`io.kew.ledger`) and its run id (`io.kew.run`), records its full
id, then starts it. When the start fails, kew removes the container it created
by that id. A container that never started refuses `docker kill`, so kew does
not rely on the kill to clear it.

Container names are shared by every project on the same Docker daemon. When
the runtime refuses a create because the name is already in use, kew reads the
container holding the name. kew removes it and creates once more only when all
of these are true:

- the runtime reports it `created` with no process;
- its labels name this project's ledger, and a run this ledger has already
  finished (a container without labels, or another project's, is never
  removed);
- no other pending run records it;
- a plain `docker rm` (no `-f`) succeeds.

Before removing it, kew records a `stale_container_removal_intended` audit
event. After the removal it records one `stale_container_removed` event naming
the container. If that second event cannot be written, the next launch finds
the intent, confirms with the runtime that the container is gone, and records
the removal then, exactly once. The intent records the runtime and the daemon
the container was on. Only that same runtime, talking to that same daemon, can
confirm the container is gone. After a switch from Docker to Podman, or to
another Docker context, the intent waits. If kew cannot tell which daemon it is
talking to, as a loaded host's `docker info` can fail, it still removes a
finished run's container by its id, and the intent names no daemon. Should the
second event then be lost, no later launch can confirm the removal, and the
intent stays open. A container that a failed create of the launching run itself
left is removed by its id under the same pair of events, naming the daemon the
run recorded before its create. The timeout path does the same: when a create
times out, the daemon may still commit the container after its client was
killed, so before the create is tried again kew removes this run's own
never-started container, by the id its labels prove when the daemon cannot be
named, and otherwise retries only once the runtime says the name is free.
Docker's "No such container" and Podman's
"no such object" both count as gone, but only when they name that exact id. If
another process removes the container first, so that kew's own `rm` fails, kew
still treats the name as free once the runtime confirms the id is gone. A
running container is never touched.

This holds on every contained launch, reviews and remediations included. A
create that still fails, a start the runtime client could not spawn, and a
`docker start` that exits without its container ever running all remove the
container kew created and finish the run as `container_start_failed`, and the
reason names the container. A review keeps that category rather than the
generic `launch_failed`, and its hold names the container too. A sidecar
worker whose `docker start` fails is removed the same way, by its full id, and
its supervisor then tears down the proxy, network and volume that hold the
credential. A `docker exec` into a sidecar worker never removes that worker;
its supervisor owns it. The
console shows this as "Container start failed", with the code as secondary
detail.

### "Not containable yet" and "not containable ever" refuse differently

An unmapped image or an undeclared credential is a gap on **kew's** side. Those fail closed and `dangerously_skip_containment` does *not* wave them through, because the fix is for kew to build the missing piece — letting the flag past them would turn it into the cargo-culted answer to every sandbox complaint and quietly un-contain every dispatch, not just one.

A runner that can **never** be contained is a different claim. `lemonade` drives a host-local inference server that a contained network cannot reach, so no image is coming; routing it through the fail-closed gate would just make it undispatchable on any machine with Docker installed, while pointing at a remedy that never arrives. Such a runner is refused by default with the reason stated, and runs **only** when `dangerously_skip_containment = true` is set explicitly — uncontained, with a banner, and attested as uncontained on the ledger.

Only runner code may declare this (`uncontainable_reason`), never configuration — the same rule native containment follows. Configuration must not be able to assert that something cannot be contained, because that assertion is what removes the boundary.

**Egress is only as restricted as the network you attach.** Each dispatch records one `egress_verification` value:

- `sidecar-certified` comes only after matching packet certification passes sidecar admission.
- `openshell-certified` comes only after a passing OpenShell egress certification matches the exact boundary tuple at admission.
- `operator-managed-unverified` permanently covers every direct contained route, including `network = "open"`.
- `unavailable` covers a refused route, non-container route, or sidecar awaiting certification.

Regular dispatch, review remediation, conflict resolution, CI fix, and epic planning share the sidecar lifecycle.
Each attempt records `unavailable` / `pending-sidecar` before sidecar setup.
Retries persist their attempt number before that pending write.
Worker and profile admission must both succeed before `sidecar-certified` / `sidecar-admission` is recorded.
Each transition carries a unique `posture_id` alongside `attempt_number`.
Kew reads the exact transition back from the run's audit events before proceeding.
An older matching transition cannot acknowledge a new write.
A missing pending record refuses setup.
A missing promotion record refuses worker execution.
The refusal reports `sandbox posture was not durably recorded` and emits `sandbox_posture_refused` with the attempt number.
A failed audit store can also prevent recording the refusal, but execution still stops.

`[sandbox] network` defaults to `"allowlist"`.
The direct container joins the Docker network named `kew-allowlist`.
Docker fails closed when that network does not exist.
Kew never falls back to open egress.
A plain Docker bridge permits outbound traffic while the provider key is present.
Endpoint restriction remains the iptables rules you add to that network yourself.
An operator can set `network = "open"` for full egress.
Kew then warns that one outbound request could exfiltrate the named credential.

The direct route's egress policy is operator-asserted and unverifiable by kew (#1945).
kew installs and verifies no destination rules there.
Docker's network API exposes no destination rules.
Docker Desktop keeps the host firewall inside its VM.
Kew permanently records `operator-managed-unverified` for every direct contained route.
What kew *can* read is the attachment network's shape, so each direct posture also records `network_driver` and `network_internal` as the configured runtime's `network inspect kew-allowlist` reports them — Docker and Podman name those two attributes differently and kew reads either, recording `unknown` when the daemon cannot answer.
A plain bridge with `Internal=false` has open egress unless the host filters it, and `kew doctor` warns when it sees one.
kew never refuses on that evidence: an `Internal=true` network has no egress at all, so the agent could not reach its inference API.
Operators who need kew-verified egress use the sidecar route, subject to its admission gates.

### OpenShell egress certification

`[sandbox] runtime = "openshell"` is a different container boundary, and kew does not claim it until it has watched it hold.
The operator-facing setup for this runtime — install, gateway config, providers, certification and teardown — is the [OpenShell runtime runbook](openshell.md).

`kew sandbox certify --runtime openshell --runner <r>` reads the runner's own policy once and, for each binary it governs, creates a throwaway sandbox from the runner's immutable image ID, runs one probe inside it, and deletes the sandbox on every path.
The probe runs as `/usr/bin/node` under a policy granting node exactly that binary's clause, so the runner's native binary's rules are probed too, and every binary's matrix must pass. Each probe sandbox is created from a private copy of the bytes that were hashed.
It records one positive control — a TLS connection to the runner's inference API host — and eight denials: an external host, a routable public IP dialled without DNS, UDP DNS to a public resolver, an external IPv6 destination, `169.254.169.254`, the sandbox's own default gateway, `github.com`, and a reserved name that must not resolve to a real address.
The positive control is what makes the denials evidence: a sandbox with no working network denies everything, so a matrix without a proven reachable inference host fails rather than certifying.
That control is TCP over IPv4, so `udp_dns` and `ipv6_external` each carry their own same-protocol control as well — the sandbox's configured resolver over UDP, and the allowed inference host over IPv6 — and a leg whose own control did not hold fails the certification rather than passing on a protocol the sandbox never had.
A refused or reset connection counts as reachable, because the packet drew an answer.
A probe that could not be attempted records `unavailable` and fails the certification; it never shortens the matrix.
Only a passing certification is appended, as `runtime_egress_certification`, signed by the `openshell-egress-gate` policy actor.
Admission matches that proof on five facts and on nothing less: the runner's immutable image ID, the SHA-256 of the policy bytes, the version the OpenShell CLI reported, a SHA-256 of the gateway's own identity (the endpoint kew dials and what the running gateway reports about itself), and the SHA-256 of `kew-openshell-launch` as the probe read it inside the image — which must equal the launcher this kew ships.
The launch matches again when it creates the sandbox, on the digest of the one policy read it hands the gateway and on the gateway it is about to use.
A probe sandbox that could not be deleted fails the certification and records nothing.
The proof must be no older than the existing 24-hour evidence window.
A dispatch that matches it records `openshell-certified` / `openshell-selftest`.
Every other case — absent, expired, bound to a different image or policy, signed by the wrong actor, self-contradictory, or unreadable because no ledger was available — refuses with `openshell-egress-uncertified` and names `kew sandbox certify` as the fix.
kew packages an OpenShell policy for `claude-code`, `codex` and `opencode` only.
A runner without one — `pi` and `hermes` — has no boundary that command could probe, so it is refused before the ledger is even read, under its own `openshell-policy-unpackaged`: the message names the missing policy and names no command, because packaging one is kew's job and not the operator's.
The review remediation, conflict resolution, CI fix and plan run flows read the same proof off the same ledger, but none of them attests an OpenShell container: they launch their agent as a container command line, and a gateway sandbox has none. Where they would be contained they are refused at admission, and `kew review` and `kew resolve` fall back to the host under `enabled = "auto"` and record the gap.
A posture kew holds no proof for records `egress_verification = unavailable` beside `egress_verification_basis = openshell-uncertified` — never the direct route's `operator-managed-unverified`.
`dangerously_skip_containment` does not waive it: naming a runtime asks for containment, and a host run is not that answer.
A certified dispatch then launches through the gateway; a certified boundary the gateway path cannot serve (a sidecar or Switchyard transport, or one of those flows) refuses with `openshell-runtime-unavailable`.

**Accepted non-goals (v1):** container image integrity beyond digest pinning, and protection against a compromised container image itself.

The **GitHub token is host-only**. The container never receives it. A prompt-injected agent cannot exfiltrate it or push arbitrary repositories. The host brokers publication for dispatch, review remediation, and contained conflict resolution. kew verifies the agent result before it invokes the host publication path.

Epic merges require a non-author GitHub `User` approval plus a fresh
collaborator-permission lookup granting `write`, `maintain`, or `admin`.
Bot reviewers, external reviewers without that authority, missing actor data,
and permission lookup failures all fail closed. Configured approver lists only
narrow this authority requirement; an empty list never grants authority.

See [Configuration — sandbox](configuration.md#sandbox) for the full option reference.

### Official Codex host boundary

On the `docker` and `podman` runtimes, official Codex is not placed in kew's
external container in v1 (`openshell` is the exception, below). It runs with
Codex-native `workspace-write` on the host, command network off, protected
`.git`, fixed extension/search/shell-secret controls, and a sanitized process
environment. Project `.codex/config.toml`, unknown entries, symlinks, and
special files fail closed; only regular content under `.codex/rules/` and
`.codex/skills/` is admitted. The official CLI login remains in its cache or
keychain and kew's host broker owns commit, push, PR, and report publication.
This runner-owned boundary is admitted and attested as posture `native` on the
default path, whether or not a container runtime is installed.

On a gateway-launched dispatch under `[sandbox] runtime = "openshell"` it is not
the outer boundary. There kew creates a sandbox from its own Codex agent image
under `docker/openshell/policy-codex.yaml`, the gateway holds the credential in a
named provider and injects it at the endpoint, and the dispatch is attested
`sandboxed` like any other contained run. Codex still applies `workspace-write`
inside that sandbox, so the two boundaries nest; what changes is which one kew
claims to hold. Everything above still describes the `docker` and `podman` lanes
and every host run — and every run on this runtime that kew has no sandbox to
offer: a shape the gateway cannot launch (a remediation flow, a sidecar
Switchyard topology, or a managed Switchyard transport with no sidecar) and a
preserved-checkout continuation.
Those keep posture `native` rather than being refused for a boundary kew was
never going to hold. A host with no gateway CLI installed is refused instead,
because the configured runtime asked for that boundary; it keeps `native` only
when `dangerously_skip_containment = true` opts into the host path
(see [the Codex reference](runners/codex.md#posture-per-runtime)).

`workspace-write` restricts writes and command network access, not reads.
The agent can read any file the host user can read, including credential files such as `~/.codex/auth.json`.
It can copy what it reads into the checkout.
Before the push, the publication output scan refuses output that contains a credential value kew holds, a secret value from the host's Codex login file (`$CODEX_HOME/auth.json`, else `~/.codex/auth.json`), or a known key shape, including OpenAI keys.
If that file is absent, unreadable, or malformed, native output does not publish (`credential_scan_unavailable`), so a Codex login kept only in the OS keychain blocks native publication.
A token kew does not read, such as another tool's credential file, is caught only by its shape.
Review a Codex pull request before you merge it.

## Verification hosts

A **host** is a machine kew ships a git bundle to and runs one job on, and the
trust boundary is the same whether that machine is this one (`local`, the
default) or a Compute Engine instance (`gcp`). A host receives **no** GitHub,
Linear, Google or cloud credential, and it is never asked to publish anything:
kew's own broker owns commit, push, PR and report publication.

The one credential that reaches a host is the **model** credential of an agent
`dispatch` placed there: only for that run kind, only a name the runner itself
declares, into that container's environment over the SSH session's stdin — so
never onto the instance's disk and never onto a command line — and recorded by
name, never by value, in the `dispatch_remote_launch` audit event. Every other
credential-shaped name is refused, for every run kind
([Hosts](hosts.md#the-credential-exception)).

- **Code in as a bundle.** A host gets a git bundle, cloned into a checkout
  there. It does not get kew's configuration, its ledger, or its audit chain.
- **Results out as data.** What comes back is an exit code, a wall-clock
  duration and log text. Nothing a host returns is treated as an instruction —
  the tool versions `kew hosts provision` records are matched off the recipe's
  output line by line, stored, and printed.
- **The job environment is an allowlist.** A declared variable whose *name*
  reads like a credential (`*TOKEN*`, `*KEY*`, `*SECRET*`, `*PASSWORD*`,
  `GITHUB_*`, `GH_*`, `LINEAR_*`, `ANTHROPIC_*`, `OPENAI_*`, `OPENROUTER_*`,
  `GOOGLE_*`, `CLOUDSDK_*`) is refused **by name**, with that name in the
  message, even when a caller asks for it. A name that is not a shell-usable
  variable name is refused too, because a remote job's variables are
  interpolated into the host's command line, where a name cannot be quoted.
- **One job at a time per host,** held by kew's own lock, so two jobs cannot
  clone over each other's checkout.
- **A host's exit code is explicit.** The remote outcome is read off a marker
  on the host's stdout alone, so a job writing that marker to stderr cannot
  forge a pass, and a missing marker is an unknown outcome rather than a clean
  zero.
- **The machine itself is built from a reviewed recipe** in this repository
  (`kew/src/kew/hosts/provision/`), not by hand, and only recipes shipped with
  kew resolve — a path or an unknown name is refused before the host is
  touched. The recipe installs uv and trivy only from release archives checked
  against a SHA-256 pinned in `trivy.pin`, on every run — never from an
  installer script, and never skipped because an installed binary reports the
  pinned version. The image scan gate identifies trivy by the SHA-256 of its
  binary, also pinned there, not by the version it reports.

`gcloud` is an optional external CLI, like `docker` and `gh`: probed at use,
reported by [`kew doctor`](cli/doctor.md), never a Python dependency. See
[Hosts](hosts.md) for what a host holds, how to create one, its quota and idle
stop, and what it costs.

Before the drive merges a pull request, [`kew verify`](cli/verify.md) runs on
a host: kew's gate, then the base branch's own tests against the change, which
the change cannot have edited.

<!-- demo-step: refused-pr/3 -->
**The merge verify refused it**

The merge verify failed on the head kew's review passed, so kew did not merge
it, and the pull request waits for a person's decision. It is the gate kew's
own repository merges through.
<!-- /demo-step -->

[See it in the demo](https://demo.kew.dev/work?tour=refused-pr&step=3)

The merge verify runs `kew/scripts/verify.sh` and the held-out base tests; the
demo's invented project shows it as kew runs it on itself. Its source of truth
is [the kew verify reference](cli/verify.md).

<!-- demo-step: refused-pr/4 -->
**Held-out tests**

The held-out phase runs the base branch's tests, written before this change,
against it, so a change cannot pass by editing the tests it breaks. Each
failure is listed by id; only a declared one is excused.
<!-- /demo-step -->

[See it in the demo](https://demo.kew.dev/work?tour=refused-pr&step=4)

A test the pull request declared under **Behavior changes**, with a reason, is
excused; the undeclared failures refuse the merge.

## Switchyard credential boundary

Switchyard is an inference transport, not a credential broker or a Kew runner.
Kew configuration contains the endpoint, route, versions, and SHA-256 digests;
it must never contain a credential value. Those non-secret fields and the
sanitized endpoint origin may be written to run/audit evidence. Route config
contents, authorization headers, raw proxy responses, and credential values
must not be logged or committed.

Credential ownership is split deliberately:

| Secret or authority | Owner | Switchyard exposure |
| --- | --- | --- |
| Runner/provider login | Runner operator and native CLI | The native CLI may present it to the configured endpoint; Switchyard is therefore a credential-bearing trust endpoint. |
| Upstream route credential | Switchyard endpoint operator | Kew neither stores nor audits its value. External endpoint lifecycle and revocation remain operator duties. |
| Managed proxy process environment | Kew | Empty except for the exact reviewed `credential_env` names required by the admitted route. |
| GitHub, tracker, SSH, or package credentials | Kew host boundary | Never required by Switchyard and must not enter its config, process, raw evidence, or endpoint. |

Codex's custom Responses provider requires its existing OpenAI authentication;
Claude Code uses its native Anthropic authentication against the configured
Messages base URL. Consequently, changing `endpoint` changes which server can
receive that native authorization material. Use a dedicated operator-owned
literal-loopback endpoint for managed mode. For an external endpoint, require
reviewed HTTPS identity, access control, retention, and incident/revocation
ownership before use. A successful health probe does not prove authorization.

For managed authenticated routes, `credential_env` declares names only. Kew
parses the privately snapshotted route, follows the exact route's target and
client references, and requires its `api_key_env` names to equal that declaration.
Only `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, and `ANTHROPIC_API_KEY` are
eligible. Missing, empty, duplicate, unsupported, extra, or mismatched names
fail before proxy startup. Version probes remain keyless; the validated dry run
and owned server receive a fresh environment containing only the exact matched
name/value pairs. GitHub, Linear, SSH, package, cloud, and unrelated provider
credentials remain excluded.

The credential value is never placed in `kew.toml`, the route snapshot, argv,
logs, reports, raw canary output, fixtures, or audit payloads. Audit evidence
contains only credential names plus the route-config and binary digests. Kew
clears its temporary Python mapping after launch and destroys the child
environment when the process exits. As with any environment-based delivery,
the operating system may expose a live process environment to the same user or
privileged process inspection; use a dedicated operator account where that
boundary is insufficient.

Rotate by stopping admissions, terminating managed runs, replacing the host
variable, and starting a new evidence window. Revoke at the provider first when
exposure is suspected, then stop Kew and verify no managed process remains.
Never broaden `credential_env` to work around an authentication failure.

Live passthrough canaries and benchmarks require a separate human gate. Canary
raw records can contain prompts, source, commands, paths, session identifiers,
or secrets; Kew accepts only a pre-existing owner-only record directory and
writes files mode `0600`. The operator must set a retention deadline, reduce
the evidence to non-secret checks and digests, delete the raw directory, and
confirm deletion in the UAT record
(a maintainer-held record that is
not published). Proxy Prometheus
metrics remain process-wide observations and never replace runner-reported
usage for accounting.

### Worker namespace filter proof

The SC-0 harness installs one nftables table inside the worker network namespace.
The table drops all new output except the exact proxy IPv4 address and TCP port.
It permits established traffic without permitting related traffic.
It permits no general loopback, DNS, UDP, subnet, or additional port traffic.

A short-lived helper receives `NET_ADMIN` and no other capability.
The helper receives no mounts, credentials, Docker socket, or host PID namespace.
Kew validates the complete filter readback before synthetic agent code starts.
Kew removes the helper and proves its exact container ID is absent first.
Any install, readback, or cleanup failure prevents agent startup.

The proof reads nftables and both legacy iptables families from the host namespace.
It compares the complete global rules before and after worker-only installation.
The topology remains unchanged during that comparison.
It then proves exact helper, worker, proxy, and per-attempt network removal.
Docker-managed rules may change after Docker removes an endpoint.
The proof makes no global-rule equality claim after endpoint removal.
Unavailable global readback fails the proof.
Failed cleanup or cleanup readback also fails the proof.

The complete packet-outcome matrix includes positive controls and negative worker checks.
SC-0 binds that matrix to immutable worker and proxy image IDs.
It also binds the generated filter rules, helper restrictions, and internal-network policy.
Certification expires after the existing 24-hour probe evidence window.
Future, malformed, or conflicting matching certifications refuse admission.
Terminal attempt events never certify another attempt's packet matrix.
A missing or unavailable matrix refuses a sidecar launch before agent execution.
A health response or filter readback cannot replace this matrix.

Default CI does not exercise this packet certification.
The Switchyard image tooling is opt-in: the tests that prove the matrix need the `kew-switchyard` image, which builds only from a pinned private source input.
They are marked `requires_switchyard_image` and are deselected unless a run sets `KEW_SWITCHYARD_IMAGE=1`.
Only opted-in runs — the private runner, and a local run on a machine that holds the input — exercise the certification.
An opted-in job asserts the marked tests are still selected before running them, so the certification cannot silently stop running.
The runner rule and the switch are in [Public repository runner rule](operations.md#public-repository-runner-rule).

Run this bounded operator gate from `kew/` after building the local images:

```sh
KEW_SWITCHYARD_IMAGE=1 KEW_SC2_PACKET_GATE_PROJECT=/absolute/registered/project uv run pytest tests/test_switchyard_sidecar.py::test_packet_matrix_enters_the_durable_production_contract -m serial -q -s
```

The project must already have its registered writable dispatch ledger and valid audit identity.
The gate never creates an operator ledger or registry.
It appends certification only after every packet control passes and exact cleanup succeeds.
Unavailable proof, cleanup, ledger access, or append confirmation fails the gate.
Without the explicit environment variable, the test writes only its temporary test ledger.
The printed certification names the exact immutable image IDs and filter-policy digest.
The worker tag is `SandboxConfig().image`, currently `kew-agent:docker-d148816b388f`.
The proxy tag uses the same suffix with the `kew-switchyard` image name.
This gate does not certify a different mapped worker image, including an OpenCode image.
An unproved image pair refuses admission; this node adds no runner or authentication support.
The synthetic route uses no provider credentials and makes no provider requests.
Its configuration bytes do not certify production route configuration.

Each attempt binds resource IDs to its run, attempt, route, and profile fingerprint.
Its configuration digest records the actual route configuration separately from operator packet certification.
The packet source and policy digest distinguish certification from current filter and cleanup observations.
Admission requires matching packet proof and a verified namespace filter.
Admission records cleanup as pending because the current attempt remains active.
Publication requires matching terminal evidence and successful cleanup.
Earlier attempts, continuation ancestors, and publication predecessors require complete terminal evidence.
Stale or conflicting identities refuse every publication path.

Production sidecar mode remains disabled.
W1.G owner acceptance remains a separate prerequisite for activation.

## Approval inbox

`kew serve` adds kew's first authenticated network surface: a single-page app
served at `/`, whose approval inbox lives at `/inbox` in the client router. Its
write surface, at HEAD, is these eighteen routes — **every one of them depends on
`require_write` (session cookie + `X-Kew-CSRF` double-submit token + `Origin`
check)**:

| Route | What it writes |
|---|---|
| `POST /api/approvals/{issue}` | Approve or deny a held dispatch |
| `POST /api/fleet/stop` | Stop the fleet |
| `POST /api/config/{field}` | Set a budget or `human_review.*` override |
| `DELETE /api/config/{field}` | Revert that override |
| `POST /api/verbs/resume` | Lift the fleet latch (approver-gated, `confirm: true`) |
| `POST /api/verbs/queue/{issue}` | Queue an issue (approver-gated, `confirm: true`) |
| `POST /api/verbs/epics/{ref}/retry/{item}` | Retry one epic item (approver-gated, `confirm: true`) |
| `POST /api/verbs/orphans/{pr}/ack` | Acknowledge an orphaned PR |
| `POST /api/verbs/no-change/{id}/confirm` | Accept a no-change judgment |
| `POST /api/verbs/no-change/{id}/reject` | Reject a no-change judgment |
| `POST /api/verbs/no-change/{id}/retry-sync` | Re-run the tracker sync for one |
| `POST /api/verbs/work/{item_id}/action` | Run the action the work projection derived |
| `POST /api/palette/ask` | Spend one model call ranking the palette's own entries (off unless `[serve] palette_ask`) |
| `POST /api/run-requests` | Queue one bounded kew command for the running `kew drive` to run as the operator (see below) |
| `POST /api/prompts/{role}/versions` | Propose an operator profile addendum (approver-gated) |
| `POST /api/prompts/{role}/versions/{version}/approve` | Activate a proposed addendum against the generation shown (approver-gated) |
| `POST /api/prompts/{role}/versions/{version}/decline` | Decline a proposed addendum (approver-gated) |
| `POST /api/prompts/{role}/retire` | Retire the active addendum against the generation shown (approver-gated) |

`POST /api/auth/login` is the nineteenth write-shaped route and the one
exception: it is the token→session exchange itself, so it is authenticated by
the startup token rather than by a session it has not issued yet. Reads
(`/api/*` GETs) go through `read_guard`.

`POST /api/run-requests` is the console's **Run** button (#2537), and it moves
no trust boundary into the console: the route writes one ledger row and one
`run_request_queued` audit fact, and the `kew serve` process launches nothing
and holds no credential. The supervised `kew drive` — which already runs as the
operator — picks the row up and runs it as a fresh kew child with `KEW_ACTOR`
set to the operator who queued it, so the command writes the same audit rows it
would typed in a terminal, and every gate inside it (approvers, budget,
admission) still applies. What a request may carry is a closed set — `kew
dispatch N` and `kew epic continue|retry|unhold EPIC ITEM` — each argument one
plain token with no option, no shell and no second command; the drive re-parses
the stored text before running it, so a row written around the route runs
nothing. Before it starts a spend-authorizing request the drive refuses it,
recording why, while the fleet is stopped, the drive is held, or the daily
budget is spent. With no live drive the route refuses (409) rather than queue
into nothing. Each request runs once: the drive claims it with a
compare-and-set before starting it, and a request whose drive exited mid-run is
marked `unknown`, never run again.

A Run that loses its answer on the way back is asked again, never pressed
again (#2975). The console sends one random submission key (16–64 characters
from `A-Za-z0-9_-`) with every attempt of one press. The key, the request row,
the command's active pointer and the request's strict `run_request_queued`
audit fact are written in one SQLite `BEGIN IMMEDIATE` transaction. A key never
exists without the request it queued, and no request exists without its audit
fact. Every later attempt with that key gets that request back, including one
that has since finished, before any gate runs again; it never gets a second
request. An attempt that wrote nothing left no key behind, and asking again
decides afresh. The same key sent with another command or model is refused
(409). While a command and model have a request queued or running — however
long ago it was queued — a press under any other key is refused (409) rather
than joined, and the refusal carries that request: one press is one request.
The console's lookup of a command and model always includes the request
holding them, however old, so Run and Copy stay blocked while it is unsettled.

If the audit fact cannot be written, the whole transaction rolls back and the
queue answers 424: nothing was queued, and nothing runs. A `kew serve` process
killed before that transaction commits wrote nothing. One killed after it left
a request the drive runs once, and the console's retry of the same press gets
that request back.

The console hands out the command to copy as well as Run, so a pasted command
can start beside a queued request. The drive fences that: in the transaction
that marks a queued `kew dispatch` running, it reads the item's runs. If the
item has a run in flight, or a dispatch run that started after the request was
queued, the drive settles the request `superseded` instead, naming that run,
and starts nothing. The console shows `superseded` as settled.

`GET /api/work/{item_id}/description` goes through `offbox_read`, which is
`read_guard` plus a cross-site refusal, because it is the one read whose cost
lands off-box: it asks the tracker (a `gh` subprocess against the repo in
`project.repo`, or one Linear API call), so a cross-origin page could otherwise
make the operator's browser spend a GitHub rate budget the dispatch driver
shares — CORS would stop it reading the answer, not from causing the request. A
request whose `Origin` is outside the allowlist is refused 403 (the same
allowlist writes use, so a declared `extra_origins` host still reads); a request
carrying no `Origin` but a cross-site `Sec-Fetch-Site` — an `<img src>` or a
form navigation on another page — is refused too. A same-origin GET declares
neither, so the panel is unaffected. The service
behind it also rations tracker reads (30 per minute, process-wide) so a
permitted caller cannot spend without bound either; past the ration the read is
reported unavailable rather than attempted. The ration counts only reads that
reach the tracker: a read refused before one is made — an id that is not an
issue number, no `project.repo`, no Linear key, or one of the read's own
bounds — spends none of it, so a permitted caller cannot exhaust the window
with requests that never leave the box.

`POST /api/palette/ask` writes no state an operator acts on, but it spends: each
call is one model request recorded as an `interactive_usage` row. So it takes the
full write guard, and a cross-site page cannot make the operator's browser pay
for one. The model sees the query and the ids and labels the palette sent —
never an item body, a token or a cookie — and its answer is filtered to ids that
request carried, so the most it can do is reorder entries the operator could
already see. Selecting one is the palette's ordinary press, confirm step
included.

### Auth model

**Token → session cookie.** On startup, kew writes a random 32-byte URL-safe token to `<logging.dir>/serve_token` (mode 0600, created with `O_EXCL` so it is never world-readable even briefly). When stdout is a terminal, the startup lines print the app URL with the token in the URL fragment:

```text
kew serve → http://127.0.0.1:8765/
http://127.0.0.1:8765/#token=<token>
```

When stdout is not a terminal (a service manager, a CI job, a pipe to a file), kew prints only the plain URL and a pointer to `kew serve url`, so the token never reaches a captured log. `kew serve url` prints the tokened URL on demand. With `KEW_SERVE_TOKEN` set there is no token file, and the pointer names the variable instead of printing its value.

The fragment (`#token=…`) is never sent to the server or written to any log by the browser. The app reads it client-side and POSTs it to `/api/auth/login`, which exchanges it for an HttpOnly `SameSite=Strict` session cookie and a CSRF token. All subsequent requests use the cookie; the raw token is not stored in the browser.

Sessions are held in memory and expire after 12 hours. Restarting `kew serve` clears all active sessions. `kew serve --new-token` rotates the token on disk; to fully revoke existing sessions, rotate and restart.

The `KEW_SERVE_TOKEN` environment variable overrides the file-based token (useful in scripted environments). When set, no file is written.

**CSRF + Origin check (writes).** Every write endpoint requires the `X-Kew-CSRF` header carrying the double-submit CSRF token issued at login, and rejects requests with an `Origin` header that does not match an allowed origin. The allowed set is derived from the bind address; when fronting kew with a TLS reverse proxy, the browser's public origin must be added via `[flight_recorder].extra_origins` or writes 403 (see [Reverse proxies](#reverse-proxies)).

**Baseline response headers.** Every response carries `X-Content-Type-Options:
nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and this
`Content-Security-Policy` — independent of `--expose`:

```text
default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline';
img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'none';
base-uri 'self'; form-action 'self'; object-src 'none'
```

`script-src` and `style-src` include **`'unsafe-inline'`**: the built SvelteKit
shell hydrates from inline tags, so the policy bounds *origins*, not injected
inline script. It is not an XSS backstop — read it as same-origin isolation
(no cross-origin loads, no framing, no `<base>` or form retargeting) rather
than as script control.

**Host-header allowlist.** All requests are checked against a set of allowed `Host` values (`127.0.0.1`, `localhost`, `::1`, and their port variants). Requests with an unexpected `Host` header are rejected with 403 before any auth logic runs. This defends against DNS-rebinding attacks.

**Read guard.** In the default loopback-only mode, reads from a loopback peer are permitted without a session, unless the request carries a forwarding header (`Forwarded`, `Via`, `X-Forwarded-For`, `X-Forwarded-Host`, `X-Forwarded-Port`, `X-Forwarded-Proto`, `X-Forwarded-Server` or `X-Real-IP`). Such a header marks a reverse proxy on the same host relaying someone else's request, so that read needs a session too, and a stream opened that way re-checks the session on every poll. Another local user on the same machine can still read without a session.

This detection is header-based, and it fails open. A proxy that adds none of these headers looks exactly like a local reader: nginx's default `proxy_pass`, HAProxy without `option forwardfor`, `stunnel`, `socat`, an `ssh -L` tunnel, or a proxy that strips the headers it adds. Behind any of those, every read is served session-free to anyone who can reach the proxy. The control for any proxy is `expose = true` (or `--expose`): when it is active, all **API** reads (`/api/*`) require a valid session, the loopback-peer bypass is disabled, and no header decides anything. (The SPA shell served at `/` — including `/inbox` — is static and unauthenticated; it contains no data, and every data call it makes hits the authenticated API.)

**No schema or docs pages.** kew does not serve `/openapi.json`, `/docs` or `/redoc`. With the SPA mounted, those paths return the shell like any other client route.

### Binding and exposure

By default, `kew serve` binds to `127.0.0.1:8765` (loopback only). To bind a non-loopback address:

1. Set `[flight_recorder].expose = true` in `kew.toml`, or
2. Pass `--expose` on the command line.

Either form is required; a non-loopback `host` without `expose` is rejected at startup with an actionable error. See [`kew serve`](cli/serve.md) and [Configuration](configuration.md#flight_recorder) for details.

**Exposure refuses plaintext transport.** kew serves HTTP and never terminates TLS, so an exposed bind would carry the admin token (pasted into the sign-in form) and the session cookie that follows it in the clear. `kew serve` therefore refuses to start when `expose` is on and `[flight_recorder].trusted_proxies` is empty: listing the TLS-terminating reverse proxy's address is how the operator declares that the hop in front of kew is not plaintext. In exposed mode both cookies are set `Secure` unconditionally — kew's own view of the request is plaintext HTTP behind the proxy, and that hop must never be what downgrades the cookie to one a browser will replay over HTTP.

**Forwarded headers.** `X-Forwarded-For` and `X-Forwarded-Proto` are honoured only from the addresses in `[flight_recorder].trusted_proxies` (IP addresses or CIDR networks). With none configured — the default — kew believes no forwarded header from any peer, including `127.0.0.1`; otherwise a local process could rewrite the peer address the read guard checks.

### Writes: approve, deny, stop

**Approve / deny.** Approving a hold via the inbox calls the same `decide_hold` service function as `kew approve`/`kew deny`. It releases the hold to `ready` (approve) or parks it (deny). The running scheduler detects the label change and dispatches — the web process does not spawn an agent.

Approving requires `gh` to be authenticated in the process running `kew serve`. If GitHub is unreachable, the inbox returns HTTP 502 with `gh_error` — it does not silently mark the hold as resolved.

**Stop.** The stop write signals process groups (real kill: `SIGTERM`, then `SIGKILL` after a grace period). It runs synchronously and is best-effort — it does not add a false timeout guard.

### Actor honesty

A web approval is audited with `source="web"` and the actor set to the OS user running `kew serve`. The token proves possession of the server process, not a distinct browser identity. Web approvals are identically policy-bound to CLI approvals: the `[approval].approvers` allowlist (if set) still gates whether the operator may approve.

Do not interpret the token as a multi-user identity mechanism — there is one shared token, and it authenticates the operator who controls the machine, not a named user.

### Reverse proxies

If you place `kew serve` behind a reverse proxy (nginx, Caddy, etc.), the loopback peer-check is meaningless — all connections appear to come from the proxy. Set `--expose` (or `[flight_recorder].expose = true`) so reads require a session regardless of peer; do not rely on kew's loopback bypass.

Because the browser's public `Origin` never matches kew's internal `host:port`, declare it in `[flight_recorder].extra_origins` (e.g. `["https://kew.example.com"]`) or every write 403s. Each entry is a bare origin — scheme + host, optional port, no path. See the [Reverse proxy + TLS recipe](cli/serve.md#reverse-proxy-tls) for the full nginx/Caddy setup.

Declare the proxy's own address in `[flight_recorder].trusted_proxies` as well: without it, exposing is refused at startup, and kew ignores the proxy's `X-Forwarded-*` headers.

kew does not provide TLS; TLS termination must be done by the reverse proxy.

## Dispatch owner identity

Dispatch admission requires a process-group scan that excludes foreign peers and ignores zombies.
Shared foreground pipelines refuse admission because their peers can keep the owner group alive.
Dispatch descendants may share the owner group.
Admission follows parent links across the process table to distinguish descendants from peers.
An unavailable scan retries three times over about two seconds before refusing admission.
Run kew without a pipeline, or under `setsid` where available.
See [dispatch ownership and orphan recovery](cli/dispatch.md#dispatch-ownership-and-orphan-recovery).

The durable owner identity contains PID, PGID, `owner_start_time`, `owner_ppid`, and `owner_command`.
Process snapshots observe birth, parent, command, group, and state together.
Signal authorization requires matching birth, parent, and command for a live owner.
Kernel start tokens distinguish process births that share the same `lstart` value.
Host execution requires tokens for both the owner and the attempt.
Missing tokens refuse host admission or startup with a platform-specific identity diagnostic.
Registered attempts retain their ancestry proof after owner death.
Fresh process snapshots recheck signal targets during termination and escalation.
Missing evidence grants no signal authority.
The age-gated stale sweep can reap a birthless legacy row when its recorded group is absent.
A present group or unavailable scan retains that row.
The sweep still requires death evidence for every recorded attempt.

Admission reconciles proven pending deaths before acquiring its SQLite admission lock.
The guarded transition requires pending status and unchanged ownership fields.
Unknown liveness retains the row and its capacity.
Contained orphan cleanup records retry intent before any signal.
It stops the container by durable ID before stopping the authenticated client.
Both deaths must be verified before finalization.
Unproven cleanup remains pending, and later ticks retry the recorded runtime.

## The update check contacts PyPI

`kew run`, `kew dispatch` and `kew serve` check whether a newer kew release exists. The check is one HTTPS GET to `https://pypi.org/pypi/kew-cli/json` with a 5-second timeout on each network read, and it sends no credentials and no project data. The result is cached for 24 hours under `~/.cache/kew/`, one file per install, so the request goes out at most once a day; a request that fails or times out is silent and never fails the command. kew prints the version from that response only when it is a PEP 440 public version of at most 64 characters (digits and dots, with an optional `a`, `b` or `rc`, `.post` or `.dev` suffix). Any other value, such as one carrying terminal escape sequences, a newline or shell text, is dropped, and a cached result is checked the same way before it is shown. Set `KEW_NO_UPDATE_CHECK` to any non-empty value, `0` included, to turn the check off; kew then sends no request.
