"""Serve-surface coverage for GET /api/prs/freshness (#2303, design-c3).

The read sits behind the router-level ``read_guard`` like every other ``/api``
read: session-gated under ``--expose`` and for non-loopback peers, loopback-open
otherwise. It is bounded (``limit``/``cursor``) and makes NO GitHub call — the
store is a fake with no network of any kind, and a 200 proves it.
"""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Any

from kew.config import DispatchConfig
from kew.run_store import NodePrFacts
from kew.serve.app import create_app

PATH = "/api/prs/freshness"
EPIC = "design-c3"


class FakeStore:
    """One open node whose head moved after a READY verdict."""

    def __init__(self) -> None:
        #: Every ``epic_ref`` the router pushed down, so a test can prove the
        #: filter reached the QUERY rather than being applied after the page
        #: was already bounded.
        self.epic_refs: list[str | None] = []

    def latest_node_pr_facts(self, **kwargs: Any) -> list[NodePrFacts]:
        self.epic_refs.append(kwargs.get("epic_ref"))
        if kwargs.get("epic_ref") not in (None, EPIC):
            return []
        return [
            NodePrFacts(
                epic_ref=EPIC,
                item_id="2303",
                observed_at="2026-10-06T12:00:00+00:00",
                status="in_review",
                pr_url="https://github.com/o/r/pull/2303",
                head_oid="a1b2c3d",
                merge_state="BEHIND",
                merge_state_since="2026-10-05T10:00:00+00:00",
                merge_state_at="2026-10-06T11:59:00+00:00",
                pr_pushed_at="2026-10-06T09:00:00+00:00",
            )
        ]

    def list_review_rounds_for_epic(self, _epic_ref: str) -> list[Any]:
        return []

    def list_pending(self) -> list[Any]:
        return []


def _mk_app(tmp_path: Path, *, expose: bool, store: FakeStore | None = None):
    return create_app(
        lambda: nullcontext(store or FakeStore()),
        repo_dir=tmp_path,
        expose=expose,
        config=DispatchConfig(),
    )


def test_exposed_read_requires_session(tmp_path: Path, inbox_client) -> None:
    c = inbox_client(_mk_app(tmp_path, expose=True), loopback_peer=True)
    assert c.get(PATH).status_code == 401


def test_non_loopback_read_requires_session(tmp_path: Path, inbox_client) -> None:
    c = inbox_client(_mk_app(tmp_path, expose=False), loopback_peer=False)
    assert c.get(PATH).status_code == 401


def test_loopback_read_open_when_not_exposed(tmp_path: Path, inbox_client) -> None:
    c = inbox_client(_mk_app(tmp_path, expose=False), loopback_peer=True)
    assert c.get(PATH).status_code == 200


def test_payload_shape(tmp_path: Path, inbox_client) -> None:
    c = inbox_client(_mk_app(tmp_path, expose=False), loopback_peer=True)

    body = c.get(PATH).json()

    assert set(body) >= {"rows", "next_cursor", "unavailable", "composed_at"}
    assert body["unavailable"] == []
    (row,) = body["rows"]
    assert set(row) >= {
        "item_id",
        "epic_ref",
        "pr_number",
        "last_verdict",
        "latest_head",
        "review_state",
        "base",
        "observed_at",
    }
    assert row["pr_number"] == 2303
    assert row["review_state"] == "no_round"
    assert row["latest_head"] == {
        "sha": "a1b2c3d",
        "source": "snapshot",
        "at": "2026-10-06T09:00:00+00:00",
    }
    assert row["base"]["state"] == "behind"
    assert row["base"]["reason"] != ""
    # When the state was READ, which the row's ``observed_at`` does not answer:
    # the driver carries a merge state forward over a tick GitHub could not
    # answer, so the value can predate the tick that reported it.
    assert row["base"]["at"] == "2026-10-06T11:59:00+00:00"


def test_the_epic_filter_is_pushed_into_the_store_query(
    tmp_path: Path, inbox_client
) -> None:
    """A surface drawing ONE epic must be able to ask only for it (#2303).

    Without it a page bounded by ``limit`` over every epic can fill before
    reaching the epic on screen, and the empty result is indistinguishable from
    "nothing here is stale". Filtering after the page was bounded would not fix
    that, so the ref has to reach the query.
    """
    store = FakeStore()
    c = inbox_client(_mk_app(tmp_path, expose=False, store=store), loopback_peer=True)

    mine = c.get(f"{PATH}?epic={EPIC}").json()
    other = c.get(f"{PATH}?epic=some-other-epic").json()

    assert store.epic_refs == [EPIC, "some-other-epic"]
    assert [r["item_id"] for r in mine["rows"]] == ["2303"]
    assert other["rows"] == []


def test_no_epic_asked_for_reads_every_epic(tmp_path: Path, inbox_client) -> None:
    store = FakeStore()
    c = inbox_client(_mk_app(tmp_path, expose=False, store=store), loopback_peer=True)

    assert c.get(PATH).status_code == 200
    assert store.epic_refs == [None]


def test_an_out_of_range_limit_is_refused_rather_than_silently_clamped(
    tmp_path: Path, inbox_client
) -> None:
    """The bound is part of the contract, so an unbounded ask is a 422."""
    c = inbox_client(_mk_app(tmp_path, expose=False), loopback_peer=True)

    assert c.get(f"{PATH}?limit=100000").status_code == 422
    assert c.get(f"{PATH}?limit=0").status_code == 422
