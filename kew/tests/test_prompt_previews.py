"""The dispatch and reviewer previews take the shape a launch would (#3018).

Each preview is checked against the production builder called with the same
inputs, so a preview that drifts from the launch fails here. The dispatch
preview shows the containment the runner's code and the config declare, and
asks no host probe or credential.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
import subprocess
from pathlib import Path

import pytest
from support.reviewer_artifact import commit_reviewer_artifact, git_repo

from kew.config import DispatchConfig
from kew.dispatch_core import dispatch_publication_style
from kew.prompt import build_prompt
from kew.prompt_previews import (
    PREVIEW_RENDERERS,
    SAMPLE_CRITERION,
    SAMPLE_INTEGRITY,
    SAMPLE_PLAN_ROW,
)
from kew.review_contract import load_reviewer_prompt, reviewer_prompt_artifact
from kew.review_dispatch import assemble_reviewer_prompt, reviewer_prompt_head
from kew.runners.registry import resolve_runner
from kew.sandbox import SandboxRuntimeUnavailable, resolve_effective_sandbox
from kew.service.prompts import (
    SAMPLE_BODY,
    SAMPLE_ISSUE_NUM,
    SAMPLE_TITLE,
    list_roles,
    role_preview,
)

_SENTINEL = re.compile(r"<<<KEW-UNTRUSTED-[0-9a-f]+>>>")


def _pinned(text: str) -> str:
    return _SENTINEL.sub("<<<KEW-UNTRUSTED-sample>>>", text)


def _config(**sections: dict) -> DispatchConfig:
    return DispatchConfig.model_validate(sections)


_UNCONTAINED = {"enabled": False, "dangerously_skip_containment": True}


@pytest.mark.parametrize(
    ("sections", "posture"),
    [
        pytest.param({}, "contained", id="canonical-contained"),
        pytest.param(
            {"publication": {"publish": "checkout"}},
            "contained",
            id="checkout-contained",
        ),
        pytest.param({"sandbox": _UNCONTAINED}, "uncontained", id="uncontained"),
        pytest.param(
            {"publication": {"publish": "checkout"}, "sandbox": _UNCONTAINED},
            "uncontained",
            id="checkout-uncontained",
        ),
        pytest.param(
            {"sandbox": {"enabled": True, "runtime": "openshell"}},
            "contained",
            id="required-openshell",
        ),
        pytest.param(
            {"publication": {"publish": "checkout"}, "agent": {"runner": "codex"}},
            "native",
            id="checkout-native-host-runner",
        ),
        pytest.param(
            {
                "agent": {"runner": "lemonade"},
                "sandbox": {"dangerously_skip_containment": True},
            },
            "uncontained",
            id="uncontainable-skipped",
        ),
    ],
)
def test_the_dispatch_preview_is_the_prompt_its_declared_launch_would_build(
    tmp_path: Path, sections: dict, posture: str
) -> None:
    config = _config(**sections)
    runner = resolve_runner(config, "medium").runner
    sandboxed = posture == "contained"
    publication = dispatch_publication_style(config, runner, sandboxed=sandboxed)
    dto = role_preview("dispatch", PREVIEW_RENDERERS, config, tmp_path)
    expected = build_prompt(
        issue_num=SAMPLE_ISSUE_NUM,
        issue_title=SAMPLE_TITLE,
        issue_body=SAMPLE_BODY,
        complexity="medium",
        config=config,
        has_coderabbit=False,
        sandboxed=sandboxed,
        publication_style=publication,
        runner_name=runner.name,
        rubric_root=tmp_path,
    )
    assert dto.preview == _pinned(expected)
    variant: dict[str, str | bool] = {
        "runner": runner.name,
        "admission": "decided at launch",
        "posture": posture,
        "contained": posture != "uncontained",
        "publication": publication,
        # No host store was handed in, so no active profile (#3023).
        "profile": "none",
    }
    if sandboxed:
        variant["runtime"] = config.sandbox.runtime
    assert dto.variant == variant


def test_a_sandbox_off_without_the_skip_previews_as_refused(tmp_path: Path) -> None:
    dto = role_preview(
        "dispatch", PREVIEW_RENDERERS, _config(sandbox={"enabled": False}), tmp_path
    )
    assert dto.preview == ""
    assert dto.variant == {
        "runner": "claude-code",
        "admission": "decided at launch",
        "posture": "refused",
        "refusal": (
            "The sandbox is off and containment is not skipped, "
            "so a dispatch is refused."
        ),
        "profile": "none",
    }


@pytest.mark.parametrize(
    "sections",
    [
        pytest.param({"agent": {"runner": "codex"}}, id="codex-docker"),
        pytest.param(
            {"agent": {"runner": "codex"}, "sandbox": {"enabled": False}},
            id="codex-sandbox-off",
        ),
    ],
)
def test_a_natively_contained_runner_previews_the_posture_admission_gives_it(
    tmp_path: Path, sections: dict
) -> None:
    # Native containment is declared in runner code, so the resolver answers
    # it before any probe or credential — and the preview must agree.
    config = _config(**sections)
    decision = resolve_effective_sandbox(config.sandbox, runner="codex")
    dto = role_preview("dispatch", PREVIEW_RENDERERS, config, tmp_path)
    assert decision.sandboxed is False
    assert dto.variant["posture"] == decision.posture == "native"
    assert dto.variant["publication"] == "host"
    assert "runtime" not in dto.variant


def test_an_uncontainable_runner_previews_as_the_refusal_its_launch_raises(
    tmp_path: Path,
) -> None:
    config = _config(agent={"runner": "lemonade"})
    with pytest.raises(SandboxRuntimeUnavailable) as refused:
        resolve_effective_sandbox(config.sandbox, runner="lemonade")
    dto = role_preview("dispatch", PREVIEW_RENDERERS, config, tmp_path)
    assert dto.preview == ""
    assert dto.variant == {
        "runner": "lemonade",
        "admission": "decided at launch",
        "posture": "refused",
        "refusal": str(refused.value),
        "profile": "none",
    }


def test_an_openshell_preview_starts_no_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the dispatch preview started a process")

    monkeypatch.setattr("kew.subprocess_utils.run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    config = _config(
        sandbox={
            "runtime": "openshell",
            "openshell": {"providers": {"claude-code": "kew-claude-code"}},
        }
    )
    dto = role_preview("dispatch", PREVIEW_RENDERERS, config, tmp_path)
    assert dto.variant["posture"] == "contained"
    assert dto.variant["runtime"] == "openshell"
    assert SAMPLE_TITLE in dto.preview


def test_a_checkout_publication_preview_tells_the_agent_to_commit(
    tmp_path: Path,
) -> None:
    checkout = _config(publication={"publish": "checkout"})
    agent = role_preview("dispatch", PREVIEW_RENDERERS, checkout, tmp_path).preview
    host = role_preview("dispatch", PREVIEW_RENDERERS, None, tmp_path).preview
    assert "git commit" in agent
    assert agent != host


def _deploy(root: Path, text: str) -> str:
    """Deploy *text* on *root*'s base ref, where a review launch reads it."""
    return commit_reviewer_artifact(git_repo(root), text)


