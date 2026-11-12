# Kew product charter

Dated: 2026-08-17

Status: **ACCEPTED PRODUCT DIRECTION**

Owner: repository maintainer

This charter governs Kew's product intent and scope boundary. Where an
older vision, roadmap, design, audit disposition, or simplicity proposal
conflicts with it, this charter governs the product decision. Those documents
remain useful as implementation history and evidence.

This is not an implementation plan, a claim that every described lane is
currently certified, a public-distribution decision, or permission to mutate
the backlog. Material changes to this charter require an explicit maintainer
decision and a dated amendment.

## Product thesis

Kew is the self-hosted operations and governance layer for a maintainer running
coding agents. It turns admitted GitHub or Linear work into verified,
reviewable GitHub changes under one routing, budget, intervention, recovery,
publication, and audit model, while release judgment remains human.

Kew's differentiation is not the number of runners, commands, or dashboards.
It is one durable, operator-owned control and evidence model across
heterogeneous agents and work sources.

The posture remains:

> Offload the work. Keep the judgment.

## Primary user and job

The primary user is a technically sophisticated maintainer or technical lead
who is personally accountable for changes to a repository. They operate
several coding agents from a trusted machine and want a prepared backlog turned
into reviewable changes without surrendering budget, release, recovery, or
security decisions.

The secondary user is a reviewer or auditor consuming a change and its
evidence. Multi-seat organizations, an always-on service, and cross-project
portfolio operation are out of scope. Kew is project/repository scoped
unless that boundary is deliberately changed.

The durable job is:

> Admit bounded work, understand the governance contract that will apply,
> receive a verified and recoverable artifact, make the release decision, and
> retain an honest record of what happened.

## Open-source covenant

**One human, one machine stays open forever.**

Kew includes the deterministic mechanics an operator uses to govern their
own agent fleet: finite queues, supervised delegation, loops, epics, routing,
budgets, holds, containment, recovery, publication, audit, and all three user
interfaces. A human-selected finite queue or epic remains in scope even when it
runs overnight without the operator watching the terminal.

The scope boundary is authority, not whether a process remains alive.
Continuous discovery and scheduling of work, portfolio-level preemption,
cross-project coordination, multi-seat identity and roles, and operation as a
durable service are outside Kew. Security controls, evidence export,
audit, recovery, and the user interfaces are part of Kew and stay there.

## North-star journey

1. A human admits a bounded GitHub or Linear work item.
2. Kew resolves and shows the execution contract: work source, GitHub delivery
   repository, runner, provider, model, endpoint, containment posture, budget,
   verification, and publication mode.
3. Kew executes through the selected, policy-compatible route.
4. Kew preserves a versioned work artifact before publication or recovery.
5. Kew verifies the exact artifact and binds the evidence to its commit.
6. The host-owned publication boundary presents it as a GitHub pull request.
7. Independent review may contribute evidence; a human makes the release
   decision.
8. Kew records the outcome and synchronizes it to the source tracker and
   scorecard.

The north-star metric is the share of admitted work that reaches a verified,
human-reviewable artifact within policy and without operator rescue.

Guardrail metrics include pending-review work and time, human-decision rate,
recovery without lost work, policy holds and violations, review cycles,
publication and tracker-sync recovery, bounded-cost accuracy, and cost per
accepted outcome. Cost per merge alone can reward cognitive surrender and is
not a sufficient success measure.

## Two human authorization gates

Kew must distinguish two decisions that older product language calls
"approval":

1. **Admission authorization** decides whether work may execute and consume
   budget. Cost, risk, or policy can hold here.
2. **Release authorization** decides whether a verified published change may
   merge. It must be explicit, current, and come from a non-author human.

A standard dispatch stops at a reviewable pull request. A governed driver may
perform the mechanical merge only after human release authorization and the
configured policy and CI conditions are satisfied. Kew may execute that
decision; it never supplies the judgment.

Human pull-request review, independent adversarial review, and agent
remediation of review comments are three different jobs. Independent-agent
verdicts are evidence for the human, never release authorization. Product copy
and interface labels must not conflate them.

## Canonical work and state model

The durable product object is the preserved commit or work package plus its
verification and governance receipt. A pull request is the GitHub publication
of that object, not the object itself.

The intended lifecycle is:

> admitted -> executing -> work preserved -> verified -> published for review
> -> human decided -> outcome synchronized

No single status enum can faithfully represent this lifecycle. Every work card
and API projection should carry orthogonal dimensions:

| Dimension | Question it answers |
|---|---|
| Source | Which tracker and item admitted the work, and what is its state? |
| Route | Which runner, provider, model, endpoint, and boundary apply? |
| Execution | Which run or loop is active, terminal, paused, or budget-limited? |
| Artifact | Which worktree, commit, or branch is preserved, verified, or published? |
| Judgment | What hold, review evidence, or human decision is outstanding? |
| Outcome | Was the change merged or closed, and was that result synchronized? |

`awaiting_publication` means implementation work is preserved and publication
must be finalized. It is not execution failure and must never default to
redispatch. Recovery reuses preserved work before asking an agent to repeat it.

