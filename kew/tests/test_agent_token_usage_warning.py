"""#3329 — kew warns when its agent token nears a provider usage limit.

Incident (2026-10-28): the agent token hit its provider usage limit with no
warning, and reviews and dispatches stalled until the operator swapped the
token. The stream had said so for hours: Claude Code sends a
``rate_limit_event`` with ``status=allowed_warning`` while the window fills.

The contracts pinned here:

* a dispatch stream and a reviewer stream that carry the warning each leave a
  stored warning for the runner's credential fingerprint;
* a stored warning is one attention event naming the token, however often the
  notice repeated in its window;
* ``kew epic status`` prints the warning line, and stops printing it once the
  token it was recorded for has been replaced.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from support import driver_fakes as _fx
from support.driver_fakes import EPIC, NOW, FakeFactsReader, FakeSource, _facts, _node
from typer.testing import CliRunner

import kew.dispatch_core as dispatch_core
from kew.cli import app
from kew.config import DispatchConfig
from kew.dispatch_sandbox import _stream_with_review_capture
from kew.graph.build import build_graph
from kew.graph.driver.attention import (
    AttentionKind,
    attention_events,
    token_warning_events,
)
from kew.graph.driver.result import TickSummary
from kew.graph.driver.tick import summarize
from kew.orchestrator import _finalize_agent_result
from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND, SqliteRunStore
from kew.runner_quota import (
    QUOTA_WARNING_EVENT_TYPE,
    QuotaWarning,
    credential_fingerprint,
    read_runner_quota_hold,
    read_runner_quota_warning,
    record_runner_quota_warning,
)
from kew.runners.claude_code import ClaudeCodeRunner
from kew.runners.registry import active_runner_token_warnings
from kew.tracking import DispatchResult
from kew.transcript_metrics import StreamDerivation

store = _fx.store
_no_real_git = _fx._no_real_git
_isolate_tool_paths = _fx._isolate_tool_paths

RESET = NOW + timedelta(days=3)
#: A reset three days after the wall clock: a stream folds under the real clock,
#: and a reset already past when the notice is read is read as unknown.
LIVE_RESET = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
_CREDENTIAL_NAMES = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
#: Stand-in credential values. Not real; only their fingerprints may persist.
_TOKEN_A = "fixture-credential-a-0123456789"
_TOKEN_B = "fixture-credential-b-9876543210"


def _use_credential(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    for name in _CREDENTIAL_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", value)


def _fingerprint(value: str) -> str:
    return credential_fingerprint({"CLAUDE_CODE_OAUTH_TOKEN": value}, _CREDENTIAL_NAMES)


def _rate_limit_line(
    status: str, *, resets: object = None, limit: str = "seven_day"
) -> str:
    """A notice in claude-code's own ``rate_limit_event`` shape."""
    return json.dumps(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": status,
                "resetsAt": int(LIVE_RESET.timestamp()) if resets is None else resets,
                "rateLimitType": limit,
                "utilization": 0.92,
                "isUsingOverage": False,
                "surpassedThreshold": 0.75,
            },
            "uuid": "00000000-0000-4000-8000-000000000002",
            "session_id": "s-1",
        }
    )


def _init_line() -> str:
    return json.dumps(
        {"type": "system", "subtype": "init", "model": "claude-x", "session_id": "s-1"}
    )


def _result_line(*, is_error: bool) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": is_error,
            "result": "Claude AI usage limit reached" if is_error else "done",
            "session_id": "s-1",
            "total_cost_usd": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    )


def _host_dispatch(lines: list[str]) -> DispatchResult:
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
    result = DispatchResult(
        exit_code=1, issue_num=1, log_file=Path("x.log"), run_id="run-1"
    )
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


def _dispatch_ctx(store_: SqliteRunStore) -> Any:
    ctx = MagicMock(spec=dispatch_core._DispatchContext)
    ctx.checkout_generation = ""
    ctx.config = DispatchConfig()
    # An epic-owned run: its claim is the driver's reservation, not a label,
    # so the quota hold hands nothing back to the forge.
    ctx.epic_ref = EPIC
    ctx.provider = "github"
    ctx.issue_num = 1
    ctx.item_id = "1"
    ctx.root_checkout_before = None
    ctx.run_id = "run-1"
    ctx.store = store_
    ctx.terminal_failure = MagicMock()
    return ctx


