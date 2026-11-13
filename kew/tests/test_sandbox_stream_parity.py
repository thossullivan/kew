"""Sandbox telemetry goes through ``runner.parse_line()`` (#1147, Task N4).

The contained path used to read claude-shaped stream-json fields directly, so a
non-claude image recorded zero cost and zero tokens (the #761 blindness class)
while the run row asserted ``runner_name="claude-code"`` regardless of which
agent actually ran. These tests pin both halves:

* an opencode-shaped stream lands native cost + tokens on the row,
* the claude route's numbers are UNCHANGED (the characterization half), and
* the row names the runner that really ran.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from support.source_ast import calls, definition, dotted

from kew.config import DispatchConfig
from kew.pricing import compute_cost_from_tokens
from kew.run_store import RunRecord, SqliteRunStore
from kew.sandbox import SandboxConfig
from kew.transcript_metrics import FirstEditMetrics, read_derived_evidence

SANDBOX_IMAGE = SandboxConfig().image


# --------------------------------------------------------------------------
# Fixtures: one real-shaped stream per runner.
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _launch_image_resolves(monkeypatch):
    """The fail-closed image gate (#1550 F1) refuses when no runtime resolves
    the tag; these tests run with detect_runtime_cached forced to None, so
    resolve to a stable fake ID by default."""
    monkeypatch.setattr(
        "kew.dispatch_sandbox._resolve_launch_image_id",
        lambda config, image, **_: "sha256:" + "f" * 64,
    )


def _claude_stream() -> list[str]:
    """A real claude stream-json transcript: init, two turns, terminal result."""
    return [
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "session_id": "sess-1",
                "model": "claude-sonnet-4-6",
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [{"type": "text", "text": "thinking about it"}],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 40,
                        "cache_creation_input_tokens": 10,
                        "cache_read_input_tokens": 5,
                    },
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": "src/kew/thing.py"},
                        }
                    ],
                    "usage": {"input_tokens": 200, "output_tokens": 60},
                },
            }
        ),
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": "sess-1",
                "total_cost_usd": 0.42,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_creation_input_tokens": 30,
                    "cache_read_input_tokens": 40,
                },
                "modelUsage": {
                    "claude-sonnet-4-6": {
                        "inputTokens": 300,
                        "outputTokens": 100,
                        "cacheCreationInputTokens": 10,
                        "cacheReadInputTokens": 5,
                        "costUSD": 0.42,
                    }
                },
            }
        ),
    ]


def _opencode_stream() -> list[str]:
    """An opencode transcript: two step_finish turns, the last one terminal.

    Native per-step cost ($0.03 + $0.07) is opencode's provider-billed truth —
    it exists ONLY as the sum of the steps; opencode never restates a run total.
    """
    return [
        json.dumps(
            {
                "type": "text",
                "sessionID": "oc-1",
                "part": {"text": "working on it"},
            }
        ),
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": "oc-1",
                "part": {
                    "reason": "tool-calls",
                    "cost": 0.03,
                    "tokens": {
                        "input": 1000,
                        "output": 200,
                        "cache": {"read": 500, "write": 100},
                    },
                },
            }
        ),
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": "oc-1",
                "part": {
                    "reason": "stop",
                    "cost": 0.07,
                    "tokens": {"input": 2000, "output": 300, "reasoning": 50},
                },
            }
        ),
    ]


def _pi_stream_without_native_cost() -> list[str]:
    """A pi transcript whose provider reported NO cost — tokens only.

    pi sends ``usage.cost`` only when the provider gives it one, so
    ``_native_step_cost`` is None on every turn while the terminal
    ``stopReason`` still produces a result event. lemonade does the same
    unconditionally. Such a run must be priced from its tokens, not recorded as
    free.
    """
    return [
        json.dumps({"type": "session", "id": "pi-1"}),
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "model": "openrouter/deepseek-v4-flash",
                    "content": [{"type": "text", "text": "working"}],
                    "usage": {"input": 1000, "output": 200},
                },
            }
        ),
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "model": "openrouter/deepseek-v4-flash",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {"input": 2000, "output": 300},
                    "stopReason": "stop",
                },
            }
        ),
    ]


def _pending_row(store: SqliteRunStore, run_id: str, log_file: Path) -> None:
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=11,
            item_id="11",
            branch="agent/issue-11",
            status="pending",
            exit_code=-1,
            started_at=datetime.now(UTC),
            log_file=log_file,
        )
    )


def _stream_and_finalize(
    tmp_path: Path, lines: list[str], runner: str, model: str
) -> RunRecord:
    """Drive the real streaming loop then the real finalizer for *runner*."""
    from kew.dispatch_core import _finalize_sandbox_run, _stream_sandbox_log

    log_file = tmp_path / f"sandbox-{runner}.log"
    handle = MagicMock()
    handle.__iter__ = MagicMock(return_value=iter(ln + "\n" for ln in lines))
    handle.wait.return_value = 0

    with SqliteRunStore(str(tmp_path / f"runs-{runner}.db")) as store:
        _pending_row(store, "run-1", log_file)
        # A clock that always exceeds the flush interval: every contributing
        # line flushes, so the mid-flight row is exercised too.
        clock = iter(range(0, 10_000, 100))
        assert (
            _stream_sandbox_log(
                handle,
                log_file,
                store,
                "run-1",
                model=model,
                overrides=None,
                runner=runner,
                _now=lambda: float(next(clock)),
            )
            == 0
        )
        mid = store.get("run-1")
        assert mid is not None
        assert mid.status == "pending"
        _finalize_sandbox_run(store, "run-1", exit_code=0, model=model, runner=runner)
        final = store.get("run-1")
        assert final is not None
        return final


# --------------------------------------------------------------------------
# The non-claude half — what was broken.
# --------------------------------------------------------------------------


class TestNonClaudeStreamTelemetry:
    def test_opencode_stream_records_native_cost_and_tokens(self, tmp_path):
        """The #761 blindness class: an opencode image's spend must reach the
        row. Before #1147 this recorded zeros — the stream was read as claude
        stream-json, which opencode never emits."""
        row = _stream_and_finalize(
            tmp_path, _opencode_stream(), "opencode", "openrouter/deepseek-v4-flash"
        )
        # Native provider-billed cost = sum of the per-step figures (#1082).
        assert row.estimated_cost_usd == pytest.approx(0.10)
        # opencode reports uncached input separately from cache read/write
        # (#1564); reasoning still folds into output.
        assert row.input_tokens == 1000 + 2000
        assert row.output_tokens == 200 + 300 + 50
        assert row.cache_read_tokens == 500
        assert row.cache_creation_tokens == 100
        # The split is reconcilable: the same 4150 the folded parse produced.
        assert row.total_tokens == 4150

    def test_opencode_telemetry_is_live_before_completion(self, tmp_path):
        """A contained non-claude run must stop showing false zeros WHILE it
        runs — the same #SFD-155 guarantee the claude route already had."""
        from kew.dispatch_core import _stream_sandbox_log

        log_file = tmp_path / "sandbox-live.log"
        handle = MagicMock()
        handle.__iter__ = MagicMock(
            return_value=iter(ln + "\n" for ln in _opencode_stream())
        )
        handle.wait.return_value = 0
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            _pending_row(store, "run-1", log_file)
            clock = iter(range(0, 10_000, 100))
            _stream_sandbox_log(
                handle,
                log_file,
                store,
                "run-1",
                model="openrouter/deepseek-v4-flash",
                overrides=None,
                runner="opencode",
                _now=lambda: float(next(clock)),
            )
            row = store.get("run-1")
        assert row is not None
        assert row.status == "pending"  # never finalized — this is mid-flight
        assert row.total_tokens == 4150
        assert row.estimated_cost_usd == pytest.approx(0.10)

    def test_a_runner_with_no_native_cost_finalizes_on_the_token_estimate(
        self, tmp_path
    ):
        """Half the runners never report a provider-billed figure (lemonade
        always; pi/hermes when the provider omits one). Finalization must keep
        the token-bound estimate the live path wrote, exactly as the host path's
        ``DispatchResult.compute_cost`` does — taking the accumulator's $0.00
        raw would record a run that spent real money as free and under-count
        every budget that sums this column."""
        overrides = {"openrouter/deepseek-v4-flash": (1.0, 5.0)}
        expected = compute_cost_from_tokens(
            model="openrouter/deepseek-v4-flash",
            input_tokens=3000,
            output_tokens=500,
            overrides=overrides,
        )
        assert expected > 0  # the fixture must actually cost something
        row = _stream_and_finalize_with_overrides(
            tmp_path,
            _pi_stream_without_native_cost(),
            "pi",
            "openrouter/deepseek-v4-flash",
            overrides,
        )
        assert row.status == "success"  # the terminal result event WAS seen
        assert row.input_tokens == 3000
        assert row.output_tokens == 500
        assert row.estimated_cost_usd == pytest.approx(expected)
        # A rate existed, so nothing is unreconciled about this row's cost.
        assert row.accounting_status != "unreconciled"

    def test_an_unpriced_model_that_burned_tokens_is_marked_unreconciled(
        self, tmp_path
    ):
        """$0.00 must not be readable as free when it only means "no rate".

        Without a mark, a run that burned real tokens against a model kew
        cannot price is summed as free by every consumer of the cost column —
        cost_per_merged_pr, the cost_anomaly_factor baselines, and the
        published evidence packs (#1565)."""
        row = _stream_and_finalize(
            tmp_path,
            _pi_stream_without_native_cost(),
            "pi",
            "some-vendor/model-with-no-rate-card",
        )
        assert row.input_tokens == 3000
        assert row.estimated_cost_usd == 0.0  # nothing to price against
        assert row.accounting_status == "unreconciled"

    def test_a_free_provider_prefix_stays_genuinely_free(self, tmp_path):
        """Local inference is $0 BY DESIGN and must not be flagged.

        This is the distinction the mark exists to draw: lemonade/ and ollama/
        runs also record $0.00 with real tokens, but there the zero is the
        answer, not a missing one. ``is_priced_model`` counts a free provider
        prefix as priced, which is what keeps these two cases apart."""
        row = _stream_and_finalize(
            tmp_path,
            _pi_stream_without_native_cost(),
            "pi",
            "lemonade/Qwen3-Coder-30B-A3B-Instruct-GGUF",
        )
        assert row.input_tokens == 3000
        assert row.estimated_cost_usd == 0.0
        assert row.accounting_status != "unreconciled"

    def test_a_model_kew_can_price_is_not_marked(self, tmp_path):
        """The mark is about missing rates, not about missing native cost."""
        row = _stream_and_finalize_with_overrides(
            tmp_path,
            _pi_stream_without_native_cost(),
            "pi",
            "openrouter/deepseek-v4-flash",
            {"openrouter/deepseek-v4-flash": (1.0, 5.0)},
        )
        assert row.estimated_cost_usd > 0
        assert row.accounting_status != "unreconciled"


# --------------------------------------------------------------------------
# The claude half — what must NOT change.
# --------------------------------------------------------------------------


class TestClaudeRouteCharacterization:
    """Locks the claude route's numbers across the parse_line refactor."""

    def test_claude_stream_telemetry_is_unchanged(self, tmp_path):
        row = _stream_and_finalize(
            tmp_path, _claude_stream(), "claude-code", "claude-sonnet-4-6"
        )
        # Authoritative modelUsage totals win over the accumulated per-turn
        # figures, exactly as before the refactor.
        assert row.estimated_cost_usd == pytest.approx(0.42)
        assert row.input_tokens == 300
        assert row.output_tokens == 100
        assert row.cache_creation_tokens == 10
        assert row.cache_read_tokens == 5
        assert row.total_tokens == 415
        assert row.reported_model == "claude-sonnet-4-6"

    def test_first_edit_metrics_survive_the_new_telemetry_source(self, tmp_path):
        """#1092's sibling metric: finalization still derives steps/tokens-to-
        first-edit from the same transcript."""
        row = _stream_and_finalize(
            tmp_path, _claude_stream(), "claude-code", "claude-sonnet-4-6"
        )
        assert row.steps_to_first_edit == 2
        assert row.tokens_to_first_edit == 100 + 40 + 10 + 5 + 200 + 60

    def test_planned_bound_is_priced_off_the_new_telemetry(self, tmp_path):
        """#1092: the planned half of the cost pair is repriced from the same
        authoritative totals the new parser produces."""
        from kew.pricing import compute_cost_from_tokens

        overrides = {"claude-sonnet-4-6": (3.0, 15.0)}
        row = _stream_and_finalize_with_overrides(
            tmp_path, _claude_stream(), "claude-code", "claude-sonnet-4-6", overrides
        )
        assert row.planned_cost_usd == pytest.approx(
            compute_cost_from_tokens(
                model="claude-sonnet-4-6",
                input_tokens=300,
                output_tokens=100,
                cache_creation_tokens=10,
                cache_read_tokens=5,
                overrides=overrides,
            )
        )

    def test_an_empty_run_summary_does_not_zero_the_accumulated_tokens(self, tmp_path):
        """claude-code ALWAYS attaches a run summary, so an aborted run's result
        event arrives with no ``modelUsage`` and no ``usage`` — all-zero totals.
        Taking them would re-zero a run whose turns reported real tokens, which
        is the #761 zeroing wearing a different hat."""
        lines = _claude_stream()[:-1] + [
            json.dumps(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "session_id": "sess-1",
                }
            )
        ]
        row = _stream_and_finalize(tmp_path, lines, "claude-code", "claude-sonnet-4-6")
        # The per-turn accumulation stands: 100+200 in, 40+60 out, 10 write, 5 read.
        assert row.input_tokens == 300
        assert row.output_tokens == 100
        assert row.cache_creation_tokens == 10
        assert row.cache_read_tokens == 5

    def test_malformed_lines_never_break_the_tail_loop(self, tmp_path):
        """Container stdout is another process's untrusted output: a JSON array,
        a truncated object, and plain text must all be tolerated."""
        lines = [
            "not json at all",
            json.dumps(["not", "a", "dict"]),
            '{"type": "result", "modelUsage": "oops"}',
        ]
        row = _stream_and_finalize(tmp_path, lines, "claude-code", "claude-sonnet-4-6")
        assert row.status == "success"
        assert row.total_tokens == 0
        assert row.estimated_cost_usd == 0.0