## Human-attention governance

Kew governs workers and spend today; it must also govern the review burden it
creates. Producing review work faster than a maintainer can understand it is a
product failure even when every run and test is green.

Review backpressure is therefore a first-class governance control:

- a configurable cap on Kew-created changes awaiting human review;
- optional diff, file-count, or risk thresholds that hold for human judgment;
- visible review-queue pressure and time-to-review across CLI, TUI, and web;
- no new publication when the configured human WIP boundary is saturated.

One exception queue should cover admission and budget holds, paused or
needs-human loops, stalled epics, preserved work awaiting publication, CI and
conflict remediation, orphan pull requests, tracker write-back failure, and
broker, finalization, or audit incidents. It should present the safest next
action. It must not offer a blind retry when recoverable work already exists.

## Scope

### In scope

Kew retains:

- GitHub as the code, pull-request, CI, and merge delivery forge;
- GitHub Issues and Linear as first-class work-intake and state sources;
- capability-aware multi-runner routing, with Claude Code as the primary
  reference and deepest lane; OpenCode/OpenRouter, Codex, and other adapters
  remain, with support and certification scoped to evidenced capabilities;
- budgets, admission gates, kill and recovery controls, containment, host-owned
  publication, audit, outcomes, scorecards, and evidence export;
- operator-started delegation, finite queue drains, loops, and supervised
  epics, including execution that continues unattended after admission;
- CLI, TUI, and web as three clients over one product vocabulary;
- configured, requested, and runner-reported model identity; endpoint and
  provenance evidence; usage, cost, and outcome intelligence; and model
  lifecycle presentation.

Linear is not a GitHub-outage failover. Work may originate and be synchronized
through Linear, but GitHub remains the delivery forge. Kew is not currently
multi-forge, and GitLab is not a product target.

### Out of scope

Continuous portfolio judgment and service operation are out of scope for
Kew: autopilot, resident event-driven intake, cross-project prioritization,
team identity and roles, and service operations. Nothing in or out of scope
replaces the human release decision.

A supervised, project-scoped driver that runs while the operator drives is in
scope; autopilot, an always-on service and resident intake stay out.

### External ownership

The `model-eol` product owns canonical model-lifecycle feeds, evidence, aliases,
dates, status computation, distributor semantics, refresh, and migration
semantics. Kew consumes that truth; it does not build another registry or feed.

### Experimental, deferred, and removed

- Local-model sovereignty remains a strategic direction and proof of Kew's
  cross-runner thesis. The Lemonade/local-model lane remains experimental until
  its containment, metering, and recovery capabilities are certified. Local or
  unpriced does not mean free.
- A public runner/plugin extension contract and organization-wide fleet remain
  deliberate future decisions, not current claims.
- Public distribution of `kew-cli` and `create-kew` is deferred until the
  maintainer chooses to schedule it; onboarding remains in product scope.
- Governed independent review retains its product role, but any route that
  lacks a composed production acceptance is described at its evidenced
  maturity rather than marketed as universally available.
- `kew-cruise` is removed from product scope; code and package removal is
  pending. It is an easter egg, not part of the control plane.
- Duplicate Kew-owned model-lifecycle data and semantics should be removed
  after the external adapter preserves the Kew-facing value.

## Capability-aware runner support

Security hardening changes the certification status of affected journeys; it
does not, by itself, decide that a historically proven runner or tracker lacks
product value.

Kew uses these evidence terms consistently:

| Term | Meaning |
|---|---|
| Implemented | The adapter or feature exists and its contracts pass automated tests. |
| Dogfooded | A real historical run completed the named journey. |
| Certified | The exact current Kew, runner CLI, model, and security boundary passed a bounded acceptance journey. |
| Supported | Kew maintains that capability contract, documents recovery, and keeps certification current enough for the claim. |
| Released | The code is part of an identified, versioned repository release; this does not imply registry distribution. |
| Publicly distributed | A release is available through the named public channel. This is separate from support. |

Each route publishes a capability manifest rather than inheriting a blanket
"same controls everywhere" claim. At minimum it states resume support,
structured telemetry, cost source and certainty, loop support, containment
posture, independent-review compatibility, publication mode, tracker/workflow
coverage, and the certification evidence.

Kew's promise is capability-aware cross-runner governance: normalize the facts
and policy vocabulary that can be normalized, expose material differences,
and fail closed when a route cannot satisfy the requested policy.

## The three product interfaces

All three interfaces remain part of Kew. They share domain language, state
semantics, and critical safety behavior without requiring identical feature
placement.

| Interface | Primary job |
|---|---|
| CLI | Complete scriptable execution, preflight, automation, repair, and break-glass control. |
| TUI | Live fleet supervision, progress, and rapid intervention. |
| Web | Durable judgment and evidence workspace: artifacts, Flight Recorder, decisions, history, configuration, epics, outcomes, and audit. |

The Flight Recorder is core to paying down comprehension debt. It belongs in
the run/artifact detail and must show the exact prompt, events or tool activity
available from the runner, diff progression, verification, and cost evidence.
Its current lack of a production UI consumer is a product regression to repair,
not a reason to delete the promise.