def test_a_deployed_reviewer_copy_is_the_preview_and_the_source(
    tmp_path: Path,
) -> None:
    deployed = "# Deployed reviewer rubric\n\nReview with care.\n"
    _deploy(tmp_path, deployed)
    dto = role_preview("adversarial_review", PREVIEW_RENDERERS, None, tmp_path)
    assert dto.preview.startswith(deployed)
    assert dto.variant["artifact"] == "deployed"
    assert dto.source.kind == "deployed-artifact"
    assert dto.source.name == reviewer_prompt_artifact().target
    assert dto.source.sha256 == hashlib.sha256(deployed.encode()).hexdigest()
    listed = next(r for r in list_roles(tmp_path) if r.role == "adversarial_review")
    assert listed.source == dto.source


def test_the_reviewer_preview_carries_the_sample_criterion(tmp_path: Path) -> None:
    dto = role_preview("adversarial_review", PREVIEW_RENDERERS, None, tmp_path)
    assert SAMPLE_CRITERION.id in dto.preview
    assert SAMPLE_CRITERION.statement in dto.preview
    assert SAMPLE_PLAN_ROW.plan_sha in dto.preview
    assert dto.variant == {
        "artifact": "pinned",
        "review_mode": "full",
        "criteria": True,
        "frontend": False,
        "integrity": "clean",
        "profile": "none",
    }


