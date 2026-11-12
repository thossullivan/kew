"""Tests for AgentRunner protocol data types."""

from kew.runners.protocol import ResultClassification, StreamEvent


class TestResultClassification:
    def test_defaults(self):
        rc = ResultClassification(category="transient")
        assert rc.category == "transient"
        assert rc.session_id is None
        assert rc.subtype == ""
        assert rc.stop_reason == ""

    def test_full_construction(self):
        rc = ResultClassification(
            category="budget_exceeded",
            session_id="abc-123",
            subtype="error_max_budget_usd",
            stop_reason="tool_use",
        )
        assert rc.category == "budget_exceeded"
        assert rc.session_id == "abc-123"


class TestStreamEvent:
    def test_defaults(self):
        event = StreamEvent(raw_line="hello")
        assert event.raw_line == "hello"
        assert event.event_type == ""
        assert event.message == ""
        assert event.input_tokens == 0
        assert event.output_tokens == 0
        assert event.tool_uses == 0
        assert event.metadata == {}

    def test_metadata_isolation(self):
        """Each instance gets its own metadata dict."""
        e1 = StreamEvent(raw_line="a")
        e2 = StreamEvent(raw_line="b")
        e1.metadata["key"] = "val"
        assert "key" not in e2.metadata

    def test_usage_event(self):
        event = StreamEvent(
            raw_line='{"type":"system"}',
            event_type="usage",
            input_tokens=100,
            output_tokens=50,
            tool_uses=3,
        )
        assert event.input_tokens == 100
        assert event.output_tokens == 50
        assert event.tool_uses == 3
