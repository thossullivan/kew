# Audit log & tamper-evidence

kew records governance events in a hash-chained audit log (`audit_events` in
`dispatch.db`). Each entry carries the hash of the previous entry, and `kew
audit verify` checks the chain. `kew audit anchor` writes the chain head to a
sink you choose. It uses `audit-anchors.jsonl` at the project root by default.
The daily `.github/workflows/audit-anchor.yml` workflow can commit and push it.

Admitted dispatch runs also retain their inference-transport identity: direct
or Switchyard mode, sanitized endpoint origin, route, managed/external flag,
expected and observed version, SHA-256 of the admitted route configuration,
the managed executable SHA-256, declared credential variable names, and the
preflight verdict. The same fields can be attached to transport audit
events. Endpoint credentials, query data, configuration contents, and provider
credential values are never persisted; only the sanitized origin, names, and
digests are evidence. These additive facts live inside the already-hashed audit
payload; they do not change the fixed chain preimage slots. Historical rows
migrate as `direct`, with unavailable evidence left null.

SC-0 writes `sidecar_packet_certification` through the strict chained append.
Its payload binds the packet-outcome matrix to immutable worker and proxy image IDs.
The `filter_policy_sha256` binds the generated filter, helper restrictions, and internal-network policy.
Every denied target retains its positive control and worker outcome.
Missing, unavailable, incomplete, or conflicting packet proof cannot authorize a sidecar attempt.
Matching certification must be no older than the existing 24-hour probe evidence window.
Future timestamps and malformed matching records refuse admission.
Terminal attempt events never become certification sources.
The operator gate in [security.md](security.md#worker-namespace-filter-proof) appends only after verified exact cleanup.
Its explicit project selection uses the existing registered ledger and refuses absent registration.
Ordinary test execution writes only an isolated temporary ledger.

The OpenShell route writes `runtime_egress_certification` through the same strict chained append.
Its payload (schema 2) carries one observed matrix per binary the policy governs and binds them to the runtime, the runner's immutable image ID, the policy bytes' SHA-256, the gateway version, a SHA-256 of the gateway's own identity, and the SHA-256 of the launcher as read inside the image.
Every denied target retains the shared positive control and its own observed outcome.
Only a passing matrix is appended, and only under the `openshell-egress-gate` policy actor.
Missing, expired, unavailable, incomplete, conflicting, or wrongly signed certification cannot authorize an OpenShell dispatch.
Matching certification must be no older than the existing 24-hour probe evidence window.
Future timestamps and malformed matching records refuse admission.
`kew sandbox certify` is the only writer, and it appends nothing when the boundary did not hold.
These facts stay inside the existing hashed payload and add no audit-chain columns or hash slots.

Each sidecar attempt writes two `sidecar_isolation_evidence` events.
The admission event uses `cleanup_verdict = pending` before agent execution.
The terminal event requires `cleanup_verdict = passed` before publication.
Both events bind these fields:

- `run_id` and `attempt_number`
- `profile_fingerprint`, `route`, and `config_sha256`
- `worker_image_id` and `proxy_image_id`
- `worker_id`, `proxy_id`, and `network_id`
- egress network, config helper, filter helper, and private storage identities
- the sanitized endpoint origin and observed proxy version
- `packet_verdict`, `filter_verdict`, and `cleanup_verdict`
- `packet_source = operator-certification` and `packet_policy_sha256`

These facts stay inside the existing hashed payload.
Packet provenance remains separate from current-attempt filter and cleanup observations.
The actual route configuration digest remains independent of the synthetic certification configuration.
They add no audit-chain columns or hash slots.
Kew persists no credential values, environment values, config bytes, or raw inspection output.

## Operator acceptance

The sidecar evidence above is narrower than it looks, and the gap matters most
at the point where someone decides the contained path is trustworthy.

A matching `sidecar_packet_certification` establishes one thing: the certified
worker and proxy image IDs produced the recorded packet outcomes under the
recorded `filter_policy_sha256`, within the freshness window that admission
enforces. It does not establish that a provider authenticated, that an agent ran
to completion, or that a contained dispatch reached a published PR. Those are
separate observations with separate evidence; certification is a precondition
for attempting them, never a substitute for having done so.

The two cases also stay separate from each other. Evidence that the native
(uncontained) route reached canonical publication says nothing about the
contained route, and evidence from the contained route says nothing about the
native one. Neither record generalizes to the other, because the credential
path, the execution boundary, and the failure modes differ.

W1.G is a human gate. It requires the owner to inspect the native and contained
canonical-publication records explicitly and rule on them; no audit event,
certification, or successful run records that ruling, and kew writes no
owner-approval receipt on its own. The evidence for that ruling is collected on
the W1.G evidence record (#1440);
until the owner records acceptance there, W1.G is unmet regardless of what the
chain contains. Production sidecar mode stays disabled meanwhile — see
[security.md](security.md#worker-namespace-filter-proof).

## Interactive usage from `kew ingest`

Usage ingested via `kew ingest file` or `kew ingest serve` is **observed external telemetry**, not an attested kew fact. It is stored in a separate `interactive_usage` table (schema v15, introduced in #256) that is **off the hash chain** — `kew audit verify` does not cover it and never will. Interactive rows are displayed in `kew report` and can trigger cap alerts, but they cannot enter the `audit_events` preimage and therefore cannot affect or perturb the chain.

The honest framing: kew records what Claude Code reports it spent; it does not enforce limits or make network-level attestation claims about interactive sessions.

## Reported-model attestation

Each run records `reported_model` — the model the runner reported in its own
output — bound into the `dispatch_completed` event's `payload` and therefore
covered by `kew audit verify`. It is **null for runners that report nothing**
(opencode) and is **inference, not network attestation**: it does not
prove which endpoint a run actually hit; a permission-skipped agent has egress
paths beyond the inference endpoint.

> Additive attested facts bind through the already-hashed `payload`, not new
> `entry_hash` preimage slots. The fixed slots (`endpoint`, `policy_hash`,
> `prev_hash`) are the envelope's grandfathered identity fields.

## Work-item addressing (`item_id`)

Every audit row also carries an `item_id` (schema v24) — the durable work-item
key from the tracker (`SFD-12` for Linear, the issue number for GitHub). Every
row written from schema v77 on hashes it: its `chain_format` is `2`, and its
`entry_hash` preimage is the literal `2`, the twelve original slots, then
`item_id` and `dedup_key`, so `kew audit verify` fails when either column is
edited on such a row. Rows written before v77 keep `chain_format` `1` and the
twelve-slot preimage, unchanged, because the published anchors cover them; on
those rows the two columns stay outside the hash. The format only moves
forward: `kew audit verify` reports a format-1 row after a format-2 row, or an
unknown format, as a break at that row. Callers thread `item_id` through
`audit.emit(..., item_id=…)` on the paths that have it (dispatch, epic driver,
merge). It is null on the classic GitHub path, where `issue_num` already
carries the key. Address a single item's events with
`kew audit tail --item <id>`.

**Stop every pre-v77 kew process before the first v77 open.** A kew process
that opened the ledger before the v77 step keeps running on its old code and
still writes format-1 rows. Once a format-2 row is stored, the database
refuses every such row, so the chain stays intact but that process's events
are not recorded. Most events are best-effort: the process logs a warning and
the event is lost for good. Events kew requires (strict writes, such as a host
lease takeover) raise instead, and the operation that wrote them fails part
way through. Stop every running kew process (`kew serve`, daemons, drives,
dispatches, verifies) before the first command of a v77 kew opens the
ledger, and start them again on the new kew.

## Recorded item plans

A launching dispatch records the acceptance criteria an item's body states, and
attests the write. Both events carry `actor_type = policy`,
`actor_id = triage-plan`, and bind through `payload` like every other additive
fact:

| Event | When | Payload |
| --- | --- | --- |
| `plan_recorded` | a new plan became the item's current plan | `item_id`, `item_plan_id`, `body_digest`, `plan_sha`, `criteria_count`, `criteria_ids`, `source` |
| `plan_superseded` | an edited body displaced the previous plan | the superseded row's `item_id`, `item_plan_id`, `body_digest`, `plan_sha`, `criteria_count`, `source`, plus `superseded_by_body_digest` |

`item_id` and `item_plan_id` name the item and the ledger row the event
describes. They are in the payload on purpose: only `issue_num` enters the
`entry_hash` preimage, so a tracker riding the `issue_num = 0` sentinel would
otherwise leave the item-to-plan binding entirely on unchained columns, where
re-pointing a recorded plan at a different item is not detectable.

`body_digest` is sha256 of the issue body exactly as the host read it, and it is
the supersession key: re-dispatching an unchanged body adds nothing, so the
chain records plan *changes* rather than every tick that re-read the same body.
Each event is keyed to the row it describes, so a re-dispatch that finds the
event already on the chain appends no duplicate -- and one that finds it
missing, because an earlier append failed, lands it then. That recovery walks
the item's whole recorded history, not just the row the dispatch wrote: a row
superseded before its own append landed is never written again, so nothing
narrower would ever retry it. `plan_recorded` takes its run id from the row it
describes and `plan_superseded` from the row that displaced it, so either
retry still names the run that read the body in question.
`plan_sha` is sha256 of the plan's canonical JSON — two bodies stating the same
criteria share it. `source` is `extracted`, `model` or `human`; an extracted
plan carries no `approved_by`, because accepting a plan is a human act.

The criteria text itself stays off the chain, in the `item_plans` ledger table
that `kew plan show` reads. The chain attests *which* plan, not its prose.

## Recorded triage decisions

The triage station classifies an item and records what it **proposed**, and a
person's resolution of that proposal is a second event. Both bind through
`payload` like every other additive fact:

| Event | When | Payload |
| --- | --- | --- |
| `triage_decided` | a new proposal became the item's current one | `item_id`, `triage_decision_id`, `body_digest`, `backend`, `model`, `prompt_sha`, `outcome`, `type`, `priority`, `complexity`, `confidence`, `actionable`, `needs_info`, `duplicate_of`, `superseded_triage_decision_id` |
| `triage_resolved` | a person decided what happens to a proposal | `item_id`, `triage_decision_id`, `body_digest`, `outcome`, `resolution`, `resolved_by`, `duplicate_of` |

The actors differ, and that is the point. `triage_decided` carries
`actor_type = policy`, `actor_id = triage-station`: a model classified, and the
host's policy derived the outcome. `triage_resolved` carries
`actor_type = human` with the resolver's own id, because resolving a proposal is
a human act and there is no code path in kew that supplies an actor for it.

`body_digest` is the supersession key, the same sha256 of the issue body that
[recorded item plans](#recorded-item-plans) use — so a plan and a decision read
from one body are joinable. Re-triaging an unchanged body adds nothing to the
chain. `prompt_sha` is sha256 of the rendered prompt with its per-render
sentinel normalized, so the same inputs always produce the same identity.

`outcome` is one of `propose_dispatch`, `ask`, `duplicate_candidate`,
`human_decision`, `split` or `unavailable`. **Every one of them is a proposal**:
nothing in the triage path applies a label, writes a comment, marks an item
`ready` or closes one. `unavailable` is the fail-closed outcome — an unreachable
classifier, or JSON that never validated after one repair retry — and it can
never become a dispatch.

The classifier's answer itself stays off the chain, in the `triage_decisions`
ledger table that `kew triage queue` reads. The chain attests *which* decision
and *who* resolved it, not the model's prose.

## Preserved-work recovery evidence

An interrupted epic continuation is recorded as a lineage rather than as a
fresh, unrelated dispatch. Recovery audit payloads use the following stable
fields:

- `original_run_id`: the run that first owned the checkout;
- `continuation_run_id` — the explicit recovery claim, when one exists;
- `item_id` and `branch` — the durable tracker and branch identities; and
- `worktree_identity`: a `sha256:` digest of the canonical checkout path.

The digest is deliberately used instead of the local path, which may disclose
usernames or host layout. Identifiers are bounded and control characters are
replaced before they enter the chained payload. The evidence is additive JSON
payload data, so existing `kew audit verify` chain semantics remain unchanged.

The driver records `continuation_detected`, `continuation_claimed`,
`continuation_started`, `continuation_publication`, and
`continuation_terminal` observations. Generic `dispatch_completed` and
`publication_deferred` events emitted while a continuation owns its reservation
are enriched with the same lineage. Polling is idempotent: repeated
observations of one generation do not create duplicate state evidence.

Idempotency is enforced atomically (schema v39). Each continuation payload
carries an `evidence_identity` — `continuation:v1:sha256:` over the event name
and the stable lineage fields above — inside the hash-chained payload, and the
same identity is stored in an additive `dedup_key` column backed by a unique
index. Appends of the same identity are resolved inside one write
transaction (check and insert under the same lock), so concurrent writers
observing the same recovery produce exactly one chained event rather than
racing a read-then-append window. The column enters the `entry_hash` preimage
of every row written from schema v77 on, as `item_id` does (see Work-item
addressing above); rows written before that keep it out.

Recovery is fail-closed across the interruption matrix. A claim whose child
crashes before writing a run row returns to `awaiting_continuation` after the
startup grace; a dead child with dirty work reopens the original continuation
record and leaves the pending row and checkout intact. A live or unprobeable
owner is never inferred to be dead from database state alone, and concurrent or
repeated resume attempts remain governed by the generation/ownership checks in
the run store.

One recovery has a runtime of its own in its payload. On `[sandbox] runtime =
"openshell"` a gateway restart kills the agent and keeps its files, so the
driver records an `openshell_gateway_restart` event naming the `epic_ref`, the
`sandbox` it recovered from, a `reason` of `openshell_gateway_restart`, the
`checkout` the work was brought home to — the host dispatch checkout
`<worktree_base>/agent/issue-<slug>`, which is the only path a continuation can
be registered against — whether the checkout came back
(`checkout_recovered`) and what the merge did to that checkout
(`checkout_merge`: `merged`, `unmerged` for a checkout left exactly as it was,
or `part_merged` for a merge that failed half-way and left it holding some of
the sandbox's files and not the rest). A full download whose tree is not a
checkout of the item's branch — a sandbox handed back on a detached HEAD, say —
is `unmerged` too: `merged` is what deletes the sandbox, so it is claimed only
of a tree a continuation can actually be read from, and a tree that fails that
is taken back off the host rather than left to refuse the item's next dispatch.
A `merged` outcome is followed by the
ordinary preserved-work events — a `continuation_detected` naming that same
checkout — because from there the recovered work is an ordinary dirty orphan.
The `checkout` is empty when the run bound no launch checkout at all, which is
itself an `unmerged` outcome. It is additive JSON payload data on the existing
chain; `kew audit verify` semantics are unchanged.

## Dispatch stream evidence

Kew derives a run's structured evidence **while the provider streams** and then
discards the raw lines, so no raw provider response, private reasoning block or
transcript is ever written to a log, an event file, the audit ledger or a
receipt. Four `evidence.*` events carry what survives:

| Event | When | Facts |
|---|---|---|
| `evidence.artifact` | before a contained launch | the exact compiled prompt's `digest`, `byte_length`, `storage`, `locator` — never its bytes |
| `evidence.receipt_fact` | before a contained launch | `trusted_base_sha`, the base commit the host observed in the checkout (or the explicit value `unavailable`) |
| `evidence.runner_progress` | at run finalization | the terminal event type, plus a `steps=… tool_uses=… report=…` counter line |
| `evidence.runner_usage` | at run finalization | `steps`, `tool_uses`, `steps_to_first_edit`, `tokens_to_first_edit` |

Every fact derived from a runner's own stream carries
`producer_kind = "runner"`, so a runner's account of its own work stays
distinguishable from a host attestation forever. Facts are a closed set per
event kind: there is no field a transcript, a prompt or a reasoning block could
land in, and the writer refuses one that tries.

Per-run **derived-evidence sidecars** (`<log>.evidence.json`) hold the same
facts beside the sanitized operational log; they are what `--include-logs`
archives. A run recorded before this contract has a raw transcript and no
sidecar: its metrics read as **unavailable**, and kew does not re-ingest the
transcript to fill the gap.

## Publication-shadow outcomes

Sandbox-broker dispatches with checkout publication append one terminal
`publication_shadow_outcome` event before reporting or pruning evidence.
Canonical publication produces no safe legacy comparison. It appends
`shadow.skipped` with reason `canonical_publication` and prints one notice.
A disabled shadow emits no event. Comparison and audit failures remain
fail-soft.

The envelope uses actor `policy:publication-shadow` and carries the canonical
repository plus the dispatch's real `run_id`, `issue_num`, and `item_id`.
`kew audit tail --event publication_shadow_outcome --item <id> --json` therefore
joins the observation to both the work item and run. The atomic append-once key
is `publication-shadow:v1:<run_id>`, so retries and concurrent observations
cannot add a second terminal row.

Every terminal status (`match`, `divergent`, `refused`, `review_required`, or
`failed`) has the same version-1 payload: `item_id`, `branch`, `base_branch`,
`runtime`, configured `image`, `runner`, orchestrator version/commit/dirty
provenance, `status`, `base_sha`, legacy and broker tree IDs, entry/deletion and
full rejection/flag counts, bounded `detail`, bounded rejection/flag samples,
and `evidence_basename`. Unavailable scalar values are explicit nulls; counts
and samples are zero/empty. `item_id` is intentionally duplicated in the
payload because the envelope's `item_id` column is hashed only on rows written
from schema v77 on; the payload copy is hashed on every row.

The payload retains at most five sanitized 256-character samples per finding
kind and a sanitized one-line detail of at most 512 characters. It records only
the retained evidence directory's basename, never its host path or the full
finding lists. The locally resolved legacy tree remains available when a later
comparison stage fails. These bounded identities remain useful after on-disk
evidence retention prunes the workspace.

## Sandbox posture attestation

Dispatch, review remediation, conflict resolution, `kew ci-fix`, and `kew epic
plan` each resolve and attest their posture. They attempt to emit a
`sandbox_resolved` event with the effective containment posture. Audit writes
are best-effort. A missing store or failed append drops the event. Therefore, a
run without a posture event proves nothing about its posture.

`kew ci-fix` and `kew epic plan` use ordinary dispatch resolution. They resolve
and attest their posture before launch. First-class loops remain separate. They
do not resolve sandbox posture or emit this event. Their agent runs on the host
(see [Security](security.md#sandboxed-dispatch-credential-posture)). The
`posture` values are: `posture` is `sandboxed` (ran in a container),
`native` (the runner enforced its code-declared native boundary), `uncontained`
(`dangerously_skip_containment = true` explicitly admitted host execution),
`refused` (containment was required
but unsatisfiable — no runtime under a fail-closed setting, or a runner with no
mapped image / no declared credential — so the dispatch did not run at all),
`uncontained_flow` (the run went ahead on the host because containment was not
available to it — see below), or `host` (the fallback token a tracked launch
records when its flow resolved no posture at all and no container image: the
agent ran on the host, and the ledger says so rather than staying silent).
`refused` is attested precisely so a dispatch that never ran leaves the same
kind of record as one that ran uncontained. `runtime` is the resolved runtime
when contained, `runner` is the resolved runner the posture was decided for,
`image` is the container image that runner resolved to (`null` when not
container-contained), and `enabled` echoes the tri-state config value. A native
event has `runtime = null` and `image = null`, because no external runtime or
image participated. When the event was recorded, the ledger shows for that
run whether and how it was contained.

`uncontained_flow` belongs to the two remediation flows, which deliberately
**warn rather than refuse**: `[sandbox] enabled = true` makes dispatch fail
closed, and extending that here would stop `kew review` working on any machine
without a container runtime. Its `reason` names the specific obstacle — a
missing runtime is the operator's to fix, an unmapped image is kew's, and
"uncontained" alone cannot tell them apart. It is deliberately a distinct token
from `refused` (containment was required and the run did not happen). These are
different facts that a ledger spelling them the same could not query apart.

On a `refused` posture, `reason` names which of the several causes fired —
without it the causes are byte-identical on the chain:

| `reason` | Cause | `runtime` / `image` |
|---|---|---|
| `runtime_unavailable` | Containment could not be provided and the escape hatch was not set: no container runtime on the host, `enabled = false` without `dangerously_skip_containment`, a runner that cannot be contained, or a first-class loop whose runner does not contain itself (#1977) | both `null` |
| `openshell-runtime-unavailable` | A validated `[sandbox] runtime` this build cannot launch THIS dispatch into — the runtime was never probed, so the host may well have it installed. Either the runtime name has no launch path at all, or `runtime = "openshell"` resolved something the gateway path cannot serve: a sidecar Switchyard topology, or a remediation flow that launches its agent as a container command line. Each would be refused at the launch seam after the posture had been attested, so each is refused at admission instead. On `openshell` it is asked after the egress certification gate below, so an uncertified boundary is refused as `openshell-egress-uncertified` first | both `null` |
| `openshell-egress-uncertified` | `[sandbox] runtime = "openshell"` is selected and no passing egress certification matches the boundary this dispatch would launch — absent, expired, bound to a different image, policy, gateway version, gateway identity or launcher, signed by the wrong actor, or unreadable because no ledger was available. A runner with no packaged OpenShell policy never reaches this gate: it is refused first, as `openshell-policy-unpackaged`. Checked before the launch-path checks behind `openshell-runtime-unavailable`, because `kew sandbox certify` clears it | runtime set; image set once it resolved |
| `openshell-policy-unpackaged` | `[sandbox] runtime = "openshell"` is selected for a runner this build packages no OpenShell policy for (`pi`, `hermes`) — there is no boundary for `kew sandbox certify` to probe, so the refusal names the missing policy instead of that command. Checked before the ledger is read, because packaging a policy is the only fix | runtime set, image `null` |
| `continuation_host_only` | A preserved continuation must run against its existing checkout on the host, but the resolved posture requires a container | as resolved |
| `image_unmapped` | The resolved runner has no `[sandbox] images.<runner>` entry | runtime set, image `null` |
| `credential_undeclared` | The runner has an image, but kew declares no in-container credential for it | both set |
| `credential_missing` | The runner's credential is not in the environment, under a fail-closed setting | both set |
| `host_free_space` | The host's free space (project device or checkout tempdir) is under the sandbox floor; the watchdog would have killed the run at its first tick | both set |
| `checkout_over_quota` | The materialised checkout already exceeds the byte or entry quota | both set |
| `proxy_unparseable` | Under `proxy_environment = "inherit"`, a present HTTP or HTTPS proxy value does not parse as a URL, so its credentials could not be redacted. Posture resolution refuses the run before any sandboxed posture is recorded; the container-launch and image-build checks that back it up raise without a ledger row | both set |

`runtime` and `image` carry what resolution actually saw before refusing, so a
refusal that happened *because* a runtime was present is never recorded as
runtime-null. `reason` is `null` on the `sandboxed`, `native` and `uncontained`
postures; `uncontained_flow` carries the obstacle named above.

**2026-09-13 — `egress_verification` added.** The `sandbox_resolved` payload now
records the resolved egress evidence. This field is an additive attested fact.
It binds through the hashed payload under the forward rule. Existing hashes
stand, and historical payloads can omit this field.

**2026-09-13 — complete sandbox posture boundary added.** Each new dispatch
adds five fields to `sandbox_resolved`:

- `filesystem_boundary` names the container mount, native workspace, or host.
- `network_boundary` records egress evidence or `not-applicable`.
- `credential_names` records a names-only credential exposure bound.
  Contained runs list injected credential names.
  Host and native runs list every parent non-`KEW_*` name.
  Publication and runner filters can remove names before launch.
- `command_guard` records `loaded` only for a configured executable hook.
  The hook bytes must match the trusted project copy.
  Other values are `absent` or `not-applicable`.
- `admission` records `policy` or `dangerously_skip_containment`.

These fields are additive attested facts under the forward rule.
They bind through the existing hashed payload.
Existing hashes stand.
Historical payloads can omit these fields.
New dispatch events also carry `run_id` for an exact surface join.
Surfaces never infer posture from requested configuration.
They show `posture: not recorded` when the event is absent.

**2026-10-06 — effective resource caps added.** The `sandbox_resolved` payload
now records the per-container caps the dispatch resolved under, as `max_memory`
(a Docker size string) and `max_cpus` (an integer). Both keys are present on
every posture, so one payload schema covers them all — but only the `sandboxed`
posture launches a kew container, so only there is a cap a fact. On `native`,
`uncontained`, `uncontained_flow`, `host` and `refused` both are `null`, because
no cgroup enforced a cap there. A `null` cap is not the sentinel `runtime` and
`image` carry: a `refused` event deliberately records both, naming the runtime
and image the launch tried and could not get, and it still records no caps. Under
`sandboxed`, with `[sandbox] max_memory` / `max_cpus` unset they record the hard
ceilings (`8g`, `4`). `max_cpus` is the ceiling the launch asked for — `--cpus`
is additionally clamped to the daemon's own CPU count, which only the launch can
know. Both are additive attested facts under the forward rule, binding through
the existing hashed payload: existing hashes stand, and historical payloads can
omit them.

A `sandboxed` dispatch on `runtime = "openshell"` records both as `null` too.
kew creates that container through the gateway (`openshell sandbox create`),
whose argv carries an image, the run's credential providers, the runner's egress
policy and labels — and no memory or CPU flag — so no cgroup limit of kew's
exists there to attest, whatever `[sandbox] max_memory` says. The same event's
`proxy_variables` records nothing inherited and nothing blanked on that runtime
for the same reason: proxy names reach a container as `-e` arguments, a gateway
sandbox has no argv to carry them, and the exec forwards an allowlist that holds
none of them — so `credential_names` does not name them either.
`proxy_environment` still records what the operator configured, because that is
a config fact; a dispatch that set a cap or `proxy_environment = "inherit"` on
this runtime is warned at launch that it cannot take effect.

A refused **epic node** is also HELD with a `sandbox_containment:`-prefixed
reason and writes a terminal `refused` run row (`error_category`
`containment_refused`) keyed to the driver's reservation, so the fleet worker
slot is released immediately rather than at reconcile-grace expiry. The hold is
cleared by the operator with `kew epic unhold` once an image is mapped — kew
cannot observe a config it never got far enough to resolve.

## Remote placement attestation

A run placed on a remote host (`[sandbox] host = "gcp"`, see
[Configuration](configuration.md#sandbox-host)) writes one
`dispatch_remote_launch` event when its container is launched there. Its
`actor_type` is `host` and its `actor_id` is the instance. The payload names:

- `host` — the provider the run was placed on (`gcp`).
- `instance` — the instance the container ran on, so a pool's report can say
  which machine answered.
- `image` and `runner` — the image the container ran and the runner behind it.
- `image_built` — whether the instance had to build the pinned image from the
  repository it was just shipped, rather than already having it.
- `credentials` — the model credential the container received, **by name**. The
  name is the whole of what may ever be recorded: the value travels on the SSH
  session's stdin into `docker run --env NAME`, so it reaches no file, no
  command line and no log. Every non-model credential name is refused before
  the instance is touched, and no credential at all reaches a non-dispatch run
  kind.

Like every other attested fact, these bind through the hashed `payload` — not
as new columns or `entry_hash` preimage slots: existing hashes stand, and a
historical payload can omit these fields.

## Governed-review verdict attestation

When the governed adversarial-review stage is enabled (`[review].adversarial`),
a review round emits an `adversarial_review_verdict` event — an additive
attested fact bound entirely through the hashed `payload` (no new chain preimage
slots).

Every governed review round emits this event, from the epic driver and from
`kew review --governed`. A pull request merged out of band while the
drive's reviewer is still running is attested `MERGED_UNREVIEWED`.

The payload carries:

- **Join keys** `pr_number`, `item_id`, `head_sha` — so "show me this merged
  PR's review provenance" is a chain lookup, not an inference.
- `round`, `verdict`, and `sidecar_digest` (the digest is over kew's
  host-stored sidecar copy, never a deletable GitHub comment).
- `reviewer_model` and `prompt_artifact_sha` — which model, running which pinned
  prompt version, produced the verdict.
- A `policy` block (`blocking`, `max_rounds`, `max_review_usd`) binding the
  verdict to the policy in force (#250 precedent): the same `NEEDS_CHANGES` means different things under `blocking = true` vs
  `false`, so the policy travels with the verdict.
- `spend_source: run_records` and `spend_usd` — the review-pool spend is summed
  from kew's own RunRecords; the reviewer's self-reported cost is dropped.
- `review_mode` — `full` (the whole PR diff) or `delta` (only the change since
  the previous reviewed head), so a delta `READY` is distinguishable from a full
  one on the chain. `null` on the degenerate verdicts, which reviewed nothing.

- On a crashed round, `failure_layer` and `error_category` name the layer that
  failed, and `refusal` says what was wrong for a reviewer that started: why its
  stream carried no admissible verdict — for example the failed run's exit
  status, or "the reviewer stream carries 2 result events" — or, when the
  review raised after its run was recorded, the raise. Absent for a reviewer
  that never started: there was no stream, and `error_category` says why.

A round whose reviewer exited cleanly but answered more than once is run again
once per node and head instead of held; that retry is recorded as an
`adversarial_review_retried` event (`item_id`, `head_sha`, `error_category`,
`refusal`), and no verdict is attested for it. The retry bound is read back
from these events, so the append is strict: a retry whose event cannot be
written holds the node instead of running again.

### Payload-shape history

**2026-10-25 — `refusal` added (#3186).** A crashed round's verdict now carries
the stream's refusal text, so the chain says what was wrong with the capture and
not only that it was. Rows appended before this date carry no `refusal`, and they
keep verifying: the field is additive through the hashed payload.

**2026-09-25 — `review_mode` added (#2111).** Delta rounds review only the
change since the previous reviewed head, so a `READY` now needs to say which
scope produced it: without `review_mode` in the payload, the chain cannot tell a
delta `READY` from a full one and the rule that a `READY` always has a full pass
within the last two rounds is unverifiable from the chain. Verdict rows appended
**before this date carry no `review_mode`**, and they keep verifying — every
round before delta review existed was a full review. Additive through the hashed
payload, so no `entry_hash` or anchor is affected.

**2026-08-25 — `policy.effort` removed (#1649).** The policy block used to carry
an `effort` field copied from `[review].effort`. That config key never reached
the reviewer command, so the attested value described nothing that happened; the
key and the payload field are both deleted. Verdict rows appended **before this
date carry `"effort"` inside `policy`**, and they keep verifying: the chain
hashes each row's stored payload TEXT, so a payload-shape change is never a
chain break and no `entry_hash` or anchor is affected. This is the forward rule
running in reverse — a fact bound only through the hashed payload can be
dropped the same way it was added. The reviewer tier is whatever the pinned
runner defaults to; `reviewer_model` is the payload field that attests which
model actually produced the verdict.

Beyond the reviewer's `READY` / `NEEDS_CHANGES` sidecar verdicts, the driver
attests degenerate verdicts so the chain never goes silent on a short-circuited
stage: `DEGRADED` (advisory reviewer crash after retry), `MERGED_UNREVIEWED`
(a human merged over a live reviewer — `actor: out_of_band`), and `PRE_POLICY`
(the flag flipped after the node was already in review). A delta round's `READY`
with no full review in the two rounds before it is attested as `NEEDS_FULL`
(see [How it works](how-it-works.md)): not ready, and the next round is a full
review. So is a full round's first `READY` on a head that touches merge gate
paths, until a second full round on that head confirms it. All shapes hash-verify
identically — `kew audit verify` covers them like any other event.

## Review outcome attestation

Every terminal path of a review dispatch — `kew review <PR>` and the governed
remediation leg alike — emits a `review.outcome` event. The point is
watcher-independence: a review that completes, or fails *before it ever
launches*, used to leave nothing behind but an exit code and some terminal
output. The event is an additive attested fact bound entirely through the hashed
`payload`, like every other post-freeze fact.

The payload carries:

- **Join keys** `pr_number`, `item_id`, `head_sha`, `branch`.
- `status` — `completed`, `failed`, or `refused` (stopped before launch by
  the operator's own prompt configuration; nothing failed).
- `stage` — where it ended: `pre_launch`, `runner`, or `publication`.
- `classification` — a stable machine-readable cause. Pre-launch:
  `budget_exhausted`, `github_unreachable`, `github_auth`, `reservation_conflict`,
  `head_unresolvable`, `head_mismatch`, `checkout_busy`, `checkout_dirty`,
  `checkout_unusable`, `prompt_configuration_refused`. Post-launch: `completed`, `dispatch_error`,
  `runner_failed`, `publication_failed`.
- `detail` and `exit_code` — the human-readable cause and the runner's code.

Query it after the fact with `kew audit tail --event review.outcome`, or from
Python with `kew.review_outcome.list_review_outcomes()` /
`latest_review_outcome()`. The same outcome is pushed to the configured notifier
(GitHub comment and/or webhook) — best-effort, and always *after* the durable
record is written, so a notifier outage can never cost the record.

## Certificate evidence appends

Journey-certificate evidence does not live in the audit database, and it does
not live in the commit it certifies. It lives on the
`refs/notes/kew-certificates` evidence ref: one immutable typed envelope per
statement and one append-only manifest per certified SHA. Attaching or updating
evidence therefore never changes the SHA being certified.

Every accepted append also emits one `certificate_appended` event, so the
evidence ref and the ledger corroborate each other rather than standing alone.
The payload carries:

- **Join keys** `subject` (the exact certified SHA), `lane`, `fingerprint`.
- `kind` — `certification_ready` (deterministic build acceptance),
  `journey_certificate` (a certificate bound to an exact lane and live trace),
  or `certificate_status` (a later `recertification_due` / `suspended` append).
- `status` — the envelope's own verdict, drawn from the vocabulary its `kind`
  allows; no kind can borrow another's.
- `digest`, `oid`, `predecessor` — the envelope's canonical digest, its git
  object id, and the manifest entry it follows.
- `ref`, `ref_before`, `ref_after` — the compare-and-swap the append performed.
- `actor` and `recorded_at`.

The resolver is fail-closed in both directions. A `journey_certificate` reads
as `certified` only once its append is bound to a `certificate_appended` row
**and** that row is covered by a verifiable current anchor. An unfetched,
unreadable, stale, or invalid carrier reads as `certificate evidence
unavailable` — never `uncertified`, and never a preserved `certified` claim.
Reading the carrier successfully and finding nothing for a subject is the only
thing that reads as `uncertified`.

## Merge-verification attestation

[`kew verify`](cli/verify.md) appends one `verify_completed` event per
verification, whether it passed, failed, or never finished. A gate nobody
recorded is a gate nobody can show afterwards, so a failure is recorded on the
same terms as a pass.

| Payload key | What it records |
| --- | --- |
| `ref` / `ref_sha` | the ref as the operator named it, and the commit it resolved to |
| `base_ref` / `base_sha` | the base ref kew found, and the merge base the verification ran against |
| `host` | where the job ran (`local`, `gcp`) |
| `exit_code` | the job's own status, or `null` when the host produced none |
| `seconds` | wall-clock duration, measured by kew around the job |
| `outcome` | `passed`, `failed` or `incomplete` |
| `frontend_changed` / `ui_check` | whether `frontend/` moved against the base, and so whether the browser check ran |
| `error` | why an incomplete verification never produced a status, or what went wrong around a job that still answered — a host kew could not release, say |
| `shards` / `collected` | present only for a [sharded](cli/verify.md) verification: how many shards ran, and the unsharded collection their counts were summed against |
| `jobs` | one row per job of a sharded verification — `job`, `instance`, `exit_code`, `seconds`, `selected`, `outcome`, `error` — so the record says which host failed, or which one went silent |
| `head` | the PR head the verification tested: a merge commit's non-base parent, or the ref itself |
| `pr` | the PR the verification was for — its merge claim's, or the one the drive's merge pass names — or `null` for a bare `kew verify <ref>` |
| `phases` | each phase the job's `kew-phase:` markers named, with its `seconds` and `outcome` |
| `failing_phase` / `failing_ids` | the first phase that did not pass, and its failing ids: held-out's undeclared ones, or the ids pytest's short test summaries name |
| `changed_inputs` | the phases whose inputs the candidate's diff touched |
| `behavior_changes` | a digest of the PR body's **Behavior changes** section, or `null` when the verification was given no body |
| `log_path` | where the verification's log is kept, under `verify-logs/` beside the ledger (pruned after 30 days) |

Everything in that payload is data the host returned or kew measured; nothing
the host prints is read as an instruction, and no credential is sent to it.

Three more events come from the [verify ledger](cli/verify.md#report), once
the same PR's next verification is recorded:

| Event | Payload |
| --- | --- |
| `verify_red_resolved` | `red` and `next` (the two `verify_completed` rows), the resolution `class`, the red's `phase` and `failing_ids`, and the `pr` and `head` |
| `verify_flaky_filed` | the `test` id, the `issue` it was filed as, and the `red` and `green` rows; once per test id |
| `verify_ledger_failed` | a resolution or filing that failed or was skipped: the `step` (`resolve`, `label`, `file` or `skip`), the rows it concerned, the `test` when it was one, and the `error` or the `reason` — a filing kew declined, such as an id that is not a plain test id or whose file does not exist at the head |

## Acceptance contract amendments

An implementer never edits its node's acceptance contract; it disputes it, and a
person amends it (see
[`kew epic acceptance amend`](cli/epic.md#kew-epic-acceptance-amend-amend-a-nodes-acceptance-contract)).
Four events record that path:

| Event | Actor | Payload |
| --- | --- | --- |
| `acceptance_disputed` | the implementer run (`agent`) | `epic_ref`, `node`, the `dispute` (`criterion`, `test`, `problem`, `evidence`, `proposed_fix`), the disputing `run_id`, and the disputed `contract_version` and `contract_sha256` |
| `acceptance_dispute_referred` | the person (`human`) | the same, plus their `reason` and `referred: true` |
| `acceptance_amended` | the person (`human`), or the author run (`agent`) when the author step amended it | `epic_ref`, `node`, `reason`, `path`, `old_version`/`new_version`, `old_sha256`/`new_sha256`, `commit`, `author_run_id` (`hand:<actor>` for a hand amendment), and the `dispute` it answered, or `null` |
| `acceptance_dispute_superseded` | the author run (`agent`) that wrote a new contract for a changed plan | `epic_ref`, `node`, the new contract's `new_version`, `new_sha256` and `plan_sha`, and the `dispute` it closed: the tests it named belonged to the contract for the old plan |

## Standing merge rule attestation

[`kew drive`](cli/drive.md)'s standing merge rule (`[drive.merge]`) is signed on
the chain. The first time the drive reads a rule, and whenever it changes, it
appends a `drive_merge_rule_signed` event — actor the GitHub login `gh` runs as
— but only when that login is the signer of record. A changed rule read under
any other login is refused and records nothing.

| Payload key | What it records |
| --- | --- |
| `signer` / `signed_by` | the rule's signer, and the login that signed this version |
| `fingerprint` / `version` | the sha-256 of the rule as signed, and the gate-class list version it covers |
| `previous_signer` / `previous_fingerprint` | the rule this one replaced, or `null` for the first |
| `ui_paths` / `gate_classes` | what the rule counts as a UI change, and the gate classes it never merges |

A merge the rule performs is an `epic_operator_merge` event with `trigger:
"drive"` and actor `drive-merge-rule:<signer>`. A READY PR the rule does not
cover is recorded once per head as `drive_merge_declined` — `epic`, `pr`,
`sha`, `reason`, `paths` and the `rule` fingerprint — and left for a person.
A verify host the drive cannot reach is recorded once per outage as
`drive_verify_host_unreachable` — `host`, `detail` and `local_fallback`.

## Drive restarts {#drive-restarts}

A [`kew drive`](cli/drive.md) that takes the drive lock from a holder that did
not stop appends one `drive_restarted` event, with the operator the new drive
runs as for its actor. Its payload is read off the lock row the previous drive
left in the ledger. The write is required: when it fails, the drive hands the
lock row back and exits abnormally, and the next start records it. `previous_pid` and
`reason` lead the payload, so the console's excerpt always shows them.

| Payload key | What it records |
| --- | --- |
| `pid` / `host` | the drive that started |
| `previous_pid` / `previous_host` | the drive whose lock it took |
| `previous_started_at` / `previous_heartbeat` | when that drive started, and its last heartbeat |
| `reason` | `gone after reboot` (this host booted after that heartbeat), `exited` (its pid is gone) or `stalled` (its heartbeat went stale) |
| `detail` | why the lock was reclaimed, as the drive logged it |

## Repository renames {#repository-renames}

[`kew rename-repo`](cli/rename-repo.md) moves the ledger's rows from a renamed
repository's old slug to `project.repo`, but never an audit row: each row's
`repo` is in its hash. It appends one `repo_renamed` event instead, recorded
against the new slug, with actor the operator, in the same transaction as the
rewrite.

| Payload key | What it records |
| --- | --- |
| `from` / `to` | the old slug and the new one |
| `rewritten` | rows rewritten, by `table.column` |
| `through_audit_id` | the id of the last audit row before the event |

A rename of the same two slugs is recorded once: `kew rename-repo` refuses to
append a second one.

`kew report provenance` and the merge-verify reading count a row recorded
against `from` as `to`'s when its id is below the earliest such event. A row
recorded against `from` after the event belongs to whatever repository holds
that name now: it does not count for `to`, and the rows before the event do
not count for that repository. Renames chain: `a → b`, then `b → c`, counts
`a`'s rows before the first event for `c`. A rename into a name that was
already renamed away joins the name's new holder: after `b → c`, a later
`a → b` counts `a`'s rows for `b`, not for `c`. The reverse holds too: after
`a → b`, then `b → c`, the repository that later takes the name `b` counts
neither `a`'s rows nor the old `b`'s.

## Run requests

A command queued by the console's **Run** button or
[`kew run-request queue`](cli/run-request.md) is recorded at each step, every
event with actor the operator who queued it and the item it names:
`run_request_queued` (by the queueing surface), then — by the drive —
`run_request_refused`, `run_request_superseded` (a dispatch whose item already
ran, #2975), or `run_request_started` and `run_request_finished`.

| Payload key | What it records |
| --- | --- |
| `request_id` / `command` | the request and the exact command it carries |
| `source` | `web` (the console) or `cli` |
| `state` | the request's state after this step |
| `reason` | why the drive refused it (`run_request_refused`) |
| `superseded_by` | the item's run that fenced it (`run_request_superseded`) |
| `pid` / `drive` | the child the drive started, and the drive's token |
| `exit_code` | the child's exit code (`run_request_finished`; absent when unknown) |

The command the drive runs writes its own events besides, as that operator
(`KEW_ACTOR`), exactly as it would typed in a terminal.

## Commands

- `kew audit verify [--anchors PATH] [--json]` — verify chain integrity.
  Exit `0` intact (and for an empty log), `1` tamper detected, `2` cannot run
  (no database, or an unreadable `--anchors` file).
- `kew audit anchor [--anchors PATH]` — append the current chain head
  `{id, entry_hash, count, timestamp, prev}` to the anchors file, binding the
  previous anchor via `prev` (the anchor chain). Pure-local; the scheduled
  `audit-anchor` workflow runs it and commits/pushes the file. Refuses to
  extend a broken local chain or a re-chained anchor history.
- `kew audit tail [-n N] [--event TYPE] [--item ID] [-f] [--json]` — show the
  most recent audit events (oldest-first). `-n` defaults to 20; `--event`
  filters by event type; `--item` filters by durable work-item id (e.g.
  `SFD-12`, or a GitHub number `42`); `-f` polls for new events every second
  (like `tail -f`). The event line addresses each row by work item using
  `item_ref` semantics — a bare tracker id (`SFD-12`) for non-GitHub items and
  `#N` for GitHub numbers.
- `kew audit export [-f json|csv] [-o PATH] [--since ISO8601]` — dump the full
  audit trail in JSON (default) or CSV to stdout or a file, optionally filtered
  to events at or after a given date. Useful for piping into external SIEM or
  compliance tooling.
- `kew audit policy [HASH] [--json]` — show the current effective governance
  policy, or resolve a retained policy hash to the snapshot that was in force
  when a specific run or event was recorded. This is the companion lookup for
  the `policy_hash` field documented in [Governance attestation](configuration.md#governance-attestation)
  — every `dispatch_completed` event carries a `policy_hash`; pass it here to
  see exactly what policy was active at dispatch time.

See [`kew audit verify`](cli/audit.md#kew-audit-verify) and
[`kew audit anchor`](cli/audit.md#kew-audit-anchor) in the CLI reference for
full flag details and exit-code semantics.

Tamper-evidence and durability are complementary: the chain proves integrity of what was written; backups protect against loss. See [Operations](operations.md) for backup/restore, retention, and migration safety.

Each event also carries a `policy_hash` binding it to the governance policy in
force when the event was recorded (#250) — dispatch runs and the governance
commands (`kew stop`/`approve`/`deny`) alike; because `policy_hash` is part of
the hash-chain preimage, the policy binding is covered by `kew audit verify` at
no extra cost.

Since 2026-08-25 (#1582) the policy snapshot includes `run_reports.sink_path`.
This additive attested fact binds the configured report destination through
the hashed payload. Events recorded before that date keep their original
snapshots and hashes and do not carry the key; `POLICY_SNAPSHOT_VERSION`
stays 1 because the change is additive.

## What tamper-evidence here does and does not mean

1. **Completeness.** Audit writes are best-effort and never block dispatch, so
   the chain proves the integrity of *what was written*, not that nothing is
   missing.
2. **No secret.** There is no keyed MAC: anyone with disk access can rewrite
   the whole local chain by re-hashing. Tamper-evidence depends on anchoring.
3. **Anchor strength and its ceiling.** Anchoring raises the bar to rewriting
   both the committed file/history *and* the GitHub Actions run logs. But:
   the committed `audit-anchors.jsonl` gives no independent assurance when the
   runner that pushes it shares a trust domain with the chain writer (the same
   `GITHUB_TOKEN` can force-push it); Actions log retention is limited (90 days
   by default, lower if configured) and logs are deletable by repo admins; and
   the operator chooses the head that gets anchored. The honest claim is:
   **tamper-evident against an attacker who does not control the GitHub Actions
   logs, within the log-retention window.** kew is not an HSM.
4. **Backfill boundary.** Events recorded before #249 are chained for
   contiguity but are not retroactively attested — tamper-evidence begins at
   the first anchor.

## Anchor-witness model

Anchors are recorded by the daily `audit-anchor` workflow
(`.github/workflows/audit-anchor.yml`, schedule `17 4 * * *`) and pushed to
`audit-anchors.jsonl` in the repository. `kew audit verify --anchors
audit-anchors.jsonl` compares the local chain against every anchor in that
file.

The workflow runs on the self-hosted runner that holds the ledger. The anchor
is therefore computed on the ledger host, not by an independent witness. The
pushed file and the Actions run log are copies held off the host. They detect
a later edit to anchored events. They do not detect an edit made before the
anchor was taken.

Each current v2 anchor line carries
`{version, ledger_id, id, entry_hash, count, timestamp, prev}`. The
`ledger_id` binds the anchor history to one audit database. Verification
refuses a v2 anchor from another ledger with `WRONG-LEDGER`. The database also
binds this identity to its canonical project root. A copied database opened
under another project root therefore fails as a ledger fork: the managed
open refuses with `WRONG-LEDGER: registry/database mismatch`,
`WRONG-LEDGER: registry project mismatch`,
`WRONG-LEDGER: configured path mismatch` (a second checkout of the same
repository pointing at a copy), or `REGISTRY-MISSING-FOR-IDENTITY` (exit 2)
before any audit row is read.

Legacy anchor lines carry `{id, entry_hash, count, timestamp, prev}`. They do
not contain a ledger identity.

- `id` — the audit-log row id at the time the anchor was taken.
- `entry_hash` — the SHA-256 hash of that row. Mismatch or absence of any
  non-lagging anchor (id ≤ local head) is classified as tamper.
- `count` — how many rows existed in the audit log when the anchor was taken.
  Two count checks fail the verification **closed** (non-zero exit), no longer
  advisory (#912):
    1. `count` must be monotonically non-decreasing across non-lagging anchors.
       A shrink (any anchor's `count` below the previously observed maximum)
       means audit rows that existed when a prior anchor was taken have since
       been deleted — tamper.
    2. **Count-shrink relative to the latest anchor.** The live chain must not
       trail the highest anchored `count` by more than the configured
       staleness window (`[audit] staleness_window`, default 0). In honest
       operation the live DB is always at or ahead of every anchor (anchors
       trail the head), so a live count below the anchored high-water mark
       means rows were truncated after anchoring — or the anchor is
       stale/lagging beyond tolerance.
- `prev` — the **anchor chain** (#912): a SHA-256 hash binding the *previous*
  anchor record (`id, entry_hash, count, timestamp, prev`). The first anchor's
  `prev` is the genesis value (64 zeros); legacy anchors written before the
  anchor chain existed carry no `prev` and are skipped (their link is
  unverifiable, but they still form the preimage for the next anchor's `prev`).
  That tolerance covers only the **leading** run of legacy anchors: once any
  anchor carries a `prev`, a later anchor without one is a break, not a skip, so
  history cannot be forged by simply deleting the field (#1122).
  Because each new anchor binds the entire previous anchor, wholesale
  re-chaining of the audit log requires rewriting the anchor history too — and
  that history lives out-of-band in git (the daily workflow commits it), so a
  silent re-chain is detectable by construction. A present `prev` that does not
  bind the previous anchor is classified as re-chained history — tamper.
- **Lagging anchors** (id > local head, i.e. the anchor is ahead of the local
  head) are tolerated only within the staleness window; beyond it they count
  as count-shrink and fail closed. Set `[audit] staleness_window` to the number
  of events a legitimately-behind clone may trail the latest anchor.

### Anchor freshness (is anyone still witnessing?)

Every check above measures the chain against the anchors that *exist*. None of
them notices when anchors stop being taken: a chain nobody has anchored for a
month verifies perfectly and is witnessed by nobody. The witness is therefore
reported and alarmed separately.

`kew audit verify --anchors` prints the age of the newest anchor:

```text
Chain intact: 3547 event(s).
Newest anchor: 2026-08-25T14:01:22.566429+00:00 (1.9 day(s) old).
Anchors: 16 matched, 0 mismatched, 0 absent, 0 ahead (not yet local).
Anchor chain: intact (5 link(s) verified).
```

The age is advisory — it never changes the exit code (an old anchor is an
operational gap, not tamper) — and an anchor whose timestamp cannot be parsed
reads `Newest anchor: unknown`, never as fresh. `--json` carries the same two
facts as `anchors.newest_timestamp` and `anchors.age_days` (both `null` when no
anchor has a readable stamp).

`kew doctor` is the alarm. It warns, and exits non-zero, when the newest anchor
is older than `[audit] anchor_max_age_days` (default 7):

```text
  anchor-age: newest anchor is 26.0 day(s) old (threshold 7) — the audit chain
  has been unwitnessed since 2026-07-30T03:44:40.382947+00:00
  hint: check the audit-anchor workflow on the self-hosted runner, then re-run
  it (gh workflow run audit-anchor.yml).
```

Set `anchor_max_age_days = 0` to turn the alarm off (an anchor cadence slower
than daily should raise the threshold instead). A missing, unparseable, or
undatable anchors file is reported as a *skipped* check, never as a pass.

### Unwitnessed windows (recorded, never backfilled)

The daily workflow failed silently, so `audit-anchors.jsonl` has real gaps. They
are recorded here rather than papered over: an anchor's value is that it was
witnessed *at the time*, so a gap can never be backfilled — an anchor written
today for a chain state from July would attest to nothing.

Anchors are taken daily (`17 4 * * *`), so every gap below is a run of missed
days, not a slow cadence:

| Window (UTC) | Length | Cause |
| --- | --- | --- |
| 2026-07-06 → 2026-07-24 | ~18 days | Anchor job not running (#1121). |
| 2026-07-25 → 2026-07-30 | ~4 days | Same. |
| 2026-07-30 → 2026-08-06 | ~7 days | Same; the 07-30 anchor is the last before the long outage (#1408). |
| 2026-08-06 → 2026-08-25 | ~19 days | Same, across the runner moves of 2026-08-10. |
| 2026-08-25 → 2026-08-28 | ~3 days | Scheduled runs reached the anchor step, then failed the push non-fast-forward: the job waited hours in the shared runner queue and pushed from a stale checkout. Fixed by rebasing before the push (#1743); closed by the scheduled run of 2026-08-28T06:52Z (anchor #3570, `9885d4e0`). |

The 2026-08-25 anchor was written by a **manual** `workflow_dispatch` after the
job was pinned to the self-hosted runner with ci.yml's shared-clone preflight.
The 2026-08-26 and 2026-08-27 scheduled runs both anchored and then lost the
push race above. The first scheduled success after the fix is the run of
2026-08-28T06:52Z (anchor #3570); its queue delay was two seconds, so the
rebase-under-delay path is pinned by the workflow test rather than by that
run. `kew doctor`'s anchor-age check is the tripwire from here on.

Events appended inside those windows are covered by the hash chain and by the
anchors on either side (each later anchor commits to a chain state that
includes them), but they were not independently witnessed as they were written.
Treat that as the residual risk it is: a full re-chain of the ledger during an
unwitnessed window would have to also rewrite the anchor history in git, but no
out-of-band record pins those specific days.

The evidence-pack attestation (`kew report export`) surfaces the same verdicts:
`anchors.count_shrink` and `anchors.chain_ok` in the JSON twin, and a
`COUNT-SHRINK` / `RE-CHAINED` marker in the HTML attestation section.

**A3 fix.** Count-shrink-as-tamper (both directions), the anchor chain, and this
documented witness model constitute the A3 mitigation. Full A3 closure (signed
anchors or external third-party notarization) is out of scope and deferred.
