"""``count_container_runtimes``: what the fleet actually ran inside (#2298).

The health page draws the configured posture beside the observed one, so the
count has to come off the real column rather than off a fake. Two properties
matter: the aggregation happens SQL-side (the reader must not drain the ledger),
and a run with no recorded runtime is counted rather than dropped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from kew.run_store import RunRecord, SqliteRunStore


def _run(run_id: str, *, runtime: str | None, days_ago: float = 0.0) -> RunRecord:
    started = datetime.now(UTC) - timedelta(days=days_ago)
    return RunRecord(
        run_id=run_id,
        issue_num=int(run_id.rsplit("-", 1)[-1]),
        branch="agent/issue-1",
        status="success",
        exit_code=0,
        started_at=started,
        finished_at=started,
        container_runtime=runtime,
    )


def test_counts_each_recorded_runtime_within_the_window(tmp_path: Path) -> None:
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(_run("r-1", runtime="docker"))
        store.save(_run("r-2", runtime="docker"))
        store.save(_run("r-3", runtime="podman"))
        counts = store.count_container_runtimes(datetime.now(UTC) - timedelta(days=7))
    assert counts == {"docker": 2, "podman": 1}


def test_a_run_with_no_recorded_runtime_is_counted_not_dropped(tmp_path: Path) -> None:
    """A host run, and a run whose posture event could not be read, both land
    here. Dropping them would let a week of entirely uncontained runs read as a
    week with no runs at all."""
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(_run("r-1", runtime="docker"))
        store.save(_run("r-2", runtime=None))
        counts = store.count_container_runtimes(datetime.now(UTC) - timedelta(days=7))
    assert counts == {"docker": 1, "": 1}


def test_a_run_outside_the_window_is_not_counted(tmp_path: Path) -> None:
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(_run("r-1", runtime="docker"))
        store.save(_run("r-2", runtime="docker", days_ago=30))
        counts = store.count_container_runtimes(datetime.now(UTC) - timedelta(days=7))
    assert counts == {"docker": 1}


def test_an_empty_window_is_an_empty_map(tmp_path: Path) -> None:
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        assert store.count_container_runtimes(datetime.now(UTC)) == {}
