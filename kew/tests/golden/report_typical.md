# kew spend report — acme/widgets

- **Period:** 2026-06-02 → 2026-06-09 (7 days)
- **Generated:** 2026-06-09 12:00 UTC

## Totals

- **Total spend:** $16.40
- **Runs:** 6 (success 3 / failure 1 / killed 1 / budget_exceeded 1 / budget_stopped 0)
- **Tokens:** 380,000 (228,000 in / 152,000 out)
- **Daily cap:** avg daily spend $2.34 of $50.00 (4.7%)
- **Weekly cap:** window spend $16.40 of $200.00 pro-rated (8.2%)

## Outcomes

- **PRs opened:** 3
- **PRs merged:** 2
- **Cost per merged PR:** $8.20
- **Median cost per run:** $2.50

## Top 5 most expensive issues

| Issue | Title | Cost | Status | PR |
| --- | --- | --- | --- | --- |
| #104 | Migrate DB layer | $6.00 | budget_exceeded | — |
| #101 | Add login form | $4.00 | success | https://github.com/acme/widgets/pull/201 |
| #103 | Refactor parser | $3.00 | failure | — |
| #102 | Fix flaky test | $2.00 | success | https://github.com/acme/widgets/pull/202 |
| #105 | Update docs | $1.00 | success | https://github.com/acme/widgets/pull/205 |

## By model

| Model | Runs | Success rate | Avg cost |
| --- | --- | --- | --- |
| claude-haiku-4-5 | 2 | 100.0% | $1.50 |
| claude-opus-4-6 | 1 | 0.0% | $6.00 |
| claude-sonnet-4-6 | 3 | 33.3% | $2.47 |

## By runner

| Runner | Runs | Success rate | Avg cost |
| --- | --- | --- | --- |
| claude-code | 5 | 40.0% | $3.08 |
| opencode | 1 | 100.0% | $1.00 |

## Held / blocked

- #104 Migrate DB layer — budget exceeded ($6.00)

---

*Cost per merged PR = total spend in window ÷ distinct PRs from window runs that are merged at report time. Spend on failed and killed runs is included — that is the honest unit economics.*
