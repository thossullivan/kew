# Tuning the governance dials

kew's governance dials interact: a budget cap can silently starve a review
cycle, a concurrency cap can make a healthy epic look stalled, and a breaker
floor tuned for one model class is wrong for another. This page maps each dial
to the failure mode you'll actually observe when it's mis-set — every entry
here was hit for real during dogfooding, with the incident issue linked.

The short version: **when an epic looks stuck, check holds, then run rows,
then slots — in that order.**

```bash
kew epic status <name>          # holds + stuck reasons, ready frontier
sqlite3 logs/dispatch/dispatch.db \
  "select issue_num, run_kind, status from runs where status='pending'"
```

---

## `[parallel] max_workers` — the fleet slot pool

**What it governs.** The global cap on concurrently live runs — and "live"
means *every* pending run row: implementation dispatches, adversarial
reviewers, remediations, and operator-invoked `kew dispatch` / `kew review`
runs all draw from the same pool.

**Failure mode: silent review starvation.** Reviewer dispatch under a full
fleet is refused with a `fleet_full` result. The refusal is recorded as a
`refused` run row, but the driver's stdout keeps printing
`signal=progressing` — nothing tells you reviews are being starved. Observed:
two operator dispatches plus one reviewer saturated a `max_workers = 3` fleet,
and an epic's due review was refused every tick for ~10 minutes.

**Failure mode: a stale epic eating the fair share.** Concurrently driven
epics split the pool: each is bounded to its fair share,
`ceil(max_workers / contending epics)`, so no one epic can take every slot. An
epic stops contending when it reaches a terminal signal (complete, stalled,
awaiting merges) — and, since #2094, when a full day passes with no tick and no
live reservation, which retires an epic finished by hand or abandoned
mid-drive. "Live" is probed, not assumed: a reservation left behind by a
hard-killed run whose owner process is gone does not keep its epic contending,
so the abandoned epic whose dispatch was killed with it still ages out.
Before that age-out, four such epics made the fair share `ceil(6 / 6) = 1`, and
the one epic still being driven dispatched a single node per tick. Any tick
that withholds a ready node now names the limiter:
`waiting: <node> (admission-held: fair share 1 of 6 active epic(s) reached)`.

**Tuning.** Size it as: expected concurrent epic nodes + 1–2 reviewer slots +
headroom for operator dispatches you run by hand alongside a drive. If you
drive epics while also dispatching ad hoc, 3 is too small; 6 is a sane
single-operator default. Watch for `refused` run rows as the signal it's
still too tight.

---

## `[review] max_review_usd` — the cap on the review-stage pool

**What it governs.** The cap checked before every adversarial-reviewer launch —
against the node's *whole* review-stage spend, not the reviewer's share of it.
There is one pool per node: `check_review_budget` sums both `adversarial_review`
and `review_remediation` rows, so every verdict round *and* every governed
remediation draws the same pool down. `max_remediation_usd` (below) is a second,
larger cap read at the remediation gate — two caps over one pool, not two
pools. Size this one for full cycles, remediations included.

**Failure mode: mid-cycle exhaustion → HELD.** A full adversarial cycle on a
frontier model costs real money (observed: verdict rounds ~$2.50, governed
remediations $3–5 each). Two rounds + remediations ran $11–13 per node —
through a $10 default pool. The node HELDs with
`review_budget: review pool exhausted` *after* the work that found the bugs
is already paid for, stranding an almost-done node. (#993's original
misdiagnosis was exactly this dial; drilled again live 2026-07-26.)

**Tuning.** Budget for `max_rounds` full cycles: roughly
`max_rounds × (verdict cost + remediation cost)` for your reviewer model.
For an Opus-class reviewer with `max_rounds = 2`, $10 is structurally too
small; $25–40 matches observed costs. The pool is the *ceiling on rigor* —
an underfunded pool converts caught-bugs into operator toil.

---

## `[review] max_remediation_usd` — the cap on the fix cycle

