"""The epic-timeline fixture is the DTO, not an approximation of it (#2270).

``frontend/fixtures/factory/epics-launch-fixes-timeline.json`` is what the
epic page's run Gantt draws in fixture mode (``npm run dev:fixtures``), and
the artboard check is only worth anything if the shape under it is the shape
``GET /api/epics/{ref}/timeline`` actually serves. A fixture that drifts —
a renamed field, a band missing its ``from``, a number where a string goes —
makes the console look right against data the server can never send.

So the fixture is validated against :class:`EpicTimelineDTO` and round-tripped
back through it: validation catches a field the DTO cannot read, and comparing
the re-serialized document to the file on disk catches the other direction —
a key the DTO would silently drop, or a field the fixture never filled in.

Shape alone is not enough, though: a document can satisfy the DTO and still
encode a state :func:`kew.service.epics.get_epic_timeline` can never produce.
The timeline draws one lane per non-external node of the epic's latest
snapshot, and a lane's open band is by construction the status that snapshot
reports — so the fixture is checked AGAINST its sibling,
``epics-launch-fixes.json``, the snapshot the epic page serves for the same
ref. Without that, a lane can go missing, or end in a status that contradicts
the node header on the page right above it, and every assertion here still
passes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import pytest

from kew.run_store import RunStatus
from kew.service.models import EpicTimelineDTO

#: Every committed timeline fixture, each beside the snapshot fixture for the
#: same ref — the snapshot that decides which nodes get a lane and what status
#: each lane ends in. The go-live (Linear) epic earned one in design review C2
#: (D22): the page reads `/timeline` for whichever epic is open, and the ref
#: with no fixture simply 404'd.
_FIXTURES = {
    "launch-fixes": (
        "frontend/fixtures/factory/epics-launch-fixes-timeline.json",
        "frontend/fixtures/factory/epics-launch-fixes.json",
    ),
    "go-live-2026-09": (
        "frontend/fixtures/factory/epics-go-live-2026-09-timeline.json",
        "frontend/fixtures/factory/epics-go-live-2026-09.json",
    ),
}


def _fixture_path(relative: str) -> Path:
    """Locate a committed fixture by walking up from this test."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / relative
        if candidate.is_file():
            return candidate
    pytest.skip(f"{relative} not present in this checkout")


def _load(relative: str) -> dict:
    return json.loads(_fixture_path(relative).read_text(encoding="utf-8"))


@pytest.fixture(params=sorted(_FIXTURES), ids=sorted(_FIXTURES))
def ref(request: pytest.FixtureRequest) -> str:
    """Each epic that has a timeline fixture: every claim below is made about
    all of them, so a second fixture cannot be committed unchecked."""
    return str(request.param)


@pytest.fixture
def payload(ref: str) -> dict:
    return _load(_FIXTURES[ref][0])


@pytest.fixture
def members(ref: str) -> list[dict]:
    """The nodes ``get_epic_timeline`` would give a lane, in snapshot order:
    this epic's own, identified; external boundary nodes left out."""
    snapshot = _load(_FIXTURES[ref][1])["snapshot"] or {}
    return [
        node
        for node in snapshot.get("nodes", [])
        if str(node.get("id") or "") and not node.get("external", False)
    ]


def test_the_timeline_fixture_is_exactly_the_dto(ref: str, payload: dict) -> None:
    dto = EpicTimelineDTO.model_validate(payload)

    assert dto.epic_ref == ref
    assert json.loads(dto.model_dump_json(by_alias=True)) == payload


def test_the_timeline_has_a_lane_for_every_node_of_the_epic_it_is_for(
    ref: str, payload: dict, members: list[dict]
) -> None:
    """One lane per non-external snapshot node, titled as the snapshot titles
    it. A lane count that drifts from the epic is the drift this fixture is
    most likely to pick up: it reads fine on its own while showing a different
    DAG from the one the page's node list has already drawn."""
    dto = EpicTimelineDTO.model_validate(payload)

    assert members, ref
    assert {node.id for node in dto.nodes} == {str(n["id"]) for n in members}
    assert {node.id: node.title for node in dto.nodes} == {
        str(n["id"]): str(n.get("title") or "") for n in members
    }


def test_every_lane_ends_in_the_status_the_snapshot_reports(
    payload: dict, members: list[dict]
) -> None:
    """Bands are folded from the driver's transitions, so a lane's open band
    IS the node's current status — the same status the snapshot carries. A
    lane ending anywhere else contradicts the node header beside it, and no
    sequence of transitions produces it. A lane with no bands at all is a node
    that has never moved, which the DAG still reports as ``todo``."""
    dto = EpicTimelineDTO.model_validate(payload)
    status = {str(n["id"]): str(n.get("status") or "") for n in members}

    for node in dto.nodes:
        if not node.bands:
            assert status[node.id] == "todo", node.id
            continue
        assert node.bands[-1].to is None, node.id
        assert node.bands[-1].status == status[node.id], node.id


def test_the_timeline_fixture_draws_what_the_gantt_has_to_render(
    payload: dict,
) -> None:
    """A fixture with only happy nodes proves nothing about the chart: the
    held band, the failed run, the merge marker and the stop rule's
    ``READY_WITH_RESIDUALS`` acceptance (#2151) are the marks the artboard is
    checked on, and a node with no activity is the empty lane.

    Asked of every committed fixture: a second epic drawn in fixture mode is
    a second chance to review the chart, and a lane set with no held band and
    no failed run reviews nothing.

    A run's status is :data:`~kew.run_store.RunStatus`, which has no ``failed``
    and no ``running`` — those are the epic node's vocabulary and the console's
    display-only refinement of ``pending``. This test asked for ``failed``, so
    the fixtures were written to it and drew runs the backend cannot emit
    (#2377); it now asks for the failure status the ledger really stores, and
    holds every run status in the fixture to the enum."""
    dto = EpicTimelineDTO.model_validate(payload)
    statuses = {band.status for node in dto.nodes for band in node.bands}
    run_statuses = {run.status for node in dto.nodes for run in node.runs}
    verdicts = {round_.verdict for node in dto.nodes for round_ in node.rounds}

    assert "held" in statuses
    assert "failure" in run_statuses
    assert run_statuses <= set(get_args(RunStatus)), run_statuses
    assert {"READY", "NEEDS_CHANGES", "READY_WITH_RESIDUALS"} <= verdicts
    assert any(node.merged_at for node in dto.nodes)
    assert any(not node.bands and not node.runs for node in dto.nodes)
    # Ordered by first activity, with the quiet lanes last.
    activity = [
        min(
            [band.from_ for band in node.bands]
            + [run.started_at or "" for run in node.runs],
            default="",
        )
        for node in dto.nodes
    ]
    assert activity == sorted(activity, key=lambda a: (a == "", a))
    # The span opens at the earliest activity anywhere in the epic.
    assert dto.span.start == activity[0]
    assert dto.span.end == dto.now


def test_every_open_band_is_the_last_one_on_its_lane(payload: dict) -> None:
    """``to=None`` means "still in this status", so it can only ever be the
    newest band — an open band mid-lane would draw over its successors."""
    dto = EpicTimelineDTO.model_validate(payload)

    for node in dto.nodes:
        assert all(band.to for band in node.bands[:-1]), node.id
        froms = [band.from_ for band in node.bands]
        assert froms == sorted(froms), node.id
