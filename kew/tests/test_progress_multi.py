"""Tests for multi-worker progress display."""

from kew.progress import format_worker_status


class TestFormatWorkerStatus:
    """format_worker_status renders a compact per-worker line."""

    def test_single_worker_running(self):
        workers = {
            42: {
                "status": "running",
                "elapsed_min": 3,
                "description": "Task 5: knowledge fragments",
            },
        }
        lines = format_worker_status(workers)
        assert len(lines) == 1
        assert "#42" in lines[0]
        assert "3m" in lines[0]
        assert "knowledge fragments" in lines[0]

    def test_multiple_workers(self):
        workers = {
            42: {"status": "running", "elapsed_min": 3, "description": "Task 5"},
            43: {"status": "running", "elapsed_min": 5, "description": "Task 17"},
            44: {"status": "initializing", "elapsed_min": 0, "description": ""},
        }
        lines = format_worker_status(workers)
        assert len(lines) == 3

    def test_empty_workers(self):
        lines = format_worker_status({})
        assert lines == []

    def test_completed_worker_shows_done(self):
        workers = {
            42: {"status": "completed", "elapsed_min": 6, "description": "Done"},
        }
        lines = format_worker_status(workers)
        assert "completed" in lines[0].lower() or "done" in lines[0].lower()

    def test_failed_worker_shows_failed(self):
        workers = {
            42: {"status": "failed", "elapsed_min": 3, "description": "Build error"},
        }
        lines = format_worker_status(workers)
        assert len(lines) == 1
        assert "failed" in lines[0].lower()
        assert "Build error" in lines[0]
