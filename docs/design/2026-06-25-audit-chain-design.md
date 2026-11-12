# Hash-Chained Audit Log + `kew audit verify` — Design (#249)

**Milestone:** M3 — Tamper-evidence (keystone)
**Builds on:** #246 audit envelope (PR #273) — `audit_events` table with `prev_hash`/`policy_hash` columns (NULL today), `Actor(type,id)`, `emit()`, `resolve_actor()`/`KEW_ACTOR`.
**Status:** approved design → adversarial review → writing-plans.

## Goal

Make the audit log tamper-evident: chain each `audit_events` row to the previous row's hash, add `kew audit verify`, and periodically anchor the chain head to a GitHub-controlled witness so the chain proves more than "nobody re-hashed the local file."

## What this is and is not

Tamper-evidence here is **detection**, not prevention, and it has honest limits (see §6). The chain proves the **integrity of what was written, not completeness** — writers are best-effort and audit failures never block dispatch. There is **no secret key**: a local-only chain is fully rewritable by anyone with disk access. Tamper-evidence depends on **anchoring** the head outside the operator's disk; backfilled pre-anchor history is made contiguous, not retroactively attested.

## Decisions (locked during brainstorm)

1. **Anchoring mechanism:** GitHub-native — publish the head to a GitHub-controlled witness. No Sigstore Rekor / RFC3161 in v1 (heavier deps, public-log metadata leak, weaker alignment with kew's GitHub-centric posture).
2. **Witness artifact:** an append-only `audit-anchors.jsonl` committed to the repo by a scheduled Action. The **Actions run log** (prints the head hash, GitHub-retained, survives a later force-push) is the real witness; the committed file is the convenient diff-able copy.
3. **Storage:** **no new column** — reuse the existing `prev_hash`; each row's own hash is computed on the fly from its stored columns.
4. **Backfill:** a one-time migration chains the pre-existing NULL-`prev_hash` rows so the table is contiguous from genesis.
5. **`kew audit anchor` is pure-local** (compute + append + print, no git/network); the workflow does `git commit`/`push`. Mirrors the #236 split (kew stays pure, the workflow does GitHub plumbing).
6. **Exit codes** for `kew audit verify`: `0` ok / `1` tamper detected / `2` cannot-run — matching `kew health` and `kew canary`.

---

## §1 — Architecture & module layout

New pure module **`src/kew/audit_chain.py`** (no I/O — trivially testable, low complexity):

- `GENESIS_PREV: str` — fixed constant for the first row's `prev_hash` (64 `"0"` chars).
- `entry_hash(fields) -> str` — SHA-256 hex over a **canonical, fixed-order** serialization of one row's **raw stored column values**:
  `id, timestamp, event, run_id, issue_num, repo, actor_type, actor_id, endpoint, policy_hash, payload (the verbatim stored TEXT), prev_hash`.
  Deterministic JSON: fixed key order, `ensure_ascii=True`, fixed separators. Computable from a single row in isolation (so anchor-verify needs only that one row, not the whole prefix).
  **Read off raw rows, never via `AuditEvent`/`_row_to_audit_event`** (I1): that read path `json.loads`es `payload` and coerces malformed/non-dict payloads to `None` (`run_store.py:550-572`), so recomputing from the dataclass would hash a re-serialized dict (key-order drift) or `None` and false-positive "tamper" on every row whose payload TEXT isn't byte-identical to `json.dumps(json.loads(text))` — and `audit_parse_report` confirms malformed-payload rows exist in practice. The chain therefore needs a dedicated **raw-row reader** (raw `payload` TEXT, raw `timestamp` string, raw `id`) used by append, verify, and anchor alike.
- `verify_chain(rows) -> ChainResult` — walk rows ascending by `id`; `expected_prev` starts at `GENESIS_PREV`; per row check `row.prev_hash == expected_prev`, then set `expected_prev = entry_hash(row)`. Returns: `ok: bool`, `broken_at_id: int | None`, `head_hash: str | None`, `count: int`.
- anchor record helpers: `format_anchor(id, entry_hash, count, timestamp) -> str` (one JSON line) and `parse_anchors(text) -> list[Anchor]`.

`run_store.py` keeps ownership of the **atomic append** (it owns the connection/transaction). `audit.py` gains the `verify` and `anchor` CLI subcommands, delegating to `audit_chain` + the store. `emit()` is unchanged for callers — the store fills `prev_hash` itself.

No new config section: a module constant `DEFAULT_ANCHORS_PATH = "audit-anchors.jsonl"` + a `--anchors` flag on both subcommands.

## §2 — Chained, serialized append (core correctness)

Today's `append_audit_event` does a bare `INSERT`. Python `sqlite3`'s default **deferred** transaction acquires the write lock only *at* the INSERT — after any SELECT. A naive read-tail-then-insert would let two worker processes both read the same tail and insert rows with the **same `prev_hash` → a forked chain**. Fix: lock *before* reading.

```text
append_audit_event (inside _with_busy_retry):
    BEGIN IMMEDIATE            # take the write lock now, before the read
    tail = SELECT * FROM audit_events ORDER BY id DESC LIMIT 1
    prev = entry_hash(tail) if tail else GENESIS_PREV
    INSERT ... prev_hash = prev
    COMMIT
```

`BEGIN IMMEDIATE` + WAL (already on) + `busy_timeout=5000` (already on) + the existing `_with_busy_retry` means a second concurrent writer blocks at `BEGIN IMMEDIATE`, then reads the *updated* tail — a strictly linear chain, no fork. The new row's `prev_hash` is the `entry_hash` of the **raw** tail row (raw `payload`/`timestamp`/`id`, per §1, I1); the inserted row's *own* hash is never computed at insert time, so the timestamp normalization in §2's INSERT can't desync from what verify later recomputes from the stored value (I2). The store computes `prev_hash` internally; `emit()`/callers no longer supply it. **The `prev_hash` kwarg stays in `append_audit_event` and the `RunStore` Protocol but is ignored/recomputed by the store** (M3 — lowest churn; keeps the Protocol and test fakes conformant, no mypy scramble).

**Transaction discipline — the rollback is the append's own job, NOT `_with_busy_retry`'s (C1):** the spec previously claimed "on any exception the wrapper rolls back." That is **false** against the real code — `_with_busy_retry` (`run_store.py:769-788`) calls `rollback()` *only* inside its `if "locked" in str(exc)` retry branch; any non-locked exception re-raises with **no rollback**, leaving the explicit transaction open. Under WAL that wedges the writer: the next `BEGIN IMMEDIATE` on the same still-open connection raises `OperationalError: cannot start a transaction within a transaction` — a string that does **not** contain `"locked"`, so it is not retried; `emit()` then swallows it (`audit.py:155`), and the process holds the write lock for its lifetime, blocking every other worker's append for the full `busy_timeout` until they fail. A single best-effort audit hiccup becomes a fleet-wide audit-write outage, silently.

So the chained append wraps its **own** `try/except: self._conn.rollback(); raise` (or a context manager) that closes the transaction on **any** exception, independent of `_with_busy_retry`. `_with_busy_retry` still wraps the whole thing for the locked-retry/backoff path. The transaction body lives in a small helper so `_do_append` stays xenon rank B (M4 — run the exact CI xenon command on the inline edit before declaring done; this repo has a documented B→C regression history). **Test (C1):** inject a mid-transaction failure, assert `conn.in_transaction is False` afterward and that the next `append_audit_event` succeeds.

## §3 — Backfill migration (schema v12)

A one-time migration chains existing rows. **It must run under `BEGIN IMMEDIATE` (C2)** — not the deferred commit `_migrate` uses today (`run_store.py:743-767`). The real topology is N worker processes each opening their own `SqliteRunStore` and running `_migrate` in `__init__` (that's *why* `_try_alter`/`INSERT OR IGNORE`/`MAX(version,…)` exist). The v12 backfill is **not** an idempotent `ALTER`; it's a read-all + per-row `UPDATE prev_hash` walk. Two consequences the plan must handle:

1. **Serialize against other migrators:** the `version < 12` check, the walk, and the version bump to 12 all happen **inside one `BEGIN IMMEDIATE` transaction**, so exactly one process does the backfill and the rest see version ≥ 12 and skip. (The deferred-commit migration path can let two processes both observe `version < 12` and interleave `UPDATE`s, producing a chain that fails its own verify.)
2. **Serialize against live appends:** the immediate transaction also blocks concurrent `append_audit_event` (which itself takes `BEGIN IMMEDIATE`), so no new row chains onto a tail whose hash the backfill is simultaneously rewriting.

Walk `audit_events` ascending; `running = GENESIS_PREV`; for each row `UPDATE … SET prev_hash = running`, then `running = entry_hash(raw_row_with_prev_now_set)`.

**Large-table cost:** the walk holds the write lock for one transaction over the whole table; on a very large audit log this is a long single lock-hold. v1 accepts this (audit tables are small in practice) and documents it; chunked backfill is deferred (a chunked version must still be crash-safe and produce one valid chain).

**Test (C2):** N processes each constructing a store against a seeded NULL-`prev_hash` table; assert exactly one valid chain afterward (and, in a second test, a valid chain with live appends interleaved).

Honest boundary (docs, not code): backfilled history is made *contiguous*, not *attested* — tamper-evidence begins at the first anchor.

## §4 — `kew audit verify [--anchors PATH] [--json]`

1. Read all rows ascending (via the raw-row reader, §1), run `verify_chain`.
2. If an anchors file is present (default `audit-anchors.jsonl`, else `--anchors`): for each anchor `(id, entry_hash, count)`, classify against the **local head id** (I3 — the committed anchors file travels with the repo but `dispatch.db` does not, so a fresh/lagging clone legitimately has fewer rows; treating that as tamper would false-positive for every consumer off the writer host):
   - anchor `id > local_head_id` → **benign lag** (the chain simply hasn't reached there yet): report as advisory, does **not** trigger exit 1.
   - anchor `id ≤ local_head_id` and the row is **present** → recompute `entry_hash`, compare; mismatch → **tamper**.
   - anchor `id ≤ local_head_id` and the row is **absent** (a gap where it should exist) → **tamper**.
   - `count` is **advisory only** (I4): recorded/displayed, never a standalone tamper trigger. It is the total row count at anchor time; do **not** assert `count == head_id` (autoincrement leaves gaps after deletes). The `entry_hash` is the real attestation.
3. Output: short human report via `rich`; `--json` for machines.

Exit codes:
- **0** — chain intact (and all in-range anchors matched; benign lag is still 0).
- **1** — tamper detected (broken chain link; or an in-range anchor mismatch/absent-row).
- **2** — cannot run (no `dispatch.db`; unreadable/malformed anchors file).
- An **empty** `audit_events` table is exit **0** (vacuously intact), distinct from exit 2 (M2).
- `verify_chain` **never assumes contiguous ids** (M5) — it walks by actual `id` comparing `prev_hash` links, so autoincrement gaps from rolled-back inserts are not flagged.

## §5 — `kew audit anchor` + `.github/workflows/audit-anchor.yml`

`kew audit anchor [--anchors PATH]` — pure local: read the tail row, compute head `entry_hash`, count total rows, append one JSON line `{id, entry_hash, count, timestamp}` to the anchors file, print it. No git, no network. When the chain is empty, it is a clean no-op (prints nothing to append, exit 0).

`audit-anchor.yml`:
- Triggers: scheduled `cron` (daily) + `workflow_dispatch`.
- `runs-on: ${{ vars.RUNNER_TARGET || 'ubuntu-latest' }}`.
- `concurrency: { group: audit-anchor, cancel-in-progress: false }` so two anchor runs can't race the push.
- Steps: checkout (default `persist-credentials` — it needs to push) → set up uv/kew → run `kew audit anchor` → `git add audit-anchors.jsonl`, commit + push **guarded to no-op when the head is unchanged** (`git diff --quiet` check).
- The Actions run log prints the head hash (the witness, within the limits in §6); the committed file is the diff-able copy.
- **Shared-trust-domain caveat (I5):** `runs-on: ${{ vars.RUNNER_TARGET }}` may be the *same self-hosted runner that holds the disk and writes the chain*, and the push uses the default `GITHUB_TOKEN`, which can also force-push/rewrite the file it anchors. So the committed `.jsonl` gives **no independent assurance** when pusher and writer share a trust domain; the run log is the only cross-boundary-ish witness, and only within the limits §6 spells out. A genuinely independent witness (push to a different repo/org, or a third-party transparency log) is **out of scope for v1** — noted as future work.

No live GitHub in pytest — the YAML is validated by parse + guard-by-inspection, same as #236's `approval.yml`.

## §6 — Honest-limitations docs (`docs/audit.md`)

State plainly:
1. **Completeness** — writers are best-effort (audit failures never block dispatch); the chain proves integrity of *what was written*, not that nothing is missing.
2. **No secret** — a local-only chain is fully rewritable by anyone with disk access (no keyed MAC); tamper-evidence depends on anchoring.
3. **Anchor strength & ceiling (tightened per I5)** — anchoring raises the bar to "rewrite the committed file/history *and* the GitHub Actions run logs." But that bar is bounded and must not be overstated:
   - The committed `audit-anchors.jsonl` provides **no independent assurance** when the runner that pushes it shares a trust domain with the chain writer (the common single-host setup) — the same `GITHUB_TOKEN` can force-push it.
   - Actions **run-log retention is limited** (90 days by default, configurable lower) and logs are **deletable by repo admins** via the API — so "survives a later force-push" is true only *within retention* and *against a non-admin* attacker. Do not claim permanence.
   - The operator **chooses** the head being anchored, so an anchor attests "this head existed at this time per GitHub," not "this is the complete or correct history."
   - Net honest claim: **tamper-evident against an attacker who does not control the GitHub Actions logs**, within the log-retention window. An operator controlling both disk and repo/Actions admin can still rewrite. kew is not an HSM.
4. **Backfill boundary** — pre-anchor history is contiguous, not retroactively attested.

## §7 — Testing strategy

- **`audit_chain` units:** `entry_hash` determinism + sensitivity (any field change flips it); genesis handling; `verify_chain` detects edited row, deleted middle row, reordered rows; anchor match vs mismatch vs absent-in-range vs benign-lag; `parse_anchors`/`format_anchor` round-trip; `verify_chain` does not assume contiguous ids (M5).
- **Raw-row round-trip (I1):** write via `append_audit_event`, read raw, recompute `entry_hash`, assert equal — including a payload with **non-ASCII** and one with **unusual key order**, plus a malformed/non-dict payload row (must still hash off the verbatim TEXT, not `None`).
- **Transaction recovery (C1):** inject a mid-transaction failure in the chained append; assert `conn.in_transaction is False` afterward and the next `append_audit_event` succeeds (no dangling-transaction lockout).
- **Concurrency (critical):** real on-disk db, N threads each appending M events with **genuine contention**; afterward assert `verify_chain` is intact and row count == N·M (no fork, no loss). (Avoids the #246 "concurrency test that wasn't actually contending" miss.)
- **Backfill (C2):** seed NULL-`prev_hash` rows, run migration, assert a valid chain from genesis; **N processes** each constructing a store against the seeded table → exactly one valid chain; a third test interleaves live appends during backfill → still one valid chain.
- **CLI:** `verify` exit 0/1/2 paths; **empty-table verify → exit 0** (M2); **benign-lag anchor (id > local head) → exit 0**; **in-range mismatch/absent → exit 1**; `anchor` appends + prints the right head; empty-chain anchor no-op; mocked/temp store.
- **Workflow YAML:** parses; guard-by-inspection of trigger/runner/concurrency/push-guard.

Gates (from `kew/`): `ruff format`, `ruff check`, `mypy src/`, `xenon B/B/A` (run_store.py and audit_chain.py are **not** excluded — new functions stay rank B), `pytest`.

## File map

- Create: `src/kew/audit_chain.py`, `tests/test_audit_chain.py`, `.github/workflows/audit-anchor.yml`, `docs/audit.md`
- Modify: `src/kew/run_store.py` (atomic chained append + v12 backfill migration), `src/kew/audit.py` (`verify` + `anchor` subcommands), `tests/test_audit.py` / `tests/test_run_store.py` / `tests/test_cli.py`
- Docs: `docs/audit.md`; cross-link from `docs/security.md`.

## Reuse

`audit.emit`/`Actor`/`resolve_actor`/`KEW_ACTOR`, `run_store.{append_audit_event, list_audit_events, AuditEvent, _with_busy_retry}`, the existing WAL + `busy_timeout` connection setup, `audit_app` (typer sub-app), the #236 workflow split pattern (pure CLI + workflow git plumbing).
