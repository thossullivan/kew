"""Docs links into the demo console open a tour step that exists (#3150).

A docs page links to ``https://demo.kew.dev/<route>?tour=<id>&step=<n>``. The
tours are the JSON files under ``frontend/demo/tours/``; a link whose tour is
not one of them, whose step is past its last step, or whose route is not the
route that step opens on, would land a reader on a tour that cannot start
there. A tour no published docs page links to at its first step can only be
found from inside the demo.
"""

from __future__ import annotations

import json
import re
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
_TOURS = REPO_ROOT / "frontend/demo/tours"
_DOCS = REPO_ROOT / "docs"
_MKDOCS = REPO_ROOT / "mkdocs.yml"

_DEMO_LINK = re.compile(r"(?:https?:)?//demo\.kew\.dev[^\s)\]>\"'`]*", re.I)
_STEP = re.compile(r"[1-9][0-9]*")
# A fence closes on a bare run of its own character at least as long as it.
_FENCE = re.compile(
    r"^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?^ {0,3}\1(?:`*|~*)[ \t]*$", re.M | re.S
)
_OPEN = re.compile(r"<!--\s*demo-step:\s*(\S+)\s*-->")
_CLOSE = re.compile(r"<!--\s*/demo-step\s*-->")

Tours = dict[str, list[str]]


def _load_tours(directory: Path) -> Tours:
    """Each tour's id and the route of each of its steps, in order."""
    tours: Tours = {}
    for path in sorted(directory.glob("*.json")):
        tour = json.loads(path.read_text())
        tours[tour["id"]] = [step["route"] for step in tour["steps"]]
    return tours


def _step_route(tours: Tours, tour: str | None, step: str | None) -> str:
    """The route ``tour``'s ``step`` opens on; a ``ValueError`` says why not."""
    if tour not in tours:
        raise ValueError(f"tour {tour!r} is not under frontend/demo/tours/")
    steps = tours[tour]
    if step is None or not _STEP.fullmatch(step) or int(step) > len(steps):
        raise ValueError(f"tour {tour!r} has no step {step!r} (it has {len(steps)})")
    return steps[int(step) - 1]


def _link_problem(link: str, tours: Tours) -> str | None:
    """Why ``link`` cannot open a registered tour step, or ``None``."""
    parts = urlsplit(link)
    query = parse_qs(parts.query)
    tour = query.get("tour", [None])[0]
    step = query.get("step", [None])[0]
    if tour is None and step is None:
        return None
    if not link.startswith(("https://demo.kew.dev/", "https://demo.kew.dev?")):
        return "a tour link starts https://demo.kew.dev/, exactly"
    try:
        route = _step_route(tours, tour, step)
    except ValueError as refusal:
        return str(refusal)
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


def check_passage_links(pages: dict[str, str], tours: Tours) -> list[str]:
    """Every demo-step passage whose next non-blank line is not its step's link.

    Fenced code blocks are skipped: a marker there is an example, not a passage.
    """
    problems = []
    for name, text in pages.items():
        prose = _FENCE.sub("", text.replace("\r\n", "\n"))
        for opened in _OPEN.finditer(prose):
            marker = opened.group(1)
            closed = _CLOSE.search(prose, opened.end())
            if closed is None:
                problems.append(f"{name}: {marker}: the passage is never closed")
                continue
            tour, _, step = marker.partition("/")
            try:
                route = _step_route(tours, tour, step)
            except ValueError as refusal:
                problems.append(f"{name}: {marker}: {refusal}")
                continue
            after = prose[closed.end() :].split("\n")
            following = next((line.strip() for line in after if line.strip()), "")
            want = f"[See it in the demo](https://demo.kew.dev{route}?tour={tour}&step={step})"
            if following != want:
                problems.append(f"{name}: {marker}: not followed by {want}")
    return problems


def _excluded_docs(mkdocs_yml: str) -> list[str]:
    """The ``exclude_docs`` globs of mkdocs.yml, as paths relative to ``docs/``."""
    block = re.search(r"^exclude_docs: \|\n((?:[ \t]+\S[^\n]*\n)+)", mkdocs_yml, re.M)
    assert block, "mkdocs.yml has no exclude_docs block"
    return [line.strip().lstrip("/") for line in block.group(1).splitlines()]


def _published_pages() -> dict[str, str]:
    """Every docs page mkdocs builds, by its path under ``docs/``."""
    excluded = _excluded_docs(_MKDOCS.read_text())
    pages = {}
    for path in sorted(_DOCS.rglob("*.md")):
        name = path.relative_to(_DOCS).as_posix()
        if not any(fnmatch(name, glob) for glob in excluded):
            pages[name] = path.read_text()
    return pages


_SAMPLE_TOURS: Tours = {"sample": ["/work", "/monitor"]}
_SAMPLE_START = "[go](https://demo.kew.dev/work?tour=sample&step=1)"
_SAMPLE_LINK_2 = "[See it in the demo](https://demo.kew.dev/monitor?tour=sample&step=2)"
_SAMPLE_PASSAGE_2 = "<!-- demo-step: sample/2 -->\nText.\n<!-- /demo-step -->"


