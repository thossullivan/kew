---
name: audit-chain-change
description: Use when touching the tamper-evident audit chain — audit_chain.py, audit.py, append_audit_event, entry_hash, anchors, or adding new attested facts to audit events
---

# Audit Chain Changes

All paths relative to `kew/` unless noted. Run commands from `kew/`.

## The forward rule (never violate)

New attested facts bind through the already-hashed `payload` dict — NEVER as new `entry_hash` preimage slots or columns. The one sanctioned exception is chain format 2 (#2029, pre-release), which brought the existing `item_id` and `dedup_key` columns into the hash; it is not a precedent. The fixed slots (`endpoint`, `policy_hash`, `prev_hash`) are grandfathered envelope fields from before the freeze; do not imitate them. Canonical example: `reported_model` (#251) is a key inside the `dispatch_completed` payload (normalized at src/kew/dispatch_core.py:546, keyed into the payload at :596), not a column.

## Invariants

- `entry_hash` (src/kew/audit_chain.py) hashes a FIXED-ORDER JSON array whose shape depends on the row's `chain_format` (schema v77, #2029). Format 1 (every row written before v77, frozen — published anchors cover them): id, timestamp, event, run_id, issue_num, repo, actor_type, actor_id, endpoint, policy_hash, payload, prev_hash. Format 2 (every row written since): the literal `2`, those twelve slots, then item_id, dedup_key. Order is load-bearing; no sort_keys. Never re-chain format-1 rows.
- The format only moves forward: `verify_chain` breaks at a format-1 row after a format-2 row, and at an unknown format. Every writer builds its INSERT through `chained_insert_statement` (src/kew/run_store_audit.py), which writes format 2 once the column exists — including the raw-connection writer in ledger_identity.py and the rolled-back write probe (`probe_audit_write`). A hand-written `INSERT INTO audit_events` takes the legacy default, and the `audit_chain_format_no_downgrade` trigger refuses it once a format-2 row is stored. `tests/test_audit_writers_chain_their_rows.py` (`-m policy`) refuses any such string literal under src/kew outside `chained_insert_statement`; it reads literals only, so an insert whose table name is assembled at runtime is caught by the trigger, not the lint.
- Readers select `*` and `_row_to_chain_row` tolerates a missing `item_id`/`dedup_key`/`chain_format` column, so a pre-v77 backup still verifies and restores.
- `payload` is hashed VERBATIM as the stored TEXT string — never re-parse or re-serialize it (test: `test_entry_hash_hashes_payload_text_verbatim_not_reparsed`).
- ONE hash implementation: writer (`AuditChainStoreMixin._chained_insert`, src/kew/run_store_audit.py) and verifier (`kew audit verify`) both call `entry_hash`. Never duplicate the hashing logic.
- `_row_to_chain_row` (src/kew/run_store_audit.py) stays in sync with `ChainRow` and `entry_hash`.
- The writer recomputes `prev_hash` under `BEGIN IMMEDIATE`; caller-supplied prev_hash is ignored.
- Off-chain data stays off-chain: `interactive_usage` (ingest, schema v15/#256) must never enter the preimage.
- `policy_hash` uses dict canonicalization with sort_keys (src/kew/policy.py:118); `entry_hash` uses an ordered array. Different on purpose — do not conflate.

## Anchors

- `kew audit anchor` appends `{id, entry_hash, count, timestamp}` to repo-root `audit-anchors.jsonl`; it refuses to anchor a broken chain.
- Daily workflow `.github/workflows/audit-anchor.yml` commits `chore(audit): anchor chain head [skip ci]`. It needs push credentials — do NOT set `persist-credentials: false` there (NO test asserts this — tests/test_audit_anchor_workflow.py checks other workflow properties; the push would just silently start failing).

## Checklist

- [ ] New fact goes in `payload`, not a new column or preimage slot
- [ ] Failing test first (tests/test_audit_chain.py for chain logic, tests/test_audit.py for CLI/emit)
- [ ] Chain verifies: `uv run kew audit verify --anchors ../audit-anchors.jsonl` → exit 0 (1 = tamper, 2 = cannot run)
- [ ] `../docs/audit.md` updated if the event schema or payload contract changed

## Acceptance

```bash
uv run pytest tests/test_audit_chain.py tests/test_audit.py tests/test_audit_anchor_workflow.py tests/test_audit_writers_chain_their_rows.py -v
```

Then the full pre-commit gate chain — `CLAUDE.md` Conventions + Trap Gates (ruff format + check, mypy, xenon, bandit, semgrep; becomes one `bash scripts/verify.sh` call once Epic 1 lands it).
