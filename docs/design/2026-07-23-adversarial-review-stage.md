# Spec — Governed Adversarial Review Stage

**Date:** 2026-07-23 (rev 2.1 — F1-F15 reworked; N1-N5 from re-review applied)
**Status:** DRAFT rev 2.1 — pending final re-review
**Motivation:** Across the delegation-hardening and delegation-followups waves, every substantive defect that reached the merge surface was caught not by the implementing agent's tests (honest, green) but by a manually-orchestrated fresh-context adversarial reviewer: the refusal-churn loop (#966), the tamper-masking verify cache (#968), the default-path-gated lock steal + ownership-unchecked heartbeat (#974), the reconcile-vs-live-claim false positive (#975). The failure is structural — same-context self-review cannot see interactions with actors outside its context — so the countermeasure must be structural: the control plane owns a fresh-context adversarial pass between "agent opened a PR" and the approval surface. Today that loop is manual session labor; this feature makes it a governed pipeline stage.

## Thesis alignment

kew's claim is governance as structure, not vigilance. An approval surface where every PR carries a chain-attested review verdict — bound to the exact commit reviewed and the policy in force — instantiates that claim directly. The verdict is an **input to human approval, not a replacement for it** (F14): human review has repeatedly caught what the full AI chain missed, and nothing here dilutes that gate.

## Design

### Node lifecycle (epic driver)

Current (corrected per F14): `in_progress` → PR opened → `in_review` → human **approves** → driver auto-merges on approval + CI-green. The human gate is approval, not the merge click.

New (when `[review].adversarial = true`):

```
in_progress → pr_opened
  → adversarial_review        (fresh-context reviewer dispatched against a RECORDED head SHA)
      ├─ READY (evidenced)    → in_review  (approval surface; verdict is context for the human)
      └─ NEEDS_CHANGES
          → remediating       (adapted `kew review` path, findings as input)
          → adversarial_review (same contract, round N+1, new head SHA recorded)
              └─ rounds exhausted → HELD: needs-human (loud driver line + label)
```

- New statuses extend **`DriverStatus`** (`graph/driver/overlay.py`, precedent: HELD/CANCELLED) — NOT the provider-derived `NodeStatus` enum (F8). Unknown-status frontier safety is already fail-closed (`tick.py:96-102` BLOCKED sentinel).
- **Review state is persisted**: round counter, per-round `{head_sha, verdict, sidecar_digest}` live in the run store (small schema addition, follows [[schema-migration]] rules). Driver restart mid-review resumes the round count; it never resets (F8).
- Max rounds default **2**. Non-convergence → HELD with the existing blast-radius stall line — never a silent loop.
- **`kew epic retry` resets the round counter** (fresh implementation ⇒ fresh review allowance) and this is attested as a new review cycle (F15).

### Blocking semantics — one rule, enforced at the merge pass (F2, N1)

**In blocking mode, a node reaches `in_review` only via an evidenced READY verdict bound to the current head SHA.** Every other outcome — reviewer crash (after one retry), unparseable or missing verdict, review-budget refusal, PR force-pushed after verdict — lands in the same place: **HELD: needs-human, with an attested event stating why**. There is no advisory-degrade path inside blocking mode; a PR-controlled reviewer crash therefore cannot bypass the gate.

**The gate's enforcement point is the merge pass, not the status label (N1 — HIGH).** HELD is a live merge-pass candidate today (`tick.py:365-371`): approval + green CI auto-merges it. Therefore the merge pass gains one invariant, checked at merge time for every **policy-covered** candidate of a review-enabled epic (PRE_POLICY grandfathered nodes — F9 — have no verdict and merge under the pre-existing rules):

> merge only if latest verdict is READY **∧** `verdict.head_sha == current head SHA` **∧** the approval timestamp postdates the verdict.

This single invariant is what makes blocking mode real: it closes the HELD-is-mergeable hole (N1), enforces merged-sha == reviewed-sha at the merge itself rather than only at the status transition (F1 residual — a post-transition force-push can no longer merge unreviewed code), and is the natural home of the approval-postdates-verdict rule (F6). A candidate failing the invariant is skipped with a loud driver line + attested reason; it never merges silently.

