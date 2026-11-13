"""The planned/actual cost pair (#1092, phase 2 of #1082).

``planned_cost_usd`` is the token-bound figure — the run's realized token
usage priced at the snapshot/override *bound* (a conservative max-across-
provider-endpoints rate). ``estimated_cost_usd`` is the authoritative
recorded spend, which since #1082 is the runner's native provider-billed
total when it reports one. Persisting both makes the planning-accuracy ratio
(actual ÷ planned) per model computable — the calibration signal #534 wants.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from kew.inference_transport import (
    AdmittedInferenceTransport,
    ResolvedInferenceTransport,
)
from kew.run_store import RunRecord, SqliteRunStore
from kew.scorecard import compute_model_scorecard, render_model_scorecard
from kew.tracking import DispatchResult

_PRICING = {"test-model": (1.0, 5.0)}  # $/M input, $/M output


def _result(**overrides: object) -> DispatchResult:
    result = DispatchResult(exit_code=0, issue_num=351, log_file=Path("/tmp/x.log"))
    result.accumulate_usage(input_tokens=1_000_000, output_tokens=100_000)
    for key, value in overrides.items():
        setattr(result, key, value)
    return result


class TestDispatchResultPair:
    def test_native_actual_wins_but_planned_bound_is_kept(self):
        """The native runner figure stays authoritative; the token bound it
        replaced is no longer discarded — both halves survive."""
        result = _result(reported_cost_usd=0.1064)
        result.compute_cost("test-model", overrides=_PRICING)
        assert result.estimated_cost_usd == 0.1064
        assert result.planned_cost_usd == 1.5  # 1M in @$1 + 100k out @$5

    def test_planned_equals_actual_when_no_native_figure(self):
        """claude-code reports no per-step native cost, so the token bound IS
        the recorded spend — the pair is honest, not degenerate-by-accident."""
        result = _result()
        result.compute_cost("test-model", overrides=_PRICING)
        assert result.estimated_cost_usd == 1.5
        assert result.planned_cost_usd == 1.5

    def test_no_bound_recorded_when_there_is_nothing_to_price_against(self):
        """A runner on its own default model, or a model with no pricing entry,
        has no bound — recording $0.00 would read as an infinite over-run."""
        for model in (None, "some/unpriced-model"):
            result = _result(reported_cost_usd=0.5)
            result.compute_cost(model)
            assert result.estimated_cost_usd == 0.5
            assert result.planned_cost_usd is None

    def test_unpriced_model_without_a_native_figure_still_meters_zero(self):
        """Unchanged pre-existing behaviour for the ACTUAL: no pricing entry and
        no native cost means the run meters $0 (and estimate_cost warns). The
        planned half stays None on both the host and sandbox paths — a $0 bound
        would count the run into planned_n without backing it."""
        import warnings

        result = _result()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result.compute_cost("some/unpriced-model")
        assert result.estimated_cost_usd == 0.0
        assert result.planned_cost_usd is None

    def test_no_model_at_all_records_no_bound_either(self):
        """A runner on its own default model has nothing to price against, with
        or without a native figure — the sandbox path's `not model` guard."""
        result = _result()
        result.compute_cost(None)
        assert result.estimated_cost_usd == 0.0
        assert result.planned_cost_usd is None

    def test_free_model_records_a_real_zero_bound(self):
        """$0 by design (local inference) is a bound, not a missing one — it
        must stay 0.0 rather than collapse into the None sentinel."""
        result = _result()
        result.compute_cost("ollama/qwen3")
        assert result.planned_cost_usd == 0.0

    def test_planned_is_none_until_cost_is_computed(self):
        """None means "no bound recorded", distinct from a $0 bound."""
        assert _result().planned_cost_usd is None

    def test_native_cost_keeps_an_overhead_only_bound_for_an_unpriced_model(self):
        """An adaptive Switchyard run can report a native cost on an unpriced
        route alias while carrying a well-priced classifier model. The
        overhead-only bound the no-native-cost path keeps must survive here
        too, or the priced half of the run is silently discarded."""
        import warnings

        result = _result(reported_cost_usd=0.5)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result.compute_cost(
                "some/unpriced-model",
                overrides=_PRICING,
                classifier_model="test-model",
                classifier_input_tokens=1_000_000,
                classifier_output_tokens=100_000,
            )
        assert result.classifier_overhead_cost_usd == 1.5
        assert result.estimated_cost_usd == 2.0
        assert result.planned_cost_usd == 1.5


