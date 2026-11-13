from kew.ingest.otlp import walk_metrics


def _doc(points):
    return {
        "resourceMetrics": [
            {
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "claude_code.token.usage",
                                "sum": {
                                    "aggregationTemporality": 1,
                                    "dataPoints": points,
                                },
                            }
                        ]
                    }
                ]
            }
        ]
    }


def test_walk_parses_asint_string_and_attributes():
    doc = _doc(
        [
            {
                "timeUnixNano": "1719403200000000000",
                "asInt": "1234",
                "attributes": [
                    {"key": "type", "value": {"stringValue": "input"}},
                    {"key": "model", "value": {"stringValue": "claude-opus-4-8"}},
                ],
            }
        ]
    )
    points = walk_metrics(doc)
    assert len(points) == 1
    p = points[0]
    assert p.metric_name == "claude_code.token.usage"
    assert p.value == 1234.0
    assert p.attributes == {"type": "input", "model": "claude-opus-4-8"}
    assert p.temporality == 1
    assert p.time_unix_nano == "1719403200000000000"


def test_walk_parses_asdouble_number():
    doc = _doc([{"timeUnixNano": "1", "asDouble": 0.42, "attributes": []}])
    assert walk_metrics(doc)[0].value == 0.42


def test_walk_reads_gauge_and_intvalue_attribute():
    doc = {
        "resourceMetrics": [
            {
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "claude_code.cost.usage",
                                "gauge": {
                                    "dataPoints": [
                                        {
                                            "timeUnixNano": "2",
                                            "asDouble": 1.5,
                                            "attributes": [
                                                {
                                                    "key": "n",
                                                    "value": {"intValue": "7"},
                                                }
                                            ],
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                ]
            }
        ]
    }
    p = walk_metrics(doc)[0]
    assert p.metric_name == "claude_code.cost.usage"
    assert p.value == 1.5
    assert p.temporality == 0  # gauge has no temporality
    assert p.attributes == {"n": "7"}


def test_walk_is_fail_soft_on_garbage():
    assert walk_metrics("not a dict") == []
    assert walk_metrics({}) == []
    assert walk_metrics({"resourceMetrics": "bad"}) == []
    # a malformed data point is skipped, a good sibling survives
    doc = _doc(
        [{"attributes": []}, {"timeUnixNano": "9", "asInt": "5", "attributes": []}]
    )
    pts = walk_metrics(doc)
    assert [p.value for p in pts] == [5.0]


def test_walk_fail_soft_on_non_iterable_levels():
    # Non-iterable scalars at each nesting level must not raise TypeError.
    assert walk_metrics({"resourceMetrics": 5}) == []
    assert walk_metrics({"resourceMetrics": [{"scopeMetrics": 5}]}) == []
    assert walk_metrics({"resourceMetrics": [{"scopeMetrics": [{"metrics": 5}]}]}) == []
    # malformed dataPoints scalar inside a valid metric shell
    doc = {
        "resourceMetrics": [
            {
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "claude_code.token.usage",
                                "sum": {"aggregationTemporality": 1, "dataPoints": 99},
                            }
                        ]
                    }
                ]
            }
        ]
    }
    assert walk_metrics(doc) == []
