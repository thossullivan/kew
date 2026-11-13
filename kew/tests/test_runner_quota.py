"""#2643 — a provider quota rejection holds the runner's lane, never fails work.

Incident (console-qol drive, 2026-10-09): the agent token's account hit its
seven-day limit. Every Claude run then failed in two seconds with zero tokens,
each was recorded as an ordinary failure, the epic loops kept dispatching into
the wall, and every node touched needed a manual ``kew epic retry``. The cause
was in each transcript: ``rate_limit_event`` with ``status=rejected``.

Three contracts, one per acceptance criterion:

* the stream fold reads that event as ``quota_exhausted`` with its reset time;
* a ``quota_exhausted`` dispatch leaves the node unfailed and holds the lane,
  so the next tick dispatches nothing to it;
* ``kew doctor --json`` reports ``runner-quota: fail`` with the reset time.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from support import driver_fakes as _fx
from support.driver_fakes import (
    FakeFactsReader,
    FakeSink,
    FakeSource,
    _advance,
    _facts,
    _node,
)
from typer.testing import CliRunner

import kew.dispatch_core as dispatch_core
from kew.cli import app
from kew.config import DispatchConfig
from kew.epic_holds import safe_hold_reason
from kew.orchestrator import _finalize_agent_result
from kew.run_store import RunRecord, SqliteRunStore
from kew.runner_quota import (
    KNOWN_LIMIT_TYPES,
    MAX_HOLD,
    QUOTA_EXHAUSTED_CATEGORY,
    QUOTA_WARNING_EVENT_TYPE,
    RUNNER_QUOTA_HELD_PREFIX,
    QuotaRejection,
    QuotaWarning,
    RunnerQuotaHold,
    active_runner_quota_hold,
    clear_runner_quota_hold,
    credential_fingerprint,
    read_runner_quota_hold,
    record_runner_quota_hold,
    warning_from_rate_limit_event,
)
from kew.runners.claude_code import ClaudeCodeRunner
from kew.runners.registry import (
    KNOWN_RUNNER_NAMES,
    runner_credential_fingerprint,
    runner_credential_names,
    runner_required_credentials,
)
from kew.tracking import DispatchResult
from kew.transcript_metrics import DerivedRunEvidence, StreamDerivation

store = _fx.store
_no_real_git = _fx._no_real_git
_isolate_tool_paths = _fx._isolate_tool_paths

#: Wed Oct 14 02:00 CDT — the reset the incident's rejection carried.
RESET = datetime(2026, 10, 14, 7, 0, tzinfo=UTC)


def _rate_limit_line(status: str = "rejected") -> str:
    """The incident's notice, in claude-code's own ``rate_limit_event`` shape."""
    return json.dumps(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": status,
                "resetsAt": int(RESET.timestamp()),
                "rateLimitType": "seven_day",
                "utilization": 1,
                "overageStatus": "rejected",
                "overageDisabledReason": "out_of_credits",
            },
            "uuid": "00000000-0000-4000-8000-000000000001",
            "session_id": "s-1",
        }
    )


def _result_line(text: str = "Claude AI usage limit reached") -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": text,
            "session_id": "s-1",
            "total_cost_usd": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    )


def _init_line() -> str:
    return json.dumps(
        {"type": "system", "subtype": "init", "model": "claude-x", "session_id": "s-1"}
    )


def _run_transcript(lines: list[str], *, exit_code: int = 1) -> DispatchResult:
    """Fold *lines* the way the host dispatch path does, then classify."""
    runner = ClaudeCodeRunner()
    derivation = StreamDerivation()
    last_line: str | None = None
    for line in lines:
        derivation.observe(line)
        events = runner.parse_line(line)
        for event in events:
            derivation.record_message(event.event_type, event.message, event.metadata)
        notices = {"quota_exhausted", QUOTA_WARNING_EVENT_TYPE}
        if any(event.event_type not in notices for event in events):
            last_line = line
    result = DispatchResult(exit_code=exit_code, issue_num=7, log_file=Path("x.log"))
    _finalize_agent_result(
        result,
        runner=runner,
        config=DispatchConfig(),
        last_output=last_line,
        inference_transport=None,
        health_check=None,
        evidence=None,
        classifier_records=(),
        timeout_override_minutes=None,
        quota_rejection=derivation.quota_exhausted,
        quota_warning=derivation.quota_warning,
    )
    return result


# --- AC1: the transcript reads as quota_exhausted, with the reset ------------


def test_rejected_rate_limit_transcript_is_quota_exhausted_with_reset() -> None:
    result = _run_transcript([_init_line(), _rate_limit_line(), _result_line()])

    assert result.error_category == QUOTA_EXHAUSTED_CATEGORY
    assert result.quota_rejection == QuotaRejection(
        runner="claude-code", limit_type="seven_day", resets_at=RESET
    )


def test_the_same_transcript_without_the_rejection_is_an_ordinary_failure() -> None:
    """The control: the rejection is what moves the category, nothing else."""
    result = _run_transcript([_init_line(), _result_line()])

    assert result.error_category != QUOTA_EXHAUSTED_CATEGORY
    assert result.quota_rejection is None


def test_a_rejection_after_the_result_still_classifies() -> None:
    """claude-code can forward the notice after the result event."""
    result = _run_transcript([_init_line(), _result_line(), _rate_limit_line()])

    assert result.error_category == QUOTA_EXHAUSTED_CATEGORY


@pytest.mark.parametrize("status", ["allowed", "allowed_warning"])
def test_a_window_filling_up_is_not_quota_exhausted(status: str) -> None:
    result = _run_transcript([_init_line(), _rate_limit_line(status), _result_line()])

    assert result.error_category != QUOTA_EXHAUSTED_CATEGORY


def test_an_allowed_warning_notice_is_read_as_a_usage_warning() -> None:
    """#3329: the window filling up is a warning, read off the same stream."""
    line = _rate_limit_line("allowed_warning")
    expected = QuotaWarning(
        runner="claude-code", limit_type="seven_day", resets_at=RESET, utilization=1
    )

    events = ClaudeCodeRunner().parse_line(line)

    assert [event.event_type for event in events] == [QUOTA_WARNING_EVENT_TYPE]
    assert events[0].metadata["quota_warning"] == expected
    assert warning_from_rate_limit_event(json.loads(line)) == expected.with_runner("")
    result = _run_transcript([_init_line(), line, _result_line()])
    assert result.quota_warning == expected
    assert result.error_category != QUOTA_EXHAUSTED_CATEGORY


