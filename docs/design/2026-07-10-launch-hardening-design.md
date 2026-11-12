# Launch Hardening — Design

**Date:** 2026-07-10
**Status:** Approved (maintainer brainstorm, 2026-07-10)
**Context:** Extends the go-live gate (#439) with two fix epics, funded by the
lesser-model operability result: Epic 1 (`operability-substrate`) ran 9/9
Sonnet nodes first-try at ~$2.08/node average. Hardening the launch surface is
now an afternoon of compute, not a week of engineering.

## Problem

The go-live gate is publish-mechanics only, while known trust and integrity
defects sit in the backlog: the 2026-07-01 audit's Critical guard hole (F1)
plus guard bypasses (F2/F3/F6) and the audit-chain count-shrink gap (A3);
data-integrity bugs that falsify kew's own economics claims (#445 breaker
false-kills, #443 loop spend linkage, #331 canary drift, #456 broken
import-linter contract); README/SECURITY.md drift; and epic-machinery
frictions from the dogfood run (#480, #481, stall-signal UX). kew sells
governance and honest cost accounting — day-1 scrutiny lands exactly on
these.

## Decision (made in brainstorm)

- **Gate is adjustable.** All four clusters (guard/security, data-integrity,
  docs truthfulness, epic-machinery) join the go-live gate.
- **Shape:** two gating epics (`launch-trust`, `launch-integrity`), driven
  sequentially through the proven Sonnet pipeline. Not one mega-epic (review
  attention must be spiky where risk is), not four micro-epics (driver
  overhead).
- **Epic 2 (`god-file-decomposition`, #472–#475) moves post-launch.** Both
  fix epics touch `dispatch_core.py` / `cli.py`; the refactor carries the
  fixes rather than racing them. Its characterization safety net is already
  merged/pending, so it loses nothing by waiting.
- **Worker model:** Sonnet 4.6 (config already committed). The guard nodes
  (T1/T2) additionally get an **adversarial security review before merge** —
  a wrong-but-plausible guard fix is the exact cheap-model failure mode this
  compensates for. No silent model escalation.
- **Enhancements (#414, #411, #434, #403) stay off the gate** — post-launch
  epic, separate brainstorm.

## Epic `launch-trust` (first)

| Node | Depends | Scope | Source |
|---|---|---|---|
| T1 | — | Guard: block Bash tokens referencing `.claude/` (redirects, `tee`, `cp`, `mv`, `rm`, `sed -i`, `ln`, `chmod`); hook fails CLOSED when `jq`/`perl` missing | Audit F1 (Critical) + F6 |
| T2 | T1 (same file) | Guard: collapse internal whitespace before pattern matching; detect `<(` / `>(` process substitution | Audit F2 + F3 |
| T3 | — | Audit chain: `classify_anchors` treats anchor count-shrink as tamper (not advisory); document the anchor-witness model (daily git-pushed `audit-anchors.jsonl`) in `docs/audit.md` | Audit A3 (partial) |
| T4 | — | kew-runner image: clear Trivy HIGH/CRITICAL CVEs | #465 |
| T5 | — | Assert `persist-credentials` stays enabled in `audit-anchor.yml` | #482 |
| T6 | T2 | README/SECURITY.md truthfulness: drop removed Aider runner; document epic DAG, dependency dispatch, cache-aware routing; align guard claims with post-T1/T2 reality | Audit doc-drift findings |

F1/F2/F3/F6 and A3 have no GitHub issues yet — file them (from the audit
findings table of the 2026-07-01 codebase audit) before `kew epic plan`
runs.

## Epic `launch-integrity` (second)

| Node | Depends | Scope | Source |
|---|---|---|---|
| I1 | — | Breaker cost-anomaly baselines per run kind (review runs vs first dispatches); revert the uncommitted `kew.toml` `cost_anomaly_factor = 6.0` workaround in the same PR | #445 |
| I2 | — | Loop-dispatched runs record their branch so `kew sync` links PRs and outcome-linked spend works for loops | #443 |
| I3 | — | Fix the canary governance drift (#331). The diagnosis happens at PLAN time, in-session — an investigation node cannot pass the dispatchability gate (no nameable target files); the node ships as a concrete fix or is dropped from the epic with the finding documented | #331 |
| I4 | — | Fix transitive `kew.serve` contract violations; add `lint-imports` to `ci.yml` AND `scripts/verify.sh` (preserving verify≡CI equality) | #456 |
| I5 | — | Enforce the dispatchability gate's >10-file re-split rule in `kew epic plan` validation, in code | #480 |
| I6 | — | Epic amend/re-plan path: add nodes to an existing epic through the governed flow | #481 |
| I7 | — | Driver terminal state `awaiting_merges` (with PR list) distinct from STALLED; file the issue first | dogfood finding, unfiled |

Nodes are independent unless the plan phase finds file collisions; the plan
serializes any it finds via `kew:depends`.

## Sequencing (load-bearing)

1. The 7 pending Epic-1 PRs (#486–#488, #490–#493) merge first — dead-code
   deletions touch the same modules these fixes edit.
2. `launch-trust` drives to its merge checkpoint; the maintainer merges (guard PRs only
   after their adversarial security review).
3. `launch-integrity` drives the same way.
4. The original four #439 steps (publishers, UAT matrix, flip) proceed in
   parallel — they share no files with the epics.
5. Post-launch: Epic 2 `god-file-decomposition`, then revert `kew.toml`
   model to opus (note on the config commit 0b823b3).

## Gate change

`#439` gains two checkboxes above "Flip the switch":
`- [ ] launch-trust epic merged (guard PRs security-reviewed)` and
`- [ ] launch-integrity epic merged`. The existing four steps are untouched.

## Execution mechanics

Same as workstream A: plan files committed before driving (per-epic, via
writing-plans); issues created by `kew epic plan` (dispatchability gate
enforced); `kew epic run` with `max_parallel = 3`; $25/agent cap and breaker
unchanged; merges human; every friction filed as a kew issue. The guard-node
security review = an in-session adversarial reviewer prompted to BYPASS the
patched guard (using the audit's attack shapes plus novel ones) + the
`/security-review` pass, both green before the maintainer merges.

## Success criteria

- Both epics fully merged before the publish flip; #439 updated.
- Guard nodes: the audit's concrete bypass examples (F1 `echo > .claude/...`,
  F2 double-space, F3 `bash <(curl ...)`) become failing-then-passing tests.
- `kew.toml` workaround reverted (working tree finally clean of it).
- `kew audit verify` treats count-shrink as tamper, with a test.
- Cost-per-merged-PR stays in the Epic-1 band (~$2–4/node).

## Error handling

Breaker/retry holds on double failure → in-session triage, no silent model
escalation. Gate-failing issues → fix the skill/plan, never the issue by
hand. Guard-node review finds a hole → back to the node, never merged-then-
patched.

## Out of scope (deliberate)

- Epic 2 `god-file-decomposition` (post-launch; unchanged issues #472–#475).
- Enhancements #414 / #411 / #434 / #403 and the M5/holy-shit feature list.
- Full A3 remediation (signed anchors, external notarization) — the count-
  shrink fix + documented witness model is the launch bar.
- F4/A4 credential-denylist porosity — accepted per threat model; T6
  documents it honestly.
