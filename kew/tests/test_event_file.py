"""Tests for event file writing in the orchestrator."""

from __future__ import annotations

import io
import json
import selectors
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from conftest import MockSelector as _MockSelector

from kew.config import DispatchConfig
from kew.orchestrator import _emit_stream_event, _write_event, run_agent
from kew.runners.protocol import ResultClassification, StreamEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_events(fh: io.StringIO) -> list[dict]:
    """Parse all JSONL events from a StringIO buffer."""
    fh.seek(0)
    return [json.loads(line) for line in fh if line.strip()]


# ---------------------------------------------------------------------------
# TestEventFileWriter
# ---------------------------------------------------------------------------


class TestEventFileWriter:
    def test_event_file_written_when_path_provided(self, tmp_path):
        """Write started/tokens/activity/finished events; verify 4 events with
        correct types and key fields."""
        fh = io.StringIO()

        _write_event(
            fh, "started", runner="claude-code", model="claude-sonnet-4", issue=42
        )
        _write_event(fh, "tokens", input=1200, output=340, cost_usd=0.02)
        _write_event(fh, "activity", msg="Reading src/config.py")
        _write_event(fh, "finished", exit_code=0, total_tokens=1540, cost_usd=0.02)

        events = _read_events(fh)
        assert len(events) == 4

        assert events[0]["type"] == "started"
        assert events[0]["runner"] == "claude-code"
        assert events[0]["model"] == "claude-sonnet-4"
        assert events[0]["issue"] == 42

        assert events[1]["type"] == "tokens"
        assert events[1]["input"] == 1200
        assert events[1]["output"] == 340
        assert events[1]["cost_usd"] == pytest.approx(0.02)

        assert events[2]["type"] == "activity"
        assert events[2]["msg"] == "Reading src/config.py"

        assert events[3]["type"] == "finished"
        assert events[3]["exit_code"] == 0
        assert events[3]["total_tokens"] == 1540

    def test_all_events_have_timestamp(self, tmp_path):
        """Every event written must include a 'ts' float field."""
        fh = io.StringIO()
        _write_event(fh, "started", runner="fake", model="m", issue=1)

        events = _read_events(fh)
        assert len(events) == 1
        assert "ts" in events[0]
        assert isinstance(events[0]["ts"], float)

    def test_no_event_file_when_none(self):
        """_write_event(None, ...) must not raise."""
        _write_event(None, "started", runner="fake", model="m", issue=1)

    def test_pid_event_written(self):
        """_write_event can write a pid event with an integer pid field."""
        fh = io.StringIO()
        _write_event(fh, "started", runner="claude-code", model="sonnet", issue=42)
        _write_event(fh, "pid", pid=12345)
        events = _read_events(fh)
        assert len(events) == 2
        assert events[1]["type"] == "pid"
        assert events[1]["pid"] == 12345


# ---------------------------------------------------------------------------
# TestStreamEventToEventFile
# ---------------------------------------------------------------------------


