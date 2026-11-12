"""Docs links into the demo console open a tour step that exists (#3150).

A docs page links to ``https://demo.kew.dev/<route>?tour=<id>&step=<n>``. The
tours are the JSON files under ``frontend/demo/tours/``; a link whose tour is
not one of them, whose step is past its last step, or whose route is not the
route that step opens on, would land a reader on a tour that cannot start
there. A tour no docs page links to at its first step can only be found from
inside the demo.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
_TOURS = REPO_ROOT / "frontend/demo/tours"
_DOCS = REPO_ROOT / "docs"

_DEMO_LINK = re.compile(r"https?://demo\.kew\.dev[^\s)\]>\"'`]*")

Tours = dict[str, list[str]]


def _load_tours(directory: Path) -> Tours:
    """Each tour's id and the route of each of its steps, in order."""
    tours: Tours = {}
    for path in sorted(directory.glob("*.json")):
        tour = json.loads(path.read_text())
        tours[tour["id"]] = [step["route"] for step in tour["steps"]]
    return tours


def _link_problem(link: str, tours: Tours) -> str | None:
    """Why ``link`` cannot open a registered tour step, or ``None``."""
    parts = urlsplit(link)
    query = parse_qs(parts.query)
    tour = query.get("tour", [None])[0]
    step = query.get("step", [None])[0]
    if tour is None and step is None:
        return None
    if tour not in tours:
        return f"tour {tour!r} is not under frontend/demo/tours/"
    if step is None or not step.isdigit() or not 1 <= int(step) <= len(tours[tour]):
        return f"tour {tour!r} has no step {step!r} (it has {len(tours[tour])})"
    route = tours[tour][int(step) - 1]
    if (parts.path or "/") != route:
        return f"step {step} of {tour!r} opens on {route}, not {parts.path or '/'}"
    return None


def _demo_links(pages: dict[str, str]) -> list[tuple[str, str]]:
    return [
        (name, link)
        for name, text in pages.items()
        for link in _DEMO_LINK.findall(text)
    ]


def check_demo_links(pages: dict[str, str], tours: Tours) -> list[str]:
    """Every refusal: a link to a missing tour step, a tour unlinked at step 1."""
    links = _demo_links(pages)
    problems = [
        f"{name}: {link}: {problem}"
        for name, link in links
        if (problem := _link_problem(link, tours)) is not None
    ]
    starts = {
        parse_qs(urlsplit(link).query).get("tour", [""])[0]
        for _, link in links
        if parse_qs(urlsplit(link).query).get("step") == ["1"]
    }
    problems += [
        f"tour {tour!r} has no docs link to its first step"
        for tour in sorted(tours)
        if tour not in starts
    ]
    return problems


_SAMPLE_TOURS: Tours = {"sample": ["/work", "/monitor"]}
_SAMPLE_START = "[go](https://demo.kew.dev/work?tour=sample&step=1)"


def test_a_link_to_each_tours_first_step_passes() -> None:
    assert check_demo_links({"a.md": _SAMPLE_START}, _SAMPLE_TOURS) == []


@pytest.mark.parametrize(
    ("link", "reason"),
    [
        ("https://demo.kew.dev/work?tour=missing&step=1", "tour 'missing' is not"),
        ("https://demo.kew.dev/monitor?tour=sample&step=3", "has no step '3'"),
        ("https://demo.kew.dev/monitor?tour=sample&step=0", "has no step '0'"),
        ("https://demo.kew.dev/monitor?tour=sample", "has no step None"),
        (
            "https://demo.kew.dev/work?tour=sample&step=2",
            "opens on /monitor, not /work",
        ),
    ],
)
def test_a_link_to_a_missing_tour_step_is_refused(link: str, reason: str) -> None:
    problems = check_demo_links(
        {"a.md": f"{_SAMPLE_START}\n[step]({link})"}, _SAMPLE_TOURS
    )
    assert len(problems) == 1
    assert problems[0].startswith(f"a.md: {link}: ")
    assert reason in problems[0]


def test_a_tour_without_a_link_to_its_first_step_is_refused() -> None:
    page = "[later](https://demo.kew.dev/monitor?tour=sample&step=2)"
    assert check_demo_links({"a.md": page}, _SAMPLE_TOURS) == [
        "tour 'sample' has no docs link to its first step"
    ]


def test_a_demo_link_without_a_tour_is_not_a_tour_link() -> None:
    page = f"{_SAMPLE_START} and [home](https://demo.kew.dev/)"
    assert check_demo_links({"a.md": page}, _SAMPLE_TOURS) == []


@pytest.mark.policy
def test_every_docs_link_into_the_demo_opens_a_registered_tour_step() -> None:
    tours = _load_tours(_TOURS)
    assert tours, "no tours under frontend/demo/tours/"
    pages = {
        str(path.relative_to(_DOCS)): path.read_text()
        for path in sorted(_DOCS.rglob("*.md"))
    }
    assert check_demo_links(pages, tours) == []


@pytest.mark.policy
def test_every_demo_step_passage_links_to_its_step() -> None:
    """A passage a tour step reads is followed by a link that opens that step."""
    tours = _load_tours(_TOURS)
    passage = re.compile(
        r"<!--\s*demo-step:\s*(\S+)\s*-->.*?<!--\s*/demo-step\s*-->\s*\n\s*\n([^\n]*)\n",
        re.S,
    )
    missing = []
    for path in sorted(_DOCS.rglob("*.md")):
        for match in passage.finditer(path.read_text()):
            tour, step = match.group(1).split("/")
            route = tours[tour][int(step) - 1]
            want = f"[See it in the demo](https://demo.kew.dev{route}?tour={tour}&step={step})"
            if match.group(2).strip() != want:
                missing.append(f"{path.relative_to(_DOCS)}: {match.group(1)}")
    assert missing == []
