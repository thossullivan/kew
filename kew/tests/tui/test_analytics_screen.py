"""Tests for analytics screen data durability features."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from kew.tui.actions import handle_export, handle_purge


class TestInsufficientDataWarning:
    """The analytics screen shows a warning when < 5 completed runs."""

    def test_warning_shown_when_insufficient(self):
        """_update_stat_cards includes warning text when runs < 5."""
        from kew.tui.screens.analytics import AnalyticsScreen

        screen = AnalyticsScreen.__new__(AnalyticsScreen)
        # _insufficient_data is set during _async_refresh; simulate it
        screen._insufficient_data = True
        warning = screen._render_insufficient_data_warning()
        assert "Insufficient data" in warning

    def test_no_warning_when_sufficient(self):
        """No warning banner when runs >= 5."""
        from kew.tui.screens.analytics import AnalyticsScreen

        screen = AnalyticsScreen.__new__(AnalyticsScreen)
        screen._insufficient_data = False
        warning = screen._render_insufficient_data_warning()
        assert warning == ""


class TestHandleExport:
    """Export action handler writes run data to a file."""

    def test_export_no_store_is_noop(self):
        app = MagicMock()
        app.state = None
        handle_export(app)
        app.push_screen.assert_not_called()
        app.notify.assert_not_called()

    def test_export_pushes_confirm_dialog(self):
        app = MagicMock()
        state = MagicMock()
        store = MagicMock()
        store.list_all.return_value = [MagicMock(), MagicMock()]
        state.store = store
        app.state = state
        handle_export(app)
        app.push_screen.assert_called_once()

    def test_export_callback_writes_json(self, tmp_path):
        """When confirmed, export writes JSON to file and notifies."""
        from kew.run_store import RunRecord

        app = MagicMock()
        state = MagicMock()
        store = MagicMock()

        record = RunRecord(
            run_id="r1",
            issue_num=1,
            branch="main",
            status="success",
            exit_code=0,
            started_at=datetime(2025, 1, 1, tzinfo=UTC),
            finished_at=datetime(2025, 1, 1, 0, 5, tzinfo=UTC),
            duration_seconds=300,
            total_tokens=1000,
            input_tokens=500,
            output_tokens=500,
            estimated_cost_usd=0.50,
        )
        store.list_all.return_value = [record]
        state.store = store
        app.state = state

        handle_export(app)
        # Extract the callback
        callback = app.push_screen.call_args[1]["callback"]

        output_file = tmp_path / "export.json"
        with patch(
            "kew.tui.actions._export_output_path", return_value=str(output_file)
        ):
            callback(True)

        assert output_file.exists()
        content = output_file.read_text()
        assert "r1" in content

    def test_export_callback_handles_write_error(self, tmp_path):
        """When file write fails, notify error and don't crash."""
        from kew.run_store import RunRecord

        app = MagicMock()
        state = MagicMock()
        store = MagicMock()

        record = RunRecord(
            run_id="r1",
            issue_num=1,
            branch="main",
            status="success",
            exit_code=0,
            started_at=datetime(2025, 1, 1, tzinfo=UTC),
            finished_at=datetime(2025, 1, 1, 0, 5, tzinfo=UTC),
            duration_seconds=300,
            total_tokens=1000,
            input_tokens=500,
            output_tokens=500,
            estimated_cost_usd=0.50,
        )
        store.list_all.return_value = [record]
        state.store = store
        app.state = state

        handle_export(app)
        callback = app.push_screen.call_args[1]["callback"]

        # Point to a non-existent directory to trigger OSError
        bad_path = str(tmp_path / "no_such_dir" / "export.json")
        with patch("kew.tui.actions._export_output_path", return_value=bad_path):
            callback(True)

        app.notify.assert_called_once()
        assert "Export failed" in app.notify.call_args[0][0]

    def test_export_callback_cancel_is_noop(self):
        app = MagicMock()
        state = MagicMock()
        store = MagicMock()
        store.list_all.return_value = [MagicMock()]
        state.store = store
        app.state = state

        handle_export(app)
        callback = app.push_screen.call_args[1]["callback"]
        callback(False)
        # No file written, no notification
        app.notify.assert_not_called()


class TestHandlePurge:
    """Purge action handler deletes old records with confirmation."""

    def test_purge_no_store_is_noop(self):
        app = MagicMock()
        app.state = None
        handle_purge(app)
        app.push_screen.assert_not_called()

    def test_purge_invalid_days_notifies_error(self):
        app = MagicMock()
        handle_purge(app, days=0)
        app.notify.assert_called_once()
        assert "greater than 0" in app.notify.call_args[0][0]
        app.push_screen.assert_not_called()

    def test_purge_pushes_confirm_dialog_with_count(self):
        app = MagicMock()
        state = MagicMock()
        store = MagicMock()
        store.list_all.return_value = [MagicMock()] * 10
        store.list_since.return_value = [MagicMock()] * 7
        state.store = store
        app.state = state
        handle_purge(app, days=30)
        app.push_screen.assert_called_once()
        # Confirm dialog prompt should contain the count
        dialog = app.push_screen.call_args[0][0]
        assert "3" in dialog.prompt

    def test_purge_callback_deletes_and_notifies(self):
        app = MagicMock()
        state = MagicMock()
        store = MagicMock()
        store.list_all.return_value = [MagicMock()] * 10
        store.list_since.return_value = [MagicMock()] * 5
        store.purge_older_than.return_value = 5
        state.store = store
        app.state = state

        handle_purge(app, days=30)
        callback = app.push_screen.call_args[1]["callback"]
        callback(True)
        store.purge_older_than.assert_called_once_with(30)
        app.notify.assert_called_once()
        assert "5" in app.notify.call_args[0][0]

    def test_purge_callback_handles_storage_error(self):
        app = MagicMock()
        state = MagicMock()
        store = MagicMock()
        store.list_all.return_value = [MagicMock()] * 5
        store.list_since.return_value = []
        store.purge_older_than.side_effect = RuntimeError("db locked")
        state.store = store
        app.state = state

        handle_purge(app, days=30)
        callback = app.push_screen.call_args[1]["callback"]
        callback(True)
        app.notify.assert_called_once()
        assert "Purge failed" in app.notify.call_args[0][0]

    def test_purge_callback_cancel_is_noop(self):
        app = MagicMock()
        state = MagicMock()
        store = MagicMock()
        store.list_all.return_value = [MagicMock()] * 5
        store.list_since.return_value = [MagicMock()] * 3
        state.store = store
        app.state = state

        handle_purge(app, days=30)
        callback = app.push_screen.call_args[1]["callback"]
        callback(False)
        store.purge_older_than.assert_not_called()


class TestAnalyticsPanelInsufficientData:
    """The compact analytics panel shows a warning for insufficient data."""

    def test_insufficient_data_warning_in_compact(self):
        from kew.tui.widgets.analytics_panel import format_analytics_compact

        stats = {
            "runs": 3,
            "success_rate": 66.7,
            "avg_cost": 0.50,
            "avg_duration": 300.0,
            "total_spend": 1.50,
        }
        result = format_analytics_compact(stats, daily_budget=25.0)
        assert "Insufficient data" in result

    def test_no_warning_when_sufficient_data_compact(self):
        from kew.tui.widgets.analytics_panel import format_analytics_compact

        stats = {
            "runs": 5,
            "success_rate": 80.0,
            "avg_cost": 0.34,
            "avg_duration": 372.0,
            "total_spend": 1.70,
        }
        result = format_analytics_compact(stats, daily_budget=25.0)
        assert "Insufficient data" not in result