@pytest.mark.parametrize("status", ["allowed", "rejected"])
def test_allowed_and_rejected_notices_are_not_warnings(status: str) -> None:
    line = _rate_limit_line(status)

    events = ClaudeCodeRunner().parse_line(line)

    assert warning_from_rate_limit_event(json.loads(line)) is None
    assert QUOTA_WARNING_EVENT_TYPE not in [event.event_type for event in events]
    assert _run_transcript([_init_line(), line, _result_line()]).quota_warning is None


def test_a_run_that_was_served_anyway_keeps_its_success() -> None:
    result = _run_transcript([_init_line(), _rate_limit_line()], exit_code=0)

    assert result.error_category == ""
    assert result.quota_rejection is None


def test_the_cli_out_of_credits_error_is_quota_exhausted_without_a_reset() -> None:
    line = _result_line("Credit balance is too low")
    result = _run_transcript([_init_line(), line])

    assert result.error_category == QUOTA_EXHAUSTED_CATEGORY
    assert result.quota_rejection is not None
    assert result.quota_rejection.resets_at is None


def test_the_contained_path_reads_the_rejection_back_from_the_sidecar() -> None:
    """The sandbox path reads derived evidence, not the transcript (I06T)."""
    rejection = QuotaRejection("claude-code", "seven_day", RESET)
    evidence = DerivedRunEvidence(quota_exhausted=rejection)

    replayed = DerivedRunEvidence.from_json(evidence.as_json())

    assert replayed is not None
    assert replayed.quota_exhausted == rejection


