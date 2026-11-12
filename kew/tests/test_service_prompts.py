"""The prompt catalog: one entry per agent role, and a base-prompt preview (#3018)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kew.config import DispatchConfig, PromptConfig
from kew.prompt_previews import PREVIEW_RENDERERS
from kew.run_store import KNOWN_RUN_KINDS
from kew.service.prompts import (
    NON_AGENT_RUN_KINDS,
    ROLES,
    SAMPLE_BODY,
    SAMPLE_TITLE,
    PreviewUnavailable,
    UnknownPromptRole,
    list_roles,
    render_preview,
    role_preview,
)

_PKG = Path(__file__).resolve().parents[1] / "src" / "kew"


@pytest.mark.policy
def test_every_run_kind_that_launches_an_agent_has_a_role() -> None:
    covered = {kind for entry in ROLES for kind in entry.run_kinds}
    missing = KNOWN_RUN_KINDS - NON_AGENT_RUN_KINDS - covered
    assert not missing, f"run kinds with no prompts.ROLES entry: {sorted(missing)}"


@pytest.mark.policy
def test_every_role_names_only_known_run_kinds() -> None:
    named = {kind for entry in ROLES for kind in entry.run_kinds}
    assert named | NON_AGENT_RUN_KINDS <= KNOWN_RUN_KINDS


def test_role_ids_are_unique() -> None:
    ids = [entry.role for entry in ROLES]
    assert len(ids) == len(set(ids))


def test_a_profile_may_apply_only_to_the_five_profiled_roles() -> None:
    profiled = {entry.role for entry in ROLES if entry.profile_applies}
    assert profiled == {
        "dispatch",
        "adversarial_review",
        "review_remediation",
        "resolve",
        "ci_fix",
    }


def test_prompt_config_applies_to_dispatch_only() -> None:
    assert [e.role for e in ROLES if e.prompt_config_applies] == ["dispatch"]


def test_every_packaged_template_source_is_a_shipped_template() -> None:
    for entry in ROLES:
        if entry.source_kind == "packaged-template":
            assert (_PKG / "_templates" / entry.source_name).is_file(), entry.role


def test_the_reviewer_source_is_the_manifest_pin() -> None:
    manifest = json.loads((_PKG / "_skills" / "manifest.json").read_text())
    pin = next(a for a in manifest["artifacts"] if a["name"] == "adversarial-reviewer")
    reviewer = next(r for r in list_roles() if r.role == "adversarial_review")
    assert reviewer.source.kind == "pinned-artifact"
    assert reviewer.source.name == "adversarial-reviewer"
    assert reviewer.source.version == pin["version"]
    assert reviewer.source.sha256 == pin["sha256"]


def test_a_non_artifact_source_carries_no_version() -> None:
    dispatch = next(r for r in list_roles() if r.role == "dispatch")
    assert dispatch.source.kind == "packaged-template"
    assert dispatch.source.name == "dispatch.md.j2"
    assert dispatch.source.version is None and dispatch.source.sha256 is None


def test_a_prompt_section_without_a_template_leaves_the_packaged_source() -> None:
    config = DispatchConfig(prompt=PromptConfig(append="EXTRA"))
    dispatch = next(r for r in list_roles(None, config) if r.role == "dispatch")
    assert (dispatch.source.kind, dispatch.source.name) == (
        "packaged-template",
        "dispatch.md.j2",
    )


def test_a_configured_template_is_the_source_of_the_roles_it_applies_to() -> None:
    config = DispatchConfig(prompt=PromptConfig(template="prompts/custom.md.j2"))
    sources = {r.role: r.source for r in list_roles(None, config)}
    assert (sources["dispatch"].kind, sources["dispatch"].name) == (
        "config-template",
        "prompts/custom.md.j2",
    )
    # A role [prompt] never applies to keeps its own source.
    assert (sources["triage"].kind, sources["triage"].name) == (
        "packaged-template",
        "triage.md.j2",
    )
    preview = role_preview("dispatch", PREVIEW_RENDERERS, config)
    assert preview.source == sources["dispatch"]


@pytest.mark.parametrize("role", [entry.role for entry in ROLES])
def test_every_role_renders_the_same_preview_on_every_read(role: str) -> None:
    first = render_preview(role, PREVIEW_RENDERERS)
    assert first.strip()
    assert render_preview(role, PREVIEW_RENDERERS) == first


def test_the_dispatch_preview_carries_the_sample_item_and_no_repo_map() -> None:
    preview = render_preview("dispatch", PREVIEW_RENDERERS)
    assert SAMPLE_TITLE in preview
    assert SAMPLE_BODY.splitlines()[0] in preview
    assert "## Repo Map" not in preview


def test_the_preview_is_the_base_without_the_operators_prompt_section() -> None:
    config = DispatchConfig(
        prompt=PromptConfig(append="OPERATOR-APPENDED-INSTRUCTIONS")
    )
    assert "OPERATOR-APPENDED-INSTRUCTIONS" not in render_preview(
        "dispatch", PREVIEW_RENDERERS, config
    )


def test_the_reviewer_preview_starts_with_the_packaged_artifact() -> None:
    artifact = (_PKG / "_skills" / "adversarial-reviewer" / "PROMPT.md").read_text()
    assert render_preview("adversarial_review", PREVIEW_RENDERERS).startswith(artifact)


def test_the_preview_sha256_is_the_previews_own_digest() -> None:
    dto = role_preview("adversarial_review", PREVIEW_RENDERERS)
    assert dto.preview_sha256 == hashlib.sha256(dto.preview.encode()).hexdigest()


def test_an_unknown_role_is_refused() -> None:
    with pytest.raises(UnknownPromptRole):
        render_preview("no_such_role", PREVIEW_RENDERERS)


def test_a_role_with_no_renderer_handed_in_is_unavailable() -> None:
    with pytest.raises(PreviewUnavailable):
        render_preview("dispatch", {})


def test_every_role_has_exactly_one_preview_renderer() -> None:
    assert set(PREVIEW_RENDERERS) == {entry.role for entry in ROLES}


def test_the_planning_preview_names_the_sample_epic() -> None:
    assert "sample-plan.md" in render_preview("planning", PREVIEW_RENDERERS)
