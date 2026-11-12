"""The planner marks each acceptance criterion's kind, and extraction reads it.

A criterion line may end in ``(kind: test)``, ``(kind: behavior)`` or
``(kind: static)``. The epic-plan prompt tells the planner when to use each,
and :func:`kew.triage.plan.extract_criteria` records the kind it finds; a line
without the marker stays ``behavior``.
"""

from __future__ import annotations

import re

import pytest

from kew.config import DispatchConfig
from kew.criteria import criteria_problems, is_generic_criterion
from kew.graph.plan import build_epic_plan_prompt
from kew.triage.plan import extract_criteria


def _prompt() -> str:
    return build_epic_plan_prompt(
        epic_name="demo", plan_path="docs/design/x.md", config=DispatchConfig()
    )


def _section(lines: str) -> str:
    return f"## Acceptance Criteria\n\n{lines}\n## Context\n\n- other\n"


def test_epic_plan_prompt_states_the_kind_rule() -> None:
    prompt = _prompt()
    assert "(kind: test)" in prompt
    assert "(kind: behavior)" in prompt
    assert "(kind: static)" in prompt
    rule = re.search(r"^- Mark each criterion's kind.*?(?=^- |\Z)", prompt, re.S | re.M)
    assert rule is not None, "kind rule missing from the acceptance-criteria rules"
    text = " ".join(rule.group(0).split())
    assert "automated test can decide it" in text
    assert "reviewer must judge it" in text
    assert "static check" in text
    assert "write no test files" in text


def test_prompt_example_criteria_carry_the_kind_they_name() -> None:
    """The prompt's own example lines parse into the kinds they mark."""
    prompt = _prompt()
    example = prompt.split("Specific enough:", 1)[1].split("## Manifest", 1)[0]
    lines = "\n".join(line.strip() for line in example.splitlines() if line.strip())
    kinds = [c.kind for c in extract_criteria(_section(lines))]
    assert kinds == ["test", "behavior"]


@pytest.mark.parametrize("kind", ["test", "behavior", "static"])
def test_extract_criteria_reads_the_kind_the_planner_wrote(kind: str) -> None:
    body = _section(
        f"- [ ] `parse_window()` accepts `7d` (`uv run pytest`) (kind: {kind})"
    )
    [criterion] = extract_criteria(body)
    assert criterion.kind == kind
    assert criterion.statement == "`parse_window()` accepts `7d` (`uv run pytest`)"


def test_criterion_without_a_kind_stays_behavior() -> None:
    [criterion] = extract_criteria(_section("- [ ] `kew report` prints rows"))
    assert criterion.kind == "behavior"
    assert criterion.statement == "`kew report` prints rows"


def test_kind_on_a_wrapped_continuation_line_is_read() -> None:
    body = _section("- [ ] `parse_window()` accepts `7d`\n  and `24h` (kind: test)")
    [criterion] = extract_criteria(body)
    assert criterion.kind == "test"
    assert criterion.statement == "`parse_window()` accepts `7d` and `24h`"


@pytest.mark.parametrize(
    "line",
    [
        "- [ ] a kind word mid-line (kind: test) is not a marker",
        "- [ ] an unknown kind stays prose (kind: manual)",
    ],
)
def test_only_a_trailing_known_kind_is_a_marker(line: str) -> None:
    [criterion] = extract_criteria(_section(line))
    assert criterion.kind == "behavior"
    assert criterion.statement == line.removeprefix("- [ ] ")


def test_the_kind_marker_does_not_hide_boilerplate() -> None:
    assert is_generic_criterion("All new and existing tests pass (kind: test)")
    problems = criteria_problems(
        _section("- [ ] `a()` returns 1 (kind: test)\n- [ ] [Outcome] (kind: test)"),
        "'x'",
    )
    assert [p for p in problems if "unfilled" in p], problems
