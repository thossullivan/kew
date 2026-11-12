"""Round-17 watcher admission, uncertain birth evidence, and v49 fixtures."""

import sqlite3
from unittest.mock import Mock

import pytest
import test_liveness_migrations as migrations
from delegate.test_cli import LABEL, MEMBER_ID, _FakeSink, _FakeTracker, _generous_toml
from delegate.test_watch import FakePoll
from test_liveness_owner import BIRTH, _record
from test_liveness_owner import process_table as process_table
from test_round14_liveness import OWNER

from kew import process_groups
from kew.delegate.run import WatchOutcome, process_candidate
from kew.delegate.watch import DelegationWatcher, InMemoryCursorStore
from kew.run_store import SqliteRunStore


def test_round17_watcher_reaps_dead_owner_and_dispatches_at_cap_one(
    tmp_path, process_table
):
    tracker, sink = _FakeTracker(), _FakeSink()
    config_path = _generous_toml(tmp_path)
    with SqliteRunStore(str(tmp_path / "watcher.db")) as store:
        row = _record(OWNER)
        store.save(row)
        assert store.live_run_count() == 1
        assert OWNER not in process_table.members
        watcher = DelegationWatcher(
            events=store,
            poll=FakePoll(["SFD-17"]),
            cursor_store=InMemoryCursorStore(),
            assignee_id=MEMBER_ID,
            label=LABEL,
            is_terminally_decided=Mock(return_value=False),
            poll_interval_seconds=20,
            clock=Mock(return_value=0),
        )

        candidates = watcher.tick()
        assert len(candidates) == 1
        outcome = process_candidate(
            candidates[0],
            member_id=MEMBER_ID,
            label=LABEL,
            store=store,
            tracker=tracker,
            sink=sink,
            fleet_cap=1,
            dry_run=False,
            config_path=config_path,
        )

        assert outcome is WatchOutcome.DISPATCHED, "dead owner must free cap one"
        reaped = store.get(row.run_id)
        assert (reaped.status, reaped.exit_code, reaped.error_category) == (
            "killed",
            137,
            "orphaned_pending_reaped",
        )
        reservations = store.list_epic_reservations("delegation")
        assert [reservation.item_id for reservation in reservations] == ["SFD-17"]
        assert tracker.fetched == ["SFD-17", "SFD-17"]
        # Dispatch acceptance reserves work; only the admitted child may claim it.
        assert tracker.transitioned == []
        assert sink.dispatched == ["SFD-17"]
        assert store.live_run_count() == 1
        assert not process_table.signals


@pytest.mark.parametrize("token", ["recorded-token", None], ids=["token", "legacy"])
@pytest.mark.parametrize("matches", [True, False], ids=["matching", "changed"])
def test_round17_unreadable_kernel_token_fallback(monkeypatch, token, matches):
    reader = Mock(return_value=None)
    monkeypatch.setattr(process_groups, "process_start_token", reader)
    live_birth = BIRTH if matches else "Wed Sep 16 10:00:00 2026"
    expected = matches if token is None else None

    assert (
        process_groups._owner_birth_alive(OWNER, BIRTH, token, live_birth) is expected
    )
    reader.assert_called_once_with(OWNER)


class _MigrationFixtureProbe:
    """Inspect the fixture's database immediately before its first upgrade."""

    def __init__(self):
        self.opens = 0

    def __call__(self, path):
        if self.opens == 1:
            with sqlite3.connect(path) as connection:
                version = connection.execute(
                    "SELECT version FROM schema_version"
                ).fetchone()[0]
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(runs)")
                }
            assert (version, "attempt_pgid" in columns) == (49, False), (
                "migration fixtures must start at v49 without attempt_pgid"
            )
        self.opens += 1
        return SqliteRunStore(path)


@pytest.mark.parametrize("pgid", [None, OWNER], ids=["null-pgid", "owner-equal-pgid"])
def test_round17_migration_fixtures_start_at_v49_without_attempt_column(
    tmp_path, monkeypatch, pgid
):
    probe = _MigrationFixtureProbe()
    monkeypatch.setattr(migrations, "SqliteRunStore", probe)

    migrations.test_round12_v50_owner_shapes_preserve_attempt(tmp_path, pgid)

    assert probe.opens == 3, "each fixture must upgrade and reopen twice"