def _stream_and_finalize_with_overrides(
    tmp_path: Path,
    lines: list[str],
    runner: str,
    model: str,
    overrides: dict[str, tuple[float, float]],
) -> RunRecord:
    from kew.dispatch_core import _finalize_sandbox_run, _stream_sandbox_log

    log_file = tmp_path / "sandbox-priced.log"
    handle = MagicMock()
    handle.__iter__ = MagicMock(return_value=iter(ln + "\n" for ln in lines))
    handle.wait.return_value = 0
    with SqliteRunStore(str(tmp_path / "runs-priced.db")) as store:
        _pending_row(store, "run-1", log_file)
        clock = iter(range(0, 10_000, 100))
        _stream_sandbox_log(
            handle,
            log_file,
            store,
            "run-1",
            model=model,
            overrides=overrides,
            runner=runner,
            _now=lambda: float(next(clock)),
        )
        _finalize_sandbox_run(
            store,
            "run-1",
            exit_code=0,
            model=model,
            overrides=overrides,
            runner=runner,
        )
        row = store.get("run-1")
        assert row is not None
        return row


# --------------------------------------------------------------------------
# Honest runner_name on the run row.
# --------------------------------------------------------------------------


class TestHonestRunnerName:
    """#1147: the sandbox run row must name the runner that actually ran.

    A hardcoded ``"claude-code"`` made run history assert a falsehood about
    every contained non-claude run — and ``kew status`` buckets spend by this
    column (``_status_cost_by_runner``), so the lie moved real dollars onto the
    wrong runner's line.
    """

    def _dispatch(
        self, tmp_path, store, runner: str, stream_lines: list[str], monkeypatch
    ):
        from kew import dispatch_core, run_credentials

        # The credential route is out of scope here: a contained direct
        # OpenRouter route requires a bounded per-run key (#1944 round 9) and
        # an issued key records an env auth path that the publication gate
        # refuses (#1862). Keep this run on the non-direct path so the row's
        # runner name and cost are what this test observes.
        monkeypatch.setattr(
            run_credentials, "direct_openrouter_route", lambda *a, **k: False
        )

        checkout = tmp_path / "checkout"
        checkout.mkdir(exist_ok=True)
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        handle = MagicMock()
        handle.__iter__ = MagicMock(return_value=iter(ln + "\n" for ln in stream_lines))
        handle.wait.return_value = 1  # non-zero: stop before the push broker

        with (
            patch.object(
                dispatch_core, "_detect_sandbox_runtime", return_value="/usr/bin/docker"
            ),
            patch.object(dispatch_core, "_launch_container", return_value=handle),
            patch(
                "kew.dispatch_sandbox._contained_auth_path",
                return_value="workload-identity",
            ),
        ):
            dispatch_core._run_sandboxed(
                prompt="do the thing",
                checkout_dir=checkout,
                config=config,
                image=SANDBOX_IMAGE,
                runner=runner,
                issue_num=11,
                item_id="11",
                branch="agent/issue-11",
                title="t",
                model="openrouter/deepseek-v4-flash",
                store=store,
            )
        [row] = store.list_by_issue(11)
        return row

    def test_row_records_the_runner_that_ran(self, tmp_path, monkeypatch):
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            row = self._dispatch(
                tmp_path, store, "opencode", _opencode_stream(), monkeypatch
            )
        assert row.runner_name == "opencode"
        assert row.estimated_cost_usd == pytest.approx(0.10)

    def test_claude_row_still_records_claude(self, tmp_path, monkeypatch):
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            row = self._dispatch(
                tmp_path, store, "claude-code", _claude_stream(), monkeypatch
            )
        assert row.runner_name == "claude-code"