def test_the_reviewer_preview_is_the_shared_assembly(tmp_path: Path) -> None:
    config = DispatchConfig()
    text, sha = load_reviewer_prompt(base_ref_root=tmp_path)
    build = reviewer_prompt_head(
        text,
        sha,
        SAMPLE_PLAN_ROW,
        item_id=SAMPLE_PLAN_ROW.item_id,
        max_review_usd=config.review.max_review_usd,
    )
    expected = assemble_reviewer_prompt(
        build, mode_section="", frontend_section="", integrity=SAMPLE_INTEGRITY
    )
    dto = role_preview("adversarial_review", PREVIEW_RENDERERS, config, tmp_path)
    assert dto.preview == _pinned(expected)


def test_the_reviewer_source_and_preview_are_one_copy_when_it_changes_mid_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployed copy replaced after it is read must not split the response."""
    import kew.service.prompts as catalog

    _deploy(tmp_path, "# First copy\n")
    resolve = catalog.resolve_reviewer_prompt

    def resolve_then_replace(root: Path | None = None, *args, **kwargs):
        loaded = resolve(root, *args, **kwargs)
        _deploy(tmp_path, "# Replaced copy\n")
        return loaded

    monkeypatch.setattr(catalog, "resolve_reviewer_prompt", resolve_then_replace)
    dto = role_preview("adversarial_review", PREVIEW_RENDERERS, None, tmp_path)
    assert dto.preview.startswith("# First copy\n")
    assert dto.source.sha256 == hashlib.sha256(b"# First copy\n").hexdigest()


def test_a_deployed_artifacts_sentinel_shaped_literal_is_left_as_written(
    tmp_path: Path,
) -> None:
    literal = "<<<KEW-UNTRUSTED-" + "deadbeef" + ">>>"
    deployed = f"# Deployed reviewer rubric\n\nQuote {literal} as it stands.\n"
    _deploy(tmp_path, deployed)
    dto = role_preview("adversarial_review", PREVIEW_RENDERERS, None, tmp_path)
    assert dto.preview.startswith(deployed)
    head = dto.preview[: len(deployed)].encode("utf-8")
    assert dto.source.sha256 == hashlib.sha256(head).hexdigest()


def test_packaged_bytes_off_the_manifest_pin_report_their_own_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kew.review_contract as contract

    real = contract.load_manifest()

    def stale_pin() -> list:
        return [
            dataclasses.replace(a, sha256="0" * 64)
            if a.name == "adversarial-reviewer"
            else a
            for a in real
        ]

    monkeypatch.setattr(contract, "load_manifest", stale_pin)
    text, sha = load_reviewer_prompt(base_ref_root=tmp_path)
    dto = role_preview("adversarial_review", PREVIEW_RENDERERS, None, tmp_path)
    assert dto.source.kind == "pinned-artifact"
    assert dto.source.sha256 == sha
    assert dto.source.pin_mismatch is True
    assert dto.preview.startswith(text)


def test_packaged_bytes_on_the_pin_report_no_mismatch(tmp_path: Path) -> None:
    dto = role_preview("adversarial_review", PREVIEW_RENDERERS, None, tmp_path)
    assert dto.source.sha256 == reviewer_prompt_artifact().sha256
    assert dto.source.pin_mismatch is False


def test_a_checkout_publication_preview_carries_the_served_append(
    tmp_path: Path,
) -> None:
    config = _config(
        publication={"publish": "checkout"}, prompt={"append": "Run extra checks"}
    )
    dto = role_preview("dispatch", PREVIEW_RENDERERS, config, tmp_path)
    assert dto.variant["publication"] == "agent"
    assert "Run extra checks" in dto.preview
    assert "refusal" not in dto.variant


@pytest.mark.parametrize(
    "prompt",
    [
        pytest.param({"append": "Run extra checks"}, id="append"),
        pytest.param({"template": "custom.md.j2"}, id="template"),
    ],
)
def test_a_host_publication_preview_with_a_prompt_override_is_refused(
    tmp_path: Path, prompt: dict[str, str]
) -> None:
    dto = role_preview("dispatch", PREVIEW_RENDERERS, _config(prompt=prompt), tmp_path)
    base = role_preview("dispatch", PREVIEW_RENDERERS, None, tmp_path)
    assert dto.preview == base.preview
    assert dto.preview_sha256 == base.preview_sha256
    assert dto.variant == {**base.variant, "refusal": dto.variant["refusal"]}
    assert dto.variant["publication"] == "host"
    assert dto.variant["refusal"] == (
        "host publication requires the built-in prompt without appended instructions"
    )
    assert dto.variant["profile"] == "none"


def test_roles_prompt_does_not_apply_to_preview_without_the_append(
    tmp_path: Path,
) -> None:
    config = _config(prompt={"append": "Run extra checks"})
    for role in ("adversarial_review", "review_remediation", "triage"):
        dto = role_preview(role, PREVIEW_RENDERERS, config, tmp_path)
        assert "Run extra checks" not in dto.preview, role
        assert "refusal" not in dto.variant, role


def test_a_missing_custom_template_previews_as_a_refusal(tmp_path: Path) -> None:
    missing = tmp_path / "absent.md.j2"
    config = _config(
        publication={"publish": "checkout"}, prompt={"template": str(missing)}
    )
    dto = role_preview("dispatch", PREVIEW_RENDERERS, config, tmp_path)
    base = role_preview(
        "dispatch",
        PREVIEW_RENDERERS,
        _config(publication={"publish": "checkout"}),
        tmp_path,
    )
    assert dto.preview == base.preview
    assert str(dto.variant["refusal"]).startswith(
        "the [prompt] template cannot be rendered: 'absent.md.j2' not found"
    )


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"{{ 1 / 0 }}\n", "ZeroDivisionError: division by zero"),
        (b"\xff\xfe not utf-8\n", "UnicodeDecodeError: "),
    ],
)
def test_a_custom_template_that_raises_previews_as_a_refusal(
    tmp_path: Path, content: bytes, reason: str
) -> None:
    template = tmp_path / "custom.md.j2"
    template.write_bytes(content)
    config = _config(
        publication={"publish": "checkout"}, prompt={"template": str(template)}
    )
    dto = role_preview("dispatch", PREVIEW_RENDERERS, config, tmp_path)
    base = role_preview(
        "dispatch",
        PREVIEW_RENDERERS,
        _config(publication={"publish": "checkout"}),
        tmp_path,
    )
    assert dto.preview == base.preview
    assert str(dto.variant["refusal"]).startswith(
        f"the [prompt] template cannot be rendered: {reason}"
    )


def test_a_checkout_publication_preview_renders_the_served_template(
    tmp_path: Path,
) -> None:
    template = tmp_path / "custom.md.j2"
    template.write_text("Custom prompt for {{ issue_num }}\n")
    config = _config(
        publication={"publish": "checkout"}, prompt={"template": str(template)}
    )
    dto = role_preview("dispatch", PREVIEW_RENDERERS, config, tmp_path)
    assert dto.preview == "Custom prompt for 1\n"
    assert "refusal" not in dto.variant
