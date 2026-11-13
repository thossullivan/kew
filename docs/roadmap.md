# Roadmap

kew's job is to be **the governance layer for agentic software work** — budgets, audit, policy, and the kill switch, applied across runners. Each route states its own capabilities; kew does not claim identical controls everywhere.

The vendors are building governance too — metering, spend caps, approval flows — each inside their own walls. kew's claim is the quadrant they structurally don't serve: **self-hosted, cross-runner, and aware of runs and outcomes rather than API calls.** If your governance lives in one vendor's console, it governs one vendor.

Every roadmap item passes one test: **does it extend governance, ownership, or trust to a new actor, a new origin, or a new standard of proof?** Items that only add orchestration breadth — more parallelism, more dashboards — don't make the list.

This roadmap is public on purpose. Items get claimed when they ship, not before — if something below slips or dies, you'll see that too.

## Layer 1 — Agents (shipped, deepening)

Already live: three-tier budget caps enforced at dispatch with an automatic circuit breaker (#245), the fleet kill switch (`kew stop` / `kew resume`), approval gates above an estimated cost threshold, outcome-linked spend reporting (`kew report`, cost per merged PR), structured audit logging, standalone clone isolation, a security hook on agent commands, and **first-class loops**. `kew dispatch <issue> --loop` drives a governed agent session to a verified, observable close (ci-green stop condition, pause/resume/reassign, budget watchdog).

### M1 — Launch window *(complete)*

Small on purpose: the schema, defined once.

| Item | Issue |
|---|---|
| **Audit envelope** — *who* acted, *which repo* spent, and the configured inference endpoint, recorded on every governance event from birth; later milestones fill fields instead of migrating shape | #246 ✓ |
| Report integration: killed runs + approval holds | #238 ✓ |
| **Outcome persistence** — store what happened to the work (merged, review cycles, diff stats), because you can't retroactively measure outcomes you never kept | #261 ✓ |

### M2 — Post-launch hardening *(complete)*

| Item | Issue |
|---|---|
| **Automatic circuit breaker** — estimated mid-run spend breaching its ceiling (or far outrunning historical medians) stops that run and records why; a fleet budget breach under `kew run` latches dispatch until `kew resume`. Both are backed by runner-level caps and wall-clock triggers for unpriced local models | #245 ✓ |
| **Cache-aware pricing** — estimates the breaker can trust | #255 ✓ |
| **`kew health`** — the control plane attests to itself: meter running, budgets enforced, audit intact | #247 ✓ |
| **Remote approve/deny** — report delivery to a webhook such as Slack, with the approve and deny commands in the message, so the gate's holder isn't terminal-bound | #236 ✓ |

### M3 — Tamper-evidence *(complete — 5/5)*

Logs satisfy operators; tamper-evidence is the next standard of proof. Honest scoping: an unanchored hash chain on your own disk provides integrity checking. You choose the anchor sink. The default is a file at the project root, and the bundled workflow can commit it to the repository.

| Item | Issue |
|---|---|
| **Hash-chained audit log** + `kew audit verify`; chain head anchored to a chosen sink, with a project-root file by default and a workflow that can commit it | #249 ✓ |
| **Policy binding** — every dispatch records a hash of the (redacted) governance policy in force, including the deployed security hook | #250 ✓ |
| **Endpoint enrichment** — record what runners were configured to hit and what they report: the first brick of egress accounting, not network-level attestation | #251 ✓ |
| **Cross-runner ingestion** — one self-hosted record across dispatched and interactive usage, via Claude Code's documented OTel export. Alerts against your caps; passive observation doesn't pretend to enforce | #256 ✓ |

### M4 — The governor's surface *(complete — 3/3)*

| Item | Issue |
|---|---|
| **Approval inbox.** kew's first network listener, treated as such: read views plus authenticated writes. Every write is listed in the committed OpenAPI document, `kew/openapi.json`, and its CI gate fails on any hidden `/api` route. Token → session-cookie auth, CSRF + Origin checks, loopback-by-default with an explicit `--expose` opt-in; web approvals are identically policy-bound to CLI approvals | #252 ✓ |
| TUI approve dialog | #237 ✓ |
| **Model scorecard** — merge rate, review iterations, cost per merged PR, by model: outcome-linked comparison from *your* fleet, not a benchmark | #262 ✓ |

### Operational maturity

A tool that sells trust doesn't get to treat its own posture as an afterthought. These ship inside the milestones above:

| Item | Milestone | Issue |
|---|---|---|
| **SECURITY.md + published threat model** — including what kew does *not* defend against | M1 | #263 ✓ |
| **CONTRIBUTING.md + dispatchable issue templates** — community issues feed the same queue kew drains | M1 | #264 ✓ |
| **Runner-compatibility canary** — a vendor CLI update must never mean silently wrong metering | M2 | #265 ✓ |
| **Supply-chain posture** — SBOM, release attestations, license checks | M2 | #266 ✓ |
| **Audit-data durability** — backup/restore/WAL; a chain over data that dies with a disk is not a record | M3 | #267 ✓ |

### Shipped since M4

The milestones above end in mid-2026. These shipped after them.
[How It Works](how-it-works.md) and
[How kew decides a change is proven](proof.md) describe them.

| Item | Status |
|---|---|
| **Epic driver**: a dependency graph of issues, driven tick by tick (`kew epic`), and the supervised `kew drive` | shipped |
| **Governed adversarial review**: a fresh-context reviewer whose verdict is attested and bound to the reviewed commit | shipped, opt-in |
| **Host-owned publication**: kew rebuilds and scans the commit; a contained agent holds no credential that can push | shipped |
| **Merge verify**: the base branch's tests run against the pull request's code, with declared behavior changes | shipped; wired to kew's own repository layout |
| **Web console**: Work, Monitor and Epics in `kew serve` | shipped |
| **More runners**: Codex, OpenCode, pi and Hermes; [Runners](runners/index.md) states what each supports | shipped |
| **Linear as a graph source**, **OpenShell runtime** | experimental |

## Layer 2 — Local execution *(direction, not claimed)*

Running agents on local models is a strategic direction. It shows that the control model follows the operator down the stack. It does not make kew a general infrastructure orchestrator. The Lemonade/local-model lane stays experimental until its containment, metering, and recovery are certified. Local or unpriced does not mean free. Verifiable egress control is also a direction, not a current claim. These ship before they're marketed.

## Layer 3 — Model intelligence *(deepening)*

kew records governance facts about the models it configures and runs: usage, cost, outcomes, and the model scorecard. Model-lifecycle truth belongs to the external `model-eol` product. Today kew ships its own curated lifecycle registry ([`kew model-eol`](cli/model-eol.md)). The next step is an offline lookup of `model-eol` data; kew then removes its duplicate registry. Model training and a kew-owned lifecycle feed are out of scope.

---

*Sequencing is honest-best-effort by a [solo maintainer](https://github.com/thossullivan). Milestones here are scope commitments, not date commitments — dates appear on releases, after the fact; the order can shuffle, but the test at the top of this page does not. The spine deepens a little with every release; it does not chase orchestration parity. Issues and PRs welcome.*