**What it governs.** The cap checked before every governed remediation the
driver dispatches against a `NEEDS_CHANGES` verdict. It reads the same pool
`max_review_usd` does — verdict rounds and remediations both count — but sits
higher, because a fix cycle and a single reviewer pass have very different cost
profiles. Neither cap is cycle-scoped: `check_review_budget` can take a cycle
lower bound, but no dispatch path passes one, so both gates always sum the
node's whole review history. `kew epic retry` restarts round *numbering*, not
the pool: while the `review_budget` hold is live retry refuses outright and
names it (#2154), and clearing the hold with `kew epic unhold` without raising
the cap only re-HOLDs the node on its pre-retry total. Raise the cap the hold
names first.

**Failure mode: exhaustion mid-fix-cycle → HELD `review_budget`.** Observed: a
node reached round 5 having spent $43.97 against the $40 default. Every later
tick refused the remediation before launch and — until #2153 — said so only in
the driver log: the node stayed `remediating`, the tick signal stayed
`progressing`, and nothing surfaced it as needing a person. It now HOLDs with

    review_budget: remediation pool $40.00 exhausted (spent $43.97)
    — raise [review].max_remediation_usd

which `kew epic status` and the tick's waiting list both show.

**Tuning.** Raise the key the hold names. The driver re-checks the pool every
tick and clears the hold itself once the new cap admits the node, so the next
tick resumes the fix cycle — no `kew epic unhold` needed. Budget roughly
`max_rounds × (verdict cost + remediation cost)` for your runner — the sum this
cap is checked against includes the verdict rounds — and keep it at or above
`max_review_usd`, or this becomes the binding cap for the whole stage. A node
that keeps exhausting a generous pool is usually not converging, which is
`max_rounds`' job to catch.

---

## `[review] max_rounds` — the remediation-cycle cap

**What it governs.** How many verdict→remediate cycles a node gets before the
driver HELDs it (`review_rounds_exhausted`) for human judgment.

**Failure mode: legitimate convergence cut short.** A review that finds
*different real bugs each round* (not thrashing) hits the cap while
converging — observed: round 1 caught a pricing bug, round 2 caught a deeper
resume-bookkeeping bug, both real, node HELD anyway. The operator then owns
the tail: remediate by hand (`kew review <PR>`), raise the cap, or merge over
the findings.

**Tuning.** 2 is right for keeping cost bounded on routine nodes. For
governance-critical or cross-cutting nodes, expect 3. Read the held node's
round history before deciding: distinct findings each round → grant another
round; the same finding rephrased → the reviewer and implementer disagree,
and that's a human call, not another round. Granting another round is
`kew epic unhold <epic> <item>`: releasing a `review_rounds_exhausted` hold
restarts the node's round numbering in the same transaction, because the cap is
read from that count and a bare release would be re-held on it by the next
review pass (#2154).

**Interaction warning.** `max_rounds × cost-per-cycle` must fit inside
`max_review_usd` or the pool HELDs you before the round cap ever applies.

---

## `[agent] max_budget_usd` vs `[budget] per_issue_usd`

**What they govern.** The single-run spend ceiling vs the lifetime per-issue
ceiling (which includes spend from killed/failed prior runs).

**Failure mode: retry death-spiral.** If `per_issue_usd` doesn't cover sunk
spend from killed runs *plus* one fresh run at the runtime cap, a retried node
fails preflight forever (#445). Separately, a runtime cap larger than the
remaining per-issue headroom means a run can spend past the per-issue budget
before the preflight gate ever sees it — kew warns about this at dispatch.

**Tuning.** Keep `per_issue_usd ≥ 1.5–2× max_budget_usd` so one kill plus one
retry fits. Heed the dispatch-banner warning when overrides skew the pair.

---

## `[breaker]` — cost-anomaly factor and floor

**What it governs.** Runaway-spend kill. Since #1116 the floor is
bucket-relative, and the threshold is the largest of three terms:

| Term | Value | When it wins |
|---|---|---|
| `median_factor` | bucket median × `cost_anomaly_factor` | established buckets (≥ `min_samples`) |
| `relative_floor` | bucket median × `anomaly_floor_multiple` | under-sampled buckets, or when the multiple exceeds the factor |
| `abs_floor` | `min_anomaly_threshold_usd` | near-zero buckets only |

Every term is proportionate to the bucket's **own** median — including on the
under-sampled path, where the bucket has samples but fewer than `min_samples`.

An under-sampled bucket arms only when **both** conditions hold:

1. It has at least **three** of its own samples. One or two observations are not
   a median — arming off them makes the threshold a coin flip on the first run's
   size — so anything thinner stays dormant, as it did before #1116.
2. That median has reached the *route reference*: the same baseline query with
   the run-kind predicate **inverted**, i.e. the median over every other kind
   the route has run. It carries the same exclusions, so the two can never
   disagree about which rows count as normal.

The reference gates arming and never sets the threshold. It measures other kinds
of work, so a `review_remediation` bucket seen only *cheaper* than the rest of
its route tells you nothing about what a normal remediation costs there, and 3×
one cheap sample would SIGTERM a legitimate one — the #992 failure again.
Excluding the bucket's own kind is what makes the comparison mean anything: read
across all kinds, the reference is a superset of the bucket, and on a route that
has only ever run one kind it equals the bucket median exactly, so the gate would
pass for free.

Dormant, likewise, for a bucket with **no samples at all**, one on a route with
**no other kinds** (no reference to clear), and one where
`anomaly_floor_multiple = 0`. In each, the only thing left to arm on is the flat
`min_anomaly_threshold_usd` — the "floor is the whole threshold on a fresh
bucket" mode that killed #992. The absolute floor never arms a bucket on its
own; it only raises a threshold some relative term already set.

**Why it's shaped this way (#1116).** A single absolute floor failed in both
directions once routing spanned Opus and OpenRouter flash models in one fleet:

- *Floor too high → breaker decorative.* With cheap OpenRouter routes
  (~$0.08 medians), a $50 floor is 69× the median — a runaway needs ~600×
  overshoot before the breaker notices.
- *Floor too low for fresh buckets → kills legitimate work.* A new run-kind
  bucket has no median, so the floor IS the threshold; a legitimately large
  run gets SIGTERM-killed at exactly the floor (the #992/#993 incident, 3×,
  ~$20 lost).

**Tuning.** Leave `min_anomaly_threshold_usd` small ($1–2): its only job is to
absorb noise in near-zero buckets. Note that lowering it is *not*
behaviour-preserving — every established bucket whose median sits below
`floor ÷ factor` was governed by the floor and is now governed by
median × factor, which is tighter. Reach for `anomaly_floor_multiple` when a
bucket's spread is wide — raising it above `cost_anomaly_factor` loosens every
bucket proportionately instead of flattening them onto one dollar value.
Each dispatch logs the armed threshold and which term won
(`Circuit-breaker cost-anomaly threshold $X … via median_factor (…)`), so a
drill can verify arming without tripping it.

**What happens when a run crosses the threshold (#1798).** It depends on
which line of the agent's output the crossing happens on:

- **On the line that ends the run**, the agent has already finished. That is
  the line carrying the runner's terminal event (Claude Code's `result`,
  Codex's `turn.completed`; Codex reports its last usage on that same line).
  kew flags the anomaly but does not kill the run. The agent exits on its
  own, the run is not retried, and its work goes through the normal
  host-owned publication path, where the review and merge gates still decide.
  The dispatch output says the run was flagged, the run row's
  `error_category` is `cost_anomaly_flagged`, and that run's cost is left out
  of the median later thresholds are drawn from. The row never records less
  than the cost the run crossed the threshold at.
- **On any other line**, kew stops the run. The agent gets SIGTERM and 10
  seconds to exit before SIGKILL. The run is recorded as `killed`, is not
  retried, and its checkout is kept. The failure line and the issue comment
  name the rule, the observed cost and the threshold, e.g.
  `circuit breaker cost_anomaly ($7.66 over the $6.75 threshold)`.

A terminal event spares only the line it arrives on. A line that spends after
it shows the run did not end there, so that line is judged normally. A later
line that carries a terminal event of its own is the run's end again, and a
crossing on it is flagged. If the budget cap stops a flagged run that kept
spending, the run is a budget stop, not a breaker trip.

The `circuit_breaker` audit event records `threshold_usd`, `median_usd`,
`sample_count`, `threshold_source` and `terminal_observed` alongside
`observed_usd`, so the ledger shows why the cost counted as anomalous and
whether the run was stopped or only flagged.

---

## `max_budget_usd` enforcement — runner coverage

Mid-run budget kills are enforced natively by the claude-code runner and by
kew's own watchdog for other runners (#1108, fixed 2026-07-25). If you run a
kew older than that fix with non-claude runners, the per-run cap is
preflight-only — verify with a low-cap drill run before trusting it.

Contained (sandboxed) runs get the same cap (#1462). The claude container
receives `--max-budget-usd` (for an adversarial reviewer,
`[review].max_review_usd`; for a remediation, `max_remediation_usd`). A
contained dispatch that stops on that flag is recorded as a budget stop and
never published, even when claude reports success. A reviewer that stops on
it is not recorded as a budget stop: its run row is finalized from the
container's exit alone.

kew also stops the container itself for a dispatch whose container runs on
this machine (a contained remediation is watched by the host-path watchdog).
When the run's streamed cost passes `max_budget_usd`, kew records a
`budget_stop`. When it passes the circuit breaker's anomaly threshold, kew
records a `circuit_breaker` kill. The checkout is kept and the item fails.
A line that carries the run's result is not judged at all on this path, so a
crossing there neither stops nor flags the run. Any other line is judged
normally, including one that spends after an earlier result line.
Two paths have no kew-side stop:

- **The contained reviewer launcher** has only the claude argv cap. A
  non-claude reviewer gets no `--max-budget-usd` flag.
- **A remote placement** (`[sandbox] placement`) has only the claude argv cap.
  A non-claude runner placed remotely has no per-run cap at all.

A contained codex run reports its token usage only on its closing
`turn.completed` line. kew cannot stop it mid-run: the stop is judged on
streamed cost, and a closing line never trips it. The usage still counts
toward the run's recorded cost.

---

## Runtime overrides from the web surface

Budget and routing knobs set through the web config manager persist in
`dispatch.db` (`config_overrides`) and **win over the committed kew.toml** —
the dispatch banner marks them `(override)`. A forgotten override from a
heavy week quietly quadruples your caps. Audit with:

```bash
sqlite3 logs/dispatch/dispatch.db "select * from config_overrides"
```

---

## Operational failure modes (not dials, but you'll hit them)

- **Don't kill the terminal to pause work.** Killing the shell/driver can take
  in-flight governed runs down with it — inconsistently (SIGTERM to the
  process vs the tree). Use `kew stop` / `kew resume`. (#1124)
- **A dead fleet is currently silent.** A driver whose lock-holder PID is dead
  plus pending runs with dead process groups produces *no alarm* — check
  `kew epic status` and the lock file when things go quiet. (#1124)
- **`kew epic retry` diagnoses driver liveness.** Retry restores the admission
  labels and marker without spawning a background driver. If no process holds
  the epic lock it exits nonzero with the exact `kew epic run <name>` relaunch
  command; a live or indeterminate lock is left alone.
- **Operator-invoked runs are invisible to the epic reaper.** A killed
  `kew review <PR>` or ad hoc dispatch leaves a `pending` corpse that holds a
  fleet slot until cleaned by hand. (#1124)
- **Container authentication depends on the selected transport.** Direct
  container authentication needs the resolved runner's credential in the
  driver's environment. Selected sidecar authentication uses the sidecar's
  worker credential. That credential can satisfy admission without its value in
  the driver's environment. A native runner such as official Codex resolves its
  posture first. It authenticates through its own CLI login. For ordinary direct
  dispatch resolution, only
  `dangerously_skip_containment = true` permits the uncontained host path, and
  a missing credential is one of the conditions it bypasses.
  Three flows are exceptions. Review remediation and conflict resolution warn
  and run on the host (`uncontained_flow` in the audit log) when containment is
  unavailable. First-class loops run the agent on the host without sandbox
  resolution. `kew ci-fix` and `kew epic plan` use ordinary dispatch resolution.
  They require the escape hatch for uncontained host execution and emit a
  posture event. Planning uses a standalone `--no-hardlinks` clone. See
  [Security](security.md#sandboxed-dispatch-credential-posture).