class TestStreamEventToEventFile:
    def test_usage_event_maps_to_tokens(self):
        """StreamEvent(event_type='usage') → tokens event with input/output/cost_usd."""
        fh = io.StringIO()
        event = StreamEvent(
            raw_line="",
            event_type="usage",
            input_tokens=0,
            output_tokens=0,
        )
        _emit_stream_event(
            fh,
            event,
            accumulated_input=1200,
            accumulated_output=340,
            model="claude-sonnet-4",
        )

        events = _read_events(fh)
        assert len(events) == 1
        assert events[0]["type"] == "tokens"
        assert events[0]["input"] == 1200
        assert events[0]["output"] == 340
        assert "cost_usd" in events[0]
        assert isinstance(events[0]["cost_usd"], float)

    def test_activity_event_carries_only_the_sanitized_line(self):
        """StreamEvent(event_type='activity') → activity event whose msg is the
        vetted progress line, never the provider's own message (I06T)."""
        fh = io.StringIO()
        event = StreamEvent(
            raw_line="",
            event_type="activity",
            message="Editing src/main.py and here is a secret",
        )
        _emit_stream_event(
            fh,
            event,
            accumulated_input=0,
            accumulated_output=0,
            model="claude-sonnet-4",
            safe_line="step 2 tokens=90 tools=Edit",
        )

        events = _read_events(fh)
        assert len(events) == 1
        assert events[0]["type"] == "activity"
        assert events[0]["msg"] == "step 2 tokens=90 tools=Edit"
        assert "secret" not in events[0]["msg"]

    def test_activity_event_without_a_sanitized_line_is_dropped(self):
        """No vetted line → no event, rather than the raw provider message."""
        fh = io.StringIO()
        event = StreamEvent(raw_line="", event_type="activity", message="raw prose")
        _emit_stream_event(
            fh,
            event,
            accumulated_input=0,
            accumulated_output=0,
            model="claude-sonnet-4",
        )

        assert _read_events(fh) == []

    def test_tool_use_event_keeps_the_name_and_drops_the_target(self):
        """StreamEvent(event_type='tool_use') → tool_use with the sanitized tool
        name; the raw target (provider-controlled input) is dropped (I06T)."""
        fh = io.StringIO()
        event = StreamEvent(
            raw_line="",
            event_type="tool_use",
            message="Edit",
            metadata={"target": "src/config.py"},
        )
        _emit_stream_event(
            fh,
            event,
            accumulated_input=0,
            accumulated_output=0,
            model="claude-sonnet-4",
        )

        events = _read_events(fh)
        assert len(events) == 1
        assert events[0]["type"] == "tool_use"
        assert events[0]["tool"] == "Edit"
        assert events[0]["target"] == ""

    def test_result_event_ignored(self):
        """StreamEvent(event_type='result') → nothing written."""
        fh = io.StringIO()
        event = StreamEvent(raw_line="", event_type="result", message="done")
        _emit_stream_event(
            fh,
            event,
            accumulated_input=0,
            accumulated_output=0,
            model="claude-sonnet-4",
        )

        events = _read_events(fh)
        assert len(events) == 0

    def test_unknown_event_type_written_as_sanitized_activity(self, tmp_path):
        from kew.orchestrator import _emit_stream_event

        event_file = tmp_path / "test.events.jsonl"
        stream_event = StreamEvent(
            raw_line="", event_type="error", message="Something went wrong: secret"
        )
        with open(event_file, "a") as fh:
            _emit_stream_event(
                fh,
                stream_event,
                accumulated_input=0,
                accumulated_output=0,
                model="",
                safe_line="step 4 tokens=200",
            )
        lines = event_file.read_text().strip().split("\n")
        events = [json.loads(line) for line in lines if line.strip()]
        assert len(events) == 1
        assert events[0]["type"] == "activity"
        assert events[0]["msg"] == "step 4 tokens=200"
        assert "secret" not in events[0]["msg"]


# ---------------------------------------------------------------------------
# TestRunAgentEventFileIntegration
# ---------------------------------------------------------------------------


class FakeRunner:
    """Minimal AgentRunner double for event file integration tests."""

    required_credentials = ()

    def __init__(self, stream_events=None):
        self._stream_events = list(stream_events or [])

    @property
    def name(self):
        return "fake"

    def build_command(self, *, prompt, config, env, model=""):
        return ["echo", "hi"]

    def parse_line(self, line):
        if self._stream_events:
            return [self._stream_events.pop(0)]
        return []

    def classify_result(self, *, exit_code, last_output, config):
        return ResultClassification(category="permanent")

    def supports_resume(self):
        return False

    def build_resume_command(self, *, session_id, prompt, config, env, model=""):
        raise NotImplementedError

    def enrich_result(self, result, config):
        pass


def _make_mock_popen(exit_code=0, lines=None):
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.stdout = MagicMock()
    mock_proc.returncode = exit_code
    mock_proc.pid = 99999
    mock_proc.poll.return_value = exit_code
    stdout_lines = list(lines or [])
    stdout_lines.append("")
    mock_proc.stdout.readline.side_effect = stdout_lines
    return mock_proc