# --------------------------------------------------------------------------
# The operator console: narrate live, stay silent on the replay.
# --------------------------------------------------------------------------


class TestReplayIsNotReNarrated:
    """``parse_line`` prints to the console as a side effect, and finalization
    replays the WHOLE transcript through it for telemetry. Without the mute,
    every contained run would print itself a second time at completion —
    including on the success path, where nothing is being diagnosed."""

    def _stream(self, tmp_path: Path, store: SqliteRunStore, log_file: Path) -> None:
        from kew.dispatch_core import _stream_sandbox_log

        handle = MagicMock()
        handle.__iter__ = MagicMock(
            return_value=iter(ln + "\n" for ln in _claude_stream())
        )
        handle.wait.return_value = 0
        _stream_sandbox_log(
            handle,
            log_file,
            store,
            "run-1",
            model="claude-sonnet-4-6",
            overrides=None,
            runner="claude-code",
        )

    def test_finalization_does_not_reprint_the_transcript(self, tmp_path, capsys):
        from kew.dispatch_core import _finalize_sandbox_run

        log_file = tmp_path / "sandbox-narration.log"
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            _pending_row(store, "run-1", log_file)
            self._stream(tmp_path, store, log_file)
            # The live half: the operator DOES see the run as it happens.
            assert "thinking about it" in capsys.readouterr().err
            _finalize_sandbox_run(
                store,
                "run-1",
                exit_code=0,
                model="claude-sonnet-4-6",
                runner="claude-code",
            )
            replay_output = capsys.readouterr().err
            row = store.get("run-1")
        assert "thinking about it" not in replay_output
        assert replay_output == ""
        # Muted display, unchanged telemetry.
        assert row is not None
        assert row.estimated_cost_usd == pytest.approx(0.42)

    def test_the_mute_is_scoped_to_the_replay(self, tmp_path, capsys):
        """The suppression must not leak past the block and silence the next
        run's live stream in the same process."""
        from kew.dispatch_core import _finalize_sandbox_run

        log_file = tmp_path / "sandbox-narration.log"
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            _pending_row(store, "run-1", log_file)
            self._stream(tmp_path, store, log_file)
            _finalize_sandbox_run(store, "run-1", exit_code=0, runner="claude-code")
            capsys.readouterr()
            _pending_row(store, "run-2", log_file)
            self._stream(tmp_path, store, log_file)
        assert "thinking about it" in capsys.readouterr().err


