"""Focused source-adapter regressions for epic container membership."""

from __future__ import annotations

import json
import subprocess
from typing import Any

from kew.dependencies import epic_container_name, format_epic_container_marker
from kew.graph.adapters.github_convention import GitHubConventionSource


def test_convention_read_excludes_any_marked_container() -> None:
    issues = [
        {
            "number": 101,
            "title": "work",
            "url": "https://github.com/o/r/issues/101",
            "state": "OPEN",
            "stateReason": None,
            "labels": [{"name": "epic:demo"}],
            "body": "work body",
        },
        {
            "number": 103,
            "title": "container",
            "url": "https://github.com/o/r/issues/103",
            "state": "OPEN",
            "stateReason": None,
            "labels": [{"name": "epic:demo"}],
            "body": format_epic_container_marker("another-epic"),
        },
    ]

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, json.dumps(issues), "")

    source = GitHubConventionSource(repo="o/r", run_fn=run)

    assert [node.id for node in source.list_epic_nodes("demo")] == ["101"]


def test_container_marker_parses_as_a_whole_line() -> None:
    body = f"Epic container for `demo`.\n\n{format_epic_container_marker('demo')}\n"

    assert epic_container_name(body) == "demo"


def test_a_marker_that_is_not_its_own_line_is_not_a_container() -> None:
    # One rule covers every Markdown context that used to need its own branch:
    # prose, inline code, fenced blocks, indented blocks, blockquotes, and the
    # multiline code span that defeated the fence tracker.
    marker = format_epic_container_marker("demo")
    for label, body in [
        ("prose", f"See {marker} for the shape."),
        ("inline code", f"Document `{marker}` here."),
        ("fenced", f"```\n{marker}\n```\n"),
        ("tilde fenced", f"~~~\n{marker}\n~~~\n"),
        ("indented", f"    {marker}\n"),
        ("blockquote", f"> {marker}\n"),
        ("multiline span", f"```bad`\ntext {marker}\nend```\n"),
    ]:
        assert epic_container_name(body) is None, label


def test_surrounding_whitespace_still_marks_a_container() -> None:
    body = f"   {format_epic_container_marker('demo')}   \n"

    assert epic_container_name(body) == "demo"


def test_a_tab_indented_marker_is_indented_code() -> None:
    # CommonMark counts a tab as up to four columns, so these are indented
    # code. Reading them as a container would drop a real work issue from
    # membership -- the node-loss direction.
    marker = format_epic_container_marker("demo")
    for label, body in [
        ("tab", f"\t{marker}\n"),
        ("spaces then tab", f"   \t{marker}\n"),
    ]:
        assert epic_container_name(body) is None, label


def test_a_two_comment_body_unions_edges_and_is_flagged_on_the_graph() -> None:
    from kew.graph.build import build_graph

    def issue(number: int, body: str) -> dict[str, Any]:
        return {
            "number": number,
            "title": f"t{number}",
            "url": f"https://github.com/o/r/issues/{number}",
            "state": "OPEN",
            "stateReason": None,
            "labels": [{"name": "epic:demo"}],
            "body": body,
        }

    issues = [
        issue(2500, "a"),
        issue(2502, "b"),
        issue(2652, "<!-- kew:depends 2502 -->\n<!-- kew:depends 2500 -->"),
        issue(2653, "<!-- kew:depends 2502 -->"),
    ]

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, json.dumps(issues), "")

    graph = build_graph(GitHubConventionSource(repo="o/r", run_fn=run), "demo")

    assert graph.dependencies_of("2652") == ["2500", "2502"]
    assert graph.multi_depends == ["2652"]