class TestRunAgentEmitEvents:
    def test_event_file_created_when_emit_events_true(self, tmp_path):
        """run_agent with emit_events=True should create a .events.jsonl file."""
        runner = FakeRunner()
        mock_proc = _make_mock_popen(exit_code=0, lines=["output\n"])
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
        ):
            result = run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=42,
                emit_events=True,
            )

        assert result.event_file is not None
        assert result.event_file.exists()
        assert result.event_file.suffix == ".jsonl"

    def test_event_file_has_started_and_finished(self, tmp_path):
        """Event file must contain 'started' as first event and 'finished' as last."""
        runner = FakeRunner()
        mock_proc = _make_mock_popen(exit_code=0, lines=["output\n"])
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
        ):
            result = run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=42,
                emit_events=True,
            )

        assert result.event_file is not None
        events = [
            json.loads(line)
            for line in result.event_file.read_text().splitlines()
            if line.strip()
        ]
        assert events[0]["type"] == "started"
        assert events[-1]["type"] == "finished"
        assert events[-1]["exit_code"] == 0

    def test_no_event_file_when_emit_events_false(self, tmp_path):
        """run_agent with emit_events=False (default) should not create event file."""
        runner = FakeRunner()
        mock_proc = _make_mock_popen(exit_code=0, lines=["output\n"])
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
        ):
            result = run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=42,
            )

        assert result.event_file is None

    def test_stream_events_written_to_event_file(self, tmp_path):
        """usage → tokens; an assistant turn → an activity carrying the SANITIZED
        progress line the derivation vetted, never the raw provider message."""
        # The activity line's raw JSON is a real assistant turn, so the
        # derivation produces a safe_line for it. The message body below is the
        # provider prose that must NOT survive into the event file.
        assistant_json = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "usage": {"input_tokens": 500, "output_tokens": 100},
                    "content": [{"type": "tool_use", "name": "Edit"}],
                },
            }
        )
        stream_events = [
            StreamEvent(
                raw_line="l1",
                event_type="usage",
                input_tokens=500,
                output_tokens=100,
            ),
            StreamEvent(
                raw_line=assistant_json,
                event_type="activity",
                message="Reading file /secrets/api.key = sk-live-SECRET",
            ),
        ]
        runner = FakeRunner(stream_events=stream_events)
        mock_proc = _make_mock_popen(
            exit_code=0, lines=["line1\n", assistant_json + "\n"]
        )
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
        ):
            result = run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=42,
                emit_events=True,
                model="claude-sonnet-4",
            )

        assert result.event_file is not None
        body = result.event_file.read_text()
        assert "sk-live-SECRET" not in body
        assert "/secrets/api.key" not in body
        events = [json.loads(line) for line in body.splitlines() if line.strip()]
        types = [e["type"] for e in events]
        assert "tokens" in types
        assert "activity" in types
        activity_events = [e for e in events if e["type"] == "activity"]
        assert activity_events[0]["msg"].startswith("step ")
        assert "Reading file" not in activity_events[0]["msg"]


# ---------------------------------------------------------------------------
# TestSelectBasedReads
# ---------------------------------------------------------------------------


def test_select_loop_breaks_on_dead_process():
    """The select-based loop breaks when the subprocess dies without closing stdout."""
    # Create a subprocess that exits immediately without writing
    proc = subprocess.Popen(
        ["python3", "-c", "import sys; sys.exit(1)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    proc.wait()  # Ensure process is dead

    # Verify that select detects no more data and we can check poll()
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    ready = sel.select(timeout=1)
    if ready:
        line = proc.stdout.readline()
        assert line == ""  # EOF
    else:
        assert proc.poll() is not None
    sel.unregister(proc.stdout)
    sel.close()
    proc.stdout.close()


def test_select_loop_reads_lines_normally():
    """The select-based loop reads lines from a subprocess normally."""
    # Subprocess that prints 3 lines and exits
    proc = subprocess.Popen(
        ["python3", "-c", "print('line1'); print('line2'); print('line3')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines = []
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    try:
        while True:
            ready = sel.select(timeout=5)
            if ready:
                line = proc.stdout.readline()
                if not line:
                    break
                lines.append(line.strip())
            else:
                if proc.poll() is not None:
                    break
    finally:
        sel.unregister(proc.stdout)
        sel.close()
        proc.stdout.close()

    assert lines == ["line1", "line2", "line3"]
    assert proc.wait() == 0