def test_sandbox_refusal_finalizes_quota_exhausted_as_refused(tmp_path: Path) -> None:
    from kew.dispatch_sandbox import _sandbox_refuses_publication
    from kew.transcript_metrics import write_derived_evidence

    log_file = tmp_path / "run.log"
    rejection = QuotaRejection("claude-code", "seven_day", RESET)
    write_derived_evidence(log_file, DerivedRunEvidence(quota_exhausted=rejection))
    result = DispatchResult(exit_code=1, issue_num=7, log_file=log_file)
    finalize = MagicMock()

    assert _sandbox_refuses_publication(1, result, log_file, finalize) is True

    finalize.assert_called_once_with(1, QUOTA_EXHAUSTED_CATEGORY, status="refused")
    assert result.quota_rejection == rejection


# --- AC2: the node stays unfailed; the lane is held; the tick withholds ------


def _quota_ctx(store_: SqliteRunStore) -> Any:
    ctx = MagicMock(spec=dispatch_core._DispatchContext)
    ctx.checkout_generation = ""
    ctx.config = DispatchConfig()
    ctx.epic_ref = None
    ctx.provider = "github"
    ctx.issue_num = 1
    ctx.item_id = "1"
    ctx.run_id = "run-1"
    ctx.store = store_
    ctx.terminal_failure = MagicMock()
    return ctx


def _quota_result(resets_at: datetime) -> DispatchResult:
    return DispatchResult(
        exit_code=1,
        issue_num=1,
        log_file=Path("x.log"),
        run_id="run-1",
        error_category=QUOTA_EXHAUSTED_CATEGORY,
        quota_rejection=QuotaRejection("claude-code", "seven_day", resets_at),
    )