@pytest.mark.parametrize("path", ["host", "sandbox"])
def test_live_narration_redacts_before_display_and_replay_stays_mute(
    tmp_path, monkeypatch, capsys, path
):
    from io import StringIO

    from kew.dispatch_core import _finalize_sandbox_run
    from kew.dispatch_sandbox import _accumulate_stream_line, _SandboxTelemetry
    from kew.orchestrator import _process_line
    from kew.runners import get_runner
    from kew.tracking import DispatchResult
    from kew.transcript_metrics import StreamDerivation

    password = "s3cret" * 60
    secret = f"http://user:{password}@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    line = json.dumps({"type": "assistant", "message": "thinking about it " + secret})
    runner = get_runner("claude-code")
    if path == "host":
        result = DispatchResult(
            exit_code=0, issue_num=1, log_file=tmp_path / "live.log"
        )
        _process_line(
            line, StringIO(), None, runner, result, "", None, None, StreamDerivation()
        )
    else:
        _accumulate_stream_line(_SandboxTelemetry(), line, runner)
    narration = capsys.readouterr().err
    assert "thinking about it" in narration
    assert "s3cret" not in narration
    assert "redacted" in narration
    with SqliteRunStore(str(tmp_path / "replay.db")) as store:
        _pending_row(store, "run-1", tmp_path / "live.log")
        _finalize_sandbox_run(store, "run-1", exit_code=0, runner="claude-code")
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# I06T: native and contained streams derive the SAME evidence, keeping neither
# transcript — and nothing downstream replays one.
# ---------------------------------------------------------------------------


