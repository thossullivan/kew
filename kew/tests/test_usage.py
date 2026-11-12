from kew.runners.protocol import StreamEvent
from kew.usage import UsageTotals, accumulate_usage, event_contributes_usage


def test_usage_event_with_tokens_contributes():
    ev = StreamEvent(raw_line="", event_type="usage", input_tokens=10, output_tokens=5)
    assert event_contributes_usage(ev) is True


def test_step_event_never_contributes():
    # claude/opencode emit a paired usage+step; counting both double-counts (#310)
    ev = StreamEvent(raw_line="", event_type="step", input_tokens=10, output_tokens=5)
    assert event_contributes_usage(ev) is False


def test_zero_token_event_does_not_contribute():
    ev = StreamEvent(raw_line="", event_type="usage", input_tokens=0, output_tokens=0)
    assert event_contributes_usage(ev) is False


def test_tokenless_tool_event_contributes():
    ev = StreamEvent(raw_line="", event_type="tool_use", tool_uses=1)
    assert event_contributes_usage(ev) is True
    assert accumulate_usage([ev]).tool_uses == 1


def test_init_event_does_not_contribute():
    ev = StreamEvent(raw_line="", event_type="init", metadata={"model": "claude-x"})
    assert event_contributes_usage(ev) is False


def test_accumulate_skips_step_no_double_count():
    events = [
        StreamEvent(
            raw_line="",
            event_type="usage",
            input_tokens=10,
            output_tokens=5,
            cache_creation_tokens=2,
            cache_read_tokens=1,
        ),
        StreamEvent(
            raw_line="",
            event_type="step",
            input_tokens=10,
            output_tokens=5,
            cache_creation_tokens=2,
            cache_read_tokens=1,
        ),
    ]
    totals = accumulate_usage(events)
    assert totals == UsageTotals(10, 5, 2, 1, 0)


def test_accumulate_empty_is_zero():
    assert accumulate_usage([]) == UsageTotals(0, 0, 0, 0, 0)
