"""Extract-only acceptance-criteria reading (#2290).

The extractor is pure: a body in, criteria out, no model and no network. These
tests pin the real issue-body shapes kew's own tracker produces.
"""

from __future__ import annotations

import pytest

from kew.triage.plan import body_digest, extract_criteria, extract_plan, plan_sha
from kew.triage.schema import ItemPlan

_DESIGN_C2_BODY = """A node of the design-c2 epic.

### Why

The console's empty states read as errors.

### Change

- Give the work table its own empty copy.

### Acceptance

- The work table with no rows reads "Nothing queued", not an error.
- `uv run pytest tests/test_serve_api_work.py` passes.
- `ruff check . && ruff format --check .` from `kew/`.
"""

_CHECKLIST_BODY = """## Summary

Close the gap.

## Acceptance Criteria

- [ ] `kew plan show 2278` prints the criteria
- [x] The migration test passes
* [ ] The audit chain still verifies

## Notes

- Not a criterion.
"""

_NUMBERED_BODY = """### Acceptance

1. The extractor returns an empty list for a body with no section.
2) A changed body supersedes the old row.
3. The dispatch prompt carries the plan sha.
"""


def test_extracts_a_design_c2_acceptance_section() -> None:
    criteria = extract_criteria(_DESIGN_C2_BODY)

    assert [c.id for c in criteria] == ["AC1", "AC2", "AC3"]
    assert criteria[0].statement == (
        'The work table with no rows reads "Nothing queued", not an error.'
    )
    assert criteria[1].statement == (
        "`uv run pytest tests/test_serve_api_work.py` passes."
    )
    assert criteria[2].statement == (
        "`ruff check . && ruff format --check .` from `kew/`."
    )


def test_a_later_section_of_the_same_level_ends_the_criteria() -> None:
    criteria = extract_criteria(_CHECKLIST_BODY)

    assert [c.statement for c in criteria] == [
        "`kew plan show 2278` prints the criteria",
        "The migration test passes",
        "The audit chain still verifies",
    ]
    assert "Not a criterion." not in [c.statement for c in criteria]


def test_checkbox_state_is_stripped_from_the_statement() -> None:
    criteria = extract_criteria(_CHECKLIST_BODY)

    assert not any(c.statement.startswith("[") for c in criteria)


def test_extracts_numbered_items_in_body_order() -> None:
    criteria = extract_criteria(_NUMBERED_BODY)

    assert [c.id for c in criteria] == ["AC1", "AC2", "AC3"]
    assert criteria[1].statement == "A changed body supersedes the old row."


@pytest.mark.parametrize(
    "body",
    [
        "",
        "Just prose, no headings at all.",
        "## Change\n\n- Do the thing.\n",
        "# Acceptance\n\n- A level-1 heading is not a section heading.\n",
    ],
)
def test_no_acceptance_section_extracts_nothing(body: str) -> None:
    assert extract_criteria(body) == []


def test_a_heading_inside_a_code_fence_is_ignored() -> None:
    body = """### Change

```markdown
### Acceptance

- A template, not this item's contract.
```

- The real change bullet.
"""

    assert extract_criteria(body) == []


def test_items_inside_a_code_fence_within_the_section_are_ignored() -> None:
    body = """### Acceptance

- The real criterion.

```text
- Sample output, not a criterion.
```

- The second real criterion.
"""

    assert [c.statement for c in extract_criteria(body)] == [
        "The real criterion.",
        "The second real criterion.",
    ]


def test_a_deeper_heading_stays_inside_the_section() -> None:
    body = """## Acceptance

- Top-level criterion.

### Gates

- A gate is still a criterion.

## Rules

- Not a criterion.
"""

    assert [c.statement for c in extract_criteria(body)] == [
        "Top-level criterion.",
        "A gate is still a criterion.",
    ]


def test_nested_bullets_are_criteria_too_rather_than_silently_dropped() -> None:
    body = "### Acceptance\n\n- Extractor tests on real bodies:\n  - a checklist\n"

    assert [c.statement for c in extract_criteria(body)] == [
        "Extractor tests on real bodies:",
        "a checklist",
    ]


