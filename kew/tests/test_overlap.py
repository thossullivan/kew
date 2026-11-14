"""Tests for overlap coordination module."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from support.cost_growth import FACTOR, LINEAR_CEILING, cost_growth

from kew.config import DispatchConfig, OverlapAdvancedConfig, OverlapConfig
from kew.overlap import (
    _PATH_SCOPE_RE,
    HoldInfo,
    MergedContext,
    OverlapCoordinator,
    OverlapDecision,
    build_module_index,
    predict_files_from_text,
    strip_stale_holds,
)


class TestOverlapDecision:
    def test_dispatch_decision(self):
        d = OverlapDecision(action="dispatch", predicted_files={"a.py"})
        assert d.action == "dispatch"
        assert d.blocking_issue is None

    def test_hold_decision(self):
        d = OverlapDecision(
            action="hold",
            blocking_issue=42,
            reason="overlaps with #42 on scheduler.py",
            predicted_files={"scheduler.py"},
        )
        assert d.action == "hold"
        assert d.blocking_issue == 42


class TestHoldInfo:
    def test_creation(self):
        h = HoldInfo(
            blocking_issues={42, 43},
            predicted_overlap_files={"a.py"},
            held_at=datetime.now(UTC),
        )
        assert 42 in h.blocking_issues
        assert "a.py" in h.predicted_overlap_files


class TestMergedContext:
    def test_creation(self):
        m = MergedContext(
            actual_files=["a.py", "b.py"],
            completed_at=datetime.now(UTC),
        )
        assert len(m.actual_files) == 2


class TestPredictFilesFromText:
    def test_extracts_python_file_paths(self):
        text = "We need to update scheduler.py and config.py"
        files = predict_files_from_text(
            text,
            source_files=[
                "src/kew/scheduler.py",
                "src/kew/config.py",
                "src/kew/budget.py",
            ],
        )
        assert "src/kew/scheduler.py" in files
        assert "src/kew/config.py" in files
        assert "src/kew/budget.py" not in files

    def test_extracts_full_paths(self):
        text = "Modify src/kew/worktree.py to handle conflicts"
        files = predict_files_from_text(
            text, source_files=["src/kew/worktree.py", "src/kew/config.py"]
        )
        assert "src/kew/worktree.py" in files

    def test_extracts_backtick_identifiers(self):
        text = "Fix the `run_tracked_agent` function"
        identifier_index = {"run_tracked_agent": {"src/kew/dispatch_core.py"}}
        files = predict_files_from_text(
            text, source_files=[], identifier_index=identifier_index
        )
        assert "src/kew/dispatch_core.py" in files

    def test_extracts_module_keywords(self):
        text = "Improve the scheduler to handle parallel dispatch"
        module_index = {"scheduler": {"src/kew/scheduler.py"}}
        files = predict_files_from_text(
            text, source_files=[], module_index=module_index
        )
        assert "src/kew/scheduler.py" in files

    def test_returns_empty_for_vague_text(self):
        text = "Fix the bug that causes issues"
        files = predict_files_from_text(text, source_files=["src/kew/scheduler.py"])
        assert files == set()

    def test_handles_multiple_extensions(self):
        text = "Update worktree.py and the dispatch template dispatch.md.j2"
        files = predict_files_from_text(
            text,
            source_files=[
                "src/kew/worktree.py",
                "src/kew/_templates/dispatch.md.j2",
                "src/kew/config.py",
            ],
        )
        assert "src/kew/worktree.py" in files
        assert "src/kew/_templates/dispatch.md.j2" in files

    def test_case_insensitive_module_matching(self):
        text = "The Scheduler needs improvement"
        module_index = {"scheduler": {"src/kew/scheduler.py"}}
        files = predict_files_from_text(
            text, source_files=[], module_index=module_index
        )
        assert "src/kew/scheduler.py" in files

    def test_bare_directory_word_has_no_repo_sized_fan_out(self):
        source_files = [f"docs/page-{index}.md" for index in range(500)] + [
            f"src/kew/module_{index}.py" for index in range(500)
        ]
        module_index = build_module_index(source_files)

        files = predict_files_from_text(
            "Improve docs for the release",
            source_files=source_files,
            module_index=module_index,
        )

        assert files == set()

    def test_directory_scope_matches_only_direct_children(self):
        source_files = [
            "docs/run.md",
            "docs/config.md",
            "docs/guides/setup.md",
            "src/kew/config.py",
        ]
        module_index = build_module_index(source_files)

        bare_files = predict_files_from_text(
            "Update docs for this behavior",
            source_files=source_files,
            module_index=module_index,
        )
        path_files = predict_files_from_text(
            "Update docs/ for this behavior",
            source_files=source_files,
            module_index=module_index,
        )
        backtick_files = predict_files_from_text(
            "Update `docs` for this behavior",
            source_files=source_files,
            module_index=module_index,
        )

        assert bare_files == set()
        assert path_files == {"docs/run.md", "docs/config.md"}
        assert backtick_files == {"docs/run.md", "docs/config.md"}

    def test_ancestor_directory_scope_has_no_recursive_fan_out(self):
        source_files = [
            "src/kew/scheduler.py",
            "src/kew/tui/app.py",
            "src/kew/graph/driver/tick.py",
        ]
        module_index = build_module_index(source_files)

        files = predict_files_from_text(
            "Update `src/kew` for this behavior",
            source_files=source_files,
            module_index=module_index,
        )
        basename_files = predict_files_from_text(
            "Update `kew` for this behavior",
            source_files=source_files,
            module_index=module_index,
        )

        assert files == {"src/kew/scheduler.py"}
        assert basename_files == set()

    def test_path_scope_matcher_processes_slash_dense_issue_body_quickly(self):
        text = "a/" * 32_767 + "!"

        matches = list(_PATH_SCOPE_RE.finditer(text))

        assert matches == []

        def scan_at(slashes):
            body = "a/" * slashes + "!"
            return lambda: list(_PATH_SCOPE_RE.finditer(body))

        growth = cost_growth(scan_at, 1000)
        assert growth < LINEAR_CEILING, f"{FACTOR}x the slashes cost {growth:.0f}x"


def _make_issue(num: int, title: str = "Test", body: str = "") -> dict:
    return {"number": num, "title": title, "body": body, "labels": [{"name": "ready"}]}


class TestOverlapCoordinatorCheck:
    def test_dispatch_when_no_inflight(self):
        coord = OverlapCoordinator()
        config = DispatchConfig()
        issue = _make_issue(42, title="Fix scheduler.py")
        decision = coord.check(issue, config, source_files=["src/kew/scheduler.py"])
        assert decision.action == "dispatch"

    def test_hold_when_overlap_detected(self):
        coord = OverlapCoordinator()
        config = DispatchConfig()
        coord.on_dispatch(10, {"src/kew/scheduler.py"})
        issue = _make_issue(42, title="Update scheduler.py")
        decision = coord.check(issue, config, source_files=["src/kew/scheduler.py"])
        assert decision.action == "hold"
        assert decision.blocking_issue == 10

    def test_dispatch_when_no_file_overlap(self):
        coord = OverlapCoordinator()
        config = DispatchConfig()
        coord.on_dispatch(10, {"src/kew/scheduler.py"})
        issue = _make_issue(42, title="Fix budget.py")
        decision = coord.check(
            issue, config, source_files=["src/kew/budget.py", "src/kew/scheduler.py"]
        )
        assert decision.action == "dispatch"

    def test_dispatch_when_no_predictions(self):
        coord = OverlapCoordinator()
        config = DispatchConfig()
        coord.on_dispatch(10, {"src/kew/scheduler.py"})
        issue = _make_issue(42, title="Fix the bug")
        decision = coord.check(issue, config, source_files=["src/kew/scheduler.py"])
        assert decision.action == "dispatch"

    def test_strategy_off_always_dispatches(self):
        coord = OverlapCoordinator()
        config = DispatchConfig(overlap=OverlapConfig(strategy="off"))
        coord.on_dispatch(10, {"src/kew/scheduler.py"})
        issue = _make_issue(42, title="Update scheduler.py")
        decision = coord.check(issue, config, source_files=["src/kew/scheduler.py"])
        assert decision.action == "dispatch"

    def test_strategy_warn_dispatches_with_context(self):
        coord = OverlapCoordinator()
        config = DispatchConfig(overlap=OverlapConfig(strategy="warn"))
        coord.on_dispatch(10, {"src/kew/scheduler.py"})
        issue = _make_issue(42, title="Update scheduler.py")
        decision = coord.check(issue, config, source_files=["src/kew/scheduler.py"])
        assert decision.action == "dispatch"
        assert decision.warning_context is not None
        assert "scheduler.py" in decision.warning_context


class TestOverlapCoordinatorHoldRelease:
    def test_on_complete_releases_hold(self):
        coord = OverlapCoordinator()
        coord.on_dispatch(10, {"src/kew/scheduler.py"})
        coord.holds[42] = HoldInfo(
            blocking_issues={10},
            predicted_overlap_files={"src/kew/scheduler.py"},
            held_at=datetime.now(UTC),
        )
        released = coord.on_complete(10, ["src/kew/scheduler.py"])
        assert 42 in released
        assert 42 not in coord.holds
        assert 10 not in coord.in_flight

    def test_on_complete_partial_release_with_multiple_blockers(self):
        coord = OverlapCoordinator()
        coord.on_dispatch(10, {"src/kew/scheduler.py"})
        coord.on_dispatch(11, {"src/kew/config.py"})
        coord.holds[42] = HoldInfo(
            blocking_issues={10, 11},
            predicted_overlap_files={"src/kew/scheduler.py", "src/kew/config.py"},
            held_at=datetime.now(UTC),
        )
        released = coord.on_complete(10, ["src/kew/scheduler.py"])
        assert 42 not in released
        assert 42 in coord.holds
        assert coord.holds[42].blocking_issues == {11}

    def test_on_complete_full_release_after_all_blockers(self):
        coord = OverlapCoordinator()
        coord.on_dispatch(10, {"src/kew/scheduler.py"})
        coord.on_dispatch(11, {"src/kew/config.py"})
        coord.holds[42] = HoldInfo(
            blocking_issues={10, 11},
            predicted_overlap_files={"src/kew/scheduler.py", "src/kew/config.py"},
            held_at=datetime.now(UTC),
        )
        coord.on_complete(10, ["src/kew/scheduler.py"])
        released = coord.on_complete(11, ["src/kew/config.py"])
        assert 42 in released
        assert 42 not in coord.holds

    def test_on_dispatch_tracks_in_flight(self):
        coord = OverlapCoordinator()
        coord.on_dispatch(10, {"a.py"})
        assert 10 in coord.in_flight
        assert coord.in_flight[10] == {"a.py"}

    def test_on_complete_moves_to_recently_merged(self):
        coord = OverlapCoordinator()
        coord.on_dispatch(10, {"a.py"})
        coord.on_complete(10, ["a.py", "b.py"])
        assert 10 not in coord.in_flight
        assert 10 in coord.recently_merged
        assert coord.recently_merged[10].actual_files == ["a.py", "b.py"]

    def test_add_hold_records_state(self):
        coord = OverlapCoordinator()
        coord.add_hold(42, blocking_issues={10}, overlap_files={"a.py"})
        assert 42 in coord.holds
        assert coord.holds[42].blocking_issues == {10}


class TestMergeContextGeneration:
    def test_returns_summary_for_previously_held_issue(self):
        coord = OverlapCoordinator()
        config = DispatchConfig()
        coord.on_dispatch(10, {"src/kew/scheduler.py"})
        coord.add_hold(42, blocking_issues={10}, overlap_files={"src/kew/scheduler.py"})
        coord._hold_history[42] = {10}
        coord.on_complete(10, ["src/kew/scheduler.py", "src/kew/selector.py"])
        context = coord.get_merge_context(42, config)
        assert context is not None
        assert "scheduler.py" in context
        assert "selector.py" in context
        assert "#10" in context

    def test_returns_none_for_unheld_issue(self):
        coord = OverlapCoordinator()
        config = DispatchConfig()
        context = coord.get_merge_context(42, config)
        assert context is None

    def test_returns_none_after_ttl_expired(self):
        coord = OverlapCoordinator()
        config = DispatchConfig(
            overlap=OverlapConfig(
                advanced=OverlapAdvancedConfig(merge_context_ttl_minutes=60)
            )
        )
        coord.on_dispatch(10, {"a.py"})
        coord._hold_history[42] = {10}
        coord.on_complete(10, ["a.py"])
        coord.recently_merged[10].completed_at = datetime.now(UTC) - timedelta(
            minutes=120
        )
        context = coord.get_merge_context(42, config)
        assert context is None

    def test_multiple_blockers_merged_into_context(self):
        coord = OverlapCoordinator()
        config = DispatchConfig()
        coord.on_dispatch(10, {"a.py"})
        coord.on_dispatch(11, {"b.py"})
        coord._hold_history[42] = {10, 11}
        coord.add_hold(42, blocking_issues={10, 11}, overlap_files={"a.py", "b.py"})
        coord.on_complete(10, ["a.py"])
        coord.on_complete(11, ["b.py"])
        context = coord.get_merge_context(42, config)
        assert context is not None
        assert "a.py" in context
        assert "b.py" in context


class TestStaleHoldCleanup:
    @patch("kew.overlap.run")
    @patch("kew.overlap._run_gh_json")
    def test_strip_stale_holds_removes_labels(self, mock_gh_json, mock_run):
        mock_gh_json.return_value = [
            {"number": 42, "labels": [{"name": "ready"}, {"name": "overlap-hold"}]},
            {"number": 43, "labels": [{"name": "ready"}, {"name": "overlap-hold"}]},
        ]
        mock_run.return_value = MagicMock(returncode=0)
        config = DispatchConfig()
        strip_stale_holds(config)
        assert mock_run.call_count == 2

    @patch("kew.overlap.run")
    @patch("kew.overlap._run_gh_json")
    def test_strip_stale_holds_noop_when_none(self, mock_gh_json, mock_run):
        mock_gh_json.return_value = []
        config = DispatchConfig()
        strip_stale_holds(config)
        mock_run.assert_not_called()