class TestSanitizedStreamParity:
    """The host path and the contained path read the same bytes and must reach
    the same facts while retaining none of them.

    Parity is the whole reason the derivation is one object rather than two: a
    contained run whose evidence disagreed with an identical host run would be
    a second interpretation of one stream, which is the #1147 class of bug all
    over again — this time in what a receipt says happened.
    """

    @staticmethod
    def _derive_via_host(tmp_path, lines, runner="claude-code"):
        """Run a fixture through the HOST orchestrator stream loop."""
        from kew.orchestrator import run_agent
        from kew.runners import get_runner

        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.returncode = 0
        proc.poll.return_value = 0
        proc.stdout.readline.side_effect = [line + "\n" for line in lines] + [""]

        selector = MagicMock()
        selector.select.return_value = [(MagicMock(), 1)]
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "hostlogs")

        with (
            patch("kew.orchestrator.popen", return_value=proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch("kew.orchestrator.selectors.DefaultSelector", return_value=selector),
        ):
            result = run_agent(
                runner=get_runner(runner),
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=5,
            )
        return result.log_file

    @staticmethod
    def _derive_via_sandbox(tmp_path, lines, runner="claude-code"):
        """Run the same fixture through the CONTAINED tail loop."""
        from kew.dispatch_sandbox import _stream_sandbox_log

        handle = MagicMock()
        handle.__iter__ = MagicMock(return_value=iter(line + "\n" for line in lines))
        handle.wait.return_value = 0
        log_file = tmp_path / "sandbox-5.log"
        _stream_sandbox_log(
            handle,
            log_file,
            None,
            "run-5",
            model="claude-sonnet-4-6",
            overrides=None,
            runner=runner,
        )
        return log_file

    def test_same_evidence_from_both_paths(self, tmp_path):
        lines = _claude_stream()
        host = read_derived_evidence(self._derive_via_host(tmp_path, lines))
        contained = read_derived_evidence(self._derive_via_sandbox(tmp_path, lines))

        assert host is not None and contained is not None
        assert host.first_edit == contained.first_edit
        assert host.first_edit == FirstEditMetrics(2, 415)
        assert host.tool_uses == contained.tool_uses
        assert host.steps == contained.steps
        assert host.terminal_type == contained.terminal_type == "result"

    def test_opencode_error_facts_match_across_both_paths(self, tmp_path):
        lines = [
            json.dumps(
                {
                    "type": "error",
                    "sessionID": "oc-1",
                    "error": {
                        "name": "APIError",
                        "data": {
                            "message": "credential-bearing provider prose",
                            "statusCode": 429,
                            "isRetryable": True,
                            "responseBody": "raw provider response",
                        },
                    },
                }
            )
        ]
        host_log = self._derive_via_host(tmp_path, lines, runner="opencode")
        contained_log = self._derive_via_sandbox(tmp_path, lines, runner="opencode")
        host = read_derived_evidence(host_log)
        contained = read_derived_evidence(contained_log)

        assert host is not None and contained is not None
        assert host.terminal_type == contained.terminal_type == "error"
        assert host.error_name == contained.error_name == "APIError"
        assert host.http_status == contained.http_status == 429
        for log_file in (host_log, contained_log):
            serialized = log_file.with_name(log_file.stem + ".evidence.json").read_text(
                encoding="utf-8"
            )
            assert "credential-bearing provider prose" not in serialized
            assert "raw provider response" not in serialized

    def test_neither_path_keeps_the_transcript(self, tmp_path):
        lines = _claude_stream()
        for log_file in (
            self._derive_via_host(tmp_path, lines),
            self._derive_via_sandbox(tmp_path, lines),
        ):
            written = log_file.read_text(encoding="utf-8")
            assert "thinking about it" not in written
            assert "src/kew/thing.py" not in written
            assert "modelUsage" not in written
            assert written.splitlines()[0].startswith("# kew sanitized dispatch log")

    def test_evidence_is_available_without_any_log_at_all(self, tmp_path):
        """The sidecar stands alone: delete the log and every fact survives."""
        log_file = self._derive_via_sandbox(tmp_path, _claude_stream())
        log_file.unlink()
        evidence = read_derived_evidence(log_file)
        assert evidence is not None
        assert evidence.first_edit == FirstEditMetrics(2, 415)


_WRITERS = frozenset({"write", "writelines"})
_READERS = frozenset({"open", "read_text", "read_bytes", "readlines"})


def _method(call: ast.Call) -> str:
    """The last segment of a call's callee: ``fh.write(...)`` is ``write``."""
    return (dotted(call.func) or "").rsplit(".", 1)[-1]


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _raw_names(fn: ast.AST) -> set[str]:
    """Every name in *fn* that holds raw provider bytes, or something made of them.

    The raw line is whatever the fold hands to ``.observe(...)`` and whatever a
    loop draws from the stream; anything assigned or looped from a raw name is
    raw too, EXCEPT what ``.observe`` hands back — that is the one sanitizer.
    Read off the tree by data flow, so renaming the line or the file handle
    cannot hide a raw write, and neither can splitting it across a local.
    """
    raw = {
        arg.id
        for call in calls(fn)
        if _method(call) == "observe"
        for arg in call.args
        if isinstance(arg, ast.Name)
    }
    raw |= {
        target.id
        for loop in ast.walk(fn)
        if isinstance(loop, ast.For | ast.AsyncFor)
        for target in ast.walk(loop.target)
        if isinstance(target, ast.Name)
    }
    flows = [
        (node.targets if isinstance(node, ast.Assign) else [node.target], node.value)
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign)
        and node.value is not None
    ]
    grew = True
    while grew:
        grew = False
        for targets, value in flows:
            sanitized = isinstance(value, ast.Call) and _method(value) == "observe"
            if sanitized or not _names(value) & raw:
                continue
            for target in targets:
                new = _names(target) - raw
                raw |= new
                grew = grew or bool(new)
    return raw


