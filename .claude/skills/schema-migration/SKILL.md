---
name: schema-migration
description: Use when bumping the run_store SQLite schema version — adding a migration step, new table/column, or anything touching CURRENT_SCHEMA_VERSION, SchemaTooNewError, or backup/restore schema guards
---

# run_store Schema Migration

All paths relative to `kew/`. Run all commands from `kew/`.

## The pattern (three edit points, in lockstep)

1. Write `_apply_vN(self, cursor)` in `src/kew/run_store.py` (latest example: `_apply_v21`, ~line 1204).
   - Column adds: `self._try_alter(cursor, "ALTER TABLE ... ADD COLUMN ...")` — idempotent, swallows duplicate-column for concurrent openers.
   - New tables/indexes: `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`.
   - Last statement, always: `UPDATE schema_version SET version = MAX(version, N) WHERE id = 1`.
2. Append `self._apply_vN` to the ordered `steps` list in `_migrate()` (~line 1281). It is a list, not a dict; order is the migration order.
3. Bump `CURRENT_SCHEMA_VERSION = N` (`run_store.py:19`).

`test_current_schema_version_matches_apply_method_count` (tests/test_run_store.py) fails if the constant and step count diverge. Never "update the test to match" — fix the mismatch.

## Test first (canonical pattern)

Copy `test_schema_migration_v2_preserves_existing_data` (tests/test_run_store.py:58): hand-build a pre-N DB with raw sqlite3, insert a row, reopen via `SqliteRunStore`, assert the row survived and the new column/table exists.

## Checklist

- [ ] Failing migration test written first (old-DB-preserves-data pattern)
- [ ] All three edit points done (`_apply_vN` + steps append + constant bump)
- [ ] Hunt hardcoded version literals: `grep -rn "== <N-1>\|v<N-1>\|schema v" tests/ ../docs/` (replace `<N-1>` with the version you are migrating FROM) — known offenders as of v21: `tests/test_run_store_epic_ticks.py:41` (hardcodes current version), `../docs/operations.md:133` (stale SchemaTooNewError example)
- [ ] `kew/scripts/pricing_audit.py` (script-only since #1641, not in the wheel) replicates migration mechanics — sync it only if you changed HOW migrations apply (not needed for a plain new step)
- [ ] Never weaken: `SchemaTooNewError` raise on open (`_migrate`, run_store.py:1275); restore guards `_assert_schema_compat` + `_assert_db_schema_compat` (src/kew/backup.py:208, :216)
- [ ] Docs if user-visible: `../docs/operations.md` (Migration safety), `../docs/cli/backup.md`, `../docs/cli/restore.md`

## Acceptance

```bash
uv run pytest tests/test_run_store.py tests/test_run_store_epic_ticks.py tests/test_cli_schema_boundary.py tests/test_serve_schema_boundary.py tests/test_backup.py -v
```

Then the full pre-commit gate chain — `CLAUDE.md` Conventions + Trap Gates (ruff format + check, mypy, xenon, bandit, semgrep; becomes one `bash scripts/verify.sh` call once Epic 1 lands it).