class TestSoftWrappedCriteria:
    """#2309 P5: a wrapped criterion is one statement, not just its first line.

    The recorded plan is the verbatim contract the ledger stores, ``kew plan
    show`` prints and the prompt presents, so dropping the rest of a wrapped
    bullet silently narrows what the diff is judged against.
    """

    def test_a_wrapped_bullet_keeps_the_whole_criterion(self) -> None:
        body = (
            "## Acceptance\n\n"
            '- The work table with no rows reads "Nothing queued",\n'
            "  not an error, and the copy says what to do next.\n"
        )

        assert [c.statement for c in extract_criteria(body)] == [
            'The work table with no rows reads "Nothing queued", not an error,'
            " and the copy says what to do next."
        ]

    def test_an_unindented_continuation_is_folded_in_too(self) -> None:
        """Markdown's lazy continuation: the wrap's indent is cosmetic."""
        body = "## Acceptance\n\n- A criterion that wraps\nwithout any indent.\n"

        assert [c.statement for c in extract_criteria(body)] == [
            "A criterion that wraps without any indent."
        ]

    def test_a_wrapped_checkbox_keeps_the_whole_criterion(self) -> None:
        body = "## Acceptance\n\n- [ ] `kew plan show 2278` prints\n  every criterion\n"

        assert [c.statement for c in extract_criteria(body)] == [
            "`kew plan show 2278` prints every criterion"
        ]

    def test_a_wrapped_nested_bullet_stays_its_own_criterion(self) -> None:
        body = (
            "## Acceptance\n\n"
            "- Extractor tests on real bodies:\n"
            "  - a checklist whose statement\n"
            "    runs on\n"
        )

        assert [c.statement for c in extract_criteria(body)] == [
            "Extractor tests on real bodies:",
            "a checklist whose statement runs on",
        ]

    def test_a_blank_line_closes_the_item_so_trailing_prose_is_not_absorbed(
        self,
    ) -> None:
        body = (
            "## Acceptance\n\n- The only criterion.\n\nA closing paragraph of prose.\n"
        )

        assert [c.statement for c in extract_criteria(body)] == ["The only criterion."]

    def test_a_deeper_heading_closes_the_item_it_follows(self) -> None:
        body = "## Acceptance\n\n- A criterion.\n### Gates\n- A gate.\n"

        assert [c.statement for c in extract_criteria(body)] == [
            "A criterion.",
            "A gate.",
        ]

    def test_prose_before_the_first_bullet_is_still_no_criterion(self) -> None:
        body = "## Acceptance\n\nThe diff must:\n\n- do the thing.\n"

        assert [c.statement for c in extract_criteria(body)] == ["do the thing."]

    def test_the_fold_changes_the_plan_sha(self) -> None:
        """A truncated statement is a different contract, so a different sha."""
        wrapped = "## Acceptance\n\n- One criterion\n  in two lines.\n"
        truncated = "## Acceptance\n\n- One criterion\n"

        assert plan_sha(extract_plan(wrapped)) != plan_sha(extract_plan(truncated))


def test_the_first_acceptance_section_wins() -> None:
    body = "## Acceptance\n\n- First.\n\n## Acceptance Criteria\n\n- Second.\n"

    assert [c.statement for c in extract_criteria(body)] == ["First."]


def test_extracted_criteria_default_to_behavior_with_no_invented_check() -> None:
    criterion = extract_criteria(_NUMBERED_BODY)[0]

    assert criterion.kind == "behavior"
    assert criterion.check is None


def test_extract_plan_fills_only_the_criteria() -> None:
    plan = extract_plan(_DESIGN_C2_BODY)

    assert isinstance(plan, ItemPlan)
    assert len(plan.acceptance_criteria) == 3
    assert (plan.summary, plan.problem, plan.approach) == (None, None, None)
    assert plan.affected_surfaces == []


def test_plan_sha_is_stable_and_statement_sensitive() -> None:
    first = plan_sha(extract_plan(_DESIGN_C2_BODY))

    assert first == plan_sha(extract_plan(_DESIGN_C2_BODY))
    assert first != plan_sha(extract_plan(_CHECKLIST_BODY))
    assert len(first) == 64


def test_body_digest_tracks_the_whole_body_not_just_the_criteria() -> None:
    edited = _DESIGN_C2_BODY.replace("The console's", "The web console's")

    assert body_digest(_DESIGN_C2_BODY) != body_digest(edited)
    assert plan_sha(extract_plan(_DESIGN_C2_BODY)) == plan_sha(extract_plan(edited))
