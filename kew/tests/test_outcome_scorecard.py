"""The weekly outcome scorecard, against a hand-computed week (C3/#2301).

AC1 of #2301: "an SQL test on a seeded ledger reproduces a hand-computed
week". The ledger below is small enough to add up on paper and is seeded with
every shape the rules exist for:

* three merged PRs sharing a week, one of them merely *touched* by kew (a CI
  fix on a PR a person opened) and two authored by it;
* a PR with two run rows, to prove PRs are counted distinct rather than
  summed off ``pr_state`` snapshots;
* a $0 base-integration marker row, which must not count as a run, as spend,
  or as the dispatch a readiness is measured from;
* a run carrying no PR, which is spend no merge can be divided by;
* a READY round whose item has no implementation run before it, which is
  omitted rather than dated from nothing;
* a node observed ``done`` twice, which closed one issue.

Each assertion's docstring writes out its own arithmetic, so a reader can
check the numbers on paper without running anything.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kew.config import BranchConfig, DispatchConfig
from kew.graph.domain import NodeStatus
from kew.graph.driver.base_integration import (
    STEP_EXHAUSTED_CATEGORY,
    STEP_MARKER_CATEGORY,
)
from kew.merged_main import (
    DEFAULT_REF,
    MergedMain,
    _parse,
    read_merged_main,
    week_start,
)
from kew.outcomes import _valid_pr_outcome
from kew.review_delta import VERDICT_READY
from kew.review_residuals import READY_WITH_RESIDUALS
from kew.run_store import PR_SUBJECT_RUN_KINDS, RunRecord, SqliteRunStore
from kew.run_store_outcomes import (
    MARKER_ERROR_CATEGORIES,
    MERGED_PR_STATE,
    NODE_STATUS_DONE,
    NON_AUTHORING_RUN_KINDS,
    READY_VERDICTS,
)
from kew.service.outcomes import MIN_SAMPLE, _base_ref, outcome_report, quantile

#: Friday of the second window week — "now" for every read here.
NOW = datetime(2026, 10, 2, 15, 0, tzinfo=UTC)
WEEK = "2026-09-21"
NEXT_WEEK = "2026-09-28"
EPIC = "design-c3"


def _ts(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def _git(repo: Path, *args: str, at: datetime | None = None) -> None:
    """A git command for test setup. *at* dates the commit it writes.

    The walk reads ``%cI`` — the committer date — so a seeded commit has to
    set it; left to the wall clock the commit lands outside the window the
    test asks for.
    """
    env = dict(os.environ)
    if at is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = at.isoformat()
    subprocess.run(
        [shutil.which("git") or "git", *args],
        cwd=repo,
        capture_output=True,
        check=True,
        env=env,
    )


def _master_only_clone(repo: Path, *, at: datetime, pr: int = 301) -> None:
    """A clone carrying one squash merge on ``origin/master`` and no ``main``.

    Read at the default ref such a repository is unavailable, so a denominator
    that reads at all is proof the configured base branch reached the walk.
    """
    repo.mkdir(exist_ok=True)
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@t")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", f"feat: a squash merge (#{pr})", at=at)
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")


def _run(
    run_id: str,
    *,
    item: str,
    started: datetime,
    finished: datetime | None = None,
    kind: str = "dispatch",
    cost: float = 0.0,
    pr: int | None = None,
    pr_state: str | None = None,
    merged_at: datetime | None = None,
    error_category: str = "",
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=int(item) if item.isdigit() else 0,
        item_id=item,
        branch=f"agent/{item}",
        status="success",
        exit_code=0,
        started_at=started,
        finished_at=finished,
        estimated_cost_usd=cost,
        run_kind=kind,
        error_category=error_category,
        pr_url=f"https://github.com/o/r/pull/{pr}" if pr else None,
        pr_state=pr_state,
        pr_merged_at=merged_at.isoformat() if merged_at else None,
    )


def _round(store: Any, item: str, *, at: datetime, verdict: str, number: int) -> None:
    """One review round at an exact instant.

    ``record_review_round`` stamps ``recorded_at`` with the wall clock, and a
    week's worth of history cannot be seeded through it. The row is inserted
    with the same columns the recorder writes; nothing here rewrites a row
    that exists.
    """
    store._conn.execute(
        "INSERT INTO review_rounds"
        " (epic_ref, item_id, round, head_sha, verdict, recorded_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (EPIC, item, number, f"sha{number}", verdict, at.isoformat()),
    )
    store._conn.commit()


def _node_status(store: Any, item: str, *, at: datetime, to: str) -> None:
    store.append_audit_event(
        timestamp=at.isoformat(),
        event="epic_node_status",
        item_id=item,
        issue_num=int(item),
        actor_type="driver",
        actor_id="kew.graph.driver",
        payload={"epic_ref": EPIC, "from": "in_review", "to": to},
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Any:
    """The hand-computed ledger. See the module docstring for what each row is."""
    with SqliteRunStore(str(tmp_path / "outcomes.db")) as store:
        # PR 101 — authored by kew, four rows, one PR. Merged 09-22.
        store.save(
            _run(
                "r-101-impl",
                item="101",
                started=_ts(21, 9),
                finished=_ts(21, 11),
                cost=10.0,
                pr=101,
                pr_state="OPEN",
            )
        )
        store.save(
            _run(
                "r-101-review",
                item="101",
                started=_ts(22, 9),
                finished=_ts(22, 9, 30),
                kind="adversarial_review",
                cost=5.0,
                pr=101,
                pr_state="MERGED",
                merged_at=_ts(22, 12),
            )
        )
        # PR 102 — a person's PR kew only fixed CI on. Merged 09-24.
        store.save(
            _run(
                "r-102-ci",
                item="102",
                started=_ts(23, 10),
                finished=_ts(23, 11),
                kind="ci_fix",
                cost=3.0,
                pr=102,
                pr_state="MERGED",
                merged_at=_ts(24, 8),
            )
        )
        # PR 103 — authored by kew, merged the same week.
        store.save(
            _run(
                "r-103-impl",
                item="103",
                started=_ts(25, 8),
                finished=_ts(25, 10),
                cost=32.0,
                pr=103,
                pr_state="MERGED",
                merged_at=_ts(25, 20),
            )
        )
        # PR 104 — opened, not merged. Spend, but no merge to divide by.
        store.save(
            _run(
                "r-104-impl",
                item="104",
                started=_ts(26, 9),
                finished=_ts(26, 10),
                cost=7.0,
                pr=104,
                pr_state="OPEN",
            )
        )
        # A run that never opened a PR: unattributed spend.
        store.save(_run("r-105-impl", item="105", started=_ts(22, 14), cost=4.0))
        # A $0 base-integration marker. Not a run, not spend, not a dispatch.
        store.save(
            _run(
                "r-103-marker",
                item="103",
                started=_ts(25, 7),
                finished=_ts(25, 7),
                kind="resolve",
                error_category=STEP_MARKER_CATEGORY,
            )
        )
        # The following week, so the window really has two rows.
        store.save(
            _run(
                "r-106-impl",
                item="106",
                started=_ts(29, 9),
                finished=_ts(29, 11),
                cost=6.0,
                pr=106,
                pr_state="MERGED",
                merged_at=_ts(30, 9),
            )
        )

        _round(store, "101", at=_ts(21, 12), verdict="NEEDS_CHANGES", number=1)
        _round(store, "101", at=_ts(22, 11, 30), verdict="READY", number=2)
        _round(store, "103", at=_ts(25, 9), verdict="READY", number=1)
        # A READY round on the PR kew only touched: no implementation run of
        # its own, so there is no dispatch to measure from.
        _round(store, "102", at=_ts(23, 10, 30), verdict="READY", number=1)

        _node_status(store, "101", at=_ts(22, 12, 5), to=NODE_STATUS_DONE)
        _node_status(store, "103", at=_ts(25, 20, 5), to=NODE_STATUS_DONE)
        # The same node observed done again on a later tick: one closed issue.
        _node_status(store, "101", at=_ts(26, 8), to=NODE_STATUS_DONE)
        # A transition that is not a close.
        _node_status(store, "104", at=_ts(26, 9, 5), to="in_review")
        yield store


def _report(store: Any, **kwargs: Any) -> Any:
    return outcome_report(
        store,
        weeks=kwargs.pop("weeks", 2),
        now=kwargs.pop("now", NOW),
        merged_main=kwargs.pop(
            "merged_main",
            MergedMain(by_week={WEEK: 66, NEXT_WEEK: 71}, head="abc1234"),
        ),
        **kwargs,
    )


def _week(store: Any, week: str = WEEK, **kwargs: Any) -> Any:
    return next(row for row in _report(store, **kwargs).weeks if row.week_start == week)


# --------------------------------------------------------------------------- #
# AC1 — the hand-computed week
# --------------------------------------------------------------------------- #


def test_window_is_mondays_oldest_first(ledger: Any) -> None:
    """Two weeks ending in the one that contains 2026-10-02 (a Friday)."""
    report = _report(ledger, weeks=2)
    assert [row.week_start for row in report.weeks] == [WEEK, NEXT_WEEK]
    assert [row.partial for row in report.weeks] == [False, True]
    assert report.min_sample == MIN_SAMPLE
    assert report.merged_main_head == "abc1234"
    assert report.merged_main_unavailable is None


def test_merged_prs_are_counted_distinct_and_split_by_authorship(ledger: Any) -> None:
    """PRs 101, 102 and 103 merged that week; 101 has two rows and is one PR.

    102 is a person's PR kew only fixed CI on, so touched is 3 and authored 2.
    """
    week = _week(ledger)
    assert week.merged_main == 66
    assert week.merged_kew_touched == 3
    assert week.merged_kew_authored == 2


def test_cost_per_merged_pr_is_hand_computable(ledger: Any) -> None:
    """Costs per merged PR are 101 -> $15 (10 + 5), 102 -> $3, 103 -> $32.

    mean = 50/3 = 16.666…; median of [3, 15, 32] = 15.
    """
    cost = _week(ledger).cost_per_merged
    assert cost.mean == pytest.approx(50.0 / 3.0)
    assert cost.p50 == pytest.approx(15.0)


def test_spend_splits_the_part_no_pr_explains(ledger: Any) -> None:
    """15 + 3 + 32 + 7 + 4 = $61 dispatched that week, $4 of it with no PR.

    The $0 marker row is in neither figure.
    """
    week = _week(ledger)
    assert week.spend_total == pytest.approx(61.0)
    assert week.spend_unattributed == pytest.approx(4.0)


def test_ready_hours_and_rounds_are_hand_computable(ledger: Any) -> None:
    """Item 101: dispatched 09-21 09:00, READY 09-22 11:30 -> 26.5 h, 2 rounds.
    Item 103: dispatched 09-25 08:00, READY 09-25 09:00 -> 1.0 h, 1 round.

    Median of [1.0, 26.5] = 13.75; p90 = 1.0 + 0.9 * 25.5 = 23.95; max 26.5.
    Rounds: median of [1, 2] = 1.5, max 2.
    """
    week = _week(ledger)
    assert week.ready_items == 2
    assert week.ready_hours.p50 == pytest.approx(13.75)
    assert week.ready_hours.p90 == pytest.approx(23.95)
    assert week.ready_hours.max == pytest.approx(26.5)
    assert week.rounds_to_ready.p50 == pytest.approx(1.5)
    assert week.rounds_to_ready.max == 2


def test_ready_round_without_a_dispatch_is_omitted_not_invented(ledger: Any) -> None:
    """Item 102's only run is a CI fix on a person's PR, so its READY round has
    no dispatch to measure from. It is left out — never dated from zero."""
    items = [row.item_id for row in ledger.ready_items_by_week(since=_ts(21, 0))]
    assert items == ["101", "103"]


def test_readiness_reads_the_accepted_verdict_not_only_the_reviewers(
    tmp_path: Path,
) -> None:
    """A round the review stop rule accepted is a readiness (#2151).

    Item 107 was dispatched 09-28 09:00 and its one round came back
    NEEDS_CHANGES; the driver then accepted it as READY_WITH_RESIDUALS, which
    ``accept_review_round`` writes BESIDE the reviewer's verdict and never over
    it. Those three hours are therefore the test of both halves of the
    expression: read ``verdict`` alone and the item vanishes from the week
    entirely, spell the accepted literal differently and it vanishes too.
    """
    with SqliteRunStore(str(tmp_path / "accepted.db")) as store:
        store.save(
            _run(
                "r-107-impl",
                item="107",
                started=_ts(28, 9),
                finished=_ts(28, 10),
                cost=9.0,
                pr=107,
                pr_state="OPEN",
            )
        )
        _round(store, "107", at=_ts(28, 12), verdict="NEEDS_CHANGES", number=1)
        # The driver's own writer, so the column the query reads is the column
        # the acceptance actually lands in.
        assert store.accept_review_round(
            "107",
            round_num=1,
            head_sha="sha1",
            sidecar_digest=None,
            verdict=READY_WITH_RESIDUALS,
        )
        rows = store.ready_items_by_week(since=_ts(21, 0))
        assert [
            (row.week_start, row.item_id, row.hours_to_ready, row.rounds)
            for row in rows
        ] == [(NEXT_WEEK, "107", 3.0, 1)]
        assert _week(store, NEXT_WEEK).ready_items == 1


def test_nodes_done_counts_nodes_not_transitions(ledger: Any) -> None:
    """101 (twice) and 103 reached done; 104 reached in_review. Two closed."""
    assert _week(ledger).nodes_done == 2


def test_pr_sync_freshness_is_the_newest_merged_snapshot(ledger: Any) -> None:
    """The newest row that observed a merge that week is 103's, at 09-25 10:00
    — so the merge counts are no fresher than that."""
    assert _week(ledger).pr_sync_observed_at == _ts(25, 10).isoformat()


def test_a_merged_snapshot_without_a_merge_timestamp_still_counts(
    tmp_path: Path,
) -> None:
    """``pr_state`` holds GitHub's own upper-case spelling and SQLite compares
    TEXT case-sensitively, so the merged predicate has to match it: a snapshot
    that says MERGED with no ``pr_merged_at`` is dated from the run that
    observed it (09-22 10:00 -> the 09-21 week), never dropped from the count.
    """
    with SqliteRunStore(str(tmp_path / "nomergedat.db")) as store:
        store.save(
            _run(
                "r-201-impl",
                item="201",
                started=_ts(22, 9),
                finished=_ts(22, 10),
                cost=8.0,
                pr=201,
                # The literal GitHub writes, not the module's own constant:
                # seeded from the constant this row would move with the bug.
                pr_state="MERGED",
            )
        )
        rows = store.merged_prs_by_week(since=_ts(21, 0))
        assert [(row.week_start, row.pr_url) for row in rows] == [
            (WEEK, "https://github.com/o/r/pull/201")
        ]
        week = _week(store)
        assert week.merged_kew_touched == 1
        assert week.merged_kew_authored == 1
        assert week.cost_per_merged.mean == pytest.approx(8.0)
        assert week.pr_sync_observed_at == _ts(22, 10).isoformat()


def test_marker_rows_are_not_runs_and_not_dispatches(ledger: Any) -> None:
    """The $0 marker started an hour before 103's implementation run. Counted
    as a dispatch it would make 103 look two hours to READY instead of one."""
    spend = {row.week_start: row for row in ledger.spend_by_week(since=_ts(21, 0))}
    assert spend[WEEK].runs == 6
    assert spend[WEEK].spend_total == pytest.approx(61.0)
    hours = {
        row.item_id: row.hours_to_ready
        for row in ledger.ready_items_by_week(since=_ts(21, 0))
    }
    assert hours["103"] == pytest.approx(1.0)


def test_the_second_week_reads_on_its_own(ledger: Any) -> None:
    """One merged PR, no review rounds, $6 — and no borrowing from week one."""
    week = _week(ledger, NEXT_WEEK)
    assert (week.merged_main, week.merged_kew_touched, week.merged_kew_authored) == (
        71,
        1,
        1,
    )
    assert week.ready_items == 0
    assert week.ready_hours.p50 is None
    assert week.rounds_to_ready.max is None
    assert week.spend_total == pytest.approx(6.0)


def test_a_week_with_nothing_in_it_is_zero_not_missing(ledger: Any) -> None:
    """Every requested week is a row. A gap in the ledger is a flat point on
    the chart, which is a reading; a missing row is a hole in the x-axis."""
    weeks = [row.week_start for row in _report(ledger, weeks=6).weeks]
    assert weeks == [
        "2026-08-24",
        "2026-08-31",
        "2026-09-07",
        "2026-09-14",
        WEEK,
        NEXT_WEEK,
    ]
    empty = _week(ledger, "2026-09-07", weeks=6)
    assert (empty.merged_kew_touched, empty.ready_items, empty.nodes_done) == (0, 0, 0)
    assert empty.cost_per_merged.mean is None


# --------------------------------------------------------------------------- #
# Bounds, drift and the git denominator
# --------------------------------------------------------------------------- #


def test_window_is_bounded(ledger: Any) -> None:
    for weeks in (0, 53):
        with pytest.raises(ValueError, match="between 1 and 52"):
            _report(ledger, weeks=weeks)


def test_a_pr_dispatched_before_the_window_counts_in_the_week_it_merged(
    tmp_path: Path,
) -> None:
    """Membership is decided by the merge, not by when the work started (#2387).

    The attribution scan reaches ``ATTRIBUTION_LOOKBACK_DAYS`` behind the
    window. PR 301's only run started 40 days before it — but it merged on
    09-23, inside week one, and ``git log`` counts it in the denominator
    regardless. Bounding the numerator by the scan alone drew the headline
    share of the oldest weeks as 0 of 1.

    PR 302 is the same hole through the other column: a merged snapshot that
    carries no ``pr_merged_at``, dated from the finish that observed it.
    """
    with SqliteRunStore(str(tmp_path / "lookback.db")) as store:
        store.save(
            _run(
                "r-301-impl",
                item="301",
                started=datetime(2026, 8, 12, 9, tzinfo=UTC),
                finished=datetime(2026, 8, 12, 11, tzinfo=UTC),
                cost=12.0,
                pr=301,
                pr_state="MERGED",
                merged_at=_ts(23, 10),
            )
        )
        store.save(
            _run(
                "r-302-impl",
                item="302",
                started=datetime(2026, 8, 13, 9, tzinfo=UTC),
                finished=_ts(24, 10),
                cost=8.0,
                pr=302,
                pr_state="MERGED",
            )
        )
        rows = store.merged_prs_by_week(since=_ts(21, 0))
        assert [(row.week_start, row.pr_url, row.cost_usd) for row in rows] == [
            (WEEK, "https://github.com/o/r/pull/301", 12.0),
            (WEEK, "https://github.com/o/r/pull/302", 8.0),
        ]
        week = _week(store)
        assert (week.merged_main, week.merged_kew_touched) == (66, 2)


def test_the_merged_pr_read_seeks_three_indexes_instead_of_scanning(
    tmp_path: Path,
) -> None:
    """The merge-evidence arms must stay index-backed.

    This module exists so a report does not read the whole ledger to print
    eight weeks, and an OR is all-or-nothing to the planner: one arm it cannot
    serve from an index — a column with no index, or a ``COALESCE`` around one
    — and the entire read becomes a table scan that grows forever. The SQL is
    read back off the connection rather than restated, so the plan asserted
    here is the plan of the query the implementation issues.
    """
    with SqliteRunStore(str(tmp_path / "plan.db")) as store:
        seen: list[str] = []
        store._conn.set_trace_callback(seen.append)
        try:
            store.merged_prs_by_week(since=_ts(21, 0))
        finally:
            store._conn.set_trace_callback(None)
        sql = next(s for s in seen if "FROM runs" in s)
        plan = " ".join(
            str(row[3])
            for row in store._conn.execute(
                "EXPLAIN QUERY PLAN " + sql, [None] * sql.count("?")
            )
        )
    assert "SCAN runs" not in plan
    for index in ("idx_runs_started", "idx_runs_pr_merged", "idx_runs_finished"):
        assert index in plan


def test_unreadable_clone_leaves_merged_main_none_with_a_reason(ledger: Any) -> None:
    """A denominator that could not be read is None on every week, with one
    sentence saying so — never a zero the operator reads as "we merged all of
    it"."""
    report = _report(ledger, merged_main=MergedMain(unavailable="no origin/main"))
    assert [row.merged_main for row in report.weeks] == [None, None]
    assert report.merged_main_unavailable == "no origin/main"


def test_merged_main_parses_squash_subjects_into_distinct_prs() -> None:
    """``(#N)`` at the END of a subject, counted once per PR per week."""
    parsed = _parse(
        "\n".join(
            [
                "2026-09-22T10:00:00+00:00\tfeat: a thing (#101)",
                "2026-09-23T10:00:00+00:00\tfix: the same PR again (#101)",
                "2026-09-24T10:00:00+00:00\tchore: another (#102)",
                "2026-09-24T10:00:00+00:00\tdocs: mentions (#999) mid-sentence",
                "2026-09-30T10:00:00+00:00\tfeat: next week (#103)",
                "not a commit line at all",
            ]
        )
    )
    assert {week: sorted(prs) for week, prs in parsed.items()} == {
        WEEK: [101, 102],
        NEXT_WEEK: [103],
    }


def test_merged_main_week_matches_the_sql_week_expression(ledger: Any) -> None:
    """The git bucket and the SQL bucket are the same seven days — Monday to
    Sunday, UTC. A Sunday evening commit belongs to the week that opened six
    days earlier, which is where the ledger puts its runs too."""
    rows = ledger._conn.execute(
        "SELECT date(?, 'weekday 0', '-6 days') AS week", (_ts(27, 23, 30).isoformat(),)
    ).fetchone()
    assert rows["week"] == week_start(_ts(27, 23, 30)) == WEEK


def test_merged_main_read_of_a_non_repository_says_why(tmp_path: Path) -> None:
    from kew import merged_main as module

    module.reset_cache()
    result = read_merged_main(tmp_path, since=_ts(21, 0))
    assert result.by_week == {}
    assert result.unavailable is not None
    assert "origin/main" in result.unavailable
    module.reset_cache()


def test_denominator_follows_the_configured_base_branch(tmp_path: Path) -> None:
    """A clone whose base branch is ``master`` gets its own denominator.

    ``read_merged_main`` defaults to ``origin/main``; left to that default the
    share chart on such a clone is permanently unavailable even though the
    merges are right there on ``origin/master``. The repository below has no
    ``main`` at all, so the week only reads if the configured base reached the
    git walk.
    """
    from kew import merged_main as module

    repo = tmp_path / "clone"
    _master_only_clone(repo, at=_ts(22, 9))

    with SqliteRunStore(str(tmp_path / "base.db")) as store:
        module.reset_cache()
        on_master = _report(
            store,
            merged_main=None,
            repo_dir=repo,
            config=DispatchConfig(branches=BranchConfig(base="master")),
        )
        module.reset_cache()
        on_default = _report(store, merged_main=None, repo_dir=repo)
        module.reset_cache()

    assert on_master.merged_main_unavailable is None
    assert [row.merged_main for row in on_master.weeks] == [1, 0]
    assert on_master.merged_main_head
    # The same clone read at the default ref: a reason, not a silent zero.
    assert on_default.merged_main_unavailable == (
        "origin/main could not be read from the local clone"
    )


def test_base_ref_is_the_remote_of_the_configured_branch() -> None:
    """Unset base keeps the default ref; a set one is read from origin."""
    assert _base_ref(None) == DEFAULT_REF
    assert _base_ref(DispatchConfig()) == DEFAULT_REF
    assert (
        _base_ref(DispatchConfig(branches=BranchConfig(base="release/2.x")))
        == "origin/release/2.x"
    )


def test_no_repo_dir_is_a_reason_not_a_crash() -> None:
    assert read_merged_main(None, since=_ts(21, 0)).unavailable is not None


def test_a_host_without_git_gets_a_sentence_not_an_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``preflight_tools`` exits the process on a missing required tool. A
    report endpoint must not: the chart says git is absent and the ledger
    numbers beside it still draw."""
    from kew import merged_main as module

    monkeypatch.setattr(module.subprocess_utils, "ensure_tool", lambda tool: None)
    module.reset_cache()
    result = read_merged_main(tmp_path, since=_ts(21, 0))
    module.reset_cache()
    assert result.unavailable == "git is not installed, so main cannot be read"


def test_ensure_tool_resolves_on_demand_and_refuses_what_is_absent() -> None:
    """The allowlist still holds only paths this process resolved itself."""
    from kew import subprocess_utils

    assert subprocess_utils.ensure_tool("git") is not None
    assert subprocess_utils.TOOL_PATHS["git"].startswith("/")
    assert subprocess_utils.ensure_tool("kew-no-such-tool-exists") is None
    assert "kew-no-such-tool-exists" not in subprocess_utils.TOOL_PATHS


def test_quantile_edges() -> None:
    assert quantile([], 0.5) is None
    assert quantile([4.0], 0.9) == 4.0
    assert quantile([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.5)
    assert quantile([1.0, 2.0, 3.0, 4.0], 1.0) == pytest.approx(4.0)


def test_restated_constants_have_not_drifted() -> None:
    """``run_store_outcomes`` restates three things it may not import, because
    it sits below them. These are the originals."""
    assert set(MARKER_ERROR_CATEGORIES) == {
        STEP_MARKER_CATEGORY,
        STEP_EXHAUSTED_CATEGORY,
    }
    non_authoring = set(NON_AUTHORING_RUN_KINDS)
    assert non_authoring >= PR_SUBJECT_RUN_KINDS
    assert NodeStatus.DONE.value == NODE_STATUS_DONE
    # ``READY_VERDICTS`` restates the two dispositions that read merge-ready:
    # the reviewer's own word and the stop rule's acceptance of a round the
    # reviewer did not call ready (#2151). A literal that drifts from either
    # drops those items out of the readiness figures silently.
    assert set(READY_VERDICTS) == {VERDICT_READY, READY_WITH_RESIDUALS}
    # ``MERGED_PR_STATE`` has to be the spelling the writer accepts: a merged
    # state the ledger can never hold drops the PR from the week's count.
    payload = {
        "url": "https://github.com/o/r/pull/301",
        "state": MERGED_PR_STATE,
        "mergedAt": None,
        "reviews": [],
        "additions": 1,
        "deletions": 0,
        "changedFiles": 1,
    }
    assert _valid_pr_outcome(payload)
    assert not _valid_pr_outcome({**payload, "state": MERGED_PR_STATE.lower()})


def test_endpoint_serves_the_report(tmp_path: Path, ledger: Any, inbox_client) -> None:
    """The route is read-only, bounded and GitHub-free; the ledger arm is real
    and the git arm answers from a directory that is not a clone.

    The store is opened inside the factory rather than shared with the fixture:
    a TestClient request runs on another thread, and a SQLite connection
    belongs to the thread that made it.
    """
    from contextlib import contextmanager

    from kew.serve.app import create_app

    @contextmanager
    def factory() -> Any:
        with SqliteRunStore(str(tmp_path / "outcomes.db")) as store:
            yield store

    client = inbox_client(
        create_app(store_factory=factory, repo_dir=tmp_path), loopback_peer=True
    )
    response = client.get("/api/report/outcomes?weeks=2")
    assert response.status_code == 200
    body = response.json()
    assert len(body["weeks"]) == 2
    assert body["weeks"][0]["week_start"] < body["weeks"][1]["week_start"]
    assert body["merged_main_unavailable"]
    assert json.dumps(body)  # serializable, no secrets, no objects
    assert client.get("/api/report/outcomes?weeks=0").status_code == 422
    assert client.get("/api/report/outcomes?weeks=53").status_code == 422


def test_endpoint_reads_the_denominator_at_the_configured_base_branch(
    tmp_path: Path, inbox_client
) -> None:
    """The route hands the serve process's config to the report (#2387).

    Reading the denominator at ``branches.base`` instead of ``main`` always is
    only true of a running serve process if this argument is on the call, and
    nothing else crosses that wire. The clone below has no ``main`` at all, so
    the unconfigured arm asserted beside it is what makes the configured one
    mean something: one reads its week, the other says why it cannot.
    """
    from contextlib import contextmanager

    from kew import merged_main as module
    from kew.serve.app import create_app

    @contextmanager
    def factory() -> Any:
        with SqliteRunStore(str(tmp_path / "wired.db")) as store:
            yield store

    repo = tmp_path / "clone"
    # Dated now: the endpoint's window ends at the real clock, and a commit
    # outside it is a zero no config can repair.
    _master_only_clone(repo, at=datetime.now(UTC))

    def read(config: DispatchConfig | None) -> Any:
        module.reset_cache()
        client = inbox_client(
            create_app(store_factory=factory, repo_dir=repo, config=config),
            loopback_peer=True,
        )
        try:
            response = client.get("/api/report/outcomes?weeks=2")
        finally:
            module.reset_cache()
        assert response.status_code == 200
        return response.json()

    wired = read(DispatchConfig(branches=BranchConfig(base="master")))
    assert wired["merged_main_unavailable"] is None
    assert sum(week["merged_main"] for week in wired["weeks"]) == 1
    assert read(None)["merged_main_unavailable"] == (
        "origin/main could not be read from the local clone"
    )
