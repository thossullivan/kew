import logging
import math

from kew.ingest.claude_code import (
    COST_METRIC,
    RUNNER,
    TOKEN_METRIC,
    assemble,
)
from kew.ingest.otlp import DataPoint


def _tok(
    type_,
    value,
    *,
    model="claude-opus-4-8",
    session="s1",
    t="100",
    qs="main",
    agent_name=None,
):
    attrs = {
        "type": type_,
        "model": model,
        "session.id": session,
        "query_source": qs,
    }
    if agent_name is not None:
        attrs["agent.name"] = agent_name
    return DataPoint(
        metric_name=TOKEN_METRIC,
        attributes=attrs,
        value=value,
        temporality=1,
        time_unix_nano=t,
    )


def _cost(
    value,
    *,
    model="claude-opus-4-8",
    session="s1",
    t="100",
    qs="main",
    agent_name=None,
):
    attrs = {"model": model, "session.id": session, "query_source": qs}
    if agent_name is not None:
        attrs["agent.name"] = agent_name
    return DataPoint(
        metric_name=COST_METRIC,
        attributes=attrs,
        value=value,
        temporality=1,
        time_unix_nano=t,
    )


def test_assemble_pivots_types_and_joins_cost():
    points = [
        _tok("input", 100),
        _tok("output", 50),
        _tok("cacheRead", 5),
        _tok("cacheCreation", 10),
        _cost(0.42),
    ]
    res = assemble(points, ingest_source="file:x")
    assert len(res.records) == 1
    r = res.records[0]
    assert r.runner == RUNNER
    assert (r.input_tokens, r.output_tokens) == (100, 50)
    assert (r.cache_read_tokens, r.cache_creation_tokens) == (5, 10)
    assert r.cost_usd == 0.42
    assert r.model == "claude-opus-4-8"
    assert r.query_source == "main"
    assert r.agent_name is None
    assert {TOKEN_METRIC, COST_METRIC} <= res.seen_metrics


def test_assemble_separates_main_and_subagent():
    points = [
        _tok("input", 100, qs="main"),
        _tok("input", 30, qs="subagent", agent_name="custom"),
        _cost(0.10, qs="main"),
        _cost(0.05, qs="subagent", agent_name="custom"),
    ]
    res = assemble(points, ingest_source="file:x")
    assert len(res.records) == 2
    by_qs = {r.query_source: r for r in res.records}
    assert by_qs["main"].input_tokens == 100
    assert by_qs["main"].cost_usd == 0.10
    assert by_qs["main"].agent_name is None
    assert by_qs["subagent"].input_tokens == 30
    assert by_qs["subagent"].cost_usd == 0.05
    assert by_qs["subagent"].agent_name == "custom"
    assert len({r.dedup_key for r in res.records}) == 2


def test_assemble_separates_auxiliary():
    points = [
        _tok("input", 100, qs="main"),
        _tok("input", 50, qs="auxiliary"),
        _tok("input", 30, qs="subagent", agent_name="custom"),
    ]
    res = assemble(points, ingest_source="file:x")
    assert len(res.records) == 3
    by_qs = {r.query_source: r for r in res.records}
    assert set(by_qs.keys()) == {"main", "subagent", "auxiliary"}


def test_assemble_distinct_export_instants_are_distinct_rows():
    points = [_tok("input", 100, t="100"), _tok("input", 100, t="200")]
    res = assemble(points, ingest_source="file:x")
    assert len(res.records) == 2
    assert len({r.dedup_key for r in res.records}) == 2  # C2 regression guard


def test_assemble_dedup_key_is_identity_not_values():
    # same identity, different values -> same key (so a retry dedups in store)
    a = assemble([_tok("input", 100)], ingest_source="file:x").records[0]
    b = assemble([_tok("input", 999)], ingest_source="file:x").records[0]
    assert a.dedup_key == b.dedup_key


def test_assemble_skips_cumulative_and_unknown_type(caplog):
    cumulative = _tok("input", 100)
    cumulative = DataPoint(**{**cumulative.__dict__, "temporality": 2})
    with caplog.at_level(logging.WARNING):
        res = assemble([cumulative, _tok("weird", 5)], ingest_source="file:x")
    assert res.records == []
    assert res.skipped == 2
    assert any("cost" in m.lower() for m in caplog.messages)


def test_cumulative_cost_not_counted_as_seen(caplog):
    """Cumulative cost point must not add COST_METRIC to seen, so the
    missing-cost warning fires and cost_usd stays 0.0 (bug-fix regression guard).
    """
    cumulative_cost = DataPoint(**{**_cost(0.5).__dict__, "temporality": 2})
    with caplog.at_level(logging.WARNING):
        res = assemble([_tok("input", 100), cumulative_cost], ingest_source="file:x")
    assert res.records[0].cost_usd == 0.0
    assert COST_METRIC not in res.seen_metrics
    assert any("cost" in m.lower() for m in caplog.messages)