def test_quota_exhausted_dispatch_leaves_node_unfailed_and_holds_the_lane(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    resets_at = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
    reset_to_ready = MagicMock()
    published_failure = MagicMock()
    monkeypatch.setattr(dispatch_core, "reset_issue_to_ready", reset_to_ready)
    monkeypatch.setattr(dispatch_core, "_publish_terminal_failure", published_failure)
    monkeypatch.setattr(
        dispatch_core, "_root_checkout_integrity_check", lambda *_a, **_k: None
    )
    ctx = _quota_ctx(store)
    result = _quota_result(resets_at)

    outcome = dispatch_core._collect_result(
        ctx, result=result, worktree_dir=tmp_path / "wt", run_sandboxed_path=False
    )

    # Not the node's failure: back to ready, no failure label or comment.
    assert outcome.exit_code != 0
    reset_to_ready.assert_called_once_with(1, ctx.config)
    published_failure.assert_not_called()
    ctx.terminal_failure.assert_not_called()
    # The row the child writes is one the overlay never latches as FAILED.
    row = RunRecord(
        run_id="run-1",
        issue_num=1,
        branch="agent/issue-1",
        status="pending",
        exit_code=0,
        started_at=datetime.now(UTC) - timedelta(seconds=3),
    )
    dispatch_core._apply_completion_status(row, result, 1)
    assert row.status == "refused"
    # The lane is held, with the reason and the reset time.
    hold = active_runner_quota_hold(store, "claude-code")
    assert hold is not None
    assert hold.until == resets_at
    assert hold.reason().startswith(RUNNER_QUOTA_HELD_PREFIX)
    assert "seven_day" in hold.reason()

    # The next tick dispatches nothing to the held lane, and says why.
    row.error_category = QUOTA_EXHAUSTED_CATEGORY
    row.finished_at = datetime.now(UTC)
    store.save(row)
    deps = FakeSource(nodes=[_node("1")], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")})
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    held = _advance(deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path)

    assert held.dispatched == ()
    assert sink.calls == []
    assert "1" not in held.failed
    reasons = {w.node_id: w.reason for w in held.waiting}
    assert reasons["1"].startswith(RUNNER_QUOTA_HELD_PREFIX)
    assert resets_at.isoformat().replace("+00:00", "Z") in reasons["1"]

    # Once the reset passes, the same node dispatches with no retry needed.
    record_runner_quota_hold(
        store,
        QuotaRejection("claude-code", "seven_day", datetime.now(UTC) - timedelta(1)),
    )
    resumed = _advance(
        deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
    )

    assert resumed.dispatched == ("1",)
    assert sink.calls == ["1"]


def test_a_quota_exhausted_attempt_is_not_retried() -> None:
    run = MagicMock(run_kind="dispatch")
    result = _quota_result(RESET)
    result.paused = False

    with patch.object(dispatch_core, "_check_operator_kill", return_value=False):
        stops = dispatch_core._classify_attempt(result, run, None, None, None)

    assert stops is True


# --- AC3: kew doctor --json reports runner-quota: fail with the reset --------


_cli = CliRunner()


def _doctor_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.chdir(tmp_path)
    logs = tmp_path / "logs"
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n'
        f"[sandbox]\nenabled = false\ndangerously_skip_containment = true\n"
        f'[logging]\ndir = "{logs}"\n'
    )
    with (
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
    ):
        result = _cli.invoke(app, ["doctor", "--json"])
    report = json.loads(result.stdout)
    return {check["id"]: check for check in report["checks"]} | {
        "_exit": result.exit_code
    }


def test_doctor_json_reports_runner_quota_fail_with_the_reset_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resets_at = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
    (tmp_path / "logs").mkdir()
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as ledger:
        record_runner_quota_hold(
            ledger, QuotaRejection("claude-code", "seven_day", resets_at)
        )

    checks = _doctor_json(tmp_path, monkeypatch)

    assert checks["_exit"] == 1
    quota = checks["runner-quota"]
    assert quota["status"] == "fail"
    assert "claude-code seven_day quota exhausted" in quota["summary"]
    assert resets_at.isoformat().replace("+00:00", "Z") in quota["summary"]


def test_doctor_runner_quota_passes_once_the_reset_has_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "logs").mkdir()
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as ledger:
        record_runner_quota_hold(
            ledger,
            QuotaRejection(
                "claude-code", "seven_day", datetime.now(UTC) - timedelta(1)
            ),
        )

    checks = _doctor_json(tmp_path, monkeypatch)

    assert checks["runner-quota"]["status"] == "ok"


# --- Review: a held lane has releases besides the reset ----------------------

_CREDENTIAL_NAMES = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
#: Stand-in credential values. Not real; only their fingerprints may persist.
_TOKEN_A = "fixture-credential-a-0123456789"
_TOKEN_B = "fixture-credential-b-9876543210"