def _raw_writes(fn: ast.AST) -> list[str]:
    raw = _raw_names(fn)
    return [
        ast.unparse(call)
        for call in calls(fn)
        if _method(call) in _WRITERS
        and any(_names(arg) & raw for arg in [*call.args, *call.keywords])
    ]


@pytest.mark.policy
class TestNoRawReingest:
    """Architecture rule (Task 27): the durable paths cannot write raw provider
    bytes, and no consumer can later re-ingest a transcript.

    Asserted against the syntax tree rather than behaviour because the failure
    this guards is a *reintroduction* — someone adding back an
    ``fh.write(line)`` or a convenience ``open(log_file)`` in a finalizer, which
    every behavioural test here would still pass for the run that happened to
    be sanitized.
    """

    def test_stream_writers_never_write_a_raw_line(self):
        from kew import dispatch_sandbox, orchestrator

        # ``sandbox_log_fold`` is the contained path's writer: the per-line body
        # of ``_stream_sandbox_log`` moved into it when a run placed on a host
        # had to fold its relayed stream through the same code (#2526). The
        # guarantee follows the writer, so this inspects the function that
        # actually holds the ``write`` — inspecting the loop that calls it would
        # pass against any body it delegated to.
        for fn in (orchestrator._process_line, dispatch_sandbox.sandbox_log_fold):
            tree = definition(fn)
            assert any(_method(call) == "observe" for call in calls(tree)), (
                f"{fn.__name__} no longer folds its line through .observe()"
            )
            assert any(_method(call) in _WRITERS for call in calls(tree)), (
                f"{fn.__name__} writes nothing — this check would hold vacuously"
            )
            assert not _raw_writes(tree), (fn.__name__, _raw_writes(tree))
        # And the loop that drives it keeps no raw line of its own.
        loop = definition(dispatch_sandbox._stream_sandbox_log)
        assert not _raw_writes(loop), _raw_writes(loop)

    def test_no_consumer_opens_a_dispatch_log_for_replay(self):
        from kew import dispatch_core, dispatch_sandbox, transcript_metrics

        for fn in (
            dispatch_core._extract_sandbox_report,
            dispatch_sandbox._telemetry_from_evidence,
            dispatch_sandbox._finalize_sandbox_run,
            transcript_metrics.first_edit_metrics_for,
        ):
            opened = [
                ast.unparse(call)
                for call in calls(definition(fn))
                if _method(call) in _READERS
            ]
            assert not opened, (fn.__name__, opened)

    def test_derived_evidence_has_no_field_a_transcript_could_land_in(self):
        from dataclasses import fields

        from kew.transcript_metrics import DerivedRunEvidence, RunnerTotals

        names = {f.name for f in fields(DerivedRunEvidence)}
        names |= {f.name for f in fields(RunnerTotals)}
        for banned in ("raw", "transcript", "reasoning", "text", "prompt", "content"):
            assert not any(banned in name for name in names), (banned, names)
