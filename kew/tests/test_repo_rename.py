"""Which audit rows a renamed repository owns (#3527).

``repo_match`` reads the ``repo_renamed`` events a real ledger holds; the rows
it is asked about are real audit rows of that ledger.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from kew.ledger_identity import open_managed_store
from kew.repo_rename import REPO_RENAMED_EVENT, RepoMatch, repo_match
from kew.run_store import AuditEvent, SqliteRunStore
from kew.run_store_rename import RepoRenameRefused

_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
HOST = "github.com"


def _store(root: Path) -> SqliteRunStore:
    db_path = root / "logs" / "dispatch.db"
    db_path.parent.mkdir(parents=True)
    return open_managed_store(
        project_root=root, db_path=db_path, anchors_path=root / "anchors.jsonl"
    )


def _row(store: SqliteRunStore, repo: str | None) -> AuditEvent:
    event_id = store.append_audit_event(
        timestamp=_NOW.isoformat(), event="dispatch_started", repo=repo
    )
    rows = store.list_audit_events(limit=None, event="dispatch_started")
    return next(row for row in rows if row.id == event_id)


def _rename(store: SqliteRunStore, old: str, new: str) -> None:
    store.rename_repo(old, new, host=HOST, actor_id="t", now=_NOW)


def test_a_chain_of_renames_counts_each_old_slug_until_its_own_rename(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        first_a = _row(store, "o/a")
        _rename(store, "o/a", "o/b")
        a_after_first = _row(store, "o/a")
        b_between = _row(store, "o/b")
        _rename(store, "o/b", "o/c")
        b_after = _row(store, "o/b")
        c_row = _row(store, "o/c")
        unrelated = _row(store, "o/z")
        no_repo = _row(store, None)
        match = repo_match(store, "o/c")
    finally:
        store.close()

    assert match.holds(first_a)
    assert match.holds(b_between)
    assert match.holds(c_row)
    assert not match.holds(a_after_first)
    assert not match.holds(b_after)
    assert not match.holds(unrelated)
    assert not match.holds(no_repo)


def test_a_rename_to_another_repository_vouches_for_nothing_here(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        old = _row(store, "o/a")
        _rename(store, "o/a", "o/b")
        match = repo_match(store, "o/elsewhere")
        empty = repo_match(store, "")
    finally:
        store.close()

    assert not match.holds(old)
    assert not empty.holds(old)


def test_the_old_name_reused_does_not_hold_the_renamed_repositorys_rows(
    tmp_path: Path,
) -> None:
    """After old -> new, the old name's history is new's: a reader scoped to
    the reused old name (a new repository holding it) counts only later rows."""
    store = _store(tmp_path)
    try:
        before = _row(store, "o/old")
        _rename(store, "o/old", "o/new")
        after = _row(store, "o/old")
        reused = repo_match(store, "o/old")
        renamed = repo_match(store, "o/new")
    finally:
        store.close()

    assert not reused.holds(before)
    assert reused.holds(after)
    assert renamed.holds(before)
    assert not renamed.holds(after)


def test_a_name_taken_after_it_was_renamed_away_joins_the_new_holder(
    tmp_path: Path,
) -> None:
    """b -> c, then a -> b: a's rows are the new b's, never c's, and the old
    b's rows stay c's."""
    store = _store(tmp_path)
    try:
        b_old = _row(store, "o/b")
        _rename(store, "o/b", "o/c")
        a_old = _row(store, "o/a")
        _rename(store, "o/a", "o/b")
        b_new = _row(store, "o/b")
        for_c = repo_match(store, "o/c")
        for_b = repo_match(store, "o/b")
    finally:
        store.close()

    assert for_c.holds(b_old)
    assert not for_c.holds(a_old)
    assert not for_c.holds(b_new)
    assert for_b.holds(a_old)
    assert for_b.holds(b_new)
    assert not for_b.holds(b_old)


def test_the_earliest_rename_is_the_cutoff_when_two_were_recorded(
    tmp_path: Path,
) -> None:
    """A ledger holding two old -> new events (an older kew appended a second
    one when old-slug rows reappeared) cuts at the first."""
    store = _store(tmp_path)
    try:
        before = _row(store, "o/old")
        _rename(store, "o/old", "o/new")
        between = _row(store, "o/old")
        store.append_audit_event(
            timestamp=_NOW.isoformat(),
            event=REPO_RENAMED_EVENT,
            repo="o/new",
            actor_type="human",
            actor_id="t",
            payload={"from": "o/old", "to": "o/new"},
        )
        match = repo_match(store, "o/new")
    finally:
        store.close()

    assert match.holds(before)
    assert not match.holds(between)


def test_a_second_rename_of_the_same_names_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _rename(store, "o/old", "o/new")
        with pytest.raises(RepoRenameRefused, match="already renamed"):
            _rename(store, "o/old", "o/new")
        events = store.list_audit_events(limit=None, event=REPO_RENAMED_EVENT)
    finally:
        store.close()

    assert len(events) == 1


def test_a_row_whose_id_cannot_be_compared_to_a_floor_fails_closed() -> None:
    match = RepoMatch("o/a", floors={"o/a": 5})

    assert not match.holds(SimpleNamespace(repo="o/a", id=None))
    assert match.holds(SimpleNamespace(repo="o/a", id=6))
    assert not match.holds(SimpleNamespace(repo="o/a", id=5))


def test_a_slug_renamed_into_a_name_before_it_was_renamed_away_stays_behind(
    tmp_path: Path,
) -> None:
    """dispatch -> kew, then kew -> kew-archive: dispatch's rows are
    kew-archive's, never those of the repository that takes the name kew."""
    store = _store(tmp_path)
    try:
        dispatch_row = _row(store, "o/dispatch")
        _rename(store, "o/dispatch", "o/kew")
        _rename(store, "o/kew", "o/kew-archive")
        reused = repo_match(store, "o/kew")
        renamed = repo_match(store, "o/kew-archive")
    finally:
        store.close()

    assert renamed.holds(dispatch_row)
    assert not reused.holds(dispatch_row)


def test_a_middle_name_renamed_away_passes_its_older_rows_on_only_once(
    tmp_path: Path,
) -> None:
    """a -> b, b -> z, then a new b is renamed b -> c: a's rows are z's only."""
    store = _store(tmp_path)
    try:
        a_row = _row(store, "o/a")
        _rename(store, "o/a", "o/b")
        _rename(store, "o/b", "o/z")
        _rename(store, "o/b", "o/c")
        holders = {slug: repo_match(store, slug) for slug in ("o/b", "o/c", "o/z")}
    finally:
        store.close()

    assert [slug for slug, match in holders.items() if match.holds(a_row)] == ["o/z"]