def _use_credential(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    for name in _CREDENTIAL_NAMES:
        monkeypatch.delenv(name, raising=False)
    if value is not None:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", value)


def _fingerprint(value: str) -> str:
    return credential_fingerprint({"CLAUDE_CODE_OAUTH_TOKEN": value}, _CREDENTIAL_NAMES)


def _tick_one_node(tmp_path: Path, store_: SqliteRunStore) -> Any:
    sink = FakeSink()
    return _advance(
        deps=FakeSource(nodes=[_node("1")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")}),
        store=store_,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
    )


def _hold_for_days(store_: SqliteRunStore, days: int, fingerprint: str = "") -> None:
    record_runner_quota_hold(
        store_,
        QuotaRejection(
            "claude-code", "seven_day", datetime.now(UTC) + timedelta(days=days)
        ),
        credential_fingerprint=fingerprint,
    )


def test_a_hold_never_outlasts_eight_days_past_the_refusal() -> None:
    observed = datetime(2026, 10, 9, 18, 30, tzinfo=UTC)
    rejection = QuotaRejection("claude-code", "seven_day", observed + timedelta(30))
    hold = RunnerQuotaHold(rejection, observed)

    assert hold.until == observed + MAX_HOLD
    assert hold.active(observed + timedelta(days=7, hours=23))
    assert not hold.active(observed + timedelta(days=8))
    assert (
        f"held until {(observed + MAX_HOLD).isoformat()}".replace("+00:00", "Z")
        in hold.reason()
    )


def test_a_real_seven_day_reset_is_not_cut_short() -> None:
    observed = datetime(2026, 10, 9, 18, 30, tzinfo=UTC)
    hold = RunnerQuotaHold(QuotaRejection("claude-code", "seven_day", RESET), observed)

    assert hold.until == RESET


def test_quota_clear_releases_the_hold_and_the_next_tick_dispatches(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_credential(monkeypatch, None)
    _hold_for_days(store, 5)
    assert _tick_one_node(tmp_path, store).dispatched == ()

    cleared = clear_runner_quota_hold(store, "claude-code", cleared_by="tom")

    assert cleared is not None
    assert active_runner_quota_hold(store, "claude-code") is None
    stored = read_runner_quota_hold(store, "claude-code")
    assert stored is not None and stored.cleared is not None
    assert stored.cleared.cleared_by == "tom"
    assert _tick_one_node(tmp_path, store).dispatched == ("1",)


def test_quota_clear_with_no_hold_is_a_no_op(store: SqliteRunStore) -> None:
    assert clear_runner_quota_hold(store, "claude-code", cleared_by="tom") is None
    assert read_runner_quota_hold(store, "claude-code") is None


def _quota_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> Any:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[logging]\ndir = "{tmp_path / "logs"}"\n'
    )
    return _cli.invoke(app, ["quota", *args])


def test_kew_quota_clear_prints_what_it_cleared_and_records_who(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "logs").mkdir()
    ledger_path = tmp_path / "logs" / "dispatch.db"
    with SqliteRunStore(str(ledger_path)) as ledger:
        _hold_for_days(ledger, 5)

    result = _quota_cli(tmp_path, monkeypatch, "clear", "claude-code")

    assert result.exit_code == 0, result.output
    assert "claude-code seven_day quota exhausted" in result.output
    with SqliteRunStore(str(ledger_path)) as ledger:
        stored = read_runner_quota_hold(ledger, "claude-code")
    assert stored is not None and stored.cleared is not None
    assert stored.cleared.cleared_by

    again = _quota_cli(tmp_path, monkeypatch, "clear", "claude-code")

    assert again.exit_code == 0
    assert "No quota hold on claude-code" in again.output


def test_kew_quota_clear_refuses_an_unknown_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _quota_cli(tmp_path, monkeypatch, "clear", "no-such-runner")

    assert result.exit_code == 2


def test_doctor_names_quota_clear_as_the_fix_while_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_credential(monkeypatch, None)
    (tmp_path / "logs").mkdir()
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as ledger:
        _hold_for_days(ledger, 3)

    checks = _doctor_json(tmp_path, monkeypatch)

    assert checks["runner-quota"]["fix"] == "kew quota clear claude-code"


def test_a_hold_blocks_under_the_credential_it_was_recorded_with(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_credential(monkeypatch, _TOKEN_A)
    _hold_for_days(store, 5, _fingerprint(_TOKEN_A))

    assert _tick_one_node(tmp_path, store).dispatched == ()


def test_a_hold_does_not_block_under_a_different_credential(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_credential(monkeypatch, _TOKEN_B)
    _hold_for_days(store, 5, _fingerprint(_TOKEN_A))

    assert _tick_one_node(tmp_path, store).dispatched == ("1",)


def test_a_hold_still_blocks_with_no_credential_in_the_environment(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_credential(monkeypatch, None)
    _hold_for_days(store, 5, _fingerprint(_TOKEN_A))

    assert _tick_one_node(tmp_path, store).dispatched == ()


def test_the_stored_hold_and_its_logs_carry_only_the_fingerprint(
    tmp_path: Path,
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The collect path fingerprints the refused credential; never stores it."""
    _use_credential(monkeypatch, _TOKEN_A)
    monkeypatch.setattr(dispatch_core, "reset_issue_to_ready", MagicMock())
    monkeypatch.setattr(
        dispatch_core, "_root_checkout_integrity_check", lambda *_a, **_k: None
    )
    caplog.set_level("DEBUG")

    dispatch_core._collect_result(
        _quota_ctx(store),
        result=_quota_result(datetime.now(UTC) + timedelta(days=3)),
        worktree_dir=tmp_path / "wt",
        run_sandboxed_path=False,
    )
    clear_runner_quota_hold(store, "claude-code", cleared_by="tom")

    raw = store.get_meta("runner_quota_hold:claude-code")
    assert raw is not None
    stored = json.loads(raw)
    assert stored["credential_fingerprint"] == _fingerprint(_TOKEN_A)
    assert len(stored["credential_fingerprint"]) == 12
    assert _TOKEN_A not in raw
    assert _TOKEN_A not in caplog.text


# --- #3330: a hold names the agent token the provider refused ----------------


def _collect_a_refusal(tmp_path: Path, store_: SqliteRunStore, resets_at: Any) -> None:
    """Collect an epic-owned run's quota refusal, the case #3330 is about.

    The driver's reservation holds an epic run's claim, so there is no label
    to hand back and collect makes no forge write; no operator-checkout
    snapshot was taken, so it has no checkout to audit either.
    """
    ctx = _quota_ctx(store_)
    ctx.epic_ref = "3330"
    ctx.root_checkout_before = None
    dispatch_core._collect_result(
        ctx,
        result=_quota_result(resets_at),
        worktree_dir=tmp_path / "wt",
        run_sandboxed_path=False,
    )


def test_a_runner_quota_hold_reason_names_the_refused_token(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_credential(monkeypatch, _TOKEN_A)
    resets_at = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
    _collect_a_refusal(tmp_path, store, resets_at)

    hold = read_runner_quota_hold(store, "claude-code")
    assert hold is not None
    resets = resets_at.isoformat().replace("+00:00", "Z")
    assert hold.reason() == (
        f"{RUNNER_QUOTA_HELD_PREFIX}: claude-code seven_day quota exhausted, "
        f"resets {resets}; agent token CLAUDE_CODE_OAUTH_TOKEN "
        f"(fingerprint {_fingerprint(_TOKEN_A)})"
    )
    # The stored row carries the names and the digest, never the value.
    raw = store.get_meta("runner_quota_hold:claude-code")
    assert raw is not None and _TOKEN_A not in raw
    assert json.loads(raw)["credential_names"] == ["CLAUDE_CODE_OAUTH_TOKEN"]
    # The tick's waiting reason is that same reason, so it still buckets.
    reasons = {w.node_id: w.reason for w in _tick_one_node(tmp_path, store).waiting}
    assert reasons["1"] == hold.reason()


def test_a_hold_with_no_known_credential_says_so(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_credential(monkeypatch, None)
    _collect_a_refusal(tmp_path, store, RESET)

    hold = read_runner_quota_hold(store, "claude-code")
    assert hold is not None
    assert hold.reason() == (
        f"{RUNNER_QUOTA_HELD_PREFIX}: claude-code seven_day quota exhausted, "
        "resets 2026-10-14T07:00:00Z; unknown credential"
    )
    assert "fingerprint" not in hold.reason()


def test_a_cleared_hold_keeps_the_token_it_named(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_credential(monkeypatch, _TOKEN_A)
    record_runner_quota_hold(
        store,
        QuotaRejection("claude-code", "seven_day", datetime.now(UTC) + timedelta(1)),
        credential_fingerprint=_fingerprint(_TOKEN_A),
        credential_names=("CLAUDE_CODE_OAUTH_TOKEN",),
    )

    cleared = clear_runner_quota_hold(store, "claude-code", cleared_by="tom")
    stored = read_runner_quota_hold(store, "claude-code")

    assert cleared is not None and stored is not None
    assert stored.credential_names == ("CLAUDE_CODE_OAUTH_TOKEN",)
    assert stored.token() == cleared.token()


def test_doctor_runner_quota_names_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_credential(monkeypatch, _TOKEN_A)
    (tmp_path / "logs").mkdir()
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as ledger:
        record_runner_quota_hold(
            ledger,
            QuotaRejection(
                "claude-code", "seven_day", datetime.now(UTC) + timedelta(days=3)
            ),
            credential_fingerprint=_fingerprint(_TOKEN_A),
            credential_names=("CLAUDE_CODE_OAUTH_TOKEN",),
        )

    checks = _doctor_json(tmp_path, monkeypatch)

    quota = checks["runner-quota"]
    assert quota["status"] == "fail"
    assert "claude-code seven_day quota exhausted" in quota["summary"]
    assert (
        f"agent token CLAUDE_CODE_OAUTH_TOKEN (fingerprint {_fingerprint(_TOKEN_A)})"
    ) in quota["summary"]
    assert _TOKEN_A not in json.dumps(checks)


#: Every env var a registered runner authenticates with, each set: the longest
#: token name kew can print (claude-code reads two).
_EVERY_CREDENTIAL = {
    "ANTHROPIC_API_KEY": "fixture-anthropic-credential-0123",
    "CLAUDE_CODE_OAUTH_TOKEN": "fixture-oauth-credential-4567",
    "OPENROUTER_API_KEY": "fixture-openrouter-credential-89ab",
}
_OBSERVED = datetime(2026, 10, 9, 18, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "runner",
    [name for name in sorted(KNOWN_RUNNER_NAMES) if runner_required_credentials(name)],
)
@pytest.mark.parametrize(
    "limit",
    # The limit name comes off a stream a contained agent can write, so any
    # name kew keeps (64 safe characters) must fit too.
    [*sorted(KNOWN_LIMIT_TYPES), "seven_day_opus_overage", "x" * 64],
)
@pytest.mark.parametrize(
    "resets_at",
    [_OBSERVED + timedelta(days=3), _OBSERVED + timedelta(days=30), None],
    ids=["reset-in-the-window", "reset-far-out", "reset-unknown"],
)
def test_the_echo_cap_never_cuts_the_token_out_of_a_runner_quota_hold(
    runner: str, limit: str, resets_at: datetime | None
) -> None:
    """Every surface flattens the reason to ``HOLD_REASON_MAX_CHARS``, cutting
    the middle out of a longer one: the token is what the operator acts on, so
    it is never what a surface loses, and every surface prints the same line."""
    names = runner_credential_names(runner, _EVERY_CREDENTIAL)
    fingerprint = runner_credential_fingerprint(runner, _EVERY_CREDENTIAL)
    hold = RunnerQuotaHold(
        QuotaRejection(runner, limit, resets_at),
        _OBSERVED,
        credential_fingerprint=fingerprint,
        credential_names=names,
    )

    capped = safe_hold_reason(hold.reason())

    assert capped == hold.reason()
    assert capped.startswith(f"{RUNNER_QUOTA_HELD_PREFIX}: {runner} ")
    assert f"agent token {'/'.join(names)} (fingerprint {fingerprint})" in capped
    assert _iso(hold.until) in capped
    assert not any(value in capped for value in _EVERY_CREDENTIAL.values())


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")
