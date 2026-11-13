"""Item-specific acceptance criteria on every planned issue (#2292).

`kew epic plan` used to stamp the same four boilerplate criteria into every
generated issue, so a review station that extracts the criteria would judge
the merged work against nothing. These tests pin the three halves of the fix:
the checklist parser, the boilerplate deny-list, and the draft rejections.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from kew.config import DispatchConfig
from kew.criteria import (
    CRITERIA_MAX,
    CRITERIA_MIN,
    criteria_problems,
    extract_criteria,
    is_generic_criterion,
    is_placeholder_criterion,
)
from kew.graph.plan import EpicPlanManifest, build_epic_plan_prompt, validate_manifest

_GOOD = (
    "- [ ] `extract_criteria()` reads the checklist under "
    "`## Acceptance Criteria` (`uv run pytest tests/test_epic_plan_criteria.py`)\n"
    "- [ ] A draft with one criterion is rejected by name "
    "(`uv run pytest tests/test_epic_plan_criteria.py -k below`)\n"
)


def _body(criteria: str = _GOOD, *, plan: str = "docs/design/x.md") -> str:
    return (
        f"## Plan Reference\n\n**Plan file:** `{plan}`\n\n"
        f"## Acceptance Criteria\n\n{criteria}\n"
        "## Context for the Agent\n\n- `src/kew/criteria.py` — the gate\n"
    )


def _issue(lid: str, **over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "local_id": lid,
        "title": f"feat: part {lid} (Tasks 1-2)",
        "body": _body(),
        "priority": "p2-medium",
        "complexity": "simple",
        "type": "feature",
        "depends_on": [],
    }
    d.update(over)
    return d


def _problems(body: str) -> list[str]:
    m = EpicPlanManifest.model_validate(
        {
            "epic": "demo",
            "plan_path": "docs/design/x.md",
            "issues": [_issue("a", body=body)],
        }
    )
    return validate_manifest(m)


class TestExtractCriteria:
    def test_reads_the_checklist_under_the_heading(self) -> None:
        assert extract_criteria(_body()) == [
            "`extract_criteria()` reads the checklist under "
            "`## Acceptance Criteria` (`uv run pytest "
            "tests/test_epic_plan_criteria.py`)",
            "A draft with one criterion is rejected by name "
            "(`uv run pytest tests/test_epic_plan_criteria.py -k below`)",
        ]

    def test_section_ends_at_the_next_heading(self) -> None:
        body = _body() + "\n## More\n\n- [ ] not a criterion\n"
        assert len(extract_criteria(body)) == 2

    def test_ignores_prose_blanks_and_bullet_style(self) -> None:
        body = (
            "## Acceptance Criteria\n\nEach line names the command:\n\n"
            "* [x] `kew epic plan --dry-run` prints each criterion\n"
            "- plain bullets are not criteria\n"
            "+ [ ] `validate_manifest` names the offending node\n"
        )
        assert extract_criteria(body) == [
            "`kew epic plan --dry-run` prints each criterion",
            "`validate_manifest` names the offending node",
        ]

    @pytest.mark.parametrize("body", ["", None, "## Summary\n\n- [ ] something\n"])
    def test_no_section_reads_as_no_criteria(self, body: str | None) -> None:
        assert extract_criteria(body) == []


class TestGenericDenyList:
    @pytest.mark.parametrize(
        "text",
        [
            "All new and existing tests pass",
            "All tests pass",
            "Tests pass",
            "CI passes",
            "CI gate passes (as configured in kew.toml `commands.ci`)",
            "Format check passes (as configured in kew.toml `commands.format`)",
            "All tasks 3-7 from the plan are implemented",
            # The form the template, the docs and the shipped skill all name as
            # refused: the range is a placeholder, not digits (round 2 of #2310).
            "All tasks N-M from the plan are implemented",
            "All tasks N-M implemented",
            "All tasks N–M from the plan are implemented",
            "All tasks 1 to 4 from the plan are complete",
            "All tasks are implemented",
            "No regressions",
            "Documentation updated",
            "Code reviewed",
            "`ruff check .` passes",
            "The full test suite is green",
        ],
    )
    def test_boilerplate_is_generic(self, text: str) -> None:
        assert is_generic_criterion(text), text

    @pytest.mark.parametrize(
        "text",
        [
            "`extract_criteria()` returns [] when the heading is absent "
            "(`uv run pytest tests/test_epic_plan_criteria.py -k absent`)",
            "A draft with 7 criteria is rejected naming the node and the count",
            "`kew epic plan --dry-run` prints every criterion per node",
            "The epic-plan template's example body parses as a checklist",
        ],
    )
    def test_specific_criteria_survive(self, text: str) -> None:
        assert not is_generic_criterion(text), text


class TestPlaceholderCriteria:
    """The skeleton the prompt prints is not a criterion (round 2 of #2310):
    a draft that copies the template verbatim judges the work against nothing,
    and the deny-list cannot see it — the skeleton's words differ per line."""

    @pytest.mark.parametrize(
        "text",
        [
            "[Observable outcome these tasks produce] (`command that proves it`)",
            "[Another outcome, naming the real symbol or file] (`cmd`)",
            "[... 2 to 6 lines; no boilerplate]",
            "`parse_window()` accepts `7d` [and whatever else]",
            # Round 3 of #2310: the prompt promises every `[...]` is refused,
            # so a one-word stub is a placeholder too — it was accepted while
            # the pattern required whitespace inside the brackets.
            "[Outcome]",
            "[TODO]",
            "[outcome] (`uv run pytest tests/test_x.py`)",
        ],
    )
    def test_bracketed_skeleton_is_a_placeholder(self, text: str) -> None:
        assert is_placeholder_criterion(text), text

    @pytest.mark.parametrize(
        "text",
        [
            "`extract_criteria()` returns [] when the heading is absent "
            "(`uv run pytest tests/test_epic_plan_criteria.py -k absent`)",
            "`_problems(body)[0]` names the offending node (`uv run pytest`)",
            "`rows[0]` holds the newest run (`uv run kew report`)",
            "The [dry run](docs/cli/epic.md) prints every criterion",
            "`kew epic plan --dry-run` prints every criterion per node",
            # Brackets written as syntax against the token before them, with
            # the backticks a hurried draft forgets: still not a placeholder.
            "extract_criteria() returns list[str] (`uv run pytest -k reads`)",
            "rows[0] holds the newest run (`uv run kew report`)",
            "_problems(body)[0] names the offending node (`uv run pytest`)",
        ],
    )
    def test_real_criteria_are_not_placeholders(self, text: str) -> None:
        assert not is_placeholder_criterion(text), text

    def test_draft_of_one_word_stubs_is_refused(self) -> None:
        """Round 3 of #2310: `[Outcome]`/`[TODO]` cleared the gate with zero
        problems, so a body of pure stubs read as two filled criteria."""
        body = _body("- [ ] [Outcome]\n- [ ] [TODO]\n")
        problems = [p for p in _problems(body) if "unfilled" in p]
        assert len(problems) == 1, _problems(body)
        assert "[Outcome]" in problems[0] and "[TODO]" in problems[0]

    def test_draft_copying_the_skeleton_is_refused_naming_the_lines(self) -> None:
        body = _body(
            "- [ ] [Observable outcome these tasks produce] "
            "(`command that proves it`)\n"
            "- [ ] [Another outcome, naming the real symbol or file] (`cmd`)\n"
        )
        problems = [p for p in _problems(body) if "unfilled" in p]
        assert len(problems) == 1, _problems(body)
        assert "Observable outcome these tasks produce" in problems[0]
        assert "Another outcome" in problems[0]


class TestCriteriaProblems:
    def test_missing_section_is_one_problem_naming_the_range(self) -> None:
        problems = criteria_problems("## Summary\n\nno criteria here\n", "'a'")
        assert len(problems) == 1
        assert "Acceptance Criteria" in problems[0]
        assert f"{CRITERIA_MIN}-{CRITERIA_MAX}" in problems[0]

    def test_min_and_max_bounds_are_the_issue_contract(self) -> None:
        assert (CRITERIA_MIN, CRITERIA_MAX) == (2, 6)


class TestValidateManifest:
    def test_rejects_missing_criteria(self) -> None:
        problems = _problems("## Plan Reference\n\n`docs/design/x.md`\n")
        assert any("Acceptance Criteria" in p and "'a'" in p for p in problems)

    def test_rejects_below_the_minimum(self) -> None:
        one = "- [ ] `extract_criteria()` reads the checklist (`uv run pytest`)\n"
        problems = _problems(_body(one))
        assert any("below the minimum 2" in p and "'a'" in p for p in problems)

    def test_rejects_above_the_maximum(self) -> None:
        seven = "".join(
            f"- [ ] `step_{i}()` returns the parsed row (`uv run pytest -k s{i}`)\n"
            for i in range(7)
        )
        problems = _problems(_body(seven))
        assert any("exceeds the maximum 6" in p and "'a'" in p for p in problems)

    def test_rejects_generic_criteria_naming_them(self) -> None:
        body = _body(
            "- [ ] `extract_criteria()` reads the checklist (`uv run pytest`)\n"
            "- [ ] All new and existing tests pass\n"
            "- [ ] CI gate passes (as configured in kew.toml `commands.ci`)\n"
        )
        problems = _problems(body)
        generic = [p for p in problems if "generic" in p]
        assert len(generic) == 1, problems
        assert "All new and existing tests pass" in generic[0]
        assert "CI gate passes" in generic[0]

    def test_accepts_two_to_six_specific_criteria(self) -> None:
        assert _problems(_body()) == []

    def test_old_boilerplate_body_is_refused_whole(self) -> None:
        # The exact section the template used to stamp into every issue.
        legacy = _body(
            "- [ ] All tasks 1-4 from the plan are implemented\n"
            "- [ ] All new and existing tests pass\n"
            "- [ ] CI gate passes (as configured in kew.toml `commands.ci`)\n"
            "- [ ] Format check passes (as configured in kew.toml `commands.format`)\n"
        )
        assert any("generic" in p for p in _problems(legacy))


class TestPromptContract:
    def test_prompt_states_the_bounds_and_the_deny_list(self) -> None:
        prompt = build_epic_plan_prompt(
            epic_name="demo", plan_path="docs/design/x.md", config=DispatchConfig()
        )
        assert f"{CRITERIA_MIN} to {CRITERIA_MAX}" in prompt
        assert "## Acceptance Criteria" in prompt
        assert "All new and existing tests pass" in prompt  # named as refused
        assert "CI gate passes" in prompt

    def test_template_body_skeleton_parses_but_is_refused_unfilled(self) -> None:
        """The body template's `- [ ]` lines read as a checklist of the right
        size and carry no boilerplate — but they are a skeleton, so a draft that
        copies them verbatim is refused rather than passing the gate."""
        prompt = build_epic_plan_prompt(
            epic_name="demo", plan_path="docs/design/x.md", config=DispatchConfig()
        )
        block = re.search(r"```markdown\n(.*?)```", prompt, re.DOTALL)
        assert block is not None, "issue body template block missing"
        example = extract_criteria(block.group(1))
        assert CRITERIA_MIN <= len(example) <= CRITERIA_MAX, example
        assert not [c for c in example if is_generic_criterion(c)], example
        problems = criteria_problems(block.group(1), "'skeleton'")
        assert [p for p in problems if "unfilled" in p], problems

    def test_prompt_says_the_skeleton_must_be_replaced(self) -> None:
        prompt = build_epic_plan_prompt(
            epic_name="demo", plan_path="docs/design/x.md", config=DispatchConfig()
        )
        assert "`[...]` placeholder" in prompt