Advisory mode (`blocking = false`) is where degradation is legal: verdict (or DEGRADED marker) is attested, node proceeds, and the merge-pass invariant is not enforced — the adoption on-ramp.

### SHA binding (F1)

At dispatch, the driver records the PR head SHA; the verdict event binds `{pr_number, item_id, head_sha, round}`. On the `adversarial_review → in_review` transition the driver re-reads the head: **if it moved, the verdict is void** and the node re-enters review (round increment). Merge-time: the existing merge attestation (`merge.py` sha binding) plus the verdict event share the same sha, so "did the merged code survive review" is answerable — and falsifiable — from the chain alone.

### Human-action races (F6, F7)

| Race | Behavior |
|---|---|
| Human merges during `adversarial_review`/`remediating` | Out-of-band-merge detection extended to these states (today it only scans IN_REVIEW/HELD candidates). Reviewer/remediator killed + reconciled; chain gets `adversarial_review_verdict{verdict: MERGED_UNREVIEWED, head_sha, actor: out_of_band}` — the chain never goes silent on a bypassed gate, it records it. |
| Pre-existing (latent) PR approval | On the verdict-driven flip to `in_review`, auto-merge requires an approval **timestamped after the READY verdict**; stale approvals surface as "approval predates review — re-approve" instead of auto-merging (F6). |
| PR closed/superseded mid-review | Explicit failure row: reviewer killed + reconciled, node → HELD: needs-human with reason. The node must NOT derive back to TODO and re-enter the dispatch frontier while a reviewer is live (F7; today pr_number→None does exactly that, #753 class). |

### Reviewer contract

- **Fresh context, sandboxed** — dispatched like any agent (containment, credential posture), never the implementing session; worktree at the recorded head SHA.
- **Context hygiene (F12/N5)**: stated as an invariant, not a mechanism: **no runner-auto-loaded file from the head ref reaches the reviewer's context** — `CLAUDE.md`, `CLAUDE.local.md`, `.claude/`, `.mcp.json`, and any future auto-load surface; that content is written by the code under review. The reviewer's instruction surface is exactly the pinned prompt artifact + base-ref project context. Mechanism note: the sandbox binds exactly one mount (`checkout_dir:/work`), so overlay-mounting is not available — the head worktree's auto-load files must be **masked/replaced with the base-ref copies before launch** (materialization step), and the same masking applies to uncontained dispatch. A test pins the invariant: a marker instruction planted in each head-ref auto-load file must not appear in the reviewer's effective context.
- **Evidence symmetric in both directions (F11)**: NEEDS_CHANGES findings each carry severity, `file:line`, executed evidence (command + output digest) — evidence-free findings are dropped. **A NEEDS_CHANGES whose findings are ALL dropped for missing evidence is treated as unparseable** (retry → HELD), never silently converted to READY. **READY carries mandatory evidence too**: the executed acceptance/test commands and their output digests. An empty-findings READY without executed evidence is unparseable → retry → HELD. (Residual: a reviewer can still run tests and lazily approve — bounded by the human gate; v2 knob: spot-audit READY verdicts with a second reviewer at sampled rate.)
- **Structured verdict, carried in the reviewer's own result event** (#2021): the reviewer ends its run with one final message holding its verdict JSON between `<<<KEW-REVIEW-VERDICT-BEGIN>>>` and `<<<KEW-REVIEW-VERDICT-END>>>`. The host reads it from the runner's typed terminal record as it streams the container's stdout (for claude-code, the one top-level `result` event), extracts the block, and **stores it under `logs/dispatch/` on the host** — the attested digest is over kew's stored copy, never over a deletable GitHub comment (F10). The PR comment is prose for humans; kew parses only that stored verdict.
  - *No verdict file.* This design first had the reviewer write a sidecar file into the checkout (report-broker pattern #759), with a subdirectory search as a fallback (#1026). The reviewer runs the PR's own tests as acceptance evidence, in the same container, as the same user, over the same mount, so the code under review could write a READY file with the right head SHA and approve itself. kew now reads no file in the checkout as a verdict.
  - *Strict channel.* The stream must carry exactly one result event, as its last event, reporting success; its text must carry exactly one delimited block; the block's `head_sha` must equal the SHA the host recorded; the reviewer must exit successfully. Anything else is the crash path below. An extra result record injected into the stream (a same-user child writing to the agent's stdout) makes the round ambiguous, never READY.
  - *Runners.* Only claude-code has a single typed terminal record kew binds today. A `[review] runner` pinned to another runner is refused before any container or credential access.
- **Prompt is a shipped artifact** (`_skills`-style, sha256-pinned, deployed via `kew skills`): "tuned" means editing a pinned artifact through a PR. The dispatch template interpolates the pinned artifact from the BASE ref, and the prompt artifact's sha is bound into the verdict event — the chain records which prompt version reviewed the code (F12 complement).

### Remediation path (F3, F4)

`kew review` is **adapted, not reused as-is** — today it keys budget/RunRecords on the PR number with `item_id=None` (`src/kew/review.py`), which is invisible to epic reservations and would get the node killed as dispatch-vanished by the reconciler at 600s. Changes:

- Reviewer + remediation runs are RunRecords with **the node's `item_id` and a new `run_kind`** (`adversarial_review` / `review_remediation`) — run_kind bucketing precedent: #445 breaker fix.
- **Status derivation filters by run_kind** (F4): `overlay_status`/`list_by_item` derive node status from implementation runs only; a pending reviewer row must not flip a node IN_PROGRESS, and a killed reviewer must not derive the node FAILED (the crash table above owns those outcomes).
- **Reconcile + reservations (N2)**: reviewer runs hold reservations released on every terminal path, matched by `run_id` (#972 semantics — this spec depends on #972). Reservations are **parked under a `review` pseudo-epic** (delegation precedent, `"delegation"` pseudo-epic) — NOT on the node's item — because overlay precedence derives IN_PROGRESS from any reservation on the item kind-blind (`overlay.py:89-93`); item-parked reviewer reservations would corrupt node status exactly the way item-keyed run rows would (F4). The `concurrent-state-change` checklist's false-positive test is mandatory: a live reviewer mid-startup must survive every reconcile pass.
- Fleet accounting (F15): reviewer runs count toward `max_workers` (they are real compute) — documented, and the driver's frontier logs say when review runs are occupying slots.

### Budget — separate attested pool, no starvation (F5)

Review spend does **NOT** draw from the per-issue implementation cap. Two pools, both enforced, both attested:

- Implementation cap: unchanged semantics (`check_budget` over implementation-run_kind rows only).
- Review pool: `max_review_usd` per node per review cycle, enforced at reviewer/remediation dispatch preflight with the same atomic reservation idiom (#939) and the same refusal semantics (audit event; blocking → HELD, advisory → proceed flagged).

Rationale: a single pool starves bidirectionally — reviews eat implementation retries, and a near-cap implementation makes its own reviewer refuse, leaving a blocking node permanently unreviewable. Separate pools make both exhaustions loud and independently tunable. Spend figures in attestation come from **kew's RunRecords, never the verdict's self-reported number** (F10) — a `spend_usd` field in the verdict is dropped.

### Attestation (F1, F10)

`adversarial_review_verdict` event per round, additive attested fact bound via hashed payload (**no new `entry_hash` slots** — forward rule). Payload: `{pr_number, item_id, head_sha, round, verdict, sidecar_digest (over the host-stored verdict record), reviewer_model, prompt_artifact_sha, policy: {blocking, min_severity_to_block, max_rounds, max_review_usd}, spend_source: run_records}`. Policy binding follows the #250 precedent — a verdict is only meaningful relative to the policy in force. Join keys (`pr_number`, `item_id`, `head_sha`) make the chain query "show me this merged PR's review provenance" a lookup, not an inference.

### Config `[review]`

`adversarial` (default false) · `blocking` (default true when adversarial) · `model` · `effort` · `max_rounds` (2) · `max_review_usd` · `min_severity_to_block`.

**Enablement rule (F9):** the flag is read at the `pr_opened` transition. Nodes already at/past `in_review` when the flag flips are grandfathered — attested as `verdict: PRE_POLICY` if they merge. Flipping the flag never throws an approved PR back into review.

### Failure modes (fail loud, degrade explicitly)

| Failure | Blocking | Advisory |
|---|---|---|
| Reviewer crash / exit 143 | 1 retry → HELD: needs-human + attested reason | 1 retry → DEGRADED attested, proceed |
| Review-pool budget exceeded | Refusal event → HELD | Refusal event → proceed flagged |
| Verdict missing, ambiguous, unparseable, head-mismatched, or evidence-free READY; reviewer exit non-zero | = crash path (never guessed from prose) | = crash path |
| Head SHA moved after verdict | Verdict void → re-review (round++) | Re-review, non-blocking |
| Remediation non-convergent | Round cap → HELD | Round cap → proceed flagged |
| Human merges mid-review | MERGED_UNREVIEWED attested | same |
| Reviewer approves a real bug | Residual by design — human approval gate + (v2) sampled second-reviewer audit | same |
| Prompt injection via reviewed code content (diff/comments steering the reviewer) | Owned residual: context masking (F12) removes the *auto-loaded* surface; the diff itself remains adversary-authored input. Mitigations: evidence-required verdicts, human gate, v2 sampled audit | same |

### Non-goals (v1)

- No multi-reviewer voting panels (v2 tuning knob; single fresh-context reviewer matched this wave's catch rate).
- **Delegation-path PRs: v1.1, not never** (F13). The inversion is owned: unattended delegation PRs are the surface that most needs this, and the shared reservation reconciler (#972) makes extension mechanical. v1 proves the stage on the epic path where a human is already watching; v1.1 (fast-follow, same epic if capacity allows) extends to `kew delegate watch` before any unattended operation is advertised as safe.
- No auto-merge on READY. Approval stays human; the verdict is evidence for it, and the docs must present it that way — never as a reason to rubber-stamp (F14).

### Concurrency note

This stage is squarely inside the `concurrent-state-change` skill's trigger set (new driver states, a reconcile surface, a repair path, shared run rows). Every implementation task carries that checklist; mandatory tests include the reconcile false-positive (live reviewer survives), the mid-transition kill (SIGTERM during `remediating`), and the ownership check on review-state writes.

### Dogfood plan

First governed drive: the follow-up pile (~7 small issues + wave LOW residuals) in **advisory** mode → tune prompt artifact against real verdicts → flip **blocking** for the Tier 2 decomposition epic (dispatch_core/run_store/cli shard), where a governed adversarial pass has the highest expected payoff.

## Grounding anchors

- Driver statuses + overlay: `kew/src/kew/graph/driver/overlay.py` (`DriverStatus`, HELD/CANCELLED precedent); frontier fail-closed sentinel `graph/driver/tick.py:96-102`
- Reconciler to extend: `graph/driver/tick.py:172-256` (`_record_dispatch_child_died`); run_id-keyed matching lands in #972
- Remediation command to adapt: `kew review` (CLI entry `cli/__init__.py:1360`; remediation module `src/kew/review.py` — PR-keyed, `item_id=None` today)
- Overlay HELD-reason routing to extend: `graph/driver/overlay.py:151-176` (recognizes only `ci_pending`/`budget_preflight*` today — new review hold reasons are a touchpoint, N3)
- Merge attestation sha binding to mirror: `graph/driver/merge.py` (~:280)
- Budget refusal semantics to mirror: `dispatch_core.py:2629-2723` + `_record_budget_refusal_row` (#898/#966); atomic reservation idiom #939
- run_kind bucketing precedent: #445; policy-binding precedent: #250; report broker: #759