def test_assemble_missing_cost_yields_zero_and_warns(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        res = assemble([_tok("input", 100)], ingest_source="file:x")
    assert res.records[0].cost_usd == 0.0
    assert COST_METRIC not in res.seen_metrics
    assert any("cost" in m.lower() for m in caplog.messages)


def test_assemble_clamps_negative_and_nonfinite():
    res = assemble([_tok("input", -50), _cost(math.inf)], ingest_source="file:x")
    r = res.records[0]
    assert r.input_tokens == 0
    assert r.cost_usd == 0.0


def test_assemble_drops_group_with_empty_observed_at():
    # A group whose time_unix_nano is empty/garbage produces no record.
    res = assemble([_tok("input", 100, t="")], ingest_source="file:x")
    assert res.records == []
    # A valid sibling in the same call still produces its record.
    res2 = assemble(
        [_tok("input", 100, t=""), _tok("input", 50, t="100")],
        ingest_source="file:x",
    )
    assert len(res2.records) == 1
    assert res2.records[0].input_tokens == 50


def test_contract_fixture_extracts_expected_usage():
    """Drift contract (#256). Multi-subagent fixture: main, subagent, auxiliary
    datapoints with query_source on BOTH cost and token metrics.
    """
    import json
    from pathlib import Path

    from kew.ingest.otlp import walk_metrics

    doc = json.loads(
        (Path(__file__).parent / "fixtures" / "claude_code_otel.json").read_text()
    )
    res = assemble(walk_metrics(doc), ingest_source="file:fixture")
    assert {TOKEN_METRIC, COST_METRIC} <= res.seen_metrics
    assert len(res.records) == 3
    by_qs = {r.query_source: r for r in res.records}
    assert set(by_qs.keys()) == {"main", "subagent", "auxiliary"}
    main = by_qs["main"]
    assert main.input_tokens == 1200
    assert main.output_tokens == 340
    assert main.cost_usd == 0.018
    assert main.agent_name is None
    sub = by_qs["subagent"]
    assert sub.input_tokens == 600
    assert sub.output_tokens == 150
    assert sub.cost_usd == 0.009
    assert sub.agent_name == "custom"
    aux = by_qs["auxiliary"]
    assert aux.input_tokens == 200
    assert aux.cost_usd == 0.003
    assert aux.agent_name is None
    for r in res.records:
        assert r.model == "claude-opus-4-8"
        assert r.observed_at.startswith("2024-06-26")


def test_assemble_separates_subagents_by_agent_name():
    points = [
        _tok("input", 100, qs="subagent", agent_name="explorer"),
        _tok("input", 60, qs="subagent", agent_name="reviewer"),
        _cost(0.10, qs="subagent", agent_name="explorer"),
        _cost(0.08, qs="subagent", agent_name="reviewer"),
    ]
    res = assemble(points, ingest_source="file:x")
    assert len(res.records) == 2
    by_name = {r.agent_name: r for r in res.records}
    assert set(by_name.keys()) == {"explorer", "reviewer"}
    assert by_name["explorer"].input_tokens == 100
    assert by_name["explorer"].cost_usd == 0.10
    assert by_name["reviewer"].input_tokens == 60
    assert by_name["reviewer"].cost_usd == 0.08
    assert len({r.dedup_key for r in res.records}) == 2


def test_idempotent_reingest_v16_era_main_only():
    """Re-ingesting a v16-era main-only export under v17 produces 0 new rows
    (backward-compatible dedup_key).
    """
    main_points = [_tok("input", 100, qs="main"), _cost(0.42, qs="main")]
    first = assemble(main_points, ingest_source="file:x")
    second = assemble(main_points, ingest_source="file:x")
    assert len(first.records) == 1
    assert len(second.records) == 1
    assert first.records[0].dedup_key == second.records[0].dedup_key
    # Also verify None query_source (pre-v17 data) gets same key as "main"
    legacy_points = [
        _tok("input", 100, qs=None),
        _cost(0.42, qs=None),
    ]
    legacy = assemble(legacy_points, ingest_source="file:x")
    assert legacy.records[0].dedup_key == first.records[0].dedup_key


def _tok_no_qs(type_, value, *, model="claude-opus-4-8", session="s1", t="100"):
    """Token datapoint without query_source, simulating pre-v17 exports."""
    return DataPoint(
        metric_name=TOKEN_METRIC,
        attributes={
            "type": type_,
            "model": model,
            "session.id": session,
        },
        value=value,
        temporality=1,
        time_unix_nano=t,
    )


def _cost_no_qs(value, *, model="claude-opus-4-8", session="s1", t="100"):
    """Cost datapoint without query_source, simulating pre-v17 exports."""
    return DataPoint(
        metric_name=COST_METRIC,
        attributes={"model": model, "session.id": session},
        value=value,
        temporality=1,
        time_unix_nano=t,
    )


def test_idempotent_reingest_legacy_no_query_source():
    """Legacy exports (no query_source at all) produce the same dedup_key as
    main, ensuring v16→v18 re-ingest is a no-op."""
    legacy = assemble(
        [_tok_no_qs("input", 100), _cost_no_qs(0.42)], ingest_source="file:x"
    )
    main = assemble(
        [_tok("input", 100, qs="main"), _cost(0.42, qs="main")], ingest_source="file:x"
    )
    assert legacy.records[0].dedup_key == main.records[0].dedup_key
