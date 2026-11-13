"""Every report reads a pull request's state by one rule.

Several run rows can name one PR (the dispatch that wrote it, a failed attempt
on the same branch, review runs, a row held for publication), and the outcome
sync refreshes only some of them. A PR is MERGED when any of its rows says
MERGED or carries a merge time; otherwise it reads as the newest non-empty
state among its rows. The `kew report` headline, the evidence pack's headline,
its per-epic ledger and the per-model scorecard must all count the same PRs
as merged.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kew.config import DispatchConfig
from kew.outcomes import SyncResult
from kew.pr_states import pr_states_by_url
from kew.report import build_report, compute_model_scorecard
from kew.report_export import EvidencePack, build_evidence_pack
from kew.run_store import RunRecord, SqliteRunStore

_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
_DAY = _NOW - timedelta(days=1)
_PR = "https://github.com/acme/widgets/pull/{}"


@pytest.fixture(autouse=True)
def _report_project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the report's ledger registry inside the test's own directory."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    monkeypatch.setattr("kew.report.project_root", lambda: tmp_path)


def _run(
    run_id: str,
    pr: int,
    *,
    hour: int,
    pr_state: str | None,
    status: str = "success",
    run_kind: str = "dispatch",
    model: str = "model-ok",
    pr_merged_at: str | None = None,
    started_at: datetime | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=pr,
        item_id=str(pr),
        branch=f"agent/issue-{pr}",
        status=status,  # type: ignore[arg-type]
        exit_code=0 if status == "success" else 1,
        started_at=started_at or _DAY.replace(hour=hour),
        estimated_cost_usd=1.0,
        model=model,
        complexity="medium",
        run_kind=run_kind,
        pr_url=_PR.format(pr),
        pr_state=pr_state,
        pr_merged_at=pr_merged_at,
    )


def _failed_attempt_then_merged() -> list[RunRecord]:
    """A 2-second failed dispatch still says OPEN; the retry merged the PR."""
    return [
        _run("a1", 1, hour=8, pr_state="OPEN", status="failure", model="model-fail"),
        _run("a2", 1, hour=9, pr_state="MERGED"),
    ]


def _merged_then_stale_open_rows() -> list[RunRecord]:
    """The writing run says MERGED; later rows the sync skipped still say OPEN."""
    return [
        _run("b1", 2, hour=8, pr_state="MERGED"),
        _run("b2", 2, hour=9, pr_state="OPEN", status="awaiting_publication"),
        _run("b3", 2, hour=10, pr_state="OPEN", run_kind="review", model="model-rv"),
    ]


def _closed_reopened_merged() -> list[RunRecord]:
    """The PR was closed (CLOSED is final for the sync), reopened and merged."""
    return [
        _run("c1", 3, hour=8, pr_state="CLOSED"),
        _run("c2", 3, hour=9, pr_state="MERGED"),
    ]


def _closed_everywhere() -> list[RunRecord]:
    return [
        _run("d1", 4, hour=8, pr_state="CLOSED"),
        _run("d2", 4, hour=9, pr_state="CLOSED", run_kind="review", model="model-rv"),
    ]


def _newest_says_open() -> list[RunRecord]:
    return [
        _run("e1", 5, hour=8, pr_state="CLOSED"),
        _run("e2", 5, hour=9, pr_state="OPEN"),
    ]


def _every_case() -> list[RunRecord]:
    return [
        *_failed_attempt_then_merged(),
        *_merged_then_stale_open_rows(),
        *_closed_reopened_merged(),
        *_closed_everywhere(),
        *_newest_says_open(),
    ]


def _config(tmp_path: Path) -> DispatchConfig:
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    config.project.repo = "acme/widgets"
    return config


def _seed(config: DispatchConfig, records: Sequence[RunRecord]) -> None:
    db_dir = Path(config.logging.dir)
    db_dir.mkdir(parents=True, exist_ok=True)
    with SqliteRunStore(str(db_dir / "dispatch.db")) as store:
        for record in records:
            store.save(record)


def _report_merged(config: DispatchConfig) -> int:
    with patch("kew.report.sync_outcomes", return_value=SyncResult()):
        content = build_report(config=config, days=7, now=_NOW)
    match = re.search(r"- \*\*PRs merged:\*\* (\d+)", content)
    assert match, content
    return int(match.group(1))


def _pack(config: DispatchConfig, *, since: datetime | None = None) -> EvidencePack:
    with patch("kew.report_export.sync_outcomes", return_value=SyncResult()):
        return build_evidence_pack(
            config=config, since=since or _NOW - timedelta(days=7), now=_NOW
        )


def _scorecard_merged(pack: EvidencePack) -> int:
    return sum(row.merged for row in pack.scorecard.rows)


def _ledger_states(pack: EvidencePack) -> dict[str | None, str | None]:
    return {row.pr_url: row.pr_state for led in pack.ledgers for row in led.rows}


_MERGED_CASES = [
    pytest.param(_failed_attempt_then_merged, id="failed-attempt-then-merged"),
    pytest.param(_merged_then_stale_open_rows, id="merged-then-stale-open"),
    pytest.param(_closed_reopened_merged, id="closed-reopened-merged"),
]
_NOT_MERGED_CASES = [
    pytest.param(_closed_everywhere, id="closed-everywhere"),
    pytest.param(_newest_says_open, id="newest-says-open"),
]


@pytest.mark.parametrize("case", _MERGED_CASES)
def test_a_pr_any_row_saw_merged_counts_as_merged_everywhere(tmp_path, case) -> None:
    config = _config(tmp_path)
    _seed(config, case())

    pack = _pack(config)

    assert _report_merged(config) == 1
    assert pack.prs_merged == 1
    assert _scorecard_merged(pack) == 1
    assert sum(r.merged for r in compute_model_scorecard(case()).rows) == 1
    assert list(_ledger_states(pack).values()) == ["MERGED"]


@pytest.mark.parametrize("case", _NOT_MERGED_CASES)
def test_a_pr_no_row_saw_merged_is_not_counted_merged(tmp_path, case) -> None:
    config = _config(tmp_path)
    _seed(config, case())

    pack = _pack(config)

    assert _report_merged(config) == 0
    assert (pack.prs_opened, pack.prs_merged) == (1, 0)
    assert _scorecard_merged(pack) == 0


def test_newest_state_names_a_pr_no_row_saw_merged(tmp_path) -> None:
    config = _config(tmp_path)
    _seed(config, [*_closed_everywhere(), *_newest_says_open()])

    states = _ledger_states(_pack(config))

    assert states == {_PR.format(4): "CLOSED", _PR.format(5): "OPEN"}


def test_export_headline_equals_the_scorecard_merged_column(tmp_path) -> None:
    config = _config(tmp_path)
    _seed(config, _every_case())

    pack = _pack(config)

    assert (pack.prs_opened, pack.prs_merged) == (5, 3)
    assert _scorecard_merged(pack) == pack.prs_merged
    assert _report_merged(config) == pack.prs_merged


def test_per_epic_ledger_agrees_with_the_export_headline(tmp_path) -> None:
    config = _config(tmp_path)
    _seed(config, _every_case())

    pack = _pack(config)

    merged_rows = [s for s in _ledger_states(pack).values() if s == "MERGED"]
    assert len(merged_rows) == pack.prs_merged == 3


def test_scorecard_covers_the_export_window_not_whole_days(tmp_path) -> None:
    """A run after the export's ``since`` but before ``now`` minus whole days
    sits in the headline, so it must sit in the scorecard too."""
    config = _config(tmp_path)
    since = _NOW - timedelta(days=7, hours=6)
    _seed(
        config,
        [
            _run(
                "w1",
                7,
                hour=0,
                pr_state="MERGED",
                started_at=since + timedelta(hours=1),
            )
        ],
    )

    pack = _pack(config, since=since)

    assert pack.prs_merged == 1
    assert _scorecard_merged(pack) == 1


def test_a_failed_attempt_does_not_take_the_credit_from_the_run_that_succeeded(
    tmp_path,
) -> None:
    config = _config(tmp_path)
    _seed(config, _failed_attempt_then_merged())

    rows = {r.model: r for r in _pack(config).scorecard.rows}

    assert (rows["model-ok"].opened, rows["model-ok"].merged) == (1, 1)
    assert (rows["model-fail"].opened, rows["model-fail"].merged) == (0, 0)
    # Runs and spend stay with the run that incurred them.
    assert (rows["model-fail"].runs, rows["model-fail"].spend) == (1, 1.0)


def test_a_pr_whose_attempts_all_failed_is_still_credited_to_the_first() -> None:
    def failed(model: str, hour: int) -> SimpleNamespace:
        return SimpleNamespace(
            model=model,
            complexity="medium",
            estimated_cost_usd=1.0,
            status="failure",
            run_kind="dispatch",
            pr_url=_PR.format(8),
            pr_state="MERGED",
            review_cycles=None,
            started_at=_DAY.replace(hour=hour),
        )

    rows = {
        r.model: r
        for r in compute_model_scorecard([failed("first", 8), failed("second", 9)]).rows
    }

    assert (rows["first"].opened, rows["first"].merged) == (1, 1)
    assert rows["second"].opened == 0


def test_a_merge_time_or_any_spelling_of_merged_reads_as_merged() -> None:
    def rec(pr: int, hour: int, state: str | None, merged_at: str | None = None):
        return SimpleNamespace(
            pr_url=_PR.format(pr),
            pr_state=state,
            pr_merged_at=merged_at,
            started_at=_DAY.replace(hour=hour),
        )

    states = pr_states_by_url(
        [
            rec(1, 8, "merged"),
            rec(1, 9, "OPEN"),
            rec(2, 8, "OPEN", merged_at="2026-07-19T09:00:00Z"),
            rec(2, 9, "CLOSED"),
            rec(3, 8, None),
        ]
    )

    assert states == {_PR.format(1): "MERGED", _PR.format(2): "MERGED"}
