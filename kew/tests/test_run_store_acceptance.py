"""Acceptance contracts in the ledger (#2726).

A node's acceptance contract is its read-only acceptance test file, written by
an ``acceptance_author`` run before the implementer starts. The ledger records
it by path, sha256, author run id and commit; an amendment adds a version, and
the newest version is the current one.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from kew.run_store import CURRENT_SCHEMA_VERSION, SqliteRunStore

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_COMMIT_A = "1" * 40
_COMMIT_B = "2" * 40
_PATH = "kew/tests/acceptance/test_issue_2726.py"
_PRE_V67 = 66
_PRE_V68 = 67
_PLAN_SHA = "c" * 64


def _record(store: SqliteRunStore, **overrides: object):
    fields: dict[str, object] = {
        "path": _PATH,
        "sha256": _SHA_A,
        "author_run_id": "run-author-1",
        "commit": _COMMIT_A,
    }
    fields.update(overrides)
    return store.record_acceptance_contract("o/r#2477", "2726", **fields)


def test_a_recorded_contract_reads_back_and_an_amendment_becomes_current(tmp_path):
    """AC1: path, digest, run id and commit round-trip; v2 is current, v1 listed."""
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        assert store.current_acceptance_contract("o/r#2477", "2726") is None
        assert store.list_acceptance_contracts("o/r#2477", "2726") == []

        first = _record(store)
        current = store.current_acceptance_contract("o/r#2477", "2726")
        assert current == first
        assert (
            current.epic_ref,
            current.item_id,
            current.version,
            current.path,
            current.sha256,
            current.author_run_id,
            current.commit,
        ) == ("o/r#2477", "2726", 1, _PATH, _SHA_A, "run-author-1", _COMMIT_A)
        assert current.created_at.tzinfo is not None

        second = _record(
            store, sha256=_SHA_B, author_run_id="run-author-2", commit=_COMMIT_B
        )
        assert second.version == 2
        current = store.current_acceptance_contract("o/r#2477", "2726")
        assert current is not None
        assert (current.version, current.sha256, current.author_run_id) == (
            2,
            _SHA_B,
            "run-author-2",
        )
        assert current.commit == _COMMIT_B

        history = store.list_acceptance_contracts("o/r#2477", "2726")
        assert [row.version for row in history] == [1, 2]
        assert history[0] == first
        assert history[1] == second


def test_re_recording_the_current_contract_mints_no_version(tmp_path):
    """A retried host write of the same contract is not an amendment."""
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        first = _record(store)
        again = _record(store)

        assert again == first
        assert len(store.list_acceptance_contracts("o/r#2477", "2726")) == 1


def test_contracts_are_scoped_per_node(tmp_path):
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        _record(store)
        other_epic = store.record_acceptance_contract(
            "o/r#9999",
            "2726",
            path=_PATH,
            sha256=_SHA_B,
            author_run_id="run-other",
            commit=_COMMIT_B,
        )
        other_item = store.record_acceptance_contract(
            "o/r#2477",
            "2727",
            path="kew/tests/acceptance/test_issue_2727.py",
            sha256=_SHA_B,
            author_run_id="run-other",
            commit=_COMMIT_B,
        )

        assert other_epic.version == 1
        assert other_item.version == 1
        current = store.current_acceptance_contract("o/r#2477", "2726")
        assert current is not None
        assert (current.version, current.sha256) == (1, _SHA_A)


@pytest.mark.parametrize(
    "overrides",
    [
        {"sha256": "A" * 64},
        {"sha256": "a" * 63},
        {"sha256": ""},
        {"commit": "abc"},
        {"commit": "g" * 40},
        {"author_run_id": ""},
        {"path": ""},
        {"path": "/abs/kew/tests/acceptance/test_issue_2726.py"},
        {"path": "kew/tests/../../etc/passwd"},
    ],
)
def test_a_malformed_contract_is_refused(tmp_path, overrides):
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        with pytest.raises(ValueError):
            _record(store, **overrides)
        assert store.list_acceptance_contracts("o/r#2477", "2726") == []


def test_a_sha256_commit_id_is_accepted(tmp_path):
    """A SHA-256 object-format repository names commits in 64 hex digits."""
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        assert _record(store, commit="c" * 64).commit == "c" * 64


def _as_pre_v67_ledger(db_path: Path) -> None:
    """Rewind a current ledger to v66: drop what v67 adds, stamp v66."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TABLE acceptance_contracts")
        conn.execute("UPDATE schema_version SET version = ? WHERE id = 1", (_PRE_V67,))
        conn.commit()
    finally:
        conn.close()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_v67_migration_applies_on_a_ledger_copy_and_is_idempotent(tmp_path):
    """AC3: a copy of a v66 ledger migrates, keeps its rows, and re-runs clean."""
    live = tmp_path / "live" / "dispatch.db"
    live.parent.mkdir()
    with SqliteRunStore(str(live)) as store:
        store.record_item_plan(
            "2726",
            body_digest="digest-a",
            source="extracted",
            plan_sha="plan-sha",
            plan_json="{}",
            criteria_count=3,
        )
    _as_pre_v67_ledger(live)

    copy = tmp_path / "copy" / "dispatch.db"
    copy.parent.mkdir()
    shutil.copy2(live, copy)

    with SqliteRunStore(str(copy)) as migrated:
        version = migrated._conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()["version"]
        assert version == CURRENT_SCHEMA_VERSION == 77
        # The ledger's existing rows survive the step.
        plan = migrated.current_item_plan("2726")
        assert plan is not None and plan.criteria_count == 3
        cols = {
            row["name"]
            for row in migrated._conn.execute("PRAGMA table_info(acceptance_contracts)")
        }
        assert cols == {
            "id",
            "epic_ref",
            "item_id",
            "version",
            "path",
            "sha256",
            "author_run_id",
            "commit_sha",
            "created_at",
            "plan_sha",
            "test_names",
        }
        _record(migrated)

        # Running the step again over a migrated ledger changes nothing.
        cursor = migrated._conn.cursor()
        migrated._apply_v67(cursor)
        migrated._conn.commit()
        assert len(migrated.list_acceptance_contracts("o/r#2477", "2726")) == 1

    # Reopening (a second migrate pass) is a no-op too, and the row stays.
    with SqliteRunStore(str(copy)) as reopened:
        current = reopened.current_acceptance_contract("o/r#2477", "2726")
        assert current is not None and current.version == 1
        assert (
            reopened._conn.execute("SELECT version FROM schema_version").fetchone()[
                "version"
            ]
            == 77
        )

    # The migration ran on the copy alone: the source ledger is still v66.
    conn = sqlite3.connect(live)
    try:
        live_version = conn.execute("SELECT version FROM schema_version").fetchone()
        assert live_version[0] == _PRE_V67
        assert "acceptance_contracts" not in _tables(conn)
    finally:
        conn.close()
    # And the copy was snapshotted before it was migrated.
    assert list(copy.parent.glob(f"dispatch.db.bak-pre-v{CURRENT_SCHEMA_VERSION}-*"))