# --- AC2: dispatch and reviewer streams each record the warning ---------------


def test_a_dispatch_stream_with_a_warning_records_it_for_its_token(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window filled, then refused: the warning is kept beside the hold."""
    _use_credential(monkeypatch, _TOKEN_A)
    result = _host_dispatch(
        [
            _init_line(),
            _rate_limit_line("allowed_warning"),
            _rate_limit_line("rejected"),
            _result_line(is_error=True),
        ]
    )

    dispatch_core._collect_result(
        _dispatch_ctx(store),
        result=result,
        worktree_dir=tmp_path / "wt",
        run_sandboxed_path=False,
    )

    stored = read_runner_quota_warning(store, "claude-code", "seven_day")
    assert stored is not None
    assert stored.credential_fingerprint == _fingerprint(_TOKEN_A)
    assert stored.credential_names == ("CLAUDE_CODE_OAUTH_TOKEN",)
    assert stored.run_id == "run-1"
    assert stored.warning.limit_type == "seven_day"
    assert stored.warning.resets_at == LIVE_RESET
    raw = store.get_meta("runner_quota_warning:claude-code:seven_day")
    assert raw is not None and _TOKEN_A not in raw


class _ReviewerStream:
    """A finished reviewer container's stdout: its lines, then its exit."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __iter__(self) -> Any:
        yield from (line + "\n" for line in self._lines)

    def wait(self) -> int:
        return 0


def test_a_reviewer_stream_with_a_warning_records_it_for_its_token(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_credential(monkeypatch, _TOKEN_A)
    lines = [
        _init_line(),
        _rate_limit_line("allowed_warning"),
        _result_line(is_error=False),
    ]

    exit_code = _stream_with_review_capture(
        _ReviewerStream(lines),  # type: ignore[arg-type]
        tmp_path / "review.log",
        store,
        "review-run-1",
        model="",
        overrides=None,
        runner="claude-code",
        run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
    )

    assert exit_code == 0
    stored = read_runner_quota_warning(store, "claude-code", "seven_day")
    assert stored is not None
    assert stored.credential_fingerprint == _fingerprint(_TOKEN_A)
    assert stored.run_id == "review-run-1"
    assert stored.warning.limit_type == "seven_day"


def test_a_stream_with_only_routine_notices_records_no_warning(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_credential(monkeypatch, _TOKEN_A)
    lines = [_init_line(), _rate_limit_line("allowed"), _result_line(is_error=False)]

    _stream_with_review_capture(
        _ReviewerStream(lines),  # type: ignore[arg-type]
        tmp_path / "review.log",
        store,
        "review-run-1",
        model="",
        overrides=None,
        runner="claude-code",
        run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
    )

    assert read_runner_quota_warning(store, "claude-code", "seven_day") is None


# --- AC3 / AC4: one attention event, and the epic status line ------------------


def _record_warning(
    store_: SqliteRunStore,
    token: str,
    *,
    minutes: int = 0,
    utilization: float = 0.92,
) -> None:
    record_runner_quota_warning(
        store_,
        QuotaWarning("claude-code", "seven_day", RESET, utilization=utilization),
        run_id="run-1",
        credential_fingerprint=_fingerprint(token),
        credential_names=("CLAUDE_CODE_OAUTH_TOKEN",),
        now=NOW + timedelta(minutes=minutes),
    )


def _summary(store_: SqliteRunStore) -> TickSummary:
    deps = FakeSource(nodes=[_node("1")], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")})
    return summarize(
        EPIC,
        build_graph(deps, EPIC),
        config=DispatchConfig(),
        store=store_,
        facts=facts,
        now=NOW,
    )


_TOKEN_SUBJECT = (
    f"agent token CLAUDE_CODE_OAUTH_TOKEN (fingerprint {_fingerprint(_TOKEN_A)}) "
    "for claude-code is near its seven_day limit"
)
_RESETS = f"resets {RESET.isoformat().replace('+00:00', 'Z')}"
_WARNING_LINE = f"{_TOKEN_SUBJECT} (92% used), {_RESETS}"
#: The event's reason carries no percentage: it rises notice by notice, and a
#: watcher de-dups on the reason.
_EVENT_REASON = f"{_TOKEN_SUBJECT}, {_RESETS}"


def test_a_recorded_warning_is_one_attention_event_naming_the_token(
    store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_credential(monkeypatch, _TOKEN_A)
    _record_warning(store, _TOKEN_A, utilization=0.76)
    first = attention_events(EPIC, _summary(store))
    # The notice again, later in the same window and further filled: the
    # same state, so a watcher raises no second event.
    _record_warning(store, _TOKEN_A, minutes=30, utilization=0.92)
    second = attention_events(EPIC, _summary(store))

    warnings = [e for e in first if e.kind is AttentionKind.USAGE_LIMIT_WARNING]
    assert len(warnings) == 1
    assert warnings[0].reason == _EVENT_REASON
    assert "CLAUDE_CODE_OAUTH_TOKEN" in warnings[0].reason
    assert _fingerprint(_TOKEN_A) in warnings[0].reason
    assert _TOKEN_A not in warnings[0].as_line()
    assert [e.state_key for e in second if e.kind is warnings[0].kind] == [
        warnings[0].state_key
    ]


def test_a_warning_past_its_reset_is_no_attention_event(
    store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_credential(monkeypatch, _TOKEN_A)
    record_runner_quota_warning(
        store,
        QuotaWarning("claude-code", "five_hour", NOW - timedelta(minutes=1)),
        credential_fingerprint=_fingerprint(_TOKEN_A),
        now=NOW - timedelta(hours=2),
    )

    events = attention_events(EPIC, _summary(store))

    assert all(e.kind is not AttentionKind.USAGE_LIMIT_WARNING for e in events)


_cli = CliRunner()


def _epic_status(tmp_path: Path, summary: TickSummary) -> Any:
    config = DispatchConfig()
    config.project.repo = "o/r"
    config.logging.dir = str(tmp_path / "logs")
    config.branches.worktree_base = str(tmp_path / "wt")
    with (
        patch("kew.cli.project_root", return_value=tmp_path),
        patch("kew.cli._load_epic_graph"),
        patch("kew.cli._load_config", return_value=config),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.summarize", return_value=summary),
    ):
        return _cli.invoke(app, ["epic", "status", EPIC])


def test_epic_status_prints_the_warning_line(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_credential(monkeypatch, _TOKEN_A)
    _record_warning(store, _TOKEN_A)

    result = _epic_status(tmp_path, _summary(store))

    assert result.exit_code == 0, result.output
    assert f"Usage warning: {_WARNING_LINE}" in result.stdout
    assert _TOKEN_A not in result.output


def _contained_run(
    tmp_path: Path, store_: SqliteRunStore, lines: list[str], run_id: str
) -> int:
    """One contained reviewer run whose stdout is *lines*: launch, fold, exit."""
    return _stream_with_review_capture(
        _ReviewerStream(lines),  # type: ignore[arg-type]
        tmp_path / f"{run_id}.log",
        store_,
        run_id,
        model="",
        overrides=None,
        runner="claude-code",
        run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
    )


def _warning_events(store_: SqliteRunStore) -> tuple[Any, ...]:
    return token_warning_events(EPIC, active_runner_token_warnings(store_))


def test_a_launch_under_a_new_token_retires_the_old_tokens_warning(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new token is a new window: once a run LAUNCHES under it, the old
    token's warning no longer applies — whatever the reader's own env holds."""
    _use_credential(monkeypatch, _TOKEN_A)
    _contained_run(
        tmp_path, store, [_init_line(), _rate_limit_line("allowed_warning")], "r-a"
    )
    _use_credential(monkeypatch, _TOKEN_B)
    _contained_run(tmp_path, store, [_init_line(), _result_line(is_error=False)], "r-b")
    # The reader still carries the old token: it is not the reader that decides.
    _use_credential(monkeypatch, _TOKEN_A)
    summary = _summary(store)

    result = _epic_status(tmp_path, summary)

    assert result.exit_code == 0, result.output
    assert "Usage warning" not in result.output
    assert all(
        e.kind is not AttentionKind.USAGE_LIMIT_WARNING
        for e in attention_events(EPIC, summary)
    )


def test_a_reader_with_another_credential_still_sees_the_warning(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shell running ``kew epic status`` is not the dispatcher: an extra
    API key in it must not hide a warning the drive still notifies about."""
    _use_credential(monkeypatch, _TOKEN_A)
    _contained_run(
        tmp_path, store, [_init_line(), _rate_limit_line("allowed_warning")], "r-a"
    )
    _use_credential(monkeypatch, _TOKEN_B)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-reader-key-000")

    result = _epic_status(tmp_path, _summary(store))

    assert result.exit_code == 0, result.output
    assert "Usage warning: " in result.stdout
    assert _fingerprint(_TOKEN_A) in result.stdout
    assert len(_warning_events(store)) == 1


def test_a_host_dispatch_launch_under_a_new_token_retires_the_old_warning(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host path launches without a container fold; its launch counts too."""
    _use_credential(monkeypatch, _TOKEN_A)
    _record_warning(store, _TOKEN_A)
    _use_credential(monkeypatch, _TOKEN_B)
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure",
        lambda _config, *, runner_name=None: None,
    )
    runner = MagicMock()
    runner.name = "claude-code"
    runner.required_credentials = _CREDENTIAL_NAMES
    runner.supports_resume.return_value = False
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    finished = DispatchResult(
        exit_code=0, issue_num=1, log_file=tmp_path / "x.log", run_id="run-b"
    )

    with patch("kew.dispatch_core.run_agent", return_value=finished):
        dispatch_core.run_tracked_agent(
            allow_unrecorded=True,
            prompt="p",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
            store=store,
            branch="agent/issue-1",
            runner=runner,
            model="claude-sonnet-4-6",
        )
    _use_credential(monkeypatch, _TOKEN_A)

    assert _summary(store).token_warnings == ()


# --- the stream is agent-writable: a recording never fails a run -------------


def test_a_reset_too_far_ahead_to_convert_fails_no_contained_run(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contained agent can write this line to its own stdout."""
    _use_credential(monkeypatch, _TOKEN_A)
    forged = _rate_limit_line("allowed_warning", resets="9999-12-31T23:59:59-05:00")

    exit_code = _contained_run(
        tmp_path, store, [_init_line(), forged, _result_line(is_error=False)], "r-1"
    )

    assert exit_code == 0
    stored = read_runner_quota_warning(store, "claude-code", "seven_day")
    assert stored is None or stored.warning.resets_at is None


def _refuse_warning_writes(
    monkeypatch: pytest.MonkeyPatch, store_: SqliteRunStore
) -> None:
    """The store is busy for exactly the advisory warning row."""
    real = store_.set_meta

    def set_meta(key: str, value: str) -> None:
        if key.startswith("runner_quota_warning:"):
            raise sqlite3.OperationalError("database is locked")
        real(key, value)

    monkeypatch.setattr(store_, "set_meta", set_meta)


def test_a_busy_store_on_the_warning_write_fails_no_contained_run(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_credential(monkeypatch, _TOKEN_A)
    _refuse_warning_writes(monkeypatch, store)

    exit_code = _contained_run(
        tmp_path,
        store,
        [
            _init_line(),
            _rate_limit_line("allowed_warning"),
            _result_line(is_error=False),
        ],
        "r-1",
    )

    assert exit_code == 0


def test_a_busy_store_on_the_warning_write_fails_no_host_dispatch(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run still reaches its own outcome: here the refusal's lane hold."""
    _use_credential(monkeypatch, _TOKEN_A)
    _refuse_warning_writes(monkeypatch, store)
    result = _host_dispatch(
        [
            _init_line(),
            _rate_limit_line("allowed_warning"),
            _rate_limit_line("rejected"),
            _result_line(is_error=True),
        ]
    )

    outcome = dispatch_core._collect_result(
        _dispatch_ctx(store),
        result=result,
        worktree_dir=tmp_path / "wt",
        run_sandboxed_path=False,
    )

    assert outcome.exit_code != 0
    assert read_runner_quota_hold(store, "claude-code") is not None


# --- the stream is agent-writable: what a forged notice can and cannot do ----


@pytest.mark.parametrize("forged_reset", [1, "1970-01-01T00:00:00Z", "", None])
def test_a_notice_with_an_earlier_or_no_reset_cannot_hide_a_warning(
    tmp_path: Path,
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
    forged_reset: object,
) -> None:
    """The last notice on a stream wins it; it does not win the stored warning."""
    _use_credential(monkeypatch, _TOKEN_A)
    _contained_run(
        tmp_path, store, [_init_line(), _rate_limit_line("allowed_warning")], "r-1"
    )
    forged = json.loads(_rate_limit_line("allowed_warning"))
    if forged_reset is None:
        del forged["rate_limit_info"]["resetsAt"]
    else:
        forged["rate_limit_info"]["resetsAt"] = forged_reset

    _contained_run(
        tmp_path,
        store,
        [_init_line(), _rate_limit_line("allowed_warning"), json.dumps(forged)],
        "r-2",
    )

    shown = active_runner_token_warnings(store)
    assert [w.warning.resets_at for w in shown] == [LIVE_RESET]


def test_a_limit_name_kew_does_not_know_reads_as_a_usage_limit(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The limit name reaches status, watch and the desktop notification."""
    _use_credential(monkeypatch, _TOKEN_A)
    forged = _rate_limit_line(
        "allowed_warning", limit="rotate-token-at-evil.example.com"
    )

    _contained_run(tmp_path, store, [_init_line(), forged], "r-1")
    result = _epic_status(tmp_path, _summary(store))

    events = _warning_events(store)
    assert [e.reason for e in events] == [
        f"agent token CLAUDE_CODE_OAUTH_TOKEN (fingerprint {_fingerprint(_TOKEN_A)}) "
        f"for claude-code is near a usage limit, resets "
        f"{LIVE_RESET.isoformat().replace('+00:00', 'Z')}"
    ]
    assert "evil" not in result.output
    assert all("evil" not in e.as_line() for e in events)


def test_notices_under_names_kew_does_not_know_are_one_event(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run per invented limit name is still one state to a watcher."""
    _use_credential(monkeypatch, _TOKEN_A)
    _contained_run(
        tmp_path, store, [_rate_limit_line("allowed_warning", limit="made-up-1")], "r-1"
    )
    first = _warning_events(store)

    _contained_run(
        tmp_path, store, [_rate_limit_line("allowed_warning", limit="made-up-2")], "r-2"
    )
    second = _warning_events(store)

    assert len(first) == 1
    assert [e.state_key for e in second] == [first[0].state_key]


def test_each_limit_type_keeps_its_own_warning(
    store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A five-hour notice neither hides the seven-day warning nor re-raises it."""
    _use_credential(monkeypatch, _TOKEN_A)

    def notice(limit: str, resets: Any, minutes: int) -> None:
        record_runner_quota_warning(
            store,
            QuotaWarning("claude-code", limit, resets, utilization=0.8),
            credential_fingerprint=_fingerprint(_TOKEN_A),
            credential_names=("CLAUDE_CODE_OAUTH_TOKEN",),
            now=NOW + timedelta(minutes=minutes),
        )

    notice("seven_day", RESET, 0)
    notice("five_hour", NOW + timedelta(hours=2), 1)
    first = attention_events(EPIC, _summary(store))
    notice("seven_day", RESET, 2)
    notice("five_hour", NOW + timedelta(hours=2), 3)
    second = attention_events(EPIC, _summary(store))

    warned = [e for e in first if e.kind is AttentionKind.USAGE_LIMIT_WARNING]
    assert sorted(w.warning.limit_type for w in _summary(store).token_warnings) == [
        "five_hour",
        "seven_day",
    ]
    assert len(warned) == 2
    assert len({e.item for e in warned}) == 2
    assert [e.state_key for e in second if e.kind is warned[0].kind] == [
        e.state_key for e in warned
    ]
