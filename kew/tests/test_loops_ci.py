"""Unit tests for the CI poll loop (kew.loops.ci). No real network, no real sleep."""

import types

from kew import subprocess_utils
from kew.ci_checks import CommitStatus
from kew.loops.ci import CheckRun, CiResult, _aggregate, _gh_check_runs, wait_for_ci


class FakeClock:
    """Virtual monotonic clock; sleep advances it instead of waiting."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, secs: float) -> None:
        self.t += secs


def _seq_fetch(pages: list):
    """Return a _fetch returning pages[i] on call i; repeats the last page after."""
    calls = {"i": 0}

    def _fetch(repo: str, sha: str):
        i = calls["i"]
        calls["i"] = i + 1
        page = pages[i] if i < len(pages) else pages[-1]
        if isinstance(page, Exception):
            raise page
        return page

    return _fetch


def _run(fetch, *, timeout_secs=300, grace_secs=90, poll=15) -> CiResult:
    clock = FakeClock()
    return wait_for_ci(
        "owner/repo",
        "deadbeef",
        timeout_secs=timeout_secs,
        gated_checks=["test", "agent-image-scan"],
        poll_interval_secs=poll,
        grace_secs=grace_secs,
        _fetch=fetch,
        # No legacy statuses either: never a live gh read from a test.
        _fetch_status=lambda repo, sha: CommitStatus("pending", 0),
        _now=clock.now,
        _sleep=clock.sleep,
    )


def test_all_green():
    res = _run(_seq_fetch([[CheckRun("test", "completed", "success")]]))
    assert res.conclusion == "success"
    assert "1 check" in res.evidence


def test_neutral_beside_a_success_is_green():
    runs = [
        CheckRun("a", "completed", "neutral"),
        CheckRun("b", "completed", "success"),
    ]
    assert _run(_seq_fetch([runs])).conclusion == "success"


# ---------------------------------------------------------------------------
# #2033: a skipped check is never green. Private-repository CI skips the
# self-hosted `test` job for an agent PR until a human adds `ci-approved`, so
# a head whose tests never ran must wait, not pass (and not fail).
# ---------------------------------------------------------------------------


def test_skipped_test_job_is_awaiting_approval_not_green():
    runs = [
        CheckRun("lint", "completed", "success", id=1),
        CheckRun("test", "completed", "skipped", id=2),
        CheckRun("agent-image-scan", "completed", "skipped", id=3),
    ]
    res = _run(_seq_fetch([runs]))
    assert res.conclusion == "awaiting_approval"
    assert "awaiting ci-approved: tests did not run" in res.evidence
    assert "test" in res.evidence


def test_all_skipped_is_awaiting_approval():
    runs = [CheckRun("test", "completed", "skipped")]
    assert _run(_seq_fetch([runs])).conclusion == "awaiting_approval"


def test_no_check_run_succeeded_is_not_green():
    runs = [CheckRun("a", "completed", "neutral")]
    res = _run(_seq_fetch([runs]))
    assert res.conclusion == "no_checks"
    assert "no check run concluded success" in res.evidence


def test_no_check_runs_at_all_is_not_green():
    assert _run(_seq_fetch([[]]), grace_secs=0).conclusion == "no_checks"


def test_approved_head_whose_tests_passed_is_green():
    # `opened` skipped the job; the human's `ci-approved` label ran it.
    runs = [
        CheckRun("test", "completed", "skipped", id=10),
        CheckRun("lint", "completed", "success", id=11),
        CheckRun("test", "completed", "success", id=20),
    ]
    assert _run(_seq_fetch([runs])).conclusion == "success"


def test_a_later_unrelated_label_event_keeps_a_tested_head_green():
    # A `labeled` event for another label (say pr-pending-review) runs a new
    # workflow on the SAME head whose jobs are all skipped. The check runs API
    # returns both; the head's tests still ran and passed.
    runs = [
        CheckRun("lint", "completed", "success", id=1),
        CheckRun("test", "completed", "success", id=2),
        CheckRun("lint", "completed", "skipped", id=7),
        CheckRun("test", "completed", "skipped", id=8),
    ]
    assert _run(_seq_fetch([runs])).conclusion == "success"


def test_a_skipped_leg_beside_a_passing_leg_is_awaiting():
    runs = [
        CheckRun("test (3.13)", "completed", "success"),
        CheckRun("test (3.12)", "completed", "skipped"),
    ]
    res = _run(_seq_fetch([runs]))
    assert res.conclusion == "awaiting_approval"
    assert "test (3.12)" in res.evidence


def test_a_skipped_rerun_does_not_hide_a_failure():
    runs = [
        CheckRun("test", "completed", "failure", id=1),
        CheckRun("test", "completed", "skipped", id=2),
    ]
    assert _run(_seq_fetch([runs])).conclusion == "failure"


def test_gh_check_runs_asks_for_every_run_with_ids(monkeypatch):
    seen = {}

    def _fake_run(argv, *, capture, check):
        seen["argv"] = argv
        out = (
            '{"id": 9, "name": "test", "status": "completed", '
            '"conclusion": "success"}\n'
        )
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess_utils, "run", _fake_run)
    runs = _gh_check_runs("owner/repo", "deadbeef")
    assert runs == [CheckRun("test", "completed", "success", id=9)]
    # `filter=latest` (the default) would hide an earlier success behind a
    # later skipped run of the same name.
    assert "repos/owner/repo/commits/deadbeef/check-runs?filter=all" in seen["argv"]
    assert "id" in seen["argv"][-1]


def test_one_failure_names_it():
    runs = [
        CheckRun("ok", "completed", "success"),
        CheckRun("lint", "completed", "failure"),
    ]
    res = _run(_seq_fetch([runs]))
    assert res.conclusion == "failure"
    assert "lint" in res.evidence


def test_unknown_conclusion_fails_closed():
    runs = [CheckRun("weird", "completed", "brand_new_value")]
    assert _run(_seq_fetch([runs])).conclusion == "failure"


def test_none_conclusion_on_completed_fails_closed():
    runs = [CheckRun("weird", "completed", None)]
    assert _run(_seq_fetch([runs])).conclusion == "failure"


def test_pending_then_green():
    pages = [
        [CheckRun("test", "in_progress", None)],
        [CheckRun("test", "completed", "success")],
    ]
    assert _run(_seq_fetch(pages)).conclusion == "success"


def test_pending_forever_times_out():
    fetch = _seq_fetch([[CheckRun("test", "queued", None)]])
    res = _run(fetch, timeout_secs=30, poll=15)
    assert res.conclusion == "timeout"
    assert "30s" in res.evidence


def test_zero_checks_after_grace_is_no_checks():
    res = _run(_seq_fetch([[]]), grace_secs=0)
    assert res.conclusion == "no_checks"


def test_zero_then_checks_appear_then_green():
    pages = [
        [],
        [CheckRun("test", "in_progress", None)],
        [CheckRun("test", "completed", "success")],
    ]
    res = _run(_seq_fetch(pages), grace_secs=1000, timeout_secs=1000)
    assert res.conclusion == "success"


def test_fetch_error_is_transient_then_times_out():
    res = _run(_seq_fetch([RuntimeError("gh boom")]), timeout_secs=30)
    assert res.conclusion == "timeout"


def test_aggregate_incomplete_is_pending():
    # _aggregate never receives an empty list in wait_for_ci (callers handle the
    # empty case); an incomplete check is the "keep polling" signal.
    state, _ = _aggregate(
        [CheckRun("a", "in_progress", None)], ["test", "agent-image-scan"]
    )
    assert state == "pending"


# ---------------------------------------------------------------------------
# _gh_check_runs — parse/error path (real fetch path, monkeypatched subprocess)
# ---------------------------------------------------------------------------

NDJSON_MULTI_PAGE = (
    '{"name": "lint", "status": "completed", "conclusion": "success"}\n'
    '{"name": "test", "status": "in_progress", "conclusion": null}\n'
    "\n"  # intentional blank line between pages — parser must skip it
    '{"name": "build", "status": "completed", "conclusion": "failure"}\n'
)


def test_gh_check_runs_parse_multi_page(monkeypatch):
    """Success path: multi-line NDJSON with blank page-separator is parsed correctly."""

    def _fake_run(argv, *, capture, check):
        return types.SimpleNamespace(returncode=0, stdout=NDJSON_MULTI_PAGE, stderr="")

    monkeypatch.setattr(subprocess_utils, "run", _fake_run)

    runs = _gh_check_runs("owner/repo", "deadbeef")

    assert len(runs) == 3, f"expected 3 CheckRuns (blank line skipped), got {len(runs)}"
    assert runs[0] == CheckRun("lint", "completed", "success")
    assert runs[1] == CheckRun("test", "in_progress", None)  # null -> Python None
    assert runs[2] == CheckRun("build", "completed", "failure")


def test_gh_check_runs_error_raises_runtime_error(monkeypatch):
    """Error path: non-zero returncode raises RuntimeError with rc and stderr."""

    def _fake_run(argv, *, capture, check):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="gh: rate limited")

    monkeypatch.setattr(subprocess_utils, "run", _fake_run)

    try:
        _gh_check_runs("owner/repo", "deadbeef")
    except RuntimeError as exc:
        assert "1" in str(exc), f"expected rc in error message, got: {exc}"
        assert "rate limited" in str(exc), (
            f"expected stderr in error message, got: {exc}"
        )
    else:
        raise AssertionError("expected RuntimeError was not raised")


# --- Infra-dead classification (F5) ---------------------------------------

from kew.loops.ci import (  # noqa: E402
    JobRun,
    _executed_step_count,
    _gh_run_jobs,
    classify_ci_failure,
)


def test_executed_step_count_only_ran_steps():
    steps = [
        {"name": "checkout", "conclusion": "success"},
        {"name": "test", "conclusion": "failure"},
        {"name": "deploy", "conclusion": "skipped"},
        {"name": "never", "conclusion": None},
        "not-a-dict",
    ]
    assert _executed_step_count(steps) == 2


def test_executed_step_count_non_list():
    assert _executed_step_count(None) == 0
    assert _executed_step_count("nope") == 0


def test_classify_code_failure_when_a_step_ran():
    jobs = [
        JobRun("build", "success", executed_steps=5),
        JobRun("test", "failure", executed_steps=3),
    ]
    assert classify_ci_failure(jobs) == "code_failure"


def test_classify_infra_dead_when_no_step_ran():
    # Billing failure / runner offline: jobs report red with zero executed steps.
    jobs = [
        JobRun("build", "failure", executed_steps=0),
        JobRun("test", "failure", executed_steps=0),
    ]
    assert classify_ci_failure(jobs) == "infra_dead"


def test_classify_infra_dead_on_empty_job_list():
    assert classify_ci_failure([]) == "infra_dead"


def test_classify_code_failure_if_any_job_ran_a_step():
    jobs = [
        JobRun("build", "failure", executed_steps=0),
        JobRun("lint", "failure", executed_steps=1),
    ]
    assert classify_ci_failure(jobs) == "code_failure"


def test_gh_run_jobs_parses_steps(monkeypatch):
    ndjson = (
        '{"name": "build", "conclusion": "failure", "steps": '
        '[{"name": "checkout", "conclusion": "success"}, '
        '{"name": "test", "conclusion": "failure"}]}\n'
        '{"name": "dead", "conclusion": "failure", "steps": []}\n'
    )

    def _fake_run(argv, *, capture, check):
        assert "actions/runs/123/jobs" in argv[3]
        return types.SimpleNamespace(returncode=0, stdout=ndjson, stderr="")

    monkeypatch.setattr(subprocess_utils, "run", _fake_run)
    jobs = _gh_run_jobs("owner/repo", "123")
    assert jobs == [
        JobRun("build", "failure", executed_steps=2),
        JobRun("dead", "failure", executed_steps=0),
    ]
    assert classify_ci_failure(jobs) == "code_failure"


def test_gh_run_jobs_error_raises(monkeypatch):
    def _fake_run(argv, *, capture, check):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="gh: boom")

    monkeypatch.setattr(subprocess_utils, "run", _fake_run)
    try:
        _gh_run_jobs("owner/repo", "123")
    except RuntimeError as exc:
        assert "boom" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


# ---------------------------------------------------------------------------
# #2033: a CI that posts only legacy commit statuses decides like the driver.
# ---------------------------------------------------------------------------


def _statuses_run(state: str, total: int, **kwargs) -> CiResult:
    from kew.ci_checks import CommitStatus

    clock = FakeClock()
    return wait_for_ci(
        "owner/repo",
        "deadbeef",
        timeout_secs=kwargs.get("timeout_secs", 300),
        gated_checks=["test", "agent-image-scan"],
        poll_interval_secs=15,
        grace_secs=kwargs.get("grace_secs", 0),
        _fetch=_seq_fetch([[]]),
        _fetch_status=lambda repo, sha: CommitStatus(state, total),
        _now=clock.now,
        _sleep=clock.sleep,
    )


def test_statuses_only_success_is_not_green_after_grace():
    # A commit status never makes a head green on its own (#2033).
    res = _statuses_run("success", 2, grace_secs=0)
    assert res.conclusion == "no_checks"
    assert res.evidence == "commit statuses green but no check runs: merge by hand"


def test_statuses_only_success_keeps_polling_before_grace():
    res = _statuses_run("success", 2, grace_secs=1000, timeout_secs=60)
    assert res.conclusion == "timeout"
    assert "commit statuses green but no check runs: merge by hand" in res.evidence


def test_an_early_success_status_then_green_check_runs_is_green():
    from kew.ci_checks import CommitStatus

    clock = FakeClock()
    res = wait_for_ci(
        "owner/repo",
        "deadbeef",
        timeout_secs=300,
        gated_checks=["test", "agent-image-scan"],
        poll_interval_secs=15,
        grace_secs=90,
        _fetch=_seq_fetch([[], [], [CheckRun("test", "completed", "success")]]),
        _fetch_status=lambda repo, sha: CommitStatus("success", 1),
        _now=clock.now,
        _sleep=clock.sleep,
    )
    assert res.conclusion == "success"
    # Green from the check runs, never from the early commit status.
    assert res.evidence == "CI green (1 check)"


def test_statuses_only_pending_waits_then_times_out():
    res = _statuses_run("pending", 1, timeout_secs=30, grace_secs=0)
    assert res.conclusion == "timeout"


def test_statuses_only_failure_is_red():
    assert _statuses_run("failure", 1).conclusion == "failure"
    assert _statuses_run("error", 1).conclusion == "failure"


def test_no_check_runs_and_no_statuses_is_not_green():
    res = _statuses_run("pending", 0)
    assert res.conclusion == "no_checks"
    assert "no CI evidence on head" in res.evidence


def test_a_push_only_skipped_job_does_not_block_the_loop_gate():
    runs = [
        CheckRun("test", "completed", "success", id=1),
        CheckRun("publish-docs", "completed", "skipped", id=2),
    ]
    assert _run(_seq_fetch([runs])).conclusion == "success"


def _unreadable_then(status_pages: list):
    """A status fetch that raises (unreadable) until its pages run out."""
    from kew.ci_checks import CommitStatus

    calls = {"i": 0}

    def _fetch_status(repo: str, sha: str):
        i = calls["i"]
        calls["i"] = i + 1
        page = status_pages[i] if i < len(status_pages) else status_pages[-1]
        if isinstance(page, Exception):
            raise page
        return CommitStatus(*page)

    return _fetch_status


def _status_poll(status_pages: list, *, timeout_secs: int, grace_secs: int):
    clock = FakeClock()
    return wait_for_ci(
        "owner/repo",
        "deadbeef",
        timeout_secs=timeout_secs,
        gated_checks=["test", "agent-image-scan"],
        poll_interval_secs=15,
        grace_secs=grace_secs,
        _fetch=_seq_fetch([[]]),
        _fetch_status=_unreadable_then(status_pages),
        _now=clock.now,
        _sleep=clock.sleep,
    )


def test_an_unreadable_status_after_grace_keeps_polling():
    res = _status_poll([RuntimeError("HTTP 502")], timeout_secs=120, grace_secs=0)
    # Past the grace period it still polls, until the overall timeout.
    assert res.conclusion == "timeout"
    assert "commit status unreadable" in res.evidence


def test_a_later_green_check_run_ends_an_unreadable_status_wait():
    """Past grace, an unreadable status keeps the poll alive; once the head's
    check runs register and pass, the head is green (a status alone never is)."""
    clock = FakeClock()
    res = wait_for_ci(
        "owner/repo",
        "deadbeef",
        timeout_secs=300,
        gated_checks=["test", "agent-image-scan"],
        poll_interval_secs=15,
        grace_secs=15,
        _fetch=_seq_fetch([[]] * 4 + [[CheckRun("test", "completed", "success")]]),
        _fetch_status=_unreadable_then([RuntimeError("HTTP 502")]),
        _now=clock.now,
        _sleep=clock.sleep,
    )
    assert res.conclusion == "success"
    assert res.evidence == "CI green (1 check)"