def _as_pre_v68_ledger(db_path: Path) -> None:
    """Rewind a current ledger to v67: drop the plan binding, stamp v67."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("ALTER TABLE acceptance_contracts DROP COLUMN plan_sha")
        conn.execute("ALTER TABLE acceptance_contracts DROP COLUMN test_names")
        conn.execute("UPDATE schema_version SET version = ? WHERE id = 1", (_PRE_V68,))
        conn.commit()
    finally:
        conn.close()


def _contract_columns(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[1]) for row in conn.execute("PRAGMA table_info(acceptance_contracts)")
    }


def test_v68_migration_applies_on_a_ledger_copy_and_is_idempotent(tmp_path):
    """#2774 AC3: a copy of a v67 ledger gains the plan binding, keeps its
    contract unbound (it matches no plan), and re-running the step is a no-op."""
    live = tmp_path / "live" / "dispatch.db"
    live.parent.mkdir()
    with SqliteRunStore(str(live)) as store:
        _record(store)
    _as_pre_v68_ledger(live)

    copy = tmp_path / "copy" / "dispatch.db"
    copy.parent.mkdir()
    shutil.copy2(live, copy)

    with SqliteRunStore(str(copy)) as migrated:
        version = migrated._conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()["version"]
        assert version == CURRENT_SCHEMA_VERSION == 77
        assert {"plan_sha", "test_names"} <= _contract_columns(migrated._conn)
        kept = migrated.current_acceptance_contract("o/r#2477", "2726")
        assert kept is not None
        assert (kept.version, kept.sha256, kept.commit) == (1, _SHA_A, _COMMIT_A)
        assert (kept.plan_sha, kept.test_names) == (None, ())

        cursor = migrated._conn.cursor()
        migrated._apply_v68(cursor)
        migrated._conn.commit()
        assert migrated.list_acceptance_contracts("o/r#2477", "2726") == [kept]

        bound = _record(migrated, plan_sha=_PLAN_SHA, test_names=("test_ac1",))
        assert bound.version == 2

    with SqliteRunStore(str(copy)) as reopened:
        current = reopened.current_acceptance_contract("o/r#2477", "2726")
        assert current == bound
        assert (current.plan_sha, current.test_names) == (_PLAN_SHA, ("test_ac1",))

    conn = sqlite3.connect(live)
    try:
        live_version = conn.execute("SELECT version FROM schema_version").fetchone()
        assert live_version[0] == _PRE_V68
        assert "plan_sha" not in _contract_columns(conn)
    finally:
        conn.close()
    assert list(copy.parent.glob(f"dispatch.db.bak-pre-v{CURRENT_SCHEMA_VERSION}-*"))


def test_rebinding_the_current_contract_to_another_plan_mints_a_version(tmp_path):
    """Same file and commit, different plan: not a retried write, an amendment."""
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        first = _record(store, plan_sha=_PLAN_SHA, test_names=("test_ac1",))
        assert _record(store, plan_sha=_PLAN_SHA, test_names=("test_ac1",)) == first
        rebound = _record(store, plan_sha="d" * 64, test_names=("test_ac1",))
        assert rebound.version == first.version + 1
        with pytest.raises(ValueError, match="plan_sha"):
            _record(store, plan_sha="not-a-digest")


def test_the_current_contracts_for_an_item_are_each_epics_newest(tmp_path):
    """#2729: publication knows the item, not always the epic. It reads every
    epic's current version for the item, and no superseded one."""
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        assert store.current_acceptance_contracts_for_item("2726") == []

        _record(store)
        amended = _record(store, sha256=_SHA_B, commit=_COMMIT_B)
        other_epic = store.record_acceptance_contract(
            "o/r#9000",
            "2726",
            path=_PATH,
            sha256=_SHA_A,
            author_run_id="run-author-2",
            commit=_COMMIT_A,
        )
        store.record_acceptance_contract(
            "o/r#2477",
            "2727",
            path="kew/tests/acceptance/test_issue_2727.py",
            sha256=_SHA_A,
            author_run_id="run-author-3",
            commit=_COMMIT_A,
        )

        assert store.current_acceptance_contracts_for_item("2726") == [
            amended,
            other_epic,
        ]