def test_a_link_to_each_tours_first_step_passes() -> None:
    assert check_demo_links({"a.md": _SAMPLE_START}, _SAMPLE_TOURS) == []


@pytest.mark.parametrize(
    ("link", "reason"),
    [
        ("https://demo.kew.dev/work?tour=missing&step=1", "tour 'missing' is not"),
        ("https://demo.kew.dev/monitor?tour=sample&step=3", "has no step '3'"),
        ("https://demo.kew.dev/monitor?tour=sample&step=0", "has no step '0'"),
        ("https://demo.kew.dev/monitor?tour=sample&step=²", "has no step '²'"),
        ("https://demo.kew.dev/monitor?tour=sample", "has no step None"),
        (
            "https://demo.kew.dev/work?tour=sample&step=2",
            "opens on /monitor, not /work",
        ),
        ("http://demo.kew.dev/monitor?tour=sample&step=2", "starts https://"),
        ("HTTPS://demo.kew.dev/monitor?tour=sample&step=2", "starts https://"),
        ("https://Demo.Kew.Dev/monitor?tour=sample&step=2", "starts https://"),
        ("//demo.kew.dev/monitor?tour=sample&step=2", "starts https://"),
        ("https://demo.kew.dev:443/monitor?tour=sample&step=2", "starts https://"),
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


@pytest.mark.parametrize(
    "page",
    [
        f"{_SAMPLE_PASSAGE_2}\n\n{_SAMPLE_LINK_2}\n",
        f"{_SAMPLE_PASSAGE_2}\n\n{_SAMPLE_LINK_2}",
        f"{_SAMPLE_PASSAGE_2}\r\n\r\n{_SAMPLE_LINK_2}\r\n",
        f"```\n<!-- demo-step: other/9 -->\n```\n\n"
        f"{_SAMPLE_PASSAGE_2}\n\n{_SAMPLE_LINK_2}",
        f"````md\n```\n<!-- demo-step: other/9 -->\n```\n````\n\n"
        f"{_SAMPLE_PASSAGE_2}\n\n{_SAMPLE_LINK_2}",
    ],
)
def test_a_passage_followed_by_its_step_link_passes(page: str) -> None:
    assert check_passage_links({"a.md": page}, _SAMPLE_TOURS) == []


@pytest.mark.parametrize(
    ("page", "reason"),
    [
        (_SAMPLE_PASSAGE_2, "sample/2: not followed by"),
        (f"{_SAMPLE_PASSAGE_2}\n\n", "sample/2: not followed by"),
        (f"{_SAMPLE_PASSAGE_2}\nNext paragraph.\n", "sample/2: not followed by"),
        (
            "<!-- demo-step: missing/1 -->\nText.\n<!-- /demo-step -->",
            "'missing' is not",
        ),
        ("<!-- demo-step: sample/7 -->\nText.\n<!-- /demo-step -->", "no step '7'"),
        ("<!-- demo-step: sample/a/b -->\nText.\n<!-- /demo-step -->", "no step 'a/b'"),
        ("<!-- demo-step: sample/2 -->\nText.\n", "never closed"),
        (
            f"````md\n```\n````\n\n{_SAMPLE_PASSAGE_2}\n\n```\nlater\n```\n",
            "sample/2: not followed by",
        ),
    ],
)
def test_a_passage_without_its_step_link_is_refused(page: str, reason: str) -> None:
    problems = check_passage_links({"a.md": page}, _SAMPLE_TOURS)
    assert len(problems) == 1
    assert problems[0].startswith("a.md: ")
    assert reason in problems[0]


def test_each_passage_is_checked_on_its_own() -> None:
    """A passage with no link does not swallow the next one's link."""
    unlinked = "<!-- demo-step: sample/1 -->\nOne.\n<!-- /demo-step -->\nProse.\n\n"
    page = f"{unlinked}{_SAMPLE_PASSAGE_2}\n\n{_SAMPLE_LINK_2}\n"
    problems = check_passage_links({"a.md": page}, _SAMPLE_TOURS)
    assert len(problems) == 1
    assert "sample/1: not followed by" in problems[0]


def test_pages_mkdocs_excludes_are_not_published() -> None:
    mkdocs_yml = "site_name: x\nexclude_docs: |\n  /audit/**\n  /brand.md\n\nnav: []\n"
    assert _excluded_docs(mkdocs_yml) == ["audit/**", "brand.md"]


@pytest.mark.policy
def test_every_docs_link_into_the_demo_opens_a_registered_tour_step() -> None:
    tours = _load_tours(_TOURS)
    assert tours, "no tours under frontend/demo/tours/"
    assert check_demo_links(_published_pages(), tours) == []


@pytest.mark.policy
def test_every_demo_step_passage_links_to_its_step() -> None:
    """A passage a tour step reads is followed by a link that opens that step."""
    assert check_passage_links(_published_pages(), _load_tours(_TOURS)) == []
