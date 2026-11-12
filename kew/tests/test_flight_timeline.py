"""Tests for the live activity chain a run's event stream reduces to.

This model says what the agent was doing, step by step. It no longer stitches
runs into a tree or hangs a git tree off a node: both were reconstructions from
a worktree the agent still owned, and what a run produced is read from the
sealed artifact instead (``tests/test_flight_evidence.py``).
"""

import inspect

from kew.flight import timeline as timeline_module
from kew.flight.timeline import build_timeline, linear_path


def _events_single_run():
    return [
        {
            "ts": 1.0,
            "type": "started",
            "runner": "claude-code",
            "model": "m",
            "issue": 312,
        },
        {"ts": 2.0, "type": "tool_use", "tool": "read", "target": "a.py"},
        {"ts": 2.1, "type": "tokens", "input": 100, "output": 20, "cost_usd": 0.001},
        {"ts": 3.0, "type": "tool_use", "tool": "edit", "target": "a.py"},
        {"ts": 3.2, "type": "tokens", "input": 300, "output": 80, "cost_usd": 0.005},
        {"ts": 4.0, "type": "finished", "exit_code": 0},
    ]


def test_single_run_builds_a_linear_chain():
    tl = build_timeline({"r1": _events_single_run()})
    path = linear_path(tl, tl.head_id)
    assert [n.kind for n in path] == ["init", "read", "edit", "result"]
    assert path[0].parent_id is None
    assert all(path[i].parent_id == path[i - 1].id for i in range(1, len(path)))


def test_cumulative_tokens_land_on_the_right_node():
    # kew's `tokens` events carry running totals (not deltas), so each node
    # records the cumulative spend as of its step.
    tl = build_timeline({"r1": _events_single_run()})
    path = linear_path(tl, tl.head_id)
    read = next(n for n in path if n.kind == "read")
    edit = next(n for n in path if n.kind == "edit")
    assert (read.tokens_in, read.tokens_out) == (100, 20)
    assert (edit.tokens_in, edit.tokens_out) == (300, 80)
    assert round(edit.cost_usd, 6) == 0.005


def test_a_snapshot_event_no_longer_decorates_a_node():
    # Legacy streams still carry `snapshot` lines. They are inert now: no node
    # may claim a git tree the recorder would have to consult a repository to
    # read back.
    events = [
        {"ts": 1.0, "type": "started", "issue": 1},
        {"ts": 2.0, "type": "tool_use", "tool": "edit", "target": "a.py"},
        {"ts": 2.1, "type": "snapshot", "tree": "deadbeef", "after": "edit"},
    ]
    tl = build_timeline({"r1": events})
    edit = next(n for n in tl.nodes.values() if n.kind == "edit")
    assert not hasattr(edit, "snapshot_tree")
    assert [n.kind for n in linear_path(tl, tl.head_id)] == ["init", "edit"]


def test_runs_are_kept_side_by_side_not_stitched_into_a_tree():
    # A retry's relationship to its predecessor is a fact somebody records, not
    # one this reader may infer from two streams sitting in the same dict.
    child = [
        {"ts": 5.0, "type": "started", "issue": 312},
        {"ts": 6.0, "type": "tool_use", "tool": "write", "target": "b.py"},
        {"ts": 7.0, "type": "finished", "exit_code": 0},
    ]
    tl = build_timeline({"r1": _events_single_run(), "r2": child})
    r2_root = next(
        n for n in tl.nodes.values() if n.run_id == "r2" and n.kind == "init"
    )
    assert r2_root.parent_id is None
    r2_head = next(
        n.id for n in tl.nodes.values() if n.run_id == "r2" and n.kind == "result"
    )
    assert {n.run_id for n in linear_path(tl, r2_head)} == {"r2"}


def test_the_fork_stitching_surface_is_gone():
    assert not hasattr(timeline_module, "ForkLink")
    assert "fork_links" not in inspect.signature(build_timeline).parameters
