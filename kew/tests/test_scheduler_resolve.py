"""Tests for auto-resolve wiring in the dispatch scheduler."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from kew.config import DispatchConfig, ParallelConfig, ResolveConfig
from kew.scheduler import DispatchScheduler


def _make_pr(number: int, mergeable: str = "CONFLICTING") -> dict[str, Any]:
    """Create a minimal PR dict for testing."""
    return {
        "number": number,
        "headRefName": f"agent/issue-{number}",
        "baseRefName": "main",
        "title": f"PR #{number}",
        "body": f"Closes #{number}",
        "state": "OPEN",
        "mergeable": mergeable,
    }


class TestAutoResolveGate:
    """Auto-resolve is a no-op when config.resolve.auto is False."""

    def test_resolve_disabled_by_default(self) -> None:
        config = DispatchConfig()
        scheduler = DispatchScheduler(config)
        assert config.resolve.auto is False
        # resolve_in_flight should still be initialized
        assert scheduler.resolve_in_flight == set()

    @patch("kew.scheduler.subprocess_run")
    def test_no_pr_poll_when_auto_disabled(self, mock_run: MagicMock) -> None:
        config = DispatchConfig(resolve=ResolveConfig(auto=False))
        scheduler = DispatchScheduler(config)
        result = scheduler._poll_conflicting_prs()
        assert result == []
        mock_run.assert_not_called()


class TestPollConflictingPRs:
    """Test the PR polling logic for conflicting agent PRs."""

    @patch("kew.scheduler.subprocess_run")
    def test_returns_conflicting_prs(self, mock_run: MagicMock) -> None:
        config = DispatchConfig(resolve=ResolveConfig(auto=True))
        scheduler = DispatchScheduler(config)

        pr1 = _make_pr(10, "CONFLICTING")
        pr2 = _make_pr(20, "MERGEABLE")
        pr3 = _make_pr(30, "CONFLICTING")

        import json

        mock_run.return_value = MagicMock(
            stdout=json.dumps([pr1, pr2, pr3]),
            returncode=0,
        )

        result = scheduler._poll_conflicting_prs()
        assert len(result) == 2
        assert result[0]["number"] == 10
        assert result[1]["number"] == 30

    @patch("kew.scheduler.subprocess_run")
    def test_excludes_already_in_flight(self, mock_run: MagicMock) -> None:
        config = DispatchConfig(resolve=ResolveConfig(auto=True))
        scheduler = DispatchScheduler(config)
        scheduler.resolve_in_flight.add(10)

        pr1 = _make_pr(10, "CONFLICTING")
        pr2 = _make_pr(30, "CONFLICTING")

        import json

        mock_run.return_value = MagicMock(
            stdout=json.dumps([pr1, pr2]),
            returncode=0,
        )

        result = scheduler._poll_conflicting_prs()
        assert len(result) == 1
        assert result[0]["number"] == 30

    @patch("kew.scheduler.subprocess_run")
    def test_returns_empty_on_error(self, mock_run: MagicMock) -> None:
        import subprocess

        config = DispatchConfig(resolve=ResolveConfig(auto=True))
        scheduler = DispatchScheduler(config)
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")

        result = scheduler._poll_conflicting_prs()
        assert result == []


class TestResolveDispatch:
    """Test that resolve dispatches go through the worker pool."""

    @patch("kew.scheduler.subprocess_run")
    @patch("kew.scheduler.time.sleep")
    @patch("kew.scheduler.select_next_issue", return_value=None)
    def test_dispatches_resolve_for_conflicting_pr(
        self,
        mock_select: MagicMock,
        mock_sleep: MagicMock,
        mock_run: MagicMock,
        capsys,
    ) -> None:
        """Scheduler dispatches resolve for conflicting PRs when auto=true."""
        import json

        config = DispatchConfig(
            resolve=ResolveConfig(auto=True),
            parallel=ParallelConfig(max_workers=2),
        )
        scheduler = DispatchScheduler(config)

        pr = _make_pr(10, "CONFLICTING")
        mock_run.return_value = MagicMock(
            stdout=json.dumps([pr]),
            returncode=0,
        )

        # Run one iteration then break
        scheduler._shutdown = False

        with (
            patch.object(scheduler, "_reap_completed"),
            patch("kew.scheduler.ProcessPoolExecutor") as mock_pool_cls,
        ):
            mock_pool = MagicMock()
            mock_future = MagicMock()
            mock_future.done.side_effect = [False, True]
            mock_future.result.return_value = SimpleNamespace(
                kind="executed", exit_code=0
            )
            mock_pool.submit.return_value = mock_future
            mock_pool_cls.return_value = mock_pool

            def stop_after_dispatch(*args: Any) -> None:
                scheduler._shutdown = True

            mock_sleep.side_effect = stop_after_dispatch
            mock_select.side_effect = [None, None, None]

            scheduler.run_loop()

            # Verify submit was called with _worker_resolve
            assert mock_pool.submit.called
            call_args = mock_pool.submit.call_args
            assert call_args[0][0].__name__ == "_worker_resolve"
        assert "Resolve worker for PR #10 completed." in capsys.readouterr().err

    def test_resolve_in_flight_prevents_duplicate(self) -> None:
        """PRs already being resolved are not dispatched again."""
        config = DispatchConfig(
            resolve=ResolveConfig(auto=True),
            parallel=ParallelConfig(max_workers=2),
        )
        scheduler = DispatchScheduler(config)
        scheduler.resolve_in_flight.add(10)
        assert scheduler.is_resolve_in_flight(10) is True
        assert scheduler.is_resolve_in_flight(20) is False


class TestResolveReaping:
    """Test that completed resolve futures are properly reaped."""

    def test_reap_resolve_removes_from_in_flight(self, capsys) -> None:
        config = DispatchConfig(resolve=ResolveConfig(auto=True))
        scheduler = DispatchScheduler(config)
        scheduler.resolve_in_flight.add(10)

        mock_future = MagicMock()
        mock_future.done.return_value = True
        mock_future.result.return_value = SimpleNamespace(
            kind="executed", exit_code=0
        )  # exit code

        resolve_futures = {mock_future: 10}
        scheduler._reap_resolve_completed(resolve_futures)

        assert 10 not in scheduler.resolve_in_flight
        assert mock_future not in resolve_futures
        assert "Resolve worker for PR #10 completed." in capsys.readouterr().err

    def test_reap_resolve_handles_exception(self) -> None:
        config = DispatchConfig(resolve=ResolveConfig(auto=True))
        scheduler = DispatchScheduler(config)
        scheduler.resolve_in_flight.add(10)

        mock_future = MagicMock()
        mock_future.done.return_value = True
        mock_future.result.side_effect = Exception("worker crashed")

        resolve_futures = {mock_future: 10}
        scheduler._reap_resolve_completed(resolve_futures)

        assert 10 not in scheduler.resolve_in_flight
        assert mock_future not in resolve_futures


class TestResolveCapacity:
    """Resolve workers share capacity with issue dispatch workers."""

    def test_resolve_counts_toward_capacity(self) -> None:
        config = DispatchConfig(
            resolve=ResolveConfig(auto=True),
            parallel=ParallelConfig(max_workers=2),
        )
        scheduler = DispatchScheduler(config)
        scheduler.in_flight.add(1)
        scheduler.resolve_in_flight.add(10)
        # Both issue and resolve workers count toward capacity
        assert scheduler.has_capacity() is False

    def test_available_slots_accounts_for_resolve(self) -> None:
        config = DispatchConfig(
            resolve=ResolveConfig(auto=True),
            parallel=ParallelConfig(max_workers=3),
        )
        scheduler = DispatchScheduler(config)
        scheduler.in_flight.add(1)
        scheduler.resolve_in_flight.add(10)
        assert scheduler.available_slots == 1