class TestPersistence:
    @pytest.mark.parametrize(
        "session_id", [None, "12345678-1234-5678-9abc-123456789abc"]
    )
    def test_persist_completion_writes_both_halves(self, tmp_path, session_id):
        from kew.config import DispatchConfig
        from kew.dispatch_core import (
            _persist_completion,
            _RetryOutcome,
            _TrackedRun,
        )

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.save(
                RunRecord(
                    run_id="run-1",
                    issue_num=351,
                    branch="agent/issue-351",
                    status="running",
                    exit_code=0,
                    started_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
            transport = AdmittedInferenceTransport(
                resolved=ResolvedInferenceTransport(
                    mode="direct",
                    requested_model="test-model",
                    model="test-model",
                    config_sha256="",
                ),
                observed_version=None,
                preflight_verdict="not_required",
            )
            run = _TrackedRun(
                config=DispatchConfig(),
                store=store,
                run_id="run-1",
                issue_num=351,
                item_id="351",
                branch="agent/issue-351",
                title="t",
                complexity="medium",
                run_kind="dispatch",
                loop_id=None,
                prefix_key=None,
                provider=None,
                runner=None,  # type: ignore[arg-type]
                effective_model="test-model",
                actor=None,  # type: ignore[arg-type]
                endpoint=None,
                repo=None,
                log_file=Path(tmp_path / "run.log"),
                dispatch_start=0.0,
                transport=transport,
            )
            result = _result(reported_cost_usd=0.1064)
            result.session_id = session_id
            result.compute_cost("test-model", overrides=_PRICING)
            _persist_completion(
                run, result, _RetryOutcome(last_exit_code=0, total_attempts=1), None
            )

            stored = store.get("run-1")
            assert stored is not None
            assert stored.estimated_cost_usd == 0.1064
            assert stored.planned_cost_usd == 1.5
            assert stored.session_id is None


class TestScorecardCalibration:
    @staticmethod
    def _record(run_id: str, *, actual: float, planned: float | None) -> RunRecord:
        return RunRecord(
            run_id=run_id,
            issue_num=351,
            branch="agent/issue-351",
            status="success",
            exit_code=0,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            model="test-model",
            complexity="medium",
            estimated_cost_usd=actual,
            planned_cost_usd=planned,
        )

    def test_planning_ratio_is_actual_over_planned(self):
        card = compute_model_scorecard(
            [
                self._record("a", actual=0.1064, planned=1.0787),
                self._record("b", actual=0.1, planned=1.0),
            ]
        )
        row = card.rows[0]
        assert row.planned_spend == 2.0787
        rendered = render_model_scorecard(
            card, repo="o/r", days=7, now=datetime(2026, 1, 8, tzinfo=UTC)
        )
        # 0.2064 / 2.0787 ≈ 0.0993 → the bound over-records ~10x.
        assert "0.10x" in rendered
        assert "Planned vs actual" in rendered

    def test_rows_without_a_planned_bound_render_no_ratio(self):
        """Historical rows carry NULL — never silently counted as $0 planned,
        which would report a fabricated infinite over-run."""
        card = compute_model_scorecard([self._record("a", actual=0.5, planned=None)])
        assert card.rows[0].planned_spend == 0.0
        assert card.rows[0].planned_n == 0
        rendered = render_model_scorecard(
            card, repo="o/r", days=7, now=datetime(2026, 1, 8, tzinfo=UTC)
        )
        assert "—" in rendered


class TestReportSurface:
    def test_totals_name_the_pair_and_its_semantics(self):
        from kew.config import BudgetConfig
        from kew.report import _render_totals, compute_stats

        records = [
            RunRecord(
                run_id="a",
                issue_num=351,
                branch="agent/issue-351",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                model="test-model",
                estimated_cost_usd=0.1064,
                planned_cost_usd=1.0787,
            )
        ]
        stats = compute_stats(records, pr_states={})
        assert stats.planned_spend == 1.0787
        body = "\n".join(_render_totals(stats, BudgetConfig(), days=7))
        assert "$1.08" in body
        assert "planned bound" in body.lower()

    def test_planned_bound_line_omitted_when_no_run_recorded_one(self):
        from kew.config import BudgetConfig
        from kew.report import _render_totals, compute_stats

        stats = compute_stats([], pr_states={})
        assert stats.planned_spend == 0.0
        body = "\n".join(_render_totals(stats, BudgetConfig(), days=7))
        assert "planned bound" not in body.lower()


class TestWebSurface:
    def test_service_dto_and_json_twin_carry_the_pair(self, tmp_path):
        """The web/scorecard API and the evidence-pack JSON both expose the
        pair plus the derived planning ratio (#1092)."""
        from datetime import timedelta

        from kew.report_export import _scorecard_metrics
        from kew.service.report import model_scorecard

        now = datetime(2026, 1, 8, tzinfo=UTC)
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.save(
                RunRecord(
                    run_id="w-1",
                    issue_num=351,
                    branch="agent/issue-351",
                    status="success",
                    exit_code=0,
                    started_at=now - timedelta(days=1),
                    model="test-model",
                    complexity="medium",
                    estimated_cost_usd=0.1064,
                    planned_cost_usd=1.0787,
                )
            )
            dto = model_scorecard(store, days=7, now=now)

        row = dto.rows[0]
        assert row.planned_spend == 1.0787
        assert row.paired_spend == 0.1064
        assert row.planned_n == 1
        assert row.by_complexity[0].planned_spend == 1.0787
        metrics = _scorecard_metrics(row)
        assert metrics["planned_spend"] == 1.0787
        assert metrics["planning_ratio"] == 0.0986

    def test_json_twin_omits_a_ratio_when_no_bound_recorded(self):
        from kew.report_export import _scorecard_metrics
        from kew.scorecard import ModelRow

        metrics = _scorecard_metrics(ModelRow(model="m", runs=1, spend=0.5))
        assert metrics["planning_ratio"] is None
        assert metrics["planned_runs"] == 0


class TestSandboxPair:
    """The contained path must record the SAME pair semantics as the host path.

    Contained runs write their row twice — a periodic live flush during
    streaming and the terminal finalize — so the bound has two chances to end
    up describing a different set of tokens than the row's own columns.
    """

    @staticmethod
    def _pending(store: SqliteRunStore, run_id: str, log_file: Path) -> None:
        store.save(
            RunRecord(
                run_id=run_id,
                issue_num=351,
                item_id="351",
                branch="agent/issue-351",
                status="pending",
                exit_code=0,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                log_file=log_file,
                model="test-model",
            )
        )

    @staticmethod
    def _result_log(path: Path, *, cost: float, input_tokens: int) -> None:
        """Stream a terminal result event the way a real contained run does.

        I06T: the finalizer reads the derived-evidence sidecar, not the
        transcript, so this fixture has to produce the sidecar — which means
        streaming through the real tail loop rather than writing a log file the
        finalizer would (correctly) refuse to re-read.
        """
        import json
        from unittest.mock import MagicMock

        from kew.dispatch_sandbox import _stream_sandbox_log

        line = (
            json.dumps(
                {
                    "type": "result",
                    "total_cost_usd": cost,
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": 100_000,
                    },
                }
            )
            + "\n"
        )
        handle = MagicMock()
        handle.__iter__ = MagicMock(return_value=iter([line]))
        handle.wait.return_value = 0
        _stream_sandbox_log(
            handle,
            path,
            None,
            "run-1",
            model="test-model",
            overrides=None,
            runner="claude-code",
        )

    def test_live_flush_records_no_bound_for_an_unpriced_model(self, tmp_path):
        """A $0 bound is exactly the fabrication the host path avoids: it reads
        as an infinite over-run and drags any mixed bucket's ratio."""
        from kew.dispatch_sandbox import (
            _SandboxTelemetry,
            _write_live_sandbox_telemetry,
        )

        live = _SandboxTelemetry()
        live.input_tokens = 1_000_000
        live.output_tokens = 100_000
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            self._pending(store, "live-1", tmp_path / "s.log")
            _write_live_sandbox_telemetry(
                store, "live-1", live, "some/unpriced-model", None
            )
            stored = store.get("live-1")
            assert stored is not None
            assert stored.planned_cost_usd is None

    def test_finalize_reprices_the_bound_against_the_final_token_totals(self, tmp_path):
        """The streaming loop flushes at most every N seconds and never after
        its last line, so the live bound covers a token subset while finalize
        writes the complete totals. Both halves must describe one run."""
        from kew.dispatch_sandbox import (
            _finalize_sandbox_run,
            _SandboxTelemetry,
            _write_live_sandbox_telemetry,
        )

        log_file = tmp_path / "sandbox-351.log"
        # Result event: 1M input + 100k output, natively billed at $0.20.
        self._result_log(log_file, cost=0.20, input_tokens=1_000_000)

        partial = _SandboxTelemetry()
        partial.input_tokens = 100_000  # a tenth of the run, mid-stream
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            self._pending(store, "run-1", log_file)
            _write_live_sandbox_telemetry(
                store, "run-1", partial, "test-model", _PRICING
            )
            assert store.get("run-1").planned_cost_usd == 0.1  # the subset bound
            _finalize_sandbox_run(
                store,
                "run-1",
                exit_code=0,
                model="test-model",
                overrides=_PRICING,
                runner="claude-code",
            )
            stored = store.get("run-1")
            assert stored is not None
            assert stored.total_tokens == 1_100_000
            assert stored.estimated_cost_usd == 0.20  # native actual
            assert stored.planned_cost_usd == 1.5  # 1M in @$1 + 100k out @$5

    def test_finalize_without_a_model_leaves_the_recorded_bound_alone(self, tmp_path):
        """Nothing to reprice against — don't silently null a bound the
        streaming path legitimately recorded."""
        from kew.dispatch_sandbox import _finalize_sandbox_run

        log_file = tmp_path / "sandbox-351.log"
        self._result_log(log_file, cost=0.20, input_tokens=1_000_000)
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            self._pending(store, "run-1", log_file)
            record = store.get("run-1")
            record.planned_cost_usd = 1.5
            store.save(record)
            _finalize_sandbox_run(store, "run-1", exit_code=0, runner="claude-code")
            assert store.get("run-1").planned_cost_usd == 1.5

    def test_finalize_without_a_result_event_keeps_the_flushed_telemetry(
        self, tmp_path
    ):
        """A launch/stream error terminates the run with no result event to
        parse. The live path already flushed real tokens and a real bound — an
        empty parse must not re-record a run that spent money as free."""
        from kew.dispatch_sandbox import (
            _finalize_sandbox_run,
            _SandboxTelemetry,
            _write_live_sandbox_telemetry,
        )

        log_file = tmp_path / "sandbox-351.log"
        log_file.write_text('{"type": "assistant"}\n', encoding="utf-8")  # no result
        live = _SandboxTelemetry()
        live.input_tokens = 1_000_000
        live.output_tokens = 100_000
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            self._pending(store, "run-1", log_file)
            _write_live_sandbox_telemetry(store, "run-1", live, "test-model", _PRICING)
            _finalize_sandbox_run(
                store,
                "run-1",
                exit_code=-1,
                error_category="sandbox_stream_error",
                model="test-model",
                overrides=_PRICING,
                runner="claude-code",
            )
            stored = store.get("run-1")
            assert stored is not None
            assert stored.status == "failure"
            assert stored.error_category == "sandbox_stream_error"
            assert stored.total_tokens == 1_100_000
            assert stored.estimated_cost_usd == 1.5
            assert stored.planned_cost_usd == 1.5


class TestKilledReviewerKeepsItsPair:
    """A reviewer killed mid-review really did spend tokens (F6/F7, #977)."""

    def test_terminal_kill_row_preserves_the_live_row_telemetry(self, tmp_path):
        from datetime import timedelta

        from kew.config import DispatchConfig
        from kew.graph.driver.tick import _kill_reviewer_reservations
        from kew.pseudo_epics import REVIEW_EPIC_REF
        from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND

        reserved_at = datetime(2026, 1, 1, tzinfo=UTC)
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            assert store.reserve_epic_node_item(REVIEW_EPIC_REF, "351", "rev-1")
            reservation = store.list_epic_reservations(REVIEW_EPIC_REF)[0]
            store.save(
                RunRecord(
                    run_id="rev-1",
                    issue_num=351,
                    item_id="351",
                    branch="agent/issue-351",
                    status="pending",
                    exit_code=0,
                    started_at=reservation.reserved_at,
                    run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
                    input_tokens=1_000_000,
                    output_tokens=100_000,
                    total_tokens=1_100_000,
                    estimated_cost_usd=0.20,
                    planned_cost_usd=1.5,
                    model="test-model",
                )
            )
            _kill_reviewer_reservations(
                store,
                "351",
                DispatchConfig(),
                now=reserved_at + timedelta(minutes=5),
            )
            killed = store.get("rev-1")
            assert killed is not None
            assert killed.status == "killed"
            assert killed.exit_code == 143
            assert killed.error_category == "review_out_of_band"
            assert killed.run_kind == ADVERSARIAL_REVIEW_RUN_KIND
            # The spend the reviewer actually incurred survives the kill —
            # both halves of the pair included.
            assert killed.estimated_cost_usd == 0.20
            assert killed.planned_cost_usd == 1.5
            assert killed.total_tokens == 1_100_000

    def test_kill_row_is_still_written_when_no_run_row_exists(self, tmp_path):
        """Pre-spawn kill: nothing to preserve, but the terminal row must still
        land or the reservation never releases."""
        from kew.config import DispatchConfig
        from kew.graph.driver.tick import _kill_reviewer_reservations
        from kew.pseudo_epics import REVIEW_EPIC_REF
        from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.reserve_epic_node_item(REVIEW_EPIC_REF, "351", "rev-1")
            _kill_reviewer_reservations(
                store, "351", DispatchConfig(), now=datetime(2026, 1, 1, tzinfo=UTC)
            )
            killed = store.get("rev-1")
            assert killed is not None
            assert killed.status == "killed"
            assert killed.run_kind == ADVERSARIAL_REVIEW_RUN_KIND
            assert killed.planned_cost_usd is None
            assert not store.list_epic_reservations(REVIEW_EPIC_REF)

    def test_fallback_row_lends_neither_telemetry_nor_run_kind(self, tmp_path):
        """``resolve_reservation_run`` may fall back to the newest item_id row
        of the generation — a DIFFERENT run. Copying its spend would double-count
        it, and copying its run_kind would file a reviewer kill under
        ``dispatch``: the synthetic row inherits nothing."""
        from datetime import timedelta

        from kew.config import DispatchConfig
        from kew.graph.driver.tick import _kill_reviewer_reservations
        from kew.pseudo_epics import REVIEW_EPIC_REF
        from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            assert store.reserve_epic_node_item(REVIEW_EPIC_REF, "351", "rev-1")
            reservation = store.list_epic_reservations(REVIEW_EPIC_REF)[0]
            # A dispatch run of the same item, newer than the reservation —
            # the fallback resolve_reservation_run returns when "rev-1" has no
            # row of its own.
            store.save(
                RunRecord(
                    run_id="dispatch-9",
                    issue_num=351,
                    item_id="351",
                    branch="agent/issue-351",
                    status="running",
                    exit_code=0,
                    started_at=reservation.reserved_at + timedelta(seconds=1),
                    run_kind="dispatch",
                    total_tokens=1_100_000,
                    estimated_cost_usd=0.20,
                    planned_cost_usd=1.5,
                )
            )
            now = reservation.reserved_at + timedelta(minutes=5)
            _kill_reviewer_reservations(store, "351", DispatchConfig(), now=now)

            killed = store.get("rev-1")
            assert killed is not None
            assert killed.run_kind == ADVERSARIAL_REVIEW_RUN_KIND
            assert killed.planned_cost_usd is None
            assert killed.estimated_cost_usd == 0.0
            assert killed.total_tokens == 0
            assert killed.duration_seconds == 300.0
            # The unrelated dispatch row is untouched.
            assert store.get("dispatch-9").status == "running"
