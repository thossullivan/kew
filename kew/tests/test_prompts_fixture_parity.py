"""The console's prompt-catalog fixtures say what ``GET /api/prompts`` serves (#3019).

``frontend/fixtures/factory/prompts.json`` and one ``prompts-<role>.json`` per
role are what the Prompts page draws in fixture mode and what ``ui-check``
reads. They were generated from :func:`kew.service.prompts.list_roles` and
:func:`~kew.service.prompts.role_preview`; this holds every role, its run kinds
and its source to :data:`~kew.service.prompts.ROLES`, so a role added to,
dropped from or moved in the catalog cannot leave the console's fixtures
describing another one. The reviewer's version and sha256 and every preview's
text are left out: they move with each artifact bump or template edit, and the
page reads them off the payload rather than from any constant.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kew.serve.api_prompts import ProfileVersionsDTO
from kew.service.prompt_profiles import PromptOutcomesDTO
from kew.service.prompts import ROLES, PromptPreviewDTO, list_roles

_DIR = "frontend/fixtures/factory"


def _fixture(name: str) -> Any:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / _DIR / name
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    pytest.fail(f"{_DIR}/{name} is missing: every listed role needs its fixture")


def _shape(role: dict[str, Any]) -> dict[str, Any]:
    """A role without the facts that move with every artifact bump."""
    source = {k: v for k, v in role["source"].items() if k in {"kind", "name"}}
    return {
        "role": role["role"],
        "run_kinds": role["run_kinds"],
        "source": source,
        "prompt_config_applies": role["prompt_config_applies"],
        "profile_applies": role["profile_applies"],
    }


def test_the_catalog_fixture_lists_every_role_as_the_service_does() -> None:
    served = [_shape(r.model_dump(mode="json")) for r in list_roles(None)]
    assert [_shape(r) for r in _fixture("prompts.json")] == served


def test_every_listed_role_has_a_preview_fixture_under_the_same_source() -> None:
    for role in _fixture("prompts.json"):
        preview = _fixture(f"prompts-{role['role']}.json")
        assert _shape(preview) == _shape(role)
        assert preview["source"] == role["source"]
        assert preview["preview"]


def test_a_missing_preview_fixture_fails_rather_than_skips() -> None:
    with pytest.raises(pytest.fail.Exception, match="prompts-no_such_role.json"):
        _fixture("prompts-no_such_role.json")


def test_each_profile_role_has_a_history_fixture_the_api_could_serve() -> None:
    """``prompts-<role>-versions.json`` (#3025) parses as the served DTO."""
    profile_roles = [e.role for e in ROLES if e.profile_applies]
    assert profile_roles
    for role in profile_roles:
        read = ProfileVersionsDTO.model_validate(
            _fixture(f"prompts-{role}-versions.json")
        )
        assert read.role == role
        # The console shows a preview and a history only from one generation.
        preview = PromptPreviewDTO.model_validate(_fixture(f"prompts-{role}.json"))
        assert preview.profile_generation == read.generation, role
        # Nor a table of outcomes from another generation than its history.
        outcomes = PromptOutcomesDTO.model_validate(
            _fixture(f"prompts-{role}-outcomes.json")
        )
        assert outcomes.generation == read.generation, role
    for entry in ROLES:
        if not entry.profile_applies:
            preview = _fixture(f"prompts-{entry.role}.json")
            assert preview["profile_generation"] is None, entry.role


def test_the_write_scenario_answers_parse_as_the_served_dto() -> None:
    """The answers ``ui-checks/prompts-write.mjs`` serves are ones the API sends."""
    scenario = _fixture("../scenarios/prompts-versions.json")
    for key in ("empty", "proposed", "approved"):
        ProfileVersionsDTO.model_validate(scenario[key])
    preview = PromptPreviewDTO.model_validate(scenario["preview_approved"])
    assert preview.variant["profile"] == "v1"
    for key, history in (
        ("preview_approved", scenario["approved"]),
        ("preview_inner_heading", None),
    ):
        served = PromptPreviewDTO.model_validate(scenario[key])
        assert served.addendum is not None, key
        assert served.profile_generation == scenario["approved"]["generation"], key
        block = served.preview[served.addendum.start : served.addendum.end]
        assert block.startswith("## Operator instructions — dispatch profile v1\n")
        if history is not None:
            assert block.endswith(history["versions"][0]["body"])
    inner = scenario["preview_inner_heading"]
    assert "\n## Deployment\n" in inner["preview"][: inner["addendum"]["end"]]
    assert scenario["approve_refused"]["status"] == 403
    assert scenario["approve_stale"]["status"] == 409


def test_each_preview_fixture_says_which_config_rendered_it_as_the_service_does() -> (
    None
):
    """``prompt_config_rendered`` (#3071) is what ``role_preview`` serves for the
    fixtures' inputs: the packaged base, no project root, no store."""
    from kew.prompt_previews import PREVIEW_RENDERERS
    from kew.service.prompts import role_preview

    for entry in ROLES:
        served = role_preview(entry.role, PREVIEW_RENDERERS)
        preview = _fixture(f"prompts-{entry.role}.json")
        assert preview["prompt_config_rendered"] is served.prompt_config_rendered, (
            entry.role
        )


def test_the_outcomes_scenario_answers_parse_as_the_served_dto() -> None:
    """The answers ``ui-checks/prompts-outcomes.mjs`` serves are ones the API
    sends, and the approval it walks adds the approved version's row only."""
    scenario = _fixture("../scenarios/prompts-outcomes.json")
    for key in ("dispatch", "adversarial_review"):
        PromptOutcomesDTO.model_validate(scenario[key])
    before = PromptOutcomesDTO.model_validate(scenario["approval"]["before"])
    after = PromptOutcomesDTO.model_validate(scenario["approval"]["after"])
    assert [v.label for v in before.versions] == ["built-in"]
    first = after.versions[0]
    assert (first.label, first.state, first.n) == ("v1", "active", 0)
    assert after.versions[1:] == before.versions
    # The approval is one decision: the table after it is one generation on.
    assert before.generation is not None
    assert after.generation == before.generation + 1


def test_the_outcomes_scenario_tables_match_their_history_and_preview() -> None:
    """Each table ``ui-checks/prompts-outcomes.mjs`` draws is served beside the
    history and preview of one store, so the three name one generation, and
    the active version the history names is the one the table calls active."""
    scenario = _fixture("../scenarios/prompts-outcomes.json")
    for role in ("dispatch", "adversarial_review"):
        outcomes = PromptOutcomesDTO.model_validate(scenario[role])
        history = ProfileVersionsDTO.model_validate(scenario["history"][role])
        preview = PromptPreviewDTO.model_validate(scenario["preview"][role])
        assert outcomes.generation == history.generation, role
        assert preview.profile_generation == history.generation, role
        active = [v.version for v in outcomes.versions if v.state == "active"]
        assert active == [history.active_version], role
        states = {v.version: v.state for v in history.versions}
        for row in outcomes.versions:
            if row.version is not None:
                assert states[row.version] == row.state, (role, row.label)
