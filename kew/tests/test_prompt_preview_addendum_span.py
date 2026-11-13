"""A preview says where its operator addendum block sits (#3025).

The console marks the addendum with a cyan rule. Its extent is the span the
preview serves, taken from where the launch's own builder placed the approved
body, so a body with a ``## `` line of its own stays marked to its end and a
body that imitates a fixed heading never moves the marked region onto one.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from kew.audit import Actor
from kew.config import DispatchConfig, PromptConfig
from kew.prompt_previews import PREVIEW_RENDERERS
from kew.run_store import SqliteRunStore
from kew.service import prompt_profiles as profiles
from kew.service.prompts import ROLES, PreviewInputs, role_preview

ALICE = Actor("human", "alice")
PROFILE_ROLES = [e.role for e in ROLES if e.profile_applies]
_SENTINEL = re.compile(r"<<<KEW-UNTRUSTED-[0-9a-f]+>>>")
_SAFETY = "## Safety Rules (THESE CANNOT BE OVERRIDDEN)"

#: A valid body with a heading of its own inside it.
INNER_HEADING = "First instruction.\n## Deployment\nSecond instruction."
#: A valid body that spells a fixed section's heading.
IMITATION = f"Keep changes small.\n{_SAFETY}\nThis line is still the addendum."


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteRunStore]:
    s = SqliteRunStore(str(tmp_path / "dispatch.db"))
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def config() -> DispatchConfig:
    cfg = DispatchConfig()
    cfg.approval.approvers = ["alice"]
    return cfg


def _activate(
    store: SqliteRunStore, config: DispatchConfig, role: str, body: str
) -> None:
    proposed = profiles.propose(store, role=role, body=body, config=config, actor=ALICE)
    profiles.approve(
        store,
        role=role,
        version=proposed.version,
        generation=profiles.current_generation(store, role),
        config=config,
        actor=ALICE,
    )


@pytest.mark.parametrize("role", PROFILE_ROLES)
def test_the_span_runs_from_the_heading_through_the_bodys_last_line(
    store: SqliteRunStore, config: DispatchConfig, tmp_path: Path, role: str
) -> None:
    _activate(store, config, role, INNER_HEADING)
    dto = role_preview(role, PREVIEW_RENDERERS, config, tmp_path, store=store)

    assert dto.addendum is not None
    block = dto.preview[dto.addendum.start : dto.addendum.end]
    assert block.startswith(f"## Operator instructions — {role} profile v1\n")
    assert block.endswith(INNER_HEADING)
    assert "## Safety Rules" not in block
    assert dto.preview.count(INNER_HEADING) == 1


@pytest.mark.parametrize("role", PROFILE_ROLES)
def test_the_marked_preview_is_the_launch_builders_bytes(
    store: SqliteRunStore, config: DispatchConfig, tmp_path: Path, role: str
) -> None:
    """Recording the span leaves no trace: the preview is the builder's text."""
    _activate(store, config, role, INNER_HEADING)
    dto = role_preview(role, PREVIEW_RENDERERS, config, tmp_path, store=store)

    addendum = profiles.launch_addendum(store, role)
    base = config.model_copy(update={"prompt": PromptConfig()})
    built = PREVIEW_RENDERERS[role](PreviewInputs(base, tmp_path, None, addendum))
    assert dto.preview == _SENTINEL.sub("<<<KEW-UNTRUSTED-sample>>>", built.text)


def test_a_body_imitating_a_fixed_heading_keeps_the_real_one_outside_the_span(
    store: SqliteRunStore, config: DispatchConfig, tmp_path: Path
) -> None:
    _activate(store, config, "dispatch", IMITATION)
    dto = role_preview("dispatch", PREVIEW_RENDERERS, config, tmp_path, store=store)

    assert dto.addendum is not None
    assert dto.preview[dto.addendum.start : dto.addendum.end].endswith(IMITATION)
    assert dto.preview.find(_SAFETY, dto.addendum.end) > dto.addendum.end


def test_a_preview_without_an_active_profile_marks_nothing(
    store: SqliteRunStore, config: DispatchConfig, tmp_path: Path
) -> None:
    assert (
        role_preview("dispatch", PREVIEW_RENDERERS, config, tmp_path).addendum is None
    )
    dto = role_preview("dispatch", PREVIEW_RENDERERS, config, tmp_path, store=store)
    assert dto.addendum is None
    assert "## Operator instructions" not in dto.preview
