"""The committed ``kew.toml`` routes every delivery lane to codex on gpt-5.6-sol.

This is a ratchet on the operator's decision of 2026-09-01: kew dogfoods its
own official Codex runner on the pre-release work, with the contained OpenCode
reviewer left in place as the safety net. Nothing else asserted the delivery
route, so a stray edit to one lane (or a merge that resurrected the old
opencode/deepseek simple lane) would have changed what every dispatch launches
and failed no test. When the route is deliberately flipped again, change this
file in the same commit; that is the point.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
_LANES = ("default", "simple", "medium", "complex")
_DELIVERY_RUNNER = "codex"
_DELIVERY_MODEL = "gpt-5.6-sol"


@pytest.fixture(scope="module")
def committed() -> dict:
    with (_REPO_ROOT / "kew.toml").open("rb") as fh:
        return tomllib.load(fh)


@pytest.mark.parametrize("lane", _LANES)
def test_every_delivery_lane_is_codex_on_sol(committed: dict, lane: str) -> None:
    route = committed["routing"][lane]
    # A bare string lane inherits [agent].model (claude-opus-5) — a table with
    # BOTH keys is the only shape that pins the runner and the model together.
    assert isinstance(route, dict), f"routing.{lane} must be an explicit table"
    assert route.get("runner") == _DELIVERY_RUNNER, route
    assert route.get("model") == _DELIVERY_MODEL, route


def test_codex_effort_is_xhigh(committed: dict) -> None:
    assert committed["agent"]["codex"]["reasoning_effort"] == "xhigh"


def test_the_delivery_model_is_priced(committed: dict) -> None:
    # Standard dispatch refuses an unpriced model; review remediation does not
    # (#1795). Assert the committed route is priced so neither path can launch
    # a $0-accounted run.
    from kew.pricing import catalog_pricing

    assert _DELIVERY_MODEL in catalog_pricing()


def test_the_reviewer_did_not_move_with_the_flip(committed: dict) -> None:
    # The adversarial reviewer is the safety net for the delivery route. It
    # rides its own route (the Opus reviewer of #1968); if it ever rides the
    # same runner and model as delivery, the review is no longer a second
    # opinion.
    review = committed["review"]
    assert (review["runner"], review["model"]) != (_DELIVERY_RUNNER, _DELIVERY_MODEL)
    # Pinned exactly, not merely "not Sol": any change to the reviewer route is
    # a deliberate decision and must edit these lines in the same commit.
    assert review["runner"] == "claude-code"
    assert review["model"] == "claude-opus-5"