After admission, surfaces use a tracker-neutral work-item identity. GitHub
numbers must not leak into Linear state or recovery. Outages and unknown state
must render as unavailable or stale, never as an empty healthy queue. Preserved
work must have the same meaning and safe next action in CLI, TUI, and web.

## Model intelligence and lifecycle boundary

Kew owns governance facts about the models it actually configures and runs:

- configured route and reviewer inventory;
- requested model and runner-reported or served model as distinct facts;
- provider, endpoint, service tier, and evidence provenance;
- usage, pricing, cost, outcomes, and model scorecards;
- read-only model-usage export; and
- lifecycle warnings and policy presentation in CLI, TUI, web, and relevant
  reports or documentation.

`model-eol` owns lifecycle truth. Kew should consume a deterministic,
versioned, offline explicit-ID lookup or derived cache with provenance. Runtime
dispatch does not fetch lifecycle data from the network. Unknown, unavailable,
stale, or retiring status is visible and warns by default; it never silently
reroutes. An operator may separately configure a hold policy.

Kew does not claim compatibility with an external schema unless it can
round-trip that schema. Its own usage export must preserve requested and
reported identity and use an honest Kew-owned schema name.

## Evidence contract

Kew produces integrity-verifiable operational receipts for recorded events. It
does not call those receipts complete third-party compliance attestation while
material audit writes remain best-effort or a trusted host can rewrite local
state before an external anchor.

The canonical change receipt binds to the exact artifact SHA and includes:

- admitted intent and source work item;
- route, runner, provider, model, endpoint, and containment posture;
- policy and budget decisions;
- verification results and diff scope;
- usage, cost, and provenance;
- independent-review evidence, when requested;
- publication facts and human release authorization; and
- audit-chain and outcome-synchronization references.

The portable evidence pack is a representation of this receipt, not a separate
product truth.

## Production and distribution language

Kew distinguishes:

1. a **versioned internal release**;
2. a **production-certified lane** for an exact supported journey;
3. a **public distribution launch** through channels such as PyPI or npm.

"Implemented," "dogfooded," "certified," "released," and "publicly
distributed" are not synonyms. Public distribution remains deliberately
unscheduled until the maintainer is ready. Production certification work does
not create a publication commitment.

## Product-change rule

Scope decisions start with the user job and the strategic boundary, then
use historical evidence and current certification to determine investment.
Lack of a post-hardening run is a reason to recertify a retained journey, not a
reason to delete it.

Subtraction remains important. Remove dead controls, duplicate mechanisms,
false interoperability, and concepts that do not earn a distinct user decision.
Use the repository's Pass 3 simplicity method to find those costs. Do not infer
broad product cuts from a security audit, and do not preserve redundant
implementation merely because the user-facing capability stays.

Kew remains governance for agentic software work. Local execution can prove
that the control model follows the operator down the stack; it does not turn
Kew into a generic infrastructure orchestrator or a lifecycle-data product.

## Near-term product sequence

Before expanding the roadmap or preparing public packages:

1. Make one coherent governed journey across the product: canonical work and
   artifact state, complete exception handling, review backpressure, restored
   Flight Recorder, tracker-neutral identity, and truthful cross-surface state.
2. Run a bounded post-hardening certification matrix over the proven lanes:
   Claude Code with GitHub, Claude Code with Linear-to-GitHub delivery,
   OpenCode/OpenRouter implementation and reviewer paths, and focused Codex
   publication-boundary evidence. Certify capabilities, not brand names in the
   abstract.
3. Define and integrate the offline `model-eol` lookup boundary while retaining
   Kew's model identity, outcomes, warnings, and interface value.
4. Run the current-code simplicity pass to collapse duplicated mechanisms and
   reduce concept and configuration count without revisiting settled scope
   decisions.
5. Replan the active dogfood epic around these product outcomes. Durable
   publication-shadow evidence and canonical external audit anchoring remain
   aligned; a Kew-owned curated lifecycle registry does not.

The public README, Philosophy, Roadmap, runner documentation, and website need
a separate product-truth alignment pass before this charter becomes a public
navigation page. Do not expose this charter as a substitute for
reconciling those claims.

## Decision record

- **2026-08-17:** Accepted this charter after the publication/security
  hardening review exposed a category error: missing current certification had
  been treated as evidence to remove historically dogfooded product lanes.
  Confirmed that GitHub and Linear intake, Claude Code, OpenCode/OpenRouter,
  Codex, CLI, TUI, web, supervised delegation/loops/epics, and model
  intelligence remain in Kew. Confirmed no GitLab, autopilot remains on hold and
  out of scope, `model-eol` owns lifecycle truth, `kew-cruise` is removed from product
  scope with code removal pending, and public package distribution is not
  scheduled.
- **2026-11-11:** Editorial amendment before publication. The charter says
  "Kew" where it named an edition of Kew, and "Scope" where it said "Product
  portfolio". No scope decision changed, and the open-source covenant stands as
  written.
